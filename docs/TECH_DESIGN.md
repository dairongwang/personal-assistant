# Personal Assistant — Investigation & Technical Design

| | |
|---|---|
| Status | Draft for review |
| Scope | Backend (FastAPI/SQLite), Frontend (single-file Alpine.js), Deployment |
| Baseline | commit `eb67f8d` ("baseline") |

> **Update:** the product direction changed to a commercial, multi-device app with a
> *privacy-first* rule (only billing data on company servers). The investigation (§2) and feature
> roadmap (§3) remain valid. The architecture in §4.2–4.9, which keeps user data in a server-side
> FastAPI/SQLite database, is **superseded by [`PLATFORM_DESIGN.md`](PLATFORM_DESIGN.md)** (local-first
> client, zero-knowledge account service). The algorithms in §4.7 carry over, implemented in the
> TypeScript core instead of Python.

---

## 1. Executive summary

The app today is a clean, minimal **task + event dashboard**: a FastAPI backend over SQLite, and a
single HTML page (Tailwind + Alpine.js). It works, but it is a *list*, not yet an *assistant*. It
tells you what is due; it does not help you decide what to do, remind you to do it, or help you
build habits around it.

This document:

1. Records what was found during investigation (including several real bugs).
2. Proposes features that move the product from "task list" to "assistant that helps people
   live and work better", prioritised by user value vs. effort.
3. Gives a technical design (architecture, data model, APIs, phasing) for delivering them.

**Recommended direction:** keep the lightweight stack (FastAPI + SQLite + no-build frontend) — it is
a good fit for a personal tool — but (a) fix the foundation (timezones, validation, tests,
structure), then (b) build the four features with the best value/effort ratio:
**Recurring tasks & habits, Daily plan/review ritual, Reminders, and Quick capture (natural-language
input)**, followed by calendar import/export and PWA/offline support.

---

## 2. Investigation findings

### 2.1 What exists

| Layer | Tech | Notes |
|---|---|---|
| API | FastAPI 0.115, Pydantic v2, SQLAlchemy 2.0 | 9 endpoints in one `main.py` (152 lines) |
| Storage | SQLite via SQLAlchemy, `create_all()` on startup | Alembic is in `requirements.txt` but not configured |
| Frontend | One `index.html`, Tailwind CDN + Alpine.js CDN | `API` URL hard-coded to `http://localhost:8000` |
| Deploy | Dockerfile + docker-compose | Compose mounts `./backend` and a named volume, but DB path is `./assistant.db` (i.e. the bind mount, not the volume) |
| Auth | none | `python-jose` and `passlib[bcrypt]` are installed but unused |
| Tests | none | README says "add pytest later" |

Endpoints: `GET/POST /tasks`, `GET/PATCH/DELETE /tasks/{id}`, `GET/POST /events`, `GET /today`,
`GET /health`. Events cannot be updated or deleted.

### 2.2 Bugs and defects found (verified by running the app)

The backend was run from the project venv (`backend/venv`) and exercised with `curl`.

> **Status:** B1–B10 are **fixed** in v0.2.0 (timezone-aware storage and `/today?tz=`,
> `completed_at`, priority ranking, validation, event PATCH/DELETE, pagination, modernised APIs,
> Docker data volume, dependency-free frontend, CORS off by default). Regression tests live in
> `backend/tests/`.

