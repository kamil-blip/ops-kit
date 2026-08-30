"""Consolidated session lifecycle hook: SessionStart, SessionEnd, PreCompact, Stop.

Dispatch priority:
1. HOOK_TYPE env var       -> set by settings.json command prefix (primary)
2. hook_event/event/type   -> from stdin JSON data (fallback)
3. session_summary lookup  -> heuristic (last resort)
4. tool_name present       -> not for us (exit early)

Registered for: SessionStart, SessionEnd, PreCompact, Stop.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

_hooks_dir = os.path.dirname(os.path.abspath(__file__))
if not sys.path or sys.path[0] != _hooks_dir:
    # Front-insert so hooks/config.py wins over core/config.py when this
    # module is imported in-process (script execution already has it first).
    sys.path.insert(0, _hooks_dir)

try:
    from config import get_conn, get_session_id, PATHS
    from health_monitor import timed, emit
except ImportError as e:
    print(f"Session started (imports unavailable: {e})", flush=True)
    sys.exit(0)

# core/paths.py is on sys.path via the installer's .pth file. Fall back to a
# portable derivation (repo root = parent of hooks/) if it is unavailable.
try:
    import paths as _paths
except ImportError:
    _paths = None


def _repo_root():
    if _paths is not None:
        r = getattr(_paths, "ROOT", None) or getattr(_paths, "OPS_ROOT", None)
        if r:
            return str(r)
        db = getattr(_paths, "DB_PATH", None)
        if db:
            # DB_PATH is <repo-root>/data/ops.db by contract
            return os.path.dirname(os.path.dirname(os.path.abspath(str(db))))
    root = os.path.dirname(_hooks_dir)
    return os.path.dirname(root) if os.path.basename(root) == "platform" else root


_REPO_ROOT = _repo_root()


def _plan_state_dir():
    if _paths is not None:
        d = getattr(_paths, "PLAN_STATE_DIR", None)
        if d:
            return str(d)
    return os.path.join(_REPO_ROOT, "data", "plan-state")


def _table_exists(conn, name):
    """Feature-detect an optional table/view. Never raises."""
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
            (name,),
        ).fetchone() is not None
    except Exception:
        return False


# --- Background-worker emit helper (feature-detected, fail-open) ------------
def _safe_emit(handler_name, payload, dedup_key=None, priority=5):
    """Fire-and-forget emit into work_queue. Never raises.

    Feature-detected: the starter kit ships the work_queue table but the
    background worker module is optional. If no work_queue module is on the
    path, this is a silent no-op (the emit is advisory, not load-bearing).

    Duplicates state_tracker._safe_emit on purpose so that either hook can
    emit even if the other hook's module fails to import.
    """
    try:
        from work_queue import emit_event  # noqa: PLC0415
        emit_event(handler_name, payload, priority=priority, dedup_key=dedup_key)
    except Exception:
        pass


# Events that count as "meaningful work" for changelog
WRITE_EVENTS = {
    "email_send", "email_draft", "notion_write", "discord_send",
    "social_post", "file_write", "memory_update", "changelog_update",
    "directory_add", "action_item_update", "email_draft_file", "mcp_call",
}

SKIP_EVENTS = {"heartbeat", "tool_call"}

# Phrases that mean "nothing happened", filter these from hook output
_QUIET_PHRASES = frozenset([
    "no items to unblock", "no recurring items to spawn",
    "no new replies found", "no waiting items with email",
    "no potential duplicates found",
])


def _run_task_mgr(cmd, timeout=10):
    """Run a task_manager.py subcommand silently. Returns stdout or None on failure."""
    import subprocess
    task_mgr_path = os.path.join(_REPO_ROOT, "tasks", "task_manager.py")
    if not os.path.exists(task_mgr_path):
        return None
    try:
        result = subprocess.run(
            [PATHS.get("python", sys.executable), task_mgr_path, cmd],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8",
        )
        if result.returncode == 0:
            return result.stdout.strip()
        emit("hook_health", f"task_mgr {cmd} returned {result.returncode}", {"stderr": (result.stderr or "")[:200]})
        return None
    except Exception as e:
        emit("hook_health", f"task_mgr {cmd} failed: {e}")
        return None


def _is_meaningful(output):
    """True if task_manager output contains real updates (not just 'nothing to do')."""
    if not output:
        return False
    return not any(phrase in output.lower() for phrase in _QUIET_PHRASES)


def _short_sid(full_id):
    if not full_id or full_id == "unknown":
        return "unknown"
    return full_id.replace("-", "")[:6]


# ---------------------------------------------------------------------------
# SessionStart
# ---------------------------------------------------------------------------
@timed("session_lifecycle:start")
def on_session_start(data):
    """Full briefing: session type, tasks, action items, deferred, bus."""
    lines = []
    now = datetime.now()
    date_str = now.strftime("%A %B %d, %Y %H:%M")
    lines.append(f"Session started: {date_str}")

    conn = get_conn()

    # --- Register FIRST: the briefing below can blow the SessionStart time
    # --- budget, and a kill would lose this INSERT if it were sequenced last
    # --- (zero session_summary rows -> fallback 24h windows in the phase4
    # --- changelog and duplicated commit lists).
    session_id = get_session_id()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO session_summary (session_id, started_at, summary)
            VALUES (?, ?, 'Active')
        """, (session_id, now.isoformat()))
        conn.commit()
        emit("session_started", f"Session started: {date_str}", {"session_id": session_id})
    except Exception:
        pass

    # Self-register into session_registry (optional multi-session bus).
    # Fail-open; no-op if the table is absent. Role: env CLAUDE_SESSION_ROLE
    # if set, else 'executor' when this sid owns the active plan, else 'live'.
    try:
        if _table_exists(conn, "session_registry"):
            import os as _os
            _role = _os.environ.get("CLAUDE_SESSION_ROLE")
            if not _role:
                try:
                    import json as _json
                    _plan_path = os.path.join(_plan_state_dir(), "current_plan.json")
                    _cp = _json.load(open(_plan_path, encoding="utf-8"))
                    _owner = (_cp.get("session") or "").replace("-", "")
                    _sid6 = (session_id or "").replace("-", "")[:6]
                    _role = "executor" if (_owner and _sid6 and _sid6.startswith(_owner)) else "live"
                except Exception:
                    _role = "live"
            conn.execute(
                "INSERT INTO session_registry (sid, role, cwd, pid, last_heartbeat, status) "
                "VALUES (?,?,?,?,datetime('now'),'active') "
                "ON CONFLICT(sid) DO UPDATE SET role=excluded.role, cwd=excluded.cwd, "
                "pid=excluded.pid, last_heartbeat=datetime('now'), status='active'",
                ((session_id or "")[:6], _role, _os.getcwd(), _os.getpid()))
            conn.commit()
    except Exception:
        pass

    # --- Session type ---
    last_session = conn.execute(
        "SELECT started_at FROM session_summary ORDER BY started_at DESC LIMIT 1"
    ).fetchone()

    if last_session:
        try:
            last_ts = datetime.fromisoformat(last_session[0])
            hours_since = (now - last_ts).total_seconds() / 3600
            if hours_since < 3:
                lines.append("SESSION TYPE: Continuation (another session was active recently)")
                lines.append("  Focus: check action items for open items, pick up where needed.")
            else:
                lines.append("SESSION TYPE: New session")
        except Exception:
            lines.append("SESSION TYPE: New session")
    else:
        lines.append("SESSION TYPE: First session with the ops DB")

    lines.append("")

    # --- Orientation payload: queue health + last-session anchor so every
    # --- session starts oriented without a manual health run. Fail-open:
    # --- any error skips the block silently; optional tables are
    # --- feature-detected so a leaner install degrades gracefully.
    try:
        orient = []
        if _table_exists(conn, "work_queue"):
            wq = conn.execute(
                "SELECT handler, COUNT(*) n, ROUND((julianday('now')-julianday(MIN(created_at)))*24,1) age_h "
                "FROM work_queue WHERE status='pending' GROUP BY handler ORDER BY n DESC LIMIT 4"
            ).fetchall()
            if wq:
                orient.append("  queue: " + ", ".join(
                    f"{r[0]}={r[1]} (oldest {r[2]}h)" for r in wq))
            else:
                orient.append("  queue: empty")
        if _table_exists(conn, "steward_health"):
            st = conn.execute(
                "SELECT last_tick_at, last_tick_status, ticks_total FROM steward_health "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            if st:
                orient.append(f"  steward: {st[1]} @ {st[0]} (tick {st[2]})")
        if _table_exists(conn, "review_queue"):
            rq = conn.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0]
            orient.append(f"  review_queue pending: {rq}")
        last_log = conn.execute(
            "SELECT date, title FROM session_logs ORDER BY id DESC LIMIT 1").fetchone()
        if last_log:
            orient.append(f"  last logged session: {last_log[0]} {(last_log[1] or '')[:70]}")
        if orient:
            lines.append("ORIENTATION:")
            lines.extend(orient)
            lines.append("")
    except Exception:
        pass

    # --- Reset per-session counters ---
    for counter_file in [
        os.path.expanduser("~/.claude/.state_tracker_counter"),
    ]:
        try:
            if os.path.exists(counter_file):
                os.remove(counter_file)
        except Exception:
            pass

    # --- hook_health retention: prune rows older than 7 days ---
    try:
        deleted = conn.execute(
            "DELETE FROM hook_health WHERE ts < datetime('now', '-7 days')"
        ).rowcount
        if deleted:
            conn.commit()
            lines.append(f"HOOK_HEALTH: pruned {deleted} rows older than 7 days")
            lines.append("")
    except Exception:
        pass

    # --- Conversation history import: sweep any prior session JSONLs into ---
    # --- conversation_history table. Fail-open: never block session start. ---
    # --- Needed because the wrap-up skill's manual call to import_session.py ---
    # --- finds nothing for the still-active session; this hook catches it   ---
    # --- next time a session opens.                                         ---
    try:
        import subprocess
        importer = os.path.join(_REPO_ROOT, "logging", "import_session.py")
        if os.path.exists(importer):
            result = subprocess.run(
                [sys.executable, importer],
                capture_output=True, text=True, timeout=20,
            )
            tail = (result.stdout or "").strip().splitlines()
            if tail and "Imported 0" not in tail[0]:
                lines.append(f"CONVERSATION_HISTORY: {tail[0]}")
                lines.append("")
    except Exception:
        pass

    # --- Auto-maintenance: reply-check, spawn, unblock ---
    maint_notes = []
    for sub_cmd in ["reply-check", "spawn", "unblock"]:
        out = _run_task_mgr(sub_cmd)
        if _is_meaningful(out):
            maint_notes.append(out)
    # --- Auto-promote learning candidates ---
    try:
        from learning_loop import promote_candidates
        promo_result = promote_candidates(auto=True)
        if promo_result.get("promoted"):
            maint_notes.append(f"Auto-promoted {len(promo_result['promoted'])} learnings: " + "; ".join(promo_result["promoted"]))
    except Exception as e:
        print(f"learning auto-promote error: {e}", file=sys.stderr)

    # --- Decay unused learnings (throttled to once per 20h, pool-pressure gated) ---
    try:
        conn_decay = get_conn()
        last_decay = conn_decay.execute(
            "SELECT MAX(updated_at) FROM learnings "
            "WHERE status='archived' AND source LIKE '%decayed:%'"
        ).fetchone()[0]
        run_decay = True
        if last_decay:
            try:
                lr_dt = datetime.fromisoformat(last_decay.replace("Z", "+00:00"))
                if lr_dt.tzinfo is None:
                    lr_dt = lr_dt.replace(tzinfo=timezone.utc)
                hours = (datetime.now(timezone.utc) - lr_dt).total_seconds() / 3600.0
                run_decay = hours >= 20
            except Exception:
                pass
        conn_decay.close()
        if run_decay:
            _ml_path = os.path.join(_REPO_ROOT, "memory")
            if _ml_path not in sys.path:
                sys.path.insert(0, _ml_path)
            import memory_lifecycle as _ml
            _ml_db = _ml.get_db()
            try:
                _ml.decay(_ml_db)
            finally:
                _ml_db.close()
    except Exception as e:
        print(f"decay error: {e}", file=sys.stderr)

    if maint_notes:
        lines.append("AUTO-MAINTENANCE:")
        for note in maint_notes:
            for ln in note.splitlines():
                lines.append(f"  {ln}")
        lines.append("")

    # --- Action items (smart surfacing via task_manager) ---
    try:
        task_mgr_path = os.path.join(_REPO_ROOT, "tasks", "task_manager.py")
        if os.path.exists(task_mgr_path):
            import subprocess
            result = subprocess.run(
                [PATHS.get("python", sys.executable), task_mgr_path, "focus", "7"],
                capture_output=True, text=True, timeout=10, encoding="utf-8"
            )
            if result.returncode == 0 and result.stdout.strip():
                lines.append(result.stdout.strip())
                lines.append("")
                # If no plan is committed for today, prompt the operator to lock
                # the day. Avoids the parallel-multitask trap by surfacing the
                # commit step.
                try:
                    today_iso = datetime.now().strftime("%Y-%m-%d")
                    plan_row = conn.execute(
                        "SELECT plan_date FROM daily_plans WHERE plan_date=?",
                        (today_iso,),
                    ).fetchone()
                    if not plan_row:
                        lines.append("PLAN NOT SET for today.")
                        lines.append("  Tell me what's important and I'll commit the plan, or say \"lock top 3 by urgency\" and I'll auto-pick.")
                        lines.append("")
                except Exception:
                    pass
            else:
                raise RuntimeError("task_manager focus failed")
        else:
            raise FileNotFoundError("task_manager.py not found")
    except Exception:
        # Fallback: original flat list
        action_items = conn.execute("""
            SELECT item_id, status, priority, description, due_date, waiting_on
            FROM action_items
            WHERE status IN ('OPEN', 'WAITING', 'BLOCKED')
            ORDER BY
                CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                due_date ASC NULLS LAST
        """).fetchall()

        if action_items:
            p0 = [a for a in action_items if a['priority'] == 'P0']
            p1 = [a for a in action_items if a['priority'] == 'P1']
            other = [a for a in action_items if a['priority'] not in ('P0', 'P1')]

            lines.append(f"ACTION ITEMS ({len(action_items)} open):")
            if p0:
                lines.append("  P0 (DO NOW):")
                for a in p0:
                    lines.append(f"    - {a['description'][:100]}")
            if p1:
                lines.append("  P1 (TODAY):")
                for a in p1:
                    status_tag = f"[{a['status']}] " if a['status'] != 'OPEN' else ""
                    lines.append(f"    - {status_tag}{a['description'][:100]}")
            if other:
                lines.append(f"  + {len(other)} lower priority items")
            lines.append("")

    # --- Deferred actions (count only, details via /brief) ---
    deferred_count = conn.execute(
        "SELECT COUNT(*) FROM deferred_actions WHERE status = 'pending'"
    ).fetchone()[0]
    if deferred_count > 0:
        lines.append(f"DEFERRED ACTIONS: {deferred_count} pending (run /brief for details)")
        lines.append("")

    # --- Action-item inbox depth (proposals awaiting triage) ---
    try:
        inbox_pending = conn.execute(
            "SELECT COUNT(*) FROM action_items_inbox WHERE status='pending'"
        ).fetchone()[0]
        inbox_recent = conn.execute(
            "SELECT COUNT(*) FROM action_items_inbox "
            "WHERE status='pending' AND proposed_at >= datetime('now','-7 days')"
        ).fetchone()[0]
        if inbox_pending > 0:
            lines.append(f"INBOX: {inbox_pending} pending proposals ({inbox_recent} from last 7 days)")
            lines.append(f"  Triage via skill `triage-inbox` or `task_manager.py inbox` to keep this from drifting.")
            lines.append("")
    except sqlite3.OperationalError:
        pass  # table missing on old DB; non-fatal

    # --- Re-inject anchor if resuming after compaction ---
    try:
        anchor_path = os.path.join(_REPO_ROOT, ".claude-anchor.json")
        if os.path.exists(anchor_path):
            with open(anchor_path, "r", encoding="utf-8") as f:
                anchor = json.load(f)
            if anchor.get("intent") or anchor.get("decisions"):
                lines.append("SESSION ANCHOR (preserved from before compaction):")
                if anchor.get("intent"):
                    lines.append(f"  Intent: {anchor['intent']}")
                if anchor.get("changes_made"):
                    lines.append(f"  Changes: {', '.join(anchor['changes_made'][:5])}")
                if anchor.get("decisions"):
                    lines.append(f"  Decisions: {', '.join(anchor['decisions'][:5])}")
                if anchor.get("next_steps"):
                    lines.append(f"  Next: {', '.join(anchor['next_steps'][:5])}")
                if anchor.get("people_mentioned"):
                    lines.append(f"  People: {', '.join(anchor['people_mentioned'][:8])}")
                if anchor.get("learning_candidates"):
                    lines.append(f"  Learning candidates: {len(anchor['learning_candidates'])} pending")
                    for lc in anchor['learning_candidates'][:3]:
                        lines.append(f"    - [{lc.get('signal_type','')}] {lc.get('error_summary','')[:60]}")
                lines.append("")
            # Don't delete, it's useful across multiple compactions
    except Exception:
        pass

    # --- Session already registered at the top of this function ---
    conn.close()

    lines.append(f"SESSION ID: {session_id[:6]} (ops.db active)")
    lines.append("")
    lines.append("Run the ops loop: orient > route > context > execute > side-fx > log")

    # <!-- phase4:digest-start -->
    # Daily digest: render once on the first session of the operator's local day.
    # No cron; relies on the human opening at least one session per day.
    # Failures are silent (digest is informational, not load-bearing).
    try:
        digest_text = _maybe_emit_daily_digest()
        if digest_text:
            lines.append("")
            lines.append("=== DAILY DIGEST (first session of day) ===")
            lines.append(digest_text)
    except Exception as e:
        emit("hook_health", f"daily_digest skipped: {e}")
    # <!-- phase4:digest-end -->

    # session_started emit moved to the top-of-function registration block.

    print("\n".join(lines), flush=True)


