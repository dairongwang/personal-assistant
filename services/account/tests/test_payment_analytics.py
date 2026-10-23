"""The administrators' payment analytics (payment_analytics.py → /admin/payments/*): KPIs,
trend vs. the previous window, breakdowns by day / status / method / plan, top customers,
refunds, KPI alerts, the filters (date range, method, status), the transaction drill-down,
the CSV export, and that only administrators get any of it."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from conftest import headers, register

import config
import database
import models

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture()
def admin(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAILS", {"admin@example.com"})
    return register(client, email="admin@example.com", password="Admin-pass-123")


def ledger(db, account_id, *rows):
    """rows: (payment id, provider, plan, cents, status, days ago, refunded cents, refunded days ago)."""
    for pid, provider, plan, cents, status, days_ago, refunded, refunded_days_ago in rows:
        paid_at = NOW - timedelta(days=days_ago)
        db.add(
            models.Payment(
                account_id=account_id,
                provider=provider,
                provider_payment_id=pid,
                plan=plan,
                amount_cents=cents,
                currency="USD",
                status=status,
                refunded_cents=refunded,
                refund_id=f"re_{pid}" if refunded else None,
                refunded_at=NOW - timedelta(days=refunded_days_ago) if refunded else None,
                failure_reason="Card declined." if status == "failed" else None,
                paid_at=paid_at,
                period_start=paid_at if plan != "lifetime" else None,
                period_end=paid_at + timedelta(days=30) if plan != "lifetime" else None,
            )
        )
    db.commit()


@pytest.fixture()
def data(client, admin):
    """Three customers, both providers, every status, spread over the last 40 days."""
    ann = register(client, email="ann@example.com")
    bob = register(client, email="bob@example.com")
    cat = register(client, email="cat@example.com")
    with database.SessionLocal() as db:
        ledger(
            db,
            ann["account_id"],
            ("pi_a1", "stripe", "pro_yearly", 1000, "succeeded", 2, 0, 0),
            ("pi_a2", "stripe", "pro_monthly", 100, "partially_refunded", 10, 40, 3),
            ("pi_a3", "stripe", "pro_monthly", 100, "failed", 1, 0, 0),
        )
        ledger(
            db,
            bob["account_id"],
            ("CAP_b1", "paypal", "lifetime", 2000, "refunded", 5, 2000, 4),
            ("CAP_b2", "paypal", "pro_monthly", 100, "succeeded", 20, 0, 0),
        )
        ledger(
            db,
            cat["account_id"],
            ("pi_c1", "stripe", "pro_monthly", 100, "succeeded", 35, 0, 0),  # previous window
            ("pi_c2", "stripe", "pro_monthly", 100, "failed", 38, 0, 0),  # previous window
        )
        sub = db.get(models.Subscription, ann["account_id"])
        sub.plan, sub.status, sub.provider = "pro", "past_due", "stripe"
        sub.current_period_end = NOW - timedelta(days=1)
        db.commit()
    return {"ann": ann, "bob": bob, "cat": cat}


def get(client, admin, path, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    r = client.get(f"/api/v1/admin/payments/{path}{'?' + query if query else ''}", headers=headers(admin))
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------- KPIs


def test_kpis_for_the_default_window(client, admin, data):
    a = get(client, admin, "analytics")
    assert a["days"] == 30 and a["until"] == NOW.date().isoformat() and a["currency"] == "USD"
    # In the window: a1 1000, a2 100 (40 back), a3 failed, b1 2000 (all back), b2 100 → 5 transactions.
    assert a["transactions"] == 5 and a["succeeded"] == 4 and a["failed"] == 1 and a["refund_count"] == 2
    assert a["revenue"] == "32.00" and a["refunded"] == "20.40" and a["net_revenue"] == "11.60"
    assert a["average_transaction"] == "8.00"  # 32.00 / 4 successful
    assert a["success_rate"] == 0.8 and a["refund_rate"] == round(2040 / 3200, 4)
    # Previous 30 days: c1 (100) and c2 (failed).
    assert a["previous_transactions"] == 2 and a["previous_net_revenue_cents"] == 100
    assert a["revenue_change"] == round((1160 - 100) / 100, 4) and a["transactions_change"] == 1.5
    assert a["mixed_currencies"] is False and a["methods"] == ["paypal", "stripe"]


def test_breakdowns(client, admin, data):
    a = get(client, admin, "analytics")
    by_status = {s["status"]: s for s in a["by_status"]}
    assert by_status["succeeded"]["count"] == 2 and by_status["succeeded"]["amount"] == "11.00"
    assert by_status["failed"]["count"] == 1 and by_status["failed"]["amount"] == "1.00"
    assert by_status["refunded"]["count"] == 1 and by_status["refunded"]["amount"] == "20.00"
    assert by_status["partially_refunded"]["amount"] == "0.40" and by_status["succeeded"]["share"] == 0.4
    by_method = {m["provider"]: m for m in a["by_method"]}
    assert by_method["stripe"] == {
        **by_method["stripe"],
        "label": "Card (Stripe)",
        "count": 3,
        "succeeded": 2,
        "failed": 1,
        "amount": "11.00",
        "refunded": "0.40",
    }
    assert by_method["paypal"]["amount"] == "21.00" and by_method["paypal"]["refunded"] == "20.00"
    assert a["by_method"][0]["provider"] == "paypal"  # sorted by gross amount
    by_plan = {p["plan"]: p for p in a["by_plan"]}
    assert by_plan["lifetime"]["title"] == "Lifetime" and by_plan["pro_yearly"]["amount"] == "10.00"
    assert by_plan["pro_monthly"]["count"] == 3 and by_plan["pro_monthly"]["amount"] == "2.00"

    days = {d["key"]: d for d in a["by_day"]}
    assert len(days) == 30 and list(days)[-1] == NOW.date().isoformat()
    two_ago = days[(NOW - timedelta(days=2)).date().isoformat()]
    assert two_ago["revenue"] == "10.00" and two_ago["succeeded"] == 1
    assert days[(NOW - timedelta(days=1)).date().isoformat()]["failed"] == 1
    assert days[(NOW - timedelta(days=4)).date().isoformat()]["refunded"] == "20.00"  # by refunded_at
    assert days[(NOW - timedelta(days=3)).date().isoformat()]["refunds"] == 1
    five_ago = days[(NOW - timedelta(days=5)).date().isoformat()]
    assert five_ago["revenue_cents"] == 2000 and five_ago["net_cents"] == 2000  # refund counted on its own day


def test_top_customers_and_refunds(client, admin, data):
    a = get(client, admin, "analytics")
    top = a["top_customers"]
    assert [c["email"] for c in top] == ["bob@example.com", "ann@example.com"]
    assert top[0] == {**top[0], "payments": 2, "amount": "21.00", "refunded": "20.00", "net_cents": 100}
    assert top[0]["share"] == round(2100 / 3200, 4) and top[0]["membership"] == "free"
    assert top[1]["membership"] == "paid" and top[1]["amount"] == "11.00"  # past due = still paid (grace)
    refunds = a["refunds"]
    assert [r["email"] for r in refunds] == ["ann@example.com", "bob@example.com"]  # newest refund first
    assert refunds[1] == {**refunds[1], "refunded": "20.00", "full": True, "refund_id": "re_CAP_b1", "plan": "lifetime"}
    assert refunds[0]["full"] is False and refunds[0]["refunded"] == "0.40"


# ---------------------------------------------------------------------------- filters


def test_filters_by_method_status_and_date_range(client, admin, data):
    stripe = get(client, admin, "analytics", method="stripe")
    assert stripe["method"] == "stripe" and stripe["transactions"] == 3 and stripe["revenue"] == "11.00"
    assert [m["provider"] for m in stripe["by_method"]] == ["stripe"]
    failed = get(client, admin, "analytics", status="failed")
    assert failed["transactions"] == 1 and failed["failed"] == 1 and failed["revenue"] == "0.00"
    assert failed["previous_failed"] == 1 and failed["failed_change"] == 0.0
    both = get(client, admin, "analytics", method="paypal", status="refunded")
    assert both["transactions"] == 1 and both["refunded"] == "20.00"

    since, until = (NOW - timedelta(days=40)).date().isoformat(), (NOW - timedelta(days=30)).date().isoformat()
    old = get(client, admin, "analytics", since=since, until=until)
    assert old["since"] == since and old["until"] == until and old["days"] == 11
    assert old["transactions"] == 2 and old["succeeded"] == 1 and old["previous_transactions"] == 0
    assert old["revenue_change"] is None  # no baseline
    week = get(client, admin, "analytics", days=7)
    assert week["days"] == 7 and week["transactions"] == 3  # a3, a1, b1
    r = client.get("/api/v1/admin/payments/analytics?since=2030-01-02&until=2030-01-01", headers=headers(admin))
    assert r.status_code == 422
    r = client.get("/api/v1/admin/payments/analytics?since=nope", headers=headers(admin))
    assert r.status_code == 422
    r = client.get("/api/v1/admin/payments/analytics?status=bogus", headers=headers(admin))
    assert r.status_code == 422


# ---------------------------------------------------------------------------- alerts


def test_kpi_alerts(client, admin, data, monkeypatch):
    a = get(client, admin, "analytics")
    alerts = {x["key"]: x for x in a["alerts"]}
    assert set(alerts) == {"failure_rate", "refund_rate", "revenue_drop", "past_due", "no_revenue"}
    # 1 of 5 failed = 20 % > 10 % → warn; 63 % refunded > 50 % (2× 25 %) → fail; revenue grew → ok;
    # one account past due (threshold 5) → ok; there were successful payments → ok.
    assert alerts["failure_rate"]["level"] == "warn" and alerts["failure_rate"]["value"] == 0.2
    assert alerts["refund_rate"]["level"] == "fail" and "20.40 of 32.00" in alerts["refund_rate"]["detail"]
    assert alerts["revenue_drop"]["level"] == "ok" and alerts["past_due"]["level"] == "ok"
    assert alerts["past_due"]["value"] == 1.0 and alerts["no_revenue"]["level"] == "ok"

    # Thresholds are configuration.
    monkeypatch.setattr(config, "PAYMENT_ALERT_FAILURE_RATE", 0.5)
    monkeypatch.setattr(config, "PAYMENT_ALERT_REFUND_RATE", 0.9)
    monkeypatch.setattr(config, "PAYMENT_ALERT_PAST_DUE", 0)
    alerts = {x["key"]: x for x in get(client, admin, "analytics")["alerts"]}
    assert alerts["failure_rate"]["level"] == "ok" and alerts["refund_rate"]["level"] == "ok"
    assert alerts["past_due"]["level"] == "fail"  # 1 > 2 × 0
    # Too few transactions: the failure rule does not fire.
    monkeypatch.setattr(config, "PAYMENT_ALERT_MIN_TRANSACTIONS", 10)
    monkeypatch.setattr(config, "PAYMENT_ALERT_FAILURE_RATE", 0.01)
    alerts = {x["key"]: x for x in get(client, admin, "analytics")["alerts"]}
    assert alerts["failure_rate"]["level"] == "ok" and "needs ≥ 10" in alerts["failure_rate"]["detail"]

    # A window with no successful payment after one that had some, and a revenue drop.
    # Five silent days right after cat's payment (35 days ago) — which falls in the previous window.
    since, until = (NOW - timedelta(days=32)).date().isoformat(), (NOW - timedelta(days=28)).date().isoformat()
    alerts = {x["key"]: x for x in get(client, admin, "analytics", since=since, until=until)["alerts"]}
    assert alerts["no_revenue"]["level"] == "fail" and alerts["revenue_drop"]["level"] == "fail"
    assert alerts["revenue_drop"]["value"] == -1.0


# ---------------------------------------------------------------------------- drill-down


def test_transactions_list_search_paging_and_detail(client, admin, data):
    page = get(client, admin, "transactions")
    assert page["total"] == 5 and [t["provider_payment_id"] for t in page["items"]] == [
        "pi_a3",
        "pi_a1",
        "CAP_b1",
        "pi_a2",
        "CAP_b2",
    ]
    assert page["items"][0] == {
        **page["items"][0],
        "email": "ann@example.com",
        "status": "failed",
        "failure_reason": "Card declined.",
        "amount": "1.00",
    }
    assert get(client, admin, "transactions", limit=2, offset=2)["items"][0]["provider_payment_id"] == "CAP_b1"
    assert get(client, admin, "transactions", q="bob")["total"] == 2
    assert get(client, admin, "transactions", q="re_CAP_b1")["total"] == 1  # refund id
    assert get(client, admin, "transactions", q="PI_A1")["total"] == 1  # case-insensitive
    assert get(client, admin, "transactions", method="paypal", status="succeeded")["total"] == 1
    assert get(client, admin, "transactions", days=400)["total"] == 7

    detail = get(client, admin, f"transactions/{page['items'][2]['id']}")
    assert detail["transaction"]["provider_payment_id"] == "CAP_b1" and detail["transaction"]["refunded"] == "20.00"
    acct = detail["account"]
    assert acct["email"] == "bob@example.com" and acct["membership"] == "free" and acct["lifetime_value_cents"] == 100
    assert [h["provider_payment_id"] for h in detail["history"]] == ["CAP_b1", "CAP_b2"]
    ann = get(client, admin, f"transactions/{page['items'][0]['id']}")["account"]
    assert ann["status"] == "past_due" and ann["plan"] == "pro" and ann["provider"] == "stripe"
    assert client.get("/api/v1/admin/payments/transactions/999999", headers=headers(admin)).status_code == 404


def test_csv_export(client, admin, data):
    r = client.get("/api/v1/admin/payments/transactions.csv?method=stripe", headers=headers(admin))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "payments-" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [row["provider_payment_id"] for row in rows] == ["pi_a3", "pi_a1", "pi_a2"]
    assert rows[2]["refunded"] == "0.40" and rows[2]["refund_id"] == "re_pi_a2" and rows[0]["failure_reason"]


def test_only_administrators(client, data):
    user = data["ann"]
    for path in ("analytics", "transactions", "transactions/1", "transactions.csv"):
        assert client.get(f"/api/v1/admin/payments/{path}", headers=headers(user)).status_code == 403
        assert client.get(f"/api/v1/admin/payments/{path}").status_code == 401


def test_empty_ledger(client, admin):
    a = get(client, admin, "analytics")
    assert a["transactions"] == 0 and a["revenue"] == "0.00" and a["average_transaction"] == "0.00"
    assert a["by_status"] == [] and a["by_method"] == [] and a["top_customers"] == [] and a["refunds"] == []
    assert len(a["by_day"]) == 30 and all(x["level"] == "ok" for x in a["alerts"])
    assert get(client, admin, "transactions")["total"] == 0
