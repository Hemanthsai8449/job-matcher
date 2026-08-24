import re
from urllib.parse import urlsplit

from app.extensions import db
from app.models import User
from tests.conftest import login_client


def test_registration_and_email_otp_verification(app, client, monkeypatch):
    monkeypatch.setattr("app.services.otp_service.generate_numeric_code", lambda length=6: "123456")
    response = client.post(
        "/auth/register",
        data={
            "full_name": "Asha Student",
            "email": "asha.new@example.com",
            "phone": "+919876543210",
            "password": "CorrectHorse123!",
            "confirm_password": "CorrectHorse123!",
            "accept_terms": "y",
            "alerts_consent": "y",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/verify-email")

    response = client.post("/auth/verify-email", data={"code": "123456"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/onboarding/resume")
    user = db.session.scalar(db.select(User).where(User.email == "asha.new@example.com"))
    assert user.email_verified_at is not None


def test_password_reset_is_real_single_use_and_revokes_sessions(app, user_factory):
    user = user_factory()
    old_client = app.test_client()
    reset_client = app.test_client()
    login_client(old_client, user)
    assert old_client.get("/dashboard").status_code == 200

    response = reset_client.post("/auth/forgot-password", data={"email": user.email})
    assert response.status_code == 302
    message = app.extensions["job_matcher_mail_outbox"][-1]
    body = message.get_body(preferencelist=("plain",)).get_content()
    reset_url = re.search(r"http://localhost/auth/reset-password/\S+", body).group(0)
    reset_path = urlsplit(reset_url).path

    response = reset_client.post(
        reset_path,
        data={"password": "EvenSaferPassword456!", "confirm_password": "EvenSaferPassword456!"},
    )
    assert response.status_code == 302
    assert user.check_password("EvenSaferPassword456!")
    assert reset_client.get(reset_path).status_code == 302
    assert old_client.get("/dashboard").status_code == 302


def test_job_action_rejects_external_next_redirect(app, client, user_factory):
    from tests.test_matching import make_job

    user = user_factory()
    job = make_job()
    db.session.add(job)
    db.session.commit()
    login_client(client, user)

    response = client.post(
        f"/jobs/{job.id}/action",
        data={"status": "saved", "notes": "Review Friday", "next": "https://evil.example"},
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/jobs/{job.id}")
