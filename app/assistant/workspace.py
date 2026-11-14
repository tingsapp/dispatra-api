"""Chat cards for the rest of the dispatcher workspace: incoming requests, quotes, sign-in passwords and integrations.

Each card's body is the workspace command's own request, and confirmation runs that command as the dispatcher.
Secrets never pass through chat: new passwords are generated on confirmation and shown once on the card, and TMS
credentials, webhook secrets and mailbox passwords stay out of drafts (the saved ones are kept)."""
import re
import secrets
from sqlalchemy import select
from app.connectors import service as connectors
from app.connectors.catalog import BY_ID
from app.connectors.schemas import ConnectorAction
from app.integrations import routes as tms
from app.intake import routes as intake
from app.intake.schemas import IntakeVersion
from app.operations import directory
from app.operations.models import Quote, Shipper
from app.operations.schemas import DirectoryPasswordChange
from . import drafting, records
from .cards import reply, proposal, reference

STATUS = {'NEEDS_REVIEW': 'Needs review', 'UNKNOWN_SENDER': 'Unknown sender', 'FAILED': 'Failed', 'NOT_AN_ORDER': 'Not an order'}
EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
SECRETS = {'credential', 'webhook_secret', 'password'}


def sender(row):
    return row.from_name or row.from_address


def requests(db, actor, plan, operation):
    """Discard, retry, link or turn into an Order one incoming request; DISCARD_ALL discards every waiting one."""
    if plan.operation == 'DISCARD_ALL': return discard_all(db, actor, operation('REQUEST', 'DISCARD_ALL'))
    term = plan.target.strip()
    if not term: return reply('Which request? Tell me the sender, subject or TMS order number.')
    found = records.find_requests(db, actor.organization_id, term)
    if not found: return reply(f'I couldn’t find an incoming request matching “{term}”.')
    if len(found) > 1:
        names = ', '.join(f'TMS order {row.external_id}' if kind == 'TMS' else f'“{row.subject or sender(row)}”' for kind, row in found)
        return reply(f'More than one request matches “{term}”: {names}. Which one?')
    kind, row = found[0]
    if kind == 'TMS':
        if plan.operation not in {'DISCARD', 'RETRY'}:
            return reply('A TMS order can only be read again or discarded here; fix it in your TMS or the field map first.')
        name, op = operation('TMS_REQUEST', plan.operation)
        after = 'Discarded' if plan.operation == 'DISCARD' else 'Read again'
        return proposal(name, op, f'TMS order {row.external_id}', {}, [{'field': f'TMS order {row.external_id}',
            'before': STATUS.get(row.status, row.status.title()), 'after': after}], row.id)
    found = operation('EMAIL_REQUEST', plan.operation)
    if not found: return reply('I can discard, read again, link a shipper to, or create the order from an email request.')
    name, op = found
    subject = f'the email from {sender(row)}'
    lines = [{'field': 'Email', 'after': (row.subject or '(no subject)')[:120]}]
    if plan.operation == 'LINK_SHIPPER':
        shippers = records.find(db, actor.organization_id, 'SHIPPER', plan.request.strip()) if plan.request.strip() else []
        if len(shippers) != 1: return reply('Which shipper sent it? Tell me the shipper’s name.' if not shippers else
            f'More than one shipper matches: {", ".join(records.label("SHIPPER", s) for s in shippers)}. Which one?')
        shipper = shippers[0]
        lines.append({'field': 'Shipper', 'before': None, 'after': records.label('SHIPPER', shipper)})
        lines.append({'field': 'Then', 'after': 'The Order agent reads it again for this shipper'})
        return proposal(name, op, subject, {'version': row.version, 'shipper_id': str(shipper.id)}, lines, row.id, reference('SHIPPER', shipper))
    if plan.operation == 'CREATE_ORDER':
        if not row.draft: return reply(f'The email from {sender(row)} has no order draft yet. Ask me to read it again first.')
        if row.missing: return reply(f'The draft from {sender(row)} still needs: {"; ".join(row.missing)}. Complete it in the review queue.')
        body = drafting.validate(op.body, {'version': row.version, 'booking': row.draft})
        lines += drafting.lines(None, drafting.humanize(db, actor.organization_id, {'booking': body['booking']}))
        return proposal(name, op, subject, body, lines, row.id)
    lines.append({'field': 'Status', 'before': STATUS.get(row.status, row.status.title()), 'after': 'Discarded' if plan.operation == 'DISCARD' else 'Read again'})
    return proposal(name, op, subject, {'version': row.version}, lines, row.id)


