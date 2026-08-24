from datetime import UTC, datetime

import requests

from app.extensions import db
from app.services import job_sources
from app.services.job_sources import fetch_and_upsert_all, fetch_greenhouse_jobs
from tests.test_matching import make_job


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
        self.content = b"{}"

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def close(self):
        return None


class PartialGreenhouseClient:
    def get(self, url, **kwargs):  # noqa: ARG002
        if "/bad/" in url:
            raise requests.Timeout("temporary board timeout")
        return FakeResponse(
            {
                "jobs": [
                    {
                        "id": 1001,
                        "title": "Graduate Software Engineer",
                        "location": {"name": "Bengaluru, India"},
                        "content": "Entry-level Python role for new graduates.",
                        "updated_at": "2026-08-24T08:00:00+00:00",
                        "absolute_url": "https://boards.example.com/jobs/1001",
                    }
                ]
            }
        )


def test_configured_board_failure_does_not_block_healthy_board(app):
    app.config["GREENHOUSE_BOARDS"] = "bad|Unavailable Board,good|Healthy Board"
    errors = []

    jobs = fetch_greenhouse_jobs(client=PartialGreenhouseClient(), errors=errors)

    assert errors == ["bad"]
    assert len(jobs) == 1
    assert jobs[0].company == "Healthy Board"


def test_partial_provider_poll_does_not_deactivate_unseen_jobs(app, monkeypatch):
    now = datetime.now(UTC)
    existing = make_job(
        provider="greenhouse",
        provider_job_id="unavailable-board:existing",
        last_seen_at=now,
    )
    db.session.add(existing)
    db.session.commit()
    app.config["GREENHOUSE_BOARDS"] = "unavailable-board,healthy-board"

    monkeypatch.setattr(job_sources, "fetch_adzuna_jobs", lambda *, client=None: [])
    monkeypatch.setattr(job_sources, "fetch_himalayas_jobs", lambda *, client=None: [])
    monkeypatch.setattr(job_sources, "fetch_lever_jobs", lambda *, client=None, errors=None: [])
    monkeypatch.setattr(job_sources, "fetch_ashby_jobs", lambda *, client=None, errors=None: [])

    def partial_greenhouse(*, client=None, errors=None):  # noqa: ARG001
        errors.append("unavailable-board")
        return []

    monkeypatch.setattr(job_sources, "fetch_greenhouse_jobs", partial_greenhouse)

    summary = fetch_and_upsert_all(now=now)

    db.session.refresh(existing)
    assert existing.is_active is True
    assert summary["sources"]["greenhouse"]["complete"] is False
    assert summary["sources"]["greenhouse"]["failed_boards"] == 1
