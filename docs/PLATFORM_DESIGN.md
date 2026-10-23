# Personal Assistant — Product Platform Design

Membership & billing · Cross-platform client · Authentication/authorization · Privacy-first data
management · Deployment & automated testing

| | |
|---|---|
| Status | Adopted — Phase A/B implemented (see "Implementation status" below) |
| Supersedes | `TECH_DESIGN.md` §4.2–4.9 (server-held data model). Feature roadmap in `TECH_DESIGN.md` §3 still applies. |
| Hard requirement | **The only user information stored on company servers is the minimum needed for billing.** |

> **Implementation status (this repository).** The local-first client is built with **Angular 21**
> (not React as proposed in §2.1) — `apps/web`. Local storage is **IndexedDB via Dexie** with
> version-controlled migrations (SQLite-wasm/OPFS from §2.1 can replace it behind the same
> repository layer when native shells arrive). The Account Service (`services/account`, FastAPI +
> PostgreSQL) implements auth (§3.2 with PBKDF2/HKDF pinned in `kdf_params`, Argon2id can be
> swapped in), devices, entitlements, Stripe and PayPal payment strategies behind the §1 adapter, the
> encrypted op-log relay with ack-based deletion (§4.3), the opt-in vault (§4.4, **on by
> default** because the product requirement is "server is master") and release manifests.
> Sync ordering is **server-assigned `seq`, last writer by seq wins** rather than HLC — simpler,
> deterministic, and it makes the server the master of order as required. Web Push (§4.6),
> passkeys, Capacitor/Electron shells are not implemented yet. Application-update and migration
> mechanics are specified in `UPDATES_AND_MIGRATIONS.md`.

---

## 0. The one decision everything else follows from

The hard requirement rules out the architecture in `TECH_DESIGN.md`, where a FastAPI server owns
tasks, events and notes. If the company runs that server, it holds user content. Therefore:

> **D1 — Local-first client, zero-knowledge server.**
> All product logic and all user content (tasks, events, habits, notes, plans, settings) live in an
> embedded database **on the user's devices**. The company backend is an *Account Service* that
> knows who you are and whether you have paid. It never receives plaintext content; multi-device
> sync passes through it only as ciphertext it cannot decrypt, and by default only transiently.

Consequences, stated up front so they can be challenged:

| Consequence | Why | Cost |
|---|---|---|
| The product core is rewritten in **TypeScript** and runs in the client (PWA / native shells). | The core must run on phones and tablets without a server, and in the browser. Python cannot run on iOS/Android for this stack (verified earlier: no `pydantic-core` wheels for Android/iOS). | The current `main.py` logic (~150 lines) is re-implemented. Small today; the decision is what matters. |
| The current FastAPI **task/event API is retired** from the cloud product. | It would store content. | None for users; the repo keeps it as the "self-hosted, no-account" mode *only if* we choose to support it (see §6 open question Q1). |
| FastAPI stays, as the **Account Service** (auth, billing, entitlements, encrypted relay). | Team knows it; it is a good fit; the requirement set is small. | — |
| Reminders on the pure-web (PWA) client require the server to know *when* to push (not *what*). | Browsers cannot schedule future notifications offline. Native shells use local notifications and need no server at all. | Documented metadata leak, user-visible in the privacy policy; mitigated in §4.6. |
| If a user loses their password **and** recovery key, their synced data is unrecoverable by us. | Zero-knowledge means we cannot reset encryption. | Must be communicated at sign-up; recovery-key flow in §3.4. |

The remaining decisions:

* **D2 — One UI codebase, three shells.** PWA first (all platforms), Capacitor for the app stores,
  optional Electron for deep desktop integration (§2).
* **D3 — Sync is an encrypted op-log relay with ack-based deletion.** The server holds ciphertext
  only until every registered device has fetched it (max 30 days), so it is a *relay*, not a store.
  A durable encrypted vault is an explicit opt-in (§4.4).
* **D4 — Billing through a Merchant-of-Record on web; native IAP in store builds; one entitlement
  model across both** (§1).
* **D5 — Login secret and encryption key derive from one password (Bitwarden model), passkeys as
  enhancement, recovery key mandatory** (§3).

---

## 1. Membership & billing

### 1.1 Membership model

