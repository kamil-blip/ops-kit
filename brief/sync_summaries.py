"""sync_summaries.py — write one row per brief.py sync run.

Captures: ingest counts (emails/discord/beeper/granola), action_item lifecycle
deltas, dossier refreshes, ingest_rejections, handler failures, and any
sources that failed. (Cascade-event and state-violation counters exist for
optional pipeline modules; they stay 0 when those tables are absent.)

The :func:`SyncSummary` context manager snapshots baseline counts on enter and
writes the delta on exit. Use::

    from sync_summaries import SyncSummary
    with SyncSummary(notes="full sync") as ss:
        ...
        ss.add_failure("gmail")
        ss.add_note("dashboard regen: 3 lines changed")

The row id is exposed as ``ss.row_id`` once :meth:`finish` has run.

CLI subcommands:

    sync_summaries.py latest        # JSON of most recent row
    sync_summaries.py last 7        # JSON of last 7 rows
    sync_summaries.py write         # Manual write (uses pre-set baseline=0)
"""
from __future__ import annotations
import paths

import json
import sqlite3
import _db  # unified connector (busy_timeout + FK ON)
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(str(paths.DB_PATH))

# Stale WAITING items threshold (days)
TRIAGE_WAITING_DAYS = 3


def _connect() -> sqlite3.Connection:
    conn = _db.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def _count(conn: sqlite3.Connection, table: str, where: str = "1=1", params=()) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params).fetchone()
        return int(row["n"]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


class SyncSummary:
    """Context manager that records a sync_summaries row on exit.

    Baselines are captured on __enter__; deltas are computed on __exit__.
    Caller can :meth:`add_failure`, :meth:`add_note`, or set counters
    directly (e.g. ``ss.dossiers_refreshed = 4``) before exit.
    """

    def __init__(self, notes: str = "") -> None:
        self._notes_init = notes
        self.notes_extra: list[str] = []
        self.failures: list[str] = []

        self.row_id: int | None = None
        self.started_at: str = ""
        self.finished_at: str = ""

        # Baseline counts (set on enter)
        self._baseline: dict[str, int] = {}

        # Optional manual overrides — caller can bump these
        self.dossiers_refreshed: int = 0
        self.people_updated: int = 0
        self.handler_failures_override: int | None = None

    # ---- public helpers --------------------------------------------------
    def add_failure(self, source: str) -> None:
        if source and source not in self.failures:
            self.failures.append(source)

    def add_note(self, line: str) -> None:
        if line:
            self.notes_extra.append(line)

    # ---- context manager -------------------------------------------------
    def __enter__(self) -> "SyncSummary":
        self.started_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
        self._t0 = time.time()
        conn = _connect()
        try:
            self._baseline = self._snapshot(conn)
        finally:
            conn.close()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.finish(exc=exc)
        return None  # don't swallow

    # ---- core ------------------------------------------------------------
    def _snapshot(self, conn: sqlite3.Connection) -> dict[str, int]:
        """Snapshot counts that change during sync. Used to compute deltas."""
        snap = {
            "emails": _count(conn, "emails"),
            "discord": _count(conn, "discord_messages"),
            "beeper": _count(conn, "beeper_messages"),
            "ai_total": _count(conn, "action_items"),
            "ai_closed": _count(conn, "action_items", "status IN ('CLOSED','DONE','RESOLVED')"),
            "ingest_rejections": _count(conn, "ingest_rejections"),
            "wq_failed": _count(conn, "work_queue", "status = 'failed'"),
        }
        # cascade_log belongs to an optional pipeline module; feature-detect
        # so its counter degrades to 0 when not installed.
        snap["cascade_log"] = (
            _count(conn, "cascade_log") if _table_exists(conn, "cascade_log") else 0
        )
        # granola: count distinct slugs in reference_docs
        snap["granola"] = _count(
            conn, "reference_docs", "slug LIKE 'transcript-%'"
        )
        return snap

    def _compute_triage(self, conn: sqlite3.Connection) -> list[str]:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=TRIAGE_WAITING_DAYS)).isoformat()
        rows = conn.execute(
            """
            SELECT item_id FROM action_items
             WHERE status IN ('WAITING','BLOCKED')
               AND COALESCE(updated_at, inserted_at) < ?
             ORDER BY priority, COALESCE(updated_at, inserted_at)
             LIMIT 25
            """,
            (cutoff,),
        ).fetchall()
        return [r["item_id"] for r in rows if r["item_id"]]

    def _ai_updated_in_window(self, conn: sqlite3.Connection) -> int:
        """Count action_items whose updated_at falls inside the sync window."""
        return _count(
            conn,
            "action_items",
            "updated_at >= ?",
            (self.started_at,),
        )

    def _state_violations_in_window(self, conn: sqlite3.Connection) -> int:
        """Best-effort: ingest_rejections rows from this run that mention
        the state-machine guard. A future revision may add a dedicated column.
        """
        if not _table_exists(conn, "ingest_rejections"):
            return 0
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM ingest_rejections
                 WHERE rejected_at >= ?
                   AND (errors LIKE '%state%' OR errors LIKE '%transition%')
                """,
                (self.started_at,),
            ).fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.OperationalError:
            return 0

    def finish(self, exc: BaseException | None = None) -> int | None:
        """Compute deltas, write the row, return its id. Idempotent."""
        if self.row_id is not None:
            return self.row_id

        self.finished_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
        duration = time.time() - getattr(self, "_t0", time.time())

        if exc is not None:
            self.add_failure(f"sync_exc:{exc.__class__.__name__}")
            self.add_note(f"sync raised {exc.__class__.__name__}: {str(exc)[:200]}")

        conn = _connect()
        try:
            now_snap = self._snapshot(conn)
            base = self._baseline

            emails_new = max(0, now_snap["emails"] - base.get("emails", 0))
            discord_new = max(0, now_snap["discord"] - base.get("discord", 0))
            beeper_new = max(0, now_snap["beeper"] - base.get("beeper", 0))
            granola_new = max(0, now_snap["granola"] - base.get("granola", 0))

            ai_created = max(0, now_snap["ai_total"] - base.get("ai_total", 0))
            ai_closed = max(0, now_snap["ai_closed"] - base.get("ai_closed", 0))
            ai_updated = self._ai_updated_in_window(conn)

            cascade_events = max(0, now_snap["cascade_log"] - base.get("cascade_log", 0))
            handler_failures = (
                self.handler_failures_override
                if self.handler_failures_override is not None
                else max(0, now_snap["wq_failed"] - base.get("wq_failed", 0))
            )
            ingest_new = max(0, now_snap["ingest_rejections"] - base.get("ingest_rejections", 0))
            state_violations = self._state_violations_in_window(conn)

            triage = self._compute_triage(conn)

            notes_parts: list[str] = []
            if self._notes_init:
                notes_parts.append(self._notes_init)
            notes_parts.extend(self.notes_extra)
            notes_parts.append(f"duration={duration:.1f}s")
            notes = " | ".join(notes_parts)

            cur = conn.execute(
                """
                INSERT INTO sync_summaries (
                    started_at, finished_at, duration_sec,
                    emails_new, discord_new, beeper_new, granola_new,
                    action_items_created, action_items_updated, action_items_closed_auto,
                    people_updated, dossiers_refreshed,
                    ingest_rejections, handler_failures, cascade_events, state_violations,
                    triage_needed, sources_failed, notes
                ) VALUES (?,?,?, ?,?,?,?, ?,?,?, ?,?, ?,?,?,?, ?,?,?)
                """,
                (
                    self.started_at, self.finished_at, round(duration, 2),
                    emails_new, discord_new, beeper_new, granola_new,
                    ai_created, ai_updated, ai_closed,
                    self.people_updated, self.dossiers_refreshed,
                    ingest_new, handler_failures, cascade_events, state_violations,
                    json.dumps(triage), ",".join(self.failures), notes,
                ),
            )
            conn.commit()
            self.row_id = int(cur.lastrowid)
            return self.row_id
        finally:
            conn.close()


@contextmanager
def sync_summary_run(notes: str = ""):
    """Convenience wrapper. ``with sync_summary_run() as ss: ...``"""
    ss = SyncSummary(notes=notes)
    ss.__enter__()
    try:
        yield ss
    finally:
        ss.__exit__(None, None, None)


def get_latest(n: int = 1) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM sync_summaries ORDER BY id DESC LIMIT ?", (n,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _cli() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    cmd = args[0] if args else "latest"

    if cmd == "latest":
        rows = get_latest(1)
        print(json.dumps(rows[0] if rows else {}, indent=2, default=str))
        return 0

    if cmd == "last":
        n = int(args[1]) if len(args) > 1 else 5
        print(json.dumps(get_latest(n), indent=2, default=str))
        return 0

    if cmd == "write":
        # Manual one-shot row (deltas of zero, just records a heartbeat)
        notes = args[1] if len(args) > 1 else "manual"
        with SyncSummary(notes=notes) as ss:
            pass
        print(json.dumps({"row_id": ss.row_id}))
        return 0

    print("usage: sync_summaries.py {latest|last N|write [notes]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
