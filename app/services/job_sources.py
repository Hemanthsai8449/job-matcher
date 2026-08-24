from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import requests
from bs4 import BeautifulSoup
from flask import current_app

from ..extensions import db
from ..models import AuditLog, Job, User
from ..utils import canonicalize_url, utcnow
from .resume_service import SKILL_ALIASES

USER_AGENT = "JobMatcher/0.1 (authorized public job-feed client)"
JOB_SOURCE_RESPONSE_LIMIT = 5 * 1024 * 1024
SOURCE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
SENIOR_TITLE_WORDS = {
    "architect",
    "director",
    "head",
    "lead",
    "manager",
    "principal",
    "senior",
    "sr",
}
SUSPICIOUS_PHRASES = {
    "application fee",
    "pay to apply",
    "processing fee",
    "registration fee",
    "security deposit",
    "training fee",
}


class HTTPClient(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


class JobSourceError(RuntimeError):
    """A configured, authorized JSON feed could not be read safely."""


@dataclass(frozen=True)
class JobPayload:
    provider: str
    provider_job_id: str
    title: str
    company: str
    location: str
    workplace_type: str
    job_type: str
    description: str
    skills: list[str]
    minimum_experience: float
    graduate_friendly: bool
    salary: str
    posted_at: datetime | None
    expires_at: datetime | None
    apply_url: str
    canonical_url: str
    source_label: str
    trust_score: int
    is_demo: bool = False


def _timeout() -> tuple[float, float]:
    connect = float(current_app.config.get("JOB_SOURCE_CONNECT_TIMEOUT", 3.05))
    read = float(current_app.config.get("JOB_SOURCE_READ_TIMEOUT", 12.0))
    return max(0.25, connect), max(0.5, read)


def _http_json(
    client: HTTPClient,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
) -> Any:
    response: Any | None = None
    try:
        response = client.get(
            url,
            params=dict(params or {}),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=_timeout(),
            stream=True,
        )
        response.raise_for_status()
        headers = getattr(response, "headers", {})
        content_length = str(headers.get("Content-Length", "")) if headers else ""
        if content_length.isdigit() and int(content_length) > JOB_SOURCE_RESPONSE_LIMIT:
            raise JobSourceError("The job source returned an oversized response.")
        if isinstance(response, requests.Response):
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=32_768):
                if not chunk:
                    continue
                size += len(chunk)
                if size > JOB_SOURCE_RESPONSE_LIMIT:
                    raise JobSourceError("The job source returned an oversized response.")
                chunks.append(chunk)
            return json.loads(b"".join(chunks).decode("utf-8"))
        content = getattr(response, "content", b"")
        if isinstance(content, bytes) and len(content) > JOB_SOURCE_RESPONSE_LIMIT:
            raise JobSourceError("The job source returned an oversized response.")
        return response.json()
    except (requests.RequestException, AttributeError, ValueError, TypeError) as exc:
        # Deliberately omit the URL because Adzuna credentials are query parameters.
        raise JobSourceError("The job source returned an invalid response.") from exc
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _clean_text(value: Any, *, maximum: int = 50_000) -> str:
    if value is None:
        return ""
    raw = html.unescape(html.unescape(str(value)))
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:maximum]


