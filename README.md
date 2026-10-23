# Personal Assistant

Tasks, events and a daily summary in a **local-first Progressive Web App**: one codebase for
Windows, macOS, Linux, phones and tablets (installable from the browser). Your data stays on your
device; an optional account adds **end-to-end-encrypted sync**. The app **updates itself**.

| Layer | Tech |
|---|---|
| PWA | Angular 21, `@angular/service-worker`, Dexie (IndexedDB), WebCrypto — `apps/web` |
| Admin console | Separate static SPA (no build, no dependencies) for administrators: ad campaigns, ad source, ad analytics, privacy & compliance, **payment analytics** (revenue, transactions, refunds, KPI alerts, drill-down) — `apps/ads-admin` |
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
| `python dev.py serve` | Production build served by the API at http://localhost:8000/ — service worker on (updates, offline, install), reachable from phones on your network; the ads admin console at http://localhost:8000/ads-admin/ |
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
  server*, delete account) and **Log out** (your data stays on the device). Free, trial and
  past-due accounts pick a plan (monthly · yearly · Lifetime) and pay with **Stripe** (card) or
  **PayPal** (`services/account/payments.py`, strategy pattern). Prices are configuration
  (`PRICE_PRO_MONTHLY=1.00`, `PRICE_PRO_YEARLY=10.00`, `PRICE_LIFETIME=20.00`); Pro accounts see
  their renewal date, their payments, and can **cancel with a refund** (subscriptions: the unused
  days of the period; Lifetime: a flat `REFUND_LIFETIME=10.00`) or **manage billing**; a failed
  payment is shown with its reason and the ways out. Locally nothing needs configuring: without
  provider credentials the **built-in fakes** of Stripe and PayPal answer (fake checkout and
  portal pages, signed webhooks, declined cards, "payment fails" / "refund from dashboard"
  simulation — [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#payments)).
- **Memberships** (header badge, e.g. `Free · 2/3`): *anonymous* (no account) and *free*
  accounts keep up to **3 active tasks**; *paid* (Pro/Lifetime) and *administrator* accounts are
  unlimited and get every feature. Administrators also get the **⚙ Admin** panel (accounts,
  memberships, devices, audit log — advertising is managed in the separate ads admin console,
  which the panel links to) for production support. Details and the
  test accounts (`free@example.com` / `pro@example.com` / `admin@example.com` …):
  [docs/MEMBERSHIP.md](docs/MEMBERSHIP.md).
- **Sponsored content** (free memberships): a card labelled **Sponsored** at the end of the
  sidebar, chosen by *country and language only* — both derived on the device (time zone /
  locale), written on the panel (`SHOWN FOR Germany (DE) + English (en)`) and changeable under
  **Why this?**, where it can also be switched off or made compact. No ad network, no cookies,
  no tracking: the request carries no sign-in, and the server keeps only anonymous daily totals
  (views/clicks per ad and country). Pro has no sponsored content. How to see it locally: next
  section. Design, privacy assessment and administration: [docs/ADVERTISING.md](docs/ADVERTISING.md).

## Seeing the advertisement panel on the dashboard (http://localhost:4200/)

The dashboard shows a **Sponsored** panel for free memberships (anyone who is not signed in as
Pro/Admin). In the default *showcase* layout it takes about **a third of the page** (the sidebar
column × a third of the height) and spells out the **country + language** it was picked for,
highlighted in yellow — e.g. `Germany (DE) + English (en)`.

1. **Check `services/account/.env`.** If it contains `APP_ENV=prod`, change it to `APP_ENV=dev`
   (or delete the line). In `prod` the service seeds nothing and the fake ad providers are off,
   so the panel would only say *"No … right now"*. (Alternative: keep `prod`, add
   `ADS_FAKE_PROVIDERS=true` to `.env`, and run `python dev.py seed --ads --force` once after
   step 2.)
2. `python dev.py setup` (first time only), then **`python dev.py run`**. The API log prints
   `Sample sponsored-content campaigns ready: 11 (10 active)` — these are the **dummy
   advertisements** (`services/account/seed.py`, `FAKE_ADS`): a *worldwide* one that everybody
   sees, plus English-, Europe (EEA)-, Asia-Pacific-, Americas-, DACH-, EU- and US/CA-targeted
   ones. They link to `example.com` and say "DUMMY AD" in the headline.
3. Open **http://localhost:4200/** — do **not** sign in (or sign in as `free@example.com` /
   `Free-pass-123`). The panel is the yellow-bordered card at the end of the right-hand column
   (on a phone: at the end of the page). It shows `SPONSORED · <sponsor>`, the highlighted
   `SHOWN FOR <country> + <language>` line, the dummy ad, and a footer line
   *"Campaign 1 of 3 for this context · this session: 1 view · 0 clicks counted"*.
4. Try the targeting: click **Why this?** → *Pick offers for* → choose a country (e.g. *Japan*
   → the Asia-Pacific dummy appears; *United States* → the Americas one; *No region* → only the
   worldwide ones). Or change your browser's language / time zone and reload. Click the ad link
   once: it opens in a new tab and the counter reads *1 click*.
5. Campaigns and their view/click totals live in the **separate ads admin console** at
   **http://localhost:4300/** (sign in there as `admin@example.com` / `Admin-pass-123`). The
   user app's ⚙ Admin dialog only links to it (**Ads admin console ↗**).
6. Sign in as `pro@example.com` / `Pro-pass-123`: the panel disappears (Pro is ad-free).
   *Why this?* → untick **Large panel** to switch to the compact production card.

No ad? `curl "http://localhost:8000/api/v1/ads/serve?region=DE&lang=en"` must list campaigns —
if `"ads": []`, run `python dev.py seed --ads` (`--force` with `APP_ENV=prod`) and reload.

### The ads admin console (separate dashboard for administrators)

Advertising is **operated exclusively from its own dashboard**, `apps/ads-admin` — a standalone
static app (no build step, no npm dependencies) that is developed, deployed and run
independently of the user app and talks to the API's `/admin/ads/*` endpoints. The user app
ships **no advertising management UI at all**: its ⚙ Admin dialog only shows an
**Ads admin console ↗** link (the URL comes from the API's `ADS_ADMIN_URL`), and
`e2e/test_ads_admin.py::test_console_is_separate_from_the_user_app` keeps it that way —
it fails if management UI ever reappears in the user app, and it exercises the console on its
own origin (separate process + CORS), the shape of a separate deployment. `python dev.py run` serves it at
**http://localhost:4300/** (sign in with `admin@example.com` / `Admin-pass-123`; your password
is turned into a login key in the browser and never sent). With Docker Compose it is the
`ads-admin` service on **http://localhost:8081/**; for your own hosting see
`apps/ads-admin/README.md` (nginx image, `ADS_ADMIN_API_BASE`, add its origin to the API's
`CORS_ORIGINS`).

What it offers:

| Section | What you see |
|---|---|
| **Overview** | Impressions, clicks, CTR for a 7–365-day window with the change vs. the previous window; per-day chart; breakdown by source kind and by country; per-campaign table (share of traffic, all-time totals); **Export CSV** |
| **Campaigns** | Create / edit / pause / activate / archive campaigns of all three first-party kinds, with per-campaign stats by day and country |
| **Ad source** | Switch between Internal ads · Sponsored content · Affiliate links · Google AdSense · Microsoft Advertising · Third-party network (fake / live / unavailable shown per source) |
| **Privacy & compliance** | *Live checks* against the running configuration and database (no identifiers in ad tables, day + country granularity, retention respected, small-count suppression, no IP geolocation, first-party images, https links, third-party consent gating, fakes off in prod, anonymous endpoints, audit trail, rate limiting); the **data inventory** (what exists, where, personal or not, retention, row counts); and the **privacy regimes** worldwide (GDPR/ePrivacy, UK GDPR/PECR, Swiss nFADP, CCPA/CPRA + US states, COPPA, LGPD, PIPEDA/Law 25, APPI, PIPA, PIPL, DPDP, Australian APPs, DSA Art. 26) with obligations, how this system meets them, and which ones need attention when a check fails |
| **Audit log** | Every campaign and source change, by whom and when |

Privacy is designed into the analytics themselves: they are computed only from anonymous daily
totals, and **per-country figures below `ADS_ANALYTICS_MIN_COUNT` (default 5) impressions are
folded into “other”** — in the dashboard and in the CSV — so a single view from a small country
can never be traced to a person. Details: [docs/ADVERTISING.md](docs/ADVERTISING.md) §11.

### Testing each advertisement source

The panel's content comes from one configurable **ad source**. Six are supported; each has a
built-in **fake** so you can confirm it works without any provider account and without a single
request leaving your machine (the fakes are served by the local API; `ADS_FAKE_PROVIDERS` is on
outside `APP_ENV=prod`).

**Switching:** open the **ads admin console** at **http://localhost:4300/** (sign in as
`admin@example.com` / `Admin-pass-123`) → **Ad source** → pick one (applies immediately, is
audited, and survives restarts). Then open or reload **http://localhost:4200/** in a window that
is *not* signed in as Pro (a private window, or the free account). Alternative without the UI:
set `ADS_SOURCE=<kind>` in `services/account/.env` and restart `python dev.py run`.

