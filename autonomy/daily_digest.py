"""daily_digest.py — formatted "last 24h ops" snapshot.

Read-only; safe to run any time. Sections: INGESTED, CASCADE, HEALTH, GROWTH,
LEARNINGS, DRIFT CHECK, INJECTION SCAN, INBOX BREACHES, REVIEW BACKLOG,
TRIAGE, OBSERVABILITY.

Pulls from:
- ``sync_summaries`` (last 24h aggregates of ingest deltas)
- ``cascade_log`` (handler counts, classifier outcomes) when present
- ``ingest_rejections``, ``work_queue``
- ``drift_alerts`` (open + suppressed)
- ``action_items`` (WAITING/BLOCKED triage)

Every optional table is feature-detected: sections whose tables are absent
degrade to a note instead of erroring, so the digest works on a fresh install
and grows richer as the operator's pipelines come online.

Run as:
    python daily_digest.py --print     # human-readable text (default)
    python daily_digest.py --json      # JSON payload
    python daily_digest.py --since 36h # custom window
"""
from __future__ import annotations
import paths
import _db  # unified connector (busy_timeout + FK ON)
import config

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(str(paths.DB_PATH))

DEFAULT_WINDOW_HOURS = 24
WAITING_TRIAGE_DAYS = 3


def _connect() -> sqlite3.Connection:
    conn = _db.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def _parse_window(spec: str) -> int:
    m = re.match(r"^(\d+)\s*([hd])?$", spec.strip().lower())
    if not m:
        return DEFAULT_WINDOW_HOURS
    n = int(m.group(1))
    unit = m.group(2) or "h"
    return n * (24 if unit == "d" else 1)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    # Views count too: some sections read views (e.g. v_inbox_breaches).
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone() is not None


def _section_ingested(conn: sqlite3.Connection, since_iso: str) -> dict:
    out = {"emails": 0, "discord": 0, "beeper": 0, "granola": 0, "alias_misflag": 0}
    if _table_exists(conn, "sync_summaries"):
        row = conn.execute(
            """
            SELECT COALESCE(SUM(emails_new),0) e,
                   COALESCE(SUM(discord_new),0) d,
                   COALESCE(SUM(beeper_new),0) b,
                   COALESCE(SUM(granola_new),0) g
              FROM sync_summaries
             WHERE finished_at >= ?
            """,
            (since_iso,),
        ).fetchone()
        out["emails"] = int(row["e"])
        out["discord"] = int(row["d"])
        out["beeper"] = int(row["b"])
        out["granola"] = int(row["g"])

    # Direct fallback (counts new rows, even if no sync_summary yet)
    if out["emails"] == 0:
        out["emails"] = conn.execute(
            "SELECT COUNT(*) n FROM emails WHERE timestamp >= ?", (since_iso,),
        ).fetchone()["n"]
    if out["discord"] == 0:
        out["discord"] = conn.execute(
            "SELECT COUNT(*) n FROM discord_messages WHERE timestamp >= ?", (since_iso,),
        ).fetchone()["n"]
    if out["beeper"] == 0 and _table_exists(conn, "beeper_messages"):
        out["beeper"] = conn.execute(
            "SELECT COUNT(*) n FROM beeper_messages WHERE timestamp >= ?", (since_iso,),
        ).fetchone()["n"]

    # alias misflag count (outbound-flag invariant). OPERATOR_ALIASES is
    # config-driven (audit_tools reads [operator] emails + [org] email_aliases
    # from config.toml); empty until configured, which skips the count.
    try:
        from audit_tools import OPERATOR_ALIASES
        aliases = tuple(a.lower() for a in OPERATOR_ALIASES)
    except Exception:
        aliases = ()
    if aliases:
        placeholders = ",".join(["?"] * len(aliases))
        row = conn.execute(
            f"SELECT COUNT(*) n FROM emails WHERE LOWER(sender_email) IN ({placeholders}) "
            f"AND is_outgoing = 0 AND is_deleted = 0",
            aliases,
        ).fetchone()
        out["alias_misflag"] = int(row["n"])
    return out


def _section_cascade(conn: sqlite3.Connection, since_iso: str) -> dict:
    out = {
        "auto_advanced": 0, "decline": 0, "context_refreshed": 0,
        "dossiers": 0, "extract_created": 0, "extract_rejected": 0,
        "by_handler": {}, "by_class": {},
    }
    # cascade_log is optional (only exists once the operator wires up an
    # automation cascade); the extract counts below come from work_queue.
    if _table_exists(conn, "cascade_log"):
        rows = conn.execute(
            """
            SELECT handler, gemini_class, decisions
              FROM cascade_log
             WHERE timestamp >= ?
            """,
            (since_iso,),
        ).fetchall()
        for r in rows:
            h = r["handler"] or "unknown"
            out["by_handler"][h] = out["by_handler"].get(h, 0) + 1
            gc = r["gemini_class"]
            if gc:
                out["by_class"][gc] = out["by_class"].get(gc, 0) + 1
            if h == "refresh_action_item_context":
                out["context_refreshed"] += 1
            if h == "refresh_dossier":
                out["dossiers"] += 1
            if gc == "CONFIRM":
                out["auto_advanced"] += 1
            elif gc == "DECLINE":
                out["decline"] += 1

    # extract_action_items outcomes from work_queue
    if _table_exists(conn, "work_queue"):
        try:
            wq = conn.execute(
                """
                SELECT status, COUNT(*) n FROM work_queue
                 WHERE handler = 'extract_action_items'
                   AND COALESCE(finished_at, claimed_at, created_at) >= ?
                 GROUP BY status
                """,
                (since_iso,),
            ).fetchall()
            for w in wq:
                if w["status"] == "completed":
                    out["extract_created"] = int(w["n"])
                elif w["status"] == "failed":
                    out["extract_rejected"] = int(w["n"])
        except sqlite3.OperationalError:
            pass
    return out


