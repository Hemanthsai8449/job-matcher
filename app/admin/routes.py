from __future__ import annotations

from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import AuditLog, Job, JobReport, SentJob, User
from ..services.job_sources import fetch_and_upsert_all

bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@bp.get("")
@admin_required
def dashboard():
    metrics = {
        "users": db.session.scalar(
            db.select(db.func.count(User.id)).where(User.deleted_at.is_(None))
        )
        or 0,
        "verified_users": db.session.scalar(
            db.select(db.func.count(User.id)).where(User.email_verified_at.is_not(None))
        )
        or 0,
        "active_jobs": db.session.scalar(
            db.select(db.func.count(Job.id)).where(Job.is_active.is_(True))
        )
        or 0,
        "reports": db.session.scalar(
            db.select(db.func.count(JobReport.id)).where(JobReport.status == "open")
        )
        or 0,
        "delivery_failures": db.session.scalar(
            db.select(db.func.count(SentJob.id)).where(SentJob.status.in_(["failed", "unknown"]))
        )
        or 0,
    }
    reports = db.session.execute(
        db.select(JobReport, Job, User)
        .join(Job, Job.id == JobReport.job_id)
        .join(User, User.id == JobReport.user_id)
        .where(JobReport.status == "open")
        .order_by(JobReport.created_at.desc())
        .limit(15)
    ).all()
    recent_jobs = db.session.scalars(db.select(Job).order_by(Job.updated_at.desc()).limit(12)).all()
    recent_logs = db.session.scalars(
        db.select(AuditLog).order_by(AuditLog.created_at.desc()).limit(15)
    ).all()
    return render_template(
        "admin/dashboard.html",
        metrics=metrics,
        reports=reports,
        recent_jobs=recent_jobs,
        recent_logs=recent_logs,
    )


@bp.post("/sync-jobs")
@admin_required
def sync_jobs():
    summary = fetch_and_upsert_all()
    db.session.add(AuditLog(actor_user_id=current_user.id, event="jobs.sync_requested"))
    db.session.commit()
    flash(f"Job-source sync completed: {summary}", "success")
    return redirect(url_for("admin.dashboard"))


@bp.post("/jobs/<int:job_id>/toggle")
@admin_required
def toggle_job(job_id: int):
    job = db.get_or_404(Job, job_id)
    job.is_active = not job.is_active
    db.session.add(
        AuditLog(
            actor_user_id=current_user.id,
            event="job.activated" if job.is_active else "job.deactivated",
            entity_type="job",
            entity_id=str(job.id),
        )
    )
    db.session.commit()
    flash(f"{job.title} is now {'active' if job.is_active else 'inactive'}.", "success")
    return redirect(url_for("admin.dashboard"))


@bp.post("/reports/<int:report_id>/resolve")
@admin_required
def resolve_report(report_id: int):
    report = db.get_or_404(JobReport, report_id)
    resolved = db.session.execute(
        db.update(JobReport)
        .where(JobReport.id == report.id, JobReport.status == "open")
        .values(status="resolved", updated_at=db.func.now())
    )
    if resolved.rowcount == 1:
        db.session.execute(
            db.update(Job)
            .where(Job.id == report.job_id)
            .values(
                reported_count=db.case(
                    (Job.reported_count > 0, Job.reported_count - 1),
                    else_=0,
                )
            )
        )
    db.session.add(
        AuditLog(
            actor_user_id=current_user.id,
            event="report.resolved",
            entity_type="job_report",
            entity_id=str(report.id),
        )
    )
    db.session.commit()
    flash("Report marked as resolved.", "success")
    return redirect(url_for("admin.dashboard"))
