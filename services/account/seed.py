"""Fake user accounts for testing — one per membership situation.

    python dev.py seed                  # or:  python seed.py   (inside services/account, venv active)
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
    ),
    FakeAccount("lifetime@example.com", "Lifetime-pass-123", "paid", plan="lifetime", status="active"),
    FakeAccount("pastdue@example.com", "Pastdue-pass-123", "paid", plan="pro", status="past_due", period_days=-2),
    FakeAccount("expired@example.com", "Expired-pass-123", "free", plan="pro", status="canceled", period_days=-40),
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
    sub = db.get(models.Subscription, account.id) or models.Subscription(account_id=account.id, provider="stub")
    sub.provider = "stub" if spec.plan == "free" else "seed"
    sub.plan = spec.plan
    sub.status = spec.status
    sub.current_period_end = (
        datetime.now(timezone.utc) + timedelta(days=spec.period_days) if spec.period_days is not None else None
    )
    sub.cancel_at_period_end = False
    sub.provider_customer_id = spec.provider_ids.get("customer")
    sub.provider_subscription_id = spec.provider_ids.get("subscription")
    db.add(sub)
    return account, created


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
            print("Refusing to create fake accounts in prod (use --force if you really mean it).", file=sys.stderr)
            return 2
        seed(db, log=lambda fmt, *a: print(fmt % a))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
