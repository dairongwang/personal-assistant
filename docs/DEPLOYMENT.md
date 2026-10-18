# Deployment

Three deployable pieces: the **PWA** (static files), the **Account & Sync Service** (one
container) and **PostgreSQL**. Application updates reach devices through the PWA's service
worker; see [UPDATES_AND_MIGRATIONS.md](UPDATES_AND_MIGRATIONS.md) for the release flow.

| Target | PWA | API | Database | How |
|---|---|---|---|---|
| **Azure (production)** | Static Web App (CDN) | Container App (linked backend under `/api`) | Azure Database for PostgreSQL Flexible Server | `infra/azure/main.bicep` + `.github/workflows/deploy.yml` |
| Docker (any box, NAS) | served by the API container (`WEB_DIST`) | container | `postgres:16` container | `docker compose up --build` |
| Developer machine | `python dev.py serve` (built) or `run` (dev server) | venv | local cluster (`dev.py db`) or Docker | `dev.py` |

## Azure

1. **Resources** (one-off):
   ```bash
   az group create -n pa-prod -l westeurope
   az deployment group create -g pa-prod -f infra/azure/main.bicep \
     -p containerImage=ghcr.io/<you>/personal-assistant/account-service:latest \
        pgPassword=… tokenSecret=… releaseAdminToken=… billingWebhookSecret=…
   ```
   The Static Web App's **linked backend** routes `/api/*` to the Container App, so the PWA and
   the API share one origin — no CORS, and `X-Client-Version` headers pass straight through.
2. **GitHub secrets/vars:** `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` (OIDC),
   `AZURE_STATIC_WEB_APPS_API_TOKEN`, `RELEASE_ADMIN_TOKEN`; vars `AZURE_RESOURCE_GROUP`,
   `AZURE_CONTAINER_APP`.
3. **Release:** `python dev.py release 1.0.5`, commit, `git tag v1.0.5 && git push --tags`.
   `deploy.yml` refuses a tag that does not match `release.json`, then: image → Container App
   (start command runs `alembic upgrade head`) → health check → PWA upload → manifest publish.

Caching on the CDN comes from `apps/web/staticwebapp.config.json`: `index.html`, `ngsw.json`,
`ngsw-worker.js` are `no-cache`; hashed bundles are immutable. The `navigationFallback` serves
the app shell for deep links.

Secrets: `TOKEN_SECRET` (access tokens — rotating it signs everyone out), `RELEASE_ADMIN_TOKEN`
(manifest publishing), `BILLING_WEBHOOK_SECRET` (provider → API), database password. The Bicep
template stores them as Container App secrets; use Key Vault references for a hardened setup.

API docs: in `APP_ENV=prod` `/api/docs` (and `/api/redoc`, `/api/openapi.json`) always ask for an
administrator sign-in; every other environment leaves them open (`DOCS_REQUIRE_ADMIN=true` gates
them there too). Check `/api/v1/health` → `"environment"` / `"docs"` to see what a process decided.

Administrators: pass `adminEmails=you@example.com` to the Bicep deployment (→ `ADMIN_EMAILS`),
then register with that e-mail in the app — the account gets the administrator role and the
**Admin** panel. Further administrators are promoted from the panel. The fake test accounts are
never created in `APP_ENV=prod` (see [MEMBERSHIP.md](MEMBERSHIP.md)).

Billing provider: `BILLING_PROVIDER=stub` is the built-in sandbox (hidden in `APP_ENV=prod`).
Real providers implement `BillingProvider` in `services/account/billing.py` (checkout URL +
webhook signature) and post to `/api/v1/billing/webhooks/<provider>`.

## Docker (self-hosted, everything in one place)

```bash
cp services/account/.env.example .env   # set TOKEN_SECRET, RELEASE_ADMIN_TOKEN, POSTGRES_PASSWORD
docker compose up --build -d            # http://<host>:8000/
```

The image builds the PWA (Node stage) and the API serves it from `/web` with the same caching
rules as the CDN. Publishing a release here = rebuilding the image and starting it (`alembic
upgrade head` runs first); browsers pick up the new build exactly as with the CDN. Installable
PWA + service worker require HTTPS (or `localhost`): put Caddy/Traefik/Tailscale in front.

## Developer machine

```bash
python dev.py setup
python dev.py run        # API with auto-reload + Angular dev server (no service worker)
python dev.py serve      # production build served by the API: updates, offline, install prompt
```

Database: `run`/`serve`/`db upgrade` use the local PostgreSQL cluster when it is running and fall back to
SQLite otherwise (a notice is printed). `python dev.py db init` creates the cluster in
`.pgdata/` with the PostgreSQL tools found on PATH, in `PG_BIN`, or in the default install
folders (`C:\Program Files\PostgreSQL\<ver>\bin`, Homebrew, `/usr/lib/postgresql/<ver>/bin`);
`python dev.py db status` shows what was detected. When port 5432 already belongs to another
PostgreSQL (the installer's service), `db init` picks a free port and stores `PGPORT` in
`services/account/.env`, which `dev.py` reads for every later command. `python dev.py docker` is
the alternative. Schema changes:
`python dev.py db revision "add column"` → review the file in `services/account/alembic/versions/`
→ `python dev.py db upgrade`. The test-suite runs the full migration chain against PostgreSQL
(`python dev.py test --postgres`, and CI).

## Operations

* **Health:** `GET /api/v1/health` (liveness/readiness probes in Bicep, `HEALTHCHECK` in the image).
* **Backups:** PostgreSQL automated backups (14 days in the template). User content is not on the
  server; the relay is transient and the vault snapshot is re-uploaded by clients.
* **Rolling back the PWA:** publish a manifest whose `version` is the previous build and re-upload
  that build; or mark the bad release's successor `critical` to force everyone forward.
* **Retiring old clients:** raise `minSupportedVersion`/`minSupportedSchema` in `release.json`.
* **Signing everyone out:** rotate `TOKEN_SECRET`.
* **Data retention:** relay ops ≤ 30 days or until acked; devices inactive 30 days stop holding
  ops back; account deletion purges devices/relay/snapshot and leaves a billing tombstone.
