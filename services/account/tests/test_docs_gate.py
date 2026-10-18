"""/api/docs, /api/redoc, /api/openapi.json: open in dev/test/qa/staging, administrators only in prod."""

import pytest
from conftest import headers, register

import config
import docs_gate


def test_docs_are_open_outside_production(client):
    assert config.DOCS_REQUIRE_ADMIN is False  # APP_ENV=test
    assert client.get("/api/docs").status_code == 200 and "swagger" in client.get("/api/docs").text.lower()
    assert client.get("/api/redoc").status_code == 200
    assert "/api/v1/admin/stats" in client.get("/api/openapi.json").json()["paths"]


@pytest.fixture()
def gated(monkeypatch):
    monkeypatch.setattr(config, "DOCS_REQUIRE_ADMIN", True)
    monkeypatch.setattr(config, "ADMIN_EMAILS", {"admin@example.com", "admin2@example.com"})


def test_production_docs_need_an_administrator(client, gated):
    # Anonymous browser: the sign-in page, not the docs (and no schema).
    r = client.get("/api/docs")
    assert r.status_code == 401 and "administrator sign-in" in r.text.lower() and "swagger" not in r.text.lower()
    assert "iterations: 600000" in r.text  # same key derivation as the PWA
    assert client.get("/api/redoc").status_code == 401
    assert client.get("/api/openapi.json").status_code == 401
    # A normal user's token does not help; an administrator's does (API clients: bearer header).
    user = register(client, email="user@example.com")
    admin = register(client, email="admin@example.com")
    assert client.get("/api/docs", headers=headers(user)).status_code == 401
    assert client.get("/api/openapi.json", headers=headers(user)).status_code == 401
    assert client.get("/api/docs", headers=headers(admin)).status_code == 200
    assert client.get("/api/redoc", headers=headers(admin)).status_code == 200
    assert "/api/v1/admin/stats" in client.get("/api/openapi.json", headers=headers(admin)).json()["paths"]


def test_browser_flow_cookie_login_and_logout(client, gated):
    user = register(client, email="user@example.com")
    admin = register(client, email="admin@example.com")
    # The gate page posts the session token it obtained from /auth/login.
    r = client.post("/api/docs/login", json={"token": user["token"]})
    assert r.status_code == 403 and docs_gate.COOKIE not in client.cookies
    r = client.post("/api/docs/login", json={"token": "garbage"})
    assert r.status_code == 403
    r = client.post("/api/docs/login", json={"token": admin["token"]})
    assert r.status_code == 200 and r.json()["redirect"] == "/api/docs"
    assert docs_gate.COOKIE in client.cookies
    assert "httponly" in r.headers["set-cookie"].lower() and "path=/api" in r.headers["set-cookie"].lower()
    # The cookie (kept by the test client) opens the docs — for browsers only, the API itself stays bearer-only.
    assert client.get("/api/docs").status_code == 200
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/api/v1/me").status_code == 401
    audit = client.get("/api/v1/admin/audit", headers=headers(admin)).json()
    assert audit[0]["action"] == "docs.login" and audit[0]["actor_email"] == "admin@example.com"
    # Logout clears the cookie and revokes that session.
    r = client.post("/api/docs/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/api/docs"
    assert client.get("/api/docs").status_code == 401
    assert client.get("/api/v1/me", headers=headers(admin)).status_code == 401  # session revoked
    # Demoted administrators lose access even with a cookie.
    admin2 = register(client, email="admin2@example.com")
    client.post("/api/docs/login", json={"token": admin2["token"]})
    assert client.get("/api/docs").status_code == 200
    import database
    import models

    with database.SessionLocal() as db:
        db.get(models.Account, admin2["account_id"]).role = "user"
        db.commit()
    assert client.get("/api/docs").status_code == 401


def test_override_flag_can_gate_any_environment(client, monkeypatch):
    monkeypatch.setattr(config, "DOCS_REQUIRE_ADMIN", True)
    assert client.get("/api/docs").status_code == 401
    monkeypatch.setattr(config, "DOCS_REQUIRE_ADMIN", False)
    assert client.get("/api/docs").status_code == 200


# ---------------------------------------------------------------------------- configuration


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("prod", "prod"),
        ("PROD", "prod"),
        (" Production ", "prod"),
        ('"prod"', "prod"),
        ("prod   # live", "prod"),
        ("staging", "staging"),
        ("qa", "qa"),
        ("UAT", "qa"),
        ("dev", "dev"),
        ("", "dev"),
        (None, "dev"),
    ],
)
def test_app_env_is_normalised(raw, expected):
    assert config.normalize_env(raw) == expected


def test_dotenv_lines_tolerate_comments_quotes_and_export():
    assert config.parse_dotenv_line("APP_ENV=prod") == ("APP_ENV", "prod")
    assert config.parse_dotenv_line("APP_ENV=prod   # production") == ("APP_ENV", "prod")
    assert config.parse_dotenv_line('APP_ENV="prod"') == ("APP_ENV", "prod")
    assert config.parse_dotenv_line("export APP_ENV=prod") == ("APP_ENV", "prod")
    assert config.parse_dotenv_line("\ufeffAPP_ENV=prod") == ("APP_ENV", "prod")
    assert config.parse_dotenv_line("TOKEN_SECRET='a #1'") == ("TOKEN_SECRET", "a #1")  # quoted: kept verbatim
    assert config.parse_dotenv_line("# APP_ENV=prod") is None
    assert config.parse_dotenv_line("") is None


def _effective_config(env: dict) -> dict:
    """Start a fresh interpreter with exactly this environment (plus a .env file in a temp
    service dir is not needed: real variables win over .env) and read what config.py decided."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = "import config, json; print(json.dumps({'ENV': config.ENV, 'docs': config.DOCS_REQUIRE_ADMIN}))"
    clean = {k: v for k, v in os.environ.items() if k not in ("APP_ENV", "DOCS_REQUIRE_ADMIN")}
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env={**clean, "TOKEN_SECRET": "x", **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_prod_always_gates_the_docs_other_environments_do_not():
    assert _effective_config({"APP_ENV": "prod"}) == {"ENV": "prod", "docs": True}
    assert _effective_config({"APP_ENV": "Production"}) == {"ENV": "prod", "docs": True}
    # Nothing switches it off in prod — not even an explicit false.
    assert _effective_config({"APP_ENV": "prod", "DOCS_REQUIRE_ADMIN": "false"}) == {"ENV": "prod", "docs": True}
    for env in ("dev", "test", "qa", "staging"):
        assert _effective_config({"APP_ENV": env}) == {"ENV": env, "docs": False}
    assert _effective_config({"APP_ENV": "qa", "DOCS_REQUIRE_ADMIN": "true"}) == {"ENV": "qa", "docs": True}


def test_prod_in_dotenv_file_is_honoured(tmp_path, monkeypatch):
    """The user's case: APP_ENV=prod in services/account/.env and nothing else."""
    import os

    for key in ("APP_ENV", "DOCS_REQUIRE_ADMIN"):
        monkeypatch.delenv(key, raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("APP_ENV=prod   # go live\nTOKEN_SECRET=abc\n", encoding="utf-8")
    config._load_dotenv(dotenv)
    try:
        assert os.environ["APP_ENV"] == "prod"
        assert os.environ["TOKEN_SECRET"] == "test-secret"  # real variables win over .env
        assert config.normalize_env(os.environ["APP_ENV"]) == "prod"
    finally:
        monkeypatch.delenv("APP_ENV", raising=False)
