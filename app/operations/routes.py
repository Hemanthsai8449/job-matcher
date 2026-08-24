from __future__ import annotations

import hmac

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..extensions import db, limiter
from ..services.alert_service import dispatch_due_alerts
from ..services.file_cleanup import process_pending_private_files
from ..services.job_sources import fetch_and_upsert_all

bp = Blueprint("operations", __name__)


@bp.before_request
def authenticate_scheduled_operation():
    if request.endpoint == "operations.health":
        return None
    secret = str(current_app.config.get("CRON_SECRET", ""))
    if not secret:
        return jsonify(error="Scheduler authentication is not configured."), 503
    expected = f"Bearer {secret}"
    supplied = request.headers.get("Authorization", "")
    if not hmac.compare_digest(supplied, expected):
        return jsonify(error="Unauthorized scheduler request."), 401
    return None


@bp.after_request
def disable_operation_caching(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/health")
@limiter.exempt
def health():
    try:
        db.session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Database health check failed")
        return jsonify(status="unavailable", database="unavailable"), 503
    return jsonify(status="ok", database="ok")


@bp.get("/operations/cron/sync-jobs")
@limiter.exempt
def sync_jobs():
    return jsonify(status="ok", operation="sync-jobs", result=fetch_and_upsert_all())


@bp.get("/operations/cron/dispatch-alerts")
@limiter.exempt
def dispatch_alerts():
    return jsonify(status="ok", operation="dispatch-alerts", result=dispatch_due_alerts())


@bp.get("/operations/cron/cleanup-private-files")
@limiter.exempt
def cleanup_private_files():
    return jsonify(
        status="ok",
        operation="cleanup-private-files",
        result=process_pending_private_files(),
    )
