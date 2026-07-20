import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler
import jwt

# Mark ACDD Shipped
#
# ACDD has no itemized shipment document the way Asmodee (invoice) and
# Universal Dist (invoice) do, so - same as the old system - this stays an
# unconditional manual action: advance every currently-Ordered ACDD row to
# Shipped. The safety property that matters here isn't "did this specific
# unit actually ship" (there's no way to check that for ACDD), it's "can
# this only touch ACDD rows that are genuinely locked" - which it can, since
# 'Ordered' rows are exactly the ones with a real Supplier Order ID.

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
ORDER_NEEDS_TAB = 'Order Needs'
ORDER_NEEDS_RANGE = f"'{ORDER_NEEDS_TAB}'!A2:H50000"
SUPPLIER = 'ACDD'

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'
STAGE_ORDER = ['NotOrdered', 'Ordered', 'Shipped', 'Arrived']

EMAIL_LIFECYCLE_TAGS = {
    'dhg-status-store-first-order', 'dhg-status-shop-first-order',
    'dhg-status-order-placed', 'dhg-status-preorder',
}

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

def mark_acdd_shipped():
    rows = sheets_get(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
    if not rows:
        return {'shipped': False, 'message': 'No rows in Order Needs tab.'}

    today = time.strftime('%Y-%m-%d')
    padded_rows = []
    advanced_count = 0
    touched_orders = set()

    for row in rows:
        if not row or not row[0]:
            continue
        row = row + [''] * (8 - len(row))
        if row[3] == SUPPLIER and row[6] == 'Ordered':
            row[6] = 'Shipped'
            row[7] = today
            advanced_count += 1
            touched_orders.add(row[0])
        padded_rows.append(row)

    if advanced_count:
        sheets_put(AGG_SHEET_ID, f"'{ORDER_NEEDS_TAB}'!A2:H{len(padded_rows) + 1}", padded_rows)

    rows_by_order = {}
    for row in padded_rows:
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

    return {
        'shipped': True,
        'unitsAdvanced': advanced_count,
        'ordersFullyShippedAndTagged': tagged,
        'skippedAlreadyInventoryQueued': skipped_inventory_queued,
        'tagErrors': tag_errors,
    }

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            result = mark_acdd_shipped()
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
