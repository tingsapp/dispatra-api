"""Assignment checks without a database: the per-driver Order count is not a limit; vehicle Maximum stops is."""
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from fastapi import HTTPException
from app.models import now
from app.operations import dispatch, routing


def order(accessorials=()):
    return SimpleNamespace(status='ASSIGNED', booking={}, facts={'items': []},
        pricing={'status': 'PRICED', 'expires_at': (now() + timedelta(days=1)).isoformat(), 'context': {'service': {}, 'accessorials': list(accessorials)}})


def team(crew_size):
    vehicle = SimpleNamespace(id=uuid4(), data={'equipment': []}, archived_at=None, active=True)
    driver = SimpleNamespace(id=uuid4(), vehicle_id=vehicle.id, archived_at=None, active=True, data={'crew_size': crew_size})
    db = Mock(); db.scalar.side_effect = [object(), object()]
    return db, driver, vehicle


class AssignmentLimitTests(unittest.TestCase):
    def test_many_orders_pass_when_every_other_rule_passes(self):
        vehicle = SimpleNamespace(id=uuid4(), data={}, archived_at=None, active=True)
        driver = SimpleNamespace(id=uuid4(), vehicle_id=vehicle.id, archived_at=None, active=True, data={})
        db = Mock(); db.scalar.side_effect = [object(), object()]  # active driver account, open duty session
        actor = SimpleNamespace(organization_id=uuid4())
        self.assertIsNone(dispatch.validate_candidate(db, actor, [order() for _ in range(10)], driver, vehicle, now()))

    def test_helper_orders_need_a_two_person_team_and_say_so(self):
        helper = order([{'name': 'Helper', 'required_crew': 2}])
        actor = SimpleNamespace(organization_id=uuid4())
        db, driver, vehicle = team(1)
        with self.assertRaises(HTTPException) as refused:
            dispatch.validate_candidate(db, actor, [helper], driver, vehicle, now())
        self.assertEqual(refused.exception.detail, 'This order includes Helper, which needs a 2-person team. Choose a driver set as a 2-person team.')
        db, driver, vehicle = team(2)
        self.assertIsNone(dispatch.validate_candidate(db, actor, [helper], driver, vehicle, now()))

    def test_vehicle_maximum_stops_still_applies(self):
        stops = [{'id': str(i), 'address': {}} for i in range(21)]
        with self.assertRaises(HTTPException) as refused:
            routing.optimize(stops, [], {'maximum_stops': 20}, now(), None, 480)
        self.assertIn('Maximum stops', refused.exception.detail)


if __name__ == '__main__': unittest.main()
