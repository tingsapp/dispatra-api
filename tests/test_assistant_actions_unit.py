"""Chat assignment preparation without a database: resolution, plain-language refusals and the checked proposal."""
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from app.models import now
from app.assistant import actions, service, provider
from app.assistant.schemas import SearchPlan, AssistantReply

ORG = uuid4()
ACTOR = SimpleNamespace(organization_id=ORG, role='DISPATCHER', id=uuid4())


def plan(order='1042', driver='Alex'):
    return SearchPlan(include_records=False, query='', kinds=[], order_statuses=[], action='ASSIGN', order_number=order, driver=driver,
        entity='NONE', operation='NONE', target='', request='')


def order(status='NEW', route_id=None, priced=True):
    return SimpleNamespace(id=uuid4(), number='ORD-1042', status=status, version=3, route_id=route_id, booking={'shipper': {'name': 'Acme'}},
        pricing={'status': 'PRICED' if priced else 'REVIEW', 'expires_at': (now() + timedelta(hours=1)).isoformat()})


def driver(name='Alex Chen'):
    return SimpleNamespace(id=uuid4(), name=name, number='D-1', vehicle_id=uuid4())


class PrepareTests(unittest.TestCase):
    def run_prepare(self, orders, drivers, route=None, current=None, candidates=(), excluded=()):
        with patch.object(actions, 'matches', side_effect=[orders, drivers]), \
             patch.object(actions, 'driver_on', return_value=(route, current)), \
             patch.object(actions, 'movable', return_value=None), \
             patch('app.dispatch.candidates.evaluate', return_value=(list(candidates), list(excluded))) as evaluate, \
             patch.object(actions, 'record', return_value=SimpleNamespace(data={'name': 'Truck 12'}, number='T-12', plate='ABC123')):
            return actions.prepare(None, ACTOR, plan()), evaluate

    def test_missing_terms_ask_a_plain_question(self):
        from unittest.mock import Mock
        db = Mock(); db.scalars.return_value.all.return_value = ['ORD-1', 'ORD-2']
        self.assertIn('order number', actions.prepare(db, ACTOR, plan(order=''))['message'])
        self.assertIn('driver’s name', actions.prepare(None, ACTOR, plan(driver=' '))['message'])

    def test_that_order_means_the_only_unassigned_order(self):
        from unittest.mock import Mock
        db = Mock(); db.scalars.return_value.all.return_value = ['ORD-1042']
        with patch.object(actions, 'matches', return_value=[]) as found:
            actions.prepare(db, ACTOR, plan(order=''))
        self.assertEqual(found.call_args.args[4], 'ORD-1042')

    def test_ambiguous_driver_lists_names_and_proposes_nothing(self):
        result, _ = self.run_prepare([order()], [driver('Alex Chen'), driver('Alex Park')])
        self.assertIn('Alex Chen or Alex Park', result['message'])
        self.assertIsNone(result['action'])
        self.assertEqual([ref.kind for ref in result['references']], ['DRIVER', 'DRIVER'])

    def test_unavailable_driver_reason_is_plain_language(self):
        result, evaluate = self.run_prepare([order()], [driver()], excluded=[{'reason': 'Driver must be On Duty before assignment.'}])
        self.assertIn('Alex Chen is off duty', result['message'])
        self.assertIsNone(result['action'])
        self.assertEqual(evaluate.call_args.kwargs['road'], False)

    def test_feasible_assignment_returns_a_proposal_only(self):
        target = order(); person = driver()
        result, evaluate = self.run_prepare([target], [person], candidates=[{'vehicle_id': str(uuid4())}])
        action = result['action']
        self.assertEqual((action.order_id, action.order_version, action.driver_id), (target.id, 3, person.id))
        self.assertEqual(action.vehicle_name, 'Truck 12 (ABC123)')
        self.assertIn('Confirm below', result['message'])
        self.assertEqual(evaluate.call_args.kwargs['driver_id'], person.id)

    def test_unpriced_or_started_orders_are_refused_before_planning(self):
        result, evaluate = self.run_prepare([order(priced=False)], [driver()])
        self.assertIn('price reviewed', result['message']); evaluate.assert_not_called()
        result, evaluate = self.run_prepare([order('IN_PROGRESS', uuid4())], [driver()], current=driver('Sam Lee'))
        self.assertIn('on the road with Sam Lee', result['message']); evaluate.assert_not_called()

    def test_same_driver_needs_no_change(self):
        person = driver()
        result, _ = self.run_prepare([order('ASSIGNED', uuid4())], [person], route=SimpleNamespace(), current=person)
        self.assertIn('already assigned to Alex Chen', result['message'])
        self.assertIsNone(result['action'])

    def test_chat_routes_assignment_requests_to_prepare_without_answer_call(self):
        calls = []
        def generate(schema, *_):
            calls.append(schema)
            return plan()
        prepared = {'message': 'Which one?', 'references': [], 'action': None}
        with patch.object(provider, 'generate', side_effect=generate), patch.object(actions, 'prepare', return_value=prepared), \
             patch('app.assistant.service.monitor_run') as run:
            run.return_value.__enter__.return_value = SimpleNamespace(step=lambda *a, **k: None)
            question = SimpleNamespace(kind='CHAT', history=[], message='assign 1042 to Alex')
            result = service.ask(None, ACTOR, question)
        self.assertIsInstance(result, AssistantReply)
        self.assertEqual((result.message, calls), ('Which one?', [SearchPlan]))


class MovableTests(unittest.TestCase):
    def test_locked_or_shared_routes_cannot_move(self):
        from unittest.mock import Mock
        db = Mock()
        self.assertIn('locked', actions.movable(db, ORG, order(), SimpleNamespace(status='PLANNED', locked=True)))
        db.scalars.return_value.all.return_value = ['ORD-7']
        self.assertIn('shares a route with ORD-7', actions.movable(db, ORG, order(), SimpleNamespace(status='PLANNED', locked=False, id=uuid4())))
        db.scalars.return_value.all.return_value = []
        self.assertIsNone(actions.movable(db, ORG, order(), SimpleNamespace(status='PLANNED', locked=False, id=uuid4())))


if __name__ == '__main__': unittest.main()
