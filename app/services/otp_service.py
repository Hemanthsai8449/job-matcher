from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from flask import current_app
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import OTPCode, User, as_utc
from ..utils import generate_numeric_code, utcnow
from .email_service import send_otp_email


class OTPError(RuntimeError):
    """Base error for OTP issuance."""


class OTPResendTooSoon(OTPError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Please wait {retry_after} seconds before requesting another code.")
        self.retry_after = retry_after


@dataclass(frozen=True)
class OTPDispatchResult:
    otp_id: int
    expires_at: datetime
    delivery_succeeded: bool | None


@dataclass(frozen=True)
class OTPVerificationResult:
    success: bool
    reason: str
    remaining_attempts: int


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _persist(*, commit: bool) -> None:
    if commit:
        db.session.commit()
    else:
        db.session.flush()


def _digest(record: OTPCode, code: str) -> str:
    key = str(current_app.config.get("OTP_PEPPER") or current_app.config["SECRET_KEY"])
    message = f"{record.id}:{record.user_id}:{record.purpose}:{code}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def _latest(user: User, purpose: str) -> OTPCode | None:
    return (
        OTPCode.query.filter_by(user_id=user.id, purpose=purpose)
        .order_by(OTPCode.created_at.desc(), OTPCode.id.desc())
        .first()
    )


def resend_wait_seconds(
    user: User,
    purpose: str = "verify_email",
    *,
    now: datetime | None = None,
) -> int:
    latest = _latest(user, purpose)
    if latest is None:
        return 0
    current_time = now or utcnow()
    created_at = as_utc(latest.created_at) or current_time
    cooldown = int(current_app.config.get("OTP_RESEND_SECONDS", 60))
    elapsed = (_aware(current_time) - created_at).total_seconds()
    return max(0, math.ceil(cooldown - elapsed))


def can_resend_otp(
    user: User,
    purpose: str = "verify_email",
    *,
    now: datetime | None = None,
) -> tuple[bool, int]:
    wait = resend_wait_seconds(user, purpose, now=now)
    return wait == 0, wait


def _create_otp(
    user: User,
    purpose: str = "verify_email",
    *,
    now: datetime | None = None,
    enforce_cooldown: bool = False,
    code_factory: Callable[[], str] | None = None,
    commit: bool = True,
) -> tuple[OTPCode, str]:
    """Create a new single-use code and invalidate every older code of its purpose."""

    ttl = int(current_app.config.get("OTP_TTL_SECONDS", 300))
    code = str((code_factory or generate_numeric_code)()).strip()
    if len(code) != 6 or not code.isdigit():
        raise OTPError("The OTP generator returned an invalid code.")

    try:
        # Execute a write before opening the savepoint. This both establishes the
        # cross-database User -> OTPCode lock order and starts a real outer
        # transaction on sqlite3's legacy transaction mode.
        db.session.execute(
            db.update(User).where(User.id == user.id).values(updated_at=User.updated_at)
        )
        db.session.refresh(user, attribute_names=["email_verified_at"])
        current_time = _aware(now or utcnow())
        if purpose == "verify_email" and user.email_verified_at is not None:
            raise OTPError("This email address is already verified.")
        with db.session.begin_nested():
            if enforce_cooldown:
                wait = resend_wait_seconds(user, purpose, now=current_time)
                if wait:
                    raise OTPResendTooSoon(wait)
            db.session.execute(
                db.update(OTPCode)
                .where(
                    OTPCode.user_id == user.id,
                    OTPCode.purpose == purpose,
                    OTPCode.consumed_at.is_(None),
                )
                .values(consumed_at=current_time)
            )
            record = OTPCode(
                user_id=user.id,
                purpose=purpose,
                code_hash="pending",
                expires_at=current_time + timedelta(seconds=ttl),
            )
            db.session.add(record)
            db.session.flush()
            record.code_hash = _digest(record, code)
            db.session.flush()
        _persist(commit=commit)
    except OTPError:
        if commit:
            db.session.rollback()
        raise
    except Exception as exc:
        if commit:
            db.session.rollback()
        if isinstance(exc, IntegrityError):
            raise OTPError(
                "A verification code was just created. Please wait before retrying."
            ) from exc
        raise

    return record, code


def create_otp(
    user: User,
    purpose: str = "verify_email",
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> str:
    """Create and persist a code, returning its plaintext exactly once to the caller."""

    _, code = _create_otp(user, purpose, now=now, commit=commit)
    return code


def issue_otp(
    user: User,
    purpose: str = "verify_email",
    *,
    now: datetime | None = None,
    mailer: Callable[[User, str], bool] = send_otp_email,
) -> OTPDispatchResult:
    """Convenience operation for non-route callers that creates and emails a code."""

    record, code = _create_otp(user, purpose, now=now)
    try:
        delivered = bool(mailer(user, code))
    except Exception:
        # The persisted code remains usable if an SMTP server accepted the message but
        # the client lost the response. A resend remains cooldown protected.
        delivered = False
    return OTPDispatchResult(record.id, _aware(record.expires_at), delivered)


def resend_otp(
    user: User,
    purpose: str = "verify_email",
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> tuple[str | None, str]:
    """Return a fresh code or a safe cooldown message without raising in a route."""

    try:
        _, code = _create_otp(
            user,
            purpose,
            now=now,
            enforce_cooldown=True,
            commit=commit,
        )
    except OTPError as exc:
        return None, str(exc)
    return code, "A new verification code is ready to send."


def _verify_otp_result(
    user: User,
    code: str,
    purpose: str = "verify_email",
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> OTPVerificationResult:
    max_attempts = int(current_app.config.get("OTP_MAX_ATTEMPTS", 5))
    supplied_code = code.strip()
    try:
        # Issuance uses User -> OTPCode ordering. Verification takes the same
        # first lock so a resend and a successful verification cannot deadlock.
        db.session.execute(
            db.update(User).where(User.id == user.id).values(updated_at=User.updated_at)
        )
        db.session.refresh(user, attribute_names=["email_verified_at"])
        current_time = _aware(now or utcnow())
        for _attempt in range(5):
            record = _latest(user, purpose)
            if record is None or record.consumed_at is not None:
                _persist(commit=commit)
                return OTPVerificationResult(False, "invalid_or_used", 0)

            expires_at = as_utc(record.expires_at)
            if expires_at is None or expires_at <= current_time:
                db.session.execute(
                    db.update(OTPCode)
                    .where(OTPCode.id == record.id, OTPCode.consumed_at.is_(None))
                    .values(consumed_at=current_time)
                )
                _persist(commit=commit)
                return OTPVerificationResult(False, "expired", 0)

            attempts = int(record.attempts or 0)
            if attempts >= max_attempts:
                db.session.execute(
                    db.update(OTPCode)
                    .where(OTPCode.id == record.id, OTPCode.consumed_at.is_(None))
                    .values(consumed_at=current_time)
                )
                _persist(commit=commit)
                return OTPVerificationResult(False, "attempts_exhausted", 0)

            valid = hmac.compare_digest(_digest(record, supplied_code), record.code_hash)
            if valid:
                consumed = db.session.execute(
                    db.update(OTPCode)
                    .where(
                        OTPCode.id == record.id,
                        OTPCode.consumed_at.is_(None),
                        OTPCode.attempts == attempts,
                    )
                    .values(consumed_at=current_time)
                )
                if consumed.rowcount == 1:
                    if purpose == "verify_email" and user.email_verified_at is None:
                        user.email_verified_at = current_time
                    _persist(commit=commit)
                    return OTPVerificationResult(True, "verified", max_attempts - attempts)
            else:
                new_attempts = attempts + 1
                updated = db.session.execute(
                    db.update(OTPCode)
                    .where(
                        OTPCode.id == record.id,
                        OTPCode.consumed_at.is_(None),
                        OTPCode.attempts == attempts,
                    )
                    .values(
                        attempts=new_attempts,
                        consumed_at=current_time if new_attempts >= max_attempts else None,
                    )
                )
                if updated.rowcount == 1:
                    _persist(commit=commit)
                    remaining = max(0, max_attempts - new_attempts)
                    reason = "attempts_exhausted" if remaining == 0 else "invalid"
                    return OTPVerificationResult(False, reason, remaining)

            # Another request changed this record. Reload and retry the CAS instead
            # of allowing parallel requests to bypass consumption or attempt limits.
            db.session.expire_all()

        _persist(commit=commit)
        return OTPVerificationResult(False, "invalid_or_used", 0)
    except Exception:
        if commit:
            db.session.rollback()
        raise


def verify_otp(
    user: User,
    code: str,
    purpose: str = "verify_email",
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> tuple[bool, str]:
    result = _verify_otp_result(user, code, purpose, now=now, commit=commit)
    messages = {
        "verified": "Email verified successfully.",
        "invalid": "That code is incorrect.",
        "invalid_or_used": "That code is invalid or has already been used.",
        "expired": "That code has expired. Request a new one.",
        "attempts_exhausted": "Too many incorrect attempts. Request a new code.",
    }
    return result.success, messages[result.reason]


verify_code = verify_otp
