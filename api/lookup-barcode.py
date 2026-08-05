import json
import os
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'

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

BARCODE_LOOKUP_QUERY = '''
query lookupBarcode($q: String!) {
  productVariants(first: 1, query: $q) {
    edges {
      node {
        sku
        title
        barcode
        product { title }
      }
    }
  }
}
'''

def lookup_barcode(upc):
    data = shopify_graphql(BARCODE_LOOKUP_QUERY, {'q': f'barcode:{upc}'})
    edges = data['productVariants']['edges']
    if not edges:
        return None
    node = edges[0]['node']
    product_title = (node.get('product') or {}).get('title') or ''
    variant_title = node.get('title') or ''
    full_title = product_title if (not variant_title or variant_title == 'Default Title') else f'{product_title} — {variant_title}'
    return {'sku': node.get('sku') or '', 'title': full_title, 'barcode': node.get('barcode') or upc}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            upc = (params.get('upc', [''])[0]).strip()
            if not upc:
                self._send_json(400, {'error': 'upc is required'})
                return

            variant = lookup_barcode(upc)
            if not variant:
                self._send_json(404, {'error': f'No product found for barcode {upc}', 'upc': upc})
                return

            self._send_json(200, variant)
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
