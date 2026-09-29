"""Gmail-compatible TLS SMTP transport with explicit acceptance boundaries."""
from email.message import EmailMessage
from email.utils import format_datetime
import os
import smtplib
import ssl
from app.models import now


class TemporaryFailure(Exception):
    def __init__(self, code: str): self.code = code


class PermanentFailure(Exception):
    def __init__(self, code: str): self.code = code


class UnknownAcceptance(Exception):
    def __init__(self, code: str): self.code = code


def configuration():
    host = os.environ.get('SMTP_HOST', '').strip()
    username = os.environ.get('SMTP_USERNAME', '').strip()
    password = os.environ.get('SMTP_APP_PASSWORD', '').replace(' ', '')
    sender = os.environ.get('SMTP_FROM', '').strip()
    try: port = int(os.environ.get('SMTP_PORT', '465'))
    except ValueError: raise PermanentFailure('SMTP_CONFIGURATION') from None
    if not host or not username or not password or not sender or sender.lower() != username.lower() or not 1 <= port <= 65535:
        raise PermanentFailure('SMTP_CONFIGURATION')
    return host, port, username, password, sender


def send(delivery):
    host, port, username, password, sender = configuration()
    message = EmailMessage()
    message['From'] = sender
    message['To'] = delivery.recipient
    message['Subject'] = delivery.subject
    message['Message-ID'] = delivery.message_id
    message['Date'] = format_datetime(now())
    message.set_content(delivery.body_text)
    message.add_alternative(delivery.body_html, subtype='html')
    try:
        smtp = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
    except (smtplib.SMTPException, OSError):
        raise TemporaryFailure('SMTP_CONNECTION') from None
    try:
        try:
            smtp.login(username, password)
            mail_code, _ = smtp.mail(sender)
            if mail_code != 250:
                raise (PermanentFailure('SMTP_SENDER_REJECTED') if mail_code >= 500 else TemporaryFailure('SMTP_SENDER_TEMPORARY'))
            rcpt_code, _ = smtp.rcpt(delivery.recipient)
            if rcpt_code not in (250, 251):
                raise (PermanentFailure('SMTP_RECIPIENT_REJECTED') if rcpt_code >= 500 else TemporaryFailure('SMTP_RECIPIENT_TEMPORARY'))
        except smtplib.SMTPAuthenticationError:
            raise PermanentFailure('SMTP_AUTHENTICATION') from None
        except smtplib.SMTPResponseException as exc:
            raise (PermanentFailure('SMTP_REJECTED') if exc.smtp_code >= 500 else TemporaryFailure('SMTP_TEMPORARY')) from None
        except (smtplib.SMTPException, OSError):
            raise TemporaryFailure('SMTP_PRE_DATA') from None
        try:
            smtp.data(message.as_bytes())
        except smtplib.SMTPDataError as exc:
            raise (PermanentFailure('SMTP_DATA_REJECTED') if exc.smtp_code >= 500 else TemporaryFailure('SMTP_DATA_TEMPORARY')) from None
        except (smtplib.SMTPException, OSError):
            # DATA may have been accepted before the connection failed. Do not auto-resend.
            raise UnknownAcceptance('SMTP_ACCEPTANCE_UNKNOWN') from None
    finally:
        try: smtp.close()
        except OSError: pass
