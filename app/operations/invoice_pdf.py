"""Dependency-free A4 PDF rendering of an immutable Invoice snapshot (standard Helvetica, optional PNG/JPEG logo)."""
import base64
import struct
import zlib
from datetime import datetime
from decimal import Decimal

_W = dict(zip(range(32, 127), [278,278,355,556,556,889,667,191,333,333,389,584,278,333,278,278,*[556]*10,278,278,584,584,584,556,
    1015,667,667,722,722,667,611,778,722,278,500,667,556,833,722,778,667,778,722,667,611,722,667,944,667,667,611,278,278,278,469,556,
    333,556,556,500,556,556,278,556,556,222,222,500,222,833,556,556,556,556,333,500,278,556,500,722,500,500,500,334,260,334,584]))
PAGE_W, PAGE_H, MARGIN = 595.28, 841.89, 48
INK, BODY, MUTED = (0.1, 0.1, 0.1), (0.2, 0.2, 0.22), (0.45, 0.45, 0.48)
RULE, BAND, ACCENT, WHITE = (0.84, 0.84, 0.86), (0.07, 0.07, 0.07), (0.07, 0.07, 0.07), (1, 1, 1)


def _latin(value):
    text = str(value if value is not None else '').replace('—', '-').replace('–', '-').replace('’', "'").replace('•', '-')
    return text.encode('cp1252', 'replace').decode('cp1252')


def width(text, size, bold=False):
    return sum(_W.get(ord(c), 556) for c in _latin(text)) * size / 1000 * (1.06 if bold else 1)


def fit(text, size, limit, bold=False):
    text = _latin(text)
    if width(text, size, bold) <= limit: return text
    while text and width(text + '...', size, bold) > limit: text = text[:-1]
    return text + '...'


def amount(value):
    value = Decimal(str(value))
    return f'{"-" if value < 0 else ""}{abs(value):,.2f}'


def day(value):
    if not value: return ''
    try: return datetime.fromisoformat(str(value)).strftime('%d/%m/%Y')
    except ValueError: return str(value)


def address_lines(address):
    if not address: return []
    if isinstance(address, str): return [line.strip() for line in address.replace(', ', '\n', 1).split('\n') if line.strip()]
    text = address.get('text', '')
    street = text.split(',')[0].strip() if text else ''
    locality = ' '.join(filter(None, [address.get('city'), address.get('province'), address.get('postal_code')]))
    country = {'CA': 'Canada', 'US': 'United States'}.get(address.get('country'), address.get('country') or '')
    return [line for line in [street or text, locality, country] if line]


