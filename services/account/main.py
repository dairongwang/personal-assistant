"""Account & Sync Service — the only server component of the Personal Assistant platform.

    /api/v1/auth/*      register · login · logout · sign out everywhere
    /api/v1/me          account, devices, entitlements, settings, delete
    /api/v1/membership  the membership types (anonymous · free · paid · admin) and their limits
    /api/v1/billing/*   checkout · sandbox · webhooks · trial
    /api/v1/admin/*     stats · accounts · membership/role changes · audit   (administrators)
    /api/v1/sync/*      push · pull · ack · status · snapshot   (ciphertext relay)
    /api/v1/release/*   manifest · check · publish · history   (client update policy)
    /api/v1/health

It stores account + billing rows and relays end-to-end-encrypted sync operations. It holds no
user content — the product logic and data live in the PWA (apps/web). Optionally serves the
built PWA (WEB_DIST) so one process can host everything on a LAN.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

import accounts
import admin
import auth
import billing
import config
import docs_gate
import membership
import models
import releases
import schemas
import sync
from database import SessionLocal, get_db, init_db

log = logging.getLogger("account")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # What this process decided from its environment — the first thing to check when "prod"
    # behaviour (docs gate, no sandbox, no fake accounts) seems to be missing.
    print(
        f"Personal Assistant API v{config.SERVICE_VERSION} · APP_ENV={config.ENV} · "
        f"database={'postgresql' if config.IS_POSTGRES else 'sqlite'} · "
        f"api docs={'administrators only' if config.DOCS_REQUIRE_ADMIN else 'open'}",
        flush=True,
    )
    if config.SEED_FAKE_ACCOUNTS:  # dev/test convenience: the fake accounts are there after `dev.py run`
        import seed

        with SessionLocal() as db:
            seed.seed(db, log=lambda fmt, *args: print(fmt % args, flush=True))
    yield


app = FastAPI(
    title="Personal Assistant — Account & Sync Service",
    version=config.SERVICE_VERSION,
    description=__doc__,
    lifespan=lifespan,
    # Docs live under /api/* and are served by docs_gate (open in dev/test/qa/staging, admin-only in prod).
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Min-Supported-Version"],
    )

api = APIRouter(prefix="/api/v1")


def client_headers(
    x_client_version: str | None = Header(default=None),
    x_schema_version: int | None = Header(default=None),
) -> tuple[str | None, int | None]:
    return x_client_version, x_schema_version


def _device_out(d: models.Device, current_id: str) -> schemas.DeviceOut:
    return schemas.DeviceOut(
        id=d.id,
        platform=d.platform,
        client_version=d.client_version,
        schema_version=d.schema_version,
        cursor=d.cursor,
        name_ciphertext=d.name_ciphertext,
        last_seen_at=d.last_seen_at,
        is_current=d.id == current_id,
    )


# ==================== Auth ====================


def _register_device(db: Session, account: models.Account, info: schemas.DeviceInfo) -> models.Device:
    device = models.Device(
        account_id=account.id,
        platform=info.platform,
        client_version=info.client_version,
        schema_version=info.schema_version,
        name_ciphertext=info.name_ciphertext,
    )
    db.add(device)
    db.flush()
    return device


def _session(account: models.Account, device: models.Device) -> schemas.Session:
    return schemas.Session(
        token=auth.issue_token(account.id, device.id, device.token_generation),
        account_id=account.id,
        device_id=device.id,
        email=account.email,
        wrapped_data_key=account.wrapped_data_key,
        kdf_params=json.loads(account.kdf_params or "{}"),
        vault_enabled=account.vault_enabled,
    )


@api.post("/auth/register", response_model=schemas.Session, status_code=201, tags=["auth"])
def register(body: schemas.RegisterRequest, db: Session = Depends(get_db)):
    email = body.email.lower()
    if db.execute(select(models.Account).where(models.Account.email == email)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="An account with this e-mail already exists. Sign in instead.")
    salt = auth.new_salt()
    account = models.Account(
        email=email,
        auth_salt=salt,
        auth_hash=auth.hash_auth_key(body.auth_key, salt),
        wrapped_data_key=body.wrapped_data_key,
        kdf_params=json.dumps(body.kdf_params),
    )
    auth.apply_bootstrap_role(account)
    db.add(account)
    db.flush()
    device = _register_device(db, account, body.device)
    billing.subscription_for(db, account)
    db.commit()
    return _session(account, device)


@api.post("/auth/login", response_model=schemas.Session, tags=["auth"])
def login(body: schemas.LoginRequest, db: Session = Depends(get_db)):
    account = db.execute(select(models.Account).where(models.Account.email == body.email.lower())).scalar_one_or_none()
    if (
        not account
        or account.deleted_at
        or not auth.verify_auth_key(body.auth_key, account.auth_salt, account.auth_hash)
    ):
        raise HTTPException(status_code=401, detail="E-mail or password is incorrect.")
    auth.apply_bootstrap_role(account)
    device = _register_device(db, account, body.device)
    db.commit()
    return _session(account, device)


@api.post("/auth/logout", status_code=204, tags=["auth"])
def logout(p: auth.Principal = Depends(auth.current_principal), db: Session = Depends(get_db)):
    p.device.revoked_at = models.utcnow()
    db.commit()
    return Response(status_code=204)


@api.post("/auth/logout-everywhere", status_code=204, tags=["auth"])
def logout_everywhere(p: auth.Principal = Depends(auth.current_principal), db: Session = Depends(get_db)):
    accounts.revoke_all_devices(db, p.account)
    db.commit()
    return Response(status_code=204)


# ==================== Me ====================


@api.get("/me", response_model=schemas.Me, tags=["me"])
def me(p: auth.Principal = Depends(auth.current_principal), db: Session = Depends(get_db)):
    devices = db.execute(
        select(models.Device).where(models.Device.account_id == p.account.id, models.Device.revoked_at.is_(None))
    ).scalars()
    return schemas.Me(
        account_id=p.account.id,
        email=p.account.email,
        role=p.account.role,
        created_at=p.account.created_at,
        vault_enabled=p.account.vault_enabled,
        devices=[_device_out(d, p.device.id) for d in devices],
        entitlements=billing.entitlements(db, p.account),
    )


@api.get("/me/entitlements", response_model=schemas.Entitlements, tags=["me"])
def my_entitlements(p: auth.Principal = Depends(auth.current_principal), db: Session = Depends(get_db)):
    return billing.entitlements(db, p.account)


@api.patch("/me/settings", response_model=schemas.Me, tags=["me"])
def update_settings(
    body: schemas.AccountSettings, p: auth.Principal = Depends(auth.current_principal), db: Session = Depends(get_db)
):
    if body.vault_enabled is not None:
        p.account.vault_enabled = body.vault_enabled
        if not body.vault_enabled:
            sync.delete_snapshot(db, p.account)
    db.commit()
    return me(p, db)


@api.delete("/me/devices/{device_id}", status_code=204, tags=["me"])
def revoke_device(device_id: str, p: auth.Principal = Depends(auth.current_principal), db: Session = Depends(get_db)):
    device = db.get(models.Device, device_id)
    if not device or device.account_id != p.account.id:
        raise HTTPException(status_code=404, detail="Device not found.")
    device.revoked_at = models.utcnow()
    db.commit()
    return Response(status_code=204)


@api.delete("/me", status_code=204, tags=["me"])
def delete_account(p: auth.Principal = Depends(auth.current_principal), db: Session = Depends(get_db)):
    """Purge: relay ops, snapshot and devices go now; the account row becomes a billing tombstone."""
    accounts.audit(db, p.account, "account.purge_self", p.account)
    accounts.purge(db, p.account)
    db.commit()
    return Response(status_code=204)


# ==================== Membership ====================


@api.get("/membership", response_model=schemas.MembershipCatalog, tags=["membership"])
def membership_catalog():
    """The membership types and what each allows (public — the pricing/account screens)."""
    return membership.catalog()


@api.get("/membership/me", response_model=schemas.MembershipInfo, tags=["membership"])
def my_membership(p: auth.Principal = Depends(auth.current_principal), db: Session = Depends(get_db)):
    """The membership strategy this account resolves to right now."""
    return membership.resolve(db, p.account).describe()


# ==================== Billing ====================


@api.post("/billing/checkout", response_model=schemas.CheckoutResponse, tags=["billing"])
def checkout(body: schemas.CheckoutRequest, p: auth.Principal = Depends(auth.current_principal)):
    prov = billing.provider()
    return schemas.CheckoutResponse(
        checkout_url=prov.create_checkout(p.account, body.plan, body.return_url), provider=prov.name
    )


@api.post("/billing/trial", response_model=schemas.Entitlements, tags=["billing"])
def trial(p: auth.Principal = Depends(auth.current_principal), db: Session = Depends(get_db)):
    billing.start_trial(db, p.account)
    return billing.entitlements(db, p.account)


@api.post("/billing/webhooks/{provider_name}", tags=["billing"])
async def webhook(provider_name: str, request: Request, db: Session = Depends(get_db)):
    if provider_name != config.BILLING_PROVIDER:
        raise HTTPException(status_code=404, detail="Unknown billing provider.")
    body = await request.body()
    if not billing.provider().verify_signature(body, request.headers.get("X-Signature")):
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")
    try:
        event = schemas.StubWebhook(**json.loads(body))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Malformed webhook payload.") from exc
    processed = billing.apply_webhook(db, event, provider_name)
    return {"ok": True, "duplicate": not processed}


@api.get("/billing/sandbox", response_class=HTMLResponse, include_in_schema=False)
def sandbox_checkout(account_id: str, plan: str, return_url: str = "/"):
    """Stand-in for the provider's hosted checkout page (dev/staging only)."""
    if config.ENV == "prod":
        raise HTTPException(status_code=404)
    confirm = "/api/v1/billing/sandbox/confirm?" + urlencode(
        {"account_id": account_id, "plan": plan, "return_url": return_url}
    )
    return f"""<!doctype html><meta charset=utf-8><title>Sandbox checkout</title>
<body style="font-family:system-ui;max-width:460px;margin:60px auto;line-height:1.5">
<h2>Sandbox checkout</h2><p>This stands in for the payment provider's hosted page. No card is charged.</p>
<p>Plan: <strong>{plan}</strong></p>
<form method="post" action="{confirm}"><button style="padding:10px 18px;font-size:16px">Pay (sandbox)</button></form>
<p><a href="{return_url}">Cancel</a></p></body>"""


