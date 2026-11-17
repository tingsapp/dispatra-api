from datetime import datetime, timezone
from app.operations.reporting import add_no_driver_attention

AT = datetime(2026, 11, 13, tzinfo=timezone.utc)


def test_monitor_keeps_one_assignment_warning_when_overdue_assignment_incident_explains_it():
    row = ('OPERATIONAL', 'Order needs an assignment: Deadline passed with no driver. No eligible driver (2 checked).', AT)
    attention = {'order-1': [row]}
    add_no_driver_attention(attention, {'order-1': ('No eligible driver (2 checked).', AT)}, {'order-1'})
    assert attention == {'order-1': [row]}


def test_monitor_keeps_no_driver_when_no_operational_incident_explains_it():
    attention = {'order-1': []}
    add_no_driver_attention(attention, {'order-1': ('No eligible driver (1 checked).', AT)})
    assert attention == {'order-1': [('NO_DRIVER', 'No eligible driver (1 checked).', AT)]}


def test_hidden_warnings_return_when_the_order_changes_or_an_incident_gets_worse():
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    from uuid import uuid4
    from app.operations import reporting
    order = SimpleNamespace(id=uuid4(), version=4, created_at=AT)
    incident = SimpleNamespace(order_id=order.id, kind='LATE_PICKUP', title='Late pickup', description='10 min', created_at=AT, severity='CRITICAL')
    def rows(dismissals):
        db = Mock(); db.scalars.side_effect = [Mock(all=Mock(return_value=[incident])), dismissals]
        with patch.object(reporting, 'attention_flags', return_value=[('PRICING', 'Pricing needs review.')]), \
             patch.object(reporting, 'no_driver', return_value={}), patch.object(reporting, 'add_closed_hours_attention'):
            return {row['key']: row['hidden'] for row in reporting.order_attention(db, uuid4(), [order], {}, AT)[order.id]}
    hide = lambda key, version=4, severity=None: SimpleNamespace(order_id=order.id, key=key, order_version=version, severity=severity)
    assert rows([]) == {'PRICING': False, 'OPERATIONAL:LATE_PICKUP': False}
    assert rows([hide('PRICING'), hide('OPERATIONAL:LATE_PICKUP', severity='CRITICAL')]) == {'PRICING': True, 'OPERATIONAL:LATE_PICKUP': True}
    assert rows([hide('PRICING', version=3)])['PRICING'] is False, 'an edited order shows its warnings again'
    assert rows([hide('OPERATIONAL:LATE_PICKUP', severity='WARNING')])['OPERATIONAL:LATE_PICKUP'] is False, 'a worse incident shows again'


def test_auto_mode_reports_unassigned_pickups_outside_work_hours():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from uuid import uuid4
    from app.operations.reporting import add_closed_hours_attention
    from app.operations.schemas import SettingsData
    hours = [{'day': d, 'open': d != 'SUN', 'start': '08:00', 'end': '17:00'} for d in ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')]
    # 2026-11-16 is a Monday: 03:00 UTC is 19:00 Sunday in Vancouver, 18:00 UTC is 10:00 Monday.
    closed = SimpleNamespace(id=uuid4(), status='NEW', scheduled_at=datetime(2026, 11, 16, 3, tzinfo=timezone.utc), created_at=AT)
    open_ = SimpleNamespace(id=uuid4(), status='NEW', scheduled_at=datetime(2026, 11, 16, 18, tzinfo=timezone.utc), created_at=AT)
    assigned = SimpleNamespace(id=uuid4(), status='ASSIGNED', scheduled_at=closed.scheduled_at, created_at=AT)
    def run(mode):
        db = Mock(); db.scalar.return_value = SimpleNamespace(data=SettingsData(work_hours=hours, dispatch_mode=mode).model_dump(mode='json'))
        attention = {o.id: [] for o in (closed, open_, assigned)}
        add_closed_hours_attention(db, uuid4(), [closed, open_, assigned], attention)
        return attention
    auto = run('AUTO')
    assert auto[closed.id] == [('OUTSIDE_HOURS', 'Pickup outside work hours; AUTO dispatch is holding it. Closed on Sundays.', AT)]
    assert auto[open_.id] == [] and auto[assigned.id] == []
    assert all(rows == [] for rows in run('MANUAL').values())
