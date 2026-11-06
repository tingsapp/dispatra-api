"""Shared, dependency-free transactional email presentation.

Use inline styles and presentation tables for email clients; keep all dynamic
text escaped. The embedded PNG is the same wordmark used by the web application.
"""
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

LOGO_CID = 'dispatra-logo@dispatra.com'
LOGO_URL = f'cid:{LOGO_CID}'


def add_html(message, html):
    """Attach the brand asset only to messages rendered with the shared header."""
    logo = (Path(__file__).parent / 'assets/dispatra-logo.png').read_bytes() if LOGO_URL in html else None
    message.add_alternative(html, subtype='html')
    if logo is not None:
        message.get_payload()[-1].add_related(logo, maintype='image', subtype='png',
            cid=f'<{LOGO_CID}>', disposition='inline')


def paragraph(text):
    return f'<p style="margin:0 0 20px;color:#475569;font-size:16px;line-height:26px">{escape(str(text))}</p>'


def button(label, url):
    parsed = urlsplit(url)
    if parsed.scheme not in {'https', 'http'} or not parsed.netloc:
        return ''
    return (f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:28px 0"><tr>'
            f'<td bgcolor="#182c34" style="border-radius:8px;text-align:center;mso-padding-alt:15px 24px">'
            f'<a href="{escape(url, quote=True)}" style="display:inline-block;padding:15px 24px;border:1px solid #182c34;'
            f'border-radius:8px;color:#ffffff;font-size:15px;font-weight:700;text-decoration:none">{escape(label)}</a></td></tr></table>')


def layout(*, category, title, preheader, content, footer, company=None):
    """Content must come from the escaped builders in this module."""
    sender = f'From {company}' if company else 'Dispatra'
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light"><title>{escape(title)}</title>
<style>@media only screen and (max-width:620px){{.email-outer{{padding:20px 12px!important}}.email-content{{padding:28px 24px!important}}.email-heading{{font-size:26px!important;line-height:34px!important}}}}</style></head>
<body style="margin:0;padding:0;background-color:#f3f5f6;color:#182c34;font-family:Arial,Helvetica,sans-serif;-webkit-text-size-adjust:100%">
<div style="display:none;font-size:1px;color:#f3f5f6;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all">{escape(preheader)}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#f3f5f6"><tr><td align="center" class="email-outer" style="padding:48px 20px">
<!--[if mso]><table role="presentation" width="600" align="center"><tr><td><![endif]-->
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;table-layout:fixed">
<tr><td style="padding:0 0 24px 4px"><img src="{LOGO_URL}" alt="Dispatra" width="150" height="40" style="display:block;width:150px;height:40px;border:0;color:#182c34;font-size:24px;font-weight:700"></td></tr>
<tr><td bgcolor="#ffffff" class="email-content" style="padding:40px;border:1px solid #e2e8ec;border-top:3px solid #182c34;border-radius:12px;overflow-wrap:anywhere;word-wrap:break-word">
<p style="margin:0 0 12px;color:#557078;font-size:11px;line-height:18px;font-weight:700;letter-spacing:1.8px;text-transform:uppercase">{escape(category)}</p>
<h1 class="email-heading" style="margin:0 0 24px;color:#182c34;font-size:30px;line-height:38px;letter-spacing:-0.6px;font-weight:700">{escape(title)}</h1>
{content}
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:32px"><tr><td style="padding-top:24px;border-top:1px solid #e8edef">
<p style="margin:0 0 6px;color:#182c34;font-size:13px;line-height:20px;font-weight:700">{escape(sender)}</p>
<p style="margin:0;color:#64748b;font-size:12px;line-height:20px">{escape(footer)}</p></td></tr></table>
</td></tr><tr><td align="center" style="padding:24px 16px 0">
<p style="margin:0 0 6px;color:#64748b;font-size:12px;line-height:20px">Dispatra &middot; Delivery management, simplified.</p>
<a href="https://dispatra.com" style="color:#64748b;font-size:12px;line-height:20px;text-decoration:underline">dispatra.com</a>
</td></tr></table><!--[if mso]></td></tr></table><![endif]-->
</td></tr></table></body></html>'''


def verification(code):
    text = f'Your Dispatra verification code is {code}. It expires in 10 minutes. If you did not create this account, ignore this email.'
    content = paragraph('Welcome to Dispatra. Enter the code below to verify your email address and finish setting up your account.')
    content += f'''<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin:28px 0"><tr><td align="center" bgcolor="#f3f7f7" style="padding:24px 12px;border:1px solid #dce7e7;border-radius:8px">