def _section_health(conn: sqlite3.Connection, since_iso: str) -> dict:
    out = {
        "violations_blocked": 0, "ingest_rejections": 0,
        "handler_failures": 0, "episode_gap_h": None,
        "gate_rejections_24h": 0,
    }
    if _table_exists(conn, "bus_events"):
        out["gate_rejections_24h"] = int(conn.execute(
            "SELECT COUNT(*) FROM bus_events WHERE event_type='gate_rejected' "
            "AND ts >= datetime('now', '-1 day')").fetchone()[0])
    if _table_exists(conn, "ingest_rejections"):
        row = conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN errors LIKE '%state%' OR errors LIKE '%transition%' THEN 1 ELSE 0 END),0) v,
              COUNT(*) total
              FROM ingest_rejections WHERE rejected_at >= ?
            """,
            (since_iso,),
        ).fetchone()
        out["violations_blocked"] = int(row["v"])
        out["ingest_rejections"] = int(row["total"])

    if _table_exists(conn, "work_queue"):
        out["handler_failures"] = int(conn.execute(
            """
            SELECT COUNT(*) n FROM work_queue
             WHERE status = 'failed'
               AND COALESCE(finished_at, claimed_at, created_at) >= ?
            """,
            (since_iso,),
        ).fetchone()["n"])

    if _table_exists(conn, "episodes"):
        row = conn.execute("SELECT MAX(ts) latest FROM episodes").fetchone()
        if row and row["latest"]:
            try:
                t = datetime.fromisoformat(row["latest"].replace("Z", "+00:00"))
                if t.tzinfo:
                    t = t.replace(tzinfo=None)
                out["episode_gap_h"] = round((datetime.now(timezone.utc).replace(tzinfo=None) - t).total_seconds() / 3600, 1)
            except ValueError:
                pass

    # Context Steward heartbeat — surface a dead/stale steward in the brief.
    if _table_exists(conn, "steward_health"):
        sh = conn.execute("SELECT last_tick_at,last_tick_status,last_review_depth,last_staged_pending "
                          "FROM steward_health WHERE id=1").fetchone()
        if sh and sh["last_tick_at"]:
            age_min = None
            try:
                last = datetime.strptime(str(sh["last_tick_at"])[:19], "%Y-%m-%d %H:%M:%S")
                age_min = (datetime.now(timezone.utc).replace(tzinfo=None) - last).total_seconds() / 60
            except ValueError:
                pass
            out["steward"] = {"last_tick_at": sh["last_tick_at"], "age_min": age_min,
                              "stale": age_min is None or age_min > 90, "status": sh["last_tick_status"],
                              "judgment_queue": sh["last_review_depth"], "staged_pending": sh["last_staged_pending"]}
        else:
            out["steward"] = {"last_tick_at": None, "stale": True, "status": "never"}

    # Comms Monitor heartbeat (drafts-only) in the brief.
    if _table_exists(conn, "comms_monitor_health"):
        cm = conn.execute("SELECT last_tick_at,status FROM comms_monitor_health WHERE id=1").fetchone()
        ready = conn.execute("SELECT COUNT(*) FROM comms_drafts WHERE status='draft'").fetchone()["COUNT(*)"] \
            if _table_exists(conn, "comms_drafts") else 0
        if cm and cm["last_tick_at"]:
            age_min = None
            try:
                age_min = (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.strptime(str(cm["last_tick_at"])[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
            except ValueError:
                pass
            _st = str(cm["status"] or "").lower()
            # A deliberately paused monitor is neither stale nor degraded --
            # it heartbeats 'paused' every tick. Only silence is stale.
            out["comms_monitor"] = {"last_tick_at": cm["last_tick_at"], "stale": age_min is None or age_min > 30,
                                    "status": cm["status"],
                                    "paused": _st == "paused",
                                    "degraded": _st not in ("ok", "", "paused"),
                                    "ready_drafts": ready}
        else:
            out["comms_monitor"] = {"last_tick_at": None, "stale": True, "status": "never", "ready_drafts": ready}
    return out


def _section_drift(conn: sqlite3.Connection) -> dict:
    if not _table_exists(conn, "drift_alerts"):
        return {"open": [], "suppressed": []}
    open_rows = conn.execute(
        """
        SELECT alert_type, severity, summary, fire_count, first_fired_at, suppressed
          FROM drift_alerts
         WHERE resolved_at IS NULL
         ORDER BY CASE severity WHEN 'crit' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                  last_fired_at DESC
        """
    ).fetchall()
    return {
        "open": [dict(r) for r in open_rows if not r["suppressed"]],
        "suppressed": [dict(r) for r in open_rows if r["suppressed"]],
    }


# Prompt-injection scan findings surfaced from the log-only hook.
# hooks/injection_scanner.py appends CRITICAL/WARNING findings to this file (it is
# log-only, never emits to model context). The digest surfaces ONLY genuine
# external-channel CRITICALs -- MCP tool output (email/chat bridges/etc.).
# Self-referential Read/Bash/WebFetch/WebSearch findings are excluded: reading a
# file full of injection vocabulary (the scanner's own patterns, say) fires the
# scanner but is not an attack surface worth a digest line.
_INJECTION_LOG = Path(str(paths.PLAN_STATE_DIR / "injection_scanner_findings.log"))
_INJ_ENTRY_RE = re.compile(r"^=====\s*(?P<ts>\S+)\s*=====\s*$")
_INJ_SEV_RE = re.compile(r"PROMPT INJECTION SCAN\s*[—-]\s*(?P<sev>CRITICAL|WARNING)")
_INJ_TOOL_RE = re.compile(r"^Tool:\s*(?P<tool>\S+)", re.MULTILINE)


def _section_injection(window_hours: int) -> dict:
    """Critical prompt-injection findings from MCP tool output within the
    window. Filters to severity=CRITICAL AND tool startswith 'mcp__' (the
    external data plane); excludes self-referential Read/Bash/WebFetch/WebSearch.

    Timestamps in the log are naive LOCAL time (datetime.now().isoformat()), so the
    cutoff is computed in local time to match, independent of the digest's UTC math.
    """
    out = {"available": _INJECTION_LOG.exists(), "critical_count": 0, "by_tool": {}, "categories": []}
    if not out["available"]:
        return out
    try:
        raw = _INJECTION_LOG.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - defensive
        out["available"] = False
        out["error"] = str(exc)
        return out
    cutoff = datetime.now() - timedelta(hours=window_hours)
    cats: set[str] = set()
    # Split into per-finding blocks on the ===== timestamp ===== delimiter.
    blocks = re.split(r"(?m)^=====\s*(\S+)\s*=====\s*$", raw)
    # re.split with one capture group yields [pre, ts1, body1, ts2, body2, ...]
    for i in range(1, len(blocks) - 1, 2):
        ts_raw, body = blocks[i], blocks[i + 1]
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        sev_m = _INJ_SEV_RE.search(body)
        if not sev_m or sev_m.group("sev") != "CRITICAL":
            continue
        tool_m = _INJ_TOOL_RE.search(body)
        tool = tool_m.group("tool") if tool_m else "?"
        if not tool.startswith("mcp__"):
            continue  # exclude self-referential Read/Bash/WebFetch/WebSearch
        out["critical_count"] += 1
        out["by_tool"][tool] = out["by_tool"].get(tool, 0) + 1
        cm = re.search(r"^Categories:\s*(.+)$", body, re.MULTILINE)
        if cm:
            for c in cm.group(1).split(","):
                cats.add(c.strip())
    out["categories"] = sorted(cats)
    return out


def _section_inbox(conn: sqlite3.Connection) -> dict:
    """SLA-breached email threads grouped by stakeholder tier (last 60d
    inbound only).

    Tier 0 (manager / executive contact): every breach listed.
    Tier 1 (external partners): top 10.
    Tier 2 (collaborators / internal): top 10.
    Tier 3 (general contacts): count only.
    Tier 4 (calendar / low-signal): never breached (no SLA).
    """
    if not _table_exists(conn, "email_threads"):
        return {"available": False, "by_tier": {}}
    try:
        rows = conn.execute(
            "SELECT thread_id, lane, stakeholder_tier, subject, age_hours, "
            "       person_name_cached, last_sender_email "
            "  FROM v_inbox_breaches "
            " ORDER BY stakeholder_tier, age_hours DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"available": False, "by_tier": {}}
    by_tier: dict[int, list[dict]] = {0: [], 1: [], 2: [], 3: [], 4: []}
    for r in rows:
        d = dict(r)
        tier = d.get("stakeholder_tier")
        if tier is None:
            continue
        by_tier.setdefault(tier, []).append(d)
    return {"available": True, "by_tier": by_tier}


# Tier labels are display vocabulary, not logic. Override per-tier in
# config.toml (FICTIONAL example):
#   # [inbox.tier_labels]
#   # "0" = "CEO (4h SLA)"
#   # "1" = "Clients (24h)"
_DEFAULT_TIER_LABELS = {
    0: "Manager (4h SLA)",
    1: "Partners / payment contacts (24h)",
    2: "Collaborators / internal (24h)",
    3: "General contacts (72h)",
}


def _tier_labels() -> dict[int, str]:
    labels = dict(_DEFAULT_TIER_LABELS)
    cfg = config.get("inbox.tier_labels", {}) or {}
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            try:
                labels[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
    return labels


def _section_review_backlog(conn: sqlite3.Connection) -> dict:
    """Pending review_queue + merge_candidates (+ extraction staging, when that
    layer is installed) backlog.

    Surfaces the human-review queue counts so they don't grow silently.
    Does NOT auto-decide; just lights up the dial.
    """
    out = {
        "review_queue": {},
        "merge_candidates": {},
        "thread_claims_staging": {},
    }
    if _table_exists(conn, "review_queue"):
        for r in conn.execute(
            "SELECT queue_type, COUNT(*) FROM review_queue WHERE status='pending' GROUP BY queue_type"
        ).fetchall():
            out["review_queue"][r[0]] = int(r[1])
    if _table_exists(conn, "merge_candidates"):
        for r in conn.execute(
            "SELECT signal, COUNT(*) FROM merge_candidates WHERE status='pending' GROUP BY signal"
        ).fetchall():
            out["merge_candidates"][r[0]] = int(r[1])
    if _table_exists(conn, "thread_claims_staging"):
        out["thread_claims_staging"]["pending"] = conn.execute(
            "SELECT COUNT(*) FROM thread_claims_staging WHERE status='pending'"
        ).fetchone()[0]
    return out


def _section_triage(conn: sqlite3.Connection) -> list[dict]:
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=WAITING_TRIAGE_DAYS)).isoformat()
    routing_v2 = os.environ.get("ROUTING_V2", "0") == "1"
    include_personal = os.environ.get("DIGEST_INCLUDE_PERSONAL", "0") == "1"
    domain_filter = (
        " AND COALESCE(domain,'general') IN ('work','public','general')"
        if routing_v2 and not include_personal else ""
    )
    rows = conn.execute(
        f"""
        SELECT item_id, status, priority, description, waiting_on, updated_at, inserted_at
          FROM action_items
         WHERE status IN ('WAITING','BLOCKED')
           AND COALESCE(updated_at, inserted_at) < ?
           {domain_filter}
         ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                  COALESCE(updated_at, inserted_at)
         LIMIT 8
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def build_payload(window_hours: int = DEFAULT_WINDOW_HOURS) -> dict:
    since = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    conn = _connect()
    try:
        return {
            "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
            "window_hours": window_hours,
            "ingested": _section_ingested(conn, since),
            "cascade": _section_cascade(conn, since),
            "health": _section_health(conn, since),
            "drift": _section_drift(conn),
            "injection": _section_injection(window_hours),
            "inbox": _section_inbox(conn),
            "review_backlog": _section_review_backlog(conn),
            "triage": _section_triage(conn),
            "learning_health": _section_learning_health(conn),
            "growth": _section_growth(conn),
            "sync_timings": _section_sync_timings(conn),
            "observability": _sections_observability(conn),
        }
    finally:
        conn.close()


