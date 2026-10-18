# Application updates, data sync and migrations

How new code reaches every device, how local data structures evolve across versions, and how
the two stay compatible. Everything here is implemented and covered by tests
(`apps/web/src/**/*.spec.ts`, `services/account/tests`, `e2e/`).

## 1. The pieces

```
 git tag vX.Y.Z ──▶ GitHub Actions (deploy.yml)
      │ 1. build+push image, roll Container App, alembic upgrade head      (API first)
      │ 2. npm run build → upload to Azure Static Web Apps (CDN)            (PWA files live)
      │ 3. POST apps/web/release.json → /api/v1/release/publish              (manifest live)
      ▼
 CDN: index.html · ngsw.json (hash of every file + appData) · main-<hash>.js …
      ▲ ngsw-worker.js on every device compares ngsw.json with its cache
      │
 Device (PWA)                                  Account & Sync Service (FastAPI + PostgreSQL)
 ┌──────────────────────────────────┐          ┌──────────────────────────────────────────┐
 │ Angular app  v1.0.4, schema 2    │──GET────▶│ /release/manifest  {version, critical,   │
 │ UpdateService (policy below)     │          │   min_supported_version/schema, notes}   │
 │ IndexedDB (Dexie, migrations)    │──X-Client-Version / X-Schema-Version on every call──▶ 426 if too old
 │ outbox → SyncService → AES-GCM ──┼──push/pull/ack/snapshot (ciphertext)──▶ sync_ops (seq!)
 └──────────────────────────────────┘          └──────────────────────────────────────────┘
```

`apps/web/release.json` is the **single, version-controlled release descriptor**:

```json
{ "version": "1.0.4", "schemaVersion": 2, "minSupportedVersion": "1.0.0",
  "minSupportedSchema": 2, "critical": false, "channel": "stable", "notes": "…" }
```

`scripts/prepare-release.mjs` (runs before every build) writes it into `src/app/core/release-info.ts`
(the app knows its own version) **and** into `ngsw-config.json → appData` (the service worker can
tell a running app what the *new* build is, including `critical`, before activating it).

## 2. Application-code updates (PWA service worker)

| Situation | What happens |
|---|---|
| App reopened after a release | The service worker serves the cached build instantly (works offline), fetches `ngsw.json`, sees a new hash, downloads the new files in the background, then `VERSION_READY` → the app activates it and reloads itself (default). First paint: old build; a few seconds later: new build. Data in IndexedDB is untouched. |
| App open while a release is published | Checks run on every return to the tab (`visibilitychange`), when coming back online, and every 6 h. Same flow. |
| User is mid-edit (dialog open, typing) | The download still happens; activation waits until it is safe (`UiStateService.safeToReload`). The Update button's tooltip says the version is ready. |
| **Auto-update off** (checkbox in the Update dialog, default **on**) | Download happens; a banner + the tooltip announce it; nothing reloads until the user taps *Install*. Re-opening the app still gets the new build (that is how service workers work) — the checkbox controls *in-session* activation. |
| **Critical release** (`critical: true`) | Forced: a modal gate (`ForcedUpdate`, cannot be dismissed) shows "Update required"; the build is activated the moment it is downloaded — auto-update off or a dialog open make no difference. |
| Client below `minSupportedVersion` / `minSupportedSchema` | Same forced gate, reason "no longer supported". The API additionally answers **426 Upgrade Required** to its sync calls (`releases.enforce_supported`), so a stale client can never push data in an old format. |
| Manifest says 1.0.6 but the CDN has not propagated yet | The gate shows "Downloading…" and the app keeps checking (no crash, no loop); the build is picked up when it appears (`test_manifest_published_before_the_build_is_live_keeps_downloading`). |
| Download fails (quota, network, hash mismatch) | `VERSION_INSTALLATION_FAILED` → plain-language error in the dialog/tooltip, *Try again*; the current build keeps working. |
| Cached build corrupted (storage evicted) | ngsw `unrecoverable` → reload fetches a fresh copy. |
| Browser without service workers | The Update button explains it; *Reload* loads the newest build directly. |

Decision logic is a pure function, `update-policy.ts → decide()`, unit-tested for every row above.

CDN caching rules (`staticwebapp.config.json`, and `PwaStatic` when the API serves the files):
`index.html`, `ngsw.json`, `ngsw-worker.js`, `manifest.webmanifest` → `no-cache`; hashed bundles
→ `immutable`. Without this, a CDN could hide a new release for hours.

