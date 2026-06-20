import json
import os
import time
import base64
import cgi
import io
from http.server import BaseHTTPRequestHandler

import pdfplumber
import jwt
import urllib.request
import urllib.parse
import urllib.error

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
ASMODEE_ORDER_RANGE = "'Asmodee Order'!A2:H1000"
RECONCILE_TAB = 'Latest Reconciliation'

# ── PDF parsing (same logic validated earlier) ──────────────────────────────

SKU_X = 43.2
DESC_X = 101.6
MSRP_X_MIN = 350
QTY_X_MIN = 415
QTY_X_MAX = 435


def parse_asmodee_quote(file_bytes):
    line_items = []
    hit_total = False

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            if hit_total:
                break

            words = page.extract_words()
            lines = {}
            for w in words:
                top_key = round(w['top'])
                lines.setdefault(top_key, []).append(w)

            sorted_tops = sorted(lines.keys())
            current_item = None
            header_seen = False

            for top in sorted_tops:
                row_words = sorted(lines[top], key=lambda w: w['x0'])
                row_text = ' '.join(w['text'] for w in row_words)

                if row_text.strip().startswith('Total $') or row_text.strip().startswith('Total'):
                    hit_total = True
                    break

                if 'Description' in row_text and 'MSRP' in row_text:
                    header_seen = True
                    continue
                if not header_seen:
                    continue
                if row_text.strip() in ('Line', 'Amount', 'Unit', 'Excl.', 'Tax') or row_text.strip().startswith('Line Amount'):
                    continue

                qty_word = next((w for w in row_words if QTY_X_MIN <= w['x0'] <= QTY_X_MAX), None)
                sku_word = next((w for w in row_words if abs(w['x0'] - SKU_X) < 2), None)

                if qty_word and sku_word:
                    if current_item:
                        line_items.append(current_item)
                    desc_words = [w['text'] for w in row_words if w['x0'] >= DESC_X and w['x0'] < MSRP_X_MIN]
                    qty_val = qty_word['text']
                    current_item = {
                        'sku': sku_word['text'],
                        'description': ' '.join(desc_words),
                        'quantity': int(qty_val) if qty_val.isdigit() else qty_val,
                    }
                elif qty_word and not sku_word and current_item is not None:
                    desc_words = [w['text'] for w in row_words if w['x0'] >= DESC_X and w['x0'] < MSRP_X_MIN]
                    if desc_words:
                        line_items.append(current_item)
                        qty_val = qty_word['text']
                        current_item = {
                            'sku': None,
                            'description': ' '.join(desc_words),
                            'quantity': int(qty_val) if qty_val.isdigit() else qty_val,
                            'is_fee': True,
                        }
                elif sku_word and not qty_word and current_item is not None:
                    fragment = row_words[0]['text']
                    if len(row_words) == 1 and len(fragment) <= 6:
                        current_item['sku'] = current_item['sku'] + fragment

            if current_item:
                line_items.append(current_item)

    return [item for item in line_items if not item.get('is_fee')]


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

def run_comparison(submitted_rows, quote_items):
    # submitted_rows columns: ProductId, Quantity, UnitOfMeasureId, VariantId, Title, Stock Status, Order Names, Notes
    submitted = {}
    for row in submitted_rows:
        if not row or not row[0]:
            continue
        sku = row[0].strip()
        qty = int(row[1]) if len(row) > 1 and str(row[1]).isdigit() else 0
        title = row[4] if len(row) > 4 else ''
        order_names = row[6] if len(row) > 6 else ''
        submitted[sku] = {'quantity': qty, 'title': title, 'order_names': order_names}

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
        order_names = sub['order_names'] if sub else ''

        if sub and quo:
            if sub['quantity'] == quo['quantity']:
                status = 'Match'
            elif quo['quantity'] > sub['quantity']:
                status = 'More than submitted (likely preorder/backorder)'
            else:
                status = 'Less than submitted (partial shipment?)'
            results.append([
                sku, sub['title'] or quo['description'],
                sub['quantity'], quo['quantity'], status, order_names
            ])
        elif sub and not quo:
            results.append([
                sku, sub['title'], sub['quantity'], 0,
                'Missing from quote entirely - needs review', order_names
            ])
        elif quo and not sub:
            results.append([
                sku, quo['description'], 0, quo['quantity'],
                'In quote but not submitted - needs review', ''
            ])

    return results


# An item "counts as shipped" for an order if its status is Match or the
# preorder/backorder overage case - both mean the customer's ordered quantity
# is genuinely covered. Anything else means that SKU has not actually arrived.
SHIPPED_OK_STATUSES = {'Match', 'More than submitted (likely preorder/backorder)'}


def compute_fully_shipped_orders(comparison_results):
    """Given reconciliation results (each row's last column is Order Names),
    returns the set of order names where EVERY Asmodee SKU belonging to that
    order has a status in SHIPPED_OK_STATUSES. An order with even one SKU
    that's missing/short/unexpected is excluded - it's not fully shipped yet."""
    order_skus_ok = {}   # order_name -> count of SKUs that are OK
    order_skus_total = {}  # order_name -> total SKUs seen for that order

    for row in comparison_results:
        sku, title, sub_qty, quo_qty, status = row[0], row[1], row[2], row[3], row[4]
        order_names_str = row[5] if len(row) > 5 else ''
        if not order_names_str:
            continue
        for order_name in order_names_str.split(', '):
            order_name = order_name.strip()
            if not order_name:
                continue
            order_skus_total[order_name] = order_skus_total.get(order_name, 0) + 1
            if status in SHIPPED_OK_STATUSES:
                order_skus_ok[order_name] = order_skus_ok.get(order_name, 0) + 1

    fully_shipped = set()
    for order_name, total in order_skus_total.items():
        if order_skus_ok.get(order_name, 0) == total:
            fully_shipped.add(order_name)
    return fully_shipped


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

            # Parse multipart manually using cgi (still available in this runtime)
            fs = cgi.FieldStorage(
                fp=io.BytesIO(body),
                headers=self.headers,
                environ={'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type}
            )

            if 'file' not in fs:
                self._send_json(400, {'error': 'No file field found in upload'})
                return

            file_bytes = fs['file'].file.read()

            quote_items = parse_asmodee_quote(file_bytes)
            submitted_rows = sheets_get(AGG_SHEET_ID, ASMODEE_ORDER_RANGE)
            results = run_comparison(submitted_rows, quote_items)

            # Write to Latest Reconciliation tab
            ensure_reconcile_tab_exists()
            header = [['Shopify SKU', 'Title', 'Submitted Qty', 'Quoted Qty', 'Status', 'Order Names']]
            sheets_clear(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A1:F1000")
            sheets_put(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A1:F1", header)
            if results:
                sheets_put(AGG_SHEET_ID, f"'{RECONCILE_TAB}'!A2:F{len(results)+1}", results)

            self._send_json(200, {
                'success': True,
                'itemsInQuote': len(quote_items),
                'itemsSubmitted': len(submitted_rows),
                'results': results,
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
