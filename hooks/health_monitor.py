"""Hook health monitoring, event bus, and audit logging.

SQLite-backed (bus_events / hook_health / audit_events tables); replaces the
older JSONL session-bus approach. All functions are best-effort: they never
raise exceptions or block hooks.

Usage:
    from health_monitor import timed, emit, log_audit, read_recent_events

    @timed("my_hook")
    def run(event):
        ...

    emit("email_sent", "Sent agenda to Jane Doe", {"to": "jane@example.org"})
    log_audit("tool_call", tool="Bash", cmd_preview="git status")
    events = read_recent_events(minutes=180, event_types=["email_sent"])
"""
import functools
import json
import os
import sys
import time
from datetime import datetime, timedelta

_hooks_dir = os.path.dirname(os.path.abspath(__file__))
if not sys.path or sys.path[0] != _hooks_dir:
    # Front-insert so hooks/config.py wins over core/config.py when this
    # module is imported in-process (script execution already has it first).
    sys.path.insert(0, _hooks_dir)

from config import get_conn, get_session_id

# Pruning thresholds (days)
_PRUNE_HOOK_HEALTH = 7
_PRUNE_BUS_EVENTS = 3
_PRUNE_AUDIT_LOG = 14

# Rate-limit pruning to at most once per process
_pruned = False


def _prune_once():
    """Delete old rows from all three tables. Runs once per process."""
    global _pruned
    if _pruned:
        return
    _pruned = True
    try:
        conn = get_conn()
        conn.execute("PRAGMA busy_timeout = 100")
        now = datetime.utcnow()
        # space-format ts: comparisons must match datetime('now'); legacy
        # ISO-T rows age out within the prune window
        conn.execute("DELETE FROM hook_health WHERE ts < ?",
                      ((now - timedelta(days=_PRUNE_HOOK_HEALTH)).strftime("%Y-%m-%d %H:%M:%S"),))
        conn.execute("DELETE FROM bus_events WHERE ts < ?",
                      ((now - timedelta(days=_PRUNE_BUS_EVENTS)).strftime("%Y-%m-%d %H:%M:%S"),))
        conn.execute("DELETE FROM audit_events WHERE ts < ?",
                      ((now - timedelta(days=_PRUNE_AUDIT_LOG)).strftime("%Y-%m-%d %H:%M:%S"),))
        conn.commit()
        conn.close()
    except Exception:
        pass


def timed(hook_name):
    """Decorator that logs hook invocation timing and errors to hook_health."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            _prune_once()
            start = time.monotonic()
            error_msg = None
            try:
                return func(*args, **kwargs)
            except Exception as e:
                error_msg = str(e)[:500]
                raise
            finally:
                try:
                    duration_ms = (time.monotonic() - start) * 1000
                    # Determine event_type from first arg if it looks like a hook event
                    event_type = "unknown"
                    if args and hasattr(args[0], "get"):
                        event_type = (args[0].get("hook_event_name")
                                      or args[0].get("hookEventName")
                                      or args[0].get("type") or "unknown")
                    elif args and isinstance(args[0], str):
                        event_type = args[0]
                    conn = get_conn()
                    # Use short busy_timeout for health logging: don't block the
                    # hook if another process holds a write lock
                    conn.execute("PRAGMA busy_timeout = 100")
                    conn.execute(
                        "INSERT INTO hook_health (hook_name, event_type, duration_ms, error_message, ts) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (hook_name, event_type, round(duration_ms, 1), error_msg,
                         datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
                    )
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
        return wrapper
    return decorator


def emit(event_type, summary, details=None):
    """Write an event to the bus_events table."""
    try:
        _prune_once()
        details_json = json.dumps(details, ensure_ascii=False) if details else None
        conn = get_conn()
        conn.execute("PRAGMA busy_timeout = 100")
        conn.execute(
            "INSERT INTO bus_events (session_id, event_type, summary, details_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (get_session_id(), event_type, (summary or "")[:500], details_json,
             datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def log_audit(event_type, tool=None, file_path=None, cmd_preview=None, meta=None):
    """Write an entry to the audit_events table."""
    try:
        _prune_once()
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        conn = get_conn()
        conn.execute("PRAGMA busy_timeout = 100")
        conn.execute(
            "INSERT INTO audit_events (session_id, event, tool, source_file, cmd_preview, meta, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (get_session_id(), event_type, tool, file_path,
             (cmd_preview or "")[:200] if cmd_preview else None, meta_json,
             datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def read_recent_events(minutes=180, event_types=None):
    """Query bus_events from the last N minutes, optionally filtered by type.

    Returns list of dicts with keys: session_id, event_type, summary, details_json, ts.
    """
    try:
        cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_conn()
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            rows = conn.execute(
                f"SELECT * FROM bus_events WHERE ts >= ? AND event_type IN ({placeholders}) ORDER BY ts",
                [cutoff] + list(event_types),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bus_events WHERE ts >= ? ORDER BY ts",
                (cutoff,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []
