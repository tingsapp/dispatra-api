"""Run directly with unittest; no database or paid provider calls."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
from app.agents.brain import Brain, Assessment


class BrainTests(unittest.TestCase):
    def test_missing_ai_uses_explicit_safe_rules(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            plan, source = Brain().assess('RUNTIME_ERROR', 'Coarse runtime failure')
            self.assertEqual((plan.recommendation, source), ('ADMIN_REVIEW', 'RULES'))
            plan, source = Brain().assess('MAILBOX_CONNECTION', {'error_code': 'CONNECTION_FAILED'}, True)
            self.assertEqual((plan.recommendation, source), ('CHECK_MAILBOX', 'RULES'))

    def test_ai_cannot_expand_automatic_permissions_and_budget_is_bounded(self):
        client = Mock()
        client.responses.parse.return_value = SimpleNamespace(output_parsed=Assessment(
            recommendation='CHECK_MAILBOX', reason='Try a probe.', next_step='Check connectivity.'))
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only'}), patch('app.agents.brain.openai.OpenAI') as factory:
            factory.return_value.__enter__.return_value = client
            brain = Brain(budget=1)
            plan, source = brain.assess('RUNTIME_ERROR', 'Limited runtime evidence', False)
            self.assertEqual((plan.recommendation, source), ('ADMIN_REVIEW', 'RULES'))
            brain.assess('MAILBOX_CONNECTION', {'error_code': 'CONNECTION_FAILED'}, True)
            self.assertEqual(client.responses.parse.call_count, 1)
            self.assertFalse(client.responses.parse.call_args.kwargs['store'])
            self.assertEqual(factory.call_args.kwargs['max_retries'], 0)

    def test_invalid_ai_output_falls_back_without_claiming_repair(self):
        client = Mock()
        client.responses.parse.side_effect = ValueError('untrusted invalid response')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only'}), patch('app.agents.brain.openai.OpenAI') as factory:
            factory.return_value.__enter__.return_value = client
            plan, source = Brain().assess('RUNTIME_ERROR', 'Coarse runtime failure')
            self.assertEqual((plan.recommendation, source), ('ADMIN_REVIEW', 'RULES'))


if __name__ == '__main__': unittest.main()
