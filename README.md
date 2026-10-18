# Personal Assistant

Tasks, events and a daily summary in a **local-first Progressive Web App**: one codebase for
Windows, macOS, Linux, phones and tablets (installable from the browser). Your data stays on your
device; an optional account adds **end-to-end-encrypted sync**. The app **updates itself**.

| Layer | Tech |
|---|---|
| PWA | Angular 21, `@angular/service-worker`, Dexie (IndexedDB), WebCrypto — `apps/web` |
| API | Python 3.10+ / FastAPI, SQLAlchemy, Alembic — `services/account` (accounts, billing, encrypted sync relay, release manifests — **no user content**) |
| Database | PostgreSQL 16 (SQLite automatically for local development) |
| Cloud | Azure Static Web Apps + Container Apps + PostgreSQL — `infra/azure`, `.github/workflows` |

## Quick start

Requirements: **Python 3.10–3.14**, **Node.js 22.12+** (20.19+/24+ also fine). PostgreSQL is optional
locally.

```bash
python dev.py setup     # venv + pip + npm ci
python dev.py run       # API http://localhost:8000/api/docs · app http://localhost:4200/  (Ctrl+C stops both)
```

`run` prints the URLs once both servers answer and keeps running; use a second terminal for other
commands. Windows: `py -3 dev.py …` if `python` is not on PATH.

| Command | Purpose |
|---|---|
| `python dev.py serve` | Production build served by the API at http://localhost:8000/ — service worker on (updates, offline, install), reachable from phones on your network |
| `python dev.py db init` | Local PostgreSQL cluster in `.pgdata` (picks a free port if 5432 is taken and remembers it in `services/account/.env`); then `db upgrade`, `db start`, `db stop`, `db status` |
| `python dev.py seed` / `promote <email>` | (Re)create the fake test accounts (one per membership type — `run` does this automatically) / make an account an administrator |
| `python dev.py test` | Python + Angular unit tests (`--postgres` to run the API tests against PostgreSQL) |
| `python dev.py e2e` | End-to-end in a real browser (uses installed Edge/Chrome or bundled Chromium; `E2E_BROWSER=…` to choose) |
| `python dev.py lint` / `format` / `check` | ruff, prettier, tsc |
| `python dev.py stop` | Kill leftover API / dev-server processes |
| `python dev.py release 1.2.3 [--critical]` | Bump `apps/web/release.json` |
| `python dev.py docker` | PostgreSQL + API + built PWA via Docker Compose → http://localhost:8000/ |

## Using the app

- **Works without an account.** Data lives in the browser's IndexedDB; install the app so it is
  never evicted. `?` turns on help mode; every control has a tooltip (hover, or press-and-hold on
  touch screens).
- **Auto-update checkbox** (header): on by default — new versions download in the background and
  install when you reopen the app or as soon as you are not mid-edit. Off — you are told when a
  version is ready and install it from the **↻ Update** button, whose tooltip always shows what is
  pending. Critical releases install either way.
- **Log in / Register** (header, shown while you have no account on this device): create a free
  account or sign in to an existing one. Once signed in the header shows **☁ Sync** (account,
  free 14-day Pro trial / subscription → encrypted sync between devices, device list, *Reset from
  server*, delete account) and **Log out** (your data stays on the device).
- **Memberships** (header badge, e.g. `Free · 2/3`): *anonymous* (no account) and *free*
  accounts keep up to **3 active tasks**; *paid* (Pro/Lifetime) and *administrator* accounts are
  unlimited and get every feature. Administrators also get the **⚙ Admin** panel (accounts,
  memberships, devices, audit log) for production support. Details and the test accounts
  (`free@example.com` / `pro@example.com` / `admin@example.com` …):
  [docs/MEMBERSHIP.md](docs/MEMBERSHIP.md).

## How updates and data work

1. `git tag v1.2.3` (matching `apps/web/release.json`) → the pipeline rolls the API (runs DB
   migrations), uploads the PWA to the CDN and publishes the release manifest.
2. The service worker on each device detects the new build, downloads it and installs it per the
   checkbox above. `critical: true` or a raised `minSupportedVersion` forces the update; the API
   answers `426` to clients that are too old.
