import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler
import jwt

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
TRACKING_RANGE = "'Shipment Tracking'!A2:F1000"
RECONCILE_RANGE = "'Latest Reconciliation'!A2:F1000"

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'

VALID_SUPPLIERS = {'Asmodee', 'Universal Dist', 'ACDD'}
VALID_STAGES = {'ordered', 'shipped', 'arrived'}

# Column index (within a tracking row) for each stage's "so far" list, and the
# Shopify tag to apply once that stage is fully complete across all suppliers.
STAGE_CONFIG = {
    'ordered': {'col': 2, 'completion_tag': 'dhg-status-order-supplier', 'next_overall': 'Pending Shipment'},
    'shipped': {'col': 3, 'completion_tag': 'dhg-shipped-from-supplier', 'next_overall': 'Pending Arrival'},
    'arrived': {'col': 4, 'completion_tag': 'dhg-status-order-received', 'next_overall': 'Ready to Pack'},
}
OVERALL_STATUS_COL = 5

# ── Google Sheets auth + access ──────────────────────────────────────────────

def get_google_token(scope='https://www.googleapis.com/auth/spreadsheets'):
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

def get_order_id_and_current_status(order_name):
    """Returns (id, dhg-status tag suffix, displayFulfillmentStatus, cancelledAt)."""
    data = shopify_graphql('''
        query getOrder($q: String!) {
            orders(first: 1, query: $q) {
                edges { node { id name tags displayFulfillmentStatus cancelledAt } }
            }
        }
    ''', {'q': f'name:{order_name}'})
    edges = data['orders']['edges']
    if not edges:
        return None, None, None, None
    node = edges[0]['node']
    current_tag = next((t for t in node['tags'] if t.startswith('dhg-status-')), None)
    current_status = current_tag.replace('dhg-status-', '') if current_tag else None
    return node['id'], current_status, node['displayFulfillmentStatus'], node['cancelledAt']

def apply_completion_tag(order_id, tag):
    shopify_graphql('''
        mutation tagsAdd($id: ID!, $tags: [String!]!) {
            tagsAdd(id: $id, tags: $tags) {
                userErrors { field message }
            }
        }
    ''', {'id': order_id, 'tags': [tag]})

def remove_status_tag(order_id, tag):
    shopify_graphql('''
        mutation tagsRemove($id: ID!, $tags: [String!]!) {
            tagsRemove(id: $id, tags: $tags) {
                userErrors { field message }
            }
        }
    ''', {'id': order_id, 'tags': [tag]})

# ── Core logic ───────────────────────────────────────────────────────────────


# Per-supplier reconciliation source. Column indices are 0-based positions
# within each row of the tab's own range. Both tabs share the same tail
# layout (..., Status, Order Names) - Asmodee's just doesn't have a Barcode
# column, so its indices sit one earlier than UD's.
RECONCILE_SOURCES = {
    'Asmodee': {
        'range': RECONCILE_RANGE,
        'status_col': 4,
        'order_names_col': 5,
    },
    'Universal Dist': {
        'range': "'Latest UD Reconciliation'!A2:G1000",
        'status_col': 5,
        'order_names_col': 6,
    },
}

def get_fully_shipped_orders(supplier):
    """Reads that supplier's reconciliation tab and returns the set of order
    names where every one of that supplier's SKUs for the order is Match or
    More-than-submitted (Pre-Order also doesn't count as shipped - it's
    expected to be absent from this week's quote, not actually in hand yet).

    Only suppliers with a reconciliation tab (currently Asmodee and
    Universal Dist) can be checked this way - a supplier missing from
    RECONCILE_SOURCES falls back to the unconditional behavior, since there's
    no itemized source of truth to check against."""
    config = RECONCILE_SOURCES[supplier]
    rows = sheets_get(AGG_SHEET_ID, config['range'])
    status_col = config['status_col']
    order_names_col = config['order_names_col']
    SHIPPED_OK = {'Match', 'More than submitted (likely preorder/backorder)'}
    order_skus_ok = {}
    order_skus_total = {}
    for row in rows:
        if len(row) <= order_names_col:
            continue
        status = row[status_col]
        order_names_str = row[order_names_col]
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