def _required_text(value: Any, fallback: str, maximum: int) -> str:
    return (_clean_text(value, maximum=maximum) or fallback)[:maximum]


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1_000
        try:
            return datetime.fromtimestamp(timestamp, UTC)
        except (OSError, OverflowError, ValueError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _safe_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("The source did not provide an application URL.")
    return canonicalize_url(raw)


def _normalize_job_type(value: Any, *, title: str = "", description: str = "") -> str:
    compact = re.sub(r"[^a-z]", "", str(value or "").casefold())
    mapping = {
        "contract": "contract",
        "contractor": "contract",
        "fulltime": "full_time",
        "intern": "internship",
        "internship": "internship",
        "parttime": "part_time",
        "temporary": "temporary",
        "volunteer": "volunteer",
    }
    if compact in mapping:
        return mapping[compact]
    sample = f"{title} {description[:500]}".casefold()
    if re.search(r"\b(intern|internship|trainee|apprentice)\b", sample):
        return "internship"
    if re.search(r"\b(contract|contractor)\b", sample):
        return "contract"
    if re.search(r"\bpart[ -]?time\b", sample):
        return "part_time"
    return "full_time"


def _normalize_workplace(
    value: Any,
    *,
    location: str,
    is_remote: bool | None = None,
) -> str:
    if is_remote is True:
        return "remote"
    compact = re.sub(r"[^a-z]", "", str(value or "").casefold())
    if compact in {"remote", "fullyremote"}:
        return "remote"
    if compact in {"hybrid", "hybridremote"}:
        return "hybrid"
    if compact in {"onsite", "inperson"}:
        return "onsite"
    location_text = location.casefold()
    if re.search(r"\b(remote|worldwide|anywhere)\b", location_text):
        return "remote"
    if "hybrid" in location_text:
        return "hybrid"
    return "onsite"


def _extract_skills(text: str) -> list[str]:
    lowered = text.casefold()
    found: dict[str, str] = {}
    for alias, canonical in sorted(SKILL_ALIASES.items(), key=lambda item: -len(item[0])):
        pattern = rf"(?<![a-z0-9]){re.escape(alias.casefold())}(?![a-z0-9])"
        if re.search(pattern, lowered):
            found.setdefault(canonical.casefold(), canonical)
    return list(found.values())[:14]


def _minimum_experience(text: str) -> float:
    lowered = text.casefold()
    patterns = (
        r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:\+|[-–]\s*\d+(?:\.\d+)?|to\s+\d+(?:\.\d+)?)?"
        r"\s*years?(?:\s+of)?\s+(?:relevant\s+|professional\s+)?experience",
        r"(?:experience|experienced)\s*(?:of|:|–|-)?\s*(?:at\s+least\s+)?"
        r"(\d+(?:\.\d+)?)\s*(?:\+|[-–]\s*\d+(?:\.\d+)?|to\s+\d+(?:\.\d+)?)?"
        r"\s*years?",
    )
    values: list[float] = []
    for pattern in patterns:
        for match in re.finditer(pattern, lowered):
            try:
                amount = float(match.group(1))
            except (TypeError, ValueError):
                continue
            if 0 <= amount <= 30:
                values.append(amount)
    # When a description lists multiple requirements, use the strictest explicit
    # minimum. Underestimating seniority is unsafe for graduate recommendations.
    return max(values, default=0.0)


def _graduate_friendly(title: str, minimum_experience: float, text: str) -> bool:
    title_words = set(re.findall(r"[a-z0-9]+", title.casefold()))
    if title_words & SENIOR_TITLE_WORDS:
        return False
    if minimum_experience > 2:
        return False
    lowered = text.casefold()
    return not bool(re.search(r"\bnot (?:an )?entry[- ]level\b", lowered))


def _trust_score(base: int, description: str) -> int:
    lowered = description.casefold()
    disclaimer_pattern = (
        r"(?:no|never|not|do not|does not|will not|won't)\s+(?:ask[^.]{0,30}|charge[^.]{0,20})"
        r"(?:application|processing|registration|security|training) fee"
    )
    suspicious_text = re.sub(disclaimer_pattern, "", lowered)
    penalty = 50 if any(phrase in suspicious_text for phrase in SUSPICIOUS_PHRASES) else 0
    return max(0, min(100, base - penalty))


def _salary_range(
    minimum: Any,
    maximum: Any,
    *,
    currency: Any = "",
    interval: Any = "",
) -> str:
    if minimum in (None, "") and maximum in (None, ""):
        return ""
    unit = _clean_text(currency, maximum=8).upper()
    try:
        low = f"{float(minimum):,.0f}" if minimum not in (None, "") else ""
        high = f"{float(maximum):,.0f}" if maximum not in (None, "") else ""
    except (TypeError, ValueError):
        return ""
    amount = f"{low}–{high}" if low and high else low or high
    suffix = _clean_text(interval, maximum=20)
    return " ".join(part for part in (unit, amount, suffix) if part)[:120]