@api.post("/billing/sandbox/confirm", include_in_schema=False)
def sandbox_confirm(account_id: str, plan: str, return_url: str = "/", db: Session = Depends(get_db)):
    if config.ENV == "prod":
        raise HTTPException(status_code=404)
    body = billing.webhook_body_for_sandbox(account_id, plan, f"evt_{uuid.uuid4().hex}")
    event = schemas.StubWebhook(**json.loads(body))
    billing.apply_webhook(db, event, "stub")
    return RedirectResponse(url=return_url, status_code=303)


# ==================== Sync (ciphertext relay) ====================


def sync_principal(
    p: auth.Principal = Depends(auth.current_principal),
    headers: tuple = Depends(client_headers),
    db: Session = Depends(get_db),
) -> auth.Principal:
    releases.enforce_supported(db, *headers)
    billing.require_feature(db, p.account, "sync")
    version, schema_version = headers
    if version:
        p.device.client_version = version
    if schema_version:
        p.device.schema_version = schema_version
    return p


@api.get("/sync/status", response_model=schemas.SyncStatus, tags=["sync"])
def sync_status(p: auth.Principal = Depends(sync_principal), db: Session = Depends(get_db)):
    return sync.status(db, p.account, p.device)


@api.post("/sync/push", response_model=schemas.PushResponse, tags=["sync"])
def sync_push(body: schemas.PushRequest, p: auth.Principal = Depends(sync_principal), db: Session = Depends(get_db)):
    return sync.push(db, p.account, p.device, body)


