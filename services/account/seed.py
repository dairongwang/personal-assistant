"""Fake user accounts for testing — one per membership situation.

    python dev.py seed                  # or:  python seed.py   (inside services/account, venv active)
    python dev.py seed --ads            # only the dummy sponsored-content campaigns (--force in prod)
    SEED_FAKE_ACCOUNTS=true             # the API seeds at startup (dev/test only; `dev.py run` sets it)
    python seed.py --promote me@x.org   # make an existing account an administrator

The accounts are *real*: keys are derived exactly as the PWA does (clientcrypto.py), so you can
sign in with them in the app. Seeding is idempotent — existing accounts keep their data; only
the membership rows are reset to the documented state so the fixtures stay predictable.
Refused in prod (APP_ENV=prod) unless --force is given.

| E-mail                  | Password          | Membership | Billing state                          |
|-------------------------|-------------------|------------|----------------------------------------|
| free@example.com        | Free-pass-123     | free       | no subscription, trial available       |
| trial@example.com       | Trial-pass-123    | paid       | Pro trial, 14 days left                |
| pro@example.com         | Pro-pass-123      | paid       | Pro, active, renews in 30 days         |
| lifetime@example.com    | Lifetime-pass-123 | paid       | Lifetime                               |
| pastdue@example.com     | Pastdue-pass-123  | paid       | Pro, payment failed 2 days ago (grace) |
| expired@example.com     | Expired-pass-123  | free       | Pro canceled — back to Free limits     |
| admin@example.com       | Admin-pass-123    | admin      | role = admin (no subscription needed)  |

Anonymous mode needs no fixture: open the app without signing in.

The paid fixtures (pro@, lifetime@, pastdue@, expired@) come with ledger rows (payments at the
configured prices; pastdue@ has a failed one) and are registered with the built-in fake Stripe
(fakes.py) whenever it is active, so *Manage billing* and *Cancel* (with refund) on the account
screen work for them like for a real purchase; without the fakes their rows are provider "seed"
and lifecycle changes stay local (refunds are recorded, no money moves).

``seed_ads()`` adds **dummy / fictional sponsored-content campaigns** (docs/ADVERTISING.md) with
different regions/languages, so the dashboard at http://localhost:4200/ always shows something:
a worldwide dummy ad for everyone, plus English-, EEA-, APAC-, Americas-, DACH-, EU- and US/CA-
targeted ones. Change the browser language/time zone, or pick a country under “Why this?”, to
watch the targeting change.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

import auth
import clientcrypto
import config
import models


@dataclass(frozen=True)
class FakeAccount:
    email: str
    password: str
    membership: str  # expected membership type (for the table above and the tests)
    plan: str = "free"
    status: str = "none"
    period_days: int | None = None  # current_period_end = now + days (negative = in the past)
    role: str = models.ROLE_USER
    provider_ids: dict[str, str] = field(default_factory=dict)
    # Ledger rows: (payment intent id, price plan, status, days ago paid, period days [start, end] from now)
    payments: tuple[tuple[str, str, str, int, tuple[int, int] | None], ...] = ()


FAKE_ACCOUNTS: tuple[FakeAccount, ...] = (
    FakeAccount("free@example.com", "Free-pass-123", "free"),
    FakeAccount("trial@example.com", "Trial-pass-123", "paid", plan="pro", status="trialing", period_days=14),
    FakeAccount(
        "pro@example.com",
        "Pro-pass-123",
        "paid",
        plan="pro",
        status="active",
        period_days=30,
        provider_ids={"customer": "cus_fake_pro", "subscription": "sub_fake_pro"},
        payments=(("pi_fake_pro_1", "pro_monthly", "succeeded", 0, (0, 30)),),
    ),
    FakeAccount(
        "lifetime@example.com",
        "Lifetime-pass-123",
        "paid",
        plan="lifetime",
        status="active",
        provider_ids={"customer": "cus_fake_lifetime"},
        payments=(("pi_fake_lifetime_1", "lifetime", "succeeded", 5, None),),
    ),
    FakeAccount(
        "pastdue@example.com",
        "Pastdue-pass-123",
        "paid",
        plan="pro",
        status="past_due",
        period_days=-2,
        provider_ids={"customer": "cus_fake_pastdue", "subscription": "sub_fake_pastdue"},
        payments=(
            ("pi_fake_pastdue_1", "pro_monthly", "succeeded", 32, (-32, -2)),
            ("pi_fake_pastdue_2", "pro_monthly", "failed", 2, (-2, 28)),
        ),
    ),
    FakeAccount(
        "expired@example.com",
        "Expired-pass-123",
        "free",
        plan="pro",
        status="canceled",
        period_days=-40,
        provider_ids={"customer": "cus_fake_expired"},
        payments=(("pi_fake_expired_1", "pro_monthly", "succeeded", 70, (-70, -40)),),
    ),
    FakeAccount("admin@example.com", "Admin-pass-123", "admin", role=models.ROLE_ADMIN),
)


def _upsert(db: Session, spec: FakeAccount, iterations: int) -> tuple[models.Account, bool]:
    account = db.execute(select(models.Account).where(models.Account.email == spec.email)).scalar_one_or_none()
    created = account is None
    if account is None:
        auth_key, wrap_key = clientcrypto.derive_keys(spec.password, spec.email, iterations)
        salt = auth.new_salt()
        account = models.Account(
            email=spec.email,
            auth_salt=salt,
            auth_hash=auth.hash_auth_key(auth_key, salt),
            wrapped_data_key=clientcrypto.wrap_data_key(os.urandom(32), wrap_key),
            kdf_params=json.dumps({**clientcrypto.KDF_PARAMS, "iterations": iterations}),
        )
        db.add(account)
        db.flush()
    account.role = spec.role
    sub = db.get(models.Subscription, account.id) or models.Subscription(account_id=account.id, provider="seed")
    sub.plan = spec.plan
    sub.status = spec.status
    sub.current_period_end = (
        datetime.now(timezone.utc) + timedelta(days=spec.period_days) if spec.period_days is not None else None
    )
    sub.cancel_at_period_end = False
    sub.provider_customer_id = spec.provider_ids.get("customer")
    sub.provider_subscription_id = spec.provider_ids.get("subscription")
    sub.provider = _register_with_fake_stripe(account, spec, sub)
    db.add(sub)
    _seed_payments(db, account, spec, sub.provider)
    return account, created


def _register_with_fake_stripe(account: models.Account, spec: FakeAccount, sub: models.Subscription) -> str:
    """Paid fixtures belong to the built-in fake Stripe when it is active, so the account
    screen's Manage / Cancel (with refund) work for them exactly as for a real purchase.
    Otherwise (prod, fakes off) the row is provider "seed": lifecycle changes stay local."""
    import payments

    if not spec.provider_ids or payments.STRIPE.fake is None:
        return "seed"
    fake = payments.STRIPE.fake
    customer_id = sub.provider_customer_id or ""
    if sub.provider_subscription_id:
        fake.adopt(
            account.id,
            spec.email,
            subscription_id=sub.provider_subscription_id,
            customer_id=customer_id,
            plan="pro_monthly",
            status=spec.status,
            period_end=sub.current_period_end,
        )
    else:
        fake.customer_for(spec.email, account.id, customer_id=customer_id)
    for payment_id, plan, status, _days_ago, _period in spec.payments:
        if payment_id not in fake.charges:
            fake.adopt_charge(
                payment_id,
                customer_id=customer_id,
                account_id=account.id,
                plan=plan,
                amount_cents=payments.plan_price_cents(plan),
                subscription_id=sub.provider_subscription_id,
                paid=status == "succeeded",
            )
    return "stripe"


def _seed_payments(db: Session, account: models.Account, spec: FakeAccount, provider: str) -> None:
    """The ledger rows behind the fixture's billing state (idempotent by payment id)."""
    import payments

    now = datetime.now(timezone.utc)
    for payment_id, plan, status, days_ago, period in spec.payments:
        row = db.execute(
            select(models.Payment).where(models.Payment.provider_payment_id == payment_id)
        ).scalar_one_or_none()
        if row is None:
            row = models.Payment(account_id=account.id, provider=provider, provider_payment_id=payment_id)
            db.add(row)
        row.account_id = account.id
        row.provider = provider
        row.plan = plan
        row.amount_cents = payments.plan_price_cents(plan)
        row.currency = config.BILLING_CURRENCY
        row.status = status
        row.refunded_cents = 0
        row.refund_id = None
        row.refunded_at = None
        row.failure_reason = "Your card was declined." if status == "failed" else None
        row.paid_at = now - timedelta(days=days_ago)
        row.period_start = now + timedelta(days=period[0]) if period else None
        row.period_end = now + timedelta(days=period[1]) if period else None


