"""Payment methods (payments.py, strategy pattern): the catalogue, checkout via Stripe and
PayPal, and their webhooks — all against a fake HTTP transport that records what the strategy
would send to the provider and answers like the provider's REST API does."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import headers, register

import config
import database
import models
import payments


class FakeHttp:
    """Routes (METHOD, path) → response or callable(data, headers) → (status, body)."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url, dict(headers or {}), data))
        handler = self.routes.get((method, urlsplit(url).path))
        if handler is None:
            return 404, {"error": {"message": f"no route for {method} {url}"}}
        return handler(data, headers or {}) if callable(handler) else handler

    def sent(self, method, path):
        return [c for c in self.calls if c[0] == method and urlsplit(c[1]).path == path]


@pytest.fixture()
def stripe_env(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_stripe")
    http = FakeHttp(
        {("POST", "/v1/checkout/sessions"): (200, {"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"})}
    )
    monkeypatch.setattr(payments.STRIPE, "http", http)
    return http


@pytest.fixture()
def paypal_env(monkeypatch):
    monkeypatch.setattr(config, "PAYPAL_CLIENT_ID", "client")
    monkeypatch.setattr(config, "PAYPAL_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "PAYPAL_WEBHOOK_ID", "WH-1")
    monkeypatch.setattr(config, "PAYPAL_PLAN_PRO_MONTHLY", "P-MONTH")
    monkeypatch.setattr(config, "PAYPAL_PLAN_PRO_YEARLY", "P-YEAR")
    monkeypatch.setattr(config, "PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com")
    http = FakeHttp(
        {
            ("POST", "/v1/oauth2/token"): (200, {"access_token": "A21.token", "expires_in": 3600}),
            ("POST", "/v1/billing/subscriptions"): (
                201,
                {
                    "id": "I-SUB1",
                    "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/approve/I-SUB1"}],
                },
            ),
            ("POST", "/v2/checkout/orders"): (
                201,
                {
                    "id": "ORDER1",
                    "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER1"}],
                },
            ),
            ("POST", "/v1/notifications/verify-webhook-signature"): (200, {"verification_status": "SUCCESS"}),
        }
    )
    monkeypatch.setattr(payments.PAYPAL, "http", http)
    payments.PAYPAL._token = None
    return http


def stripe_post(client, event: dict, *, signature: str | None = None):
    body = json.dumps(event).encode()
    sig = signature if signature is not None else payments.StripePayment.sign(body)
    return client.post(
        "/api/v1/billing/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
    )


PAYPAL_HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/cert.pem",
    "PAYPAL-TRANSMISSION-ID": "tx-1",
    "PAYPAL-TRANSMISSION-SIG": "sig",
    "PAYPAL-TRANSMISSION-TIME": "2026-01-01T00:00:00Z",
    "Content-Type": "application/json",
}


def paypal_post(client, event: dict, hdrs: dict | None = None):
    return client.post(
        "/api/v1/billing/webhooks/paypal", content=json.dumps(event).encode(), headers=hdrs or PAYPAL_HEADERS
    )


def entitlements(client, s):
    return client.get("/api/v1/me/entitlements", headers=headers(s)).json()


# ---------------------------------------------------------------------------- catalogue


