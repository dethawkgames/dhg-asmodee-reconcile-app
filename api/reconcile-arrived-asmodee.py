import json
import os
import time
import re
import cgi
import io
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler
import pdfplumber
import jwt
import urllib.request
import urllib.parse
import urllib.error

# Reconcile Arrived - Asmodee
#
# Same fix as reconcile-arrived-ud.py, for Asmodee. Replaces the blanket
# "Asmodee - Shipment Arrived" button (mark-arrived.py) with an itemized,
# invoice-driven advancement: mark-arrived.py advances EVERY Order Needs row
# currently at 'Shipped' for a supplier to 'Arrived', regardless of which
# invoice they came from. With overlapping Asmodee invoices in flight (one
# already physically received, an earlier one still in transit), that
# blanket action would incorrectly mark still-in-transit units as arrived.
#
# Reuses the exact PDF parser from reconcile.py (parse_asmodee_quote - the
# name is legacy, it actually parses the Invoice format). Comparison is
# SKU-only, matching reconcile.py's existing behavior for this supplier (no
# barcode fallback pass - Asmodee invoices don't carry a reliable barcode
# column the way UD's export does).
#
# Inventory note: same reasoning as the UD version - this endpoint does NOT
# post inventory adjustments. Physical on-hand inventory is credited when an
# Asmodee invoice first advances units to 'Shipped' (see reconcile.py's
# apply_received_inventory). Units processed here were already counted at
# that point; adjusting inventory again here would double-count them.
# Surplus invoice lines with nothing at 'Shipped' are flagged for manual
# inventory add instead of posted automatically.

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
ORDER_NEEDS_TAB = 'Order Needs'
ORDER_NEEDS_RANGE = f"'{ORDER_NEEDS_TAB}'!A2:H50000"
RECONCILE_TAB = 'Latest Asmodee Arrival Reconciliation'
SUPPLIER = 'Asmodee'
STAGE_ORDER = ['NotOrdered', 'Ordered', 'Shipped', 'Arrived']

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'

EMAIL_LIFECYCLE_TAGS = {
    'dhg-status-store-first-order', 'dhg-status-shop-first-order',
    'dhg-status-order-placed', 'dhg-status-preorder',
}

RELEASE_DATE_TAG_RE = re.compile(r'^release-date-(\d{4}-\d{2}-\d{2})$')
HOLD_WINDOW_DAYS = 3

# ── PDF parsing (identical to reconcile.py - same validated column positions) ─

SKU_X = 43.7
DESC_X = 100
GTIN_X_MIN = 240
GTIN_X_MAX = 260
QTY_X_MIN = 320
QTY_X_MAX = 345

def parse_asmodee_invoice(file_bytes):
    """Same parser as reconcile.py's parse_asmodee_quote (that name is
    legacy - it actually parses the Invoice / shipment-confirmation format,
    not the pre-shipment Sales Quote)."""
    line_items = []
    hit_subtotal = False
    header_seen = False
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            if hit_subtotal:
                break
            words = page.extract_words()
            lines = {}
            for w in words:
                top_key = round(w['top'])
                lines.setdefault(top_key, []).append(w)
            sorted_tops = sorted(lines.keys())
            current_item = None
            for top in sorted_tops:
                row_words = sorted(lines[top], key=lambda w: w['x0'])
                row_text = ' '.join(w['text'] for w in row_words)
                if row_text.strip().startswith('Subtotal'):
                    hit_subtotal = True
                    break
                if row_text.strip().startswith('No.') and 'Description' in row_text:
                    header_seen = True
                    continue
                if not header_seen:
                    continue
                if row_text.strip().startswith('Home Page'):
                    continue
                gtin_word = next((w for w in row_words if GTIN_X_MIN <= w['x0'] <= GTIN_X_MAX), None)
                qty_word = next((w for w in row_words if QTY_X_MIN <= w['x0'] <= QTY_X_MAX), None)
                sku_word = next((w for w in row_words if abs(w['x0'] - SKU_X) < 2), None)
                if gtin_word and qty_word and sku_word:
                    if current_item:
                        line_items.append(current_item)
                    desc_words = [w['text'] for w in row_words if w['x0'] >= DESC_X and w['x0'] < 180]
                    qty_val = qty_word['text']
                    current_item = {
                        'sku': sku_word['text'],
                        'barcode': gtin_word['text'],
                        'description': ' '.join(desc_words),
                        'quantity': int(qty_val) if qty_val.isdigit() else qty_val,
                    }
                elif not gtin_word and not qty_word and current_item is not None:
                    if sku_word:
                        fragment = sku_word['text']
                        if len(fragment) <= 6 and fragment.isalnum():
                            current_item['sku'] = current_item['sku'] + fragment
                    desc_words = [w['text'] for w in row_words
                                  if w is not sku_word and w['x0'] >= DESC_X and w['x0'] < 180]
                    if desc_words:
                        current_item['description'] = (current_item['description'] + ' ' + ' '.join(desc_words)).strip()
            if current_item:
                line_items.append(current_item)
    return line_items

