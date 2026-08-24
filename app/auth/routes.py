from __future__ import annotations

import hashlib
import secrets
import time

from authlib.integrations.base_client.errors import OAuthError
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from joserfc.errors import JoseError
from requests import RequestException
from sqlalchemy.exc import IntegrityError

from ..extensions import db, limiter, oauth
from ..models import AuditLog, User
from ..services.email_service import (
    EmailDeliveryError,
    send_otp_email,
    send_password_reset_email,
)
from ..services.otp_service import create_otp, resend_otp, resend_wait_seconds, verify_otp
from ..utils import is_safe_relative_url, normalize_email, normalize_phone, utcnow
from .forms import (
    ForgotPasswordForm,
    GoogleCompleteForm,
    LoginForm,
    OTPForm,
    RegisterForm,
    ResetPasswordForm,
)

bp = Blueprint("auth", __name__, url_prefix="/auth")


def _reset_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt="job-matcher-password-reset-v1")


def _password_fingerprint(user: User) -> str:
    identity = user.password_hash or f"google:{user.google_sub or user.id}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _create_reset_token(user: User) -> str:
    return _reset_serializer().dumps(
        {"user_id": user.id, "password": _password_fingerprint(user)}
    )


def _load_reset_user(token: str) -> User | None:
    try:
        payload = _reset_serializer().loads(
            token,
            max_age=int(current_app.config.get("PASSWORD_RESET_TTL_SECONDS", 3600)),
        )
        user = db.session.get(User, int(payload.get("user_id")))
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not user or user.deleted_at is not None:
        return None
    if payload.get("password") != _password_fingerprint(user):
        return None
    return user


def is_safe_next(target: str | None) -> bool:
    return is_safe_relative_url(target)


def _google_enabled() -> bool:
    return bool(
        current_app.config.get("GOOGLE_CLIENT_ID")
        and current_app.config.get("GOOGLE_CLIENT_SECRET")
    )


def _google_callback_url() -> str:
    base_url = str(current_app.config["APP_BASE_URL"]).rstrip("/")
    return f"{base_url}{url_for('auth.google_callback')}"


def _pending_google_identity() -> dict | None:
    identity = session.get("google_pending")
    if not isinstance(identity, dict):
        return None
    issued_at = identity.get("issued_at")
    required = ("sub", "email", "name")
    try:
        expired = time.time() - float(issued_at) > int(
            current_app.config.get("GOOGLE_PENDING_TTL_SECONDS", 600)
        )
    except (TypeError, ValueError):
        expired = True
    if expired or any(not identity.get(key) for key in required):
        session.pop("google_pending", None)
        return None
    return identity


