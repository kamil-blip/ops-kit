"""Helper for populating _audit_context before mutating audited canonical tables.

The audit-log triggers read main._audit_context to learn who is responsible
for a change. _audit_context is a singleton row keyed id=1; callers update it
before any UPDATE/INSERT on an audited table, and the trigger reads it when
firing.

SQLite doesn't allow triggers to reference temp-schema tables, so this is
main-schema. That means the row is global - writers in different processes
would step on each other. In practice the background worker is single-
threaded and interactive CLIs don't run concurrently with it.

EMPIRICAL NOTE: a proposed "move _audit_context to a per-connection TEMP
table" change was tested against the live CDC triggers and REFUTED. Main-
schema triggers hard-bind the unqualified `_audit_context` name to main at
fire time: a populated temp._audit_context is silently ignored (the trigger
still reads main), and dropping main._audit_context makes every canonical
write raise `no such table: main._audit_context`. So the table MUST stay
main-schema. The cross-connection race is also a non-issue in the current
architecture: the actor_scope callers are the canonical-write bus (which sets
the actor INSIDE its per-row SAVEPOINT, holding the write lock across
set->write->clear) and other single-threaded steward-process writers; the
draft monitor is a separate process but writes only drafts (no CDC triggers).
A live rollback probe confirmed raw writes attribute to NULL and scoped
writes attribute to their actor, so the NULL-actor gate metric is a clean
signal of un-migrated raw writers, not corruption.

Usage:

    from audit_actor import actor_scope

    with actor_scope(conn, "cascade:on_new_inbound", source_ref="email:abc123"):
        conn.execute("UPDATE people SET status='active' WHERE id=?", (pid,))
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

try:
    import paths
    import _db  # unified connector (busy_timeout + FK ON)
except ImportError:
    # Flat-import fallback: paths.py and _db.py sit next to this file in core/.
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import paths
    import _db


def set_actor(conn: sqlite3.Connection, actor: str, source_ref: str | None = None) -> None:
    # Upsert: a fresh install ships _audit_context empty (zero rows by design),
    # and a plain UPDATE on the missing singleton would silently attribute
    # nothing. The row is created on first use and kept from then on.
    conn.execute(
        "INSERT INTO _audit_context (id, actor, source_ref, set_at) VALUES (1, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(id) DO UPDATE SET actor = excluded.actor, source_ref = excluded.source_ref, "
        "set_at = CURRENT_TIMESTAMP",
        (actor, source_ref),
    )


def clear_actor(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE _audit_context SET actor = NULL, source_ref = NULL, set_at = NULL WHERE id = 1"
    )


def record_gate_rejection(exc: Exception, table: str, op: str,
                          source_ref: str | None = None) -> None:
    """Shared except-IntegrityError hook: call from any writer's
    `except sqlite3.IntegrityError` when the message starts with 'write_gate('
    so gate rejections become observable bus_events instead of silent deaths.

    Uses its OWN short-lived connection (the caller's txn is typically about
    to roll back) and never raises -- telemetry must not break the writer.
    daily_digest surfaces the trailing-24h count.
    """
    if "write_gate" not in str(exc):
        return
    try:
        import json
        c = _db.connect(str(paths.DB_PATH), timeout=15)
        c.execute("PRAGMA busy_timeout=15000")
        c.execute(
            "INSERT INTO bus_events (session_id, event_type, summary, details_json, ts) "
            "VALUES ('gate', 'gate_rejected', ?, ?, datetime('now'))",
            (f"{op} on {table} rejected: {exc}",
             json.dumps({"table": table, "op": op, "error": str(exc),
                         "source_ref": source_ref})),
        )
        c.commit()
        c.close()
    except Exception:
        pass


@contextmanager
def actor_scope(conn: sqlite3.Connection, actor: str, source_ref: str | None = None) -> Iterator[None]:
    """Set the actor for the scope, restoring the PREVIOUS actor on exit.

    Save/restore instead of clear-to-NULL so scopes nest: a bus write inside a
    worker-stamped dispatch restores the worker's actor instead of wiping
    attribution for the rest of the dispatch. When the outer state is the
    cleared singleton (actor NULL), restore is byte-identical to the old
    clear_actor behavior.
    """
    prev = conn.execute(
        "SELECT actor, source_ref FROM _audit_context WHERE id = 1"
    ).fetchone()
    set_actor(conn, actor, source_ref)
    try:
        yield
    finally:
        if prev and (prev[0] is not None or prev[1] is not None):
            conn.execute(
                "UPDATE _audit_context SET actor = ?, source_ref = ?, set_at = CURRENT_TIMESTAMP WHERE id = 1",
                (prev[0], prev[1]),
            )
        else:
            clear_actor(conn)