3. Local data structures are versioned (`SCHEMA_VERSION` + one migration table); sync operations
   carry their schema version and are up-cast; data from a newer schema is never applied by an
   older app.
4. Sync: the server assigns the order of encrypted operations (it is the master of ordering and
   keeps an encrypted reference copy) but never sees content; a device can rebuild its cache from
   the server at any time.

Details: [docs/UPDATES_AND_MIGRATIONS.md](docs/UPDATES_AND_MIGRATIONS.md) ·
memberships & admin: [docs/MEMBERSHIP.md](docs/MEMBERSHIP.md) ·
deployment: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) · phones/tablets:
[docs/MOBILE_DEPLOYMENT.md](docs/MOBILE_DEPLOYMENT.md) · product design:
[docs/PLATFORM_DESIGN.md](docs/PLATFORM_DESIGN.md).

## Project layout

```
apps/web/            Angular PWA (release.json, ngsw-config.json, src/app/core/{db,data,sync,update,account,membership,admin,ui})
services/account/    FastAPI service (membership.py strategies, admin.py, seed.py), Alembic migrations, tests, Dockerfile
e2e/                 Playwright end-to-end tests (updates, sync, privacy)
infra/azure/         Bicep: Static Web App + Container App + PostgreSQL
.github/workflows/   ci.yml (Win/macOS/Linux) · deploy.yml (tag → Azure)
dev.py               cross-platform developer commands
```

API (`/api/v1`, docs at `/api/docs` — open in dev/test/qa/staging, administrator sign-in in
prod): `auth/*`, `me`, `membership`, `billing/*`, `sync/*` (ciphertext only), `release/*`,
`admin/*` (administrator accounts), `health`.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `http://localhost:4200/` unreachable after `run` | Wait for the "API: … App: …" lines; if the dev server exits, the reason is printed (usually Node.js < 22.12). Try `http://127.0.0.1:4200/`. Leftover processes: `python dev.py stop`. |
| Old page keeps loading / `410` or `404` for `/sw.js`, `/update/status`, `/tasks` | A previous app version is still installed in the browser. Reload once — the server replaces the old service worker and clears its caches. |
| `WinError 2` on `db init` | PostgreSQL tools not found. `winget install PostgreSQL.PostgreSQL.16` (found automatically afterwards) or set `PG_BIN`; or just use SQLite (`run` does this by default). |
| `db init` finds another PostgreSQL on 5432 | It picks a free port automatically (`PGPORT` saved in `services/account/.env`). To use that server instead, set `DATABASE_URL=postgresql+psycopg://postgres:PASSWORD@127.0.0.1:5432/assistant` there. |
| `table accounts already exists` on `db upgrade` | Run it again — databases created by older versions are adopted automatically. |
| `python dev.py e2e`: "Executable doesn't exist" | No Playwright browser yet; the runner uses Edge/Chrome if installed, else downloads Chromium once. |
| Update downloaded but not installed | Auto-update is off or a dialog is open — tap *Install* in the banner / Update dialog. |
| "Update required" stays on "Downloading…" | Manifest published before the CDN had the files; it resolves by itself, or *Reload page*. |
| Sync says it needs Pro | Start the trial in the account dialog (sandbox billing in development). |
| "Active task limit reached" | Free memberships keep 3 active tasks: finish/delete one, or upgrade (trial). Testing? Sign in as `pro@example.com` / `Pro-pass-123` (seeded by `dev.py run`). |
| `TOKEN_SECRET must be set in staging/prod` / API exits right after start | `APP_ENV` is `prod` or `staging` (check `services/account/.env`). `python dev.py run` / `serve` now generate a `TOKEN_SECRET` into `.env` automatically; when starting uvicorn yourself, set it: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `/api/docs` shows a sign-in page | `APP_ENV=prod`: the docs need an administrator. Register in the app, then `python dev.py promote you@example.com` (fake accounts are not created in prod). Any other environment leaves the docs open. |
| `Could not open requirements file … requirements-dev.txt` on `setup` | The download is incomplete; `setup` now falls back to `requirements.txt` + the test tools. Re-download if other files are missing too. |
| No **Admin** button after signing in | The account needs `role = admin`: `python dev.py promote you@example.com`, or put the e-mail in `ADMIN_EMAILS` before registering, or use `admin@example.com` / `Admin-pass-123` locally. |
