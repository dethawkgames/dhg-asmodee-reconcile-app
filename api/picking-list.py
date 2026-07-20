import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image, Flowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'detective-hawk-games.myshopify.com')
SHOPIFY_API_VERSION = '2025-01'
LOCAL_DELIVERY_TAG = os.environ.get('LOCAL_DELIVERY_TAG', 'local-delivery').lower()

# ── Shopify auth (same pattern as api/mark-stage.py) ────────────────────────

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


# ── Order list (unfulfilled/partial, excluding cancelled) ──────────────────

ORDER_LIST_QUERY = '''
query PickableOrders($cursor: String) {
  orders(first: 50, after: $cursor, query: "fulfillment_status:unfulfilled OR fulfillment_status:partial", sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        createdAt
        displayFulfillmentStatus
        cancelledAt
        tags
        customer { firstName lastName }
        shippingAddress { name city provinceCode }
      }
    }
  }
}
'''

def list_pickable_orders():
    results = []
    cursor = None
    has_next = True
    while has_next:
        data = shopify_graphql(ORDER_LIST_QUERY, {'cursor': cursor})
        for edge in data['orders']['edges']:
            node = edge['node']
            if node.get('cancelledAt'):
                continue
            tags = [t.lower() for t in (node.get('tags') or [])]
            ship = node.get('shippingAddress') or {}
            customer = node.get('customer') or {}
            name = ship.get('name') or ' '.join(filter(None, [customer.get('firstName'), customer.get('lastName')])) or 'No name on file'
            results.append({
                'id': node['id'],
                'orderNumber': node['name'],
                'createdAt': node['createdAt'],
                'fulfillmentStatus': node['displayFulfillmentStatus'],
                'customerName': name,
                'city': ship.get('city') or '',
                'provinceCode': ship.get('provinceCode') or '',
                'isLocalDelivery': LOCAL_DELIVERY_TAG in tags,
            })
        page_info = data['orders']['pageInfo']
        has_next = page_info['hasNextPage']
        cursor = page_info['endCursor']
    return results


# ── Order detail for PDF generation ─────────────────────────────────────────

ORDER_DETAIL_QUERY = '''
query OrderDetail($id: ID!) {
  order(id: $id) {
    id
    name
    createdAt
    tags
    customer { firstName lastName }
    shippingAddress { name address1 address2 city provinceCode zip country }
    lineItems(first: 250) {
      edges {
        node {
          title
          sku
          quantity
          currentQuantity
          image { url(transform: {maxWidth: 200, maxHeight: 200}) }
          variant {
            title
            image { url(transform: {maxWidth: 200, maxHeight: 200}) }
            product { featuredImage { url(transform: {maxWidth: 200, maxHeight: 200}) } }
          }
        }
      }
    }
  }
}
'''

def get_order_details(order_ids):
    orders = []
    for order_id in order_ids:
        data = shopify_graphql(ORDER_DETAIL_QUERY, {'id': order_id})
        node = data.get('order')
        if not node:
            continue
        tags = [t.lower() for t in (node.get('tags') or [])]
        ship = node.get('shippingAddress') or {}
        customer = node.get('customer') or {}
        name = ship.get('name') or ' '.join(filter(None, [customer.get('firstName'), customer.get('lastName')])) or 'No name on file'
        address_lines = [
            ship.get('address1'),
            ship.get('address2'),
            ', '.join(filter(None, [ship.get('city'), ship.get('provinceCode'), ship.get('zip')])),
            ship.get('country'),
        ]
        address = '\n'.join(filter(None, address_lines)) or 'No shipping address on file'

        line_items = []
        for edge in node['lineItems']['edges']:
            li = edge['node']
            qty = li.get('currentQuantity')
            if qty is None:
                qty = li.get('quantity', 0)
            if qty <= 0:
                continue  # fully refunded line item, excluded per DHG standing rule
            variant = li.get('variant') or {}
            product = variant.get('product') or {}
            image_url = (
                (li.get('image') or {}).get('url')
                or (variant.get('image') or {}).get('url')
                or (product.get('featuredImage') or {}).get('url')
            )
            variant_title = variant.get('title')
            line_items.append({
                'title': li['title'],
                'sku': li.get('sku') or '',
                'quantity': qty,
                'variantTitle': variant_title if variant_title and variant_title != 'Default Title' else '',
                'imageUrl': image_url,
            })

        orders.append({
            'orderNumber': node['name'],
            'createdAt': node['createdAt'],
            'isLocalDelivery': LOCAL_DELIVERY_TAG in tags,
            'shippingName': name,
            'shippingAddress': address,
            'lineItems': line_items,
        })
    return orders


# ── PDF generation (reportlab) ──────────────────────────────────────────────

PLUM_DARK = colors.HexColor('#38112F')
CORAL = colors.HexColor('#EF3F45')
TEAL_DEEP = colors.HexColor('#059B9C')
CREAM = colors.HexColor('#E2D4C3')
INK = colors.HexColor('#2A1622')
INK_SOFT = colors.HexColor('#6b5a63')

_image_cache = {}

