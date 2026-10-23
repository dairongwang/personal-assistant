"""Configurable ad sources end to end: an administrator switches the source in the separate ads
admin console; an anonymous visitor's dashboard shows each one — the three first-party kinds from
our own campaigns, and the three third-party kinds through their (fake) provider tags behind a
consent card — with the right label, disclosure and network behaviour (docs/ADVERTISING.md §10)."""

import json
import urllib.request

from test_ads_admin import console_sign_in

KINDS = ("internal", "sponsored", "affiliate", "adsense", "microsoft", "network")


def switch_source(ad, kind: str):
    """Ads admin console → Ad source radio."""
    if "#/sources" not in ad.url:
        ad.click("[data-nav='sources']")
    ad.wait_for_selector("#sources")
    ad.check(f"#source-{kind}")
    ad.wait_for_selector(f".source.active[data-kind='{kind}']", timeout=10000)


def fresh_visitor(browser, server, **ctx):
    context = browser.new_context(
        viewport={"width": 1200, "height": 900}, timezone_id="Europe/Berlin", locale="en-GB", **ctx
    )
    pg = context.new_page()
    requests = []
    pg.on("request", lambda r: requests.append(r.url))
    pg.add_init_script("localStorage.setItem('pa.settings.v1', JSON.stringify({helpHintShown:true}))")
    pg.goto(server + "/")
    pg.wait_for_selector("#ad-panel", timeout=10000)
    return context, pg, requests


