"""Membership types (strategy pattern): anonymous · free · paid · admin — and how an account
resolves to one of them from its role and billing state."""

import json
from datetime import timedelta

import pytest
from conftest import headers, register

import config
import database
import membership
import models

# ---------------------------------------------------------------------------- strategies (pure)


def test_every_strategy_answers_the_same_questions():
    for strategy in membership.STRATEGIES.values():
        info = strategy.describe()
        assert info.type == strategy.type and info.label
        assert strategy.can_activate_task(0)
        assert strategy.can_use("sync") == ("sync" in strategy.features)


def test_free_memberships_allow_three_active_tasks_paid_unlimited():
    for free in (membership.ANONYMOUS, membership.FREE):
        assert free.max_active_tasks == 3
        assert free.can_activate_task(2) and not free.can_activate_task(3) and not free.can_activate_task(10)
        assert not free.can_use("sync")
    for full in (membership.PAID, membership.ADMIN):
        assert full.max_active_tasks is None
        assert full.can_activate_task(6) and full.can_activate_task(1000)
        assert full.can_use("sync") and full.can_use("vault")
    assert membership.ADMIN.can_use("admin") and not membership.PAID.can_use("admin")


def test_free_limit_is_configurable(monkeypatch):
    monkeypatch.setattr(config, "FREE_MAX_ACTIVE_TASKS", 5)
    assert membership.FREE.max_active_tasks == 5 and membership.ANONYMOUS.max_active_tasks == 5
    assert membership.FREE.can_activate_task(4) and not membership.FREE.can_activate_task(5)


def test_strategy_for_role_beats_subscription():
    admin = models.Account(email="a@x", role=models.ROLE_ADMIN, auth_salt="", auth_hash="", wrapped_data_key="")
    user = models.Account(email="u@x", role=models.ROLE_USER, auth_salt="", auth_hash="", wrapped_data_key="")
    assert membership.strategy_for(None, False) is membership.ANONYMOUS
    assert membership.strategy_for(admin, False) is membership.ADMIN
    assert membership.strategy_for(admin, True) is membership.ADMIN
    assert membership.strategy_for(user, False) is membership.FREE
    assert membership.strategy_for(user, True) is membership.PAID


# ---------------------------------------------------------------------------- through the API


def test_public_catalog_lists_the_four_memberships(client):
    cat = client.get("/api/v1/membership").json()["memberships"]
    assert [m["type"] for m in cat] == ["anonymous", "free", "paid", "admin"]
    by_type = {m["type"]: m for m in cat}
    assert by_type["anonymous"]["requires_account"] is False and by_type["anonymous"]["max_active_tasks"] == 3
    assert by_type["free"]["max_active_tasks"] == 3 and by_type["free"]["can_upgrade"]
    assert by_type["paid"]["max_active_tasks"] is None and "sync" in by_type["paid"]["features"]
    assert by_type["admin"]["max_active_tasks"] is None and "admin" in by_type["admin"]["features"]


def test_account_membership_follows_billing_state(client):
    s = register(client)
    ent = client.get("/api/v1/me/entitlements", headers=headers(s)).json()
    assert ent["membership"] == "free" and ent["max_active_tasks"] == 3 and ent["features"] == []
    assert client.get("/api/v1/membership/me", headers=headers(s)).json()["type"] == "free"

    client.post("/api/v1/billing/trial", headers=headers(s))
    ent = client.get("/api/v1/me/entitlements", headers=headers(s)).json()
    assert ent["membership"] == "paid" and ent["membership_label"] == "Pro" and ent["max_active_tasks"] is None
    assert client.get("/api/v1/membership/me", headers=headers(s)).json()["type"] == "paid"

    # Trial lapses (beyond the grace period) → back to the free membership and its limits.
    with database.SessionLocal() as db:
        sub = db.get(models.Subscription, s["account_id"])
        sub.current_period_end = models.utcnow() - timedelta(days=config.GRACE_DAYS + 1)
        db.commit()
    ent = client.get("/api/v1/me/entitlements", headers=headers(s)).json()
    assert ent["membership"] == "free" and ent["max_active_tasks"] == 3 and ent["plan"] == "free"


def test_paid_features_are_gated_by_the_strategy(client):
    s = register(client)
    assert client.get("/api/v1/sync/status", headers=headers(s)).status_code == 402  # free: no sync
    client.post("/api/v1/billing/trial", headers=headers(s))
    assert client.get("/api/v1/sync/status", headers=headers(s)).status_code == 200  # paid: sync


def test_admin_emails_bootstrap_the_administrator_role(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAILS", {"ops@example.com"})
    s = register(client, email="ops@example.com")
    me = client.get("/api/v1/me", headers=headers(s)).json()
    assert me["role"] == "admin"
    ent = me["entitlements"]
    assert ent["membership"] == "admin" and ent["max_active_tasks"] is None and "admin" in ent["features"]
    assert "sync" in ent["features"]  # everything paid has
    # Admins are never demoted by the bootstrap list shrinking; a normal user is not promoted.
    monkeypatch.setattr(config, "ADMIN_EMAILS", set())
    other = register(client, email="user@example.com")
    assert client.get("/api/v1/me", headers=headers(other)).json()["role"] == "user"
    assert client.get("/api/v1/me", headers=headers(s)).json()["role"] == "admin"


@pytest.mark.parametrize("path", ["/api/v1/admin/stats", "/api/v1/admin/accounts", "/api/v1/admin/audit"])
def test_admin_api_needs_an_administrator_account(client, path):
    assert client.get(path).status_code == 401
    s = register(client)
    assert client.get(path, headers=headers(s)).status_code == 403
    # The pipeline token is for publishing releases only, not for looking at accounts.
    assert client.get(path, headers={"X-Admin-Token": "admin-token"}).status_code == 401


def test_release_publish_accepts_pipeline_token_or_admin_account(client, monkeypatch):
    manifest = {
        "version": "1.2.0",
        "schemaVersion": 2,
        "minSupportedVersion": "1.0.0",
        "minSupportedSchema": 1,
        "critical": True,
        "notes": "hotfix",
    }
    user = register(client, email="u@example.com")
    assert client.post("/api/v1/release/publish", json=manifest, headers=headers(user)).status_code == 403
    monkeypatch.setattr(config, "ADMIN_EMAILS", {"admin@example.com"})
    admin = register(client, email="admin@example.com")
    assert client.post("/api/v1/release/publish", json=manifest, headers=headers(admin)).status_code == 201
    assert client.get("/api/v1/release/manifest").json()["critical"] is True
    audit = client.get("/api/v1/admin/audit", headers=headers(admin)).json()
    assert audit[0]["action"] == "release.publish" and audit[0]["actor_email"] == "admin@example.com"
    assert json.dumps(audit[0]["detail"])  # JSON-serialisable metadata
