"""Membership types end to end: anonymous (3 active tasks) → free account (still 3) → Pro
(unlimited) — and the administrator account managing memberships from the admin panel."""

import time

from conftest import phone_context
from test_sync_privacy import wait_synced


def badge(pg) -> str:
    return " ".join(pg.inner_text("#btn-membership").split())


def add_task(pg, title: str, button="#btn-new-task") -> bool:
    """Try to add a task; returns False (and closes the dialog) when the membership blocks it."""
    pg.click(button)
    pg.wait_for_timeout(200)
    if pg.locator("#task-limit-notice").count():
        assert pg.is_disabled("#task-dialog button[type=submit]")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)
        return False
    pg.fill("#task-title", title)
    pg.click("#task-dialog button[type=submit]")
    pg.wait_for_timeout(300)
    return True


def sign_in(pg, email, password, existing=True):
    """Header buttons: Log in / Register are shown while anonymous; Sync / Log out once signed in."""
    assert pg.locator("#btn-logout").count() == 0
    pg.click("#btn-login" if existing else "#btn-register")
    assert pg.inner_text("#account-dialog-title") == ("Sign in" if existing else "Create account")
    pg.fill("#acc-email", email)
    pg.fill("#acc-password", password)
    pg.click("#account-dialog button[type=submit]")
    pg.wait_for_selector("#account-dialog-title:has-text('Account & sync')", timeout=20000)
    pg.wait_for_selector("#btn-logout")
    assert pg.locator("#btn-register").count() == 0 and pg.locator("#btn-login").count() == 0


def test_free_limit_then_pro_unlimited(browser, server, releaser):
    email, password = f"carol{int(time.time())}@example.com", "a fine password"
    ctx = browser.new_context(viewport={"width": 1200, "height": 900})
    pg = ctx.new_page()
    api_calls = set()
    pg.on("request", lambda r: api_calls.add(r.url.split("/api/v1/")[-1]) if "/api/v1/" in r.url else None)
    pg.goto(server + "/")
    pg.wait_for_timeout(1500)

    # 1. Anonymous: no account, everything local, three active tasks at most.
    assert badge(pg) == "◇ Free · 0/3"
    for n in range(1, 4):
        assert add_task(pg, f"Anonymous task {n}")
    assert badge(pg) == "◇ Free · 3/3"
    assert pg.inner_text("#task-usage") == "3 of 3 active tasks"
    assert not add_task(pg, "One too many")
    assert pg.locator("#task-list .task").count() == 3
    assert {c.split("?")[0] for c in api_calls} == {"release/manifest"}  # nothing else left the device
    # Finishing a task frees a slot; re-opening it at the limit is refused.
    pg.click("#task-list li:has-text('Anonymous task 1') .check")
    pg.wait_for_timeout(300)
    assert badge(pg) == "◇ Free · 2/3"
    assert add_task(pg, "Anonymous task 4")
    pg.click("#task-list li.done .check")
    pg.wait_for_timeout(400)
    assert "up to 3 active tasks" in pg.inner_text("#toast")
    assert pg.locator("#task-list li.done").count() == 1
    # The upgrade path from the task dialog leads to the account screen.
    pg.click("#btn-new-task")
    pg.click("#btn-task-upgrade")
    assert pg.evaluate("document.getElementById('account-dialog').open")
    assert "Up to 3 active tasks" in pg.inner_text("#membership-table")
    pg.keyboard.press("Escape")

    # 2. Free account: same limit — the account alone does not buy more.
    sign_in(pg, email, password, existing=False)
    assert pg.inner_text("#account-task-usage") == "3 of 3 active tasks"  # local tasks count
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(500)
    assert badge(pg) == "◇ Free · 3/3"
    assert not add_task(pg, "Still too many")

    # 3. Paid membership (trial): unlimited active tasks, sync on.
    pg.click("#btn-account")
    pg.click("text=Start free trial")
    pg.wait_for_timeout(1000)
    pg.keyboard.press("Escape")
    assert wait_synced(pg)
    assert badge(pg) == "◆ Pro"
    for n in range(5, 10):
        assert add_task(pg, f"Pro task {n}")
    assert pg.inner_text("#task-usage") == "8 active tasks"
    assert pg.locator("#task-list .task").count() == 9
    assert pg.locator("#btn-admin").count() == 0  # paid ≠ administrator

    # A second device of the same account gets the same membership (and the tasks).
    phone = phone_context(browser)
    ph = phone.new_page()
    ph.goto(server + "/")
    ph.wait_for_timeout(1500)
    assert badge(ph) == "◇"  # phone: label hidden, anonymous until signed in
    sign_in(ph, email, password)
    ph.keyboard.press("Escape")
    assert wait_synced(ph)
    assert ph.get_attribute("#btn-membership", "data-membership") == "paid"
    assert ph.locator("#task-list .task").count() == 9
    # Log out from the header: Log in / Register return, data stays, membership is anonymous again.
    ph.tap("#btn-logout")
    ph.wait_for_selector("#btn-register")
    assert ph.locator("#btn-logout").count() == 0
    assert ph.locator("#task-list .task").count() == 9
    assert ph.get_attribute("#btn-membership", "data-membership") == "anonymous"
    phone.close()
    ctx.close()