def test_every_source_can_be_switched_to_and_displays(browser, server, releaser):
    admin_ctx, ad = console_sign_in(browser, server + "/ads-admin/")

    # The catalogue: all six, the default active, third parties as built-in fakes.
    ad.click("[data-nav='sources']")
    ad.wait_for_selector("#sources")
    for kind in KINDS:
        assert ad.locator(f"#source-{kind}").count() == 1, kind
    assert ad.locator(".source.active[data-kind='sponsored']").count() == 1
    for kind in ("adsense", "microsoft", "network"):
        assert "fake" in ad.locator(f".source[data-kind='{kind}'] .badge.fake").inner_text()

    # ---- 1. Internal ads: our own promotion, may link into the app.
    switch_source(ad, "internal")
    ctx, pg, reqs = fresh_visitor(browser, server)
    pg.wait_for_selector("#ad-body")
    assert pg.inner_text("#ad-label").strip().lower() == "internal ad"
    assert pg.get_attribute("#ad-panel", "data-source") == "internal"
    assert "INTERNAL AD" in pg.inner_text("#ad-body")
    assert "not a paid placement" in pg.inner_text("#ad-disclosure")
    assert pg.locator("#ad-fake").count() == 0  # first-party sources are real, not fakes
    for _ in range(3):  # find the "Go Pro" house ad (weight 30 → usually first)
        if "Go Pro" in pg.inner_text("#ad-body"):
            break
        pg.click("#ad-dismiss")
        pg.wait_for_timeout(200)
    assert "Go Pro" in pg.inner_text("#ad-body")
    pg.click("#ad-panel .ad-link")  # app:upgrade → the account screen, no new tab
    pg.wait_for_timeout(300)
    assert pg.evaluate("document.getElementById('account-dialog').open")
    assert len(pg.context.pages) == 1
    ctx.close()

    # ---- 2. Sponsored content: paid placements we host (the default).
    switch_source(ad, "sponsored")
    ctx, pg, reqs = fresh_visitor(browser, server)
    pg.wait_for_selector("#ad-body")
    assert pg.inner_text("#ad-label").strip().lower() == "sponsored"
    assert "Paid placement" in pg.inner_text("#ad-disclosure")
    assert "INTERNAL AD" not in pg.inner_text("#ad-body") and "AFFILIATE" not in pg.inner_text("#ad-body")
    ctx.close()

    # ---- 3. Affiliate links: commission disclosure + tag appended to the link.
    switch_source(ad, "affiliate")
    ctx, pg, reqs = fresh_visitor(browser, server)
    pg.wait_for_selector("#ad-body")
    assert pg.inner_text("#ad-label").strip().lower() == "affiliate link"
    assert "AFFILIATE DUMMY" in pg.inner_text("#ad-body")
    assert "commission" in pg.inner_text("#ad-disclosure")
    href = pg.get_attribute("#ad-panel .ad-link", "href")
    assert href.startswith("https://example.com/") and "tag=e2e-21" in href, href
    ctx.close()

    # ---- 4./5. Google AdSense and Microsoft Advertising: consent card first, then the provider tag.
    for kind, label, container, box_text in (
        ("adsense", "ad · google adsense", "ins.adsbygoogle", "Google AdSense — FAKE"),
        ("microsoft", "ad · microsoft advertising", "div.pubcenter-ad", "Microsoft Advertising — FAKE"),
    ):
        switch_source(ad, kind)
        ctx, pg, reqs = fresh_visitor(browser, server)
        pg.wait_for_selector("#ad-consent")
        assert pg.inner_text("#ad-label").strip().lower() == label
        assert pg.inner_text("#ad-fake") == "FAKE"
        assert pg.locator(container).count() == 0
        assert not any(f"/ads/fake/{kind}.js" in u for u in reqs), "nothing loaded before consent"
        # Decline → still nothing; allow → the tag loads through the provider's own markup.
        pg.click("#ad-consent-deny")
        pg.wait_for_timeout(300)
        assert "declined" in pg.inner_text("#ad-consent") and not any(f"/ads/fake/{kind}.js" in u for u in reqs)
        pg.click("#ad-consent-allow")
        pg.wait_for_selector(f"{container} .fake-ad-box", timeout=10000)
        assert any(f"/ads/fake/{kind}.js" in u for u in reqs)
        assert box_text in pg.inner_text(f"{container}")
        pg.wait_for_selector("#ad-slot-status:has-text('rendered')", timeout=5000)
        # Every request stayed on this origin: the fake never calls out.
        assert all(u.startswith(server) or u.startswith("data:") for u in reqs), [
            u for u in reqs if not u.startswith(server)
        ]
        # Consent persists across reloads and can be withdrawn in "Why this?".
        pg.reload()
        pg.wait_for_selector(f"{container} .fake-ad-box", timeout=10000)
        pg.click("#ad-why")
        assert "allowed on this device" in pg.inner_text("#ad-info-source")
        pg.click("#ad-info-withdraw")
        pg.keyboard.press("Escape")
        pg.wait_for_selector("#ad-consent")
        ctx.close()

    # ---- 6. Third-party network: creatives relayed server-to-server, no consent needed.
    switch_source(ad, "network")
    ctx, pg, reqs = fresh_visitor(browser, server)
    pg.wait_for_selector("#ad-body")
    assert pg.inner_text("#ad-label").strip().lower() == "ad · example ad network"
    assert pg.inner_text("#ad-fake") == "FAKE"
    assert "FAKE NETWORK AD" in pg.inner_text("#ad-body") and "server-to-server" in pg.inner_text("#ad-body")
    assert pg.locator("#ad-consent").count() == 0
    assert all(u.startswith(server) or u.startswith("data:") for u in reqs)
    pg.click("#ad-why")
    assert "your browser never contacts the network" in pg.inner_text("#ad-info-source").lower()
    ctx.close()
    with urllib.request.urlopen(server + "/api/v1/ads/fake/network.json?region=DE&lang=en") as r:
        assert "FAKE" in json.load(r)["network"]

    # Back to the default, and the switches were audited.
    switch_source(ad, "sponsored")
    with urllib.request.urlopen(server + "/api/v1/ads/serve?region=DE&lang=en") as r:
        assert json.load(r)["source"]["kind"] == "sponsored"
    token = ad.evaluate("sessionStorage.getItem('adsadmin.token')")
    req = urllib.request.Request(server + "/api/v1/admin/audit?limit=20", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        actions = [e for e in json.load(r) if e["action"] == "ad.source"]
    assert [e["detail"]["after"] for e in actions[:7]] == [
        "sponsored", "network", "microsoft", "adsense", "affiliate", "sponsored", "internal"
    ]  # fmt: skip
    admin_ctx.close()