def _section_sync_timings(conn: sqlite3.Connection, factor: float = 3.0) -> dict:
    """Slow-source rule: per source, WARN when the latest sync elapsed_ms
    exceeds `factor` x its trailing-14-day median. Empty until sync_source_timings
    has data. Read-only."""
    out: dict = {"warnings": [], "sources": []}
    if not _table_exists(conn, "sync_source_timings"):
        out["note"] = "sync_source_timings not present"
        return out
    try:
        srcs = [r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM sync_source_timings "
            "WHERE created_at >= datetime('now','-14 days')").fetchall()]
        for src in srcs:
            vals = [r[0] for r in conn.execute(
                "SELECT elapsed_ms FROM sync_source_timings WHERE source=? "
                "AND created_at >= datetime('now','-14 days') AND elapsed_ms IS NOT NULL "
                "ORDER BY created_at", (src,)).fetchall()]
            if not vals:
                continue
            latest = vals[-1]
            s = sorted(vals)
            n = len(s)
            median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
            row = {"source": src, "latest_ms": latest, "median_ms": round(median, 1), "n": n}
            out["sources"].append(row)
            if median > 0 and latest > factor * median and n >= 3:
                out["warnings"].append(
                    f"{src}: last sync {latest} ms > {factor}x median {median:.0f} ms (n={n})")
    except sqlite3.Error as e:
        out["error"] = str(e)[:120]
    return out


