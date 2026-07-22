import json
import os
import time
import cgi
import io
from http.server import BaseHTTPRequestHandler
import pdfplumber
import jwt
import urllib.request
import urllib.parse
import urllib.error

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
ORDER_NEEDS_TAB = 'Order Needs'
ORDER_NEEDS_RANGE = f"'{ORDER_NEEDS_TAB}'!A2:H50000"
RECONCILE_TAB = 'Latest Reconciliation'
SUPPLIER = 'Asmodee'

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'

EMAIL_LIFECYCLE_TAGS = {
    'dhg-status-store-first-order', 'dhg-status-shop-first-order',
    'dhg-status-order-placed', 'dhg-status-preorder',
}

# ── PDF parsing (unchanged from v1 - already validated) ──────────────────────

# ── PDF parsing (Invoice format - column positions verified against real
# Asmodee invoice PDFs, NOT the Quote format, which has different column
# positions entirely) ────────────────────────────────────────────────────────

SKU_X = 43.7
DESC_X = 100  # was 102.8, which sat just above real word positions (~102.757)
              # for the first description word, silently dropping it
GTIN_X_MIN = 240
GTIN_X_MAX = 260
QTY_X_MIN = 320
QTY_X_MAX = 345

def parse_asmodee_quote(file_bytes):
    """Despite the name (kept for compatibility with the rest of this file),
    this parses the Asmodee INVOICE format - the shipment-confirmation
    document, not the pre-shipment Sales Quote. Column positions differ
    significantly between the two document types."""
    line_items = []
    hit_subtotal = False
    header_seen = False  # persists across pages - the header row only appears once
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
    sa_key = os.environ['GOOGLE_SA_PRIVATE_KEY'].replace('\\n', '\n')
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

def get_order_id_and_current_status(order_name):
    data = shopify_graphql('''
        query getOrder($q: String!) { orders(first: 1, query: $q) { edges { node { id name tags } } } }
    ''', {'q': f'name:{order_name}'})
    edges = data['orders']['edges']
    if not edges:
        return None, None
    node = edges[0]['node']
    current_tag = next((t for t in node['tags'] if t.startswith('dhg-status-') and t not in EMAIL_LIFECYCLE_TAGS), None)
    return node['id'], (current_tag.replace('dhg-status-', '') if current_tag else None)

def apply_completion_tag(order_id, tag):
    shopify_graphql('''
        mutation tagsAdd($id: ID!, $tags: [String!]!) { tagsAdd(id: $id, tags: $tags) { userErrors { field message } } }
    ''', {'id': order_id, 'tags': [tag]})

def remove_status_tag(order_id, tag):
    shopify_graphql('''
        mutation tagsRemove($id: ID!, $tags: [String!]!) { tagsRemove(id: $id, tags: $tags) { userErrors { field message } } }
    ''', {'id': order_id, 'tags': [tag]})

# ── Cancellation / refund safety check (same logic as lock-supplier-order.py) ─

def reconcile_against_shopify(rows, touched_order_names):
    if not touched_order_names:
        return rows, set()
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
    if manual_review_flags:
        sheets_append(AGG_SHEET_ID, "'Needs Manual Review'!A2:F1000", manual_review_flags)
    cleaned_rows = [r for i, r in enumerate(rows) if i not in to_delete]
    return cleaned_rows, blocked_pairs

# ── Comparison logic ─────────────────────────────────────────────────────────
# "Submitted" now means: currently-Ordered (locked, not yet shipped) Order
# Needs rows for this supplier, aggregated by SKU - regardless of which
# Supplier Order ID they belong to. This replaces reading the old Asmodee
# Order tab, which mixed in demand that hadn't even been submitted yet.

def load_submitted_from_order_needs(order_needs_rows, blocked_pairs=frozenset()):
    submitted = {}  # sku -> {quantity, title, order_names(set)}
    for row in order_needs_rows:
        if not row or not row[0]:
            continue
        row = row + [''] * (8 - len(row))
        order_name, sku, title, supplier, unit, sup_id, stage, updated = row
        if supplier != SUPPLIER or stage != 'Ordered':
            continue
        if (order_name, sku) in blocked_pairs:
            continue
        if sku not in submitted:
            submitted[sku] = {'quantity': 0, 'title': title, 'order_names': set()}
        submitted[sku]['quantity'] += 1
        submitted[sku]['order_names'].add(order_name)
    return submitted

def run_comparison(submitted, quote_items):
    quoted = {}
    for item in quote_items:
        sku = (item.get('sku') or '').strip()
        if not sku:
            continue
        qty = item['quantity'] if isinstance(item['quantity'], int) else 0
        quoted[sku] = {'quantity': qty, 'description': item.get('description', '')}

    results = []
    all_skus = set(submitted.keys()) | set(quoted.keys())
    for sku in sorted(all_skus):
        sub = submitted.get(sku)
        quo = quoted.get(sku)
        order_names = ', '.join(sorted(sub['order_names'])) if sub else ''

        if sub and quo:
            if sub['quantity'] == quo['quantity']:
                status = 'Match'
            elif quo['quantity'] > sub['quantity']:
                status = 'More than submitted (likely preorder/backorder)'
            else:
                status = 'Less than submitted (partial shipment?)'
            results.append([sku, sub['title'] or quo['description'], sub['quantity'], quo['quantity'], status, order_names])
        elif sub and not quo:
            results.append([sku, sub['title'], sub['quantity'], 0, 'Missing from quote entirely - needs review', order_names])
        elif quo and not sub:
            results.append([sku, quo['description'], 0, quo['quantity'], 'In quote but not submitted - needs review', ''])

    return results