| Source (`ADS_SOURCE`) | What you should see on the dashboard | What to check |
|---|---|---|
| `internal` — **Internal ads** | Badge **INTERNAL AD**; headline *"INTERNAL AD — Go Pro …"* or *"… Install the app …"*; disclosure *"A message from the makers of Personal Assistant — not a paid placement."* | Click **See Pro →**: it opens the account screen (an `app:upgrade` link) instead of a website. No FAKE chip: these are real house ads from the campaign list (kind *internal*). |
| `sponsored` — **Sponsored Content** (default) | Badge **SPONSORED**; a *"DUMMY AD — …"* or fictional sponsor; disclosure *"Paid placement …"* | Region/language targeting as described above; views/clicks appear in the admin totals. |
| `affiliate` — **Affiliated Links** | Badge **AFFILIATE LINK**; *"AFFILIATE DUMMY — Noise-cancelling headphones …"*; a highlighted disclosure *"Affiliate link: if you buy through it we may earn a commission…"* | Hover/inspect the link: the affiliate tag from `ADS_AFFILIATE_TAG` (e.g. `tag=pa-21`; set it in `.env`) is appended to the URL at serve time. |
| `adsense` — **Google AdSense** | Badge **AD · GOOGLE ADSENSE** + red **FAKE** chip; a **consent card** *"Ads here are provided by Google AdSense, a third party…"* with **Allow** / **Not now** | Open DevTools → Network before clicking: nothing is loaded until you press **Allow**. Then `…/api/v1/ads/fake/adsense.js?client=ca-pub-000…` loads, an `<ins class="adsbygoogle">` appears exactly as Google documents it, and the fake tag draws a blue dashed box **"Google AdSense — FAKE"**. Status line: *"Creative rendered by Google AdSense (fake tag served by this API)"*. **Why this?** shows *allowed on this device* with a **Withdraw** link → the consent card returns. |
| `microsoft` — **Microsoft Advertising** | Badge **AD · MICROSOFT ADVERTISING** + **FAKE**; the same consent card for Microsoft | After **Allow**: `…/api/v1/ads/fake/microsoft.js?siteId=…&publisherId=…` loads, a `<div class="pubcenter-ad" data-ad-unit-id=…>` appears, and a green dashed **"Microsoft Advertising — FAKE"** box is drawn. |
| `network` — **Third Party Network** | Badge **AD · EXAMPLE AD NETWORK** + **FAKE**; *"FAKE NETWORK AD — served server-to-server for DE"* | No consent card: the creatives were fetched by *our server* from the (fake) network endpoint and relayed — the browser never contacts the network (Network tab: only `localhost` requests). `curl "http://localhost:8000/api/v1/ads/fake/network.json?region=DE&lang=en"` shows the raw answer the server normalises. |