def test_methods_catalogue_lists_both_strategies_and_the_priced_plans(client, monkeypatch):
    # Nothing configured: both methods are listed (the client shows their buttons) but unavailable.
    cat = client.get("/api/v1/billing/methods").json()
    assert [m["name"] for m in cat["methods"]] == ["stripe", "paypal"] and cat["default"] is None
    assert all(m["available"] is False and m["sandbox"] is False for m in cat["methods"])
    assert cat["methods"][0]["plans"] == ["pro_monthly", "pro_yearly", "lifetime"]
    # The plans carry the configured prices (PRICE_* → 1 / 10 / 20 by default) and refund rules.
    assert cat["currency"] == "USD"
    assert [(p["id"], p["amount"], p["amount_cents"], p["interval"]) for p in cat["plans"]] == [
        ("pro_monthly", "1.00", 100, "month"),
        ("pro_yearly", "10.00", 1000, "year"),
        ("lifetime", "20.00", 2000, None),
    ]
    assert "unused days" in cat["plans"][0]["refund_rule"] and "10.00 USD back" in cat["plans"][2]["refund_rule"]
    monkeypatch.setattr(config, "PRICE_PRO_MONTHLY", Decimal("2.50"))
    monkeypatch.setattr(config, "REFUND_LIFETIME", Decimal("5"))
    cat = client.get("/api/v1/billing/methods").json()
    assert cat["plans"][0]["amount"] == "2.50" and "5.00 USD back" in cat["plans"][2]["refund_rule"]

    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_live_1")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec")
    monkeypatch.setattr(config, "PAYPAL_CLIENT_ID", "c")
    monkeypatch.setattr(config, "PAYPAL_CLIENT_SECRET", "s")
    monkeypatch.setattr(config, "PAYPAL_WEBHOOK_ID", "w")
    monkeypatch.setattr(config, "PAYPAL_PLAN_PRO_MONTHLY", "P-1")
    cat = client.get("/api/v1/billing/methods").json()
    by_name = {m["name"]: m for m in cat["methods"]}
    assert cat["default"] == "stripe"  # BILLING_PROVIDER default
    assert by_name["stripe"]["plans"] == ["pro_monthly", "pro_yearly", "lifetime"]  # ad-hoc prices: every plan
    assert by_name["stripe"]["sandbox"] is False and by_name["stripe"]["available"] is True
    # PayPal subscriptions need a Plan id each; Lifetime is an order at the configured price.
    assert by_name["paypal"]["plans"] == ["pro_monthly", "lifetime"] and by_name["paypal"]["sandbox"] is True
    monkeypatch.setattr(config, "PAYPAL_ENV", "live")
    assert client.get("/api/v1/billing/methods").json()["methods"][1]["sandbox"] is False

    # BILLING_METHODS narrows and orders; BILLING_PROVIDER picks the default when configured.
    monkeypatch.setattr(config, "BILLING_METHODS", ["paypal", "stripe"])
    cat = client.get("/api/v1/billing/methods").json()
    assert [m["name"] for m in cat["methods"]] == ["paypal", "stripe"] and cat["default"] == "stripe"
    monkeypatch.setattr(config, "BILLING_PROVIDER", "nope")  # not configured → first configured
    assert client.get("/api/v1/billing/methods").json()["default"] == "paypal"
    monkeypatch.setattr(config, "BILLING_METHODS", ["paypal"])
    assert [m["name"] for m in client.get("/api/v1/billing/methods").json()["methods"]] == ["paypal"]


def test_checkout_explains_an_unconfigured_method(client, monkeypatch):
    s = register(client)
    r = client.post("/api/v1/billing/checkout", json={"plan": "pro_yearly", "method": "stripe"}, headers=headers(s))
    assert r.status_code == 503 and "Card (Stripe) payments are not set up" in r.json()["detail"]
    r = client.post("/api/v1/billing/checkout", json={"plan": "pro_yearly"}, headers=headers(s))
    assert r.status_code == 503 and "No payment method" in r.json()["detail"]
    r = client.post("/api/v1/billing/checkout", json={"plan": "pro_yearly", "method": "bitcoin"}, headers=headers(s))
    assert r.status_code == 404
    monkeypatch.setattr(config, "BILLING_METHODS", ["stripe"])
    r = client.post("/api/v1/billing/checkout", json={"plan": "pro_yearly", "method": "paypal"}, headers=headers(s))
    assert r.status_code == 404  # disabled by the operator: not offered at all
    # Webhooks for unconfigured methods are refused (nothing could verify them).
    assert client.post("/api/v1/billing/webhooks/paypal", content=b"{}").status_code == 404
    assert client.post("/api/v1/billing/webhooks/stripe", content=b"{}").status_code == 404


