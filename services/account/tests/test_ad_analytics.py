"""Advertising analytics + compliance for the separate ad-admin dashboard (docs/ADVERTISING.md §11):
aggregates that can never be more personal than their anonymous source, small-count suppression,
CSV export, live privacy checks mapped to regimes worldwide, and how the dashboard is hosted."""

import csv
import io
import os
from datetime import date, timedelta

import pytest
from conftest import headers, register

import ad_analytics
import config
import database
import models
import schemas


@pytest.fixture()
def admin(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAILS", {"admin@example.com"})
    return register(client, email="admin@example.com", password="Admin-pass-123")


def make(client, admin, **over):
    body = {
        "sponsor": "Example Co",
        "headline": "H",
        "target_url": "https://example.com/x",
        "status": "active",
        **over,
    }
    r = client.post("/api/v1/admin/ads", json=body, headers=headers(admin))
    assert r.status_code == 201, r.text
    return r.json()


def stat(db, ad_id, days_ago, region, imp, clk=0):
    db.add(
        models.AdDailyStat(
            ad_id=ad_id, day=date.today() - timedelta(days=days_ago), region=region, impressions=imp, clicks=clk
        )
    )


def test_analytics_totals_trend_breakdowns_and_suppression(client, admin, monkeypatch):
    monkeypatch.setattr(config, "ADS_ANALYTICS_MIN_COUNT", 5)
    a = make(client, admin, sponsor="A", kind="sponsored")
    b = make(client, admin, sponsor="B", kind="affiliate")
    idle = make(client, admin, sponsor="Idle", kind="internal", status="paused")
    with database.SessionLocal() as db:
        stat(db, a["id"], 0, "DE", 100, 10)
        stat(db, a["id"], 1, "DE", 50, 5)
        stat(db, a["id"], 1, "FR", 20, 1)
        stat(db, b["id"], 2, "LI", 1, 1)  # one view from Liechtenstein → folded into "other"
        stat(db, b["id"], 2, "AT", 3, 0)  # below the threshold too
        stat(db, a["id"], 10, "DE", 1000, 100)  # previous window (7-day report)
        db.commit()

    r = client.get("/api/v1/admin/ads/analytics?days=7", headers=headers(admin))
    assert r.status_code == 200, r.text
    x = r.json()
    assert (x["impressions"], x["clicks"]) == (174, 17) and x["ctr"] == round(17 / 174, 4)
    assert (x["previous_impressions"], x["previous_clicks"]) == (1000, 100)
    assert x["impressions_change"] == round((174 - 1000) / 1000, 4)
    assert len(x["by_day"]) == 7 and x["by_day"][-1] == {
        "key": date.today().isoformat(),
        "impressions": 100,
        "clicks": 10,
    }
    assert sum(d["impressions"] for d in x["by_day"]) == 174
    kinds = {k["kind"]: k for k in x["by_kind"]}
    assert kinds["sponsored"]["impressions"] == 170 and kinds["affiliate"]["impressions"] == 4
    assert kinds["internal"] == {"kind": "internal", "impressions": 0, "clicks": 0, "ctr": 0.0, "campaigns": 1}
    # Regions: DE and FR kept, LI + AT folded — nobody can read "1 view from Liechtenstein" here.
    assert x["by_region"] == [
        {"key": "DE", "impressions": 150, "clicks": 15},
        {"key": "FR", "impressions": 20, "clicks": 1},
        {"key": "other", "impressions": 4, "clicks": 1},
    ]
    assert x["regions_folded"] == 2 and x["min_count"] == 5
    assert "LI" not in r.text and "AT" not in {p["key"] for p in x["by_region"]}
    # Campaign table: ordered by impressions, idle campaigns included with zeros, share of total.
    rows = x["campaigns"]
    assert [c["sponsor"] for c in rows] == ["A", "B", "Idle"]
    assert rows[0]["share"] == round(170 / 174, 4) and rows[2]["impressions"] == 0 and rows[2]["live"] is False
    assert x["active_source"]["kind"] == "sponsored" and "Third-party" in x["third_party_note"]
    assert idle["id"] == rows[2]["id"]

    # Kind filter and the "no suppression" setting.
    y = client.get("/api/v1/admin/ads/analytics?days=7&kind=affiliate", headers=headers(admin)).json()
    assert y["impressions"] == 4 and [c["sponsor"] for c in y["campaigns"]] == ["B"]
    monkeypatch.setattr(config, "ADS_ANALYTICS_MIN_COUNT", 1)
    z = client.get("/api/v1/admin/ads/analytics?days=7", headers=headers(admin)).json()
    assert [p["key"] for p in z["by_region"]] == ["DE", "FR", "AT", "LI"] and z["regions_folded"] == 0
    # No baseline → change is None; empty window → zeros.
    assert (
        client.get("/api/v1/admin/ads/analytics?days=400", headers=headers(admin)).json()["impressions_change"] is None
    )
    # Administrators only.
    user = register(client, email="u@example.com")
    assert client.get("/api/v1/admin/ads/analytics", headers=headers(user)).status_code == 403


def test_csv_export_is_aggregate_and_suppressed(client, admin, monkeypatch):
    monkeypatch.setattr(config, "ADS_ANALYTICS_MIN_COUNT", 5)
    a = make(client, admin, sponsor="A, Inc.", headline='Say "hi"')
    with database.SessionLocal() as db:
        stat(db, a["id"], 0, "DE", 12, 2)
        stat(db, a["id"], 0, "LI", 1, 0)
        stat(db, a["id"], 1, "US", 7, 1)
        db.commit()
    r = client.get("/api/v1/admin/ads/analytics.csv?days=7", headers=headers(admin))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="ad-performance-7d.csv"' in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [(x["day"], x["region"], x["impressions"], x["clicks"]) for x in rows] == [
        ((date.today() - timedelta(days=1)).isoformat(), "US", "7", "1"),
        (date.today().isoformat(), "DE", "12", "2"),
        (date.today().isoformat(), "other", "1", "0"),
    ]
    assert rows[0]["sponsor"] == "A, Inc." and rows[0]["headline"] == 'Say "hi"' and rows[0]["kind"] == "sponsored"
    assert set(rows[0]) == {
        "day",
        "campaign_id",
        "kind",
        "sponsor",
        "headline",
        "region",
        "impressions",
        "clicks",
        "ctr",
    }


