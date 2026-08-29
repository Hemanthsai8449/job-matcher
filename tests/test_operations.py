from app.operations import routes


def test_health_checks_database(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"database": "ok", "status": "ok"}
    assert response.headers["Cache-Control"] == "no-store"


def test_cron_endpoint_rejects_missing_or_wrong_secret(client):
    assert client.get("/operations/cron/sync-jobs").status_code == 401
    response = client.get(
        "/operations/cron/sync-jobs",
        headers={"Authorization": "Bearer incorrect-secret"},
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "Unauthorized scheduler request."


def test_cron_endpoint_fails_closed_without_server_secret(app, client):
    app.config["CRON_SECRET"] = ""

    response = client.get(
        "/operations/cron/sync-jobs",
        headers={"Authorization": "Bearer anything"},
    )

    assert response.status_code == 503


def test_authorized_job_sync_returns_service_summary(app, client, monkeypatch):
    monkeypatch.setattr(routes, "fetch_and_upsert_all", lambda: {"created": 7})

    response = client.get(
        "/operations/cron/sync-jobs",
        headers={"Authorization": f"Bearer {app.config['CRON_SECRET']}"},
    )

    assert response.status_code == 200
    assert response.get_json()["result"] == {"created": 7}


def test_authorized_alert_dispatch_returns_service_summary(app, client, monkeypatch):
    monkeypatch.setattr(routes, "dispatch_due_alerts", lambda: {"sent": 3})

    headers = {"Authorization": f"Bearer {app.config['CRON_SECRET']}"}
    response = client.get("/operations/cron/dispatch-alerts", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["result"] == {"sent": 3}
    assert client.get("/operations/cron/dispatch-alerts/0", headers=headers).status_code == 200
    assert client.get("/operations/cron/dispatch-alerts/23", headers=headers).status_code == 200
    assert client.get("/operations/cron/dispatch-alerts/24", headers=headers).status_code == 404


def test_authorized_cleanup_returns_service_summary(app, client, monkeypatch):
    monkeypatch.setattr(
        routes,
        "process_pending_private_files",
        lambda: {"checked": 1, "deleted": 1, "failed": 0},
    )

    response = client.get(
        "/operations/cron/cleanup-private-files",
        headers={"Authorization": f"Bearer {app.config['CRON_SECRET']}"},
    )

    assert response.status_code == 200
    assert response.get_json()["result"]["deleted"] == 1