def test_administrator_manages_memberships(browser, server, releaser):
    email, password = f"dave{int(time.time())}@example.com", "a fine password"
    user_ctx = browser.new_context(viewport={"width": 1200, "height": 900})
    user = user_ctx.new_page()
    user.goto(server + "/")
    user.wait_for_timeout(1500)
    sign_in(user, email, password, existing=False)
    user.keyboard.press("Escape")
    user.wait_for_timeout(300)
    assert badge(user) == "◇ Free · 0/3"
    assert user.locator("#btn-admin").count() == 0

    admin_ctx = browser.new_context(viewport={"width": 1200, "height": 1000})
    admin = admin_ctx.new_page()
    admin.on("dialog", lambda d: d.accept("beta tester") if d.type == "prompt" else d.accept())
    admin.goto(server + "/")
    admin.wait_for_timeout(1500)
    sign_in(admin, "admin@example.com", "Admin-pass-123")  # seeded fake account
    admin.keyboard.press("Escape")
    admin.wait_for_timeout(300)
    assert badge(admin) == "★ Admin"
    assert add_task(admin, "Admin task")  # administrators have every Pro feature
    admin.click("#btn-admin")
    admin.wait_for_selector("#admin-dialog .admin-stats")
    admin.fill("#admin-search", email)
    admin.press("#admin-search", "Enter")
    admin.wait_for_selector(f"#admin-accounts .admin-email:has-text('{email}')")
    assert admin.locator("#admin-accounts > li").count() == 1
    admin.click("#admin-accounts .admin-row")
    admin.wait_for_selector("#admin-accounts .admin-detail")
    assert "Free (user)" in admin.inner_text("#admin-accounts .admin-detail")
    admin.click("text=Grant Pro 30 d")
    admin.wait_for_selector("#admin-accounts .admin-detail:has-text('Pro (user)')")
    assert "unlimited" in admin.inner_text("#admin-accounts .admin-detail")
    admin.click("#admin-accounts details summary")
    assert "beta tester" in admin.inner_text("#admin-accounts details")

    # The user sees the upgrade the next time the app looks at the account.
    user.click("#btn-account")
    user.wait_for_selector("#account-dialog .update-summary strong:has-text('Pro')", timeout=10000)
    user.keyboard.press("Escape")
    user.wait_for_timeout(300)
    assert badge(user) == "◆ Pro"
    for n in range(1, 8):
        assert add_task(user, f"Task {n}")
    assert user.inner_text("#task-usage") == "7 active tasks"

    # …and the downgrade: existing tasks stay, no new active ones until below the limit.
    admin.click("text=Set free")
    admin.wait_for_selector("#admin-accounts .admin-detail:has-text('Free (user)')")
    user.click("#btn-account")
    user.wait_for_selector("#account-dialog .update-summary strong:has-text('Free')", timeout=10000)
    user.keyboard.press("Escape")
    user.wait_for_timeout(300)
    assert badge(user) == "◇ Free · 7/3"
    assert user.locator("#task-list .task").count() == 7
    assert not add_task(user, "Blocked again")

    # Support actions: sign out everywhere — the user's device must sign in again.
    admin.click("text=Sign out everywhere")
    admin.wait_for_selector("#admin-accounts .admin-meta:has-text('0 devices')")
    user.click("#btn-account")
    user.wait_for_selector("#btn-register", timeout=15000)  # session gone → Log in / Register are back
    user.keyboard.press("Escape")
    assert user.locator("#task-list .task").count() == 7  # local data untouched
    # A normal user cannot reach the admin API at all.
    status = user.evaluate(
        "fetch('/api/v1/admin/stats', {headers: {Authorization: 'Bearer ' + "
        "(localStorage.getItem('pa.token') || 'x')}}).then(r => r.status)"
    )
    assert status in (401, 403)
    admin_ctx.close()
    user_ctx.close()