# ── Google Sheets auth + access ──────────────────────────────────────────────

def get_google_token(scope='https://www.googleapis.com/auth/spreadsheets'):
    sa_email = os.environ['GOOGLE_SA_EMAIL']
    raw_key = os.environ.get('GOOGLE_SA_PRIVATE_KEY_B64') or os.environ.get('GOOGLE_SA_PRIVATE_KEY', '')
    if os.environ.get('GOOGLE_SA_PRIVATE_KEY_B64'):
        import base64
        sa_key = base64.b64decode(raw_key).decode('utf-8')
    else:
        sa_key = raw_key.replace('\\n', '\n')
    now = int(time.time())
    payload = {'iss': sa_email, 'scope': scope, 'aud': 'https://oauth2.googleapis.com/token', 'exp': now + 3600, 'iat': now}
    assertion = jwt.encode(payload, sa_key, algorithm='RS256')
    data = urllib.parse.urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion}).encode()
    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data, method='POST')
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())['access_token']

def sheets_get(spreadsheet_id, range_str):
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str)}'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read()).get('values', [])

def sheets_put(spreadsheet_id, range_str, values):
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str)}?valueInputOption=RAW'
    body = json.dumps({'values': values}).encode()
    req = urllib.request.Request(url, data=body, method='PUT', headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def sheets_clear(spreadsheet_id, range_str):
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str)}:clear'
    req = urllib.request.Request(url, data=b'', method='POST', headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def sheets_append(spreadsheet_id, range_str, values):
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str)}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS'
    body = json.dumps({'values': values}).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def ensure_reconcile_tab_exists():
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{AGG_SHEET_ID}?fields=sheets.properties.title'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    titles = [s['properties']['title'] for s in result['sheets']]
    if RECONCILE_TAB not in titles:
        body = json.dumps({'requests': [{'addSheet': {'properties': {'title': RECONCILE_TAB}}}]}).encode()
        req = urllib.request.Request(f'https://sheets.googleapis.com/v4/spreadsheets/{AGG_SHEET_ID}:batchUpdate',
            data=body, method='POST', headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as resp:
            json.loads(resp.read())

# ── Shopify auth + tagging ───────────────────────────────────────────────────

def get_shopify_token():
    data = urllib.parse.urlencode({
        'grant_type': 'client_credentials',
        'client_id': os.environ['SHOPIFY_CLIENT_ID'],
        'client_secret': os.environ['SHOPIFY_CLIENT_SECRET'],
    }).encode()
    req = urllib.request.Request(f'https://{SHOPIFY_SHOP}/admin/oauth/access_token', data=data, method='POST')
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())['access_token']

def shopify_graphql(query, variables=None):
    token = get_shopify_token()
    body = json.dumps({'query': query, 'variables': variables or {}}).encode()
    req = urllib.request.Request(f'https://{SHOPIFY_SHOP}/admin/api/{SHOPIFY_API_VERSION}/graphql.json',
        data=body, method='POST', headers={'Content-Type': 'application/json', 'X-Shopify-Access-Token': token})
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    if result.get('errors'):
        raise Exception(f"Shopify GraphQL errors: {result['errors']}")
    return result['data']

def get_order_details(order_name):
    """Returns (id, current_status, displayFulfillmentStatus, cancelledAt, lineItems)."""
    data = shopify_graphql('''
        query getOrder($q: String!) {
            orders(first: 1, query: $q) {
                edges {
                    node {
                        id name tags displayFulfillmentStatus cancelledAt
                        lineItems(first: 50) {
                            edges { node { sku currentQuantity product { tags } } }
                        }
                    }
                }
            }
        }
    ''', {'q': f'name:{order_name}'})
    edges = data['orders']['edges']
    if not edges:
        return None, None, None, None, []
    node = edges[0]['node']
    current_tag = next((t for t in node['tags'] if t.startswith('dhg-status-') and t not in EMAIL_LIFECYCLE_TAGS), None)
    current_status = current_tag.replace('dhg-status-', '') if current_tag else None
    line_items = [edge['node'] for edge in node['lineItems']['edges']]
    return node['id'], current_status, node['displayFulfillmentStatus'], node['cancelledAt'], line_items

