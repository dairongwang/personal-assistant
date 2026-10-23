"""The hard requirement: the central database holds billing/account data and ciphertext only."""

import models

ALLOWED_TABLES = {
    "accounts",
    "devices",
    "subscriptions",
    "billing_events",
    "payments",  # the ledger: amount, status, refund and period per charge — money metadata, never card data
    "sync_ops",
    "sync_snapshots",
    "releases",
    "audit_log",  # admin actions: who changed which account's membership/role — metadata only
    "ad_campaigns",  # our creatives (docs/ADVERTISING.md) — nothing about users
    "ad_daily_stats",  # anonymous totals per ad × day × coarse region — no identifiers by construction
    "app_settings",  # operator settings changed from the admin panel (active ad source) — no user data
}
FORBIDDEN_COLUMN_WORDS = ("title", "description", "note", "body", "content", "location", "tag", "due", "task")
ALLOWED_COLUMN_NAMES = {"provider_event_id", "notes"}  # billing idempotency key; release notes (ours, not the user's)
# The sponsored-content tables must never grow a way to single out a person, a device or a request.
AD_TABLES = ("ad_campaigns", "ad_daily_stats")
AD_FORBIDDEN_COLUMN_WORDS = (
    "account",
    "device",
    "user",
    "ip",
    "agent",
    "session",
    "cookie",
    "fingerprint",
    "email",
    "token",
    "lat",
    "lon",
    "city",
    "postal",
    "zip",
)
AD_ALLOWED_COLUMN_NAMES = {"created_by"}  # the administrator who created the campaign (staff, not a user)


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


def test_ad_tables_cannot_identify_anyone():
    """Sponsored content is contextual: the daily counters are keyed by ad, day and coarse region
    only, and neither table has a column that could name a viewer, device or request."""
    for name in AD_TABLES:
        table = models.Base.metadata.tables[name]
        for column in table.columns:
            col = column.name.lower()
            if col in AD_ALLOWED_COLUMN_NAMES:
                continue
            assert not any(word in col for word in AD_FORBIDDEN_COLUMN_WORDS), f"{name}.{column.name}"
    stats = models.Base.metadata.tables["ad_daily_stats"]
    assert {c.name for c in stats.columns} == {"id", "ad_id", "day", "region", "impressions", "clicks"}
    assert stats.columns["region"].type.length == 2  # a country, never anything finer
    assert stats.columns["day"].type.python_type.__name__ == "date"  # a day, never a timestamp


def test_ad_endpoints_take_and_return_no_identifiers(client):
    """The public ad endpoints are anonymous by contract: no auth, and the request/response
    schemas carry no account, device or location-precise fields."""
    spec = client.get("/api/openapi.json").json()
    for path in ("/api/v1/ads/serve", "/api/v1/ads/metrics"):
        for op in spec["paths"][path].values():
            assert not op.get("security"), f"{path} must not require a token"
            names = {p["name"] for p in op.get("parameters", [])}
            assert names <= {"region", "lang"}, names
    schemas = spec["components"]["schemas"]
    for name in ("AdCreative", "AdServeResponse", "AdEventIn", "AdEventsRequest"):
        for prop in schemas[name]["properties"]:
            assert not any(w in prop for w in ("account", "device", "user", "ip", "lat", "lon", "city", "token")), (
                f"{name}.{prop}"
            )


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