```
 anonymous ──sign-up──▶ registered ──start trial──▶ trialing ──pay──▶ active ──▶ past_due ──▶ canceled
 (local only,            (free tier,                 (14 days,         │ ▲          (dunning,   (Pro off,
  no account)             1 device)                   all Pro)         │ └──renew──┘  7 days)    data stays
                                                                       └──cancel──▶ canceled      on device)
 any state ──delete account──▶ deleted (tombstone for billing records only, §4.7)
```

| Tier | Price (proposal) | What it includes | Why it is the paid line |
|---|---|---|---|
| **Free** | — | Full assistant on one device: tasks, habits, plan/review, capture, local reminders, local backup/export | Product is genuinely useful without an account — this is also the privacy story: *no account, no data anywhere but your device* |
| **Pro** | ~$4/mo or ~$36/yr; 14-day trial | Encrypted multi-device sync, web-push reminders, calendar subscription URL, insights history >90 days, priority support | Everything that needs the company's servers or ongoing cost |
| Lifetime (optional) | ~$99 one-off | Pro forever | Popular in this category (Things, Obsidian); cash-flow vs. ongoing server cost — decide at launch |

Feature gating is by **entitlement claims** (`features: ["sync","push","calendar_feed","insights_full","ad_free"]`)
so tiers can change without client releases.

### 1.2 Payment provider investigation

| Option | Role | Fees (approx., verify at signing) | Tax/VAT handling | Fit |
|---|---|---|---|---|
| **Stripe** (Billing + Checkout + Customer Portal + Stripe Tax) | Payment processor; *you* are the merchant | ~2.9 % + $0.30 + 0.5 % Tax | Stripe Tax calculates; **you** must register and remit in each jurisdiction (EU OSS, UK, US states, …) | Best tooling; heaviest compliance burden for a small team |
| **Paddle** | **Merchant of Record** (MoR) | ~5 % + $0.50 | Paddle is the seller; handles global tax, invoices, refunds, chargebacks | Lowest operational burden; good subscription tooling |
| **Lemon Squeezy** (Stripe-owned) | MoR | ~5 % + $0.50 | Same as Paddle | Simpler, smaller; roadmap is converging into Stripe |
| **Apple IAP / Google Play Billing** | Mandatory for digital subscriptions sold *inside* store-distributed apps | 15 % (small-business / subscription yr 2+) to 30 % | Stores handle tax | Required for App Store/Play builds; cannot be used on web/desktop |
| **RevenueCat** | Entitlement layer over IAP + (optionally) Stripe | Free tier, then ~1 % of tracked revenue | — | Removes receipt-validation pain; unifies store + web subscriptions |

**Recommendation (D4):**

1. **Web and desktop:** Paddle (MoR). The team is small; global tax registration with Stripe is a
   real ongoing cost. Paddle's hosted checkout and customer portal cover upgrade/cancel/refund.
   (If the team already has a Stripe account and accounting support, Stripe + Stripe Tax is the
   equally valid alternative; the integration shape below is the same.)
2. **Store builds (iOS/Android):** native IAP via RevenueCat. Apple guideline 3.1.1 requires IAP
   for in-app digital subscriptions; external-purchase links are allowed in some regions
   (US post-2025 ruling, EU under DMA) but rules change — do not depend on them.
3. **Entitlements unified** in *our* Account Service: both Paddle webhooks and RevenueCat webhooks
   write the same `subscriptions` row. A user who subscribed on iPhone is Pro on desktop and vice
   versa (Apple permits using a subscription bought elsewhere; it only forbids *selling* outside
   IAP from within the app — store builds therefore show "Manage on web" without a price link).

### 1.3 Billing flow

```
Client                 Account Service                 Paddle / RevenueCat
  │  POST /billing/checkout  │                                │
  │─────────────────────────▶│  create checkout (account_id)  │
  │  ◀── checkout URL ───────│───────────────────────────────▶│
  │  (hosted page; we never see card data)                    │
  │                          │ ◀── webhook: subscription.created/updated/canceled,
  │                          │              transaction.completed/payment_failed
  │                          │  verify signature → idempotent upsert subscriptions
  │  GET /me/entitlements    │                                │
  │─────────────────────────▶│  sign entitlement token (JWT, 24 h, grace 7 d)
  │  ◀── token ──────────────│
  │  cache; enforce offline  │
```