def _section_growth(conn: sqlite3.Connection) -> dict:
    """Top-10 rowcount deltas vs the prior table_rowcounts snapshot + DB file
    size (growth gauge). Empty until two nightly snapshots exist."""
    out: dict = {"deltas": [], "db_bytes": None}
    try:
        out["db_bytes"] = os.path.getsize(str(paths.DB_PATH))
    except OSError:
        pass
    try:
        stamps = [r[0] for r in conn.execute(
            "SELECT DISTINCT substr(captured_at,1,10) FROM table_rowcounts "
            "ORDER BY 1 DESC LIMIT 2").fetchall()]
        if len(stamps) < 2:
            out["note"] = "need 2 snapshots (%d so far)" % len(stamps)
            return out
        cur, prev = stamps[0], stamps[1]
        rows = conn.execute("""
            SELECT c.table_name, p.rows, c.rows, c.rows - p.rows AS delta
            FROM (SELECT table_name, MAX(rows) rows FROM table_rowcounts
                  WHERE substr(captured_at,1,10)=? GROUP BY table_name) c
            JOIN (SELECT table_name, MAX(rows) rows FROM table_rowcounts
                  WHERE substr(captured_at,1,10)=? GROUP BY table_name) p
              ON p.table_name = c.table_name
            WHERE c.rows != p.rows
            ORDER BY ABS(c.rows - p.rows) DESC LIMIT 10""", (cur, prev)).fetchall()
        out["deltas"] = [tuple(r) for r in rows]
        out["window"] = "%s vs %s" % (cur, prev)
    except sqlite3.Error as e:
        out["error"] = str(e)[:120]
    return out


