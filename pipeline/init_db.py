"""Create the pipeline database from schema.sql and seed the transition table.

Usage: python pipeline/init_db.py [--db PATH]
Prints one line per step and ends with the table count. Safe to re-run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _db  # noqa: E402
import states  # noqa: E402


def init(db_path=None) -> int:
    conn = _db.connect(db_path)
    conn.executescript(_db.SCHEMA.read_text(encoding="utf-8"))
    conn.executemany("INSERT OR IGNORE INTO role_transitions (from_status, to_status) VALUES (?, ?)",
                     states.transition_rows())
    conn.commit()
    n_tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    n_edges = conn.execute("SELECT COUNT(*) FROM role_transitions").fetchone()[0]
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    print(f"database: {db_path or _db.DB_PATH}")
    print(f"tables={n_tables} transitions={n_edges} integrity={ok}")
    return 0 if ok == "ok" else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="database path (default: $SOURCING_DB or data/sourcing.db)")
    a = ap.parse_args()
    sys.exit(init(a.db))
