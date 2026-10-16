from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

import config
from models import Base

connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


if config.DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        # WAL lets reads proceed while a write is in progress (several devices hitting
        # the API at once); foreign keys are off by default in SQLite.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# Columns added after the first release. ``create_all`` never alters existing tables,
# so databases created by older versions get the new columns here. (Alembic is the
# long-term answer; this keeps upgrades painless while the schema is small.)
_ADDED_COLUMNS = {
    "tasks": {"completed_at": "DATETIME"},
}


def _ensure_columns() -> None:
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def init_db() -> None:
    database = engine.url.database
    if engine.url.drivername.startswith("sqlite") and database and database != ":memory:":
        # Make sure the directory for the SQLite file exists (e.g. backend/data/ or /app/data).
        from pathlib import Path

        Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _ensure_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