# ---------------------------------------------------------------------------
# Observability sections: each asserted to emit >= 1 rendered line against
# live data ("0 items (clean)" counts as emitting). Verify with
# --sections-check.
# ---------------------------------------------------------------------------

def _sections_observability(conn: sqlite3.Connection) -> dict:
    out: dict = {}
    # 1. silent-done
    try:
        n = conn.execute("SELECT COUNT(*) FROM silent_done_candidates WHERE status='pending'").fetchone()[0] \
            if _table_exists(conn, "silent_done_candidates") else -1
        top = conn.execute(
            "SELECT item_id FROM silent_done_candidates WHERE status='pending' LIMIT 3").fetchall() if n > 0 else []
        out["silent_done"] = {"pending": n, "top": [r[0] for r in top]}
    except Exception as e:
        out["silent_done"] = {"error": str(e)[:120]}
    # 2. drift remediation
    try:
        opened = conn.execute(
            "SELECT COUNT(*) FROM action_items_inbox WHERE source='audit-drift-reconcile' AND status='pending'"
        ).fetchone()[0]
        alerts = conn.execute(
            "SELECT COUNT(*) FROM drift_alerts WHERE resolved_at IS NULL AND COALESCE(suppressed,0)=0"
        ).fetchone()[0] if _table_exists(conn, "drift_alerts") else -1
        out["drift_remediation"] = {"inbox_pending": opened, "alerts_open": alerts}
    except Exception as e:
        out["drift_remediation"] = {"error": str(e)[:120]}
    # 3. gate scoreboard (view created by the write-gate machinery; optional)
    try:
        if _table_exists(conn, "v_gate_scoreboard_7d"):
            rows = conn.execute(
                "SELECT table_name, actor, writes_7d, rung FROM v_gate_scoreboard_7d "
                "ORDER BY writes_7d DESC LIMIT 5").fetchall()
            null_actors = conn.execute(
                "SELECT COALESCE(SUM(writes_7d),0) FROM v_gate_scoreboard_7d WHERE actor='<null>'").fetchone()[0]
            out["gate_scoreboard"] = {"top": [tuple(r) for r in rows], "null_actor_writes_7d": null_actors}
        else:
            out["gate_scoreboard"] = {"note": "v_gate_scoreboard_7d view not present"}
    except Exception as e:
        out["gate_scoreboard"] = {"error": str(e)[:120]}
    # 4. staging quarantine
    try:
        n = conn.execute("SELECT COUNT(*) FROM staging WHERE status='quarantined'").fetchone()[0]
        out["staging_quarantine"] = {"quarantined": n}
    except Exception as e:
        out["staging_quarantine"] = {"error": str(e)[:120]}
    # 4b. extraction health: thread_extractions_staging by status. Only when
    # the claim-extraction layer (not part of this starter kit) is installed.
    try:
        if _table_exists(conn, "thread_extractions_staging"):
            by = dict(conn.execute(
                "SELECT status, COUNT(*) FROM thread_extractions_staging GROUP BY status").fetchall())
            out["extraction_health"] = {"by_status": by,
                                        "failed": by.get("failed", 0),
                                        "parked": by.get("parked", 0)}
        else:
            out["extraction_health"] = {"note": "extraction layer not included in this starter kit"}
    except Exception as e:
        out["extraction_health"] = {"error": str(e)[:120]}
    # 5. dead_letter
    try:
        n7 = conn.execute("SELECT COUNT(*) FROM dead_letter WHERE created_at >= datetime('now','-7 day')").fetchone()[0] \
            if _table_exists(conn, "dead_letter") else -1
        total = conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0] if n7 >= 0 else -1
        out["dead_letter"] = {"last_7d": n7, "total": total}
    except Exception as e:
        out["dead_letter"] = {"error": str(e)[:120]}
    # 6. heartbeat staleness (dead-man checker). JOB_SPECS comes from the
    # optional job_heartbeat module; without it, fall back to the distinct
    # jobs already present in job_heartbeats with a 24h default interval.
    try:
        try:
            from job_heartbeat import JOB_SPECS  # {job: (interval_min, min_rows)}
            specs = dict(JOB_SPECS)
        except ImportError:
            specs = {r[0]: (1440, 0) for r in conn.execute(
                "SELECT DISTINCT job FROM job_heartbeats").fetchall()} \
                if _table_exists(conn, "job_heartbeats") else {}
        jobs = []
        for job, (interval, min_rows) in sorted(specs.items()):
            row = conn.execute(
                "SELECT started_at, finished_at, exit_note, rows_touched FROM job_heartbeats "
                "WHERE job=? ORDER BY id DESC LIMIT 1", (job,)).fetchone()
            if row is None:
                jobs.append({"job": job, "state": "NEVER-SEEN"})
                continue
            age_min = conn.execute(
                "SELECT (julianday('now') - julianday(?)) * 1440", (row[0],)).fetchone()[0]
            if age_min > 2 * interval:
                state = "RED (silent %dm > 2x%dm)" % (age_min, interval)
            elif (row[3] or 0) == 0 and min_rows > 0:
                state = "WARN (up-but-empty)"
            else:
                state = "ok"
            jobs.append({"job": job, "state": state, "last": row[0], "note": row[2]})
        out["heartbeat_staleness"] = {"jobs": jobs}
        if not jobs:
            out["heartbeat_staleness"]["note"] = "no heartbeats recorded yet"
    except Exception as e:
        out["heartbeat_staleness"] = {"error": str(e)[:120]}
    # 7. VEC DRIFT (fork detector): main-schema vec0 beyond allowed + vecdb-vs-ledger deltas
    try:
        main_vec0 = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE sql LIKE '%USING vec0%'").fetchone()[0]
        deltas = []
        vec_path = Path(str(paths.VEC_DB_PATH))
        if vec_path.exists() and _table_exists(conn, "vec_freshness"):
            vconn = sqlite3.connect(f"file:{vec_path.as_posix()}?mode=ro", uri=True)
            try:
                ledger = {r[0]: r[1] for r in conn.execute(
                    "SELECT table_name, rows_embedded FROM vec_freshness")}
                for (tbl,) in vconn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'vec\\_%' ESCAPE '\\' AND sql LIKE '%vec0%'"):
                    modality = tbl[4:]
                    try:
                        cnt = vconn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                    except sqlite3.OperationalError:
                        cnt = -1  # vec0 ext not loaded on ro conn; shadow-count fallback
                        try:
                            cnt = vconn.execute(f"SELECT COUNT(*) FROM {tbl}_rowids").fetchone()[0]
                        except Exception:
                            pass
                    led = ledger.get(modality)
                    pct = None
                    if led and cnt >= 0:
                        pct = abs(cnt - led) / max(led, 1) * 100
                    deltas.append({"modality": modality, "vecdb": cnt, "ledger": led,
                                   "state": ("RED" if pct is not None and pct > 5 else "ok")})
            finally:
                vconn.close()
        out["vec_drift"] = {"main_vec0_beyond_allowed": main_vec0, "deltas": deltas}
    except Exception as e:
        out["vec_drift"] = {"error": str(e)[:120]}
    return out