# <!-- phase4:digest-helpers-start -->
def _daily_digest_state_path():
    return os.path.expanduser("~/.claude/.daily_digest_state")


def _maybe_emit_daily_digest():
    """Run daily_digest.py --print iff this is the first session of the day.

    Persists the last-emitted date in ~/.claude/.daily_digest_state. Returns
    the digest text on first-of-day, empty string otherwise.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    state_path = _daily_digest_state_path()
    last = ""
    try:
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                last = f.read().strip()
    except Exception:
        pass
    if last == today:
        return ""

    digest_path = os.path.join(_REPO_ROOT, "autonomy", "daily_digest.py")
    if not os.path.exists(digest_path):
        return ""

    import subprocess
    try:
        out = subprocess.run(
            [PATHS.get("python", sys.executable), digest_path, "--print"],
            capture_output=True, text=True, timeout=20, encoding="utf-8",
        )
        if out.returncode != 0:
            emit("hook_health", "daily_digest non-zero", {"stderr": (out.stderr or "")[:200]})
            return ""
    except Exception as e:
        emit("hook_health", f"daily_digest failed: {e}")
        return ""

    try:
        with open(state_path, "w", encoding="utf-8") as f:
            f.write(today)
    except Exception:
        pass

    return out.stdout.strip()
# <!-- phase4:digest-helpers-end -->



# ---------------------------------------------------------------------------
# SessionEnd
# ---------------------------------------------------------------------------
@timed("session_lifecycle:end")
def on_session_end(data):
    """Wrap-up checklist, deferred queue warning, emit session_ended event."""
    conn = get_conn()

    # Check deferred queue
    pending_count = conn.execute(
        "SELECT COUNT(*) FROM deferred_actions WHERE status = 'pending'"
    ).fetchone()[0]

    pending_msg = ""
    if pending_count > 0:
        pending_items = conn.execute(
            "SELECT action_type, details FROM deferred_actions WHERE status = 'pending' LIMIT 5"
        ).fetchall()
        pending_msg = f"\n  *** {pending_count} DEFERRED ACTIONS STILL PENDING ***\n"
        for p in pending_items:
            pending_msg += f"    - [{p['action_type']}] {(p['details'] or '')[:60]}\n"
        pending_msg += "  These will resurface at next session start.\n"

    # State-aware wrap-up nudge. If this session mutated state (audit_events
    # beyond reads) but left no session_logs row, say so explicitly; silent
    # unlogged sessions are how changes go undocumented.
    unlogged_msg = ""
    try:
        sid = get_session_id()
        changed = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE session_id = ? AND event NOT IN "
            "('bash_cmd', 'mcp_call', 'file_read')", (sid,)
        ).fetchone()[0]
        has_log = conn.execute(
            "SELECT COUNT(*) FROM session_logs WHERE session_id LIKE ?", (sid + "%",)
        ).fetchone()[0]
        if changed and not has_log:
            unlogged_msg = (
                f"\n  *** THIS SESSION CHANGED STATE ({changed} write events) BUT HAS NO "
                "session_logs ROW *** Run the wrap-up skill (session log entry) NOW.\n"
            )
    except Exception:
        pass

    conn.close()

    print(
        "SESSION ENDING - WRAP-UP CHECKLIST:\n"
        "  1. Update action_items in ops.db (new items, status changes)\n"
        "  2. Changelog auto-generated from audit_events (in reference_docs table)\n"
        "  3. Any template improvements to flag?\n"
        "  4. Any learnings to log (INSERT INTO learnings)?\n"
        f"{pending_msg}"
        f"{unlogged_msg}"
        "\nInvoke wrap-up skill if not done yet.",
        file=sys.stdout,
    )

    # <!-- phase4:changelog-start -->
    # Per-session changelog addendum: capture action_items mutated, drafts
    # created, emails sent via outbound_log, and commits authored. Writes a
    # second reference_docs row distinct from on_stop_changelog so the two
    # don't overwrite each other. Resilient to partial failure: a STATUS:
    # partial header is written if any source crashes.
    try:
        _phase4_session_addendum(get_session_id())
    except Exception as e:
        emit("hook_health", f"phase4 changelog addendum failed: {e}")
    # <!-- phase4:changelog-end -->

    emit("session_ended", "Session ending")


# <!-- phase4:changelog-helpers-start -->
def _drafts_dir():
    """Optional on-disk drafts directory scanned by the phase4 changelog.

    Resolution order: paths.DRAFTS_DIR (if the spine defines it) -> the
    OPS_DRAFTS_DIR env var -> empty (scan skipped).
    """
    if _paths is not None:
        d = getattr(_paths, "DRAFTS_DIR", None)
        if d:
            return str(d)
    return os.environ.get("OPS_DRAFTS_DIR", "")


def _phase4_session_addendum(sid):
    """Phase 4 per-session changelog: captures cross-source session activity.

    Sources:
        - action_items mutated this session (best-effort: rows updated since
          session start)
        - drafts under the configured drafts dir matching session id
        - outbound_log rows tagged with this session_id
        - git commits authored in this window
    Writes one reference_docs row with slug ``changelog-{date}-{sid6}-phase4``.
    """
    if not sid or sid == "unknown":
        return

    today = datetime.now().strftime("%Y-%m-%d")
    sid6 = sid.replace("-", "")[:6]
    slug = f"changelog-{today}-{sid6}-phase4"

    conn = get_conn()

    # Skip if already written this session (idempotent)
    existing = conn.execute(
        "SELECT 1 FROM reference_docs WHERE slug = ?", (slug,)
    ).fetchone()
    if existing:
        conn.close()
        return

    started_at = None
    try:
        row = conn.execute(
            "SELECT started_at FROM session_summary WHERE session_id = ?", (sid,)
        ).fetchone()
        if row and row["started_at"]:
            started_at = row["started_at"]
    except Exception:
        pass
    if not started_at:
        # Fallback: 24h ago
        from datetime import timedelta
        started_at = (datetime.now() - timedelta(hours=24)).isoformat()

    parts = [f"### {today} | Phase 4 changelog | session {sid6}", ""]
    is_partial = False

    # 1. action_items mutated this session
    try:
        rows = conn.execute(
            """
            SELECT item_id, status, priority, description
              FROM action_items
             WHERE updated_at >= ?
             ORDER BY priority, updated_at DESC
             LIMIT 20
            """,
            (started_at,),
        ).fetchall()
        if rows:
            parts.append("**Action items touched this session:**")
            for r in rows:
                d = (r["description"] or "")[:90]
                parts.append(f"- [{r['priority'] or '?'}] {r['item_id']} ({r['status']}): {d}")
            parts.append("")
    except Exception as e:
        parts.append(f"_action_items query failed: {e}_")
        is_partial = True

    # 2. outbound_log rows for this session
    try:
        rows = conn.execute(
            """
            SELECT channel, recipient, subject, sent_via, timestamp
              FROM outbound_log
             WHERE session_id = ?
             ORDER BY timestamp
            """,
            (sid,),
        ).fetchall()
        if rows:
            parts.append("**Outbound this session:**")
            for r in rows:
                subj = (r["subject"] or "")[:60]
                parts.append(
                    f"- [{r['channel']}] {r['recipient']} via {r['sent_via']}"
                    + (f", {subj}" if subj else "")
                )
            parts.append("")
    except Exception as e:
        parts.append(f"_outbound_log query failed: {e}_")
        is_partial = True

    conn.close()

    # 3. drafts on disk for this session (only if a drafts dir is configured)
    try:
        import glob
        drafts_root = _drafts_dir()
        if drafts_root and os.path.isdir(drafts_root):
            patterns = [
                os.path.join(drafts_root, f"drafts-*/*{sid6}*"),
                os.path.join(drafts_root, f"drafts-*{sid6}*/*"),
            ]
            seen = set()
            for pat in patterns:
                for p in glob.glob(pat):
                    if p in seen:
                        continue
                    seen.add(p)
            if seen:
                parts.append("**Drafts created:**")
                for p in sorted(seen):
                    parts.append(f"- `{os.path.relpath(p, drafts_root)}`")
                parts.append("")
    except Exception as e:
        parts.append(f"_drafts scan failed: {e}_")
        is_partial = True

    # 4. git commits in this session window
    try:
        import subprocess
        proj = _REPO_ROOT
        if proj and os.path.isdir(os.path.join(proj, ".git")):
            since = started_at.replace("T", " ").split(".")[0]
            out = subprocess.run(
                ["git", "log", "--no-merges", "--pretty=format:%h %s",
                 "--since", since],
                cwd=proj, capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                # First-claim ledger: a plain time-window git log makes every
                # concurrent session log the SAME commits. The first session to
                # claim a hash owns it; later sessions skip it. Fail-open: any
                # ledger error keeps the old (duplicating) behavior.
                lines = out.stdout.strip().splitlines()[:20]
                try:
                    lconn = get_conn()
                    kept = []
                    for line in lines:
                        chash = line.split(" ", 1)[0]
                        claimed = lconn.execute(
                            "INSERT OR IGNORE INTO changelog_commit_ledger (commit_hash, session_id, slug) VALUES (?,?,?)",
                            (chash, sid6, slug)).rowcount
                        if claimed:
                            kept.append(line)
                    lconn.commit()
                    lconn.close()
                    lines = kept
                except Exception:  # noqa: BLE001
                    pass
                if lines:
                    parts.append("**Commits this session:**")
                    for line in lines:
                        parts.append(f"- `{line}`")
                    parts.append("")
    except Exception as e:
        parts.append(f"_git log failed: {e}_")
        is_partial = True

    if is_partial:
        parts.insert(1, "STATUS: partial, one or more sources errored")
        parts.insert(2, "")

    if len(parts) <= 2:
        # Nothing meaningful to log
        return

    try:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO reference_docs (slug, title, category, content, doc_type, updated_at)
            VALUES (?, ?, 'changelog', ?, 'changelog', datetime('now'))
            """,
            (slug, f"Phase 4 changelog session {sid6}", "\n".join(parts)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        emit("hook_health", f"phase4 changelog write failed: {e}")
# <!-- phase4:changelog-helpers-end -->


# ---------------------------------------------------------------------------
# PreCompact
# ---------------------------------------------------------------------------
def _extract_transcript_context(transcript_path, max_lines=500):
    """Read conversation from transcript JSONL. Returns structured context.

    Transcript JSONL envelope format: each line is {type, message: {role, content}}.
    Reads at most max_lines from the end of the file. Extracts:
    - User intent (first substantial user message)
    - Recent user messages (last 5)
    - Files being edited (from tool_use blocks)
    - Corrections (user messages with correction signals)
    """
    result = {
        "intent": "",
        "recent_user_messages": [],
        "files_edited": [],
        "corrections": [],
    }

    if not transcript_path or not os.path.exists(transcript_path):
        return result

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Take last max_lines if file is very large
        if len(lines) > max_lines:
            lines = lines[-max_lines:]

        # Parse JSONL envelope format: each line has {type, message: {role, content}}
        entries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue

        if not entries:
            return result

        # Helper: extract text from message content (string or list-of-blocks)
        def get_text(content):
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, str):
                        parts.append(b)
                    elif isinstance(b, dict) and b.get("type") == "text":
                        parts.append(b.get("text", ""))
                return " ".join(parts).strip()
            return ""

        # Helper: strip system-reminder tags from user text
        def clean_user_text(text):
            import re
            text = re.sub(r'<system-reminder>.*?</system-reminder>', '', text,
                          flags=re.DOTALL).strip()
            text = re.sub(r'<local-command-.*?>.*?</local-command-.*?>', '', text,
                          flags=re.DOTALL).strip()
            text = re.sub(r'<command-.*?>.*?</command-.*?>', '', text,
                          flags=re.DOTALL).strip()
            return text.strip()

        # Extract user messages and assistant tool calls from envelope format
        user_msgs = []
        assistant_entries = []
        for entry in entries:
            etype = entry.get("type", "")
            msg = entry.get("message", {})
            if etype == "user" and msg.get("role") == "user":
                text = clean_user_text(get_text(msg.get("content", "")))
                if text and len(text) > 5:
                    user_msgs.append(text)
            elif etype == "assistant" and msg.get("role") == "assistant":
                assistant_entries.append(msg)

        # Intent: first substantial user message in this window
        for text in user_msgs:
            if len(text) > 20:  # skip short commands like "/effort"
                result["intent"] = text[:300]
                break

        # Recent user messages (last 5, truncated)
        for text in user_msgs[-5:]:
            result["recent_user_messages"].append(text[:200])

        # Files edited: scan assistant tool_use blocks for Edit/Write
        files_seen = []
        for msg in assistant_entries:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") in ("Edit", "Write"):
                    fp = (block.get("input") or {}).get("file_path", "")
                    if fp:
                        short = fp.replace("\\", "/").split("/")[-1]
                        if short not in files_seen:
                            files_seen.append(short)
        result["files_edited"] = files_seen[:10]

        # Corrections: user messages starting with correction signals
        # Only check the first 50 chars to avoid false positives in long messages
        correction_starts = ("no,", "no ", "no.", "don't", "wrong", "instead",
                             "not that", "stop", "actually,", "actually ",
                             "wait,", "wait ", "that's not", "that's wrong")
        for text in user_msgs[-10:]:
            lead = text[:50].lower().lstrip()
            if any(lead.startswith(w) for w in correction_starts):
                result["corrections"].append(text[:200])

    except Exception:
        pass

    return result


