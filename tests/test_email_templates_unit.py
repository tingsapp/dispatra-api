"""Presentation contracts and the database-independent alert fallback."""
import importlib.util
from email.message import EmailMessage
from email.parser import BytesParser
from email import policy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import email_templates as templates


class EmailTemplateTests(unittest.TestCase):
    def test_mime_embeds_logo_and_preserves_plain_text_after_serialization(self):
        text, html = templates.verification('012345')
        message = EmailMessage()
        message.set_content(text)
        templates.add_html(message, html)
        parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        self.assertIn('012345', parsed.get_body(preferencelist=('plain',)).get_content())
        self.assertIn(templates.LOGO_URL, parsed.get_body(preferencelist=('html',)).get_content())
        logo = next(part for part in parsed.walk() if part.get_content_type() == 'image/png')
        self.assertEqual(logo['Content-ID'], f'<{templates.LOGO_CID}>')
        self.assertEqual(logo.get_content_disposition(), 'inline')
        self.assertEqual(logo.get_payload(decode=True), (Path(templates.__file__).parent / 'assets/dispatra-logo.png').read_bytes())

    def test_previously_queued_html_does_not_gain_an_unreferenced_attachment(self):
        message = EmailMessage()
        message.set_content('Existing message')
        templates.add_html(message, '<p>Existing message</p>')
        self.assertEqual([part.get_content_type() for part in message.walk()],
                         ['multipart/alternative', 'text/plain', 'text/html'])

    def test_smtp_transport_sends_embedded_logo_with_original_envelope(self):
        from app.operations import smtp_transport
        text, html = templates.verification('012345')
        delivery = SimpleNamespace(recipient='recipient@example.com', subject='Verify your Dispatra account',
            message_id='<test@example.com>', body_text=text, body_html=html)
        account = smtp_transport.Account('smtp.example.com', 465, 'sender@example.com', 'test-only')
        with patch.object(smtp_transport, '_open') as opened:
            smtp = opened.return_value
            smtp.mail.return_value = (250, b'OK')
            smtp.rcpt.return_value = (250, b'OK')
            smtp_transport.send(delivery, account)
            smtp.mail.assert_called_once_with(account.username)
            smtp.rcpt.assert_called_once_with(delivery.recipient)
            parsed = BytesParser(policy=policy.default).parsebytes(smtp.data.call_args.args[0])
            self.assertEqual(parsed['Subject'], delivery.subject)
            self.assertEqual(sum(part.get_content_type() == 'image/png' for part in parsed.walk()), 1)
            smtp.close.assert_called_once()

    def test_verification_preserves_leading_zeroes_and_expiry(self):
        text, html = templates.verification('012345')
        for part in (text, html):
            self.assertIn('012345', part)
            self.assertIn('10 minutes', part)
        self.assertNotIn('012345', html.split('</head>')[0])
        self.assertIn('alt="Dispatra"', html)
        self.assertIn('If you did not create', html)

    def test_quote_preserves_snapshot_amounts_and_escapes_every_field(self):
        pricing = {'currency':'CAD', 'subtotal':'120.00', 'tax':'6.00', 'total':'126.00',
                   'lines':[{'label':'Delivery <special>', 'amount':'120.00'}]}
        text, html = templates.quote('A & B', 'Quote <ABC>', '<buyer@example.com>', pricing,
                                     ['Pickup: <script>alert(1)</script>', 'Valid until: 2026-12-01'])
        for value in ('120.00', '6.00', '126.00 CAD', '2026-12-01'):
            self.assertIn(value, text)
            self.assertIn(value, html)
        self.assertNotIn('<script>', html)
        self.assertIn('Delivery &lt;special&gt;', html)
        self.assertIn('From A &amp; B', html)
        self.assertIn('&lt;buyer@example.com&gt;', html)

    def test_order_notification_content_preferences_and_link_are_preserved(self):
        text, html = templates.order_update('A & B', '<Alice>', 'Driver assigned',
            'Order D-123: driver assigned.', 'https://example.com/acme/shipper?a=1&b=2')
        self.assertIn('Order D-123: driver assigned.', text)
        self.assertIn('Order D-123: driver assigned.', html)
        self.assertIn('Hello &lt;Alice&gt;', html)
        self.assertIn('https://example.com/acme/shipper?a=1&amp;b=2', html)
        self.assertIn('Ask them to turn these emails off.', html)
        self.assertNotIn('View your orders', templates.order_update('Acme', 'Alice', 'Update', 'Body')[1])

    def test_buttons_do_not_render_unsafe_or_relative_urls(self):
        for url in ('javascript:alert(1)', '//example.com', '/shipper', 'data:text/html,test'):
            self.assertEqual(templates.button('View', url), '')

    def test_service_alert_escapes_checks_and_contains_monitor_link(self):
        html = templates.service_alert(['database_and_api', '<unsafe>'])
        self.assertIn('&lt;unsafe&gt;', html)
        self.assertIn('https://dispatra.com/admin/monitor', html)
        self.assertIn('no customer data or logs', html)

    def test_watchdog_sends_multipart_and_survives_missing_template(self):
        spec = importlib.util.spec_from_file_location('email_watchdog', Path(__file__).parents[1] / 'scripts/production-watchdog.py')
        watchdog = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(watchdog)
        env = {'SMTP_FROM':'system@example.com', 'TECHNICAL_ALERT_EMAIL':'owner@example.com',
               'SMTP_HOST':'smtp.example.com', 'SMTP_USERNAME':'system@example.com',
               'SMTP_APP_PASSWORD':'test-only', 'SMTP_PORT':'465'}
        with patch.dict(watchdog.os.environ, env), patch.object(watchdog.smtplib, 'SMTP_SSL') as smtp:
            connection = smtp.return_value
            watchdog.notify(['database_and_api'])
            message = connection.send_message.call_args.args[0]
            self.assertIn('database_and_api', message.get_body(preferencelist=('plain',)).get_content())
            self.assertIn('alt="Dispatra"', message.get_body(preferencelist=('html',)).get_content())
            with patch.object(watchdog.runpy, 'run_path', side_effect=FileNotFoundError):
                watchdog.notify(['database_and_api'])
            fallback = connection.send_message.call_args.args[0]
            self.assertFalse(fallback.is_multipart())
            self.assertIn('database_and_api', fallback.get_content())


if __name__ == '__main__':
    unittest.main()