def render_observability(obs: dict) -> list[str]:
    L: list[str] = []
    L.append("")
    L.append("OBSERVABILITY")
    sd = obs.get("silent_done", {})
    if sd.get("pending", -1) == -1:
        L.append("  silent-done:        (silent_done_candidates table absent)")
    else:
        L.append("  silent-done:        %s pending%s" % (
            sd.get("pending", "?"), (" (top: %s)" % ", ".join(map(str, sd["top"]))) if sd.get("top") else " (clean)" if sd.get("pending") == 0 else ""))
    dr = obs.get("drift_remediation", {})
    L.append("  drift remediation:  %s inbox proposals pending, %s alerts open" % (
        dr.get("inbox_pending", "?"), dr.get("alerts_open", "?")))
    gs = obs.get("gate_scoreboard", {})
    if "note" in gs:
        L.append("  gate scoreboard:    (%s)" % gs["note"])
    else:
        L.append("  gate scoreboard:    null-actor writes 7d = %s; top: %s" % (
            gs.get("null_actor_writes_7d", "?"),
            "; ".join("%s/%s=%s(%s)" % t for t in gs.get("top", [])[:3]) or "none"))
    sq = obs.get("staging_quarantine", {})
    L.append("  staging quarantine: %s rows%s" % (sq.get("quarantined", "?"), " (clean)" if sq.get("quarantined") == 0 else ""))
    eh = obs.get("extraction_health", {})
    if "note" in eh:
        L.append("  extraction health:  (%s)" % eh["note"])
    else:
        _eh_by = eh.get("by_status", {})
        L.append("  extraction health:  failed=%s parked=%s (%s)" % (
            eh.get("failed", "?"), eh.get("parked", "?"),
            ", ".join("%s=%s" % kv for kv in sorted(_eh_by.items())) or eh.get("error", "no data")))
    dl = obs.get("dead_letter", {})
    L.append("  dead_letter:        %s last 7d / %s total" % (dl.get("last_7d", "?"), dl.get("total", "?")))
    hb = obs.get("heartbeat_staleness", {})
    for j in hb.get("jobs", []):
        L.append("  heartbeat %-22s %s" % (j["job"] + ":", j["state"]))
    if not hb.get("jobs"):
        L.append("  heartbeat staleness: %s" % (hb.get("note") or hb.get("error", "no data")))
    vd = obs.get("vec_drift", {})
    reds = [d for d in vd.get("deltas", []) if d.get("state") == "RED"]
    L.append("  vec drift:          main-vec0=%s (allowed 0); %d modalities checked, %d RED%s" % (
        vd.get("main_vec0_beyond_allowed", "?"), len(vd.get("deltas", [])), len(reds),
        (" -> " + ", ".join("%s vecdb=%s ledger=%s" % (d["modality"], d["vecdb"], d["ledger"]) for d in reds)) if reds else ""))
    return L


def _section_learning_health(conn) -> dict:
    """Learnings-loop health (adherence coverage, embed currency, Tier-A, consolidation).
    Fail-open: a missing module/table must never break the digest."""
    try:
        import learning_health as _lh
        return _lh.summary(conn)
    except Exception:
        return {}


