# Deployment

The app is one process (FastAPI serving the API **and** the web app) plus one SQLite file.
Every option below ends with the same result: open `http://<host>:8000/` on any desktop,
phone or tablet browser and — optionally — *Install* it as an app.

| Where it runs | How | Data location | Best for |
|---|---|---|---|
| Docker (any OS, NAS, Raspberry Pi) | `docker compose -f docker-compose.yml up -d --build` | named volume `assistant_data` | Always-on home server |
| Linux service | `deploy/personal-assistant.service` (systemd) | `/var/lib/personal-assistant` | Linux box / VPS |
| macOS at login | `deploy/com.personal-assistant.plist` (launchd) | `backend/data/` | A Mac that stays on |
| Windows at logon | `deploy\install-windows-task.ps1` (Task Scheduler) | `backend\data\` | A Windows PC that stays on |
| Ad-hoc | `python dev.py serve` | `backend/data/` | Trying it out |

Configuration is by environment variables (see `backend/.env.example`): `DATA_DIR`,
`DATABASE_URL`, `DEFAULT_TZ`, `CORS_ORIGINS`, `PORT`.

## Reaching it from phones and tablets

* **Same network:** bind to all interfaces (`--host 0.0.0.0`, which every option above does),
  allow the port through the OS firewall (Windows: see README), and open `http://<pc-ip>:8000/`.
* **Away from home:** use Tailscale (private VPN, free for personal use) — see
  `MOBILE_DEPLOYMENT.md` §2. The app has **no authentication**; do not expose it to the public
  internet.
* **Installable PWA + offline:** browsers only allow "Install app" and service workers on
  `localhost` or **HTTPS**. On a LAN that means putting a reverse proxy with a certificate in
  front (Caddy does this automatically with `tls internal`, or `tailscale serve --bg 8000`
  gives a trusted HTTPS URL with zero config). Plain `http://192.168…` still works as a
  normal web page on every device — you just won't get the install prompt or offline cache.

Minimal Caddyfile for HTTPS on a LAN:

```
assistant.local {
    tls internal
    reverse_proxy 127.0.0.1:8000
}
```

## Upgrading

```bash
git pull
python dev.py setup          # installs any new dependencies into the venv
# restart the service / container
```

Database schema changes are applied automatically at startup (`database.init_db`), and the
web app tells open browsers when a new version is available ("Reload").

## Backups

Everything is in one SQLite database: `backend/data/assistant.db` (or the Docker volume /
`DATA_DIR`). Copy it while the app is stopped, or use `sqlite3 assistant.db ".backup copy.db"`
while running (WAL mode makes this safe).

## Health & monitoring

`GET /health` returns `{"status":"ok","version":…}`; the Docker image has a `HEALTHCHECK`
using it. Point any uptime monitor at it.
