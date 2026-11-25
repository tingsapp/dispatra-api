"""Dispatra's fixed driver reason list, shared by stop reports and progress replies.

Each reason names who is responsible for time lost at the stop (Extra time skips CARRIER stops),
the Issue kind it files, and whether it holds Order completion until dispatch resolves it.
Companies cannot edit it: billing and completion rules depend on these values.
"""
from typing import Literal

Responsibility = Literal['SHIPPER', 'CARRIER', 'UNDECIDED']
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
# Stop reports filed before reasons existed carry only an Issue kind.
LEGACY_KIND = {'VEHICLE': 'VEHICLE_PROBLEM', 'FAILED_PICKUP': 'ITEMS_NOT_READY', 'FAILED_DELIVERY': 'RECIPIENT_UNAVAILABLE',
    'WRONG_ADDRESS': 'WRONG_ADDRESS', 'SHORT_LOAD': 'MISSING_ITEMS', 'UNSAFE': 'UNSAFE', 'OTHER': 'OTHER'}
KIND_PRIORITY = ['UNSAFE', 'VEHICLE', 'WRONG_ADDRESS', 'SHORT_LOAD', 'FAILED_PICKUP', 'FAILED_DELIVERY', 'OTHER']


def choices(report=True):
    return [{'value': value, 'label': label} for value, label, _, _, _, offered in REASONS if offered or not report]


def label(reason): return BY_VALUE[reason][1]
def responsibility(reason): return BY_VALUE[reason][2]
def holds_completion(reasons): return any(BY_VALUE[r][4] for r in reasons)


def issue_kind(reasons):
    """The most operationally serious Issue kind among the reasons."""
    kinds = {BY_VALUE[r][3] for r in reasons}
    return next(kind for kind in KIND_PRIORITY if kind in kinds)


def primary(reasons):
    """One reason for older single-reason views: the one behind the Issue kind, else the first."""
    kind = issue_kind(reasons)
    return next((r for r in reasons if BY_VALUE[r][3] == kind and kind != 'OTHER'), reasons[0])


def describe(reasons, notes):
    text = '; '.join(label(r) for r in reasons)
    return f'{text}\n\n{notes.strip()}' if notes.strip() else text


def stop_responsibility(reasons):
    """CARRIER when any reason is the carrier's, SHIPPER when every reason is the shipper's, else UNDECIDED."""
    sides = {responsibility(r) for r in reasons}
    return 'CARRIER' if 'CARRIER' in sides else 'SHIPPER' if sides == {'SHIPPER'} else 'UNDECIDED'