For every source the panel keeps the **SHOWN FOR country + language** line, and **Why this?**
names the source (*Google AdSense (fake)*, *Third-party network*, …) and whether it is
first-party, relayed, or a third party with consent. Switching back: choose **Sponsored
content** again.

**Going live** (no fakes): set the provider's values in `services/account/.env` — AdSense
`ADS_ADSENSE_CLIENT=ca-pub-…` + `ADS_ADSENSE_SLOT=…`; Microsoft `ADS_MICROSOFT_SITE_ID`,
`ADS_MICROSOFT_PUBLISHER_ID`, `ADS_MICROSOFT_AD_UNIT_ID`; a network `ADS_NETWORK_NAME` plus
`ADS_NETWORK_JSON_URL=https://…?region={region}&lang={lang}` (server-to-server) *or*
`ADS_NETWORK_SCRIPT_URL` (consent-gated tag); affiliates `ADS_AFFILIATE_TAG=key=value`. With
credentials present the admin panel shows the source as **live** and the real tag/URL is used;
`ADS_FAKE_PROVIDERS=false` (the prod default) makes unconfigured providers **unavailable**
instead of fake. Privacy implications per source: [docs/ADVERTISING.md](docs/ADVERTISING.md) §10.

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
sponsored content: [docs/ADVERTISING.md](docs/ADVERTISING.md) ·
deployment: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) · phones/tablets:
[docs/MOBILE_DEPLOYMENT.md](docs/MOBILE_DEPLOYMENT.md) · product design:
[docs/PLATFORM_DESIGN.md](docs/PLATFORM_DESIGN.md).