# A SKU whose invoice quantity is >= what's currently Ordered counts as fully
# shipped for advancement purposes; "Less than submitted" only advances the
# invoiced quantity, leaving the remainder at Ordered (a genuine short-ship).
def shipped_qty_for_sku(row):
    sku, title, sub_qty, quo_qty, status, order_names = row
    if status in ('Match', 'More than submitted (likely preorder/backorder)'):
        return sub_qty
    if status == 'Less than submitted (partial shipment?)':
        return quo_qty
    return 0

# ── Advance Order Needs rows to 'Shipped' ────────────────────────────────────
# Oldest Supplier Order ID first (PREEXISTING backlog counts as oldest), so a
# short-ship correctly drains the longest-outstanding commitment first.

def sort_key(supplier_order_id):
    if supplier_order_id.endswith('-PREEXISTING'):
        return (0, supplier_order_id)
    return (1, supplier_order_id)

def advance_shipped_stage(order_needs_rows, comparison_results, blocked_pairs=frozenset()):
    to_advance = {row[0]: shipped_qty_for_sku(row) for row in comparison_results if shipped_qty_for_sku(row) > 0}
    if not to_advance:
        return order_needs_rows, set(), 0

    # Index Ordered rows for this supplier by SKU, sorted oldest-ID-first
    by_sku = {}
    for idx, row in enumerate(order_needs_rows):
        if not row or not row[0]:
            continue
        row = row + [''] * (8 - len(row))
        order_needs_rows[idx] = row
        order_name, sku, title, supplier, unit, sup_id, stage, updated = row
        if supplier != SUPPLIER or stage != 'Ordered':
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
            order_needs_rows[idx][6] = 'Shipped'
            order_needs_rows[idx][7] = today
            touched_orders.add(order_needs_rows[idx][0])
            advanced_count += 1

    return order_needs_rows, touched_orders, advanced_count

# ── Merge logic for the display/audit tab (unchanged from v1) ───────────────

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
    CONFIRMED = {'Match', 'More than submitted (likely preorder/backorder)'}
    merged = dict(existing)
    for row in new_results:
        sku = row[0]
        prior = merged.get(sku)
        if (row[4] == 'Missing from quote entirely - needs review' and prior is not None
                and len(prior) > 4 and prior[4] in CONFIRMED):
            continue
        merged[sku] = row
    return [merged[sku] for sku in sorted(merged.keys())]

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
            quote_items = parse_asmodee_quote(file_bytes)

            order_needs_rows = sheets_get(AGG_SHEET_ID, ORDER_NEEDS_RANGE)

            # Safety check: re-verify against Shopify before advancing anything
            candidate_order_names = sorted({
                row[0] for row in order_needs_rows
                if row and row[0] and len(row) >= 7 and row[3] == SUPPLIER and row[6] == 'Ordered'
            })
            order_needs_rows, blocked_pairs = reconcile_against_shopify(order_needs_rows, candidate_order_names)

            submitted = load_submitted_from_order_needs(order_needs_rows, blocked_pairs)
            new_results = run_comparison(submitted, quote_items)

            # Advance Order Needs, write it back (clear first - the safety
            # check above may have shrunk the row count)
            updated_rows, touched_orders, advanced_count = advance_shipped_stage(order_needs_rows, new_results, blocked_pairs)
            sheets_clear(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
            if updated_rows:
                sheets_put(AGG_SHEET_ID, f"'{ORDER_NEEDS_TAB}'!A2:H{len(updated_rows) + 1}", updated_rows)

            # Apply dhg-shipped-from-supplier to any touched order whose
            # EVERY needed unit (across every supplier) is now Shipped-or-beyond
            STAGE_ORDER = ['NotOrdered', 'Ordered', 'Shipped', 'Arrived']
            rows_by_order = {}
            for row in updated_rows:
                if row and row[0]:
                    rows_by_order.setdefault(row[0], []).append(row)
            tagged, tag_errors, skipped_inventory_queued = [], [], []
            for order_name in touched_orders:
                order_rows = rows_by_order.get(order_name, [])
                fully_shipped = all(STAGE_ORDER.index(r[6]) >= STAGE_ORDER.index('Shipped') for r in order_rows)
                if not fully_shipped:
                    continue
                try:
                    order_id, current_status = get_order_id_and_current_status(order_name)
                    if not order_id:
                        tag_errors.append({'order': order_name, 'error': 'Order not found in Shopify'})
                        continue
                    if current_status == 'inventory-queued':
                        skipped_inventory_queued.append(order_name)
                        continue
                    if current_status:
                        remove_status_tag(order_id, f'dhg-status-{current_status}')
                    apply_completion_tag(order_id, 'dhg-shipped-from-supplier')
                    tagged.append(order_name)
                except Exception as e:
                    tag_errors.append({'order': order_name, 'error': str(e)})

            # Update the audit/display tab
            ensure_reconcile_tab_exists()
            existing = load_existing_reconciliation()
            merged_results = merge_results(existing, new_results)
            header = [['Shopify SKU', 'Title', 'Ordered Qty', 'Quoted Qty', 'Status', 'Order Names']]
            sheets_clear(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A1:F1000")
            sheets_put(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A1:F1", header)
            if merged_results:
                sheets_put(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A2:F{len(merged_results)+1}", merged_results)

            self._send_json(200, {
                'success': True,
                'itemsInQuote': len(quote_items),
                'skusCompared': len(new_results),
                'unitsAdvancedToShipped': advanced_count,
                'ordersFullyShippedAndTagged': tagged,
                'skippedAlreadyInventoryQueued': skipped_inventory_queued,
                'tagErrors': tag_errors,
                'results': new_results,
                'blockedByCancellationOrRefund': [{'order': o, 'sku': s} for o, s in sorted(blocked_pairs)],
            })
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