# ---------------------------------------------------------------------------- Stripe


def test_stripe_checkout_creates_hosted_session(client, stripe_env):
    s = register(client)
    r = client.post(
        "/api/v1/billing/checkout",
        json={"plan": "pro_yearly", "method": "stripe", "return_url": "https://app.example/?billing=done"},
        headers=headers(s),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"checkout_url": "https://checkout.stripe.com/c/cs_1", "provider": "stripe"}

    (method, url, hdrs, data) = stripe_env.calls[0]
    assert method == "POST" and url == "https://api.stripe.com/v1/checkout/sessions"
    assert hdrs["Authorization"] == "Bearer sk_test_123"
    form = {k: v[0] for k, v in parse_qs(data.decode()).items()}
    assert form["mode"] == "subscription" and "line_items[0][price]" not in form
    # The configured price travels with the session (no Price objects to create at Stripe).
    assert form["line_items[0][price_data][unit_amount]"] == "1000"  # PRICE_PRO_YEARLY 10.00
    assert form["line_items[0][price_data][currency]"] == "usd"
    assert form["line_items[0][price_data][recurring][interval]"] == "year"
    assert form["line_items[0][price_data][product_data][name]"] == "Personal Assistant — Pro — yearly"
    assert form["client_reference_id"] == s["account_id"] == form["subscription_data[metadata][account_id]"]
    assert form["customer_email"] == "ann@example.com"
    assert form["success_url"] == "https://app.example/?billing=done"
    assert form["cancel_url"] == "https://app.example/?billing=canceled"

    r = client.post("/api/v1/billing/checkout", json={"plan": "lifetime", "method": "stripe"}, headers=headers(s))
    assert r.status_code == 200
    form = {k: v[0] for k, v in parse_qs(stripe_env.calls[1][3].decode()).items()}
    assert form["mode"] == "payment" and form["line_items[0][price_data][unit_amount]"] == "2000"
    assert "line_items[0][price_data][recurring][interval]" not in form
    assert "subscription_data[metadata][account_id]" not in form


def test_checkout_during_a_trial_defers_the_first_charge(client, stripe_env, paypal_env):
    s = register(client)
    client.post("/api/v1/billing/trial", headers=headers(s))
    trial_end = client.get("/api/v1/me/entitlements", headers=headers(s)).json()["current_period_end"]

    client.post("/api/v1/billing/checkout", json={"plan": "pro_yearly", "method": "stripe"}, headers=headers(s))
    form = {k: v[0] for k, v in parse_qs(stripe_env.calls[-1][3].decode()).items()}
    assert form["subscription_data[trial_end]"] == str(int(payments.from_iso(trial_end).timestamp()))
    client.post("/api/v1/billing/checkout", json={"plan": "lifetime", "method": "stripe"}, headers=headers(s))
    assert "subscription_data[trial_end]" not in parse_qs(stripe_env.calls[-1][3].decode())

    client.post("/api/v1/billing/checkout", json={"plan": "pro_monthly", "method": "paypal"}, headers=headers(s))
    payload = json.loads(paypal_env.sent("POST", "/v1/billing/subscriptions")[-1][3])
    assert payload["start_time"] == payments.from_iso(trial_end).strftime("%Y-%m-%dT%H:%M:%SZ")

    # A trial that ends within 48 h cannot be deferred by Stripe: the subscription starts now.
    with database.SessionLocal() as db:
        sub = db.get(models.Subscription, s["account_id"])
        sub.current_period_end = models.utcnow() + timedelta(hours=12)
        db.commit()
    client.post("/api/v1/billing/checkout", json={"plan": "pro_yearly", "method": "stripe"}, headers=headers(s))
    assert "subscription_data[trial_end]" not in parse_qs(stripe_env.calls[-1][3].decode())
    # Not on a trial at all: nothing to carry over.
    s2 = register(client, email="bob@example.com")
    client.post("/api/v1/billing/checkout", json={"plan": "pro_monthly", "method": "paypal"}, headers=headers(s2))
    assert "start_time" not in json.loads(paypal_env.sent("POST", "/v1/billing/subscriptions")[-1][3])