def apply_completion_tag(order_id, tag):
    shopify_graphql('''
        mutation tagsAdd($id: ID!, $tags: [String!]!) { tagsAdd(id: $id, tags: $tags) { userErrors { field message } } }
    ''', {'id': order_id, 'tags': [tag]})

def remove_status_tag(order_id, tag):
    shopify_graphql('''
        mutation tagsRemove($id: ID!, $tags: [String!]!) { tagsRemove(id: $id, tags: $tags) { userErrors { field message } } }
    ''', {'id': order_id, 'tags': [tag]})

def determine_arrived_tag(line_items):
    """Same preorder-hold logic as mark-arrived.py."""
    latest_release_date = None
    for item in line_items:
        tags = [t.lower() for t in (item.get('product', {}).get('tags') or [])]
        if 'preorder' not in tags:
            continue
        for t in item['product']['tags']:
            m = RELEASE_DATE_TAG_RE.match(t.strip())
            if m:
                try:
                    d = datetime.strptime(m.group(1), '%Y-%m-%d').replace(tzinfo=timezone.utc)
                    if latest_release_date is None or d > latest_release_date:
                        latest_release_date = d
                except ValueError:
                    continue
    if latest_release_date is None:
        return 'order-received'
    if latest_release_date - datetime.now(timezone.utc) > timedelta(days=HOLD_WINDOW_DAYS):
        return 'order-received-preorder'
    return 'order-received'

# ── Cancellation / refund safety check (same logic as reconcile.py) ─────────

def reconcile_against_shopify(rows, touched_order_names):
    if not touched_order_names:
        return rows, set(), []
    name_query = '(' + ' OR '.join(f'name:{n.lstrip("#")}' for n in touched_order_names) + ')'
    data = shopify_graphql('''
        query getOrders($q: String!) {
            orders(first: 250, query: $q) {
                edges { node { name cancelledAt lineItems(first: 50) { edges { node { sku currentQuantity } } } } }
            }
        }
    ''', {'q': name_query})
    current_qty = {}
    for edge in data['orders']['edges']:
        node = edge['node']
        if node['cancelledAt']:
            current_qty[node['name']] = {}
            continue
        qtys = {}
        for li in node['lineItems']['edges']:
            sku = li['node']['sku']
            qtys[sku] = qtys.get(sku, 0) + (li['node']['currentQuantity'] or 0)
        current_qty[node['name']] = qtys
    by_pair = {}
    for idx, row in enumerate(rows):
        if not row or not row[0]:
            continue
        by_pair.setdefault((row[0], row[1]), []).append(idx)
    to_delete = set()
    blocked_pairs = set()
    manual_review_flags = []
    today = time.strftime('%Y-%m-%d')
    for (order_name, sku), idxs in by_pair.items():
        if order_name not in current_qty:
            continue
        actual = current_qty[order_name].get(sku, 0)
        existing = len(idxs)
        if actual >= existing:
            continue
        excess = existing - actual
        unlocked = [i for i in idxs if not rows[i][5]]
        if len(unlocked) >= excess:
            for i in sorted(unlocked, key=lambda i: -int(rows[i][4]))[:excess]:
                to_delete.add(i)
        else:
            blocked_pairs.add((order_name, sku))
            manual_review_flags.append([
                order_name, sku, rows[idxs[0]][2], '',
                f'Shopify qty dropped to {actual} but {existing} Order Needs rows exist '
                f'and only {len(unlocked)} are unlocked - {excess - len(unlocked)} committed '
                f'unit(s) need manual reconciliation (cancellation/refund detected)',
                today,
            ])
    cleaned_rows = [r for i, r in enumerate(rows) if i not in to_delete]
    return cleaned_rows, blocked_pairs, manual_review_flags

# ── Comparison logic: invoice vs rows currently 'Shipped' (awaiting arrival) ─
# SKU-only, matching reconcile.py's existing Asmodee comparison (no barcode
# fallback pass - kept for parity with the Shipped-stage script).

def load_awaiting_arrival_from_order_needs(order_needs_rows, blocked_pairs=frozenset()):
    awaiting = {}  # sku -> {quantity, title, order_names(set)}
    for row in order_needs_rows:
        if not row or not row[0]:
            continue
        row = row + [''] * (8 - len(row))
        order_name, sku, title, supplier, unit, sup_id, stage, updated = row
        if supplier != SUPPLIER or stage != 'Shipped':
            continue
        if (order_name, sku) in blocked_pairs:
            continue
        if sku not in awaiting:
            awaiting[sku] = {'quantity': 0, 'title': title, 'order_names': set()}
        awaiting[sku]['quantity'] += 1
        awaiting[sku]['order_names'].add(order_name)
    return awaiting

