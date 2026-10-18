"""Fake accounts for testing: real accounts, one per membership situation, idempotent."""

import pytest
from conftest import headers

import clientcrypto
import database
import models
import seed

FAST = 1000  # PBKDF2 iterations for tests (the real seed uses the PWA's pinned 600k)


def _login(client, spec: seed.FakeAccount, iterations=FAST):
    auth_key, wrap_key = clientcrypto.derive_keys(spec.password, spec.email, iterations)
    r = client.post(
        "/api/v1/auth/login", json={"email": spec.email, "auth_key": auth_key, "device": {"platform": "test"}}
    )
    assert r.status_code == 200, (spec.email, r.text)
    return r.json(), wrap_key


def test_seed_creates_one_account_per_membership_and_they_can_sign_in(client):
    with database.SessionLocal() as db:
        result = seed.seed(db, iterations=FAST)
    assert all(created for _, _, created in result)
    assert {spec.membership for spec in seed.FAKE_ACCOUNTS} == {"free", "paid", "admin"}

    for spec in seed.FAKE_ACCOUNTS:
        session, wrap_key = _login(client, spec)
        # The device can unwrap the data key with the key derived from the password → sync works.
        assert len(clientcrypto.unwrap_data_key(session["wrapped_data_key"], wrap_key)) == 32
        assert session["kdf_params"]["iterations"] == FAST
        me = client.get("/api/v1/me", headers=headers(session)).json()
        assert me["entitlements"]["membership"] == spec.membership, spec.email
        assert me["role"] == spec.role
        if spec.membership == "free":
            assert me["entitlements"]["max_active_tasks"] == 3
            assert client.get("/api/v1/sync/status", headers=headers(session)).status_code == 402
        else:
            assert me["entitlements"]["max_active_tasks"] is None
            assert client.get("/api/v1/sync/status", headers=headers(session)).status_code == 200

    free = client.get("/api/v1/me/entitlements", headers=headers(_login(client, seed.FAKE_ACCOUNTS[0])[0])).json()
    assert free["trial_available"] is True
    expired = next(s for s in seed.FAKE_ACCOUNTS if s.email.startswith("expired"))
    ent = client.get("/api/v1/me/entitlements", headers=headers(_login(client, expired)[0])).json()
    assert ent["status"] == "canceled" and ent["trial_available"] is False and ent["plan"] == "free"


def test_seed_is_idempotent_and_resets_membership_state(client):
    with database.SessionLocal() as db:
        seed.seed(db, iterations=FAST)
        pro = db.query(models.Account).filter_by(email="pro@example.com").one()
        sub = db.get(models.Subscription, pro.id)
        sub.status = "canceled"  # somebody played with it
        db.commit()
        result = seed.seed(db, iterations=FAST)
        assert not any(created for _, _, created in result)
        db.refresh(sub)
        assert sub.status == "active" and db.query(models.Account).count() == len(seed.FAKE_ACCOUNTS)


def test_seed_refuses_prod_and_can_promote(client, monkeypatch, capsys):
    import config

    monkeypatch.setattr(config, "ENV", "prod")
    assert seed.main([]) == 2
    monkeypatch.setattr(config, "ENV", "test")
    with pytest.raises(SystemExit):
        seed.main(["--promote", "nobody@example.com"])
    with database.SessionLocal() as db:
        seed.seed(db, iterations=FAST)
    assert seed.main(["--promote", "free@example.com"]) == 0
    assert "free@example.com: role = admin" in capsys.readouterr().out
    with database.SessionLocal() as db:
        assert db.query(models.Account).filter_by(email="free@example.com").one().role == "admin"
    assert seed.main(["--demote", "free@example.com"]) == 0
    with database.SessionLocal() as db:
        assert db.query(models.Account).filter_by(email="free@example.com").one().role == "user"


def test_client_crypto_matches_the_pwa_key_derivation():
    """Known-answer test computed with WebCrypto (Node 22) for crypto.ts → deriveKeys().
    If this fails, seeded accounts can no longer sign in from the app."""
    auth_key, wrap_key = clientcrypto.derive_keys("correct horse battery", "Ann@Example.com ")
    assert auth_key == "c3cb2a5368f61af3b9ccea859de3f015476777b3f519ec199d72d6df8b837c8d"
    wrapped = "AAECAwQFBgcICQoLmdO6bwtNVRHUwO83j4WR7UxIRDPgaEajDSDE7bqP760WqKAmbJAcT2QMv4N1tOV/"
    assert clientcrypto.unwrap_data_key(wrapped, wrap_key) == bytes(range(32))
