"""Driver reasons for stop reports and progress replies.

Dispatra's built-in reasons (REASONS) are the fixed base meanings: the Issue kind each files and whether it holds
Order completion until dispatch resolves it. Those never change per company. Each company's policy (company
settings `stop_reasons`) can rename, reorder and hide reasons, add custom reasons on top of a built-in base, and set
per reason who is responsible, how Extra time is charged and what evidence the driver must give.
"""
from dataclasses import dataclass, replace
from typing import Annotated, Literal
from pydantic import StringConstraints

Responsibility = Literal['SHIPPER', 'CARRIER', 'UNDECIDED']
# AUTO charges Extra time, REVIEW holds it for a dispatcher, NEVER does not charge it.
Charge = Literal['AUTO', 'REVIEW', 'NEVER']
Stops = Literal['BOTH', 'PICKUP', 'DROPOFF']
# value, label, responsibility, issue kind, holds completion, offered on stop reports
REASONS = [
    ('ITEMS_NOT_READY', 'Goods not ready', 'SHIPPER', 'OTHER', False, True),
    ('WAITING_FOR_STAFF', 'Waiting for staff', 'SHIPPER', 'OTHER', False, True),
    ('HANDLING_IN_PROGRESS', 'Loading or unloading', 'SHIPPER', 'OTHER', False, True),
    ('ACCESS_BLOCKED', 'Access or parking problem', 'SHIPPER', 'OTHER', False, True),
    ('RECIPIENT_UNAVAILABLE', 'Recipient unavailable', 'SHIPPER', 'FAILED_DELIVERY', True, True),
    ('WRONG_ADDRESS', 'Wrong address', 'SHIPPER', 'WRONG_ADDRESS', True, True),
    ('MISSING_ITEMS', 'Missing items', 'SHIPPER', 'SHORT_LOAD', True, True),
    ('DAMAGED_ITEM', 'Item damaged', 'UNDECIDED', 'OTHER', True, True),
    ('VEHICLE_PROBLEM', 'Vehicle problem', 'CARRIER', 'VEHICLE', True, True),
    ('WRONG_EQUIPMENT', 'Wrong vehicle or equipment', 'CARRIER', 'OTHER', True, True),
    ('NEED_HELPER', 'Need handling assistance', 'UNDECIDED', 'OTHER', False, True),
    ('TRAFFIC', 'Traffic delay', 'UNDECIDED', 'OTHER', False, True),
    ('UNSAFE', 'Unsafe situation', 'UNDECIDED', 'UNSAFE', True, True),
    ('OTHER', 'Another issue', 'UNDECIDED', 'OTHER', True, True),
    ('NO_PROBLEM', 'Progress is normal', 'UNDECIDED', 'OTHER', False, False),
]
Reason = Literal['ITEMS_NOT_READY', 'WAITING_FOR_STAFF', 'HANDLING_IN_PROGRESS', 'ACCESS_BLOCKED', 'RECIPIENT_UNAVAILABLE', 'WRONG_ADDRESS',
    'MISSING_ITEMS', 'DAMAGED_ITEM', 'VEHICLE_PROBLEM', 'WRONG_EQUIPMENT', 'NEED_HELPER', 'TRAFFIC', 'UNSAFE', 'OTHER', 'NO_PROBLEM']
BY_VALUE = {row[0]: row for row in REASONS}
# A reason code as stored on reports and replies: a built-in value or a company's CUSTOM_ code.
ReasonCode = Annotated[str, StringConstraints(pattern=r'^[A-Z][A-Z0-9_]{1,39}$')]
CUSTOM_PREFIX = 'CUSTOM_'
# Stop reports filed before reasons existed carry only an Issue kind.
LEGACY_KIND = {'VEHICLE': 'VEHICLE_PROBLEM', 'FAILED_PICKUP': 'ITEMS_NOT_READY', 'FAILED_DELIVERY': 'RECIPIENT_UNAVAILABLE',
    'WRONG_ADDRESS': 'WRONG_ADDRESS', 'SHORT_LOAD': 'MISSING_ITEMS', 'UNSAFE': 'UNSAFE', 'OTHER': 'OTHER'}
KIND_PRIORITY = ['UNSAFE', 'VEHICLE', 'WRONG_ADDRESS', 'SHORT_LOAD', 'FAILED_PICKUP', 'FAILED_DELIVERY', 'OTHER']


@dataclass(frozen=True)
class Row:
    code: str
    base: str
    label: str
    responsibility: str
    charge: str
    stops: str = 'BOTH'
    active: bool = True

    @property
    def notes_required(self): return self.base == 'OTHER'

    @property
    def photo_required(self): return False


ORDER = {value: index for index, (value, *_) in enumerate(REASONS)}


def default_row(value):
    _, label, side, _, _, _ = BY_VALUE[value]
    return Row(value, value, label, side, 'NEVER' if side == 'CARRIER' else 'AUTO')


