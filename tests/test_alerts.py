from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models import Job, JobMatch, SentJob
from app.services.alert_service import dispatch_user_alerts


def test_alerts_never_repeat_and_respect_total_daily_limit(app, user_factory):
    now = datetime.now(UTC).replace(hour=10, minute=0, second=0, microsecond=0)
    user = user_factory(
        telegram_chat_id="999001",
        telegram_user_id="999001",
        phone_verified_at=now,
        alert_status="active",
        active_until=now + timedelta(days=7),
        daily_job_limit=3,
        preferred_time="09:00",
        timezone="UTC",
    )
    for index in range(5):
        job = Job(
            provider="test",
            provider_job_id=f"job-{index}",
            fingerprint=f"{index:064x}",
            title=f"Graduate Python Developer {index}",
            company="Example Labs",
            location="Remote",
            workplace_type="remote",
            job_type="full_time",
            description="Python SQL Git graduate role",
            skills=["Python", "SQL", "Git"],
            minimum_experience=0,
            graduate_friendly=True,
            posted_at=now - timedelta(hours=index),
            expires_at=now + timedelta(days=20),
            apply_url=f"https://jobs.example.com/apply/{index}",
            canonical_url=f"https://jobs.example.com/apply/{index}",
            source_label="Test employer board",
            source_checked_at=now,
            last_seen_at=now,
            trust_score=90,
        )
        db.session.add(job)
        db.session.flush()
        db.session.add(
            JobMatch(
                user_id=user.id,
                job_id=job.id,
                score=90 - index,
                matched_skills=["Python", "SQL", "Git"],
                missing_skills=[],
                reasons=["Matches confirmed skills."],
                concerns=[],
            )
        )
    db.session.commit()

    sent_payloads = []

    def sender(chat_id, text, **kwargs):
        sent_payloads.append((chat_id, text, kwargs))
        return {"ok": True, "result": {"message_id": len(sent_payloads)}}

    first = dispatch_user_alerts(user, now=now, sender=sender)
    second = dispatch_user_alerts(user, now=now + timedelta(minutes=5), sender=sender)

    assert first["sent"] == 3
    assert second["sent"] == 0
    deliveries = db.session.scalars(db.select(SentJob).where(SentJob.user_id == user.id)).all()
    assert len(deliveries) == 3
    assert len({delivery.job_id for delivery in deliveries}) == 3


def test_alert_dispatch_rejects_non_durable_reservations(app, user_factory):
    user = user_factory()

    with pytest.raises(ValueError, match="durable database reservations"):
        dispatch_user_alerts(user, commit=False)