def run_comparison(awaiting, invoice_items):
    invoiced = {}
    for item in invoice_items:
        sku = (item.get('sku') or '').strip()
        if not sku:
            continue
        qty = item['quantity'] if isinstance(item['quantity'], int) else 0
        invoiced[sku] = {'quantity': qty, 'description': item.get('description', '')}

    results = []
    all_skus = set(awaiting.keys()) | set(invoiced.keys())
    for sku in sorted(all_skus):
        a = awaiting.get(sku)
        inv = invoiced.get(sku)
        order_names = ', '.join(sorted(a['order_names'])) if a else ''

        if a and inv:
            if a['quantity'] == inv['quantity']:
                status = 'Match'
            elif inv['quantity'] > a['quantity']:
                status = 'More than awaiting arrival (surplus beyond what was shipped-pending)'
            else:
                status = 'Less than awaiting arrival (partial arrival?)'
            results.append([sku, a['title'] or inv['description'], a['quantity'], inv['quantity'], status, order_names])
        elif a and not inv:
            results.append([sku, a['title'], a['quantity'], 0, 'Missing from this invoice - still awaiting arrival, not touched', order_names])
        elif inv and not a:
            results.append([sku, inv['description'], 0, inv['quantity'],
                'In invoice but nothing currently Shipped for it - surplus, needs manual inventory add', ''])

    return results

def arrived_qty_for_sku(row):
    sku, title, awaiting_qty, inv_qty, status, order_names = row
    if status in ('Match', 'More than awaiting arrival (surplus beyond what was shipped-pending)'):
        return awaiting_qty
    if status == 'Less than awaiting arrival (partial arrival?)':
        return inv_qty
    return 0

def sort_key(supplier_order_id):
    if supplier_order_id.endswith('-PREEXISTING'):
        return (0, supplier_order_id)
    return (1, supplier_order_id)

def advance_arrived_stage(order_needs_rows, comparison_results, blocked_pairs=frozenset()):
    to_advance = {row[0]: arrived_qty_for_sku(row) for row in comparison_results if arrived_qty_for_sku(row) > 0}
    if not to_advance:
        return order_needs_rows, set(), 0

    by_sku = {}
    for idx, row in enumerate(order_needs_rows):
        if not row or not row[0]:
            continue
        row = row + [''] * (8 - len(row))
        order_needs_rows[idx] = row
        order_name, sku, title, supplier, unit, sup_id, stage, updated = row
        if supplier != SUPPLIER or stage != 'Shipped':
            continue
        if (order_name, sku) in blocked_pairs:
            continue
        by_sku.setdefault(sku, []).append(idx)

    today = time.strftime('%Y-%m-%d')
    advanced_count = 0
    touched_orders = set()
    for sku, qty_to_advance in to_advance.items():
        candidates = by_sku.get(sku, [])
        candidates.sort(key=lambda idx: sort_key(order_needs_rows[idx][5]))
        for idx in candidates[:qty_to_advance]:
            order_needs_rows[idx][6] = 'Arrived'
            order_needs_rows[idx][7] = today
            touched_orders.add(order_needs_rows[idx][0])
            advanced_count += 1

    return order_needs_rows, touched_orders, advanced_count

# ── Merge logic for the display/audit tab ────────────────────────────────────

