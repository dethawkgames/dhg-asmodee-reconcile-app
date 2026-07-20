import json
import os
import time
import cgi
import io
from http.server import BaseHTTPRequestHandler
import openpyxl
import jwt
import urllib.request
import urllib.parse
import urllib.error

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
SKUS_SHEET_ID = '1yC-oZ-0hD5ReTcOA9iTjTGC6mONbDUCpfbZZA9GrQtI'
ORDER_NEEDS_TAB = 'Order Needs'
ORDER_NEEDS_RANGE = f"'{ORDER_NEEDS_TAB}'!A2:H50000"
RECONCILE_TAB = 'Latest UD Reconciliation'
SUPPLIER = 'Universal Dist'

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'

EMAIL_LIFECYCLE_TAGS = {
    'dhg-status-store-first-order', 'dhg-status-shop-first-order',
    'dhg-status-order-placed', 'dhg-status-preorder',
}

NON_PRODUCT_ITEM_NOS = {'41040'}
NON_PRODUCT_DESCRIPTIONS = {'2% cash discount reversal'}

# ── Invoice parsing (unchanged from v1) ───────────────────────────────────────

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

# ── Comparison logic (submitted now from Order Needs, not the Order tab) ────

def load_submitted_from_order_needs(order_needs_rows, barcode_by_sku):
    submitted = {}  # sku -> {barcode, quantity, title, order_names(set)}
    for row in order_needs_rows:
        if not row or not row[0]:
            continue
        row = row + [''] * (8 - len(row))
        order_name, sku, title, supplier, unit, sup_id, stage, updated = row
        if supplier != SUPPLIER or stage != 'Ordered':
            continue
        if sku not in submitted:
            submitted[sku] = {'barcode': barcode_by_sku.get(sku, ''), 'quantity': 0, 'title': title, 'order_names': set()}
        submitted[sku]['quantity'] += 1
        submitted[sku]['order_names'].add(order_name)
    return submitted

def run_comparison(submitted_by_sku, invoice_items_by_key):
    submitted = [
        {'sku': sku, 'barcode': v['barcode'], 'quantity': v['quantity'], 'title': v['title'],
         'order_names': ', '.join(sorted(v['order_names']))}
        for sku, v in submitted_by_sku.items()
    ]
    invoice_items = list(invoice_items_by_key.values())

    sub_matched = [False] * len(submitted)
    inv_matched = [False] * len(invoice_items)
    pairs = []

    sub_by_barcode = {}
    for i, s in enumerate(submitted):
        if s['barcode']:
            sub_by_barcode.setdefault(s['barcode'], i)
    for j, inv in enumerate(invoice_items):
        if inv['barcode'] and inv['barcode'] in sub_by_barcode:
            i = sub_by_barcode[inv['barcode']]
            if not sub_matched[i]:
                pairs.append((i, j)); sub_matched[i] = True; inv_matched[j] = True

    sub_by_sku = {}
    for i, s in enumerate(submitted):
        if not sub_matched[i] and s['sku']:
            sub_by_sku.setdefault(s['sku'], i)
    for j, inv in enumerate(invoice_items):
        if inv_matched[j] or not inv['sku']:
            continue
        i = sub_by_sku.get(inv['sku'])
        if i is not None and not sub_matched[i]:
            pairs.append((i, j)); sub_matched[i] = True; inv_matched[j] = True

    results = []
    for i, j in pairs:
        sub, inv = submitted[i], invoice_items[j]
        if sub['quantity'] == inv['quantity']:
            status = 'Match'
        elif inv['quantity'] > sub['quantity']:
            status = 'More than submitted (likely preorder/backorder)'
        else:
            status = 'Less than submitted (partial shipment?)'
        results.append([sub['sku'] or inv['sku'], sub['barcode'] or inv['barcode'],
            sub['title'] or inv['description'], sub['quantity'], inv['quantity'], status, sub['order_names']])

    for i, sub in enumerate(submitted):
        if sub_matched[i]:
            continue
        results.append([sub['sku'], sub['barcode'], sub['title'], sub['quantity'], 0,
            'Missing from invoice entirely - needs review', sub['order_names']])

    for j, inv in enumerate(invoice_items):
        if inv_matched[j]:
            continue
        results.append([inv['sku'], inv['barcode'], inv['description'], 0, inv['quantity'],
            'In invoice but not submitted - needs review', ''])

    results.sort(key=lambda r: (r[0] or '', r[1] or ''))
    return results

