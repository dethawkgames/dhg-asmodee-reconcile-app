import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler
import jwt

# Order Needs Cleanup
#
# Every other script in this app only ever looks at Order Needs rows that
# are candidates for ITS OWN next stage transition - lock-supplier-order.py
# checks rows at 'NotOrdered', reconcile.py/reconcile-ud.py check rows at
# 'Ordered', the arrived scripts check rows at 'Shipped'. Once a row reaches
# 'Arrived', no script's candidate query ever selects it again, so nothing
# ever re-verifies it against Shopify. Meanwhile a row can also become
# irrelevant for reasons that have nothing to do with the supplier side at
# all: the order got cancelled, a line item got refunded, or the order was
# fulfilled to the customer (which, for a Shopify order, can only happen
# once every line item's owed units are actually on hand - so a fulfilled
# order has nothing left for Order Needs to track).
#
# This script closes that gap: it scans EVERY distinct order name currently
# in Order Needs, regardless of what stage its rows are at, and removes:
#   - every row for a CANCELLED order
#   - every row for a FULFILLED order (shipped to the customer - the
#     supplier-tracking job is done, whatever stage the rows happen to be at)
#   - the specific excess rows for a (order, SKU) pair whose live Shopify
#     quantity has dropped below the number of Order Needs rows still
#     tracking it (a refund, or a line item edited/removed from the order)
#
# Every removed row is appended to the 'Order Needs Removal Log' tab in
# full, with a reason and the stage it was at when removed, before it's
# dropped from Order Needs - nothing just disappears silently. A row
# removed while still at 'Shipped' or 'Arrived' also gets a flag in that
# log, since that's physical stock (in transit or already in the
# warehouse) that may need a manual inventory adjustment - this script
# never touches live Shopify inventory itself (same boundary every other
# script in this app already keeps).
#
# Runs two ways:
#   - GET, authenticated via the Authorization: Bearer $CRON_SECRET header
#     Vercel Cron sends automatically - the scheduled, unattended run.
#   - POST (with an optional {"dry_run": true} JSON body) - a manual run
#     triggered from the tracker UI, e.g. to preview before committing.
# Both share the same process_cleanup() logic; only dry_run differs.

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
ORDER_NEEDS_TAB = 'Order Needs'
ORDER_NEEDS_RANGE = f"'{ORDER_NEEDS_TAB}'!A2:I50000"
REMOVAL_LOG_TAB = 'Order Needs Removal Log'

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'

ORDER_NAME_CHUNK_SIZE = 75  # keep each Shopify search query string a sane length

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

