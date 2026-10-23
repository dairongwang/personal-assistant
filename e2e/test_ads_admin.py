"""The separate ads admin console (apps/ads-admin) end to end: administrator sign-in with the
browser-side key derivation, analytics (KPIs, chart, breakdowns with small-count suppression),
campaign management, ad-source switch, the privacy & compliance page, CSV export, sign-out."""

import json
import time
import urllib.request

ADMIN = ("admin@example.com", "Admin-pass-123")


def login(pg, email, password):
    pg.wait_for_selector("#login-form")
    pg.fill("#login-email", email)
    pg.fill("#login-password", password)
    pg.click("#login-submit")


def console_sign_in(browser, url, viewport=(1280, 1000)):
    """A fresh context signed in to the ads admin console as the seeded administrator."""
    ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    pg = ctx.new_page()
    pg.goto(url)
    login(pg, *ADMIN)
    pg.wait_for_selector("#kpi-impressions", timeout=20000)
    return ctx, pg


def generate_traffic(server, region="DE", views=6, clicks=2):
    """Anonymous metrics exactly as the user app sends them (no token)."""
    with urllib.request.urlopen(server + f"/api/v1/ads/serve?region={region}&lang=en") as r:
        ads = json.load(r)["ads"]
    ad_id = ads[0]["id"]
    for _ in range(views):
        body = {"events": [{"ad_id": ad_id, "kind": "impression", "region": region}]}
        req = urllib.request.Request(
            server + "/api/v1/ads/metrics", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req).read()
    for _ in range(clicks):
        body = {"events": [{"ad_id": ad_id, "kind": "click", "region": region}]}
        req = urllib.request.Request(
            server + "/api/v1/ads/metrics", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req).read()
    return ad_id


def test_console_is_separate_from_the_user_app(browser, server, releaser, ads_admin_origin):
    """The separation itself, at browser level: the user app contains no advertising management
    (only a link), and the console runs as its own deployment on its own origin."""
    from test_membership import sign_in

    # 1. The user app, signed in as an administrator: admin dialog has no campaign/source UI,
    #    only the link to the console; nothing ads-admin-ish ships in or loads from the user app.
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    admin_calls = []
    pg.on("request", lambda r: admin_calls.append(r.url) if "/admin/ads" in r.url or "ads-admin" in r.url else None)
    pg.goto(server + "/")
    pg.wait_for_timeout(1200)
    sign_in(pg, *ADMIN)
    pg.keyboard.press("Escape")
    pg.wait_for_selector("#btn-admin")
    pg.click("#btn-admin")
    pg.wait_for_selector("#admin-dialog .admin-stats")
    assert pg.locator("#btn-admin-ads").count() == 0  # the old in-app dialog is gone
    assert pg.locator("#ads-admin-dialog").count() == 0
    assert pg.locator("#ads-overview").count() == 0
    link = pg.locator("#link-ads-admin")
    assert link.count() == 1 and link.get_attribute("target") == "_blank"
    assert link.get_attribute("href") == "/ads-admin/"  # where this deployment hosts the console
    # The user app never called any advertising-management endpoint, nor loaded console files.
    assert admin_calls == [], admin_calls
    ctx.close()

    # 2. The console on its own origin (separate process, separate port — like its own container),
    #    talking to the API via CORS, with none of the user app's assets or service worker.
    ctx2, ad = console_sign_in(browser, ads_admin_origin + "/")
    loaded = []
    ad.on("request", lambda r: loaded.append(r.url))
    assert ad.url.startswith(ads_admin_origin)
    ad.click("[data-nav='privacy']")
    ad.wait_for_selector("#checks")
    assert "all checks pass" in ad.inner_text("#main")
    # Its own origin serves only the console's files; data comes from the API origin.
    assert all(u.startswith((ads_admin_origin, server + "/api/", "data:")) for u in loaded), loaded
    assert not any("main-" in u and u.endswith(".js") for u in loaded)  # no Angular bundle
    assert ad.evaluate("navigator.serviceWorker.getRegistrations().then(r => r.length)") == 0
    # The user app's origin is not involved at all beyond the API path.
    assert not any(u.startswith(server) and "/api/" not in u for u in loaded), loaded
    ctx2.close()

    # 3. A non-admin cannot use the console; the advertising endpoints refuse without admin.
    import urllib.error

    try:
        urllib.request.urlopen(server + "/api/v1/admin/ads/analytics")
        raise AssertionError("analytics must require authentication")
    except urllib.error.HTTPError as e:
        assert e.code == 401


