"""Prices, the payments ledger, refunds on cancellation and payment failures — for every paid
membership (monthly, yearly, Lifetime) and both providers, against a recorded HTTP transport
(the exact REST calls the strategies make) and the neutral events the webhooks turn into."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs

import pytest
from conftest import headers, register
from test_payments import PAYPAL_HEADERS, FakeHttp, paypal_post, stripe_post

import billing
import config
import database
import models
import schemas

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def ent(client, s):
    return client.get("/api/v1/me/entitlements", headers=headers(s)).json()


def ledger(client, s):
    return client.get("/api/v1/billing/payments", headers=headers(s)).json()


def paid(db, account_id, *, plan, provider="stripe", payment_id="pi_1", days_ago=0, period_days=None, cents=None):
    """A succeeded charge in the ledger + the matching subscription row."""
    amount = cents if cents is not None else {"pro_monthly": 100, "pro_yearly": 1000, "lifetime": 2000}[plan]
    paid_at = NOW - timedelta(days=days_ago)
    row = models.Payment(
        account_id=account_id,
        provider=provider,
        provider_payment_id=payment_id,
        plan=plan,
        amount_cents=amount,
        currency="USD",
        status="succeeded",
        paid_at=paid_at,
        period_start=paid_at if plan != "lifetime" else None,
        period_end=paid_at + timedelta(days=period_days or {"pro_monthly": 30, "pro_yearly": 365}[plan])
        if plan != "lifetime"
        else None,
    )
    db.add(row)
    sub = db.get(models.Subscription, account_id) or models.Subscription(account_id=account_id, provider=provider)
    sub.provider = provider
    sub.plan = "lifetime" if plan == "lifetime" else "pro"
    sub.status = "active"
    sub.current_period_end = row.period_end
    sub.provider_customer_id = "cus_1"
    sub.provider_subscription_id = None if plan == "lifetime" else "sub_1"
    db.add(sub)
    db.commit()
    return row.id


# ---------------------------------------------------------------------------- the arithmetic


@pytest.mark.parametrize(
    "plan, days_in, expected, basis",
    [
        ("pro_monthly", 0, "1.00", "prorated"),  # nothing used yet: everything back
        ("pro_monthly", 15, "0.50", "prorated"),  # half of 30 days used
        ("pro_monthly", 29, "0.03", "prorated"),  # 1 of 30 days left: 1.00 × 1/30 = 0.033 → 0.03
        ("pro_monthly", 30, "0.00", "none"),  # the period is over
        ("pro_yearly", 100, "7.26", "prorated"),  # 265/365 × 10.00 = 7.2603
        ("pro_yearly", 364, "0.03", "prorated"),
        ("lifetime", 0, "10.00", "flat"),  # REFUND_LIFETIME
        ("lifetime", 400, "10.00", "flat"),  # … whenever
    ],
)
def test_refund_is_prorated_by_unused_days_or_flat_for_lifetime(client, plan, days_in, expected, basis):
    s = register(client)
    with database.SessionLocal() as db:
        paid(db, s["account_id"], plan=plan, days_ago=days_in)
        account = db.get(models.Account, s["account_id"])
        preview, payment = billing.refund_preview(db, account, now=NOW)
    assert preview.amount == expected and preview.basis == basis and preview.currency == "USD"
    if basis == "prorated":
        assert preview.days_left == pytest.approx((30 if plan == "pro_monthly" else 365) - days_in)
        assert preview.days_total == (30 if plan == "pro_monthly" else 365) and "unused" in preview.note
    if basis == "flat":
        assert preview.paid == "20.00" and "flat 10.00 USD" in preview.note


def test_refund_uses_the_configured_prices_and_never_exceeds_what_is_left(client, monkeypatch):
    s = register(client)
    monkeypatch.setattr(config, "REFUND_LIFETIME", Decimal("50"))  # more than the 20.00 paid
    with database.SessionLocal() as db:
        paid(db, s["account_id"], plan="lifetime")
        account = db.get(models.Account, s["account_id"])
        assert billing.refund_preview(db, account, now=NOW)[0].amount == "20.00"
        # A partial refund that already happened comes off the basis.
        row = db.query(models.Payment).one()
        row.refunded_cents, row.status = 1500, "partially_refunded"
        db.commit()
        assert billing.refund_preview(db, account, now=NOW)[0].amount == "5.00"
    s2 = register(client, email="bob@example.com")
    with database.SessionLocal() as db:
        paid(db, s2["account_id"], plan="pro_yearly", cents=36500, payment_id="pi_2")  # 100/day
        account = db.get(models.Account, s2["account_id"])
        preview, _ = billing.refund_preview(db, account, now=NOW + timedelta(days=200))
        assert preview.amount == "165.00" and preview.days_left == 165


def test_nothing_to_refund_without_a_paid_charge(client):
    s = register(client)
    r = client.get("/api/v1/billing/cancel/preview", headers=headers(s))
    assert r.status_code == 200 and r.json()["basis"] == "none" and r.json()["amount"] == "0.00"
    assert client.post("/api/v1/billing/cancel", headers=headers(s)).status_code == 409  # nothing paid
    client.post("/api/v1/billing/trial", headers=headers(s))
    assert client.get("/api/v1/billing/cancel/preview", headers=headers(s)).json()["basis"] == "none"
    r = client.post("/api/v1/billing/cancel", headers=headers(s))
    assert r.status_code == 409 and "trial" in r.json()["detail"]  # a bare trial just runs out
    assert ent(client, s)["can_cancel"] is False
    assert client.get("/api/v1/billing/cancel/preview").status_code == 401


# ---------------------------------------------------------------------------- cancel via the providers


@pytest.fixture()
def stripe_live(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_x")
    http = FakeHttp(
        {
            ("POST", "/v1/refunds"): (200, {"id": "re_1", "object": "refund", "status": "succeeded"}),
            ("DELETE", "/v1/subscriptions/sub_1"): (200, {"id": "sub_1", "status": "canceled"}),
        }
    )
    import payments

    monkeypatch.setattr(payments.STRIPE, "http", http)
    return http


@pytest.fixture()
def paypal_live(monkeypatch):
    monkeypatch.setattr(config, "PAYPAL_CLIENT_ID", "c")
    monkeypatch.setattr(config, "PAYPAL_CLIENT_SECRET", "s")
    monkeypatch.setattr(config, "PAYPAL_WEBHOOK_ID", "WH-1")
    monkeypatch.setattr(config, "PAYPAL_PLAN_PRO_MONTHLY", "P-MONTH")
    http = FakeHttp(
        {
            ("POST", "/v1/oauth2/token"): (200, {"access_token": "tok", "expires_in": 3600}),
            ("POST", "/v2/payments/captures/CAP1/refund"): (201, {"id": "REF1", "status": "COMPLETED"}),
            ("POST", "/v1/billing/subscriptions/I-1/cancel"): (204, {}),
            ("POST", "/v1/notifications/verify-webhook-signature"): (200, {"verification_status": "SUCCESS"}),
        }
    )
    import payments

    monkeypatch.setattr(payments.PAYPAL, "http", http)
    payments.PAYPAL._token = None
    return http


def test_cancel_monthly_via_stripe_refunds_then_ends_the_subscription(client, stripe_live):
    s = register(client)
    with database.SessionLocal() as db:
        paid(db, s["account_id"], plan="pro_monthly", days_ago=0)
    e = ent(client, s)
    assert e["plan"] == "pro" and e["can_cancel"] is True and e["last_payment"]["amount"] == "1.00"
    preview = client.get("/api/v1/billing/cancel/preview", headers=headers(s)).json()
    assert preview["basis"] == "prorated" and Decimal(preview["amount"]) >= Decimal("0.99")

    r = client.post("/api/v1/billing/cancel", headers=headers(s))
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["refund_id"] == "re_1" and result["refunded_via"] == "provider"
    assert result["refund"]["amount"] == preview["amount"]
    assert result["entitlements"]["plan"] == "free" and result["entitlements"]["status"] == "canceled"

    # Refund first (money matters most), then the subscription is ended — both at Stripe.
    assert [(c[0], c[1].rsplit("/v1/", 1)[1]) for c in stripe_live.calls] == [
        ("POST", "refunds"),
        ("DELETE", "subscriptions/sub_1"),
    ]
    form = {k: v[0] for k, v in parse_qs(stripe_live.calls[0][3].decode()).items()}
    assert form["payment_intent"] == "pi_1" and form["amount"] == str(preview["amount_cents"])
    rows = ledger(client, s)
    assert len(rows) == 1 and rows[0]["refunded"] == preview["amount"] and rows[0]["refund_id"] == "re_1"
    assert rows[0]["status"] in ("refunded", "partially_refunded")
    assert client.post("/api/v1/billing/cancel", headers=headers(s)).status_code == 409
    assert ent(client, s)["last_payment"]["refunded"] == preview["amount"]


def test_cancel_yearly_via_stripe_uses_the_charge_id_when_there_is_no_intent(client, stripe_live):
    s = register(client)
    with database.SessionLocal() as db:
        paid(db, s["account_id"], plan="pro_yearly", payment_id="ch_abc", days_ago=100)
    with database.SessionLocal() as db:  # the preview is relative to *now*: pin the period
        row = db.query(models.Payment).one()
        row.period_start = datetime.now(timezone.utc) - timedelta(days=100)
        row.period_end = row.period_start + timedelta(days=365)
        db.commit()
    r = client.post("/api/v1/billing/cancel", headers=headers(s))
    assert r.status_code == 200 and r.json()["refund"]["amount"] == "7.26"
    form = {k: v[0] for k, v in parse_qs(stripe_live.calls[0][3].decode()).items()}
    assert form["charge"] == "ch_abc" and form["amount"] == "726" and "payment_intent" not in form


def test_cancel_lifetime_via_stripe_refunds_the_flat_amount_without_a_subscription_call(client, stripe_live):
    s = register(client)
    with database.SessionLocal() as db:
        paid(db, s["account_id"], plan="lifetime")
    r = client.post("/api/v1/billing/cancel", headers=headers(s))
    assert r.status_code == 200, r.text
    assert r.json()["refund"] == {**r.json()["refund"], "amount": "10.00", "basis": "flat", "paid": "20.00"}
    assert [c[0] for c in stripe_live.calls] == ["POST"]  # only the refund: nothing renews
    e = ent(client, s)
    assert e["plan"] == "free" and e["membership"] == "free" and e["last_payment"]["refunded"] == "10.00"
    assert e["last_payment"]["status"] == "partially_refunded"


def test_cancel_via_paypal_refunds_the_capture_and_cancels_the_agreement(client, paypal_live):
    s = register(client)
    with database.SessionLocal() as db:
        paid(db, s["account_id"], plan="pro_monthly", provider="paypal", payment_id="CAP1", days_ago=15)
        sub = db.get(models.Subscription, s["account_id"])
        sub.provider_subscription_id = "I-1"
        row = db.query(models.Payment).one()
        row.period_start = datetime.now(timezone.utc) - timedelta(days=15)
        row.period_end = row.period_start + timedelta(days=30)
        db.commit()
    r = client.post("/api/v1/billing/cancel", headers=headers(s))
    assert r.status_code == 200, r.text
    assert r.json()["refund"]["amount"] == "0.50" and r.json()["refund_id"] == "REF1"
    refund_call = paypal_live.sent("POST", "/v2/payments/captures/CAP1/refund")[0]
    assert json.loads(refund_call[3]) == {"amount": {"value": "0.50", "currency_code": "USD"}}
    assert paypal_live.sent("POST", "/v1/billing/subscriptions/I-1/cancel")
    assert ent(client, s)["plan"] == "free"


def test_a_refused_refund_changes_nothing(client, stripe_live):
    stripe_live.routes[("POST", "/v1/refunds")] = (
        400,
        {"error": {"message": "Charge ch_1 has already been refunded."}},
    )
    s = register(client)
    with database.SessionLocal() as db:
        paid(db, s["account_id"], plan="pro_monthly")
    r = client.post("/api/v1/billing/cancel", headers=headers(s))
    assert r.status_code == 502 and "already been refunded" in r.json()["detail"]
    assert len(stripe_live.calls) == 1  # the subscription was not touched
    e = ent(client, s)
    assert e["plan"] == "pro" and e["status"] == "active" and ledger(client, s)[0]["refunded"] == "0.00"


def test_a_refund_that_went_through_is_kept_when_the_cancellation_fails(client, stripe_live):
    stripe_live.routes[("DELETE", "/v1/subscriptions/sub_1")] = (500, {"error": {"message": "Stripe is down"}})
    s = register(client)
    with database.SessionLocal() as db:
        paid(db, s["account_id"], plan="pro_monthly")
    r = client.post("/api/v1/billing/cancel", headers=headers(s))
    assert r.status_code == 502 and "Stripe is down" in r.json()["detail"]
    row = ledger(client, s)[0]
    assert row["refund_id"] == "re_1" and row["refunded"] != "0.00"  # the money went back: on record
    assert ent(client, s)["status"] == "active"  # the subscription still runs: try again later


def test_cancel_without_a_usable_provider_is_recorded_only(client):
    s = register(client)
    with database.SessionLocal() as db:
        paid(db, s["account_id"], plan="pro_yearly", provider="seed")
    r = client.post("/api/v1/billing/cancel", headers=headers(s))
    assert r.status_code == 200 and r.json()["refunded_via"] == "recorded" and r.json()["refund_id"] is None
    assert r.json()["refund"]["basis"] == "prorated" and ledger(client, s)[0]["status"] in (
        "refunded",
        "partially_refunded",
    )
    assert ent(client, s)["plan"] == "free"


def test_cancel_during_a_carried_over_trial_keeps_the_free_days(client, stripe_live):
    s = register(client)
    client.post("/api/v1/billing/trial", headers=headers(s))
    trial_end = ent(client, s)["current_period_end"]
    with database.SessionLocal() as db:  # the subscription exists at Stripe, nothing paid yet
        sub = db.get(models.Subscription, s["account_id"])
        sub.provider, sub.provider_subscription_id, sub.provider_customer_id = "stripe", "sub_1", "cus_1"
        db.commit()
    assert ent(client, s)["can_cancel"] is True
    r = client.post("/api/v1/billing/cancel", headers=headers(s))
    assert r.status_code == 200 and r.json()["refund"]["basis"] == "none" and r.json()["refunded_via"] == "none"
    assert [c[0] for c in stripe_live.calls] == ["DELETE"]
    e = ent(client, s)
    assert e["plan"] == "pro" and e["status"] == "canceled" and e["access_until"] == trial_end


def test_cancel_while_past_due_refunds_nothing_and_ends_the_grace(client, stripe_live):
    s = register(client)
    with database.SessionLocal() as db:
        paid(db, s["account_id"], plan="pro_monthly", days_ago=32)
        db.add(
            models.Payment(
                account_id=s["account_id"],
                provider="stripe",
                provider_payment_id="pi_2",
                plan="pro_monthly",
                amount_cents=100,
                currency="USD",
                status="failed",
                failure_reason="Your card was declined.",
                paid_at=datetime.now(timezone.utc) - timedelta(days=2),
            )
        )
        sub = db.get(models.Subscription, s["account_id"])
        sub.status = "past_due"
        sub.current_period_end = datetime.now(timezone.utc) - timedelta(days=2)
        db.commit()
    e = ent(client, s)
    assert e["plan"] == "pro" and e["status"] == "past_due" and e["last_payment"]["status"] == "failed"
    assert e["last_payment"]["failure_reason"] == "Your card was declined."
    assert client.get("/api/v1/billing/cancel/preview", headers=headers(s)).json()["basis"] == "none"
    r = client.post("/api/v1/billing/cancel", headers=headers(s))
    assert r.status_code == 200 and r.json()["refund"]["amount"] == "0.00"
    assert [c[0] for c in stripe_live.calls] == ["DELETE"] and ent(client, s)["plan"] == "free"


# ---------------------------------------------------------------------------- the ledger from webhooks


def test_stripe_invoices_fill_the_ledger_and_drive_dunning(client, stripe_live):
    s = register(client)
    acc = s["account_id"]
    now = int(datetime.now(timezone.utc).timestamp())
    created = {
        "id": "evt_1",
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_1",
                "customer": "cus_1",
                "status": "active",
                "current_period_end": now + 30 * 86400,
                "metadata": {"account_id": acc, "plan": "pro_monthly"},
            }
        },
    }
    assert stripe_post(client, created).status_code == 200

    def invoice(event_id, kind, pi, *, paid_cents, due_cents, start, end):
        return {
            "id": event_id,
            "type": kind,
            "data": {
                "object": {
                    "id": "in_" + event_id,
                    "customer": "cus_1",
                    "payment_intent": pi,
                    "amount_paid": paid_cents,
                    "amount_due": due_cents,
                    "currency": "usd",
                    "created": now,
                    "status_transitions": {"paid_at": now if paid_cents else None},
                    "last_payment_error": None if paid_cents else {"message": "Insufficient funds."},
                    "lines": {"data": [{"period": {"start": start, "end": end}}]},
                    "parent": {
                        "subscription_details": {
                            "subscription": "sub_1",
                            "metadata": {"account_id": acc, "plan": "pro_monthly"},
                        }
                    },
                }
            },
        }

    assert (
        stripe_post(
            client,
            invoice("evt_2", "invoice.paid", "pi_a", paid_cents=100, due_cents=100, start=now, end=now + 30 * 86400),
        ).status_code
        == 200
    )
    rows = ledger(client, s)
    assert len(rows) == 1 and rows[0]["amount"] == "1.00" and rows[0]["status"] == "succeeded"
    assert (
        rows[0]["plan"] == "pro_monthly"
        and rows[0]["period_end"][:10] == datetime.fromtimestamp(now + 30 * 86400, tz=timezone.utc).isoformat()[:10]
    )

    # The renewal fails: a failed row, past_due (grace), the failure visible on the account.
    failed = invoice(
        "evt_3",
        "invoice.payment_failed",
        "pi_b",
        paid_cents=0,
        due_cents=100,
        start=now + 30 * 86400,
        end=now + 60 * 86400,
    )
    assert stripe_post(client, failed).status_code == 200
    e = ent(client, s)
    assert e["status"] == "past_due" and e["plan"] == "pro"
    assert e["last_payment"]["status"] == "failed" and e["last_payment"]["failure_reason"] == "Insufficient funds."
    assert [r["status"] for r in ledger(client, s)] == ["failed", "succeeded"]

    # Stripe retries the same invoice (same payment intent): the failed row becomes the success.
    retried = invoice(
        "evt_4", "invoice.paid", "pi_b", paid_cents=100, due_cents=100, start=now + 30 * 86400, end=now + 60 * 86400
    )
    assert stripe_post(client, retried).status_code == 200
    e = ent(client, s)
    assert (
        e["status"] == "active"
        and e["last_payment"]["status"] == "succeeded"
        and e["last_payment"]["failure_reason"] is None
    )
    assert len(ledger(client, s)) == 2

    # A refund issued from the Stripe dashboard: the ledger learns it and the membership ends.
    refunded = {
        "id": "evt_5",
        "type": "charge.refunded",
        "data": {
            "object": {
                "id": "ch_b",
                "payment_intent": "pi_b",
                "customer": "cus_1",
                "amount": 100,
                "amount_refunded": 100,
                "refunded": True,
                "currency": "usd",
                "metadata": {},
                "refunds": {"data": [{"id": "re_dash"}]},
            }
        },
    }
    assert stripe_post(client, refunded).status_code == 200
    e = ent(client, s)
    assert e["plan"] == "free" and e["status"] == "canceled"
    assert ledger(client, s)[0] == {
        **ledger(client, s)[0],
        "status": "refunded",
        "refunded": "1.00",
        "refund_id": "re_dash",
    }
    assert stripe_post(client, {**refunded, "id": "evt_5b"}).status_code == 200  # same refund again: no double count
    assert ledger(client, s)[0]["refunded"] == "1.00"


def test_stripe_failed_lifetime_checkout_is_recorded_and_grants_nothing(client, stripe_live):
    s = register(client)
    failed = {
        "id": "evt_f",
        "type": "checkout.session.async_payment_failed",
        "data": {
            "object": {
                "mode": "payment",
                "payment_status": "unpaid",
                "client_reference_id": s["account_id"],
                "payment_intent": "pi_life",
                "amount_total": 2000,
                "currency": "usd",
                "metadata": {"plan": "lifetime"},
            }
        },
    }
    assert stripe_post(client, failed).status_code == 200
    e = ent(client, s)
    assert e["plan"] == "free" and e["status"] == "none" and e["trial_available"] is True
    assert e["last_payment"]["status"] == "failed" and e["last_payment"]["amount"] == "20.00"
    assert e["last_payment"]["plan"] == "lifetime"
    # … and when the delayed payment does go through after all:
    done = {**failed, "id": "evt_ok", "type": "checkout.session.async_payment_succeeded"}
    done["data"]["object"]["payment_status"] = "paid"
    assert stripe_post(client, done).status_code == 200
    e = ent(client, s)
    assert e["plan"] == "lifetime" and e["last_payment"]["status"] == "succeeded" and len(ledger(client, s)) == 1


def test_paypal_sales_and_captures_fill_the_ledger(client, paypal_live):
    s = register(client)
    acc = s["account_id"]
    subscription = {
        "id": "I-1",
        "status": "ACTIVE",
        "plan_id": "P-MONTH",
        "custom_id": f"{acc}:pro_monthly",
        "subscriber": {"payer_id": "PAYER1"},
        "billing_info": {"next_billing_time": "2031-02-01T10:00:00Z"},
    }
    paypal_live.routes[("GET", "/v1/billing/subscriptions/I-1")] = (200, subscription)
    assert (
        paypal_post(
            client, {"id": "WH-1", "event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": subscription}
        ).status_code
        == 200
    )
    sale = {
        "id": "WH-2",
        "event_type": "PAYMENT.SALE.COMPLETED",
        "resource": {
            "id": "SALE1",
            "billing_agreement_id": "I-1",
            "amount": {"total": "1.00", "currency": "USD"},
            "create_time": (NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    assert paypal_post(client, sale).status_code == 200
    rows = ledger(client, s)
    assert rows[0] == {**rows[0], "amount": "1.00", "plan": "pro_monthly", "status": "succeeded", "provider": "paypal"}
    assert rows[0]["period_end"].startswith("2031-02-01")

    failed = {
        "id": "WH-3",
        "event_type": "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
        "resource": {
            **subscription,
            "billing_info": {"outstanding_balance": {"value": "1.00", "currency_code": "USD"}},
        },
    }
    assert paypal_post(client, failed).status_code == 200
    e = ent(client, s)
    assert (
        e["status"] == "past_due" and e["last_payment"]["status"] == "failed" and e["last_payment"]["amount"] == "1.00"
    )

    # A refund issued at PayPal for the sale: the ledger learns it.
    refunded = {
        "id": "WH-4",
        "event_type": "PAYMENT.SALE.REFUNDED",
        "resource": {
            "id": "REFX",
            "amount": {"value": "0.40", "currency_code": "USD"},
            "links": [{"rel": "up", "href": "https://api-m.sandbox.paypal.com/v2/payments/captures/SALE1"}],
        },
    }
    assert paypal_post(client, refunded).status_code == 200
    row = next(r for r in ledger(client, s) if r["status"] != "failed")
    assert row["refunded"] == "0.40" and row["status"] == "partially_refunded" and row["refund_id"] == "REFX"

    # Lifetime: a denied capture records a failure and grants nothing.
    s2 = register(client, email="bob@example.com")
    denied = {
        "id": "WH-5",
        "event_type": "PAYMENT.CAPTURE.DENIED",
        "resource": {
            "id": "CAPD",
            "status": "DECLINED",
            "custom_id": f"{s2['account_id']}:lifetime",
            "amount": {"value": "20.00", "currency_code": "USD"},
        },
    }
    assert paypal_post(client, denied, PAYPAL_HEADERS).status_code == 200
    e = ent(client, s2)
    assert e["plan"] == "free" and e["last_payment"]["status"] == "failed" and e["last_payment"]["amount"] == "20.00"


def test_ledger_rows_are_idempotent_per_provider_payment(client):
    s = register(client)
    with database.SessionLocal() as db:
        account = db.get(models.Account, s["account_id"])
        details = schemas.PaymentDetails(
            provider_payment_id="pi_x", amount_cents=1000, currency="USD", plan="pro_yearly"
        )
        first = billing.record_payment(db, account, "stripe", details, failed=True)
        again = billing.record_payment(db, account, "stripe", details, failed=False)
        db.commit()
        assert first.id == again.id and again.status == "succeeded" and again.failure_reason is None
        assert again.period_end is not None and (again.period_end - again.period_start).days == 365
        assert db.query(models.Payment).count() == 1
