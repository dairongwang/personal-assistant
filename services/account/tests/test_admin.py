"""Administrator account: production support and management through /api/v1/admin/*."""

import base64

import pytest
from conftest import auth_key_for, headers, login, register

import config
import seed


@pytest.fixture()
def admin(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAILS", {"admin@example.com"})
    return register(client, email="admin@example.com", password="Admin-pass-123")


def test_stats_and_account_list(client, admin):
    free = register(client, email="free@example.com")
    pro = register(client, email="pro@example.com")
    client.post("/api/v1/billing/trial", headers=headers(pro))
    login(client, email="pro@example.com")  # second device

    stats = client.get("/api/v1/admin/stats", headers=headers(admin)).json()
    assert stats["accounts_total"] == 3 and stats["accounts_deleted"] == 0
    assert stats["memberships"] == {"free": 1, "paid": 1, "admin": 1}
    assert stats["subscriptions"] == {"none": 2, "trialing": 1}
    assert stats["devices_active"] == 4 and stats["sync_ops"] == 0 and stats["database"] in ("sqlite", "postgresql")

    page = client.get("/api/v1/admin/accounts", headers=headers(admin)).json()
    assert page["total"] == 3 and {a["email"] for a in page["items"]} == {
        "admin@example.com",
        "free@example.com",
        "pro@example.com",
    }
    by_email = {a["email"]: a for a in page["items"]}
    assert by_email["free@example.com"]["membership"] == "free" and by_email["free@example.com"]["devices_active"] == 1
    assert by_email["pro@example.com"]["membership"] == "paid" and by_email["pro@example.com"]["devices_active"] == 2
    assert by_email["admin@example.com"]["role"] == "admin" and by_email["admin@example.com"]["membership"] == "admin"

    found = client.get("/api/v1/admin/accounts?q=PRO", headers=headers(admin)).json()
    assert [a["email"] for a in found["items"]] == ["pro@example.com"]
    paid_only = client.get("/api/v1/admin/accounts?membership=paid", headers=headers(admin)).json()
    assert [a["email"] for a in paid_only["items"]] == ["pro@example.com"]

    page = client.get("/api/v1/admin/accounts?limit=2&offset=1", headers=headers(admin)).json()
    assert page["total"] == 3 and len(page["items"]) == 2 and page["offset"] == 1
    page = client.get("/api/v1/admin/accounts?membership=free&limit=1&offset=0", headers=headers(admin)).json()
    assert page["total"] == 1 and [a["email"] for a in page["items"]] == ["free@example.com"]

    detail = client.get(f"/api/v1/admin/accounts/{free['account_id']}", headers=headers(admin)).json()
    assert detail["max_active_tasks"] == 3 and detail["features"] == [] and len(detail["devices"]) == 1
    assert "ciphertext" not in str(detail)
    assert client.get("/api/v1/admin/accounts/nope", headers=headers(admin)).status_code == 404


def test_admin_grants_and_revokes_paid_membership_with_audit_trail(client, admin):
    user = register(client, email="user@example.com")
    uid = user["account_id"]
    assert client.get("/api/v1/sync/status", headers=headers(user)).status_code == 402

    r = client.patch(
        f"/api/v1/admin/accounts/{uid}/membership",
        json={"plan": "pro", "days": 90, "note": "comp for beta feedback"},
        headers=headers(admin),
    )
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["membership"] == "paid" and detail["plan"] == "pro" and detail["status"] == "active"
    assert detail["provider"] == "admin" and detail["current_period_end"]
    assert detail["audit"][0]["action"] == "membership.set"
    assert detail["audit"][0]["actor_email"] == "admin@example.com"
    assert detail["audit"][0]["detail"]["note"] == "comp for beta feedback"
    # The user now has the full feature set.
    ent = client.get("/api/v1/me/entitlements", headers=headers(user)).json()
    assert ent["membership"] == "paid" and ent["max_active_tasks"] is None
    assert client.get("/api/v1/sync/status", headers=headers(user)).status_code == 200

    r = client.patch(f"/api/v1/admin/accounts/{uid}/membership", json={"plan": "lifetime"}, headers=headers(admin))
    assert r.json()["plan"] == "lifetime" and r.json()["current_period_end"] is None

    r = client.patch(f"/api/v1/admin/accounts/{uid}/membership", json={"plan": "free"}, headers=headers(admin))
    assert r.json()["membership"] == "free" and r.json()["status"] == "canceled"
    assert client.get("/api/v1/sync/status", headers=headers(user)).status_code == 402
    # status "none" hands the trial back.
    r = client.patch(
        f"/api/v1/admin/accounts/{uid}/membership", json={"plan": "free", "status": "none"}, headers=headers(admin)
    )
    assert client.get("/api/v1/me/entitlements", headers=headers(user)).json()["trial_available"] is True

    log = client.get("/api/v1/admin/audit", headers=headers(admin)).json()
    assert [e["action"] for e in log][:4] == ["membership.set"] * 4
    assert all(e["detail"]["target_account_id"] == uid for e in log[:4])


def test_admin_promotes_and_cannot_demote_self(client, admin):
    user = register(client, email="user@example.com")
    uid = user["account_id"]
    assert client.get("/api/v1/admin/stats", headers=headers(user)).status_code == 403

    r = client.patch(f"/api/v1/admin/accounts/{uid}/role", json={"role": "admin"}, headers=headers(admin))
    assert r.status_code == 200 and r.json()["role"] == "admin" and r.json()["membership"] == "admin"
    assert client.get("/api/v1/admin/stats", headers=headers(user)).status_code == 200
    assert client.get("/api/v1/me", headers=headers(user)).json()["entitlements"]["max_active_tasks"] is None

    r = client.patch(f"/api/v1/admin/accounts/{uid}/role", json={"role": "user"}, headers=headers(admin))
    assert r.json()["role"] == "user" and r.json()["membership"] == "free"
    assert client.get("/api/v1/admin/stats", headers=headers(user)).status_code == 403

    me = admin["account_id"]
    r = client.patch(f"/api/v1/admin/accounts/{me}/role", json={"role": "user"}, headers=headers(admin))
    assert r.status_code == 409


def test_admin_signs_an_account_out_everywhere_and_purges_it(client, admin):
    user = register(client, email="user@example.com")
    second = login(client, email="user@example.com")
    uid = user["account_id"]

    r = client.post(f"/api/v1/admin/accounts/{uid}/sign-out", headers=headers(admin))
    assert r.status_code == 200 and r.json()["devices_active"] == 0
    assert client.get("/api/v1/me", headers=headers(user)).status_code == 401
    assert client.get("/api/v1/me", headers=headers(second)).status_code == 401
    third = login(client, email="user@example.com")  # the password still works
    client.post("/api/v1/billing/trial", headers=headers(third))
    client.post(
        "/api/v1/sync/push",
        json={"ops": [{"op_id": "op-1", "schema_version": 1, "ciphertext": base64.b64encode(b"x" * 20).decode()}]},
        headers=headers(third),
    )

    assert client.delete(f"/api/v1/admin/accounts/{uid}", headers=headers(admin)).status_code == 204
    assert client.get("/api/v1/me", headers=headers(third)).status_code == 401
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "user@example.com", "auth_key": auth_key_for("correct horse", "user@example.com"), "device": {}},
    )
    assert r.status_code == 401
    stats = client.get("/api/v1/admin/stats", headers=headers(admin)).json()
    assert stats["accounts_deleted"] == 1 and stats["sync_ops"] == 0
    deleted = client.get("/api/v1/admin/accounts?include_deleted=true", headers=headers(admin)).json()
    assert any(a["deleted_at"] for a in deleted["items"])
    assert client.get("/api/v1/admin/accounts", headers=headers(admin)).json()["total"] == 1
    # Admins delete their own account like everybody else (from the account screen).
    assert client.delete(f"/api/v1/admin/accounts/{admin['account_id']}", headers=headers(admin)).status_code == 409


def test_seeded_admin_can_manage_seeded_users(client):
    """The fake accounts (seed.py) cover every membership; the admin one works against the API."""
    import database

    with database.SessionLocal() as db:
        seed.seed(db, iterations=1000)
    by_email = {spec.email: spec for spec in seed.FAKE_ACCOUNTS}
    admin_spec = by_email["admin@example.com"]
    r = client.post(
        "/api/v1/auth/login",
        json={
            "email": admin_spec.email,
            "auth_key": _seed_auth_key(admin_spec, 1000),
            "device": {"platform": "test"},
        },
    )
    assert r.status_code == 200, r.text
    admin_session = r.json()
    page = client.get("/api/v1/admin/accounts", headers=headers(admin_session)).json()
    assert page["total"] == len(seed.FAKE_ACCOUNTS)
    assert {a["email"]: a["membership"] for a in page["items"]} == {
        spec.email: spec.membership for spec in seed.FAKE_ACCOUNTS
    }
    assert {a["email"]: a["status"] for a in page["items"]}["pastdue@example.com"] == "past_due"


def _seed_auth_key(spec: seed.FakeAccount, iterations: int) -> str:
    import clientcrypto

    return clientcrypto.derive_keys(spec.password, spec.email, iterations)[0]
