from types import SimpleNamespace

import pytest

from app.events import rules
from app.operations import models as m


class Database:
    def __init__(self, order, shipper): self.rows = {m.Order: order, m.Shipper: shipper}

    def get(self, model, _identity): return self.rows.get(model)


class Recipients:
    def __init__(self, dispatchers, shipper): self.users, self.owner = dispatchers, shipper

    def dispatchers(self): return self.users

    def shipper(self, _audience): return [self.owner]

    def driver(self, _audience): return []


def created(actor, status):
    order = SimpleNamespace(number='DAO-1234', shipper_id='s', pricing={'status': status})
    dispatchers = [SimpleNamespace(id='d1'), SimpleNamespace(id='d2')]
    audience = SimpleNamespace(order_id='o', shipper_id='s', driver_id=None)
    db = Database(order, SimpleNamespace(name='Acme'))
    return rules.notes(db, 'order.created', 'o', audience, actor, Recipients(dispatchers, SimpleNamespace(id='shipper-user'))), dispatchers


@pytest.mark.parametrize('actor, title', [
    (SimpleNamespace(kind='USER', role='SHIPPER'), 'New order booked'),
    (SimpleNamespace(kind='AGENT', role=None), 'New order from email')])
def test_unpriced_booking_gives_each_dispatcher_one_note(actor, title):
    # Notifications are unique per recipient and event, so a second note for the same dispatcher fails the booking.
    notes, dispatchers = created(actor, 'NEEDS_ATTENTION')
    mine = [n for n in notes if n.user in dispatchers]
    assert [n.user for n in mine] == dispatchers
    assert all(n.title == title and n.severity == 'WARNING' and n.body == 'DAO-1234 from Acme. Needs a price review.' for n in mine)


def test_priced_shipper_booking_and_unpriced_dispatcher_booking():
    notes, _ = created(SimpleNamespace(kind='USER', role='SHIPPER'), 'PRICED')
    assert {(n.title, n.severity, n.body) for n in notes} == {('New order booked', 'INFO', 'DAO-1234 from Acme.')}
    notes, _ = created(SimpleNamespace(kind='USER', role='DISPATCHER'), 'NEEDS_ATTENTION')
    assert {(n.title, n.body) for n in notes} == {('Price review needed', 'DAO-1234 needs a price review.')}
