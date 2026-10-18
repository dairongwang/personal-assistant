"""Release manifests and client gating.

The deploy pipeline publishes ``apps/web/release.json`` here after the new PWA build is on the
CDN. Clients compare themselves against the latest manifest:

* ``version`` newer than the client's         → update available (the service worker fetches it;
                                                installed automatically on reopen by default)
* ``critical`` or client < min_supported_*    → update **required**: the client blocks until the
                                                new version is active; sync calls answer 426.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

import config
import models
import schemas
from versioning import compare

log = logging.getLogger("releases")


def fallback_manifest() -> schemas.ReleaseManifest | None:
    """Before the first publish (fresh dev database), use the checked-in release.json."""
    try:
        data = json.loads(config.RELEASE_FILE.read_text(encoding="utf-8"))
        return schemas.ReleaseManifest.model_validate(data)
    except (OSError, ValueError) as exc:
        log.debug("No fallback release manifest (%s)", exc)
        return None


def latest(db: Session, channel: str = "stable") -> schemas.ReleaseManifest | None:
    row = (
        db.execute(
            select(models.Release).where(models.Release.channel == channel).order_by(models.Release.published_at.desc())
        )
        .scalars()
        .first()
    )
    if row is None:
        return fallback_manifest()
    return schemas.ReleaseManifest(
        channel=row.channel,
        version=row.version,
        schema_version=row.schema_version,
        min_supported_version=row.min_supported_version,
        min_supported_schema=row.min_supported_schema,
        critical=row.critical,
        notes=row.notes,
        published_at=row.published_at,
    )


def publish(db: Session, manifest: schemas.ReleaseManifest) -> schemas.ReleaseManifest:
    if compare(manifest.min_supported_version, manifest.version) > 0:
        raise HTTPException(status_code=422, detail="min_supported_version cannot be newer than version")
    if manifest.min_supported_schema > manifest.schema_version:
        raise HTTPException(status_code=422, detail="min_supported_schema cannot be newer than schema_version")
    existing = db.execute(
        select(models.Release).where(
            models.Release.channel == manifest.channel, models.Release.version == manifest.version
        )
    ).scalar_one_or_none()
    row = existing or models.Release(channel=manifest.channel, version=manifest.version)
    row.schema_version = manifest.schema_version
    row.min_supported_version = manifest.min_supported_version
    row.min_supported_schema = manifest.min_supported_schema
    row.critical = manifest.critical
    row.notes = manifest.notes
    row.published_at = manifest.published_at or datetime.now(timezone.utc)
    db.add(row)
    db.commit()
    return latest(db, manifest.channel)


def check_client(
    manifest: schemas.ReleaseManifest | None, client_version: str, client_schema: int
) -> schemas.ClientCheck:
    if manifest is None:
        return schemas.ClientCheck(
            latest=None,
            client_version=client_version,
            client_schema=client_schema,
            update_available=False,
            update_required=False,
            reason=None,
        )
    newer = compare(manifest.version, client_version) > 0
    required = False
    reason = None
    if client_version and compare(client_version, manifest.min_supported_version) < 0:
        required, reason = True, f"Versions older than {manifest.min_supported_version} are no longer supported."
    elif client_schema and client_schema < manifest.min_supported_schema:
        required, reason = True, "Your app uses an outdated data format and must be updated before it can sync."
    elif manifest.critical and newer:
        required, reason = True, f"Version {manifest.version} is a required update."
    return schemas.ClientCheck(
        latest=manifest,
        client_version=client_version,
        client_schema=client_schema,
        update_available=newer,
        update_required=required,
        reason=reason,
    )


def enforce_supported(db: Session, client_version: str | None, client_schema: int | None) -> None:
    """Raise 426 Upgrade Required for clients the latest manifest no longer supports."""
    if not client_version and not client_schema:
        return
    check = check_client(latest(db), client_version or "", client_schema or 0)
    if check.update_required:
        raise HTTPException(
            status_code=426,
            detail=f"Please update the app. {check.reason}",
            headers={"X-Min-Supported-Version": check.latest.min_supported_version if check.latest else ""},
        )
