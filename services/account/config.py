"""Runtime configuration of the Account & Sync Service (environment variables, sane defaults).

Standard library only, identical on Windows/macOS/Linux/containers. A ``.env`` file next to this
module is loaded if present (KEY=VALUE lines; real environment variables take precedence).

What this service is — and is not:
  * It holds **account + billing rows** and a **transient, end-to-end-encrypted sync relay**.
  * It never holds plaintext user content. There are no task/event tables here by design
    (see tests/test_privacy.py, which fails the build if one is added).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SERVICE_DIR.parent.parent


def parse_dotenv_line(raw: str) -> tuple[str, str] | None:
    """``KEY=value`` → (KEY, value). Accepts ``export KEY=…``, quoted values (kept verbatim) and
    trailing ``# comments`` after unquoted values; returns None for blank/comment lines."""
    line = raw.strip().lstrip("\ufeff")  # BOM from some Windows editors
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    if key.startswith("export "):
        key = key[7:].strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    else:
        value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
    return (key, value) if key else None


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_dotenv_line(raw)
        if parsed:
            os.environ.setdefault(parsed[0], parsed[1])


_load_dotenv(SERVICE_DIR / ".env")


def _bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


_ENV_ALIASES = {
    "production": "prod",
    "prd": "prod",
    "live": "prod",
    "development": "dev",
    "local": "dev",
    "testing": "test",
    "uat": "qa",
    "stage": "staging",
    "preprod": "staging",
}


def normalize_env(raw: str | None) -> str:
    """APP_ENV → one of dev | test | qa | staging | prod. Case-insensitive, tolerant of
    surrounding quotes/whitespace/inline comments and of common aliases (``Production`` →
    ``prod``), so that "prod" never silently turns into "not prod"."""
    value = (raw or "").split("#", 1)[0].strip().strip("\"'").strip().lower()
    return _ENV_ALIASES.get(value, value) or "dev"


SERVICE_VERSION = "1.0.0"
ENV = normalize_env(os.environ.get("APP_ENV"))  # dev | test | qa | staging | prod
IS_PROD = ENV == "prod"

# ---- Database -----------------------------------------------------------------------
# PostgreSQL in every real environment (Azure Database for PostgreSQL in the cloud):
#   DATABASE_URL=postgresql+psycopg://user:password@host:5432/assistant
# SQLite is accepted for quick local hacking and for the unit tests only.
DATA_DIR = Path(os.environ.get("DATA_DIR", SERVICE_DIR / "data")).resolve()
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'account.db').as_posix()}")
IS_POSTGRES = DATABASE_URL.startswith("postgresql")
# Run Alembic migrations at startup for PostgreSQL too (SQLite always does). Fine for a single
# replica; multi-replica deployments migrate in the pipeline instead (see Dockerfile CMD).
AUTO_MIGRATE = _bool(os.environ.get("AUTO_MIGRATE"), False)

# ---- Auth ---------------------------------------------------------------------------
# Secret used to sign access tokens. MUST be set in staging/prod (a random one is generated for
# dev so that restarting the dev server simply signs everybody out).
TOKEN_SECRET = os.environ.get("TOKEN_SECRET") or secrets.token_urlsafe(32)
TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", str(30 * 24 * 3600)))
if ENV in ("staging", "prod") and not os.environ.get("TOKEN_SECRET"):
    raise RuntimeError(
        f"APP_ENV={ENV} requires TOKEN_SECRET (it signs every session token). Set it in the environment or in "
        f'{SERVICE_DIR / ".env"}, e.g.:  python -c "import secrets; print(secrets.token_urlsafe(48))"  '
        "(python dev.py run/serve generate one for you)."
    )

# ---- Releases (what clients compare themselves against) -------------------------------
# Token the deploy pipeline uses to publish a release manifest after the PWA is on the CDN.
RELEASE_ADMIN_TOKEN = os.environ.get("RELEASE_ADMIN_TOKEN", "")
# Fallback manifest used until the first release is published (dev convenience).
RELEASE_FILE = Path(os.environ.get("RELEASE_FILE", PROJECT_DIR / "apps" / "web" / "release.json"))

# ---- Sync relay ---------------------------------------------------------------------
SYNC_OP_TTL_DAYS = int(os.environ.get("SYNC_OP_TTL_DAYS", "30"))  # hard TTL for relayed ciphertext
SYNC_INACTIVE_DEVICE_DAYS = int(os.environ.get("SYNC_INACTIVE_DEVICE_DAYS", "30"))
SYNC_MAX_OP_BYTES = int(os.environ.get("SYNC_MAX_OP_BYTES", str(64 * 1024)))
SYNC_MAX_SNAPSHOT_BYTES = int(os.environ.get("SYNC_MAX_SNAPSHOT_BYTES", str(16 * 1024 * 1024)))
SYNC_MAX_BATCH = int(os.environ.get("SYNC_MAX_BATCH", "500"))

# ---- Billing ------------------------------------------------------------------------
# Provider adapter: "stub" (built-in sandbox, no external dependency) today; "paddle"/"stripe"
# adapters plug in behind the same interface (billing.py).
BILLING_PROVIDER = os.environ.get("BILLING_PROVIDER", "stub")
BILLING_WEBHOOK_SECRET = os.environ.get("BILLING_WEBHOOK_SECRET", "dev-webhook-secret")
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "14"))
GRACE_DAYS = int(os.environ.get("GRACE_DAYS", "7"))

# ---- Membership ---------------------------------------------------------------------
# Membership types (membership.py, strategy pattern): anonymous (no account) · free (account,
# no subscription) · paid (Pro/Lifetime) · admin (production support). Free memberships may
# keep at most this many *active* (not done) tasks; paid and admin are unlimited.
FREE_MAX_ACTIVE_TASKS = int(os.environ.get("FREE_MAX_ACTIVE_TASKS", "3"))
# Comma-separated e-mails that get the administrator role when they register or sign in
# (bootstrap for a fresh deployment; further admins are promoted from the admin panel).
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
# Create the fake test accounts (seed.py) at startup. Ignored in prod.
SEED_FAKE_ACCOUNTS = _bool(os.environ.get("SEED_FAKE_ACCOUNTS"), False) and ENV != "prod"

# ---- API docs ------------------------------------------------------------------------
# /api/docs, /api/redoc and /api/openapi.json need an administrator account whenever
# APP_ENV=prod — always, no variable can switch that off (docs_gate.py). In every other
# environment they are open; DOCS_REQUIRE_ADMIN=true enables the gate there too (e.g. to try it).
DOCS_REQUIRE_ADMIN = IS_PROD or _bool(os.environ.get("DOCS_REQUIRE_ADMIN"), False)

# ---- Web ---------------------------------------------------------------------------
# Comma-separated origins allowed to call the API (the PWA's origin when hosted separately,
# e.g. https://<name>.azurestaticapps.net). Empty = same-origin only.
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
# If set to the Angular build output (apps/web/dist/web/browser), the API also serves the PWA —
# one process for LAN testing and small self-hosted setups. In Azure the CDN serves it instead.
WEB_DIST = Path(os.environ["WEB_DIST"]).resolve() if os.environ.get("WEB_DIST") else None
# Where the app lives while developing (`python dev.py run` sets this to the Angular dev server);
# requests for "/" on the API are redirected there so an old bookmark/tab still ends up in the app.
DEV_WEB_URL = os.environ.get("DEV_WEB_URL", "").strip()
