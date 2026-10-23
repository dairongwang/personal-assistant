"""Administration API (``/api/v1/admin/*``) — production support and management.

Only accounts with ``role = admin`` (membership.AdminMembership) may call these; every change
is written to ``audit_log``. What an administrator can see is *metadata*: e-mails, plans,
device platforms/versions, relay counters. What they can never see is content — the relay
holds ciphertext only and none of these endpoints return it (``tests/test_admin.py``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import accounts
import ad_analytics
import ad_sources
import ads
import auth
import billing
import config
import membership
import models
import payment_analytics
import releases
import schemas
from database import get_db

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(auth.admin_principal)])


def _device_out(d: models.Device) -> schemas.AdminDeviceOut:
    return schemas.AdminDeviceOut(
        id=d.id,
        platform=d.platform,
        client_version=d.client_version,
        schema_version=d.schema_version,
        cursor=d.cursor,
        created_at=d.created_at,
        last_seen_at=d.last_seen_at,
    )


def _summary(db: Session, account: models.Account) -> schemas.AdminAccountSummary:
    sub = billing.subscription_for(db, account)
    devices = db.execute(
        select(func.count(models.Device.id), func.max(models.Device.last_seen_at)).where(
            models.Device.account_id == account.id, models.Device.revoked_at.is_(None)
        )
    ).one()
    strategy = membership.strategy_for(account, billing.subscription_entitles(sub))
    return schemas.AdminAccountSummary(
        id=account.id,
        email=account.email,
        role=account.role,
        membership=strategy.type,
        plan=sub.plan,
        status=sub.status,
        current_period_end=sub.current_period_end,
        created_at=account.created_at,
        deleted_at=account.deleted_at,
        devices_active=devices[0] or 0,
        last_seen_at=devices[1],
    )


def _get(db: Session, account_id: str) -> models.Account:
    account = db.get(models.Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found.")
    return account


# ---------------------------------------------------------------------------- read


@router.get("/stats", response_model=schemas.AdminStats)
def stats(db: Session = Depends(get_db)):
    live = select(models.Account).where(models.Account.deleted_at.is_(None))
    subs = {s.account_id: s for s in db.execute(select(models.Subscription)).scalars()}
    memberships = {"free": 0, "paid": 0, "admin": 0}
    for account in db.execute(live).scalars():
        sub = subs.get(account.id)
        entitled = sub is not None and billing.subscription_entitles(sub)
        memberships[membership.strategy_for(account, entitled).type] += 1
    subscriptions = {
        status: n
        for status, n in db.execute(
            select(models.Subscription.status, func.count()).group_by(models.Subscription.status)
        ).all()
    }
    ops = db.execute(select(func.count(models.SyncOp.id), func.coalesce(func.sum(models.SyncOp.size_bytes), 0))).one()
    snaps = db.execute(
        select(func.count(models.SyncSnapshot.account_id), func.coalesce(func.sum(models.SyncSnapshot.size_bytes), 0))
    ).one()
    latest = releases.latest(db)
    return schemas.AdminStats(
        accounts_total=db.execute(select(func.count()).select_from(models.Account)).scalar_one(),
        accounts_deleted=db.execute(
            select(func.count()).select_from(models.Account).where(models.Account.deleted_at.is_not(None))
        ).scalar_one(),
        memberships=memberships,
        subscriptions=subscriptions,
        devices_active=db.execute(
            select(func.count()).select_from(models.Device).where(models.Device.revoked_at.is_(None))
        ).scalar_one(),
        sync_ops=ops[0],
        sync_bytes=int(ops[1]),
        snapshots=snaps[0],
        snapshot_bytes=int(snaps[1]),
        latest_release=latest.version if latest else None,
        service_version=config.SERVICE_VERSION,
        database="postgresql" if config.IS_POSTGRES else "sqlite",
        ads_admin_url=config.ADS_ADMIN_URL,
    )


@router.get("/accounts", response_model=schemas.AdminAccountPage)
def list_accounts(
    q: str = Query(default="", max_length=320, description="E-mail contains"),
    membership_type: str = Query(default="", alias="membership", pattern="^(|free|paid|admin)$"),
    include_deleted: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = select(models.Account)
    if q.strip():
        stmt = stmt.where(func.lower(models.Account.email).contains(q.strip().lower()))
    if not include_deleted:
        stmt = stmt.where(models.Account.deleted_at.is_(None))
    stmt = stmt.order_by(models.Account.created_at.desc(), models.Account.id)
    if not membership_type:
        total = db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar_one()
        items = [_summary(db, a) for a in db.execute(stmt.offset(offset).limit(limit)).scalars()]
    else:
        # Membership is derived (role + billing state machine), so filter while walking the rows.
        items, total = [], 0
        for account in db.execute(stmt).scalars():
            summary = _summary(db, account)
            if summary.membership != membership_type:
                continue
            if offset <= total < offset + limit:
                items.append(summary)
            total += 1
    return schemas.AdminAccountPage(items=items, total=total, offset=offset, limit=limit)


@router.get("/accounts/{account_id}", response_model=schemas.AdminAccountDetail)
def account_detail(account_id: str, db: Session = Depends(get_db)):
    account = _get(db, account_id)
    summary = _summary(db, account)
    sub = billing.subscription_for(db, account)
    strategy = membership.resolve(db, account)
    devices = db.execute(
        select(models.Device)
        .where(models.Device.account_id == account.id, models.Device.revoked_at.is_(None))
        .order_by(models.Device.last_seen_at.desc())
    ).scalars()
    ops = db.execute(
        select(func.count(models.SyncOp.id), func.coalesce(func.sum(models.SyncOp.size_bytes), 0)).where(
            models.SyncOp.account_id == account.id
        )
    ).one()
    snapshot = db.get(models.SyncSnapshot, account.id)
    audit_rows = db.execute(
        select(models.AuditLog)
        .where(models.AuditLog.target_account_id == account.id)
        .order_by(models.AuditLog.at.desc())
        .limit(50)
    ).scalars()
    actors: dict[str, str | None] = {}
    audit = []
    for row in audit_rows:
        if row.actor_account_id and row.actor_account_id not in actors:
            actor = db.get(models.Account, row.actor_account_id)
            actors[row.actor_account_id] = actor.email if actor else None
        audit.append(
            schemas.AdminAuditEntry(
                at=row.at,
                actor_account_id=row.actor_account_id,
                actor_email=actors.get(row.actor_account_id or ""),
                action=row.action,
                detail=json.loads(row.detail or "{}"),
            )
        )
    return schemas.AdminAccountDetail(
        **summary.model_dump(),
        vault_enabled=account.vault_enabled,
        provider=sub.provider,
        cancel_at_period_end=sub.cancel_at_period_end,
        features=list(strategy.features),
        max_active_tasks=strategy.max_active_tasks,
        devices=[_device_out(d) for d in devices],
        sync_ops=ops[0],
        sync_bytes=int(ops[1]),
        snapshot_bytes=snapshot.size_bytes if snapshot else None,
        audit=audit,
    )


# ---------------------------------------------------------------------------- write


@router.patch("/accounts/{account_id}/membership", response_model=schemas.AdminAccountDetail)
def set_membership(
    account_id: str,
    body: schemas.AdminMembershipUpdate,
    p: auth.Principal = Depends(auth.admin_principal),
    db: Session = Depends(get_db),
):
    """Comp a paid membership, extend it, or take it away. The account's *role* is untouched
    (an administrator stays an administrator)."""
    account = _get(db, account_id)
    if account.deleted_at:
        raise HTTPException(status_code=409, detail="This account was deleted.")
    sub = billing.subscription_for(db, account)
    before = {"plan": sub.plan, "status": sub.status, "current_period_end": sub.current_period_end}
    sub.provider = "admin"
    sub.plan = body.plan
    sub.cancel_at_period_end = False
    if body.plan == "free":
        sub.status = body.status or "canceled"
        sub.current_period_end = None
    elif body.plan == "lifetime":
        sub.status = body.status or "active"
        sub.current_period_end = None
    else:
        sub.status = body.status or "active"
        sub.current_period_end = datetime.now(timezone.utc) + timedelta(days=body.days or 30)
    accounts.audit(
        db,
        p.account,
        "membership.set",
        account,
        before=before,
        after={"plan": sub.plan, "status": sub.status, "current_period_end": sub.current_period_end},
        note=body.note,
    )
    db.commit()
    return account_detail(account_id, db)


@router.patch("/accounts/{account_id}/role", response_model=schemas.AdminAccountDetail)
def set_role(
    account_id: str,
    body: schemas.AdminRoleUpdate,
    p: auth.Principal = Depends(auth.admin_principal),
    db: Session = Depends(get_db),
):
    account = _get(db, account_id)
    if account.deleted_at:
        raise HTTPException(status_code=409, detail="This account was deleted.")
    if account.id == p.account.id and body.role != models.ROLE_ADMIN:
        raise HTTPException(status_code=409, detail="You cannot remove your own administrator role.")
    if account.role != body.role:
        accounts.audit(db, p.account, "role.set", account, before=account.role, after=body.role)
        account.role = body.role
        db.commit()
    return account_detail(account_id, db)


@router.post("/accounts/{account_id}/sign-out", response_model=schemas.AdminAccountDetail)
def sign_out_everywhere(
    account_id: str, p: auth.Principal = Depends(auth.admin_principal), db: Session = Depends(get_db)
):
    """Support action for a lost/stolen device or a suspected token leak: every device of the
    account has to sign in again. Local data on the devices is untouched."""
    account = _get(db, account_id)
    n = accounts.revoke_all_devices(db, account)
    accounts.audit(db, p.account, "devices.revoke_all", account, devices=n)
    db.commit()
    return account_detail(account_id, db)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(account_id: str, p: auth.Principal = Depends(auth.admin_principal), db: Session = Depends(get_db)):
    """Same purge the user can trigger themselves (GDPR requests handled by support)."""
    account = _get(db, account_id)
    if account.id == p.account.id:
        raise HTTPException(status_code=409, detail="Delete your own account from the account screen, not here.")
    if not account.deleted_at:
        accounts.audit(db, p.account, "account.purge", account, email=account.email)
        accounts.purge(db, account)
        db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------- sponsored content


def _get_ad(db: Session, ad_id: str) -> models.AdCampaign:
    ad = db.get(models.AdCampaign, ad_id)
    if not ad:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    return ad


@router.get("/ads/overview", response_model=schemas.AdOverview)
def ads_overview(db: Session = Depends(get_db)):
    ads.prune_stats(db)
    return ads.overview(db)


@router.get("/ads/analytics", response_model=schemas.AdAnalytics)
def ads_analytics(
    days: int = Query(default=30, ge=1, le=400),
    kind: str = Query(default="", pattern="^(|internal|sponsored|affiliate)$"),
    db: Session = Depends(get_db),
):
    """Performance across campaigns: totals vs. the previous window, per day, per source kind,
    per country (small counts folded into "other"), per campaign."""
    ads.prune_stats(db)
    return ad_analytics.analytics(db, days, kind or None)


@router.get("/ads/analytics.csv", response_class=Response)
def ads_analytics_csv(days: int = Query(default=30, ge=1, le=400), db: Session = Depends(get_db)):
    """Aggregate export (day × campaign × country, suppression applied) for sponsors and auditors."""
    body = ad_analytics.export_csv(db, days)
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="ad-performance-{days}d.csv"'},
    )


# ---------------------------------------------------------------------------- payments


METHOD_PATTERN = "^[a-z_]{0,32}$"
STATUS_PATTERN = "^(|succeeded|failed|refunded|partially_refunded)$"


@router.get("/payments/analytics", response_model=schemas.PaymentAnalytics)
def payment_analytics_view(
    since: str = Query(default="", pattern=r"^(|\d{4}-\d{2}-\d{2})$"),
    until: str = Query(default="", pattern=r"^(|\d{4}-\d{2}-\d{2})$"),
    days: int = Query(default=30, ge=1, le=1100),
    method: str = Query(default="", pattern=METHOD_PATTERN),
    status: str = Query(default="", pattern=STATUS_PATTERN),
    db: Session = Depends(get_db),
):
    """Revenue, transactions, successes/failures, refunds and average value for a date range
    (explicit since/until or the last `days`), per day / status / method / plan, the top
    customers, the refunds, and the KPI alerts — optionally narrowed to one method and status."""
    start, end = payment_analytics.window(since or None, until or None, days)
    return payment_analytics.analytics(db, start, end, method or None, status or None)


@router.get("/payments/transactions", response_model=schemas.AdminTransactionPage)
def payment_transactions(
    since: str = Query(default="", pattern=r"^(|\d{4}-\d{2}-\d{2})$"),
    until: str = Query(default="", pattern=r"^(|\d{4}-\d{2}-\d{2})$"),
    days: int = Query(default=30, ge=1, le=1100),
    method: str = Query(default="", pattern=METHOD_PATTERN),
    status: str = Query(default="", pattern=STATUS_PATTERN),
    q: str = Query(default="", max_length=120),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """The transactions behind the analytics (drill-down), newest first; `q` matches the
    customer's e-mail, the provider's payment id or the refund id."""
    start, end = payment_analytics.window(since or None, until or None, days)
    return payment_analytics.transactions(db, start, end, method or None, status or None, q or None, limit, offset)


@router.get("/payments/transactions/{payment_id}", response_model=schemas.AdminTransactionDetail)
def payment_transaction(payment_id: int, db: Session = Depends(get_db)):
    """One transaction with its customer's account/subscription state and payment history."""
    return payment_analytics.transaction(db, payment_id)


