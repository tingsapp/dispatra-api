from decimal import Decimal
import pytest
from pydantic import ValidationError
from app.operations.pricing import extra_time_status
from app.operations.schemas import SettingsData
from app.operations.stop_reasons import DEFAULT, REASONS, Policy

CUSTOM = {'code': 'CUSTOM_DOCK_BUSY', 'base': 'WAITING_FOR_STAFF', 'label': 'Dock busy', 'stops': 'PICKUP', 'responsibility': 'SHIPPER', 'charge': 'AUTO'}


def test_defaults_keep_the_built_in_meanings():
    assert [c['value'] for c in DEFAULT.choices()] == [r[0] for r in REASONS if r[0] != 'NO_PROBLEM']
    assert DEFAULT.charge(['VEHICLE_PROBLEM']) == 'NEVER' and DEFAULT.charge(['TRAFFIC']) == 'AUTO'
    assert next(c for c in DEFAULT.choices() if c['value'] == 'OTHER')['notes_required'] is True
    assert DEFAULT.choices(report=False)[-1]['value'] == 'NO_PROBLEM'


def test_a_company_renames_orders_hides_and_adds_reasons_on_a_built_in_base():
    policy = Policy([{'code': 'TRAFFIC', 'label': 'Road congestion', 'responsibility': 'UNDECIDED', 'charge': 'REVIEW'}, CUSTOM,
        {'code': 'WRONG_ADDRESS', 'label': 'Wrong address', 'responsibility': 'SHIPPER', 'charge': 'AUTO', 'active': False}])
    codes = [c['value'] for c in policy.choices()]
    assert codes[:2] == ['TRAFFIC', 'CUSTOM_DOCK_BUSY'] and 'WRONG_ADDRESS' not in codes and 'ITEMS_NOT_READY' in codes
    assert policy.label('TRAFFIC') == 'Road congestion' and policy.label('CUSTOM_DOCK_BUSY') == 'Dock busy'
    # A custom reason files and holds like its base; completion and Issue kind never change per company.
    assert policy.issue_kind(['CUSTOM_DOCK_BUSY']) == 'OTHER' and policy.holds_completion(['CUSTOM_DOCK_BUSY']) is False
    assert policy.holds_completion(['UNSAFE']) is True
    assert 'CUSTOM_DOCK_BUSY' not in [c['value'] for c in policy.choices(stop_kind='DROPOFF')]
    # A removed custom reason on an old report still reads by its code.
    assert policy.label('CUSTOM_OLD_GATE') == 'Old gate'
    assert policy.check(['WRONG_ADDRESS'], '', []) == 'Wrong address is no longer offered. Refresh and choose again.'


def test_notes_and_photo_rules_are_the_companys():
    policy = Policy([{'code': 'DAMAGED_ITEM', 'label': 'Item damaged', 'responsibility': 'UNDECIDED', 'charge': 'REVIEW', 'photo_required': True},
        {'code': 'OTHER', 'label': 'Another issue', 'responsibility': 'UNDECIDED', 'charge': 'AUTO', 'notes_required': False}])
    assert policy.check(['DAMAGED_ITEM'], 'Box crushed', []) == 'Add a photo when choosing Item damaged.'
    assert policy.check(['DAMAGED_ITEM'], '', ['photo']) is None
    assert policy.check(['OTHER'], '', []) is None
    assert policy.check(['NO_PROBLEM'], '', []) is not None and policy.check(['NO_PROBLEM'], '', [], report=False) is None


def test_extra_time_follows_responsibility_charge_rule_and_review_amount():
    policy = Policy([{'code': 'TRAFFIC', 'label': 'Traffic delay', 'responsibility': 'UNDECIDED', 'charge': 'REVIEW'},
        {'code': 'NEED_HELPER', 'label': 'Need handling assistance', 'responsibility': 'UNDECIDED', 'charge': 'NEVER'}, CUSTOM])
    assert extra_time_status(policy, ['CUSTOM_DOCK_BUSY'], Decimal('50'), None) == ('SHIPPER', 'CHARGED')
    assert extra_time_status(policy, ['VEHICLE_PROBLEM', 'ITEMS_NOT_READY'], Decimal('50'), None) == ('CARRIER', 'EXCLUDED')
    assert extra_time_status(policy, ['NEED_HELPER'], Decimal('50'), None) == ('UNDECIDED', 'EXCLUDED')
    assert extra_time_status(policy, ['TRAFFIC', 'ITEMS_NOT_READY'], Decimal('50'), None) == ('UNDECIDED', 'REVIEW')
    assert extra_time_status(policy, [], Decimal('150'), Decimal('100')) == ('UNDECIDED', 'REVIEW')
    assert extra_time_status(policy, [], Decimal('100'), Decimal('100')) == ('UNDECIDED', 'CHARGED')


def test_settings_reject_unclear_reason_lists():
    ok = {'responsibility': 'SHIPPER', 'charge': 'AUTO'}
    for bad in [[{'code': 'DOCK', 'label': 'Dock', **ok}], [{'code': 'CUSTOM_A', 'label': 'Ab', **ok}], [{'code': 'NO_PROBLEM', 'label': 'Fine', **ok}],
            [CUSTOM, {**CUSTOM, 'label': 'Other name'}], [CUSTOM, {**CUSTOM, 'code': 'CUSTOM_B'}], [{**CUSTOM, 'active': False}]]:
        with pytest.raises(ValidationError): SettingsData(stop_reasons=bad)
    assert SettingsData(stop_reasons=[{'code': 'TRAFFIC', 'base': 'OTHER', 'label': 'Traffic', **ok}]).stop_reasons[0].base is None
