"""Billing: provider adapters + the entitlement model (PLATFORM_DESIGN.md §1).

Only the minimum billing data lives here (plan, status, period, provider ids). Card data,
addresses and tax ids stay with the provider (Merchant of Record). The ``StubProvider`` is a
built-in sandbox so the whole flow (checkout → webhook → entitlements) works with no external
dependency; ``PaddleProvider``/``StripeProvider`` implement the same two methods.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

import config
import membership
import models
import schemas

PAID_PLANS = ("pro", "lifetime")
ENTITLED_STATUSES = ("trialing", "active", "past_due")  # past_due = grace period (dunning)


# ---------------------------------------------------------------------------- providers


class BillingProvider:
    name = "base"

    def create_checkout(self, account: models.Account, plan: str, return_url: str) -> str:
        raise NotImplementedError

    def verify_signature(self, body: bytes, signature: str | None) -> bool:
        raise NotImplementedError


class StubProvider(BillingProvider):
    """Sandbox: the checkout URL is a page on this service that 'pays' and posts the webhook."""

    name = "stub"

    def create_checkout(self, account: models.Account, plan: str, return_url: str) -> str:
        from urllib.parse import urlencode

        return "/api/v1/billing/sandbox?" + urlencode(
            {"account_id": account.id, "plan": plan, "return_url": return_url}
        )

    def verify_signature(self, body: bytes, signature: str | None) -> bool:
        expected = hmac.new(config.BILLING_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return bool(signature) and hmac.compare_digest(expected, signature)

    @staticmethod
    def sign(body: bytes) -> str:
        return hmac.new(config.BILLING_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def provider() -> BillingProvider:
    if config.BILLING_PROVIDER == "stub":
        return StubProvider()
    raise HTTPException(status_code=501, detail=f"Billing provider '{config.BILLING_PROVIDER}' is not configured.")


# ---------------------------------------------------------------------------- entitlements


def subscription_for(db: Session, account: models.Account) -> models.Subscription:
    sub = db.get(models.Subscription, account.id)
    if sub is None:
        sub = models.Subscription(account_id=account.id, provider=config.BILLING_PROVIDER, plan="free", status="none")
        db.add(sub)
        db.flush()
    return sub


def subscription_entitles(sub: models.Subscription, now: datetime | None = None) -> bool:
    """Does this subscription row currently pay for a paid membership? (trial, active, dunning
    grace, offline grace after the period end — the billing state machine, nothing else)."""
    now = now or datetime.now(timezone.utc)
    entitled = sub.status in ENTITLED_STATUSES and sub.plan in PAID_PLANS
    if (
        sub.status == "past_due"
        and sub.current_period_end
        and now > sub.current_period_end + timedelta(days=config.GRACE_DAYS)
    ):
        entitled = False
    if (
        sub.status in ("trialing", "active")
        and sub.plan != "lifetime"
        and sub.current_period_end
        and now > sub.current_period_end + timedelta(days=config.GRACE_DAYS)
    ):
        entitled = False  # period lapsed without a webhook (offline grace applies)
    return entitled


def is_entitled(db: Session, account: models.Account) -> bool:
    return subscription_entitles(subscription_for(db, account))


def entitlements(db: Session, account: models.Account) -> schemas.Entitlements:
    """Billing state + the membership strategy it resolves to. Features, limits and labels come
    from the strategy (membership.py) — nothing here knows what a plan is *allowed* to do."""
    sub = subscription_for(db, account)
    entitled = subscription_entitles(sub)
    strategy = membership.strategy_for(account, entitled)
    return schemas.Entitlements(
        plan=sub.plan if entitled else "free",
        status=sub.status,
        features=list(strategy.features),
        current_period_end=sub.current_period_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        trial_available=sub.status == "none",
        membership=strategy.type,
        membership_label=strategy.label,
        max_active_tasks=strategy.max_active_tasks,
    )


def require_feature(db: Session, account: models.Account, feature: str) -> None:
    if not membership.resolve(db, account).can_use(feature):
        raise HTTPException(
            status_code=402, detail=f"'{feature}' needs a Pro plan. Start a free trial from the account screen."
        )


def start_trial(db: Session, account: models.Account) -> models.Subscription:
    sub = subscription_for(db, account)
    if sub.status != "none":
        raise HTTPException(status_code=409, detail="A trial was already used on this account.")
    sub.plan, sub.status = "pro", "trialing"
    sub.current_period_end = datetime.now(timezone.utc) + timedelta(days=config.TRIAL_DAYS)
    db.commit()
    return sub


def apply_webhook(db: Session, event: schemas.StubWebhook, provider_name: str) -> bool:
    """Idempotent upsert of the subscription row. Returns False if the event was seen before."""
    seen = (
        db.query(models.BillingEvent).filter_by(provider=provider_name, provider_event_id=event.event_id).one_or_none()
    )
    if seen:
        return False
    db.add(models.BillingEvent(provider=provider_name, provider_event_id=event.event_id, type=event.type))
    account = db.get(models.Account, event.account_id)
    if account is None:
        db.commit()  # record it anyway (audit) and move on
        return True
    sub = subscription_for(db, account)
    sub.provider = provider_name
    sub.provider_customer_id = event.customer_id or sub.provider_customer_id
    sub.provider_subscription_id = event.subscription_id or sub.provider_subscription_id
    if event.type in ("subscription.created", "subscription.updated", "transaction.completed"):
        sub.plan = event.plan
        sub.status = event.status
        sub.current_period_end = event.current_period_end
        sub.cancel_at_period_end = event.cancel_at_period_end
    elif event.type == "subscription.canceled":
        sub.status = "canceled"
        sub.cancel_at_period_end = False
    elif event.type == "payment_failed":
        sub.status = "past_due"
    db.query(models.BillingEvent).filter_by(provider=provider_name, provider_event_id=event.event_id).update(
        {"processed_at": models.utcnow()}
    )
    db.commit()
    return True


def webhook_body_for_sandbox(account_id: str, plan: str, event_id: str) -> bytes:
    """What the sandbox 'payment page' posts back — mimics a real provider's payload."""
    period = (
        None if plan == "lifetime" else datetime.now(timezone.utc) + timedelta(days=365 if plan == "pro_yearly" else 30)
    )
    payload = {
        "event_id": event_id,
        "type": "subscription.created",
        "account_id": account_id,
        "plan": "lifetime" if plan == "lifetime" else "pro",
        "status": "active",
        "current_period_end": period.isoformat() if period else None,
        "cancel_at_period_end": False,
        "customer_id": f"cus_stub_{account_id[:8]}",
        "subscription_id": f"sub_stub_{event_id[:8]}",
    }
    return json.dumps(payload).encode("utf-8")
