from app.extensions import db
from app.models import OTPCode
from app.services.otp_service import create_otp, resend_otp, verify_otp


def test_only_latest_otp_is_live_and_code_is_single_use(app, user_factory, monkeypatch):
    user = user_factory(email_verified_at=None)
    codes = iter(["111111", "222222"])
    monkeypatch.setattr(
        "app.services.otp_service.generate_numeric_code", lambda length=6: next(codes)
    )

    first = create_otp(user)
    second = create_otp(user)
    live = db.session.scalars(
        db.select(OTPCode).where(
            OTPCode.user_id == user.id,
            OTPCode.purpose == "verify_email",
            OTPCode.consumed_at.is_(None),
        )
    ).all()

    assert first == "111111"
    assert second == "222222"
    assert len(live) == 1
    assert verify_otp(user, first)[0] is False
    assert verify_otp(user, second)[0] is True
    assert verify_otp(user, second)[0] is False


def test_otp_attempt_limit_is_consumed_atomically(app, user_factory, monkeypatch):
    user = user_factory(email_verified_at=None)
    monkeypatch.setattr(
        "app.services.otp_service.generate_numeric_code", lambda length=6: "333333"
    )
    create_otp(user)

    results = [verify_otp(user, "000000") for _ in range(app.config["OTP_MAX_ATTEMPTS"])]

    assert results[-1][1] == "Too many incorrect attempts. Request a new code."
    assert verify_otp(user, "333333")[0] is False


def test_verified_account_cannot_issue_another_email_otp(app, user_factory, monkeypatch):
    user = user_factory(email_verified_at=None)
    monkeypatch.setattr(
        "app.services.otp_service.generate_numeric_code", lambda length=6: "444444"
    )
    create_otp(user)
    assert verify_otp(user, "444444")[0] is True

    code, message = resend_otp(user)

    assert code is None
    assert message == "This email address is already verified."


def test_commit_false_otp_is_removed_by_outer_rollback(app, user_factory, monkeypatch):
    user = user_factory(email_verified_at=None)
    monkeypatch.setattr(
        "app.services.otp_service.generate_numeric_code", lambda length=6: "555555"
    )

    create_otp(user, commit=False)
    db.session.rollback()

    assert db.session.scalar(
        db.select(OTPCode).where(OTPCode.user_id == user.id)
    ) is None
