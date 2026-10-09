"""Stable company-scoped public references; UUIDs remain relational keys."""
import secrets
import re
import unicodedata
from sqlalchemy import select
from app.models import Organization
from .common import company_lock


def company_initial(name, slug):
    for value in (name, slug):
        normalized = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().upper()
        for character in normalized:
            if 'A' <= character <= 'Z':
                return character
    return 'X'


def vehicle_number(unit_number, company_name, company_slug, previous=None):
    """Unit-based public reference; retain the issued company prefix on edits."""
    prefix = previous[:4] if previous and re.match(r'^D[A-Z]V-', previous) else f'D{company_initial(company_name, company_slug)}V-'
    return prefix + unit_number.strip().upper()


def unused_number(prefix, used):
    digits = 4
    while True:
        start, count = 10 ** (digits - 1), 9 * 10 ** (digits - 1)
        occupied = {int(value[len(prefix):]) for value in used if value.startswith(prefix) and value[len(prefix):].isdigit() and len(value[len(prefix):]) == digits}
        if len(occupied) >= count:
            digits += 1
            continue
        for _ in range(32):
            candidate = start + secrets.randbelow(count)
            if candidate not in occupied:
                return f'{prefix}{candidate}'
        return f'{prefix}{secrets.choice([value for value in range(start, start + count) if value not in occupied])}'


def issue_number(db, actor, model, entity_code):
    company_lock(db, actor)
    company = db.get(Organization, actor.organization_id)
    prefix = f'D{company_initial(company.name, company.slug)}{entity_code}-'
    for _ in range(32):
        candidate = f'{prefix}{1000 + secrets.randbelow(9000)}'
        exists = db.scalar(select(model.id).where(model.organization_id == actor.organization_id, model.number == candidate))
        if exists is None:
            return candidate
    used = set(db.scalars(select(model.number).where(model.organization_id == actor.organization_id, model.number.startswith(prefix))))
    return unused_number(prefix, used)
