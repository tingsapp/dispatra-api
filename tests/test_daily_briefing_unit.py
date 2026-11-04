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
        db.scalar.side_effect = [{'time_zone': 'America/Vancouver'}, 3]
        db.execute.return_value.all.return_value = [('NEW', 12), ('COMPLETED', 1)]
        org = uuid4()
        result = search.daily_briefing(db, org, datetime(2026, 11, 5, 2, tzinfo=timezone.utc))
        self.assertEqual(result.day, '2026-11-04')
        self.assertTrue(result.evening)
        self.assertEqual((result.received, result.unassigned, result.completed, result.open_incidents), (13, 12, 1, 3))
        query = db.execute.call_args.args[0]
        params = query.compile().params
        self.assertIn(org, params.values())
        self.assertIn('CANCELLED', params.values())
        self.assertIn(org, db.scalar.call_args.args[0].compile().params.values())

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


if __name__ == '__main__': unittest.main()
