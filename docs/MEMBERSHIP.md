# Membership management

Four membership types decide what the app does for a user. They are implemented with the
**strategy pattern** on both sides: one strategy object per type, the same interface for all of
them, and no `if (plan === 'pro')` anywhere else in the code.

| Type | Who | Active tasks | What you get |
|---|---|---|---|
| **anonymous** | No account — the default. Data lives in the browser's IndexedDB only. | **3** | The whole local app: tasks, events, today view, updates. Shows the small *Sponsored* panel ([ADVERTISING.md](ADVERTISING.md)) |
| **free** | An account without a paid subscription | **3** | Same as anonymous, plus: device list, free 14-day Pro trial, subscribe, manageable by support |
| **paid** | Pro (trial, active, or in the dunning/offline grace period) and Lifetime | **unlimited** | Full features: unlimited active tasks, end-to-end-encrypted sync, encrypted backup (vault), push, calendar feed, full insights, **no sponsored content** (`ad_free`) |
| **admin** | Accounts with `role = admin` — developers/managers doing production support | **unlimited** | Everything *paid* has, plus the **Admin** panel and `/api/v1/admin/*` |

"Active" means a task whose status is *To do* or *In progress* — what the dashboard counts as
*Open tasks*. Finishing or deleting a task frees a slot; re-opening a done task needs one;
creating a task directly as *Done* never needs one. Data that arrives through sync is never
refused, and tasks that already exist are never removed when a membership lapses — the user just
cannot open a new one until they are below the limit again (the header badge shows `Free · 7/3`
in that case).

## Where the strategy lives

```
services/account/membership.py          apps/web/src/app/core/membership/membership.ts
┌──────────────────────────────┐         ┌───────────────────────────────────────────┐
│ MembershipStrategy           │         │ MembershipStrategy (interface)             │
│   type · label · features    │         │   type · label · shortLabel · features     │
│   max_active_tasks           │  /me    │   maxActiveTasks · canUpgrade              │
│   can_use(feature)           │ ──────▶ │   canUse(feature)                          │
│   can_activate_task(n)       │         │   checkActivateTask(n) → {allowed, reason} │
│   describe()                 │         │   usageLabel(n) · upgradeHint()            │
├──────────────────────────────┤         ├───────────────────────────────────────────┤
│ AnonymousMembership          │         │ AnonymousMembership                        │
│ FreeMembership               │         │ FreeMembership                             │
│ PaidMembership               │         │ PaidMembership                             │
│ AdminMembership              │         │ AdminMembership                            │
└──────────────────────────────┘         └───────────────────────────────────────────┘
   resolve(db, account):                    MembershipService.strategy():
   role == admin → ADMIN                      not signed in → ANONYMOUS
   billing entitled → PAID                    /me loaded → createMembership(ent.membership,
   else → FREE                                   {maxActiveTasks, features})   ← server wins
   no account → ANONYMOUS                     offline → membership cached with the session
```

**Server (`membership.py`).** `billing.entitlements()` evaluates the subscription state machine
(trial → active → past_due → canceled, grace periods) and hands the boolean to
`membership.strategy_for(account, entitled)`. The strategy supplies `features`,
`max_active_tasks`, the label and `membership` type that go out in `/me` and
`/me/entitlements`. `billing.require_feature()` asks the strategy (`can_use`) before any paid
endpoint runs — sync answers `402` to free memberships. `GET /api/v1/membership` publishes the
catalogue; `GET /api/v1/membership/me` the caller's resolved strategy.

**Client (`membership.ts`, `membership.service.ts`).** The app must work with no account and
offline, so the strategies carry the defaults (`FREE_MAX_ACTIVE_TASKS = 3`, the paid feature
list). When signed in, the values the server sent are authoritative (`createMembership(type,
overrides)`) — raising the free limit on the server (`FREE_MAX_ACTIVE_TASKS=5`) reaches every
device without an app update. The last membership seen is cached with the session so an offline
start applies the right limits. Consumers:

- `TasksService.create()` / `update()` call `assertCanActivate()` → `TaskLimitError` with the
  strategy's reason; `tasks.limit()` / `tasks.usageLabel()` drive the header badge
  (`Free · 2/3`, `Pro`, `Admin`), the *Tasks* card and the task dialog, which shows the limit
  notice with an **Upgrade to Pro** button and disables *Save* instead of failing afterwards.