def test_paypal_only_sells_subscriptions_with_a_plan_id(client, paypal_env, monkeypatch):
    monkeypatch.setattr(config, "PAYPAL_PLAN_PRO_YEARLY", "")
    s = register(client)
    r = client.post("/api/v1/billing/checkout", json={"plan": "pro_yearly", "method": "paypal"}, headers=headers(s))
    assert r.status_code == 400 and "PayPal" in r.json()["detail"]
    r = client.post("/api/v1/billing/checkout", json={"plan": "lifetime", "method": "paypal"}, headers=headers(s))
    assert r.status_code == 200  # a one-off order needs no plan at PayPal


def test_stripe_api_errors_surface_as_502(client, stripe_env):
    stripe_env.routes[("POST", "/v1/checkout/sessions")] = (400, {"error": {"message": "Invalid currency: xxx"}})
    s = register(client)
    r = client.post("/api/v1/billing/checkout", json={"plan": "pro_yearly", "method": "stripe"}, headers=headers(s))
    assert r.status_code == 502 and "Invalid currency" in r.json()["detail"]


def test_stripe_webhook_signature(client, stripe_env):
    event = {"id": "evt_x", "type": "ping", "data": {"object": {}}}
    assert stripe_post(client, event, signature="").status_code == 400
    assert stripe_post(client, event, signature="t=abc,v1=zzz").status_code == 400
    body = json.dumps(event).encode()
    wrong_secret = payments.StripePayment.sign(body, secret="whsec_other")
    assert stripe_post(client, event, signature=wrong_secret).status_code == 400
    stale = payments.StripePayment.sign(body, timestamp=int(time.time()) - 3600)
    assert stripe_post(client, event, signature=stale).status_code == 400
    # Several v1 signatures (secret rotation): any valid one is accepted.
    good = payments.StripePayment.sign(body)
    r = stripe_post(client, event, signature=good + ",v1=deadbeef")
    assert r.status_code == 200 and r.json() == {"ok": True, "ignored": True}
    assert stripe_post(client, {"no": "id"}).status_code == 422


