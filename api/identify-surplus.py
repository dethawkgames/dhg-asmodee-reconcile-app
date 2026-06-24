import json
import os
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

import jwt
import urllib.request
import urllib.parse

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
SURPLUS_TAB = 'Surplus to Bin'

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP')
SHOPIFY_CLIENT_ID = os.environ.get('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SECRET = os.environ.get('SHOPIFY_CLIENT_SECRET')
SHOPIFY_API_VERSION = '2025-01'


# ── Google Sheets auth + access ──────────────────────────────────────────────

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


def sheets_clear(spreadsheet_id, range_str):
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str)}:clear'
    req = urllib.request.Request(url, data=b'', method='POST', headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def ensure_surplus_tab_exists():
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{AGG_SHEET_ID}?fields=sheets.properties.title'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    titles = [s['properties']['title'] for s in result['sheets']]
    if SURPLUS_TAB not in titles:
        body = json.dumps({'requests': [{'addSheet': {'properties': {'title': SURPLUS_TAB}}}]}).encode()
        req = urllib.request.Request(
            f'https://sheets.googleapis.com/v4/spreadsheets/{AGG_SHEET_ID}:batchUpdate',
            data=body, method='POST',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req) as resp:
            json.loads(resp.read())


# ── "Arrived" quantities per SKU, across all three suppliers ────────────────

def get_arrived_quantities():
    """Returns {sku: {quantity, title}}. Asmodee uses the reconciled Quoted
    Qty (falls back to ordered Quantity if a SKU wasn't part of this week's
    reconciliation). Universal Dist / ACDD use the manually-entered Received
    Qty when present, otherwise assume the full ordered Quantity arrived."""
    arrived = {}

    # Asmodee Order (ordered quantities, fallback source)
    asmodee_order_rows = sheets_get(AGG_SHEET_ID, "'Asmodee Order'!A2:H1000")
    for row in asmodee_order_rows:
        if not row or not row[0]:
            continue
        sku = row[0].strip()
        qty = int(row[1]) if len(row) > 1 and str(row[1]).isdigit() else 0
        title = row[4] if len(row) > 4 else ''
        arrived[sku] = {'quantity': qty, 'title': title}

    # Asmodee's Latest Reconciliation overrides with the real quoted/shipped
    # quantity where available
    try:
        recon_rows = sheets_get(AGG_SHEET_ID, "'Latest Reconciliation'!A2:F1000")
    except Exception:
        recon_rows = []
    for row in recon_rows:
        if not row or not row[0]:
            continue
        sku = row[0].strip()
        title = row[1] if len(row) > 1 else ''
        quoted_qty = int(row[3]) if len(row) > 3 and str(row[3]).isdigit() else 0
        arrived[sku] = {'quantity': quoted_qty, 'title': title or arrived.get(sku, {}).get('title', '')}

    # Universal Dist Order: SKU, Barcode, Quantity, Title, Warehouse, Order Names, Notes, Received Qty
    try:
        ud_rows = sheets_get(AGG_SHEET_ID, "'Universal Dist Order'!A2:H1000")
    except Exception:
        ud_rows = []
    for row in ud_rows:
        if not row or not row[0]:
            continue
        sku = row[0].strip()
        ordered_qty = int(row[2]) if len(row) > 2 and str(row[2]).isdigit() else 0
        title = row[3] if len(row) > 3 else ''
        received_raw = row[7].strip() if len(row) > 7 and row[7] is not None else ''
        qty = int(received_raw) if received_raw.isdigit() else ordered_qty
        arrived[sku] = {'quantity': qty, 'title': title}

    # ACDD Order: ACDD SKU, Shopify SKU, Quantity, Title, Order Names, Notes, Received Qty
    try:
        acdd_rows = sheets_get(AGG_SHEET_ID, "'ACDD Order'!A2:G1000")
    except Exception:
        acdd_rows = []
    for row in acdd_rows:
        if not row or not row[1]:
            continue
        sku = row[1].strip()  # Shopify SKU
        ordered_qty = int(row[2]) if len(row) > 2 and str(row[2]).isdigit() else 0
        title = row[3] if len(row) > 3 else ''
        received_raw = row[6].strip() if len(row) > 6 and row[6] is not None else ''
        qty = int(received_raw) if received_raw.isdigit() else ordered_qty
        arrived[sku] = {'quantity': qty, 'title': title}

    return arrived


# ── "Still needed" quantities: all unfulfilled orders from the last 30 days ─

def get_shopify_token():
    data = urllib.parse.urlencode({
        'grant_type': 'client_credentials',
        'client_id': SHOPIFY_CLIENT_ID,
        'client_secret': SHOPIFY_CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(
        f'https://{SHOPIFY_SHOP}/admin/oauth/access_token', data=data, method='POST'
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result['access_token']


def shopify_graphql(query, variables=None):
    token = get_shopify_token()
    body = json.dumps({'query': query, 'variables': variables or {}}).encode()
    req = urllib.request.Request(
        f'https://{SHOPIFY_SHOP}/admin/api/{SHOPIFY_API_VERSION}/graphql.json',
        data=body, method='POST',
        headers={'Content-Type': 'application/json', 'X-Shopify-Access-Token': token}
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    if result.get('errors'):
        raise Exception(f"Shopify GraphQL errors: {result['errors']}")
    return result['data']


def get_still_needed_quantities():
    """Returns {sku: total_quantity_needed} across all unfulfilled orders
    placed in the last 30 days. (Anything older is the Backorder Weekly
    Sweep's territory, not this tool's.)"""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%d')
    needed = {}
    cursor = None
    has_next = True

    while has_next:
        data = shopify_graphql('''
            query getOrders($cursor: String, $q: String!) {
              orders(first: 50, after: $cursor, query: $q) {
                pageInfo { hasNextPage endCursor }
                edges {
                  node {
                    name
                    lineItems(first: 50) {
                      edges { node { sku quantity } }
                    }
                  }
                }
              }
            }
        ''', {'cursor': cursor, 'q': f'fulfillment_status:unfulfilled created_at:>={since}'})

        page = data['orders']
        for edge in page['edges']:
            order = edge['node']
            for li_edge in order['lineItems']['edges']:
                item = li_edge['node']
                sku = (item.get('sku') or '').strip()
                if not sku:
                    continue
                needed[sku] = needed.get(sku, 0) + item['quantity']
        has_next = page['pageInfo']['hasNextPage']
        cursor = page['pageInfo']['endCursor']

    return needed


# ── HTTP handler ─────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            arrived = get_arrived_quantities()
            needed = get_still_needed_quantities()

            results = []
            for sku, info in arrived.items():
                surplus = info['quantity'] - needed.get(sku, 0)
                if surplus > 0:
                    results.append([sku, info['title'], surplus])

            results.sort(key=lambda r: r[0])

            ensure_surplus_tab_exists()
            header = [['SKU', 'Title', 'Surplus Qty']]
            sheets_clear(AGG_SHEET_ID, f"'{SURPLUS_TAB}'!A1:C1000")
            sheets_put(AGG_SHEET_ID, f"'{SURPLUS_TAB}'!A1:C1", header)
            if results:
                sheets_put(AGG_SHEET_ID, f"'{SURPLUS_TAB}'!A2:C{len(results)+1}", results)

            self._send_json(200, {
                'success': True,
                'skusChecked': len(arrived),
                'surplusCount': len(results),
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
