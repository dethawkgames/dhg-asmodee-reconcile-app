import json
import os
import time
from http.server import BaseHTTPRequestHandler

import jwt
import urllib.request
import urllib.parse

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
BOX_TAB = 'Box Inventory'
LOW_STOCK_THRESHOLD = 5


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


def load_boxes():
    """Returns a list of dicts: {size, type, dims (sorted desc), onHand, rowNumber}."""
    rows = sheets_get(AGG_SHEET_ID, f"'{BOX_TAB}'!A2:D100")
    boxes = []
    for i, row in enumerate(rows):
        if not row or not row[0]:
            continue
        size = row[0].strip()
        box_type = row[1] if len(row) > 1 else 'Box'
        on_hand = int(row[2]) if len(row) > 2 and str(row[2]).strip().isdigit() else 0
        dims = sorted([int(d) for d in size.lower().split('x')], reverse=True)
        boxes.append({
            'size': size, 'type': box_type, 'dims': dims,
            'onHand': on_hand, 'rowNumber': i + 2,
        })
    return boxes


def find_all_fits(pile_dims, boxes, clearance):
    """pile_dims: [L, W, H] sorted desc. Returns every box where every sorted
    box dimension >= corresponding sorted pile dimension + clearance,
    allowing any pile orientation. Flat mailers naturally get excluded for
    anything with real height, since their own height dimension is tiny.

    Sorted so the recommended default is the smallest box that's actually in
    stock; out-of-stock boxes are still included (so a 0-on-hand box never
    gets silently recommended), just ranked after every in-stock option of
    the same or smaller size. The full list lets the picker choose any
    fitting size, not just the top one."""
    needed = [d + clearance for d in pile_dims]
    fitting = []
    for box in boxes:
        if all(box['dims'][i] >= needed[i] for i in range(3)):
            volume = box['dims'][0] * box['dims'][1] * box['dims'][2]
            fitting.append((volume, box))
    fitting.sort(key=lambda x: (x[1]['onHand'] <= 0, x[0]))
    return [b for _, b in fitting]


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            boxes = load_boxes()
            result = [{
                'size': b['size'], 'type': b['type'], 'onHand': b['onHand'],
                'lowStock': b['onHand'] < LOW_STOCK_THRESHOLD,
            } for b in boxes]
            self._send_json(200, {'success': True, 'boxes': result, 'lowStockThreshold': LOW_STOCK_THRESHOLD})
        except Exception as e:
            import traceback
            self._send_json(500, {'error': str(e), 'trace': traceback.format_exc()})

    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_length)) if content_length else {}
            action = body.get('action')

            if action == 'recommend':
                length = float(body['length'])
                width = float(body['width'])
                height = float(body['height'])
                pile_dims = sorted([length, width, height], reverse=True)

                boxes = load_boxes()
                insured_options = find_all_fits(pile_dims, boxes, clearance=2)
                tight_options = find_all_fits(pile_dims, boxes, clearance=0)

                def fmt(b):
                    return {'size': b['size'], 'type': b['type'], 'onHand': b['onHand'], 'lowStock': b['onHand'] < LOW_STOCK_THRESHOLD}

                self._send_json(200, {
                    'success': True,
                    'insuredFitOptions': [fmt(b) for b in insured_options],
                    'tightFitOptions': [fmt(b) for b in tight_options],
                })

            elif action == 'use':
                size = body['size']
                boxes = load_boxes()
                box = next((b for b in boxes if b['size'] == size), None)
                if not box:
                    self._send_json(404, {'error': f'Box size {size} not found'})
                    return
                new_qty = max(0, box['onHand'] - 1)
                sheets_put(AGG_SHEET_ID, f"'{BOX_TAB}'!C{box['rowNumber']}", [[new_qty]])
                self._send_json(200, {
                    'success': True, 'size': size, 'onHand': new_qty,
                    'lowStock': new_qty < LOW_STOCK_THRESHOLD,
                })

            else:
                self._send_json(400, {'error': 'Unknown action. Use "recommend" or "use".'})

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
