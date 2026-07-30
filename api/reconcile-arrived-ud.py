import json
import os
import time
import re
import cgi
import io
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler
import openpyxl
import jwt
import urllib.request
import urllib.parse
import urllib.error

# Reconcile Arrived - Universal Dist
#
# Replaces the blanket "Universal Dist - Shipment Arrived" button
# (mark-arrived.py) with an itemized, invoice-driven advancement, the same
# way reconcile-ud.py replaced the blanket "Shipped" button.
#
# Why this exists: mark-arrived.py advances EVERY Order Needs row currently
# at 'Shipped' for a supplier to 'Arrived', regardless of which invoice they
# came from. With overlapping UD invoices in flight (one already physically
# received, an earlier one still in transit), that blanket action would
# incorrectly mark still-in-transit units as arrived. This endpoint instead
# matches the uploaded arrival invoice's actual line items against Order
# Needs rows currently at 'Shipped', and only advances what the invoice
# actually covers.
#
# Inventory note: this endpoint does NOT post inventory adjustments.
# Physical on-hand inventory is credited when a UD invoice first advances
# units to 'Shipped' (see reconcile-ud.py's apply_received_inventory) - by
# design, in this system a UD invoice is normally the same document that
# both confirms shipment and reflects physical receipt. Since the units
# processed here were already counted at that point, adjusting inventory
# again here would double-count them. Surplus invoice lines that don't
# match any 'Shipped' Order Needs row (no outstanding order needs them) are
# flagged for manual inventory add instead of posted automatically - same
# as the existing surplus-handling precedent used elsewhere in this app.

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
SKUS_SHEET_ID = '1yC-oZ-0hD5ReTcOA9iTjTGC6mONbDUCpfbZZA9GrQtI'
ORDER_NEEDS_TAB = 'Order Needs'
ORDER_NEEDS_RANGE = f"'{ORDER_NEEDS_TAB}'!A2:H50000"
RECONCILE_TAB = 'Latest UD Arrival Reconciliation'
SUPPLIER = 'Universal Dist'
STAGE_ORDER = ['NotOrdered', 'Ordered', 'Shipped', 'Arrived']

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'

EMAIL_LIFECYCLE_TAGS = {
    'dhg-status-store-first-order', 'dhg-status-shop-first-order',
    'dhg-status-order-placed', 'dhg-status-preorder',
}

NON_PRODUCT_ITEM_NOS = {'41040'}
NON_PRODUCT_DESCRIPTIONS = {'2% cash discount reversal'}

RELEASE_DATE_TAG_RE = re.compile(r'^release-date-(\d{4}-\d{2}-\d{2})$')
HOLD_WINDOW_DAYS = 3

# ── Invoice parsing (identical to reconcile-ud.py) ───────────────────────────

def parse_ud_invoice(file_bytes):
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(h).strip() if h is not None else '' for h in rows[0]]

    def col(name):
        for i, h in enumerate(header):
            if h.lower() == name.lower():
                return i
        return None

    item_no_idx = col('Item No.')
    vendor_item_idx = col('Vendor Item No.')
    product_idx = col('Product')
    qty_idx = col('Quantity')
    if item_no_idx is None or vendor_item_idx is None or qty_idx is None:
        raise ValueError(f"Couldn't find expected columns in invoice header: {header}")

    items = []
    for row in rows[1:]:
        if row is None or all(c is None for c in row):
            continue
        item_no = str(row[item_no_idx]).strip() if row[item_no_idx] is not None else ''
        vendor_item = str(row[vendor_item_idx]).strip() if row[vendor_item_idx] is not None else ''
        product = str(row[product_idx]).strip() if (product_idx is not None and row[product_idx] is not None) else ''
        qty_raw = row[qty_idx] if qty_idx is not None else None
        if not vendor_item and not item_no:
            continue
        if item_no in NON_PRODUCT_ITEM_NOS:
            continue
        if product.strip().lower() in NON_PRODUCT_DESCRIPTIONS:
            continue
        try:
            qty = int(qty_raw)
        except (TypeError, ValueError):
            continue
        items.append({'barcode': item_no, 'sku': vendor_item, 'description': product, 'quantity': qty})
    return items

def aggregate_invoice_items(all_items):
    combined = {}
    for item in all_items:
        key = item['barcode'] or item['sku']
        if not key:
            continue
        if key not in combined:
            combined[key] = {'barcode': item['barcode'], 'sku': item['sku'], 'description': item['description'], 'quantity': 0}
        combined[key]['quantity'] += item['quantity']
    return combined

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