def load_existing_reconciliation():
    rows = sheets_get(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A2:F1000")
    existing = {}
    for row in rows:
        if not row or not row[0]:
            continue
        sku = row[0].strip()
        padded = row + [''] * (6 - len(row))
        existing[sku] = padded[:6]
    return existing

def merge_results(existing, new_results):
    CONFIRMED = {'Match', 'More than awaiting arrival (surplus beyond what was shipped-pending)'}
    merged = dict(existing)
    for row in new_results:
        sku = row[0]
        prior = merged.get(sku)
        if (row[4] == 'Missing from this invoice - still awaiting arrival, not touched' and prior is not None
                and len(prior) > 4 and prior[4] in CONFIRMED):
            continue
        merged[sku] = row
    return [merged[sku] for sku in sorted(merged.keys())]

# ── Core handler logic (shared between dry-run and live) ────────────────────

def process_invoice(file_bytes, dry_run):
    invoice_items = parse_asmodee_invoice(file_bytes)

    order_needs_rows = sheets_get(AGG_SHEET_ID, ORDER_NEEDS_RANGE)

    candidate_order_names = sorted({
        row[0] for row in order_needs_rows
        if row and row[0] and len(row) >= 7 and row[3] == SUPPLIER and row[6] == 'Shipped'
    })
    order_needs_rows, blocked_pairs, manual_review_flags = reconcile_against_shopify(order_needs_rows, candidate_order_names)

    awaiting = load_awaiting_arrival_from_order_needs(order_needs_rows, blocked_pairs)
    new_results = run_comparison(awaiting, invoice_items)

    updated_rows, touched_orders, advanced_count = advance_arrived_stage(order_needs_rows, new_results, blocked_pairs)

    if not dry_run and manual_review_flags:
        sheets_append(AGG_SHEET_ID, "'Needs Manual Review'!A2:F1000", manual_review_flags)

    if not dry_run:
        sheets_clear(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
        if updated_rows:
            sheets_put(AGG_SHEET_ID, f"'{ORDER_NEEDS_TAB}'!A2:H{len(updated_rows) + 1}", updated_rows)

    rows_by_order = {}
    for row in updated_rows:
        if row and row[0]:
            rows_by_order.setdefault(row[0], []).append(row)

    tagged, planned_tags, tag_errors, skipped_inventory_queued = [], [], [], []
    for order_name in touched_orders:
        order_rows = rows_by_order.get(order_name, [])
        fully_arrived = all(STAGE_ORDER.index(r[6]) >= STAGE_ORDER.index('Arrived') for r in order_rows)
        if not fully_arrived:
            continue
        try:
            order_id, current_status, fulfillment, cancelled_at, line_items = get_order_details(order_name)
            if not order_id:
                tag_errors.append({'order': order_name, 'error': 'Order not found in Shopify'})
                continue
            if current_status == 'inventory-queued':
                skipped_inventory_queued.append(order_name)
                continue
            tag_suffix = determine_arrived_tag(line_items)
            tag = f'dhg-status-{tag_suffix}'
            if dry_run:
                planned_tags.append({'order': order_name, 'wouldRemove': f'dhg-status-{current_status}' if current_status else None, 'wouldApply': tag})
            else:
                if current_status:
                    remove_status_tag(order_id, f'dhg-status-{current_status}')
                apply_completion_tag(order_id, tag)
                tagged.append({'order': order_name, 'tag': tag})
        except Exception as e:
            tag_errors.append({'order': order_name, 'error': str(e)})

    if not dry_run:
        ensure_reconcile_tab_exists()
        existing = load_existing_reconciliation()
        merged_results = merge_results(existing, new_results)
        header = [['Shopify SKU', 'Title', 'Awaiting Arrival Qty', 'Invoice Qty', 'Status', 'Order Names']]
        sheets_clear(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A1:F1000")
        sheets_put(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A1:F1", header)
        if merged_results:
            sheets_put(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A2:F{len(merged_results)+1}", merged_results)

    return {
        'success': True,
        'dryRun': dry_run,
        'itemsInInvoice': len(invoice_items),
        'skusCompared': len(new_results),
        ('unitsThatWouldAdvanceToArrived' if dry_run else 'unitsAdvancedToArrived'): advanced_count,
        ('ordersThatWouldBeFullyArrivedAndTagged' if dry_run else 'ordersFullyArrivedAndTagged'): (planned_tags if dry_run else tagged),
        'skippedAlreadyInventoryQueued': skipped_inventory_queued,
        'tagErrors': tag_errors,
        'results': new_results,
        'blockedByCancellationOrRefund': [{'order': o, 'sku': s} for o, s in sorted(blocked_pairs)],
        'manualReviewFlagsRaised': manual_review_flags,
        'note': 'No Shopify inventory was adjusted by this endpoint - see file header comment for why.' if not dry_run else
                'DRY RUN: no writes were made to the Order Needs sheet, the reconciliation tab, the Needs Manual Review tab, or Shopify tags.',
    }

# ── HTTP handler ─────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_type = self.headers.get('Content-Type', '')
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)

            if 'multipart/form-data' not in content_type:
                self._send_json(400, {'error': 'Expected multipart/form-data with a PDF file'})
                return

            fs = cgi.FieldStorage(fp=io.BytesIO(body), headers=self.headers,
                environ={'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type})
            if 'file' not in fs:
                self._send_json(400, {'error': 'No file field found in upload'})
                return

            file_bytes = fs['file'].file.read()
            dry_run_raw = fs.getvalue('dry_run', 'false')
            dry_run = str(dry_run_raw).strip().lower() in ('1', 'true', 'yes', 'on')

            result = process_invoice(file_bytes, dry_run)
            self._send_json(200, result)
        except Exception as e:
            import traceback
            self._send_json(500, {'error': str(e), 'trace': traceback.format_exc()})

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _send_json(self, status, data):
        self.send_response(status)
        self._cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
