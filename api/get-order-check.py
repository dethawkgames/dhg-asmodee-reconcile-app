import json
import os
import urllib.request
import urllib.parse
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler
import jwt

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
ORDER_NEEDS_TAB = 'Order Needs'
ORDER_NEEDS_RANGE = f"'{ORDER_NEEDS_TAB}'!A2:H50000"

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'

# ── Google Sheets auth (same pattern as api/mark-stage.py) ──────────────────

def get_google_token(scope='https://www.googleapis.com/auth/spreadsheets.readonly'):
    sa_email = os.environ['GOOGLE_SA_EMAIL']
    raw_key = os.environ.get('GOOGLE_SA_PRIVATE_KEY_B64') or os.environ.get('GOOGLE_SA_PRIVATE_KEY', '')
    if os.environ.get('GOOGLE_SA_PRIVATE_KEY_B64'):
        import base64
        sa_key = base64.b64decode(raw_key).decode('utf-8')
    else:
        sa_key = raw_key.replace('\\n', '\n')
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

# ── Shopify auth (same pattern as api/mark-stage.py, api/picking-list.py) ───

def get_shopify_token():
    client_id = os.environ['SHOPIFY_CLIENT_ID']
    client_secret = os.environ['SHOPIFY_CLIENT_SECRET']
    data = urllib.parse.urlencode({
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret,
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

ORDER_LOOKUP_QUERY = '''
query getOrder($q: String!) {
  orders(first: 1, query: $q) {
    edges {
      node {
        id
        name
        cancelledAt
        displayFulfillmentStatus
        lineItems(first: 250) {
          edges {
            node {
              title
              sku
              currentQuantity
              quantity
            }
          }
        }
      }
    }
  }
}
'''

def get_shopify_order_lines(order_number):
    """Returns (order_name, cancelled, fulfillment_status, line_items) or
    (None, None, None, None) if not found. line_items excludes fully
    refunded lines (currentQuantity 0), per DHG standing rule.

    order_number may come in with or without the '#' (barcode scans have no
    prefix; the Shopify order.name field always does) - normalized to a
    single '#' and quoted, since Shopify's search parser has been reported
    to silently mismatch on bare `name:1234` without quotes."""
    bare_number = str(order_number).strip().lstrip('#')
    search_query = f"name:'#{bare_number}'"
    data = shopify_graphql(ORDER_LOOKUP_QUERY, {'q': search_query})
    edges = data['orders']['edges']
    if not edges:
        return None, None, None, None
    node = edges[0]['node']
    # Belt-and-suspenders: confirm the match is exact, not a partial/fuzzy
    # hit from Shopify's search, before trusting it.
    if node['name'].lstrip('#') != bare_number:
        return None, None, None, None
    lines = []
    for edge in node['lineItems']['edges']:
        li = edge['node']
        qty = li.get('currentQuantity')
        if qty is None:
            qty = li.get('quantity', 0)
        if qty <= 0:
            continue
        lines.append({
            'sku': li.get('sku') or '',
            'title': li['title'],
            'quantity': qty,
        })
    return node['name'], bool(node.get('cancelledAt')), node.get('displayFulfillmentStatus'), lines

def get_order_needs_counts(order_number):
    """Returns {sku: count} of Order Needs rows (one row per physical unit
    owed) for this order number. Column A = order number, column B = SKU."""
    order_number_bare = order_number.lstrip('#')
    rows = sheets_get(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
    counts = defaultdict(int)
    for row in rows:
        if not row or len(row) < 2:
            continue
        row_order = str(row[0]).strip().lstrip('#')
        if row_order == order_number_bare and row[1]:
            counts[row[1].strip()] += 1
    return counts


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            order_number = (params.get('order_number', [''])[0]).strip()
            if not order_number:
                self._send_json(400, {'error': 'order_number is required'})
                return

            order_name, cancelled, fulfillment_status, shopify_lines = get_shopify_order_lines(order_number)
            if order_name is None:
                self._send_json(404, {'error': f'Order {order_number} not found in Shopify'})
                return

            needs_counts = get_order_needs_counts(order_name)

            shopify_qty_by_sku = defaultdict(int)
            title_by_sku = {}
            for li in shopify_lines:
                shopify_qty_by_sku[li['sku']] += li['quantity']
                title_by_sku[li['sku']] = li['title']

            all_skus = set(shopify_qty_by_sku) | set(needs_counts)
            lines = []
            for sku in sorted(all_skus):
                shopify_qty = shopify_qty_by_sku.get(sku, 0)
                needs_qty = needs_counts.get(sku, 0)
                lines.append({
                    'sku': sku,
                    'title': title_by_sku.get(sku, sku),
                    'expected_qty': max(shopify_qty, needs_qty),
                    'shopify_qty': shopify_qty,
                    'order_needs_qty': needs_qty,
                    'mismatch': shopify_qty != needs_qty,
                })

            self._send_json(200, {
                'order_number': order_name,
                'cancelled': cancelled,
                'fulfillment_status': fulfillment_status,
                'lines': lines,
                'has_mismatch': any(l['mismatch'] for l in lines),
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
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _send_json(self, status, data):
        self.send_response(status)
        self._cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