def load_ud_barcode_by_sku():
    rows = sheets_get(SKUS_SHEET_ID, "'Universal Dist'!A1:L")
    if not rows:
        return {}
    header = rows[0]
    sku_i = header.index('Variant SKU') if 'Variant SKU' in header else None
    bc_i = header.index('Barcode') if 'Barcode' in header else None
    if sku_i is None or bc_i is None:
        return {}
    out = {}
    for row in rows[1:]:
        sku = row[sku_i].strip() if len(row) > sku_i and row[sku_i] else ''
        barcode = row[bc_i].strip() if len(row) > bc_i and row[bc_i] else ''
        if sku and barcode:
            out[sku] = barcode
    return out

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
    """Same preorder-hold logic as mark-arrived.py: hold the normal
    'received' tag if any line item is a preorder whose release date is
    more than HOLD_WINDOW_DAYS out."""
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

# ── Cancellation / refund safety check (same logic as reconcile-ud.py) ───────

def reconcile_against_shopify(rows, touched_order_names):
    if not touched_order_names:
        return rows, set(), [], set()
    name_query = '(' + ' OR '.join(f'name:{n.lstrip("#")}' for n in touched_order_names) + ')'
    data = shopify_graphql('''
        query getOrders($q: String!) {
            orders(first: 250, query: $q) {
                edges { node { name cancelledAt displayFulfillmentStatus lineItems(first: 50) { edges { node { sku currentQuantity } } } } }
            }
        }
    ''', {'q': name_query})
    current_qty = {}
    fulfilled_or_cancelled_orders = set()
    for edge in data['orders']['edges']:
        node = edge['node']
        if node['cancelledAt'] or node['displayFulfillmentStatus'] == 'FULFILLED':
            fulfilled_or_cancelled_orders.add(node['name'])
            current_qty[node['name']] = {}
            continue
        qtys = {}
        for li in node['lineItems']['edges']:
            sku = li['node']['sku']
            qtys[sku] = qtys.get(sku, 0) + (li['node']['currentQuantity'] or 0)
        current_qty[node['name']] = qtys

    # Drop every row for a now-Fulfilled or now-Cancelled order outright -
    # these should never be advanced or matched against invoice quantities,
    # regardless of which SKU they're for. Mirrors mark-arrived.py's
    # removed_fulfilled_or_cancelled behavior, which the original blanket
    # button had but this invoice-driven rewrite initially omitted.
    fulfilled_or_cancelled_rows = {
        idx for idx, row in enumerate(rows)
        if row and row[0] and row[0] in fulfilled_or_cancelled_orders
    }
    rows = [r for i, r in enumerate(rows) if i not in fulfilled_or_cancelled_rows]

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
    return cleaned_rows, blocked_pairs, manual_review_flags, fulfilled_or_cancelled_orders

# ── Comparison logic: invoice vs rows currently 'Shipped' (awaiting arrival) ─

def load_awaiting_arrival_from_order_needs(order_needs_rows, barcode_by_sku, blocked_pairs=frozenset()):
    awaiting = {}  # sku -> {barcode, quantity, title, order_names(set)}
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
            awaiting[sku] = {'barcode': barcode_by_sku.get(sku, ''), 'quantity': 0, 'title': title, 'order_names': set()}
        awaiting[sku]['quantity'] += 1
        awaiting[sku]['order_names'].add(order_name)
    return awaiting

def run_comparison(awaiting_by_sku, invoice_items_by_key):
    awaiting = [
        {'sku': sku, 'barcode': v['barcode'], 'quantity': v['quantity'], 'title': v['title'],
         'order_names': ', '.join(sorted(v['order_names']))}
        for sku, v in awaiting_by_sku.items()
    ]
    invoice_items = list(invoice_items_by_key.values())

    a_matched = [False] * len(awaiting)
    inv_matched = [False] * len(invoice_items)
    pairs = []

    a_by_barcode = {}
    for i, a in enumerate(awaiting):
        if a['barcode']:
            a_by_barcode.setdefault(a['barcode'], i)
    for j, inv in enumerate(invoice_items):
        if inv['barcode'] and inv['barcode'] in a_by_barcode:
            i = a_by_barcode[inv['barcode']]
            if not a_matched[i]:
                pairs.append((i, j)); a_matched[i] = True; inv_matched[j] = True

    a_by_sku = {}
    for i, a in enumerate(awaiting):
        if not a_matched[i] and a['sku']:
            a_by_sku.setdefault(a['sku'], i)
    for j, inv in enumerate(invoice_items):
        if inv_matched[j] or not inv['sku']:
            continue
        i = a_by_sku.get(inv['sku'])
        if i is not None and not a_matched[i]:
            pairs.append((i, j)); a_matched[i] = True; inv_matched[j] = True

    results = []
    for i, j in pairs:
        a, inv = awaiting[i], invoice_items[j]
        if a['quantity'] == inv['quantity']:
            status = 'Match'
        elif inv['quantity'] > a['quantity']:
            status = 'More than awaiting arrival (surplus beyond what was shipped-pending)'
        else:
            status = 'Less than awaiting arrival (partial arrival?)'
        results.append([a['sku'] or inv['sku'], a['barcode'] or inv['barcode'],
            a['title'] or inv['description'], a['quantity'], inv['quantity'], status, a['order_names']])

    for i, a in enumerate(awaiting):
        if a_matched[i]:
            continue
        results.append([a['sku'], a['barcode'], a['title'], a['quantity'], 0,
            'Missing from this invoice - still awaiting arrival, not touched', a['order_names']])

    for j, inv in enumerate(invoice_items):
        if inv_matched[j]:
            continue
        results.append([inv['sku'], inv['barcode'], inv['description'], 0, inv['quantity'],
            'In invoice but nothing currently Shipped for it - surplus, needs manual inventory add', ''])

    results.sort(key=lambda r: (r[0] or '', r[1] or ''))
    return results

