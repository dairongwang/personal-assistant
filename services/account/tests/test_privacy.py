"""The hard requirement: the central database holds billing/account data and ciphertext only."""

import models

ALLOWED_TABLES = {
    "accounts",
    "devices",
    "subscriptions",
    "billing_events",
    "sync_ops",
    "sync_snapshots",
    "releases",
    "audit_log",  # admin actions: who changed which account's membership/role — metadata only
}
FORBIDDEN_COLUMN_WORDS = ("title", "description", "note", "body", "content", "location", "tag", "due", "task")
ALLOWED_COLUMN_NAMES = {"provider_event_id", "notes"}  # billing idempotency key; release notes (ours, not the user's)


def test_no_content_tables_or_columns():
    tables = set(models.Base.metadata.tables)
    assert tables == ALLOWED_TABLES, f"unexpected tables: {tables - ALLOWED_TABLES}"
    for table in models.Base.metadata.tables.values():
        for column in table.columns:
            name = column.name.lower()
            if name in ALLOWED_COLUMN_NAMES:
                continue
            assert not any(word in name for word in FORBIDDEN_COLUMN_WORDS), (
                f"{table.name}.{column.name} looks like content"
            )
        if table.name in ("sync_ops", "sync_snapshots"):
            assert table.columns["ciphertext"].type.python_type is bytes


def test_api_has_no_content_endpoints(client):
    paths = client.get("/api/openapi.json").json()["paths"]
    for path in paths:
        assert not any(word in path for word in ("/tasks", "/events", "/notes", "/today")), path


def test_admin_api_never_returns_ciphertext(client):
    """Administrators see metadata (e-mails, plans, device versions, byte counts) — never the
    relayed blobs, not even encrypted ones."""
    spec = client.get("/api/openapi.json").json()
    schemas = spec["components"]["schemas"]

    def fields(name: str, seen: set[str]) -> set[str]:
        out: set[str] = set()
        if name in seen:
            return out
        seen.add(name)
        for prop, schema in schemas[name].get("properties", {}).items():
            out.add(prop)
            for ref in _refs(schema):
                out |= fields(ref, seen)
        return out

    for path, ops in spec["paths"].items():
        if not path.startswith("/api/v1/admin/"):
            continue
        for op in ops.values():
            for ref in _refs(op.get("responses", {}).get("200", {}).get("content", {})):
                exposed = {f for f in fields(ref, set()) if "ciphertext" in f}
                assert not exposed, f"{path} exposes {exposed}"


def _refs(node) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        if "$ref" in node:
            found.append(node["$ref"].rsplit("/", 1)[-1])
        for value in node.values():
            found += _refs(value)
    elif isinstance(node, list):
        for value in node:
            found += _refs(value)
    return found


def test_relay_stores_exactly_the_bytes_it_received(client, pro):
    import base64

    from conftest import headers

    import database

    blob = bytes(range(256)) * 4
    client.post(
        "/api/v1/sync/push",
        json={"ops": [{"op_id": "bin", "schema_version": 1, "ciphertext": base64.b64encode(blob).decode()}]},
        headers=headers(pro),
    )
    with database.SessionLocal() as db:
        row = db.query(models.SyncOp).one()
        assert row.ciphertext == blob and row.size_bytes == len(blob)
