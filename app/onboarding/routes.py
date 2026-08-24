from __future__ import annotations

import re

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from ..extensions import db, limiter
from ..models import AuditLog, Resume, TelegramLinkToken
from ..services.file_cleanup import (
    is_discarded_private_file,
    process_private_file,
    queue_private_file,
)
from ..services.matching_service import rebuild_matches
from ..services.resume_service import ResumeValidationError, save_and_parse_resume
from ..services.telegram_service import build_deep_link, create_link_token
from .forms import ResumeUploadForm

bp = Blueprint("onboarding", __name__, url_prefix="/onboarding")

SUPPORTED_TIMEZONES = {
    "Asia/Kolkata",
    "UTC",
    "Asia/Dubai",
    "Europe/London",
    "America/New_York",
    "America/Los_Angeles",
}


def split_values(raw: str) -> list[str]:
    return list(
        dict.fromkeys(value.strip()[:120] for value in raw[:3000].split(",") if value.strip())
    )


def current_resume() -> Resume | None:
    return db.session.scalar(
        db.select(Resume)
        .where(Resume.user_id == current_user.id, Resume.is_current.is_(True))
        .order_by(Resume.created_at.desc())
    )


def mask_phone(phone: str) -> str:
    if len(phone) < 6:
        return "Registered number"
    return f"{phone[:3]} ••••• {phone[-3:]}"


@bp.route("/resume", methods=["GET", "POST"])
@login_required
@limiter.limit("12 per hour")
def resume():
    form = ResumeUploadForm()
    if form.validate_on_submit():
        try:
            record = save_and_parse_resume(current_user, form.resume.data)
        except ResumeValidationError as exc:
            flash(str(exc), "danger")
        else:
            db.session.add(
                AuditLog(
                    actor_user_id=current_user.id,
                    event="resume.uploaded",
                    entity_type="resume",
                    entity_id=str(record.id),
                    metadata_json={
                        "extension": record.extension,
                        "parse_status": record.parse_status,
                    },
                )
            )
            db.session.commit()
            if record.parse_status not in {"parsed", "reviewed"}:
                flash(record.parse_message or "Review the information we could extract.", "warning")
            else:
                flash("Resume processed. Please review the extracted details.", "success")
            return redirect(url_for("onboarding.resume_review"))
    return render_template("onboarding/resume.html", form=form, resume=current_resume())


@bp.route("/resume/review", methods=["GET", "POST"])
@login_required
def resume_review():
    record = current_resume()
    return_to = request.form.get("next") if request.method == "POST" else request.args.get("next")
    return_to = "settings" if return_to == "settings" else ""
    profile = dict(record.parsed_profile or {}) if record else {}
    confirmed_profile = {
        "headline": current_user.headline or "",
        "education": current_user.education or "",
        "experience": current_user.experience_summary or "",
        "experience_years": current_user.experience_years or 0,
        "graduation_year": current_user.graduation_year,
        "skills": current_user.skills or [],
    }
    for key, value in confirmed_profile.items():
        if profile.get(key) in (None, "", []):
            profile[key] = value
    if request.method == "POST":
        headline = request.form.get("headline", "").strip()[:160]
        education = request.form.get("education", "").strip()[:3000]
        experience = request.form.get("experience", "").strip()[:5000]
        skills: list[str] = []
        seen_skills: set[str] = set()
        for raw_skill in request.form.getlist("skills"):
            skill = raw_skill.strip()[:80]
            key = skill.casefold()
            if skill and key not in seen_skills:
                seen_skills.add(key)
                skills.append(skill)
            if len(skills) >= 100:
                break
        try:
            experience_years = float(request.form.get("experience_years") or 0)
            graduation_year = int(request.form.get("graduation_year") or 0) or None
        except ValueError:
            flash("Check the experience and graduation values.", "danger")
            profile.update(
                headline=headline,
                education=education,
                experience=experience,
                skills=skills,
            )
            return render_template(
                "onboarding/resume_review.html",
                resume=record,
                parsed_profile=profile,
                return_to=return_to,
            )
        if not 0 <= experience_years <= 60 or (
            graduation_year is not None and not 1990 <= graduation_year <= 2100
        ):
            flash("Enter a valid experience amount and graduation year.", "danger")
            profile.update(
                headline=headline,
                education=education,
                experience=experience,
                experience_years=experience_years,
                graduation_year=graduation_year,
                skills=skills,
            )
            return render_template(
                "onboarding/resume_review.html",
                resume=record,
                parsed_profile=profile,
                return_to=return_to,
            )
        current_user.headline = headline
        current_user.education = education
        current_user.experience_summary = experience
        current_user.experience_years = experience_years
        current_user.graduation_year = graduation_year
        current_user.skills = skills
        profile.update(
            {
                "headline": headline,
                "education": education,
                "experience": experience,
                "experience_years": experience_years,
                "graduation_year": graduation_year,
                "skills": skills,
            }
        )
        if record:
            record.parsed_profile = profile
            record.parse_status = "reviewed"
        db.session.add(
            AuditLog(
                actor_user_id=current_user.id,
                event="resume.reviewed" if record else "profile.updated_manually",
                entity_type="resume" if record else "user",
                entity_id=str(record.id if record else current_user.id),
            )
        )
        db.session.commit()
        if current_user.onboarding_complete:
            rebuild_matches(current_user)
        flash("Your confirmed matching profile was saved.", "success")
        return redirect(
            url_for("main.settings")
            if return_to == "settings"
            else url_for("onboarding.preferences")
        )
    return render_template(
        "onboarding/resume_review.html",
        resume=record,
        parsed_profile=profile,
        return_to=return_to,
    )


