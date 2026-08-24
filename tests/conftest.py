from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app import create_app
from app.extensions import db
from app.models import User


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "APP_ENV": "testing",
            "TESTING": True,
            "DEBUG": False,
            "SECRET_KEY": "test-secret-key-with-at-least-thirty-two-characters",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
            "WTF_CSRF_ENABLED": False,
            "RATELIMIT_ENABLED": False,
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "EXPORT_FOLDER": str(tmp_path / "exports"),
            "APP_BASE_URL": "http://localhost",
            "MAIL_HOST": "",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_BOT_USERNAME": "JobMatcherTestBot",
            "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
            "CRON_SECRET": "test-cron-secret-1234",
            "GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
            "GOOGLE_CLIENT_SECRET": "test-google-client-secret",
            "GOOGLE_SIGN_IN_ENABLED": True,
            "SERVER_NAME": "localhost",
        }
    )
    context = application.app_context()
    context.push()
    yield application
    db.session.remove()
    db.drop_all()
    context.pop()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def user_factory(app):
    def create_user(**overrides):
        values = {
            "full_name": "Asha Student",
            "email": "asha@example.com",
            "phone_e164": "+919876543210",
            "accepted_terms_at": datetime.now(UTC),
            "email_verified_at": datetime.now(UTC),
            "skills": ["Python", "SQL", "Git"],
            "desired_roles": ["Python Developer"],
            "preferred_locations": ["Bengaluru", "Remote"],
            "work_modes": ["remote", "hybrid", "onsite"],
            "job_types": ["full_time", "internship"],
            "onboarding_complete": True,
        }
        values.update(overrides)
        user = User(**values)
        user.set_password("CorrectHorse123!")
        db.session.add(user)
        db.session.commit()
        return user

    return create_user


def login_client(client, user: User) -> None:
    with client.session_transaction() as session:
        session["_user_id"] = user.get_id()
        session["_fresh"] = True
