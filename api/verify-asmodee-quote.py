import json
import os
import time
import cgi
import io
from http.server import BaseHTTPRequestHandler
import pdfplumber
import jwt
import urllib.request
import urllib.parse
import urllib.error

# Verifies an Asmodee Sales Quote against what was actually Locked with this
# supplier under the most recent Supplier Order ID, BEFORE the order goes to
# the warehouse. It exists to catch Iain-side or sales-rep-side mistakes
# (wrong SKU, wrong quantity, missed item) while they're still cheap to fix,
# i.e. before the warehouse ships anything.
#
# IMPORTANT (2026-09-14 fix): lock-supplier-order.py sets Stage straight to
# 'Ordered' the moment a row is locked, before any quote has confirmed the
# supplier actually accepted it. If a SKU never makes it onto the real quote
# (Asmodee out of stock, rep missed it, etc.), that row used to sit
# permanently mislabeled 'Ordered' with no way back in - the tracker would
# keep reporting the order as fully ordered even though nothing was ever
# actually placed for that unit, and lock-supplier-order.py would never pick
# it up again since it only looks for rows already back at 'NotOrdered'.
#
# This script now closes that loop: any SKU that's Locked under the latest
# Supplier Order ID but MISSING from the uploaded quote gets automatically
# reverted here - Supplier Order ID cleared, Stage set back to 'NotOrdered' -
# so the next Lock & Order run for Asmodee picks it back up on its own.
# Quantity mismatches and quote-but-not-locked extras are left alone and
# only reported, since those need a human judgment call (which number is
# right, whether an extra was intentional padding, etc.) rather than an
# automatic revert. This script still never touches Shopify tags - that
# stays the job of lock-supplier-order.py / reconcile.py.

AGG_SHEET_ID = '1rsUU7qZJZGhivsofBiFPa7FK6qnHosrxps10NYzLxAE'
ORDER_NEEDS_TAB = 'Order Needs'
ORDER_NEEDS_RANGE = f"'{ORDER_NEEDS_TAB}'!A2:H50000"
SUPPLIER_ORDERS_LOG_TAB = 'Supplier Orders Log'
SUPPLIER = 'Asmodee'

# ── Quote PDF parsing (identical to the original reconcile.py) ──────────────

SKU_X = 43.2
DESC_X = 101.6
MSRP_X_MIN = 350
QTY_X_MIN = 415
QTY_X_MAX = 435

def parse_asmodee_quote(file_bytes):
    line_items = []
    hit_total = False
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            if hit_total:
                break
            words = page.extract_words()
            lines = {}
            for w in words:
                top_key = round(w['top'])
                lines.setdefault(top_key, []).append(w)
            sorted_tops = sorted(lines.keys())
            current_item = None
            header_seen = False
            for top in sorted_tops:
                row_words = sorted(lines[top], key=lambda w: w['x0'])
                row_text = ' '.join(w['text'] for w in row_words)
                if row_text.strip().startswith('Total $') or row_text.strip().startswith('Total'):
                    hit_total = True
                    break
                if 'Description' in row_text and 'MSRP' in row_text:
                    header_seen = True
                    continue
                if not header_seen:
                    continue
                if row_text.strip() in ('Line', 'Amount', 'Unit', 'Excl.', 'Tax') or row_text.strip().startswith('Line Amount'):
                    continue
                qty_word = next((w for w in row_words if QTY_X_MIN <= w['x0'] <= QTY_X_MAX), None)
                sku_word = next((w for w in row_words if abs(w['x0'] - SKU_X) < 2), None)
                if qty_word and sku_word:
                    if current_item:
                        line_items.append(current_item)
                    desc_words = [w['text'] for w in row_words if w['x0'] >= DESC_X and w['x0'] < MSRP_X_MIN]
                    qty_val = qty_word['text']
                    current_item = {
                        'sku': sku_word['text'], 'description': ' '.join(desc_words),
                        'quantity': int(qty_val) if qty_val.isdigit() else qty_val,
                    }
                elif qty_word and not sku_word and current_item is not None:
                    desc_words = [w['text'] for w in row_words if w['x0'] >= DESC_X and w['x0'] < MSRP_X_MIN]
                    if desc_words:
                        line_items.append(current_item)
                        qty_val = qty_word['text']
                        current_item = {
                            'sku': None, 'description': ' '.join(desc_words),
                            'quantity': int(qty_val) if qty_val.isdigit() else qty_val, 'is_fee': True,
                        }
                elif sku_word and not qty_word and current_item is not None:
                    fragment = row_words[0]['text']
                    if len(row_words) == 1 and len(fragment) <= 6:
                        current_item['sku'] = current_item['sku'] + fragment
            if current_item:
                line_items.append(current_item)
    return [item for item in line_items if not item.get('is_fee')]