def test_stripe_subscription_lifecycle(client, stripe_env):
    s = register(client)
    acc = s["account_id"]
    period_end = int(time.time()) + 30 * 86400

    # 1. The subscription object is what grants Pro (the checkout session itself adds nothing
    #    in subscription mode — it would only clobber a trial status).
    session_done = {
        "id": "evt_0",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "mode": "subscription",
                "payment_status": "paid",
                "client_reference_id": acc,
                "customer": "cus_1",
                "subscription": "sub_1",
            }
        },
    }
    assert stripe_post(client, session_done).json()["ignored"] is True
    r = stripe_post(
        client,
        {
            "id": "evt_1",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_1",
                    "customer": "cus_1",
                    "status": "active",
                    "current_period_end": period_end,
                    "metadata": {"account_id": acc, "plan": "pro_monthly"},
                }
            },
        },
    )
    assert r.status_code == 200 and r.json()["duplicate"] is False
    ent = entitlements(client, s)
    assert ent["plan"] == "pro" and ent["status"] == "active" and "sync" in ent["features"]

    # 2. The subscription object carries the period (either order of arrival is fine).
    r = stripe_post(
        client,
        {
            "id": "evt_2",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_1",
                    "customer": "cus_1",
                    "status": "active",
                    "cancel_at_period_end": True,
                    "items": {"data": [{"current_period_end": period_end}]},  # API ≥ 2025-03-31 shape
                    "metadata": {"account_id": acc, "plan": "pro_monthly"},
                }
            },
        },
    )
    assert r.status_code == 200
    ent = entitlements(client, s)
    assert ent["cancel_at_period_end"] is True and ent["current_period_end"][:10] == time.strftime(
        "%Y-%m-%d", time.gmtime(period_end)
    )

    # 3. A failed renewal names only the subscription → resolved through the stored id.
    failed = {
        "id": "evt_3",
        "type": "invoice.payment_failed",
        "data": {"object": {"customer": "cus_1", "parent": {"subscription_details": {"subscription": "sub_1"}}}},
    }
    r = stripe_post(client, failed)
    assert r.status_code == 200 and r.json()["duplicate"] is False
    ent = entitlements(client, s)
    assert ent["status"] == "past_due" and ent["plan"] == "pro"  # dunning grace

    # 4. Replay of an event is a no-op; 'incomplete' subscriptions change nothing.
    assert stripe_post(client, failed).json()["duplicate"] is True
    r = stripe_post(
        client,
        {
            "id": "evt_4",
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_1", "status": "incomplete"}},
        },
    )
    assert r.json()["ignored"] is True and entitlements(client, s)["status"] == "past_due"

    # 5. Gone.
    r = stripe_post(
        client,
        {
            "id": "evt_5",
            "type": "customer.subscription.deleted",
            "data": {
                "object": {"id": "sub_1", "customer": "cus_1", "status": "canceled", "metadata": {"account_id": acc}}
            },
        },
    )
    assert r.status_code == 200
    ent = entitlements(client, s)
    assert ent["plan"] == "free" and ent["status"] == "canceled" and ent["features"] == []


def test_stripe_lifetime_purchase(client, stripe_env):
    s = register(client)
    client.post("/api/v1/billing/trial", headers=headers(s))  # has a trial period end
    pending = {
        "id": "evt_p",
        "type": "checkout.session.completed",
        "data": {"object": {"mode": "payment", "payment_status": "unpaid", "client_reference_id": s["account_id"]}},
    }
    assert stripe_post(client, pending).json()["ignored"] is True  # bank transfer still pending
    paid = {
        "id": "evt_l",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "mode": "payment",
                "payment_status": "paid",
                "client_reference_id": s["account_id"],
                "customer": "cus_9",
                "metadata": {"plan": "lifetime"},
            }
        },
    }
    assert stripe_post(client, paid).status_code == 200
    ent = entitlements(client, s)
    assert ent["plan"] == "lifetime" and ent["status"] == "active" and ent["current_period_end"] is None


# ---------------------------------------------------------------------------- PayPal


def test_paypal_checkout_subscription_and_order(client, paypal_env):
    s = register(client)
    r = client.post(
        "/api/v1/billing/checkout",
        json={"plan": "pro_monthly", "method": "paypal", "return_url": "https://app.example/?billing=done"},
        headers=headers(s),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"checkout_url": "https://www.sandbox.paypal.com/approve/I-SUB1", "provider": "paypal"}

    token_call = paypal_env.sent("POST", "/v1/oauth2/token")[0]
    assert token_call[2]["Authorization"].startswith("Basic ") and token_call[3] == b"grant_type=client_credentials"
    sub_call = paypal_env.sent("POST", "/v1/billing/subscriptions")[0]
    assert sub_call[2]["Authorization"] == "Bearer A21.token"
    payload = json.loads(sub_call[3])
    assert payload["plan_id"] == "P-MONTH" and payload["custom_id"] == f"{s['account_id']}:pro_monthly"
    assert payload["subscriber"] == {"email_address": "ann@example.com"}
    assert payload["application_context"]["return_url"] == "https://app.example/?billing=done"
    assert payload["application_context"]["cancel_url"] == "https://app.example/?billing=canceled"
    assert payload["application_context"]["user_action"] == "SUBSCRIBE_NOW"

    r = client.post("/api/v1/billing/checkout", json={"plan": "lifetime", "method": "paypal"}, headers=headers(s))
    assert r.status_code == 200 and r.json()["checkout_url"].endswith("token=ORDER1")
    order = json.loads(paypal_env.sent("POST", "/v2/checkout/orders")[0][3])
    unit = order["purchase_units"][0]
    assert order["intent"] == "CAPTURE" and unit["amount"] == {
        "currency_code": "USD",
        "value": "20.00",
    }  # PRICE_LIFETIME
    assert unit["custom_id"] == f"{s['account_id']}:lifetime"
    assert len(paypal_env.sent("POST", "/v1/oauth2/token")) == 1  # token cached


