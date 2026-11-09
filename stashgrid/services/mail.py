"""Outgoing email (SG-33).

send_mail() picks a backend from config.MAIL_BACKEND:
  console  write the message to the app log (development, self-hosting
           without SMTP). Emailed links contain secret tokens, so don't use
           this backend where untrusted people can read the logs.
  smtp     send through any SMTP provider (Postmark, SES, Resend, ...).

The last messages are kept in OUTBOX so tests can read them.
"""
import smtplib
import ssl
from collections import deque
from email.message import EmailMessage
from email.utils import make_msgid

from stashgrid import config
from stashgrid.config import logger

OUTBOX = deque(maxlen=100)


def _build(to, subject, body):
    msg = EmailMessage()
    msg["From"] = config.MAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="stashgrid")
    msg.set_content(body)
    return msg


def _send_smtp(msg):
    if config.SMTP_SECURITY == "ssl":
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=15,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15)
    try:
        if config.SMTP_SECURITY == "starttls":
            server.starttls(context=ssl.create_default_context())
        if config.SMTP_USER:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)
    finally:
        server.quit()


def send_mail(to, subject, body):
    """Send a plain-text email. Returns True on success; never raises."""
    msg = _build(to, subject, body)
    OUTBOX.append({"to": to, "subject": subject, "body": body})
    try:
        if config.MAIL_BACKEND == "smtp":
            _send_smtp(msg)
            logger.info("Mail: sent '%s' to %s via SMTP", subject, to)
        else:
            logger.info("Mail (console backend) to %s — %s\n%s", to, subject, body)
        return True
    except Exception as e:
        logger.error("Mail: failed to send '%s' to %s: %s", subject, to, e)
        return False
