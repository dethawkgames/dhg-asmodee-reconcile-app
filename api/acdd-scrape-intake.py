import json
import os
import time
from http.server import BaseHTTPRequestHandler

import jwt
import urllib.request
import urllib.parse

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
TAB = 'ACDD Scrape Raw'


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


def sheets_append(spreadsheet_id, range_str, values):
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str)}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS'
    body = json.dumps({'values': values}).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def ensure_tab_exists():
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{AGG_SHEET_ID}?fields=sheets.properties.title'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    titles = [s['properties']['title'] for s in result['sheets']]
    if TAB not in titles:
        body = json.dumps({'requests': [{'addSheet': {'properties': {'title': TAB}}}]}).encode()
        req = urllib.request.Request(
            f'https://sheets.googleapis.com/v4/spreadsheets/{AGG_SHEET_ID}:batchUpdate',
            data=body, method='POST',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req) as resp:
            json.loads(resp.read())
        token2 = get_google_token()
        req2 = urllib.request.Request(
            f'https://sheets.googleapis.com/v4/spreadsheets/{AGG_SHEET_ID}/values/{urllib.parse.quote(TAB)}!A1:H1?valueInputOption=RAW',
            data=json.dumps({'values': [['Category', 'SKU', 'Title', 'Slug', 'Qty', 'MSRP', 'Price', 'Image']]}).encode(),
            method='PUT',
            headers={'Authorization': f'Bearer {token2}', 'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req2) as resp:
            json.loads(resp.read())


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_length))
            category = body.get('category', 'unknown')
            items = body.get('items', [])

            ensure_tab_exists()

            # Dedup against what's already there for this category before appending
            existing_rows = sheets_get(AGG_SHEET_ID, f"'{TAB}'!A2:H100000")
            existing_skus = set()
            for row in existing_rows:
                if len(row) >= 2 and row[0] == category:
                    existing_skus.add(row[1])

            new_rows = []
            for item in items:
                if item.get('sku') in existing_skus:
                    continue
                new_rows.append([
                    category, item.get('sku', ''), item.get('title', ''),
                    item.get('slug', ''), item.get('qty', ''), item.get('msrp', ''),
                    item.get('price', ''), item.get('image', ''),
                ])
                existing_skus.add(item.get('sku'))

            if new_rows:
                sheets_append(AGG_SHEET_ID, f"'{TAB}'!A1:H1", new_rows)

            self._send_json(200, {'success': True, 'received': len(items), 'newRowsAdded': len(new_rows)})
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
