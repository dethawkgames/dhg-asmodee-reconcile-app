import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler
import jwt

# Mark Arrived (v2)
#
# This is the ONLY stage mark-stage.py still handles. 'Ordered' is now
# handled by lock-supplier-order.py (Lock & Order), and 'Shipped' is now
# handled by reconcile.py / reconcile-ud.py (driven directly by an actual
# invoice, not a manual button click). Arrived has no equivalent automated
# document to parse against - it still requires Iain physically confirming
# what showed up, so it stays a manual action here.
#
# Unlike v1, this only ever advances rows that are already at 'Shipped' for
# the given supplier - a row still sitting at 'Ordered' or 'NotOrdered' is
# structurally ineligible, so clicking this can never mark something arrived
# that was never even confirmed shipped.

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
ORDER_NEEDS_TAB = 'Order Needs'
ORDER_NEEDS_RANGE = f"'{ORDER_NEEDS_TAB}'!A2:H50000"

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'

VALID_SUPPLIERS = {'Asmodee', 'Universal Dist', 'ACDD'}
STAGE_ORDER = ['NotOrdered', 'Ordered', 'Shipped', 'Arrived']

EMAIL_LIFECYCLE_TAGS = {
    'dhg-status-store-first-order', 'dhg-status-shop-first-order',
    'dhg-status-order-placed', 'dhg-status-preorder',
}

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

def sheets_clear(spreadsheet_id, range_str):
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str)}:clear'
    req = urllib.request.Request(url, data=b'', method='POST', headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def sheets_append(spreadsheet_id, range_str, values):
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str)}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS'
    body = json.dumps({'values': values}).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

# ── Shopify auth + tagging ───────────────────────────────────────────────────

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

def get_order_details(order_name):
    """Returns (id, current_status, displayFulfillmentStatus, cancelledAt, lineItems).
    lineItems includes sku, product tags, and currentQuantity (used for the
    per-unit cancellation/refund check below, not just whole-order status)."""
    data = shopify_graphql('''
        query getOrder($q: String!) {
            orders(first: 1, query: $q) {
                edges {
                    node {
                        id name tags displayFulfillmentStatus cancelledAt
                        lineItems(first: 50) {
                            edges { node { sku currentQuantity product { tags } } }
                        }
                    }
                }
            }
        }
    ''', {'q': f'name:{order_name}'})
    edges = data['orders']['edges']
    if not edges:
        return None, None, None, None, []
    node = edges[0]['node']
    current_tag = next((t for t in node['tags'] if t.startswith('dhg-status-') and t not in EMAIL_LIFECYCLE_TAGS), None)
    current_status = current_tag.replace('dhg-status-', '') if current_tag else None
    line_items = [edge['node'] for edge in node['lineItems']['edges']]
    return node['id'], current_status, node['displayFulfillmentStatus'], node['cancelledAt'], line_items

def apply_completion_tag(order_id, tag):
    shopify_graphql('''
        mutation tagsAdd($id: ID!, $tags: [String!]!) { tagsAdd(id: $id, tags: $tags) { userErrors { field message } } }
    ''', {'id': order_id, 'tags': [tag]})

def remove_status_tag(order_id, tag):
    shopify_graphql('''
        mutation tagsRemove($id: ID!, $tags: [String!]!) { tagsRemove(id: $id, tags: $tags) { userErrors { field message } } }
    ''', {'id': order_id, 'tags': [tag]})

# ── Preorder-hold determination ──────────────────────────────────────────────
# An order shouldn't get the normal "received" email if it still contains a
# preorder item whose release date is more than 3 days out - that would tell
# a customer their order is ready before it's legally allowed to ship. This
# logic was designed in an earlier session but never actually deployed -
# building it for real now as part of this rewrite.

import re
from datetime import datetime, timezone, timedelta

RELEASE_DATE_TAG_RE = re.compile(r'^release-date-(\d{4}-\d{2}-\d{2})$')
HOLD_WINDOW_DAYS = 3

def determine_arrived_tag(line_items):
    """Given an order's Shopify line items (with product tags), returns
    'order-received-preorder' if any line item is tagged 'preorder' and its
    matching release-date-YYYY-MM-DD tag is more than HOLD_WINDOW_DAYS out;
    otherwise returns the normal 'order-received'."""
    latest_release_date = None
    for item in line_items:
        tags = [t.lower() for t in (item.get('product', {}).get('tags') or [])]
        if 'preorder' not in tags:
            continue
        for t in item['product']['tags']:
            m = RELEASE_DATE_TAG_RE.match(t.strip())
            if m:
                try:
                    d = datetime.strptime(m.group(1), '%Y-%m-%d').replace(tzinfo=timezone.utc)
                    if latest_release_date is None or d > latest_release_date:
                        latest_release_date = d
                except ValueError:
                    continue
    if latest_release_date is None:
        return 'order-received'
    if latest_release_date - datetime.now(timezone.utc) > timedelta(days=HOLD_WINDOW_DAYS):
        return 'order-received-preorder'
    return 'order-received'

# ── Core logic ────────────────────────────────────────────────────────────

