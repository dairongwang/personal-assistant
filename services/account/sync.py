"""Encrypted op-log relay — the server is the master of *ordering*, never of content.

* ``push``: the server assigns a strictly increasing per-account ``seq`` to every op. Two
  devices writing concurrently get a total order decided here; clients apply ops in ``seq``
  order (last writer by seq wins), so every device converges to the same state.
* ``pull``: ops with ``seq > cursor`` for this device, in order.
* ``ack``: the device reports the last seq it applied. When all *active* devices have acked an
  op (or it is older than the TTL) it is deleted — the relay keeps ciphertext for hours, not
  years. Devices unseen for SYNC_INACTIVE_DEVICE_DAYS no longer hold ops back.
* ``snapshot`` (vault, on by default): the latest encrypted full state so a device can rebuild
  its local cache from the server ("server is master") even after ops were pruned.

Ciphertext is base64 in JSON and bytes in the database. It is validated for size only.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

import config
import models
import schemas


def _decode(ciphertext: str, limit: int) -> bytes:
    try:
        raw = base64.b64decode(ciphertext, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Sync payload must be base64 ciphertext.") from exc
    if not raw or len(raw) > limit:
        raise HTTPException(status_code=413, detail=f"Sync payload exceeds {limit} bytes.")
    return raw


def _encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _lock_account(db: Session, account_id: str) -> models.Account:
    """Serialise seq allocation per account (row lock on Postgres; SQLite serialises anyway)."""
    stmt = select(models.Account).where(models.Account.id == account_id)
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one()


def push(
    db: Session, principal_account: models.Account, device: models.Device, req: schemas.PushRequest
) -> schemas.PushResponse:
    if len(req.ops) > config.SYNC_MAX_BATCH:
        raise HTTPException(status_code=413, detail=f"Send at most {config.SYNC_MAX_BATCH} operations per request.")
    account = _lock_account(db, principal_account.id)
    existing = {
        row.op_id: row.seq
        for row in db.execute(
            select(models.SyncOp.op_id, models.SyncOp.seq).where(
                models.SyncOp.account_id == account.id, models.SyncOp.op_id.in_([o.op_id for o in req.ops])
            )
        )
    }
    now = datetime.now(timezone.utc)
    accepted: list[schemas.PushedOp] = []
    for op in req.ops:
        if op.op_id in existing:  # retry of an already-relayed op → idempotent
            accepted.append(schemas.PushedOp(op_id=op.op_id, seq=existing[op.op_id]))
            continue
        raw = _decode(op.ciphertext, config.SYNC_MAX_OP_BYTES)
        account.last_seq += 1
        db.add(
            models.SyncOp(
                account_id=account.id,
                seq=account.last_seq,
                device_id=device.id,
                op_id=op.op_id,
                schema_version=op.schema_version,
                ciphertext=raw,
                size_bytes=len(raw),
                created_at=now,
                expires_at=now + timedelta(days=config.SYNC_OP_TTL_DAYS),
            )
        )
        accepted.append(schemas.PushedOp(op_id=op.op_id, seq=account.last_seq))
    device.schema_version = max(device.schema_version, max((o.schema_version for o in req.ops), default=0))
    # Note: the cursor is NOT advanced here. Ops from other devices may sit between this
    # device's cursor and the seqs just assigned; the client skips its own ops when pulling
    # and acks explicitly once it has applied everything in order.
    db.commit()
    return schemas.PushResponse(accepted=accepted, last_seq=account.last_seq)


def pull(db: Session, account: models.Account, device: models.Device, since: int, limit: int) -> schemas.PullResponse:
    rows = (
        db.execute(
            select(models.SyncOp)
            .where(models.SyncOp.account_id == account.id, models.SyncOp.seq > since)
            .order_by(models.SyncOp.seq)
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    snapshot = db.get(models.SyncSnapshot, account.id)
    return schemas.PullResponse(
        ops=[
            schemas.PulledOp(
                seq=r.seq,
                op_id=r.op_id,
                device_id=r.device_id,
                schema_version=r.schema_version,
                ciphertext=_encode(r.ciphertext),
            )
            for r in rows
        ],
        last_seq=account.last_seq,
        cursor=device.cursor,
        has_more=has_more,
        snapshot_seq=snapshot.seq if snapshot else None,
    )


def ack(db: Session, account: models.Account, device: models.Device, cursor: int) -> schemas.SyncStatus:
    if cursor > account.last_seq:
        raise HTTPException(status_code=422, detail="Cursor is ahead of the server.")
    device.cursor = max(device.cursor, cursor)
    db.flush()
    prune(db, account)
    db.commit()
    return status(db, account, device)


def prune(db: Session, account: models.Account) -> int:
    """Delete ops every active device has applied, and ops past their TTL."""
    now = datetime.now(timezone.utc)
    inactive_before = now - timedelta(days=config.SYNC_INACTIVE_DEVICE_DAYS)
    min_cursor = db.execute(
        select(func.min(models.Device.cursor)).where(
            models.Device.account_id == account.id,
            models.Device.revoked_at.is_(None),
            models.Device.last_seen_at >= inactive_before,
        )
    ).scalar()
    conditions = [models.SyncOp.expires_at < now]
    if min_cursor is not None:
        conditions.append(models.SyncOp.seq <= min_cursor)
    from sqlalchemy import or_

    result = db.execute(delete(models.SyncOp).where(models.SyncOp.account_id == account.id, or_(*conditions)))
    return result.rowcount or 0


def status(db: Session, account: models.Account, device: models.Device) -> schemas.SyncStatus:
    pending = db.execute(
        select(func.count())
        .select_from(models.SyncOp)
        .where(models.SyncOp.account_id == account.id, models.SyncOp.seq > device.cursor)
    ).scalar_one()
    snapshot = db.get(models.SyncSnapshot, account.id)
    active = db.execute(
        select(func.count())
        .select_from(models.Device)
        .where(models.Device.account_id == account.id, models.Device.revoked_at.is_(None))
    ).scalar_one()
    return schemas.SyncStatus(
        last_seq=account.last_seq,
        cursor=device.cursor,
        pending_for_device=pending,
        snapshot_seq=snapshot.seq if snapshot else None,
        vault_enabled=account.vault_enabled,
        active_devices=active,
    )


def put_snapshot(
    db: Session, account: models.Account, device: models.Device, body: schemas.SnapshotIn
) -> schemas.SnapshotOut:
    if not account.vault_enabled:
        raise HTTPException(status_code=409, detail="The encrypted vault is turned off for this account.")
    if body.seq > account.last_seq:
        raise HTTPException(status_code=422, detail="Snapshot seq is ahead of the server.")
    raw = _decode(body.ciphertext, config.SYNC_MAX_SNAPSHOT_BYTES)
    snapshot = db.get(models.SyncSnapshot, account.id)
    if snapshot and snapshot.seq > body.seq:
        raise HTTPException(status_code=409, detail="A newer snapshot already exists.")
    if snapshot is None:
        snapshot = models.SyncSnapshot(account_id=account.id)
        db.add(snapshot)
    snapshot.seq = body.seq
    snapshot.schema_version = body.schema_version
    snapshot.device_id = device.id
    snapshot.ciphertext = raw
    snapshot.size_bytes = len(raw)
    db.commit()
    db.refresh(snapshot)
    return get_snapshot(db, account)


def get_snapshot(db: Session, account: models.Account) -> schemas.SnapshotOut:
    snapshot = db.get(models.SyncSnapshot, account.id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No snapshot has been uploaded for this account yet.")
    return schemas.SnapshotOut(
        seq=snapshot.seq,
        schema_version=snapshot.schema_version,
        device_id=snapshot.device_id,
        ciphertext=_encode(snapshot.ciphertext),
        updated_at=snapshot.updated_at,
    )


def delete_snapshot(db: Session, account: models.Account) -> None:
    db.execute(delete(models.SyncSnapshot).where(models.SyncSnapshot.account_id == account.id))
    db.commit()