@bp.route("/preferences", methods=["GET", "POST"])
@login_required
def preferences():
    profile = {
        "desired_roles": current_user.desired_roles or [],
        "preferred_locations": current_user.preferred_locations or [],
        "work_modes": current_user.work_modes or ["remote", "hybrid", "onsite"],
        "job_types": current_user.job_types or ["full_time", "internship"],
        "graduation_year": current_user.graduation_year,
        "experience_years": current_user.experience_years,
        "minimum_match_score": current_user.minimum_match_score,
        "salary_preference": current_user.salary_preference,
        "daily_job_limit": current_user.daily_job_limit,
        "preferred_time": current_user.preferred_time,
        "timezone": current_user.timezone,
    }
    if request.method == "POST":
        desired_roles = split_values(request.form.get("desired_roles", ""))
        locations = split_values(request.form.get("preferred_locations", ""))
        work_modes = [
            value
            for value in request.form.getlist("work_modes")
            if value in {"remote", "hybrid", "onsite"}
        ]
        job_types = [
            value
            for value in request.form.getlist("job_types")
            if value in {"full_time", "internship", "contract"}
        ]
        if not desired_roles or not locations or not work_modes or not job_types:
            flash("Add at least one role, location, work mode, and opportunity type.", "danger")
        else:
            try:
                graduation_year = int(request.form.get("graduation_year") or 0) or None
                experience_years = float(request.form.get("experience_years") or 0)
                match_score = int(request.form.get("minimum_match_score") or 45)
                daily_limit = int(
                    request.form.get("daily_job_limit") or current_user.daily_job_limit
                )
            except ValueError:
                flash("Check the graduation, experience, and matching values.", "danger")
                return render_template("onboarding/preferences.html", profile=profile)
            if graduation_year and not 1990 <= graduation_year <= 2100:
                flash("Enter a valid graduation year.", "danger")
                return render_template("onboarding/preferences.html", profile=profile)
            current_user.desired_roles = desired_roles[:20]
            current_user.preferred_locations = locations[:20]
            current_user.work_modes = list(dict.fromkeys(work_modes))
            current_user.job_types = list(dict.fromkeys(job_types))
            current_user.graduation_year = graduation_year
            current_user.experience_years = max(0.0, min(experience_years, 60.0))
            current_user.minimum_match_score = max(30, min(match_score, 95))
            current_user.daily_job_limit = daily_limit if daily_limit in {3, 5, 10} else 5
            preferred_time = request.form.get("preferred_time", "")
            timezone = request.form.get("timezone", "")
            current_user.preferred_time = (
                preferred_time
                if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", preferred_time)
                else "09:00"
            )
            current_user.timezone = timezone if timezone in SUPPORTED_TIMEZONES else "Asia/Kolkata"
            current_user.salary_preference = request.form.get("salary_preference", "").strip()[:80]
            current_user.onboarding_complete = True
            db.session.add(AuditLog(actor_user_id=current_user.id, event="onboarding.completed"))
            db.session.commit()
            rebuild_matches(current_user)
            flash("Preferences saved. Connect Telegram when you are ready.", "success")
            return redirect(url_for("onboarding.telegram_connect"))
    return render_template("onboarding/preferences.html", profile=profile)


