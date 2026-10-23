"""Paying for Pro end to end, in a real browser, against the built-in Stripe / PayPal fakes
(services/account/fakes.py — on automatically while no provider credentials are configured):

* free account → plan picker with the configured prices → "Pay with Card (Stripe)" → the fake
  checkout page → Pro with the charge in the payment history;
* Cancel subscription → the confirmation quotes the prorated refund → refunded, Free again;
* Manage billing → the fake portal simulating a failed renewal (past due: failure notice, pay
  button + plan picker come back), its recovery, and a refund issued from the dashboard;
* trial account → PayPal with the trial carried over → cancel refunds nothing, trial kept;
* Lifetime → Cancel membership refunds the flat amount; a declined PayPal Lifetime payment is
  reported, then paid; an abandoned checkout changes nothing.
"""

import time

from test_membership import sign_in

PAY_BUTTON = {
    "stripe": "button:has-text('USD')",  # "Pay 1.00 USD" (the decline button has no amount)
    "paypal": "button:has-text('Agree & Subscribe'), button:has-text('Pay now')",
}


def open_account(pg):
    if not pg.evaluate("document.getElementById('account-dialog').open"):
        pg.click("#btn-account")
    pg.wait_for_selector("#account-billing")


def billing_row(pg) -> str:
    open_account(pg)
    pg.wait_for_timeout(300)
    return pg.inner_text("#account-billing")


def history(pg) -> list[str]:
    open_account(pg)
    pg.wait_for_timeout(300)
    if not pg.locator("#payment-history").count():
        return []
    return [" ".join(t.split()) for t in pg.locator("#payment-history li").all_inner_texts()]


def log_out(pg):
    """Close the dialog and log out from the header (the header buttons sit behind the modal)."""
    if pg.evaluate("document.getElementById('account-dialog').open"):
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)
    pg.click("#btn-logout")
    pg.wait_for_selector("#btn-login")


def pay(pg, server, plan: str, method: str, *, expect_fake_text: str, decline: bool = False):
    """Pick a plan, press the method's button, complete (or fail) the fake provider page, come back."""
    open_account(pg)
    pg.select_option("#acc-plan", plan)
    pg.click(f"#checkout button[data-method={method}]")
    pg.wait_for_url(f"**/billing/fake/{method}/**")
    assert "Built-in fake" in pg.content() and expect_fake_text in pg.content()
    if decline:
        pg.click("button:has-text('declined')")
        pg.wait_for_url(server + "/**")
        pg.wait_for_selector("text=Checkout canceled", timeout=15000)
        return
    pg.click(PAY_BUTTON[method])
    pg.wait_for_url(server + "/**")
    pg.wait_for_selector("text=Thanks! Pro is active", timeout=15000)


def portal_press(pg, server, button_text: str, *, expect: str | None = None, simulate: bool = True):
    """Manage billing → the fake provider page → press a button there → back to the app."""
    open_account(pg)
    pg.click("#btn-manage")
    pg.wait_for_url("**/billing/fake/**")
    if simulate:
        for section in pg.locator("summary").all():
            section.click()
    pg.click(f"button:has-text('{button_text}')")
    if expect:
        pg.wait_for_selector(f"text={expect}")
    pg.click("text=← Return to Personal Assistant")
    pg.wait_for_url(server + "/**")
    pg.wait_for_selector("text=Billing details refreshed", timeout=15000)


def cancel(pg, dialogs: list[str]) -> str:
    """Press Cancel, return what the confirmation said (the dialog handler accepts it)."""
    open_account(pg)
    before = len(dialogs)
    pg.click("#btn-cancel")
    for _ in range(100):  # the preview is fetched first, then confirm() pops up
        if len(dialogs) > before:
            break
        pg.wait_for_timeout(100)
    assert len(dialogs) == before + 1, "no confirmation dialog"
    return dialogs[-1]


