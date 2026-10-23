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
        pgPassword=… tokenSecret=… releaseAdminToken=… stripeSecretKey=… stripeWebhookSecret=…
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
(manifest publishing), the payment providers' secrets (see *Payments*), database password. The Bicep
template stores them as Container App secrets; use Key Vault references for a hardened setup.

API docs: in `APP_ENV=prod` `/api/docs` (and `/api/redoc`, `/api/openapi.json`) always ask for an
administrator sign-in; every other environment leaves them open (`DOCS_REQUIRE_ADMIN=true` gates
them there too). Check `/api/v1/health` → `"environment"` / `"docs"` to see what a process decided.

Administrators: pass `adminEmails=you@example.com` to the Bicep deployment (→ `ADMIN_EMAILS`),
then register with that e-mail in the app — the account gets the administrator role and the
**Admin** panel. Further administrators are promoted from the panel. The fake test accounts are
never created in `APP_ENV=prod` (see [MEMBERSHIP.md](MEMBERSHIP.md)).

Sponsored content: `ADS_ENABLED` (kill switch, default on), `ADS_IMAGE_HOSTS` (first-party
hosts for creative images — empty keeps creatives text-only) and, only if you consciously want
the CDN's country as a fallback for devices that report none, `ADS_REGION_HEADER`. The **ad
source** (`ADS_SOURCE`: internal · sponsored · affiliate · adsense · microsoft · network) and the
provider credentials (`ADS_ADSENSE_*`, `ADS_MICROSOFT_*`, `ADS_NETWORK_*`, `ADS_AFFILIATE_TAG`)
are plain environment variables too; in `prod` the built-in fakes are off, so a provider without
credentials is *unavailable* rather than fake. Third-party sources load the provider's tag only
after the user's consent in the panel — for EEA/UK traffic Google additionally requires a
certified CMP. Everything the first-party feature stores is anonymous; see
[ADVERTISING.md](ADVERTISING.md) §5 and §10 before changing defaults.

Ads admin console: `apps/ads-admin` is deployed **separately** (its own nginx image or any
static host). Set `ADS_ADMIN_API_BASE` to the API origin and add the console's origin to the
API's `CORS_ORIGINS`; restrict the console's hostname to staff (VPN / IP allow-list / identity
proxy) — it only works with `role = admin` accounts, but it does not need to be public at all.
Single-process alternative: `ADS_ADMIN_DIST=/path/to/apps/ads-admin` serves it at `/ads-admin/`.

## Payments

How an account pays is a **strategy** in `services/account/payments.py` — one object per method
with the same interface (`create_checkout`, `verify_signature`, `parse_webhook`, `cancel`,
`refund`, `manage_url`, `describe`), the same way `membership.py` models what a membership may
do. Two strategies ship:

| Method | Env vars (all in `services/account/.env.example`) | Hosted page | Webhook URL |
|---|---|---|---|
| `stripe` | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Stripe Checkout (card, Apple/Google Pay) | `/api/v1/billing/webhooks/stripe` |
| `paypal` | `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID`, `PAYPAL_PLAN_PRO_MONTHLY` / `_PRO_YEARLY` (+ `PAYPAL_ENV`) | PayPal approval page (Subscriptions API; Orders API for Lifetime) | `/api/v1/billing/webhooks/paypal` |

**Prices** are configuration, not provider objects: `PRICE_PRO_MONTHLY` (default `1.00`),
`PRICE_PRO_YEARLY` (`10.00`), `PRICE_LIFETIME` (`20.00`) in `BILLING_CURRENCY` (`USD`).
Stripe is charged exactly these amounts (the checkout session carries ad-hoc price data — no
Price objects to keep in sync); PayPal subscriptions bill the configured Plan ids, which you
create at PayPal with the same amounts (Lifetime is a one-off PayPal order at `PRICE_LIFETIME`).
`GET /api/v1/billing/methods` returns the plans with their prices and refund rules; the account
dialog's plan picker shows them ("Pro — monthly · $1.00 / month").

The account dialog shows the plan picker and one *Pay with …* button per method to every
account that can still buy — free accounts, **trial accounts** (a running trial is carried over:
Stripe `trial_end` / PayPal `start_time`, so paid billing starts when the trial ends),
**past-due** accounts (subscribe again with another method) and lapsed ones. A method **works as
soon as its credentials are set**. `BILLING_METHODS=stripe,paypal` narrows and orders the list,
`BILLING_PROVIDER` is the pre-selected method. Card/PayPal data never reaches the API: both
providers host the payment page, and the API only stores plan, status, period and the
provider's customer/subscription ids (`subscriptions` table) plus one **ledger row per charge**
(`payments` table: amount, status, period, refund — `GET /api/v1/billing/payments`, shown as
*Payments* on the account screen).

**Cancellation refunds** (`GET /billing/cancel/preview` → `POST /billing/cancel`, the *Cancel
subscription* / *Cancel membership* button): the membership ends **at once** and money goes
back — a monthly or yearly subscription refunds the unused share of the period it paid for
(`days left ÷ days in the period × amount paid`, e.g. 265 of 365 days left on a $10.00 year →
$7.26), Lifetime refunds the flat `REFUND_LIFETIME` (default `10.00`). The confirmation quotes
the exact amount the server computed. The refund is issued at the provider first (Stripe
`/v1/refunds` against the payment intent/charge, PayPal `/v2/payments/captures/{id}/refund`),
then the subscription is ended there (Stripe `DELETE /v1/subscriptions/{id}`, PayPal `/cancel`),
then the ledger and the subscription row are updated. A refused refund changes nothing (HTTP
502); a refund that went through before the cancellation failed stays on record. Nothing is
refunded when nothing was paid (a carried-over trial keeps its free days), when the last
payment failed (past due: the paid period is over) or when the charge was refunded already.
Rows without a usable provider (seeded accounts, admin grants) are refunded "on record" only.

