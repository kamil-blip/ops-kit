"""Shared helpers for the sourcing demo: repo paths, DB connection, demo tags.

Everything the demo writes is tagged so it can be found and removed:
  people.tags contains DEMO_TAG, staging.submitted_by == DEMO_ACTOR,
  entities.source == DEMO_ACTOR, learnings.source == DEMO_ACTOR.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for sub in ("core", "comms", "learning", "hooks", "search", "tools"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import paths  # noqa: E402
import _db  # noqa: E402

DEMO_TAG = "demo:sourcing"
DEMO_ACTOR = "demo:sourcing"
HERE = Path(__file__).resolve().parent


def connect():
    """Open the kit database with its standard pragmas (WAL, busy timeout)."""
    return _db.connect(paths.DB_PATH)


def demo_people(conn):
    """All people rows seeded by this demo, oldest first."""
    return conn.execute(
        "SELECT * FROM people WHERE tags LIKE ? ORDER BY id", (f"%{DEMO_TAG}%",)
    ).fetchall()
