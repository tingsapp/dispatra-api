"""Read-only IMAP access to a company mailbox and parsing of received messages.

The folder is opened read-only so messages stay unread for people sharing the mailbox. The first
connection starts at the mailbox's current end: earlier mail is never imported. Error codes are
stable identifiers only; server responses, credentials and message content are never logged.
"""
import email
import email.policy
import email.utils
import hashlib
import imaplib
import ipaddress
import os
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

MAX_MESSAGE_BYTES = 5_000_000
MAX_BODY_CHARS = 20_000
BATCH = 25


class MailboxError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass
class Fetched:
    uid: int
    raw: bytes | None
    too_large: bool = False


@dataclass
class Parsed:
    message_id: str
    from_address: str
    from_name: str
    subject: str
    received_at: datetime
    body: str
    sender_verified: bool


def _public(host, port):
    """Mailbox hosts are entered by company users: refuse private, loopback and other internal addresses."""
    if os.environ.get('MAILBOX_ALLOW_PRIVATE_HOSTS', '').lower() == 'true': return
    try: addresses = {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except (OSError, UnicodeError): raise MailboxError('HOST_NOT_FOUND') from None
    if not addresses or any(not ipaddress.ip_address(a.split('%')[0]).is_global for a in addresses): raise MailboxError('HOST_NOT_ALLOWED')


def connect(host, port, username, password, timeout=20):
    _public(host, port)
    try:
        client = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)
    except (OSError, ssl.SSLError): raise MailboxError('CONNECTION_FAILED') from None
    try: client.login(username, password)
    except imaplib.IMAP4.error:
        _close(client)
        raise MailboxError('AUTHENTICATION_FAILED') from None
    return client


def _close(client):
    try: client.logout()
    except (OSError, imaplib.IMAP4.error): pass


def _quoted(folder):
    return '"' + folder.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _status(client, folder):
    typ, data = client.status(_quoted(folder), '(UIDNEXT UIDVALIDITY)')
    if typ != 'OK' or not data or not data[0]: raise MailboxError('FOLDER_NOT_FOUND')
    text = data[0].decode(errors='replace')
    values = {key: int(value) for key, value in re.findall(r'(UIDNEXT|UIDVALIDITY) (\d+)', text)}
    if 'UIDNEXT' not in values or 'UIDVALIDITY' not in values: raise MailboxError('FOLDER_STATUS_UNAVAILABLE')
    return values['UIDVALIDITY'], values['UIDNEXT']


def check(host, port, username, password, folder):
    client = connect(host, port, username, password)
    try: _status(client, folder)
    except (OSError, imaplib.IMAP4.error): raise MailboxError('CONNECTION_FAILED') from None
    finally: _close(client)


def fetch_new(host, port, username, password, folder, uid_validity, last_uid, limit=BATCH):
    """Return (uid_validity, last_uid, messages). A new or changed UIDVALIDITY restarts at the current end."""
    client = connect(host, port, username, password)
    try:
        validity, uid_next = _status(client, folder)
        if validity != uid_validity: return validity, uid_next - 1, []
        typ, _ = client.select(_quoted(folder), readonly=True)
        if typ != 'OK': raise MailboxError('FOLDER_NOT_FOUND')
        typ, data = client.uid('SEARCH', None, f'UID {last_uid + 1}:*')
        if typ != 'OK': raise MailboxError('SEARCH_FAILED')
        uids = sorted(int(uid) for uid in (data[0] or b'').split() if int(uid) > last_uid)[:limit]
        messages = []
        for uid in uids:
            typ, data = client.uid('FETCH', str(uid), '(RFC822.SIZE)')
            size = re.search(rb'RFC822\.SIZE (\d+)', data[0] if typ == 'OK' and data and data[0] else b'')
            if size is None: raise MailboxError('FETCH_FAILED')
            large = int(size.group(1)) > MAX_MESSAGE_BYTES
            typ, data = client.uid('FETCH', str(uid), '(BODY.PEEK[HEADER])' if large else '(BODY.PEEK[])')
            part = next((item for item in data or [] if isinstance(item, tuple)), None) if typ == 'OK' else None
            if part is None: raise MailboxError('FETCH_FAILED')
            messages.append(Fetched(uid, part[1], large))
        return validity, (uids[-1] if uids else last_uid), messages
    except (OSError, imaplib.IMAP4.error): raise MailboxError('CONNECTION_FAILED') from None
    finally: _close(client)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}: self.skip += 1
        if tag in {'br', 'p', 'div', 'tr', 'li'}: self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'} and self.skip: self.skip -= 1

    def handle_data(self, data):
        if not self.skip: self.parts.append(data)


def _html_text(html):
    parser = _Text()
    parser.feed(html)
    return re.sub(r'\n\s*\n+', '\n\n', ''.join(parser.parts))


def _body(message):
    part = message.get_body(preferencelist=('plain', 'html'))
    if part is None: return ''
    try: content = part.get_content()
    except (LookupError, ValueError): content = part.get_payload(decode=True).decode('utf-8', 'replace') if part.get_payload(decode=True) else ''
    if part.get_content_type() == 'text/html': content = _html_text(content)
    return content.strip()[:MAX_BODY_CHARS]


def _domain(value):
    return value.rsplit('@', 1)[-1].strip().lower().rstrip('.') if '@' in value else value.strip().lower().rstrip('.')


def _aligned(signer, sender):
    return bool(signer) and (sender == signer or sender.endswith('.' + signer))


def authenticated(results, from_address):
    """Trust only the receiving provider's topmost Authentication-Results header: DMARC pass for the From
    domain, or a DKIM signature that passed for the From domain. SPF alone checks the envelope, not From."""
    if not results or '@' not in from_address: return False
    header, sender = results.lower(), _domain(from_address)
    for clause in header.split(';'):
        clause = clause.strip()
        if re.match(r'dmarc=pass\b', clause):
            source = re.search(r'header\.from=([^\s;]+)', clause)
            if source is None or _domain(source.group(1)) == sender: return True
        if re.match(r'dkim=pass\b', clause):
            signer = re.search(r'header\.(?:d|i)=([^\s;]+)', clause)
            if signer and _aligned(_domain(signer.group(1)), sender): return True
    return False


def parse(raw: bytes) -> Parsed:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    def header(name):
        try: return str(message[name] or '').strip()
        except (ValueError, TypeError, IndexError): return ''
    name, address = email.utils.parseaddr(header('From'))
    message_id = header('Message-ID')[:998] or '<sha256:' + hashlib.sha256(raw).hexdigest() + '>'
    try: received = email.utils.parsedate_to_datetime(header('Date'))
    except (TypeError, ValueError, IndexError): received = None
    if received is None or received.tzinfo is None: received = datetime.now(timezone.utc)
    try: results = message.get_all('Authentication-Results') or []
    except (ValueError, TypeError): results = []
    try: body = _body(message)
    except (KeyError, LookupError, ValueError, AttributeError): body = ''
    address = address.strip().lower()[:254]
    return Parsed(message_id, address, name.strip()[:160], header('Subject')[:500], received, body,
        authenticated(str(results[0]) if results else '', address))
