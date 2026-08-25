from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import Application, PrivateFileCleanup, Resume
from app.services.resume_service import save_and_parse_resume
from app.services.telegram_service import create_link_token
from tests.conftest import login_client
from tests.test_matching import make_job


def text_upload(name: str, skill: str) -> FileStorage:
    return FileStorage(
        stream=BytesIO(
            f"Asha Student\nGraduate Developer\nB.Tech Computer Science 2026\n"
            f"Skills: {skill}, Git\nProjects\nBuilt a secure student application"
            .encode()
        ),
        filename=name,
        content_type="text/plain",
    )


def test_replacing_resume_removes_every_previous_record_and_file(app, user_factory):
    user = user_factory()
    first = save_and_parse_resume(user, text_upload("first.txt", "Python"))
    first_path = Path(app.config["UPLOAD_FOLDER"]) / first.stored_name
    assert first_path.exists()

    second = save_and_parse_resume(user, text_upload("second.txt", "SQL"))

    resumes = db.session.scalars(db.select(Resume).where(Resume.user_id == user.id)).all()
    assert [resume.id for resume in resumes] == [second.id]
    assert not first_path.exists()
    assert (Path(app.config["UPLOAD_FOLDER"]) / second.stored_name).exists()
    cleanup = db.session.scalar(
        db.select(PrivateFileCleanup).where(PrivateFileCleanup.stored_name == first.stored_name)
    )
    assert cleanup is not None
    assert cleanup.status == "completed"


def test_discard_storage_parses_resume_without_retaining_file(app, user_factory):
    app.config["RESUME_STORAGE_MODE"] = "discard"
    user = user_factory()

    record = save_and_parse_resume(user, text_upload("private.txt", "Python"))

    assert record.stored_name.startswith(f"discarded/{user.id}/")
    assert record.extracted_text == ""
    assert "Python" in record.parsed_profile["skills"]
    assert not (Path(app.config["UPLOAD_FOLDER"]) / str(user.id)).exists()


def test_delete_resume_route_erases_hidden_historical_files(app, client, user_factory):
    user = user_factory()
    folder = Path(app.config["UPLOAD_FOLDER"]) / str(user.id)
    folder.mkdir(parents=True)
    paths = [folder / "old.txt", folder / "current.txt"]
    for path in paths:
        path.write_text("private resume data", encoding="utf-8")
    records = [
        Resume(
            user_id=user.id,
            original_name=path.name,
            stored_name=f"{user.id}/{path.name}",
            mime_type="text/plain",
            extension="txt",
            size_bytes=path.stat().st_size,
            sha256=str(index) * 64,
            is_current=index == 2,
        )
        for index, path in enumerate(paths, start=1)
    ]
    db.session.add_all(records)
    db.session.commit()
    login_client(client, user)

    response = client.post("/onboarding/resume/delete")

    assert response.status_code == 302
    assert db.session.scalar(
        db.select(db.func.count(Resume.id)).where(Resume.user_id == user.id)
    ) == 0
    assert all(not path.exists() for path in paths)


def test_manual_profile_flow_saves_editable_matching_data(app, client, user_factory):
    user = user_factory(
        skills=[],
        desired_roles=[],
        preferred_locations=[],
        onboarding_complete=False,
    )
    login_client(client, user)

    response = client.get("/onboarding/resume/review")
    assert response.status_code == 200
    assert b"Tell us what you can do" in response.data

    response = client.post(
        "/onboarding/resume/review",
        data={
            "headline": "Graduate backend developer",
            "education": "B.Tech Computer Science",
            "experience": "Built a Flask placement portal",
            "experience_years": "0.5",
            "graduation_year": "2026",
            "skills": ["Python", "Flask", "python"],
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/onboarding/preferences")
    db.session.refresh(user)
    assert user.headline == "Graduate backend developer"
    assert user.skills == ["Python", "Flask"]
    assert user.experience_years == 0.5
    assert user.graduation_year == 2026


def test_telegram_disconnect_stops_delivery_and_invalidates_links(app, client, user_factory):
    user = user_factory(
        telegram_user_id="777001",
        telegram_chat_id="777001",
        telegram_username="asha_student",
        phone_verified_at=datetime.now(UTC),
        alert_status="active",
        active_until=datetime.now(UTC) + timedelta(days=7),
        alerts_consent_at=datetime.now(UTC),
    )
    _raw_token, token = create_link_token(user)
    login_client(client, user)

    response = client.post("/telegram/disconnect")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/settings")
    db.session.refresh(user)
    db.session.refresh(token)
    assert user.telegram_connected is False
    assert user.telegram_user_id is None
    assert user.alert_status == "inactive"
    assert user.active_until is None
    assert token.consumed_at is not None


def test_withdrawn_application_remains_visible(app, client, user_factory):
    user = user_factory()
    job = make_job(title="Graduate API Engineer")
    db.session.add(job)
    db.session.flush()
    db.session.add(Application(user_id=user.id, job_id=job.id, status="withdrawn"))
    db.session.commit()
    login_client(client, user)

    response = client.get("/applications")

    assert response.status_code == 200
    assert b' data-stage="withdrawn"' in response.data
    assert b"Graduate API Engineer" in response.data


def test_settings_links_to_confirmed_profile_editor(app, client, user_factory):
    user = user_factory()
    login_client(client, user)

    response = client.get("/settings")

    assert response.status_code == 200
    assert b"Confirmed profile" in response.data
    assert b"/onboarding/resume/review?next=settings" in response.data


def test_match_score_is_automatic_and_cannot_be_selected(app, client, user_factory):
    user = user_factory(minimum_match_score=95)
    login_client(client, user)

    settings_response = client.get("/settings")
    onboarding_response = client.get("/onboarding/preferences")

    assert settings_response.status_code == 200
    assert onboarding_response.status_code == 200
    assert b"Minimum match score" not in settings_response.data
    assert b"Minimum match score" not in onboarding_response.data
    assert b"automatically ranked" in settings_response.data
    assert b"automatically ranked" in onboarding_response.data

    response = client.post(
        "/settings",
        data={
            "desired_roles": "Python Developer",
            "preferred_locations": "Bengaluru, Remote",
            "remote": "y",
            "hybrid": "y",
            "onsite": "y",
            "full_time": "y",
            "internship": "y",
            "graduation_year": "2027",
            "minimum_match_score": "95",
            "daily_job_limit": "5",
            "preferred_time": "09:00",
            "timezone": "Asia/Kolkata",
            "salary_preference": "",
        },
    )

    assert response.status_code == 302
    db.session.refresh(user)
    assert user.minimum_match_score == 50
