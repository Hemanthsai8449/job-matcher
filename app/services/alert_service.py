from __future__ import annotations

import html
import ipaddress
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, time, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app
from sqlalchemy import case
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import Application, AuditLog, Job, JobMatch, SentJob, User
from ..utils import utcnow
from .matching_service import rebuild_matches
from .telegram_service import (
    TelegramServiceError,
    TelegramUpdateClaimLost,
    renew_update_claim,
    send_message,
)

SUCCESSFUL_DELIVERY_STATUSES = {"sent", "delivered"}
QUEUE_LEASE_MINUTES = 15


Sender = Callable[..., Mapping[str, Any]]


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


def _local_day_bounds(user: User, now: datetime) -> tuple[datetime, datetime]:
    timezone = _user_timezone(user)
    local_now = now.astimezone(timezone)
    local_midnight = datetime.combine(local_now.date(), time.min, tzinfo=timezone)
    day_start = local_midnight.astimezone(UTC)
    day_end = (local_midnight + timedelta(days=1)).astimezone(UTC)
    return day_start, day_end


def _daily_limit(user: User, requested: int | None, *, now: datetime) -> int:
    configured = max(0, min(20, int(user.daily_job_limit or 0)))
    day_start, day_end = _local_day_bounds(user, now)
    delivered_today = db.session.scalar(
        db.select(db.func.count(SentJob.id)).where(
            SentJob.user_id == user.id,
            SentJob.status.in_(SUCCESSFUL_DELIVERY_STATUSES),
            SentJob.sent_at >= day_start,
            SentJob.sent_at < day_end,
        )
    ) or 0
    remaining = max(0, configured - int(delivered_today))
    if requested is None:
        return remaining
    return max(0, min(remaining, int(requested)))


def select_alert_jobs(
    user: User,
    *,
    limit: int | None = None,
    now: datetime | None = None,
    refresh_matches: bool = True,
) -> list[JobMatch]:
    """Return deterministic, eligible matches not already delivered to this user."""

    current_time = _aware(now or utcnow())
    assert current_time is not None
    result_limit = _daily_limit(user, limit, now=current_time)
    if result_limit == 0 or user.id is None:
        return []
    if refresh_matches:
        rebuild_matches(user, now=current_time, commit=False)

    already_claimed = (
        db.select(SentJob.id)
        .where(
            SentJob.user_id == user.id,
            SentJob.job_id == Job.id,
        )
        .exists()
    )
    not_interested = (
        db.select(Application.id)
        .where(
            Application.user_id == user.id,
            Application.job_id == Job.id,
            Application.status == "not_interested",
        )
        .exists()
    )
    statement = (
        db.select(JobMatch)
        .join(Job, Job.id == JobMatch.job_id)
        .where(
            JobMatch.user_id == user.id,
            JobMatch.score > 0,
            JobMatch.score >= max(0, min(100, int(user.minimum_match_score or 0))),
            Job.is_active.is_(True),
            Job.is_demo.is_(False),
            Job.trust_score >= 40,
            Job.reported_count < 3,
            db.or_(Job.expires_at.is_(None), Job.expires_at > current_time),
            ~already_claimed,
            ~not_interested,
        )
        .order_by(
            JobMatch.score.desc(),
            case((Job.posted_at.is_(None), 1), else_=0),
            Job.posted_at.desc(),
            Job.id.asc(),
        )
        .limit(result_limit)
    )
    return list(db.session.scalars(statement).all())


def _safe_line(value: Any, maximum: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]
    return html.escape(text, quote=False)


def _job_message(job: Job, match: JobMatch) -> str:
    heading = f"<b>{int(match.score)}% match · {_safe_line(job.title, 220)}</b>"
    company = _safe_line(job.company, 180)
    location = _safe_line(job.location, 180)
    mode = _safe_line(job.workplace_type.replace("_", " ").title(), 30)
    kind = _safe_line(job.job_type.replace("_", " ").title(), 40)
    lines = ["MATCH · " + heading, f"{company} · {location}", f"{mode} · {kind}"]
    if job.is_demo:
        lines.append("<b>DEMO ONLY — this is not a real vacancy.</b>")
    reasons = [str(reason) for reason in (match.reasons or []) if str(reason).strip()]
    if reasons:
        lines.append("\n<b>Why it matches</b>")
        lines.extend(f"• {_safe_line(reason, 220)}" for reason in reasons[:3])
    matched = [str(skill) for skill in (match.matched_skills or []) if str(skill).strip()]
    missing = [str(skill) for skill in (match.missing_skills or []) if str(skill).strip()]
    if matched:
        lines.append(f"\n<b>Matched skills:</b> {_safe_line(', '.join(matched[:6]), 300)}")
    if missing:
        lines.append(f"<b>Skills to strengthen:</b> {_safe_line(', '.join(missing[:5]), 260)}")
    if job.salary:
        lines.append(f"<b>Compensation:</b> {_safe_line(job.salary, 120)}")
    lines.append(f"\nSource: {_safe_line(job.source_label, 80)}")
    lines.append(
        "Review the full details before applying. Job Matcher never charges an application fee."
    )
    return "\n".join(lines)[:4096]


