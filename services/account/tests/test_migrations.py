"""Schema management: Alembic owns the schema; databases created the old way are adopted."""

import sqlite3
import tempfile
from pathlib import Path

import sqlalchemy

import database


def _with_engine(url):
    original = database.engine
    database.engine = sqlalchemy.create_engine(url)
    return original


def test_fresh_database_is_created_by_migrations():
    db = Path(tempfile.mkdtemp()) / "fresh.db"
    original = _with_engine(f"sqlite:///{db.as_posix()}")
    try:
        assert database.migrate() == "schema is up to date"
        con = sqlite3.connect(db)
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        assert "accounts" in tables and "alembic_version" in tables
        assert con.execute("select version_num from alembic_version").fetchone()[0]
        assert database.migrate() == "schema is up to date"  # idempotent
    finally:
        database.engine = original


def test_database_created_with_create_all_is_adopted_not_broken():
    """Regression: `python dev.py run` (SQLite fallback) used to create tables with create_all;
    a following `alembic upgrade head` failed with 'table accounts already exists'."""
    from alembic import command

    db = Path(tempfile.mkdtemp()) / "legacy.db"
    original = _with_engine(f"sqlite:///{db.as_posix()}")
    try:
        # The old behaviour: the schema of the first release (what create_all() produced back
        # then) without an alembic_version table.
        command.upgrade(database._alembic_config(), database.INITIAL_REVISION)
        con = sqlite3.connect(db)
        con.execute("drop table alembic_version")
        con.commit()
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        assert "alembic_version" not in tables and "audit_log" not in tables
        con.execute(
            "insert into accounts (id, email, auth_salt, auth_hash, wrapped_data_key, kdf_params, vault_enabled, "
            "created_at, last_seq) values ('a', 'x@y.z', 's', 'h', 'w', '{}', 1, '2026-01-01', 0)"
        )
        con.commit()
        assert database.migrate() == "adopted an existing database and upgraded it"
        assert con.execute("select count(*) from accounts").fetchone()[0] == 1  # data kept
        assert con.execute("select role from accounts").fetchone()[0] == "user"  # later migrations applied
        assert con.execute("select version_num from alembic_version").fetchone()[0]
        assert database.migrate() == "schema is up to date"
    finally:
        database.engine = original