Rules:

* **Webhook idempotency:** `billing_events.provider_event_id` UNIQUE; process in a transaction;
  replay-safe. Reconcile nightly with the provider's API (`GET subscription`) to self-heal missed
  webhooks.
* **Entitlement token:** short-lived JWT (ES256, claims: `sub`, `plan`, `features`, `status`,
  `exp`, `grace_until`). Clients work offline until `grace_until` (7 days past period end), then
  downgrade to Free — **never lose data**, only sync/push stop.
* **Server-side enforcement** for anything with server cost: relay and push endpoints check the
  subscription row, not the client's claim.
* **Dunning:** provider retries failed payments for 7 days (`past_due`); we e-mail once via the
  provider's own notifications (we send no marketing mail).
* **Refunds:** 14-day no-questions via provider portal (also satisfies EU distance-selling).
* **Taxes, invoices, receipts:** MoR responsibility; we only link to the provider's portal.

### 1.4 Minimum billing data (the *only* user data on company servers)

| Table | Columns | Justification |
|---|---|---|
| `accounts` | `id`, `email`, `email_verified_at`, `created_at`, `deleted_at`, auth fields (§3) | Login + legally required receipts |
| `subscriptions` | `account_id`, `provider`, `provider_customer_id`, `provider_subscription_id`, `plan`, `status`, `current_period_end`, `cancel_at_period_end`, `updated_at` | Entitlement decisions |
| `billing_events` | `provider_event_id`, `type`, `received_at`, `processed_at` | Idempotency / audit |
| `devices` | `id`, `account_id`, `platform`, `client_version`, `last_seen_at`, `push_token` (encrypted at rest), `name_ciphertext` | Sync ack tracking, push delivery, "sign out everywhere" |
| `sync_ops` (transient) | `account_id`, `seq`, `device_id`, `ciphertext`, `size_bytes`, `created_at`, `expires_at` | Relay — deleted on full ack or TTL |

Explicitly **not** stored: name, address, card data, tax ID, country (provider holds them),
IP addresses beyond 24-hour rate-limit counters, analytics, any plaintext content, any content
metadata beyond byte counts.

---

## 2. Cross-platform client design (desktop · phone · tablet)

### 2.1 One codebase, three shells (D2)

```
                          ┌──────────────────────────────────────────────┐
                          │  packages/core  (TypeScript, no UI)          │
                          │  domain · recurrence · planner · capture ·   │
                          │  sqlite repo · op-log/CRDT · crypto · sync   │
                          └───────────────┬──────────────────────────────┘
                                          │
                          ┌───────────────┴──────────────────────────────┐
                          │  packages/ui  (React + Vite + TS)            │
                          │  adaptive layout · views · design tokens     │
                          └──────┬───────────────┬───────────────┬───────┘
                                 │               │               │
            ┌────────────────────┴───┐  ┌────────┴────────┐  ┌───┴──────────────────┐
            │ apps/web  (PWA)        │  │ apps/mobile     │  │ apps/desktop         │
            │ Win/mac/Linux browsers │  │ Capacitor       │  │ Electron (optional)  │
            │ + installable on all   │  │ iOS / Android   │  │ tray, global hotkey, │
            │ phones/tablets/desktop │  │ App Store/Play  │  │ native notifications │
            └────────────────────────┘  └─────────────────┘  └──────────────────────┘
   Platform adapters (interface in core, one impl per shell):
   Storage · SecureStorage · Notifications · Share/Capture · FilePicker · Background
```

| Shell | Covers | When |
|---|---|---|
| **PWA** | Every OS with Chrome/Edge/Safari/Firefox; "Install" gives a windowed app with icon on Windows, macOS, Linux, Android, iOS (≥16.4 incl. push) | **Phase A** — ships first and is the baseline for all platforms |
| **Capacitor** | iOS and Android store apps: local notifications (no server), share-sheet capture, widgets via native plugins, Keychain/Keystore, IAP | Phase C |
| **Electron** *(only if needed)* | System tray, global quick-capture hotkey, auto-start, native notifications on Linux | Phase D, demand-driven. Electron chosen over Tauri deliberately: Tauri requires a Rust toolchain to build, which has already cost this team time; Electron is Node-only. Trade-off: ~150 MB installers. Revisit if size matters. |