@router.get("/payments/transactions.csv", response_class=Response)
def payment_transactions_csv(
    since: str = Query(default="", pattern=r"^(|\d{4}-\d{2}-\d{2})$"),
    until: str = Query(default="", pattern=r"^(|\d{4}-\d{2}-\d{2})$"),
    days: int = Query(default=30, ge=1, le=1100),
    method: str = Query(default="", pattern=METHOD_PATTERN),
    status: str = Query(default="", pattern=STATUS_PATTERN),
    db: Session = Depends(get_db),
):
    """The filtered transactions as CSV (for accounting)."""
    start, end = payment_analytics.window(since or None, until or None, days)
    body = payment_analytics.export_csv(db, start, end, method or None, status or None)
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="payments-{start}-{end}.csv"'},
    )


@router.get("/ads/compliance", response_model=schemas.AdCompliance)
def ads_compliance(request: Request, db: Session = Depends(get_db)):
    """Live privacy checks against configuration and database, mapped to privacy regimes worldwide."""
    return ad_analytics.compliance(db, request.app)


@router.get("/ads/sources", response_model=schemas.AdSourcesOut)
def ads_sources(db: Session = Depends(get_db)):
    """Every ad source this deployment knows, with its mode (fake / live / unavailable) and the active one."""
    active = ad_sources.active_kind(db)
    return schemas.AdSourcesOut(
        active=active,
        fakes_enabled=config.ADS_FAKE_PROVIDERS,
        sources=[ad_sources.admin_out(info, active) for info in ad_sources.describe_all()],
    )