def fetch_image_reader(url):
    if not url:
        return None
    if url in _image_cache:
        return _image_cache[url]
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
        reader = ImageReader(io.BytesIO(data))
        _image_cache[url] = reader
        return reader
    except Exception:
        return None


def format_date(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return dt.strftime('%b %-d, %Y')
    except Exception:
        return iso_str


def build_order_flowables(order, styles):
    flow = []

    header_data = [[
        Paragraph(f"Order {order['orderNumber']}", styles['OrderNum']),
        Paragraph(
            'LOCAL DELIVERY' if order['isLocalDelivery'] else 'SHIPPING',
            styles['BadgeLocal'] if order['isLocalDelivery'] else styles['BadgeShip'],
        ),
    ]]
    header_table = Table(header_data, colWidths=[4.5 * inch, 2.3 * inch])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('LINEBELOW', (0, 0), (-1, 0), 2, PLUM_DARK),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    flow.append(header_table)
    flow.append(Paragraph(f"Ordered {format_date(order['createdAt'])}", styles['DateLine']))
    flow.append(Spacer(1, 12))

    flow.append(Paragraph('SHIP TO', styles['ShipToLabel']))
    flow.append(Paragraph(order['shippingName'], styles['ShipToName']))
    for line in order['shippingAddress'].split('\n'):
        flow.append(Paragraph(line, styles['ShipToAddr']))
    flow.append(Spacer(1, 16))

    rows = [['', 'Item', 'Qty']]
    row_styles = []
    for i, li in enumerate(order['lineItems'], start=1):
        reader = fetch_image_reader(li['imageUrl'])
        img_cell = Image(reader, width=0.55 * inch, height=0.55 * inch) if reader else ''
        title_bits = f"<b>{li['title']}</b>"
        if li['variantTitle']:
            title_bits += f"<br/><font size=8 color='#6b5a63'>{li['variantTitle']}</font>"
        if li['sku']:
            title_bits += f"<br/><font size=8 color='#6b5a63'>SKU: {li['sku']}</font>"
        rows.append([img_cell, Paragraph(title_bits, styles['ItemTitle']), Paragraph(str(li['quantity']), styles['QtyBox'])])

    items_table = Table(rows, colWidths=[0.75 * inch, 5.05 * inch, 1.0 * inch], repeatRows=1)
    table_style = [
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (2, 0), (2, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('TEXTCOLOR', (0, 0), (-1, 0), INK_SOFT),
        ('LINEBELOW', (0, 0), (-1, 0), 1, colors.HexColor('#cccccc')),
        ('LINEBELOW', (0, 1), (-1, -1), 0.5, colors.HexColor('#e2e2e2')),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]
    items_table.setStyle(TableStyle(table_style))
    flow.append(items_table)

    return flow


def generate_pdf(orders):
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle('OrderNum', fontSize=20, fontName='Helvetica-Bold', textColor=INK, alignment=TA_LEFT))
    styles.add(ParagraphStyle('DateLine', fontSize=10, textColor=INK_SOFT))
    styles.add(ParagraphStyle('BadgeLocal', fontSize=10, fontName='Helvetica-Bold', textColor=colors.HexColor('#1e6b34'), backColor=colors.HexColor('#dff5e1'), borderPadding=5))
    styles.add(ParagraphStyle('BadgeShip', fontSize=10, fontName='Helvetica-Bold', textColor=colors.HexColor('#1c4587'), backColor=colors.HexColor('#e7eefc'), borderPadding=5))
    styles.add(ParagraphStyle('ShipToLabel', fontSize=8, fontName='Helvetica-Bold', textColor=INK_SOFT))
    styles.add(ParagraphStyle('ShipToName', fontSize=13, fontName='Helvetica-Bold', textColor=INK))
    styles.add(ParagraphStyle('ShipToAddr', fontSize=10, textColor=INK))
    styles.add(ParagraphStyle('ItemTitle', fontSize=10.5, textColor=INK, leading=13))
    styles.add(ParagraphStyle('QtyBox', fontSize=14, fontName='Helvetica-Bold', textColor=INK, alignment=1))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, topMargin=0.5 * inch, bottomMargin=0.5 * inch, leftMargin=0.5 * inch, rightMargin=0.5 * inch)

    story = []
    for i, order in enumerate(orders):
        story.extend(build_order_flowables(order, styles))
        if i < len(orders) - 1:
            story.append(PageBreak())

    doc.build(story)
    return buf.getvalue()


# ── HTTP handler ─────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            orders = list_pickable_orders()
            self._send_json(200, {'success': True, 'orders': orders})
        except Exception as e:
            import traceback
            self._send_json(500, {'error': str(e), 'trace': traceback.format_exc()})

    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_length)) if content_length else {}
            order_ids = body.get('orderIds') or []
            if not order_ids:
                self._send_json(400, {'error': 'orderIds must be a non-empty array'})
                return

            orders = get_order_details(order_ids)
            pdf_bytes = generate_pdf(orders)

            self.send_response(200)
            self._cors_headers()
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Disposition', 'attachment; filename="picking-lists.pdf"')
            self.end_headers()
            self.wfile.write(pdf_bytes)
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
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _send_json(self, status, data):
        self.send_response(status)
        self._cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