**Framework choice:** ~~React + TypeScript + Vite~~ → **Angular + TypeScript** (decided after this
document was written; implemented in `apps/web`). Angular brings the service-worker package
(`@angular/service-worker`) that drives the automatic-update mechanism, a batteries-included
toolchain (CLI, Vitest, strict templates) and a single opinionated way of doing things, which
suits a small team. The shell layering above is unchanged: the `core` logic (db, sync, crypto,
update policy) has no Angular dependency beyond dependency injection and can be reused by
Capacitor/Electron shells.

**Storage:** SQLite everywhere — `@sqlite.org/sqlite-wasm` with OPFS in browsers (Safari ≥17,
Chrome, Firefox ≥111), `@capacitor-community/sqlite` natively, `better-sqlite3` in Electron. One
schema, one migration runner (in `core`), one repository layer. The database file is the user's;
"Export everything" = copy of the SQLite file + JSON + ICS.

### 2.2 Adaptive layout (not just responsive)

| Form factor | Breakpoint | Navigation | Layout | Input |
|---|---|---|---|---|
| Phone | < 640 px | Bottom tab bar (Today · Tasks · Habits · Calendar · More); FAB for capture | Single column, stacked; sheets for edit | Touch ≥ 44 px targets; swipe complete/snooze; pull-to-refresh |
| Tablet | 640–1024 px, or any width in portrait | Sidebar rail (icons) | Two-pane: list + detail; split-view safe | Touch + keyboard; Apple Pencil handwriting to text via OS |
| Desktop | > 1024 px | Sidebar (labels) | Three-pane: nav · list · detail/today; multiple windows in Electron | Keyboard-first: `⌘/Ctrl-K` command palette, `N` new, `J/K`; drag-and-drop |

Cross-cutting: dark mode (system), `prefers-reduced-motion`, safe-area insets (notch), text
scaling, RTL-ready, WCAG 2.1 AA (axe in CI), offline indicator, optimistic UI (everything writes to
local SQLite first — no spinners for CRUD).

### 2.3 Platform-specific capabilities

| Capability | PWA | Capacitor | Electron |
|---|---|---|---|
| Reminders | Web Push (server "tickle", §4.6) | **Local notifications, scheduled on device, zero server** | Local (node-notifier / OS) |
| Quick capture | Share Target API (Android), URL scheme | Share sheet, widgets, Siri/Assistant shortcuts | Global hotkey, tray |
| Secure key storage | WebCrypto non-extractable keys + IndexedDB | Keychain / Keystore | OS keychain (keytar/safeStorage) |
| Background sync | Limited (Android only) | Background fetch (best effort) | Always-on |
| Calendar | ICS subscription URL (Pro) | Native calendar plugin (read), EventKit/CalendarContract | ICS |
| Offline | Full (service worker + OPFS) | Full | Full |

---

## 3. Authentication & authorization

### 3.1 Requirements

* Accounts are **optional** (Free tier works without one).
* The server must never be able to read content ⇒ the encryption key must not be derivable by the
  server ⇒ key material derives client-side.
* Minimum stored: e-mail + verifier. No phone number, no social-profile data.
* Multi-device with explicit device list and remote sign-out.
* Works offline for days; survives token expiry gracefully.

### 3.2 Credential model (D5 — Bitwarden-style single password)

```
password + email(salt) ──Argon2id(m=64MiB,t=3,p=4)──▶ master_key (32 B)   never leaves device
master_key ──HKDF("auth")──▶ auth_key ──▶ sent at login as the verifier (server stores Argon2id(auth_key))
master_key ──HKDF("wrap")──▶ wrap_key ──▶ wraps a random data_key (XChaCha20-Poly1305)
data_key   ──▶ encrypts all sync payloads and backups
wrapped_data_key stored on server (ciphertext) so any device that knows the password can unwrap it
```

* Password change = re-wrap `data_key` with new `wrap_key`; no data re-encryption.
* **Recovery key** (24-word BIP39-style encoding of `data_key`) shown once at sign-up, must be
  confirmed by re-entering 3 words; regenerable from any signed-in device. Without password *or*
  recovery key, synced data is unrecoverable — the UI says so in plain words.