# ── Google Sheets auth + access ──────────────────────────────────────────────
# Scope upgraded from readonly to read/write (2026-09-14) so this script can
# perform the auto-revert described above, not just report mismatches.

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

def sheets_batch_update(spreadsheet_id, range_value_pairs):
    """range_value_pairs: list of (range_str, [[v1, v2, ...]]) tuples."""
    if not range_value_pairs:
        return
    token = get_google_token()
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchUpdate'
    body = json.dumps({
        'valueInputOption': 'RAW',
        'data': [{'range': r, 'values': v} for r, v in range_value_pairs],
    }).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

# ── Comparison logic ─────────────────────────────────────────────────────────

def get_latest_supplier_order_id():
    """Finds the most recently created Supplier Order ID for Asmodee, by
    looking at the Date Locked column. PREEXISTING backlog entries are never
    the 'latest' - they're excluded since they predate this whole ID system."""
    rows = sheets_get(AGG_SHEET_ID, f"'{SUPPLIER_ORDERS_LOG_TAB}'!A2:F10000")
    candidates = {}  # id -> date string
    for row in rows:
        if not row or len(row) < 3:
            continue
        sup_id, supplier, date_locked = row[0], row[1], row[2]
        if supplier != SUPPLIER or sup_id.endswith('-PREEXISTING'):
            continue
        candidates[sup_id] = date_locked
    if not candidates:
        return None
    # IDs are date-prefixed (Asmodee-YYYY-MM-DD[-N]) so lexical max = most recent
    return max(candidates.keys())

def load_locked_skus(supplier_order_id):
    rows = sheets_get(AGG_SHEET_ID, f"'{SUPPLIER_ORDERS_LOG_TAB}'!A2:F10000")
    locked = {}  # sku -> {qty, title, order_names}
    for row in rows:
        if not row or len(row) < 6 or row[0] != supplier_order_id:
            continue
        sku, qty, order_names = row[3], row[4], row[5]
        locked[sku] = {'qty': int(qty) if str(qty).isdigit() else 0, 'order_names': order_names}
    return locked

def revert_missing_from_quote(supplier_order_id, missing_skus):
    """Reverts every Order Needs row that's Locked under supplier_order_id
    for one of the given missing_skus back to its pre-lock state: Supplier
    Order ID cleared, Stage set to 'NotOrdered'. This is what lets
    lock-supplier-order.py naturally pick these units back up on its next
    run for this supplier, instead of leaving them stranded at 'Ordered'
    for a unit the supplier never actually accepted.

    Returns a list of {'sku', 'orderName'} dicts describing what was
    reverted, for the caller to report back.
    """
    if not missing_skus:
        return []

    rows = sheets_get(AGG_SHEET_ID, ORDER_NEEDS_RANGE)
    today = time.strftime('%Y-%m-%d')
    updates = []
    reverted = []
    for idx, row in enumerate(rows):
        if not row or not row[0]:
            continue
        row = row + [''] * (8 - len(row))
        order_name, sku, title, supplier, unit, sup_id, stage, updated = row
        if supplier != SUPPLIER or sup_id != supplier_order_id or sku not in missing_skus:
            continue
        sheet_row = idx + 2  # +1 for header, +1 for 1-indexing
        updates.append((f"'{ORDER_NEEDS_TAB}'!F{sheet_row}:H{sheet_row}",
                         [['', 'NotOrdered', today]]))
        reverted.append({'sku': sku, 'orderName': order_name})

    sheets_batch_update(AGG_SHEET_ID, updates)
    return reverted