def test_paypal_api_errors_surface_as_502(client, paypal_env):
    s = register(client)
    paypal_env.routes[("POST", "/v1/oauth2/token")] = (401, {"error": "invalid_client"})
    payments.PAYPAL._token = None
    r = client.post("/api/v1/billing/checkout", json={"plan": "pro_yearly", "method": "paypal"}, headers=headers(s))
    assert r.status_code == 502 and "credentials" in r.json()["detail"]
    paypal_env.routes[("POST", "/v1/oauth2/token")] = (200, {"access_token": "t", "expires_in": 100})
    paypal_env.routes[("POST", "/v1/billing/subscriptions")] = (422, {"message": "The requested plan is not active."})
    r = client.post("/api/v1/billing/checkout", json={"plan": "pro_yearly", "method": "paypal"}, headers=headers(s))
    assert r.status_code == 502 and "plan is not active" in r.json()["detail"]


def test_paypal_webhook_is_verified_with_paypal(client, paypal_env):
    event = {"id": "WH-EVT-0", "event_type": "BILLING.SUBSCRIPTION.CREATED", "resource": {}}
    assert paypal_post(client, event, {"Content-Type": "application/json"}).status_code == 400  # no headers
    paypal_env.routes[("POST", "/v1/notifications/verify-webhook-signature")] = (
        200,
        {"verification_status": "FAILURE"},
    )
    assert paypal_post(client, event).status_code == 400
    paypal_env.routes[("POST", "/v1/notifications/verify-webhook-signature")] = (
        200,
        {"verification_status": "SUCCESS"},
    )
    r = paypal_post(client, event)
    assert r.status_code == 200 and r.json()["ignored"] is True
    sent = json.loads(paypal_env.sent("POST", "/v1/notifications/verify-webhook-signature")[-1][3])
    assert sent["webhook_id"] == "WH-1" and sent["transmission_id"] == "tx-1" and sent["webhook_event"] == event
    assert sent["auth_algo"] == "SHA256withRSA" and sent["cert_url"].startswith("https://")


