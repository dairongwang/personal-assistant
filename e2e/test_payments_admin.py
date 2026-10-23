"""The Payments section of the admin console (apps/ads-admin → Payments) end to end: purchases,
a cancellation with refund and a declined card made through the user app against the built-in
provider fakes, then the dashboard — KPIs with trend, KPI alerts, revenue chart, status /
method donuts, top customers, by-plan bars, the refunds list — its filters (date range, method,
status), the transaction drill-down with search and paging, and the CSV export."""

import re
import time

from test_ads_admin import console_sign_in
from test_billing import cancel, pay
from test_membership import sign_in


def buy_through_the_app(browser, server, email, plan, method, *, cancel_after=False, decline=False, dialogs=None):
    ctx = browser.new_context(viewport={"width": 1200, "height": 1000})
    pg = ctx.new_page()
    dialogs = dialogs if dialogs is not None else []
    pg.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    pg.goto(server + "/")
    sign_in(pg, email, "a fine password", existing=False)
    pay(pg, server, plan, method, expect_fake_text="Built-in fake", decline=decline)
    if cancel_after:
        cancel(pg, dialogs)
        pg.wait_for_selector("text=refunded", timeout=15000)
    ctx.close()


def cell_texts(pg, selector):
    return [" ".join(t.split()) for t in pg.locator(selector).all_inner_texts()]


def test_payments_dashboard(browser, server, releaser, ads_admin_origin):
    stamp = int(time.time())
    # Real purchases through the user app (fake Stripe / PayPal): a monthly that is canceled and
    # refunded, a yearly by PayPal, a Lifetime, and a declined Lifetime card.
    buy_through_the_app(browser, server, f"pa{stamp}@example.com", "pro_monthly", "stripe", cancel_after=True)
    buy_through_the_app(browser, server, f"pb{stamp}@example.com", "pro_yearly", "paypal")
    buy_through_the_app(browser, server, f"pc{stamp}@example.com", "lifetime", "stripe")
    buy_through_the_app(browser, server, f"pd{stamp}@example.com", "lifetime", "stripe", decline=True)

    ctx, pg = console_sign_in(browser, ads_admin_origin + "/")
    pg.click("a[data-nav=payments]")
    pg.wait_for_selector("#kpi-revenue", timeout=20000)

    # KPIs: the four purchases above plus the seeded ledger (pro@ 1.00, lifetime@ 20.00, pastdue@
    # 1.00 failed + an older 1.00) are in the 30-day window.
    revenue = pg.inner_text("#kpi-revenue")
    assert "Net revenue" in revenue and "vs. previous 30 days" in revenue and "gross $" in revenue
    assert "success rate" in pg.inner_text("#kpi-success") and "refund rate" in pg.inner_text("#kpi-refunds")
    assert re.search(r"\$\d", pg.inner_text("#kpi-average"))
    assert pg.locator("#kpi-alerts .check").count() == 5
    assert pg.locator("#kpi-alerts .check[data-key=past_due]").count() == 1
    assert pg.locator("#revenue-chart circle.dot").count() == 30
    status_legend = " ".join(cell_texts(pg, "#by-status .donut-legend li"))
    assert "Succeeded" in status_legend and "Failed" in status_legend and "refunded" in status_legend.lower()
    method_legend = " ".join(cell_texts(pg, "#by-method .donut-legend li"))
    assert "Card (Stripe)" in method_legend and "PayPal" in method_legend
    top = cell_texts(pg, "#top-customers tbody tr")
    assert any(f"pc{stamp}@example.com" in r and "$20.00" in r for r in top)  # Lifetime buyer on top
    plans = cell_texts(pg, "#by-plan li")
    assert any(p.startswith("Lifetime") for p in plans) and any("Pro — yearly" in p for p in plans)
    refunds = cell_texts(pg, "#refunds tbody tr")
    assert any(f"pa{stamp}@example.com" in r and "$1.00" in r and "full" in r for r in refunds)

    # Filters: method, status, a custom date range.
    pg.select_option("#pay-method", "paypal")
    pg.click("#pay-apply")
    pg.wait_for_function("() => document.querySelector('#by-method .donut-legend')?.innerText.includes('PayPal')")
    assert "Card (Stripe)" not in " ".join(cell_texts(pg, "#by-method .donut-legend li"))
    assert all("paypal" in r for r in cell_texts(pg, "#transactions tbody tr"))
    pg.select_option("#pay-method", "")
    pg.select_option("#pay-status", "failed")
    pg.click("#pay-apply")
    pg.wait_for_function("() => document.querySelector('#kpi-revenue')?.innerText.includes('$0.00')")
    rows = cell_texts(pg, "#transactions tbody tr")
    assert rows and all("Failed" in r for r in rows) and any(f"pd{stamp}@example.com" in r for r in rows)
    assert "Failed 100.0 %" in " ".join(cell_texts(pg, "#by-status .donut-legend li"))
    pg.select_option("#pay-status", "")
    pg.select_option("#pay-preset", "custom")
    pg.fill("#pay-since", "2020-01-01")
    pg.fill("#pay-until", "2020-01-31")
    pg.click("#pay-apply")
    pg.wait_for_function("() => document.querySelector('#pay-window')?.innerText.includes('2020-01-01')")
    assert "31 days" in pg.inner_text("#pay-window") and "No transactions match" in pg.inner_text("#transactions + p")
    assert "0 successful payment(s) between 2020-01-01 and 2020-01-31" in pg.inner_text(
        "#kpi-alerts .check[data-key=no_revenue]"
    )
    pg.select_option("#pay-preset", "7")
    pg.wait_for_function("() => document.querySelector('#pay-window')?.innerText.includes('7 days')")

    # Drill-down: search, open a transaction, the customer's history, paging controls.
    pg.fill("#pay-q", f"pa{stamp}@example.com")
    pg.click("#pay-search-form button")
    pg.wait_for_function("() => document.querySelectorAll('#transactions tbody tr').length === 1")
    pg.click("#transactions tbody tr.tx")
    pg.wait_for_selector("#tx-detail")
    detail = pg.inner_text("#tx-detail")
    assert f"pa{stamp}@example.com" in detail and "Refunded" in detail and "$1.00" in detail
    assert "Lifetime value" in detail and "$0.00" in detail  # paid 1.00, refunded 1.00
    assert pg.locator("#tx-history tbody tr").count() == 1
    pg.click("[data-action=tx-close]")
    assert pg.locator("#tx-detail").count() == 0
    assert pg.is_disabled("[data-action=pay-page] >> nth=0")  # first page: no "previous"

    # CSV export of the filtered window.
    with pg.expect_download() as dl:
        pg.click("#btn-pay-csv")
    text = open(dl.value.path(), encoding="utf-8").read()
    assert text.splitlines()[0].startswith("paid_at,id,email,provider,provider_payment_id,plan,amount")
    assert f"pa{stamp}@example.com" in text
    ctx.close()