- `AccountService.canSync()` and `SyncService` use the feature list (`sync`, `vault`).
- The header shows **Log in** / **Register** while anonymous (they open the account dialog in
  the matching mode) and **☁ Sync** / **Log out** once signed in — `Register` disappears on
  login; `Log out` drops the session and keys on this device (data stays, membership becomes
  anonymous again).
- `MembershipService.isAdmin()` (= `canUse('admin')`) shows the **Admin** button and panel.
- `AccountService` re-reads `/me` when the tab becomes visible again (≤ 1/min) and whenever
  the account dialog opens, so a grant from the admin panel or a renewed subscription shows up
  without a restart.

Adding a membership type = one class on each side and a row in the table above; adding a rule =
one method on the interface, implemented by every strategy.

## Administrator account

`role` is a column on `accounts` (`user` | `admin`, Alembic `0002_membership`). It is separate
from billing: an administrator needs no subscription and keeps the role whatever the billing
state. How an account becomes an administrator:

1. **`ADMIN_EMAILS`** (environment) — e-mails that are promoted when they register or sign in.
   This is how the first administrator of a deployment is created (set it in the Container App
   / `docker-compose` environment, register in the app, done). The list never demotes.
2. **Admin panel** → *Make admin* / *Remove admin* (you cannot remove your own role).
3. **`python dev.py promote <email>`** (`seed.py --promote/--demote`) against the configured
   database.

What the panel (★ **Admin** in the header) and the API offer for production support:

| Action | API | Notes |
|---|---|---|
| Service health numbers | `GET /admin/stats` | accounts by membership, subscriptions by status, active devices, relay ops/bytes, vault snapshots, latest release |
| Find accounts | `GET /admin/accounts?q=&membership=&include_deleted=` | e-mail, membership, plan/status, devices, last seen |
| Account detail | `GET /admin/accounts/{id}` | billing row, limits/features, devices (platform, version, cursor), relay counters, audit trail |
| Grant / extend / revoke a paid membership | `PATCH /admin/accounts/{id}/membership` `{plan: pro\|lifetime\|free, days?, status?, note?}` | writes the subscription row with provider `admin`; `free` + `status: none` hands the trial back |
| Promote / demote | `PATCH /admin/accounts/{id}/role` | |
| Sign out everywhere | `POST /admin/accounts/{id}/sign-out` | lost phone, leaked token — every device signs in again, local data untouched |
| Delete account | `DELETE /admin/accounts/{id}` | the same purge the user can run (GDPR requests via support) |
| Audit log | `GET /admin/audit` | who did what to which account, with the note |
| Publish / flag a release | `POST /release/publish` | also accepts an administrator's bearer token (e.g. mark a release `critical` during an incident) |

**API docs in production.** `/api/docs`, `/api/redoc` and `/api/openapi.json` are open in
dev, test, qa and staging (`DOCS_REQUIRE_ADMIN=true` turns the gate on there to try it). In
`APP_ENV=prod` they always need an administrator — nothing can switch that off. `APP_ENV` is
normalised (`Production`, `PROD`, `"prod"  # comment` all mean `prod`), the startup line
(`APP_ENV=prod · api docs=administrators only`) and `/api/v1/health` (`"docs": "admin-only"`)
show what the process decided. How it works: a browser gets a small sign-in page (`docs_gate.py`) that derives the auth key
from e-mail + password *in the browser* — same PBKDF2/HKDF as the PWA, the password never
leaves it — signs in like any device and stores the session token in an HttpOnly,
SameSite=strict cookie scoped to `/api`; API clients send `Authorization: Bearer …` instead.
`POST /api/docs/logout` clears the cookie and revokes that session. Sign-ins are audited
(`docs.login`); demoted administrators lose access immediately.