def place(row):
    """Fixed display order: built-ins as listed, custom reasons by name before Another issue, which is last."""
    if row.code == 'OTHER': return (2, 0, '')
    return (1, 0, row.label.casefold()) if row.code not in BY_VALUE else (0, ORDER[row.code], '')


class Policy:
    """A company's reasons. A saved list is complete: a reason left out is deleted (old reports still read by its name).
    No saved list means Dispatra's built-ins with their defaults."""
    def __init__(self, entries=()):
        rows = {}
        for entry in entries or ():
            data = entry if isinstance(entry, dict) else entry.model_dump()
            code = data['code']
            if code == 'NO_PROBLEM' or data.get('active') is False: continue
            base = code if code in BY_VALUE else data.get('base') or 'OTHER'
            rows[code] = replace(default_row(code) if code in BY_VALUE else default_row(base), code=code, base=base,
                **{k: data[k] for k in ('label', 'responsibility', 'charge', 'stops') if data.get(k) is not None})
        if not rows: rows = {value: default_row(value) for value, *_ in REASONS if value != 'NO_PROBLEM'}
        self.rows = dict(sorted(rows.items(), key=lambda item: place(item[1])))
        self.progress_normal = default_row('NO_PROBLEM')

    def row(self, code):
        if code == 'NO_PROBLEM': return self.progress_normal
        if code in self.rows: return self.rows[code]
        if code in BY_VALUE: return replace(default_row(code), active=False)
        return replace(default_row('OTHER'), code=code, label=code.removeprefix(CUSTOM_PREFIX).replace('_', ' ').capitalize(), active=False)

    def choices(self, report=True, stop_kind=None):
        rows = [r for r in self.rows.values() if stop_kind is None or r.stops in ('BOTH', stop_kind)]
        if not report: rows.append(self.progress_normal)
        return [{'value': r.code, 'label': r.label, 'stops': r.stops, 'notes_required': r.notes_required, 'photo_required': r.photo_required} for r in rows]

    def label(self, code): return self.row(code).label
    def base(self, code): return self.row(code).base
    def responsibility(self, code): return self.row(code).responsibility
    def holds_completion(self, reasons): return any(BY_VALUE[self.base(r)][4] for r in reasons)

    def issue_kind(self, reasons):
        """The most operationally serious Issue kind among the reasons' bases."""
        kinds = {BY_VALUE[self.base(r)][3] for r in reasons}
        return next(kind for kind in KIND_PRIORITY if kind in kinds)

    def primary(self, reasons):
        """One reason for older single-reason views: the one behind the Issue kind, else the first."""
        kind = self.issue_kind(reasons)
        return next((r for r in reasons if BY_VALUE[self.base(r)][3] == kind and kind != 'OTHER'), reasons[0])

    def describe(self, reasons, notes):
        text = '; '.join(self.label(r) for r in reasons)
        return f'{text}\n\n{notes.strip()}' if notes.strip() else text

    def stop_responsibility(self, reasons):
        """CARRIER when any reason is the carrier's, SHIPPER when every reason is the shipper's, else UNDECIDED."""
        sides = {self.responsibility(r) for r in reasons}
        return 'CARRIER' if 'CARRIER' in sides else 'SHIPPER' if sides == {'SHIPPER'} else 'UNDECIDED'

    def charge(self, reasons):
        """NEVER when any reason is never charged, REVIEW when any needs review, else AUTO."""
        modes = {self.row(r).charge for r in reasons}
        return 'NEVER' if 'NEVER' in modes else 'REVIEW' if 'REVIEW' in modes else 'AUTO'

    def check(self, reasons, notes, photos, report=True):
        """The reasons a driver may send now, with notes where they are needed. Returns an error or None."""
        for code in reasons:
            row = self.row(code)
            if code == 'NO_PROBLEM' and report or code != 'NO_PROBLEM' and not row.active: return f'{row.label} is no longer offered. Refresh and choose again.'
            if row.notes_required and len(notes.strip()) < 3: return f'Describe the issue when choosing {row.label}.'
        return None


DEFAULT = Policy()


def company_policy(db, actor):
    return policy_for(db, actor.organization_id)


def policy_for(db, organization_id):
    """A company's policy from its saved settings; the defaults when it has none."""
    from sqlalchemy import select
    from .models import Settings
    data = db.scalar(select(Settings.data).where(Settings.organization_id == organization_id))
    return Policy((data or {}).get('stop_reasons') or ())


# The default policy for callers without a company (older imports and tests).
def choices(report=True): return [{'value': c['value'], 'label': c['label']} for c in DEFAULT.choices(report)]
def label(reason): return DEFAULT.label(reason)
def responsibility(reason): return DEFAULT.responsibility(reason)
def holds_completion(reasons): return DEFAULT.holds_completion(reasons)
def issue_kind(reasons): return DEFAULT.issue_kind(reasons)
def primary(reasons): return DEFAULT.primary(reasons)
def describe(reasons, notes): return DEFAULT.describe(reasons, notes)
def stop_responsibility(reasons): return DEFAULT.stop_responsibility(reasons)