<p style="margin:0 0 10px;color:#557078;font-size:11px;line-height:18px;font-weight:700;letter-spacing:1.5px">YOUR VERIFICATION CODE</p>
<p style="margin:0;color:#182c34;font-family:Consolas,'Courier New',monospace;font-size:36px;line-height:46px;font-weight:700;letter-spacing:7px">{escape(str(code))}</p>
<p style="margin:10px 0 0;color:#64748b;font-size:12px;line-height:20px">Expires in 10 minutes</p></td></tr></table>'''
    content += paragraph('For your security, do not share this code with anyone.')
    return text, layout(category='Account verification', title='Verify your email address',
        preheader='One quick step to get started. Your verification code expires in 10 minutes.', content=content,
        footer='If you did not create a Dispatra account, you can safely ignore this email.')


def quote(company, title, recipient, pricing, details):
    currency = str(pricing.get('currency', 'CAD'))
    lines = [(str(line['label']), str(line['amount'])) for line in pricing.get('lines', [])]
    summary = [('Subtotal', str(pricing['subtotal'])), ('Tax', str(pricing['tax']))]
    total = f"{pricing['total']} {currency}"
    text = '\n'.join([company, title, '', f'To: {recipient}', *details, '',
        *(f'{label}: {amount}' for label, amount in lines + summary), f'Total: {total}']) + '\n'
    content = paragraph(f'Your delivery quote from {company} is ready. Review the details and pricing below.')
    content += paragraph(f'To: {recipient}')
    content += '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin:24px 0;background:#f6f8f9;border-radius:8px"><tr><td style="padding:20px">'
    for detail in details:
        label, separator, value = detail.partition(': ')
        content += f'<p style="margin:0 0 10px;color:#475569;font-size:14px;line-height:22px"><strong style="color:#182c34">{escape(label)}{":" if separator else ""}</strong>{"<br>" + escape(value) if separator else ""}</p>'
    content += '</td></tr></table>'
    content += '<h2 style="margin:28px 0 12px;font-size:16px;line-height:24px;color:#182c34">Price summary</h2>'
    content += f'<table aria-label="Quote price summary" width="100%" cellspacing="0" cellpadding="0" border="0" style="table-layout:fixed;font-size:14px;line-height:22px"><tr><th align="left" style="padding:10px 0;color:#64748b;font-weight:400">Description</th><th align="right" style="padding:10px 0;color:#64748b;font-weight:400">Amount ({escape(currency)})</th></tr>'
    for label, amount in lines + summary:
        content += f'<tr><td style="padding:12px 0;border-top:1px solid #e8edef;color:#475569">{escape(label)}</td><td align="right" style="padding:12px 0 12px;border-top:1px solid #e8edef;color:#182c34">{escape(amount)}</td></tr>'
    content += f'<tr><th align="left" style="padding:18px 12px;background:#f3f7f7;border-top:2px solid #dce7e7;font-size:16px">Total</th><td align="right" style="padding:18px 12px;background:#f3f7f7;border-top:2px solid #dce7e7;font-size:18px;font-weight:700">{escape(total)}</td></tr></table>'
    return text, layout(category='Delivery quote', title=title, preheader=f'Your delivery quote from {company}: {total}.',
        content=content, company=company, footer=f'This quote was prepared by {company}. Contact them with any questions about your delivery.')


def order_update(company, name, title, body, link=''):
    footer = f'You receive order updates from {company}. Ask them to turn these emails off.'
    text = '\n'.join([f'Hello {name},', '', body, *(['', f'View your orders: {link}'] if link else []), '', company, '', footer]) + '\n'
    content = paragraph(f'Hello {name},') + paragraph(body)
    if link:
        content += button('View your orders', link)
    return text, layout(category='Order update', title=title, preheader=body,
        content=content, company=company, footer=footer)


def service_alert(keys):
    content = paragraph('The live Technical watchdog could not restore normal operation within its recovery window.')
    content += '<h2 style="margin:24px 0 12px;font-size:16px;line-height:24px">Checks needing attention</h2>'
    content += ''.join(paragraph(key) for key in keys)
    content += button('Open Monitor', 'https://dispatra.com/admin/monitor')
    return layout(category='Service alert', title='Your application needs attention',
        preheader='An application check needs review after automatic recovery.', content=content,
        footer='This automated service alert contains no customer data or logs.')
