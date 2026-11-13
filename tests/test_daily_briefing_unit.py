"""Pure briefing regressions; also runnable directly without a database fixture."""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
from app.assistant import search, service, provider
from app.assistant.schemas import DailyBriefing, GroundedAnswer, BriefingCommentary


class DailyBriefingTests(unittest.TestCase):
    def test_commentary_is_bounded(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError): BriefingCommentary(message='x' * 181)

    def test_company_day_counts_and_tenant_filters(self):
        db = Mock()
        db.scalar.side_effect = [{'time_zone': 'America/Vancouver', 'dispatch_mode': 'AUTO'}, 3]
        statuses, drivers = Mock(), Mock()
        statuses.all.return_value = [('NEW', 12), ('COMPLETED', 1)]
        drivers.all.return_value = [('Alex Chen', 1)]
        db.execute.side_effect = [statuses, drivers]
        org = uuid4()
        result = search.daily_briefing(db, org, datetime(2026, 11, 5, 2, tzinfo=timezone.utc))
        self.assertEqual(result.day, '2026-11-04')
        self.assertTrue(result.evening)
        self.assertEqual((result.received, result.unassigned, result.completed, result.open_incidents), (13, 12, 1, 3))
        self.assertEqual(result.dispatch_mode, 'AUTO')
        self.assertEqual([(row.driver_name, row.orders) for row in result.assigned_by_driver], [('Alex Chen', 1)])
        query = db.execute.call_args_list[0].args[0]
        params = query.compile().params
        self.assertIn(org, params.values())
        self.assertIn('CANCELLED', params.values())
        per_driver = db.execute.call_args_list[1].args[0].compile().params
        self.assertEqual(list(per_driver.values()).count(org), 3, 'orders, routes and drivers are each company-filtered')
        self.assertIn(org, db.scalar.call_args.args[0].compile().params.values())

    def test_dispatch_mode_defaults_to_manual(self):
        db = Mock()
        db.scalar.side_effect = [{}, 0]
        empty = Mock(); empty.all.return_value = []
        db.execute.side_effect = [empty, empty]
        result = search.daily_briefing(db, uuid4(), datetime(2026, 11, 5, 18, tzinfo=timezone.utc))
        self.assertEqual((result.dispatch_mode, result.assigned_by_driver), ('MANUAL', []))

    def test_ai_and_unavailable_keep_authoritative_card(self):
        data = DailyBriefing(day='2026-11-04', time_zone='UTC', evening=False, received=1,
            unassigned=1, assigned=0, in_progress=0, completed=0, open_incidents=0)
        actor = SimpleNamespace(organization_id=uuid4())
        with patch.object(search, 'daily_briefing', return_value=data), patch.object(provider, 'generate', return_value=GroundedAnswer(message='Review the queue.', references=[])) as generate:
            reply = service.brief(Mock(), actor)
            self.assertEqual(reply.briefing, data)
            self.assertEqual(reply.mode, 'AI')
            generate.side_effect = provider.Unavailable()
            reply = service.brief(Mock(), actor)
            self.assertEqual(reply.briefing, data)
            self.assertEqual(reply.mode, 'SEARCH')

    def test_live_totals_never_call_the_provider(self):
        data = DailyBriefing(day='2026-11-04', time_zone='UTC', evening=False, received=2,
            unassigned=1, assigned=1, in_progress=0, completed=0, open_incidents=0)
        actor = SimpleNamespace(organization_id=uuid4(), role='DISPATCHER', id=uuid4())
        with patch.object(search, 'daily_briefing', return_value=data), patch.object(provider, 'generate') as generate:
            result = service.totals(Mock(), actor)
        self.assertEqual(result.briefing, data)
        generate.assert_not_called()
        from fastapi import HTTPException
        with self.assertRaises(HTTPException): service.totals(Mock(), SimpleNamespace(organization_id=uuid4(), role='SHIPPER', id=uuid4()))


if __name__ == '__main__': unittest.main()