def _build_additional_context(anchor):
    """Build concise additionalContext string for post-compaction injection."""
    parts = []
    parts.append("SESSION ANCHOR (preserved before compaction):")

    if anchor.get("intent"):
        parts.append(f"  Intent: {anchor['intent']}")

    if anchor.get("changes_made"):
        parts.append(f"  Changes: {', '.join(anchor['changes_made'][:5])}")

    if anchor.get("decisions"):
        parts.append(f"  Decisions: {'; '.join(anchor['decisions'][:5])}")

    if anchor.get("files_touched"):
        parts.append(f"  Files: {', '.join(anchor['files_touched'][:8])}")

    if anchor.get("files_edited"):
        parts.append(f"  Editing: {', '.join(anchor['files_edited'][:8])}")

    if anchor.get("people_mentioned"):
        parts.append(f"  People: {', '.join(anchor['people_mentioned'][:8])}")

    if anchor.get("next_steps"):
        parts.append(f"  Next: {'; '.join(anchor['next_steps'][:5])}")

    if anchor.get("corrections"):
        parts.append("  Corrections from user:")
        for c in anchor["corrections"][:3]:
            parts.append(f"    - {c}")

    if anchor.get("recent_user_messages"):
        parts.append("  Recent user messages:")
        for m in anchor["recent_user_messages"][-3:]:
            parts.append(f"    - {m}")

    if anchor.get("learning_candidates"):
        parts.append(f"  Learning candidates: {len(anchor['learning_candidates'])} pending")
        for lc in anchor["learning_candidates"][:3]:
            parts.append(f"    - [{lc.get('signal_type','')}] {lc.get('error_summary','')[:60]}")

    return "\n".join(parts)


