"""The entitlement model (billing.py) independent of any payment provider: trial, the neutral
PaymentEvent applied idempotently, cancellation, dunning grace. Provider-specific checkout and
webhooks live in test_payments.py."""

from datetime import timedelta

from conftest import headers, register

import billing
import database
import models
import schemas


def apply(event: dict, provider: str = "stripe") -> bool:
    with database.SessionLocal() as db:
        return billing.apply_webhook(db, schemas.PaymentEvent(**event), provider)


def entitlements(client, s):
    return client.get("/api/v1/me/entitlements", headers=headers(s)).json()


def test_trial_once_then_paid_subscription(client):
    s = register(client)
    ent = client.post("/api/v1/billing/trial", headers=headers(s)).json()
    assert ent["plan"] == "pro" and ent["status"] == "trialing" and "sync" in ent["features"]
    assert ent["trial_available"] is False and ent["membership"] == "paid"
    assert client.post("/api/v1/billing/trial", headers=headers(s)).status_code == 409

    period_end = models.utcnow() + timedelta(days=365)
    assert apply(
        {
            "event_id": "evt_1",
            "type": "subscription.created",
            "account_id": s["account_id"],
            "plan": "pro",
            "status": "active",
            "current_period_end": period_end.isoformat(),
            "customer_id": "cus_1",
            "subscription_id": "sub_1",
        }
    )
    ent = entitlements(client, s)
    assert ent["status"] == "active" and ent["plan"] == "pro" and ent["current_period_end"][:10] == str(period_end)[:10]


def test_events_are_idempotent_and_cancel_ends_pro(client):
    s = register(client)
    created = {"event_id": "evt_1", "type": "subscription.created", "account_id": s["account_id"], "plan": "pro"}
    assert apply(created) is True
    assert apply(created) is False  # seen before: no-op
    assert entitlements(client, s)["plan"] == "pro"

    assert apply({"event_id": "evt_2", "type": "subscription.canceled", "account_id": s["account_id"]})
    ent = entitlements(client, s)
    assert ent["plan"] == "free" and ent["status"] == "canceled" and ent["features"] == []
    assert ent["membership"] == "free" and ent["trial_available"] is False


def test_events_for_unknown_accounts_are_recorded_but_harmless(client):
    assert apply({"event_id": "evt_x", "type": "subscription.created", "account_id": "no-such-account"}) is True
    with database.SessionLocal() as db:
        assert db.query(models.BillingEvent).filter_by(provider_event_id="evt_x").one().processed_at is None


def test_lookup_by_provider_subscription_id(client):
    s = register(client)
    apply({"event_id": "e1", "type": "subscription.created", "account_id": s["account_id"], "subscription_id": "sub_9"})
    # A renewal that only names the provider's subscription (no account id).
    later = (models.utcnow() + timedelta(days=30)).isoformat()
    apply({"event_id": "e2", "type": "subscription.updated", "subscription_id": "sub_9", "current_period_end": later})
    ent = entitlements(client, s)
    assert ent["status"] == "active" and ent["current_period_end"][:10] == later[:10]
    # … but only for the same provider.
    apply({"event_id": "e3", "type": "subscription.canceled", "subscription_id": "sub_9"}, provider="paypal")
    assert entitlements(client, s)["status"] == "active"


def test_unknown_period_keeps_the_stored_one_and_lifetime_has_none(client):
    s = register(client)
    client.post("/api/v1/billing/trial", headers=headers(s))
    trial_end = entitlements(client, s)["current_period_end"]
    apply({"event_id": "e1", "type": "subscription.created", "account_id": s["account_id"], "status": "active"})
    assert entitlements(client, s)["current_period_end"] == trial_end
    apply({"event_id": "e2", "type": "transaction.completed", "account_id": s["account_id"], "plan": "lifetime"})
    ent = entitlements(client, s)
    assert ent["plan"] == "lifetime" and ent["current_period_end"] is None


def test_past_due_grace_period(client):
    s = register(client)
    client.post("/api/v1/billing/trial", headers=headers(s))
    apply({"event_id": "evt_fail", "type": "payment_failed", "account_id": s["account_id"]})
    assert entitlements(client, s)["plan"] == "pro"  # within grace
    with database.SessionLocal() as db:
        sub = db.get(models.Subscription, s["account_id"])
        sub.current_period_end = models.utcnow() - timedelta(days=10)
        db.commit()
    assert entitlements(client, s)["plan"] == "free"
