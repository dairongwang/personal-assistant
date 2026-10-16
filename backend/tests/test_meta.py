import json


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and "version" in body
    assert body["timestamp"].endswith("+00:00")


def test_frontend_is_served_from_same_origin(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in r.text
    # No third-party CDN dependencies: the app must work on a LAN without internet.
    assert "cdn." not in r.text and "unpkg.com" not in r.text and "googleapis" not in r.text


def test_pwa_assets(client, frontend_dir):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    data = json.loads(manifest.text)
    assert data["display"] == "standalone" and data["start_url"] == "/"
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon
    assert client.get("/sw.js").status_code == 200
    assert client.get("/css/app.css").status_code == 200
    assert client.get("/js/app.js").status_code == 200


def test_api_routes_take_precedence_over_static(client):
    assert client.get("/tasks").headers["content-type"].startswith("application/json")
    assert client.get("/does-not-exist").status_code == 404


def test_old_database_gets_new_columns():
    """A database created by v0.1 (no completed_at column) must be upgraded on startup."""
    import sqlite3
    import tempfile
    from pathlib import Path

    import sqlalchemy

    import database

    old_db = Path(tempfile.mkdtemp()) / "old.db"
    con = sqlite3.connect(old_db)
    con.execute(
        "CREATE TABLE tasks (id INTEGER NOT NULL, title VARCHAR NOT NULL, description TEXT, priority VARCHAR, "
        "status VARCHAR, due_date DATETIME, tags JSON, created_at DATETIME, updated_at DATETIME, PRIMARY KEY (id))"
    )
    con.execute("INSERT INTO tasks (title, status) VALUES ('legacy', 'todo')")
    con.commit()
    con.close()

    original_engine = database.engine
    database.engine = sqlalchemy.create_engine(f"sqlite:///{old_db.as_posix()}")
    try:
        database.init_db()
        con = sqlite3.connect(old_db)
        cols = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
        assert "completed_at" in cols
        assert con.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        con.close()
    finally:
        database.engine = original_engine
