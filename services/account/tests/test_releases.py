from versioning import compare, is_valid

ADMIN = {"X-Admin-Token": "admin-token"}


def test_version_compare():
    assert compare("1.2.3", "1.2.3") == 0
    assert compare("1.10.0", "1.9.9") > 0
    assert compare("1.0.0-beta.1", "1.0.0") < 0
    assert compare("v2.0.0", "2.0.0") == 0
    assert is_valid("1.2.3") and not is_valid("latest")


def test_manifest_publish_check_history(client):
    assert client.get("/api/v1/release/manifest").json() is None  # nothing published, no fallback file in tests
    r = client.post(
        "/api/v1/release/publish",
        json={"version": "1.0.0", "schema_version": 1, "min_supported_version": "1.0.0", "min_supported_schema": 1},
    )
    assert r.status_code == 403  # admin token required
    r = client.post(
        "/api/v1/release/publish",
        json={
            "version": "1.1.0",
            "schema_version": 2,
            "min_supported_version": "1.0.0",
            "min_supported_schema": 1,
            "notes": "Tags on tasks",
        },
        headers=ADMIN,
    )
    assert r.status_code == 201 and r.json()["version"] == "1.1.0" and r.json()["published_at"]

    check = client.get("/api/v1/release/check?version=1.0.0&schema=1").json()
    assert check["update_available"] is True and check["update_required"] is False
    check = client.get("/api/v1/release/check?version=1.1.0&schema=2").json()
    assert check["update_available"] is False and check["update_required"] is False
    check = client.get("/api/v1/release/check?version=0.9.0&schema=1").json()
    assert check["update_required"] is True and "no longer supported" in check["reason"]

    # Critical release: everybody on an older version must update now.
    client.post(
        "/api/v1/release/publish",
        json={
            "version": "1.1.1",
            "schema_version": 2,
            "min_supported_version": "1.0.0",
            "min_supported_schema": 1,
            "critical": True,
            "notes": "Security fix",
        },
        headers=ADMIN,
    )
    check = client.get("/api/v1/release/check?version=1.1.0&schema=2").json()
    assert check["update_required"] is True and "required update" in check["reason"]
    assert client.get("/api/v1/release/manifest").json()["version"] == "1.1.1"
    history = client.get("/api/v1/release/history").json()
    assert [h["version"] for h in history] == ["1.1.1", "1.1.0"]


def test_manifest_validation(client):
    bad = {"version": "1.0.0", "schema_version": 1, "min_supported_version": "2.0.0", "min_supported_schema": 1}
    assert client.post("/api/v1/release/publish", json=bad, headers=ADMIN).status_code == 422
    bad = {"version": "latest", "schema_version": 1, "min_supported_version": "1.0.0", "min_supported_schema": 1}
    assert client.post("/api/v1/release/publish", json=bad, headers=ADMIN).status_code == 422


def test_health(client):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok" and body["database"] in ("sqlite", "postgresql")
