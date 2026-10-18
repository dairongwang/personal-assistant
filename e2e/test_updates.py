"""Application-code updates through the service worker: automatic, manual, forced, on reopen."""

import time


def version_on_page(pg) -> str:
    return pg.inner_text(".footer").split("·")[0].strip()


def returned_to_tab(pg):
    """The user comes back to the tab — the app re-checks for updates (visibilitychange)."""
    pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def wait_for_version(pg, version, seconds=45) -> str:
    """Poll the footer version; nudge the tab every few seconds like a returning user would."""
    deadline = time.time() + seconds
    last_nudge = time.time()
    while time.time() < deadline:
        pg.wait_for_timeout(400)
        try:
            if version_on_page(pg) == version:
                return version
            if time.time() - last_nudge > 6:
                returned_to_tab(pg)
                last_nudge = time.time()
        except Exception:  # noqa: BLE001 — page is reloading mid-poll
            pass
    return version_on_page(pg)


def test_new_version_installs_automatically_while_open(browser, server, releaser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(server + "/")
    pg.wait_for_timeout(1500)
    assert pg.evaluate("!!navigator.serviceWorker.controller")
    assert version_on_page(pg) == "v1.0.0"
    pg.click("#btn-new-task")
    pg.fill("#task-title", "Survives the update")
    pg.click("#task-dialog button[type=submit]")

    releaser.publish("1.0.1", notes="auto while open")
    returned_to_tab(pg)
    assert wait_for_version(pg, "v1.0.1") == "v1.0.1"
    pg.wait_for_selector("#task-list li", timeout=10000)  # local data untouched by the code update
    assert pg.locator("#task-list li").count() == 1
    ctx.close()


def test_manual_mode_shows_banner_and_waits_for_the_user(browser, server, releaser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(server + "/")
    pg.wait_for_timeout(1500)
    assert pg.is_checked("#auto-update")  # default: automatic updates on
    pg.uncheck("#auto-update")  # the checkbox on the dashboard header
    pg.click("#btn-update")
    assert not pg.is_checked("#update-auto")  # the dialog's checkbox mirrors it
    pg.keyboard.press("Escape")
    before = version_on_page(pg)

    releaser.publish("1.0.2", notes="manual")
    returned_to_tab(pg)
    pg.wait_for_selector("#banner-update", timeout=25000)
    pg.wait_for_timeout(1000)
    assert version_on_page(pg) == before  # nothing reloaded on its own
    pg.hover("#btn-update")
    pg.wait_for_timeout(500)
    assert "1.0.2" in pg.inner_text("#tooltip") and "Tap to install" in pg.inner_text("#tooltip")
    pg.click("#btn-reload")
    assert wait_for_version(pg, "v1.0.2") == "v1.0.2"
    ctx.close()


def test_critical_release_is_forced_even_with_auto_off_and_a_dialog_open(browser, server, releaser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(server + "/")
    pg.wait_for_timeout(1500)
    pg.uncheck("#auto-update")
    pg.click("#btn-new-task")  # user is in the middle of something

    releaser.publish("1.0.3", critical=True, notes="security fix")
    returned_to_tab(pg)
    pg.wait_for_selector("dialog.forced-dialog[open]", timeout=25000)
    assert "required update" in pg.inner_text("dialog.forced-dialog")
    pg.keyboard.press("Escape")  # cannot be dismissed
    assert pg.is_visible("dialog.forced-dialog[open]")
    assert wait_for_version(pg, "v1.0.3") == "v1.0.3"
    assert not pg.is_visible("dialog.forced-dialog[open]")
    ctx.close()


def test_reopening_the_app_picks_up_a_release_published_meanwhile(browser, server, releaser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(server + "/")
    pg.wait_for_timeout(1500)
    pg.close()

    releaser.publish("1.0.4", notes="published while closed")
    pg = ctx.new_page()
    pg.goto(server + "/")
    first_paint = version_on_page(pg)  # served instantly from the service worker cache
    assert wait_for_version(pg, "v1.0.4") == "v1.0.4"
    assert first_paint in ("v1.0.3", "v1.0.4")
    ctx.close()


def test_dropping_support_for_old_versions_forces_them_to_update(browser, server, releaser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(server + "/")
    pg.wait_for_timeout(1500)
    releaser.publish("1.0.5", min_supported="1.0.5", notes="old versions retired")
    returned_to_tab(pg)
    pg.wait_for_selector("dialog.forced-dialog[open]", timeout=25000)
    assert "no longer supported" in pg.inner_text("dialog.forced-dialog")
    assert wait_for_version(pg, "v1.0.5") == "v1.0.5"
    ctx.close()


def test_manifest_published_before_the_build_is_live_keeps_downloading(browser, server, releaser):
    """CDN propagation lag: the manifest says 1.0.6 but the build is not there yet → keeps polling, no crash."""
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(server + "/")
    pg.wait_for_timeout(1500)
    releaser.publish("1.0.6", critical=True, notes="manifest first", build=False)
    returned_to_tab(pg)
    pg.wait_for_selector("dialog.forced-dialog[open]", timeout=25000)
    assert "Downloading" in pg.inner_text("dialog.forced-dialog")
    releaser.publish("1.0.6", critical=True, notes="now the build is live")
    returned_to_tab(pg)
    assert wait_for_version(pg, "v1.0.6") == "v1.0.6"
    ctx.close()
