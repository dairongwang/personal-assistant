"""Membership types — the *strategy* pattern for "what may this user do?".

Four memberships exist; each is one strategy object that answers the same questions (which
features, how many active tasks, which label to show). Callers never branch on plan names —
they ask the strategy:

    strategy = membership.resolve(db, account)        # or ANONYMOUS for no account
    strategy.can_use("sync")                           # feature gate (billing.require_feature)
    strategy.max_active_tasks                          # None = unlimited
    strategy.describe()                                # what the client/UI shows

| Type        | Who                                   | Active tasks | Features                        |
|-------------|---------------------------------------|--------------|---------------------------------|
| anonymous   | no account (local-only)               | 3            | local app only (sponsored panel)|
| free        | account, no paid subscription         | 3            | + devices, trial, upgrade       |
| paid        | Pro (trial/active/grace) or Lifetime  | unlimited    | + sync, vault, push, ad_free …  |
| admin       | role = admin (production support)     | unlimited    | everything paid has + admin API |

The PWA mirrors this table in ``apps/web/src/app/core/membership/membership.ts`` (the client
must work offline and without an account) and takes the server's values as authoritative
whenever it is signed in (``/me`` → ``entitlements.membership`` / ``max_active_tasks``).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

import config
import models
import schemas

MembershipType = str  # "anonymous" | "free" | "paid" | "admin"

# "ad_free": no sponsored-content panel (docs/ADVERTISING.md). Anonymous and free memberships
# see first-party, contextual sponsored content; it is part of what Pro buys.
PAID_FEATURES: tuple[str, ...] = ("sync", "vault", "push", "calendar_feed", "insights_full", "ad_free")


class MembershipStrategy:
    """One membership type (the strategy interface + shared behaviour). Subclasses only
    *configure*; behaviour lives here so every type answers every question the same way."""

    type: MembershipType = "anonymous"
    label: str = ""
    description: str = ""
    requires_account: bool = False
    max_active_tasks: int | None = None  # None = unlimited
    features: tuple[str, ...] = ()
    can_upgrade: bool = False  # shows "Subscribe / Start trial" in the UI

    # ---- behaviour ---------------------------------------------------------------------

    def can_use(self, feature: str) -> bool:
        return feature in self.features

    def can_activate_task(self, active_count: int) -> bool:
        """May a user with `active_count` open tasks open one more? (Enforced on the device —
        the server never sees tasks — but the rule is defined here so both sides agree.)"""
        return self.max_active_tasks is None or active_count < self.max_active_tasks

    def describe(self) -> schemas.MembershipInfo:
        return schemas.MembershipInfo(
            type=self.type,
            label=self.label,
            description=self.description,
            requires_account=self.requires_account,
            max_active_tasks=self.max_active_tasks,
            features=list(self.features),
            can_upgrade=self.can_upgrade,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Membership {self.type}>"


class AnonymousMembership(MembershipStrategy):
    type = "anonymous"
    label = "Free (no account)"
    description = "Everything stays on this device. No account needed."
    can_upgrade = True

    @property
    def max_active_tasks(self) -> int | None:  # type: ignore[override]
        return config.FREE_MAX_ACTIVE_TASKS


class FreeMembership(MembershipStrategy):
    type = "free"
    label = "Free"
    description = "A free account: same limits as without one, ready to start a trial or subscribe."
    requires_account = True
    can_upgrade = True

    @property
    def max_active_tasks(self) -> int | None:  # type: ignore[override]
        return config.FREE_MAX_ACTIVE_TASKS


class PaidMembership(MembershipStrategy):
    type = "paid"
    label = "Pro"
    description = "Full features: unlimited active tasks and end-to-end-encrypted sync between devices."
    requires_account = True
    max_active_tasks = None
    features = PAID_FEATURES


class AdminMembership(MembershipStrategy):
    type = "admin"
    label = "Administrator"
    description = "Production support and management. Everything Pro has, plus the admin panel."
    requires_account = True
    max_active_tasks = None
    features = (*PAID_FEATURES, "admin")


ANONYMOUS = AnonymousMembership()
FREE = FreeMembership()
PAID = PaidMembership()
ADMIN = AdminMembership()

STRATEGIES: dict[MembershipType, MembershipStrategy] = {s.type: s for s in (ANONYMOUS, FREE, PAID, ADMIN)}


def strategy_for(account: models.Account | None, entitled: bool) -> MembershipStrategy:
    """Pick the strategy: role first, then the (already evaluated) subscription entitlement."""
    if account is None:
        return ANONYMOUS
    if account.role == models.ROLE_ADMIN:
        return ADMIN
    return PAID if entitled else FREE


def resolve(db: Session, account: models.Account | None) -> MembershipStrategy:
    """Strategy for an account, evaluating its subscription (trial/grace/lapse rules in billing)."""
    if account is None:
        return ANONYMOUS
    import billing  # local import: billing imports this module

    return strategy_for(account, billing.is_entitled(db, account))


def catalog() -> schemas.MembershipCatalog:
    return schemas.MembershipCatalog(memberships=[s.describe() for s in STRATEGIES.values()])
