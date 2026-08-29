"""The Context Steward tick — the single canonical-writer loop (Write-Audit-Publish).

One tick: change-detect (cheap, zero-LLM exit if nothing new) -> [optional] pull
sources -> PUBLISH staged writes through the command bus -> advance the CDC cursor
-> optional reconcile subroutine -> surface the judgment queue -> integrity gate ->
heartbeat -> wal checkpoint -> exit.

Run modes:
    steward.py tick                # one mechanics tick (safe; no source pull, no LLM)
    steward.py tick --pull         # also run brief.py sync first (heavier; opt-in)
    steward.py status              # print steward_health + queue depths

Mechanics are deterministic and headless-safe. Judgments are frontier-model-only
and FAIL CLOSED (steward_brain): a headless tick never calls a cheap model and
never resolves a judgment with anything but the configured frontier brain; if no
brain runs, the queue depth is surfaced as an alarm and the agent-driven
(in-session) tick drains it.
"""
from __future__ import annotations
import json, os, sys, time, subprocess

import paths
from _db import connect
import steward_bus as B
import steward_cdc as CDC
import steward_brain as BR

# Integrity gate. If the operator has a full db_gate module on the path (archive-
# aware FK exemptions, ratcheting baseline), use it. Otherwise fall back to a
# minimal built-in gate: REAL PRAGMA checks (quick_check + foreign_key_check)
# gated against a baseline in steward_config (0 on a fresh database), plus an
# explicit no-op for the advisory write-gate observability layer.
try:
    import db_gate as G
except ImportError:
    class _MinimalGate:
        """Built-in fallback integrity gate (db_gate module not included)."""

        @staticmethod
        def _baseline(conn) -> int:
            r = conn.execute(
                "SELECT value FROM steward_config WHERE key='fk_violation_baseline'").fetchone()
            try:
                return int(r[0]) if r else 0
            except (TypeError, ValueError):
                return 0

        @staticmethod
        def status(conn) -> dict:
            qc = conn.execute("PRAGMA quick_check").fetchone()[0]
            fk = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            base = _MinimalGate._baseline(conn)
            return {"quick_check": qc, "fk_true": fk, "fk_baseline": base,
                    "gate": "builtin-minimal",
                    "clean": (qc == "ok" and fk <= base)}

        @staticmethod
        def write_gate_observe(conn) -> dict:
            return {"note": "db_gate module not on path; "
                            "advisory write-gate observation skipped"}

        @staticmethod
        def write_gate_status(conn) -> dict:
            return {"note": "db_gate module not on path; "
                            "advisory write-gate observation skipped"}

    G = _MinimalGate

PYEXE = str(paths.PYTHON)
HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(HERE, "steward.lock")
BRIEF_PY = paths.ROOT / "brief" / "brief.py"