def shipped_qty_for_sku(row):
    sku, barcode, title, sub_qty, inv_qty, status, order_names = row
    if status in ('Match', 'More than submitted (likely preorder/backorder)'):
        return sub_qty
    if status == 'Less than submitted (partial shipment?)':
        return inv_qty
    return 0

def sort_key(supplier_order_id):
    if supplier_order_id.endswith('-PREEXISTING'):
        return (0, supplier_order_id)
    return (1, supplier_order_id)

def advance_shipped_stage(order_needs_rows, comparison_results):
    to_advance = {row[0]: shipped_qty_for_sku(row) for row in comparison_results if shipped_qty_for_sku(row) > 0}
    if not to_advance:
        return order_needs_rows, set(), 0

    by_sku = {}
    for idx, row in enumerate(order_needs_rows):
        if not row or not row[0]:
            continue
        row = row + [''] * (8 - len(row))
        order_needs_rows[idx] = row
        order_name, sku, title, supplier, unit, sup_id, stage, updated = row
        if supplier != SUPPLIER or stage != 'Ordered':
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
    CONFIRMED = {'Match', 'More than submitted (likely preorder/backorder)'}
    merged = dict(existing)
    for row in new_results:
        key = reconciliation_key(row)
        if not key:
            continue
        prior = merged.get(key)
        if (row[5] == 'Missing from invoice entirely - needs review' and prior is not None
                and len(prior) > 5 and prior[5] in CONFIRMED):
            continue
        merged[key] = row
    return [merged[k] for k in sorted(merged.keys())]

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

            all_items = []
            invoices_parsed = []
            for f in file_fields:
                file_bytes = f.file.read()
                items = parse_ud_invoice(file_bytes)
                all_items.extend(items)
                invoices_parsed.append({'filename': f.filename, 'lineItems': len(items)})

            invoice_items_by_key = aggregate_invoice_items(all_items)
            barcode_by_sku = load_ud_barcode_by_sku()
            order_needs_rows = sheets_get(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
            submitted = load_submitted_from_order_needs(order_needs_rows, barcode_by_sku)
            new_results = run_comparison(submitted, invoice_items_by_key)

            updated_rows, touched_orders, advanced_count = advance_shipped_stage(order_needs_rows, new_results)
            if advanced_count:
                sheets_put(AGG_SHEET_ID, f"'{ORDER_NEEDS_TAB}'!A2:H{len(updated_rows) + 1}", updated_rows)

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

            ensure_reconcile_tab_exists()
            existing = load_existing_reconciliation()
            merged_results = merge_results(existing, new_results)
            header = [['SKU', 'Barcode', 'Title', 'Ordered Qty', 'Invoice Qty', 'Status', 'Order Names']]
            sheets_clear(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A1:G1000")
            sheets_put(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A1:G1", header)
            if merged_results:
                sheets_put(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A2:G{len(merged_results)+1}", merged_results)

            self._send_json(200, {
                'success': True,
                'invoicesParsed': invoices_parsed,
                'itemsInInvoices': len(invoice_items_by_key),
                'skusCompared': len(new_results),
                'unitsAdvancedToShipped': advanced_count,
                'ordersFullyShippedAndTagged': tagged,
                'skippedAlreadyInventoryQueued': skipped_inventory_queued,
                'tagErrors': tag_errors,
                'results': new_results,
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
