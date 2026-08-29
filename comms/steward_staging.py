"""Staging API — the only canonical-write entrypoint for non-steward code.

A writer calls `submit()` (async: stage, the steward drains later) or
`steward_bus.write()` (sync: stage + publish-now in the same call). Either way
the write goes through the single gateway: validate -> resolve identity ->
idempotent upsert on the natural key -> stamp provenance -> mark promoted/rejected.

Idempotency: a re-submission with the same (target_table, natural_key, payload)
collapses to the existing staging row (INSERT OR IGNORE on idempotency_key).
"""
from __future__ import annotations
import hashlib, json, sqlite3
from typing import Optional


BUSY_TIMEOUT_FLOOR_MS = 30_000


def _ensure_busy_timeout(conn: sqlite3.Connection) -> None:
    """The staging layer must not trust callers to set busy_timeout (submit()
    raised database-is-locked until the caller set it — enforce the floor here)."""
    try:
        cur = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        if cur is None or int(cur) < BUSY_TIMEOUT_FLOOR_MS:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_FLOOR_MS}")
    except sqlite3.Error:
        pass


def _max_attempts(conn: sqlite3.Connection) -> int:
    r = conn.execute("SELECT value FROM steward_config WHERE key='max_publish_attempts'").fetchone()
    return int(r[0]) if r else 3


def idempotency_key(target_table: str, natural_key, payload) -> str:
    raw = "|".join([
        target_table,
        json.dumps(natural_key, sort_keys=True, default=str) if natural_key is not None else "",
        json.dumps(payload, sort_keys=True, default=str),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def submit(
    conn: sqlite3.Connection,
    *,
    target_table: str,
    payload: dict,
    submitted_by: str,
    natural_key: Optional[dict] = None,
    op: str = "upsert",
    source_table: Optional[str] = None,
    source_id: Optional[str] = None,
    source_quote: Optional[str] = None,
    trace_json: Optional[str] = None,
) -> dict:
    """Stage a canonical write. Returns {'staging_id', 'idempotency_key', 'dedup': bool}."""
    _ensure_busy_timeout(conn)
    idem = idempotency_key(target_table, natural_key, payload)
    existing = conn.execute(
        "SELECT id,status,attempts FROM staging WHERE idempotency_key=?", (idem,)).fetchone()
    if existing:
        eid, estatus, eattempts = existing
        if estatus == "promoted":
            return {"staging_id": eid, "idempotency_key": idem, "dedup": True, "status": "promoted"}
        # Non-promoted: enrich provenance, and re-arm a fixable row so a corrected
        # re-submit retries (bulletproof: rejected != permanently blocked). Poison-pill
        # cap: don't re-arm a row past max_publish_attempts (terminal quarantine).
        cap = _max_attempts(conn)
        rearm = (eattempts or 0) < cap
        conn.execute(
            "UPDATE staging SET source_table=COALESCE(?,source_table), source_id=COALESCE(?,source_id), "
            "source_quote=COALESCE(?,source_quote), trace_json=COALESCE(?,trace_json), "
            "status=CASE WHEN ? THEN 'pending' ELSE status END, "
            "error=CASE WHEN ? THEN NULL ELSE error END WHERE id=?",
            (source_table, str(source_id) if source_id is not None else None, source_quote,
             trace_json, rearm, rearm, eid))
        conn.commit()
        return {"staging_id": eid, "idempotency_key": idem, "dedup": True,
                "status": "pending" if rearm else estatus, "rearmed": rearm}
    cur = conn.execute(
        "INSERT INTO staging(idempotency_key,target_table,op,natural_key,payload,source_table,source_id,"
        "source_quote,trace_json,submitted_by,status) VALUES(?,?,?,?,?,?,?,?,?,?,'pending')",
        (idem, target_table, op,
         json.dumps(natural_key, default=str) if natural_key is not None else None,
         json.dumps(payload, default=str),
         source_table, str(source_id) if source_id is not None else None,
         source_quote, trace_json, submitted_by),
    )
    conn.commit()
    return {"staging_id": cur.lastrowid, "idempotency_key": idem, "dedup": False, "status": "pending"}


def pending(conn: sqlite3.Connection, target_table: Optional[str] = None, limit: int = 1000) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    if target_table:
        return conn.execute(
            "SELECT * FROM staging WHERE status='pending' AND target_table=? ORDER BY id LIMIT ?",
            (target_table, limit)).fetchall()
    return conn.execute("SELECT * FROM staging WHERE status='pending' ORDER BY id LIMIT ?", (limit,)).fetchall()


def counts(conn: sqlite3.Connection) -> dict:
    return {f"{r[0]}/{r[1]}": r[2] for r in conn.execute(
        "SELECT target_table,status,COUNT(*) FROM staging GROUP BY 1,2").fetchall()}


if __name__ == "__main__":
    from _db import connect
    c = connect()
    print("staging counts:", counts(c))