def _acquire_lock(max_age_s: int = 600) -> bool:
    """File/PID lock to prevent overlapping ticks (scheduled-task safety). A lock
    older than max_age_s is treated as stale (a crashed tick) and reclaimed."""
    if os.path.exists(LOCK):
        try:
            age = time.time() - os.path.getmtime(LOCK)
            if age < max_age_s:
                return False
        except OSError:
            pass
    try:
        with open(LOCK, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False


def _release_lock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


def _staged_pending(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM staging WHERE status='pending'").fetchone()[0])


def _heartbeat(conn, status, *, ms, promoted=0, rejected=0, review_depth=None, staged_pending=None, error=None):
    # The health row ships empty in a fresh install; make sure it exists so the
    # very first tick is recorded (all columns besides id are nullable/defaulted).
    conn.execute("INSERT OR IGNORE INTO steward_health(id) VALUES(1)")
    conn.execute(
        "UPDATE steward_health SET last_tick_at=datetime('now'), last_tick_status=?, last_tick_ms=?, "
        "ticks_total=COALESCE(ticks_total,0)+1, last_error=?, last_promoted=?, last_rejected=?, "
        "last_review_depth=?, last_staged_pending=?, pid=?, updated_at=datetime('now') WHERE id=1",
        (status, ms, error, promoted, rejected, review_depth, staged_pending, os.getpid()))
    conn.commit()


def _reconcile(conn) -> dict:
    """Optional reconcile/roster-audit subroutine. The reference implementation was
    domain-specific and is not included in this starter kit; to enable one, put a
    steward_reconcile.py exposing tick_reconcile(conn) on the import path."""
    try:
        import steward_reconcile  # noqa
        return steward_reconcile.tick_reconcile(conn)
    except ImportError:
        return {"reconcile": "skipped: steward_reconcile not included in this starter kit"}


def tick(pull: bool = False, publish: bool = True) -> dict:
    if not _acquire_lock():
        return {"status": "locked", "note": "another steward tick is running"}
    t0 = time.monotonic()
    conn = connect()
    out = {"status": "ok"}
    try:
        new_cdc = CDC.count_new(conn)
        staged = _staged_pending(conn)
        changed = (new_cdc > 0) or (staged > 0) or pull
        if not changed:
            ms = int((time.monotonic() - t0) * 1000)
            _heartbeat(conn, "skipped_no_change", ms=ms, review_depth=BR.pending_depth(conn), staged_pending=0)
            return {"status": "skipped_no_change", "new_cdc": 0, "staged_pending": 0}

        if pull:
            try:
                subprocess.run([PYEXE, str(BRIEF_PY), "sync"], cwd=str(paths.ROOT),
                               timeout=600, capture_output=True)
                out["pulled"] = True
            except Exception as e:  # noqa: BLE001 — a pull failure must not abort the tick
                out["pull_error"] = str(e)[:200]

        pub = B.publish_pending(conn) if publish else {}
        out["publish"] = pub
        CDC.advance_cursor(conn)
        out["reconcile"] = _reconcile(conn)

        review_depth = BR.pending_depth(conn)
        out["review_queue_depth"] = review_depth
        gate = G.status(conn)
        out["integrity"] = gate
        status = "ok" if gate["clean"] else "error"
        # advisory write-gate: observe + advance the watermark on a clean tick
        if gate["clean"]:
            try:
                out["write_gate"] = G.write_gate_observe(conn)
            except Exception as e:  # noqa: BLE001 — observability must never fail the tick
                out["write_gate_error"] = str(e)[:200]
        ms = int((time.monotonic() - t0) * 1000)
        _heartbeat(conn, status, ms=ms, promoted=pub.get("promoted", 0), rejected=pub.get("rejected", 0),
                   review_depth=review_depth, staged_pending=_staged_pending(conn),
                   error=None if gate["clean"] else f"integrity: {gate}")
        try:
            conn.execute("PRAGMA wal_checkpoint(RESTART)")
        except Exception:  # noqa: BLE001
            pass
        out["status"] = status
        return out
    except Exception as exc:  # noqa: BLE001
        ms = int((time.monotonic() - t0) * 1000)
        try:
            _heartbeat(conn, "error", ms=ms, error=str(exc)[:1000])
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error", "error": str(exc)[:500]}
    finally:
        conn.close()
        _release_lock()


def status() -> dict:
    conn = connect()
    try:
        conn.row_factory = None
        h = conn.execute("SELECT last_tick_at,last_tick_status,last_tick_ms,ticks_total,last_promoted,"
                         "last_review_depth,last_staged_pending,pid FROM steward_health WHERE id=1").fetchone()
        return {
            "health": dict(zip(["last_tick_at","last_tick_status","last_tick_ms","ticks_total",
                                "last_promoted","last_review_depth","last_staged_pending","pid"], h)) if h else None,
            "staged_pending": _staged_pending(conn),
            "review_queue_pending": BR.pending_depth(conn),
            "cdc_new": CDC.count_new(conn),
            "integrity": G.status(conn),
            "write_gate": G.write_gate_status(conn),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "tick":
        # Scheduled-tick liveness heartbeat (fail-open). NB: aliased _job_heartbeat
        # because steward.py has its own module-level _heartbeat (this block runs
        # at module scope and would shadow it).
        try:
            from job_heartbeat import heartbeat as _job_heartbeat
        except Exception:
            import contextlib as _cl
            def _job_heartbeat(job):
                return _cl.nullcontext(type("_HB", (), {"rows_touched": 0, "exit_note": None})())
        with _job_heartbeat("ContextSteward-WAP") as _hb:
            _out = tick(pull="--pull" in sys.argv)
            try:
                _hb.rows_touched = int(_out.get("published", 0) or 0) if isinstance(_out, dict) else 0
            except Exception:
                pass
            print(json.dumps(_out, indent=2, default=str))
    else:
        print(json.dumps(status(), indent=2, default=str))
