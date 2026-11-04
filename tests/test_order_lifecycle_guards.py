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
    edited = w['client'].put(BASE + f'/orders/{one["id"]}', headers={'Idempotency-Key': str(uuid4())}, json={
        'version': completed['version'], 'booking': booking(w['shipper']['id'], w['service'], w['type_id'])})
    assert edited.status_code == 409, edited.text
    assert check(w['client'].get(BASE + f'/orders/{one["id"]}')) == completed


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
