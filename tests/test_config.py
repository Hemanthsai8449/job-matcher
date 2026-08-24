import pytest

from app import create_app
from app.config import normalize_database_url


def production_config(**overrides):
    values = {
        "APP_ENV": "production",
        "TESTING": False,
        "DEBUG": False,
        "SECRET_KEY": "a-production-secret-that-is-longer-than-thirty-two-characters",
        "SESSION_COOKIE_SECURE": True,
        "APP_BASE_URL": "https://jobs.example.com",
        "RATELIMIT_STORAGE_URI": "redis://localhost:6379/0",
        "SQLALCHEMY_DATABASE_URI": "postgresql+psycopg://user:pass@db.example.com/jobs",
        "CRON_SECRET": "production-cron-secret-1234",
        "TELEGRAM_WEBHOOK_SECRET": "production-webhook-secret-1234",
        "RESUME_STORAGE_MODE": "discard",
    }
    values.update(overrides)
    return values


def test_production_rejects_memory_rate_limit_storage(tmp_path):
    config = production_config(
        RATELIMIT_STORAGE_URI="memory://",
        UPLOAD_FOLDER=str(tmp_path / "uploads"),
        EXPORT_FOLDER=str(tmp_path / "exports"),
    )
    with pytest.raises(RuntimeError, match="shared rate-limit storage"):
        create_app(config)


def test_production_rejects_debug_mode(tmp_path):
    config = production_config(
        DEBUG=True,
        UPLOAD_FOLDER=str(tmp_path / "uploads"),
        EXPORT_FOLDER=str(tmp_path / "exports"),
    )
    with pytest.raises(RuntimeError, match="FLASK_DEBUG=0"):
        create_app(config)


def test_database_url_uses_psycopg_three_driver():
    assert normalize_database_url("postgres://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )
    assert normalize_database_url("postgresql://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )


def test_production_rejects_sqlite_database():
    with pytest.raises(RuntimeError, match="managed PostgreSQL"):
        create_app(production_config(SQLALCHEMY_DATABASE_URI="sqlite:///production.db"))


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"CRON_SECRET": ""}, "CRON_SECRET"),
        ({"TELEGRAM_WEBHOOK_SECRET": ""}, "TELEGRAM_WEBHOOK_SECRET"),
    ],
)
def test_production_requires_scheduler_and_webhook_secrets(override, message):
    with pytest.raises(RuntimeError, match=message):
        create_app(production_config(**override))


def test_vercel_requires_discarded_resume_storage():
    with pytest.raises(RuntimeError, match="RESUME_STORAGE_MODE=discard"):
        create_app(production_config(SERVERLESS_DEPLOYMENT=True, RESUME_STORAGE_MODE="local"))


def test_vercel_rejects_resume_uploads_larger_than_four_mb():
    with pytest.raises(RuntimeError, match="limited to 4 MB"):
        create_app(
            production_config(
                SERVERLESS_DEPLOYMENT=True,
                MAX_CONTENT_LENGTH=5 * 1024 * 1024,
            )
        )


def test_valid_production_configuration_initializes():
    application = create_app(production_config())

    assert application.config["APP_ENV"] == "production"
    assert application.config["RESUME_STORAGE_MODE"] == "discard"