@timed("session_lifecycle:pre_compact")
def on_pre_compact(data):
    """Save session anchor before compaction.

    Three extraction sources:
    1. Transcript (user intent, corrections, files being edited)
    2. DB audit trail (changes made, files touched, people contacted)
    3. DB state (open action items, learning candidates, bus event decisions)

    Output:
    - Anchor saved to the ops DB (bus_events) for cross-session persistence
    - additionalContext returned to Claude for post-compaction context injection
    """
    emit("context_compacting", "Context window compressing. Saving anchor.")

    conn = get_conn()
    sid = get_session_id()

    # --- 1. Transcript extraction ---
    transcript_ctx = _extract_transcript_context(data.get("transcript_path", ""))

    # Build anchor
    anchor = {
        "session_id": sid,
        "saved_at": datetime.now().isoformat(),
        "intent": transcript_ctx["intent"],
        "recent_user_messages": transcript_ctx["recent_user_messages"],
        "corrections": transcript_ctx["corrections"],
        "files_edited": transcript_ctx["files_edited"],
        "changes_made": [],
        "decisions": [],
        "next_steps": [],
        "people_mentioned": [],
        "files_touched": [],
    }

    # --- 2. DB audit trail ---
    try:
        audits = conn.execute("""
            SELECT event AS event_type, tool, source_file AS file_path, cmd_preview, meta AS meta_json
            FROM audit_events WHERE session_id = ?
            ORDER BY ts DESC LIMIT 30
        """, (sid,)).fetchall()

        files = set()
        actions = []
        for a in audits:
            if a["file_path"]:
                fp = a["file_path"].replace("\\", "/").split("/")[-1]
                files.add(fp)
            if a["event_type"] in ("email_send", "discord_send", "notion_write", "social_post"):
                meta = json.loads(a["meta_json"]) if a["meta_json"] else {}
                actions.append(f"{a['event_type']}: {meta.get('recipient', '')}")
        anchor["files_touched"] = list(files)[:10]
        anchor["changes_made"] = actions[:10]
    except Exception:
        pass

    # Recently mentioned people
    try:
        recent_people = conn.execute("""
            SELECT DISTINCT name FROM people
            WHERE last_contact_date > datetime('now', '-1 hour')
            LIMIT 10
        """).fetchall()
        anchor["people_mentioned"] = [p["name"] for p in recent_people]
    except Exception:
        pass

    # --- 3. DB state: decisions, next steps, learning candidates ---
    try:
        events = conn.execute("""
            SELECT event_type, summary FROM bus_events
            WHERE session_id = ? ORDER BY ts DESC LIMIT 10
        """, (sid,)).fetchall()
        for evt in events:
            s = evt["summary"] or ""
            if "decision" in s.lower() or "chose" in s.lower() or "approved" in s.lower():
                anchor["decisions"].append(s[:100])
    except Exception:
        pass

    try:
        pending = conn.execute("""
            SELECT description FROM action_items
            WHERE status = 'OPEN' AND priority IN ('P0', 'P1')
            ORDER BY priority LIMIT 5
        """).fetchall()
        anchor["next_steps"] = [p["description"][:80] for p in pending]
    except Exception:
        pass

    try:
        from learning_loop import anchor_candidates
        anchor_candidates(anchor, conn, sid)
    except Exception:
        pass

    conn.close()

    # --- Persist anchor ---
    # File (for SessionStart re-injection)
    try:
        anchor_path = os.path.join(_REPO_ROOT, ".claude-anchor.json")
        with open(anchor_path, "w", encoding="utf-8") as f:
            json.dump(anchor, f, indent=2)
    except Exception:
        pass

    # DB (cross-session persistence)
    try:
        conn = get_conn()
        conn.execute("""
            INSERT INTO bus_events (session_id, event_type, summary, details_json, ts)
            VALUES (?, 'pre_compact_anchor', 'Session anchor saved before compaction', ?, ?)
        """, (sid, json.dumps(anchor), datetime.now().isoformat()))
        # Default checkpoint row: ordinary sessions survive compaction too
        # (the checkpoints table was barely used before this default row).
        conn.execute(
            "INSERT INTO checkpoints (session_id, task_name, intent, progress, "
            "next_steps, state_json, created_at, status) "
            "VALUES (?, 'precompact-auto', ?, ?, ?, ?, datetime('now'), 'open')",
            (sid,
             (anchor.get("intent") or "")[:400],
             json.dumps(anchor.get("files_edited", [])[:20]),
             json.dumps(anchor.get("next_steps", [])[:10]),
             json.dumps({"recent_user_messages": anchor.get("recent_user_messages", [])[-3:],
                         "corrections": anchor.get("corrections", [])[-3:]})),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    # --- Output ---
    # Diagnostics to stderr
    msg = f"CONTEXT COMPACTING: Anchor saved (session {sid[:6]}).\n"
    msg += f"  Intent: {anchor['intent'][:80]}{'...' if len(anchor['intent']) > 80 else ''}\n"
    msg += f"  Files edited: {len(anchor['files_edited'])}, Files touched: {len(anchor['files_touched'])}\n"
    msg += f"  People: {len(anchor['people_mentioned'])}, Changes: {len(anchor['changes_made'])}, Corrections: {len(anchor['corrections'])}"
    print(msg, file=sys.stderr)

    # additionalContext to stdout, injected into Claude's context before compaction
    additional = _build_additional_context(anchor)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreCompact",
            "additionalContext": additional,
        }
    }))