def render_text(p: dict) -> str:
    L: list[str] = []
    when = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%b %d, %H:%M UTC")
    L.append(f"=== Last {p['window_hours']}h ops digest ({when}) ===")
    L.append("")
    L.append("INGESTED")
    ing = p["ingested"]
    misflag = ing.get("alias_misflag", 0)
    misflag_tag = f"  (+{misflag} alias-misflag, regression!)" if misflag else "  (+ 0 alias-misflag, invariant holding)"
    L.append(f"  emails:   {ing['emails']:>4} new{misflag_tag}")
    L.append(f"  discord:  {ing['discord']:>4} new")
    L.append(f"  beeper:   {ing['beeper']:>4} new")
    L.append(f"  granola:  {ing['granola']:>4} transcripts")
    L.append("")
    L.append("CASCADE (automation handlers)")
    c = p["cascade"]
    confirm = c["by_class"].get("CONFIRM", 0)
    decline = c["by_class"].get("DECLINE", 0)
    L.append(f"  action_items auto-advanced:   {confirm} (CONFIRM), {decline} (DECLINE)")
    L.append(f"  action_items context-refreshed: {c['context_refreshed']}")
    L.append(f"  dossiers refreshed: {c['dossiers']} people")
    L.append(
        f"  extract_action_items: {c['extract_created']} created, {c['extract_rejected']} rejected"
    )
    cmh = c.get("comms_monitor") or {}
    if cmh:
        if cmh.get("paused"):
            cm_tag = "PAUSED (deliberate; heartbeat fresh)" if not cmh.get("stale") else "PAUSED but heartbeat stale!"
        elif cmh.get("stale"):
            cm_tag = "STALE/DEAD"
        else:
            cm_tag = cmh.get("status") or "?"
        L.append(f"  comms monitor: {cm_tag}  (last tick {cmh.get('last_tick_at') or 'never'}, ready drafts {cmh.get('ready_drafts', 0)})")
    L.append("")
    L.append("HEALTH")
    h = p["health"]
    L.append(f"  state-machine violations blocked: {h['violations_blocked']}")
    L.append(f"  ingest rejections (24h): {h['ingest_rejections']}")
    L.append(f"  gate rejections (24h): {h.get('gate_rejections_24h', 0)}")
    L.append(f"  handler failures: {h['handler_failures']}")
    if h["episode_gap_h"] is not None:
        gap_tag = "healthy" if h["episode_gap_h"] < 6 else "STALE" if h["episode_gap_h"] < 24 else "VERY STALE"
        L.append(f"  episode gap: {h['episode_gap_h']}h ({gap_tag})")
    else:
        L.append("  episode gap: (no episodes table)")
    L.append("")
    g = p.get("growth") or {}
    L.append("GROWTH (top table deltas, nightly snapshots)")
    if g.get("db_bytes"):
        L.append("  %s: %.2f GB" % (DB_PATH.name, g["db_bytes"] / 1e9))
    if g.get("deltas"):
        for name, prev_n, cur_n, delta in g["deltas"]:
            L.append("  %-32s %+d (%d -> %d)" % (name, delta, prev_n, cur_n))
    else:
        L.append("  %s" % (g.get("note") or g.get("error") or "no deltas"))
    L.append("")
    lh = p.get("learning_health") or {}
    if lh:
        emb = f"{lh.get('embed_pct')}%" if lh.get("embed_pct") is not None else "?"
        L.append("LEARNINGS")
        L.append(f"  active {lh.get('active', '?')}, embed {emb}, "
                 f"adherence {lh.get('adherence_pct', 0)}% ({lh.get('adherence_judged', 0)}/{lh.get('adherence_total', 0)})")
        L.append(f"  Tier-A {lh.get('tier_a', '?')}/{lh.get('tier_a_cap', 40)}"
                 + ("" if lh.get("tier_a_ok", True) else "  (OVER CAP!)")
                 + f", consolidation {lh.get('consolidation_proposed', 0)} proposed / {lh.get('consolidation_conflict', 0)} conflict")
        L.append("")
    L.append("DRIFT CHECK")
    d = p["drift"]
    if not d["open"]:
        sup = f" ({len(d['suppressed'])} suppressed)" if d["suppressed"] else ""
        L.append(f"  OK - no alerts{sup}")
    else:
        for a in d["open"]:
            L.append(f"  [{a['severity'].upper()}] {a['alert_type']}: {a['summary']} (since {a['first_fired_at']}, fires={a['fire_count']})")
    L.append("")
    L.append("INJECTION SCAN (external tool output, MCP CRITICALs)")
    inj = p.get("injection") or {}
    if not inj.get("available"):
        L.append("  (no injection_scanner findings log yet)")
    elif not inj.get("critical_count"):
        L.append("  OK - no critical injection findings in MCP tool output")
    else:
        cats = (", ".join(inj.get("categories", [])) or "?")[:80]
        L.append(f"  {inj['critical_count']} CRITICAL finding(s) [{cats}] - see injection_scanner_findings.log")
        for tool, n in sorted(inj.get("by_tool", {}).items(), key=lambda x: -x[1])[:5]:
            L.append(f"    {tool}: {n}")
    L.append("")
    L.append("INBOX BREACHES (last 60d, SLA-overdue)")
    ib = p.get("inbox") or {}
    if not ib.get("available"):
        L.append("  (inbox_triage layer not initialised - run inbox_triage.py classify --all)")
    else:
        by_tier = ib["by_tier"]
        tier_names = _tier_labels()
        any_breach = False
        for tier in (0, 1, 2, 3):
            items = by_tier.get(tier, [])
            if not items:
                continue
            any_breach = True
            n = len(items)
            label = tier_names.get(tier, f"Tier {tier}")
            if tier == 0:
                L.append(f"  Tier 0 - {label}: {n} breached")
                for it in items:
                    age = it["age_hours"]
                    age_s = f"{age}h" if age < 48 else f"{age // 24}d"
                    who = it.get("person_name_cached") or it.get("last_sender_email") or "?"
                    L.append(f"    [{age_s}] {who[:30]} | {(it.get('subject') or '')[:70]}")
            elif tier in (1, 2):
                L.append(f"  Tier {tier} - {label}: {n} breached" + (", top 10:" if n > 10 else ":"))
                for it in items[:10]:
                    age = it["age_hours"]
                    age_s = f"{age}h" if age < 48 else f"{age // 24}d"
                    who = it.get("person_name_cached") or it.get("last_sender_email") or "?"
                    L.append(f"    [{age_s}] {who[:30]} | {(it.get('subject') or '')[:70]}")
                if n > 10:
                    L.append(f"    ... ({n - 10} more - query.py inbox)")
            else:  # tier 3
                L.append(f"  Tier 3 - {label}: {n} breached (query.py inbox --breaches for the list)")
        if not any_breach:
            L.append("  OK - no SLA breaches")
    L.append("")
    L.append("REVIEW BACKLOG (human triage queues)")
    rb = p.get("review_backlog") or {}
    rq = rb.get("review_queue") or {}
    mc = rb.get("merge_candidates") or {}
    tcs = (rb.get("thread_claims_staging") or {}).get("pending", 0)
    rq_total = sum(rq.values())
    mc_total = sum(mc.values())
    if rq_total or mc_total or tcs:
        if rq:
            L.append(f"  review_queue (pending): {rq_total}  " + ", ".join(f"{k}={v}" for k, v in sorted(rq.items(), key=lambda x: -x[1])))
        if mc:
            L.append(f"  merge_candidates (pending): {mc_total}  " + ", ".join(f"{k}={v}" for k, v in sorted(mc.items(), key=lambda x: -x[1])))
        if tcs:
            L.append(f"  thread_claims_staging pending: {tcs}")
    else:
        L.append("  OK - all review queues at 0")
    L.append("")
    L.append("TRIAGE (needs you)")
    if not p["triage"]:
        L.append("  (nothing past 3-day WAITING threshold)")
    else:
        for t in p["triage"]:
            wait = (t["waiting_on"] or "")[:30]
            tag = f"[{t['status']}]" if t["status"] != "WAITING" else ""
            L.append(f"  {t['item_id']}: {(t['description'] or '')[:90]} {tag}".rstrip())
            if wait:
                L.append(f"    waiting_on: {wait}")
    # TOIL: proposed toil_candidates need a review surface or they rot unseen.
    try:
        import sqlite3 as _sqt
        _ct = _sqt.connect(str(DB_PATH), timeout=15)
        _ct.execute("PRAGMA busy_timeout=15000")
        _toil = _ct.execute(
            "SELECT COUNT(*) FROM toil_candidates WHERE status='proposed'").fetchone()[0]
        if _toil:
            L.append("")
            L.append("TOIL CANDIDATES (%d proposed; review during weekend maintenance)" % _toil)
            for r in _ct.execute(
                    "SELECT substr(COALESCE(candidate,''),1,90) FROM toil_candidates"
                    " WHERE status='proposed' ORDER BY id DESC LIMIT 3"):
                L.append("  - %s" % r[0])
        _ct.close()
    except Exception:
        pass
    L.extend(render_observability(p.get("observability") or {}))
    return "\n".join(L)