def compare_quote_to_locked(quote_items, locked_skus):
    # Sum, don't overwrite - same fix as reconcile.py / reconcile-arrived-
    # asmodee.py's run_comparison. A repeated SKU across multiple invoice/
    # quote line items was silently undercounted to just the last occurrence.
    quoted = {}
    for item in quote_items:
        sku = (item.get('sku') or '').strip()
        if not sku:
            continue
        qty = item['quantity'] if isinstance(item['quantity'], int) else 0
        if sku not in quoted:
            quoted[sku] = {'quantity': 0, 'description': item.get('description', '')}
        quoted[sku]['quantity'] += qty

    mismatches = []
    all_skus = set(locked_skus.keys()) | set(quoted.keys())
    for sku in sorted(all_skus):
        locked = locked_skus.get(sku)
        quo = quoted.get(sku)
        if locked and quo:
            if locked['qty'] != quo['quantity']:
                mismatches.append({
                    'sku': sku, 'issue': 'Quantity mismatch',
                    'lockedQty': locked['qty'], 'quotedQty': quo['quantity'],
                    'orderNames': locked['order_names'],
                })
        elif locked and not quo:
            mismatches.append({
                'sku': sku, 'issue': 'Locked but MISSING from quote - rep may have missed it',
                'lockedQty': locked['qty'], 'quotedQty': 0, 'orderNames': locked['order_names'],
            })
        elif quo and not locked:
            mismatches.append({
                'sku': sku, 'issue': 'On quote but was NOT locked - unexpected item, verify before approving',
                'lockedQty': 0, 'quotedQty': quo['quantity'], 'orderNames': '',
            })
    return mismatches

# ── HTTP handler ─────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_type = self.headers.get('Content-Type', '')
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)

            if 'multipart/form-data' not in content_type:
                self._send_json(400, {'error': 'Expected multipart/form-data with a PDF file'})
                return

            fs = cgi.FieldStorage(fp=io.BytesIO(body), headers=self.headers,
                environ={'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type})
            if 'file' not in fs:
                self._send_json(400, {'error': 'No file field found in upload'})
                return

            file_bytes = fs['file'].file.read()
            quote_items = parse_asmodee_quote(file_bytes)

            supplier_order_id = get_latest_supplier_order_id()
            if not supplier_order_id:
                self._send_json(200, {
                    'success': True,
                    'warning': 'No Supplier Order ID found in Supplier Orders Log for Asmodee - nothing to compare against. Did you run Lock & Order first?',
                    'itemsInQuote': len(quote_items),
                })
                return

            locked_skus = load_locked_skus(supplier_order_id)
            mismatches = compare_quote_to_locked(quote_items, locked_skus)

            # Auto-revert anything Locked but missing from the quote back to
            # NotOrdered, so it's picked back up by the next lock run instead
            # of sitting mislabeled 'Ordered' indefinitely. Quantity
            # mismatches and quote-but-not-locked extras are left for Iain
            # to judge - see module docstring.
            missing_skus = {m['sku'] for m in mismatches if m['issue'].startswith('Locked but MISSING')}
            reverted = revert_missing_from_quote(supplier_order_id, missing_skus)

            self._send_json(200, {
                'success': True,
                'comparedAgainstSupplierOrderId': supplier_order_id,
                'itemsInQuote': len(quote_items),
                'skusLocked': len(locked_skus),
                'mismatchCount': len(mismatches),
                'mismatches': mismatches,
                'clean': len(mismatches) == 0,
                'revertedToNotOrdered': reverted,
                'revertedCount': len(reverted),
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