def _job_markup(job: Job) -> dict[str, Any]:
    base_url = str(current_app.config.get("APP_BASE_URL", "http://127.0.0.1:5000")).rstrip("/")
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise TelegramServiceError("The public Job Matcher URL is not configured.")
    if not current_app.config.get("TESTING"):
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            address = None
        if (
            parts.scheme != "https"
            or parts.hostname.casefold() == "localhost"
            or "." not in parts.hostname
            or parts.hostname.casefold().endswith((".local", ".internal"))
            or (address is not None and not address.is_global)
        ):
            raise TelegramServiceError("A public HTTPS Job Matcher URL is required.")
    detail_url = urljoin(f"{base_url}/", f"jobs/{job.id}")
    return {
        "inline_keyboard": [
            [{"text": "Review & apply", "url": detail_url}],
            [
                {"text": "Save", "callback_data": f"job:save:{job.id}"},
                {
                    "text": "Not interested",
                    "callback_data": f"job:not_interested:{job.id}",
                },
            ],
        ]
    }


def _extract_message_id(response: Any) -> str | None:
    if not isinstance(response, Mapping):
        return None
    result = response.get("result")
    if isinstance(result, Mapping) and result.get("message_id") is not None:
        return str(result["message_id"])[:120]
    return None


def _claim_delivery(
    user: User,
    job: Job,
    *,
    now: datetime,
    commit: bool,
) -> SentJob | None:
    """Claim a user/job pair under its database unique constraint before I/O."""

    lease_cutoff = now - timedelta(minutes=QUEUE_LEASE_MINUTES)
    delivery: SentJob | None = None
    skipped = False
    try:
        with db.session.begin_nested():
            # Serialize quota claims per user. The no-op UPDATE supplies a write lock
            # on SQLite, where SELECT FOR UPDATE is ignored.
            db.session.execute(
                db.update(User)
                .where(User.id == user.id)
                .values(updated_at=User.updated_at)
            )
            day_start, day_end = _local_day_bounds(user, now)
            quota_used = db.session.scalar(
                db.select(db.func.count(SentJob.id)).where(
                    SentJob.user_id == user.id,
                    db.or_(
                        db.and_(
                            SentJob.status.in_(SUCCESSFUL_DELIVERY_STATUSES),
                            SentJob.sent_at >= day_start,
                            SentJob.sent_at < day_end,
                        ),
                        db.and_(
                            SentJob.status == "queued",
                            SentJob.updated_at >= max(day_start, lease_cutoff),
                            SentJob.updated_at < day_end,
                        ),
                    ),
                )
            ) or 0
            if int(quota_used) >= max(0, min(20, int(user.daily_job_limit or 0))):
                skipped = True
            if not skipped:
                delivery = db.session.scalar(
                    db.select(SentJob)
                    .where(SentJob.user_id == user.id, SentJob.job_id == job.id)
                    .with_for_update()
                )
                if delivery is not None:
                    # A row is a permanent student/job send reservation. Even an
                    # uncertain network failure must not risk repeating the link.
                    skipped = True
                else:
                    delivery = SentJob(
                        user_id=user.id,
                        job_id=job.id,
                        channel="telegram",
                        status="queued",
                    )
                    db.session.add(delivery)
            if not skipped:
                db.session.flush()
        _persist(commit=commit)
        return None if skipped else delivery
    except IntegrityError:
        # The savepoint contains the failed insert; do not roll back the caller's UoW.
        return None


def _safe_failure(exc: Exception) -> str:
    if isinstance(exc, TelegramServiceError):
        message = str(exc)
    else:
        message = f"{exc.__class__.__name__}: delivery failed"
    return re.sub(r"https://api\.telegram\.org/bot[^/\s]+", "Telegram API", message)[:240]


