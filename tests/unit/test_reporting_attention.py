from datetime import datetime, timezone
from app.operations.reporting import add_no_driver_attention

AT = datetime(2026, 11, 13, tzinfo=timezone.utc)


def test_monitor_scopes_orders_routes_and_issues_to_the_same_company_day():
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    from uuid import uuid4
    from zoneinfo import ZoneInfo
    from app.operations import reporting
    org = uuid4()
    db = Mock()
    db.scalars.return_value = []
    with patch.object(reporting, 'now', return_value=AT), \
         patch.object(reporting, 'company_settings', return_value=(None, SimpleNamespace(time_zone='America/Vancouver'))), \
         patch.object(reporting, 'order_attention', return_value={}), \
         patch('app.order_watch.estimates.collect', return_value={}):
        result = reporting.monitor('acme', db, SimpleNamespace(organization_id=org))
    assert result['orders'] == result['routes'] == result['needs_attention'] == []
    # At midnight UTC it is still the preceding company-local date.
    expected = [datetime(2026, 11, day, tzinfo=ZoneInfo('America/Vancouver')).astimezone(timezone.utc) for day in (12, 13)]
    for call in db.scalars.call_args_list[:3]:
        query = call.args[0]
        params = query.compile().params
        assert org in params.values()
        assert 'orders.archived_at IS NULL' in str(query)
        assert 'orders.scheduled_at >=' in str(query) and 'orders.scheduled_at <' in str(query)
        assert 'CANCELLED' in params.values()
        assert [value.astimezone(timezone.utc) for value in params.values() if isinstance(value, datetime)] == expected


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


def test_due_pickup_warning_is_hidden_once_dismissed_at_current_order_version():
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    from uuid import uuid4
    from app.operations import reporting
    order = SimpleNamespace(id=uuid4(), version=2, status='NEW', archived_at=None, scheduled_at=AT, created_at=AT,
        facts={'stops': [{'id': 'pickup', 'kind': 'PICKUP'}]})
    def rows(dismissals):
        db = Mock(); db.scalars.side_effect = [Mock(all=Mock(return_value=[])), dismissals]
        with patch.object(reporting, 'attention_flags', return_value=[]), patch.object(reporting, 'no_driver', return_value={}):
            return reporting.order_attention(db, uuid4(), [order], {}, AT)[order.id]
    shown = rows([])
    assert [(row['key'], row['hidden']) for row in shown] == [('DUE', False)]
    hidden = rows([SimpleNamespace(order_id=order.id, key='DUE', order_version=2, severity=None)])
    assert [(row['key'], row['hidden']) for row in hidden] == [('DUE', True)]
    assert reporting.attention_order_ids([order], {order.id: hidden}, set(), {}, AT) == set()
