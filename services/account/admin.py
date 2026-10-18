"""Administration API (``/api/v1/admin/*``) — production support and management.

Only accounts with ``role = admin`` (membership.AdminMembership) may call these; every change
is written to ``audit_log``. What an administrator can see is *metadata*: e-mails, plans,
device platforms/versions, relay counters. What they can never see is content — the relay
holds ciphertext only and none of these endpoints return it (``tests/test_admin.py``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import accounts
import auth
import billing
import config
import membership
import models
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
