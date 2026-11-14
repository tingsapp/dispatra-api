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
             patch.object(reporting, 'no_driver', return_value={}):
            return {row['key']: row['hidden'] for row in reporting.order_attention(db, uuid4(), [order], {}, AT)[order.id]}
    hide = lambda key, version=4, severity=None: SimpleNamespace(order_id=order.id, key=key, order_version=version, severity=severity)
    assert rows([]) == {'PRICING': False, 'OPERATIONAL:LATE_PICKUP': False}
    assert rows([hide('PRICING'), hide('OPERATIONAL:LATE_PICKUP', severity='CRITICAL')]) == {'PRICING': True, 'OPERATIONAL:LATE_PICKUP': True}
    assert rows([hide('PRICING', version=3)])['PRICING'] is False, 'an edited order shows its warnings again'
    assert rows([hide('OPERATIONAL:LATE_PICKUP', severity='WARNING')])['OPERATIONAL:LATE_PICKUP'] is False, 'a worse incident shows again'
