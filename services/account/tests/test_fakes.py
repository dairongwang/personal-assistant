"""The built-in Stripe / PayPal fakes (fakes.py) driving the real strategies end to end:
checkout → the fake's hosted page → signed webhooks delivered in-process → entitlements and
the payments ledger; every lifecycle action the account screen offers (cancel with refund,
manage, renew, fail, recover, decline, refund from the provider's dashboard) for every paid
membership. Also the seeded paid accounts, which the fake Stripe adopts."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from conftest import headers, register

import clientcrypto
import config
import database
import fakes
import models
import seed

SPECS = {spec.email: spec for spec in seed.FAKE_ACCOUNTS}


@pytest.fixture(autouse=True)
def fake_providers(monkeypatch):
    monkeypatch.setattr(config, "BILLING_FAKE_PROVIDERS", True)
    fakes.reset()
    yield
    fakes.reset()


def login_seeded(client, email):
    """Sign in as a seeded account (keys derived the PWA's way, test-speed iterations)."""
    spec = SPECS[email]
    auth_key, _ = clientcrypto.derive_keys(spec.password, spec.email, 1000)
    r = client.post(
        "/api/v1/auth/login",
        json={"email": email, "auth_key": auth_key, "device": {"platform": "test", "client_version": "1.0.0"}},
    )
    assert r.status_code == 200, r.text
    return r.json()


def ent(client, s):
    return client.get("/api/v1/me/entitlements", headers=headers(s)).json()


def ledger(client, s):
    return client.get("/api/v1/billing/payments", headers=headers(s)).json()


def checkout(client, s, plan, method):
    r = client.post(
        "/api/v1/billing/checkout",
        json={"plan": plan, "method": method, "return_url": "http://app.test/?billing=done"},
        headers=headers(s),
    )
    assert r.status_code == 200, r.text
    return r.json()["checkout_url"]


def press(client, page_url, action_suffix):
    """Press the button on a fake page whose form posts to …/<action_suffix>."""
    page = client.get(page_url)
    assert page.status_code == 200 and "Built-in fake" in page.text, page.text
    match = re.search(rf'action="([^"]+/{re.escape(action_suffix)}(?:\?[^"]*)?)"', page.text)
    assert match, f"no '{action_suffix}' button on:\n{page.text}"
    r = client.post(match.group(1).replace("&amp;", "&"), follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"]


def pay_on_fake_stripe(client, url, *, declined=False):
    return press(client, url, "decline" if declined else "pay")


def approve_on_fake_paypal(client, url, *, declined=False):
    return press(client, url, "decline" if declined else "approve")


def portal(client, s):
    r = client.post("/api/v1/billing/manage", json={"return_url": "http://app.test/"}, headers=headers(s))
    assert r.status_code == 200, r.text
    return r.json()["url"]


def cancel(client, s):
    r = client.post("/api/v1/billing/cancel", headers=headers(s))
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------- catalogue


def test_fakes_make_both_methods_available_without_credentials(client):
    cat = client.get("/api/v1/billing/methods").json()
    by = {m["name"]: m for m in cat["methods"]}
    assert cat["default"] == "stripe"
    assert by["stripe"] == {
        **by["stripe"],
        "available": True,
        "fake": True,
        "sandbox": True,
        "plans": ["pro_monthly", "pro_yearly", "lifetime"],
    }
    assert by["paypal"]["available"] and by["paypal"]["fake"] and by["paypal"]["sandbox"]
    assert by["paypal"]["plans"] == ["pro_monthly", "pro_yearly", "lifetime"]
    assert [p["amount"] for p in cat["plans"]] == ["1.00", "10.00", "20.00"]


def test_fake_pages_are_hidden_without_the_flag(client, monkeypatch):
    monkeypatch.setattr(config, "BILLING_FAKE_PROVIDERS", False)
    assert client.get("/api/v1/billing/fake/stripe/checkout/cs_x").status_code == 404
    assert client.get("/api/v1/billing/fake/paypal/approve/order/ORD").status_code == 404
    assert client.get("/api/v1/billing/methods").json()["methods"][0]["available"] is False


# ---------------------------------------------------------------------------- Stripe


@pytest.mark.parametrize(
    "plan, amount, refund, basis",
    [
        ("pro_monthly", "1.00", "1.00", "prorated"),
        ("pro_yearly", "10.00", "10.00", "prorated"),
        ("lifetime", "20.00", "10.00", "flat"),
    ],
)
def test_stripe_pay_then_cancel_refunds_every_membership(client, plan, amount, refund, basis):
    s = register(client)
    url = checkout(client, s, plan, "stripe")
    assert url.startswith("/api/v1/billing/fake/stripe/checkout/cs_fake_")
    page = client.get(url).text
    assert f"Pay {amount} USD" in page and ("one-off payment" if plan == "lifetime" else "subscription") in page
    assert pay_on_fake_stripe(client, url) == "http://app.test/?billing=done"

    e = ent(client, s)
    assert e["plan"] == ("lifetime" if plan == "lifetime" else "pro") and e["status"] == "active"
    assert e["provider"] == "stripe" and e["can_cancel"] is True and e["can_manage"] is True
    assert e["last_payment"]["amount"] == amount and e["last_payment"]["status"] == "succeeded"
    assert e["last_payment"]["plan"] == plan
    rows = ledger(client, s)
    assert len(rows) == 1 and rows[0]["provider"] == "stripe" and rows[0]["amount"] == amount
    if plan != "lifetime":
        assert rows[0]["period_end"] == e["current_period_end"] and e["renews"] is True

    preview = client.get("/api/v1/billing/cancel/preview", headers=headers(s)).json()
    assert preview["basis"] == basis and Decimal(preview["amount"]) >= Decimal(refund) - Decimal("0.01")
    result = cancel(client, s)
    assert result["refunded_via"] == "provider" and result["refund_id"].startswith("re_fake_")
    assert Decimal(result["refund"]["amount"]) >= Decimal(refund) - Decimal("0.01")
    e = result["entitlements"]
    assert e["plan"] == "free" and e["status"] == "canceled" and e["can_cancel"] is False
    # The fake Stripe saw the refund and (for subscriptions) the cancellation.
    charge = next(iter(fakes.STRIPE.charges.values()))
    assert (
        charge["amount_refunded"] == result["refund"]["amount_cents"]
        and charge["refunds"][0]["id"] == result["refund_id"]
    )
    if plan != "lifetime":
        assert next(iter(fakes.STRIPE.subscriptions.values()))["status"] == "canceled"
    row = ledger(client, s)[0]
    assert row["refunded"] == result["refund"]["amount"] and row["refund_id"] == result["refund_id"]
    assert row["status"] == ("partially_refunded" if plan == "lifetime" else row["status"])
    assert client.post("/api/v1/billing/cancel", headers=headers(s)).status_code == 409
    # Subscribing again afterwards works (a fresh subscription, a fresh charge).
    pay_on_fake_stripe(client, checkout(client, s, "pro_monthly", "stripe"))
    assert ent(client, s)["renews"] is True and len(ledger(client, s)) == 2


def test_stripe_declined_cards(client):
    s = register(client)
    # Subscription checkout: a declined card completes nothing — no subscription, no charge.
    url = checkout(client, s, "pro_monthly", "stripe")
    declined = pay_on_fake_stripe(client, url, declined=True)
    assert declined.endswith("/declined") and "Your card was declined" in client.get(declined).text
    assert ent(client, s)["plan"] == "free" and ledger(client, s) == [] and not fakes.STRIPE.subscriptions
    pay_on_fake_stripe(client, url)  # the same session can still be paid
    assert ent(client, s)["plan"] == "pro"

    # Lifetime with a delayed payment method that fails: recorded as a failed charge, nothing granted.
    s2 = register(client, email="bob@example.com")
    url = checkout(client, s2, "lifetime", "stripe")
    assert pay_on_fake_stripe(client, url, declined=True) == "http://app.test/?billing=canceled"
    e = ent(client, s2)
    assert e["plan"] == "free" and e["last_payment"]["status"] == "failed" and e["last_payment"]["amount"] == "20.00"
    assert "declined" in e["last_payment"]["failure_reason"]


def test_stripe_portal_simulates_renewal_failure_recovery_and_dashboard_refund(client):
    s = register(client)
    pay_on_fake_stripe(client, checkout(client, s, "pro_monthly", "stripe"))
    first_end = ent(client, s)["current_period_end"]
    url = portal(client, s)
    assert url.startswith("/api/v1/billing/fake/stripe/portal/cus_fake_")
    page = client.get(url).text
    assert "Customer portal" in page and "Pro Monthly" in page and "1.00 USD" in page

    press(client, url, "renew")
    e = ent(client, s)
    assert e["status"] == "active" and e["current_period_end"] > first_end
    assert [r["status"] for r in ledger(client, s)] == ["succeeded", "succeeded"]

    press(client, url, "fail")
    e = ent(client, s)
    assert e["status"] == "past_due" and e["plan"] == "pro"  # dunning grace keeps Pro on
    assert e["access_until"] > e["current_period_end"] and e["renews"] is False
    assert e["last_payment"]["status"] == "failed" and e["last_payment"]["amount"] == "1.00"
    assert e["last_payment"]["failure_reason"] == "Your card was declined."
    assert client.get("/api/v1/billing/cancel/preview", headers=headers(s)).json()["basis"] == "none"

    press(client, url, "recover")
    e = ent(client, s)
    assert e["status"] == "active" and e["renews"] is True and e["last_payment"]["status"] == "succeeded"
    assert len(ledger(client, s)) == 3  # the retried invoice reused its payment intent

    # An operator refunds the latest charge from the Stripe dashboard: only the webhook tells
    # us — the ledger learns it and the membership ends.
    press(client, url, "refund")
    e = ent(client, s)
    assert e["plan"] == "free" and e["status"] == "canceled"
    assert ledger(client, s)[0]["status"] == "refunded" and ledger(client, s)[0]["refunded"] == "1.00"


def test_stripe_cancel_now_from_the_portal_ends_access_without_a_refund(client):
    s = register(client)
    pay_on_fake_stripe(client, checkout(client, s, "pro_monthly", "stripe"))
    press(client, portal(client, s), "cancel-now")
    e = ent(client, s)
    assert e["plan"] == "free" and e["status"] == "canceled" and ledger(client, s)[0]["refunded"] == "0.00"


def test_stripe_trial_carry_over_then_cancel_keeps_the_trial(client):
    s = register(client)
    client.post("/api/v1/billing/trial", headers=headers(s))
    trial_end = ent(client, s)["current_period_end"]
    url = checkout(client, s, "pro_yearly", "stripe")
    assert "first charge when the trial ends" in client.get(url).text
    pay_on_fake_stripe(client, url)
    e = ent(client, s)
    assert e["status"] == "trialing" and e["provider"] == "stripe" and e["can_cancel"] is True
    assert e["current_period_end"][:19] == trial_end[:19] and ledger(client, s) == []  # nothing charged yet
    result = cancel(client, s)
    assert result["refund"]["basis"] == "none" and result["refunded_via"] == "none"
    e = ent(client, s)
    assert e["plan"] == "pro" and e["status"] == "canceled" and e["access_until"][:19] == trial_end[:19]
    assert next(iter(fakes.STRIPE.subscriptions.values()))["status"] == "canceled"


def test_stripe_checkout_can_be_abandoned(client):
    s = register(client)
    url = checkout(client, s, "pro_monthly", "stripe")
    assert 'href="http://app.test/?billing=canceled"' in client.get(url).text
    assert ent(client, s)["plan"] == "free"


# ---------------------------------------------------------------------------- PayPal


@pytest.mark.parametrize(
    "plan, amount, refund, basis",
    [
        ("pro_monthly", "1.00", "1.00", "prorated"),
        ("pro_yearly", "10.00", "10.00", "prorated"),
        ("lifetime", "20.00", "10.00", "flat"),
    ],
)
def test_paypal_pay_then_cancel_refunds_every_membership(client, plan, amount, refund, basis):
    s = register(client)
    url = checkout(client, s, plan, "paypal")
    kind = "order" if plan == "lifetime" else "subscription"
    assert url.startswith(f"/api/v1/billing/fake/paypal/approve/{kind}/")
    assert amount in client.get(url).text
    assert approve_on_fake_paypal(client, url) == "http://app.test/?billing=done"
    e = ent(client, s)
    assert e["plan"] == ("lifetime" if plan == "lifetime" else "pro") and e["status"] == "active"
    assert e["provider"] == "paypal" and e["can_cancel"] is True
    assert e["last_payment"]["amount"] == amount and e["last_payment"]["plan"] == plan
    assert e["can_manage"] is (plan != "lifetime")  # a one-off order has no autopay page

    result = cancel(client, s)
    assert result["refunded_via"] == "provider" and result["refund_id"].startswith("REF")
    assert result["refund"]["basis"] == basis and Decimal(result["refund"]["amount"]) >= Decimal(refund) - Decimal(
        "0.01"
    )
    e = result["entitlements"]
    assert e["plan"] == "free" and e["status"] == "canceled"
    capture = next(iter(fakes.PAYPAL.captures.values()))
    assert capture["refunds"][0]["id"] == result["refund_id"]
    if plan != "lifetime":
        assert next(iter(fakes.PAYPAL.subscriptions.values()))["status"] == "CANCELLED"
    assert ledger(client, s)[0]["refunded"] == result["refund"]["amount"]


def test_paypal_declined_funding_for_lifetime(client):
    s = register(client)
    url = checkout(client, s, "lifetime", "paypal")
    assert approve_on_fake_paypal(client, url, declined=True) == "http://app.test/?billing=canceled"
    e = ent(client, s)
    assert e["plan"] == "free" and e["last_payment"]["status"] == "failed" and e["last_payment"]["amount"] == "20.00"


def test_paypal_autopay_page_simulates_renewal_failure_recovery_and_refund(client):
    s = register(client)
    approve_on_fake_paypal(client, checkout(client, s, "pro_yearly", "paypal"))
    first_end = ent(client, s)["current_period_end"]
    url = portal(client, s)
    assert url.startswith("/api/v1/billing/fake/paypal/autopay/I-FAKE")
    assert "10.00 USD" in client.get(url).text
    press(client, url, "renew")
    e = ent(client, s)
    assert e["current_period_end"] > first_end and len(ledger(client, s)) == 2
    press(client, url, "fail")
    e = ent(client, s)
    assert (
        e["status"] == "past_due" and e["last_payment"]["status"] == "failed" and e["last_payment"]["amount"] == "10.00"
    )
    press(client, url, "recover")
    e = ent(client, s)
    assert e["status"] == "active" and e["last_payment"]["status"] == "succeeded"
    # A refund issued at PayPal itself: the membership ends.
    press(client, url, "refund")
    e = ent(client, s)
    assert e["plan"] == "free" and e["status"] == "canceled" and ledger(client, s)[0]["status"] == "refunded"
    press(client, url, "cancel")  # cancelling at PayPal afterwards is harmless
    assert ent(client, s)["plan"] == "free"


def test_paypal_cancel_at_paypal_keeps_the_paid_period(client):
    s = register(client)
    approve_on_fake_paypal(client, checkout(client, s, "pro_monthly", "paypal"))
    press(client, portal(client, s), "cancel")
    e = ent(client, s)
    # Cancelled at PayPal (no refund from there): what was paid for stays usable.
    assert e["plan"] == "pro" and e["status"] == "canceled" and e["cancel_at_period_end"] is True
    assert e["can_cancel"] is False and e["access_until"] == e["current_period_end"]


def test_paypal_trial_carry_over_on_the_fake(client):
    s = register(client)
    client.post("/api/v1/billing/trial", headers=headers(s))
    trial_end = ent(client, s)["current_period_end"]
    url = checkout(client, s, "pro_monthly", "paypal")
    assert "after your trial" in client.get(url).text
    approve_on_fake_paypal(client, url)
    e = ent(client, s)
    assert e["status"] == "active" and e["current_period_end"][:19] == trial_end[:19]  # PayPal: whole seconds
    assert ledger(client, s) == []  # the first charge is due when the trial ends


# ---------------------------------------------------------------------------- seeded accounts


def test_seeded_paid_accounts_belong_to_the_fake_stripe_and_can_be_refunded(client):
    with database.SessionLocal() as db:
        seed.seed(db, iterations=1000)
        seed.seed(db, iterations=1000)  # idempotent: one ledger row per payment id
        rows = {a.email: db.get(models.Subscription, a.id) for a in db.query(models.Account).all()}
        assert db.query(models.Payment).count() == 5
    assert rows["pro@example.com"].provider == "stripe" and rows["pastdue@example.com"].provider == "stripe"
    assert rows["lifetime@example.com"].provider == "stripe" and rows["free@example.com"].provider == "seed"
    assert "sub_fake_pro" in fakes.STRIPE.subscriptions and "pi_fake_pro_1" in fakes.STRIPE.charges

    s = login_seeded(client, "pro@example.com")
    e = ent(client, s)
    assert e["renews"] is True and e["can_manage"] is True and e["last_payment"]["amount"] == "1.00"
    assert client.get(portal(client, s)).status_code == 200
    result = cancel(client, s)
    assert result["refunded_via"] == "provider" and Decimal(result["refund"]["amount"]) >= Decimal("0.99")
    assert fakes.STRIPE.subscriptions["sub_fake_pro"]["status"] == "canceled"

    s = login_seeded(client, "lifetime@example.com")
    e = ent(client, s)
    assert e["plan"] == "lifetime" and e["last_payment"]["amount"] == "20.00"
    result = cancel(client, s)
    assert result["refund"]["amount"] == "10.00" and result["refund"]["basis"] == "flat"
    assert result["refunded_via"] == "provider" and ent(client, s)["plan"] == "free"

    s = login_seeded(client, "pastdue@example.com")
    e = ent(client, s)
    assert e["status"] == "past_due" and e["plan"] == "pro" and e["can_manage"] is True
    assert e["last_payment"]["status"] == "failed" and e["last_payment"]["failure_reason"]
    assert [r["status"] for r in ledger(client, s)] == ["failed", "succeeded"]
    press(client, portal(client, s), "recover")
    e = ent(client, s)
    assert e["status"] == "active" and e["last_payment"]["status"] == "succeeded"

    s = login_seeded(client, "expired@example.com")
    assert ent(client, s)["plan"] == "free" and ledger(client, s)[0]["status"] == "succeeded"


def test_seeded_accounts_stay_local_without_fakes(client, monkeypatch):
    monkeypatch.setattr(config, "BILLING_FAKE_PROVIDERS", False)
    with database.SessionLocal() as db:
        seed.seed(db, iterations=1000)
        provider = db.get(models.Subscription, db.query(models.Account).filter_by(email="pro@example.com").one().id)
        assert provider.provider == "seed"
    s = login_seeded(client, "pro@example.com")
    e = ent(client, s)
    assert e["can_manage"] is False and e["renews"] is True and e["can_cancel"] is True
    result = cancel(client, s)  # local only: the refund is on record, no money moves
    assert result["refunded_via"] == "recorded" and result["refund_id"] is None
    assert result["entitlements"]["plan"] == "free" and ledger(client, s)[0]["refunded"] == result["refund"]["amount"]
    assert client.post("/api/v1/billing/manage", json={}, headers=headers(s)).status_code == 404


def test_paypal_trial_carry_over_then_cancel_keeps_the_free_days(client):
    s = register(client)
    client.post("/api/v1/billing/trial", headers=headers(s))
    trial_end = ent(client, s)["current_period_end"]
    approve_on_fake_paypal(client, checkout(client, s, "pro_monthly", "paypal"))
    e = ent(client, s)
    assert e["status"] == "active" and e["last_payment"] is None and e["can_cancel"] is True
    result = cancel(client, s)
    assert result["refund"]["basis"] == "none" and result["refunded_via"] == "none"
    e = ent(client, s)
    assert e["plan"] == "pro" and e["status"] == "canceled" and e["access_until"][:19] == trial_end[:19]
    assert next(iter(fakes.PAYPAL.subscriptions.values()))["status"] == "CANCELLED"