**Payment failures**, for every membership: a failed renewal (Stripe `invoice.payment_failed`,
PayPal `BILLING.SUBSCRIPTION.PAYMENT.FAILED`) is recorded in the ledger and puts the
subscription in `past_due` — Pro stays for `GRACE_DAYS` while the provider retries, the account
screen shows *Payment failed: $… — reason*, *Update payment method* (the provider's page) and
the pay buttons to subscribe again; a retried invoice that succeeds turns the same ledger row
into a success and reactivates. A declined one-off Lifetime payment (Stripe
`checkout.session.async_payment_failed`, PayPal `PAYMENT.CAPTURE.DENIED`) is recorded and grants
nothing. A refund issued in the provider's dashboard (Stripe `charge.refunded`, PayPal
`PAYMENT.SALE.REFUNDED` / `PAYMENT.CAPTURE.REFUNDED`) updates the ledger and ends the membership
the refunded charge was paying for.

*Manage billing* opens the provider's self-service page (Stripe Customer Portal / PayPal
*Automatic payments*); it comes back to `/?billing=managed`.

**Payment analytics for administrators** live in the separate admin console
(`apps/ads-admin` → *Payments*; API `/api/v1/admin/payments/{analytics,transactions,
transactions/{id},transactions.csv}`, administrator accounts only): revenue, transactions,
successful/failed, refunds and average transaction value with the trend vs. the previous
window; revenue per day, status and method donuts, top customers, by-plan bars and the refunds
list; filters for date range, method and status; a transaction drill-down with the customer's
account state and history; CSV export; and **KPI alerts** (failure rate, refund rate, revenue
drop, subscriptions in dunning, no successful payment) with thresholds in `PAYMENT_ALERT_*`.

**Built-in fakes (no credentials needed).** Outside `APP_ENV=prod`, a provider whose
credentials are missing is replaced by its fake in `services/account/fakes.py`
(`BILLING_FAKE_PROVIDERS`, default on): the strategy code is unchanged and makes the same REST
calls, but they are answered in-process, the hosted pages are served by this API under
`/api/v1/billing/fake/{stripe,paypal}/…`, and every outcome arrives as a **signed webhook
delivered through the normal webhook path** (`billing.receive_webhook`). The fake pages offer
what a buyer, the provider's scheduler or an operator in the provider's dashboard would do: pay
/ approve, *pay with a card that is declined*, cancel, *renewal collected*, *renewal payment
fails*, *card updated → invoice paid*, *refund in full (dashboard)*; charges carry the
configured prices, and refunds through `/v1/refunds` / `/v2/payments/captures/…/refund` are
answered and reported back. The seeded `pro@` / `pastdue@` / `lifetime@` / `expired@example.com`
accounts come with ledger rows and are registered with the fake Stripe, so their Manage /
Cancel (with refund) buttons work too. Fake state lives in `DATA_DIR/billing_fakes.json`. The
buttons are labelled *(fake)*; `GET /api/v1/billing/methods` reports `fake: true`. In prod, or
with `BILLING_FAKE_PROVIDERS=false`, a method without credentials stays visible but unavailable:
a click explains what is missing (HTTP 503).

Set-up checklist per provider:

- **Stripe:** register the webhook endpoint with the events `checkout.session.completed`,
  `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`,
  `customer.subscription.created`, `customer.subscription.updated`,
  `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed`, `charge.refunded`
  and copy its signing secret. No Price objects are needed. Test keys (`sk_test_…`) mark the
  method as *test mode* in the UI. Locally:
  `stripe listen --forward-to localhost:8000/api/v1/billing/webhooks/stripe`.
- **PayPal:** create a REST app (sandbox first: `PAYPAL_ENV=sandbox`), a Product with a billing
  Plan per subscription (`P-…` ids, priced like `PRICE_PRO_MONTHLY` / `PRICE_PRO_YEARLY`), and a
  webhook subscribed to `BILLING.SUBSCRIPTION.*`, `PAYMENT.SALE.COMPLETED`,
  `PAYMENT.SALE.REFUNDED`, `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`,
  `PAYMENT.CAPTURE.DENIED`, `PAYMENT.CAPTURE.REFUNDED`; its id is `PAYPAL_WEBHOOK_ID` (PayPal
  confirms every delivery through its *verify-webhook-signature* API, so the API must be able to
  reach `api-m.paypal.com`). Lifetime is a one-off order that the API captures when
  `CHECKOUT.ORDER.APPROVED` arrives — a buyer who closes the tab after approving still gets the
  purchase. PayPal webhooks need a public HTTPS URL (use a tunnel locally).

Every webhook is authenticated by its strategy, translated into one neutral
`schemas.PaymentEvent` (optionally carrying the charge as `payment`) and applied idempotently
per provider event id (`billing.apply_webhook`); renewals/failures/refunds that only name the
provider's subscription or charge are resolved through the stored ids. Adding a third method =
one subclass of `PaymentMethod` registered in `payments.METHODS` (plus, optionally, a fake in
`fakes.py`); nothing else changes.

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