@api.get("/sync/pull", response_model=schemas.PullResponse, tags=["sync"])
def sync_pull(
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    p: auth.Principal = Depends(sync_principal),
    db: Session = Depends(get_db),
):
    return sync.pull(db, p.account, p.device, since, limit)


@api.post("/sync/ack", response_model=schemas.SyncStatus, tags=["sync"])
def sync_ack(body: schemas.AckRequest, p: auth.Principal = Depends(sync_principal), db: Session = Depends(get_db)):
    return sync.ack(db, p.account, p.device, body.cursor)


@api.get("/sync/snapshot", response_model=schemas.SnapshotOut, tags=["sync"])
def snapshot_get(p: auth.Principal = Depends(sync_principal), db: Session = Depends(get_db)):
    return sync.get_snapshot(db, p.account)


@api.put("/sync/snapshot", response_model=schemas.SnapshotOut, tags=["sync"])
def snapshot_put(body: schemas.SnapshotIn, p: auth.Principal = Depends(sync_principal), db: Session = Depends(get_db)):
    billing.require_feature(db, p.account, "vault")
    return sync.put_snapshot(db, p.account, p.device, body)


# ==================== Releases ====================


@api.get("/release/manifest", response_model=schemas.ReleaseManifest | None, tags=["release"])
def release_manifest(channel: str = "stable", db: Session = Depends(get_db)):
    return releases.latest(db, channel)


@api.get("/release/check", response_model=schemas.ClientCheck, tags=["release"])
def release_check(
    version: str = Query(default=""),
    schema: int = Query(default=0, alias="schema", ge=0),
    channel: str = "stable",
    db: Session = Depends(get_db),
):
    """Is this client version still fine? (update_available / update_required)."""
    return releases.check_client(releases.latest(db, channel), version, schema)


@api.post("/release/publish", response_model=schemas.ReleaseManifest, status_code=201, tags=["release"])
def release_publish(
    body: schemas.ReleaseManifest,
    db: Session = Depends(get_db),
    admin_user: auth.Principal | None = Depends(auth.require_admin),
):
    """Called by the deploy pipeline once the new PWA build is live on the CDN (X-Admin-Token),
    or by an administrator account (e.g. to mark a release critical during an incident)."""
    log.info("Publishing release %s (critical=%s)", body.version, body.critical)
    accounts.audit(
        db, admin_user.account if admin_user else None, "release.publish", version=body.version, critical=body.critical
    )
    return releases.publish(db, body)


