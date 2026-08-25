from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from flask_login import UserMixin
from sqlalchemy import UniqueConstraint
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .utils import utcnow


class TimestampMixin:
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    # An unverified number is intentionally not unique: otherwise an attacker could
    # reserve somebody else's number. Uniqueness is enforced when Telegram verifies it.
    phone_e164 = db.Column(db.String(24), nullable=False, index=True)
    password_hash = db.Column(db.String(255))
    google_sub = db.Column(db.String(255), unique=True)
    auth_version = db.Column(db.Integer, default=1, nullable=False)
    email_verified_at = db.Column(db.DateTime(timezone=True))
    phone_verified_at = db.Column(db.DateTime(timezone=True))
    telegram_user_id = db.Column(db.String(32), unique=True)
    telegram_chat_id = db.Column(db.String(32), unique=True)
    telegram_username = db.Column(db.String(80))
    alert_status = db.Column(db.String(20), default="inactive", nullable=False)
    active_until = db.Column(db.DateTime(timezone=True))
    renewal_reminder_sent_at = db.Column(db.DateTime(timezone=True))
    preferred_time = db.Column(db.String(5), default="09:00", nullable=False)
    timezone = db.Column(db.String(64), default="Asia/Kolkata", nullable=False)
    daily_job_limit = db.Column(db.Integer, default=5, nullable=False)
    headline = db.Column(db.String(160), default="")
    education = db.Column(db.Text, default="")
    experience_summary = db.Column(db.Text, default="")
    experience_years = db.Column(db.Float, default=0.0, nullable=False)
    skills = db.Column(db.JSON, default=list, nullable=False)
    desired_roles = db.Column(db.JSON, default=list, nullable=False)
    preferred_locations = db.Column(db.JSON, default=list, nullable=False)
    work_modes = db.Column(db.JSON, default=lambda: ["remote", "hybrid", "onsite"], nullable=False)
    job_types = db.Column(db.JSON, default=lambda: ["full_time", "internship"], nullable=False)
    # Kept for backward-compatible databases; matching uses the system-wide 50% floor.
    minimum_match_score = db.Column(db.Integer, default=50, nullable=False)
    salary_preference = db.Column(db.String(80), default="")
    graduation_year = db.Column(db.Integer)
    onboarding_complete = db.Column(db.Boolean, default=False, nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    accepted_terms_at = db.Column(db.DateTime(timezone=True), nullable=False)
    alerts_consent_at = db.Column(db.DateTime(timezone=True))
    deleted_at = db.Column(db.DateTime(timezone=True))

    resumes = db.relationship("Resume", back_populates="user", cascade="all, delete-orphan")
    applications = db.relationship(
        "Application", back_populates="user", cascade="all, delete-orphan"
    )
    sent_jobs = db.relationship("SentJob", back_populates="user", cascade="all, delete-orphan")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password, method="scrypt")

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    def get_id(self) -> str:
        """Include a revocable version in Flask-Login session identifiers."""

        return f"{self.id}:{self.auth_version}"

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_phone_verified(self) -> bool:
        return self.phone_verified_at is not None

    @property
    def telegram_connected(self) -> bool:
        return bool(self.telegram_chat_id and self.phone_verified_at)

    @property
    def alerts_active(self) -> bool:
        if self.alert_status != "active" or not self.active_until:
            return False
        value = self.active_until
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value > utcnow()

    def activate_alerts(self, days: int = 7) -> None:
        now = utcnow()
        # A renewal creates one fresh, bounded window; repeated clicks cannot
        # accumulate months of unattended delivery or API usage.
        self.active_until = now + timedelta(days=days)
        self.alert_status = "active"
        self.renewal_reminder_sent_at = None


db.Index(
    "uq_users_verified_phone",
    User.phone_e164,
    unique=True,
    sqlite_where=User.phone_verified_at.is_not(None),
    postgresql_where=User.phone_verified_at.is_not(None),
)


class OTPCode(TimestampMixin, db.Model):
    __tablename__ = "otp_codes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    purpose = db.Column(db.String(30), default="verify_email", nullable=False)
    code_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    consumed_at = db.Column(db.DateTime(timezone=True))

    user = db.relationship("User")


db.Index(
    "uq_live_otp_user_purpose",
    OTPCode.user_id,
    OTPCode.purpose,
    unique=True,
    sqlite_where=OTPCode.consumed_at.is_(None),
    postgresql_where=OTPCode.consumed_at.is_(None),
)


class TelegramLinkToken(TimestampMixin, db.Model):
    __tablename__ = "telegram_link_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    pending_chat_id = db.Column(db.String(32), index=True)
    pending_telegram_user_id = db.Column(db.String(32))
    consumed_at = db.Column(db.DateTime(timezone=True))

    user = db.relationship("User")