@router.put("/ads/sources", response_model=schemas.AdSourcesOut)
def ads_set_source(
    body: schemas.AdSourceUpdate, p: auth.Principal = Depends(auth.admin_principal), db: Session = Depends(get_db)
):
    """Switch the active ad source at runtime (overrides ADS_SOURCE until changed again). Audited."""
    info = ad_sources.describe(body.kind)
    if info.mode == "unavailable":
        raise HTTPException(status_code=409, detail=f"{ad_sources.TITLES[body.kind]} is not configured: {info.reason}")
    before = ad_sources.active_kind(db)
    if before != body.kind:
        ad_sources.set_active(db, body.kind)
        accounts.audit(db, p.account, "ad.source", before=before, after=body.kind, mode=info.mode)
        db.commit()
    return ads_sources(db)


@router.get("/ads", response_model=list[schemas.AdCampaignOut])
def ads_list(include_archived: bool = False, db: Session = Depends(get_db)):
    stmt = select(models.AdCampaign).order_by(models.AdCampaign.created_at.desc())
    if not include_archived:
        stmt = stmt.where(models.AdCampaign.status != "archived")
    return [ads.admin_out(a) for a in db.execute(stmt).scalars()]


@router.post("/ads", response_model=schemas.AdCampaignOut, status_code=201)
def ads_create(
    body: schemas.AdCampaignIn, p: auth.Principal = Depends(auth.admin_principal), db: Session = Depends(get_db)
):
    ad = models.AdCampaign(created_by=p.account.id)
    try:
        ads.apply_input(ad, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(ad)
    db.flush()
    accounts.audit(db, p.account, "ad.create", ad_id=ad.id, sponsor=ad.sponsor, headline=ad.headline, status=ad.status)
    db.commit()
    return ads.admin_out(ad)


@router.get("/ads/{ad_id}", response_model=schemas.AdCampaignOut)
def ads_get(ad_id: str, db: Session = Depends(get_db)):
    return ads.admin_out(_get_ad(db, ad_id))


@router.patch("/ads/{ad_id}", response_model=schemas.AdCampaignOut)
def ads_update(
    ad_id: str,
    body: schemas.AdCampaignPatch,
    p: auth.Principal = Depends(auth.admin_principal),
    db: Session = Depends(get_db),
):
    ad = _get_ad(db, ad_id)
    try:
        changes = ads.apply_input(ad, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if ad.starts_at and ad.ends_at and ad.ends_at <= ad.starts_at:
        raise HTTPException(status_code=422, detail="ends_at must be after starts_at.")
    if changes:
        accounts.audit(db, p.account, "ad.update", ad_id=ad.id, sponsor=ad.sponsor, changes=changes)
        db.commit()
    return ads.admin_out(ad)


@router.delete("/ads/{ad_id}", response_model=schemas.AdCampaignOut)
def ads_archive(ad_id: str, p: auth.Principal = Depends(auth.admin_principal), db: Session = Depends(get_db)):
    """Archive (never hard-delete: the aggregate history keeps meaning)."""
    ad = _get_ad(db, ad_id)
    if ad.status != "archived":
        accounts.audit(db, p.account, "ad.archive", ad_id=ad.id, sponsor=ad.sponsor, before=ad.status)
        ad.status = "archived"
        db.commit()
    return ads.admin_out(ad)


@router.get("/ads/{ad_id}/stats", response_model=schemas.AdStats)
def ads_stats(ad_id: str, days: int = Query(default=30, ge=1, le=400), db: Session = Depends(get_db)):
    return ads.stats(db, _get_ad(db, ad_id), days)


@router.get("/audit", response_model=list[schemas.AdminAuditEntry])
def audit_log(limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)):
    rows = db.execute(
        select(models.AuditLog).order_by(models.AuditLog.at.desc(), models.AuditLog.id.desc()).limit(limit)
    )
    out = []
    for row in rows.scalars():
        actor = db.get(models.Account, row.actor_account_id) if row.actor_account_id else None
        out.append(
            schemas.AdminAuditEntry(
                at=row.at,
                actor_account_id=row.actor_account_id,
                actor_email=actor.email if actor else None,
                action=row.action,
                detail={"target_account_id": row.target_account_id, **json.loads(row.detail or "{}")},
            )
        )
    return out
