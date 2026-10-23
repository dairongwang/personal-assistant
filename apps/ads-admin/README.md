# Ads admin — separate operations console (advertising & payments)

A standalone dashboard for administrators to **manage advertising** (campaigns, the active ad
source), **analyse performance** (impressions, clicks, CTR per day / source kind / country /
campaign, trend vs. the previous window, CSV export), **watch the privacy posture** (live
checks, data inventory, privacy regimes worldwide) and **follow the money** (the *Payments*
section). It is deliberately separate from the user app (`apps/web`): different audience,
different release cadence, different hosting.

**Payments** (`#/payments`, API `/api/v1/admin/payments/*`, `services/account/payment_analytics.py`):

* KPIs for the window — net revenue (gross − refunds), transactions, successful / failed with
  the success rate, refunds (count and amount, refund rate), average transaction value — each
  with the change vs. the previous window of the same length.
* Charts: revenue trend per day (gross, net, refunds), payment status and payment method
  donuts, top customers, revenue by plan, and the refunds list.
* Filters: date range (presets or a custom from/to), payment method, status; CSV export of the
  filtered transactions.
* Drill-down: the transactions table (search by e-mail / payment id / refund id, paging) opens
  a detail panel with the payment, the customer's account and subscription state, lifetime value
  and the customer's whole payment history; top customers and refunds link into it.
* KPI alerts: failure rate, refund rate, revenue drop vs. the previous window, subscriptions in
  dunning, a window without any successful payment — thresholds in the API's
  `PAYMENT_ALERT_*` settings (warn above the threshold, alert above twice it).

* **No build step, no dependencies**: `index.html` + ES modules (`src/`) + `styles.css`.
  Charts are inline SVG. Deploy the folder to any static host.
* **Talks to the existing Account & Sync Service** (`/api/v1/admin/ads/*`, administrator
  accounts only). Sign-in derives the login key in the browser exactly like the user app — the
  password never leaves it. The session token lives in `sessionStorage` (gone with the tab).
* **Runtime configuration**: `config.js` → `apiBase` (the API's origin; `""` = same origin).
  The Docker image writes it from `ADS_ADMIN_API_BASE`.
* **Security headers** in the nginx image: CSP (`script-src 'self'`, `connect-src` only the
  API), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `noindex`.

## Run it

| Situation | How |
|---|---|
| Development | `python dev.py run` also serves it at **http://localhost:4300/** (API on :8000 with CORS for that origin) — sign in with `admin@example.com` / `Admin-pass-123` |
| Docker, with the rest | `docker compose up --build` → **http://localhost:8081/** (service `ads-admin`, `ADS_ADMIN_API_BASE=http://localhost:8000`) |
| Own deployment | `docker build -t pa-ads-admin -f apps/ads-admin/Dockerfile apps/ads-admin` and run with `ADS_ADMIN_API_BASE=https://api.example.com`; add that dashboard origin to the API's `CORS_ORIGINS` |
| Single process | `ADS_ADMIN_DIST=apps/ads-admin` on the API → served at `/ads-admin/` (used by the e2e tests) |

## Tests

* `node --test apps/ads-admin/tests/*.test.mjs` — pure helpers (CTR, chart geometry, labels;
  money formatting, date presets, revenue chart, donut segments, alert summaries).
* `services/account/tests/test_ad_analytics.py` — analytics, suppression, CSV, compliance, CORS, mount.
* `services/account/tests/test_payment_analytics.py` — payment KPIs, trend, breakdowns, top
  customers, refunds, filters, KPI alerts, drill-down, CSV, admin-only.
* `e2e/test_ads_admin.py` — real browser: sign in, KPIs and chart, campaign CRUD, source switch,
  compliance page, CSV export, sign out.
* `e2e/test_payments_admin.py` — real browser: purchases/refund/decline through the user app,
  then the Payments dashboard, its filters, drill-down and CSV export.

Design and privacy notes: `docs/ADVERTISING.md` §11.
