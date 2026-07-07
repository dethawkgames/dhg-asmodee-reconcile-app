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
UD_ORDER_RANGE = "'Universal Dist Order'!A2:G1000"
RECONCILE_TAB = 'Latest UD Reconciliation'

# Item Nos that show up on UD invoices but aren't real products - exclude these
# from reconciliation entirely.
NON_PRODUCT_ITEM_NOS = {'41040'}  # SHIPPING & HANDLING
NON_PRODUCT_DESCRIPTIONS = {'2% cash discount reversal'}

# ── Invoice parsing (xlsx) ───────────────────────────────────────────────────

def parse_ud_invoice(file_bytes):
    """Parses a single Universal Dist invoice .xlsx. Returns a list of
    {'barcode', 'sku', 'description', 'quantity'} dicts, one per real product
    line (shipping/handling and discount-reversal lines excluded)."""
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
        raise ValueError(
            f"Couldn't find expected columns in invoice header: {header}"
        )

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
            continue  # not a real line item (e.g. blank trailing row)

        items.append({
            'barcode': item_no,
            'sku': vendor_item,
            'description': product,
            'quantity': qty,
        })
    return items

def aggregate_invoice_items(all_items):
    """Combines line items across multiple invoices (e.g. primary + secondary
    warehouse), keyed by barcode when present, falling back to SKU."""
    combined = {}
    for item in all_items:
        key = item['barcode'] or item['sku']
        if not key:
            continue
        if key not in combined:
            combined[key] = {
                'barcode': item['barcode'],
                'sku': item['sku'],
                'description': item['description'],
                'quantity': 0,
            }
        combined[key]['quantity'] += item['quantity']
    return combined

# ── Google Sheets auth + access ──────────────────────────────────────────────

def get_google_token(scope='https://www.googleapis.com/auth/spreadsheets'):
    sa_email = os.environ['GOOGLE_SA_EMAIL']
    sa_key = os.environ['GOOGLE_SA_PRIVATE_KEY'].replace('\\n', '\n')
    now = int(time.time())
    payload = {
        'iss': sa_email,
        'scope': scope,
        'aud': 'https://oauth2.googleapis.com/token',
        'exp': now + 3600,
        'iat': now,
    }
    assertion = jwt.encode(payload, sa_key, algorithm='RS256')
    data = urllib.parse.urlencode({
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': assertion,
    }).encode()
    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data, method='POST')
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result['access_token']

def sheets_get(spreadsheet_id, range_str):
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str)}'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result.get('values', [])

def sheets_put(spreadsheet_id, range_str, values):
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str)}?valueInputOption=RAW'
    body = json.dumps({'values': values}).encode()
    req = urllib.request.Request(url, data=body, method='PUT', headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    })
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
        body = json.dumps({
            'requests': [{'addSheet': {'properties': {'title': RECONCILE_TAB}}}]
        }).encode()
        req = urllib.request.Request(
            f'https://sheets.googleapis.com/v4/spreadsheets/{AGG_SHEET_ID}:batchUpdate',
            data=body, method='POST',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req) as resp:
            json.loads(resp.read())

# ── Comparison logic ─────────────────────────────────────────────────────────
# Mirrors the Asmodee reconciliation logic exactly, but keyed by Barcode
# instead of Shopify SKU (falling back to SKU when a barcode is missing on
# either side - the UD catalog's barcode mapping isn't fully built out yet).