def discard_all(db, actor, found):
    name, op = found
    emails, orders = records.open_requests(db, actor.organization_id)
    if not emails and not orders: return reply('No incoming requests are waiting for review.')
    lines = [{'field': f'Email from {sender(row)}', 'before': STATUS.get(row.status, row.status.title()), 'after': 'Discarded'} for row in emails]
    lines += [{'field': f'TMS order {row.external_id}', 'before': STATUS.get(row.status, row.status.title()), 'after': 'Discarded'} for row in orders]
    count = len(lines)
    if count > 10: lines = lines[:10] + [{'field': 'More', 'after': f'{count - 10} more requests'}]
    body = {'emails': [{'id': str(row.id), 'version': row.version} for row in emails], 'tms': [str(row.id) for row in orders]}
    return proposal(name, op, f'{count} incoming request{"" if count == 1 else "s"}', body, lines)


def run_discard_all(db, actor, body, key):
    """Each request goes through its own workspace command; e-mail commands get a per-request key for safe retries."""
    for item in body.emails: intake.discard(None, item.id, IntakeVersion(version=item.version), db, f'{key}:{item.id}', actor)
    for identity in body.tms: tms.dismiss_inbox(None, identity, db, key, actor)
    return {'updated': len(body.emails) + len(body.tms)}


def send_quote(db, actor, name, op, plan):
    """Send the newest quote, or one asked about earlier in the chat, to the e-mail address the dispatcher gave."""
    recipient = EMAIL.search(plan.request) or EMAIL.search(plan.target)
    if not recipient: return reply('Which e-mail address should I send the quote to?')
    quote = db.scalar(select(Quote).where(Quote.organization_id == actor.organization_id).order_by(Quote.created_at.desc()).limit(1))
    if quote is None: return reply('There is no quote to send yet. Ask me to make one first.')
    total = (quote.pricing or {}).get('total')
    lines = [{'field': 'Quote', 'after': f'{(quote.pricing or {}).get("currency", "")} {total}'.strip() if total is not None else 'Needs price review'},
        {'field': 'Created', 'after': quote.created_at.strftime('%b %d, %H:%M UTC')}, {'field': 'Send to', 'after': recipient.group()}]
    return proposal(name, op, 'the latest quote', {'version': quote.version, 'recipient': recipient.group()}, lines, quote.id)


def reset_password(db, actor, entity, name, op, row):
    if getattr(row, 'archived_at', None): return reply(f'{records.label(entity, row)} is archived, so their sign-in can’t be changed.')
    lines = [{'field': 'Password', 'before': 'Current', 'after': 'New temporary password, shown once after you confirm'},
        {'field': 'Signed out', 'after': 'Everywhere they are signed in now'}]
    return proposal(name, op, records.label(entity, row), {'version': row.version}, lines, row.id, reference(entity, row))


def reset_driver(db, actor, target, key):
    return directory.reset_driver_password(db, actor, target, key)


def reset_shipper(db, actor, target, body, key):
    """The new password is generated on confirmation, returned once and never kept in chat or idempotent results."""
    password = secrets.token_urlsafe(12)
    result = directory.change_directory_password(db, actor, target, DirectoryPasswordChange(version=body.version, new_password=password), key, 'shipper')
    return {**result, 'initial_password': password}


def connector(db, actor, name, op, plan):
    term = plan.target.strip().lower()
    matches = [info for info in BY_ID.values() if term and (term in info.name.lower() or term == info.id)]
    if len(matches) != 1: return reply('Which connector? For example Samsara or Rose Rocket.' if not matches else
        f'More than one connector matches: {", ".join(info.name for info in matches)}. Which one?')
    info = matches[0]
    row = connectors.get(db, actor, info.id)
    if row is None: return reply(f'{info.name} isn’t connected. Connect it in Integrations first.')
    after = 'Disconnected' if name.endswith('DISCONNECT') else 'Sync started'
    return proposal(name, op, info.name, {'provider': info.id, 'version': row.version}, [{'field': info.name, 'before': row.status.title(), 'after': after}])


def connector_sync(db, actor, body, key):
    return connectors.action(db, actor, body.provider, ConnectorAction(version=body.version), key)


def connector_disconnect(db, actor, body, key):
    return connectors.action(db, actor, body.provider, ConnectorAction(version=body.version), key, True)


def strip_secrets(body):
    """Drafts never carry credentials: an omitted secret keeps the saved one."""
    return {key: value for key, value in body.items() if key not in SECRETS}