# ---------------------------------------------------------------------------
# Stop (auto-changelog)
# ---------------------------------------------------------------------------
def _path_strip_prefixes():
    """Prefixes stripped from file paths in the changelog, derived at runtime.

    Order matters: longest / most specific first. Covers the Claude Code
    per-project data dir (home/.claude/projects/<cwd-slug>/), the repo root,
    and the home dir.
    """
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    root = _REPO_ROOT.replace("\\", "/").rstrip("/")
    # Claude Code slugifies the project cwd into its data-dir name
    slug = root.replace(":", "-").replace("/", "-")
    prefixes = [
        f"{home}/.claude/projects/{slug}/",
        f"{root}/",
        f"{home}/",
    ]
    return [p for p in prefixes if len(p) > 1]


@timed("session_lifecycle:stop_changelog")
def on_stop_changelog(data):
    """Generate changelog fragment from audit_events entries for this session."""
    sid = get_session_id()

    # --- Background-worker emit: promote_learning -------------------------
    # Queue a promotion pass over ALL pending learning_candidates (no session
    # filter). The worker runs independently, so session-scoping would cause
    # events to miss candidates from other sessions. Dedup_key ensures only
    # one promotion per session even if Stop fires multiple times.
    # (No-op when no background worker is installed; see _safe_emit.)
    try:
        _safe_emit(
            "promote_learning",
            {"batch_size": 20},
            dedup_key=f"promote_{sid}",
            priority=3,
        )
    except Exception:
        pass

    # --- Dedup check ---
    dedup_out = _run_task_mgr("dedup")
    if _is_meaningful(dedup_out):
        print(f"WARNING: {dedup_out}", file=sys.stderr)

    if not sid or sid == "unknown":
        return

    today = datetime.now().strftime("%Y-%m-%d")

    # Read audit entries for this session from the ops DB
    conn = get_conn()
    entries = conn.execute("""
        SELECT event AS event_type, tool, source_file AS file_path, cmd_preview, meta AS meta_json, ts
        FROM audit_events
        WHERE session_id = ? AND ts >= ?
        ORDER BY ts
    """, (sid, today)).fetchall()
    conn.close()

    if not entries:
        return

    # Check if meaningful work was done
    write_entries = [e for e in entries if e['event_type'] in WRITE_EVENTS]
    if not write_entries:
        return

    # Check if changelog already exists in DB
    conn2 = get_conn()
    changelog_slug = f"changelog-{today}-auto-{sid}"
    existing = conn2.execute(
        "SELECT 1 FROM reference_docs WHERE slug = ?", (changelog_slug,)
    ).fetchone()
    if existing:
        conn2.close()
        return

    # Build the fragment
    actions = []
    files_changed = set()
    seen_actions = set()
    strip_prefixes = _path_strip_prefixes()

    for entry in entries:
        event = entry['event_type'] or ""
        if event in SKIP_EVENTS:
            continue

        tool = entry['tool'] or ""
        file_path = entry['file_path'] or ""
        cmd_preview = entry['cmd_preview'] or ""

        # Parse meta_json
        meta = {}
        if entry['meta_json']:
            try:
                meta = json.loads(entry['meta_json'])
            except Exception:
                pass

        # Track files changed
        if file_path and file_path != "unknown":
            short_path = file_path.replace("\\", "/")
            for prefix in strip_prefixes:
                if short_path.startswith(prefix):
                    short_path = short_path[len(prefix):]
                    break
            files_changed.add(short_path)

        # Build action descriptions (deduplicated)
        action_key = None
        action_desc = None

        if event == "email_send":
            recipient = meta.get("recipient", "unknown")
            subject = meta.get("subject", "")
            action_key = f"email:{recipient}"
            action_desc = f"Sent email to {recipient}" + (f" ({subject})" if subject else "")

        elif event == "email_draft":
            recipient = meta.get("recipient", "unknown")
            action_key = f"draft:{recipient}"
            action_desc = f"Drafted email to {recipient}"

        elif event == "notion_write":
            action_key = "notion_write"
            action_desc = "Updated Notion"

        elif event == "discord_send":
            channel = meta.get("channel", "")
            action_key = f"discord:{channel}"
            action_desc = f"Sent Discord message" + (f" (#{channel})" if channel else "")

        elif event == "social_post":
            action_key = "social_post"
            action_desc = "Scheduled social media post"

        elif event == "bash_cmd" and cmd_preview:
            cmd_lower = cmd_preview.lower().strip()
            skip_prefixes = ("ls ", "dir ", "head ", "tail ", "cat ", "type ", "echo ", "cd ")
            if not any(cmd_lower.startswith(p) for p in skip_prefixes):
                short_cmd = cmd_preview[:80].split("\n")[0]
                action_key = f"bash:{short_cmd}"
                action_desc = f"Ran: `{short_cmd}`"

        elif event in ("memory_update", "file_write", "changelog_update",
                       "directory_add", "action_item_update", "email_draft_file"):
            pass  # Covered by files_changed

        if action_desc and action_key not in seen_actions:
            seen_actions.add(action_key)
            actions.append(action_desc)

    if not actions and files_changed:
        actions.append(f"Edited {len(files_changed)} file(s)")

    # Build fragment content
    frag_lines = [f"### {today} | Auto-logged session {sid}", ""]

    if actions:
        frag_lines.append("**Actions:**")
        for a in actions[:20]:
            frag_lines.append(f"- {a}")
        frag_lines.append("")

    if files_changed:
        frag_lines.append("**Files changed:**")
        for fp in sorted(files_changed)[:30]:
            frag_lines.append(f"- `{fp}`")
        frag_lines.append("")

    try:
        # Min-content guard: header-only fragments (mostly mcp_call-triggered)
        # are noise, not a record.
        if len(frag_lines) > 2:
            conn2.execute("""
                INSERT INTO reference_docs (slug, title, category, content, doc_type, updated_at)
                VALUES (?, ?, 'changelog', ?, 'changelog', datetime('now'))
            """, (changelog_slug, f"Auto-logged session {sid}", "\n".join(frag_lines)))
            conn2.commit()
    except Exception:
        pass
    finally:
        conn2.close()

    # FSRS auto-review: rate surfaced learnings and update spaced repetition state
    _auto_review_learnings(sid)


