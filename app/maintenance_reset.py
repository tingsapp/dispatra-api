"""Private, explicit installation reset; never expose through HTTP or an agent tool.

The caller must stop writers and verify a private database backup first. Run with
migration credentials, an exact database/schema inventory, owner version and a
stable operation key. Existing business history is removed only for this reset.
"""
import re
from uuid import UUID
from sqlalchemy import insert, select, text
from . import bootstrap
from .models import User
from .services import audit, once

PRESERVED = frozenset({'alembic_version', 'subscription_plans', 'connector_providers'})


def tables(db):
    return tuple(db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")).scalars())


def quoted(names):
    if not all(re.fullmatch(r'[a-z_][a-z0-9_]*', name) for name in names):
        raise ValueError('Unexpected database identifier.')
    return ', '.join('public."' + name + '"' for name in names)


def counts(db, names):
    return {name: db.scalar(text('SELECT count(*) FROM ' + quoted([name]))) for name in names}


def reset_application_data(db, *, database, keep_owner, owner_version, expected_tables, operation_id):
    """Erase application history atomically; preserve owner/config and audit the reset."""
    key = str(UUID(str(operation_id)))
    if db.scalar(text('SELECT current_database()')) != database:
        raise ValueError('Database does not match the approved reset target.')
    privileged = db.scalar(text("SELECT rolsuper OR EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tableowner=current_user) FROM pg_roles WHERE rolname=current_user"))
    if not privileged:
        raise PermissionError('Reset requires private migration/owner credentials.')
    names = tables(db)
    if set(names) != set(expected_tables) or not PRESERVED.issubset(names) or 'users' not in names:
        raise ValueError('Database tables changed; review a fresh inventory before resetting.')
    db.execute(text('SELECT pg_advisory_xact_lock(:lock)'), {'lock': bootstrap.LOCK})
    bootstrap.context(db, platform=True)
    actor = bootstrap.owner(db)
    if actor is None or actor.login_id != keep_owner or actor.version != owner_version:
        raise ValueError('Preserved admin identity/version does not match the approved reset.')
    payload = dict(database=database, keep_owner=keep_owner, owner_version=owner_version, tables=sorted(names))

    def run():
        db.execute(text('LOCK TABLE ' + quoted(names) + ' IN ACCESS EXCLUSIVE MODE'))
        if bootstrap.migration_problem(db):
            raise ValueError('Database migration is not current.')
        # Never orphan external recurring charges by erasing their local identity.
        external = db.scalar(text('SELECT count(*) FROM company_billing WHERE stripe_subscription IS NOT NULL'))
        if external:
            raise ValueError('External subscriptions must be resolved before resetting this installation.')
        owner_row = dict(db.execute(select(User.__table__).where(User.id == actor.id)).mappings().one())
        if owner_row['role'] != 'ADMIN' or owner_row['scope'] != 'platform' or owner_row['organization_id'] is not None:
            raise ValueError('Invalid preserved admin scope.')
        before = counts(db, names)
        if db.scalar(text("SELECT count(*) FROM users WHERE role='ADMIN'")) != 1:
            raise ValueError('Expected exactly one platform owner.')
        configuration = {name: db.execute(text('SELECT row_to_json(t)::text FROM ' + quoted([name]) + ' t ORDER BY 1')).scalars().all() for name in PRESERVED}
        cleared = tuple(name for name in names if name not in PRESERVED)
        db.execute(text('TRUNCATE TABLE ' + quoted(cleared) + ' RESTART IDENTITY'))
        db.execute(insert(User.__table__).values(**owner_row))
        db.expire_all()
        restored = dict(db.execute(select(User.__table__).where(User.id == actor.id)).mappings().one())
        if restored != owner_row:
            raise RuntimeError('Admin preservation failed; transaction must roll back.')
        after = counts(db, names)
        if after['users'] != 1 or any(after[name] for name in cleared if name != 'users'):
            raise RuntimeError('Reset verification failed; transaction must roll back.')
        for name in PRESERVED:
            current = db.execute(text('SELECT row_to_json(t)::text FROM ' + quoted([name]) + ' t ORDER BY 1')).scalars().all()
            if current != configuration[name]:
                raise RuntimeError('Platform configuration changed; transaction must roll back.')
        audit(db, actor, 'platform_owner.data_reset', UUID(key), None)
        db.flush()
        return {'operation_id': key, 'database': database, 'owner_login': keep_owner,
                'owner_identity_and_password_preserved': True, 'old_sessions_revoked': True,
                'removed_counts': {name: before[name] - (1 if name == 'users' else 0) for name in cleared},
                'preserved_configuration': sorted(PRESERVED)}

    return once(db, actor, key, 'platform-data-reset', payload, run)
