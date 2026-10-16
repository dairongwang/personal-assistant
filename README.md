# Personal Assistant Dashboard

Tasks, events and a daily summary — one app that works on **desktop (Windows, macOS, Linux),
phones and tablets**. FastAPI + SQLite backend; dependency-free web frontend (no build step,
no CDNs) that installs as a PWA and keeps working offline for reading.

**Features:** tasks (priorities, due dates, tags, search), today summary computed in *your*
timezone, overdue highlighting, calendar events, dark mode, keyboard shortcuts on desktop,
touch-friendly layout on phones and tablets.

## Requirements

- Python **3.10 – 3.14** (all dependencies ship prebuilt wheels — no compiler needed)
- Windows: install from [python.org](https://www.python.org/downloads/) and tick
  **"Add python.exe to PATH"** (the Microsoft Store shortcut is what produces
  *"Python was not found"*).
- Optional: Docker, Node.js (only for the JavaScript syntax check in `dev.py lint`)

## Quick start — identical on every OS

```bash
cd personal-assistant
python dev.py setup     # creates backend/venv and installs everything into it
python dev.py run       # http://localhost:8000/  (API docs at /docs)
```

`dev.py` is a small standard-library script that always uses the project's virtual
environment (`backend/venv`) — nothing is installed globally and no `activate` step is needed.
On Windows use `py -3 dev.py …` if `python` is not on your PATH.

| Command | What it does |
|---|---|
| `python dev.py setup` | Create the venv, install runtime + dev dependencies |
| `python dev.py run` | Start with auto-reload on `localhost` |
| `python dev.py serve` | Start on all interfaces and print the URL to open on your phone |
| `python dev.py test` | Run the test suite (pytest) |
| `python dev.py lint` / `format` | Ruff checks / auto-format (+ JS syntax check if Node is present) |
| `python dev.py check` | Lint + tests — exactly what CI runs |
| `python dev.py docker` | Build and run with Docker Compose (dev mode, live reload) |

<details>
<summary>Manual commands (without dev.py)</summary>

Windows (PowerShell / cmd):
```bat
cd personal-assistant\backend
py -3 -m venv venv
.\venv\Scripts\python -m pip install -r requirements-dev.txt
.\venv\Scripts\python -m uvicorn main:app --reload
```
macOS / Linux:
```bash
cd personal-assistant/backend
python3 -m venv venv
venv/bin/python -m pip install -r requirements-dev.txt
venv/bin/python -m uvicorn main:app --reload
```
</details>

## Use it from your phone or tablet

```bash
python dev.py serve
```
prints something like `http://192.168.1.23:8000/` — open it on any device on the same Wi-Fi.
Windows needs a one-time firewall rule (PowerShell as Administrator):

```powershell
netsh advfirewall firewall add rule name="Personal Assistant 8000" dir=in action=allow protocol=TCP localport=8000 profile=private
```

- **Install as an app** ("Add to Home Screen" / "Install") and **offline reading** require
  HTTPS or `localhost` — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for a two-line Caddy
  or Tailscale setup. Over plain `http://192.168…` it works as a normal web page.
- Access away from home, remote-access options: [docs/MOBILE_DEPLOYMENT.md](docs/MOBILE_DEPLOYMENT.md).
- The app has **no authentication** — keep it on your private network.

## Deployment

| Target | How |
|---|---|
| Docker (any OS / NAS / Raspberry Pi) | `docker compose -f docker-compose.yml up -d --build` |
| Linux (systemd) | `deploy/personal-assistant.service` |
| macOS (launchd) | `deploy/com.personal-assistant.plist` |
| Windows (Task Scheduler) | `deploy\install-windows-task.ps1` |

Details, HTTPS, upgrades and backups: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
Configuration via environment variables — see `backend/.env.example`.

## Testing

```bash
python dev.py check
```

Tests live in `backend/tests/` and run against a temporary SQLite database. CI
(`.github/workflows/ci.yml`) runs them on **Windows, macOS and Linux × Python 3.10/3.12/3.14**,
smoke-tests the running server, and builds the Docker image.

## API

All times are ISO-8601; send timezone-aware values (`…Z` or `±hh:mm`), naive values are read
as UTC. Pass `?tz=Area/City` (or an `X-Timezone` header) to `/today` so "today" is computed
in the user's timezone.

| Method | Path | Description |
|---|---|---|
| `GET` | `/today?tz=` | Today summary (due, overdue, events, counts) for the given timezone |
| `GET` / `POST` | `/tasks` | List (`?status= ?priority= ?tag= ?q= ?limit= ?offset=`) / create |
| `GET` / `PATCH` / `DELETE` | `/tasks/{id}` | Read / update / delete (`completed_at` tracked automatically) |
| `GET` / `POST` | `/events` | List (`?start= ?end=`) / create |
| `GET` / `PATCH` / `DELETE` | `/events/{id}` | Read / update / delete |
| `GET` | `/health` | Health check with version |
| `GET` | `/docs` | Interactive API docs |

## Project structure

```
personal-assistant/
├── dev.py                     # cross-platform developer commands
├── backend/
│   ├── main.py                # FastAPI app + routes; serves frontend/ at /
│   ├── config.py              # environment-based settings
│   ├── models.py  schemas.py  database.py
│   ├── tests/                 # pytest suite
│   ├── requirements.txt  requirements-dev.txt  pyproject.toml  .env.example
│   └── Dockerfile
├── frontend/
│   ├── index.html  css/app.css  js/app.js   # dependency-free web app
│   ├── manifest.webmanifest  sw.js  icons/  # PWA
├── deploy/                    # systemd / launchd / Windows Task Scheduler
├── docs/                      # design documents
├── scripts/make_icons.py
├── docker-compose.yml         # production-style
└── docker-compose.override.yml# dev overrides (live reload)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Python was not found … Microsoft Store` | Install Python from python.org with "Add to PATH", or disable the alias under *Settings → Apps → Advanced app settings → App execution aliases*. Use `py -3` or `python`, not `python3`. |
| `'.venv' is not recognized as an internal or external command` | Windows venvs use `Scripts\`, not `bin/`: `.\venv\Scripts\python …` — or just use `python dev.py …`. |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | Activation isn't needed. To allow it anyway: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. |
| `Failed building wheel for pydantic-core` / `newer than PyO3's maximum supported version` | pip found no prebuilt wheel for your Python and tried to compile. Use the current `requirements.txt`, upgrade pip, and Python 3.10–3.14. `pip install --only-binary=:all: -r requirements.txt` fails fast instead of compiling. |
| `Unknown timezone` (422) on Windows | `tzdata` is in `requirements.txt`; re-run `python dev.py setup`. |
| Phone can't open the URL | Same Wi-Fi? Firewall rule added? Server started with `dev.py serve` (not `run`, which binds to localhost only)? |
| No "Install app" prompt / no offline mode | Requires HTTPS or `localhost` — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). |

## Documentation

- [docs/TECH_DESIGN.md](docs/TECH_DESIGN.md) — investigation, bugs found, feature roadmap
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — running it as a service, HTTPS, upgrades, backups
- [docs/MOBILE_DEPLOYMENT.md](docs/MOBILE_DEPLOYMENT.md) — phone access options (LAN, Tailscale, PWA, stores)
- [docs/PLATFORM_DESIGN.md](docs/PLATFORM_DESIGN.md) — commercial multi-device product: billing, auth, privacy-first sync, deployment & testing