Every endpoint requires a bearer token of an account with `role = admin` (`auth.admin_principal`,
`403` otherwise — the deploy pipeline's `X-Admin-Token` only publishes releases). Every change
lands in `audit_log` with the actor. **Privacy is unchanged:** administrators see account and
billing metadata, device platforms/versions and byte counters — never ciphertext, never
content. `tests/test_privacy.py::test_admin_api_never_returns_ciphertext` walks the OpenAPI
schema of every `/admin/*` response to enforce that.

## Fake accounts for testing

`services/account/seed.py` creates one account per membership situation. They are real
accounts — keys are derived exactly like the PWA does (`clientcrypto.py` is a Python twin of
`crypto.ts`, pinned by a known-answer test) — so you can sign in with them in the app:

| E-mail | Password | Membership | Billing state |
|---|---|---|---|
| `free@example.com` | `Free-pass-123` | free | no subscription, trial available |
| `trial@example.com` | `Trial-pass-123` | paid | Pro trial, 14 days left |
| `pro@example.com` | `Pro-pass-123` | paid | Pro, active, renews in 30 days |
| `lifetime@example.com` | `Lifetime-pass-123` | paid | Lifetime |
| `pastdue@example.com` | `Pastdue-pass-123` | paid | Pro, payment failed 2 days ago (in the 7-day grace period) |
| `expired@example.com` | `Expired-pass-123` | free | Pro canceled — back to Free limits, trial used |
| `admin@example.com` | `Admin-pass-123` | admin | role = admin |

Anonymous mode needs no fixture — open the app without signing in.

- `python dev.py run` / `serve` seed them automatically (`SEED_FAKE_ACCOUNTS=true`, printed at
  startup); `python dev.py seed` (re)creates them on demand. Seeding is idempotent: existing
  accounts keep their data, the membership rows are reset to the states above.
- Refused when `APP_ENV=prod` (`seed.py --force` to override, which you should not need).
- The API test-suite (`tests/test_seed.py`, `tests/test_admin.py`) and the end-to-end tests
  (`e2e/test_membership.py`) use the same fixtures.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FREE_MAX_ACTIVE_TASKS` | `3` | Active-task limit of the anonymous and free memberships |
| `ADMIN_EMAILS` | — | Comma-separated e-mails promoted to administrator on register/login |
| `SEED_FAKE_ACCOUNTS` | `false` (`dev.py run`: `true`) | Seed the fake accounts at API startup; ignored in prod |
| `DOCS_REQUIRE_ADMIN` | `false` | Gate `/api/docs`, `/api/redoc`, `/api/openapi.json` outside prod as well. In `APP_ENV=prod` the gate is always on, regardless of this variable |

## Tests

| Where | What |
|---|---|
| `services/account/tests/test_membership.py` | strategy behaviour, resolution from role + billing, catalogue, feature gating, `ADMIN_EMAILS`, release publishing by admins |
| `services/account/tests/test_billing.py` · `test_payments.py` · `test_refunds.py` · `test_fakes.py` | the entitlement model (trial, neutral payment events, idempotency, grace periods, canceled-but-paid periods); the payment strategies (`payments.py`): priced plan catalogue, Stripe and PayPal checkout (incl. trial carry-over) + webhook lifecycles against a recorded HTTP transport; refunds on cancellation for monthly / yearly / Lifetime (prorated arithmetic, provider calls, refused refunds, partial refunds), the payments ledger and failure handling from every provider event; the built-in provider fakes driving checkout → pay → webhooks → cancel + refund / manage / fail / recover / decline / dashboard refund, and the seeded paid accounts |
| `services/account/tests/test_payment_analytics.py` · `e2e/test_payments_admin.py` | the administrators' payment dashboard: KPIs and trend, breakdowns, top customers, refunds, filters, KPI alerts, drill-down, CSV, admin-only; in a real browser after purchases/refunds through the fakes |
| `e2e/test_billing.py` | real browser: priced plan picker → Stripe (fake) → Pro → cancel with the quoted prorated refund → failed renewal (past due: failure notice, pay button + plan picker) → recovery → refund from the dashboard; trial → PayPal (trial carried over) → cancel keeps the free days; Lifetime → cancel membership refunds the flat amount; declined PayPal payment; abandoned checkout |
| `services/account/tests/test_admin.py` | stats, search, grant/revoke with audit, promote/demote, sign-out-everywhere, purge, seeded admin managing seeded users |
| `services/account/tests/test_seed.py` | every fake account signs in and resolves to the documented membership; idempotency; prod refusal; crypto known-answer test |
| `services/account/tests/test_privacy.py` | `audit_log` allow-listed; `/admin/*` responses contain no ciphertext |
| `services/account/tests/test_docs_gate.py` | docs open outside prod; gated by role/bearer/cookie in prod; login/logout/revocation/demotion |
| `apps/web/src/app/core/membership/membership.spec.ts` | the four client strategies, server overrides, unknown types |
| `apps/web/src/app/core/data/tasks.service.spec.ts` | limits enforced in `TasksService` for every membership, slot freeing/re-opening, offline cache, sign-out |
| `e2e/test_membership.py` | real browser: anonymous limit → free account → Pro unlimited on two devices; administrator grants/revokes Pro and signs a user out |