def _cli() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]

    window = DEFAULT_WINDOW_HOURS
    fmt = "print"
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--print":
            fmt = "print"
        elif a == "--json":
            fmt = "json"
        elif a == "--sections-check":
            fmt = "sections-check"
        elif a == "--since" and i + 1 < len(args):
            window = _parse_window(args[i + 1]); i += 1
        else:
            print(f"unknown arg: {a}", file=sys.stderr)
            return 2
        i += 1

    payload = build_payload(window)
    if fmt == "json":
        print(json.dumps(payload, indent=2, default=str))
    elif fmt == "sections-check":
        # machine check: each observability section must emit >= 1 line
        obs = payload.get("observability") or {}
        lines = render_observability(obs)
        keys = ["silent_done", "drift_remediation", "gate_scoreboard", "staging_quarantine",
                "dead_letter", "heartbeat_staleness", "vec_drift"]
        bad = 0
        for k in keys:
            sec = obs.get(k, {})
            emitted = bool(sec) and "error" not in sec
            if k == "heartbeat_staleness":
                emitted = bool(sec.get("jobs")) or "note" in sec
            print(f"  section {k:22s} {'EMITS' if emitted else 'SILENT/ERROR: ' + str(sec.get('error','empty'))}")
            bad += 0 if emitted else 1
        print(f"sections emitting: {len(keys) - bad}/{len(keys)} (rendered {len(lines)} lines)")
        return 1 if bad else 0
    else:
        print(render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
