from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, select

from ..extensions import db
from ..models import Job, JobMatch, SentJob, User
from ..utils import utcnow
from .resume_service import SKILL_ALIASES


@dataclass(frozen=True)
class MatchResult:
    eligible: bool
    score: int
    matched_skills: list[str]
    missing_skills: list[str]
    reasons: list[str]
    concerns: list[str]


SENIOR_TITLE_TERMS = {
    "senior",
    "sr",
    "lead",
    "principal",
    "manager",
    "architect",
    "director",
    "head",
}

JOB_TYPE_ALIASES = {
    "full-time": "full_time",
    "full time": "full_time",
    "full_time": "full_time",
    "part-time": "part_time",
    "part time": "part_time",
    "part_time": "part_time",
    "intern": "internship",
    "internship": "internship",
    "contract": "contract",
    "temporary": "temporary",
}

INDIA_LOCATION_TERMS = {
    "india",
    "bengaluru",
    "bangalore",
    "hyderabad",
    "pune",
    "chennai",
    "mumbai",
    "gurugram",
    "gurgaon",
    "noida",
    "delhi",
    "kolkata",
    "ahmedabad",
    "kochi",
    "cochin",
}

LOCATION_ALIAS_GROUPS = (
    {"bengaluru", "bangalore"},
    {"gurugram", "gurgaon"},
    {"kochi", "cochin"},
    {"delhi", "new delhi", "ncr"},
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#]+", value.casefold()))


def normalize_skill(value: str) -> str:
    key = re.sub(r"\s+", " ", value.strip().casefold())
    return SKILL_ALIASES.get(key, value.strip()).casefold()


def _skill_map(values: Iterable[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values or []:
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = normalize_skill(value)
        display = SKILL_ALIASES.get(value.strip().casefold(), value.strip())
        result.setdefault(normalized, display)
    return result


def _contains_term(value: str, term: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", value))


def _is_india_location(value: str) -> bool:
    return any(_contains_term(value, term) for term in INDIA_LOCATION_TERMS)


def _direct_location_match(preferred_locations: list[str], job_location: str) -> bool:
    concrete_preferences = [location for location in preferred_locations if location != "remote"]
    if any(
        preferred in job_location or job_location in preferred
        for preferred in concrete_preferences
    ):
        return True
    for aliases in LOCATION_ALIAS_GROUPS:
        if any(_contains_term(job_location, alias) for alias in aliases) and any(
            any(_contains_term(preferred, alias) for alias in aliases)
            for preferred in concrete_preferences
        ):
            return True
    if _contains_term(job_location, "india") and any(
        _is_india_location(preferred) for preferred in concrete_preferences
    ):
        return True
    if any(_contains_term(preferred, "india") for preferred in concrete_preferences):
        return _is_india_location(job_location)
    return False


def _location_compatible(user: User, job: Job) -> bool:
    preferred_locations = [
        str(location).strip().casefold()
        for location in (user.preferred_locations or [])
        if str(location).strip()
    ]
    if not preferred_locations:
        return True
    job_location = str(job.location or "").strip().casefold()
    if _direct_location_match(preferred_locations, job_location):
        return True
    if job.workplace_type.casefold() != "remote" or "remote" not in preferred_locations:
        return False
    if not job_location or job_location in {"remote", "not specified", "worldwide", "anywhere"}:
        return True
    if any(term in job_location for term in ("worldwide", "anywhere", "global")):
        return True
    if "location restrictions may apply" in job_location:
        return True
    india_user = (
        str(user.timezone or "") == "Asia/Kolkata"
        or str(user.phone_e164 or "").startswith("+91")
        or any(_is_india_location(location) for location in preferred_locations)
    )
    return india_user and _is_india_location(job_location)


def _explicit_graduation_years(description: str) -> set[int]:
    years: set[int] = set()
    for context in re.findall(
        r"(?:graduat(?:ing|ion)?(?:\s+year)?|batch|class\s+of)[^.\n]{0,90}",
        description.casefold(),
    ):
        years.update(
            int(value)
            for value in re.findall(r"\b20\d{2}\b", context)
            if 2000 <= int(value) <= 2100
        )
    return years


def _hard_filter(user: User, job: Job, now: datetime) -> str | None:
    if job.is_active is False:
        return "This job is no longer active."
    expires_at = _aware(job.expires_at)
    if expires_at is not None and expires_at <= now:
        return "This job has expired."
    if int(job.trust_score or 0) < 40 or int(job.reported_count or 0) >= 3:
        return "This job is withheld while its trust signals are reviewed."

    experience = max(0.0, float(user.experience_years or 0.0))
    required_experience = max(0.0, float(job.minimum_experience or 0.0))
    if required_experience > experience:
        return f"This role asks for at least {required_experience:g} years of experience."
    title_words = _words(job.title)
    if experience < 2 and title_words & SENIOR_TITLE_TERMS:
        return "The seniority in this title is not graduate-friendly."
    if not job.graduate_friendly and experience < 2:
        return "The employer has not marked this role as graduate-friendly."

    preferred_job_types = {
        JOB_TYPE_ALIASES.get(str(item).casefold(), str(item).casefold())
        for item in (user.job_types or [])
    }
    normalized_job_type = JOB_TYPE_ALIASES.get(job.job_type.casefold(), job.job_type.casefold())
    if preferred_job_types and normalized_job_type not in preferred_job_types:
        return "This employment type is outside your preferences."
    preferred_modes = {str(item).strip().casefold() for item in (user.work_modes or [])}
    if preferred_modes and job.workplace_type.casefold() not in preferred_modes:
        return "This workplace type is outside your preferences."
    if not _location_compatible(user, job):
        return "This job's stated location is outside your preferences."
    allowed_graduation_years = _explicit_graduation_years(job.description or "")
    if (
        user.graduation_year
        and allowed_graduation_years
        and int(user.graduation_year) not in allowed_graduation_years
    ):
        return "This role names graduation years that do not include your profile."
    return None


def score_job(user: User, job: Job, *, now: datetime | None = None) -> MatchResult:
    current_time = _aware(now or utcnow())
    assert current_time is not None
    blocked_reason = _hard_filter(user, job, current_time)
    if blocked_reason:
        return MatchResult(False, 0, [], [], [], [blocked_reason])

    reasons: list[str] = []
    concerns: list[str] = []
    score = 0

    user_skills = _skill_map(user.skills)
    job_skills = _skill_map(job.skills)
    if job_skills:
        matched_keys = sorted(user_skills.keys() & job_skills.keys())
        missing_keys = sorted(job_skills.keys() - user_skills.keys())
        coverage = len(matched_keys) / len(job_skills)
        score += round(40 * coverage)
        matched = [job_skills[key] for key in matched_keys]
        missing = [job_skills[key] for key in missing_keys]
        if matched:
            reasons.append(f"Matches {len(matched)} of {len(job_skills)} listed skills.")
        if missing:
            concerns.append("Skills to strengthen: " + ", ".join(missing[:4]) + ".")
    else:
        matched = []
        missing = []
        score += 24
        concerns.append("The employer did not publish a detailed skill list.")

    desired_roles = [str(role).strip() for role in (user.desired_roles or []) if str(role).strip()]
    title = job.title.casefold()
    if not desired_roles:
        score += 12
    elif any(role.casefold() in title for role in desired_roles):
        score += 20
        reasons.append("The job title matches a desired role.")
    else:
        title_tokens = _words(title)
        overlap = max((len(title_tokens & _words(role)) for role in desired_roles), default=0)
        if overlap:
            score += 13
            reasons.append("The role is related to one of your preferred job titles.")
        else:
            score += 3
            concerns.append("The title is outside your main role preferences.")

    preferred_modes = {str(mode).casefold() for mode in (user.work_modes or [])}
    if not preferred_modes or job.workplace_type.casefold() in preferred_modes:
        score += 8
        reasons.append(f"{job.workplace_type.title()} work fits your preference.")

    score += 7
    reasons.append(
        "The remote location is compatible with your preference."
        if job.workplace_type.casefold() == "remote"
        else "The location matches your preference."
    )

    required_experience = max(0.0, float(job.minimum_experience or 0.0))
    score += 15
    if required_experience == 0:
        reasons.append("No prior professional experience is required.")
    else:
        reasons.append("Your experience meets the stated minimum.")

    posted_at = _aware(job.posted_at)
    if posted_at is None:
        score += 2
    else:
        age_days = max(0, (current_time - posted_at).days)
        if age_days <= 7:
            score += 5
            reasons.append("Recently posted.")
        elif age_days <= 30:
            score += 3
        else:
            score += 1
            concerns.append("This listing is more than 30 days old.")

    score += round(max(0, min(100, int(job.trust_score or 0))) * 0.05)
    if int(job.trust_score or 0) >= 80:
        reasons.append("Published through a trusted employer or authorized source.")

    allowed_graduation_years = _explicit_graduation_years(job.description or "")
    if user.graduation_year and int(user.graduation_year) in allowed_graduation_years:
        reasons.append("Your graduation year matches the stated eligibility window.")
    if user.salary_preference:
        if job.salary:
            reasons.append("Salary information is available for comparison with your preference.")
        else:
            concerns.append("The employer did not publish salary information.")

    return MatchResult(
        eligible=True,
        score=max(0, min(100, score)),
        matched_skills=matched,
        missing_skills=missing,
        reasons=reasons[:6],
        concerns=concerns[:5],
    )


def upsert_match(
    user: User,
    job: Job,
    *,
    now: datetime | None = None,
    commit: bool = False,
) -> JobMatch:
    result = score_job(user, job, now=now)
    match = JobMatch.query.filter_by(user_id=user.id, job_id=job.id).first()
    if match is None:
        match = JobMatch(user_id=user.id, job_id=job.id, score=result.score)
        db.session.add(match)
    match.score = result.score
    match.matched_skills = result.matched_skills
    match.missing_skills = result.missing_skills
    match.reasons = result.reasons
    match.concerns = result.concerns
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return match


def rebuild_matches(
    user: User,
    jobs: Iterable[Job] | None = None,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> list[JobMatch]:
    # Serialize same-user rebuilds before querying/inserting the unique
    # (user_id, job_id) rows. The no-op write also supplies SQLite's write lock.
    db.session.execute(
        db.update(User).where(User.id == user.id).values(updated_at=User.updated_at)
    )
    locked_user = db.session.get(User, int(user.id), populate_existing=True)
    if locked_user is None:
        raise ValueError("The user no longer exists.")
    user = locked_user
    job_items = list(jobs) if jobs is not None else Job.query.all()
    matches = [upsert_match(user, job, now=now, commit=False) for job in job_items]
    if commit:
        db.session.commit()
    return matches


def ranked_matches(
    user: User,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    exclude_sent: bool = False,
) -> list[JobMatch]:
    current_time = _aware(now or utcnow())
    assert current_time is not None
    statement = (
        select(JobMatch)
        .join(Job, Job.id == JobMatch.job_id)
        .where(
            JobMatch.user_id == user.id,
            JobMatch.score >= max(0, min(100, int(user.minimum_match_score or 0))),
            Job.is_active.is_(True),
            Job.trust_score >= 40,
            Job.reported_count < 3,
            (Job.expires_at.is_(None) | (Job.expires_at > current_time)),
        )
        .order_by(
            JobMatch.score.desc(),
            case((Job.posted_at.is_(None), 1), else_=0),
            Job.posted_at.desc(),
            Job.id.asc(),
        )
    )
    if exclude_sent:
        statement = statement.where(
            ~select(SentJob.id)
            .where(SentJob.user_id == user.id, SentJob.job_id == Job.id)
            .exists()
        )
    if limit is not None:
        statement = statement.limit(max(0, int(limit)))
    return list(db.session.scalars(statement).all())


# Compatibility names for route modules.
calculate_match = score_job
compute_match = score_job
