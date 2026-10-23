"""Fixtures. SQLite by default; set TEST_DATABASE_URL=postgresql+psycopg://… to run the same
suite against PostgreSQL (CI does both). Postgres tables come from the Alembic migrations so the
migration chain itself is under test."""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

_TMP = tempfile.mkdtemp(prefix="pa-account-tests-")
os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = _TMP
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{Path(_TMP, 'test.db').as_posix()}"
os.environ["TOKEN_SECRET"] = "test-secret"
os.environ["RELEASE_ADMIN_TOKEN"] = "admin-token"
os.environ["BILLING_FAKE_PROVIDERS"] = "false"  # test_fakes.py switches the built-in providers on explicitly
os.environ["RELEASE_FILE"] = str(Path(_TMP, "missing-release.json"))  # no fallback manifest in tests
os.environ.pop("CORS_ORIGINS", None)
os.environ.pop("WEB_DIST", None)

import ads  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import main  # noqa: E402
import models  # noqa: E402


def _reset_schema() -> None:
    if config.IS_POSTGRES:
        from alembic.config import Config

        from alembic import command

        cfg = Config(str(SERVICE_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(SERVICE_DIR / "alembic"))
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
    else:
        from sqlalchemy import text

        models.Base.metadata.drop_all(bind=database.engine)
        with database.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        database.migrate()  # SQLite is migrated with Alembic too — same chain as PostgreSQL


@pytest.fixture()
def client():
    _reset_schema()
    # The anonymous ad endpoints are rate-limited per caller; every test starts with a clean window.
    ads.SERVE_LIMITER = ads.RateLimiter(ads.SERVE_LIMITER.limit, ads.SERVE_LIMITER.window)
    ads.METRICS_LIMITER = ads.RateLimiter(ads.METRICS_LIMITER.limit, ads.METRICS_LIMITER.window)
    with TestClient(main.app) as c:
        yield c


# ---------------------------------------------------------------------------- helpers


def auth_key_for(password: str, email: str) -> str:
    """What the PWA derives on the device (see crypto.ts) — the server only ever sees this."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), hashlib.sha256(email.encode()).digest(), 1000).hex()


def register(client, email="ann@example.com", password="correct horse", device=None, version="1.0.0", schema=1):
    r = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "auth_key": auth_key_for(password, email),
            "wrapped_data_key": base64.b64encode(b"wrapped-key-" + email.encode()).decode(),
            "kdf_params": {"kdf": "pbkdf2", "iterations": 1000},
            "device": device or {"platform": "web", "client_version": version, "schema_version": schema},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def login(client, email="ann@example.com", password="correct horse", version="1.0.0", schema=1):
    r = client.post(
        "/api/v1/auth/login",
        json={
            "email": email,
            "auth_key": auth_key_for(password, email),
            "device": {"platform": "web", "client_version": version, "schema_version": schema},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def headers(session, version="1.0.0", schema=1):
    return {"Authorization": f"Bearer {session['token']}", "X-Client-Version": version, "X-Schema-Version": str(schema)}


def go_pro(client, session):
    assert client.post("/api/v1/billing/trial", headers=headers(session)).status_code == 200


def cipher(text: str) -> str:
    """Stand-in for the client's AES-GCM output: the server must treat it as opaque bytes."""
    return base64.b64encode(b"\x00ENC" + text.encode()).decode()


@pytest.fixture()
def pro(client):
    session = register(client)
    go_pro(client, session)
    return session
