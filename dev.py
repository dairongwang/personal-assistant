#!/usr/bin/env python3
"""Cross-platform developer commands for the Personal Assistant (Windows, macOS, Linux).

Standard library only; always runs tools from the project's own environments
(services/account/venv for Python, apps/web/node_modules for the Angular PWA).

    python dev.py setup            create the venv, install Python + npm dependencies
    python dev.py db [cmd]         local PostgreSQL: init [--port N] | start | stop | status | upgrade | revision "msg"
                                   (init picks a free port by itself when 5432 belongs to another server)
    python dev.py run              API (auto-reload) + Angular dev server with /api proxy
                                   (uses the local PostgreSQL if it is running, otherwise SQLite;
                                   creates the fake test accounts — SEED_FAKE_ACCOUNTS=false to skip)
    python dev.py seed             (re)create the fake test accounts: free@ / trial@ / pro@ / lifetime@ /
                                   pastdue@ / expired@ / admin@example.com  (services/account/seed.py)
    python dev.py promote <email>  give an existing account the administrator role (demote: --demote)
    python dev.py stop             kill leftover API / dev-server processes from a previous run
    python dev.py serve            production build of the PWA served by the API (service worker on,
                                   reachable from phones on your network)
    python dev.py build            production build of the PWA (apps/web/dist)
    python dev.py test             Python unit tests (SQLite) + Angular unit tests
    python dev.py test --postgres  same, with the API tests against TEST_DATABASE_URL / local db
    python dev.py e2e              end-to-end tests in a real browser (updates, sync, privacy) — uses any
                                   browser found: bundled Chromium, Edge, Chrome, Firefox, WebKit
                                   (E2E_BROWSER=msedge|chrome|chromium|firefox|webkit to pick one)
    python dev.py lint             ruff + prettier/tsc checks
    python dev.py format           auto-format Python and TypeScript
    python dev.py check            lint + test (what CI runs)
    python dev.py release <ver>    bump apps/web/release.json (add --critical for a forced update)
    python dev.py publish <url>    publish release.json to a deployed API (RELEASE_ADMIN_TOKEN)
    python dev.py docker           build and run everything with Docker Compose (Postgres + app)
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "apps" / "web"
SERVICE = ROOT / "services" / "account"
VENV = SERVICE / "venv"
E2E = ROOT / "e2e"
IS_WINDOWS = platform.system() == "Windows"
PG_DIR = ROOT / ".pgdata"
ENV_FILE = SERVICE / ".env"


def load_env_file() -> None:
    """services/account/.env (KEY=VALUE lines) provides defaults for dev.py too — so a port or
    DATABASE_URL chosen once (e.g. by `db init`) is used by every later command. Real environment
    variables win."""
    if not ENV_FILE.is_file():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def save_env_value(key: str, value: str) -> None:
    """Write/replace KEY=value in services/account/.env (created if missing)."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.is_file() else []
    lines = [ln for ln in lines if not ln.strip().startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


load_env_file()
PG_PORT = os.environ.get("PGPORT", "5432")
LOCAL_DB_URL = f"postgresql+psycopg://postgres@127.0.0.1:{PG_PORT}/assistant"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def npm() -> str:
    return shutil.which("npm.cmd" if IS_WINDOWS else "npm") or "npm"


def run(cmd: list[str], cwd: Path = SERVICE, env: dict | None = None, check: bool = False) -> int:
    print("$", " ".join(str(c) for c in cmd), flush=True)
    try:
        full_env = {**os.environ, "NG_CLI_ANALYTICS": "false", **(env or {})}
        code = subprocess.call([str(c) for c in cmd], cwd=str(cwd), env=full_env)
    except FileNotFoundError:
        print(f"\nerror: '{cmd[0]}' was not found. Is it installed and on your PATH?", file=sys.stderr)
        code = 127
    if check and code:
        sys.exit(code)
    return code


# ---------------------------------------------------------------------------- Node / processes

NODE_REQUIREMENT = "Node.js 20.19+, 22.12+ or 24+ (Angular 21 CLI requirement)"


def check_node() -> None:
    """Exit with a clear message when Node is missing or too old — `ng serve` would otherwise just die."""
    node = shutil.which("node")
    if not node:
        sys.exit(f"Node.js not found. Install {NODE_REQUIREMENT} from https://nodejs.org and run setup again.")
    try:
        raw = subprocess.run([node, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        raw = ""
    parts = raw.lstrip("v").split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return
    ok = (major == 20 and minor >= 19) or (major == 22 and minor >= 12) or major >= 24
    if not ok:
        sys.exit(
            f"Node.js {raw} is too old for the Angular CLI; it needs {NODE_REQUIREMENT}.\n"
            "Install the current LTS from https://nodejs.org (or: winget install OpenJS.NodeJS.LTS),\n"
            "open a NEW terminal and run:  python dev.py setup"
        )


def popen_tree(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.Popen:
    """Start a child we can later kill together with *its* children (uvicorn --reload spawns a
    worker; npm spawns node). Windows: own process group; POSIX: own session."""
    print("$", " ".join(str(c) for c in cmd), flush=True)
    kwargs: dict = {"cwd": str(cwd), "env": {**os.environ, **(env or {})}}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([str(c) for c in cmd], **kwargs)


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.call(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            import signal

            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(10)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()


def listeners(port: str) -> list[int]:
    """PIDs listening on a TCP port (best effort, stdlib + OS tools only)."""
    pids: set[int] = set()
    try:
        if IS_WINDOWS:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, check=False).stdout
            for line in out.splitlines():
                cols = line.split()
                if len(cols) >= 5 and cols[1].endswith(f":{port}") and cols[3] == "LISTENING":
                    pids.add(int(cols[4]))
        elif shutil.which("lsof"):
            out = subprocess.run(
                ["lsof", "-t", "-iTCP:" + port, "-sTCP:LISTEN"], capture_output=True, text=True, check=False
            ).stdout
            pids.update(int(x) for x in out.split())
        elif Path("/proc/net/tcp").exists():
            pids.update(_linux_proc_listeners(int(port)))
    except (OSError, ValueError):
        pass
    return sorted(pids)


def _linux_proc_listeners(port: int) -> set[int]:
    """Linux without lsof/ss: socket inodes in LISTEN state from /proc/net/tcp*, mapped to PIDs via /proc/*/fd."""
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            if len(cols) > 9 and cols[3] == "0A" and int(cols[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(cols[9])
    pids: set[int] = set()
    if not inodes:
        return pids
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                target = os.readlink(fd)
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    pids.add(int(proc.name))
                    break
        except OSError:
            continue
    return pids


def port_in_use(port: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


# ---------------------------------------------------------------------------- PostgreSQL tools

PG_INSTALL_HINT = """PostgreSQL command-line tools were not found (initdb / pg_ctl / createdb).

You have three options:

  1. Install PostgreSQL and run this command again. dev.py looks on PATH, in PG_BIN, and in the
     usual install folders:
       Windows:  winget install PostgreSQL.PostgreSQL.16      (or the EDB installer)
                 -> tools live in C:\\Program Files\\PostgreSQL\\16\\bin (found automatically)
       macOS:    brew install postgresql@16
       Linux:    sudo apt install postgresql-16     (Debian/Ubuntu; tools in /usr/lib/postgresql/16/bin)
     If they are installed somewhere else:   set PG_BIN=C:\\path\\to\\bin   (Windows)  /  export PG_BIN=...

  2. Use Docker instead of a local cluster:        python dev.py docker

  3. Skip PostgreSQL for now: `python dev.py run` / `serve` fall back to SQLite automatically when no
     PostgreSQL is reachable (fine for development; PostgreSQL is used in the cloud).
"""


def _pg_candidate_dirs() -> list[Path]:
    """Where installers put PostgreSQL without adding it to PATH (newest version first)."""
    dirs: list[Path] = []
    if env := os.environ.get("PG_BIN"):
        dirs.append(Path(env))
    if IS_WINDOWS:
        for root in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramW6432"),
        ):
            if root:
                dirs += sorted(Path(root).glob("PostgreSQL/*/bin"), reverse=True)
        if local := os.environ.get("LOCALAPPDATA"):
            dirs += sorted(Path(local).glob("Programs/PostgreSQL/*/bin"), reverse=True)
    elif platform.system() == "Darwin":
        for pattern in (
            "/opt/homebrew/opt/postgresql*/bin",
            "/usr/local/opt/postgresql*/bin",
            "/Applications/Postgres.app/Contents/Versions/*/bin",
        ):
            dirs += sorted(Path("/").glob(pattern.lstrip("/")), reverse=True)
    else:
        for pattern in ("usr/lib/postgresql/*/bin", "usr/pgsql-*/bin", "usr/local/pgsql/bin"):
            dirs += sorted(Path("/").glob(pattern), reverse=True)
    return dirs


def pg_tool(name: str) -> str:
    """Full path of a PostgreSQL tool, or exit with an explanation of how to get it."""
    found = shutil.which(name)
    if found:
        return found
    exe = f"{name}.exe" if IS_WINDOWS else name
    for d in _pg_candidate_dirs():
        if (d / exe).is_file():
            return str(d / exe)
    sys.exit(PG_INSTALL_HINT)


def pg_probe(url: str = "") -> tuple[str, str]:
    """Try to connect with the venv's psycopg. Returns (status, detail):
    'ok' — server answers and accepts the login; 'auth' — a server answers but rejects the
    login (typically a PostgreSQL *service* from the installer that owns port 5432 with its own
    password); 'down' — nothing answers; 'no-driver' — psycopg missing (run setup)."""
    py = venv_python()
    if not py.exists():
        return "no-driver", "virtual environment not set up"
    probe = (
        "import sys\n"
        "try:\n    import psycopg\nexcept ImportError:\n    print('no-driver'); sys.exit()\n"
        "url = sys.argv[1].replace('postgresql+psycopg://', 'postgresql://')\n"
        "try:\n    psycopg.connect(url, connect_timeout=3).close(); print('ok')\n"
        "except psycopg.OperationalError as e:\n"
        "    m = str(e).lower()\n"
        "    print('auth' if ('password' in m or 'authentication' in m or 'role' in m or 'pg_hba' in m) else 'down')\n"
        "    print(str(e).strip().splitlines()[0] if str(e).strip() else '')\n"
    )
    try:
        out = subprocess.run(
            [str(py), "-c", probe, url or LOCAL_DB_URL], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "down", str(exc)
    lines = (out.stdout or "").strip().splitlines() or ["down"]
    return lines[0], (lines[1] if len(lines) > 1 else "")


def pg_running(url: str = "") -> bool:
    return pg_probe(url)[0] == "ok"


FOREIGN_PG_HINT = f"""A PostgreSQL server is listening on port {PG_PORT} but rejected the login  postgres@127.0.0.1
(no password). That is usually the PostgreSQL *service* installed by the installer/winget,
not the project's own cluster. Pick one:

  a) Use that server: create a database and point DATABASE_URL at it (password from the installer):
       createdb -U postgres assistant
       set DATABASE_URL=postgresql+psycopg://postgres:YOUR_PASSWORD@127.0.0.1:{PG_PORT}/assistant
       python dev.py db upgrade
     (put the DATABASE_URL line into services\\account\\.env to make it permanent)

  b) Run the project's own cluster on another port:
       python dev.py db init --port 5433
     (the port is remembered in services\\account\\.env, so run / db upgrade / db start use it automatically)
"""


def database_url() -> str:
    """DATABASE_URL if set; else the local PostgreSQL cluster if it answers; else SQLite with a notice."""
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    status, detail = pg_probe()
    if status == "ok":
        return LOCAL_DB_URL
    if status == "auth":
        print(f"note: {detail}\n" + FOREIGN_PG_HINT, file=sys.stderr)
    sqlite_path = SERVICE / "data" / "account.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"note: no PostgreSQL server at {LOCAL_DB_URL} — using SQLite ({sqlite_path}) for this run.\n"
        "      For PostgreSQL: python dev.py db init && python dev.py db start   (or: python dev.py docker)\n",
        flush=True,
    )
    return f"sqlite:///{sqlite_path.as_posix()}"


def require_venv() -> Path:
    py = venv_python()
    if not py.exists():
        sys.exit(f"Virtual environment not found at {VENV}.\nRun:  python dev.py setup")
    return py


def require_node_modules() -> None:
    if not (WEB / "node_modules").is_dir():
        sys.exit("apps/web/node_modules not found.\nRun:  python dev.py setup")


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "<your-pc-ip>"
    finally:
        s.close()


# ---------------------------------------------------------------------------- commands


# What services/account/requirements-dev.txt and e2e/requirements.txt add on top of requirements.txt
# (used when those files are missing from a partial download).
DEV_TOOLS = ["pytest==9.1.1", "httpx2==2.13.1", "ruff==0.17.0"]
E2E_TOOLS = ["pytest==9.1.1", "playwright==1.56.0"]


def cmd_setup(args: list[str]) -> int:
    base_python = args[args.index("--python") + 1] if "--python" in args else sys.executable
    if not venv_python().exists():
        run([base_python, "-m", "venv", str(VENV)], check=True)
    py = venv_python()
    run([py, "-m", "pip", "install", "--upgrade", "pip"], check=True)
    # Absolute paths (cwd-independent); incomplete downloads without the -dev file still set up.
    dev_reqs = SERVICE / "requirements-dev.txt"
    if dev_reqs.is_file():
        run([py, "-m", "pip", "install", "-r", str(dev_reqs)], check=True)
    else:
        print(f"note: {dev_reqs} is missing — installing requirements.txt plus the test tools directly.")
        run([py, "-m", "pip", "install", "-r", str(SERVICE / "requirements.txt"), *DEV_TOOLS], check=True)
    e2e_reqs = E2E / "requirements.txt"
    if e2e_reqs.is_file():
        run([py, "-m", "pip", "install", "-r", str(e2e_reqs)], check=True)
    else:
        print(f"note: {e2e_reqs} is missing — installing the end-to-end test tools directly.")
        run([py, "-m", "pip", "install", *E2E_TOOLS], check=True)
    check_node()
    run(
        [
            npm(),
            "ci" if (WEB / "package-lock.json").exists() else "install",
            "--no-audit",
            "--no-fund",
        ],
        cwd=WEB,
        check=True,
    )
    print("\nSetup complete.  Next:  python dev.py run          (SQLite, no database server needed)")
    print("                   or:  python dev.py db init && python dev.py run   (local PostgreSQL)")
    return 0


def cmd_db(args: list[str]) -> int:
    """Local PostgreSQL cluster in .pgdata/ (no Docker needed): init | start | stop | status | upgrade | revision."""
    sub = args[0] if args else "status"
    if sub == "init":
        global PG_PORT, LOCAL_DB_URL
        if PG_DIR.exists():
            print(f"{PG_DIR} already exists — run:  python dev.py db start")
            return 0
        if "--port" in args:
            save_env_value("PGPORT", args[args.index("--port") + 1])
            PG_PORT = os.environ["PGPORT"]
            LOCAL_DB_URL = f"postgresql+psycopg://postgres@127.0.0.1:{PG_PORT}/assistant"
        status, detail = pg_probe()
        if status == "auth" or (status != "ok" and port_in_use(PG_PORT)):
            # Another PostgreSQL (typically the installer's Windows service) owns this port. Pick a
            # free one for the project's own cluster and remember it for every later command.
            free = next((str(n) for n in range(5433, 5450) if not port_in_use(str(n))), None)
            if free is None:
                sys.exit(f"{detail}\n\n{FOREIGN_PG_HINT}")
            print(
                f"Port {PG_PORT} is used by another PostgreSQL server ({detail or 'login rejected'}).\n"
                f"Using port {free} for the project's cluster — saved as PGPORT={free} in services/account/.env,\n"
                "so run / db upgrade / db start use it automatically. (To use that other server instead, put its\n"
                "DATABASE_URL into the same file.)\n"
            )
            save_env_value("PGPORT", free)
            PG_PORT = free
            LOCAL_DB_URL = f"postgresql+psycopg://postgres@127.0.0.1:{PG_PORT}/assistant"
            status = "down"
        if status == "ok":
            print(
                f"A PostgreSQL server that accepts postgres@127.0.0.1:{PG_PORT} is already running — nothing to init."
            )
            return run([pg_tool("createdb"), "-h", "127.0.0.1", "-p", PG_PORT, "-U", "postgres", "assistant"], cwd=ROOT)
        initdb = pg_tool("initdb")
        # --locale=C: works on every OS/language setting and avoids "encoding does not match locale" on Windows.
        code = run([initdb, "-D", str(PG_DIR), "-U", "postgres", "--auth=trust", "-E", "UTF8", "--locale=C"], cwd=ROOT)
        if code:
            print(
                "\ninitdb failed. On Windows, run this from a normal (non-Administrator) Command Prompt and make sure\n"
                f"the folder {PG_DIR} is not inside a OneDrive/synced or read-only location.",
                file=sys.stderr,
            )
            return code
        code = cmd_db(["start"])
        if code:
            return code
        return run([pg_tool("createdb"), "-h", "127.0.0.1", "-p", PG_PORT, "-U", "postgres", "assistant"], cwd=ROOT)
    if sub == "start":
        if not PG_DIR.exists():
            sys.exit(f"No local cluster at {PG_DIR} yet. Run:  python dev.py db init")
        if pg_running():
            print(f"PostgreSQL is already running on port {PG_PORT}")
            return 0
        code = run(
            [
                pg_tool("pg_ctl"),
                "-D",
                str(PG_DIR),
                "-l",
                str(PG_DIR / "postgres.log"),
                "-o",
                f"-p {PG_PORT}",
                "-w",
                "start",
            ],
            cwd=ROOT,
        )
        if code:
            print(
                f"\nSee the log: {PG_DIR / 'postgres.log'}\n"
                f"(is port {PG_PORT} already used by another PostgreSQL? set PGPORT=5433 and try again)",
                file=sys.stderr,
            )
        return code
    if sub == "stop":
        return run([pg_tool("pg_ctl"), "-D", str(PG_DIR), "stop"], cwd=ROOT)
    if sub == "upgrade":
        # database.migrate(): Alembic upgrade, adopting databases created by an older version.
        return run([require_venv(), "database.py"], env={"DATABASE_URL": database_url()})
    if sub == "revision":
        msg = args[1] if len(args) > 1 else "change"
        return run(
            [require_venv(), "-m", "alembic", "revision", "--autogenerate", "-m", msg],
            env={"DATABASE_URL": database_url()},
        )
    if sub == "status":
        tools = shutil.which("initdb") or next(
            (str(d) for d in _pg_candidate_dirs() if (d / ("initdb.exe" if IS_WINDOWS else "initdb")).is_file()), None
        )
        print(f"PostgreSQL tools:  {tools or 'not found (see: python dev.py db init)'}")
        print(f"Local cluster:     {PG_DIR} ({'exists' if PG_DIR.exists() else 'not initialised'})")
        target = os.environ.get("DATABASE_URL") or LOCAL_DB_URL
        status, detail = pg_probe(target) if target.startswith("postgresql") else ("ok", "")
        label = {
            "ok": "running",
            "auth": "running but login rejected",
            "down": "not running",
            "no-driver": "cannot probe",
        }[status]
        shown = target.split("@")[-1] if "@" in target else target  # never print a password
        print(f"Server:            {label} at {shown}{(' — ' + detail) if detail else ''}")
        configured = (
            "(set)" if os.environ.get("DATABASE_URL") else "(not set — dev.py picks PostgreSQL if running, else SQLite)"
        )
        print(f"DATABASE_URL:      {configured}")
        return 0
    print("Subcommands: init | start | stop | status | upgrade | revision <msg>")
    return 1


ENV_ALIASES = {"production": "prod", "prd": "prod", "live": "prod", "stage": "staging", "preprod": "staging"}


def app_env() -> str:
    """APP_ENV as the service will read it (config.normalize_env): case-insensitive, aliases, comments."""
    raw = (os.environ.get("APP_ENV") or "dev").split("#", 1)[0].strip().strip("\"'").lower()
    return ENV_ALIASES.get(raw, raw) or "dev"


def prepare_environment() -> None:
    """Keep a local prod/staging run from dying at startup: the service refuses to start without
    TOKEN_SECRET in those environments, so generate one once and remember it in .env (gitignored).
    Real deployments set it explicitly (docker-compose / Bicep)."""
    env = app_env()
    if env in ("staging", "prod"):
        if not os.environ.get("TOKEN_SECRET"):
            import secrets

            save_env_value("TOKEN_SECRET", secrets.token_urlsafe(48))
            print(f"note: APP_ENV={env} needs TOKEN_SECRET — generated one and saved it to {ENV_FILE}", flush=True)
        hints = [
            f"note: APP_ENV={env}: /api/docs needs an administrator account, the billing sandbox is hidden"
            + (" and the fake test accounts are not created." if env == "prod" else "."),
            "      First administrator: put your e-mail in ADMIN_EMAILS (services/account/.env) before registering,",
            "      or register in the app and run:  python dev.py promote you@example.com",
            "",
        ]
        print("\n".join(hints), flush=True)


def _api(host: str, port: str, reload: bool, extra_env: dict | None = None) -> subprocess.Popen:
    py = require_venv()
    prepare_environment()
    cmd = [str(py), "-m", "uvicorn", "main:app", "--host", host, "--port", port]
    if reload:
        cmd += ["--reload", "--reload-dir", str(SERVICE)]
    env = {
        "DATABASE_URL": database_url(),
        # Fake accounts for testing every membership type (ignored in prod). Printed at startup.
        "SEED_FAKE_ACCOUNTS": os.environ.get("SEED_FAKE_ACCOUNTS", "true"),
        **(extra_env or {}),
    }
    return popen_tree(cmd, SERVICE, env)


def cmd_run(_args: list[str]) -> int:
    require_node_modules()
    check_node()
    api_port, web_port = os.environ.get("PORT", "8000"), os.environ.get("WEB_PORT", "4200")
    for port in (api_port, web_port):
        if port_in_use(port):
            pids = listeners(port)
            sys.exit(
                f"Port {port} is already in use{f' (PID {pids})' if pids else ''} — probably a previous run.\n"
                "Stop it first:  python dev.py stop      (or set PORT / WEB_PORT to use other ports)"
            )
    api = _api("127.0.0.1", api_port, reload=True, extra_env={"DEV_WEB_URL": f"http://localhost:{web_port}/"})
    # NG_CLI_ANALYTICS=false: on first use the Angular CLI otherwise stops at an interactive
    # "share usage data?" question. --host 127.0.0.1 keeps the address unambiguous (IPv4 vs IPv6).
    web = popen_tree(
        [npm(), "start", "--", "--port", web_port, "--host", "127.0.0.1"], WEB, {"NG_CLI_ANALYTICS": "false"}
    )
    _cleanup_on_termination()
    try:
        ready = {"API": False, "app": False}
        started = time.time()
        announced = False
        while True:
            if web.poll() is not None:
                print(
                    f"\nThe Angular dev server exited with code {web.returncode} — see the messages above "
                    "(usually a Node.js version problem or a missing `python dev.py setup`).",
                    file=sys.stderr,
                )
                return web.returncode or 1
            if api.poll() is not None:
                print(f"\nThe API exited with code {api.returncode} — see the messages above.", file=sys.stderr)
                return api.returncode or 1
            if not announced:
                ready["API"] = ready["API"] or port_in_use(api_port)
                ready["app"] = ready["app"] or http_ok(f"http://127.0.0.1:{web_port}/")
                if all(ready.values()):
                    announced = True
                    print(f"\n  API:  http://localhost:{api_port}/api/docs")
                    print(f"  App:  http://localhost:{web_port}/   (or http://127.0.0.1:{web_port}/)")
                    print("        (Angular dev server — no service worker; use `python dev.py serve` to test updates)")
                    print("  Stop: Ctrl+C\n", flush=True)
                elif time.time() - started > 180:
                    announced = True
                    waiting = [k for k, v in ready.items() if not v]
                    print(
                        f"\nStill waiting for the {' and '.join(waiting)} after 3 minutes: the process runs but "
                        "does not answer.\nLook above for a question waiting for input or an error. Press Ctrl+C, "
                        "run `python dev.py stop`, then try again.",
                        file=sys.stderr,
                    )
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        kill_tree(web)
        kill_tree(api)


def http_ok(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=2) as res:
            return 200 <= res.status < 400
    except Exception:  # noqa: BLE001 — any failure just means "not ready yet"
        return False


def _cleanup_on_termination() -> None:
    """Turn SIGTERM (and a closed terminal on POSIX) into KeyboardInterrupt so `finally:` kills the children."""
    import signal

    def raise_interrupt(_signum, _frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), raise_interrupt)
            except (OSError, ValueError):
                pass


def cmd_stop(_args: list[str]) -> int:
    """Kill leftover dev processes (API on PORT, dev server on WEB_PORT) — e.g. after a closed terminal."""
    found = False
    for port in (os.environ.get("PORT", "8000"), os.environ.get("WEB_PORT", "4200")):
        for pid in listeners(port):
            found = True
            print(f"stopping PID {pid} (port {port})")
            if IS_WINDOWS:
                subprocess.call(
                    ["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            else:
                import signal

                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
    if not found:
        busy = [p for p in (os.environ.get("PORT", "8000"), os.environ.get("WEB_PORT", "4200")) if port_in_use(p)]
        if busy:
            print(f"Ports {busy} are in use but the owning process could not be identified (need lsof on this OS).")
            return 1
    print("nothing to stop" if not found else "done")
    return 0


def cmd_seed(args: list[str]) -> int:
    """Create/refresh the fake test accounts (one per membership type) in the development database."""
    return run([require_venv(), "seed.py", *args], env={"DATABASE_URL": database_url()})


def cmd_promote(args: list[str]) -> int:
    if not args:
        sys.exit("usage: python dev.py promote <email> [--demote]")
    flag = "--demote" if "--demote" in args else "--promote"
    return run([require_venv(), "seed.py", flag, args[0]], env={"DATABASE_URL": database_url()})


def cmd_build(_args: list[str]) -> int:
    require_node_modules()
    check_node()
    return run([npm(), "run", "build"], cwd=WEB)


def cmd_serve(_args: list[str]) -> int:
    if cmd_build([]):
        return 1
    port = os.environ.get("PORT", "8000")
    print(f"\n  On this PC:     http://localhost:{port}/")
    print(
        f"  On your phone:  http://{lan_ip()}:{port}/   (same Wi-Fi; installing as an app needs HTTPS or localhost)\n"
    )
    api = _api(
        "0.0.0.0",
        port,
        reload=False,
        extra_env={"WEB_DIST": str(WEB / "dist" / "web" / "browser")},
    )
    try:
        return api.wait()
    except KeyboardInterrupt:
        api.terminate()
        return 0


def cmd_test(args: list[str]) -> int:
    py = require_venv()
    env = {}
    if "--postgres" in args:
        env["TEST_DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", LOCAL_DB_URL + "_test")
        args = [a for a in args if a != "--postgres"]
    code = run([py, "-m", "pytest", *args], env=env)
    if (WEB / "node_modules").is_dir():
        code = code or run([npm(), "test", "--", "--watch=false"], cwd=WEB)
    return code


def cmd_e2e(args: list[str]) -> int:
    py = require_venv()
    require_node_modules()
    return run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=E2E)


def cmd_lint(_args: list[str]) -> int:
    py = require_venv()
    code = run([py, "-m", "ruff", "check", "."]) or run([py, "-m", "ruff", "format", "--check", "."])
    code = code or run([py, "-m", "ruff", "check", str(E2E), str(ROOT / "dev.py")], cwd=ROOT)
    if (WEB / "node_modules").is_dir():
        code = code or run([npm(), "run", "lint"], cwd=WEB)
    return code


def cmd_format(_args: list[str]) -> int:
    py = require_venv()
    code = run([py, "-m", "ruff", "check", "--fix", "."]) or run([py, "-m", "ruff", "format", "."])
    code = code or run([py, "-m", "ruff", "format", str(E2E), str(ROOT / "dev.py")], cwd=ROOT)
    if (WEB / "node_modules").is_dir():
        code = code or run([npm(), "run", "format"], cwd=WEB)
    return code


def cmd_check(args: list[str]) -> int:
    return cmd_lint(args) or cmd_test(args)


def cmd_release(args: list[str]) -> int:
    """Bump the version-controlled release descriptor. Schema/min-supported are edited by hand on purpose."""
    if not args:
        sys.exit("usage: python dev.py release <version> [--critical] [--notes 'text'] [--min-supported <version>]")
    path = WEB / "release.json"
    rel = json.loads(path.read_text(encoding="utf-8"))
    rel["version"] = args[0]
    rel["critical"] = "--critical" in args
    if "--notes" in args:
        rel["notes"] = args[args.index("--notes") + 1]
    if "--min-supported" in args:
        rel["minSupportedVersion"] = args[args.index("--min-supported") + 1]
    path.write_text(json.dumps(rel, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(rel, indent=2))
    print("\nCommit this file; the deploy pipeline builds, uploads and publishes it.")
    return 0


def cmd_publish(args: list[str]) -> int:
    """POST release.json to a running API (what the pipeline does after the CDN upload)."""
    import urllib.request

    if not args:
        sys.exit(
            "usage: python dev.py publish https://api.example.com   (needs RELEASE_ADMIN_TOKEN in the environment)"
        )
    token = os.environ.get("RELEASE_ADMIN_TOKEN")
    if not token:
        sys.exit("RELEASE_ADMIN_TOKEN is not set")
    body = (WEB / "release.json").read_bytes()
    req = urllib.request.Request(
        args[0].rstrip("/") + "/api/v1/release/publish",
        data=body,
        headers={"Content-Type": "application/json", "X-Admin-Token": token},
    )
    with urllib.request.urlopen(req) as res:
        print(res.status, res.read().decode())
    return 0


def cmd_docker(args: list[str]) -> int:
    docker = shutil.which("docker")
    if not docker:
        sys.exit("docker not found. Install Docker Desktop (Windows/macOS) or Docker Engine (Linux).")
    return run([docker, "compose", "up", "--build", *args], cwd=ROOT)


COMMANDS = {
    "setup": cmd_setup,
    "db": cmd_db,
    "run": cmd_run,
    "stop": cmd_stop,
    "seed": cmd_seed,
    "promote": cmd_promote,
    "serve": cmd_serve,
    "build": cmd_build,
    "test": cmd_test,
    "e2e": cmd_e2e,
    "lint": cmd_lint,
    "format": cmd_format,
    "check": cmd_check,
    "release": cmd_release,
    "publish": cmd_publish,
    "docker": cmd_docker,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"} or argv[0] not in COMMANDS:
        print(__doc__)
        return 0 if argv and argv[0] in {"-h", "--help", "help"} else 1
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
