"""Gap-free event cursors for clients (`/sync`) and server consumers.

A position is `<tx>-<seq>`. Reads only return events whose writing transaction is older than every
transaction still in progress, ordered by (tx, seq), so a lower sequence that commits later can never
fall behind an advanced cursor. A long-running transaction delays delivery; it never loses events.
"""
import re
from sqlalchemy import text
from .models import EventSubscription

POSITION = re.compile(r'^(\d{1,20})-(\d{1,20})$')
COLUMNS = 'tx::text AS tx, seq, id, type, actor_type, entity, entity_id, entity_version, order_id, route_id, created_at'


def head(db):
    """Start position for a reader that only needs events committed from now on."""
    return db.scalar(text('SELECT pg_snapshot_xmin(pg_current_snapshot())::text')) + '-0'


def parse(db, position):
    """(tx, seq), or None when malformed or ahead of this database (restored or foreign cursor)."""
    match = POSITION.match(position or '')
    if not match: return None
    ahead = db.scalar(text('SELECT CAST(:tx AS xid8) > pg_snapshot_xmax(pg_current_snapshot())'), {'tx': match[1]})
    return None if ahead else (match[1], int(match[2]))


def audience(user):
    if user.role == 'SHIPPER': return 'shipper_id = :shipper AND (user_id IS NULL OR user_id = :user)'
    if user.role == 'DRIVER': return 'driver_id = :driver AND (user_id IS NULL OR user_id = :user)'
    if user.role == 'DISPATCHER': return '((dispatchers AND user_id IS NULL) OR user_id = :user)'
    return 'false'


def read(db, organization_id, position, limit, where='true', params=None):
    rows = db.execute(text(f"""SELECT {COLUMNS} FROM events
        WHERE organization_id = :org AND (tx, seq) > (CAST(:tx AS xid8), :seq)
            AND tx < pg_snapshot_xmin(pg_current_snapshot()) AND {where}
        ORDER BY tx, seq LIMIT :limit"""), {'org': organization_id, 'tx': position[0], 'seq': position[1],
            'limit': limit, **(params or {})}).mappings().all()
    return rows, (f'{rows[-1]["tx"]}-{rows[-1]["seq"]}' if rows else f'{position[0]}-{position[1]}')


def for_user(db, user, position, limit):
    """Changes visible to one account: (rows, next position, reset)."""
    if position is None: return [], head(db), False
    parsed = parse(db, position)
    if parsed is None: return [], head(db), True
    rows, after = read(db, user.organization_id, parsed, limit, audience(user),
        {'user': user.id, 'shipper': user.shipper_id, 'driver': user.driver_id})
    return rows, after, False


def consume(db, organization_id, consumer, limit=100):
    """Next events for a server consumer. Call `advance` with the returned position after handling them."""
    row = db.get(EventSubscription, (organization_id, consumer), with_for_update=True)
    if row is None:
        row = EventSubscription(organization_id=organization_id, consumer=consumer, position=head(db))
        db.add(row); db.flush()
        return [], row.position
    return read(db, organization_id, parse(db, row.position) or parse(db, head(db)), limit)


def advance(db, organization_id, consumer, position):
    row = db.get(EventSubscription, (organization_id, consumer), with_for_update=True)
    if row is None or parse(db, position) is None: raise ValueError('Unknown consumer or invalid position.')
    row.position = position
    db.flush()
