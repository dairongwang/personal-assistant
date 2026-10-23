"""Billing: the entitlement model (PLATFORM_DESIGN.md §1) and the payments ledger on top of
the payment strategies.

Only the minimum billing data lives here (plan, status, period, provider ids; one ledger row
per charge with what became of it). Card data, addresses and tax ids stay with the provider.
*How* an account pays is a strategy in ``payments.py`` (Stripe · PayPal); every strategy turns
its webhook into the same ``schemas.PaymentEvent``, and ``apply_webhook`` below is the single
place that updates the subscription row and the ledger — it never branches on a provider name.

Cancellation (``cancel``) ends the membership **now** and refunds: a subscription gives back the
unused share of the period it paid for (days left ÷ days in the period × amount), Lifetime
gives back the flat ``REFUND_LIFETIME``. ``refund_preview`` is the same arithmetic without side
effects (the account screen shows it before asking for confirmation).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

import config
import membership
import models
import payments
import schemas

PAID_PLANS = ("pro", "lifetime")
ENTITLED_STATUSES = ("trialing", "active", "past_due")  # past_due = grace period (dunning)

# Stripe accepts a deferred first charge only if it is at least this far away; below that a
# subscription simply starts now (the buyer loses at most two trial days).
TRIAL_CARRY_OVER_MIN = timedelta(hours=48)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    grace, offline grace after the period end, a canceled period that was paid for — the
    billing state machine, nothing else)."""
    now = now or _now()
    entitled = sub.status in ENTITLED_STATUSES and sub.plan in PAID_PLANS
    if sub.status == "canceled" and sub.plan == "pro" and sub.current_period_end and now < sub.current_period_end:
        return True  # the provider ended the renewal; what was paid for stays usable until the period ends
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


def access_until(sub: models.Subscription) -> datetime | None:
    """Until when the paid membership lasts as things stand (what the account screen shows)."""
    if sub.plan == "lifetime" or not sub.current_period_end:
        return None
    if sub.status == "past_due":
        return sub.current_period_end + timedelta(days=config.GRACE_DAYS)
    if sub.status in ("trialing", "active", "canceled"):
        return sub.current_period_end
    return None


def can_cancel(sub: models.Subscription) -> bool:
    """Is there a paid membership to end (and refund)? A bare trial just runs out; a trial
    that already has a subscription behind it (trial carried over) can be canceled."""
    if not subscription_entitles(sub) or sub.status == "canceled":
        return False
    if sub.plan == "lifetime":
        return True
    return sub.status in ("active", "past_due") or (sub.status == "trialing" and bool(sub.provider_subscription_id))


