from app.operations.reporting import add_no_driver_attention


def test_monitor_keeps_one_assignment_warning_when_operational_incident_has_dispatch_detail():
    description = 'Recorded dispatch check found no eligible driver: verified stop coordinates are required for truck routing.'
    attention = {'order-1': [('OPERATIONAL', 'Order needs an assignment: ' + description)]}
    add_no_driver_attention(attention, {'order-1': description})
    assert attention == {'order-1': [('OPERATIONAL', 'Order needs an assignment: ' + description)]}


def test_monitor_keeps_no_driver_when_no_operational_incident_explains_it():
    attention = {'order-1': []}
    add_no_driver_attention(attention, {'order-1': 'Recorded dispatch check found no eligible driver.'})
    assert attention == {'order-1': [('NO_DRIVER', 'Recorded dispatch check found no eligible driver.')]}