def test_compliance_checks_reflect_configuration_and_data(client, admin, monkeypatch):
    r = client.get("/api/v1/admin/ads/compliance", headers=headers(admin))
    assert r.status_code == 200, r.text
    c = r.json()
    by = {x["key"]: x for x in c["checks"]}
    for key in (
        "no_identifiers",
        "coarse_granularity",
        "retention",
        "small_count_suppression",
        "no_ip_geolocation",
        "first_party_images",
        "https_links",
        "third_party_consent",
        "fakes_off_in_prod",
        "anonymous_endpoints",
        "audit_trail",
        "rate_limited",
    ):
        assert by[key]["status"] == "ok", by[key]
    assert "cmp_required" not in by  # first-party source: no CMP question
    assert {r["code"] for r in c["regimes"]} >= {
        "GDPR",
        "UK-GDPR",
        "CCPA",
        "LGPD",
        "PIPEDA",
        "APPI",
        "PIPL",
        "DPDP",
        "APP",
        "DSA",
    }
    assert all(r["status"] == "ok" for r in c["regimes"])
    assert any(i["location"] == "ad_daily_stats" and i["personal"] is False for i in c["inventory"])
    assert "not legal advice" in c["disclaimer"] and c["environment"] == "test"

    # Third-party source active → consent check present and a CMP warning; GDPR-like regimes stay ok
    # (consent gating is in place) but the warning is surfaced.
    client.put("/api/v1/admin/ads/sources", json={"kind": "adsense"}, headers=headers(admin))
    c = client.get("/api/v1/admin/ads/compliance", headers=headers(admin)).json()
    by = {x["key"]: x for x in c["checks"]}
    assert by["third_party_consent"]["status"] == "ok" and "Google AdSense" in by["third_party_consent"]["title"]
    assert by["cmp_required"]["status"] == "warn"
    client.put("/api/v1/admin/ads/sources", json={"kind": "sponsored"}, headers=headers(admin))

    # Configuration drift shows up: IP header on → warn, GDPR moves to "attention".
    monkeypatch.setattr(config, "ADS_REGION_HEADER", "CF-IPCountry")
    c = client.get("/api/v1/admin/ads/compliance", headers=headers(admin)).json()
    by = {x["key"]: x for x in c["checks"]}
    assert by["no_ip_geolocation"]["status"] == "warn"
    gdpr = next(r for r in c["regimes"] if r["code"] == "GDPR")
    assert gdpr["status"] == "attention" and gdpr["attention"] == [by["no_ip_geolocation"]["title"]]
    monkeypatch.setattr(config, "ADS_REGION_HEADER", "")

    # Retention drift: a row older than the window fails the retention check until pruned.
    a = make(client, admin)
    with database.SessionLocal() as db:
        db.add(models.AdDailyStat(ad_id=a["id"], day=date.today() - timedelta(days=500), region="DE", impressions=9))
        db.commit()
    by = {x["key"]: x for x in client.get("/api/v1/admin/ads/compliance", headers=headers(admin)).json()["checks"]}
    assert by["retention"]["status"] == "fail" and "500 days old" in by["retention"]["detail"]
    client.get("/api/v1/admin/ads/analytics", headers=headers(admin))  # analytics prune first …
    by = {x["key"]: x for x in client.get("/api/v1/admin/ads/compliance", headers=headers(admin)).json()["checks"]}
    assert by["retention"]["status"] == "ok"

    # Fakes in prod → fail.
    monkeypatch.setattr(config, "IS_PROD", True)
    monkeypatch.setattr(config, "ADS_FAKE_PROVIDERS", True)
    by = {x["key"]: x for x in client.get("/api/v1/admin/ads/compliance", headers=headers(admin)).json()["checks"]}
    assert by["fakes_off_in_prod"]["status"] == "fail"


