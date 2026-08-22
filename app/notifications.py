"""Outbound email.

Two things worth noticing.

**The recipient is never a parameter.** It is looked up from the verified
customer's account using the ``sub`` claim of their token. No caller -- model
or human -- can redirect a support summary to an address of their choosing.
An unverified conversation therefore sends nothing at all, because there is no
address we have any reason to trust.

**Two transports behind one interface**, for the same reason the LLM has two:
a demo that depends on SMTP credentials being right is a demo that fails in
front of someone. With no SMTP configured, mail is written to ``outbox/`` as a
real .eml file you can open, and is visible at ``/api/outbox``.
"""
import logging
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from app import config

log = logging.getLogger("bookly.notifications")

OUTBOX_DIR = Path(__file__).resolve().parent.parent / "outbox"


@dataclass
class Email:
    to: str
    subject: str
    body: str


def mask(address: str) -> str:
    """For transcripts and traces: confirm where it went without reprinting it."""
    local, _, domain = address.partition("@")
    if not domain:
        return "***"
    keep = local[:1]
    return f"{keep}{'•' * max(len(local) - 1, 3)}@{domain}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", text)[:60]


class FileOutbox:
    """Default. Writes a real RFC-822 message; no credentials, never fails."""

    name = "file"

    def __init__(self) -> None:
        self._sent: list[dict] = []

    def send(self, email: Email) -> dict:
        OUTBOX_DIR.mkdir(exist_ok=True)
        message = EmailMessage()
        message["To"] = email.to
        message["From"] = config.EMAIL_FROM
        message["Subject"] = email.subject
        message.set_content(email.body)

        path = OUTBOX_DIR / f"{_slug(email.subject)}.eml"
        path.write_bytes(bytes(message))
        record = {
            "to": email.to, "subject": email.subject,
            "body": email.body, "transport": "file", "path": str(path),
        }
        self._sent.append(record)
        log.info("email written to %s for %s", path.name, mask(email.to))
        return record

    def sent(self) -> list[dict]:
        return list(self._sent)


class SMTPOutbox(FileOutbox):
    """Used when SMTP settings are present. Still records to the outbox list so
    the demo can show what went out, and still writes the .eml as a receipt."""

    name = "smtp"

    def send(self, email: Email) -> dict:
        record = super().send(email)
        message = EmailMessage()
        message["To"] = email.to
        message["From"] = config.EMAIL_FROM
        message["Subject"] = email.subject
        message.set_content(email.body)
        try:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
                smtp.starttls()
                if config.SMTP_USER:
                    smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
                smtp.send_message(message)
            record["transport"] = "smtp"
            log.info("email sent via smtp to %s", mask(email.to))
        except Exception as exc:  # noqa: BLE001
            # A mail outage must not break the handoff. The .eml is already on
            # disk, so nothing is lost and the conversation carries on.
            record["transport"] = "file (smtp failed)"
            record["error"] = f"{type(exc).__name__}: {exc}"
            log.error("smtp send failed, kept file copy: %s", type(exc).__name__)
        return record


_outbox: FileOutbox | None = None


def outbox() -> FileOutbox:
    global _outbox
    if _outbox is None:
        _outbox = SMTPOutbox() if config.SMTP_HOST else FileOutbox()
    return _outbox


def resolve_recipient(account_email: str) -> str:
    """In a demo you want every message in your own inbox. Redirecting all mail
    to a single address is what staging environments do; the body still records
    who it was really for, so nothing is quietly misaddressed."""
    return config.DEMO_EMAIL_OVERRIDE or account_email
