"""Test fixtures.

The app reads its configuration from environment variables at import time, so we point
DATABASE_URL at a temporary file *before* importing it. Every test gets a fresh database.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

_TMP = tempfile.mkdtemp(prefix="pa-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP, 'test.db').as_posix()}"
os.environ["DEFAULT_TZ"] = "UTC"
os.environ.pop("CORS_ORIGINS", None)

import config  # noqa: E402
import database  # noqa: E402
import main  # noqa: E402
import models  # noqa: E402


@pytest.fixture()
def client():
    models.Base.metadata.drop_all(bind=database.engine)
    with TestClient(main.app) as c:  # runs lifespan -> init_db()
        yield c


@pytest.fixture()
def frontend_dir():
    return config.FRONTEND_DIR


def reload_config_with(**env):
    """Helper for tests that need different settings (re-imports config)."""
    for k, v in env.items():
        os.environ[k] = v
    return importlib.reload(config)
