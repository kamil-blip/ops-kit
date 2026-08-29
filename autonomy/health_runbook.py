"""Self-service debug CLI.

Three subcommands, dispatched from tools/query.py:

    python query.py health              # system-wide snapshot (sync freshness,
                                        # action items, ingest, work queue,
                                        # drift, steward, comms monitor,
                                        # learnings)
    python query.py explain AI-xxx      # full timeline for one action_item
                                        # (related people, thread, next action)
    python query.py runbook [topic]     # print runbook recipe from debug_tasks
                                        # (no topic = list)

Can also be invoked directly as a script:
    python health_runbook.py health
    python health_runbook.py explain AI-xxx
    python health_runbook.py runbook duplicate-reply
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paths
import _db  # unified connector (busy_timeout + FK ON)
import config

# Windows encoding fix: reconfigure IN PLACE (never swap the stream object --
# replacing sys.stdout at import time discards the importer's unflushed output
# and breaks embedders whose streams have no .buffer). Module-level on purpose:
# tools/query.py imports this module and relies on utf-8-safe printing.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, io.UnsupportedOperation):
    pass

DB_PATH = Path(str(paths.DB_PATH))


def _connect() -> sqlite3.Connection:
    conn = _db.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        try:
            t = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


def _hours_ago(raw: str | None) -> float | None:
    t = _parse_ts(raw)
    if not t:
        return None
    return (_utcnow() - t).total_seconds() / 3600


def _fmt_hours(h: float | None) -> str:
    if h is None:
        return "?"
    if h < 24:
        return f"{h:.1f}h"
    return f"{h/24:.1f}d"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# ═══════════════════════════════════════════════════════════════════════════
# query.py health — system snapshot
# ═══════════════════════════════════════════════════════════════════════════

def _health_sync(conn: sqlite3.Connection) -> list[tuple[str, float | None, str]]:
    """Returns (label, hours_ago, status_tag) per source."""
    rows: list[tuple[str, float | None, str]] = []

    email_last = conn.execute("SELECT MAX(timestamp) t FROM emails").fetchone()["t"]
    rows.append(("emails", _hours_ago(email_last), _tag_fresh(email_last, 24)))

    dsc_last = conn.execute("SELECT MAX(timestamp) t FROM discord_messages").fetchone()["t"]
    rows.append(("discord", _hours_ago(dsc_last), _tag_fresh(dsc_last, 24)))

    if _table_exists(conn, "beeper_messages"):
        bp = conn.execute("SELECT MAX(timestamp) t FROM beeper_messages").fetchone()["t"]
        rows.append(("beeper", _hours_ago(bp), _tag_fresh(bp, 24)))
        # Per-network health: read the attempt/success ledger (sync_state
        # beeper:<net>:attempt/:success) instead of message recency -- a QUIET
        # channel with healthy sync attempts is OK; only a failing/absent
        # ATTEMPT warns. Falls back to the old message-recency gauge when the
        # ledger has no rows yet.
        try:
            ledger = {r["source"]: r["last_sync"] for r in conn.execute(
                "SELECT source, last_sync FROM sync_state WHERE source LIKE 'beeper:%'")}
            nets = sorted({s.split(":")[1] for s in ledger})
            for net in nets:
                att = ledger.get(f"beeper:{net}:attempt")
                suc = ledger.get(f"beeper:{net}:success")
                att_h = _hours_ago(att)
                if att_h is None or att_h > 48:
                    rows.append((f"beeper:{net}", att_h, "WARN (no sync attempt)"))
                elif suc is None or (_hours_ago(suc) or 999) > att_h + 1:
                    rows.append((f"beeper:{net}", _hours_ago(suc), "WARN (attempts not succeeding)"))
                else:
                    rows.append((f"beeper:{net}", _hours_ago(suc), "OK (ledger)"))
            if not nets:
                for net, nt in conn.execute(
                    "SELECT lower(network) n, MAX(timestamp) t FROM beeper_messages "
                    "WHERE network IS NOT NULL AND lower(network) IN ('slack','signal') "
                    "GROUP BY lower(network) ORDER BY n").fetchall():
                    rows.append((f"beeper:{net}", _hours_ago(nt), _tag_fresh(nt, 48)))
        except sqlite3.OperationalError:
            pass

    # Meeting transcripts: stored as reference_docs with slug transcript-*
    try:
        g = conn.execute(
            "SELECT MAX(updated_at) t FROM reference_docs WHERE slug LIKE 'transcript-%'"
        ).fetchone()["t"]
        rows.append(("transcripts", _hours_ago(g), _tag_fresh(g, 48)))
    except sqlite3.OperationalError:
        pass

    if _table_exists(conn, "episodes"):
        ep = conn.execute("SELECT MAX(ts) t FROM episodes").fetchone()["t"]
        rows.append(("episodes", _hours_ago(ep), _tag_fresh(ep, 24)))

    return rows


def _tag_fresh(raw: str | None, threshold_hours: float) -> str:
    h = _hours_ago(raw)
    if h is None:
        return "UNKNOWN"
    if h < threshold_hours:
        return "OK"
    if h < threshold_hours * 2:
        return "WARN"
    return "STALE"


def _health_action_items(conn: sqlite3.Connection) -> dict:
    out = {"p0": 0, "p1": 0, "waiting_stale": [], "blocked": 0}

    p = conn.execute(
        "SELECT priority, COUNT(*) n FROM action_items "
        "WHERE status='OPEN' GROUP BY priority"
    ).fetchall()
    for r in p:
        if r["priority"] == "P0":
            out["p0"] = int(r["n"])
        elif r["priority"] == "P1":
            out["p1"] = int(r["n"])

    cutoff = (_utcnow() - timedelta(days=3)).isoformat(timespec="seconds")
    stale = conn.execute(
        "SELECT item_id FROM action_items "
        "WHERE status='WAITING' AND COALESCE(updated_at, inserted_at) < ? "
        "ORDER BY COALESCE(updated_at, inserted_at) LIMIT 10",
        (cutoff,),
    ).fetchall()
    out["waiting_stale"] = [r["item_id"] for r in stale if r["item_id"]]

    out["blocked"] = int(conn.execute(
        "SELECT COUNT(*) n FROM action_items WHERE status='BLOCKED'"
    ).fetchone()["n"])
    return out


def _health_ingest(conn: sqlite3.Connection) -> dict:
    out = {"alias_misflag": None, "rejections_7d": 0, "extract_backlog": None}
    # is_outgoing=1 should mean: any sender on the org email domain, plus the
    # operator's own aliases. Mirrors the sync-side correction and
    # drift_check's alias-misflag check -- keep them in sync. Both signals
    # come from config ([org] domain + [operator] emails); the check is
    # skipped until at least one is set.
    clauses: list[str] = []
    params: list[str] = []
    org_domain = str(config.get("org_domain") or "").strip().lower().lstrip("@")
    if org_domain:
        clauses.append("LOWER(sender_email) LIKE '%@' || ?")
        params.append(org_domain)
    for alias in config.get("operator_emails") or []:
        a = str(alias).strip().lower()
        if a:
            clauses.append("LOWER(sender_email) = ?")
            params.append(a)
    if clauses:
        r = conn.execute(
            "SELECT COUNT(*) n FROM emails "
            f"WHERE ({' OR '.join(clauses)}) "
            "AND is_outgoing=0 AND is_deleted=0 "
            # Mailing-list relays ("'Name' via list") are genuinely inbound.
            "AND COALESCE(sender_name, '') NOT LIKE \"%' via %\"",
            params,
        ).fetchone()
        out["alias_misflag"] = int(r["n"])

    if _table_exists(conn, "ingest_rejections"):
        r = conn.execute(
            "SELECT COUNT(*) n FROM ingest_rejections WHERE rejected_at > datetime('now', '-7 days')"
        ).fetchone()
        out["rejections_7d"] = int(r["n"])

    if _table_exists(conn, "work_queue"):
        r = conn.execute(
            "SELECT COUNT(*) n FROM work_queue "
            "WHERE handler='extract_action_items' AND status IN ('pending','claimed','paused')"
        ).fetchone()
        out["extract_backlog"] = int(r["n"])
    return out


def _health_work_queue(conn: sqlite3.Connection) -> dict:
    """Per-handler work_queue health + alert flags. Delegates to
    worker.work_queue.queue_health (single source of truth) when the optional
    worker layer is installed."""
    try:
        from worker.work_queue import queue_health
        return queue_health()
    except ImportError:
        return {"handlers": {}, "alerts": [], "total": 0,
                "error": "optional worker layer not installed"}
    except Exception as exc:  # noqa: BLE001 — health must never crash
        return {"handlers": {}, "alerts": [], "total": 0, "error": str(exc)[:200]}


def _health_bus_shape(conn: sqlite3.Connection) -> dict:
    """Write-shape lint: bus_events rows whose summary is empty or a bare
    'event: ' prefix carry no actor/object/outcome and are useless for audit.
    Target 0 on new writes."""
    out = {"empty_summary_24h": 0}
    if _table_exists(conn, "bus_events"):
        r = conn.execute(
            "SELECT COUNT(*) n FROM bus_events WHERE ts >= datetime('now','-1 day') "
            "AND (summary IS NULL OR TRIM(summary)='' OR TRIM(summary) LIKE '%:')"
        ).fetchone()
        out["empty_summary_24h"] = int(r["n"])
    return out


def _health_drift(conn: sqlite3.Connection) -> dict:
    out = {"open": 0, "suppressed": 0}
    if _table_exists(conn, "drift_alerts"):
        r = conn.execute(
            "SELECT "
            "SUM(CASE WHEN suppressed=0 THEN 1 ELSE 0 END) o, "
            "SUM(CASE WHEN suppressed=1 THEN 1 ELSE 0 END) s "
            "FROM drift_alerts WHERE resolved_at IS NULL"
        ).fetchone()
        out["open"] = int(r["o"] or 0)
        out["suppressed"] = int(r["s"] or 0)
    return out


def _health_outcomes(conn: sqlite3.Connection) -> dict | None:
    """Learn-from-edits: comms_draft_outcomes roll-up. total, per-bucket split,
    matched-draft edit rate, and per-scenario edit rate when a template
    registry table carries it."""
    if not _table_exists(conn, "comms_draft_outcomes"):
        return None
    cols = {r[1] for r in conn.execute("PRAGMA table_info(comms_draft_outcomes)").fetchall()}
    if "bucket" not in cols:
        return {"total": conn.execute("SELECT COUNT(*) FROM comms_draft_outcomes").fetchone()[0],
                "buckets": {}, "matched": 0, "edit_rate": None, "scenarios": {}}
    total = conn.execute("SELECT COUNT(*) FROM comms_draft_outcomes").fetchone()[0]
    buckets = dict(conn.execute(
        "SELECT COALESCE(bucket,'?'), COUNT(*) FROM comms_draft_outcomes GROUP BY bucket").fetchall())
    matched = sum(n for b, n in buckets.items() if b != "unsent")
    kept = buckets.get("kept", 0)
    edit_rate = round(1 - kept / matched, 3) if matched else None
    scenarios: dict[str, float] = {}
    if _table_exists(conn, "comms_templates"):
        for scen, es in conn.execute(
                "SELECT scenario, edit_stats_json FROM comms_templates WHERE edit_stats_json IS NOT NULL"):
            try:
                d = json.loads(es)
                if d.get("edit_rate") is not None:
                    scenarios[scen] = d["edit_rate"]
            except Exception:  # noqa: BLE001
                continue
    return {"total": total, "buckets": buckets, "matched": matched,
            "edit_rate": edit_rate, "scenarios": scenarios}


def cmd_health() -> int:
    conn = _connect()
    try:
        when = _utcnow().strftime("%b %d, %H:%M UTC")
        print(f"=== System Health ({when}) ===")
        print()

        print("SYNC FRESHNESS (target: <24h)")
        for label, h, tag in _health_sync(conn):
            disp = _fmt_hours(h).rjust(6)
            print(f"  {label:<18}: {disp}  {tag}")
        print()

        ai = _health_action_items(conn)
        print("ACTION ITEMS")
        print(f"  P0 open             : {ai['p0']}")
        print(f"  P1 open             : {ai['p1']}")
        if ai["waiting_stale"]:
            sample = ", ".join(ai["waiting_stale"][:4])
            suffix = f" (...{len(ai['waiting_stale']) - 4} more)" if len(ai["waiting_stale"]) > 4 else ""
            print(f"  WAITING stale >3d   : {len(ai['waiting_stale'])}   ({sample}{suffix})")
        else:
            print(f"  WAITING stale >3d   : 0")
        print(f"  BLOCKED             : {ai['blocked']}")
        print()

        ing = _health_ingest(conn)
        print("INGEST HEALTH")
        if ing["alias_misflag"] is None:
            print("  email alias misflags: skipped (set [org] domain / [operator] emails in config.toml)")
        else:
            tag = "OK" if ing["alias_misflag"] == 0 else "BAD"
            print(f"  email alias misflags: {ing['alias_misflag']}   {tag}")
        tag = "OK" if ing["rejections_7d"] < 25 else "WARN"
        print(f"  ingest_rejections 7d: {ing['rejections_7d']}   {tag}")
        if ing["extract_backlog"] is not None:
            print(f"  extract_action_items backlog: {ing['extract_backlog']} pending work_queue rows")
        print()

        wq = _health_work_queue(conn)
        print("WORK QUEUE (per-handler)")
        if wq.get("error"):
            print(f"  unavailable: {wq['error']}")
        else:
            active = {h: d for h, d in wq["handlers"].items()
                      if d["pending"] or d["in_progress"] or d["stuck_in_progress"]}
            if active:
                for h, d in sorted(active.items(), key=lambda kv: kv[1]["pending"], reverse=True):
                    age = d["oldest_pending_age_min"]
                    age_s = f"{age}m" if age is not None else "-"
                    stuck = f" STUCK={d['stuck_in_progress']}" if d["stuck_in_progress"] else ""
                    print(f"  {h:<24}: pending={d['pending']} oldest={age_s} "
                          f"in_prog={d['in_progress']} done/hr={d['done_last_hour']}{stuck}")
            else:
                print("  (no pending/in_progress work)")
            print(f"  total rows: {wq['total']}")
            if wq["alerts"]:
                for a in wq["alerts"]:
                    print(f"  ALERT: {a}")
            else:
                print("  alerts: none")
        bus = _health_bus_shape(conn)
        tag = "OK" if bus["empty_summary_24h"] == 0 else "BAD"
        print(f"  bus empty-summary 24h: {bus['empty_summary_24h']}   {tag} (target 0; write-shape lint)")
        print()

        dr = _health_drift(conn)
        print("DRIFT ALERTS")
        print(f"  open                : {dr['open']}")
        print(f"  suppressed          : {dr['suppressed']}")
        print()

        # SCHEDULED TASKS (manifest drift). Read-only: schtasks_manifest.diff()
        # shells the OS task scheduler and compares to a desired-state
        # manifest. Optional module; fail-soft.
        print("SCHEDULED TASKS (manifest drift)")
        try:
            import schtasks_manifest as _stm
        except ImportError:
            _stm = None
        if _stm is None:
            print("  drift               : skipped (optional schtasks_manifest module not installed)")
        else:
            try:
                _sd = _stm.diff()
                if not _sd.get("live_readable"):
                    print("  drift               : ?   (task scheduler unreadable)")
                else:
                    _ms = _sd.get("mismatches", [])
                    _tag = "OK" if not _ms else "DRIFT"
                    print(f"  drift               : {len(_ms)}   {_tag}  (of {_sd.get('checked', 0)} manifest task(s))")
                    for _m in _ms[:8]:
                        print(f"    [{_m['type']}] {_m['task']}: desired={_m['desired']} installed={_m['installed']}")
            except Exception as _exc:  # noqa: BLE001 - fail-soft, never break health
                print(f"  drift               : unavailable ({str(_exc)[:60]})")
        print()

        # MERGE TOMBSTONES. Read-only: 0 sole-copy people-FK rows should be
        # stranded on merged tombstones. Simulates the remap on a throwaway
        # in-memory copy. Optional module; fail-soft.
        print("MERGE TOMBSTONES")
        try:
            import merge_tombstone_audit as _mta
        except ImportError:
            _mta = None
        if _mta is None:
            print("  sole-copy stranded  : skipped (optional merge_tombstone_audit module not installed)")
        else:
            try:
                _mn, _mdet = _mta.audit()
                print(f"  sole-copy stranded  : {_mn}   {'OK' if _mn == 0 else 'VIOLATION'}")
                for _mk, _mv in list(_mdet.items())[:8]:
                    print(f"    {_mk}: {_mv}")
            except Exception as _exc:  # noqa: BLE001
                print(f"  sole-copy stranded  : unavailable ({str(_exc)[:60]})")
        print()

        # STEWARD (Context Steward heartbeat — surfaces a dead/stale steward + queue depths)
        print("STEWARD")
        try:
            sh = conn.execute("SELECT last_tick_at,last_tick_status,ticks_total,last_review_depth,"
                              "last_staged_pending FROM steward_health WHERE id=1").fetchone()
        except sqlite3.OperationalError:
            sh = None
        if sh and sh[0]:
            import datetime as _dt
            age_min = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    last = _dt.datetime.strptime(str(sh[0])[:19], fmt)
                    age_min = (_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - last).total_seconds() / 60
                    break
                except ValueError:
                    continue
            tag = "  STALE/DEAD" if (age_min is None or age_min > 90) else "  OK"
            print(f"  last tick           : {sh[0]} ({'%.0fm ago' % age_min if age_min is not None else '?'}){tag}")
            print(f"  status / ticks      : {sh[1]} / {sh[2]}")
            print(f"  judgment queue      : {sh[3]}   staged pending: {sh[4]}")
        else:
            print("  last tick           : NEVER  STALE/DEAD")

        # WRITE-GATE — STOCK (historical NULL-actor backlog) + FLOW. Header
        # and scoreboard labels read the LIVE mode: hardcoded mode strings
        # once kept printing 'advisory' for weeks after the gate went
        # blocking, so the mode is always read from steward_config.
        print()
        try:
            mode = conn.execute("SELECT value FROM steward_config WHERE key='write_gate_mode'").fetchone()
            _gate_mode = mode[0] if mode else "advisory"
            print(f"WRITE-GATE ({_gate_mode})")
            wm = conn.execute("SELECT value FROM steward_config WHERE key='write_gate_watermark_cdc_id'").fetchone()
            cum = conn.execute("SELECT table_name, COUNT(*) FROM cdc_log WHERE actor IS NULL GROUP BY table_name ORDER BY 2 DESC").fetchall()
            total_null = sum(n for _, n in cum)
            last_flow = conn.execute("SELECT cdc_to, new_null_actor FROM write_gate_snapshots ORDER BY id DESC LIMIT 1").fetchone() \
                if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='write_gate_snapshots'").fetchone()[0] else None
            print(f"  mode / watermark    : {_gate_mode} / cdc#{wm[0] if wm else 0}")
            print(f"  NULL-actor (stock)  : {total_null}   ({', '.join(f'{t}:{n}' for t, n in cum) or 'none'})")
            if last_flow:
                print(f"  new NULL-actor (flow): {last_flow[1]}  since last observe (watermark now cdc#{last_flow[0]})")
            try:
                um = conn.execute("SELECT value FROM steward_config WHERE key='write_gate_unmigrated_writers'").fetchone()
                ums = json.loads(um[0]) if um and um[0] else []
                print(f"  unmigrated writers  : {len(ums)}  ({', '.join(ums[:10])})")
            except Exception:  # noqa: BLE001
                pass
            # gate-scoreboard: per-writer trailing-7d counts (mode-aware)
            try:
                sb = conn.execute(
                    "SELECT table_name, actor, writes_7d, rung FROM v_gate_scoreboard_7d "
                    "ORDER BY writes_7d DESC LIMIT 12").fetchall()
                print(f"  gate-scoreboard ({_gate_mode}): {len(sb)} writer-table rows, trailing 7d")
                for t, a, n, rung in sb:
                    print(f"    {t:<18} {a:<32} {n:>6}  {rung}")
                ck = conn.execute(
                    "SELECT status, COUNT(*) FROM write_gate_writer_checklist GROUP BY status"
                ).fetchall()
                print(f"  writer checklist    : {', '.join(f'{s}:{n}' for s, n in ck) or 'empty'}")
            except sqlite3.OperationalError:
                pass
        except sqlite3.OperationalError as e:
            print(f"  (gate view unavailable: {e})")

        # REDERIVE (nightly derived-store drift gauges)
        print()
        print("REDERIVE (nightly derived-store drift)")
        if not _table_exists(conn, "rederive_log"):
            print("  (no rederive job configured; optional)")
        else:
            try:
                last_run = conn.execute(
                    "SELECT run_id FROM rederive_log ORDER BY id DESC LIMIT 1").fetchone()
                if last_run:
                    rows = conn.execute(
                        "SELECT store, drift_found, fixed, ran_at FROM rederive_log WHERE run_id=? ORDER BY id",
                        (last_run[0],)).fetchall()
                    total = sum(r[1] for r in rows if r[1] and r[1] > 0)
                    print(f"  last run {last_run[0]}: total drift {total} across {len(rows)} stores")
                    for st, dr2, fx, ts in rows:
                        print(f"    {st:<26} drift={dr2:<5} fixed={fx}")
                else:
                    print("  (no rederive runs logged yet)")
            except sqlite3.OperationalError as e:
                print(f"  (rederive_log unavailable: {e})")

        # COMMS MONITOR (drafts-only; dead/stale = no tick in >30 min)
        print()
        print("COMMS MONITOR (drafts-only)")
        try:
            cm = conn.execute("SELECT last_tick_at,status,ticks_total,drafts_made FROM comms_monitor_health WHERE id=1").fetchone()
        except sqlite3.OperationalError:
            cm = None
        if cm and cm[0]:
            import datetime as _dt2
            age2 = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    age2 = (_dt2.datetime.now(_dt2.timezone.utc).replace(tzinfo=None) - _dt2.datetime.strptime(str(cm[0])[:19], fmt)).total_seconds() / 60
                    break
                except ValueError:
                    continue
            # DEGRADED when the last tick status is not 'ok' (a lane crashed),
            # not just when the tick is stale. A broken lane must surface even
            # if it ticked recently.
            if age2 is None or age2 > 30:
                tag2 = "  STALE/DEAD"
            elif str(cm[1] or "").lower() != "ok":
                tag2 = "  DEGRADED"
            else:
                tag2 = "  OK"
            pend = conn.execute("SELECT COUNT(*) FROM comms_drafts WHERE status='pending_gate'").fetchone()[0]
            ready = conn.execute("SELECT COUNT(*) FROM comms_drafts WHERE status='draft'").fetchone()[0]
            print(f"  last tick           : {cm[0]} ({'%.0fm ago' % age2 if age2 is not None else '?'}){tag2}")
            print(f"  status / ticks      : {cm[1]} / {cm[2]}")
            print(f"  awaiting gate       : {pend}   ready drafts for the operator: {ready}")
        else:
            print("  last tick           : NEVER  STALE/DEAD")

        # COMMS DRAFT OUTCOMES (learn-from-edits): how close autonomous drafts
        # land to what the operator actually sends.
        oc = _health_outcomes(conn)
        if oc is not None:
            print()
            print("COMMS DRAFT OUTCOMES (learn-from-edits)")
            tag = "  OK" if oc["total"] >= 2000 else "  LOW"
            print(f"  total outcomes      : {oc['total']}{tag}")
            order = ["kept", "light_edit", "heavy_edit", "discarded", "unsent"]
            split = ", ".join(f"{b}={oc['buckets'][b]}" for b in order if b in oc["buckets"])
            extra = ", ".join(f"{b}={n}" for b, n in oc["buckets"].items() if b not in order)
            print(f"  bucket split        : {split}{(', ' + extra) if extra else ''}")
            er = f"{oc['edit_rate']:.0%}" if oc["edit_rate"] is not None else "n/a"
            print(f"  matched drafts      : {oc['matched']}   edit rate: {er} (share the operator changed)")
            if oc["scenarios"]:
                top = sorted(oc["scenarios"].items(), key=lambda kv: kv[1], reverse=True)[:6]
                print(f"  per-scenario edit rate: {', '.join(f'{s}={r:.0%}' for s, r in top)}")
            else:
                print("  per-scenario edit rate: (pending a template registry with edit_stats_json)")

        # LEARNINGS (adherence write path + retrieval + Tier-A + consolidation). Fail-open.
        print()
        print("LEARNINGS")
        try:
            import learning_health as _lh
            s = _lh.summary(conn)
            emb = f"{s.get('embed_pct')}%" if s.get("embed_pct") is not None else "?"
            print(f"  active / embed      : {s.get('active', '?')} / {emb}")
            print(f"  adherence coverage  : {s.get('adherence_pct', 0)}%  ({s.get('adherence_judged', 0)}/{s.get('adherence_total', 0)} judged)")
            ta_tag = "" if s.get("tier_a_ok", True) else "  OVER CAP!"
            print(f"  Tier-A (always-on)  : {s.get('tier_a', '?')}/{s.get('tier_a_cap', 40)}{ta_tag}")
            print(f"  consolidation       : {s.get('consolidation_proposed', 0)} proposed / {s.get('consolidation_conflict', 0)} conflict")
        except Exception as _e:
            print(f"  (learning_health unavailable: {_e})")
        return 0
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# query.py explain AI-xxx
# ═══════════════════════════════════════════════════════════════════════════

def _explain_people(conn: sqlite3.Connection, item_row: sqlite3.Row) -> list[dict]:
    out: list[dict] = []
    seen: set[int] = set()

    wop = item_row["waiting_on_person_id"] if "waiting_on_person_id" in item_row.keys() else None
    if wop:
        p = conn.execute("SELECT id, name, email FROM people WHERE id=?", (wop,)).fetchone()
        if p and p["id"] not in seen:
            out.append(dict(p))
            seen.add(p["id"])

    if _table_exists(conn, "action_item_people"):
        try:
            rows = conn.execute(
                "SELECT p.id, p.name, p.email FROM action_item_people aip "
                "JOIN people p ON p.id = aip.person_id WHERE aip.item_id=?",
                (item_row["item_id"],),
            ).fetchall()
            for r in rows:
                if r["id"] not in seen:
                    out.append(dict(r))
                    seen.add(r["id"])
        except sqlite3.OperationalError:
            pass

    # Fallback: resolve waiting_on string, stripping trailing action words
    if not out and item_row["waiting_on"]:
        raw = str(item_row["waiting_on"]).strip()
        candidates = [raw]
        cleaned = re.sub(r"\b(reply|response|confirm(?:ation)?|answer|decision|to send|to reply)\b.*$",
                         "", raw, flags=re.IGNORECASE).strip(" ,.")
        if cleaned and cleaned != raw:
            candidates.append(cleaned)
        try:
            from audit_tools import resolve_person
            for c in candidates:
                p = resolve_person(c)
                if p:
                    out.append({"id": p["id"], "name": p["name"], "email": p["primary_email"]})
                    break
        except Exception:
            pass
    return out


def _explain_thread(conn: sqlite3.Connection, thread_id: str | None) -> list[dict]:
    if not thread_id:
        return []
    try:
        from audit_tools import get_thread
        t = get_thread(thread_id, include_bodies=False)
        return t.get("messages", []) or []
    except Exception:
        pass
    rows = conn.execute(
        "SELECT timestamp, sender_email, is_outgoing, subject FROM emails "
        "WHERE thread_id=? ORDER BY timestamp LIMIT 20",
        (thread_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _explain_next_action(item: sqlite3.Row, thread: list[dict]) -> str:
    status = item["status"]
    if status == "DONE":
        return "Already DONE. No action."
    if status == "BLOCKED":
        if item["resolution_note"]:
            return f"Blocked. Resolution note: {item['resolution_note']}"
        return "Blocked. Set resolution_note and either reopen or close."
    if status == "WAITING":
        last_in = item["email_last_inbound_at"] or ""
        last_out = item["email_last_outbound_at"] or ""
        if thread and not last_in:
            return "Thread exists but no inbound since the send. Chase or snooze 3d."
        if last_in and last_in > (last_out or ""):
            return "Inbound arrived after last outbound: reply or advance to OPEN."
        return "WAITING on counterparty. Chase if >7d, snooze otherwise."
    if status == "OPEN":
        if not thread:
            return "OPEN with no thread. Draft outbound or resolve as irrelevant."
        return "OPEN. Next touch depends on thread content: read and decide."
    return f"Status={status}. Human triage."


def cmd_explain(item_id: str) -> int:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM action_items WHERE item_id=?", (item_id,)
        ).fetchone()
        if not row:
            print(f"No action_item with item_id={item_id!r}.")
            return 1

        print(f"=== {item_id} ===")
        print(f"Description: {row['description']}")
        meta = f"Priority: {row['priority']} | Status: {row['status']}"
        if row["inserted_at"]:
            meta += f" | Created: {str(row['inserted_at'])[:16]}"
        if row["updated_at"]:
            meta += f" | Updated: {str(row['updated_at'])[:16]}"
        print(meta)
        if row["waiting_on"]:
            print(f"Waiting on: {row['waiting_on']}")
        if row["context"]:
            ctx = row["context"].replace("\n", " ")[:400]
            print(f"Context: {ctx}")
        print()

        people = _explain_people(conn, row)
        if people:
            print("RELATED PEOPLE")
            for p in people:
                print(f"  {p.get('name','?')} (id={p.get('id','?')})")
                if p.get("email"):
                    print(f"    primary_email: {p['email']}")
            print()
        else:
            print("RELATED PEOPLE")
            print("  (none resolved from waiting_on or action_item_people)")
            print()

        thread_id = row["email_thread_id"]
        msgs = _explain_thread(conn, thread_id)
        if thread_id:
            print(f"THREAD ({thread_id})")
            if msgs:
                for m in msgs[-8:]:
                    ts = str(m.get("timestamp", ""))[:16]
                    outbound = (m.get("is_operator_outbound")
                                or m.get("is_outgoing")
                                or m.get("is_outgoing_raw"))
                    direction = "OUT" if outbound else "IN "
                    who = m.get("sender_email", "?")
                    subj = (m.get("subject") or "")[:60]
                    print(f"  {ts}  {direction} {who[:32]:<32} {subj}")
            else:
                print("  (thread_id set but no messages found)")
            print()

        print("NEXT ACTION")
        print(f"  {_explain_next_action(row, msgs)}")
        return 0
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# query.py runbook <topic>
# ═══════════════════════════════════════════════════════════════════════════

def cmd_runbook(topic: str | None) -> int:
    conn = _connect()
    try:
        if not _table_exists(conn, "debug_tasks"):
            print("debug_tasks table not present. Create it to store your own debug recipes:")
            print("  CREATE TABLE debug_tasks (")
            print("      id               INTEGER PRIMARY KEY,")
            print("      topic            TEXT UNIQUE NOT NULL,  -- slug used by `query.py runbook <topic>`")
            print("      symptom          TEXT,                  -- what the operator sees first")
            print("      diagnosis_steps  TEXT,                  -- markdown: numbered shell / SQL checks")
            print("      fix_commands     TEXT,                  -- markdown: ordered remediation steps")
            print("      related_learning TEXT,                  -- id from the learnings table")
            print("      updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP)")
            return 2
        if not topic:
            rows = conn.execute(
                "SELECT topic, substr(symptom, 1, 80) s FROM debug_tasks ORDER BY topic"
            ).fetchall()
            if not rows:
                print("(no runbook topics seeded)")
                return 0
            print("=== Runbook topics ===")
            for r in rows:
                print(f"  {r['topic']:<26} {r['s']}")
            print()
            print("Usage: query.py runbook <topic>")
            return 0

        row = conn.execute(
            "SELECT * FROM debug_tasks WHERE topic=?", (topic,)
        ).fetchone()
        if not row:
            # Fuzzy fallback
            like = f"%{topic}%"
            row = conn.execute(
                "SELECT * FROM debug_tasks WHERE topic LIKE ? OR symptom LIKE ? LIMIT 1",
                (like, like),
            ).fetchone()
        if not row:
            print(f"No runbook topic matching '{topic}'.")
            conn_list = conn.execute("SELECT topic FROM debug_tasks ORDER BY topic").fetchall()
            if conn_list:
                print("Available topics: " + ", ".join(r["topic"] for r in conn_list))
            return 1

        print(f"=== Runbook: {row['topic']} ===")
        print()
        print("SYMPTOM")
        print(f"  {row['symptom']}")
        print()
        print("DIAGNOSIS")
        print(row["diagnosis_steps"] or "  (none)")
        print()
        print("FIX")
        print(row["fix_commands"] or "  (none)")
        if row["related_learning"]:
            print()
            print(f"Related learning: {row['related_learning']}")
            try:
                lrn = conn.execute(
                    "SELECT title, description FROM learnings WHERE id=? AND status='active'",
                    (row["related_learning"],),
                ).fetchone()
                if lrn:
                    print(f"  [{row['related_learning']}] {lrn['title']}")
                    desc = (lrn["description"] or "")[:200]
                    if desc:
                        print(f"    {desc}")
            except sqlite3.OperationalError:
                pass
        if row["updated_at"]:
            print()
            print(f"(Updated: {str(row['updated_at'])[:16]})")
        return 0
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry
# ═══════════════════════════════════════════════════════════════════════════

def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "health":
        return cmd_health()
    if cmd == "explain":
        if len(argv) < 2:
            print("Usage: query.py explain AI-xxx")
            return 2
        return cmd_explain(argv[1])
    if cmd == "runbook":
        topic = argv[1] if len(argv) > 1 else None
        return cmd_runbook(topic)
    print(f"Unknown subcommand: {cmd}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
