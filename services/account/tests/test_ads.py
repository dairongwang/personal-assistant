"""Sponsored content: region-targeted serving, anonymous counting, admin management, and the
privacy properties the design promises (docs/ADVERTISING.md)."""

import random
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import headers, register

import ads
import config
import database
import models
import seed


@pytest.fixture()
def admin(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAILS", {"admin@example.com"})
    return register(client, email="admin@example.com", password="Admin-pass-123")


def campaign(**over):
    base = {
        "sponsor": "Example Co",
        "headline": "A headline",
        "blurb": "Some blurb.",
        "cta_label": "Learn more",
        "target_url": "https://example.com/x",
        "status": "active",
    }
    return {**base, **over}


def create(client, admin, **over):
    r = client.post("/api/v1/admin/ads", json=campaign(**over), headers=headers(admin))
    assert r.status_code == 201, r.text
    return r.json()


def serve(client, region=None, lang=None, **extra):
    params = {}
    if region is not None:
        params["region"] = region
    if lang is not None:
        params["lang"] = lang
    r = client.get("/api/v1/ads/serve", params=params, **extra)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------- serving


def test_serve_is_public_contextual_and_region_aware(client, admin):
    everywhere = create(client, admin, headline="Everywhere")
    dach = create(client, admin, headline="DACH only", regions="de, at ,ch")
    eu = create(client, admin, headline="EU group", regions="EU")
    us_en = create(client, admin, headline="US English", regions="US", languages="en")
    assert dach["regions"] == "DE,AT,CH" and us_en["languages"] == "en"

    by = lambda res: {a["headline"] for a in res["ads"]}  # noqa: E731

    # No token anywhere in these calls — and no region: only worldwide campaigns.
    res = serve(client)
    assert by(res) == {"Everywhere"} and res["region"] == "ZZ"
    assert res["rotation_seconds"] == config.ADS_ROTATION_SECONDS and res["enabled"] is True
    # The user explicitly chose "no region".
    assert by(serve(client, region="ZZ")) == {"Everywhere"}
    # Germany: worldwide + DACH + EU; Switzerland: no EU.
    assert by(serve(client, region="de")) == {"Everywhere", "DACH only", "EU group"}
    assert by(serve(client, region="CH")) == {"Everywhere", "DACH only"}
    # Language gate uses the primary subtag only.
    assert by(serve(client, region="US", lang="en-US")) == {"Everywhere", "US English"}
    assert by(serve(client, region="US", lang="es")) == {"Everywhere"}
    # Garbage region → treated as unknown, never an error.
    assert by(serve(client, region="x1")) == {"Everywhere"}
    # The response carries nothing but the creative.
    assert set(res["ads"][0]) == {"id", "sponsor", "headline", "blurb", "cta_label", "target_url", "image_url"}
    assert everywhere["id"] == res["ads"][0]["id"] and eu["live"] is True


def test_serve_respects_status_schedule_budget_and_limit(client, admin, monkeypatch):
    now = datetime.now(timezone.utc)
    create(client, admin, headline="live")
    create(client, admin, headline="draft", status="draft")
    create(client, admin, headline="paused", status="paused")
    create(client, admin, headline="future", starts_at=(now + timedelta(days=1)).isoformat())
    create(client, admin, headline="expired", ends_at=(now - timedelta(days=1)).isoformat())
    create(
        client,
        admin,
        headline="current",
        starts_at=(now - timedelta(days=1)).isoformat(),
        ends_at=(now + timedelta(days=1)).isoformat(),
    )
    capped = create(client, admin, headline="capped", max_impressions=1)
    assert {a["headline"] for a in serve(client)["ads"]} <= {"live", "current", "capped"}
    monkeypatch.setattr(config, "ADS_MAX_PER_RESPONSE", 10)
    assert {a["headline"] for a in serve(client)["ads"]} == {"live", "current", "capped"}

    # One impression exhausts the budget → no longer served, marked not live for the admin.
    client.post("/api/v1/ads/metrics", json={"events": [{"ad_id": capped["id"], "kind": "impression"}]})
    assert {a["headline"] for a in serve(client)["ads"]} == {"live", "current"}
    assert client.get(f"/api/v1/admin/ads/{capped['id']}", headers=headers(admin)).json()["live"] is False

    monkeypatch.setattr(config, "ADS_MAX_PER_RESPONSE", 2)
    assert len(serve(client)["ads"]) == 2
    # Kill switch.
    monkeypatch.setattr(config, "ADS_ENABLED", False)
    res = serve(client, region="DE")
    assert res["ads"] == [] and res["enabled"] is False


def test_weighted_rotation_prefers_heavier_campaigns_but_shows_all():
    light = models.AdCampaign(id="light", weight=1)
    heavy = models.AdCampaign(id="heavy", weight=99)
    firsts = [ads._weighted_order([light, heavy], random.Random(i))[0].id for i in range(200)]
    assert firsts.count("heavy") > 150 and firsts.count("light") > 0
    assert {a.id for a in ads._weighted_order([light, heavy], random.Random(1))} == {"light", "heavy"}


def test_edge_country_header_is_opt_in_and_never_overrides_the_user(client, admin, monkeypatch):
    create(client, admin, headline="FR only", regions="FR")
    # Default: the header is ignored even if a proxy sends it.
    assert serve(client, headers={"CF-IPCountry": "FR"})["ads"] == []
    monkeypatch.setattr(config, "ADS_REGION_HEADER", "CF-IPCountry")
    # Enabled by the operator: used only when the client sent nothing …
    assert {a["headline"] for a in serve(client, headers={"CF-IPCountry": "FR"})["ads"]} == {"FR only"}
    # … never when the user chose "no region" or another region.
    assert serve(client, region="ZZ", headers={"CF-IPCountry": "FR"})["ads"] == []
    assert serve(client, region="DE", headers={"CF-IPCountry": "FR"})["ads"] == []


# ---------------------------------------------------------------------------- anonymous metrics


def test_metrics_are_aggregated_per_ad_day_and_region_only(client, admin):
    ad = create(client, admin, headline="x")
    other = create(client, admin, headline="y")
    events = [
        {"ad_id": ad["id"], "kind": "impression", "region": "DE"},
        {"ad_id": ad["id"], "kind": "impression", "region": "DE"},  # duplicate in one batch: counted once
        {"ad_id": ad["id"], "kind": "click", "region": "DE"},
        {"ad_id": other["id"], "kind": "impression"},  # no region → ZZ
        {"ad_id": "does-not-exist", "kind": "impression", "region": "DE"},  # dropped silently
    ]
    r = client.post("/api/v1/ads/metrics", json=events and {"events": events})
    assert r.status_code == 200 and r.json() == {"accepted": 3}
    client.post("/api/v1/ads/metrics", json={"events": [{"ad_id": ad["id"], "kind": "impression", "region": "de"}]})

    with database.SessionLocal() as db:
        rows = db.query(models.AdDailyStat).order_by(models.AdDailyStat.region).all()
        assert [(r.ad_id, r.region, r.impressions, r.clicks) for r in rows] == [
            (ad["id"], "DE", 2, 1),
            (other["id"], "ZZ", 1, 0),
        ]
        assert all(r.day == date.today() or abs((r.day - date.today()).days) <= 1 for r in rows)
        # Nothing else was written anywhere: no per-event rows, no audit entries for visitors.
        assert db.query(models.AuditLog).filter(models.AuditLog.action.like("ad.%")).count() == 2  # the two creates

    detail = client.get(f"/api/v1/admin/ads/{ad['id']}", headers=headers(admin)).json()
    assert detail["impressions_total"] == 2 and detail["clicks_total"] == 1
    stats = client.get(f"/api/v1/admin/ads/{ad['id']}/stats?days=7", headers=headers(admin)).json()
    assert stats["impressions"] == 2 and stats["clicks"] == 1 and stats["ctr"] == 0.5
    assert stats["by_region"] == [{"key": "DE", "impressions": 2, "clicks": 1}]
    assert len(stats["by_day"]) == 1

    # Validation: kinds are an enum, batches are capped, bodies must be well-formed.
    assert (
        client.post("/api/v1/ads/metrics", json={"events": [{"ad_id": ad["id"], "kind": "hover"}]}).status_code == 422
    )
    too_many = {"events": [{"ad_id": ad["id"], "kind": "click"}] * 21}
    assert client.post("/api/v1/ads/metrics", json=too_many).status_code == 422
    assert client.post("/api/v1/ads/metrics", json={"events": []}).json() == {"accepted": 0}


def test_metrics_for_archived_ads_are_dropped_and_old_rows_pruned(client, admin, monkeypatch):
    ad = create(client, admin)
    client.delete(f"/api/v1/admin/ads/{ad['id']}", headers=headers(admin))
    assert client.post("/api/v1/ads/metrics", json={"events": [{"ad_id": ad["id"], "kind": "click"}]}).json() == {
        "accepted": 0
    }
    with database.SessionLocal() as db:
        db.add(models.AdDailyStat(ad_id=ad["id"], day=date.today() - timedelta(days=500), region="DE", impressions=5))
        db.add(models.AdDailyStat(ad_id=ad["id"], day=date.today() - timedelta(days=5), region="DE", impressions=5))
        db.commit()
        assert ads.prune_stats(db) == 1
        assert db.query(models.AdDailyStat).count() == 1
        monkeypatch.setattr(config, "ADS_STATS_RETENTION_DAYS", 1)
        assert ads.prune_stats(db) == 1


def test_anonymous_endpoints_are_rate_limited_in_memory_only(client, admin, monkeypatch):
    ad = create(client, admin)
    limiter = ads.RateLimiter(limit=3, window_seconds=600)
    monkeypatch.setattr(ads, "METRICS_LIMITER", limiter)
    body = {"events": [{"ad_id": ad["id"], "kind": "impression"}]}
    for _ in range(3):
        assert client.post("/api/v1/ads/metrics", json=body).status_code == 200
    assert client.post("/api/v1/ads/metrics", json=body).status_code == 429
    # The window slides; and the key is a salted hash, never the address itself.
    assert limiter.allow("testclient", now=time.monotonic() + 601)
    assert "testclient" not in str(limiter._hits) and len(limiter._hits) >= 1
    monkeypatch.setattr(ads, "SERVE_LIMITER", ads.RateLimiter(limit=1, window_seconds=600))
    assert client.get("/api/v1/ads/serve").status_code == 200
    assert client.get("/api/v1/ads/serve").status_code == 429


# ---------------------------------------------------------------------------- administration


def test_admin_manages_campaigns_with_audit_trail(client, admin):
    user = register(client, email="user@example.com")
    # Only administrators.
    assert client.get("/api/v1/admin/ads", headers=headers(user)).status_code == 403
    assert client.post("/api/v1/admin/ads", json=campaign(), headers=headers(user)).status_code == 403
    assert client.get("/api/v1/admin/ads").status_code == 401

    ad = create(client, admin, status="draft", regions="EU, us", weight=30)
    assert ad["status"] == "draft" and ad["live"] is False and ad["regions"] == "EU,US" and ad["weight"] == 30
    assert serve(client, region="DE")["ads"] == []  # drafts are never served

    r = client.patch(
        f"/api/v1/admin/ads/{ad['id']}",
        json={"status": "active", "headline": "  New   headline "},
        headers=headers(admin),
    )
    assert r.status_code == 200 and r.json()["headline"] == "New headline" and r.json()["live"] is True
    assert [a["id"] for a in serve(client, region="DE")["ads"]] == [ad["id"]]

    listed = client.get("/api/v1/admin/ads", headers=headers(admin)).json()
    assert [a["id"] for a in listed] == [ad["id"]]
    overview = client.get("/api/v1/admin/ads/overview", headers=headers(admin)).json()
    assert overview["live"] == 1 and overview["campaigns"]["active"] == 1 and "EU" in overview["region_groups"]

    archived = client.delete(f"/api/v1/admin/ads/{ad['id']}", headers=headers(admin)).json()
    assert archived["status"] == "archived"
    assert client.get("/api/v1/admin/ads", headers=headers(admin)).json() == []
    assert len(client.get("/api/v1/admin/ads?include_archived=true", headers=headers(admin)).json()) == 1
    assert serve(client, region="DE")["ads"] == []

    actions = [e["action"] for e in client.get("/api/v1/admin/audit", headers=headers(admin)).json()]
    assert actions[:3] == ["ad.archive", "ad.update", "ad.create"]
    entry = client.get("/api/v1/admin/audit", headers=headers(admin)).json()[1]
    assert entry["actor_email"] == "admin@example.com" and entry["detail"]["changes"]["headline"][1] == "New headline"
    assert client.get("/api/v1/admin/ads/nope", headers=headers(admin)).status_code == 404


def test_campaign_validation(client, admin, monkeypatch):
    bad = lambda **o: client.post("/api/v1/admin/ads", json=campaign(**o), headers=headers(admin))  # noqa: E731
    assert bad(target_url="http://example.com").status_code == 422  # https only
    assert bad(target_url="javascript:alert(1)").status_code == 422
    assert bad(image_url="http://example.com/i.png").status_code == 422
    # Images only from first-party hosts (a sponsor-hosted image would leak viewers' IPs).
    r = bad(image_url="https://sponsor.example.net/i.png")
    assert r.status_code == 422 and "first-party" in r.json()["detail"]
    monkeypatch.setattr(config, "ADS_IMAGE_HOSTS", {"cdn.example.com"})
    assert bad(image_url="https://sponsor.example.net/i.png").status_code == 422
    assert bad(image_url="https://CDN.example.com/i.png").status_code == 201
    assert bad(regions="Germany").status_code == 422
    assert bad(languages="deutsch").status_code == 422
    assert bad(status="running").status_code == 422
    assert bad(weight=0).status_code == 422
    assert bad(headline="x" * 91).status_code == 422
    now = datetime.now(timezone.utc)
    assert bad(starts_at=now.isoformat(), ends_at=(now - timedelta(hours=1)).isoformat()).status_code == 422
    ok = bad(image_url="", regions="", languages="")  # empty optionals are fine
    assert ok.status_code == 201 and ok.json()["image_url"] is None
    r = client.patch(f"/api/v1/admin/ads/{ok.json()['id']}", json={"regions": "Mars"}, headers=headers(admin))
    assert r.status_code == 422 and "Mars" in r.json()["detail"]


def test_paid_memberships_are_ad_free_by_strategy(client):
    user = register(client, email="u@example.com")
    ent = client.get("/api/v1/me/entitlements", headers=headers(user)).json()
    assert "ad_free" not in ent["features"]
    client.post("/api/v1/billing/trial", headers=headers(user))
    ent = client.get("/api/v1/me/entitlements", headers=headers(user)).json()
    assert "ad_free" in ent["features"]
    catalog = {m["type"]: m for m in client.get("/api/v1/membership").json()["memberships"]}
    assert "ad_free" not in catalog["anonymous"]["features"] and "ad_free" not in catalog["free"]["features"]
    assert "ad_free" in catalog["paid"]["features"] and "ad_free" in catalog["admin"]["features"]


def test_seeded_campaigns_target_different_regions(client, monkeypatch):
    with database.SessionLocal() as db:
        first = seed.seed_ads(db)
        again = seed.seed_ads(db)  # idempotent
        assert [a.id for a in first] == [a.id for a in again]
        assert db.query(models.AdCampaign).count() == len(seed.FAKE_ADS)
    monkeypatch.setattr(config, "ADS_MAX_PER_RESPONSE", 20)
    sponsors = lambda **kw: {a["sponsor"] for a in serve(client, **kw)["ads"]}  # noqa: E731
    assert sponsors() == {"Dummy Sponsor (worldwide)", "Example Notebooks"}  # no region, no language
    assert sponsors(region="DE", lang="de-DE") == {
        "Dummy Sponsor (worldwide)",
        "Dummy Sponsor (Europe)",
        "Example Notebooks",
        "Beispiel Kaffeerösterei",
        "Example Rail Pass",
    }
    assert sponsors(region="US", lang="en-US") == {
        "Dummy Sponsor (worldwide)",
        "Dummy Sponsor (English)",
        "Dummy Sponsor (Americas)",
        "Example Notebooks",
        "Example Credit Union",
    }
    assert sponsors(region="JP", lang="ja") == {
        "Dummy Sponsor (worldwide)",
        "Dummy Sponsor (Asia-Pacific)",
        "Example Notebooks",
    }
    assert "Example Standing Desks" not in sponsors(region="GB", lang="en")  # paused
