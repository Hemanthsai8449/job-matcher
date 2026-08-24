from __future__ import annotations

from pathlib import Path

from flask import current_app

from ..extensions import db
from ..models import PrivateFileCleanup
from ..utils import utcnow


def is_discarded_private_file(stored_name: str) -> bool:
    """Return whether a resume reference intentionally has no backing file."""

    return str(stored_name).replace("\\", "/").startswith("discarded/")


def queue_private_file(stored_name: str) -> PrivateFileCleanup:
    """Persist cleanup intent in the same transaction that removes its DB owner."""

    relative = Path(str(stored_name))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Invalid private file path.")
    record = PrivateFileCleanup(stored_name=relative.as_posix(), status="pending")
    db.session.add(record)
    return record


def process_private_file(cleanup_id: int, *, commit: bool = True) -> bool:
    """Erase one queued file safely; a missing file counts as completed cleanup."""

    record = db.session.get(PrivateFileCleanup, cleanup_id)
    if record is None or record.status == "completed":
        return True
    if is_discarded_private_file(record.stored_name):
        record.status = "completed"
        record.last_error = ""
        record.completed_at = utcnow()
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return True
    upload_root = Path(current_app.config["UPLOAD_FOLDER"]).resolve()
    target = (upload_root / record.stored_name).resolve()
    record.attempts = int(record.attempts or 0) + 1
    if upload_root not in target.parents:
        record.status = "failed"
        record.last_error = "Unsafe stored path was rejected."
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return False
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        record.status = "failed"
        record.last_error = f"{exc.__class__.__name__}: private file cleanup failed"[:160]
        success = False
    else:
        record.status = "completed"
        record.last_error = ""
        record.completed_at = utcnow()
        success = True
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return success


def process_pending_private_files(*, commit: bool = True) -> dict[str, int]:
    records = db.session.scalars(
        db.select(PrivateFileCleanup)
        .where(PrivateFileCleanup.status.in_(["pending", "failed"]))
        .order_by(PrivateFileCleanup.created_at.asc(), PrivateFileCleanup.id.asc())
    ).all()
    summary = {"checked": len(records), "deleted": 0, "failed": 0}
    for record in records:
        if process_private_file(record.id, commit=commit):
            summary["deleted"] += 1
        else:
            summary["failed"] += 1
    return summary


__all__ = [
    "is_discarded_private_file",
    "process_pending_private_files",
    "process_private_file",
    "queue_private_file",
]
