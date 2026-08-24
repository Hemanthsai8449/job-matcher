import time

from flask import redirect

from app.extensions import db, oauth
from app.models import AuditLog, User
from tests.conftest import login_client


class FakeGoogleClient:
    def __init__(self, userinfo=None):
        self.userinfo = userinfo or {}
        self.redirect_uri = ""
        self.redirect_kwargs = {}

    def authorize_redirect(self, redirect_uri, **kwargs):
        self.redirect_uri = redirect_uri
        self.redirect_kwargs = kwargs
        return redirect("https://accounts.google.test/authorize")

    def authorize_access_token(self):
        return {"userinfo": self.userinfo, "access_token": "not-persisted"}


def google_identity(**overrides):
    values = {
        "sub": "google-subject-123",
        "email": "asha@example.com",
        "email_verified": True,
        "name": "Asha Student",
    }
    values.update(overrides)
    return values


def use_fake_google(monkeypatch, identity=None):
    fake = FakeGoogleClient(identity)
    monkeypatch.setattr(oauth, "create_client", lambda name: fake)
    return fake


def test_google_login_starts_oidc_with_nonce_and_canonical_callback(
    client, monkeypatch
):
    fake = use_fake_google(monkeypatch)

    response = client.get("/auth/google?next=/jobs")

    assert response.status_code == 302
    assert response.headers["Location"] == "https://accounts.google.test/authorize"
    assert fake.redirect_uri == "http://localhost/auth/google/callback"
    assert len(fake.redirect_kwargs["nonce"]) >= 32
    assert fake.redirect_kwargs["prompt"] == "select_account"
    with client.session_transaction() as session:
        assert session["google_login_next"] == "/jobs"


def test_verified_google_email_links_existing_account_and_signs_in(
    client, user_factory, monkeypatch
):
    user = user_factory(email="asha@example.com")
    use_fake_google(monkeypatch, google_identity())

    response = client.get("/auth/google/callback")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")
    db.session.refresh(user)
    assert user.google_sub == "google-subject-123"
    assert client.get("/dashboard").status_code == 200
    event = db.session.scalar(
        db.select(AuditLog).where(AuditLog.event == "account.google_linked")
    )
    assert event is not None


def test_google_subject_remains_identity_when_google_email_changes(
    client, user_factory, monkeypatch
):
    user = user_factory(email="original@example.com", google_sub="stable-google-sub")
    use_fake_google(
        monkeypatch,
        google_identity(sub="stable-google-sub", email="changed@example.com"),
    )

    response = client.get("/auth/google/callback")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")
    db.session.refresh(user)
    assert user.email == "original@example.com"


def test_new_google_user_adds_phone_and_accepts_terms_before_account_creation(
    client, monkeypatch
):
    use_fake_google(
        monkeypatch,
        google_identity(email="new.google@example.com", sub="new-google-sub"),
    )

    callback = client.get("/auth/google/callback")
    assert callback.status_code == 302
    assert callback.headers["Location"].endswith("/auth/google/complete")
    assert db.session.scalar(
        db.select(User).where(User.email == "new.google@example.com")
    ) is None

    response = client.post(
        "/auth/google/complete",
        data={
            "phone": "+91 98765 43210",
            "accept_terms": "y",
            "alerts_consent": "y",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/onboarding/resume")
    user = db.session.scalar(
        db.select(User).where(User.email == "new.google@example.com")
    )
    assert user is not None
    assert user.google_sub == "new-google-sub"
    assert user.password_hash is None
    assert user.email_verified_at is not None
    assert user.phone_e164 == "+919876543210"
    assert user.alerts_consent_at is not None


def test_unverified_google_email_is_rejected(client, monkeypatch):
    use_fake_google(
        monkeypatch,
        google_identity(email="unverified@example.com", email_verified=False),
    )

    response = client.get("/auth/google/callback")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/login")
    assert db.session.scalar(
        db.select(User).where(User.email == "unverified@example.com")
    ) is None


def test_google_only_account_can_be_deleted_after_fresh_login(
    app, client, user_factory
):
    user = user_factory(google_sub="delete-google-sub")
    user.password_hash = None
    db.session.commit()
    user_id = user.id
    login_client(client, user)
    with client.session_transaction() as session:
        session["google_authenticated_at"] = int(time.time())

    response = client.post("/account/delete", data={"confirmation": "DELETE"})

    assert response.status_code == 302
    assert db.session.get(User, user_id) is None