| # | Severity | Finding | Evidence |
|---|---|---|---|
| B1 | **High** | **Timezone information is silently discarded.** Frontend sends `new Date(...).toISOString()` (UTC, `...Z`). SQLAlchemy `DateTime` (naive) drops the offset; `/today` then compares against `date.today()` in the *server's* timezone. For any user not in the server's TZ, "due today" / "overdue" are wrong near day boundaries, and displayed times are shifted. | `POST /tasks` with `"due_date":"2026-10-15T23:30:00Z"` → stored/returned as `2026-10-15T23:30:00` (no `Z`). |
| B2 | Medium | **"Completed today" is a heuristic on `updated_at`.** Editing the title of a task finished last week counts it as completed today. There is no `completed_at` column. | `main.py:136-139` |
| B3 | Medium | **Priority sorting is alphabetical.** `order_by(priority.desc())` on a string column yields `urgent > medium > low > high`. | `GET /tasks` returned `urgent, high, medium, medium` only by coincidence of due-date order. |
| B4 | Medium | **No input validation beyond enums.** Empty titles are accepted; events with `end_time < start_time` are accepted. | `POST /tasks {"title":""}` → 200, id 4. `POST /events` with end before start → 200. |
| B5 | Low | Unknown filter values return `[]` silently instead of 422 (`?status=bogus`). | verified |
| B6 | Low | Events have no `PATCH`/`DELETE`; `tags` exist in the model but are not exposed in the UI. | `PATCH /events/1` → 404 Not Found |
| B7 | Low | No pagination on list endpoints. | — |
| B8 | Low | Deprecated APIs: `@app.on_event` (→ lifespan), `sqlalchemy.ext.declarative.declarative_base` (→ `sqlalchemy.orm`), Pydantic `class Config` (→ `ConfigDict`), `datetime.utcnow()` (removed in a future Python). | `python -W always -c "import main"` |
| B9 | Low | `docker-compose.yml` declares a data volume at `/app/data` but the app writes `./assistant.db` in `/app` (the bind mount), so the named volume is unused and the DB is committed-adjacent (`*.db` is git-ignored, but `backend/assistant.db` is present in the upload). | — |
| B10 | Low | Frontend loads Tailwind and Alpine from CDNs (Tailwind's CDN build is explicitly "not for production"); the page is blank offline. `allow_origins=["*"]` with `allow_credentials=True`. | — |

### 2.3 Product gaps (why it is not yet an "assistant")

* **No memory of repetition** — rent, workouts, weekly reviews must be re-entered by hand.
* **No nudges** — nothing reaches the user; they must open the page.
* **No planning ritual** — the dashboard shows counts but never asks "what are your top 3 today?"
* **High capture friction** — a modal with 4 fields to note "call dentist tomorrow".
* **No reflection** — no history, streaks or trends, so there is no feedback loop.
* **Calendar is an island** — events live only in this SQLite file; the user's real calendar is elsewhere.
* **Not installable/offline** — on a phone it is a browser tab that dies without network.

---

## 3. Proposed features

Scored on **user value** (how much it helps someone live/work better) and **effort**
(S ≤ 2 days, M ≤ 1 week, L > 1 week). Priority = value ÷ effort.

### 3.1 Priority matrix

| # | Feature | Value | Effort | Priority | Phase |
|---|---|---|---|---|---|
| F1 | Recurring tasks & habit streaks | ★★★★★ | M | **P0** | 1 |
| F2 | Daily plan & evening review ritual ("Top 3", carry-over) | ★★★★★ | M | **P0** | 1 |
| F3 | Reminders & notifications (Web Push, email) | ★★★★★ | M | **P0** | 2 |
| F4 | Quick capture with natural-language parsing | ★★★★ | S | **P0** | 1 |
| F5 | Snooze / defer ("tomorrow", "next week", "someday") | ★★★★ | S | **P0** | 1 |
| F6 | Projects, sub-tasks, tags in UI, search | ★★★★ | M | P1 | 2 |
| F7 | Focus mode (Pomodoro timer bound to a task, time tracking) | ★★★ | S | P1 | 2 |
| F8 | Calendar import/export (ICS) → later Google/Microsoft OAuth sync | ★★★★ | M / L | P1 / P2 | 2 / 4 |
| F9 | Weekly review & insights (completion rate, overdue trend, busiest days, workload warning) | ★★★★ | M | P1 | 3 |
| F10 | Daily journal, mood & energy check-in (wellbeing) | ★★★ | S | P1 | 3 |
| F11 | PWA: installable, offline cache, background sync | ★★★★ | M | P1 | 3 |
| F12 | Authentication & multi-device sync | ★★★ (prereq for hosting) | M | P1 | 2 |
| F13 | AI assistant layer (briefing, task breakdown, smart scheduling) — provider-pluggable, optional | ★★★ | L | P2 | 4 |
| F14 | Break / hydration / posture nudges during focus sessions | ★★ | S | P2 | 4 |

### 3.2 Feature descriptions

**F1 — Recurring tasks & habits.**
A task may carry a recurrence rule (RFC 5545 RRULE: `FREQ=WEEKLY;BYDAY=MO,WE,FR`). Completing an
occurrence records a `TaskCompletion` and materialises the next occurrence. *Habits* are recurring
tasks with `kind = habit`: they are never "overdue", they track **streaks** (current, longest) and a
weekly completion rate. Examples: "Pay rent (monthly on the 1st)", "Stretch (daily)", "Call mum
(weekly, Sunday)".

**F2 — Daily plan & review ritual.**
Two lightweight flows built on a `DailyPlan` record:
* *Morning plan* — the app proposes candidates (due today, overdue, habits due, calendar gaps) and
  asks the user to pick up to **3 focus tasks**. Shows a workload estimate (sum of `estimate_minutes`
  vs. free calendar time) and warns when over-committed.
* *Evening review* — shows what was completed, asks what to do with the rest (carry to tomorrow,
  reschedule, drop), and captures a one-line reflection. Generates a streak for "reviewed today".

**F3 — Reminders & notifications.**
`Reminder` rows (`remind_at`, `channel`) attached to tasks/events; a scheduler (APScheduler, in-process)
dispatches via Web Push (VAPID) and optional SMTP e-mail. Defaults: event → 15 min before; task with
due time → at due time; habits → user-chosen time of day. A digest option sends one morning message
with the day's plan instead of many pings.

**F4 — Quick capture with natural-language parsing.**
Single input box (and `POST /capture`) accepting e.g.
`"Call dentist tomorrow 3pm #health !high every year"` → title, due date, tags, priority, recurrence.
Implemented rule-based (`dateparser` + small grammar) so it works offline and deterministically;
an LLM backend can be plugged in later (F13) behind the same interface.

**F5 — Snooze / defer.**
One-tap actions: *Later today*, *Tomorrow*, *Next week*, *Someday* (clears due date, tags
`someday`). Reduces overdue guilt and keeps the list honest.

**F6 — Projects, sub-tasks, tags, search.**
`Project` grouping, `parent_id` on tasks for checklists, tag chips in the UI, `GET /search?q=`
using SQLite FTS5 over tasks, events and notes.

**F7 — Focus mode.**
Start a 25/50-minute timer on a task; records a `FocusSession` (start, end, task). Feeds time-spent
into insights and the daily review.

**F8 — Calendar integration.**
Phase 2: `GET /calendar.ics` (subscribe from any calendar app) and `POST /calendar/import` (ICS
upload). Phase 4: OAuth connectors for Google/Microsoft with two-way sync via a `CalendarSource`
table. ICS first because it is universal, has no OAuth/verification burden, and delivers most of the
value.

**F9 — Weekly review & insights.**
`GET /insights?range=week`: completion rate, tasks created vs. completed, overdue ageing, habit
streaks, focus minutes, busiest weekday. UI: a simple "Your week" card plus a guided weekly review
checklist.

**F10 — Journal & mood check-in.**
A `Note` per day (markdown) plus optional mood (1–5) and energy (1–5). Correlated with completion in
insights ("you complete 40 % more on days you rate energy ≥ 4").

**F11 — PWA & offline.**
`manifest.json`, service worker with cache-first for the shell and a queue for writes
(IndexedDB → replay on reconnect). Required for reliable phone use and for Web Push (F3).

**F12 — Authentication.**
Single-user by default (local mode, no login), with an optional multi-user mode (e-mail + password,
JWT access/refresh tokens, `user_id` on every table). Dependencies already present. Needed before
exposing the app on the internet.

**F13 — AI layer (optional, pluggable).**
A `LLMProvider` interface (OpenAI / Anthropic / local Ollama / none). Use cases: daily briefing text,
breaking a large task into sub-tasks, suggesting time blocks. Must degrade gracefully when no
provider is configured — every feature works without it.

---

## 4. Technical design

### 4.1 Guiding principles

1. **Local-first, personal scale.** SQLite stays the default. Postgres is supported via
   `DATABASE_URL` but not required.
2. **Keep the no-build frontend** for now (Alpine.js), but vendor the libraries, split into modules,
   and serve it from FastAPI so there is one origin and no CORS/URL configuration.
3. **Every feature works without AI and without network** where possible.
4. **Store UTC, render local.** The client always sends its IANA timezone.
5. **Tests before features.** The foundation phase adds pytest and CI so later phases are safe.

### 4.2 Target architecture

```
┌──────────────────────────── Browser / PWA ────────────────────────────┐
│  index.html  (Alpine.js, Tailwind — vendored)                         │
│  js/api.js · js/store.js · js/views/*.js · sw.js (offline queue)      │
└───────────────▲───────────────────────────────────────▲───────────────┘
                │ JSON / fetch                          │ Web Push
┌───────────────┴───────────────────────────────────────┴───────────────┐
│                     FastAPI  (backend/app/)                           │
│  api/        tasks · events · plans · habits · reminders · capture ·  │
│              insights · notes · calendar · auth · health              │
│  services/   recurrence · planner · nlp_capture · notifier ·          │
│              insights · ics · llm (pluggable)                         │
│  scheduler/  APScheduler (reminders, habit roll-over, digests)        │
│  db/         models · session · alembic migrations                    │
│  core/       config (pydantic-settings) · security · timezone utils   │
└───────────────┬───────────────────────────────────────────────────────┘
                │ SQLAlchemy 2.0
        ┌───────┴────────┐        ┌────────────────┐   ┌──────────────┐
        │ SQLite (FTS5)  │        │ SMTP / Web Push│   │ LLM (opt.)   │
        └────────────────┘        └────────────────┘   └──────────────┘
```

**Why in-process APScheduler and not Celery/Redis:** the app is single-instance and personal;
a job table + APScheduler with `SQLAlchemyJobStore` gives durable reminders with zero extra
infrastructure. If the app is ever hosted multi-instance, swap to a worker — the `Notifier`
interface stays the same.

### 4.3 Backend package layout (replaces flat `main.py`)

```
backend/
  app/
    main.py             # create_app(), lifespan, routers, static mount
    core/config.py      # Settings: DATABASE_URL, TZ default, CORS, VAPID keys, SMTP, LLM_PROVIDER
    core/time.py        # now_utc(), to_utc(dt, tz), local_day_bounds(date, tz)
    db/models.py        # SQLAlchemy 2.0 Mapped[] models
    db/session.py
    schemas/*.py        # Pydantic v2, ConfigDict(from_attributes=True)
    api/*.py            # thin routers → services
    services/*.py       # business logic, unit-testable without HTTP
    scheduler.py
  alembic/              # migrations (alembic init; autogenerate from models)
  tests/
    conftest.py         # in-memory SQLite, TestClient, frozen clock
    test_tasks.py test_today.py test_recurrence.py test_capture.py ...
  requirements.txt requirements-dev.txt
  venv/                 # project virtual environment (git-ignored)
frontend/
  index.html  js/  css/  vendor/  manifest.json  sw.js
```

### 4.4 Data model

New / changed tables (SQLite types shown; all timestamps are UTC):

```sql
-- changed
tasks (
  id, user_id?, project_id?, parent_id?,
  title TEXT NOT NULL CHECK(length(trim(title))>0),
  description, kind TEXT DEFAULT 'task',          -- task | habit
  priority TEXT, priority_rank INT,               -- rank: low=1 … urgent=4 (for ORDER BY)
  status TEXT, due_at DATETIME, due_all_day BOOL,
  estimate_minutes INT, tags JSON,
  recurrence_rule TEXT,                           -- RRULE string, NULL = one-off
  recurrence_anchor DATETIME,                     -- first occurrence
  completed_at DATETIME,                          -- replaces updated_at heuristic
  snoozed_until DATETIME,
  created_at, updated_at
)
events ( …existing…, all_day BOOL, recurrence_rule TEXT, source_id?, external_id, updated_at,
         CHECK(end_time > start_time) )

-- new
task_completions (id, task_id, occurrence_date DATE, completed_at)     -- habit history / streaks
daily_plans      (id, user_id?, plan_date DATE UNIQUE, focus_task_ids JSON,
                  reviewed_at, reflection TEXT, mood INT, energy INT)
reminders        (id, task_id?, event_id?, remind_at, channel, sent_at, status)
push_subscriptions (id, user_id?, endpoint, keys JSON, created_at)
projects         (id, name, color, archived BOOL)
notes            (id, note_date DATE, body TEXT, created_at, updated_at)   -- + FTS5 virtual table
focus_sessions   (id, task_id?, started_at, ended_at, kind)                -- pomodoro / deep
calendar_sources (id, provider, name, credentials JSON, last_sync_at)      -- phase 4
users            (id, email, password_hash, timezone, created_at)          -- phase 2
```

Migration notes: Alembic `0001_baseline` reflects today's schema; `0002_foundation` renames
`due_date → due_at`, adds `completed_at`, `priority_rank`, `kind`, `snoozed_until` and back-fills
`completed_at = updated_at WHERE status='done'`.

### 4.5 Timezone handling (fixes B1)

* Schemas use `AwareDatetime`; naive input is rejected with 422 (the frontend already sends `Z`).
* Models store UTC (`DateTime(timezone=True)`; SQLite stores ISO text — normalise to UTC before
  write in a validator/`TypeDecorator`).
* Day-scoped endpoints (`/today`, `/plans/{date}`, `/insights`) accept `tz` (IANA name, e.g.
  `America/New_York`); default from user profile, else `Settings.DEFAULT_TZ`. Day bounds are computed
  with `zoneinfo` and converted to UTC for the query.
* Frontend sends `Intl.DateTimeFormat().resolvedOptions().timeZone` on every day-scoped call.

### 4.6 API design (additions and changes)

All existing endpoints remain; `due_date` is kept as an alias for one release then removed.

| Method & path | Purpose |
|---|---|
| `PATCH/DELETE /events/{id}` | Complete event CRUD (B6) |
| `GET /tasks?status&priority&tag&project_id&due_before&due_after&q&limit&offset` | Filters validated as enums (B5), pagination (B7) |
| `POST /tasks/{id}/complete` | Records completion; for recurring tasks returns the next occurrence |
| `POST /tasks/{id}/snooze {until \| preset}` | F5 |
| `POST /capture {text, tz}` | F4 — returns a *preview* (`parsed` + `confidence`); `POST /capture?commit=true` creates |
| `GET /today?tz=` | Adds `focus_tasks`, `habits_due`, `workload_minutes`, `free_minutes` |
| `GET/PUT /plans/{date}` | F2 morning plan (focus task ids) |
| `POST /plans/{date}/review` | F2 evening review (carry-over actions, reflection, mood) |
| `GET /habits` · `GET /habits/{id}/streak` | F1 |
| `GET/POST/DELETE /reminders` · `POST /push/subscribe` | F3 |
| `GET /insights?range=week\|month&tz=` | F9 |
| `GET/PUT /notes/{date}` · `GET /search?q=` | F10, F6 |
| `GET /calendar.ics` · `POST /calendar/import` | F8 |
| `POST /auth/register|login|refresh` · `GET /me` | F12 (optional mode) |

Error contract: `422` for validation, `404` for missing, `409` for conflicts (e.g. plan already
reviewed), JSON body `{"detail": ...}` as FastAPI already does.

### 4.7 Key algorithms

**Recurrence (F1).** Use `python-dateutil`'s `rrule` — no custom parser. A recurring task is a
*template*; the "current occurrence" is the next `rrule.after(last_completion or anchor)`. On
complete: insert `task_completions`, set `due_at` to the next occurrence. Habits: `streak` = count of
consecutive scheduled occurrences with a completion, walking backwards from today; skipped days that
are not scheduled do not break a streak.

**Natural-language capture (F4).** Pipeline: (1) extract `#tag`, `!priority`, `every …` recurrence
phrase via regex; (2) remaining text → `dateparser.search.search_dates(..., settings={'TIMEZONE': tz,
'PREFER_DATES_FROM': 'future'})`; (3) strip matched date text to form the title; (4) return
preview with confidence so the UI can show "Due: tomorrow 15:00 — tap to change". Never auto-commit
on low confidence.

**Planner (F2).** Candidates = overdue ∪ due-today ∪ habits-due ∪ snoozed-expired, sorted by
(overdue days desc, priority_rank desc, estimate asc). Workload = Σ `estimate_minutes` of focus
tasks; free time = working hours − Σ event durations today. Warn if workload > free time.

**Reminders (F3).** On task/event create/update a `reminders` row is (re)computed. APScheduler runs
`dispatch_due_reminders()` every minute: select `status='pending' AND remind_at <= now`, send via
channel, mark `sent`/`failed` (retry ×3 with back-off). Web Push uses `pywebpush` with VAPID keys
from settings; delivery requires the service worker (F11).

### 4.8 Frontend plan

* **Phase 1:** keep single-page Alpine but split into `js/` modules and vendor Tailwind (compiled
  CSS via the standalone CLI, committed) and Alpine. Serve `/` from FastAPI `StaticFiles`; remove
  the hard-coded `API` constant (use relative URLs). Add views: *Today* (plan/review), *Tasks*,
  *Habits*, *Calendar (week)*, *Insights*, *Settings*.
* **Phase 3:** PWA (`manifest.json`, `sw.js`), offline write queue, push permission flow.
* **Later (optional):** if native features are required (widgets, share-sheet capture), wrap the PWA
  with Capacitor rather than rewriting in React Native — the API contract is unchanged either way.

### 4.9 Configuration, security, deployment

* `pydantic-settings` `Settings` with `.env`: `DATABASE_URL`, `DEFAULT_TZ`, `CORS_ORIGINS`,
  `AUTH_MODE=local|multi`, `VAPID_*`, `SMTP_*`, `LLM_PROVIDER`, `LLM_API_KEY`.
* CORS: explicit origins only; when the frontend is served from FastAPI, CORS is not needed at all.
* Auth (F12): bcrypt via passlib, JWT (python-jose) access 15 min + refresh 7 d in HttpOnly cookie;
  rate-limit login. In `local` mode the auth dependency returns a fixed single user.
* Secrets never in the DB for calendar connectors — store encrypted (Fernet, key from env).
* Docker: write DB to `/app/data/assistant.db` so the named volume is actually used (B9); add a
  `HEALTHCHECK` using `/health`; run as non-root.
* Backups: `GET /export` (JSON of all user data) and a nightly SQLite `VACUUM INTO` job.

### 4.10 Testing strategy

| Level | Tooling | Coverage target |
|---|---|---|
| Unit | pytest, `freezegun` for the clock | recurrence, streaks, capture parser, planner, timezone utils |
| API | `TestClient` + in-memory SQLite, fixtures per test | every endpoint, validation errors, tz edge cases (23:30 UTC vs local day) |
| Migration | run Alembic up/down on a copy of a seeded DB | each migration |
| Frontend | Playwright smoke (create task, complete, plan day) | critical paths |
| CI | GitHub Actions: `ruff`, `mypy --strict app/`, `pytest --cov` | gate on PR |

All commands run through the project venv: `backend/venv/bin/python -m pytest`.

### 4.11 Phasing & milestones

| Phase | Duration | Deliverables | Exit criteria |
|---|---|---|---|
| **0 – Foundation** | 1 week | Package layout; Alembic; tz-aware datetimes; validation fixes (B1–B9); `completed_at`; event PATCH/DELETE; pagination; pytest + CI; frontend served by FastAPI, vendored assets | All tests green; `/today` correct for a user in a non-server TZ |
| **1 – Assistant core** | 2 weeks | F1 recurring/habits, F2 plan & review, F4 capture, F5 snooze; new Today/Habits views | A user can run a full day: capture → plan top-3 → complete → review, with a habit streak visible |
| **2 – Reach & structure** | 2 weeks | F3 reminders (push + email), F6 projects/sub-tasks/tags/search, F7 focus mode, F8 ICS, F12 auth (optional mode) | Reminder delivered to a phone; calendar app subscribes to `/calendar.ics` |
| **3 – Reflection & mobile** | 2 weeks | F9 insights, F10 journal/mood, F11 PWA/offline | Installed PWA works offline and syncs on reconnect; weekly review screen |
| **4 – Integrations & AI** | ongoing | F8 OAuth calendar sync, F13 LLM layer, F14 wellbeing nudges | Works with provider = none |

### 4.12 Risks & mitigations

| Risk | Mitigation |
|---|---|
| Timezone regressions | Single `core/time.py`; tests at day boundaries; reject naive datetimes at the API |
| Notification fatigue | Digest mode default; quiet hours; per-task opt-out |
| NL parsing mistakes create wrong tasks | Preview-before-commit; confidence threshold; one-tap edit |
| Scope creep toward a full calendar client | ICS first; OAuth sync is phase 4 and read-mostly |
| SQLite write contention with scheduler | WAL mode; short transactions; scheduler uses its own session |
| AI dependency / cost / privacy | Provider pluggable; off by default; local Ollama option |

### 4.13 Open questions

1. Is the product single-user (self-hosted) or multi-tenant? This decides how early F12 lands.
2. Working hours / quiet hours defaults — per user settings or global?
3. Should habits allow "N times per week" (flexible) in addition to fixed weekdays? (Recommend yes;
   model as `FREQ=WEEKLY;COUNT_PER_PERIOD=3` extension handled in the service layer.)
4. Preferred notification channel for phase 2: Web Push only, or also e-mail/Telegram?

---

## Appendix A — Reproduction commands used in the investigation

```bash
cd personal-assistant/backend
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
uvicorn main:app --port 8000 &

# B1: timezone dropped
curl -s -X POST localhost:8000/tasks -H 'Content-Type: application/json' \
  -d '{"title":"UTC task","due_date":"2026-10-15T23:30:00Z"}'      # → "due_date":"2026-10-15T23:30:00"
# B4: empty title / inverted event accepted
curl -s -X POST localhost:8000/tasks  -H 'Content-Type: application/json' -d '{"title":""}'
curl -s -X POST localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"title":"Bad","start_time":"2026-10-15T15:00:00","end_time":"2026-10-15T14:00:00"}'
# B6: no event update/delete
curl -s -X PATCH localhost:8000/events/1 -d '{}'                     # → 404
# B8: deprecations
python -W always -c "import main"
```