## Project layout

```
apps/web/            Angular PWA (release.json, ngsw-config.json, src/app/core/{db,data,sync,update,account,membership,admin,ads,ui})
apps/ads-admin/      Ads admin console: static SPA (index.html, src/, styles.css), serve.py (dev), Dockerfile (nginx), tests/ (node --test)
services/account/    FastAPI service (membership.py strategies, admin.py, ads.py, seed.py), Alembic migrations, tests, Dockerfile
e2e/                 Playwright end-to-end tests (updates, sync, privacy, membership, sponsored content)
infra/azure/         Bicep: Static Web App + Container App + PostgreSQL
.github/workflows/   ci.yml (Win/macOS/Linux) · deploy.yml (tag → Azure)
dev.py               cross-platform developer commands
```

API (`/api/v1`, docs at `/api/docs` — open in dev/test/qa/staging, administrator sign-in in
prod): `auth/*`, `me`, `membership`, `billing/*`, `sync/*` (ciphertext only), `release/*`,
`ads/*` (anonymous sponsored content; `ads/fake/*` = built-in fake providers outside prod),
`admin/*` (administrator accounts; `admin/ads/analytics`, `admin/ads/analytics.csv`,
`admin/ads/compliance`, `admin/ads/sources` feed the ads admin console), `health`.

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
| Sync says it needs Pro | Start the free trial in the account dialog, or subscribe — locally the built-in Stripe/PayPal fakes complete the purchase without credentials. |
| "Active task limit reached" | Free memberships keep 3 active tasks: finish/delete one, or upgrade (trial). Testing? Sign in as `pro@example.com` / `Pro-pass-123` (seeded by `dev.py run`). |
| `TOKEN_SECRET must be set in staging/prod` / API exits right after start | `APP_ENV` is `prod` or `staging` (check `services/account/.env`). `python dev.py run` / `serve` now generate a `TOKEN_SECRET` into `.env` automatically; when starting uvicorn yourself, set it: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `/api/docs` shows a sign-in page | `APP_ENV=prod`: the docs need an administrator. Register in the app, then `python dev.py promote you@example.com` (fake accounts are not created in prod). Any other environment leaves the docs open. |
| `Could not open requirements file … requirements-dev.txt` on `setup` | The download is incomplete; `setup` now falls back to `requirements.txt` + the test tools. Re-download if other files are missing too. |
| No **Admin** button after signing in | The account needs `role = admin`: `python dev.py promote you@example.com`, or put the e-mail in `ADMIN_EMAILS` before registering, or use `admin@example.com` / `Admin-pass-123` locally. |
