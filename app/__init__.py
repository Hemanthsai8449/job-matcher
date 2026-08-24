from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import click
from dotenv import load_dotenv
from flask import Flask, render_template
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .extensions import csrf, db, limiter, login_manager, migrate, oauth

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@event.listens_for(Engine, "connect")
def configure_sqlite(dbapi_connection, connection_record):  # noqa: ARG001
    module = dbapi_connection.__class__.__module__
    if not module.startswith("sqlite3"):
        return
    # Foreign-key PRAGMAs require temporary DBAPI autocommit. Restore the
    # driver's original mode afterward; OTP and matching writers explicitly
    # start with DML before using savepoints.
    original_autocommit = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()
        dbapi_connection.autocommit = original_autocommit


def create_app(config_object=None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if config_object:
        if isinstance(config_object, dict):
            app.config.update(config_object)
        else:
            app.config.from_object(config_object)

    storage_mode = str(app.config.get("RESUME_STORAGE_MODE", "local")).strip().lower()
    if storage_mode not in {"local", "discard"}:
        raise RuntimeError("RESUME_STORAGE_MODE must be either local or discard.")
    app.config["RESUME_STORAGE_MODE"] = storage_mode

    if app.config.get("APP_ENV") == "production":
        secret = str(app.config.get("SECRET_KEY", ""))
        if len(secret) < 32 or secret in {
            "dev-only-change-me",
            "replace-with-a-long-random-value",
        }:
            raise RuntimeError("Production requires a strong SECRET_KEY of at least 32 characters.")
        if not app.config.get("SESSION_COOKIE_SECURE"):
            raise RuntimeError("Production requires COOKIE_SECURE=true.")
        app_base_url = urlsplit(str(app.config.get("APP_BASE_URL", "")))
        if app_base_url.scheme != "https" or not app_base_url.hostname:
            raise RuntimeError("Production requires an HTTPS APP_BASE_URL.")
        if app.config.get("DEBUG"):
            raise RuntimeError("Production requires FLASK_DEBUG=0.")
        rate_limit_storage = str(app.config.get("RATELIMIT_STORAGE_URI", ""))
        if not rate_limit_storage or rate_limit_storage == "memory://":
            raise RuntimeError("Production requires shared rate-limit storage.")
        database_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI", ""))
        if database_uri.startswith("sqlite:"):
            raise RuntimeError("Production requires a managed PostgreSQL database.")
        if len(str(app.config.get("CRON_SECRET", ""))) < 16:
            raise RuntimeError("Production requires a CRON_SECRET of at least 16 characters.")
        if len(str(app.config.get("TELEGRAM_WEBHOOK_SECRET", ""))) < 16:
            raise RuntimeError(
                "Production requires a TELEGRAM_WEBHOOK_SECRET of at least 16 characters."
            )
        if app.config.get("SERVERLESS_DEPLOYMENT"):
            if storage_mode != "discard":
                raise RuntimeError("Vercel requires RESUME_STORAGE_MODE=discard.")
            if int(app.config.get("MAX_CONTENT_LENGTH", 0)) > 4 * 1024 * 1024:
                raise RuntimeError("Vercel resume uploads must be limited to 4 MB.")

    trusted_proxy_hops = max(0, int(app.config.get("TRUSTED_PROXY_HOPS", 0)))
    if trusted_proxy_hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxy_hops)

    if storage_mode == "local":
        Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    oauth.init_app(app)
    oauth.register(
        name="google",
        overwrite=True,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "info"

    from .models import User

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            raw_id, raw_version = user_id.split(":", 1)
            user = db.session.get(User, int(raw_id))
            if (
                user
                and user.deleted_at is None
                and user.auth_version == int(raw_version)
            ):
                return user
            return None
        except (AttributeError, TypeError, ValueError):
            return None

    from .admin.routes import bp as admin_bp
    from .auth.routes import bp as auth_bp
    from .integrations.routes import bp as integrations_bp
    from .jobs.routes import bp as jobs_bp
    from .main.routes import bp as main_bp
    from .onboarding.routes import bp as onboarding_bp
    from .operations.routes import bp as operations_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(onboarding_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(integrations_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(operations_bp)

    register_cli(app)
    register_error_handlers(app)
    register_security_headers(app)

    with app.app_context():
        if app.config.get("TESTING"):
            db.create_all()

    return app


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(400)
    def bad_request(error):
        return render_template(
            "errors/error.html",
            code=400,
            title="Request not accepted",
            message=str(
                getattr(error, "description", "Please check the submitted information.")
            ),
        ), 400

    @app.errorhandler(403)
    def forbidden(error):  # noqa: ARG001
        return render_template(
            "errors/error.html",
            code=403,
            title="Access denied",
            message="You do not have permission to view this page.",
        ), 403

    @app.errorhandler(404)
    def not_found(error):  # noqa: ARG001
        return render_template(
            "errors/error.html",
            code=404,
            title="Page not found",
            message="The page may have moved or the link may be incomplete.",
        ), 404

    @app.errorhandler(413)
    def too_large(error):  # noqa: ARG001
        limit_mb = app.config.get("MAX_CONTENT_LENGTH_MB", 10)
        return render_template(
            "errors/error.html",
            code=413,
            title="File is too large",
            message=f"Resume files must be {limit_mb} MB or smaller.",
        ), 413

    @app.errorhandler(500)
    def internal_error(error):  # noqa: ARG001
        db.session.rollback()
        return render_template(
            "errors/error.html",
            code=500,
            title="Something went wrong",
            message="Your request may not have completed. Check its status before trying again.",
        ), 500


def register_security_headers(app: Flask) -> None:
    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self' https://t.me",
        )
        if not app.debug and app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command():
        """Apply schema migrations and optionally add demonstration jobs."""
        from flask_migrate import upgrade

        from .services.job_sources import seed_demo_jobs

        upgrade()
        created = seed_demo_jobs() if app.config.get("SEED_DEMO_JOBS") else 0
        click.echo(f"Database ready. Added {created} demonstration jobs.")

    @app.cli.command("sync-jobs")
    def sync_jobs_command():
        """Fetch authorized job feeds configured in the environment."""
        from .services.job_sources import fetch_and_upsert_all

        summary = fetch_and_upsert_all()
        click.echo(summary)

    @app.cli.command("dispatch-alerts")
    def dispatch_alerts_command():
        """Send due Telegram job digests. Run from one external scheduler."""
        from .services.alert_service import dispatch_due_alerts

        summary = dispatch_due_alerts()
        click.echo(summary)

    @app.cli.command("cleanup-private-files")
    def cleanup_private_files_command():
        """Retry durable private upload deletion work."""
        from .services.file_cleanup import process_pending_private_files

        summary = process_pending_private_files()
        click.echo(summary)

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    def create_admin_command(email: str):
        """Grant administrator access to an existing verified account."""
        from .models import User
        from .utils import normalize_email

        user = db.session.scalar(
            db.select(User).where(
                User.email == normalize_email(email),
                User.email_verified_at.is_not(None),
                User.deleted_at.is_(None),
            )
        )
        if not user:
            raise click.ClickException("Register and verify that email first.")
        user.is_admin = True
        db.session.commit()
        click.echo(f"Administrator access enabled for {user.email}.")
