from datetime import UTC, datetime

from app.services.telegram_service import (
    bind_contact,
    claim_update,
    create_link_token,
    finish_update,
    pop_update_claim,
    prepare_contact_link,
    push_update_claim,
    renew_update_claim,
    verify_webhook_secret,
)


def test_registered_number_can_bind_and_activates_seven_days(app, user_factory):
    user = user_factory()
    raw_token, _record = create_link_token(user)
    prepare_contact_link(raw_token, "777001", "777001")

    result = bind_contact(
        "777001",
        "777001",
        {"user_id": 777001, "phone_number": "+91 98765 43210"},
        username="asha_student",
    )

    assert result.success is True
    assert user.telegram_connected is True
    assert user.alert_status == "active"
    assert user.active_until > datetime.now(UTC).replace(tzinfo=None)


def test_mismatched_number_is_rejected(app, user_factory):
    user = user_factory()
    raw_token, _record = create_link_token(user)
    prepare_contact_link(raw_token, "777002", "777002")

    result = bind_contact(
        "777002",
        "777002",
        {"user_id": 777002, "phone_number": "+91 91234 56789"},
    )

    assert result.success is False
    assert result.reason == "phone_mismatch"
    assert user.telegram_connected is False


def test_webhook_secret_fails_closed(app):
    assert verify_webhook_secret("test-webhook-secret") is True
    assert verify_webhook_secret("wrong") is False
    app.config["TELEGRAM_WEBHOOK_SECRET"] = ""
    assert verify_webhook_secret("") is False


def test_failed_webhook_update_can_retry_but_completed_update_cannot(app):
    first_claim = claim_update(6001, commit=True)
    assert first_claim == 1
    assert finish_update(
        6001,
        claim_attempt=first_claim,
        success=False,
        error="temporary failure",
    ) is True
    retry_claim = claim_update(6001, commit=True)
    assert retry_claim == 2
    assert finish_update(6001, claim_attempt=first_claim, success=True) is False
    assert finish_update(6001, claim_attempt=retry_claim, success=True) is True
    assert claim_update(6001, commit=True) is None


def test_processing_webhook_is_not_reclaimed_while_its_worker_runs(app):
    first_claim = claim_update(6002, commit=True)

    assert first_claim == 1
    assert claim_update(6002, commit=True) == 0
    assert finish_update(6002, claim_attempt=first_claim, success=True) is True


def test_abandoned_webhook_claim_can_be_recovered_after_lease(app):
    started = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    first_claim = claim_update(6004, now=started, commit=True)

    recovered_claim = claim_update(
        6004,
        now=started.replace(minute=6),
        commit=True,
    )

    assert first_claim == 1
    assert recovered_claim == 2
    assert finish_update(6004, claim_attempt=first_claim, success=True) is False
    assert finish_update(6004, claim_attempt=recovered_claim, success=True) is True


def test_webhook_heartbeat_keeps_the_current_claim_fresh(app):
    started = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    first_claim = claim_update(6005, now=started, commit=True)
    token = push_update_claim(6005, first_claim)
    try:
        renew_update_claim(now=started.replace(minute=4), commit=True)
    finally:
        pop_update_claim(token)

    assert claim_update(6005, now=started.replace(minute=6), commit=True) == 0
    assert finish_update(6005, claim_attempt=first_claim, success=True) is True


def test_busy_webhook_returns_retryable_error(app, client):
    assert claim_update(6003, commit=True) == 1

    response = client.post(
        "/integrations/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
        json={"update_id": 6003, "message": {}},
    )

    assert response.status_code == 503
    assert response.get_json() == {"ok": False, "retry": True}


def test_webhook_does_not_acknowledge_a_lost_finish_claim(app, client, monkeypatch):
    monkeypatch.setattr(
        "app.integrations.routes.finish_update",
        lambda *args, **kwargs: False,
    )

    response = client.post(
        "/integrations/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
        json={"update_id": 6006, "message": {}},
    )

    assert response.status_code == 503
    assert response.get_json() == {"ok": False, "retry": True}


def test_unregistered_bot_user_receives_no_job_links(app, client):
    response = client.post(
        "/integrations/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
        json={
            "update_id": 1001,
            "message": {
                "message_id": 1,
                "from": {"id": 818181, "first_name": "Unknown"},
                "chat": {"id": 818181, "type": "private"},
                "text": "/jobs",
            },
        },
    )

    assert response.status_code == 200
    outbox = app.extensions["job_matcher_telegram_outbox"]
    assert len(outbox) == 1
    message = outbox[0]["payload"]["text"]
    assert "Connect from Job Matcher first" in message
    assert "job:" not in message