def test_stripe_pay_cancel_refund_failure_and_recovery(browser, server, releaser):
    email, password = f"buyer{int(time.time())}@example.com", "a fine password"
    ctx = browser.new_context(viewport={"width": 1200, "height": 1100})
    pg = ctx.new_page()
    dialogs: list[str] = []
    pg.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    pg.goto(server + "/")
    sign_in(pg, email, password, existing=False)
    assert billing_row(pg) == "No subscription"
    # Both methods are offered although nothing is configured — the fakes answer.
    assert pg.locator("#checkout button[data-method=stripe]:has-text('(fake)')").count() == 1
    assert pg.locator("#checkout button[data-method=paypal]:has-text('(fake)')").count() == 1
    # The plan picker shows the configured prices (PRICE_* → 1 / 10 / 20) and the refund rule.
    options = pg.locator("#acc-plan option").all_inner_texts()
    assert any("$1.00" in o and "month" in o for o in options)
    assert any("$10.00" in o and "year" in o for o in options)
    assert any("$20.00" in o and "once" in o for o in options)
    pg.select_option("#acc-plan", "lifetime")
    assert "$10.00" in pg.inner_text("#refund-rule") or "10.00 USD" in pg.inner_text("#refund-rule")

    pay(pg, server, "pro_monthly", "stripe", expect_fake_text="Pro Monthly")
    assert billing_row(pg).startswith("Renews ")
    assert pg.is_visible("#btn-cancel") and pg.is_visible("#btn-manage") and not pg.is_visible("#checkout")
    assert history(pg)[0].endswith("Pro — monthly · $1.00 · paid")

    # Cancel → the confirmation quotes the prorated refund (all 30 days unused: $1.00) → Free.
    said = cancel(pg, dialogs)
    assert "$1.00 will be refunded" in said and "unused" in said
    pg.wait_for_selector("text=Subscription ended — $1.00 refunded", timeout=15000)
    assert billing_row(pg) == "Canceled"
    assert history(pg)[0].endswith("refunded $1.00")

    # Subscribe again (yearly) and let the renewal fail: failure notice, pay button + plan picker.
    pay(pg, server, "pro_yearly", "stripe", expect_fake_text="Pro Yearly")
    assert billing_row(pg).startswith("Renews ") and history(pg)[0].endswith("Pro — yearly · $10.00 · paid")
    portal_press(pg, server, "Renewal payment fails (past due)", expect="status: past_due")
    assert billing_row(pg).startswith("Payment failed · Pro until ")
    assert "Your card was declined" in pg.inner_text("#payment-failed")
    assert pg.is_visible("#checkout") and pg.inner_text("#btn-manage").startswith("Update payment method")
    assert history(pg)[0].startswith(time.strftime("%b")) and "failed" in history(pg)[0]
    portal_press(pg, server, "Card updated → open invoice paid", expect="status: active")
    assert billing_row(pg).startswith("Renews ") and not pg.is_visible("#checkout")
    assert "paid" in history(pg)[0] and "failed" not in history(pg)[0]

    # A refund issued from the provider's dashboard ends the membership (webhook-driven).
    portal_press(pg, server, "Refund in full (dashboard)", simulate=False)
    assert billing_row(pg) == "Canceled" and history(pg)[0].endswith("refunded $10.00")
    ctx.close()


def test_trial_paypal_lifetime_and_declines(browser, server, releaser):
    ctx = browser.new_context(viewport={"width": 1200, "height": 1100})
    pg = ctx.new_page()
    dialogs: list[str] = []
    pg.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    pg.goto(server + "/")

    # trial@example.com (seeded): subscribe via PayPal with the trial carried over; cancelling
    # before the first charge refunds nothing and keeps the trial.
    sign_in(pg, "trial@example.com", "Trial-pass-123")
    assert billing_row(pg).startswith("Trial ends ")
    assert "paid billing only starts when the trial ends" in pg.inner_text("#upgrade-callout p")
    pay(pg, server, "pro_yearly", "paypal", expect_fake_text="after your trial")
    assert billing_row(pg).startswith("Trial ends ") and history(pg) == []
    assert "starts when the free trial ends" in pg.inner_text("#subscription-callout p")
    said = cancel(pg, dialogs)
    assert "Nothing is refunded" in said
    pg.wait_for_selector("text=Subscription ended", timeout=15000)
    assert billing_row(pg).startswith("Ends ")  # the free days stay
    log_out(pg)

    # lifetime@example.com (seeded): never expires; Cancel membership refunds the flat $10.
    sign_in(pg, "lifetime@example.com", "Lifetime-pass-123")
    assert billing_row(pg) == "Lifetime — never expires"
    assert pg.inner_text("#btn-cancel") == "Cancel membership" and not pg.is_visible("#checkout")
    assert history(pg)[0].endswith("Lifetime · $20.00 · paid")
    said = cancel(pg, dialogs)
    assert "$10.00 will be refunded" in said
    pg.wait_for_selector("text=Membership ended — $10.00 refunded", timeout=15000)
    assert billing_row(pg) == "Canceled" and history(pg)[0].endswith("paid · $10.00 refunded")
    pg.wait_for_selector("#account-dialog .update-summary strong:has-text('Free')")
    # Buy Lifetime back through PayPal: first with a funding source that is declined …
    pay(pg, server, "lifetime", "paypal", expect_fake_text="Pay now", decline=True)
    assert billing_row(pg) == "Canceled" and "failed" in history(pg)[0]
    # … then for real (a one-off order, captured from the webhook).
    pay(pg, server, "lifetime", "paypal", expect_fake_text="Pay now")
    assert billing_row(pg) == "Lifetime — never expires" and history(pg)[0].endswith("Lifetime · $20.00 · paid")
    log_out(pg)

    # pastdue@example.com (seeded): failure notice, pay button + plan picker; an abandoned checkout.
    sign_in(pg, "pastdue@example.com", "Pastdue-pass-123")
    assert billing_row(pg).startswith("Payment failed · Pro until ")
    assert "Your card was declined" in pg.inner_text("#payment-failed")
    assert pg.is_visible("#checkout") and pg.is_visible("#acc-plan") and pg.is_visible("#btn-manage")
    pg.select_option("#acc-plan", "pro_monthly")
    pg.click("#checkout button[data-method=stripe]")
    pg.wait_for_url("**/billing/fake/stripe/checkout/**")
    pg.click("text=Cancel and go back")
    pg.wait_for_url(server + "/**")
    pg.wait_for_selector("text=Checkout canceled")
    assert billing_row(pg).startswith("Payment failed · Pro until ")
    ctx.close()