* **Passkeys (WebAuthn)** as a second login method; where the PRF extension is available (Chrome,
  Safari/iOS 18+, Android) the passkey can also derive `wrap_key`, enabling password-less E2EE.
  Rolled out after launch; password remains the universal fallback.
* **OAuth "Sign in with Apple/Google"** is *not* offered at launch: it would still require a separate
  encryption passphrase (two secrets = confusion), and offering Google on iOS forces Sign in with
  Apple. Revisit with passkeys.
* Optional **TOTP 2FA** for login (protects the wrapped key blob from offline attack after a
  password leak).

### 3.3 Sessions & tokens

| Token | Lifetime | Storage | Notes |
|---|---|---|---|
| Access JWT (ES256) | 15 min | Memory | Claims: `sub`, `dev` (device id), `scope` |
| Refresh token | 30 days, **rotating**, one per device | Web: `HttpOnly; Secure; SameSite=Strict` cookie · Capacitor: Keychain/Keystore · Electron: `safeStorage` | Reuse detection revokes the whole device family |
| Entitlement JWT | 24 h + 7-day grace | Local DB | See §1.3 |

Login hardening: Argon2id verifier, per-account and per-IP rate limits (sliding window, 24 h
retention only), constant-time compare, e-mail verification before sync is enabled, new-device
e-mail notice, "sign out everywhere" (revokes all refresh tokens; data stays on devices but is
re-locked until password re-entry).

### 3.4 Authorization model

There is little to authorize because the server holds no content:

| Principal | Can | Cannot |
|---|---|---|
| **Account owner** | Read/write own `sync_ops`, `devices`, `subscriptions` (read), delete account | Access any other account's rows (row-level `WHERE account_id = :sub` enforced in the repository layer, tested) |
| **Device** (access token `dev` claim) | Push/pull ops for its account; ack its own cursor | Act as another device |
| **Support staff** | Look up account by e-mail: status, plan, device count, last seen; trigger re-send verification; refund via provider portal | See ciphertext or content; **no "impersonate"** feature exists |
| **Admin** | Feature flags, plan catalogue, webhook replay | Same as above |
| **Service (webhook)** | Update `subscriptions` after signature verification | Anything else |

Staff actions are written to an append-only audit log (who, what, when, account id). Staff access
requires SSO + hardware 2FA. Client-side feature gating (Pro UI) is convenience only; server cost
features are enforced server-side.

### 3.5 Local lock

Device-level protection is the user's OS (Face ID/Touch ID/Windows Hello via Capacitor biometric
plugin or WebAuthn `userVerification`), plus optional app PIN that re-locks the local database key
after N minutes. Local SQLite is encrypted at rest with `data_key` on native shells (SQLCipher via
the Capacitor plugin); in browsers it relies on OS user-profile isolation (documented limitation).

---

## 4. Privacy-first data management

### 4.1 Data classification

| Class | Examples | Where it lives | Who can read |
|---|---|---|---|
| **Content** | tasks, events, notes, habits, plans, tags, settings, device names | User devices (SQLite); user-owned backups; relay as ciphertext | User only |
| **Account** | e-mail, auth verifier, wrapped data key, device list | Account Service (Postgres, EU region) | Company (verifier and wrapped key are useless without password) |
| **Billing** | customer/subscription ids, plan, status, period | Account Service + provider | Company, provider |
| **Operational** | rate-limit counters (24 h), error traces (scrubbed), aggregate metrics | Account Service | Company |
| **Telemetry** | feature usage | **None by default.** Opt-in, aggregate counts only, no identifiers | — |
| **Sponsored content** | our campaigns; views/clicks per ad × day × country; the active ad source (operator setting) | Account Service — anonymous totals only, no identifiers by construction (`ADVERTISING.md`). Third-party ad sources (AdSense, Microsoft, network tags) run only after the user's consent in the panel | Company |

### 4.2 Local data model (client SQLite)

Same entities as `TECH_DESIGN.md` §4.4 (tasks, events, habits, completions, plans, reminders,
notes, projects, focus sessions) with three changes for sync:

* Primary keys are **UUIDv7** (generated on device, globally unique, time-ordered).
* Every table has `updated_at_hlc` (hybrid logical clock) and `deleted_at` (tombstones).
* An `oplog` table records every mutation: `(op_id, entity, entity_id, field, value, hlc, device_id)`.