## 3. Local data and version-controlled migrations

All user content lives in IndexedDB on the device (`AssistantDb`, Dexie). Two things change
shape over time and are kept in **one table**, `apps/web/src/app/core/db/migrations.ts`:

```ts
export const SCHEMA_VERSION = 2;
export const MIGRATIONS = [
  { version: 1, stores: { tasks: 'id, status, dueAt, updatedAt', events: 'id, startAt', outbox: '++localSeq, op.opId', meta: 'key' } },
  { version: 2, stores: { tasks: 'id, status, dueAt, updatedAt, *tags' },
    upgrade: (tx) => /* add tags/completedAt to existing rows */,
    upcastOp: (op) => /* same transformation for an op written by a v1 app */ },
];
```

1. **Database migration** — when a newer app opens an older database, Dexie runs the missing
   `stores`/`upgrade` steps before the UI renders. Rules: never edit a step, only append;
   `release.json → schemaVersion` must equal `SCHEMA_VERSION` (a unit test enforces this, CI
   checks the built `ngsw.json` against `release.json`).
2. **Op up-casting** — every sync operation carries the `schema` it was written with. A device
   receiving an op from an older app up-casts it step by step before applying it. An op (or
   snapshot) from a **newer** schema is never applied: `UnsupportedSchemaError` → sync pauses
   with "update required" and the update flow takes over. The server refuses pushes from
   clients below `minSupportedSchema`, so a fleet can be moved forward deliberately: ship the
   app that understands schema N+1 first, then raise `minSupportedSchema` once old clients are
   gone (or immediately with `critical: true` when the formats are incompatible).
3. **Server migrations** (PostgreSQL) — Alembic, `services/account/alembic/versions/`, applied by
   the container's start command (`alembic upgrade head`) before the API serves traffic. The
   service's test-suite runs against PostgreSQL through the migration chain (downgrade → upgrade).

## 4. Sync: local cache, server as master of order

Requirement tension, stated plainly: *"save all user data locally, only billing data in the
central database"* and *"treat the server as the master system"* cannot both be literal.
The implemented reconciliation:

* **Content never reaches the server in readable form.** Each op is AES-256-GCM-encrypted on the
  device with a key derived from the user's password (`crypto.ts`; the server stores only a
  login verifier and the *wrapped* data key). The privacy test (`e2e/test_sync_privacy.py`)
  intercepts every request and a full `pg_dump` to prove no title or password appears anywhere.
* **The server is the master of ordering and of the reference copy.** `POST /sync/push` assigns a
  strictly increasing per-account `seq` to every op (row lock on the account). Devices apply
  ops in `seq` order; for one entity the op with the higher `seq` wins. This is deterministic on
  every device, so all replicas converge — and it is what "server is master" means here: the
  device's IndexedDB is a cache that can be thrown away and rebuilt (*Reset from server* in the
  account dialog) from the encrypted snapshot + the op sequence.
* **Relay, not archive.** Ops are deleted once every active device has acked them (or after
  30 days). The optional-but-default **vault** keeps one encrypted full snapshot so a new device
  can bootstrap without another device being online. Turning the vault off deletes it.
* **Offline-first.** Every local mutation is written to the entity table *and* the outbox in one
  IndexedDB transaction; sync runs when online, on tab focus, after each change (debounced) and
  every 5 minutes. Conflicts resolve by server order; nothing is ever blocked on the network.

One sync run: `push outbox → pull since cursor (skip own ops, apply in order) → ack cursor →
upload snapshot if something changed and the vault is on`.

## 5. Operating a release

```bash
python dev.py release 1.0.5 --notes "…"                 # bump release.json (add --critical for a forced update)
# touching the local data format? bump SCHEMA_VERSION + schemaVersion, add a MIGRATIONS step, add a spec
git commit -am "Release 1.0.5" && git tag v1.0.5 && git push --tags   # → deploy.yml
```

Retiring old clients later: raise `minSupportedVersion`/`minSupportedSchema` in a subsequent
release; clients below it get the forced gate and 426s. Emergency without a new build: publish a
manifest by hand (`python dev.py publish https://api… ` with `critical: true`) — clients on older
versions are forced to the newest build that is already on the CDN.
