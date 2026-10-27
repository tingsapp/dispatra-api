"""Four assigned Orders: explicit arrival, individual POD completion, final route finish."""
import base64
from uuid import uuid4
from datetime import datetime, timezone
from test_manual_operations import setup, new_order, assign, check, post, BASE


def stamp():
    return datetime.now(timezone.utc).isoformat()


def test_four_orders_arrival_and_pod_are_scoped_before_final_route_completion(setup):
    w = setup
    client, mobile = w['client'], w['mobile']
    settings = check(client.get(BASE + '/settings'))
    settings['data']['maximum_active_orders'] = 4
    check(client.put(BASE + '/settings', json={'version': settings['version'], 'data': settings['data']}, headers={'Idempotency-Key': str(uuid4())}))
    orders = [new_order(w) for _ in range(4)]
    route = None
    for order in orders:
        fresh = check(client.get(BASE + f'/orders/{order["id"]}'))
        route = assign(w, fresh, route)
    route = check(post(mobile, BASE + f'/driver/routes/{route["id"]}/start', dict(
        version=route['version'], generation=route['generation'], supports_live_updates=True)))
    by_stop = {stop['id']: order['id'] for order in orders for stop in order['facts']['stops']}
    completed = set()
    while any(visit['status'] != 'COMPLETED' for visit in route['stops']):
        visit = next(visit for visit in route['stops'] if visit['status'] != 'COMPLETED')
        url = BASE + f'/driver/routes/{route["id"]}/stops/{visit["id"]}'
        arrival = dict(version=route['version'], generation=route['generation'], captured_at=stamp())
        route = check(post(mobile, url + '/arrive', arrival))
        assert check(client.get(BASE + f'/orders/{by_stop[visit["stop_id"]]}'))['status'] == 'IN_PROGRESS'
        body = dict(version=route['version'], generation=route['generation'], captured_at=stamp(),
            quantities={item['id']: item['quantity'] for item in route['items'] if item['pickup_id' if visit['stop']['kind'] == 'PICKUP' else 'delivery_id'] == visit['stop_id']})
        if visit['stop']['kind'] == 'DROPOFF':
            assert post(mobile, url + '/complete', body).status_code == 409
            evidence = []
            for kind in ['SIGNATURE', 'PHOTO']:
                proof = check(post(mobile, BASE + f'/driver/stops/{visit["stop_id"]}/evidence', dict(
                    kind=kind, captured_at=stamp(), media_type='image/png',
                    content_base64=base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode())), 201)
                evidence.append(proof['id'])
            assert check(client.get(BASE + f'/orders/{by_stop[visit["stop_id"]]}'))['status'] == 'IN_PROGRESS'
            body.update(recipient_name='Fixture receiver', evidence_ids=evidence, captured_at=stamp())
        route = check(post(mobile, url + '/complete', body))
        if visit['stop']['kind'] == 'DROPOFF':
            completed.add(by_stop[visit['stop_id']])
        for order in orders:
            assert check(client.get(BASE + f'/orders/{order["id"]}'))['status'] == ('COMPLETED' if order['id'] in completed else 'IN_PROGRESS')
        assert route['status'] == 'IN_PROGRESS'
        if len(completed) < 4:
            assert post(mobile, BASE + f'/driver/routes/{route["id"]}/finish', dict(version=route['version'], generation=route['generation'])).status_code == 409
    route = check(post(mobile, BASE + f'/driver/routes/{route["id"]}/finish', dict(version=route['version'], generation=route['generation'])))
    assert len(completed) == 4 and route['status'] == 'COMPLETED'