### 4.3 Sync = encrypted op-log with per-field last-writer-wins

* Each local mutation appends ops. Ops are batched, serialised (CBOR), compressed, encrypted with
  `data_key` (XChaCha20-Poly1305, random nonce, AAD = account id + seq) and pushed as one blob.
* Server assigns a monotonic `seq` per account and stores `(seq, device_id, ciphertext, size)`.
  It can see: who pushed, when, how many bytes. Nothing else.
* Devices pull `seq > cursor`, decrypt, apply ops with **LWW per field by HLC** (ties broken by
  device id). This is convergent, simple to reason about and to property-test; text notes use LWW
  at the note level (no collaborative editing — single user).
* Ops are idempotent (`op_id` UNIQUE locally), so retries and duplicates are harmless.
* **Ack-based deletion (D3):** each device reports its cursor. When `min(cursor over active devices)
  ≥ seq`, the server deletes that op. Devices not seen for 30 days are marked inactive (and the
  user is told). Hard TTL 30 days regardless. Result: the company holds ciphertext for hours,
  not years.
* A new device needs a **snapshot**: an existing device uploads an encrypted full snapshot on
  request (same pipeline, flagged `snapshot=true`, deleted after the new device acks). If no other
  device is online, the user restores from their own backup (§4.5).

### 4.4 Optional durable vault (opt-in)

Some users will want "my data is in the cloud, encrypted" without managing backups. Offer
**Encrypted Vault** as an explicit toggle: the latest snapshot is retained (ciphertext, replaced on
each upload, deleted on toggle-off or account deletion). This is the only way the company ever
holds content for more than 30 days, and it is off by default. The privacy page shows exactly
which mode an account is in.

### 4.5 User-owned backup & portability

* **Export** (all tiers): SQLite file, JSON, ICS, Markdown for notes — one tap, no account needed.
* **Encrypted backup to the user's own storage** (all tiers): file save (desktop), iCloud Drive /
  Google Drive / Files app via OS file picker, WebDAV URL. Encrypted with `data_key` (or a separate
  backup passphrase for accountless users). Scheduled daily on native shells.
* **Import** from the same formats, plus Todoist/Things/Google Tasks CSV in a later phase.

### 4.6 Notifications without content

* Native shells: reminders are scheduled **on the device**; the server is not involved.
* PWA: browsers cannot fire a future notification offline, so the client registers
  `(fire_at, encrypted_payload ≤ 4 KB)` with the server; at `fire_at` the server sends the
  encrypted payload via Web Push (itself encrypted end-to-end per RFC 8291 — the push service sees
  nothing). The server learns *that* a reminder exists at a time — the documented metadata leak.
  Option for stricter users: "reminders only on native apps" toggle. Rows are deleted on send.

### 4.7 Retention, deletion, legal

| Data | Retention |
|---|---|
| Relay ops | Until acked, max 30 days |
| Vault snapshot (opt-in) | Until replaced / toggled off / account deleted |
| Push schedule rows | Deleted on send; max 90 days ahead |
| Rate-limit counters | 24 h |
| Error traces | 30 days, content-free |
| Account row | Until deletion request; then **purged within 30 days** |
| Billing records | Kept by the provider (MoR) for its statutory period (typically 7–10 years); we keep only `provider_customer_id` in a tombstone |

