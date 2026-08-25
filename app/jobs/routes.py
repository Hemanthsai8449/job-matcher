from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db, limiter
from ..models import Application, AuditLog, Job, JobMatch, JobReport, as_utc
from ..services.matching_service import MATCH_SCORE_FLOOR, rebuild_matches, score_job
from ..utils import canonicalize_url, is_safe_relative_url, utcnow
from .forms import JobActionForm, JobReportForm, MatchFilterForm

bp = Blueprint("jobs", __name__, url_prefix="/jobs")

ALLOWED_STATUSES = {
    "saved",
    "applied",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
    "not_interested",
}


@bp.get("")
@login_required
def matches():
    form = MatchFilterForm(request.args, meta={"csrf": False})
    current_time = utcnow()
    demo_visibility = (
        True if current_app.config.get("SEED_DEMO_JOBS") else Job.is_demo.is_(False)
    )
    statement = (
        db.select(JobMatch, Job, Application)
        .join(Job, Job.id == JobMatch.job_id)
        .outerjoin(
            Application,
            db.and_(
                Application.job_id == Job.id,
                Application.user_id == current_user.id,
            ),
        )
        .where(
            JobMatch.user_id == current_user.id,
            JobMatch.score >= MATCH_SCORE_FLOOR,
            Job.is_active.is_(True),
            demo_visibility,
            Job.trust_score >= 40,
            Job.reported_count < 3,
            db.or_(Job.expires_at.is_(None), Job.expires_at > current_time),
            db.or_(Application.id.is_(None), Application.status != "not_interested"),
        )
    )
    q = (request.args.get("q") or "").strip()
    location = (request.args.get("location") or "").strip()
    work_mode = (request.args.get("work_mode") or "").strip()
    job_type = (request.args.get("job_type") or "").strip()
    if q:
        pattern = f"%{q}%"
        statement = statement.where(
            db.or_(
                Job.title.ilike(pattern),
                Job.company.ilike(pattern),
                Job.description.ilike(pattern),
            )
        )
    if location:
        statement = statement.where(Job.location.ilike(f"%{location}%"))
    if work_mode:
        statement = statement.where(Job.workplace_type == work_mode)
    if job_type:
        statement = statement.where(Job.job_type == job_type)
    rows = db.session.execute(
        statement.order_by(JobMatch.score.desc(), Job.posted_at.desc(), Job.id.desc())
    ).all()
    return render_template(
        "jobs/matches.html",
        rows=rows,
        form=form,
        filters=request.args,
        match_score_floor=MATCH_SCORE_FLOOR,
    )


@bp.get("/<int:job_id>")
@login_required
def detail(job_id: int):
    job = db.get_or_404(Job, job_id)
    expires_at = as_utc(job.expires_at)
    apply_allowed = bool(
        not job.is_demo
        and job.is_active
        and job.trust_score >= 40
        and job.reported_count < 3
        and (expires_at is None or expires_at > utcnow())
    )
    if not apply_allowed:
        flash(
            "This listing cannot currently accept applications, but your tracker history "
            "is preserved.",
            "warning",
        )
    match = score_job(current_user, job)
    application = db.session.scalar(
        db.select(Application).where(
            Application.job_id == job.id, Application.user_id == current_user.id
        )
    )
    action_form = JobActionForm(obj=application)
    report_form = JobReportForm()
    return render_template(
        "jobs/detail.html",
        job=job,
        match=match,
        application=application,
        action_form=action_form,
        report_form=report_form,
        apply_allowed=apply_allowed,
        expires_at_utc=(
            expires_at.isoformat().replace("+00:00", "Z") if expires_at is not None else None
        ),
    )


@bp.post("/refresh")
@login_required
@limiter.limit("12 per hour")
def refresh():
    if not current_user.onboarding_complete:
        flash("Complete your profile preferences before refreshing matches.", "info")
        return redirect(url_for("onboarding.preferences"))
    rebuild_matches(current_user)
    flash("Your matches were refreshed using your latest profile and active listings.", "success")
    return redirect(url_for("jobs.matches"))


