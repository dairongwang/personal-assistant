"""Sponsored content end to end (docs/ADVERTISING.md): a free user in Germany sees a clearly
labelled, region-matched panel requested without any token; the explainer shows what is used;
switching it off removes it; Pro removes it; an administrator creates a campaign and reads the
anonymous totals after a real click."""

import json
import time
import urllib.request

from test_membership import sign_in

DUMMY_WORLDWIDE = {"Dummy Sponsor (worldwide)", "Example Notebooks"}
DE_SPONSORS = DUMMY_WORLDWIDE | {"Dummy Sponsor (Europe)", "Beispiel Kaffeerösterei", "Example Rail Pass"}
US_SPONSORS = DUMMY_WORLDWIDE | {"Dummy Sponsor (English)", "Dummy Sponsor (Americas)", "Example Credit Union"}


def watch_ads(page):
    """Collect every sponsored-content request the page makes (url, headers, body)."""
    calls = []
    page.on(
        "request",
        lambda r: (
            calls.append({"url": r.url, "headers": r.headers, "body": r.post_data}) if "/api/v1/ads/" in r.url else None
        ),
    )
    return calls


def sponsor(pg) -> str:
    return pg.inner_text("#ad-panel .ad-sponsor").strip()


def wait_for_metrics(pg, calls, kind: str, seconds=8) -> dict | None:
    """Playwright (sync) dispatches request events only while it is waiting on the page."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        for c in calls:
            if c["url"].endswith("/ads/metrics") and c["body"] and kind in c["body"]:
                return c
        pg.wait_for_timeout(250)
    return None


def test_free_user_sees_a_labelled_regional_panel_and_controls_it(browser, server, releaser):
    ctx = browser.new_context(viewport={"width": 1200, "height": 900}, timezone_id="Europe/Berlin", locale="de-DE")
    pg = ctx.new_page()
    calls = watch_ads(pg)
    pg.goto(server + "/")
    pg.wait_for_selector("#ad-body", timeout=10000)

    # 1. Clearly labelled, region-matched, part of the page flow (not an overlay).
    assert pg.inner_text("#ad-label").strip().lower() == "sponsored"  # rendered upper-case by CSS
    assert sponsor(pg) in DE_SPONSORS
    # The targeting is written on the panel, highlighted: country + language (UI language is German).
    assert pg.inner_text("#ad-target-country") == "Deutschland (DE)"
    assert pg.inner_text("#ad-target-lang") == "Deutsch (de)"
    assert "time zone" in pg.inner_text("#ad-target")
    assert pg.get_attribute("#ad-panel", "data-region") == "DE" and pg.get_attribute("#ad-panel", "data-lang") == "de"
    # Large showcase layout by default: about a third of the viewport height, in the sidebar column.
    box = pg.locator("#ad-panel").bounding_box()
    assert box["height"] >= 0.33 * 900 - 1 and pg.locator("#ad-panel.large").count() == 1
    assert "view" in pg.inner_text("#ad-debug")
    assert pg.locator(".sidebar #ad-panel").count() == 1
    assert pg.evaluate("getComputedStyle(document.getElementById('ad-panel')).position") == "static"
    link = pg.locator("#ad-panel .ad-link")
    assert link.get_attribute("target") == "_blank"
    assert "sponsored" in (link.get_attribute("rel") or "") and "noreferrer" in (link.get_attribute("rel") or "")
    assert link.get_attribute("href").startswith("https://")

    # 2. The request carried the on-device region/language and no token; metrics are anonymous.
    serve = [c for c in calls if "/ads/serve" in c["url"]]
    assert serve and serve[0]["url"].endswith("/ads/serve?region=DE&lang=de")
    assert "authorization" not in {k.lower() for k in serve[0]["headers"]}
    impression = wait_for_metrics(pg, calls, "impression")
    assert impression, "an impression should be counted after ~1 s of visibility"
    body = json.loads(impression["body"])
    assert body["events"][0]["kind"] == "impression" and body["events"][0]["region"] == "DE"
    assert set(body["events"][0]) == {"ad_id", "kind", "region"}
    assert "authorization" not in {k.lower() for k in impression["headers"]}
    assert "cookie" not in {k.lower() for k in impression["headers"]}

    # 3. "Why this?" explains what is used and offers the controls.
    pg.click("#ad-why")
    assert pg.evaluate("document.getElementById('ad-info-dialog').open")
    assert "Deutschland (detected on this device)" in pg.inner_text("#ad-info-region")  # UI language = de
    assert pg.locator("#ad-info-upgrade").count() == 1  # "Go Pro — no sponsored content"
    # Hide one creative: it is replaced (or the panel goes away when nothing is left).
    pg.keyboard.press("Escape")
    first = pg.get_attribute("#ad-panel", "data-ad-id")
    pg.click("#ad-dismiss")
    pg.wait_for_timeout(300)
    assert pg.locator("#ad-panel").count() == 0 or pg.get_attribute("#ad-panel", "data-ad-id") != first
    # Fold / unfold.
    if pg.locator("#ad-panel").count():
        pg.click("#ad-toggle")
        pg.wait_for_selector("#ad-panel.collapsed")
        assert pg.locator("#ad-body").count() == 0
        pg.click("#ad-toggle")
        pg.wait_for_selector("#ad-body")

    # 4. Off switch (GDPR Art. 21 objection): the panel is gone and nothing more is requested.
    pg.click("#footer-ad-info")
    pg.click("#ad-enabled")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(300)
    assert pg.locator("#ad-panel").count() == 0
    assert pg.inner_text("#footer-ad-info").strip() == "sponsored content: off"
    before = len([c for c in calls if "/ads/serve" in c["url"]])
    pg.reload()
    pg.wait_for_timeout(2500)
    assert pg.locator("#ad-panel").count() == 0
    assert len([c for c in calls if "/ads/serve" in c["url"]]) == before  # no request while off
    pg.click("#footer-ad-info")
    pg.click("#ad-enabled")
    pg.keyboard.press("Escape")
    pg.wait_for_selector("#ad-body", timeout=10000)

    # 5. Pro membership has the ad_free feature: no panel, no footer link, no requests.
    sign_in(pg, "pro@example.com", "Pro-pass-123")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(800)
    assert pg.locator("#ad-panel").count() == 0 and pg.locator("#footer-ad-info").count() == 0
    n = len(calls)
    pg.reload()
    pg.wait_for_timeout(2500)
    # (An impression seen *before* signing in may still be flushed on unload — that is fine.)
    assert len([c for c in calls[n:] if "/ads/serve" in c["url"]]) == 0
    ctx.close()


def test_region_targeting_follows_the_device_and_the_users_choice(browser, server, releaser):
    us = browser.new_context(viewport={"width": 1200, "height": 900}, timezone_id="America/New_York", locale="en-US")
    pg = us.new_page()
    calls = watch_ads(pg)
    pg.goto(server + "/")
    pg.wait_for_selector("#ad-body", timeout=10000)
    assert sponsor(pg) in US_SPONSORS
    assert [c for c in calls if "/ads/serve" in c["url"]][0]["url"].endswith("?region=US&lang=en")
    assert pg.inner_text("#ad-target-country") == "United States (US)"
    assert pg.inner_text("#ad-target-lang") == "English (en)"
    assert "Germany" not in pg.inner_text("#ad-panel")

    # "No region" → worldwide creatives only, sent as ZZ.
    pg.click("#ad-why")
    assert "United States" in pg.inner_text("#ad-info-region")
    pg.select_option("#ad-region-mode", "off")
    pg.wait_for_timeout(800)
    assert "none" in pg.inner_text("#ad-info-region")
    pg.keyboard.press("Escape")
    pg.wait_for_selector("#ad-body")
    assert [c for c in calls if "/ads/serve" in c["url"]][-1]["url"].endswith("?region=ZZ&lang=en")
    assert sponsor(pg) in DUMMY_WORLDWIDE | {"Dummy Sponsor (English)"}
    assert pg.inner_text("#ad-target-country") == "no region (ZZ)"

    # A manually chosen country applies immediately (and is remembered on this device only).
    pg.click("#ad-why")
    pg.select_option("#ad-region-mode", "manual")
    pg.select_option("#ad-region", "DE")
    pg.wait_for_timeout(800)
    assert "Germany (chosen by you)" in pg.inner_text("#ad-info-region")
    pg.keyboard.press("Escape")
    pg.wait_for_selector("#ad-body")
    pg.wait_for_timeout(300)
    assert [c for c in calls if "/ads/serve" in c["url"]][-1]["url"].endswith("?region=DE&lang=en")
    assert sponsor(pg) in DUMMY_WORLDWIDE | {
        "Dummy Sponsor (English)",
        "Dummy Sponsor (Europe)",
        "Example Coffee Roasters",  # the English DACH creative, not the German one
        "Example Rail Pass",
    }
    assert pg.inner_text("#ad-target-country") == "Germany (DE)" and "chosen by you" in pg.inner_text("#ad-target")

    # Compact layout: the "Large panel" switch off → a clearly smaller card (targeting row still shown).
    large_height = pg.locator("#ad-panel").bounding_box()["height"]
    pg.click("#ad-why")
    pg.click("#ad-large")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(300)
    assert pg.locator("#ad-panel.large").count() == 0 and pg.locator("#ad-debug").count() == 0
    assert pg.locator("#ad-panel").bounding_box()["height"] < large_height - 40
    assert pg.locator("#ad-target-country").count() == 1
    us.close()


def test_admin_creates_a_campaign_and_reads_anonymous_totals_after_a_real_click(browser, server, releaser):
    # Campaigns are managed in the separate ads admin console, not in the user app.
    from test_ads_admin import console_sign_in

    admin_ctx, ad = console_sign_in(browser, server + "/ads-admin/")
    ad.click("[data-nav='campaigns']")
    ad.wait_for_selector("#campaigns")
    headline = f"E2E offer {int(time.time())}"
    ad.click("#btn-new")
    ad.wait_for_selector("#campaign-form")
    ad.fill("#campaign-form [name=sponsor]", "E2E Sponsor")
    ad.fill("#campaign-form [name=headline]", headline)
    ad.fill("#campaign-form [name=blurb]", "Created by the end-to-end test.")
    ad.fill("#campaign-form [name=target_url]", "https://example.com/e2e?utm_source=test")
    ad.fill("#campaign-form [name=regions]", "gb, IE")
    ad.fill("#campaign-form [name=weight]", "100")
    ad.select_option("#campaign-form [name=status]", "active")
    ad.click("#campaign-save")
    ad.wait_for_selector(f"#campaigns:has-text('{headline}')")
    row = ad.locator("#campaigns tbody tr", has_text=headline)
    assert "live" in row.inner_text() and "GB,IE" in row.inner_text()

    # Served to Ireland (next to the worldwide and EU creatives) …
    with urllib.request.urlopen(server + "/api/v1/ads/serve?region=IE&lang=en") as r:
        served = {a["sponsor"]: a for a in json.load(r)["ads"]}
    assert "E2E Sponsor" in served and served["E2E Sponsor"]["headline"] == headline
    # … and not to Germany.
    with urllib.request.urlopen(server + "/api/v1/ads/serve?region=DE&lang=en") as r:
        assert "E2E Sponsor" not in {a["sponsor"] for a in json.load(r)["ads"]}

    # An anonymous Irish visitor views and clicks it.
    ie = browser.new_context(viewport={"width": 1200, "height": 900}, timezone_id="Europe/Dublin", locale="en-IE")
    pg = ie.new_page()
    calls = watch_ads(pg)
    pg.goto(server + "/")
    pg.wait_for_selector("#ad-body", timeout=10000)
    for _ in range(8):  # weighted rotation: hide other creatives until ours is on screen
        if sponsor(pg) == "E2E Sponsor":
            break
        pg.click("#ad-dismiss")
        pg.wait_for_timeout(300)
    assert sponsor(pg) == "E2E Sponsor" and headline in pg.inner_text("#ad-panel")
    assert wait_for_metrics(pg, calls, "impression")
    with pg.expect_popup() as popup:
        pg.click("#ad-panel .ad-link")
    popup.value.close()
    click = wait_for_metrics(pg, calls, "click")
    assert click and json.loads(click["body"])["events"][0]["region"] == "IE"
    ie.close()

    # The administrator sees the anonymous totals in the console: 1 view, 1 click, by country.
    stats_button = ad.locator("#campaigns tbody tr", has_text=headline).locator("button[data-action=stats]")
    if ad.locator("#campaign-stats").count() == 0:  # saving usually opens the stats already
        stats_button.click()
        ad.wait_for_selector("#campaign-stats")
    for _ in range(20):
        if "Ireland (IE)" in ad.inner_text("#campaign-stats"):
            break
        stats_button.click()  # toggle closed …
        ad.wait_for_timeout(300)
        stats_button.click()  # … and open again = refresh
        ad.wait_for_selector("#campaign-stats")
    stats = ad.inner_text("#campaign-stats")
    assert headline in stats and "Ireland (IE)" in stats and "1 · 1" in stats
    assert "ie@" not in stats and "Dublin" not in stats  # nothing identifying a person or a device

    # Pause → no longer served; archive → gone from the default list, totals kept.
    ad.locator("#campaigns tbody tr", has_text=headline).locator("button[data-action=pause]").click()
    ad.wait_for_selector(f"#campaigns tr:has-text('{headline}') .badge.paused")
    with urllib.request.urlopen(server + "/api/v1/ads/serve?region=IE&lang=en") as r:
        assert "E2E Sponsor" not in {a["sponsor"] for a in json.load(r)["ads"]}
    ad.once("dialog", lambda d: d.accept())
    ad.locator("#campaigns tbody tr", has_text=headline).locator("button[data-action=archive]").click()
    ad.wait_for_timeout(800)
    assert ad.locator("#campaigns tbody tr", has_text=headline).count() == 0
    ad.check("#archived")
    ad.wait_for_selector(f"#campaigns tr:has-text('{headline}') .badge.archived")
    assert "1 · 1" in ad.locator("#campaigns tbody tr", has_text=headline).inner_text()
    admin_ctx.close()