def _png(raw):
    """Decode an 8-bit PNG into (width, height, colour space, pixel bytes, alpha bytes|None) for PDF embedding."""
    pos, chunks, palette, header = 8, [], None, None
    while pos < len(raw):
        size, kind = struct.unpack('>I4s', raw[pos:pos + 8]); data = raw[pos + 8:pos + 8 + size]; pos += 12 + size
        if kind == b'IHDR': header = struct.unpack('>IIBBBBB', data)
        elif kind == b'PLTE': palette = data
        elif kind == b'IDAT': chunks.append(data)
        elif kind == b'IEND': break
    w, h, depth, colour, _, _, interlace = header
    if depth != 8 or interlace or colour not in (0, 2, 3, 4, 6): return None
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour]
    stride, data, rows, prev = w * channels, zlib.decompress(b''.join(chunks)), [], bytearray(w * channels)
    for y in range(h):
        kind, line = data[y * (stride + 1)], bytearray(data[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0; b = prev[i]; c = prev[i - channels] if i >= channels else 0
            if kind == 1: line[i] = (line[i] + a) & 255
            elif kind == 2: line[i] = (line[i] + b) & 255
            elif kind == 3: line[i] = (line[i] + (a + b) // 2) & 255
            elif kind == 4:
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line)); prev = line
    pixels = b''.join(rows)
    if colour == 3: return w, h, f'[/Indexed /DeviceRGB {len(palette) // 3 - 1} <{palette.hex()}>]', pixels, None
    if colour in (4, 6):
        n = channels - 1
        alpha = bytes(pixels[i + n] for i in range(0, len(pixels), channels))
        pixels = b''.join(pixels[i:i + n] for i in range(0, len(pixels), channels))
        return w, h, '/DeviceGray' if n == 1 else '/DeviceRGB', pixels, alpha
    return w, h, '/DeviceGray' if colour == 0 else '/DeviceRGB', pixels, None


def _jpeg(raw):
    pos = 2
    while pos < len(raw):
        marker, size = raw[pos + 1], struct.unpack('>H', raw[pos + 2:pos + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w, components = struct.unpack('>HHB', raw[pos + 5:pos + 10])
            return w, h, {1: '/DeviceGray', 4: '/DeviceCMYK'}.get(components, '/DeviceRGB')
        pos += 2 + size
    return None


def logo(url):
    """Return PDF image objects for a data-URL logo; remote URLs are never fetched while rendering."""
    try:
        if url.startswith('data:image/jpeg;base64,'):
            raw = base64.b64decode(url.split(',', 1)[1]); w, h, space = _jpeg(raw)
            return w, h, [f'<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace {space} /BitsPerComponent 8 /Filter /DCTDecode /Length {len(raw)} >>'.encode(), raw], None
        if url.startswith('data:image/png;base64,'):
            w, h, space, pixels, alpha = _png(base64.b64decode(url.split(',', 1)[1]))
            image = zlib.compress(pixels)
            mask = zlib.compress(alpha) if alpha else None
            return w, h, [f'<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace {space} /BitsPerComponent 8 /Filter /FlateDecode /Length {len(image)}{{smask}} >>'.encode(), image], \
                [f'<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length {len(mask)} >>'.encode(), mask] if mask else None
    except Exception:
        return None
    return None


class Page:
    def __init__(self): self.ops = []

    def text(self, x, y, value, size=10, bold=False, color=BODY, align='left'):
        value = _latin(value)
        if align == 'right': x -= width(value, size, bold)
        escaped = value.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        self.ops.append(f'BT {color[0]} {color[1]} {color[2]} rg /{"F2" if bold else "F1"} {size} Tf {x:.2f} {y:.2f} Td ({escaped}) Tj ET')

    def rect(self, x, y, w, h, color): self.ops.append(f'{color[0]} {color[1]} {color[2]} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f')

    def line(self, x1, y, x2, color=RULE, weight=0.7): self.ops.append(f'{color[0]} {color[1]} {color[2]} RG {weight} w {x1:.2f} {y:.2f} m {x2:.2f} {y:.2f} l S')

    def image(self, x, y, w, h): self.ops.append(f'q {w:.2f} 0 0 {h:.2f} {x:.2f} {y:.2f} cm /Im1 Do Q')

    def stream(self): return '\n'.join(self.ops).encode('cp1252', 'replace')


def _stream_object(header, body): return header + b'\nstream\n' + body + b'\nendstream'


def _document(pages, image=None):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', None,
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>']
    xobject = ''
    if image:
        (_, _, (header, body), mask) = image
        if mask:
            objects.append(_stream_object(*mask)); header = header.replace(b'{smask}', f' /SMask {len(objects)} 0 R'.encode())
        objects.append(_stream_object(header.replace(b'{smask}', b''), body)); xobject = f' /XObject << /Im1 {len(objects)} 0 R >>'
    kids = []
    for page in pages:
        objects.append(_stream_object(b'<< /Length %d >>' % len(page.stream()), page.stream()))
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >>{xobject} >> /Contents {len(objects)} 0 R >>'.encode())
        kids.append(f'{len(objects)} 0 R')
    objects[1] = f'<< /Type /Pages /Kids [{" ".join(kids)}] /Count {len(kids)} >>'.encode()
    out, offsets = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n'), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out)); out += f'{number} 0 obj\n'.encode() + body + b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode() + b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets)
    out += f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return bytes(out)


def render(number, snapshot, subtotal, tax, total):
    issuer, payer, pricing = snapshot['issuer'], snapshot['booking']['payer'], snapshot['pricing']
    currency = pricing.get('currency', 'CAD')
    left, right = MARGIN, PAGE_W - MARGIN
    page = Page(); pages = [page]
    image = logo(issuer.get('logo_url') or '')
    # Header: logo (or company name) on the left; INVOICE and the issuer block on the right.
    top = PAGE_H - MARGIN
    page.text(right, top - 26, 'INVOICE', 26, color=INK, align='right')
    y = top - 52
    page.text(right, y, fit(issuer.get('company_name', ''), 11, 240, True), 11, True, MUTED, 'right'); y -= 14
    for item in address_lines(issuer.get('address')): page.text(right, y, fit(item, 10, 240), 10, color=MUTED, align='right'); y -= 13
    y -= 8
    for item in filter(None, [issuer.get('email'), issuer.get('phone'),
                              f"GST/HST No. {issuer['tax_registration_number']}" if issuer.get('tax_registration_number') else None]):
        page.text(right, y, fit(item, 10, 240), 10, color=MUTED, align='right'); y -= 13
    if image:
        w, h = image[0], image[1]; scale = min(170 / w, 110 / h)
        page.image(left, top - 20 - h * scale, w * scale, h * scale); logo_bottom = top - 20 - h * scale
    else:
        page.text(left, top - 40, fit(issuer.get('company_name', ''), 20, 260, True), 20, True, INK); logo_bottom = top - 50
    # Client on the left, invoice facts on the right.
    y = min(y, logo_bottom) - 30
    client = [payer.get('company_name') or payer.get('name'), payer.get('name') if payer.get('company_name') else None,
              *address_lines(payer.get('warehouse') or payer.get('address')), payer.get('email'), payer.get('phone')]
    cy = y
    page.text(left, cy, 'BILL TO', 8, True, MUTED); cy -= 16
    for index, item in enumerate(filter(None, client)):
        page.text(left, cy, fit(item, 10, 250, index == 0), 10, index == 0, INK if index == 0 else BODY); cy -= 14
    fy = y - 16
    facts = [('Invoice No.:', number), ('Issue date:', day(snapshot.get('issued_at'))), ('Due date:', day(snapshot.get('due_date'))),
             ('Terms:', {'COD': 'Due on receipt'}.get(payer.get('terms'), (payer.get('terms') or '').replace('NET', 'Net '))),
             ('Order No.:', snapshot.get('order_number', ''))]
    for label, value in facts:
        page.text(right - 190, fy, label, 10); page.text(right - 6, fy, fit(value, 10, 110, True), 10, True, INK, 'right'); fy -= 15
    y = min(cy, fy) - 26
    cols = [right - 260, right - 150, right - 12]  # quantity (right edge), unit price, amount

    def header(page, y):
        page.rect(left, y - 10, right - left, 28, BAND)
        page.text(left + 12, y, 'DESCRIPTION', 9, True, WHITE)
        page.text(cols[0], y, 'QUANTITY', 9, True, WHITE, 'right')
        page.text(cols[1], y, f'UNIT PRICE ({currency})', 9, True, WHITE, 'right')
        page.text(cols[2], y, f'AMOUNT ({currency})', 9, True, WHITE, 'right')
        return y - 34

    y = header(page, y)
    for item in [l for l in pricing.get('lines', []) if l.get('group') != 'TAX']:
        if y < MARGIN + 140:
            page = Page(); pages.append(page); y = header(page, PAGE_H - MARGIN - 20)
        page.text(left + 12, y, fit(item.get('label', ''), 10, cols[0] - left - 90), 10)
        page.text(cols[0], y, '1', 10, align='right')
        page.text(cols[1], y, amount(item.get('amount', 0)), 10, align='right')
        page.text(cols[2], y, amount(item.get('amount', 0)), 10, align='right'); y -= 24
    page.line(left, y + 12, right); y -= 8
    # Totals block under the amount columns.
    tx = cols[0] - 70
    taxes = [l for l in pricing.get('lines', []) if l.get('group') == 'TAX']
    rows = [('SUBTOTAL', subtotal)] + ([(f"{l['label']} ({Decimal(str(l['rate_percent'])).normalize():f}%)" if l.get('rate_percent') else l['label'], l['amount']) for l in taxes] or [('TAX', tax)])
    for label, value in rows:
        page.text(tx + 12, y, fit(label.upper(), 9, 180, True), 9, True, BODY); page.text(cols[2], y, f'${amount(value)}', 10, align='right'); y -= 18
    page.line(tx, y + 8, right, ACCENT, 1); y -= 18
    page.text(tx + 12, y, f'TOTAL DUE ({currency})', 16, True, INK); page.text(cols[2], y, f'${amount(total)}', 16, color=INK, align='right')
    for page_number, item in enumerate(pages, 1):
        item.line(left, MARGIN + 18, right)
        item.text(left, MARGIN, fit(f'Thank you for your business. Please quote {number} with your payment.', 8, right - left - 80), 8, color=MUTED)
        item.text(right, MARGIN, f'Page {page_number} of {len(pages)}', 8, color=MUTED, align='right')
    return _document(pages, image)
