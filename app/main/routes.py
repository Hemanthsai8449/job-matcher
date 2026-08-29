from __future__ import annotations

import io
import json
import time
from collections import Counter, defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user, login_required, logout_user

from ..extensions import db
from ..models import (
    Application,
    AuditLog,
    Job,
    JobMatch,
    JobReport,
    Resume,
    SentJob,
    TelegramLinkToken,
    User,
    as_utc,
)
from ..onboarding.forms import PreferencesForm
from ..services.file_cleanup import (
    is_discarded_private_file,
    process_private_file,
    queue_private_file,
)
from ..services.matching_service import MATCH_SCORE_FLOOR, rebuild_matches
from ..utils import utcnow

bp = Blueprint("main", __name__)


def comma_list(value: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def alert_delivery_context(user: User) -> dict[str, str | None]:
    """Return truthful, user-local delivery information for alert surfaces."""

    try:
        timezone = ZoneInfo(str(user.timezone or "Asia/Kolkata"))
    except (ZoneInfoNotFoundError, ValueError):
        timezone = ZoneInfo("UTC")
    last_sent_at = db.session.scalar(
        db.select(SentJob.sent_at)
        .where(
            SentJob.user_id == user.id,
            SentJob.status.in_(("sent", "delivered")),
            SentJob.sent_at.is_not(None),
        )
        .order_by(SentJob.sent_at.desc(), SentJob.id.desc())
        .limit(1)
    )
    last_sent = as_utc(last_sent_at)
    return {
        "last_sent": (
            last_sent.astimezone(timezone).strftime("%d %b %Y, %I:%M %p")
            if last_sent is not None
            else None
        ),
        "timezone": str(user.timezone or "Asia/Kolkata"),
    }


@bp.get("/")
def index():
    return render_template("main/index.html")


@bp.get("/dashboard")
@login_required
def dashboard():
    current_time = utcnow()
    demo_visibility = (
        True if current_app.config.get("SEED_DEMO_JOBS") else Job.is_demo.is_(False)
    )
    top_matches = db.session.execute(
        db.select(JobMatch, Job)
        .join(Job, Job.id == JobMatch.job_id)
        .where(
            JobMatch.user_id == current_user.id,
            Job.is_active.is_(True),
            demo_visibility,
            Job.trust_score >= 40,
            Job.reported_count < 3,
            db.or_(Job.expires_at.is_(None), Job.expires_at > current_time),
            JobMatch.score >= MATCH_SCORE_FLOOR,
        )
        .order_by(JobMatch.score.desc(), Job.posted_at.desc(), Job.id.desc())
        .limit(3)
    ).all()

    application_counts = dict(
        db.session.execute(
            db.select(Application.status, db.func.count(Application.id))
            .where(Application.user_id == current_user.id)
            .group_by(Application.status)
        ).all()
    )
    skill_counter: Counter[str] = Counter()
    for match, _job in top_matches:
        skill_counter.update(match.matched_skills or [])
        skill_counter.update(match.missing_skills or [])
    profile_steps = [
        ("Email verified", current_user.is_email_verified),
        (
            "Profile confirmed",
            bool(
                current_user.skills
                or current_user.headline
                or current_user.education
                or current_user.experience_summary
            ),
        ),
        ("Preferences saved", current_user.onboarding_complete),
        ("Telegram connected", current_user.telegram_connected),
    ]
    return render_template(
        "main/dashboard.html",
        top_matches=top_matches,
        application_counts=application_counts,
        skill_summary=skill_counter.most_common(6),
        profile_steps=profile_steps,
        readiness=round(sum(done for _, done in profile_steps) / len(profile_steps) * 100),
        alert_delivery=alert_delivery_context(current_user),
    )


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    form = PreferencesForm()
    if request.method == "GET":
        form.desired_roles.data = ", ".join(current_user.desired_roles or [])
        form.preferred_locations.data = ", ".join(current_user.preferred_locations or [])
        form.remote.data = "remote" in (current_user.work_modes or [])
        form.hybrid.data = "hybrid" in (current_user.work_modes or [])
        form.onsite.data = "onsite" in (current_user.work_modes or [])
        form.full_time.data = "full_time" in (current_user.job_types or [])
        form.internship.data = "internship" in (current_user.job_types or [])
        form.contract.data = "contract" in (current_user.job_types or [])
        form.graduation_year.data = current_user.graduation_year
        form.daily_job_limit.data = str(current_user.daily_job_limit)
        form.preferred_time.data = current_user.preferred_time
        form.timezone.data = current_user.timezone
        form.salary_preference.data = current_user.salary_preference
    elif form.validate_on_submit():
        work_modes = [
            mode
            for mode, selected in {
                "remote": form.remote.data,
                "hybrid": form.hybrid.data,
                "onsite": form.onsite.data,
            }.items()
            if selected
        ]
        job_types = [
            kind
            for kind, selected in {
                "full_time": form.full_time.data,
                "internship": form.internship.data,
                "contract": form.contract.data,
            }.items()
            if selected
        ]
        if not work_modes or not job_types:
            flash("Select at least one work mode and one job type.", "warning")
        else:
            current_user.desired_roles = comma_list(form.desired_roles.data)
            current_user.preferred_locations = comma_list(form.preferred_locations.data)
            current_user.work_modes = work_modes
            current_user.job_types = job_types
            current_user.graduation_year = form.graduation_year.data
            current_user.minimum_match_score = MATCH_SCORE_FLOOR
            current_user.daily_job_limit = int(form.daily_job_limit.data)
            current_user.preferred_time = form.preferred_time.data
            current_user.timezone = form.timezone.data
            current_user.salary_preference = form.salary_preference.data.strip()
            db.session.add(AuditLog(actor_user_id=current_user.id, event="settings.updated"))
            db.session.commit()
            rebuild_matches(current_user)
            flash("Your preferences and alert schedule were updated.", "success")
            return redirect(url_for("main.settings"))
    current_resume = db.session.scalar(
        db.select(Resume)
        .where(Resume.user_id == current_user.id, Resume.is_current.is_(True))
        .order_by(Resume.created_at.desc())
    )
    return render_template(
        "main/settings.html",
        form=form,
        current_resume=current_resume,
        alert_delivery=alert_delivery_context(current_user),
    )


@bp.post("/alerts/pause")
@login_required
def pause_alerts():
    current_user.alert_status = "paused"
    db.session.add(AuditLog(actor_user_id=current_user.id, event="alerts.paused"))
    db.session.commit()
    flash("Telegram alerts are paused. Your saved jobs remain available.", "success")
    return redirect(url_for("main.settings"))


@bp.post("/telegram/disconnect")
@login_required
def disconnect_telegram():
    now = utcnow()
    pending_tokens = db.session.scalars(
        db.select(TelegramLinkToken).where(
            TelegramLinkToken.user_id == current_user.id,
            TelegramLinkToken.consumed_at.is_(None),
        )
    ).all()
    for token in pending_tokens:
        token.consumed_at = now
        token.pending_chat_id = None
        token.pending_telegram_user_id = None
    current_user.telegram_user_id = None
    current_user.telegram_chat_id = None
    current_user.telegram_username = None
    current_user.phone_verified_at = None
    current_user.alert_status = "inactive"
    current_user.active_until = None
    current_user.renewal_reminder_sent_at = None
    current_user.alerts_consent_at = None
    db.session.add(AuditLog(actor_user_id=current_user.id, event="telegram.disconnected"))
    db.session.commit()
    flash("Telegram was disconnected and job alerts were stopped.", "success")
    return redirect(url_for("main.settings"))


@bp.post("/alerts/resume")
@login_required
def resume_alerts():
    if not current_user.telegram_connected:
        flash("Connect and verify Telegram before resuming alerts.", "warning")
        return redirect(url_for("onboarding.telegram_connect"))
    active_until = as_utc(current_user.active_until)
    if active_until and active_until > utcnow():
        current_user.alert_status = "active"
        message = "Alerts resumed for the current seven-day period."
    else:
        current_user.activate_alerts(current_app.config["ALERT_WINDOW_DAYS"])
        message = "Alerts renewed for seven days."
    db.session.add(AuditLog(actor_user_id=current_user.id, event="alerts.resumed"))
    db.session.commit()
    flash(message, "success")
    return redirect(url_for("main.settings"))


@bp.post("/alerts/renew")
@login_required
def renew_alerts():
    if not current_user.telegram_connected:
        flash("Connect and verify Telegram before activating alerts.", "warning")
        return redirect(url_for("onboarding.telegram_connect"))
    current_user.alerts_consent_at = current_user.alerts_consent_at or utcnow()
    current_user.activate_alerts(current_app.config["ALERT_WINDOW_DAYS"])
    db.session.add(AuditLog(actor_user_id=current_user.id, event="alerts.renewed"))
    db.session.commit()
    flash("Your job alerts are active for the next seven days.", "success")
    return redirect(url_for("main.dashboard"))


@bp.get("/applications")
@login_required
def applications():
    rows = db.session.execute(
        db.select(Application, Job)
        .join(Job, Job.id == Application.job_id)
        .where(Application.user_id == current_user.id, Application.status != "not_interested")
        .order_by(Application.updated_at.desc())
    ).all()
    grouped = defaultdict(list)
    for application, job in rows:
        grouped[application.status].append((application, job))
    return render_template("main/applications.html", grouped=grouped, rows=rows)


@bp.get("/toolkit")
@login_required
def toolkit():
    matches = db.session.scalars(
        db.select(JobMatch)
        .where(JobMatch.user_id == current_user.id)
        .order_by(JobMatch.score.desc())
        .limit(25)
    ).all()
    missing = Counter(skill for match in matches for skill in (match.missing_skills or []))
    current_resume = db.session.scalar(
        db.select(Resume)
        .where(Resume.user_id == current_user.id, Resume.is_current.is_(True))
        .order_by(Resume.created_at.desc())
    )
    return render_template(
        "main/toolkit.html",
        skill_gaps=missing.most_common(10),
        current_resume=current_resume,
        match_count=len(matches),
    )


@bp.get("/weekly-report")
@login_required
def weekly_report():
    since = utcnow() - timedelta(days=7)
    applications = db.session.scalars(
        db.select(Application).where(
            Application.user_id == current_user.id, Application.updated_at >= since
        )
    ).all()
    deliveries = db.session.scalar(
        db.select(db.func.count(SentJob.id)).where(
            SentJob.user_id == current_user.id,
            SentJob.status.in_(["sent", "delivered"]),
            SentJob.sent_at >= since,
        )
    ) or 0
    counts = Counter(item.status for item in applications)
    return render_template(
        "main/weekly_report.html", application_counts=counts, deliveries=deliveries, since=since
    )


@bp.get("/privacy")
def privacy():
    return render_template("main/privacy.html")


@bp.get("/terms")
def terms():
    return render_template("main/terms.html")


@bp.get("/job-safety")
def safety():
    return render_template("main/safety.html")


@bp.get("/account/export")
@login_required
def export_account():
    payload = {
        "profile": {
            "name": current_user.full_name,
            "email": current_user.email,
            "phone": current_user.phone_e164,
            "skills": current_user.skills,
            "desired_roles": current_user.desired_roles,
            "preferred_locations": current_user.preferred_locations,
            "sign_in_methods": {
                "password": bool(current_user.password_hash),
                "google": bool(current_user.google_sub),
            },
        },
        "applications": [
            {"job_id": app.job_id, "status": app.status, "notes": app.notes}
            for app in current_user.applications
        ],
    }
    data = io.BytesIO(json.dumps(payload, indent=2, default=str).encode("utf-8"))
    return send_file(
        data,
        as_attachment=True,
        download_name="job-matcher-data.json",
        mimetype="application/json",
    )


@bp.post("/account/delete")
@login_required
def delete_account():
    confirmation_ok = request.form.get("confirmation") == "DELETE"
    if current_user.password_hash:
        identity_ok = current_user.check_password(request.form.get("password", ""))
        failure_message = "Enter your password and type DELETE exactly to remove the account."
    else:
        try:
            google_sign_in_is_recent = (
                time.time() - float(session.get("google_authenticated_at")) <= 600
            )
        except (TypeError, ValueError):
            google_sign_in_is_recent = False
        identity_ok = bool(current_user.google_sub and google_sign_in_is_recent)
        failure_message = (
            "Sign out, sign in with Google again, and type DELETE to remove the account."
        )
    if not confirmation_ok or not identity_ok:
        flash(failure_message, "danger")
        return redirect(url_for("main.settings"))
    user = db.session.get(User, current_user.id)
    reported_job_ids = set(
        db.session.scalars(
            db.select(JobReport.job_id).where(
                JobReport.user_id == user.id,
                JobReport.status == "open",
            )
        ).all()
    )
    cleanup_records = [
        queue_private_file(resume.stored_name)
        for resume in list(user.resumes)
        if not is_discarded_private_file(resume.stored_name)
    ]
    db.session.flush()
    cleanup_ids = [record.id for record in cleanup_records]
    logout_user()
    db.session.delete(user)
    db.session.flush()
    for job_id in reported_job_ids:
        open_count = db.session.scalar(
            db.select(db.func.count(JobReport.id)).where(
                JobReport.job_id == job_id,
                JobReport.status == "open",
            )
        ) or 0
        db.session.execute(
            db.update(Job)
            .where(Job.id == job_id)
            .values(reported_count=int(open_count))
        )
    db.session.commit()
    cleanup_failed = sum(not process_private_file(cleanup_id) for cleanup_id in cleanup_ids)
    if cleanup_failed:
        flash(
            "Your account was deleted. Private-file erasure is queued for automatic retry.",
            "warning",
        )
    else:
        flash("Your Job Matcher account and stored resume were deleted.", "success")
    return redirect(url_for("main.index"))
