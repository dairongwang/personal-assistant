#!/usr/bin/env python3
"""Cross-platform developer commands for the Personal Assistant (Windows, macOS, Linux).

Uses only the standard library and always runs tools from the project's virtual
environment (backend/venv) — nothing is installed globally.

    python dev.py setup            create venv + install runtime and dev dependencies
    python dev.py run              start the API + frontend with auto-reload (localhost)
    python dev.py serve            start on all interfaces for phones/tablets on your network
    python dev.py test             run the test suite (pytest)
    python dev.py lint             ruff check + format check (+ JS syntax check if node exists)
    python dev.py format           auto-format Python with ruff
    python dev.py check            lint + test (what CI runs)
    python dev.py icons            regenerate PWA icons (needs Pillow in the venv)
    python dev.py docker           build and run with Docker Compose

Options: --python <interpreter> for setup (default: the interpreter running this script).
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
VENV = BACKEND / "venv"
IS_WINDOWS = platform.system() == "Windows"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def run(cmd: list[str], cwd: Path = BACKEND, env: dict | None = None) -> int:
    print("$", " ".join(str(c) for c in cmd), flush=True)
    full_env = {**os.environ, **(env or {})}
    return subprocess.call([str(c) for c in cmd], cwd=str(cwd), env=full_env)


def require_venv() -> Path:
    py = venv_python()
    if not py.exists():
        sys.exit(f"Virtual environment not found at {VENV}.\nRun:  python dev.py setup")
    return py


def lan_ip() -> str:
    """Best-effort LAN address for the 'open this on your phone' hint."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets are sent; picks the default route
        return s.getsockname()[0]
    except OSError:
        return "<your-pc-ip>"
    finally:
        s.close()


# ---------------------------------------------------------------------------- commands


def cmd_setup(args: list[str]) -> int:
    base_python = sys.executable
    if "--python" in args:
        base_python = args[args.index("--python") + 1]
    if not venv_python().exists():
        code = run([base_python, "-m", "venv", str(VENV)])
        if code:
            return code
    py = venv_python()
    code = run([py, "-m", "pip", "install", "--upgrade", "pip"])
    if code:
        return code
    code = run([py, "-m", "pip", "install", "-r", "requirements-dev.txt"])
    if code:
        return code
    print("\nSetup complete. Next:  python dev.py run")
    return 0


def _uvicorn(host: str, reload: bool) -> int:
    py = require_venv()
    cmd = [py, "-m", "uvicorn", "main:app", "--host", host, "--port", os.environ.get("PORT", "8000")]
    if reload:
        cmd += ["--reload", "--reload-dir", str(BACKEND), "--reload-dir", str(ROOT / "frontend")]
    return run(cmd)


def cmd_run(_args: list[str]) -> int:
    port = os.environ.get("PORT", "8000")
    print(f"\n  App:  http://localhost:{port}/\n  Docs: http://localhost:{port}/docs\n")
    return _uvicorn("127.0.0.1", reload=True)


def cmd_serve(_args: list[str]) -> int:
    port = os.environ.get("PORT", "8000")
    print(f"\n  On this PC:       http://localhost:{port}/")
    print(f"  On your phone:    http://{lan_ip()}:{port}/   (same Wi-Fi)")
    if IS_WINDOWS:
        print("  Windows firewall: allow port", port, "once (see README → Use it from your phone)")
    print()
    return _uvicorn("0.0.0.0", reload=False)


def cmd_test(args: list[str]) -> int:
    return run([require_venv(), "-m", "pytest", *args])


def cmd_lint(_args: list[str]) -> int:
    py = require_venv()
    code = run([py, "-m", "ruff", "check", "."]) or run([py, "-m", "ruff", "format", "--check", "."])
    node = shutil.which("node")
    if node:
        for js in (ROOT / "frontend" / "js" / "app.js", ROOT / "frontend" / "sw.js"):
            code = code or run([node, "--check", str(js)], cwd=ROOT)
    else:
        print("(node not found — skipping JavaScript syntax check)")
    return code


def cmd_format(_args: list[str]) -> int:
    py = require_venv()
    return run([py, "-m", "ruff", "check", "--fix", "."]) or run([py, "-m", "ruff", "format", "."])


def cmd_check(args: list[str]) -> int:
    return cmd_lint(args) or cmd_test(args)


def cmd_icons(_args: list[str]) -> int:
    return run([require_venv(), str(ROOT / "scripts" / "make_icons.py")], cwd=ROOT)


def cmd_docker(args: list[str]) -> int:
    docker = shutil.which("docker")
    if not docker:
        sys.exit("docker not found. Install Docker Desktop (Windows/macOS) or Docker Engine (Linux).")
    return run([docker, "compose", "up", "--build", *args], cwd=ROOT)


COMMANDS = {
    "setup": cmd_setup,
    "run": cmd_run,
    "serve": cmd_serve,
    "test": cmd_test,
    "lint": cmd_lint,
    "format": cmd_format,
    "check": cmd_check,
    "icons": cmd_icons,
    "docker": cmd_docker,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"} or argv[0] not in COMMANDS:
        print(__doc__)
        return 0 if argv and argv[0] in {"-h", "--help", "help"} else 1
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
