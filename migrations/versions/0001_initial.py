"""Create the initial Job Matcher schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone_e164", sa.String(24), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True)),
        sa.Column("phone_verified_at", sa.DateTime(timezone=True)),
        sa.Column("telegram_user_id", sa.String(32), unique=True),
        sa.Column("telegram_chat_id", sa.String(32), unique=True),
        sa.Column("telegram_username", sa.String(80)),
        sa.Column("alert_status", sa.String(20), nullable=False),
        sa.Column("active_until", sa.DateTime(timezone=True)),
        sa.Column("renewal_reminder_sent_at", sa.DateTime(timezone=True)),
        sa.Column("preferred_time", sa.String(5), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("daily_job_limit", sa.Integer(), nullable=False),
        sa.Column("headline", sa.String(160)),
        sa.Column("education", sa.Text()),
        sa.Column("experience_summary", sa.Text()),
        sa.Column("experience_years", sa.Float(), nullable=False),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("desired_roles", sa.JSON(), nullable=False),
        sa.Column("preferred_locations", sa.JSON(), nullable=False),
        sa.Column("work_modes", sa.JSON(), nullable=False),
        sa.Column("job_types", sa.JSON(), nullable=False),
        sa.Column("minimum_match_score", sa.Integer(), nullable=False),
        sa.Column("salary_preference", sa.String(80)),
        sa.Column("graduation_year", sa.Integer()),
        sa.Column("onboarding_complete", sa.Boolean(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("accepted_terms_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("alerts_consent_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_phone_e164", "users", ["phone_e164"])
    op.create_index(
        "uq_users_verified_phone",
        "users",
        ["phone_e164"],
        unique=True,
        sqlite_where=sa.text("phone_verified_at IS NOT NULL"),
        postgresql_where=sa.text("phone_verified_at IS NOT NULL"),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("provider_job_id", sa.String(160), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("title", sa.String(220), nullable=False),
        sa.Column("company", sa.String(180), nullable=False),
        sa.Column("location", sa.String(180), nullable=False),
        sa.Column("workplace_type", sa.String(20), nullable=False),
        sa.Column("job_type", sa.String(30), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("minimum_experience", sa.Float(), nullable=False),
        sa.Column("graduate_friendly", sa.Boolean(), nullable=False),
        sa.Column("salary", sa.String(120)),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("apply_url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("source_label", sa.String(80), nullable=False),
        sa.Column("source_checked_at", sa.DateTime(timezone=True)),
        sa.Column("trust_score", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("reported_count", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "provider", "provider_job_id", name="uq_job_provider_external"
        ),
    )
    op.create_index("ix_jobs_provider", "jobs", ["provider"])
    op.create_index("ix_jobs_fingerprint", "jobs", ["fingerprint"], unique=True)
    op.create_index("ix_jobs_title", "jobs", ["title"])
    op.create_index("ix_jobs_company", "jobs", ["company"])
    op.create_index("ix_jobs_is_active", "jobs", ["is_active"])

    op.create_table(
        "otp_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("code_hash", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index(
        "uq_live_otp_user_purpose",
        "otp_codes",
        ["user_id", "purpose"],
        unique=True,
        sqlite_where=sa.text("consumed_at IS NULL"),
        postgresql_where=sa.text("consumed_at IS NULL"),
    )

    op.create_table(
        "telegram_link_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pending_chat_id", sa.String(32)),
        sa.Column("pending_telegram_user_id", sa.String(32)),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index(
        "ix_telegram_link_tokens_token_hash",
        "telegram_link_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_telegram_link_tokens_pending_chat_id",
        "telegram_link_tokens",
        ["pending_chat_id"],
    )

    op.create_table(
        "resumes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("stored_name", sa.String(255), unique=True, nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("extension", sa.String(12), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("parsed_profile", sa.JSON(), nullable=False),
        sa.Column("parse_status", sa.String(30), nullable=False),
        sa.Column("parse_message", sa.String(300)),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        *_timestamps(),
    )

    op.create_table(
        "job_matches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("matched_skills", sa.JSON(), nullable=False),
        sa.Column("missing_skills", sa.JSON(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("concerns", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "job_id", name="uq_match_user_job"),
    )

    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("next_action_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "job_id", name="uq_application_user_job"),
    )
    op.create_index("ix_applications_status", "applications", ["status"])

    op.create_table(
        "sent_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider_message_id", sa.String(120)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("failure_reason", sa.String(240)),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "job_id", name="uq_sent_user_job"),
    )

    op.create_table(
        "job_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(60), nullable=False),
        sa.Column("details", sa.String(500)),
        sa.Column("status", sa.String(20), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "job_id", name="uq_report_user_job"),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("event", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(50)),
        sa.Column("entity_id", sa.String(80)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_event", "audit_logs", ["event"])

    op.create_table(
        "telegram_updates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("update_id", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(160), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_telegram_updates_update_id",
        "telegram_updates",
        ["update_id"],
        unique=True,
    )
    op.create_index("ix_telegram_updates_status", "telegram_updates", ["status"])

    op.create_table(
        "private_file_cleanups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stored_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(160), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index(
        "ix_private_file_cleanups_stored_name",
        "private_file_cleanups",
        ["stored_name"],
    )
    op.create_index(
        "ix_private_file_cleanups_status",
        "private_file_cleanups",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("private_file_cleanups")
    op.drop_table("telegram_updates")
    op.drop_table("audit_logs")
    op.drop_table("job_reports")
    op.drop_table("sent_jobs")
    op.drop_table("applications")
    op.drop_table("job_matches")
    op.drop_table("resumes")
    op.drop_table("telegram_link_tokens")
    op.drop_table("otp_codes")
    op.drop_table("jobs")
    op.drop_table("users")