def entitlements(db: Session, account: models.Account) -> schemas.Entitlements:
    """Billing state + the membership strategy it resolves to. Features, limits and labels come
    from the strategy (membership.py) — nothing here knows what a plan is *allowed* to do."""
    sub = subscription_for(db, account)
    entitled = subscription_entitles(sub)
    strategy = membership.strategy_for(account, entitled)
    method = payments.for_subscription(sub)
    pending_cancel = sub.cancel_at_period_end or (sub.status == "canceled" and entitled)
    last = latest_payment(db, account)
    return schemas.Entitlements(
        plan=sub.plan if entitled else "free",
        status=sub.status,
        features=list(strategy.features),
        current_period_end=sub.current_period_end,
        cancel_at_period_end=pending_cancel,
        trial_available=sub.status == "none",
        provider=sub.provider,
        access_until=access_until(sub) if entitled else None,
        renews=entitled and sub.plan == "pro" and sub.status == "active" and not pending_cancel,
        can_cancel=can_cancel(sub),
        can_manage=method is not None and method.can_manage(sub),
        last_payment=payment_out(last) if last else None,
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
    sub.current_period_end = _now() + timedelta(days=config.TRIAL_DAYS)
    db.commit()
    return sub


def trial_carry_over(sub: models.Subscription, now: datetime | None = None) -> datetime | None:
    """When a trial account subscribes, the date paid billing should start (the trial's end) —
    None when not on a trial or when it ends too soon for the providers to defer."""
    now = now or _now()
    if sub.status != "trialing" or sub.plan != "pro" or not sub.current_period_end:
        return None
    return sub.current_period_end if sub.current_period_end - now >= TRIAL_CARRY_OVER_MIN else None


# ---------------------------------------------------------------------------- the ledger


def payment_out(p: models.Payment) -> schemas.PaymentOut:
    return schemas.PaymentOut(
        id=p.id,
        provider=p.provider,
        plan=p.plan,
        amount=payments.money(p.amount_cents),
        currency=p.currency,
        status=p.status,
        refunded=payments.money(p.refunded_cents),
        refund_id=p.refund_id,
        refunded_at=p.refunded_at,
        failure_reason=p.failure_reason,
        period_start=p.period_start,
        period_end=p.period_end,
        paid_at=p.paid_at,
    )


def list_payments(db: Session, account: models.Account, limit: int = 50) -> list[models.Payment]:
    return (
        db.query(models.Payment)
        .filter_by(account_id=account.id)
        .order_by(desc(models.Payment.paid_at), desc(models.Payment.id))
        .limit(limit)
        .all()
    )


def latest_payment(db: Session, account: models.Account, *, succeeded: bool = False) -> models.Payment | None:
    """The newest ledger row — optionally only one that brought money in (for a refund)."""
    query = db.query(models.Payment).filter_by(account_id=account.id)
    if succeeded:
        query = query.filter(models.Payment.status.in_(("succeeded", "partially_refunded")))
    return query.order_by(desc(models.Payment.paid_at), desc(models.Payment.id)).first()


def record_payment(
    db: Session, account: models.Account, provider: str, details: schemas.PaymentDetails, *, failed: bool
) -> models.Payment:
    """Upsert one charge by the provider's id (a retried Stripe invoice keeps its payment
    intent: a failure later turns into a success on the same row)."""
    row = (
        db.query(models.Payment)
        .filter_by(provider=provider, provider_payment_id=details.provider_payment_id)
        .one_or_none()
    )
    plan = details.plan or payments.plan_for_cents(details.amount_cents) or (row.plan if row else "pro_monthly")
    if row is None:
        row = models.Payment(
            account_id=account.id,
            provider=provider,
            provider_payment_id=details.provider_payment_id,
            plan=plan,
            amount_cents=details.amount_cents,
            currency=details.currency,
            status="failed" if failed else "succeeded",
            paid_at=details.paid_at or _now(),
        )
        db.add(row)
    elif row.status == "failed" or not failed:
        row.status = "failed" if failed else row.status if row.refunded_cents else "succeeded"
        row.plan = plan
        if details.amount_cents:
            row.amount_cents = details.amount_cents
        row.paid_at = details.paid_at or row.paid_at
    row.failure_reason = details.failure_reason if failed else None
    if details.period_start:
        row.period_start = details.period_start
    if details.period_end:
        row.period_end = details.period_end
    if not row.period_end and plan != "lifetime" and row.status != "failed":
        start = row.period_start or row.paid_at
        row.period_start = start
        row.period_end = start + timedelta(days=payments.PLAN_PERIOD_DAYS[plan] or 30)
    db.flush()
    return row


def apply_refund(db: Session, payment: models.Payment, cents: int, refund_id: str | None) -> None:
    payment.refunded_cents = min(payment.amount_cents, payment.refunded_cents + cents)
    payment.refund_id = refund_id or payment.refund_id
    payment.refunded_at = _now()
    payment.status = "refunded" if payment.refunded_cents >= payment.amount_cents else "partially_refunded"


# ---------------------------------------------------------------------------- refunds


def refund_preview(
    db: Session, account: models.Account, now: datetime | None = None
) -> tuple[schemas.RefundPreview, models.Payment | None]:
    """What cancelling right now gives back, and the ledger row it comes out of.

    Subscriptions: (days left in the paid period ÷ days in that period) × the amount paid for
    it — the period comes from the charge itself, falling back to the stored period end and the
    plan's nominal length. Lifetime: the flat REFUND_LIFETIME, never more than was paid.
    Nothing to refund: no charge on record, the period is over, the last charge failed, or it
    was refunded already."""
    now = now or _now()
    sub = subscription_for(db, account)
    currency = config.BILLING_CURRENCY
    payment = latest_payment(db, account, succeeded=True)
    if not can_cancel(sub) or payment is None:
        note = "Nothing was paid for the current period." if can_cancel(sub) else "There is no paid membership to end."
        return schemas.RefundPreview(amount="0.00", amount_cents=0, currency=currency, basis="none", note=note), None
    left_to_refund = payment.amount_cents - payment.refunded_cents
    paid = payments.money(payment.amount_cents)
    if sub.status == "past_due" and sub.plan != "lifetime":
        return (
            schemas.RefundPreview(
                amount="0.00",
                amount_cents=0,
                currency=payment.currency,
                basis="none",
                paid=paid,
                note="The last payment failed: the period that was paid for is over — nothing left to refund.",
            ),
            payment,
        )
    if payment.plan == "lifetime" or sub.plan == "lifetime":
        cents = min(payments.to_cents(config.REFUND_LIFETIME), left_to_refund)
        return (
            schemas.RefundPreview(
                amount=payments.money(cents),
                amount_cents=cents,
                currency=payment.currency,
                basis="flat",
                paid=paid,
                note=f"Lifetime: a flat {payments.money(cents)} {payment.currency} of the {paid} paid comes back.",
            ),
            payment,
        )
    period_end = payment.period_end or sub.current_period_end
    period_start = payment.period_start or payment.paid_at
    if not period_end or period_end <= now or period_end <= period_start:
        return (
            schemas.RefundPreview(
                amount="0.00",
                amount_cents=0,
                currency=payment.currency,
                basis="none",
                paid=paid,
                note="The paid period is over — nothing left to refund.",
            ),
            payment,
        )
    days_total = (period_end - period_start).total_seconds() / 86400
    days_left = max(0.0, (period_end - now).total_seconds() / 86400)
    share = Decimal(str(days_left)) / Decimal(str(days_total))
    cents = int((Decimal(left_to_refund) * share).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    cents = max(0, min(cents, left_to_refund))
    return (
        schemas.RefundPreview(
            amount=payments.money(cents),
            amount_cents=cents,
            currency=payment.currency,
            basis="prorated",
            days_left=round(days_left, 2),
            days_total=round(days_total, 2),
            paid=paid,
            note=(
                f"{days_left:.1f} of {days_total:.0f} days of the current period are unused: "
                f"{payments.money(cents)} of the {paid} {payment.currency} paid comes back."
            ),
        ),
        payment,
    )


def cancel(db: Session, account: models.Account) -> schemas.CancelResult:
    """End the paid membership now and refund what `refund_preview` says: the refund is issued
    at the provider first (money matters most), then the subscription is ended there, then the
    local row and ledger are updated. Rows without a usable provider (seeded, admin-granted)
    change locally only — the refund is recorded in the ledger but no money moves."""
    sub = subscription_for(db, account)
    if not subscription_entitles(sub):
        raise HTTPException(status_code=409, detail="There is no paid membership to cancel.")
    if sub.status == "canceled":
        raise HTTPException(status_code=409, detail="The subscription is already canceled.")
    if not can_cancel(sub):
        raise HTTPException(status_code=409, detail="A free trial just runs out — there is nothing to cancel.")
    preview, payment = refund_preview(db, account)
    # Nothing was ever paid (a trial carried over to the provider, first charge still ahead):
    # the free days stay — the provider's "deleted" webhook must not cut them short.
    keep_free_days = payment is None and sub.current_period_end is not None and sub.current_period_end > _now()
    free_until = sub.current_period_end
    method = payments.for_subscription(sub)
    refund_id: str | None = None
    via = "none"
    if preview.amount_cents > 0 and payment is not None:
        if method is not None and payment.provider == method.name:
            refund_id = method.refund(payment, preview.amount_cents)  # may raise PaymentError (502)
            via = "provider"
        else:
            via = "recorded"
    if method is not None:
        try:
            method.cancel(sub)
        except HTTPException:
            if refund_id:  # the money went back already: keep that on record before failing
                db.expire_all()
                apply_refund(db, db.get(models.Payment, payment.id), preview.amount_cents, refund_id)  # type: ignore[union-attr]
                db.commit()
            raise
    # The provider's webhooks (refund, cancellation) may have updated the rows meanwhile.
    db.expire_all()
    sub = subscription_for(db, account)
    if payment is not None and preview.amount_cents > 0:
        payment = db.get(models.Payment, payment.id)
        if payment is not None and (refund_id is None or payment.refund_id != refund_id):
            apply_refund(db, payment, preview.amount_cents, refund_id)
    sub.status = "canceled"
    sub.cancel_at_period_end = False
    # Access ends now (it was refunded) — except free days that were never paid for.
    sub.current_period_end = free_until if keep_free_days else _now()
    db.commit()
    return schemas.CancelResult(
        entitlements=entitlements(db, account), refund=preview, refund_id=refund_id, refunded_via=via
    )


def manage_url(db: Session, account: models.Account, return_url: str) -> tuple[str, str]:
    """(URL, provider) of the provider's self-service page for this account's subscription."""
    sub = subscription_for(db, account)
    method = payments.for_subscription(sub)
    url = method.manage_url(sub, return_url) if method else None
    if not url:
        raise HTTPException(status_code=404, detail="There is no billing page for this subscription.")
    return url, method.name  # type: ignore[union-attr]


# ---------------------------------------------------------------------------- webhooks


def event_seen(db: Session, provider_name: str, event_id: str | None) -> bool:
    """Was this provider event already recorded? (Asked before a strategy translates a delivery,
    because translating may involve the provider — e.g. capturing a PayPal order.)"""
    if not event_id:
        return False
    return (
        db.query(models.BillingEvent).filter_by(provider=provider_name, provider_event_id=event_id).first() is not None
    )


def receive_webhook(db: Session, method: payments.PaymentMethod, body: bytes, headers) -> dict:
    """A provider delivery: authenticate, deduplicate, translate, apply. Used by the webhook
    route and by the built-in fakes (which deliver in-process)."""
    if not method.verify_signature(body, headers):
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")
    try:
        if event_seen(db, method.name, method.event_id(body)):
            return {"ok": True, "duplicate": True}
        event = method.parse_webhook(body)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:  # json/pydantic/shape errors
        raise HTTPException(status_code=422, detail="Malformed webhook payload.") from exc
    if event is None:
        return {"ok": True, "ignored": True}
    processed = apply_webhook(db, event, method.name)
    return {"ok": True, "duplicate": not processed}


def _account_for_event(db: Session, event: schemas.PaymentEvent, provider_name: str) -> models.Account | None:
    account = db.get(models.Account, event.account_id) if event.account_id else None
    if account is None and event.subscription_id:
        # Renewals / failures name only the provider's subscription: find the account by it.
        sub = (
            db.query(models.Subscription)
            .filter_by(provider=provider_name, provider_subscription_id=event.subscription_id)
            .one_or_none()
        )
        account = db.get(models.Account, sub.account_id) if sub else None
    if account is None and event.payment:
        # Refunds / retries name only the charge: find it in the ledger.
        row = (
            db.query(models.Payment)
            .filter_by(provider=provider_name, provider_payment_id=event.payment.provider_payment_id)
            .one_or_none()
        )
        account = db.get(models.Account, row.account_id) if row else None
    return account


def apply_webhook(db: Session, event: schemas.PaymentEvent, provider_name: str) -> bool:
    """Idempotent upsert of the subscription row and the ledger from a neutral payment event
    (any strategy). Returns False if the event was seen before."""
    seen = (
        db.query(models.BillingEvent).filter_by(provider=provider_name, provider_event_id=event.event_id).one_or_none()
    )
    if seen:
        return False
    db.add(models.BillingEvent(provider=provider_name, provider_event_id=event.event_id, type=event.type))
    account = _account_for_event(db, event, provider_name)
    if account is None:
        db.commit()  # record it anyway (audit) and move on
        return True
    sub = subscription_for(db, account)
    if event.type != "payment.refunded":
        sub.provider = provider_name
    sub.provider_customer_id = event.customer_id or sub.provider_customer_id
    sub.provider_subscription_id = event.subscription_id or sub.provider_subscription_id

    if event.type in ("subscription.created", "subscription.updated", "transaction.completed"):
        sub.plan = event.plan
        sub.status = event.status
        if event.plan == "lifetime":
            sub.current_period_end = None
        elif event.current_period_end is not None:  # None = provider did not say: keep what we know
            sub.current_period_end = event.current_period_end
        sub.cancel_at_period_end = event.cancel_at_period_end
        if event.payment:
            record_payment(db, account, provider_name, event.payment, failed=False)
    elif event.type == "subscription.canceled":
        sub.status = "canceled"
        sub.cancel_at_period_end = False
        if event.current_period_end is not None:  # when access ends (None = the stored period end)
            sub.current_period_end = event.current_period_end
    elif event.type == "payment.succeeded":
        if event.payment:
            record_payment(db, account, provider_name, event.payment, failed=False)
        if event.current_period_end is not None:
            sub.current_period_end = event.current_period_end
        if sub.plan == "pro" and sub.status == "past_due":
            sub.status = event.status or "active"  # the dunning is over
    elif event.type == "payment_failed":
        if event.payment:
            record_payment(db, account, provider_name, event.payment, failed=True)
        if sub.plan == "pro" and sub.status in ("active", "trialing", "past_due"):
            sub.status = "past_due"  # grace period; a failed one-off (Lifetime) grants nothing
    elif event.type == "payment.refunded" and event.payment:
        row = (
            db.query(models.Payment)
            .filter_by(provider=provider_name, provider_payment_id=event.payment.provider_payment_id)
            .one_or_none()
        )
        if row is not None and (event.payment.refund_id is None or row.refund_id != event.payment.refund_id):
            total = event.payment.refunded_cents or 0
            cents = total - row.refunded_cents if event.payment.refund_is_total else total
            apply_refund(db, row, max(0, cents), event.payment.refund_id)
            latest = latest_payment(db, account)
            if latest is not None and latest.id == row.id and subscription_entitles(sub):
                # Money for the current membership went back (e.g. refunded from the provider's
                # dashboard): the membership ends now.
                sub.status = "canceled"
                sub.cancel_at_period_end = False
                sub.current_period_end = _now() if sub.plan == "pro" else None
    db.query(models.BillingEvent).filter_by(provider=provider_name, provider_event_id=event.event_id).update(
        {"processed_at": models.utcnow()}
    )
    db.commit()
    return True