def mark_stage(supplier, stage):
    """Generic handler for all (supplier, stage) button combinations.

    For stage='shipped' and a supplier with a reconciliation tab (currently
    Asmodee and Universal Dist), the itemized reconciliation check applies -
    only orders whose SKUs for that supplier fully matched this week's
    invoice(s) are eligible. This matters because a slow-shipping supplier
    (Universal Dist especially) routinely ships a prior week's order behind
    a newer one; without this check, clicking "shipped" would incorrectly
    close out every order still waiting on that supplier, not just the ones
    that actually arrived. Every other (supplier, stage) combination -
    including suppliers without a reconciliation tab, like ACDD - is
    unconditional: clicking the button marks every order in the tracking tab
    that currently needs this supplier at this stage.

    An order already at dhg-status-inventory-queued is never downgraded -
    it's already fully ready to pack from bin stock, so completion tags from
    earlier pipeline stages would be a regression, not progress.

    Orders that are already Fulfilled or Cancelled in Shopify are dropped
    from the Shipment Tracking tab entirely - they're not carried forward,
    not tagged, and not counted in any result bucket other than
    'removedFulfilledOrCancelled'. Fulfilled = already shipped, Cancelled =
    items unavailable - neither belongs in this tab or any downstream report.
    """
    if supplier not in VALID_SUPPLIERS:
        raise ValueError(f'supplier must be one of {sorted(VALID_SUPPLIERS)}')
    if stage not in VALID_STAGES:
        raise ValueError(f'stage must be one of {sorted(VALID_STAGES)}')

    config = STAGE_CONFIG[stage]
    stage_col = config['col']

    tracking_rows = sheets_get(AGG_SHEET_ID, TRACKING_RANGE)
    if not tracking_rows:
        return {'updated': [], 'completed': [], 'skipped': [], 'message': 'No rows in Shipment Tracking tab.'}

    eligible_orders = None
    if stage == 'shipped' and supplier in RECONCILE_SOURCES:
        eligible_orders = get_fully_shipped_orders(supplier)

    updated_rows = []
    completed_order_names = []
    skipped_order_names = []
    skipped_inventory_queued = []
    removed_fulfilled_or_cancelled = []

    # Pre-fetch current Shopify status for every order on the tracking tab so we
    # can skip inventory-queued orders, and drop Fulfilled/Cancelled orders,
    # BEFORE touching their sheet row at all - not just before applying the
    # completion tag.
    order_names_on_sheet = [row[0] for row in tracking_rows if row]
    current_statuses = {}
    current_fulfillment = {}
    current_cancelled = {}
    for name in order_names_on_sheet:
        try:
            _, status, fulfillment, cancelled_at = get_order_id_and_current_status(name)
            current_statuses[name] = status
            current_fulfillment[name] = fulfillment
            current_cancelled[name] = cancelled_at
        except Exception:
            current_statuses[name] = None
            current_fulfillment[name] = None
            current_cancelled[name] = None

    for row in tracking_rows:
        row = row + [''] * (6 - len(row))  # pad to 6 columns in case sheet has short rows
        order_name = row[0]

        # Drop Fulfilled/Cancelled orders entirely - do not carry them forward,
        # do not tag them, do not report them as skipped-for-later. They're
        # done or dead; they don't belong in this tab.
        if current_fulfillment.get(order_name) == 'FULFILLED' or current_cancelled.get(order_name):
            removed_fulfilled_or_cancelled.append(order_name)
            continue

        if current_statuses.get(order_name) == 'inventory-queued':
            skipped_inventory_queued.append(order_name)
            updated_rows.append(row)
            continue

        suppliers_needed = row[1]
        needed_set = {s.strip() for s in suppliers_needed.split(',') if s.strip()}
        stage_so_far = {s.strip() for s in row[stage_col].split(',') if s.strip()}

        if supplier not in needed_set or supplier in stage_so_far:
            updated_rows.append(row)
            continue

        if eligible_orders is not None and order_name not in eligible_orders:
            skipped_order_names.append(order_name)
            updated_rows.append(row)
            continue

        stage_so_far.add(supplier)
        row[stage_col] = ', '.join(sorted(stage_so_far))
        stage_complete = stage_so_far == needed_set
        if stage_complete:
            row[OVERALL_STATUS_COL] = config['next_overall']
            completed_order_names.append(order_name)
        updated_rows.append(row)

    # Write the updated tracking tab back (Fulfilled/Cancelled rows are simply
    # absent from updated_rows, so this also removes them from the sheet)
    sheets_clear(AGG_SHEET_ID, TRACKING_RANGE)
    if updated_rows:
        sheets_put(AGG_SHEET_ID, f"'Shipment Tracking'!A2:F{len(updated_rows)+1}", updated_rows)

    # Apply the completion tag in Shopify for every order that just finished this stage
    tagged = []
    tag_errors = []
    for order_name in completed_order_names:
        try:
            order_id, current_status, _, _ = get_order_id_and_current_status(order_name)
            if not order_id:
                tag_errors.append({'order': order_name, 'error': 'Order not found in Shopify'})
                continue
            tag = config['completion_tag']
            if tag.startswith('dhg-status-') and current_status:
                remove_status_tag(order_id, f'dhg-status-{current_status}')
            apply_completion_tag(order_id, tag)
            tagged.append(order_name)
        except Exception as e:
            tag_errors.append({'order': order_name, 'error': str(e)})

    return {
        'supplier': supplier,
        'stage': stage,
        'completedAndTagged': tagged,
        'tagErrors': tag_errors,
        'skippedNotYetFullyMatched': skipped_order_names,
        'skippedAlreadyInventoryQueued': skipped_inventory_queued,
        'removedFulfilledOrCancelled': removed_fulfilled_or_cancelled,
    }

# ── HTTP handler ─────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length else b'{}'
            payload = json.loads(body) if body else {}
            supplier = payload.get('supplier')
            stage = payload.get('stage')
            try:
                result = mark_stage(supplier, stage)
            except ValueError as e:
                self._send_json(400, {'error': str(e)})
                return
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
