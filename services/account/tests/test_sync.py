"""Relay semantics: server-assigned ordering, idempotency, ack-based pruning, snapshots, gating."""

import base64

from conftest import cipher, go_pro, headers, login, register


def push(client, session, ops, **hdr):
    return client.post("/api/v1/sync/push", json={"ops": ops}, headers=headers(session, **hdr))


def test_sync_requires_pro(client):
    s = register(client)
    r = push(client, s, [{"op_id": "op-1", "schema_version": 1, "ciphertext": cipher("a")}])
    assert r.status_code == 402 and "Pro" in r.json()["detail"]


def test_server_assigns_global_order_across_devices(client, pro):
    phone = login(client)
    go = push(client, pro, [{"op_id": "d1-1", "schema_version": 1, "ciphertext": cipher("desktop 1")}]).json()
    assert [a["seq"] for a in go["accepted"]] == [1]
    go = push(
        client,
        phone,
        [
            {"op_id": "p1-1", "schema_version": 1, "ciphertext": cipher("phone 1")},
            {"op_id": "p1-2", "schema_version": 1, "ciphertext": cipher("phone 2")},
        ],
    ).json()
    assert [a["seq"] for a in go["accepted"]] == [2, 3] and go["last_seq"] == 3

    # Desktop pulls everything after its cursor; it skips its own op (seq 1) client-side.
    pulled = client.get("/api/v1/sync/pull?since=0", headers=headers(pro)).json()
    assert [o["seq"] for o in pulled["ops"]] == [1, 2, 3]
    assert pulled["ops"][0]["device_id"] == pro["device_id"]
    pulled["ops"] = [o for o in pulled["ops"] if o["device_id"] != pro["device_id"]]
    assert pulled["ops"][0]["device_id"] == phone["device_id"]
    assert base64.b64decode(pulled["ops"][0]["ciphertext"]).endswith(b"phone 1")
    assert pulled["has_more"] is False and pulled["last_seq"] == 3

    # Phone pulls the desktop's op.
    pulled = client.get("/api/v1/sync/pull?since=0", headers=headers(phone)).json()
    assert [o["seq"] for o in pulled["ops"]] == [1, 2, 3]


def test_push_is_idempotent(client, pro):
    first = push(client, pro, [{"op_id": "same", "schema_version": 1, "ciphertext": cipher("x")}]).json()
    again = push(client, pro, [{"op_id": "same", "schema_version": 1, "ciphertext": cipher("x")}]).json()
    assert first["accepted"] == again["accepted"] and again["last_seq"] == 1


def test_pull_pagination(client, pro):
    push(client, pro, [{"op_id": f"op-{i}", "schema_version": 1, "ciphertext": cipher(str(i))} for i in range(5)])
    page = client.get("/api/v1/sync/pull?since=0&limit=2", headers=headers(pro)).json()
    assert [o["seq"] for o in page["ops"]] == [1, 2] and page["has_more"] is True
    page = client.get("/api/v1/sync/pull?since=4&limit=2", headers=headers(pro)).json()
    assert [o["seq"] for o in page["ops"]] == [5] and page["has_more"] is False


def test_push_does_not_skip_other_devices_ops(client, pro):
    """Regression: a device pushing must not advance past ops it has not pulled yet."""
    phone = login(client)
    push(client, phone, [{"op_id": "p-1", "schema_version": 1, "ciphertext": cipher("phone")}])  # seq 1
    push(client, pro, [{"op_id": "d-1", "schema_version": 1, "ciphertext": cipher("desktop")}])  # seq 2
    status = client.get("/api/v1/sync/status", headers=headers(pro)).json()
    assert status["cursor"] == 0 and status["pending_for_device"] == 2
    pulled = client.get("/api/v1/sync/pull?since=0", headers=headers(pro)).json()
    assert [o["seq"] for o in pulled["ops"]] == [1, 2]


def test_ack_prunes_ops_every_active_device_has_applied(client, pro):
    phone = login(client)
    push(
        client,
        pro,
        [
            {"op_id": "a", "schema_version": 1, "ciphertext": cipher("a")},
            {"op_id": "b", "schema_version": 1, "ciphertext": cipher("b")},
        ],
    )
    status = client.post("/api/v1/sync/ack", json={"cursor": 2}, headers=headers(pro)).json()
    assert status["pending_for_device"] == 0 and status["active_devices"] == 2
    # Phone has not acked → ops stay.
    assert len(client.get("/api/v1/sync/pull?since=0", headers=headers(phone)).json()["ops"]) == 2
    client.post("/api/v1/sync/ack", json={"cursor": 2}, headers=headers(phone))
    import database
    import models

    with database.SessionLocal() as db:
        assert db.query(models.SyncOp).count() == 0  # relay emptied: nothing lingers on the server
    # Ordering continues from the account's last_seq even though the rows are gone.
    nxt = push(client, pro, [{"op_id": "c", "schema_version": 1, "ciphertext": cipher("c")}]).json()
    assert nxt["accepted"][0]["seq"] == 3