Compliance posture: GDPR data-minimisation by design; DSAR export = account/billing rows (content
is already in the user's hands); deletion self-service in-app; DPAs with host, e-mail provider,
billing provider; EU hosting region; no cookies other than the auth cookie (no consent banner
needed); privacy policy written from the tables in this section; `security.txt`; breach
notification runbook (72 h). CCPA: no sale of data, trivially.

### 4.8 What remains of the Python backend

```
account-service/ (FastAPI, Postgres, S3-compatible object store for blobs > 64 KB)
  /auth/*         register · verify-email · login (verifier) · refresh · logout · passkeys · totp
  /me             account · devices · entitlements · export · delete
  /billing/*      checkout · portal-link · webhooks/{paddle,revenuecat}
  /sync/*         push · pull?since= · ack · snapshot-request · vault
  /push/*         subscribe · schedule · unschedule
  /health /metrics(private)
```

No tasks/events endpoints. `docs/TECH_DESIGN.md` §4.7 algorithms (recurrence, planner, capture)
are implemented in `packages/core` (TypeScript) instead of Python.

---

## 5. Deployment & automated testing

### 5.1 Repository layout (monorepo)

```
personal-assistant/
  packages/core/         TS domain + sync + crypto (no DOM, no platform APIs)
  packages/ui/           React components, adaptive layout
  apps/web/              PWA (Vite) → static hosting
  apps/mobile/           Capacitor project (ios/, android/)
  apps/desktop/          Electron (optional)
  services/account/      FastAPI Account Service (Python, venv per README)
  infra/                 Terraform/OpenTofu for Postgres, object store, DNS, secrets
  e2e/                   Playwright + Maestro flows
  .github/workflows/     CI/CD
```

### 5.2 Environments & release trains

| Env | Account Service | Clients | Billing | Purpose |
|---|---|---|---|---|
| `dev` | Docker Compose locally (Postgres + MinIO + MailHog) | Vite dev server, local Capacitor builds | Provider sandbox | Daily work |
| `staging` | Same image as prod, separate DB, EU region | `staging.app…` PWA; TestFlight / Play internal track | Provider sandbox, webhook replay | Pre-release verification, E2E |
| `prod` | Blue/green on Fly.io or Render (EU); managed Postgres with PITR; object store with lifecycle rule (30 d) | CDN (Cloudflare Pages); App Store / Play; GitHub Releases + auto-update (Electron) | Live | — |

Release cadence: Account Service continuous (every green `main`); PWA continuous (service worker
prompts "update available"); store apps every 2 weeks (review latency); desktop with PWA.
**Compatibility contract:** sync protocol and op schema are versioned; a client must read ops from
one major version older and newer (unknown fields ignored, unknown entities kept opaque and
re-emitted) — tested in CI with a stored corpus.

### 5.3 CI/CD pipelines (GitHub Actions)

```
PR ──▶ lint/typecheck (ruff, mypy --strict | eslint, tsc)
   ──▶ unit + property tests (pytest | vitest + fast-check)
   ──▶ contract tests: OpenAPI schema → schemathesis against Account Service
   ──▶ privacy regression suite (§5.4)
   ──▶ build PWA + Lighthouse CI (PWA installable, perf ≥ 90, a11y ≥ 95)
   ──▶ Playwright E2E (desktop + phone + tablet viewports) against staging-like compose
   ──▶ security: CodeQL/Semgrep, dependency audit (pip-audit, npm audit), gitleaks, SBOM
merge to main ──▶ build & sign images (cosign) ──▶ deploy Account Service to staging ──▶ smoke
            ──▶ deploy PWA to staging ──▶ Maestro mobile E2E on device cloud (nightly)
tag vX.Y.Z ──▶ prod deploy (blue/green, auto-rollback on health/metrics)
            ──▶ Fastlane: iOS (TestFlight → phased release) · Android (internal → 10 % → 100 %)
            ──▶ Electron: signed + notarised installers → GitHub Release → auto-updater
```

Code signing: Apple Developer Program (notarisation), Windows via Azure Trusted Signing or EV
cert, Android upload key in Play App Signing. Secrets only in the CI secret store; never in repo.

### 5.4 Test strategy

| Layer | Tooling | What it proves |
|---|---|---|
| **Core unit** | Vitest | Recurrence, planner, capture parser, timezone/day-boundary, migrations |
| **Property-based** | fast-check | Sync convergence: any interleaving/duplication/reordering of ops on N replicas converges; ops are idempotent; LWW is commutative. Crypto round-trip. |
| **Golden/corpus** | Stored op-logs from every released protocol version | Forward/backward compatibility |
| **Crypto** | Known-answer tests (libsodium vectors), key-derivation parameters pinned | No silent algorithm drift |
| **Privacy regression** (CI gate) | Playwright intercepts every request from the client; asserts bodies to `/sync`, `/push` are high-entropy ciphertext, contain no task titles from the fixture, and no request goes to third-party hosts | *The* test that enforces the hard requirement |
| **Account Service** | pytest + in-memory/`testcontainers` Postgres; schemathesis from OpenAPI | Auth flows, row-level isolation (account A cannot read B — parametrised over every endpoint), webhook idempotency, ack-deletion |
| **Billing** | Provider sandbox + replayed webhook fixtures; subscription lifecycle (trial → active → past_due → canceled) with time-travel | Entitlement state machine |
| **E2E web** | Playwright: phone (390×844), tablet (820×1180), desktop (1440×900) projects; offline mode via `context.setOffline` | Full flows: capture → plan → complete → review; two-browser-context sync; install prompt |
| **E2E mobile** | Maestro on BrowserStack/Firebase Test Lab, nightly | Local notifications fire, share-sheet capture, IAP sandbox purchase |
| **Accessibility** | axe-core in Playwright | WCAG AA on every view |
| **Performance** | Lighthouse CI; k6 against `/sync` (1k devices, 10k ops/min) | PWA budgets; relay throughput |
| **Security** | CodeQL, Semgrep, pip-audit/npm audit, gitleaks; annual external pentest; threat model reviewed each quarter | — |
| **Chaos/recovery** | Restore Postgres PITR into staging monthly; kill relay mid-push in E2E | Backups work; clients resume |

Definition of done for any feature touching sync or auth: unit + property + privacy regression +
at least one E2E.

### 5.5 Observability without surveillance

Metrics are aggregate (request counts, latencies, ops/sec, bytes relayed, subscription counts).
Logs contain account ids only where needed for abuse handling, never e-mail or content. Error
tracking (Sentry, self-hosted or EU) with PII scrubbing; client crash reports are **opt-in**,
strip breadcrumbs of user text. Uptime checks on `/health`. Alerting: webhook failure rate,
relay backlog age, auth error spikes.

---

## 6. Roadmap, effort, open questions

### 6.1 Phases

| Phase | Scope | Est. (2 engineers) | Exit criteria |
|---|---|---|---|
| **A — Local-first core & PWA** | `packages/core` (domain, SQLite, oplog), `packages/ui`, `apps/web` PWA, export/backup, local reminders (native later) | 8 wks | Free tier usable on phone, tablet, desktop browsers; installable; offline; privacy regression test exists (asserts *no* network content) |
| **B — Accounts, sync, billing** | Account Service (auth, devices, relay, ack-deletion, entitlements), client crypto, Paddle integration, customer portal, account deletion/export | 8 wks | Two devices converge under property tests and E2E; checkout → Pro in < 1 min; webhook replay safe; DSAR/deletion self-service |
| **C — Store apps** | Capacitor iOS/Android, local notifications, share capture, RevenueCat IAP, Fastlane pipelines | 6 wks | TestFlight/Play internal; IAP purchase grants Pro on web too |
| **D — Desktop shell & extras** | Electron (tray, hotkey), passkeys + PRF, vault opt-in, ICS feed, insights | 6 wks | — |

### 6.2 Risks

| Risk | Mitigation |
|---|---|
| Users lose password + recovery key → data loss → support load & reputation | Mandatory recovery-key confirmation; periodic "verify your recovery key" nudge; local data is never lost, only sync |
| Apple rejects store build over subscription messaging | Follow 3.1.1 strictly; no external price links in iOS build; legal review of copy |
| Ack-based deletion deletes ops a stale device still needs | Inactive-device rule (30 d) + snapshot-on-demand + user warning |
| Team unfamiliar with TypeScript core | Phase A starts with a thin vertical slice (tasks only) to build competence; Python remains for the service |
| MoR pricing erodes margin at scale | Entitlement layer is provider-agnostic; migrate to Stripe later if revenue justifies tax ops |
| Browser storage eviction (Safari 7-day rule for *unused* PWAs) | Request `navigator.storage.persist()`; installed PWAs are exempt; nudge to install or to native app |

### 6.3 Open questions for you

1. **Self-hosted mode:** keep the current FastAPI task API alive as a "bring your own server"
   option, or retire it entirely once the local-first client ships? (Recommendation: retire; the
   local-first client *is* the no-account mode.)
2. **Lifetime plan:** offer it at launch or not?
3. **Paddle vs. Stripe:** does the company already have tax registration/accounting support?
4. **Durable vault (§4.4):** acceptable as an opt-in, or should the product be relay-only?
5. **Hosting region:** EU-only, or EU + US for latency (relay is latency-tolerant; EU-only is
   simpler for compliance)?
6. **Electron vs. Tauri** for desktop, given the Rust-toolchain concern?
