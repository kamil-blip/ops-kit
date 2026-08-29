"""CDC cursor accessors for the Context Steward.

cdc_log is fed by AFTER INSERT/UPDATE triggers on the core canonical tables.
The steward reads rows past its externally-stored cursor to (a) detect that any
canonical write happened (change-detect) and (b) find unattributed writes
(actor IS NULL) that bypassed the bus.
"""
from __future__ import annotations
import sqlite3


def get_cursor(conn: sqlite3.Connection, consumer: str = "steward") -> int:
    r = conn.execute("SELECT last_cdc_id FROM cdc_cursor WHERE consumer=?", (consumer,)).fetchone()
    return int(r[0]) if r else 0


def head(conn: sqlite3.Connection) -> int:
    r = conn.execute("SELECT COALESCE(MAX(id),0) FROM cdc_log").fetchone()
    return int(r[0])


def count_new(conn: sqlite3.Connection, consumer: str = "steward") -> int:
    cur = get_cursor(conn, consumer)
    return int(conn.execute("SELECT COUNT(*) FROM cdc_log WHERE id>?", (cur,)).fetchone()[0])


def new_changes(conn: sqlite3.Connection, consumer: str = "steward", limit: int = 500) -> list[tuple]:
    cur = get_cursor(conn, consumer)
    return conn.execute(
        "SELECT id,table_name,row_id,op,actor,changed_at FROM cdc_log WHERE id>? ORDER BY id LIMIT ?",
        (cur, limit)).fetchall()


def unattributed_since(conn: sqlite3.Connection, consumer: str = "steward") -> int:
    cur = get_cursor(conn, consumer)
    return int(conn.execute(
        "SELECT COUNT(*) FROM cdc_log WHERE id>? AND actor IS NULL", (cur,)).fetchone()[0])


def advance_cursor(conn: sqlite3.Connection, consumer: str = "steward", to_id: int | None = None) -> int:
    if to_id is None:
        to_id = head(conn)
    conn.execute(
        "INSERT INTO cdc_cursor(consumer,last_cdc_id,updated_at) VALUES(?,?,datetime('now')) "
        "ON CONFLICT(consumer) DO UPDATE SET last_cdc_id=excluded.last_cdc_id, updated_at=excluded.updated_at",
        (consumer, to_id))
    conn.commit()
    return to_id


if __name__ == "__main__":
    from _db import connect
    c = connect()
    print("cursor:", get_cursor(c), "head:", head(c), "new:", count_new(c), "unattributed:", unattributed_since(c))