def _finish_google_login(user: User, *, event: str, next_url: str = ""):
    user.email_verified_at = user.email_verified_at or utcnow()
    db.session.add(
        AuditLog(
            actor_user_id=user.id,
            event=event,
            entity_type="user",
            entity_id=str(user.id),
            metadata_json={"provider": "google"},
        )
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        session.clear()
        flash("That Google account could not be linked. Sign in another way and retry.", "danger")
        return redirect(url_for("auth.login"))
    session.clear()
    login_user(user, fresh=True)
    session["google_authenticated_at"] = int(time.time())
    flash("Signed in securely with Google.", "success")
    destination = next_url if is_safe_next(next_url) else url_for("main.dashboard")
    return redirect(destination)


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("8 per hour")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    form = RegisterForm()
    if form.validate_on_submit():
        email = normalize_email(form.email.data)
        try:
            phone = normalize_phone(form.phone.data)
        except ValueError as exc:
            form.phone.errors.append(str(exc))
            return render_template("auth/register.html", form=form)

        existing_email = db.session.scalar(db.select(User).where(User.email == email))
        verified_phone_owner = db.session.scalar(
            db.select(User).where(
                User.phone_e164 == phone,
                User.phone_verified_at.is_not(None),
                User.deleted_at.is_(None),
            )
        )
        if existing_email:
            form.email.errors.append("An account already uses this email. Try signing in.")
        elif verified_phone_owner:
            form.phone.errors.append(
                "This verified mobile number is already connected to an account."
            )
        else:
            now = utcnow()
            user = User(
                full_name=form.full_name.data.strip(),
                email=email,
                phone_e164=phone,
                accepted_terms_at=now,
                alerts_consent_at=now if form.alerts_consent.data else None,
                is_admin=email in current_app.config["ADMIN_EMAILS"],
            )
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.flush()
            code = create_otp(user, commit=False)
            db.session.add(
                AuditLog(
                    actor_user_id=user.id,
                    event="account.registered",
                    entity_type="user",
                    entity_id=str(user.id),
                )
            )
            db.session.commit()
            try:
                sent = send_otp_email(user, code)
            except EmailDeliveryError:
                sent = False
            session.clear()
            session["pending_user_id"] = user.id
            if current_app.debug and not current_app.config.get("MAIL_HOST"):
                flash(f"Development verification code: {code}", "dev")
            elif not sent:
                flash("We could not send the email. You can request another code.", "warning")
            else:
                flash("We sent a six-digit verification code to your email.", "success")
            return redirect(url_for("auth.verify_email"))
    return render_template("auth/register.html", form=form)


@bp.route("/verify-email", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def verify_email():
    user_id = session.get("pending_user_id")
    user = db.session.get(User, user_id) if user_id else None
    if not user:
        flash("Start by creating an account or signing in.", "info")
        return redirect(url_for("auth.register"))
    if user.email_verified_at:
        login_user(user)
        return redirect(url_for("onboarding.resume"))

    form = OTPForm()
    if form.validate_on_submit():
        ok, message = verify_otp(user, form.code.data, commit=False)
        if ok:
            user.email_verified_at = utcnow()
            db.session.add(
                AuditLog(
                    actor_user_id=user.id,
                    event="email.verified",
                    entity_type="user",
                    entity_id=str(user.id),
                )
            )
            db.session.commit()
            session.clear()
            login_user(user)
            flash("Email verified. Let's build your job profile.", "success")
            return redirect(url_for("onboarding.resume"))
        db.session.commit()
        form.code.errors.append(message)
    return render_template(
        "auth/verify_email.html",
        form=form,
        user=user,
        resend_seconds=resend_wait_seconds(user),
    )


@bp.post("/resend-otp", endpoint="resend_otp")
@limiter.limit("5 per hour")
def resend_otp_route():
    user_id = session.get("pending_user_id")
    user = db.session.get(User, user_id) if user_id else None
    if not user or user.email_verified_at:
        return redirect(url_for("auth.login"))
    code, message = resend_otp(user, commit=False)
    if not code:
        flash(message, "warning")
    else:
        db.session.commit()
        try:
            sent = send_otp_email(user, code)
        except EmailDeliveryError:
            sent = False
        if current_app.debug and not current_app.config.get("MAIL_HOST"):
            flash(f"Development verification code: {code}", "dev")
        elif sent:
            flash("A new verification code was sent.", "success")
        else:
            flash("The new code was created, but email delivery is not configured.", "warning")
    return redirect(url_for("auth.verify_email"))


@bp.get("/google")
@limiter.limit("20 per hour")
def google_login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if not _google_enabled():
        flash("Google sign-in is not configured yet. Use email and password for now.", "warning")
        return redirect(url_for("auth.login"))
    next_url = request.args.get("next", "")
    session["google_login_next"] = next_url if is_safe_next(next_url) else ""
    client = oauth.create_client("google")
    try:
        return client.authorize_redirect(
            _google_callback_url(),
            nonce=secrets.token_urlsafe(32),
            prompt="select_account",
        )
    except (JoseError, OAuthError, RequestException):
        current_app.logger.exception("Google authorization could not be started")
        session.pop("google_login_next", None)
        flash("Google sign-in is temporarily unavailable. Please try again.", "warning")
        return redirect(url_for("auth.login"))


@bp.get("/google/callback")
@limiter.limit("30 per hour")
def google_callback():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if not _google_enabled():
        flash("Google sign-in is not configured.", "warning")
        return redirect(url_for("auth.login"))
    next_url = session.pop("google_login_next", "")
    client = oauth.create_client("google")
    try:
        token = client.authorize_access_token()
    except (JoseError, OAuthError, RequestException):
        current_app.logger.warning("Google sign-in callback was rejected or unavailable")
        flash("Google sign-in was cancelled or could not be verified.", "warning")
        return redirect(url_for("auth.login"))

    userinfo = token.get("userinfo") if isinstance(token, dict) else None
    if not isinstance(userinfo, dict):
        flash("Google did not return a verifiable identity.", "danger")
        return redirect(url_for("auth.login"))
    subject = str(userinfo.get("sub", "")).strip()[:255]
    email = normalize_email(str(userinfo.get("email", "")))
    if not subject or not email or userinfo.get("email_verified") is not True:
        flash("Use a Google account with a verified email address.", "danger")
        return redirect(url_for("auth.login"))

    user = db.session.scalar(
        db.select(User).where(User.google_sub == subject, User.deleted_at.is_(None))
    )
    if user:
        return _finish_google_login(user, event="account.google_login", next_url=next_url)

    user = db.session.scalar(
        db.select(User).where(User.email == email, User.deleted_at.is_(None))
    )
    if user:
        if user.google_sub and user.google_sub != subject:
            flash("That email is already linked to a different Google account.", "danger")
            return redirect(url_for("auth.login"))
        user.google_sub = subject
        return _finish_google_login(user, event="account.google_linked", next_url=next_url)

    full_name = " ".join(str(userinfo.get("name", "")).split())[:120]
    if not full_name:
        full_name = email.split("@", 1)[0][:120]
    session.clear()
    session["google_pending"] = {
        "sub": subject,
        "email": email,
        "name": full_name,
        "issued_at": int(time.time()),
    }
    return redirect(url_for("auth.google_complete"))


@bp.route("/google/complete", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def google_complete():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    identity = _pending_google_identity()
    if not identity:
        flash("Your Google registration session expired. Please start again.", "warning")
        return redirect(url_for("auth.register"))

    form = GoogleCompleteForm()
    if form.validate_on_submit():
        try:
            phone = normalize_phone(form.phone.data)
        except ValueError as exc:
            form.phone.errors.append(str(exc))
            return render_template(
                "auth/google_complete.html", form=form, identity=identity
            )

        subject = identity["sub"]
        email = identity["email"]
        existing_identity = db.session.scalar(
            db.select(User).where(User.google_sub == subject, User.deleted_at.is_(None))
        )
        existing_email = db.session.scalar(
            db.select(User).where(User.email == email, User.deleted_at.is_(None))
        )
        if existing_identity:
            return _finish_google_login(
                existing_identity,
                event="account.google_login",
            )
        if existing_email:
            if existing_email.google_sub and existing_email.google_sub != subject:
                flash("That email is already linked to a different Google account.", "danger")
                return redirect(url_for("auth.login"))
            existing_email.google_sub = subject
            return _finish_google_login(
                existing_email,
                event="account.google_linked",
            )

        verified_phone_owner = db.session.scalar(
            db.select(User).where(
                User.phone_e164 == phone,
                User.phone_verified_at.is_not(None),
                User.deleted_at.is_(None),
            )
        )
        if verified_phone_owner:
            form.phone.errors.append(
                "This verified mobile number is already connected to an account."
            )
        else:
            now = utcnow()
            user = User(
                full_name=identity["name"],
                email=email,
                phone_e164=phone,
                password_hash=None,
                google_sub=subject,
                email_verified_at=now,
                accepted_terms_at=now,
                alerts_consent_at=now if form.alerts_consent.data else None,
                is_admin=email in current_app.config["ADMIN_EMAILS"],
            )
            db.session.add(user)
            try:
                db.session.flush()
                db.session.add(
                    AuditLog(
                        actor_user_id=user.id,
                        event="account.registered_with_google",
                        entity_type="user",
                        entity_id=str(user.id),
                        metadata_json={"provider": "google"},
                    )
                )
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash(
                    "An account was created at the same time. Please start Google sign-in again.",
                    "warning",
                )
                return redirect(url_for("auth.login"))
            session.clear()
            login_user(user, fresh=True)
            session["google_authenticated_at"] = int(time.time())
            flash("Your verified Google account is ready. Now build your job profile.", "success")
            return redirect(url_for("onboarding.resume"))

    return render_template("auth/google_complete.html", form=form, identity=identity)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("12 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        email = normalize_email(form.email.data)
        user = db.session.scalar(
            db.select(User).where(User.email == email, User.deleted_at.is_(None))
        )
        if not user or not user.check_password(form.password.data):
            form.password.errors.append("Email or password is incorrect.")
        elif not user.email_verified_at:
            session.clear()
            session["pending_user_id"] = user.id
            flash("Verify your email before signing in.", "info")
            return redirect(url_for("auth.verify_email"))
        else:
            remember = bool(form.remember.data)
            next_url = request.values.get("next")
            session.clear()
            login_user(user, remember=remember, fresh=True)
            db.session.add(
                AuditLog(
                    actor_user_id=user.id,
                    event="account.login",
                    entity_type="user",
                    entity_id=str(user.id),
                )
            )
            db.session.commit()
            return redirect(next_url if is_safe_next(next_url) else url_for("main.dashboard"))
    return render_template("auth/login.html", form=form)


@bp.post("/logout")
def logout():
    if current_user.is_authenticated:
        db.session.add(
            AuditLog(
                actor_user_id=current_user.id,
                event="account.logout",
                entity_type="user",
                entity_id=str(current_user.id),
            )
        )
        db.session.commit()
    logout_user()
    session.clear()
    flash("You're signed out safely.", "success")
    return redirect(url_for("main.index"))


@bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user = db.session.scalar(
            db.select(User).where(
                User.email == normalize_email(form.email.data),
                User.deleted_at.is_(None),
            )
        )
        if user:
            token = _create_reset_token(user)
            reset_path = url_for("auth.reset_password", token=token)
            reset_url = f"{str(current_app.config['APP_BASE_URL']).rstrip('/')}{reset_path}"
            try:
                send_password_reset_email(
                    user,
                    reset_url,
                    int(current_app.config.get("PASSWORD_RESET_TTL_SECONDS", 3600)),
                )
            except EmailDeliveryError:
                # Keep the response enumeration-safe while operators diagnose mail.
                current_app.logger.warning(
                    "Password reset email delivery failed for user_id=%s", user.id
                )
            db.session.add(
                AuditLog(
                    actor_user_id=user.id,
                    event="password.reset_requested",
                    entity_type="user",
                    entity_id=str(user.id),
                )
            )
            db.session.commit()
        flash("If that email belongs to an account, recovery instructions will be sent.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html", form=form)


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def reset_password(token: str):
    user = _load_reset_user(token)
    if not user:
        flash("That password reset link is invalid or has expired.", "warning")
        return redirect(url_for("auth.forgot_password"))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        user.auth_version = int(user.auth_version or 1) + 1
        db.session.add(
            AuditLog(
                actor_user_id=user.id,
                event="password.reset_completed",
                entity_type="user",
                entity_id=str(user.id),
            )
        )
        db.session.commit()
        session.clear()
        logout_user()
        flash("Your password has been updated. You can now sign in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", form=form, token=token)
