"""Payment analytics for the administrators' console (apps/ads-admin → Payments).

Everything is computed from the payments ledger (``models.Payment`` — one row per charge with
what became of it) joined to accounts for the customer view. A *window* is a date range
applied to ``paid_at`` (the charge), optionally narrowed to one payment method (provider) and
one status; the previous window of the same length gives the trend. Refund figures are those
of the charges in the window (cohort view: "of what was paid in this window, how much went
back"), while the refunds list and the per-day refund line use ``refunded_at``.

KPI alerts (``alerts()``) are simple threshold rules over the same numbers — failure rate,
refund rate, revenue drop vs. the previous window, accounts in dunning, a window without any
successful payment — with the thresholds in config so an operator can tune them.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import config
import membership
import models
import payments
import schemas

STATUSES = ("succeeded", "failed", "refunded", "partially_refunded")
PAID_STATUSES = ("succeeded", "refunded", "partially_refunded")  # money came in (some may have gone back)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def _end(d: date) -> datetime:
    return datetime.combine(d + timedelta(days=1), time.min, tzinfo=timezone.utc)


def _pct_change(now: int, before: int) -> float | None:
    if before == 0:
        return None
    return round((now - before) / before, 4)


def window(since: str | None, until: str | None, days: int | None) -> tuple[date, date]:
    """(since, until) inclusive dates: explicit range, or the last `days` days (default 30)."""
    try:
        end = date.fromisoformat(until) if until else _today()
        start = date.fromisoformat(since) if since else end - timedelta(days=(days or 30) - 1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Dates must be YYYY-MM-DD.") from exc
    if start > end:
        raise HTTPException(status_code=422, detail="'since' must not be after 'until'.")
    if (end - start).days > 1100:
        raise HTTPException(status_code=422, detail="The window may span at most three years.")
    return start, end


def _query(db: Session, since: date, until: date, method: str | None, status: str | None):
    stmt = (
        select(models.Payment, models.Account.email, models.Account.role)
        .join(models.Account, models.Account.id == models.Payment.account_id)
        .where(models.Payment.paid_at >= _start(since), models.Payment.paid_at < _end(until))
    )
    if method:
        stmt = stmt.where(models.Payment.provider == method)
    if status:
        stmt = stmt.where(models.Payment.status == status)
    return stmt


def _rows(db: Session, since: date, until: date, method: str | None, status: str | None) -> list[tuple]:
    return list(db.execute(_query(db, since, until, method, status).order_by(models.Payment.paid_at.desc())).all())


def _row_out(p: models.Payment, email: str) -> schemas.AdminTransaction:
    return schemas.AdminTransaction(
        id=p.id,
        account_id=p.account_id,
        email=email,
        provider=p.provider,
        provider_payment_id=p.provider_payment_id,
        plan=p.plan,
        amount=payments.money(p.amount_cents),
        amount_cents=p.amount_cents,
        currency=p.currency,
        status=p.status,
        refunded=payments.money(p.refunded_cents),
        refunded_cents=p.refunded_cents,
        refund_id=p.refund_id,
        refunded_at=p.refunded_at,
        failure_reason=p.failure_reason,
        period_start=p.period_start,
        period_end=p.period_end,
        paid_at=p.paid_at,
    )


# ---------------------------------------------------------------------------- totals


class _Totals:
    def __init__(self):
        self.transactions = 0
        self.succeeded = 0
        self.failed = 0
        self.refunds = 0
        self.revenue = 0  # gross: every charge that brought money in
        self.refunded = 0

    def add(self, p: models.Payment) -> None:
        self.transactions += 1
        if p.status == "failed":
            self.failed += 1
            return
        self.succeeded += 1
        self.revenue += p.amount_cents
        if p.refunded_cents:
            self.refunds += 1
            self.refunded += p.refunded_cents

    @property
    def net(self) -> int:
        return self.revenue - self.refunded

    @property
    def average(self) -> int:
        return round(self.revenue / self.succeeded) if self.succeeded else 0


def _totals(rows) -> _Totals:
    t = _Totals()
    for p, *_ in rows:
        t.add(p)
    return t


# ---------------------------------------------------------------------------- alerts


def alerts(db: Session, now: _Totals, before: _Totals, since: date, until: date) -> list[schemas.KpiAlert]:
    """Threshold rules over the window (levels: ok · warn · fail). Every rule is reported, so the
    console shows what is watched even when all is well."""
    out: list[schemas.KpiAlert] = []
    cur = config.BILLING_CURRENCY

    failure_rate = now.failed / now.transactions if now.transactions else 0.0
    enough = now.transactions >= config.PAYMENT_ALERT_MIN_TRANSACTIONS
    level = "ok"
    if enough and failure_rate > config.PAYMENT_ALERT_FAILURE_RATE * 2:
        level = "fail"
    elif enough and failure_rate > config.PAYMENT_ALERT_FAILURE_RATE:
        level = "warn"
    out.append(
        schemas.KpiAlert(
            key="failure_rate",
            level=level,
            title="Payment failure rate",
            detail=(
                f"{now.failed} of {now.transactions} transactions failed ({failure_rate:.0%}); "
                f"threshold {config.PAYMENT_ALERT_FAILURE_RATE:.0%}"
                + ("" if enough else f" (rule needs ≥ {config.PAYMENT_ALERT_MIN_TRANSACTIONS} transactions)")
            ),
            value=round(failure_rate, 4),
            threshold=config.PAYMENT_ALERT_FAILURE_RATE,
        )
    )

    refund_rate = now.refunded / now.revenue if now.revenue else 0.0
    level = "ok"
    if now.revenue and refund_rate > config.PAYMENT_ALERT_REFUND_RATE * 2:
        level = "fail"
    elif now.revenue and refund_rate > config.PAYMENT_ALERT_REFUND_RATE:
        level = "warn"
    out.append(
        schemas.KpiAlert(
            key="refund_rate",
            level=level,
            title="Refund rate",
            detail=(
                f"{payments.money(now.refunded)} of {payments.money(now.revenue)} {cur} refunded ({refund_rate:.0%}); "
                f"threshold {config.PAYMENT_ALERT_REFUND_RATE:.0%}"
            ),
            value=round(refund_rate, 4),
            threshold=config.PAYMENT_ALERT_REFUND_RATE,
        )
    )

    change = _pct_change(now.net, before.net)
    drop = -change if change is not None and change < 0 else 0.0
    level = "ok"
    if before.net and drop > config.PAYMENT_ALERT_REVENUE_DROP * 2:
        level = "fail"
    elif before.net and drop > config.PAYMENT_ALERT_REVENUE_DROP:
        level = "warn"
    out.append(
        schemas.KpiAlert(
            key="revenue_drop",
            level=level,
            title="Revenue vs. previous window",
            detail=(
                f"net {payments.money(now.net)} {cur} now vs. {payments.money(before.net)} {cur} before"
                + (f" ({change:+.0%})" if change is not None else " (no baseline)")
                + f"; alert when down more than {config.PAYMENT_ALERT_REVENUE_DROP:.0%}"
            ),
            value=change,
            threshold=-config.PAYMENT_ALERT_REVENUE_DROP,
        )
    )

    past_due = db.execute(
        select(func.count()).select_from(models.Subscription).where(models.Subscription.status == "past_due")
    ).scalar_one()
    level = "ok"
    if past_due > config.PAYMENT_ALERT_PAST_DUE * 2:
        level = "fail"
    elif past_due > config.PAYMENT_ALERT_PAST_DUE:
        level = "warn"
    out.append(
        schemas.KpiAlert(
            key="past_due",
            level=level,
            title="Subscriptions in dunning",
            detail=(
                f"{past_due} subscription(s) past due right now (payment failed, grace period running); "
                f"threshold {config.PAYMENT_ALERT_PAST_DUE}"
            ),
            value=float(past_due),
            threshold=float(config.PAYMENT_ALERT_PAST_DUE),
        )
    )

    silent = now.succeeded == 0 and before.succeeded > 0
    out.append(
        schemas.KpiAlert(
            key="no_revenue",
            level="fail" if silent else "ok",
            title="Successful payments in the window",
            detail=(
                f"none between {since} and {until} although there were {before.succeeded} in the previous window"
                if silent
                else f"{now.succeeded} successful payment(s) between {since} and {until}"
            ),
            value=float(now.succeeded),
            threshold=1.0,
        )
    )
    return out


# ---------------------------------------------------------------------------- analytics


def analytics(
    db: Session, since: date, until: date, method: str | None, status: str | None
) -> schemas.PaymentAnalytics:
    rows = _rows(db, since, until, method, status)
    days = (until - since).days + 1
    prev_rows = _rows(db, since - timedelta(days=days), since - timedelta(days=1), method, status)
    now, before = _totals(rows), _totals(prev_rows)
    cur = config.BILLING_CURRENCY

    by_day: dict[str, schemas.PaymentDayPoint] = {}
    for i in range(days):
        key = (since + timedelta(days=i)).isoformat()
        by_day[key] = schemas.PaymentDayPoint(key=key)
    by_status: dict[str, schemas.PaymentStatusShare] = {
        s: schemas.PaymentStatusShare(status=s, count=0, amount_cents=0, amount="0.00") for s in STATUSES
    }
    by_method: dict[str, schemas.PaymentMethodShare] = {}
    by_plan: dict[str, schemas.PaymentPlanShare] = {}
    customers: dict[str, dict] = {}
    refunds: list[schemas.AdminRefund] = []
    currencies = set()
    for p, email, _role in rows:
        currencies.add(p.currency)
        day = by_day.get(p.paid_at.date().isoformat())
        if day is not None:
            day.transactions += 1
            if p.status == "failed":
                day.failed += 1
            else:
                day.succeeded += 1
                day.revenue_cents += p.amount_cents
        if p.refunded_cents and p.refunded_at:
            rday = by_day.get(p.refunded_at.date().isoformat())
            if rday is not None:
                rday.refunded_cents += p.refunded_cents
                rday.refunds += 1
        st = (
            by_status[p.status]
            if p.status in by_status
            else by_status.setdefault(
                p.status, schemas.PaymentStatusShare(status=p.status, count=0, amount_cents=0, amount="0.00")
            )
        )
        st.count += 1
        st.amount_cents += p.refunded_cents if p.status in ("refunded", "partially_refunded") else p.amount_cents
        m = by_method.setdefault(
            p.provider, schemas.PaymentMethodShare(provider=p.provider, label=_method_label(p.provider))
        )
        m.count += 1
        pl = by_plan.setdefault(
            p.plan, schemas.PaymentPlanShare(plan=p.plan, title=payments.PLAN_TITLES.get(p.plan, p.plan))
        )
        pl.count += 1
        if p.status == "failed":
            m.failed += 1
        else:
            m.succeeded += 1
            m.amount_cents += p.amount_cents
            m.refunded_cents += p.refunded_cents
            pl.amount_cents += p.amount_cents
            pl.refunded_cents += p.refunded_cents
            c = customers.setdefault(
                p.account_id, {"email": email, "payments": 0, "amount": 0, "refunded": 0, "last": p.paid_at}
            )
            c["payments"] += 1
            c["amount"] += p.amount_cents
            c["refunded"] += p.refunded_cents
            c["last"] = max(c["last"], p.paid_at)
        if p.refunded_cents:
            refunds.append(
                schemas.AdminRefund(
                    id=p.id,
                    account_id=p.account_id,
                    email=email,
                    provider=p.provider,
                    plan=p.plan,
                    amount=payments.money(p.amount_cents),
                    refunded=payments.money(p.refunded_cents),
                    refunded_cents=p.refunded_cents,
                    refund_id=p.refund_id,
                    refunded_at=p.refunded_at,
                    paid_at=p.paid_at,
                    full=p.refunded_cents >= p.amount_cents,
                )
            )
    for point in by_day.values():
        point.revenue = payments.money(point.revenue_cents)
        point.refunded = payments.money(point.refunded_cents)
        point.net_cents = point.revenue_cents - point.refunded_cents
    for st in by_status.values():
        st.amount = payments.money(st.amount_cents)
        st.share = round(st.count / now.transactions, 4) if now.transactions else 0.0
    for m in by_method.values():
        m.amount = payments.money(m.amount_cents)
        m.refunded = payments.money(m.refunded_cents)
        m.share = round(m.amount_cents / now.revenue, 4) if now.revenue else 0.0
    for pl in by_plan.values():
        pl.amount = payments.money(pl.amount_cents)
        pl.refunded = payments.money(pl.refunded_cents)
        pl.share = round(pl.amount_cents / now.revenue, 4) if now.revenue else 0.0
    memberships = _memberships(db, list(customers))
    top = sorted(customers.items(), key=lambda kv: (-kv[1]["amount"], kv[1]["email"]))[:10]
    top_customers = [
        schemas.TopCustomer(
            account_id=aid,
            email=c["email"],
            membership=memberships.get(aid, "free"),
            payments=c["payments"],
            amount=payments.money(c["amount"]),
            amount_cents=c["amount"],
            refunded=payments.money(c["refunded"]),
            refunded_cents=c["refunded"],
            net_cents=c["amount"] - c["refunded"],
            share=round(c["amount"] / now.revenue, 4) if now.revenue else 0.0,
            last_paid_at=c["last"],
        )
        for aid, c in top
    ]
    refunds.sort(key=lambda r: r.refunded_at or r.paid_at, reverse=True)
    return schemas.PaymentAnalytics(
        since=since.isoformat(),
        until=until.isoformat(),
        days=days,
        method=method,
        status=status,
        currency=cur,
        mixed_currencies=len(currencies - {cur}) > 0,
        revenue=payments.money(now.revenue),
        revenue_cents=now.revenue,
        refunded=payments.money(now.refunded),
        refunded_cents=now.refunded,
        net_revenue=payments.money(now.net),
        net_revenue_cents=now.net,
        transactions=now.transactions,
        succeeded=now.succeeded,
        failed=now.failed,
        refund_count=now.refunds,
        success_rate=round(now.succeeded / now.transactions, 4) if now.transactions else 0.0,
        refund_rate=round(now.refunded / now.revenue, 4) if now.revenue else 0.0,
        average_transaction=payments.money(now.average),
        average_transaction_cents=now.average,
        previous_revenue_cents=before.revenue,
        previous_net_revenue_cents=before.net,
        previous_transactions=before.transactions,
        previous_succeeded=before.succeeded,
        previous_failed=before.failed,
        previous_refunded_cents=before.refunded,
        revenue_change=_pct_change(now.net, before.net),
        transactions_change=_pct_change(now.transactions, before.transactions),
        failed_change=_pct_change(now.failed, before.failed),
        by_day=list(by_day.values()),
        by_status=[s for s in by_status.values() if s.count],
        by_method=sorted(by_method.values(), key=lambda m: -m.amount_cents),
        by_plan=sorted(by_plan.values(), key=lambda pl: -pl.amount_cents),
        top_customers=top_customers,
        refunds=refunds[:50],
        alerts=alerts(db, now, before, since, until),
        methods=sorted({m for (m,) in db.execute(select(models.Payment.provider).distinct()).all()}),
    )


def _method_label(provider: str) -> str:
    method = payments.METHODS.get(provider)
    return method.label if method else provider


def _memberships(db: Session, account_ids: list[str]) -> dict[str, str]:
    if not account_ids:
        return {}
    out = {}
    for account in db.execute(select(models.Account).where(models.Account.id.in_(account_ids))).scalars():
        out[account.id] = membership.resolve(db, account).type
    return out


# ---------------------------------------------------------------------------- transactions (drill-down)


def transactions(
    db: Session,
    since: date,
    until: date,
    method: str | None,
    status: str | None,
    q: str | None,
    limit: int,
    offset: int,
) -> schemas.AdminTransactionPage:
    stmt = _query(db, since, until, method, status)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            func.lower(models.Account.email).like(like)
            | func.lower(models.Payment.provider_payment_id).like(like)
            | func.lower(func.coalesce(models.Payment.refund_id, "")).like(like)
        )
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(models.Payment.paid_at.desc(), models.Payment.id.desc()).limit(limit).offset(offset)
    ).all()
    return schemas.AdminTransactionPage(
        items=[_row_out(p, email) for p, email, _ in rows], total=total, limit=limit, offset=offset
    )


def transaction(db: Session, payment_id: int) -> schemas.AdminTransactionDetail:
    row = db.execute(
        select(models.Payment, models.Account)
        .join(models.Account, models.Account.id == models.Payment.account_id)
        .where(models.Payment.id == payment_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    p, account = row
    sub = db.get(models.Subscription, account.id)
    history = (
        db.query(models.Payment)
        .filter_by(account_id=account.id)
        .order_by(models.Payment.paid_at.desc(), models.Payment.id.desc())
        .limit(50)
        .all()
    )
    return schemas.AdminTransactionDetail(
        transaction=_row_out(p, account.email),
        account=schemas.AdminTransactionAccount(
            id=account.id,
            email=account.email,
            role=account.role,
            membership=membership.resolve(db, account).type,
            created_at=account.created_at,
            deleted_at=account.deleted_at,
            plan=sub.plan if sub else "free",
            status=sub.status if sub else "none",
            provider=sub.provider if sub else "",
            provider_customer_id=sub.provider_customer_id if sub else None,
            provider_subscription_id=sub.provider_subscription_id if sub else None,
            current_period_end=sub.current_period_end if sub else None,
            cancel_at_period_end=bool(sub.cancel_at_period_end) if sub else False,
            lifetime_value_cents=sum(h.amount_cents - h.refunded_cents for h in history if h.status != "failed"),
        ),
        history=[_row_out(h, account.email) for h in history],
    )


def export_csv(db: Session, since: date, until: date, method: str | None, status: str | None) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(
        [
            "paid_at", "id", "email", "provider", "provider_payment_id", "plan", "amount", "currency", "status",
            "refunded", "refund_id", "refunded_at", "failure_reason", "period_start", "period_end",
        ]
    )  # fmt: skip
    for p, email, _ in _rows(db, since, until, method, status):
        w.writerow(
            [
                p.paid_at.isoformat(), p.id, email, p.provider, p.provider_payment_id, p.plan,
                payments.money(p.amount_cents), p.currency, p.status, payments.money(p.refunded_cents),
                p.refund_id or "", p.refunded_at.isoformat() if p.refunded_at else "", p.failure_reason or "",
                p.period_start.isoformat() if p.period_start else "", p.period_end.isoformat() if p.period_end else "",
            ]
        )  # fmt: skip
    return out.getvalue()
