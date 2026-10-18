"""End-to-end tests: real Chromium (Playwright), real service worker, real API.

    python dev.py e2e            # builds the PWA, starts the API on a free port, runs these

The API uses a temporary SQLite database here (the service's own pytest suite covers
PostgreSQL); the PWA is served by the API from apps/web/dist so the service worker is active
(it only runs on localhost or HTTPS). "Deploying a release" = rebuilding into dist + publishing
the manifest — exactly what the Azure pipeline does with the CDN.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
SERVICE = ROOT / "services" / "account"
DIST = WEB / "dist" / "web" / "browser"
RELEASE_FILE = WEB / "release.json"
ADMIN_TOKEN = "e2e-admin-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _venv_python() -> str:
    exe = SERVICE / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(exe) if exe.exists() else sys.executable


TMP_RELEASE = Path(tempfile.mkdtemp(prefix="pa-e2e-release-")) / "release.json"


def build_web(release_file: Path | None = None) -> None:
    """`npm run build` with RELEASE_JSON pointing at a temporary descriptor — the committed
    apps/web/release.json is never modified by the tests."""
    npm = shutil.which("npm") or "npm"
    env = {**os.environ, **({"RELEASE_JSON": str(release_file)} if release_file else {})}
    out = subprocess.run([npm, "run", "build"], cwd=WEB, capture_output=True, text=True, env=env, check=False)
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]


@pytest.fixture(scope="session")
def release_json():
    """The committed descriptor as a template; generated files are restored after the session."""
    yield json.loads(RELEASE_FILE.read_text(encoding="utf-8"))
    subprocess.run([shutil.which("node") or "node", "scripts/prepare-release.mjs"], cwd=WEB, check=False)


@pytest.fixture(scope="session")
def server(release_json):
    port = _free_port()
    tmp = tempfile.mkdtemp(prefix="pa-e2e-")
    env = {
        **os.environ,
        "APP_ENV": "test",
        "DATA_DIR": tmp,
        "DATABASE_URL": f"sqlite:///{Path(tmp, 'e2e.db').as_posix()}",
        "TOKEN_SECRET": "e2e-secret",
        "RELEASE_ADMIN_TOKEN": ADMIN_TOKEN,
        "WEB_DIST": str(DIST),
        "SEED_FAKE_ACCOUNTS": "true",  # free@/pro@/admin@example.com … (services/account/seed.py)
    }
    proc = subprocess.Popen(
        [
            _venv_python(),
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=SERVICE,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/v1/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("API did not start")
    yield base
    proc.terminate()
    proc.wait(10)


class Releaser:
    """Simulates the deploy pipeline: bump release.json → build → publish the manifest."""

    def __init__(self, base: str, template: dict):
        self.base = base
        self.template = template

    def publish(
        self,
        version: str,
        *,
        critical=False,
        notes="",
        min_supported="1.0.0",
        build=True,
    ) -> dict:
        rel = {
            **self.template,
            "version": version,
            "critical": critical,
            "notes": notes,
            "minSupportedVersion": min_supported,
        }
        TMP_RELEASE.write_text(json.dumps(rel, indent=2) + "\n", encoding="utf-8")
        if build:
            build_web(TMP_RELEASE)
        req = urllib.request.Request(
            self.base + "/api/v1/release/publish",
            data=json.dumps(rel).encode(),
            headers={"Content-Type": "application/json", "X-Admin-Token": ADMIN_TOKEN},
        )
        assert urllib.request.urlopen(req).status == 201
        return rel


@pytest.fixture(scope="session")
def releaser(server, release_json):
    r = Releaser(server, release_json)
    r.publish("1.0.0", notes="e2e baseline")
    return r


BROWSER_ORDER = ("chromium", "msedge", "chrome", "firefox", "webkit")


def launch_any_browser(p):
    """Launch whatever browser is available, without requiring a download first.

    Order: E2E_BROWSER if set; Playwright's bundled Chromium; Microsoft Edge or Google Chrome
    installed on the machine (Playwright "channels" — Edge ships with Windows); bundled
    Firefox/WebKit. If none exists, download Chromium once (`playwright install chromium`).
    Returns (browser, name).
    """
    preferred = os.environ.get("E2E_BROWSER", "").strip().lower()
    candidates = [preferred] if preferred else list(BROWSER_ORDER)
    problems = []
    for name in candidates:
        try:
            if name in ("msedge", "chrome"):
                return p.chromium.launch(channel=name), name
            return getattr(p, name).launch(), name
        except Exception as exc:  # noqa: BLE001 — "executable doesn't exist", try the next one
            problems.append(f"  {name}: {str(exc).splitlines()[0][:100]}")
    print("No browser found for end-to-end tests:\n" + "\n".join(problems), flush=True)
    print("Downloading Chromium for Playwright (one-time, ~150 MB)…", flush=True)
    code = subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])
    if code:
        raise RuntimeError(
            "Could not download Chromium. Install Google Chrome or Microsoft Edge, or run:  "
            "services/account/venv/Scripts/python -m playwright install chromium"
        )
    return p.chromium.launch(), "chromium (downloaded)"


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b, name = launch_any_browser(p)
        print(f"\n[e2e] using browser: {name} {b.version}", flush=True)
        b.e2e_name = name  # type: ignore[attr-defined]
        yield b
        b.close()


def phone_context(browser, **kwargs):
    """A phone-like context. `is_mobile` is Chromium-only; other engines still get touch + viewport."""
    options = {"viewport": {"width": 390, "height": 844}, "has_touch": True, **kwargs}
    if "firefox" not in getattr(browser, "e2e_name", "") and "webkit" not in getattr(browser, "e2e_name", ""):
        options["is_mobile"] = True
    return browser.new_context(**options)