class Resume(TimestampMixin, db.Model):
    __tablename__ = "resumes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), unique=True, nullable=False)
    mime_type = db.Column(db.String(120), nullable=False)
    extension = db.Column(db.String(12), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    extracted_text = db.Column(db.Text, default="")
    parsed_profile = db.Column(db.JSON, default=dict, nullable=False)
    parse_status = db.Column(db.String(30), default="pending", nullable=False)
    parse_message = db.Column(db.String(300), default="")
    is_current = db.Column(db.Boolean, default=True, nullable=False)

    user = db.relationship("User", back_populates="resumes")


class Job(TimestampMixin, db.Model):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("provider", "provider_job_id", name="uq_job_provider_external"),
    )

    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(40), nullable=False, index=True)
    provider_job_id = db.Column(db.String(160), nullable=False)
    fingerprint = db.Column(db.String(64), unique=True, nullable=False, index=True)
    title = db.Column(db.String(220), nullable=False, index=True)
    company = db.Column(db.String(180), nullable=False, index=True)
    location = db.Column(db.String(180), default="Not specified", nullable=False)
    workplace_type = db.Column(db.String(20), default="onsite", nullable=False)
    job_type = db.Column(db.String(30), default="full_time", nullable=False)
    description = db.Column(db.Text, nullable=False)
    skills = db.Column(db.JSON, default=list, nullable=False)
    minimum_experience = db.Column(db.Float, default=0.0, nullable=False)
    graduate_friendly = db.Column(db.Boolean, default=True, nullable=False)
    salary = db.Column(db.String(120), default="")
    posted_at = db.Column(db.DateTime(timezone=True))
    expires_at = db.Column(db.DateTime(timezone=True))
    apply_url = db.Column(db.Text, nullable=False)
    canonical_url = db.Column(db.Text, nullable=False)
    source_label = db.Column(db.String(80), nullable=False)
    source_checked_at = db.Column(db.DateTime(timezone=True))
    trust_score = db.Column(db.Integer, default=70, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    is_demo = db.Column(db.Boolean, default=False, nullable=False)
    reported_count = db.Column(db.Integer, default=0, nullable=False)
    last_seen_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    applications = db.relationship("Application", back_populates="job")

    @property
    def initials(self) -> str:
        from .utils import safe_initials

        return safe_initials(self.company)


class JobMatch(TimestampMixin, db.Model):
    __tablename__ = "job_matches"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_match_user_job"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    score = db.Column(db.Integer, nullable=False)
    matched_skills = db.Column(db.JSON, default=list, nullable=False)
    missing_skills = db.Column(db.JSON, default=list, nullable=False)
    reasons = db.Column(db.JSON, default=list, nullable=False)
    concerns = db.Column(db.JSON, default=list, nullable=False)

    user = db.relationship("User")
    job = db.relationship("Job")


class Application(TimestampMixin, db.Model):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_application_user_job"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    status = db.Column(db.String(30), default="saved", nullable=False, index=True)
    notes = db.Column(db.Text, default="")
    applied_at = db.Column(db.DateTime(timezone=True))
    next_action_at = db.Column(db.DateTime(timezone=True))

    user = db.relationship("User", back_populates="applications")
    job = db.relationship("Job", back_populates="applications")


class SentJob(TimestampMixin, db.Model):
    __tablename__ = "sent_jobs"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_sent_user_job"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    channel = db.Column(db.String(20), default="telegram", nullable=False)
    status = db.Column(db.String(20), default="queued", nullable=False)
    provider_message_id = db.Column(db.String(120))
    sent_at = db.Column(db.DateTime(timezone=True))
    failure_reason = db.Column(db.String(240), default="")

    user = db.relationship("User", back_populates="sent_jobs")
    job = db.relationship("Job")


class JobReport(TimestampMixin, db.Model):
    __tablename__ = "job_reports"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_report_user_job"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    reason = db.Column(db.String(60), nullable=False)
    details = db.Column(db.String(500), default="")
    status = db.Column(db.String(20), default="open", nullable=False)

    user = db.relationship("User")
    job = db.relationship("Job")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    event = db.Column(db.String(80), nullable=False, index=True)
    entity_type = db.Column(db.String(50), default="")
    entity_id = db.Column(db.String(80), default="")
    metadata_json = db.Column(db.JSON, default=dict, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    actor = db.relationship("User")


class TelegramUpdate(db.Model):
    __tablename__ = "telegram_updates"

    id = db.Column(db.Integer, primary_key=True)
    update_id = db.Column(db.String(40), unique=True, nullable=False, index=True)
    status = db.Column(db.String(20), default="processing", nullable=False, index=True)
    attempts = db.Column(db.Integer, default=1, nullable=False)
    last_error = db.Column(db.String(160), default="", nullable=False)
    processed_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at = db.Column(db.DateTime(timezone=True))


class PrivateFileCleanup(TimestampMixin, db.Model):
    """Durable, idempotent deletion work for private uploaded files."""

    __tablename__ = "private_file_cleanups"

    id = db.Column(db.Integer, primary_key=True)
    stored_name = db.Column(db.String(255), nullable=False, index=True)
    status = db.Column(db.String(20), default="pending", nullable=False, index=True)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    last_error = db.Column(db.String(160), default="", nullable=False)
    completed_at = db.Column(db.DateTime(timezone=True))


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def serialize_job(job: Job, match: JobMatch | None = None) -> dict[str, Any]:
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "workplace_type": job.workplace_type,
        "job_type": job.job_type,
        "skills": job.skills,
        "apply_url": job.apply_url,
        "score": match.score if match else None,
        "matched_skills": match.matched_skills if match else [],
        "missing_skills": match.missing_skills if match else [],
    }