def seed(
    db: Session,
    *,
    iterations: int = clientcrypto.KDF_PARAMS["iterations"],
    log: Callable[..., None] | None = None,
) -> list[tuple[FakeAccount, models.Account, bool]]:
    """Create/refresh every fake account. `iterations` < default only makes sense for server-side
    tests (the PWA always derives with the pinned default, so those accounts cannot sign in there)."""
    out = []
    for spec in FAKE_ACCOUNTS:
        account, created = _upsert(db, spec, iterations)
        out.append((spec, account, created))
    db.commit()
    if log:
        log("Fake accounts ready (%s created, %s refreshed):", sum(c for _, _, c in out), sum(not c for _, _, c in out))
        for spec, _, _ in out:
            log("  %-22s %-18s %s", spec.email, spec.password, spec.membership)
    return out


# ---------------------------------------------------------------------------- sponsored content


@dataclass(frozen=True)
class FakeAd:
    id: str  # fixed UUID → idempotent
    sponsor: str
    headline: str
    blurb: str
    cta_label: str
    target_url: str
    regions: str = ""
    languages: str = ""
    status: str = "active"
    weight: int = 10
    kind: str = "sponsored"  # internal | sponsored | affiliate (ad_sources.py)


FAKE_ADS: tuple[FakeAd, ...] = (
    # --- Internal ads (house promotions, source "internal"): our own messages, no money involved.
    FakeAd(
        "00000000-0000-4000-8000-00000000ad20",
        "Personal Assistant",
        "INTERNAL AD — Go Pro: unlimited tasks, encrypted sync, no ads",
        "A house ad from the makers of this app. The link opens the account screen instead of a website.",
        "See Pro",
        "app:upgrade",
        kind="internal",
        weight=30,
    ),
    FakeAd(
        "00000000-0000-4000-8000-00000000ad21",
        "Personal Assistant",
        "INTERNAL AD — Install the app on your phone",
        "Open this page on your phone and choose “Add to Home Screen”; your data stays on the device.",
        "How to install",
        "https://example.com/personal-assistant/install",
        kind="internal",
        weight=10,
    ),
    # --- Affiliate links (source "affiliate"): product links that earn a commission; ADS_AFFILIATE_TAG
    #     is appended to the URL at serve time and the panel shows the disclosure.
    FakeAd(
        "00000000-0000-4000-8000-00000000ad30",
        "Example Store (affiliate)",
        "AFFILIATE DUMMY — Noise-cancelling headphones for deep work",
        "An affiliate product link. Look at the URL: the affiliate tag is appended when it is served.",
        "View product",
        "https://example.com/shop/headphones",
        kind="affiliate",
        weight=20,
    ),
    FakeAd(
        "00000000-0000-4000-8000-00000000ad31",
        "Example Books (affiliate)",
        "AFFILIATE DUMMY — “Getting Things Done”, paperback",
        "Another affiliate dummy, English-speaking devices only.",
        "View book",
        "https://example.com/books/gtd?ref=existing",
        kind="affiliate",
        languages="en",
        weight=10,
    ),
    # --- Dummy sponsored advertisements: obviously fake, one for every context, so the dashboard
    #     always has something to show (http://localhost:4200/ — README "Seeing the advertisement panel").
    FakeAd(
        "00000000-0000-4000-8000-00000000ad00",
        "Dummy Sponsor (worldwide)",
        "DUMMY AD — shown in every country and language",
        "This is a test advertisement seeded by `python dev.py run`. It proves the panel works; nothing is sold here.",
        "Open dummy link",
        "https://example.com/dummy-worldwide?utm_source=personal-assistant",
        weight=30,
    ),
    FakeAd(
        "00000000-0000-4000-8000-00000000ad10",
        "Dummy Sponsor (English)",
        "DUMMY AD — English-speaking devices only",
        "Targeted by language (en), any country. Switch your browser language to see it disappear.",
        "Open dummy link",
        "https://example.com/dummy-en?utm_source=personal-assistant",
        languages="en",
        weight=20,
    ),
    FakeAd(
        "00000000-0000-4000-8000-00000000ad11",
        "Dummy Sponsor (Europe)",
        "DUMMY AD — European Union + EEA only",
        "Targeted by region group EEA. Pick a country outside Europe under “Why this?” to see it go.",
        "Open dummy link",
        "https://example.com/dummy-eea?utm_source=personal-assistant",
        regions="EEA",
        weight=20,
    ),
    FakeAd(
        "00000000-0000-4000-8000-00000000ad12",
        "Dummy Sponsor (Asia-Pacific)",
        "DUMMY AD — Asia-Pacific only",
        "Targeted by region group APAC (India, Japan, Australia, Singapore, …).",
        "Open dummy link",
        "https://example.com/dummy-apac?utm_source=personal-assistant",
        regions="APAC",
        weight=20,
    ),
    FakeAd(
        "00000000-0000-4000-8000-00000000ad13",
        "Dummy Sponsor (Americas)",
        "DUMMY AD — North and Latin America only",
        "Targeted by region groups NA + LATAM.",
        "Open dummy link",
        "https://example.com/dummy-americas?utm_source=personal-assistant",
        regions="NA, LATAM",
        weight=20,
    ),
    # --- Realistic-looking fictional campaigns with finer targeting.
    FakeAd(
        "00000000-0000-4000-8000-00000000ad01",
        "Example Notebooks",
        "A paper notebook for the thoughts that don't fit in a task",
        "Dot grid, lies flat, made from recycled paper. Ships worldwide.",
        "See the notebook",
        "https://example.com/notebooks?utm_source=personal-assistant",
        weight=10,
    ),
    FakeAd(
        "00000000-0000-4000-8000-00000000ad02",
        "Beispiel Kaffeerösterei",
        "Frisch gerösteter Kaffee für den Wochenplan",
        "Röstung in Hamburg, versandkostenfrei in DE, AT und CH.",
        "Zum Shop",
        "https://example.com/kaffee?utm_source=personal-assistant",
        regions="DACH",
        languages="de",
        weight=20,
    ),
    FakeAd(
        "00000000-0000-4000-8000-00000000ad03",
        "Example Coffee Roasters",
        "Fresh-roasted coffee for the week ahead",
        "Roasted in Hamburg, free shipping across Germany, Austria and Switzerland.",
        "Visit the shop",
        "https://example.com/coffee?utm_source=personal-assistant",
        regions="DACH",
        languages="en",
        weight=20,
    ),
    FakeAd(
        "00000000-0000-4000-8000-00000000ad04",
        "Example Rail Pass",
        "One pass, 33 countries — plan the trip, we handle the trains",
        "Flexible passes for residents of the European Union.",
        "Plan a trip",
        "https://example.com/railpass?utm_source=personal-assistant",
        regions="EU",
        weight=15,
    ),
    FakeAd(
        "00000000-0000-4000-8000-00000000ad05",
        "Example Credit Union",
        "A savings account that rounds up what you get done",
        "Member-owned, no monthly fees. United States and Canada.",
        "Open an account",
        "https://example.com/savings?utm_source=personal-assistant",
        regions="US, CA",
        weight=15,
    ),
    FakeAd(
        "00000000-0000-4000-8000-00000000ad06",
        "Example Standing Desks",
        "Paused campaign — not shown to anyone",
        "Kept here so the admin panel has a paused row to look at.",
        "Learn more",
        "https://example.com/desks",
        regions="GB",
        status="paused",
    ),
)


