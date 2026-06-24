import json
import os
import time
from http.server import BaseHTTPRequestHandler

import jwt
import urllib.request
import urllib.parse

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'

# Universal Dist Order: SKU, Barcode, Quantity, Title, Warehouse, Order Names, Notes, Received Qty
UD_RANGE = "'Universal Dist Order'!A2:H1000"
UD_SKU_COL = 0
UD_QTY_COL = 2
UD_TITLE_COL = 3
UD_RECEIVED_COL = 7  # column H (0-indexed)
UD_RECEIVED_LETTER = 'H'

# ACDD Order: ACDD SKU, Shopify SKU, Quantity, Title, Order Names, Notes, Received Qty
ACDD_RANGE = "'ACDD Order'!A2:G1000"
ACDD_SKU_COL = 1  # Shopify SKU is the customer-facing identifier
ACDD_QTY_COL = 2
ACDD_TITLE_COL = 3
ACDD_RECEIVED_COL = 6  # column G (0-indexed)
ACDD_RECEIVED_LETTER = 'G'


def get_google_token(scope='https://www.googleapis.com/auth/spreadsheets'):
    sa_email = os.environ['GOOGLE_SA_EMAIL']
    sa_key = os.environ['GOOGLE_SA_PRIVATE_KEY'].replace('\\n', '\n')
    now = int(time.time())
    payload = {
        'iss': sa_email, 'scope': scope,
        'aud': 'https://oauth2.googleapis.com/token',
        'exp': now + 3600, 'iat': now,
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
        'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def load_items():
    items = []

    ud_rows = sheets_get(AGG_SHEET_ID, UD_RANGE)
    for i, row in enumerate(ud_rows):
        if not row or not row[0]:
            continue
        sku = row[UD_SKU_COL].strip()
        qty = int(row[UD_QTY_COL]) if len(row) > UD_QTY_COL and str(row[UD_QTY_COL]).isdigit() else 0
        title = row[UD_TITLE_COL] if len(row) > UD_TITLE_COL else ''
        received = row[UD_RECEIVED_COL] if len(row) > UD_RECEIVED_COL else ''
        items.append({
            'supplier': 'Universal Dist', 'sku': sku, 'title': title,
            'orderedQty': qty, 'receivedQty': received, 'rowNumber': i + 2,
        })

    acdd_rows = sheets_get(AGG_SHEET_ID, ACDD_RANGE)
    for i, row in enumerate(acdd_rows):
        if not row or not row[ACDD_SKU_COL]:
            continue
        sku = row[ACDD_SKU_COL].strip()
        qty = int(row[ACDD_QTY_COL]) if len(row) > ACDD_QTY_COL and str(row[ACDD_QTY_COL]).isdigit() else 0
        title = row[ACDD_TITLE_COL] if len(row) > ACDD_TITLE_COL else ''
        received = row[ACDD_RECEIVED_COL] if len(row) > ACDD_RECEIVED_COL else ''
        items.append({
            'supplier': 'ACDD', 'sku': sku, 'title': title,
            'orderedQty': qty, 'receivedQty': received, 'rowNumber': i + 2,
        })

    return items


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            items = load_items()
            self._send_json(200, {'success': True, 'items': items})
        except Exception as e:
            import traceback
            self._send_json(500, {'error': str(e), 'trace': traceback.format_exc()})

    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_length))
            updates = body.get('updates', [])
            # Each update: {supplier, rowNumber, receivedQty}
            ud_updates = [u for u in updates if u['supplier'] == 'Universal Dist']
            acdd_updates = [u for u in updates if u['supplier'] == 'ACDD']

            for u in ud_updates:
                sheets_put(AGG_SHEET_ID, f"'Universal Dist Order'!{UD_RECEIVED_LETTER}{u['rowNumber']}",
                           [[u['receivedQty']]])
            for u in acdd_updates:
                sheets_put(AGG_SHEET_ID, f"'ACDD Order'!{ACDD_RECEIVED_LETTER}{u['rowNumber']}",
                           [[u['receivedQty']]])

            self._send_json(200, {'success': True, 'updated': len(updates)})
        except Exception as e:
            import traceback
            self._send_json(500, {'error': str(e), 'trace': traceback.format_exc()})

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _send_json(self, status, data):
        self.send_response(status)
        self._cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
