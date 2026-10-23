"""Configurable ad sources (docs/ADVERTISING.md §10): the six kinds, their fakes, switching at
runtime, and what each one sends to the client."""

import pytest
from conftest import headers, register

import ad_sources
import config
import database
import models
import seed


@pytest.fixture()
def admin(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAILS", {"admin@example.com"})
    return register(client, email="admin@example.com", password="Admin-pass-123")


@pytest.fixture()
def seeded(client):
    with database.SessionLocal() as db:
        seed.seed_ads(db)
    ad_sources.NETWORK.clear()


def switch(client, admin, kind):
    r = client.put("/api/v1/admin/ads/sources", json={"kind": kind}, headers=headers(admin))
    assert r.status_code == 200, r.text
    return r.json()


def serve(client, **params):
    r = client.get("/api/v1/ads/serve", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_default_source_and_catalogue(client, admin):
    out = client.get("/api/v1/admin/ads/sources", headers=headers(admin)).json()
    assert out["active"] == "sponsored" and out["fakes_enabled"] is True
    by = {s["kind"]: s for s in out["sources"]}
    assert list(by) == ["internal", "sponsored", "affiliate", "adsense", "microsoft", "network"]
    assert by["sponsored"]["active"] and by["sponsored"]["mode"] == "live" and by["sponsored"]["render"] == "creatives"
    for kind in ("adsense", "microsoft", "network"):
        assert by[kind]["mode"] == "fake", kind
    assert by["adsense"]["render"] == "embed" and by["adsense"]["third_party"] and by["adsense"]["consent_required"]
    assert by["adsense"]["embed"]["script_url"].startswith("/api/v1/ads/fake/adsense.js?client=ca-pub-")
    assert by["adsense"]["embed"]["push_global"] == "adsbygoogle"
    assert by["microsoft"]["embed"]["container_class"] == "pubcenter-ad"
    assert by["network"]["render"] == "creatives" and by["network"]["third_party"] is False  # server-to-server
    assert by["affiliate"]["disclosure"].startswith("Affiliate link")
    assert by["internal"]["label"] == "Internal ad" and by["sponsored"]["label"] == "Sponsored"
    # Only administrators.
    user = register(client, email="u@example.com")
    assert client.get("/api/v1/admin/ads/sources", headers=headers(user)).status_code == 403


def test_switching_sources_changes_what_is_served(client, admin, seeded, monkeypatch):
    monkeypatch.setattr(config, "ADS_MAX_PER_RESPONSE", 20)
    monkeypatch.setattr(config, "ADS_AFFILIATE_TAG", "tag=pa-21")

    res = serve(client, region="DE", lang="en")
    assert res["source"]["kind"] == "sponsored" and res["source"]["render"] == "creatives"
    assert all("DUMMY AD" in a["headline"] or "Example" in a["sponsor"] for a in res["ads"])
    assert not any("INTERNAL AD" in a["headline"] or "AFFILIATE" in a["headline"] for a in res["ads"])

    out = switch(client, admin, "internal")
    assert out["active"] == "internal"
    res = serve(client, region="DE", lang="en")
    assert res["source"]["kind"] == "internal" and res["source"]["label"] == "Internal ad"
    assert {a["headline"][:11] for a in res["ads"]} == {"INTERNAL AD"}
    assert any(a["target_url"] == "app:upgrade" for a in res["ads"])  # in-app link allowed for house ads

    switch(client, admin, "affiliate")
    res = serve(client, region="DE", lang="en")
    assert res["source"]["kind"] == "affiliate" and "commission" in res["source"]["disclosure"]
    urls = {a["sponsor"]: a["target_url"] for a in res["ads"]}
    assert urls["Example Store (affiliate)"] == "https://example.com/shop/headphones?tag=pa-21"
    assert urls["Example Books (affiliate)"] == "https://example.com/books/gtd?ref=existing&tag=pa-21"
    assert serve(client, region="DE", lang="de")["ads"][0]["sponsor"] == "Example Store (affiliate)"  # en-only one gone

    for kind, cls in (("adsense", "adsbygoogle"), ("microsoft", "pubcenter-ad")):
        switch(client, admin, kind)
        res = serve(client, region="DE", lang="en")
        assert res["ads"] == [] and res["enabled"] is True
        assert res["source"]["render"] == "embed" and res["source"]["mode"] == "fake"
        assert res["source"]["embed"]["container_class"] == cls and res["source"]["consent_required"] is True

    switch(client, admin, "network")
    res = serve(client, region="JP", lang="ja")
    assert res["source"]["kind"] == "network" and res["source"]["provider_name"] == "Example Ad Network"
    assert res["source"]["render"] == "creatives" and res["source"]["third_party"] is False
    assert [a["id"] for a in res["ads"]] == ["network:fake-net-1-JP", "network:fake-net-2"]
    assert "server-to-server" in res["ads"][0]["headline"]

    # The switch is persisted (survives a restart: it is in app_settings, not in memory) and audited.
    with database.SessionLocal() as db:
        assert db.get(models.AppSetting, "ads.source").value == "network"
    audit = client.get("/api/v1/admin/audit", headers=headers(admin)).json()
    assert [e["action"] for e in audit[:5]] == ["ad.source"] * 5
    assert audit[0]["detail"] == {"target_account_id": None, "before": "microsoft", "after": "network", "mode": "fake"}
    assert switch(client, admin, "network")["active"] == "network"  # idempotent, no new audit row
    assert (
        len([e for e in client.get("/api/v1/admin/audit", headers=headers(admin)).json() if e["action"] == "ad.source"])
        == 5
    )


def test_unavailable_sources_without_fakes_or_credentials(client, admin, monkeypatch):
    monkeypatch.setattr(config, "ADS_FAKE_PROVIDERS", False)
    by = {s["kind"]: s for s in client.get("/api/v1/admin/ads/sources", headers=headers(admin)).json()["sources"]}
    for kind in ("adsense", "microsoft", "network"):
        assert by[kind]["mode"] == "unavailable" and "ADS_" in by[kind]["reason"]
    r = client.put("/api/v1/admin/ads/sources", json={"kind": "adsense"}, headers=headers(admin))
    assert r.status_code == 409 and "ADS_ADSENSE_CLIENT" in r.json()["detail"]
    assert client.get("/api/v1/ads/fake/adsense.js").status_code == 404
    assert client.get("/api/v1/ads/fake/network.json").status_code == 404
    # An env default pointing at an unavailable source → the panel is simply off.
    monkeypatch.setattr(config, "ADS_SOURCE", "microsoft")
    res = serve(client, region="DE")
    assert res["enabled"] is False and res["ads"] == [] and res["source"]["mode"] == "unavailable"

    # With credentials the real tags are used.
    monkeypatch.setattr(config, "ADS_ADSENSE_CLIENT", "ca-pub-1234567890123456")
    monkeypatch.setattr(config, "ADS_ADSENSE_SLOT", "9876543210")
    monkeypatch.setattr(config, "ADS_MICROSOFT_SITE_ID", "111")
    monkeypatch.setattr(config, "ADS_MICROSOFT_PUBLISHER_ID", "222")
    monkeypatch.setattr(config, "ADS_MICROSOFT_AD_UNIT_ID", "333")
    monkeypatch.setattr(config, "ADS_NETWORK_NAME", "Acme Ads")
    monkeypatch.setattr(config, "ADS_NETWORK_SCRIPT_URL", "https://tags.acme-ads.example/tag.js")
    by = {s["kind"]: s for s in client.get("/api/v1/admin/ads/sources", headers=headers(admin)).json()["sources"]}
    assert by["adsense"]["mode"] == "live"
    assert by["adsense"]["embed"]["script_url"] == (
        "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-1234567890123456"
    )
    assert by["adsense"]["embed"]["container_attrs"]["data-ad-slot"] == "9876543210"
    assert (
        by["microsoft"]["embed"]["script_url"]
        == "https://adsdk.microsoft.com/pubcenter/sdk.js?siteId=111&publisherId=222"
    )
    assert by["microsoft"]["embed"]["container_attrs"]["data-ad-unit-id"] == "333"
    assert by["network"]["mode"] == "live" and by["network"]["render"] == "embed" and by["network"]["third_party"]
    assert by["network"]["label"] == "Ad · Acme Ads" and by["network"]["embed"]["script_url"].endswith("tag.js")


def test_fake_provider_scripts_and_network_endpoint(client):
    for name, marker in (("adsense", "adsbygoogle"), ("microsoft", "pubcenter-ad"), ("network", "ad-network-slot")):
        r = client.get(f"/api/v1/ads/fake/{name}.js")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/javascript")
        assert marker in r.text and "FAKE" in r.text and "__NAME__" not in r.text
    assert "window.adsbygoogle = { push" in client.get("/api/v1/ads/fake/adsense.js").text
    assert client.get("/api/v1/ads/fake/nope.js").status_code == 404
    payload = client.get("/api/v1/ads/fake/network.json?region=fr&lang=fr-FR").json()
    assert payload["request"] == {"region": "FR", "lang": "fr", "note": payload["request"]["note"]}
    assert len(payload["ads"]) == 2 and payload["ads"][0]["url"].startswith("https://")


def test_network_json_normalisation_and_failure_tolerance(monkeypatch):
    ads = ad_sources.normalize_network_payload(
        {
            "ads": [
                {"id": 7, "advertiser": "Acme", "title": "T", "text": "B", "cta": "Go", "url": "https://a.example/x"},
                {"id": 8, "title": "no https", "url": "http://a.example/x"},  # dropped
                "garbage",
                {"sponsor": "S", "headline": "H", "target_url": "https://b.example", "image_url": "http://img"},
            ]
        }
    )
    assert [a.id for a in ads] == ["network:7", "network:1"]
    assert ads[0].sponsor == "Acme" and ads[0].cta_label == "Go" and ads[1].image_url is None
    assert ad_sources.normalize_network_payload(["x", None]) == []
    # Live endpoint unreachable → no ads, no exception, cached for a minute.
    monkeypatch.setattr(config, "ADS_NETWORK_JSON_URL", "http://127.0.0.1:9/ads?region={region}&lang={lang}")
    net = ad_sources.NetworkClient()
    assert net.creatives("DE", "de") == [] and net.creatives("DE", "de") == []
    assert ("DE", "de") in net._cache


def test_affiliate_url_tagging(monkeypatch):
    monkeypatch.setattr(config, "ADS_AFFILIATE_TAG", "tag=pa-21")
    assert ad_sources.affiliate_url("https://shop.example/p") == "https://shop.example/p?tag=pa-21"
    assert ad_sources.affiliate_url("https://shop.example/p?a=1#top") == "https://shop.example/p?a=1&tag=pa-21#top"
    assert ad_sources.affiliate_url("https://shop.example/p?tag=mine") == "https://shop.example/p?tag=mine"
    monkeypatch.setattr(config, "ADS_AFFILIATE_TAG", "")
    assert ad_sources.affiliate_url("https://shop.example/p") == "https://shop.example/p"


def test_campaign_kind_rules(client, admin):
    base = {"sponsor": "X", "headline": "H", "target_url": "app:upgrade", "status": "active"}
    r = client.post("/api/v1/admin/ads", json={**base, "kind": "sponsored"}, headers=headers(admin))
    assert r.status_code == 422 and "internal" in r.json()["detail"]
    r = client.post("/api/v1/admin/ads", json={**base, "kind": "internal"}, headers=headers(admin))
    assert r.status_code == 201 and r.json()["kind"] == "internal"
    r = client.patch(f"/api/v1/admin/ads/{r.json()['id']}", json={"kind": "affiliate"}, headers=headers(admin))
    assert r.status_code == 422
    assert client.post("/api/v1/admin/ads", json={**base, "kind": "house"}, headers=headers(admin)).status_code == 422
    assert (
        client.post(
            "/api/v1/admin/ads", json={**base, "target_url": "app:evil", "kind": "internal"}, headers=headers(admin)
        ).status_code
        == 422
    )
    # Default kind is sponsored; the list shows it.
    r = client.post("/api/v1/admin/ads", json={**base, "target_url": "https://x.example"}, headers=headers(admin))
    assert r.json()["kind"] == "sponsored"
