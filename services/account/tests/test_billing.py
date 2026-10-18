import json

from conftest import headers, register


def test_trial_then_sandbox_checkout_then_webhook(client):
    s = register(client)
    ent = client.post("/api/v1/billing/trial", headers=headers(s)).json()
    assert ent["plan"] == "pro" and ent["status"] == "trialing" and "sync" in ent["features"]
    assert client.post("/api/v1/billing/trial", headers=headers(s)).status_code == 409

    r = client.post(
        "/api/v1/billing/checkout", json={"plan": "pro_yearly", "return_url": "/account"}, headers=headers(s)
    )
    assert r.status_code == 200 and r.json()["provider"] == "stub"
    url = r.json()["checkout_url"]
    page = client.get(url)
    assert page.status_code == 200 and "Sandbox checkout" in page.text
    confirm = client.post(url.replace("/sandbox?", "/sandbox/confirm?"), follow_redirects=False)
    assert confirm.status_code == 303 and confirm.headers["location"] == "/account"

    ent = client.get("/api/v1/me/entitlements", headers=headers(s)).json()
    assert ent["status"] == "active" and ent["plan"] == "pro" and ent["current_period_end"]


def test_webhook_signature_and_idempotency(client):
    import billing

    s = register(client)
    body = json.dumps(
        {
            "event_id": "evt_1",
            "type": "subscription.created",
            "account_id": s["account_id"],
            "plan": "pro",
            "status": "active",
        }
    ).encode()
    r = client.post(
        "/api/v1/billing/webhooks/stub",
        content=body,
        headers={"X-Signature": "nope", "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    sig = billing.StubProvider.sign(body)
    r = client.post(
        "/api/v1/billing/webhooks/stub", content=body, headers={"X-Signature": sig, "Content-Type": "application/json"}
    )
    assert r.status_code == 200 and r.json()["duplicate"] is False
    r = client.post(
        "/api/v1/billing/webhooks/stub", content=body, headers={"X-Signature": sig, "Content-Type": "application/json"}
    )
    assert r.json()["duplicate"] is True
    assert client.get("/api/v1/me/entitlements", headers=headers(s)).json()["plan"] == "pro"

    cancel = json.dumps({"event_id": "evt_2", "type": "subscription.canceled", "account_id": s["account_id"]}).encode()
    client.post(
        "/api/v1/billing/webhooks/stub",
        content=cancel,
        headers={"X-Signature": billing.StubProvider.sign(cancel), "Content-Type": "application/json"},
    )
    ent = client.get("/api/v1/me/entitlements", headers=headers(s)).json()
    assert ent["plan"] == "free" and ent["status"] == "canceled" and ent["features"] == []


def test_past_due_grace_period(client):
    from datetime import timedelta

    import billing
    import database
    import models

    s = register(client)
    client.post("/api/v1/billing/trial", headers=headers(s))
    body = json.dumps({"event_id": "evt_fail", "type": "payment_failed", "account_id": s["account_id"]}).encode()
    client.post(
        "/api/v1/billing/webhooks/stub",
        content=body,
        headers={"X-Signature": billing.StubProvider.sign(body), "Content-Type": "application/json"},
    )
    assert client.get("/api/v1/me/entitlements", headers=headers(s)).json()["plan"] == "pro"  # within grace
    with database.SessionLocal() as db:
        sub = db.get(models.Subscription, s["account_id"])
        sub.current_period_end = models.utcnow() - timedelta(days=10)
        db.commit()
    assert client.get("/api/v1/me/entitlements", headers=headers(s)).json()["plan"] == "free"
