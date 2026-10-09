"""Gmail-compatible TLS SMTP transport with explicit acceptance boundaries.

Mail goes out through the company's own mailbox account when one is connected, else the platform `SMTP_*` account.
"""
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
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


@dataclass(frozen=True)
class Account:
    host: str
    port: int
    username: str
    password: str
    sender_name: str = ''
    company: bool = False  # company-entered hosts get the public-host check


def configuration():
    host = os.environ.get('SMTP_HOST', '').strip()
    username = os.environ.get('SMTP_USERNAME', '').strip()
    password = os.environ.get('SMTP_APP_PASSWORD', '').replace(' ', '')
    sender = os.environ.get('SMTP_FROM', '').strip()
    try: port = int(os.environ.get('SMTP_PORT', '465'))
    except ValueError: raise PermanentFailure('SMTP_CONFIGURATION') from None
    if not host or not username or not password or not sender or sender.lower() != username.lower() or not 1 <= port <= 65535:
        raise PermanentFailure('SMTP_CONFIGURATION')
    return Account(host, port, username, password)


def _open(account, timeout=20):
    """Implicit TLS on 465; STARTTLS (required) on any other port."""
    if account.company:
        from app.intake.mailbox import MailboxError, _public
        try: _public(account.host, account.port)
        except MailboxError as error: raise PermanentFailure('SMTP_' + error.code) from None
    context = ssl.create_default_context()
    try:
        if account.port == 465: return smtplib.SMTP_SSL(account.host, account.port, timeout=timeout, context=context)
        smtp = smtplib.SMTP(account.host, account.port, timeout=timeout)
        smtp.starttls(context=context)
        return smtp
    except (smtplib.SMTPException, OSError):
        raise TemporaryFailure('SMTP_CONNECTION') from None


def check(account):
    """Log in only; raises PermanentFailure('SMTP_AUTHENTICATION') or TemporaryFailure('SMTP_CONNECTION')."""
    smtp = _open(account)
    try: smtp.login(account.username, account.password)
    except smtplib.SMTPAuthenticationError: raise PermanentFailure('SMTP_AUTHENTICATION') from None
    except (smtplib.SMTPException, OSError): raise TemporaryFailure('SMTP_CONNECTION') from None
    finally:
        try: smtp.close()
        except OSError: pass


def send(delivery, account=None):
    account = account or configuration()
    username, password, sender = account.username, account.password, account.username
    message = EmailMessage()
    message['From'] = formataddr((account.sender_name, sender)) if account.sender_name else sender
    message['To'] = delivery.recipient
    message['Subject'] = delivery.subject
    message['Message-ID'] = delivery.message_id
    message['Date'] = format_datetime(now())
    message.set_content(delivery.body_text)
    message.add_alternative(delivery.body_html, subtype='html')
    smtp = _open(account)
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