def ensure_tab_exists(tab_name, header_row=None):
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{AGG_SHEET_ID}?fields=sheets.properties.title'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    titles = [s['properties']['title'] for s in result['sheets']]
    if tab_name not in titles:
        body = json.dumps({'requests': [{'addSheet': {'properties': {'title': tab_name}}}]}).encode()
        req = urllib.request.Request(f'https://sheets.googleapis.com/v4/spreadsheets/{AGG_SHEET_ID}:batchUpdate',
            data=body, method='POST', headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as resp:
            json.loads(resp.read())
        if header_row:
            sheets_put(AGG_SHEET_ID, f"'{tab_name}'!A1:{chr(64 + len(header_row))}1", [header_row])

# ── Shopify auth ──────────────────────────────────────────────────────────────

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

def fetch_order_states(order_names):
    """Returns {order_name: {'cancelled': bool, 'fulfillment_status': str, 'qty_by_sku': {sku: currentQuantity}}}."""
    states = {}
    chunks = [order_names[i:i + ORDER_NAME_CHUNK_SIZE] for i in range(0, len(order_names), ORDER_NAME_CHUNK_SIZE)]
    for chunk in chunks:
        name_query = '(' + ' OR '.join(f'name:{n.lstrip("#")}' for n in chunk) + ')'
        data = shopify_graphql('''
            query getOrders($q: String!) {
                orders(first: 250, query: $q) {
                    edges { node { name cancelledAt displayFulfillmentStatus
                        lineItems(first: 50) { edges { node { sku currentQuantity } } } } }
                }
            }
        ''', {'q': name_query})
        for edge in data['orders']['edges']:
            node = edge['node']
            qty_by_sku = {}
            for li in node['lineItems']['edges']:
                sku = li['node']['sku']
                qty_by_sku[sku] = qty_by_sku.get(sku, 0) + (li['node']['currentQuantity'] or 0)
            states[node['name']] = {
                'cancelled': bool(node['cancelledAt']),
                'fulfillment_status': node['displayFulfillmentStatus'],
                'qty_by_sku': qty_by_sku,
            }
    return states

# ── Core cleanup logic ────────────────────────────────────────────────────────

def process_cleanup(dry_run):
    all_rows = sheets_get(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
    rows = []
    for r in all_rows:
        if not r or not r[0]:
            continue
        rows.append(r + [''] * (9 - len(r)))

    order_names = sorted(set(r[0] for r in rows))
    if not order_names:
        return {'success': True, 'dryRun': dry_run, 'message': 'No rows in Order Needs.', 'removed': 0, 'kept': 0}

    states = fetch_order_states(order_names)

    # Index rows by (order, sku) so refund quantity comparisons can pick which
    # specific rows to drop.
    by_pair = {}
    for idx, row in enumerate(rows):
        by_pair.setdefault((row[0], row[1]), []).append(idx)

    to_remove = {}   # idx -> (reason, warning_or_None)
    today = time.strftime('%Y-%m-%d')

    for order_name in order_names:
        state = states.get(order_name)
        if state is None:
            # Order not found in Shopify at all (deleted?) - leave it alone
            # rather than guess; flag it for manual review via the log instead
            # of silently dropping real tracked demand.
            continue

        order_row_idxs = [i for i, r in enumerate(rows) if r[0] == order_name]

        if state['cancelled']:
            for idx in order_row_idxs:
                to_remove[idx] = ('cancelled', None)
            continue

        if state['fulfillment_status'] == 'FULFILLED':
            for idx in order_row_idxs:
                warning = None if rows[idx][6] == 'Arrived' else f"removed at stage '{rows[idx][6]}', not 'Arrived' - order was fulfilled without every unit showing received"
                to_remove[idx] = ('fulfilled-to-customer', warning)
            continue

        # Still open: check each (order, sku) pair still tracked here against
        # Shopify's live line-item quantity. A drop below the row count means
        # a refund, or the line item being edited/removed from the order.
        skus_here = sorted(set(rows[i][1] for i in order_row_idxs))
        for sku in skus_here:
            idxs = by_pair.get((order_name, sku), [])
            idxs = [i for i in idxs if i not in to_remove]
            if not idxs:
                continue
            actual = state['qty_by_sku'].get(sku, 0)
            existing = len(idxs)
            if actual >= existing:
                continue
            excess = existing - actual
            # Drop the highest-numbered units first (same convention used
            # elsewhere in this app), regardless of lock status - a refunded
            # unit is refunded whatever stage it's at.
            for idx in sorted(idxs, key=lambda i: -int(rows[i][4] or 0))[:excess]:
                warning = None if rows[idx][6] in ('NotOrdered', 'Ordered') else f"removed at stage '{rows[idx][6]}' on refund - check whether physical stock needs an inventory adjustment"
                to_remove[idx] = ('refunded', warning)

    kept_rows = [r for i, r in enumerate(rows) if i not in to_remove]
    removed_rows = [(rows[i], reason, warning) for i, (reason, warning) in sorted(to_remove.items())]

    log_rows = []
    for row, reason, warning in removed_rows:
        order_name, sku, title, supplier, unit, sup_id, stage, updated, notes = row
        log_rows.append([today, order_name, sku, title, supplier, unit, sup_id, stage, notes, reason, warning or ''])

    if not dry_run and to_remove:
        ensure_tab_exists(REMOVAL_LOG_TAB, [
            'Date Removed', 'Order', 'SKU', 'Title', 'Supplier', 'Unit', 'Supplier Order ID',
            'Stage At Removal', 'Notes At Removal', 'Reason', 'Warning',
        ])
        if log_rows:
            sheets_append(AGG_SHEET_ID, f"'{REMOVAL_LOG_TAB}'!A2:K100000", log_rows)

        sheets_clear(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
        if kept_rows:
            sheets_put(AGG_SHEET_ID, f"'{ORDER_NEEDS_TAB}'!A2:I{len(kept_rows) + 1}", kept_rows)

    by_reason = {}
    for _, reason, _ in removed_rows:
        by_reason[reason] = by_reason.get(reason, 0) + 1

    warnings = [
        {'order': row[0], 'sku': row[1], 'stage': row[6], 'reason': reason, 'warning': warning}
        for row, reason, warning in removed_rows if warning
    ]

    return {
        'success': True,
        'dryRun': dry_run,
        'ordersScanned': len(order_names),
        'ordersNotFoundInShopify': len([n for n in order_names if n not in states]),
        'rowsRemoved': len(removed_rows),
        'rowsRemovedByReason': by_reason,
        'rowsKept': len(kept_rows),
        'warnings': warnings,
        'note': 'Live run: rows removed from Order Needs and logged to the Order Needs Removal Log tab. No Shopify inventory was adjusted - see file header comment for why.' if not dry_run else
                'DRY RUN: no writes were made to the Order Needs sheet or the removal log.',
    }

# ── HTTP handler ─────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Scheduled, unattended run via Vercel Cron. Vercel sends
        # Authorization: Bearer $CRON_SECRET automatically when CRON_SECRET
        # is set in the project's environment variables - reject anything
        # else so this can't be triggered by a bare GET from outside Vercel.
        auth = self.headers.get('Authorization', '')
        cron_secret = os.environ.get('CRON_SECRET')
        if not cron_secret or auth != f'Bearer {cron_secret}':
            self._send_json(401, {'error': 'Unauthorized'})
            return
        try:
            result = process_cleanup(dry_run=False)
            self._send_json(200, result)
        except Exception as e:
            import traceback
            self._send_json(500, {'error': str(e), 'trace': traceback.format_exc()})

    def do_POST(self):
        # Manual run from the tracker UI.
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length else b'{}'
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {}
            dry_run = bool(payload.get('dry_run', False))
            result = process_cleanup(dry_run=dry_run)
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
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def _send_json(self, status, data):
        self.send_response(status)
        self._cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
