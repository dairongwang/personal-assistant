"""Two devices, one account: local-first, API-based sync with the server as master of order,
and the privacy regression test — no plaintext ever leaves a device."""

import time

from conftest import phone_context

SECRET_TITLES = ["Dentist appointment Tuesday", "Salary negotiation notes"]


def titles(pg):
    return sorted(pg.locator("#task-list .task-title").all_inner_texts())


def wait_synced(pg, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        pg.wait_for_timeout(400)
        if "Everything is synced" in (pg.get_attribute("#btn-account", "data-tip") or ""):
            return True
    return False


def refresh(pg):
    pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    wait_synced(pg)
    pg.wait_for_timeout(600)


def test_two_devices_sync_and_nothing_readable_leaves_them(browser, server, releaser):
    email, password = f"ann{int(time.time())}@example.com", "correct horse battery"
    leaks, hosts, endpoints = [], set(), set()

    def watch(page, name):
        def on_request(req):
            hosts.add(req.url.split("/")[2])
            endpoints.add((req.method, req.url.split(hosts.copy().pop())[-1].split("?")[0]))
            payload = (req.post_data or "") + req.url
            for secret in SECRET_TITLES + [password]:
                if secret in payload:
                    leaks.append((name, secret, req.url))

        page.on("request", on_request)

    desktop = browser.new_context(viewport={"width": 1200, "height": 800})
    d = desktop.new_page()
    watch(d, "desktop")
    d.goto(server + "/")
    d.wait_for_timeout(1500)
    for t in SECRET_TITLES:
        d.click("#btn-new-task")
        d.fill("#task-title", t)
        d.click("#task-dialog button[type=submit]")
        d.wait_for_timeout(300)
    assert titles(d) == sorted(SECRET_TITLES)
    # Without an account the only API calls are the release manifest and the anonymous
    # sponsored-content requests (no token, no content — the leak check above covers them too).
    assert ("GET", "/api/v1/release/manifest") in endpoints
    assert {e for e in endpoints if "/api/" in e[1]} <= {
        ("GET", "/api/v1/release/manifest"),
        ("GET", "/api/v1/ads/serve"),
        ("POST", "/api/v1/ads/metrics"),
    }

    # Create the account on the desktop: local data is adopted and uploaded encrypted.
    d.click("#btn-register")
    d.fill("#acc-email", email)
    d.fill("#acc-password", password)
    d.click("#account-dialog button[type=submit]")
    d.wait_for_selector("text=Start free trial", timeout=15000)
    d.click("text=Start free trial")
    d.wait_for_timeout(1000)
    d.keyboard.press("Escape")
    assert wait_synced(d)

    # Sign in on the phone: the local cache is rebuilt from the server's encrypted copy.
    phone = phone_context(browser)
    ph = phone.new_page()
    watch(ph, "phone")
    ph.goto(server + "/")
    ph.wait_for_timeout(1500)
    ph.tap("#btn-login")
    ph.fill("#acc-email", email)
    ph.fill("#acc-password", password)
    ph.tap("#account-dialog button[type=submit]")
    ph.wait_for_timeout(3000)
    ph.keyboard.press("Escape")
    assert wait_synced(ph)
    assert titles(ph) == sorted(SECRET_TITLES)

    # Phone → desktop and desktop → phone.
    ph.tap("#fab-new-task")
    ph.fill("#task-title", "Buy milk (from phone)")
    ph.tap("#task-dialog button[type=submit]")
    wait_synced(ph)
    refresh(d)
    assert "Buy milk (from phone)" in titles(d)
    d.click("#task-list li:has-text('Dentist') .check")
    wait_synced(d)
    refresh(ph)
    assert (
        ph.evaluate("document.querySelector('#task-list li.done .task-title')?.textContent")
        == "Dentist appointment Tuesday"
    )

    # Server is master: the phone can throw its cache away and rebuild it.
    ph.tap("#btn-account")
    ph.once("dialog", lambda dlg: dlg.accept())
    ph.tap("text=Reset from server")
    ph.wait_for_timeout(3000)
    ph.keyboard.press("Escape")
    assert titles(ph) == sorted(SECRET_TITLES + ["Buy milk (from phone)"])

    # Privacy regression.
    assert hosts == {server.split("//")[1]}, hosts
    assert leaks == [], leaks
    desktop.close()
    phone.close()


def test_signing_in_with_local_data_into_an_existing_account_keeps_both(browser, server, releaser):
    """Device A syncs; device B used the app offline first, then signs in: B's local tasks are
    adopted and A's tasks arrive — nothing is lost on either side."""
    email, password = f"bob{int(time.time())}@example.com", "another good password"
    a_ctx = browser.new_context()
    a = a_ctx.new_page()
    a.goto(server + "/")
    a.wait_for_timeout(1200)
    a.click("#btn-new-task")
    a.fill("#task-title", "From device A")
    a.click("#task-dialog button[type=submit]")
    a.click("#btn-register")
    a.fill("#acc-email", email)
    a.fill("#acc-password", password)
    a.click("#account-dialog button[type=submit]")
    a.wait_for_selector("text=Start free trial", timeout=15000)
    a.click("text=Start free trial")
    a.wait_for_timeout(800)
    a.keyboard.press("Escape")
    assert wait_synced(a)

    b_ctx = browser.new_context()
    b = b_ctx.new_page()
    b.goto(server + "/")
    b.wait_for_timeout(1200)
    b.click("#btn-new-task")
    b.fill("#task-title", "Made on device B before signing in")
    b.click("#task-dialog button[type=submit]")
    b.click("#btn-login")
    b.fill("#acc-email", email)
    b.fill("#acc-password", password)
    b.click("#account-dialog button[type=submit]")
    b.wait_for_timeout(3000)
    b.keyboard.press("Escape")
    assert wait_synced(b)
    assert titles(b) == ["From device A", "Made on device B before signing in"]
    refresh(a)
    assert titles(a) == ["From device A", "Made on device B before signing in"]
    a_ctx.close()
    b_ctx.close()