def test_suppression_helper():
    rows, folded = ad_analytics.suppress_regions({"DE": [10, 1], "LI": [1, 0], "AT": [4, 2], "FR": [5, 0]}, 5)
    assert [(r.key, r.impressions, r.clicks) for r in rows] == [("DE", 10, 1), ("FR", 5, 0), ("other", 5, 2)]
    assert folded == 2
    assert ad_analytics.suppress_regions({}, 5) == ([], 0)
    assert isinstance(rows[0], schemas.AdStatPoint)


def test_dashboard_can_be_served_by_the_api_and_cors_for_its_own_origin(monkeypatch, tmp_path):
    """Normally the dashboard is its own deployment (CORS); ADS_ADMIN_DIST mounts it here for
    single-process setups — outside the PWA's service-worker scope and without shadowing /api."""
    from fastapi.testclient import TestClient

    dist = tmp_path / "ads-admin"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Ads admin</title>", encoding="utf-8")
    monkeypatch.setattr(config, "ADS_ADMIN_DIST", dist)
    monkeypatch.setattr(config, "CORS_ORIGINS", ["http://localhost:4300"])
    import importlib

    import main as main_module

    fresh = importlib.reload(main_module)
    try:
        with TestClient(fresh.app) as c:
            r = c.get("/ads-admin/")
            assert r.status_code == 200 and "Ads admin" in r.text and r.headers["cache-control"] == "no-cache"
            assert r.headers["x-frame-options"] == "DENY"
            assert c.get("/api/v1/health").status_code == 200
            pre = c.options(
                "/api/v1/admin/ads/analytics",
                headers={
                    "Origin": "http://localhost:4300",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization",
                },
            )
            assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "http://localhost:4300"
            assert (
                c.get("/api/v1/health", headers={"Origin": "http://evil.example"}).headers.get(
                    "access-control-allow-origin"
                )
                is None
            )
    finally:
        monkeypatch.setattr(config, "ADS_ADMIN_DIST", None)
        monkeypatch.setattr(config, "CORS_ORIGINS", [])
        importlib.reload(main_module)
    assert os.environ.get("APP_ENV") == "test"
