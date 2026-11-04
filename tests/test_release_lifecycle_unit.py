"""Isolated release-command state guards; runnable without database fixtures."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
from fastapi import HTTPException
from app.operations import dispatch


class ReleaseLifecycleTests(unittest.TestCase):
    def run_release(self, statuses, *, status='PLANNED', locked=False):
        org, identity = uuid4(), uuid4()
        route = SimpleNamespace(id=identity, status=status, locked=locked, generation=1)
        orders = [SimpleNamespace(status=s, route_id=identity) for s in statuses]
        db = Mock()
        db.scalars.return_value = iter(orders)
        actor = SimpleNamespace(organization_id=org)
        data = Mock(generation=1, version=1)
        with patch.object(dispatch, 'command', side_effect=lambda *args: args[-1]()), patch.object(dispatch, 'company_lock'), patch.object(dispatch, 'record', return_value=route), patch.object(dispatch, 'version'), patch.object(dispatch, 'changed') as changed, patch.object(dispatch, 'route_view', return_value={'ok': True}):
            if locked or status != 'PLANNED' or any(s != 'ASSIGNED' for s in statuses):
                with self.assertRaises(HTTPException) as error:
                    dispatch.release(db, actor, identity, data, 'test')
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual([o.status for o in orders], statuses)
                self.assertTrue(all(o.route_id == identity for o in orders))
                changed.assert_not_called()
                db.execute.assert_not_called()
                self.assertEqual(route.status, status)
            else:
                dispatch.release(db, actor, identity, data, 'test')
                self.assertTrue(all(o.status == 'NEW' and o.route_id is None for o in orders))
                self.assertEqual(route.status, 'CANCELLED')

    def test_partial_closed_routes_preserve_every_order(self):
        for state in ['IN_PROGRESS', 'COMPLETED', 'CANCELLED']:
            with self.subTest(state=state): self.run_release(['ASSIGNED', state])

    def test_locked_or_started_route_cannot_release(self):
        self.run_release(['ASSIGNED'], locked=True)
        self.run_release(['ASSIGNED'], status='IN_PROGRESS')
        self.run_release(['COMPLETED'], status='COMPLETED')

    def test_unlocked_assigned_route_still_releases(self):
        self.run_release(['ASSIGNED', 'ASSIGNED'])


if __name__ == '__main__': unittest.main()
