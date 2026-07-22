import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler
import jwt

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
ORDER_NEEDS_TAB = 'Order Needs'
ORDER_NEEDS_RANGE = f"'{ORDER_NEEDS_TAB}'!A2:H50000"
SUPPLIER_ORDERS_LOG_TAB = 'Supplier Orders Log'

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'

VALID_SUPPLIERS = {'Asmodee', 'Universal Dist', 'ACDD'}
SUPPLIER_ABBR = {'Asmodee': 'Asmodee', 'Universal Dist': 'UD', 'ACDD': 'ACDD'}

# These are set once by the welcome-email system (lib/status.js in
# dhg-automation) and are NOT fulfillment-stage tags, even though they share
# the dhg-status- prefix. Fulfillment tagging must never remove these -
# doing so silently destroys email-lifecycle data unrelated to supplier
# ordering. They simply coexist alongside whatever real fulfillment-stage
# tag gets applied.
EMAIL_LIFECYCLE_TAGS = {
    'dhg-status-store-first-order',
    'dhg-status-shop-first-order',
    'dhg-status-order-placed',
    'dhg-status-preorder',
}

# Stage progression, low to high. A row's stage only ever moves rightward.
STAGE_ORDER = ['NotOrdered', 'Ordered', 'Shipped', 'Arrived']

# ── Google Sheets auth + access (same pattern as mark-stage.py) ─────────────

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
        'iss': sa_email, 'scope': scope, 'aud': 'https://oauth2.googleapis.com/token',
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