def mark_arrived(supplier):
    """Advances every Order Needs row for the given supplier that's
    currently at 'Shipped' to 'Arrived'. Rows at 'Ordered' or 'NotOrdered'
    are untouched - Arrived can only ever be reached from Shipped.

    Orders already Fulfilled or Cancelled in Shopify have their Order Needs
    rows dropped entirely, same protection as v1. An order already at
    dhg-status-inventory-queued is never downgraded.
    """
    if supplier not in VALID_SUPPLIERS:
        raise ValueError(f'supplier must be one of {sorted(VALID_SUPPLIERS)}')

    rows = sheets_get(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
    if not rows:
        return {'arrived': False, 'message': 'No rows in Order Needs tab.'}

    today = time.strftime('%Y-%m-%d')
    order_names_on_sheet = sorted({row[0] for row in rows if row and row[0]})

    current_details = {}
    for name in order_names_on_sheet:
        try:
            current_details[name] = get_order_details(name)
        except Exception:
            current_details[name] = (None, None, None, None, [])

    padded_rows = []
    removed_fulfilled_or_cancelled = set()
    advanced_count = 0
    touched_orders = set()

    # Build actual current-quantity map per (order, sku) from the same data
    # already fetched above, for the per-unit cancellation/refund check.
    current_qty = {}
    for name, (order_id, current_status, fulfillment, cancelled_at, line_items) in current_details.items():
        if cancelled_at:
            current_qty[name] = {}
            continue
        qtys = {}
        for li in line_items:
            qtys[li['sku']] = qtys.get(li['sku'], 0) + (li.get('currentQuantity') or 0)
        current_qty[name] = qtys

    by_pair = {}
    for idx, row in enumerate(rows):
        if not row or not row[0]:
            continue
        by_pair.setdefault((row[0], row[1]), []).append(idx)

    to_delete = set()
    blocked_pairs = set()
    manual_review_flags = []
    for (order_name, sku), idxs in by_pair.items():
        if order_name not in current_qty:
            continue
        actual = current_qty[order_name].get(sku, 0)
        existing = len(idxs)
        if actual >= existing:
            continue
        excess = existing - actual
        unlocked = [i for i in idxs if not rows[i][5]]
        if len(unlocked) >= excess:
            for i in sorted(unlocked, key=lambda i: -int(rows[i][4]))[:excess]:
                to_delete.add(i)
        else:
            blocked_pairs.add((order_name, sku))
            manual_review_flags.append([
                order_name, sku, rows[idxs[0]][2], '',
                f'Shopify qty dropped to {actual} but {existing} Order Needs rows exist '
                f'and only {len(unlocked)} are unlocked - {excess - len(unlocked)} committed '
                f'unit(s) need manual reconciliation (cancellation/refund detected)',
                today,
            ])
    if manual_review_flags:
        sheets_append(AGG_SHEET_ID, "'Needs Manual Review'!A2:F1000", manual_review_flags)

    for idx, row in enumerate(rows):
        if not row or not row[0]:
            continue
        if idx in to_delete:
            continue
        row = row + [''] * (8 - len(row))
        order_name, sku, title, row_supplier, unit, sup_id, stage, updated = row

        order_id, current_status, fulfillment, cancelled_at, line_items = current_details.get(order_name, (None, None, None, None, []))
        if fulfillment == 'FULFILLED' or cancelled_at:
            removed_fulfilled_or_cancelled.add(order_name)
            continue

        if row_supplier == supplier and stage == 'Shipped' and (order_name, sku) not in blocked_pairs:
            row[6] = 'Arrived'
            row[7] = today
            advanced_count += 1
            touched_orders.add(order_name)

        padded_rows.append(row)

    sheets_clear(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
    if padded_rows:
        sheets_put(AGG_SHEET_ID, f"'{ORDER_NEEDS_TAB}'!A2:H{len(padded_rows) + 1}", padded_rows)

    # Recompute order-level completion for every touched order
    rows_by_order = {}
    for row in padded_rows:
        rows_by_order.setdefault(row[0], []).append(row)

    tagged, tag_errors, skipped_inventory_queued = [], [], []
    for order_name in touched_orders:
        order_rows = rows_by_order.get(order_name, [])
        fully_arrived = all(STAGE_ORDER.index(r[6]) >= STAGE_ORDER.index('Arrived') for r in order_rows)
        if not fully_arrived:
            continue
        order_id, current_status, fulfillment, cancelled_at, line_items = current_details.get(order_name, (None, None, None, None, []))
        if not order_id:
            tag_errors.append({'order': order_name, 'error': 'Order not found in Shopify'})
            continue
        if current_status == 'inventory-queued':
            skipped_inventory_queued.append(order_name)
            continue
        try:
            tag_suffix = determine_arrived_tag(line_items)
            tag = f'dhg-status-{tag_suffix}'
            if current_status:
                remove_status_tag(order_id, f'dhg-status-{current_status}')
            apply_completion_tag(order_id, tag)
            tagged.append({'order': order_name, 'tag': tag})
        except Exception as e:
            tag_errors.append({'order': order_name, 'error': str(e)})

    return {
        'arrived': True,
        'supplier': supplier,
        'unitsAdvanced': advanced_count,
        'ordersTouched': sorted(touched_orders),
        'ordersFullyArrivedAndTagged': tagged,
        'skippedAlreadyInventoryQueued': skipped_inventory_queued,
        'removedFulfilledOrCancelled': sorted(removed_fulfilled_or_cancelled),
        'tagErrors': tag_errors,
        'blockedByCancellationOrRefund': [{'order': o, 'sku': s} for o, s in sorted(blocked_pairs)],
    }

# ── HTTP handler ─────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length else b'{}'
            payload = json.loads(body) if body else {}
            supplier = payload.get('supplier')
            try:
                result = mark_arrived(supplier)
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