@bp.post("/<int:job_id>/action")
@login_required
def action(job_id: int):
    job = db.get_or_404(Job, job_id)
    form = JobActionForm()
    if not form.validate_on_submit() or form.status.data not in ALLOWED_STATUSES:
        abort(400, description="Choose a valid application status.")
    application = db.session.scalar(
        db.select(Application).where(
            Application.job_id == job.id, Application.user_id == current_user.id
        )
    )
    if not application:
        application = Application(user_id=current_user.id, job_id=job.id)
        db.session.add(application)
    application.status = form.status.data
    application.notes = (form.notes.data or application.notes or "").strip()
    if form.status.data == "applied" and not application.applied_at:
        application.applied_at = utcnow()
    db.session.add(
        AuditLog(
            actor_user_id=current_user.id,
            event=f"job.{form.status.data}",
            entity_type="job",
            entity_id=str(job.id),
        )
    )
    db.session.commit()
    label = form.status.data.replace("_", " ").title()
    flash(f"{job.title} is now marked as {label}.", "success")
    next_url = request.form.get("next")
    return redirect(
        next_url if is_safe_relative_url(next_url) else url_for("jobs.detail", job_id=job.id)
    )


@bp.get("/<int:job_id>/apply")
@login_required
def apply_redirect(job_id: int):
    job = db.get_or_404(Job, job_id)
    expires_at = as_utc(job.expires_at)
    if (
        job.is_demo
        or not job.is_active
        or (expires_at and expires_at <= utcnow())
        or job.trust_score < 40
        or job.reported_count >= 3
    ):
        message = (
            "Demonstration listings cannot accept applications."
            if job.is_demo
            else "This listing is no longer active."
        )
        flash(message, "warning")
        return redirect(url_for("jobs.detail", job_id=job.id))
    try:
        destination = canonicalize_url(job.apply_url)
    except ValueError:
        flash("This application link did not pass our safety checks.", "danger")
        return redirect(url_for("jobs.detail", job_id=job.id))
    return redirect(destination)


@bp.post("/<int:job_id>/report")
@login_required
def report(job_id: int):
    job = db.get_or_404(Job, job_id)
    form = JobReportForm()
    if not form.validate_on_submit():
        abort(400, description="Select a reason and check the report details.")
    existing = db.session.scalar(
        db.select(JobReport).where(
            JobReport.job_id == job.id, JobReport.user_id == current_user.id
        )
    )
    if existing:
        if existing.status == "open":
            existing.reason = form.reason.data
            existing.details = (form.details.data or "").strip()
        else:
            reopened = db.session.execute(
                db.update(JobReport)
                .where(JobReport.id == existing.id, JobReport.status != "open")
                .values(
                    reason=form.reason.data,
                    details=(form.details.data or "").strip(),
                    status="open",
                    updated_at=utcnow(),
                )
            )
            if reopened.rowcount == 1:
                db.session.execute(
                    db.update(Job)
                    .where(Job.id == job.id)
                    .values(reported_count=Job.reported_count + 1)
                )
    else:
        report = JobReport(
            user_id=current_user.id,
            job_id=job.id,
            reason=form.reason.data,
            details=(form.details.data or "").strip(),
        )
        db.session.add(report)
        db.session.flush()
        db.session.execute(
            db.update(Job)
            .where(Job.id == job.id)
            .values(reported_count=Job.reported_count + 1)
        )
    db.session.add(
        AuditLog(
            actor_user_id=current_user.id,
            event="job.reported",
            entity_type="job",
            entity_id=str(job.id),
        )
    )
    db.session.commit()
    flash("Thank you. The listing was sent to our review queue.", "success")
    return redirect(url_for("jobs.detail", job_id=job.id))
