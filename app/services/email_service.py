from __future__ import annotations

import html
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import TYPE_CHECKING

from flask import current_app

if TYPE_CHECKING:
    from ..models import User


class EmailDeliveryError(RuntimeError):
    """Raised when an email could not be handed to the configured SMTP server."""


@dataclass(frozen=True)
class EmailDelivery:
    recipient: str
    subject: str
    backend: str


def _safe_header(value: str, field_name: str) -> str:
    value = value.strip()
    if not value or "\r" in value or "\n" in value:
        raise ValueError(f"Invalid {field_name} header.")
    return value


def _sender_header(configured_sender: str) -> str:
    display_name, address = parseaddr(configured_sender)
    address = _safe_header(address, "sender")
    return formataddr((display_name, address)) if display_name else address


def send_email(
    recipient: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    *,
    smtp_factory: Callable[..., smtplib.SMTP] | None = None,
) -> EmailDelivery:
    """Send one transactional email.

    Tests and local development without SMTP use an in-memory outbox on the Flask
    application. Production fails closed when no mail host is configured.
    """

    recipient = _safe_header(recipient, "recipient")
    subject = _safe_header(subject, "subject")
    config = current_app.config
    host = str(config.get("MAIL_HOST", "")).strip()

    message = EmailMessage()
    message["To"] = recipient
    message["From"] = _sender_header(
        str(config.get("MAIL_FROM", "Job Matcher <jobmatchersupport@gmail.com>"))
    )
    message["Subject"] = subject
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    if not host:
        if not (config.get("TESTING") or config.get("DEBUG")):
            raise EmailDeliveryError("Email delivery is not configured.")
        outbox = current_app.extensions.setdefault("job_matcher_mail_outbox", [])
        outbox.append(message)
        return EmailDelivery(recipient=recipient, subject=subject, backend="memory")

    factory = smtp_factory or smtplib.SMTP
    smtp: smtplib.SMTP | None = None
    try:
        smtp = factory(host, int(config.get("MAIL_PORT", 587)), timeout=10)
        smtp.ehlo()
        if config.get("MAIL_USE_TLS", True):
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        username = str(config.get("MAIL_USERNAME", ""))
        password = str(config.get("MAIL_PASSWORD", ""))
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError("The verification email could not be sent.") from exc
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except (OSError, smtplib.SMTPException):
                pass

    return EmailDelivery(recipient=recipient, subject=subject, backend="smtp")


def send_otp_email(user: User | str, code: str, ttl_seconds: int | None = None) -> bool:
    recipient = user.email if hasattr(user, "email") else str(user)
    if ttl_seconds is None:
        ttl_seconds = int(current_app.config.get("OTP_TTL_SECONDS", 300))
    minutes = max(1, (ttl_seconds + 59) // 60)
    subject = "Verify your Job Matcher email"
    text = (
        "Welcome to Job Matcher.\n\n"
        f"Your verification code is: {code}\n\n"
        f"It expires in {minutes} minutes. If you did not request this code, "
        "you can ignore this email. Never share this code with anyone."
    )
    html = (
        "<h2>Verify your email</h2>"
        "<p>Use this code to finish creating your Job Matcher account:</p>"
        f"<p style=\"font-size:28px;font-weight:700;letter-spacing:6px\">{code}</p>"
        f"<p>It expires in {minutes} minutes. Never share this code with anyone.</p>"
    )
    send_email(recipient, subject, text, html)
    return True


def send_password_reset_email(user: User, reset_url: str, ttl_seconds: int = 3600) -> bool:
    """Send an enumeration-safe password recovery link."""

    minutes = max(1, (ttl_seconds + 59) // 60)
    subject = "Reset your Job Matcher password"
    text = (
        f"Hello {user.full_name},\n\n"
        "Someone requested a password reset for your Job Matcher account.\n\n"
        f"Reset your password: {reset_url}\n\n"
        f"This link expires in {minutes} minutes and stops working after your password changes. "
        "If you did not request this, you can ignore this email."
    )
    html_body = (
        f"<h2>Reset your password</h2><p>Hello {html.escape(user.full_name)},</p>"
        "<p>Use the secure link below to choose a new Job Matcher password.</p>"
        f'<p><a href="{html.escape(reset_url, quote=True)}">Reset my password</a></p>'
        f"<p>This link expires in {minutes} minutes and stops working after your password changes. "
        "If you did not request it, no action is needed.</p>"
    )
    send_email(user.email, subject, text, html_body)
    return True