def arrived_qty_for_sku(row):
    sku, barcode, title, awaiting_qty, inv_qty, status, order_names = row
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

def reconciliation_key(row):
    barcode = row[1] if len(row) > 1 else ''
    sku = row[0] if len(row) > 0 else ''
    return sku or barcode

def load_existing_reconciliation():
    rows = sheets_get(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A2:G1000")
    existing = {}
    for row in rows:
        if not row or (not row[0] and (len(row) < 2 or not row[1])):
            continue
        padded = row + [''] * (7 - len(row))
        key = reconciliation_key(padded)
        if key:
            existing[key] = padded[:7]
    return existing

def merge_results(existing, new_results):
    CONFIRMED = {'Match', 'More than awaiting arrival (surplus beyond what was shipped-pending)'}
    merged = dict(existing)
    for row in new_results:
        key = reconciliation_key(row)
        if not key:
            continue
        prior = merged.get(key)
        if (row[5] == 'Missing from this invoice - still awaiting arrival, not touched' and prior is not None
                and len(prior) > 5 and prior[5] in CONFIRMED):
            continue
        merged[key] = row
    return [merged[k] for k in sorted(merged.keys())]

# ── Core handler logic (shared between dry-run and live) ────────────────────

def process_invoice(file_fields_bytes_and_names, dry_run):
    all_items = []
    invoices_parsed = []
    for filename, file_bytes in file_fields_bytes_and_names:
        items = parse_ud_invoice(file_bytes)
        all_items.extend(items)
        invoices_parsed.append({'filename': filename, 'lineItems': len(items)})

    invoice_items_by_key = aggregate_invoice_items(all_items)

    barcode_by_sku = load_ud_barcode_by_sku()
    order_needs_rows = sheets_get(AGG_SHEET_ID, ORDER_NEEDS_RANGE)

    candidate_order_names = sorted({
        row[0] for row in order_needs_rows
        if row and row[0] and len(row) >= 7 and row[3] == SUPPLIER and row[6] == 'Shipped'
    })
    order_needs_rows, blocked_pairs, manual_review_flags, removed_fulfilled_or_cancelled = reconcile_against_shopify(order_needs_rows, candidate_order_names)

    awaiting = load_awaiting_arrival_from_order_needs(order_needs_rows, barcode_by_sku, blocked_pairs)
    new_results = run_comparison(awaiting, invoice_items_by_key)

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
        header = [['SKU', 'Barcode', 'Title', 'Awaiting Arrival Qty', 'Invoice Qty', 'Status', 'Order Names']]
        sheets_clear(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A1:G1000")
        sheets_put(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A1:G1", header)
        if merged_results:
            sheets_put(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A2:G{len(merged_results)+1}", merged_results)

    return {
        'success': True,
        'dryRun': dry_run,
        'invoicesParsed': invoices_parsed,
        'itemsInInvoices': len(invoice_items_by_key),
        'skusCompared': len(new_results),
        ('unitsThatWouldAdvanceToArrived' if dry_run else 'unitsAdvancedToArrived'): advanced_count,
        ('ordersThatWouldBeFullyArrivedAndTagged' if dry_run else 'ordersFullyArrivedAndTagged'): (planned_tags if dry_run else tagged),
        'skippedAlreadyInventoryQueued': skipped_inventory_queued,
        'tagErrors': tag_errors,
        'results': new_results,
        'blockedByCancellationOrRefund': [{'order': o, 'sku': s} for o, s in sorted(blocked_pairs)],
        'removedFulfilledOrCancelled': sorted(removed_fulfilled_or_cancelled),
        'manualReviewFlagsRaised': manual_review_flags if dry_run else (manual_review_flags if manual_review_flags else []),
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
                self._send_json(400, {'error': 'Expected multipart/form-data with one or more invoice files'})
                return

            fs = cgi.FieldStorage(fp=io.BytesIO(body), headers=self.headers,
                environ={'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type})
            file_fields = fs['file'] if 'file' in fs else None
            if file_fields is None:
                self._send_json(400, {'error': 'No file field found in upload'})
                return
            if not isinstance(file_fields, list):
                file_fields = [file_fields]

            dry_run_raw = fs.getvalue('dry_run', 'false')
            dry_run = str(dry_run_raw).strip().lower() in ('1', 'true', 'yes', 'on')

            files = [(f.filename, f.file.read()) for f in file_fields]
            result = process_invoice(files, dry_run)
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