def test_paypal_subscription_lifecycle(client, paypal_env):
    s = register(client)
    acc = s["account_id"]
    subscription = {
        "id": "I-SUB1",
        "status": "ACTIVE",
        "custom_id": f"{acc}:pro",
        "subscriber": {"payer_id": "PAYER1"},
        "billing_info": {"next_billing_time": "2030-02-01T10:00:00Z"},
    }
    r = paypal_post(client, {"id": "WH-1", "event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": subscription})
    assert r.status_code == 200 and r.json()["duplicate"] is False
    ent = entitlements(client, s)
    assert ent["plan"] == "pro" and ent["status"] == "active" and ent["current_period_end"].startswith("2030-02-01")

    # Renewal: PAYMENT.SALE.COMPLETED only names the subscription; the strategy asks PayPal for it.
    paypal_env.routes[("GET", "/v1/billing/subscriptions/I-SUB1")] = (
        200,
        {**subscription, "billing_info": {"next_billing_time": "2030-03-01T10:00:00Z"}},
    )
    sale = {
        "id": "WH-2",
        "event_type": "PAYMENT.SALE.COMPLETED",
        "resource": {"id": "SALE1", "billing_agreement_id": "I-SUB1"},
    }
    assert paypal_post(client, sale).status_code == 200
    assert entitlements(client, s)["current_period_end"].startswith("2030-03-01")

    failed = {"id": "WH-3", "event_type": "BILLING.SUBSCRIPTION.PAYMENT.FAILED", "resource": {"id": "I-SUB1"}}
    assert paypal_post(client, failed).status_code == 200
    assert entitlements(client, s)["status"] == "past_due"  # resolved via provider_subscription_id

    suspended = {
        "id": "WH-4",
        "event_type": "BILLING.SUBSCRIPTION.SUSPENDED",
        "resource": {**subscription, "status": "SUSPENDED"},
    }
    assert paypal_post(client, suspended).status_code == 200
    assert entitlements(client, s)["status"] == "past_due"

    cancelled = {
        "id": "WH-5",
        "event_type": "BILLING.SUBSCRIPTION.CANCELLED",
        "resource": {**subscription, "status": "CANCELLED"},
    }
    assert paypal_post(client, cancelled).status_code == 200
    ent = entitlements(client, s)
    # PayPal stops charging at once, but the period that was paid for stays usable.
    assert ent["plan"] == "pro" and ent["status"] == "canceled" and ent["cancel_at_period_end"] is True
    assert ent["access_until"].startswith("2030-02-01") and ent["renews"] is False and ent["can_cancel"] is False
    assert paypal_post(client, cancelled).json()["duplicate"] is True
    with database.SessionLocal() as db:
        sub = db.get(models.Subscription, acc)
        sub.current_period_end = models.utcnow() - timedelta(minutes=1)
        db.commit()
    ent = entitlements(client, s)
    assert ent["plan"] == "free" and ent["status"] == "canceled" and ent["access_until"] is None


def test_paypal_lifetime_order_is_captured_from_the_webhook(client, paypal_env):
    s = register(client)
    acc = s["account_id"]
    paypal_env.routes[("POST", "/v2/checkout/orders/ORDER1/capture")] = (
        201,
        {"id": "ORDER1", "status": "COMPLETED", "payer": {"payer_id": "PAYER1"}},
    )
    approved = {
        "id": "WH-A",
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "resource": {"id": "ORDER1", "purchase_units": [{"custom_id": f"{acc}:lifetime"}]},
    }
    r = paypal_post(client, approved)
    assert r.status_code == 200 and r.json()["duplicate"] is False
    assert len(paypal_env.sent("POST", "/v2/checkout/orders/ORDER1/capture")) == 1
    ent = entitlements(client, s)
    assert ent["plan"] == "lifetime" and ent["status"] == "active" and ent["current_period_end"] is None

    # PayPal then reports the capture itself — same outcome, nothing captured twice.
    captured = {
        "id": "WH-C",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "CAP1", "custom_id": f"{acc}:lifetime"},
    }
    assert paypal_post(client, captured).status_code == 200
    assert len(paypal_env.sent("POST", "/v2/checkout/orders/ORDER1/capture")) == 1
    assert entitlements(client, s)["plan"] == "lifetime"

    # A re-delivered approval is recognised before anything is captured again …
    assert paypal_post(client, approved).json()["duplicate"] is True
    assert len(paypal_env.sent("POST", "/v2/checkout/orders/ORDER1/capture")) == 1
    # … and a *new* approval event for an already captured order is harmless (ORDER_ALREADY_CAPTURED).
    paypal_env.routes[("POST", "/v2/checkout/orders/ORDER1/capture")] = (
        422,
        {"details": [{"issue": "ORDER_ALREADY_CAPTURED"}]},
    )
    assert paypal_post(client, {**approved, "id": "WH-A2"}).status_code == 200

    # A capture that fails is not acknowledged, so PayPal retries the delivery later.
    paypal_env.routes[("POST", "/v2/checkout/orders/ORDER1/capture")] = (
        422,
        {"details": [{"issue": "INSTRUMENT_DECLINED"}]},
    )
    assert paypal_post(client, {**approved, "id": "WH-A3"}).status_code == 502
