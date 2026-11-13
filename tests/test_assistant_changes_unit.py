"""Chat record changes without a database: the allowed operations, drafting, proposals and the confirmed command."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
from fastapi import HTTPException
from app.assistant import changes, drafting, provider, records, service
from app.assistant.schemas import SearchPlan, DraftedRecord, AssistantChangeCommand, AssistantReply

ORG = uuid4()
ACTOR = SimpleNamespace(organization_id=ORG, role='DISPATCHER', id=uuid4())
ADDRESS = {'text': '400 W 12th Ave, Vancouver, BC V5Y 1V4, Canada', 'city': 'Vancouver', 'province': 'BC', 'postal_code': 'V5Y 1V4',
    'country': 'CA', 'latitude': 49.26, 'longitude': -123.11}


def plan(entity, operation, target='', request=''):
    return SearchPlan(include_records=False, query='', kinds=[], order_statuses=[], action='CHANGE', order_number='', driver='',
        entity=entity, operation=operation, target=target, request=request)


def driver_row():
    return SimpleNamespace(id=uuid4(), name='Driver', number='DDD-8596', email='driver@gmail.com', phone='(604) 567-8777', address=ADDRESS,
        vehicle_id=None, active=True, version=4, data={'qualifications': [], 'crew_size': 1, 'maximum_work_minutes': 480, 'avatar_url': ''})


class OperationTests(unittest.TestCase):
    def test_only_listed_company_records_are_reachable(self):
        self.assertEqual(records.operation('DRIVER', 'UPDATE')[0], 'DRIVER.UPDATE')
        self.assertEqual(records.operation('ORDER', 'DELETE')[0], 'ORDER.CANCEL')
        self.assertIsNone(records.operation('ISSUE', 'CREATE'))
        self.assertIsNone(records.operation('NONE', 'NONE'))
        self.assertEqual(records.named('ORDER.DELETE'), (None, None))
        self.assertEqual(records.named('USER.UPDATE'), (None, None))

    def test_unsupported_request_proposes_nothing(self):
        result = changes.prepare(None, ACTOR, plan('ISSUE', 'CREATE'))
        self.assertIsNone(result['change'])
        self.assertIn('Logins, passwords', result['message'])


class DraftingTests(unittest.TestCase):
    def test_names_resolve_inside_the_company_and_ambiguity_is_reported(self):
        vehicle = SimpleNamespace(id=uuid4(), number='DDV-V12', plate='V12', data={'name': 'Van 12'})
        with patch.object(records, 'find', return_value=[vehicle]) as find:
            body = drafting.resolve(None, ORG, {'data': {'vehicle_id': 'Van 12'}}, None)
        self.assertEqual(body['data']['vehicle_id'], str(vehicle.id))
        self.assertEqual(find.call_args.args[:3], (None, ORG, 'VEHICLE'))
        with patch.object(records, 'find', return_value=[vehicle, vehicle]), self.assertRaises(drafting.Incomplete) as error:
            drafting.resolve(None, ORG, {'data': {'vehicle_id': 'Van'}}, None)
        self.assertIn('More than one vehicle', error.exception.problems[0])

    def test_accessorial_selections_resolve_by_name(self):
        extra = SimpleNamespace(id=uuid4(), code='LIFTGATE', data={'name': 'Liftgate'})
        with patch.object(records, 'find', return_value=[extra]) as find:
            body = drafting.resolve(None, ORG, {'booking': {'accessorials': [{'id': 'Liftgate', 'quantity': '1'}]}}, None)
        self.assertEqual(body['booking']['accessorials'][0]['id'], str(extra.id))
        self.assertEqual(find.call_args.args[2], 'ACCESSORIAL')

    def test_unchanged_addresses_stay_and_new_ones_are_verified(self):
        with patch('app.intake.geocode.verify') as verify:
            self.assertEqual(drafting.resolve(None, ORG, {'data': {'address': dict(ADDRESS)}}, None, {'data': {'address': ADDRESS}})['data']['address'], ADDRESS)
            verify.assert_not_called()
            verify.return_value = {**ADDRESS, 'text': '500 Powell St, Vancouver, BC V6A 1G7, Canada', 'postal_code': 'V6A 1G7'}
            moved = drafting.resolve(None, ORG, {'data': {'address': '500 Powell St, Vancouver, BC V6A 1G7'}}, None)
            self.assertEqual(verify.call_args.args[1], 'V6A 1G7')
            self.assertEqual(moved['data']['address']['postal_code'], 'V6A 1G7')
            verify.return_value = None
            with self.assertRaises(drafting.Incomplete) as error:
                drafting.resolve(None, ORG, {'data': {'address': 'somewhere'}}, None)
            self.assertIn('on the map', error.exception.problems[0])
            self.assertEqual(drafting.resolve(None, ORG, {'data': {'address': 'PO Box 7'}}, None, free_text_address=True)['data']['address'], 'PO Box 7')

    def test_lines_show_only_changes_for_edits_and_filled_fields_for_new_records(self):
        before = {'data': {'name': 'Driver', 'phone': '1', 'address': ADDRESS, 'active': True}}
        after = {'data': {'name': 'Driver', 'phone': '2', 'address': ADDRESS, 'active': False}}
        self.assertEqual(drafting.lines(before, after), [{'field': 'Phone', 'before': '1', 'after': '2'},
            {'field': 'Active', 'before': 'Yes', 'after': 'No'}])
        created = drafting.lines(None, {'name': 'Acme', 'phone': '', 'warehouse': ADDRESS})
        self.assertEqual([line['field'] for line in created], ['Name', 'Warehouse'])

    def test_missing_values_are_plain_questions(self):
        from app.operations.schemas import DriverData
        problems = drafting.Incomplete([])
        try: drafting.validate(DriverData, {'name': 'Sam'})
        except drafting.Incomplete as error: problems = error
        self.assertIn('Email is required.', problems.problems)


class PrepareTests(unittest.TestCase):
    def test_driver_edit_returns_a_checked_proposal_only(self):
        row = driver_row()
        updated = {'data': {**records.editable(None, ACTOR, 'DRIVER', row)['data'], 'phone': '(604) 555-0100'}}
        drafted = DraftedRecord(record_json=json.dumps(updated), missing=[])
        with patch.object(records, 'find', return_value=[row]), patch.object(provider, 'generate', return_value=drafted) as generate, \
             patch.object(changes, 'context', return_value=(None, {})), patch.object(drafting, 'humanize', side_effect=lambda db, org, data: data):
            result = changes.prepare(None, ACTOR, plan('DRIVER', 'UPDATE', 'Driver', 'change phone to 604 555 0100'))
        change = result['change']
        self.assertEqual((change.operation, change.target_id, change.body['version']), ('DRIVER.UPDATE', row.id, 4))
        self.assertEqual([line.model_dump() for line in change.lines], [{'field': 'Phone', 'before': '(604) 567-8777', 'after': '(604) 555-0100'}])
        self.assertIn('Nothing is saved until you confirm', result['message'])
        self.assertNotIn('version', json.loads(generate.call_args.args[2][0]['content'].split('\n', 1)[1])['request_schema']['properties'])

    def test_missing_details_ask_instead_of_proposing(self):
        drafted = DraftedRecord(record_json='{}', missing=['What is the driver’s email?', 'What is their phone number?'])
        with patch.object(provider, 'generate', return_value=drafted), patch.object(changes, 'context', return_value=(None, {})):
            result = changes.prepare(None, ACTOR, plan('DRIVER', 'CREATE', '', 'add driver Sam'))
        self.assertIsNone(result['change'])
        self.assertIn('- What is their phone number?', result['message'])

    def test_assigned_orders_are_not_cancelled_or_edited(self):
        order = SimpleNamespace(id=uuid4(), number='DDO-8711', status='ASSIGNED', version=2, route_id=uuid4(), booking={'shipper': {'name': 'Shipper'}})
        with patch.object(records, 'find', return_value=[order]):
            cancel = changes.prepare(None, ACTOR, plan('ORDER', 'DELETE', 'DDO-8711'))
            edit = changes.prepare(None, ACTOR, plan('ORDER', 'UPDATE', 'DDO-8711', 'pickup at 3pm'))
        self.assertIsNone(cancel['change']); self.assertIn('unassign it first', cancel['message'])
        self.assertIsNone(edit['change']); self.assertIn('Only unassigned orders can be edited', edit['message'])

    def test_new_order_cancel_proposes_the_version_only(self):
        order = SimpleNamespace(id=uuid4(), number='DDO-8711', status='NEW', version=2, route_id=None, booking={'shipper': {'name': 'Shipper'}})
        with patch.object(records, 'find', return_value=[order]):
            change = changes.prepare(None, ACTOR, plan('ORDER', 'CANCEL', 'DDO-8711'))['change']
        self.assertEqual((change.operation, change.body, change.target_id, change.destructive), ('ORDER.CANCEL', {'version': 2}, order.id, True))

    def test_provider_failure_saves_nothing(self):
        with patch.object(provider, 'generate', side_effect=provider.Unavailable('x')), patch.object(changes, 'context', return_value=(None, {})):
            result = changes.prepare(None, ACTOR, plan('VEHICLE', 'CREATE', '', 'add van'))
        self.assertIsNone(result['change'])

    def test_chat_routes_change_requests_to_prepare(self):
        prepared = {'message': 'Which driver?', 'references': [], 'action': None, 'change': None}
        with patch.object(provider, 'generate', return_value=plan('DRIVER', 'UPDATE')), patch.object(changes, 'prepare', return_value=prepared) as prepare, \
             patch('app.assistant.service.monitor_run') as run:
            run.return_value.__enter__.return_value = SimpleNamespace(step=lambda *a, **k: None)
            result = service.ask(None, ACTOR, SimpleNamespace(kind='CHAT', history=[], message='edit driver'))
        self.assertIsInstance(result, AssistantReply)
        self.assertEqual(result.message, 'Which driver?'); prepare.assert_called_once()


class ConfirmTests(unittest.TestCase):
    def confirm(self, operation, body, target_id=None, result=None):
        """Confirm with the workspace command replaced by a stub; `stored` is what idempotency would keep for retries."""
        stored = {}
        def command(db, actor, key, name, payload, run):
            stored.update(key=key, name=name, result=run())
            return stored['result']
        entity, verb = operation.split('.')
        op = records.OPERATIONS[entity][verb]
        runner = Mock(return_value=result or {'id': str(uuid4()), 'version': 1})
        with patch.object(changes, 'command', side_effect=command), patch.object(changes, 'audit') as audit, \
             patch.dict(records.OPERATIONS[entity], {verb: records.Operation(op.verb, op.body, runner, op.drafted, op.destructive)}):
            response = changes.confirm(None, ACTOR, AssistantChangeCommand(operation=operation, target_id=target_id, body=body), 'chat-key-0123456789')
        return response, runner, stored, audit

    def test_confirm_runs_the_workspace_command_with_the_request_key(self):
        target = uuid4()
        response, runner, stored, audit = self.confirm('ORDER.CANCEL', {'version': 2}, target, {'id': str(target), 'number': 'DDO-8711', 'version': 3})
        self.assertEqual(runner.call_args.args[2], target)
        self.assertEqual(runner.call_args.args[4], 'chat-key-0123456789')
        self.assertEqual(response['message'], 'Done. Order DDO-8711 is cancelled.')
        self.assertEqual(audit.call_args.args[2], 'assistant.change_confirmed')

    def test_new_account_password_is_returned_once_and_never_stored(self):
        body = {'name': 'Sam', 'email': 'sam@example.com', 'phone': '6045550100', 'address': ADDRESS}
        response, _, stored, _ = self.confirm('DRIVER.CREATE', body, None, {'id': str(uuid4()), 'name': 'Sam', 'version': 1, 'initial_password': 'secret-once'})
        self.assertEqual(response['initial_password'], 'secret-once')
        self.assertNotIn('initial_password', stored['result'])

    def test_unknown_mismatched_or_invalid_changes_are_refused(self):
        for operation, target, body in [('USER.UPDATE', None, {}), ('ORDER.DELETE', uuid4(), {'version': 1}),
                ('ORDER.CANCEL', None, {'version': 1}), ('SETTINGS.UPDATE', uuid4(), {}), ('ORDER.CANCEL', uuid4(), {'version': 0})]:
            with self.subTest(operation=operation), self.assertRaises(HTTPException) as error:
                changes.confirm(None, ACTOR, AssistantChangeCommand(operation=operation, target_id=target, body=body), 'chat-key-0123456789')
            self.assertEqual(error.exception.status_code, 422)

    def test_catalog_create_cannot_switch_kind(self):
        body = {'kind': 'ACCESSORIAL', 'code': 'X', 'data': {'name': 'X'}}
        with self.assertRaises(HTTPException):
            changes.confirm(None, ACTOR, AssistantChangeCommand(operation='SERVICE.CREATE', body=body), 'chat-key-0123456789')


if __name__ == '__main__': unittest.main()
