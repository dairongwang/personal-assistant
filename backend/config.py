"""Runtime configuration from environment variables (with sane local defaults).

Only the standard library is used so this works identically on Windows, macOS,
Linux and inside Docker. A ``.env`` file next to this module is loaded if present
(simple KEY=VALUE lines; existing environment variables take precedence).
"""

from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BACKEND_DIR / ".env")

APP_VERSION = "0.2.0"

# Where SQLite lives. Docker mounts a volume at /app/data; locally it is backend/data/.
DATA_DIR = Path(os.environ.get("DATA_DIR", BACKEND_DIR / "data")).resolve()
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'assistant.db').as_posix()}")

# IANA timezone used for "today" when a client does not say which timezone it is in.
DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "UTC")

# Comma-separated origins. Only needed if the frontend is served from another origin;
# the backend serves the frontend itself, so the default is "no CORS".
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]

FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", PROJECT_DIR / "frontend")).resolve()