def _auto_review_learnings(session_id):
    """Auto-rate surfaced learnings as Good(3) and update FSRS state.

    Runs at session end. Respects manual overrides set during wrap-up
    (those have rating set but fsrs_state_after still NULL).
    """
    try:
        from fsrs import Scheduler, Card, Rating, State

        scheduler = Scheduler(
            desired_retention=0.9,
            learning_steps=(),
            relearning_steps=(),
            enable_fuzzing=False,
        )

        conn = get_conn()
        now = datetime.now(timezone.utc)

        # Process all unprocessed reviews (not just this session's). Session IDs
        # in hook calls are not stable across UserPromptSubmit/Stop because Claude
        # Code passes session_id via stdin JSON, not env var, while get_session_id()
        # reads env. The 30-second guard avoids racing an in-flight surface write.
        # Batch limit caps per-Stop work so a large backlog doesn't stall the hook.
        unprocessed = conn.execute("""
            SELECT lr.id as review_id, lr.learning_id, lr.rating, lr.adherence,
                   l.fsrs_state, l.fsrs_stability, l.fsrs_difficulty,
                   l.fsrs_due, l.fsrs_last_review, l.fsrs_step
            FROM learning_reviews lr
            JOIN learnings l ON l.id = lr.learning_id
            WHERE lr.fsrs_state_after IS NULL
              AND lr.surfaced_at <= datetime('now', '-30 seconds')
            ORDER BY lr.id ASC
            LIMIT 50
        """).fetchall()

        if not unprocessed:
            conn.close()
            return

        reviewed_count = 0
        for row in unprocessed:
            try:
                # Adherence-derived rating (stop training FSRS on a constant
                # Good(3) -- the bug that locked critical rules out of surfacing).
                # Manual rating wins; else map the adherence verdict; else mark
                # processed at the current FSRS state (no advance) so an un-judged
                # review never fabricates a Good and never churns the queue.
                _adh = row["adherence"]
                if row["rating"]:
                    rating = Rating(row["rating"]); effective_rating = row["rating"]
                elif _adh == "violated":
                    rating = Rating.Again; effective_rating = 1
                elif _adh == "followed":
                    rating = Rating.Good; effective_rating = 3
                elif _adh == "na":
                    rating = Rating.Hard; effective_rating = 2
                else:
                    conn.execute(
                        "UPDATE learning_reviews SET fsrs_state_after = ?, reviewed_at = ?, "
                        "source = 'unjudged' WHERE id = ?",
                        ((row["fsrs_state"] if row["fsrs_state"] is not None else 2),
                         now.strftime('%Y-%m-%d %H:%M:%S'), row["review_id"]),
                    )
                    reviewed_count += 1
                    continue

                # Reconstruct Card from DB state
                due_dt = now
                if row["fsrs_due"]:
                    try:
                        due_dt = datetime.fromisoformat(row["fsrs_due"])
                        if due_dt.tzinfo is None:
                            due_dt = due_dt.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        pass

                last_review_dt = now
                if row["fsrs_last_review"]:
                    try:
                        last_review_dt = datetime.fromisoformat(row["fsrs_last_review"])
                        if last_review_dt.tzinfo is None:
                            last_review_dt = last_review_dt.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        pass

                card = Card(
                    card_id=str(row["learning_id"]),
                    state=State(row["fsrs_state"] or 2),
                    step=row["fsrs_step"],
                    stability=row["fsrs_stability"] or 3.0,
                    difficulty=row["fsrs_difficulty"] or 5.0,
                    due=due_dt,
                    last_review=last_review_dt,
                )

                updated_card, _ = scheduler.review_card(card, rating, now)

                # Write updated FSRS state to learnings
                conn.execute("""
                    UPDATE learnings SET
                        fsrs_state = ?, fsrs_stability = ?, fsrs_difficulty = ?,
                        fsrs_due = ?, fsrs_last_review = ?, fsrs_step = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                """, (
                    updated_card.state.value,
                    updated_card.stability,
                    updated_card.difficulty,
                    updated_card.due.strftime("%Y-%m-%d %H:%M:%S"),
                    updated_card.last_review.strftime("%Y-%m-%d %H:%M:%S") if updated_card.last_review else now.strftime("%Y-%m-%d %H:%M:%S"),
                    updated_card.step,
                    row["learning_id"],
                ))

                # Complete the review record
                conn.execute("""
                    UPDATE learning_reviews SET
                        rating = ?,
                        reviewed_at = ?,
                        fsrs_state_before = ?, fsrs_stability_before = ?, fsrs_difficulty_before = ?,
                        fsrs_state_after = ?, fsrs_stability_after = ?, fsrs_difficulty_after = ?,
                        source = CASE WHEN rating IS NOT NULL THEN 'manual' ELSE 'auto' END
                    WHERE id = ?
                """, (
                    effective_rating,
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    row["fsrs_state"], row["fsrs_stability"], row["fsrs_difficulty"],
                    updated_card.state.value, updated_card.stability, updated_card.difficulty,
                    row["review_id"],
                ))

                reviewed_count += 1
            except Exception:
                continue  # Skip individual failures

        conn.commit()
        conn.close()

        if reviewed_count:
            print(f"FSRS: auto-reviewed {reviewed_count} learning(s)", file=sys.stderr)

    except ImportError:
        pass  # fsrs not installed
    except Exception:
        pass  # Never crash the Stop hook


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def dispatch(data):
    """Route to the correct handler based on HOOK_TYPE env var or hook_event field.

    Primary detection: HOOK_TYPE env var set by settings.json command prefix.
    Fallback: hook_event/event/type fields in stdin JSON data.
    Last resort: session_summary heuristic.
    """
    # If tool_name is present, this is PostToolUse (wrong registration)
    if data.get("tool_name") or data.get("tool_input") or data.get("tool_output"):
        return

    # Prime session_id from Claude Code payload so hooks within the same turn
    # share an ID (context_injector surfaces, Stop processes what it surfaced).
    try:
        from config import set_session_id
        set_session_id(data.get("session_id", "") or data.get("sessionId", ""))
    except Exception:
        pass

    # PRIMARY: HOOK_TYPE env var (set by settings.json command prefix)
    hook_event = os.environ.get("HOOK_TYPE", "")

    # FALLBACK: try known field names in stdin JSON data
    if not hook_event:
        hook_event = (data.get("hook_event", "")
                      or data.get("event", "")
                      or data.get("type", "")
                      or os.environ.get("CLAUDE_HOOK_EVENT", ""))

    if hook_event in ("SessionEnd", "session_end"):
        on_session_end(data)
    elif hook_event in ("PreCompact", "pre_compact"):
        on_pre_compact(data)
    elif hook_event in ("Stop", "stop"):
        on_stop_changelog(data)
    elif hook_event in ("SessionStart", "session_start"):
        on_session_start(data)
    else:
        # Last resort heuristic: check if we already logged a session today
        # If yes, this is likely Stop (auto-changelog). If no, SessionStart.
        try:
            conn = get_conn()
            sid = get_session_id()
            existing = conn.execute(
                "SELECT id FROM session_summary WHERE session_id = ?", (sid,)
            ).fetchone()
            conn.close()
            if existing:
                on_stop_changelog(data)
            else:
                on_session_start(data)
        except Exception:
            on_session_start(data)


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    try:
        dispatch(data)
    except Exception as e:
        # Never crash. Print minimal fallback.
        print(f"Session started: {datetime.now().strftime('%Y-%m-%d %H:%M')} (lifecycle hook error: {e})", flush=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