def sheets_append(spreadsheet_id, range_str, values):
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str)}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS'
    body = json.dumps({'values': values}).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={
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

# ── Shopify auth + tagging (same pattern as mark-stage.py) ──────────────────

def get_shopify_token():
    client_id = os.environ['SHOPIFY_CLIENT_ID']
    client_secret = os.environ['SHOPIFY_CLIENT_SECRET']
    data = urllib.parse.urlencode({
        'grant_type': 'client_credentials', 'client_id': client_id, 'client_secret': client_secret,
    }).encode()
    req = urllib.request.Request(f'https://{SHOPIFY_SHOP}/admin/oauth/access_token', data=data, method='POST')
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
    data = shopify_graphql('''
        query getOrder($q: String!) {
            orders(first: 1, query: $q) { edges { node { id name tags } } }
        }
    ''', {'q': f'name:{order_name}'})
    edges = data['orders']['edges']
    if not edges:
        return None, None
    node = edges[0]['node']
    # Only consider genuine fulfillment-stage tags as "current status" -
    # email-lifecycle tags (store-first-order, shop-first-order,
    # order-placed, preorder) are a separate lineage and must be ignored
    # here so they never get removed by fulfillment tagging.
    current_tag = next(
        (t for t in node['tags'] if t.startswith('dhg-status-') and t not in EMAIL_LIFECYCLE_TAGS),
        None
    )
    current_status = current_tag.replace('dhg-status-', '') if current_tag else None
    return node['id'], current_status

def apply_completion_tag(order_id, tag):
    shopify_graphql('''
        mutation tagsAdd($id: ID!, $tags: [String!]!) {
            tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
        }
    ''', {'id': order_id, 'tags': [tag]})

def remove_status_tag(order_id, tag):
    shopify_graphql('''
        mutation tagsRemove($id: ID!, $tags: [String!]!) {
            tagsRemove(id: $id, tags: $tags) { userErrors { field message } }
        }
    ''', {'id': order_id, 'tags': [tag]})

# ── Cancellation / refund safety check ───────────────────────────────────
# Customers can cancel a whole order, cancel individual line items, or
# refund part of a quantity - and this can happen after a unit is already
# committed to a supplier order. Every stage action re-checks Shopify's
# CURRENT line-item quantity for any order it's about to touch before
# acting, so nothing gets locked/shipped/arrived for a unit that's no
# longer actually needed.

def reconcile_against_shopify(rows, touched_order_names):
    """Cross-checks Order Needs rows for the given orders against Shopify's
    live line-item quantities. Removes unlocked (no Supplier Order ID)
    excess rows automatically. Excess among LOCKED rows gets flagged to
    'Needs Manual Review' instead of silently un-committing something
    already ordered from a supplier - those (order, sku) pairs are excluded
    from this run entirely so the calling action can't touch them.

    Returns (cleaned_rows, blocked_pairs).
    """
    if not touched_order_names:
        return rows, set()

    name_query = '(' + ' OR '.join(f'name:{n.lstrip("#")}' for n in touched_order_names) + ')'
    data = shopify_graphql('''
        query getOrders($q: String!) {
            orders(first: 250, query: $q) {
                edges {
                    node {
                        name cancelledAt
                        lineItems(first: 50) { edges { node { sku currentQuantity } } }
                    }
                }
            }
        }
    ''', {'q': name_query})

    current_qty = {}
    for edge in data['orders']['edges']:
        node = edge['node']
        if node['cancelledAt']:
            current_qty[node['name']] = {}
            continue
        qtys = {}
        for li in node['lineItems']['edges']:
            sku = li['node']['sku']
            qtys[sku] = qtys.get(sku, 0) + (li['node']['currentQuantity'] or 0)
        current_qty[node['name']] = qtys

    by_pair = {}
    for idx, row in enumerate(rows):
        if not row or not row[0]:
            continue
        by_pair.setdefault((row[0], row[1]), []).append(idx)

    to_delete = set()
    blocked_pairs = set()
    manual_review_flags = []
    today = time.strftime('%Y-%m-%d')

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

    cleaned_rows = [r for i, r in enumerate(rows) if i not in to_delete]
    return cleaned_rows, blocked_pairs

# ── Core logic ────────────────────────────────────────────────────────────

def lock_supplier_order(supplier):
    """Locks every currently-unassigned (blank Supplier Order ID, NotOrdered
    stage) Order Needs row for the given supplier under a single new
    date-stamped Supplier Order ID, advances those rows to 'Ordered', logs
    the submission to Supplier Orders Log, and applies the
    dhg-status-order-supplier Shopify tag to any order whose EVERY needed
    unit (across all suppliers, not just this one) has now reached at least
    'Ordered'. A row that's already inventory-queued in Shopify is skipped
    for tagging (never downgraded), matching the old mark-stage.py behavior.
    """
    if supplier not in VALID_SUPPLIERS:
        raise ValueError(f'supplier must be one of {sorted(VALID_SUPPLIERS)}')

    rows = sheets_get(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
    if not rows:
        return {'locked': False, 'message': 'No rows in Order Needs tab.'}

    # Safety check: re-verify against Shopify's live quantities before
    # locking anything, in case a customer cancelled or refunded (in whole
    # or in part) since these rows were created.
    candidate_order_names = sorted({
        row[0] for row in rows
        if row and row[0] and len(row) >= 7 and row[3] == supplier and not row[5] and row[6] == 'NotOrdered'
    })
    rows, blocked_pairs = reconcile_against_shopify(rows, candidate_order_names)

    # Generate a unique ID for today - if this supplier was already locked
    # today (a second lock in the same day), suffix with -2, -3, etc.
    today = time.strftime('%Y-%m-%d')
    base_id = f'{SUPPLIER_ABBR[supplier]}-{today}'
    log_rows = sheets_get(AGG_SHEET_ID, f"'{SUPPLIER_ORDERS_LOG_TAB}'!A2:F10000")
    existing_ids = {r[0] for r in log_rows if r}
    new_id = base_id
    suffix = 2
    while new_id in existing_ids:
        new_id = f'{base_id}-{suffix}'
        suffix += 1

    locked_rows = []       # (row_index_in_body, row) that got locked just now
    touched_orders = set()
    sku_agg = {}            # sku -> {title, qty, order_names}

    for idx, row in enumerate(rows):
        if not row or not row[0]:
            continue
        row = row + [''] * (8 - len(row))
        order_name, sku, title, row_supplier, unit, sup_id, stage, updated = row
        if row_supplier != supplier or sup_id or stage != 'NotOrdered':
            continue
        if (order_name, sku) in blocked_pairs:
            continue
        row[5] = new_id
        row[6] = 'Ordered'
        row[7] = today
        rows[idx] = row
        locked_rows.append(row)
        touched_orders.add(order_name)
        if sku not in sku_agg:
            sku_agg[sku] = {'title': title, 'qty': 0, 'order_names': set()}
        sku_agg[sku]['qty'] += 1
        sku_agg[sku]['order_names'].add(order_name)

    if not locked_rows:
        return {'locked': False, 'message': f'Nothing currently unlocked for {supplier} - nothing to order.'}

    # Write the updated Order Needs tab back in full (clear first, since the
    # cancellation/refund check above may have shrunk the row count)
    sheets_clear(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
    if rows:
        sheets_put(AGG_SHEET_ID, f"'{ORDER_NEEDS_TAB}'!A2:H{len(rows) + 1}", rows)

    # Append to the permanent Supplier Orders Log - one row per SKU
    log_append_rows = []
    for sku, v in sorted(sku_agg.items()):
        log_append_rows.append([new_id, supplier, today, sku, v['qty'], ', '.join(sorted(v['order_names']))])
    sheets_append(AGG_SHEET_ID, f"'{SUPPLIER_ORDERS_LOG_TAB}'!A2:F10000", log_append_rows)

    # Recompute order-level completion: fetch ALL rows again post-write to
    # check whether every row for a touched order (across every supplier it
    # needs, not just this one) is now at 'Ordered' or beyond.
    all_rows = sheets_get(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
    rows_by_order = {}
    for row in all_rows:
        if not row or not row[0]:
            continue
        row = row + [''] * (8 - len(row))
        rows_by_order.setdefault(row[0], []).append(row)

    tagged = []
    tag_errors = []
    skipped_inventory_queued = []
    for order_name in touched_orders:
        order_rows = rows_by_order.get(order_name, [])
        fully_ordered = all(STAGE_ORDER.index(r[6]) >= STAGE_ORDER.index('Ordered') for r in order_rows)
        if not fully_ordered:
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
            apply_completion_tag(order_id, 'dhg-status-order-supplier')
            tagged.append(order_name)
        except Exception as e:
            tag_errors.append({'order': order_name, 'error': str(e)})

    return {
        'locked': True,
        'supplierOrderId': new_id,
        'supplier': supplier,
        'unitsLocked': len(locked_rows),
        'ordersAffected': sorted(touched_orders),
        'skuOrderList': [
            {'sku': sku, 'title': v['title'], 'qty': v['qty'], 'orderNames': sorted(v['order_names'])}
            for sku, v in sorted(sku_agg.items())
        ],
        'ordersFullyOrderedAndTagged': tagged,
        'skippedAlreadyInventoryQueued': skipped_inventory_queued,
        'tagErrors': tag_errors,
        'blockedByCancellationOrRefund': [{'order': o, 'sku': s} for o, s in sorted(blocked_pairs)],
    }

# ── HTTP handler ─────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length else b'{}'
            payload = json.loads(body) if body else {}
            supplier = payload.get('supplier')
            try:
                result = lock_supplier_order(supplier)
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
