from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

import requests
from flask import current_app
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import AuditLog, TelegramLinkToken, TelegramUpdate, User
from ..utils import generate_token, hash_token, normalize_phone, utcnow

BOT_USERNAME = re.compile(r"^[A-Za-z0-9_]{5,32}$")
START_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
TELEGRAM_API_RESPONSE_LIMIT = 1 * 1024 * 1024
UPDATE_LEASE_MINUTES = 5
_UPDATE_CLAIM: ContextVar[tuple[str, int] | None] = ContextVar(
    "job_matcher_telegram_update_claim",
    default=None,
)


class HTTPClient(Protocol):
    def post(self, url: str, **kwargs: Any) -> Any: ...


class TelegramServiceError(RuntimeError):
    """Telegram delivery failed without exposing the bot token."""


class TelegramLinkError(ValueError):
    """A single-use Telegram account link is invalid or cannot be claimed."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class TelegramUpdateClaimLost(TelegramServiceError):
    """The webhook worker no longer owns the update it was processing."""


@dataclass(frozen=True)
class ContactBindingResult:
    success: bool
    reason: str
    message: str
    user: User | None = None

    def __bool__(self) -> bool:
        return self.success


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _persist(*, commit: bool) -> None:
    if commit:
        db.session.commit()
    else:
        db.session.flush()


def _telegram_identifier(value: Any, *, allow_negative: bool = False) -> str:
    raw = str(value or "").strip()
    pattern = r"-?[0-9]{1,31}" if allow_negative else r"[0-9]{1,31}"
    if not re.fullmatch(pattern, raw):
        raise ValueError("Invalid Telegram identifier.")
    return raw


def create_link_token(
    user: User,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> tuple[str, TelegramLinkToken]:
    """Return plaintext once plus its hashed record, invalidating older live links."""

    if user.id is None:
        raise ValueError("Persist the user before creating a Telegram link.")
    current_time = _aware(now or utcnow())
    assert current_time is not None
    ttl = max(1, int(current_app.config.get("TELEGRAM_LINK_TTL_SECONDS", 900)))
    raw_token = generate_token(24)
    if not START_TOKEN.fullmatch(raw_token):
        raise TelegramServiceError("A secure Telegram link could not be created.")

    try:
        with db.session.begin_nested():
            # A set-based UPDATE acquires SQLite's write lock before insertion. If two
            # requests race, the later UPDATE also invalidates the earlier insertion.
            db.session.execute(
                db.update(TelegramLinkToken)
                .where(
                    TelegramLinkToken.user_id == user.id,
                    TelegramLinkToken.consumed_at.is_(None),
                )
                .values(consumed_at=current_time)
            )
            record = TelegramLinkToken(
                user_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=current_time + timedelta(seconds=ttl),
            )
            db.session.add(record)
            db.session.flush()
        _persist(commit=commit)
    except Exception:
        if commit:
            db.session.rollback()
        raise
    return raw_token, record


def build_deep_link(raw_token: str, *, bot_username: str | None = None) -> str:
    """Build a Telegram start link containing only an opaque, short-lived token."""

    token = str(raw_token).strip()
    if not START_TOKEN.fullmatch(token):
        raise ValueError("Invalid Telegram link token.")
    username = str(
        bot_username
        if bot_username is not None
        else current_app.config.get("TELEGRAM_BOT_USERNAME", "")
    ).strip()
    username = username.removeprefix("@")
    if not BOT_USERNAME.fullmatch(username):
        raise TelegramServiceError("The Telegram bot username is not configured.")
    return f"https://t.me/{username}?{urlencode({'start': token})}"


def parse_start_payload(text: Any) -> str | None:
    """Extract a valid opaque token from an exact ``/start TOKEN`` command."""

    match = re.fullmatch(
        r"/start(?:@[A-Za-z0-9_]{5,32})?\s+([A-Za-z0-9_-]{16,64})",
        str(text or "").strip(),
    )
    return match.group(1) if match else None


def resolve_link_token(
    raw_token: str,
    *,
    now: datetime | None = None,
    include_expired: bool = False,
) -> TelegramLinkToken | None:
    token = str(raw_token).strip()
    if not START_TOKEN.fullmatch(token):
        return None
    record = db.session.scalar(
        db.select(TelegramLinkToken).where(
            TelegramLinkToken.token_hash == hash_token(token),
            TelegramLinkToken.consumed_at.is_(None),
        )
    )
    if record is None:
        return None
    expires_at = _aware(record.expires_at)
    current_time = _aware(now or utcnow())
    if not include_expired and (expires_at is None or expires_at <= current_time):
        return None
    return record


def prepare_contact_link(
    raw_token: str,
    chat_id: Any,
    telegram_user_id: Any,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> TelegramLinkToken:
    """Atomically reserve a start token for one private chat before asking for contact."""

    token = str(raw_token).strip()
    if not START_TOKEN.fullmatch(token):
        raise TelegramLinkError("invalid", "This connection link is invalid or already used.")
    chat = _telegram_identifier(chat_id)
    telegram_user = _telegram_identifier(telegram_user_id)
    if not hmac.compare_digest(chat, telegram_user):
        raise TelegramLinkError(
            "private_chat_required",
            "Open this connection link in a private chat with the Job Matcher bot.",
        )
    current_time = _aware(now or utcnow())
    assert current_time is not None

    link_error: TelegramLinkError | None = None
    record: TelegramLinkToken | None = None
    try:
        with db.session.begin_nested():
            record = db.session.scalar(
                db.select(TelegramLinkToken)
                .where(
                    TelegramLinkToken.token_hash == hash_token(token),
                    TelegramLinkToken.consumed_at.is_(None),
                )
                .with_for_update()
            )
            if record is None:
                raise TelegramLinkError(
                    "invalid",
                    "This connection link is invalid or already used.",
                )
            expires_at = _aware(record.expires_at)
            if expires_at is None or expires_at <= current_time:
                db.session.execute(
                    db.update(TelegramLinkToken)
                    .where(
                        TelegramLinkToken.id == record.id,
                        TelegramLinkToken.consumed_at.is_(None),
                    )
                    .values(consumed_at=current_time)
                )
                link_error = TelegramLinkError("expired", "This connection link has expired.")
            elif record.pending_telegram_user_id and (
                record.pending_telegram_user_id != telegram_user
                or record.pending_chat_id != chat
            ):
                link_error = TelegramLinkError(
                    "claimed",
                    "This connection link is already being used in another chat.",
                )
            else:
                claimed = db.session.execute(
                    db.update(TelegramLinkToken)
                    .where(
                        TelegramLinkToken.id == record.id,
                        TelegramLinkToken.consumed_at.is_(None),
                        db.or_(
                            TelegramLinkToken.pending_telegram_user_id.is_(None),
                            db.and_(
                                TelegramLinkToken.pending_telegram_user_id == telegram_user,
                                TelegramLinkToken.pending_chat_id == chat,
                            ),
                        ),
                    )
                    .values(
                        pending_chat_id=chat,
                        pending_telegram_user_id=telegram_user,
                    )
                )
                if claimed.rowcount != 1:
                    link_error = TelegramLinkError(
                        "claimed",
                        "This connection link is already being used in another chat.",
                    )
                db.session.flush()
        _persist(commit=commit)
        if link_error is not None:
            raise link_error
        assert record is not None
        db.session.refresh(record)
        return record
    except TelegramLinkError:
        raise
    except Exception:
        if commit:
            db.session.rollback()
        raise


def contact_request_markup() -> dict[str, Any]:
    """Reply keyboard that asks Telegram to share only the current user's contact."""

    return {
        "keyboard": [[{"text": "Share my phone number", "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
        "input_field_placeholder": "Share the number registered on Job Matcher",
    }


def remove_keyboard_markup() -> dict[str, bool]:
    return {"remove_keyboard": True}


def _normalize_telegram_phone(value: Any) -> str:
    raw = re.sub(r"[\s()-]", "", str(value or "").strip())
    if raw and not raw.startswith("+") and raw.isdigit() and len(raw) > 10:
        raw = f"+{raw}"
    return normalize_phone(raw)


def _binding_failure(
    record: TelegramLinkToken,
    reason: str,
    message: str,
    *,
    now: datetime,
) -> ContactBindingResult:
    record.consumed_at = now
    db.session.add(
        AuditLog(
            actor_user_id=record.user_id,
            event=f"telegram.connection_{reason}",
            entity_type="user",
            entity_id=str(record.user_id),
            created_at=now,
        )
    )
    return ContactBindingResult(False, reason, message)


def bind_contact(
    chat_id: Any,
    telegram_user_id: Any,
    contact: Mapping[str, Any],
    *,
    username: str | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> ContactBindingResult:
    """Verify a Telegram-owned contact and bind it to the reserved registered user."""

    chat = _telegram_identifier(chat_id)
    telegram_user = _telegram_identifier(telegram_user_id)
    current_time = _aware(now or utcnow())
    assert current_time is not None
    if not hmac.compare_digest(chat, telegram_user):
        return ContactBindingResult(
            False,
            "private_chat_required",
            "Connect Job Matcher from a private Telegram chat.",
        )
    if not isinstance(contact, Mapping):
        return ContactBindingResult(False, "invalid_contact", "Share your own contact to continue.")
    try:
        contact_owner = _telegram_identifier(contact.get("user_id"))
    except ValueError:
        return ContactBindingResult(False, "invalid_contact", "Share your own contact to continue.")
    if not hmac.compare_digest(contact_owner, telegram_user):
        return ContactBindingResult(
            False,
            "contact_owner_mismatch",
            "Use Telegram's Share my phone number button to share your own contact.",
        )

    try:
        shared_phone = _normalize_telegram_phone(contact.get("phone_number"))
    except ValueError:
        return ContactBindingResult(
            False,
            "invalid_phone",
            "Telegram did not provide a valid number.",
        )

    result: ContactBindingResult | None = None
    try:
        with db.session.begin_nested():
            record = db.session.scalar(
                db.select(TelegramLinkToken)
                .where(
                    TelegramLinkToken.pending_chat_id == chat,
                    TelegramLinkToken.pending_telegram_user_id == telegram_user,
                    TelegramLinkToken.consumed_at.is_(None),
                )
                .order_by(TelegramLinkToken.created_at.desc(), TelegramLinkToken.id.desc())
                .with_for_update()
            )
            if record is None:
                result = ContactBindingResult(
                    False,
                    "no_pending_link",
                    "Open a fresh connection link from Job Matcher first.",
                )
            else:
                claimed = db.session.execute(
                    db.update(TelegramLinkToken)
                    .where(
                        TelegramLinkToken.id == record.id,
                        TelegramLinkToken.consumed_at.is_(None),
                    )
                    .values(consumed_at=current_time)
                )
                if claimed.rowcount != 1:
                    result = ContactBindingResult(
                        False,
                        "no_pending_link",
                        "Open a fresh connection link from Job Matcher first.",
                    )
            expires_at = _aware(record.expires_at) if record is not None else None
            if result is None and (expires_at is None or expires_at <= current_time):
                result = _binding_failure(
                    record,
                    "expired",
                    "This connection link has expired. Create a new one on Job Matcher.",
                    now=current_time,
                )

            user = None
            if result is None:
                user = db.session.scalar(
                    db.select(User).where(User.id == record.user_id).with_for_update()
                )
            if result is None and (user is None or user.deleted_at is not None):
                result = _binding_failure(
                    record,
                    "account_unavailable",
                    "The Job Matcher account is no longer available.",
                    now=current_time,
                )
            if result is None and user.email_verified_at is None:
                result = _binding_failure(
                    record,
                    "email_not_verified",
                    "Verify your Job Matcher email before connecting Telegram.",
                    now=current_time,
                )
            if result is None and not hmac.compare_digest(shared_phone, user.phone_e164):
                result = _binding_failure(
                    record,
                    "phone_mismatch",
                    "That number does not match the number registered on Job Matcher.",
                    now=current_time,
                )

            phone_owner = None
            telegram_owner = None
            if result is None:
                # Serialize claims for every account carrying this phone number,
                # including on databases where the verification rule is not a schema
                # constraint because unverified duplicate numbers are allowed.
                db.session.execute(
                    db.update(User)
                    .where(
                        User.phone_e164 == shared_phone,
                        User.deleted_at.is_(None),
                    )
                    .values(updated_at=current_time)
                )
                phone_owner = db.session.scalar(
                    db.select(User)
                    .where(
                        User.id != user.id,
                        User.phone_e164 == shared_phone,
                        User.phone_verified_at.is_not(None),
                        User.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                telegram_owner = db.session.scalar(
                    db.select(User)
                    .where(
                        User.id != user.id,
                        User.deleted_at.is_(None),
                        db.or_(
                            User.telegram_user_id == telegram_user,
                            User.telegram_chat_id == chat,
                        ),
                    )
                    .with_for_update()
                )
            if result is None and (phone_owner is not None or telegram_owner is not None):
                result = _binding_failure(
                    record,
                    "already_connected",
                    "This Telegram number is already connected to another account.",
                    now=current_time,
                )

            if result is None:
                user.telegram_user_id = telegram_user
                user.telegram_chat_id = chat
                clean_username = re.sub(r"[^A-Za-z0-9_]", "", str(username or ""))[:80]
                user.telegram_username = clean_username or None
                user.phone_verified_at = current_time
                user.alerts_consent_at = user.alerts_consent_at or current_time
                active_until = _aware(user.active_until)
                activation_start = (
                    active_until
                    if active_until is not None and active_until > current_time
                    else current_time
                )
                user.active_until = activation_start + timedelta(
                    days=max(1, int(current_app.config.get("ALERT_WINDOW_DAYS", 7)))
                )
                user.alert_status = "active"
                user.renewal_reminder_sent_at = None
                db.session.add(
                    AuditLog(
                        actor_user_id=user.id,
                        event="telegram.connected",
                        entity_type="user",
                        entity_id=str(user.id),
                        created_at=current_time,
                    )
                )
                result = ContactBindingResult(
                    True,
                    "connected",
                    "Telegram is connected and seven-day job alerts are active.",
                    user,
                )
            db.session.flush()
        _persist(commit=commit)
        assert result is not None
        return result
    except IntegrityError:
        if commit:
            db.session.rollback()
        return ContactBindingResult(
            False,
            "already_connected",
            "This Telegram account is already connected to another Job Matcher account.",
        )
    except Exception:
        if commit:
            db.session.rollback()
        raise


def verify_and_bind_contact(
    chat_id: Any,
    telegram_user_id: Any,
    contact: Mapping[str, Any],
    **kwargs: Any,
) -> tuple[bool, str, User | None]:
    """Tuple-returning compatibility wrapper for webhook route handlers."""

    result = bind_contact(chat_id, telegram_user_id, contact, **kwargs)
    return result.success, result.message, result.user


def verify_webhook_secret(provided_secret: str | bytes | None) -> bool:
    """Fail closed unless Telegram's secret-token header exactly matches configuration."""

    expected = str(current_app.config.get("TELEGRAM_WEBHOOK_SECRET", ""))
    if not expected or provided_secret is None:
        return False
    if isinstance(provided_secret, bytes):
        try:
            provided = provided_secret.decode("utf-8")
        except UnicodeDecodeError:
            return False
    else:
        provided = str(provided_secret)
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def claim_update(
    update_id: Any,
    *,
    now: datetime | None = None,
    commit: bool = False,
) -> int | None:
    """Return a fencing attempt, ``0`` while busy, or ``None`` when completed."""

    raw_id = str(update_id or "").strip()
    if not re.fullmatch(r"-?[0-9]{1,39}", raw_id):
        raise ValueError("Invalid Telegram update identifier.")
    processed_at = _aware(now or utcnow())
    assert processed_at is not None
    stale_before = processed_at - timedelta(minutes=UPDATE_LEASE_MINUTES)
    claim_attempt = 1
    try:
        with db.session.begin_nested():
            existing = db.session.scalar(
                db.select(TelegramUpdate).where(TelegramUpdate.update_id == raw_id)
            )
            if existing is None:
                db.session.add(
                    TelegramUpdate(
                        update_id=raw_id,
                        status="processing",
                        attempts=1,
                        processed_at=processed_at,
                    )
                )
            else:
                if existing.status == "completed":
                    return None
                last_attempt = _aware(existing.processed_at)
                if (
                    existing.status == "processing"
                    and last_attempt is not None
                    and last_attempt >= stale_before
                ):
                    return 0
                previous_attempt = int(existing.attempts or 0)
                claim_attempt = previous_attempt + 1
                claimed = db.session.execute(
                    db.update(TelegramUpdate)
                    .where(
                        TelegramUpdate.id == existing.id,
                        TelegramUpdate.attempts == previous_attempt,
                        db.or_(
                            TelegramUpdate.status == "failed",
                            db.and_(
                                TelegramUpdate.status == "processing",
                                TelegramUpdate.processed_at < stale_before,
                            ),
                        ),
                    )
                    .values(
                        status="processing",
                        attempts=TelegramUpdate.attempts + 1,
                        last_error="",
                        processed_at=processed_at,
                        completed_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    return 0
            db.session.flush()
        _persist(commit=commit)
        return claim_attempt
    except IntegrityError:
        return 0


def push_update_claim(update_id: Any, claim_attempt: int) -> Token:
    """Set the webhook claim used to fence downstream Telegram API calls."""

    raw_id = str(update_id or "").strip()
    if not re.fullmatch(r"-?[0-9]{1,39}", raw_id) or int(claim_attempt) < 1:
        raise ValueError("Invalid Telegram update claim.")
    return _UPDATE_CLAIM.set((raw_id, int(claim_attempt)))


def pop_update_claim(token: Token) -> None:
    _UPDATE_CLAIM.reset(token)


def renew_update_claim(*, now: datetime | None = None, commit: bool = False) -> None:
    """Heartbeat and lock the active webhook claim inside the current transaction."""

    claim = _UPDATE_CLAIM.get()
    if claim is None:
        return
    raw_id, claim_attempt = claim
    heartbeat_at = _aware(now or utcnow())
    assert heartbeat_at is not None
    renewed = db.session.execute(
        db.update(TelegramUpdate)
        .where(
            TelegramUpdate.update_id == raw_id,
            TelegramUpdate.status == "processing",
            TelegramUpdate.attempts == claim_attempt,
        )
        .values(processed_at=heartbeat_at)
    )
    db.session.flush()
    if renewed.rowcount != 1:
        raise TelegramUpdateClaimLost("This webhook update is owned by another worker.")
    if commit:
        db.session.commit()


def finish_update(
    update_id: Any,
    *,
    claim_attempt: int,
    success: bool,
    error: str = "",
    now: datetime | None = None,
    commit: bool = True,
) -> bool:
    """Finish only the still-current claim, fencing out stale webhook workers."""

    raw_id = str(update_id or "").strip()
    if not re.fullmatch(r"-?[0-9]{1,39}", raw_id):
        raise ValueError("Invalid Telegram update identifier.")
    finished_at = _aware(now or utcnow())
    assert finished_at is not None
    safe_error = re.sub(r"\s+", " ", str(error or "")).strip()[:160]
    finished = db.session.execute(
        db.update(TelegramUpdate)
        .where(
            TelegramUpdate.update_id == raw_id,
            TelegramUpdate.status == "processing",
            TelegramUpdate.attempts == int(claim_attempt),
        )
        .values(
            status="completed" if success else "failed",
            last_error="" if success else safe_error,
            completed_at=finished_at if success else None,
        )
    )
    _persist(commit=commit)
    return finished.rowcount == 1


def is_duplicate_update(update_id: Any) -> bool:
    raw_id = str(update_id or "").strip()
    if not raw_id:
        return False
    status = db.session.scalar(
        db.select(TelegramUpdate.status).where(TelegramUpdate.update_id == raw_id)
    )
    return status in {"processing", "completed"}


def _memory_api_result(method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    outbox = current_app.extensions.setdefault("job_matcher_telegram_outbox", [])
    message_id = len(outbox) + 1
    outbox.append({"method": method, "payload": dict(payload)})
    result: dict[str, Any] = {"ok": True, "_backend": "memory", "result": True}
    if method == "sendMessage":
        result["result"] = {
            "message_id": message_id,
            "chat": {"id": payload.get("chat_id")},
        }
    return result


def _api_call(
    method: str,
    payload: Mapping[str, Any],
    *,
    client: HTTPClient | None = None,
) -> dict[str, Any]:
    # When called from a webhook, this heartbeat also holds the claim row lock
    # until the surrounding handler commits or finishes the update.
    renew_update_claim(commit=True)
    token = str(current_app.config.get("TELEGRAM_BOT_TOKEN", "")).strip()
    if not token:
        if current_app.config.get("TESTING") or current_app.config.get("DEBUG"):
            return _memory_api_result(method, payload)
        raise TelegramServiceError("Telegram delivery is not configured.")
    http = client or requests
    connect = max(0.25, float(current_app.config.get("TELEGRAM_CONNECT_TIMEOUT", 3.05)))
    read = max(0.5, float(current_app.config.get("TELEGRAM_READ_TIMEOUT", 10.0)))
    response: Any | None = None
    try:
        response = http.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=dict(payload),
            timeout=(connect, read),
            stream=True,
        )
        response.raise_for_status()
        headers = getattr(response, "headers", {})
        content_length = str(headers.get("Content-Length", "")) if headers else ""
        if content_length.isdigit() and int(content_length) > TELEGRAM_API_RESPONSE_LIMIT:
            raise TelegramServiceError("Telegram returned an oversized response.")
        if isinstance(response, requests.Response):
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=16_384):
                if not chunk:
                    continue
                size += len(chunk)
                if size > TELEGRAM_API_RESPONSE_LIMIT:
                    raise TelegramServiceError("Telegram returned an oversized response.")
                chunks.append(chunk)
            data = json.loads(b"".join(chunks).decode("utf-8"))
        else:
            content = getattr(response, "content", b"")
            if content and len(content) > TELEGRAM_API_RESPONSE_LIMIT:
                raise TelegramServiceError("Telegram returned an oversized response.")
            data = response.json()
    except TelegramServiceError:
        raise
    except (requests.RequestException, AttributeError, ValueError, TypeError) as exc:
        raise TelegramServiceError("Telegram did not accept the request.") from exc
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if not isinstance(data, Mapping) or data.get("ok") is not True:
        raise TelegramServiceError("Telegram reported that the request failed.")
    return dict(data)


def send_message(
    chat_id: Any,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    reply_markup: Mapping[str, Any] | None = None,
    disable_web_page_preview: bool = True,
    client: HTTPClient | None = None,
) -> dict[str, Any]:
    """Send one bounded Telegram message through the official Bot API."""

    chat = _telegram_identifier(chat_id, allow_negative=True)
    message = str(text)
    if not message or len(message) > 4096:
        raise ValueError("Telegram messages must contain between 1 and 4096 characters.")
    payload: dict[str, Any] = {
        "chat_id": chat,
        "text": message,
        "disable_web_page_preview": bool(disable_web_page_preview),
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = dict(reply_markup)
    return _api_call("sendMessage", payload, client=client)


def answer_callback(
    callback_query_id: Any,
    text: str = "",
    *,
    show_alert: bool = False,
    client: HTTPClient | None = None,
) -> dict[str, Any]:
    callback_id = str(callback_query_id or "").strip()
    if not callback_id or len(callback_id) > 160:
        raise ValueError("Invalid Telegram callback identifier.")
    payload: dict[str, Any] = {
        "callback_query_id": callback_id,
        "show_alert": bool(show_alert),
    }
    if text:
        payload["text"] = str(text)[:200]
    return _api_call("answerCallbackQuery", payload, client=client)


# Clear compatibility names for route code and tests.
verify_webhook = verify_webhook_secret
contact_keyboard = contact_request_markup
process_contact = bind_contact


__all__ = [
    "ContactBindingResult",
    "TelegramLinkError",
    "TelegramServiceError",
    "TelegramUpdateClaimLost",
    "answer_callback",
    "bind_contact",
    "build_deep_link",
    "claim_update",
    "contact_request_markup",
    "create_link_token",
    "finish_update",
    "is_duplicate_update",
    "parse_start_payload",
    "pop_update_claim",
    "prepare_contact_link",
    "push_update_claim",
    "remove_keyboard_markup",
    "renew_update_claim",
    "resolve_link_token",
    "send_message",
    "verify_and_bind_contact",
    "verify_webhook_secret",
]
