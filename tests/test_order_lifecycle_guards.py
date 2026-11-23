"""Database regressions for closed-order mutations and partial route completion."""
from test_manual_operations import setup, new_order, assign, booking, check, BASE
from conftest import post
from uuid import uuid4


def test_partial_route_completion_cannot_be_released_or_reassigned(setup):
    w = setup
    one, two = new_order(w), new_order(w)
    route = assign(w, one)
    route = assign(w, two, route)
    current = check(w['client'].get(BASE + f'/orders/{one["id"]}'))
    completed = check(post(w['client'], BASE + f'/orders/{one["id"]}/complete', {'version': current['version']}))
    assert completed['status'] == 'COMPLETED'
    # Compare persisted snapshots; a write response and a database read may use different UTC offsets.
    completed = check(w['client'].get(BASE + f'/orders/{one["id"]}'))
    before_route = next(row for row in check(w['client'].get(BASE + '/routes')) if row['id'] == route['id'])
    before_other = check(w['client'].get(BASE + f'/orders/{two["id"]}'))
    assert before_route['status'] == 'PLANNED'
    assert any(stop['status'] == 'COMPLETED' for stop in before_route['stops'])
    released = post(w['client'], BASE + f'/routes/{route["id"]}/release', {
        'version': before_route['version'], 'generation': before_route['generation']})
    assert released.status_code == 409, released.text
    assert check(w['client'].get(BASE + f'/orders/{one["id"]}')) == completed
    assert check(w['client'].get(BASE + f'/orders/{two["id"]}')) == before_other
    assert next(row for row in check(w['client'].get(BASE + '/routes')) if row['id'] == route['id']) == before_route
    assigned = post(w['client'], BASE + f'/orders/{one["id"]}/assign', {
        'version': completed['version'], 'driver_id': w['driver']['id'], 'vehicle_id': w['vehicle']['id'],
        'planned_at': completed['scheduled_at']})
    assert assigned.status_code == 409, assigned.text
    # Replacing the visited stops would orphan the Route visit; revising them in place keeps it.
    replaced = edit(w, completed, booking(w['shipper']['id'], w['service'], w['type_id']))
    assert replaced.status_code == 409, replaced.text
    facts = completed['facts']
    facts['stops'][0]['instructions'] = 'Use the loading dock'
    revised = check(edit(w, completed, facts))
    assert revised['status'] == 'COMPLETED'
    assert [stop['id'] for stop in revised['facts']['stops']] == [stop['id'] for stop in completed['facts']['stops']]
    after_route = next(row for row in check(w['client'].get(BASE + '/routes')) if row['id'] == route['id'])
    assert [stop['status'] for stop in after_route['stops']] == [stop['status'] for stop in before_route['stops']]


def edit(w, order, facts):
    return w['client'].put(BASE + f'/orders/{order["id"]}', headers={'Idempotency-Key': str(uuid4())}, json={
        'version': order['version'], 'booking': facts})


def test_assigned_order_edit_keeps_stop_identities_and_planned_route(setup):
    w = setup
    order = new_order(w)
    route = assign(w, order)
    current = check(w['client'].get(BASE + f'/orders/{order["id"]}'))
    facts = current['facts']
    facts['stops'][1]['instructions'] = 'Call on arrival'
    revised = check(edit(w, current, facts))
    assert revised['status'] == 'ASSIGNED'
    assert revised['facts']['stops'][1]['instructions'] == 'Call on arrival'
    planned = next(row for row in check(w['client'].get(BASE + '/routes')) if row['id'] == route['id'])
    assert sorted(stop['stop_id'] for stop in planned['stops']) == sorted(stop['id'] for stop in facts['stops'])


