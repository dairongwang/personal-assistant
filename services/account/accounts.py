"""Account lifecycle helpers shared by the user-facing API (main.py), the admin API (admin.py)
and the seed script: purge, sign-out-everywhere, audit trail."""

from __future__ import annotations

import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

import models


def purge(db: Session, account: models.Account) -> None:
    """Relay ops, snapshot and devices go now; the account row becomes a billing tombstone
    (the subscription row stays for accounting). Caller commits."""
    db.execute(delete(models.SyncOp).where(models.SyncOp.account_id == account.id))
    db.execute(delete(models.SyncSnapshot).where(models.SyncSnapshot.account_id == account.id))
    db.execute(delete(models.Device).where(models.Device.account_id == account.id))
    account.deleted_at = models.utcnow()
    account.email = f"deleted-{account.id}@invalid"
    account.wrapped_data_key = ""
    account.auth_hash = ""
    account.role = models.ROLE_USER


def revoke_all_devices(db: Session, account: models.Account) -> int:
    """Sign the account out everywhere (tokens of every device stop working). Caller commits."""
    n = 0
    for device in db.execute(
        select(models.Device).where(models.Device.account_id == account.id, models.Device.revoked_at.is_(None))
    ).scalars():
        device.revoked_at = models.utcnow()
        n += 1
    return n


def audit(
    db: Session,
    actor: models.Account | None,
    action: str,
    target: models.Account | None = None,
    **detail: object,
) -> models.AuditLog:
    """Append an audit entry (metadata only — never user content). Caller commits."""
    row = models.AuditLog(
        actor_account_id=actor.id if actor else None,
        action=action,
        target_account_id=target.id if target else None,
        detail=json.dumps(detail, default=str, sort_keys=True),
    )
    db.add(row)
    return row
