import pytest
from pydantic import ValidationError
from app.operations.pricing import extra_time_status
from app.operations.schemas import SettingsData
from app.operations.stop_reasons import DEFAULT, REASONS, Policy

CUSTOM = {'code': 'CUSTOM_DOCK_BUSY', 'base': 'WAITING_FOR_STAFF', 'label': 'Dock busy', 'stops': 'PICKUP', 'responsibility': 'SHIPPER', 'charge': 'AUTO'}
row = lambda code, label, side='UNDECIDED', charge='AUTO', **extra: {'code': code, 'label': label, 'responsibility': side, 'charge': charge, **extra}


def test_defaults_keep_the_built_in_meanings():
    assert [c['value'] for c in DEFAULT.choices()] == [r[0] for r in REASONS if r[0] != 'NO_PROBLEM']
    assert DEFAULT.charge(['VEHICLE_PROBLEM']) == 'NEVER' and DEFAULT.charge(['TRAFFIC']) == 'AUTO'
    assert next(c for c in DEFAULT.choices() if c['value'] == 'OTHER')['notes_required'] is True
    assert DEFAULT.choices(report=False)[-1]['value'] == 'NO_PROBLEM'


def test_a_saved_list_is_complete_renames_and_adds_custom_reasons_in_a_fixed_order():
    policy = Policy([row('OTHER', 'Another issue'), CUSTOM, row('TRAFFIC', 'Road congestion', charge='REVIEW'), row('ITEMS_NOT_READY', 'Goods not ready', 'SHIPPER')])
    # Built-ins in Dispatra's order, custom reasons by name, Another issue last; deleted reasons are gone.
    assert [c['value'] for c in policy.choices()] == ['ITEMS_NOT_READY', 'TRAFFIC', 'CUSTOM_DOCK_BUSY', 'OTHER']
    assert policy.label('TRAFFIC') == 'Road congestion' and policy.label('CUSTOM_DOCK_BUSY') == 'Dock busy'
    # A custom reason files and holds like its base; completion and Issue kind never change per company.
    assert policy.issue_kind(['CUSTOM_DOCK_BUSY']) == 'OTHER' and policy.holds_completion(['CUSTOM_DOCK_BUSY']) is False
    assert 'CUSTOM_DOCK_BUSY' not in [c['value'] for c in policy.choices(stop_kind='DROPOFF')]
    # Deleted and removed reasons on old reports still read by name but cannot be sent again.
    assert policy.label('VEHICLE_PROBLEM') == 'Vehicle problem' and policy.label('CUSTOM_OLD_GATE') == 'Old gate'
    assert policy.check(['VEHICLE_PROBLEM'], '', []) == 'Vehicle problem is no longer offered. Refresh and choose again.'
    assert policy.check(['OTHER'], '', []) == 'Describe the issue when choosing Another issue.'
    assert policy.check(['NO_PROBLEM'], '', []) is not None and policy.check(['NO_PROBLEM'], '', [], report=False) is None


def test_extra_time_follows_responsibility_and_the_reason_charge_rule():
    policy = Policy([row('TRAFFIC', 'Traffic delay', charge='REVIEW'), row('NEED_HELPER', 'Need handling assistance', charge='NEVER'),
        row('VEHICLE_PROBLEM', 'Vehicle problem', 'CARRIER', 'NEVER'), row('ITEMS_NOT_READY', 'Goods not ready', 'SHIPPER'), CUSTOM])
    assert extra_time_status(policy, ['CUSTOM_DOCK_BUSY']) == ('SHIPPER', 'CHARGED')
    assert extra_time_status(policy, ['VEHICLE_PROBLEM', 'ITEMS_NOT_READY']) == ('CARRIER', 'EXCLUDED')
    assert extra_time_status(policy, ['NEED_HELPER']) == ('UNDECIDED', 'EXCLUDED')
    assert extra_time_status(policy, ['TRAFFIC', 'ITEMS_NOT_READY']) == ('UNDECIDED', 'REVIEW')
    assert extra_time_status(policy, []) == ('UNDECIDED', 'CHARGED')


def test_settings_validate_sort_and_read_the_first_page_saves():
    for bad in [[row('DOCK', 'Dock')], [row('CUSTOM_A', 'Ab')], [row('NO_PROBLEM', 'Fine')], [CUSTOM, {**CUSTOM, 'label': 'Other name'}], [CUSTOM, {**CUSTOM, 'code': 'CUSTOM_B'}], []]:
        with pytest.raises(ValidationError): SettingsData(stop_reasons=bad)
    assert [r.code for r in SettingsData(stop_reasons=[row('OTHER', 'Another issue'), row('TRAFFIC', 'Traffic')]).stop_reasons] == ['TRAFFIC', 'OTHER']
    # The first page saved active, notes and photo flags and a review amount: off reasons were deletions.
    saved = SettingsData.model_validate({'extra_time_review_above': '100', 'stop_reasons': [row('TRAFFIC', 'Traffic', active=False, notes_required=False, photo_required=True),
        row('OTHER', 'Another issue', active=True, notes_required=True, photo_required=False)]})
    assert [r.code for r in saved.stop_reasons] == ['OTHER']
    assert len(SettingsData.model_validate({'stop_reasons': [row('TRAFFIC', 'Traffic', active=False)]}).stop_reasons) == 14
