"""Authentication and sender identities are independent for platform aliases."""
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.operations import smtp_transport as transport


class SenderTests(unittest.TestCase):
    def environment(self):
        return dict(SMTP_HOST='smtp.example.test', SMTP_PORT='465',
                    SMTP_USERNAME='support@example.test', SMTP_FROM='contact@brand.test',
                    SMTP_FROM_NAME='Brand', SMTP_APP_PASSWORD='test only')

    def capture(self, account=None):
        delivery = SimpleNamespace(recipient='recipient@example.test', subject='Test',
            message_id='<test@example.test>', body_text='Test body', body_html='<p>Test body</p>')
        with patch.object(transport, '_open') as opened:
            smtp = opened.return_value
            smtp.mail.return_value = smtp.rcpt.return_value = (250, b'OK')
            transport.send(delivery, account)
            message = BytesParser(policy=policy.default).parsebytes(smtp.data.call_args.args[0])
        return smtp, message

    def test_platform_alias_authenticates_with_mailbox_and_sends_from_alias(self):
        with patch.dict(transport.os.environ, self.environment(), clear=True):
            smtp, message = self.capture()
        smtp.login.assert_called_once_with('support@example.test', 'testonly')
        smtp.mail.assert_called_once_with('contact@brand.test')
        self.assertEqual(str(message['From']), 'Brand <contact@brand.test>')
        self.assertNotIn('support@example.test', str(message))

    def test_company_mailbox_preserves_original_sender_and_name(self):
        account = transport.Account('smtp.company.test', 465, 'office@company.test',
                                    'test-only', 'Company', company=True)
        with patch.dict(transport.os.environ, self.environment(), clear=True):
            smtp, message = self.capture(account)
        smtp.login.assert_called_once_with('office@company.test', 'test-only')
        smtp.mail.assert_called_once_with('office@company.test')
        self.assertEqual(str(message['From']), 'Company <office@company.test>')

    def test_existing_matching_configuration_and_no_display_name(self):
        env = self.environment()
        env['SMTP_FROM'] = env['SMTP_USERNAME']
        del env['SMTP_FROM_NAME']
        with patch.dict(transport.os.environ, env, clear=True):
            smtp, message = self.capture()
        smtp.mail.assert_called_once_with('support@example.test')
        self.assertEqual(str(message['From']), 'support@example.test')

    def test_missing_required_settings_and_invalid_port_are_rejected(self):
        for field in ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_FROM', 'SMTP_APP_PASSWORD', 'SMTP_PORT'):
            with self.subTest(field=field):
                env = self.environment(); env[field] = ''
                with patch.dict(transport.os.environ, env, clear=True):
                    with self.assertRaises(transport.PermanentFailure):
                        transport.configuration()

    def test_sender_header_injection_is_rejected(self):
        for field in ('SMTP_FROM', 'SMTP_FROM_NAME'):
            env = self.environment(); env[field] += '\r\nBcc: stranger@example.test'
            with patch.dict(transport.os.environ, env, clear=True):
                with self.assertRaises(transport.PermanentFailure):
                    transport.configuration()


if __name__ == '__main__':
    unittest.main()