def test_inactive_devices_do_not_hold_ops_back(client, pro):
    from datetime import timedelta

    import database
    import models

    phone = login(client)
    push(client, pro, [{"op_id": "a", "schema_version": 1, "ciphertext": cipher("a")}])
    with database.SessionLocal() as db:
        dev = db.get(models.Device, phone["device_id"])
        dev.last_seen_at = models.utcnow() - timedelta(days=40)
        db.commit()
    client.post("/api/v1/sync/ack", json={"cursor": 1}, headers=headers(pro))
    with database.SessionLocal() as db:
        assert db.query(models.SyncOp).count() == 0


def test_expired_ops_are_pruned(client, pro):
    from datetime import timedelta

    import database
    import models

    push(client, pro, [{"op_id": "old", "schema_version": 1, "ciphertext": cipher("old")}])
    with database.SessionLocal() as db:
        db.query(models.SyncOp).update({"expires_at": models.utcnow() - timedelta(days=1)})
        db.commit()
    client.post("/api/v1/sync/ack", json={"cursor": 0}, headers=headers(pro))
    with database.SessionLocal() as db:
        assert db.query(models.SyncOp).count() == 0


def test_payload_validation(client, pro):
    r = push(client, pro, [{"op_id": "bad", "schema_version": 1, "ciphertext": "not base64 at all!!"}])
    assert r.status_code == 422 and "base64" in r.json()["detail"]
    huge = base64.b64encode(b"x" * (64 * 1024 + 1)).decode()
    r = push(client, pro, [{"op_id": "huge", "schema_version": 1, "ciphertext": huge}])
    assert r.status_code == 413
    r = client.post("/api/v1/sync/ack", json={"cursor": 99}, headers=headers(pro))
    assert r.status_code == 422


def test_snapshot_roundtrip_and_vault_toggle(client, pro):
    phone = login(client)
    push(client, pro, [{"op_id": "a", "schema_version": 1, "ciphertext": cipher("a")}])
    r = client.put(
        "/api/v1/sync/snapshot",
        json={"seq": 1, "schema_version": 1, "ciphertext": cipher("FULL STATE")},
        headers=headers(pro),
    )
    assert r.status_code == 200 and r.json()["seq"] == 1
    # A brand-new device rebuilds from the snapshot, then applies ops after it.
    got = client.get("/api/v1/sync/snapshot", headers=headers(phone)).json()
    assert base64.b64decode(got["ciphertext"]).endswith(b"FULL STATE")
    assert client.get("/api/v1/sync/pull?since=0", headers=headers(phone)).json()["snapshot_seq"] == 1
    # Older snapshot cannot overwrite a newer one.
    r = client.put(
        "/api/v1/sync/snapshot",
        json={"seq": 0, "schema_version": 1, "ciphertext": cipher("stale")},
        headers=headers(phone),
    )
    assert r.status_code == 409
    # Turning the vault off deletes the snapshot.
    me = client.patch("/api/v1/me/settings", json={"vault_enabled": False}, headers=headers(pro)).json()
    assert me["vault_enabled"] is False
    assert client.get("/api/v1/sync/snapshot", headers=headers(pro)).status_code == 404
    r = client.put(
        "/api/v1/sync/snapshot", json={"seq": 1, "schema_version": 1, "ciphertext": cipher("x")}, headers=headers(pro)
    )
    assert r.status_code == 409


def test_stale_clients_get_426_upgrade_required(client, pro):
    admin = {"X-Admin-Token": "admin-token"}
    client.post(
        "/api/v1/release/publish",
        json={
            "version": "2.0.0",
            "schema_version": 3,
            "min_supported_version": "1.5.0",
            "min_supported_schema": 2,
            "critical": False,
        },
        headers=admin,
    )
    r = push(client, pro, [{"op_id": "a", "schema_version": 1, "ciphertext": cipher("a")}], version="1.0.0", schema=1)
    assert r.status_code == 426
    assert "update the app" in r.json()["detail"].lower()
    assert r.headers["X-Min-Supported-Version"] == "1.5.0"
    # Outdated data format alone is also blocked (it would push ops nobody could migrate back).
    r = push(client, pro, [{"op_id": "a", "schema_version": 1, "ciphertext": cipher("a")}], version="2.0.0", schema=1)
    assert r.status_code == 426 and "data format" in r.json()["detail"]
    # A current client is fine and its version is recorded on the device row.
    r = push(client, pro, [{"op_id": "a", "schema_version": 3, "ciphertext": cipher("a")}], version="2.0.0", schema=3)
    assert r.status_code == 200
    me = client.get("/api/v1/me", headers=headers(pro, version="2.0.0", schema=3)).json()
    assert me["devices"][0]["client_version"] == "2.0.0" and me["devices"][0]["schema_version"] == 3


def test_accounts_are_isolated(client, pro):
    other = register(client, email="bob@example.com")
    go_pro(client, other)
    push(client, pro, [{"op_id": "ann", "schema_version": 1, "ciphertext": cipher("ann")}])
    assert client.get("/api/v1/sync/pull?since=0", headers=headers(other)).json()["ops"] == []
    assert client.get("/api/v1/sync/snapshot", headers=headers(other)).status_code == 404
    assert client.delete(f"/api/v1/me/devices/{pro['device_id']}", headers=headers(other)).status_code == 404