def test_cancelled_order_cannot_be_assigned_or_completed(setup):
    w = setup
    order = new_order(w)
    cancelled = check(post(w['client'], BASE + f'/orders/{order["id"]}/cancel', {'version': order['version']}))
    assert cancelled['status'] == 'CANCELLED'
    assigned = post(w['client'], BASE + f'/orders/{order["id"]}/assign', {
        'version': cancelled['version'], 'driver_id': w['driver']['id'], 'vehicle_id': w['vehicle']['id'],
        'planned_at': cancelled['scheduled_at']})
    assert assigned.status_code == 409, assigned.text
    completed = post(w['client'], BASE + f'/orders/{order["id"]}/complete', {'version': cancelled['version']})
    assert completed.status_code == 409, completed.text
    assert check(w['client'].get(BASE + f'/orders/{order["id"]}')) == cancelled


def test_locked_planned_route_release_preserves_order_and_stops(setup):
    from conftest import owner_engine
    from sqlalchemy import text
    w = setup
    order = new_order(w)
    route = assign(w, order)
    with owner_engine.begin() as db:
        db.execute(text('UPDATE routes SET locked=true WHERE id=:id'), {'id': route['id']})
    before = check(w['client'].get(BASE + f'/orders/{order["id"]}'))
    before_route = next(row for row in check(w['client'].get(BASE + '/routes')) if row['id'] == route['id'])
    response = post(w['client'], BASE + f'/routes/{route["id"]}/release', {
        'version': route['version'], 'generation': route['generation']})
    assert response.status_code == 409, response.text
    assert check(w['client'].get(BASE + f'/orders/{order["id"]}')) == before
    assert next(row for row in check(w['client'].get(BASE + '/routes')) if row['id'] == route['id']) == before_route


def test_unlocked_planned_route_release_still_unassigns_order(setup):
    w = setup
    order = new_order(w)
    route = assign(w, order)
    released = check(post(w['client'], BASE + f'/routes/{route["id"]}/release', {
        'version': route['version'], 'generation': route['generation']}))
    assert released['status'] == 'CANCELLED'
    current = check(w['client'].get(BASE + f'/orders/{order["id"]}'))
    assert current['status'] == 'NEW' and current['route_id'] is None


def test_archived_order_leaves_the_list_and_keeps_its_history(setup):
    w = setup
    cancelled, unassigned, assigned = new_order(w), new_order(w), new_order(w)
    cancelled = check(post(w['client'], BASE + f'/orders/{cancelled["id"]}/cancel', {'version': cancelled['version']}))
    archived = check(post(w['client'], BASE + f'/orders/{cancelled["id"]}/archive', {'version': cancelled['version']}))
    assert archived['status'] == 'CANCELLED' and archived['archived_at']
    # An unassigned order is cancelled and archived in one step.
    removed = check(post(w['client'], BASE + f'/orders/{unassigned["id"]}/archive', {'version': unassigned['version']}))
    assert removed['status'] == 'CANCELLED' and removed['archived_at']
    assign(w, assigned)
    current = check(w['client'].get(BASE + f'/orders/{assigned["id"]}'))
    refused = post(w['client'], BASE + f'/orders/{assigned["id"]}/archive', {'version': current['version']})
    assert refused.status_code == 409, refused.text
    again = post(w['client'], BASE + f'/orders/{cancelled["id"]}/archive', {'version': archived['version']})
    assert again.status_code == 409, again.text
    listed = {row['id'] for row in check(w['client'].get(BASE + '/orders'))}
    assert listed == {assigned['id']}
    assert check(w['client'].get(BASE + f'/orders/{cancelled["id"]}'))['archived_at']


def test_archived_order_can_be_listed_and_restored(setup):
    w = setup
    order = new_order(w)
    archived = check(post(w['client'], BASE + f'/orders/{order["id"]}/archive', {'version': order['version']}))
    assert [row['id'] for row in check(w['client'].get(BASE + '/orders?archived=true'))] == [order['id']]
    restored = check(post(w['client'], BASE + f'/orders/{order["id"]}/restore', {'version': archived['version']}))
    assert restored['status'] == 'CANCELLED' and restored['archived_at'] is None
    assert [row['id'] for row in check(w['client'].get(BASE + '/orders'))] == [order['id']]
    assert check(w['client'].get(BASE + '/orders?archived=true')) == []
    again = post(w['client'], BASE + f'/orders/{order["id"]}/restore', {'version': restored['version']})
    assert again.status_code == 409, again.text
