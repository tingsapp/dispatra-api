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
