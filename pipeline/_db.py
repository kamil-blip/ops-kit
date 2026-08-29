"""Database connection for the pipeline modules.

The database path is `SOURCING_DB` if set, otherwise `data/sourcing.db` next to
the repository root. Every connection turns foreign keys on and uses Row access.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("SOURCING_DB", ROOT / "data" / "sourcing.db"))
SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def days_since(iso: str | None) -> int | None:
    """Whole days between an ISO timestamp and now; None when the value is empty."""
    if not iso:
        return None
    from datetime import datetime, timezone
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - t).total_seconds() // 86400)