def dispatch_user_alerts(
    user: User,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    sender: Sender | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Send one user's due digest, recording every success or failure."""

    if not commit:
        raise ValueError("Telegram delivery requires durable database reservations.")
    current_time = _aware(now or utcnow())
    assert current_time is not None
    summary: dict[str, Any] = {
        "user_id": user.id,
        "selected": 0,
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "reminders": 0,
        "reason": "ready",
    }
    active_until = _aware(user.active_until)
    if user.deleted_at is not None:
        summary["reason"] = "account_deleted"
        return summary
    if (
        not user.telegram_chat_id
        or user.phone_verified_at is None
        or user.email_verified_at is None
    ):
        summary["reason"] = "telegram_not_connected"
        return summary
    if user.alert_status != "active" or active_until is None or active_until <= current_time:
        summary["reason"] = "alerts_inactive"
        return summary

    matches = select_alert_jobs(user, limit=limit, now=current_time)
    summary["selected"] = len(matches)
    deliver = sender or send_message
    for match in matches:
        job = match.job
        if job is None:
            summary["skipped"] += 1
            continue
        # In webhook-driven /jobs requests, stop a stale worker before it can
        # reserve another permanent student/job delivery row. Scheduler calls
        # have no active webhook context, so this is a no-op for daily alerts.
        renew_update_claim(commit=True)
        delivery = _claim_delivery(user, job, now=current_time, commit=commit)
        if delivery is None:
            summary["skipped"] += 1
            continue
        try:
            response = deliver(
                user.telegram_chat_id,
                _job_message(job, match),
                parse_mode="HTML",
                reply_markup=_job_markup(job),
                disable_web_page_preview=True,
            )
            delivery.status = "sent"
            delivery.sent_at = current_time
            delivery.provider_message_id = _extract_message_id(response)
            delivery.failure_reason = ""
            summary["sent"] += 1
        except TelegramUpdateClaimLost:
            # Claim fencing runs before Telegram I/O, so this reservation is
            # known to be unsent and can safely be released for the live worker.
            db.session.delete(delivery)
            db.session.commit()
            raise
        except Exception as exc:
            delivery.status = "failed"
            delivery.failure_reason = _safe_failure(exc)
            summary["failed"] += 1
        _persist(commit=commit)

    remaining = active_until - current_time
    if remaining <= timedelta(hours=24) and user.renewal_reminder_sent_at is None:
        try:
            deliver(
                user.telegram_chat_id,
                "<b>Your Job Matcher alert period ends soon.</b>\n"
                "Renew from Settings to keep receiving matched jobs. Pausing or expiry never "
                "removes your saved jobs.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            user.renewal_reminder_sent_at = current_time
            summary["reminders"] = 1
        except TelegramUpdateClaimLost:
            raise
        except Exception:
            pass

    db.session.add(
        AuditLog(
            actor_user_id=user.id,
            event="alerts.dispatch_completed",
            entity_type="user",
            entity_id=str(user.id),
            metadata_json={
                "sent": summary["sent"],
                "failed": summary["failed"],
                "skipped": summary["skipped"],
            },
            created_at=current_time,
        )
    )
    _persist(commit=commit)
    summary["reason"] = "completed"
    return summary


def _user_timezone(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(str(user.timezone or "Asia/Kolkata"))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _preferred_time(user: User) -> time:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(user.preferred_time or ""))
    if not match:
        return time(9, 0)
    return time(int(match.group(1)), int(match.group(2)))


def _is_due(user: User, now: datetime) -> bool:
    timezone = _user_timezone(user)
    local_now = now.astimezone(timezone)
    if local_now.time().replace(tzinfo=None) < _preferred_time(user):
        return False
    latest_dispatch = db.session.scalar(
        db.select(AuditLog.created_at)
        .where(
            AuditLog.actor_user_id == user.id,
            AuditLog.event == "alerts.dispatch_completed",
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    )
    if latest_dispatch is None:
        return True
    dispatched_at = _aware(latest_dispatch)
    return dispatched_at is None or dispatched_at.astimezone(timezone).date() < local_now.date()


def dispatch_due_alerts(
    *,
    now: datetime | None = None,
    sender: Sender | None = None,
    commit: bool = True,
) -> dict[str, int]:
    """Dispatch all locally due users. Intended for one recurring external scheduler."""

    if not commit:
        raise ValueError("Scheduled Telegram delivery requires durable database commits.")
    current_time = _aware(now or utcnow())
    assert current_time is not None
    summary = {
        "users_checked": 0,
        "users_due": 0,
        "users_processed": 0,
        "jobs_sent": 0,
        "jobs_failed": 0,
        "jobs_skipped": 0,
        "reminders_sent": 0,
        "expired": 0,
        "user_errors": 0,
    }
    users = db.session.scalars(
        db.select(User).where(
            User.deleted_at.is_(None),
            User.alert_status == "active",
            User.telegram_chat_id.is_not(None),
            User.phone_verified_at.is_not(None),
            User.email_verified_at.is_not(None),
        )
    ).all()
    summary["users_checked"] = len(users)
    for user in users:
        active_until = _aware(user.active_until)
        if active_until is None or active_until <= current_time:
            user.alert_status = "expired"
            summary["expired"] += 1
            if commit:
                db.session.commit()
            continue
        if not _is_due(user, current_time):
            continue
        summary["users_due"] += 1
        try:
            result = dispatch_user_alerts(
                user,
                now=current_time,
                sender=sender,
                commit=commit,
            )
        except Exception:
            summary["user_errors"] += 1
            if not commit:
                raise
            db.session.rollback()
            try:
                db.session.add(
                    AuditLog(
                        actor_user_id=user.id,
                        event="alerts.dispatch_failed",
                        entity_type="user",
                        entity_id=str(user.id),
                        created_at=current_time,
                    )
                )
                db.session.commit()
            except Exception:
                db.session.rollback()
            continue
        summary["users_processed"] += result["reason"] == "completed"
        summary["jobs_sent"] += int(result["sent"])
        summary["jobs_failed"] += int(result["failed"])
        summary["jobs_skipped"] += int(result["skipped"])
        summary["reminders_sent"] += int(result["reminders"])
    _persist(commit=commit)
    return summary


__all__ = [
    "dispatch_due_alerts",
    "dispatch_user_alerts",
    "select_alert_jobs",
]
