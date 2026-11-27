"""Chat record changes without a database: the allowed operations, drafting, proposals and the confirmed command."""
import dataclasses
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
from fastapi import HTTPException
from app.assistant import changes, cleanup, drafting, provider, records, service, workspace
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
        self.assertEqual(records.operation('ORDER', 'DELETE')[0], 'ORDER.ARCHIVE')
        self.assertEqual(records.named('ORDER.ARCHIVE')[0], 'ORDER')
        self.assertIsNone(records.operation('ISSUE', 'CREATE'))
        self.assertEqual(records.operation('NOTIFICATION', 'READ_ALL')[0], 'NOTIFICATION.READ_ALL')
        self.assertIsNone(records.operation('NOTIFICATION', 'DELETE'))
        self.assertIsNone(records.operation('NONE', 'NONE'))
        self.assertEqual(records.named('ORDER.DELETE'), (None, None))
        self.assertEqual(records.named('USER.UPDATE'), (None, None))

    def test_unsupported_request_proposes_nothing(self):
        result = changes.prepare(None, ACTOR, plan('ISSUE', 'CREATE'))
        self.assertIsNone(result['change'])
        self.assertIn('Dispatcher accounts and billing', result['message'])


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

    def test_the_ai_schema_asks_for_names_and_one_line_addresses(self):
        vehicle = changes.schema(records.OPERATIONS['VEHICLE']['CREATE'])
        self.assertEqual(vehicle['properties']['type_id']['type'], 'string')
        self.assertIn('vehicle type', vehicle['properties']['type_id']['description'])
        self.assertEqual(vehicle['properties']['parking_address']['type'], 'string')
        order = changes.schema(records.OPERATIONS['ORDER']['UPDATE'])['$defs']
        self.assertIn('additional service', order['AccessorialSelection']['properties']['id']['description'])

    def test_labels_and_values_read_plainly(self):
        self.assertEqual([drafting.title(key) for key in ('type_id', 'payload_kg', 'volume_m3')], ['Type', 'Payload (kg)', 'Volume (m³)'])
        self.assertEqual((drafting.shown('AVAILABLE'), drafting.shown('BC'), drafting.shown('CHT123')), ('Available', 'BC', 'CHT123'))
        created = drafting.lines(None, {'booking': {'stops': [{'kind': 'PICKUP', 'photo_required': False}], 'items': [{'pallets': 0, 'quantity': 2}]}})
        self.assertEqual([line['field'] for line in created], ['Stop 1 Kind', 'Item 1 Quantity'])
        many = drafting.lines(None, {f'field_{n}': 'x' for n in range(25)})
        self.assertEqual(many[-1], {'field': 'More', 'before': None, 'after': '5 more fields'})

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

    def test_assigned_orders_can_be_edited_but_not_cancelled_or_deleted(self):
        order = SimpleNamespace(id=uuid4(), number='DDO-8711', status='ASSIGNED', version=2, route_id=uuid4(), booking={'shipper': {'name': 'Shipper'}})
        with patch.object(records, 'find', return_value=[order]):
            cancel = changes.prepare(None, ACTOR, plan('ORDER', 'CANCEL', 'DDO-8711'))
            delete = changes.prepare(None, ACTOR, plan('ORDER', 'DELETE', 'DDO-8711'))
        self.assertIsNone(cancel['change']); self.assertIn('unassign it first', cancel['message'])
        self.assertIsNone(delete['change']); self.assertIn('Unassign or complete it first, then I can archive it', delete['message'])

    def test_cancelling_a_closed_order_says_it_is_already_closed(self):
        order = SimpleNamespace(id=uuid4(), number='DTO-2559', status='CANCELLED', version=5, route_id=None, booking={'shipper': {'name': 'Shipper'}})
        with patch.object(records, 'find', return_value=[order]):
            cancel = changes.prepare(None, ACTOR, plan('ORDER', 'CANCEL', 'DTO-2559'))
        self.assertIsNone(cancel['change'])
        self.assertEqual(cancel['message'], 'Order DTO-2559 is already cancelled, so there is nothing to cancel.')

    def test_deleting_an_order_archives_it(self):
        for status, lines in [('CANCELLED', [('Orders list', 'Shown', 'Archived (history kept)')]),
                ('COMPLETED', [('Orders list', 'Shown', 'Archived (history kept)')]),
                ('NEW', [('Status', 'Unassigned', 'Cancelled'), ('Orders list', 'Shown', 'Archived (history kept)')])]:
            order = SimpleNamespace(id=uuid4(), number='DTO-2559', status=status, version=5, route_id=None, booking={'shipper': {'name': 'Shipper'}})
            with self.subTest(status=status), patch.object(records, 'find', return_value=[order]):
                change = changes.prepare(None, ACTOR, plan('ORDER', 'DELETE', 'DTO-2559'))['change']
                self.assertEqual((change.operation, change.body, change.target_id, change.destructive), ('ORDER.ARCHIVE', {'version': 5}, order.id, True))
                self.assertEqual([(line.field, line.before, line.after) for line in change.lines], lines)

    def test_restore_looks_only_among_archived_orders(self):
        order = SimpleNamespace(id=uuid4(), number='DTO-2559', status='CANCELLED', version=6, route_id=None, booking={'shipper': {'name': 'Shipper'}})
        with patch.object(records, 'find', return_value=[order]) as find:
            change = changes.prepare(None, ACTOR, plan('ORDER', 'RESTORE', 'DTO-2559'))['change']
        self.assertEqual(find.call_args.kwargs, {'archived': True})
        self.assertEqual((change.operation, change.body, change.target_id, change.destructive), ('ORDER.RESTORE', {'version': 6}, order.id, False))
        self.assertEqual([(line.field, line.before, line.after) for line in change.lines], [('Orders list', 'Archived', 'Shown (Cancelled)')])
        with patch.object(records, 'find', return_value=[]):
            self.assertIn('an archived order', changes.prepare(None, ACTOR, plan('ORDER', 'RESTORE', 'DTO-1'))['message'])
        self.assertIsNone(records.operation('DRIVER', 'RESTORE'))

    def test_new_order_cancel_proposes_the_version_only(self):
        order = SimpleNamespace(id=uuid4(), number='DDO-8711', status='NEW', version=2, route_id=None, booking={'shipper': {'name': 'Shipper'}})
        with patch.object(records, 'find', return_value=[order]):
            change = changes.prepare(None, ACTOR, plan('ORDER', 'CANCEL', 'DDO-8711'))['change']
        self.assertEqual((change.operation, change.body, change.target_id, change.destructive), ('ORDER.CANCEL', {'version': 2}, order.id, True))

    def test_provider_failure_saves_nothing(self):
        with patch.object(provider, 'generate', side_effect=provider.Unavailable('x')), patch.object(changes, 'context', return_value=(None, {})):
            result = changes.prepare(None, ACTOR, plan('VEHICLE', 'CREATE', '', 'add van'))
        self.assertIsNone(result['change'])

    def test_notifications_are_marked_read_never_deleted(self):
        with patch.object(cleanup, 'unread', return_value=4):
            change = changes.prepare(None, ACTOR, plan('NOTIFICATION', 'READ_ALL'))['change']
        self.assertEqual((change.operation, change.title, change.target_id, change.body, change.destructive),
            ('NOTIFICATION.READ_ALL', 'Mark all notifications read', None, {}, False))
        self.assertEqual(change.lines[0].model_dump(), {'field': 'Unread notifications', 'before': '4', 'after': '0'})
        with patch.object(cleanup, 'unread', return_value=0):
            self.assertIsNone(changes.prepare(None, ACTOR, plan('NOTIFICATION', 'READ_ALL'))['change'])

    def test_all_open_issues_are_proposed_with_their_versions(self):
        order = uuid4()
        rows = [SimpleNamespace(id=uuid4(), order_id=order, kind='DAMAGED_GOODS', version=2 + n) for n in range(12)]
        db = Mock(); db.execute.return_value.all.return_value = [(order, 'DTO-2559')]
        with patch.object(records, 'open_issues', return_value=rows) as found:
            result = changes.prepare(db, ACTOR, plan('ISSUE', 'RESOLVE_ALL'))
        change = result['change']
        self.assertIsNone(found.call_args.args[2], 'no order named means every open issue in the company')
        self.assertEqual((change.operation, change.target_id, change.subject), ('ISSUE.RESOLVE_ALL', None, '12 open issues'))
        self.assertEqual(change.body['issues'][0], {'id': str(rows[0].id), 'version': 2})
        self.assertEqual(change.body['resolution'], 'Resolved from Dispatra chat.')
        fields = [line.field for line in change.lines]
        self.assertEqual(fields[0], 'DTO-2559 damaged goods')
        self.assertEqual(fields[10:], ['More', 'Resolution'])
        with patch.object(records, 'open_issues', return_value=[]):
            empty = changes.prepare(db, ACTOR, plan('ISSUE', 'RESOLVE_ALL'))
        self.assertIsNone(empty['change']); self.assertIn('no open reported issues', empty['message'])

    def test_notifications_can_be_deleted_behind_a_red_card(self):
        db = Mock(); db.scalar.return_value = 7
        change = changes.prepare(db, ACTOR, plan('NOTIFICATION', 'DELETE_ALL'))['change']
        self.assertEqual((change.operation, change.destructive, change.target_id), ('NOTIFICATION.DELETE_ALL', True, None))
        self.assertIn('can’t be undone', change.lines[1].after)

    def test_clearing_alerts_resolves_issues_hides_warnings_and_leaves_requests(self):
        order = SimpleNamespace(id=uuid4(), number='DTO-2559', version=5)
        warning = {'key': 'OPERATIONAL:LATE_PICKUP', 'flag': 'OPERATIONAL', 'description': 'Late', 'at': None, 'severity': 'WARNING', 'hidden': False}
        hidden = {**warning, 'key': 'PRICING', 'severity': None, 'hidden': True}
        issue = SimpleNamespace(id=uuid4(), order_id=order.id, kind='DAMAGED_GOODS', version=2)
        db = Mock(); db.execute.return_value.all.return_value = [(order.id, 'DTO-2559')]
        with patch.object(cleanup, 'open_attention', return_value=({order.id: order}, {order.id: [warning, hidden]})), \
             patch.object(records, 'open_issues', return_value=[issue]), patch.object(records, 'waiting_requests', return_value=2):
            change = changes.prepare(db, ACTOR, plan('ALERT', 'CLEAR_ALL'))['change']
        self.assertEqual((change.operation, change.subject, change.target_id), ('ALERT.CLEAR_ALL', '2 alerts', None))
        self.assertEqual(change.body['hide'], [{'order_id': str(order.id), 'key': 'OPERATIONAL:LATE_PICKUP', 'order_version': 5, 'severity': 'WARNING'}])
        self.assertEqual(change.body['issues'], [{'id': str(issue.id), 'version': 2}])
        fields = [line.field for line in change.lines]
        self.assertEqual(fields, ['DTO-2559 damaged goods', 'DTO-2559 late pickup', 'Resolution', 'Not included'])
        self.assertIn('2 incoming requests', change.lines[-1].after)

    def test_confirmed_clear_hides_then_resolves(self):
        from app.assistant.schemas import AlertClear
        body = AlertClear(issues=[{'id': uuid4(), 'version': 2}], resolution='Checked with driver',
            hide=[{'order_id': uuid4(), 'key': 'PRICING', 'order_version': 3}])
        with patch('app.operations.alerts.hide', return_value={'updated': 1}) as hide, patch('app.operations.issues.resolve_many') as resolve:
            self.assertEqual(cleanup.run_clear(None, ACTOR, body, 'chat-key-0123456789'), {'updated': 2})
        self.assertEqual(hide.call_args.args[2], body.hide)
        self.assertEqual(resolve.call_args.args[2].resolution, 'Checked with driver')

    def test_one_email_request_is_found_and_discarded_by_its_version(self):
        row = SimpleNamespace(id=uuid4(), version=3, status='NEEDS_REVIEW', from_name='Pacific Office Supply', from_address='po@example.com',
            subject='Pickup tomorrow', draft=None, missing=[])
        with patch.object(records, 'find_requests', return_value=[('EMAIL', row)]):
            change = changes.prepare(None, ACTOR, plan('REQUEST', 'DISCARD', 'Pacific'))['change']
            draftless = changes.prepare(None, ACTOR, plan('REQUEST', 'CREATE_ORDER', 'Pacific'))
        self.assertEqual((change.operation, change.target_id, change.body, change.destructive), ('EMAIL_REQUEST.DISCARD', row.id, {'version': 3}, True))
        self.assertIsNone(draftless['change']); self.assertIn('no order draft', draftless['message'])
        tms = SimpleNamespace(id=uuid4(), external_id='TMS-88', status='FAILED')
        with patch.object(records, 'find_requests', return_value=[('TMS', tms)]):
            self.assertEqual(changes.prepare(None, ACTOR, plan('REQUEST', 'RETRY', 'TMS-88'))['change'].operation, 'TMS_REQUEST.RETRY')
            self.assertIsNone(changes.prepare(None, ACTOR, plan('REQUEST', 'LINK_SHIPPER', 'TMS-88'))['change'])

    def test_discarding_all_requests_gives_every_email_its_own_key(self):
        from app.assistant.schemas import RequestBatch
        first, second = uuid4(), uuid4()
        body = RequestBatch(emails=[{'id': first, 'version': 1}, {'id': second, 'version': 4}], tms=[uuid4()])
        with patch('app.intake.routes.discard') as discard, patch('app.integrations.routes.dismiss_inbox') as dismiss:
            self.assertEqual(workspace.run_discard_all(None, ACTOR, body, 'chat-key-0123456789'), {'updated': 3})
        self.assertEqual([call.args[4] for call in discard.call_args_list], [f'chat-key-0123456789:{first}', f'chat-key-0123456789:{second}'])
        dismiss.assert_called_once()

    def test_quote_is_sent_to_the_address_given(self):
        from datetime import datetime, timezone
        quote = SimpleNamespace(id=uuid4(), version=1, pricing={'total': '42.00', 'currency': 'CAD'}, created_at=datetime(2026, 11, 14, tzinfo=timezone.utc))
        db = Mock(); db.scalar.return_value = quote
        change = changes.prepare(db, ACTOR, plan('QUOTE', 'SEND', '', 'send it to buyer@example.com'))['change']
        self.assertEqual((change.target_id, change.body), (quote.id, {'version': 1, 'recipient': 'buyer@example.com'}))
        self.assertIsNone(changes.prepare(db, ACTOR, plan('QUOTE', 'SEND', '', 'send it'))['change'])

    def test_password_reset_card_never_carries_a_password(self):
        row = driver_row(); row.archived_at = None
        with patch.object(records, 'find', return_value=[row]):
            change = changes.prepare(None, ACTOR, plan('DRIVER', 'RESET_PASSWORD', 'Driver'))['change']
        self.assertEqual((change.operation, change.target_id, change.body), ('DRIVER.RESET_PASSWORD', row.id, {'version': 4}))

    def test_integration_drafts_drop_secrets_and_unset_integrations_are_refused(self):
        tms = changes.schema(records.OPERATIONS['TMS']['UPDATE'])
        self.assertNotIn('credential', tms['properties']); self.assertNotIn('webhook_secret', tms['properties'])
        self.assertNotIn('password', changes.schema(records.OPERATIONS['MAILBOX']['UPDATE'])['properties'])
        with patch.object(records, 'company_record', return_value=None):
            result = changes.prepare(None, ACTOR, plan('MAILBOX', 'UPDATE', '', 'use port 995'))
        self.assertIsNone(result['change']); self.assertIn('Integrations', result['message'])
        with patch.object(workspace.connectors, 'get', return_value=None):
            self.assertIn('isn’t connected', changes.prepare(None, ACTOR, plan('CONNECTOR', 'DISCONNECT', 'samsara'))['message'])

    def test_chat_routes_change_requests_to_prepare(self):
        prepared = {'message': 'Which driver?', 'references': [], 'action': None, 'change': None}
        with patch.object(provider, 'generate', return_value=plan('DRIVER', 'UPDATE')), patch.object(changes, 'prepare', return_value=prepared) as prepare, \
             patch('app.assistant.service.monitor_run') as run:
            run.return_value.__enter__.return_value = SimpleNamespace(step=lambda *a, **k: None)
            result = service.ask(None, ACTOR, SimpleNamespace(kind='CHAT', history=[], message='edit driver'))
        self.assertIsInstance(result, AssistantReply)
        self.assertEqual(result.message, 'Which driver?'); prepare.assert_called_once()

    def test_unassign_by_driver_name_proposes_releasing_their_planned_route(self):
        alex = driver_row(); alex.name = 'Alex Chen'
        route = SimpleNamespace(id=uuid4(), status='PLANNED', locked=False, version=2, generation=1, driver_id=alex.id)
        order = SimpleNamespace(id=uuid4(), number='ORD-123', status='ASSIGNED', version=3, route_id=route.id, booking={'shipper': {'name': 'Acme'}})
        db = Mock()
        db.scalar.return_value = route
        db.scalars.return_value.all.return_value = [order]
        with patch.object(records, 'find', side_effect=[[], [alex]]):
            result = changes.prepare(db, ACTOR, plan('ORDER', 'UNASSIGN', 'Alex'))
        change = result['change']
        self.assertEqual((change.operation, change.target_id, change.body), ('ORDER.UNASSIGN', route.id, {'version': 2, 'generation': 1}))
        self.assertEqual([line.model_dump() for line in change.lines], [{'field': 'Order ORD-123', 'before': 'Alex Chen', 'after': 'Unassigned'}])

    def test_unassign_by_driver_entity_handles_multiple_assigned_orders(self):
        alex = driver_row(); alex.name = 'Alex Chen'
        route = SimpleNamespace(id=uuid4(), status='PLANNED', locked=False, version=4, generation=2, driver_id=alex.id)
        orders = [
            SimpleNamespace(id=uuid4(), number='ORD-1', status='ASSIGNED', version=1, route_id=route.id, booking={'shipper': {'name': 'Acme'}}),
            SimpleNamespace(id=uuid4(), number='ORD-2', status='ASSIGNED', version=1, route_id=route.id, booking={'shipper': {'name': 'Acme'}}),
        ]
        db = Mock(); db.scalar.return_value = route; db.scalars.return_value.all.return_value = orders
        with patch.object(records, 'find', return_value=[alex]):
            result = changes.prepare(db, ACTOR, plan('DRIVER', 'UNASSIGN', 'Alex'))
        change = result['change']
        self.assertEqual((change.operation, change.subject, change.target_id), ('ORDER.UNASSIGN', 'Alex Chen’s 2 assigned orders', route.id))
        self.assertEqual([line.field for line in change.lines], ['Order ORD-1', 'Order ORD-2'])

    def test_completed_or_cancelled_orders_refuse_edit_before_drafting(self):
        order = SimpleNamespace(id=uuid4(), number='ORD-99', status='COMPLETED', version=2, route_id=None, booking={'shipper': {'name': 'Acme'}})
        with patch.object(records, 'find', return_value=[order]), patch.object(provider, 'generate') as generate:
            result = changes.prepare(None, ACTOR, plan('ORDER', 'UPDATE', 'ORD-99', 'change pickup time'))
        self.assertIsNone(result['change'])
        self.assertIn('completed', result['message'])
        generate.assert_not_called()

    def test_drafting_repairs_once_on_validation_error(self):
        row = driver_row()
        bad = DraftedRecord(record_json=json.dumps({'data': {**records.editable(None, ACTOR, 'DRIVER', row)['data'], 'email': 'not-an-email'}}), missing=[])
        good = DraftedRecord(record_json=json.dumps({'data': {**records.editable(None, ACTOR, 'DRIVER', row)['data'], 'phone': '(604) 555-0199'}}), missing=[])
        with patch.object(records, 'find', return_value=[row]), patch.object(provider, 'generate', side_effect=[bad, good]) as generate, \
             patch.object(changes, 'context', return_value=(None, {})), patch.object(drafting, 'humanize', side_effect=lambda db, org, data: data):
            result = changes.prepare(None, ACTOR, plan('DRIVER', 'UPDATE', 'Driver', 'change phone to 604 555 0199'))
        self.assertEqual(generate.call_count, 2)
        self.assertIn('previous_errors', generate.call_args_list[1].args[2][0]['content'])
        self.assertEqual(result['change'].operation, 'DRIVER.UPDATE')

    def test_clearing_alerts_includes_due_pickups(self):
        order = SimpleNamespace(id=uuid4(), number='DTO-100', version=2)
        due = {'key': 'DUE', 'flag': 'DUE', 'description': 'Scheduled pickup time reached; pickup is still pending.', 'at': None, 'severity': None, 'hidden': False}
        db = Mock(); db.execute.return_value.all.return_value = []
        with patch.object(cleanup, 'open_attention', return_value=({order.id: order}, {order.id: [due]})), \
             patch.object(records, 'open_issues', return_value=[]), patch.object(records, 'waiting_requests', return_value=0):
            change = changes.prepare(db, ACTOR, plan('ALERT', 'CLEAR_ALL'))['change']
        self.assertEqual(change.body['hide'], [{'order_id': str(order.id), 'key': 'DUE', 'order_version': 2, 'severity': None}])
        self.assertEqual(change.lines[0].field, 'DTO-100 pickup due')

    def test_search_tokens_and_order_number_cleaning(self):
        from app.assistant import search
        self.assertEqual(records.clean_order_term('#123'), '123')
        self.assertEqual(records.clean_order_term('order #ORD-42'), 'ORD-42')
        self.assertEqual(search.tokens('Alex Burnaby'), ['Alex', 'Burnaby'])
        self.assertEqual(search.tokens('%'), ['%'])
        self.assertEqual(search.tokens('604-555-0102'), ['604-555-0102'])

    def test_multi_order_edit_from_conversation_history_prepares_one_batch_card(self):
        first = SimpleNamespace(id=uuid4(), number='DDO-3967', status='NEW', version=1, route_id=None, booking={'shipper': {'name': 'Zia'}}, facts={'scheduled_at': '2026-11-25T20:00:00+00:00'})
        second = SimpleNamespace(id=uuid4(), number='DDO-2067', status='NEW', version=2, route_id=None, booking={'shipper': {'name': 'Zia'}}, facts={'scheduled_at': '2026-11-25T20:00:00+00:00'})
        db = Mock(); db.scalars.return_value.all.return_value = [first, second]
        history = [SimpleNamespace(role='assistant', content='Yes, there are 2 orders:\n- order DDO-3967\n- order DDO-2067')]
        updated = lambda row: {'version': row.version, 'booking': {'scheduled_at': '2026-11-27T20:00:00+00:00'}}
        with patch.object(changes, 'context', return_value=(None, {})), \
             patch.object(drafting, 'humanize', side_effect=lambda db, org, data: data), \
             patch.object(changes, '_resolve_and_validate', side_effect=[updated(first), updated(second)]), \
             patch.object(changes, 'normalized', side_effect=lambda db, actor, op, body: {k: v for k, v in body.items() if k != 'version'}):
            result = changes.prepare(db, ACTOR, plan('ORDER', 'UPDATE', '', 'change scheduled pickup to today'), history=history)
        change = result['change']
        self.assertEqual((change.operation, change.subject, change.target_id), ('ORDER.UPDATE_MANY', '2 orders', None))
        self.assertEqual([item['id'] for item in change.body['items']], [str(first.id), str(second.id)])
        self.assertEqual([line.field for line in change.lines], ['DDO-3967 Scheduled at', 'DDO-2067 Scheduled at'])

    def test_multi_order_archive_splits_comma_targets_and_uses_per_order_keys(self):
        from app.assistant.schemas import OrderBatchClose
        first = SimpleNamespace(id=uuid4(), number='DDO-3967', status='NEW', version=1, route_id=None, booking={'shipper': {'name': 'Zia'}})
        second = SimpleNamespace(id=uuid4(), number='DDO-2067', status='NEW', version=3, route_id=None, booking={'shipper': {'name': 'Zia'}})
        with patch.object(records, 'find', side_effect=[[first], [second]]):
            change = changes.prepare(None, ACTOR, plan('ORDER', 'DELETE', 'DDO-3967, DDO-2067'))['change']
        self.assertEqual((change.operation, change.destructive, change.target_id), ('ORDER.ARCHIVE_MANY', True, None))
        self.assertEqual(change.body, {'items': [{'id': str(first.id), 'version': 1}, {'id': str(second.id), 'version': 3}]})
        body = OrderBatchClose.model_validate(change.body)
        with patch('app.operations.orders.archive_order') as archive_order:
            self.assertEqual(changes.run_order_batch_archive(None, ACTOR, body, 'chat-key-0123456789'), {'updated': 2})
        self.assertEqual([call.args[4] for call in archive_order.call_args_list], [f'chat-key-0123456789:{first.id}', f'chat-key-0123456789:{second.id}'])

    def test_order_status_change_via_update_redirects_without_drafting_schema_errors(self):
        in_progress = SimpleNamespace(id=uuid4(), number='DTO-7479', status='IN_PROGRESS', version=3, route_id=uuid4(), booking={'shipper': {'name': 'Pacific Office Supply'}})
        with patch.object(records, 'find', return_value=[in_progress]), patch.object(provider, 'generate') as generate:
            result = changes.prepare(None, ACTOR, plan('ORDER', 'UPDATE', 'DTO-7479', 'change the status of dto-7479 to new'))
        self.assertIsNone(result['change'])
        self.assertIn('already on the road', result['message'])
        self.assertNotIn('schema', result['message'].lower())
        generate.assert_not_called()

        assigned = SimpleNamespace(id=uuid4(), number='DTO-7480', status='ASSIGNED', version=2, route_id=uuid4(), booking={'shipper': {'name': 'Pacific'}})
        route = SimpleNamespace(id=assigned.route_id, status='PLANNED', locked=False, version=1, generation=1, driver_id=uuid4())
        alex = SimpleNamespace(id=route.driver_id, name='Alex Chen')
        db = Mock(); db.scalars.return_value.all.return_value = []
        with patch.object(records, 'find', return_value=[assigned]), patch.object(changes, 'record', side_effect=[route, alex]), \
             patch.object(provider, 'generate') as generate:
            result = changes.prepare(db, ACTOR, plan('ORDER', 'UPDATE', 'DTO-7480', 'change status to new'))
        self.assertEqual(result['change'].operation, 'ORDER.UNASSIGN')
        generate.assert_not_called()

        with patch.object(records, 'find', return_value=[in_progress]), patch.object(provider, 'generate') as generate:
            done = changes.prepare(None, ACTOR, plan('ORDER', 'UPDATE', 'DTO-7479', 'change status to completed'))
        self.assertEqual(done['change'].operation, 'ORDER.COMPLETE')
        generate.assert_not_called()


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
             patch.dict(records.OPERATIONS[entity], {verb: dataclasses.replace(op, run=runner)}):
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

    def test_bulk_changes_have_no_target_and_report_a_count(self):
        response, runner, _, audit = self.confirm('NOTIFICATION.READ_ALL', {}, None, {'updated': 3})
        self.assertEqual(response['message'], 'Done. 3 notifications are marked read.')
        self.assertEqual(audit.call_args.args[3], ACTOR.id)
        self.assertEqual(runner.call_args.args[4], 'chat-key-0123456789')
        issues = {'issues': [{'id': str(uuid4()), 'version': 1}], 'resolution': 'Customer called back'}
        response, runner, _, _ = self.confirm('ISSUE.RESOLVE_ALL', issues, None, {'updated': 1})
        self.assertEqual(response['message'], 'Done. 1 issue is resolved.')
        self.assertEqual(runner.call_args.args[3].resolution, 'Customer called back')

    def test_shipper_password_reset_returns_the_new_password_once(self):
        target = uuid4()
        with patch('app.operations.directory.change_directory_password', return_value={'id': str(target), 'version': 5}) as change:
            response, _, stored, _ = self.confirm('SHIPPER.RESET_PASSWORD', {'version': 4}, target, {'id': str(target), 'version': 5, 'initial_password': 'once'})
            result = workspace.reset_shipper(None, ACTOR, target, SimpleNamespace(version=4), 'chat-key-0123456789')
        self.assertEqual(response['initial_password'], 'once')
        self.assertNotIn('initial_password', stored['result'])
        self.assertGreaterEqual(len(result['initial_password']), 12)
        self.assertEqual(change.call_args.args[3].new_password, result['initial_password'])

    def test_unknown_mismatched_or_invalid_changes_are_refused(self):
        for operation, target, body in [('USER.UPDATE', None, {}), ('ORDER.DELETE', uuid4(), {'version': 1}),
                ('ORDER.CANCEL', None, {'version': 1}), ('SETTINGS.UPDATE', uuid4(), {}), ('ORDER.CANCEL', uuid4(), {'version': 0}),
                ('NOTIFICATION.READ_ALL', uuid4(), {}), ('NOTIFICATION.READ_ALL', None, {'all': True}),
                ('ISSUE.RESOLVE_ALL', None, {'issues': [], 'resolution': 'done'}), ('ALERT.CLEAR_ALL', uuid4(), {'resolution': 'done'}),
                ('ALERT.CLEAR_ALL', None, {'resolution': 'done'}), ('DRIVER.RESET_PASSWORD', None, {'version': 1}),
                ('TMS.UPDATE', None, {'base_url': 'https://tms.example.com', 'credential': 'new-secret-123'})]:
            with self.subTest(operation=operation), self.assertRaises(HTTPException) as error:
                changes.confirm(None, ACTOR, AssistantChangeCommand(operation=operation, target_id=target, body=body), 'chat-key-0123456789')
            self.assertEqual(error.exception.status_code, 422)

    def test_catalog_create_cannot_switch_kind(self):
        body = {'kind': 'ACCESSORIAL', 'code': 'X', 'data': {'name': 'X'}}
        with self.assertRaises(HTTPException):
            changes.confirm(None, ACTOR, AssistantChangeCommand(operation='SERVICE.CREATE', body=body), 'chat-key-0123456789')


if __name__ == '__main__': unittest.main()