@api.get("/release/history", response_model=list[schemas.ReleaseManifest], tags=["release"])
def release_history(channel: str = "stable", limit: int = Query(default=20, le=100), db: Session = Depends(get_db)):
    rows = db.execute(
        select(models.Release)
        .where(models.Release.channel == channel)
        .order_by(models.Release.published_at.desc())
        .limit(limit)
    ).scalars()
    return [
        schemas.ReleaseManifest(
            channel=r.channel,
            version=r.version,
            schema_version=r.schema_version,
            min_supported_version=r.min_supported_version,
            min_supported_schema=r.min_supported_schema,
            critical=r.critical,
            notes=r.notes,
            published_at=r.published_at,
        )
        for r in rows
    ]


# ==================== Meta ====================


@api.get("/health", tags=["meta"])
def health():
    return {
        "status": "ok",
        "service": "account",
        "version": config.SERVICE_VERSION,
        "environment": config.ENV,
        "database": "postgresql" if config.IS_POSTGRES else "sqlite",
        "docs": "admin-only" if config.DOCS_REQUIRE_ADMIN else "open",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


api.include_router(admin.router)
app.include_router(api)
docs_gate.install(app)  # /api/docs · /api/redoc · /api/openapi.json


# ==================== Legacy paths (previous app versions) ====================
# Browsers that still run an older build of the app (its service worker caches the old page)
# keep requesting the old endpoints. Instead of 404s, heal them: /sw.js becomes a "safety"
# service worker that unregisters itself, clears the old caches and reloads every open tab,
# so the next load gets the current app. The old API paths answer 410 Gone with a clear text.

LEGACY_SAFETY_WORKER = """// Safety service worker: replaces an outdated one, cleans up and reloads the app.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
    await self.registration.unregister();
    const clients = await self.clients.matchAll({ type: 'window' });
    for (const client of clients) client.navigate(client.url);
  })());
});
"""


@app.get("/sw.js", include_in_schema=False)
def legacy_service_worker():
    return Response(LEGACY_SAFETY_WORKER, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.api_route(
    "/{legacy:path}",
    methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    include_in_schema=False,
)
def legacy_paths(legacy: str):
    """Catch-all (runs after the real routes and, when WEB_DIST is set, is never reached for
    files the PWA mount serves — the mount is matched first)."""
    head = legacy.split("/", 1)[0]
    if head in ("update", "tasks", "events", "today", "health", "js", "css", "icons", "manifest.webmanifest"):
        raise HTTPException(
            status_code=410,
            detail="This address belongs to an old version of the app. Reload the page to load the current version.",
        )
    if legacy in ("", "index.html"):
        if config.DEV_WEB_URL:
            return RedirectResponse(url=config.DEV_WEB_URL, status_code=307)
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Personal Assistant API</title>"
            "<body style='font-family:system-ui;max-width:560px;margin:60px auto;line-height:1.5'>"
            "<h2>Personal Assistant — API only</h2>"
            "<p>This process serves the API (<a href='/api/docs'>/api/docs</a>). The web app is not served "
            "from here because <code>WEB_DIST</code> is not set.</p>"
            "<ul><li><code>python dev.py run</code> → app at <a href='http://localhost:4200/'>http://localhost:4200/</a></li>"
            "<li><code>python dev.py serve</code> → production build served from this address</li></ul></body>"
        )
    raise HTTPException(status_code=404, detail="Not found")


# ==================== Optional: serve the built PWA ====================
# In Azure the Static Web App/CDN serves apps/web; locally (and for small self-hosted setups)
# WEB_DIST lets this process serve it from the same origin. ngsw.json and index.html must not
# be cached by intermediaries so the service worker can detect new builds.
if config.WEB_DIST and config.WEB_DIST.is_dir():
    from starlette.exceptions import HTTPException as StarletteHTTPException

    class PwaStatic(StaticFiles):
        async def get_response(self, path: str, scope):
            try:
                response = await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                if exc.status_code != 404:
                    raise
                path = "index.html"  # single-page app: unknown routes render the app shell
                response = await super().get_response(path, scope)
            name = path.rsplit("/", 1)[-1]
            hashed = (
                "-" in name and name.rsplit(".", 1)[0].rsplit("-", 1)[-1].isalnum() and len(name.rsplit("-", 1)[-1]) > 8
            )
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if hashed else "no-cache"
            return response

    # The catch-all above would shadow the mount; drop it (the mount serves index.html for unknown paths).
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/{legacy:path}"]
    app.mount("/", PwaStatic(directory=config.WEB_DIST, html=True), name="pwa")