def run_comparison(submitted_rows, invoice_items_by_key):
    # submitted_rows columns: SKU, Barcode, Quantity, Title, Warehouse, Order Names, Notes
    submitted = []
    for row in submitted_rows:
        if not row or not row[0]:
            continue
        sku = row[0].strip()
        barcode = row[1].strip() if len(row) > 1 else ''
        qty = int(row[2]) if len(row) > 2 and str(row[2]).isdigit() else 0
        title = row[3] if len(row) > 3 else ''
        order_names = row[5] if len(row) > 5 else ''
        notes = row[6] if len(row) > 6 else ''
        submitted.append({
            'sku': sku, 'barcode': barcode, 'quantity': qty,
            'title': title, 'order_names': order_names, 'notes': notes,
        })

    invoice_items = list(invoice_items_by_key.values())

    # Two-pass match: barcode-to-barcode first (the reliable UD identifier),
    # then fall back to SKU-to-SKU for whatever's left unmatched on either
    # side - this covers the catalog's current barcode-mapping gaps without
    # producing false "missing" + "unexpected" pairs for the same product.
    sub_matched = [False] * len(submitted)
    inv_matched = [False] * len(invoice_items)
    pairs = []  # (sub_idx, inv_idx)

    sub_by_barcode = {}
    for i, s in enumerate(submitted):
        if s['barcode']:
            sub_by_barcode.setdefault(s['barcode'], i)

    for j, inv in enumerate(invoice_items):
        if inv['barcode'] and inv['barcode'] in sub_by_barcode:
            i = sub_by_barcode[inv['barcode']]
            if not sub_matched[i]:
                pairs.append((i, j))
                sub_matched[i] = True
                inv_matched[j] = True

    sub_by_sku = {}
    for i, s in enumerate(submitted):
        if not sub_matched[i] and s['sku']:
            sub_by_sku.setdefault(s['sku'], i)

    for j, inv in enumerate(invoice_items):
        if inv_matched[j] or not inv['sku']:
            continue
        i = sub_by_sku.get(inv['sku'])
        if i is not None and not sub_matched[i]:
            pairs.append((i, j))
            sub_matched[i] = True
            inv_matched[j] = True

    results = []
    for i, j in pairs:
        sub, inv = submitted[i], invoice_items[j]
        if sub['quantity'] == inv['quantity']:
            status = 'Match'
        elif inv['quantity'] > sub['quantity']:
            status = 'More than submitted (likely preorder/backorder)'
        else:
            status = 'Less than submitted (partial shipment?)'
        results.append([
            sub['sku'] or inv['sku'], sub['barcode'] or inv['barcode'],
            sub['title'] or inv['description'], sub['quantity'], inv['quantity'],
            status, sub['order_names']
        ])

    for i, sub in enumerate(submitted):
        if sub_matched[i]:
            continue
        # UD has no Stock Status field like Asmodee does - this checks the
        # Notes column for a manually-entered "Pre-Order" marker instead.
        if 'pre-order' in sub['notes'].strip().lower():
            status = 'Pre-Order - not expected on this shipment yet'
        else:
            status = 'Missing from invoice entirely - needs review'
        results.append([
            sub['sku'], sub['barcode'], sub['title'], sub['quantity'], 0,
            status, sub['order_names']
        ])

    for j, inv in enumerate(invoice_items):
        if inv_matched[j]:
            continue
        results.append([
            inv['sku'], inv['barcode'], inv['description'], 0, inv['quantity'],
            'In invoice but not submitted - needs review', ''
        ])

    results.sort(key=lambda r: (r[0] or '', r[1] or ''))
    return results

# ── Merge logic ──────────────────────────────────────────────────────────────
# Same rationale as reconcile.py: overlapping order cycles (e.g. a delayed
# shipment's invoice arriving after the next week's order was already placed
# and reconciled) means a blind clear-and-replace on every upload would
# silently destroy whichever batch's results aren't part of THIS invoice.
# Instead: read what's already there, update/insert only the rows touched by
# this invoice (keyed by barcode, falling back to SKU - matching the same
# identifier priority used everywhere else in this file), and leave every
# other row exactly as it was.

def reconciliation_key(row):
    # row: [SKU, Barcode, Title, Submitted Qty, Invoice Qty, Status, Order Names]
    barcode = row[1] if len(row) > 1 else ''
    sku = row[0] if len(row) > 0 else ''
    return barcode or sku

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
    merged = dict(existing)
    for row in new_results:
        key = reconciliation_key(row)
        if key:
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

            fs = cgi.FieldStorage(
                fp=io.BytesIO(body),
                headers=self.headers,
                environ={'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type}
            )

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
            submitted_rows = sheets_get(AGG_SHEET_ID, UD_ORDER_RANGE)
            new_results = run_comparison(submitted_rows, invoice_items_by_key)

            ensure_reconcile_tab_exists()
            existing = load_existing_reconciliation()
            merged_results = merge_results(existing, new_results)

            header = [['SKU', 'Barcode', 'Title', 'Submitted Qty', 'Invoice Qty', 'Status', 'Order Names']]
            sheets_clear(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A1:G1000")
            sheets_put(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A1:G1", header)
            if merged_results:
                sheets_put(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A2:G{len(merged_results)+1}", merged_results)

            self._send_json(200, {
                'success': True,
                'invoicesParsed': invoices_parsed,
                'itemsInInvoices': len(invoice_items_by_key),
                'itemsSubmitted': len(submitted_rows),
                'newOrUpdatedThisUpload': len(new_results),
                'totalAfterMerge': len(merged_results),
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
