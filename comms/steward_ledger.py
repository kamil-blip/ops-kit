"""Long-running plan ledger — crash-safe, compaction-proof journaling for
long-running agent runs.

A run journals every phase to three places so resume never depends on chat memory:
  * plan_phase_log  — one row per (run_id, phase); the resume cursor.
  * plan_runs       — one row per (plan_id, phase_id, phase_hash); idempotency.
  * bus_events      — a visible event stream surfaced by query.py health.
Plus current_plan.json on disk (under data/plan-state/) as the human-readable
contract. steward_ledger is the sole owner of that file.

Idempotency: a phase whose plan_phase_log row is already status='done' is a
no-op on re-entry (phase_done returns "already"). Each mutation in the phase body
should still wrap itself in a SAVEPOINT (see steward_bus).

Usage:
    import steward_ledger as L
    L.start_run("example-run-20260101", "plan-example-20260101",
                ["0","1","2","3","4","5","6"])
    if L.phase_status("example-run-20260101","1") != "done":
        L.phase_start("example-run-20260101","1","build spine")
        ... do work ...
        L.phase_done("example-run-20260101","1", counts={"tables":3})
"""
from __future__ import annotations
import hashlib, json, os, sqlite3, datetime
from pathlib import Path

import paths
from _db import connect

CURRENT_PLAN_JSON = Path(paths.PLAN_STATE_DIR) / "current_plan.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def idem_key(*parts) -> str:
    """sha256(plan|phase|step|inputs) — stable across re-entry."""
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def start_run(run_id: str, plan_id: str, phases: list[str], conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO plan_phase_log(run_id,phase,status,started_at) VALUES(?,?,?,?)",
            (run_id, "__run__", "in_progress", _now()),
        )
        conn.commit()
        bus_event("plan_run_start", f"{run_id} started", {"plan_id": plan_id, "phases": phases},
                  session_id=run_id, conn=conn)
        _write_current_plan(run_id, plan_id, phases, current="0")
    finally:
        if own:
            conn.close()


def phase_status(run_id: str, phase: str, conn: sqlite3.Connection | None = None) -> str | None:
    own = conn is None
    conn = conn or connect()
    try:
        r = conn.execute(
            "SELECT status FROM plan_phase_log WHERE run_id=? AND phase=?", (run_id, phase)
        ).fetchone()
        return r[0] if r else None
    finally:
        if own:
            conn.close()


def phase_start(run_id: str, phase: str, notes: str = "", conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            "INSERT INTO plan_phase_log(run_id,phase,status,started_at,notes) VALUES(?,?,?,?,?) "
            "ON CONFLICT(run_id,phase) DO UPDATE SET status='in_progress', started_at=COALESCE(started_at,excluded.started_at), notes=excluded.notes",
            (run_id, phase, "in_progress", _now(), notes),
        )
        conn.commit()
        bus_event("phase_start", f"{run_id} phase {phase}", {"notes": notes}, session_id=run_id, conn=conn)
    finally:
        if own:
            conn.close()


def phase_done(run_id: str, phase: str, counts: dict | None = None, notes: str = "",
               plan_id: str | None = None, conn: sqlite3.Connection | None = None) -> str:
    """Mark a phase done. Returns 'already' if it was already done (idempotent)."""
    own = conn is None
    conn = conn or connect()
    try:
        cur = conn.execute("SELECT status FROM plan_phase_log WHERE run_id=? AND phase=?", (run_id, phase)).fetchone()
        if cur and cur[0] == "done":
            return "already"
        conn.execute(
            "INSERT INTO plan_phase_log(run_id,phase,status,started_at,completed_at,counts_json,notes) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id,phase) DO UPDATE SET "
            "status='done', completed_at=excluded.completed_at, counts_json=excluded.counts_json, notes=excluded.notes",
            (run_id, phase, "done", _now(), _now(), json.dumps(counts or {}), notes),
        )
        if plan_id:
            ph_hash = idem_key(plan_id, phase, notes)
            conn.execute(
                "INSERT OR IGNORE INTO plan_runs(plan_id,phase_id,phase_name,phase_hash,started_at,finished_at,outcome,idempotency_key,evidence_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (plan_id, phase, notes or phase, ph_hash, _now(), _now(), "DONE", ph_hash, json.dumps(counts or {})),
            )
        conn.commit()
        bus_event("phase_done", f"{run_id} phase {phase} done", counts or {}, session_id=run_id, conn=conn)
        return "done"
    finally:
        if own:
            conn.close()


def phase_fail(run_id: str, phase: str, error: str, conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            "INSERT INTO plan_phase_log(run_id,phase,status,started_at,notes) VALUES(?,?,?,?,?) "
            "ON CONFLICT(run_id,phase) DO UPDATE SET status='failed', notes=excluded.notes",
            (run_id, phase, "failed", _now(), error[:2000]),
        )
        conn.commit()
        bus_event("phase_fail", f"{run_id} phase {phase} FAILED", {"error": error[:500]}, session_id=run_id, conn=conn)
    finally:
        if own:
            conn.close()


def bus_event(event_type: str, summary: str, details: dict | None = None,
              session_id: str = "steward-ledger", conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            "INSERT INTO bus_events(session_id,event_type,summary,details_json,ts) VALUES(?,?,?,?,?)",
            (session_id, event_type, summary[:500], json.dumps(details or {}), _now()),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def _write_current_plan(run_id: str, plan_id: str, phases: list[str], current: str) -> None:
    CURRENT_PLAN_JSON.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_PLAN_JSON.write_text(json.dumps({
        "run_id": run_id, "plan_id": plan_id, "phases": phases,
        "current_phase": current, "updated_at": _now(),
    }, indent=2), encoding="utf-8")


def set_current_phase(run_id: str, plan_id: str, phases: list[str], current: str) -> None:
    _write_current_plan(run_id, plan_id, phases, current)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        conn = connect()
        if len(sys.argv) > 2:
            rid = sys.argv[2]
            for row in conn.execute(
                "SELECT phase,status,completed_at,counts_json FROM plan_phase_log WHERE run_id=? ORDER BY id", (rid,)
            ):
                print(row)
        else:
            print("runs (pass a run_id for phase detail):")
            for row in conn.execute(
                "SELECT run_id, COUNT(*), MAX(completed_at) FROM plan_phase_log GROUP BY run_id ORDER BY 3 DESC"
            ):
                print(row)
