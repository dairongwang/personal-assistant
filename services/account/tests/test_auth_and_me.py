from conftest import headers, login, register


def test_register_login_and_me(client):
    s = register(client)
    assert s["token"] and s["device_id"] and s["wrapped_data_key"]
    me = client.get("/api/v1/me", headers=headers(s)).json()
    assert me["email"] == "ann@example.com"
    assert me["entitlements"]["plan"] == "free" and me["entitlements"]["trial_available"] is True
    assert len(me["devices"]) == 1 and me["devices"][0]["is_current"]

    s2 = login(client)
    assert s2["wrapped_data_key"] == s["wrapped_data_key"]  # second device can unwrap the data key
    assert s2["kdf_params"] == {"kdf": "pbkdf2", "iterations": 1000}
    me = client.get("/api/v1/me", headers=headers(s2)).json()
    assert len(me["devices"]) == 2


def test_wrong_password_and_duplicate_email(client):
    register(client)
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "ann@example.com", "auth_key": "0" * 64, "device": {}},
    )
    assert r.status_code == 401 and "incorrect" in r.json()["detail"]
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "Ann@Example.com", "auth_key": "1" * 64, "wrapped_data_key": "x" * 32},
    )
    assert r.status_code == 409


def test_password_never_reaches_the_server(client):
    """The API contract accepts a derived auth key only; a raw short password is rejected."""
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "x@example.com", "auth_key": "hunter2", "wrapped_data_key": "x" * 32},
    )
    assert r.status_code == 422


def test_logout_revokes_device_and_logout_everywhere(client):
    s1 = register(client)
    s2 = login(client)
    assert client.post("/api/v1/auth/logout", headers=headers(s1)).status_code == 204
    assert client.get("/api/v1/me", headers=headers(s1)).status_code == 401
    assert client.get("/api/v1/me", headers=headers(s2)).status_code == 200
    assert client.post("/api/v1/auth/logout-everywhere", headers=headers(s2)).status_code == 204
    assert client.get("/api/v1/me", headers=headers(s2)).status_code == 401


def test_revoke_other_device(client):
    s1 = register(client)
    s2 = login(client)
    assert client.delete(f"/api/v1/me/devices/{s2['device_id']}", headers=headers(s1)).status_code == 204
    assert client.get("/api/v1/me", headers=headers(s2)).status_code == 401


def test_tampered_or_missing_token(client):
    s = register(client)
    assert client.get("/api/v1/me").status_code == 401
    bad = {"Authorization": f"Bearer {s['token'][:-3]}abc"}
    assert client.get("/api/v1/me", headers=bad).status_code == 401


def test_delete_account_purges_everything(client, pro):
    import base64

    h = headers(pro)
    client.post(
        "/api/v1/sync/push",
        json={"ops": [{"op_id": "op-1", "schema_version": 1, "ciphertext": base64.b64encode(b"x" * 20).decode()}]},
        headers=h,
    )
    assert client.delete("/api/v1/me", headers=h).status_code == 204
    assert client.get("/api/v1/me", headers=h).status_code == 401
    r = client.post("/api/v1/auth/login", json={"email": "ann@example.com", "auth_key": "1" * 64, "device": {}})
    assert r.status_code == 401
    # Tombstone keeps no e-mail, key material or relay data.
    import database
    import models

    with database.SessionLocal() as db:
        acc = db.query(models.Account).one()
        assert acc.deleted_at and acc.email.startswith("deleted-") and acc.wrapped_data_key == ""
        assert db.query(models.SyncOp).count() == 0 and db.query(models.Device).count() == 0
        assert db.query(models.Subscription).count() == 1  # billing tombstone stays