def _configured_sources(value: Any) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for raw_entry in str(value or "").split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        separator = "|" if "|" in entry else "=" if "=" in entry else None
        if separator:
            identifier, label = (part.strip() for part in entry.split(separator, 1))
        else:
            identifier, label = entry, re.sub(r"[-_]", " ", entry).title()
        if not SOURCE_IDENTIFIER.fullmatch(identifier):
            continue
        result.append((identifier, (label or identifier)[:180]))
    return list(dict.fromkeys(result))


def _fingerprint(company: str, title: str, location: str) -> str:
    def normalized(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

    company_key = normalized(company)
    company_key = re.sub(
        r"\b(?:incorporated|inc|limited|ltd|llc|private|pvt|corp|corporation)\b",
        "",
        company_key,
    )
    location_key = normalized(location)
    if any(word in location_key for word in ("anywhere", "remote", "worldwide")):
        location_key = "remote"
    identity = "|".join((re.sub(r"\s+", " ", company_key).strip(), normalized(title), location_key))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _manually_disabled(job: Job) -> bool:
    if job.id is None or job.is_active:
        return False
    latest_toggle = db.session.scalar(
        db.select(AuditLog.event)
        .where(
            AuditLog.entity_type == "job",
            AuditLog.entity_id == str(job.id),
            AuditLog.event.in_(("job.activated", "job.deactivated")),
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    return latest_toggle == "job.deactivated"


def _refresh_job_fields(
    job: Job,
    payload: JobPayload,
    *,
    now: datetime,
    cross_provider: bool = False,
) -> None:
    manually_disabled = _manually_disabled(job)
    values = asdict(payload)
    for field in (
        "title",
        "company",
        "location",
        "workplace_type",
        "job_type",
        "description",
        "skills",
        "minimum_experience",
        "graduate_friendly",
        "salary",
        "posted_at",
        "expires_at",
        "apply_url",
        "canonical_url",
    ):
        setattr(job, field, values[field])
    if not cross_provider:
        job.source_label = payload.source_label
        job.trust_score = payload.trust_score
        job.is_demo = payload.is_demo
    else:
        job.trust_score = max(int(job.trust_score or 0), payload.trust_score)
        job.source_label = payload.source_label
    feed_active = payload.expires_at is None or _as_utc(payload.expires_at) > now
    if not manually_disabled:
        job.is_active = feed_active
    job.last_seen_at = now
    job.source_checked_at = now


def _finish_payload(
    *,
    provider: str,
    provider_job_id: Any,
    title: Any,
    company: Any,
    location: Any,
    workplace_type: Any,
    job_type: Any,
    description: Any,
    salary: Any,
    posted_at: Any,
    expires_at: Any,
    apply_url: Any,
    canonical_url: Any,
    source_label: str,
    trust_score: int,
    is_remote: bool | None = None,
) -> JobPayload:
    external_id = str(provider_job_id or "").strip()
    if not external_id or external_id.endswith(":"):
        raise ValueError("The source did not provide a stable job identifier.")
    clean_title = _required_text(title, "Untitled role", 220)
    clean_company = _required_text(company, "Employer not specified", 180)
    clean_location = _required_text(location, "Not specified", 180)
    clean_description = _required_text(
        description,
        "The employer did not publish a detailed description in this feed.",
        50_000,
    )
    clean_apply_url = _safe_url(apply_url or canonical_url)
    clean_canonical_url = _safe_url(canonical_url or apply_url)
    experience = _minimum_experience(clean_description)
    return JobPayload(
        provider=provider[:40],
        provider_job_id=external_id[:160],
        title=clean_title,
        company=clean_company,
        location=clean_location,
        workplace_type=_normalize_workplace(
            workplace_type,
            location=clean_location,
            is_remote=is_remote,
        ),
        job_type=_normalize_job_type(
            job_type,
            title=clean_title,
            description=clean_description,
        ),
        description=clean_description,
        skills=_extract_skills(f"{clean_title} {clean_description}"),
        minimum_experience=experience,
        graduate_friendly=_graduate_friendly(
            clean_title,
            experience,
            clean_description,
        ),
        salary=_clean_text(salary, maximum=120),
        posted_at=_parse_datetime(posted_at),
        expires_at=_parse_datetime(expires_at),
        apply_url=clean_apply_url,
        canonical_url=clean_canonical_url,
        source_label=source_label[:80],
        trust_score=_trust_score(trust_score, clean_description),
    )


def fetch_adzuna_jobs(*, client: HTTPClient | None = None) -> list[JobPayload]:
    app_id = str(current_app.config.get("ADZUNA_APP_ID", "")).strip()
    app_key = str(current_app.config.get("ADZUNA_APP_KEY", "")).strip()
    country = str(current_app.config.get("ADZUNA_COUNTRY", "in")).strip().casefold()
    if not app_id or not app_key or not re.fullmatch(r"[a-z]{2}", country):
        return []
    http = client or requests
    data = _http_json(
        http,
        f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
        params={
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": 50,
            "what_or": "graduate junior entry level internship",
            "content-type": "application/json",
        },
    )
    if not isinstance(data, Mapping) or not isinstance(data.get("results"), list):
        raise JobSourceError("Adzuna returned an unexpected payload.")
    jobs: list[JobPayload] = []
    for item in data["results"]:
        if not isinstance(item, Mapping):
            continue
        external_id = item.get("id")
        if external_id in (None, ""):
            continue
        try:
            description = _clean_text(item.get("description"))
            salary = _salary_range(
                item.get("salary_min"),
                item.get("salary_max"),
                currency="INR" if country == "in" else "",
                interval="per year",
            )
            jobs.append(
                _finish_payload(
                    provider="adzuna",
                    provider_job_id=f"{country}:{external_id}",
                    title=item.get("title"),
                    company=(item.get("company") or {}).get("display_name"),
                    location=(item.get("location") or {}).get("display_name"),
                    workplace_type="",
                    job_type=item.get("contract_time") or item.get("contract_type"),
                    description=description,
                    salary=salary,
                    posted_at=item.get("created"),
                    expires_at=None,
                    apply_url=item.get("redirect_url"),
                    canonical_url=item.get("redirect_url"),
                    source_label="Adzuna authorized API",
                    trust_score=76,
                )
            )
        except (AttributeError, TypeError, ValueError):
            continue
    return jobs


def fetch_himalayas_jobs(*, client: HTTPClient | None = None) -> list[JobPayload]:
    if not current_app.config.get("ENABLE_HIMALAYAS", True):
        return []
    http = client or requests
    data = _http_json(
        http,
        "https://himalayas.app/jobs/api",
        params={"limit": 20, "offset": 0},
    )
    if not isinstance(data, Mapping) or not isinstance(data.get("jobs"), list):
        raise JobSourceError("Himalayas returned an unexpected payload.")
    jobs: list[JobPayload] = []
    for item in data["jobs"]:
        if not isinstance(item, Mapping):
            continue
        restrictions = item.get("locationRestrictions") or []
        locations = [
            str(location.get("name", "")).strip()
            for location in restrictions
            if isinstance(location, Mapping) and location.get("name")
        ]
        location_label = ", ".join(locations[:5]) or "Remote — location restrictions may apply"
        salary = _salary_range(
            item.get("minSalary"),
            item.get("maxSalary"),
            currency=item.get("currency"),
            interval="per year",
        )
        try:
            jobs.append(
                _finish_payload(
                    provider="himalayas",
                    provider_job_id=item.get("guid"),
                    title=item.get("title"),
                    company=item.get("companyName"),
                    location=location_label,
                    workplace_type="remote",
                    job_type=item.get("employmentType"),
                    description=item.get("description") or item.get("excerpt"),
                    salary=salary,
                    posted_at=item.get("pubDate"),
                    expires_at=item.get("expiryDate"),
                    apply_url=item.get("applicationLink"),
                    canonical_url=item.get("applicationLink"),
                    source_label="Himalayas public API",
                    trust_score=82,
                    is_remote=True,
                )
            )
        except (TypeError, ValueError):
            continue
    return jobs


def fetch_greenhouse_jobs(
    *,
    client: HTTPClient | None = None,
    errors: list[str] | None = None,
) -> list[JobPayload]:
    boards = _configured_sources(current_app.config.get("GREENHOUSE_BOARDS", ""))
    http = client or requests
    jobs: list[JobPayload] = []
    for board, company in boards:
        try:
            data = _http_json(
                http,
                f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                params={"content": "true"},
            )
            if not isinstance(data, Mapping) or not isinstance(data.get("jobs"), list):
                raise JobSourceError("Greenhouse returned an unexpected payload.")
        except JobSourceError:
            if errors is None:
                raise
            errors.append(board)
            continue
        for item in data["jobs"]:
            if not isinstance(item, Mapping):
                continue
            external_id = item.get("id")
            if external_id in (None, ""):
                continue
            try:
                jobs.append(
                    _finish_payload(
                        provider="greenhouse",
                        provider_job_id=f"{board}:{external_id}",
                        title=item.get("title"),
                        company=company,
                        location=(item.get("location") or {}).get("name"),
                        workplace_type="",
                        job_type="",
                        description=item.get("content"),
                        salary="",
                        posted_at=item.get("updated_at"),
                        expires_at=None,
                        apply_url=item.get("absolute_url"),
                        canonical_url=item.get("absolute_url"),
                        source_label=f"Greenhouse — {company}",
                        trust_score=90,
                    )
                )
            except (AttributeError, TypeError, ValueError):
                continue
    return jobs


def fetch_lever_jobs(
    *,
    client: HTTPClient | None = None,
    errors: list[str] | None = None,
) -> list[JobPayload]:
    sites = _configured_sources(current_app.config.get("LEVER_SITES", ""))
    http = client or requests
    jobs: list[JobPayload] = []
    for site, company in sites:
        try:
            data = _http_json(
                http,
                f"https://api.lever.co/v0/postings/{site}",
                params={"mode": "json", "limit": 100},
            )
            if not isinstance(data, list):
                raise JobSourceError("Lever returned an unexpected payload.")
        except JobSourceError:
            if errors is None:
                raise
            errors.append(site)
            continue
        for item in data:
            if not isinstance(item, Mapping):
                continue
            external_id = item.get("id")
            if external_id in (None, ""):
                continue
            categories = item.get("categories") or {}
            salary_data = item.get("salaryRange") or {}
            if not isinstance(categories, Mapping):
                categories = {}
            if not isinstance(salary_data, Mapping):
                salary_data = {}
            salary = item.get("salaryDescriptionPlain") or _salary_range(
                salary_data.get("min"),
                salary_data.get("max"),
                currency=salary_data.get("currency"),
                interval=salary_data.get("interval"),
            )
            try:
                jobs.append(
                    _finish_payload(
                        provider="lever",
                        provider_job_id=f"{site}:{external_id}",
                        title=item.get("text"),
                        company=company,
                        location=categories.get("location"),
                        workplace_type=item.get("workplaceType"),
                        job_type=categories.get("commitment"),
                        description=(
                            item.get("descriptionPlain")
                            or item.get("description")
                            or item.get("additionalPlain")
                        ),
                        salary=salary,
                        posted_at=item.get("createdAt"),
                        expires_at=None,
                        apply_url=item.get("applyUrl") or item.get("hostedUrl"),
                        canonical_url=item.get("hostedUrl") or item.get("applyUrl"),
                        source_label=f"Lever — {company}",
                        trust_score=90,
                    )
                )
            except (AttributeError, TypeError, ValueError):
                continue
    return jobs


def fetch_ashby_jobs(
    *,
    client: HTTPClient | None = None,
    errors: list[str] | None = None,
) -> list[JobPayload]:
    boards = _configured_sources(current_app.config.get("ASHBY_BOARDS", ""))
    http = client or requests
    jobs: list[JobPayload] = []
    for board, company in boards:
        try:
            data = _http_json(
                http,
                f"https://api.ashbyhq.com/posting-api/job-board/{board}",
                params={"includeCompensation": "true"},
            )
            if not isinstance(data, Mapping) or not isinstance(data.get("jobs"), list):
                raise JobSourceError("Ashby returned an unexpected payload.")
        except JobSourceError:
            if errors is None:
                raise
            errors.append(board)
            continue
        for item in data["jobs"]:
            if not isinstance(item, Mapping) or item.get("isListed") is False:
                continue
            stable_url = str(item.get("jobUrl") or item.get("applyUrl") or "")
            if not stable_url:
                continue
            stable_id = hashlib.sha256(stable_url.encode("utf-8")).hexdigest()[:32]
            compensation = item.get("compensation") or {}
            if not isinstance(compensation, Mapping):
                compensation = {}
            salary = (
                compensation.get("scrapeableCompensationSalarySummary")
                or compensation.get("compensationTierSummary")
                or ""
            )
            try:
                jobs.append(
                    _finish_payload(
                        provider="ashby",
                        provider_job_id=f"{board}:{stable_id}",
                        title=item.get("title"),
                        company=company,
                        location=item.get("location"),
                        workplace_type=item.get("workplaceType"),
                        job_type=item.get("employmentType"),
                        description=item.get("descriptionPlain") or item.get("descriptionHtml"),
                        salary=salary,
                        posted_at=item.get("publishedAt"),
                        expires_at=None,
                        apply_url=item.get("applyUrl") or item.get("jobUrl"),
                        canonical_url=item.get("jobUrl") or item.get("applyUrl"),
                        source_label=f"Ashby — {company}",
                        trust_score=90,
                        is_remote=item.get("isRemote") if "isRemote" in item else None,
                    )
                )
            except (AttributeError, TypeError, ValueError):
                continue
    return jobs


def upsert_job(
    payload: JobPayload,
    *,
    now: datetime | None = None,
) -> tuple[Job, str]:
    """Insert or refresh a normalized job and return ``created``, ``updated`` or ``duplicate``."""

    current_time = _as_utc(now or utcnow())
    assert current_time is not None
    fingerprint = _fingerprint(payload.company, payload.title, payload.location)
    job = db.session.scalar(
        db.select(Job).where(
            Job.provider == payload.provider,
            Job.provider_job_id == payload.provider_job_id,
        )
    )
    if job is None:
        duplicate = db.session.scalar(db.select(Job).where(Job.fingerprint == fingerprint))
        if duplicate is not None:
            _refresh_job_fields(
                duplicate,
                payload,
                now=current_time,
                cross_provider=True,
            )
            return duplicate, "duplicate"
        job = Job(
            provider=payload.provider,
            provider_job_id=payload.provider_job_id,
            fingerprint=fingerprint,
            title=payload.title,
            company=payload.company,
            description=payload.description,
            apply_url=payload.apply_url,
            canonical_url=payload.canonical_url,
            source_label=payload.source_label,
        )
        db.session.add(job)
        outcome = "created"
    else:
        outcome = "updated"
        fingerprint_owner = db.session.scalar(
            db.select(Job).where(Job.fingerprint == fingerprint, Job.id != job.id)
        )
        if fingerprint_owner is not None:
            _refresh_job_fields(
                fingerprint_owner,
                payload,
                now=current_time,
                cross_provider=True,
            )
            job.is_active = False
            return fingerprint_owner, "duplicate"
        job.fingerprint = fingerprint

    _refresh_job_fields(job, payload, now=current_time)
    return job, outcome


def seed_demo_jobs(
    *,
    path: str | Path | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> int:
    """Load unmistakably fictional showcase jobs. They are never presented as live vacancies."""

    seed_path = (
        Path(path)
        if path
        else Path(__file__).resolve().parents[2] / "data" / "seed_jobs.json"
    )
    with seed_path.open("r", encoding="utf-8") as handle:
        records = json.load(handle)
    if not isinstance(records, list):
        raise ValueError("Demo job seed data must be a JSON list.")

    current_time = _as_utc(now or utcnow())
    assert current_time is not None
    created = 0
    for item in records:
        if not isinstance(item, Mapping):
            continue
        provider_id = str(item.get("provider_job_id", "")).strip()
        if not provider_id:
            continue
        title = _required_text(item.get("title"), "DEMO — Sample role", 220)
        company = _required_text(item.get("company"), "Fictional employer (DEMO)", 180)
        if "demo" not in title.casefold():
            title = f"DEMO — {title}"[:220]
        if "fictional" not in company.casefold() and "demo" not in company.casefold():
            company = f"{company} (fictional demo)"[:180]
        notice = "DEMONSTRATION ONLY — this is not a real vacancy and cannot accept applications."
        description = _clean_text(item.get("description"))
        if not description.startswith(notice):
            description = f"{notice} {description}".strip()
        posted_at = current_time - timedelta(days=max(0, int(item.get("posted_days_ago", 1))))
        expires_at = current_time + timedelta(
            days=max(1, int(item.get("expires_days_from_now", 30)))
        )
        payload = JobPayload(
            provider="demo",
            provider_job_id=provider_id[:160],
            title=title,
            company=company,
            location=_required_text(item.get("location"), "Demo location", 180),
            workplace_type=_normalize_workplace(
                item.get("workplace_type"),
                location=str(item.get("location", "")),
            ),
            job_type=_normalize_job_type(item.get("job_type"), title=title),
            description=description,
            skills=[str(skill)[:80] for skill in item.get("skills", []) if str(skill).strip()][
                :14
            ],
            minimum_experience=max(0.0, float(item.get("minimum_experience", 0))),
            graduate_friendly=bool(item.get("graduate_friendly", True)),
            salary=_clean_text(item.get("salary"), maximum=120),
            posted_at=posted_at,
            expires_at=expires_at,
            apply_url=_safe_url(item.get("apply_url")),
            canonical_url=_safe_url(item.get("canonical_url") or item.get("apply_url")),
            source_label="DEMO only — not a real vacancy",
            trust_score=85,
            is_demo=True,
        )
        _, outcome = upsert_job(payload, now=current_time)
        created += outcome == "created"
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return created


def fetch_and_upsert_all(
    *,
    client: HTTPClient | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Synchronize configured read-only JSON feeds without scraping career pages."""

    current_time = _as_utc(now or utcnow())
    assert current_time is not None
    summary: dict[str, Any] = {
        "sources": {},
        "fetched": 0,
        "created": 0,
        "updated": 0,
        "duplicates": 0,
        "skipped": 0,
        "expired": 0,
        "errors": [],
    }
    adapters = (
        ("adzuna", fetch_adzuna_jobs, False),
        ("himalayas", fetch_himalayas_jobs, False),
        ("greenhouse", fetch_greenhouse_jobs, True),
        ("lever", fetch_lever_jobs, True),
        ("ashby", fetch_ashby_jobs, True),
    )
    fetched_sources: dict[str, tuple[list[JobPayload], bool]] = {}

    # Finish all bounded network reads before opening a database write transaction.
    # This prevents a slow vendor from holding SQLite's application-wide write lock.
    for source_name, adapter, isolates_configured_boards in adapters:
        source_summary = {
            "fetched": 0,
            "created": 0,
            "updated": 0,
            "duplicates": 0,
            "deactivated": 0,
            "complete": True,
        }
        try:
            board_errors: list[str] = []
            payloads = (
                adapter(client=client, errors=board_errors)
                if isolates_configured_boards
                else adapter(client=client)
            )
            complete = not board_errors
            fetched_sources[source_name] = (payloads, complete)
            source_summary["fetched"] = len(payloads)
            source_summary["complete"] = complete
            summary["fetched"] += len(payloads)
            if board_errors:
                source_summary["failed_boards"] = len(board_errors)
                error = f"{source_name}: {len(board_errors)} configured board(s) unavailable"
                summary["errors"].append(error)
        except Exception as exc:
            error = f"{source_name}: {exc.__class__.__name__}"
            summary["errors"].append(error)
            source_summary["error"] = error
        summary["sources"][source_name] = source_summary

    for source_name, (payloads, complete) in fetched_sources.items():
        source_summary = summary["sources"][source_name]
        local_skipped = 0
        try:
            with db.session.begin_nested():
                for payload in payloads:
                    if not payload.provider_job_id or not payload.apply_url:
                        local_skipped += 1
                        continue
                    _, outcome = upsert_job(payload, now=current_time)
                    outcome_key = "duplicates" if outcome == "duplicate" else outcome
                    source_summary[outcome_key] += 1

                configured_full_feed = {
                    "greenhouse": bool(
                        _configured_sources(current_app.config.get("GREENHOUSE_BOARDS", ""))
                    ),
                    "lever": bool(
                        _configured_sources(current_app.config.get("LEVER_SITES", ""))
                    ),
                    "ashby": bool(
                        _configured_sources(current_app.config.get("ASHBY_BOARDS", ""))
                    ),
                }.get(source_name, False)
                if configured_full_feed and complete:
                    seen_ids = {payload.provider_job_id for payload in payloads}
                    stale_statement = db.update(Job).where(
                        Job.provider == source_name,
                        Job.is_active.is_(True),
                    )
                    if seen_ids:
                        stale_statement = stale_statement.where(
                            Job.provider_job_id.not_in(seen_ids)
                        )
                    deactivated = db.session.execute(
                        stale_statement.values(is_active=False)
                    ).rowcount
                    source_summary["deactivated"] = int(deactivated or 0)
                db.session.flush()
            if commit:
                db.session.commit()
            summary["skipped"] += local_skipped
            summary["created"] += source_summary["created"]
            summary["updated"] += source_summary["updated"]
            summary["duplicates"] += source_summary["duplicates"]
        except Exception as exc:  # isolate writes so one malformed source cannot stop the rest
            if commit:
                db.session.rollback()
            error = f"{source_name}: {exc.__class__.__name__}"
            for key in ("created", "updated", "duplicates", "deactivated"):
                source_summary[key] = 0
            summary["errors"].append(error)
            source_summary["error"] = error

    expired = Job.query.filter(
        Job.is_active.is_(True),
        Job.expires_at.is_not(None),
        Job.expires_at <= current_time,
    ).update({Job.is_active: False}, synchronize_session=False)
    summary["expired"] = int(expired or 0)
    stale_cutoff = current_time - timedelta(days=45)
    stale = Job.query.filter(
        Job.is_active.is_(True),
        Job.is_demo.is_(False),
        Job.last_seen_at < stale_cutoff,
    ).update({Job.is_active: False}, synchronize_session=False)
    summary["stale_deactivated"] = int(stale or 0)
    if commit:
        db.session.commit()

    # Dashboard GET requests stay read-only: materialize fresh explanations once
    # after the complete source sync instead of rebuilding on every page view.
    from .matching_service import rebuild_matches

    summary["users_rematched"] = 0
    summary["rematch_errors"] = 0
    onboarded_users = db.session.scalars(
        db.select(User).where(
            User.onboarding_complete.is_(True),
            User.deleted_at.is_(None),
        )
    ).all()
    for user in onboarded_users:
        try:
            with db.session.begin_nested():
                rebuild_matches(user, now=current_time, commit=False)
                db.session.flush()
            if commit:
                db.session.commit()
            summary["users_rematched"] += 1
        except Exception:
            if commit:
                db.session.rollback()
            summary["rematch_errors"] += 1
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return summary


__all__ = [
    "JobPayload",
    "JobSourceError",
    "fetch_adzuna_jobs",
    "fetch_and_upsert_all",
    "fetch_ashby_jobs",
    "fetch_greenhouse_jobs",
    "fetch_himalayas_jobs",
    "fetch_lever_jobs",
    "seed_demo_jobs",
    "upsert_job",
]
