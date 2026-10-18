"""Engine/session factory and schema management.

The schema is owned by Alembic (``alembic/``) for *every* database — PostgreSQL in real
environments, SQLite for tests and quick local hacking. ``migrate()`` brings a database to the
current revision and adopts databases that were created without Alembic (an earlier version
used ``create_all``): if tables exist but there is no ``alembic_version`` table, the database is
stamped with the initial revision first, then upgraded. It is safe to call repeatedly.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

import config

log = logging.getLogger("database")

SERVICE_DIR = Path(__file__).resolve().parent
INITIAL_REVISION = "0001_initial"  # the revision whose schema equals what create_all() used to produce

DATABASE_IS_SQLITE = config.DATABASE_URL.startswith("sqlite")
_connect_args = {"check_same_thread": False} if DATABASE_IS_SQLITE else {}
engine = create_engine(config.DATABASE_URL, connect_args=_connect_args, pool_pre_ping=not DATABASE_IS_SQLITE)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

if DATABASE_IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _alembic_config():
    from alembic.config import Config

    cfg = Config()  # no ini file → alembic does not reconfigure logging from inside the app
    cfg.set_main_option("script_location", str(SERVICE_DIR / "alembic"))
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%"))
    return cfg


def migrate() -> str:
    """Upgrade the database to the current schema. Returns a short description of what happened."""
    from alembic import command

    cfg = _alembic_config()
    tables = set(inspect(engine).get_table_names())
    adopted = False
    if tables and "alembic_version" not in tables:
        # Created by create_all() in an earlier version → adopt it as the initial revision.
        command.stamp(cfg, INITIAL_REVISION)
        adopted = True
    command.upgrade(cfg, "head")
    result = "adopted an existing database and upgraded it" if adopted else "schema is up to date"
    log.info("Database %s: %s", engine.url.render_as_string(hide_password=True), result)
    return result


def init_db() -> None:
    """Called at application startup."""
    if DATABASE_IS_SQLITE:
        database = engine.url.database
        if database and database != ":memory:":
            Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    if DATABASE_IS_SQLITE or config.AUTO_MIGRATE:
        # SQLite (single process) always self-migrates. For PostgreSQL the deploy pipeline /
        # container start command runs `alembic upgrade head` before the app starts; set
        # AUTO_MIGRATE=true to do it here instead (single-replica setups).
        migrate()


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


if __name__ == "__main__":  # python database.py  → migrate whatever DATABASE_URL points at
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(migrate())