def seed_ads(db: Session, *, log: Callable[..., None] | None = None) -> list[models.AdCampaign]:
    """Create/refresh the sample campaigns (fixed ids; counters are kept)."""
    out = []
    for spec in FAKE_ADS:
        ad = db.get(models.AdCampaign, spec.id)
        if ad is None:
            ad = models.AdCampaign(id=spec.id)
            db.add(ad)
        ad.sponsor = spec.sponsor
        ad.headline = spec.headline
        ad.blurb = spec.blurb
        ad.cta_label = spec.cta_label
        ad.target_url = spec.target_url
        ad.regions = spec.regions.replace(" ", "")
        ad.languages = spec.languages
        ad.status = spec.status
        ad.weight = spec.weight
        ad.kind = spec.kind
        out.append(ad)
    db.commit()
    if log:
        kinds = {k: sum(1 for a in out if a.kind == k and a.status == "active") for k in models.AD_KINDS}
        log(
            "Sample ad campaigns ready: %s (%s active: %s internal, %s sponsored, %s affiliate)",
            len(out),
            sum(a.status == "active" for a in out),
            kinds["internal"],
            kinds["sponsored"],
            kinds["affiliate"],
        )
    return out


def promote(db: Session, email: str, role: str = models.ROLE_ADMIN) -> models.Account:
    account = db.execute(select(models.Account).where(models.Account.email == email.lower())).scalar_one_or_none()
    if account is None or account.deleted_at:
        raise SystemExit(f"No account with e-mail {email!r}. Register it in the app first.")
    account.role = role
    db.commit()
    return account


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="allow seeding in APP_ENV=prod")
    parser.add_argument("--ads", action="store_true", help="seed only the dummy sponsored-content campaigns")
    parser.add_argument("--promote", metavar="EMAIL", help="give an existing account the administrator role")
    parser.add_argument("--demote", metavar="EMAIL", help="take the administrator role away")
    args = parser.parse_args(argv)

    import database

    database.init_db()
    with database.SessionLocal() as db:
        if args.promote or args.demote:
            email = args.promote or args.demote
            account = promote(db, email, models.ROLE_ADMIN if args.promote else models.ROLE_USER)
            print(f"{account.email}: role = {account.role}")
            return 0
        if config.ENV == "prod" and not args.force:
            what = "dummy campaigns" if args.ads else "fake accounts"
            print(f"Refusing to create {what} in prod (use --force if you really mean it).", file=sys.stderr)
            return 2
        if not args.ads:
            seed(db, log=lambda fmt, *a: print(fmt % a))
        seed_ads(db, log=lambda fmt, *a: print(fmt % a))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