def test_ads_admin_console(browser, server, releaser):
    generate_traffic(server, "DE", views=8, clicks=2)
    generate_traffic(server, "LI", views=1, clicks=0)  # one view from Liechtenstein → must be folded away

    ctx = browser.new_context(viewport={"width": 1280, "height": 1000})
    pg = ctx.new_page()
    requests = []
    pg.on("request", lambda r: requests.append(r.url))
    pg.goto(server + "/ads-admin/")

    # 1. Sign-in: wrong password is refused; a non-admin account is refused; an administrator gets in.
    login(pg, ADMIN[0], "wrong-password")
    pg.wait_for_selector("#login-error")
    assert "incorrect" in pg.inner_text("#login-error").lower()
    login(pg, "free@example.com", "Free-pass-123")
    pg.wait_for_selector("#login-error")
    assert "administrator" in pg.inner_text("#login-error").lower()
    login(pg, *ADMIN)
    pg.wait_for_selector("#kpi-impressions", timeout=20000)
    assert ADMIN[0] in pg.inner_text("#who")
    assert not any("Admin-pass-123" in u for u in requests)  # the password never leaves the browser

    # 2. Overview: KPIs, chart, breakdowns — with the Liechtenstein view folded into "other".
    assert int(pg.inner_text("#kpi-impressions strong").replace(",", "").replace(".", "")) >= 9
    assert int(pg.inner_text("#kpi-clicks strong").replace(",", "")) >= 2
    assert "%" in pg.inner_text("#kpi-ctr strong")
    assert "Sponsored content" in pg.inner_text("#kpi-source")
    assert pg.locator("#chart circle").count() == 30  # 30-day window, one dot per day
    region_text = pg.inner_text("#by-region")
    assert "Germany" in region_text and "Liechtenstein" not in region_text and "Other" in region_text
    assert "Sponsored content" in pg.inner_text("#by-kind")
    assert pg.locator("#perf tbody tr").count() >= 5
    # Window / kind filters re-query.
    pg.select_option("#days", "7")
    pg.wait_for_selector("#chart")
    assert pg.locator("#chart circle").count() == 7
    pg.select_option("#kind", "internal")
    pg.wait_for_selector("#perf")
    assert all("Internal" in t for t in pg.locator("#perf tbody .badge.kind").all_inner_texts())
    pg.select_option("#kind", "")
    pg.wait_for_selector("#perf")

    # 3. CSV export downloads an aggregate file.
    with pg.expect_download() as dl:
        pg.click("#btn-csv")
    path = dl.value.path()
    text = open(path, encoding="utf-8").read()
    assert text.splitlines()[0] == "day,campaign_id,kind,sponsor,headline,region,impressions,clicks,ctr"
    assert "LI" not in {line.split(",")[5] for line in text.splitlines()[1:] if line.count(",") >= 8}

    # 4. Campaigns: create, see it, stats, pause, archive.
    pg.click("[data-nav='campaigns']")
    pg.wait_for_selector("#campaigns")
    before = pg.locator("#campaigns tbody tr").count()
    pg.click("#btn-new")
    pg.wait_for_selector("#campaign-form")
    headline = f"Console campaign {int(time.time())}"
    pg.select_option("#campaign-form [name=kind]", "affiliate")
    pg.fill("#campaign-form [name=sponsor]", "Console Sponsor")
    pg.fill("#campaign-form [name=headline]", headline)
    pg.fill("#campaign-form [name=target_url]", "https://example.com/console")
    pg.fill("#campaign-form [name=regions]", "EU")
    pg.select_option("#campaign-form [name=status]", "active")
    pg.click("#campaign-save")
    pg.wait_for_selector(f"#campaigns:has-text('{headline}')")
    assert pg.locator("#campaigns tbody tr").count() == before + 1
    row = pg.locator("#campaigns tbody tr", has_text=headline)
    assert "live" in row.inner_text() and "Affiliate" in row.inner_text() and "EU" in row.inner_text()
    pg.wait_for_selector("#campaign-stats")  # selected after save
    assert headline in pg.inner_text("#campaign-stats")
    row.locator("button[data-action=pause]").click()
    pg.wait_for_selector(f"#campaigns tr:has-text('{headline}') .badge.paused")
    pg.once("dialog", lambda d: d.accept())
    pg.locator("#campaigns tbody tr", has_text=headline).locator("button[data-action=archive]").click()
    pg.wait_for_timeout(600)
    assert pg.locator("#campaigns tbody tr", has_text=headline).count() == 0
    pg.check("#archived")
    pg.wait_for_selector(f"#campaigns tr:has-text('{headline}') .badge.archived")

    # 5. Ad source switch from the console reaches the user-facing API.
    pg.click("[data-nav='sources']")
    pg.wait_for_selector("#sources")
    assert pg.locator(".source.active[data-kind='sponsored']").count() == 1
    pg.check("#source-network")
    pg.wait_for_selector(".source.active[data-kind='network']")
    with urllib.request.urlopen(server + "/api/v1/ads/serve?region=DE&lang=en") as r:
        assert json.load(r)["source"]["kind"] == "network"
    pg.check("#source-sponsored")
    pg.wait_for_selector(".source.active[data-kind='sponsored']")

    # 6. Privacy & compliance: live checks, inventory, regimes.
    pg.click("[data-nav='privacy']")
    pg.wait_for_selector("#checks")
    statuses = {li.get_attribute("data-key"): li.get_attribute("class") for li in pg.locator("#checks li").all()}
    for key in (
        "no_identifiers",
        "coarse_granularity",
        "retention",
        "no_ip_geolocation",
        "third_party_consent",
        "anonymous_endpoints",
    ):
        assert "ok" in statuses[key], (key, statuses[key])
    assert "all checks pass" in pg.inner_text("#main")
    assert "ad_daily_stats" in pg.inner_text("#inventory")
    codes = {d.get_attribute("data-code") for d in pg.locator("#regimes details").all()}
    assert {"GDPR", "CCPA", "LGPD", "PIPL", "DPDP", "APPI", "DSA"} <= codes
    pg.click("#regimes details[data-code='GDPR'] summary")
    assert "Art. 21(2)" in pg.inner_text("#regimes details[data-code='GDPR']")

    # 7. Audit log shows the console's own actions; sign out clears the session.
    pg.click("[data-nav='audit']")
    pg.wait_for_selector("#audit")
    audit = pg.inner_text("#audit")
    assert "ad.create" in audit and "ad.archive" in audit and "ad.source" in audit and ADMIN[0] in audit
    pg.click("#btn-logout")
    pg.wait_for_selector("#login-form")
    assert pg.evaluate("sessionStorage.getItem('adsadmin.token')") is None
    pg.reload()
    pg.wait_for_selector("#login-form")
    ctx.close()
