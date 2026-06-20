import json 
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler

import jwt

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
TRACKING_RANGE = "'Shipment Tracking'!A2:D1000"
RECONCILE_RANGE = "'Latest Reconciliation'!A2:F1000"

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'

VALID_SUPPLIERS = {'Asmodee', 'Universal Dist', 'ACDD'}


# ── Google Sheets auth + access (same pattern as reconcile.py) ──────────────

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


# ── Shopify auth + tagging ──────────────────────────────────────────────────

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


def get_order_id_by_name(order_name):
    """order_name like '#5360' - Shopify's order search query needs it without
    URL-unsafe characters causing issues, so we pass it through as a query string."""
    data = shopify_graphql('''
        query getOrder($q: String!) {
          orders(first: 1, query: $q) { edges { node { id name } } }
        }
    ''', {'q': f'name:{order_name}'})
    edges = data['orders']['edges']
    return edges[0]['node']['id'] if edges else None


def tag_order_shipped_from_supplier(order_id):
    shopify_graphql('''
        mutation tagsAdd($id: ID!, $tags: [String!]!) {
          tagsAdd(id: $id, tags: $tags) {
            userErrors { field message }
          }
        }
    ''', {'id': order_id, 'tags': ['dhg-shipped-from-supplier']})


# ── Core logic ───────────────────────────────────────────────────────────────

def get_asmodee_fully_shipped_orders():
    """Reads the Latest Reconciliation tab and returns the set of order names
    where every Asmodee SKU for that order is Match or More-than-submitted."""
    rows = sheets_get(AGG_SHEET_ID, RECONCILE_RANGE)
    SHIPPED_OK = {'Match', 'More than submitted (likely preorder/backorder)'}

    order_skus_ok = {}
    order_skus_total = {}

    for row in rows:
        if len(row) < 5:
            continue
        status = row[4]
        order_names_str = row[5] if len(row) > 5 else ''
        if not order_names_str:
            continue
        for order_name in order_names_str.split(', '):
            order_name = order_name.strip()
            if not order_name:
                continue
            order_skus_total[order_name] = order_skus_total.get(order_name, 0) + 1
            if status in SHIPPED_OK:
                order_skus_ok[order_name] = order_skus_ok.get(order_name, 0) + 1

    return {name for name, total in order_skus_total.items() if order_skus_ok.get(name, 0) == total}


def mark_supplier_shipped(supplier):
    """Core logic for all three buttons. For Asmodee, only orders that pass the
    itemized reconciliation check are eligible. For Universal Dist/ACDD, every
    order in the tracking tab needing that supplier is eligible unconditionally."""

    tracking_rows = sheets_get(AGG_SHEET_ID, TRACKING_RANGE)
    if not tracking_rows:
        return {'updated': [], 'completed': [], 'skipped': [], 'message': 'No rows in Shipment Tracking tab.'}

    eligible_orders = None
    if supplier == 'Asmodee':
        eligible_orders = get_asmodee_fully_shipped_orders()

    updated_rows = []
    completed_order_names = []
    skipped_order_names = []

    for row in tracking_rows:
        # Order # | Suppliers Needed | Suppliers Shipped So Far | Status
        order_name = row[0] if len(row) > 0 else ''
        suppliers_needed = row[1] if len(row) > 1 else ''
        suppliers_shipped = row[2] if len(row) > 2 else ''
        status = row[3] if len(row) > 3 else 'Pending'

        needed_set = {s.strip() for s in suppliers_needed.split(',') if s.strip()}
        shipped_set = {s.strip() for s in suppliers_shipped.split(',') if s.strip()}

        if supplier not in needed_set or supplier in shipped_set or status == 'Complete':
            updated_rows.append([order_name, suppliers_needed, suppliers_shipped, status])
            continue

        if eligible_orders is not None and order_name not in eligible_orders:
            # Asmodee button clicked, but this order's Asmodee items didn't fully
            # match the quote yet - leave it pending, don't mark shipped.
            skipped_order_names.append(order_name)
            updated_rows.append([order_name, suppliers_needed, suppliers_shipped, status])
            continue

        shipped_set.add(supplier)
        new_status = 'Complete' if shipped_set == needed_set else 'Pending'
        updated_rows.append([order_name, suppliers_needed, ', '.join(sorted(shipped_set)), new_status])

        if new_status == 'Complete':
            completed_order_names.append(order_name)

    # Write the updated tracking tab back
    if updated_rows:
        sheets_clear(AGG_SHEET_ID, TRACKING_RANGE)
        sheets_put(AGG_SHEET_ID, f"'Shipment Tracking'!A2:D{len(updated_rows)+1}", updated_rows)

    # Tag completed orders in Shopify
    tagged = []
    tag_errors = []
    for order_name in completed_order_names:
        try:
            order_id = get_order_id_by_name(order_name)
            if order_id:
                tag_order_shipped_from_supplier(order_id)
                tagged.append(order_name)
            else:
                tag_errors.append({'order': order_name, 'error': 'Order not found in Shopify'})
        except Exception as e:
            tag_errors.append({'order': order_name, 'error': str(e)})

    return {
        'supplier': supplier,
        'ordersMarkedForThisSupplier': len([r for r in updated_rows]) - len(skipped_order_names),
        'completedAndTagged': tagged,
        'tagErrors': tag_errors,
        'skippedNotYetFullyMatched': skipped_order_names,
    }


# ── HTTP handler ─────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length else b'{}'
            payload = json.loads(body) if body else {}
            supplier = payload.get('supplier')

            if supplier not in VALID_SUPPLIERS:
                self._send_json(400, {'error': f'supplier must be one of {sorted(VALID_SUPPLIERS)}'})
                return

            result = mark_supplier_shipped(supplier)
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
