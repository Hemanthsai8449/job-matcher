from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import Job
from app.services.matching_service import score_job


def make_job(**overrides):
    now = datetime.now(UTC)
    values = {
        "provider": "test",
        "provider_job_id": "job-1",
        "fingerprint": "f" * 64,
        "title": "Graduate Python Developer",
        "company": "Example Labs",
        "location": "Bengaluru",
        "workplace_type": "hybrid",
        "job_type": "full_time",
        "description": "Build Python services and work with SQL and Git.",
        "skills": ["Python", "SQL", "Git", "REST APIs"],
        "minimum_experience": 0,
        "graduate_friendly": True,
        "posted_at": now - timedelta(days=1),
        "expires_at": now + timedelta(days=20),
        "apply_url": "https://jobs.example.com/apply/1",
        "canonical_url": "https://jobs.example.com/apply/1",
        "source_label": "Test employer board",
        "source_checked_at": now,
        "last_seen_at": now,
        "trust_score": 90,
    }
    values.update(overrides)
    return Job(**values)


def test_explainable_match_highlights_strengths_and_gaps(app, user_factory):
    user = user_factory()
    job = make_job()
    db.session.add(job)
    db.session.commit()

    result = score_job(user, job)

    assert result.eligible is True
    assert result.score >= 70
    assert {"Python", "SQL", "Git"}.issubset(set(result.matched_skills))
    assert "REST APIs" in result.missing_skills
    assert result.reasons


def test_senior_role_is_filtered_for_a_fresher(app, user_factory):
    user = user_factory(experience_years=0)
    job = make_job(title="Senior Python Developer")

    result = score_job(user, job)

    assert result.eligible is False
    assert result.score == 0
    assert "seniority" in result.concerns[0].lower()


def test_remote_role_restricted_to_another_country_is_filtered(app, user_factory):
    user = user_factory(preferred_locations=["Bengaluru", "Remote"])
    job = make_job(
        location="Remote - United States only",
        workplace_type="remote",
    )

    result = score_job(user, job)

    assert result.eligible is False
    assert "location" in result.concerns[0].lower()


def test_remote_india_role_is_eligible_for_india_student(app, user_factory):
    user = user_factory(preferred_locations=["Bengaluru", "Remote"])
    job = make_job(location="Remote - India", workplace_type="remote")

    result = score_job(user, job)

    assert result.eligible is True
    assert any("remote location" in reason.lower() for reason in result.reasons)


def test_onsite_city_outside_preferences_is_filtered(app, user_factory):
    user = user_factory(preferred_locations=["Bengaluru", "Remote"])
    job = make_job(location="Mumbai", workplace_type="onsite")

    result = score_job(user, job)

    assert result.eligible is False
    assert "location" in result.concerns[0].lower()


def test_explicit_graduation_year_requirement_is_enforced(app, user_factory):
    user = user_factory(graduation_year=2026)
    job = make_job(
        description="Applicants must be from the graduating batch of 2024 or 2025. Python required."
    )

    result = score_job(user, job)

    assert result.eligible is False
    assert "graduation" in result.concerns[0].lower()