@bp.route("/telegram", methods=["GET", "POST"])
@login_required
@limiter.limit("20 per hour")
def telegram_connect():
    state = request.args.get("state", "")
    if current_user.telegram_connected:
        state = "connected"
    telegram_url = None
    if request.method == "POST":
        if not current_user.is_email_verified:
            flash("Verify your email before connecting Telegram.", "warning")
            return redirect(url_for("auth.verify_email"))
        if not current_app.config.get("TELEGRAM_BOT_USERNAME"):
            flash(
                "Telegram is ready in the application, but the bot username is not configured yet.",
                "warning",
            )
        else:
            raw_token, token_record = create_link_token(current_user)
            telegram_url = build_deep_link(raw_token)
            state = "waiting"
            db.session.add(
                AuditLog(
                    actor_user_id=current_user.id,
                    event="telegram.link_created",
                    entity_type="telegram_link_token",
                    entity_id=str(token_record.id),
                )
            )
            db.session.commit()
    if not telegram_url and state == "waiting":
        token_record = db.session.scalar(
            db.select(TelegramLinkToken)
            .where(
                TelegramLinkToken.user_id == current_user.id,
                TelegramLinkToken.consumed_at.is_(None),
            )
            .order_by(TelegramLinkToken.created_at.desc())
        )
        if token_record:
            # Raw tokens are never stored, so a refresh requires a new secure link.
            state = "expired"
    return render_template(
        "onboarding/telegram_connect.html",
        user=current_user,
        telegram_status=state or "not_connected",
        telegram_url=telegram_url,
        masked_phone=mask_phone(current_user.phone_e164),
        expires_minutes=current_app.config["TELEGRAM_LINK_TTL_SECONDS"] // 60,
    )


@bp.get("/telegram/status")
@login_required
def telegram_status():
    """Return only the signed-in student's connection state for status polling."""

    return jsonify(
        connected=current_user.telegram_connected,
        alert_status=current_user.alert_status,
        redirect=url_for("main.dashboard") if current_user.telegram_connected else None,
    )


@bp.post("/resume/delete")
@login_required
def delete_resume():
    records = db.session.scalars(
        db.select(Resume)
        .where(Resume.user_id == current_user.id)
        .order_by(Resume.created_at.desc())
    ).all()
    if not records:
        return redirect(url_for("main.settings"))
    cleanup_records = [
        queue_private_file(record.stored_name)
        for record in records
        if not is_discarded_private_file(record.stored_name)
    ]
    db.session.flush()
    cleanup_ids = [cleanup.id for cleanup in cleanup_records]
    for record in records:
        db.session.delete(record)
    db.session.add(
        AuditLog(
            actor_user_id=current_user.id,
            event="resume.deleted",
            entity_type="resume",
            metadata_json={"resume_records_removed": len(records)},
        )
    )
    db.session.commit()
    failed = sum(not process_private_file(cleanup_id) for cleanup_id in cleanup_ids)
    if not failed:
        flash(
            "All stored resume files were deleted. Your confirmed profile remains editable.",
            "success",
        )
    else:
        flash(
            "The resume records were removed. Some private-file erasure is queued for retry.",
            "warning",
        )
    return redirect(url_for("main.settings"))
