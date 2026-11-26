from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from pydantic import ValidationError
from app.operations import stop_reasons
from app.operations.pricing import blocks_over, priced_extra_time, with_extra_time
from app.operations.schemas import IssueInput
from app.order_watch.schemas import Reply

AT = datetime(2026, 11, 25, 17, tzinfo=timezone.utc)


def over(minutes, seconds=0):
    return blocks_over(30, AT, AT + timedelta(minutes=minutes, seconds=seconds))


def test_first_block_is_free_then_each_started_block_is_charged():
    assert over(30, 59) == (0, 0)
    assert over(31) == (1, 1)
    assert over(60) == (30, 1)
    assert over(61) == (31, 2)


def test_responsibility_follows_the_reasons_at_a_stop():
    assert stop_reasons.stop_responsibility(['WAITING_FOR_STAFF', 'ITEMS_NOT_READY']) == 'SHIPPER'
    assert stop_reasons.stop_responsibility(['WAITING_FOR_STAFF', 'VEHICLE_PROBLEM']) == 'CARRIER'
    assert stop_reasons.stop_responsibility(['TRAFFIC']) == 'UNDECIDED'
    assert stop_reasons.holds_completion(['WAITING_FOR_STAFF', 'HANDLING_IN_PROGRESS']) is False
    assert stop_reasons.holds_completion(['WAITING_FOR_STAFF', 'DAMAGED_ITEM']) is True
    assert stop_reasons.issue_kind(['WAITING_FOR_STAFF', 'VEHICLE_PROBLEM', 'MISSING_ITEMS']) == 'VEHICLE'
    assert stop_reasons.describe(['WAITING_FOR_STAFF'], ' Dock closed ') == 'Waiting for staff\n\nDock closed'


def test_report_and_reply_share_one_reason_list():
    assert IssueInput(reasons=['DAMAGED_ITEM'], photo_ids=[]).reasons == ['DAMAGED_ITEM']
    assert IssueInput(kind='OTHER', description='Legacy report').reasons == []
    # Notes and photo rules belong to the company's policy (stop_reasons.Policy.check), not the schema.
    assert IssueInput(reasons=['OTHER']).reasons == ['OTHER']
    for bad in [dict(), dict(reasons=['NO_PROBLEM']), dict(reasons=['UNSAFE', 'UNSAFE']), dict(reasons=['not a code'])]:
        with pytest.raises(ValidationError): IssueInput(**bad)
    assert Reply(version=1, reason='TRAFFIC').reasons == ['TRAFFIC']
    assert Reply(version=1, reasons=['WAITING_FOR_STAFF', 'ACCESS_BLOCKED']).reasons == ['WAITING_FOR_STAFF', 'ACCESS_BLOCKED']
    with pytest.raises(ValidationError): Reply(version=1, reasons=['NO_PROBLEM', 'TRAFFIC'])
    assert [c['value'] for c in stop_reasons.choices()][-1] == 'OTHER'
    assert 'NO_PROBLEM' in [c['value'] for c in stop_reasons.choices(report=False)]


def priced(lines, minimum='0'):
    subtotal = sum(Decimal(l['amount']) for l in lines if l['group'] != 'TAX')
    tax = sum(Decimal(l['amount']) for l in lines if l['group'] == 'TAX')
    return {'status': 'PRICED', 'lines': lines, 'subtotal': str(subtotal), 'tax': str(tax), 'total': str(subtotal + tax),
        'context': {'card': {'minimum_subtotal': minimum}}}


def charge(group, amount, taxable=True):
    return {'group': group, 'label': group, 'amount': amount, 'taxable': taxable, 'fuel_eligible': False}


def test_extra_time_line_is_taxed_before_discounts_and_adjustments():
    gst = {**charge('TAX', '5.00', False), 'taxable_base': '100.00', 'rate_percent': '5'}
    result = with_extra_time(priced([charge('FREIGHT', '100.00'), charge('ADJUSTMENT', '0.00'), gst]), Decimal('50'), 'Extra time')
    assert [l['group'] for l in result['lines']] == ['FREIGHT', 'EXTRA_TIME', 'ADJUSTMENT', 'TAX']
    assert result['lines'][-1]['taxable_base'] == '150.00' and result['lines'][-1]['amount'] == '7.50'
    assert (result['subtotal'], result['tax'], result['total']) == ('150.00', '7.50', '157.50')


def test_extra_time_reduces_the_minimum_top_up():
    gst = {**charge('TAX', '6.00', False), 'taxable_base': '120.00', 'rate_percent': '5'}
    result = with_extra_time(priced([charge('FREIGHT', '100.00'), charge('MINIMUM', '20.00'), gst], '120'), Decimal('50'), 'Extra time')
    assert [l['group'] for l in result['lines']] == ['FREIGHT', 'EXTRA_TIME', 'TAX']
    assert (result['subtotal'], result['tax'], result['total']) == ('150.00', '7.50', '157.50')


def breakdown(*statuses):
    gst = {**charge('TAX', '5.00', False), 'taxable_base': '100.00', 'rate_percent': '5'}
    stops = [{'stop_id': str(i), 'stop': f'Pickup {i}', 'minutes_over': 45, 'amount': '100.00', 'reasons': [], 'responsibility': 'UNDECIDED',
        'status': status, 'decided_by_dispatcher': False} for i, status in enumerate(statuses, 1)]
    return {**priced([charge('FREIGHT', '100.00'), gst]), 'extra_time': {'minutes_free': 30, 'amount': '50', 'stops': stops}}


def test_only_charged_stops_are_billed_and_a_decision_replaces_the_line():
    first = priced_extra_time(breakdown('CHARGED', 'EXCLUDED'))
    assert [(l['group'], l['label'], l['amount']) for l in first['lines'] if l['group'] == 'EXTRA_TIME'] == [('EXTRA_TIME', 'Extra time (Pickup 1 45 min)', '100.00')]
    assert first['total'] == '210.00'
    stops = [{**s, 'status': 'WAIVED'} for s in first['extra_time']['stops']]
    waived = priced_extra_time({**first, 'extra_time': {**first['extra_time'], 'stops': stops}})
    assert [l['group'] for l in waived['lines']] == ['FREIGHT', 'TAX']
    assert (waived['subtotal'], waived['tax'], waived['total']) == ('100.00', '5.00', '105.00')
