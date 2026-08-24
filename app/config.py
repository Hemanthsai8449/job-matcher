import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / "instance"
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def normalize_database_url(value: str) -> str:
    """Use psycopg 3 explicitly for managed PostgreSQL connection strings."""

    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value.removeprefix("postgresql://")
    return value


class Config:
    APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = normalize_database_url(
        os.getenv("DATABASE_URL", f"sqlite:///{(INSTANCE_DIR / 'job_matcher.db').as_posix()}")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = (
        {"pool_pre_ping": True, "pool_recycle": 300}
        if not SQLALCHEMY_DATABASE_URI.startswith("sqlite:")
        else {}
    )
    MAX_CONTENT_LENGTH_MB = env_int(
        "MAX_CONTENT_LENGTH_MB",
        4 if APP_ENV == "production" else 10,
        minimum=1,
        maximum=10,
    )
    MAX_CONTENT_LENGTH = MAX_CONTENT_LENGTH_MB * 1024 * 1024
    RESUME_STORAGE_MODE = os.getenv(
        "RESUME_STORAGE_MODE", "discard" if APP_ENV == "production" else "local"
    ).strip().lower()
    UPLOAD_FOLDER = str(INSTANCE_DIR / "uploads")
    EXPORT_FOLDER = str(INSTANCE_DIR / "exports")
    ALLOWED_RESUME_EXTENSIONS = {
        "pdf",
        "docx",
        "doc",
        "odt",
        "rtf",
        "txt",
        "md",
        "html",
        "htm",
        "jpg",
        "jpeg",
        "png",
        "webp",
    }
    OTP_TTL_SECONDS = 300
    OTP_MAX_ATTEMPTS = 5
    OTP_RESEND_SECONDS = 60
    PASSWORD_RESET_TTL_SECONDS = 3600
    TELEGRAM_LINK_TTL_SECONDS = 900
    ALERT_WINDOW_DAYS = 7
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = env_bool("COOKIE_SECURE", False)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    WTF_CSRF_TIME_LIMIT = 3600
    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000")
    MAIL_HOST = os.getenv("MAIL_HOST", "")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
    MAIL_FROM = os.getenv(
        "MAIL_FROM", "Job Matcher <jobmatchersupport@gmail.com>"
    )
    SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "jobmatchersupport@gmail.com")
    MAIL_USE_TLS = env_bool("MAIL_USE_TLS", True)
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_SIGN_IN_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
    GOOGLE_PENDING_TTL_SECONDS = 600
    ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
    ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")
    ADZUNA_COUNTRY = os.getenv("ADZUNA_COUNTRY", "in")
    GREENHOUSE_BOARDS = os.getenv("GREENHOUSE_BOARDS", "")
    LEVER_SITES = os.getenv("LEVER_SITES", "")
    ASHBY_BOARDS = os.getenv("ASHBY_BOARDS", "")
    ENABLE_HIMALAYAS = env_bool("ENABLE_HIMALAYAS", True)
    SEED_DEMO_JOBS = env_bool("SEED_DEMO_JOBS", APP_ENV == "development")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")
    TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    CRON_SECRET = os.getenv("CRON_SECRET", "")
    ADMIN_EMAILS = {
        email.strip().lower()
        for email in os.getenv("ADMIN_EMAILS", "").split(",")
        if email.strip()
    }
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    TRUSTED_PROXY_HOPS = max(0, int(os.getenv("TRUSTED_PROXY_HOPS", "0")))
    SERVERLESS_DEPLOYMENT = env_bool("VERCEL", False)
    DEBUG = env_bool("FLASK_DEBUG", APP_ENV != "production")


class TestConfig(Config):
    APP_ENV = "testing"
    TESTING = True
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    RESUME_STORAGE_MODE = "local"
    SERVER_NAME = "localhost"
    SECRET_KEY = "test-secret-key-that-is-long-enough"
