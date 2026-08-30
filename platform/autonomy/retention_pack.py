"""Recurring retention pack. Idempotent; approved-delete class.

Keeps the database from growing without bound. Each policy either archives
(moves rows to an *_archive twin, id-stable) or deletes rows past an age
threshold. All counts land in the run report + one summary row per op in
retention_log (the evidence trail, created by this module if missing).

Policies:
  review_queue          : resolved rows older than 14d -> _archive (move)
  cdc_log               : delete rows older than 90d (indexes stay)
  hook_health           : rollup into hook_health_daily, delete raw > 30d
  observations          : delete rows past expires_at (promoted ones live on
                          in their promotion targets; counts split in evidence)
  ingest_rejections     : delete rows older than 90d
  retention_log         : self-cap, delete summary rows older than 180d
  audit_events          : delete rows older than 180d
  staging               : terminal canonical-write rows older than 30d ->
                          staging_archive (move); quarantined rows KEPT
  episodes              : delete rows with source-ts older than 730d
  conversation_history  : delete rows older than 90d
  learning_reviews      : delete telemetry rows older than 180d
  telemetry TTLs        : job_heartbeats / bus_events / write_gate_snapshots
                          older than 90d (feature-detected); plan_runs KEEP ALL

The claim/extraction-staging retention policies from the full system are NOT
included in this starter kit (the extraction layer does not ship).

Intended to run nightly (register it as an off_session_nightly step or your
own scheduled job).
"""
import json
import sqlite3

import _db  # unified connector (busy_timeout + FK ON)
import paths
from audit_actor import set_actor, clear_actor

try:
    # Optional archive-aware FK exemption helper: registers exemptions so refs
    # onto archived parents count as archive-resolvable, not true violations.
    # Not included in this starter kit; the shipped schema has no hard FKs onto
    # the archived parents, so nothing is lost when absent.
    import db_gate
except ImportError:
    db_gate = None

DB = str(paths.DB_PATH)
RQ_DAYS = 14
CDC_DAYS = 90
HOOK_RAW_DAYS = 30
REJ_DAYS = 90
LOG_DAYS = 180            # retention_log self-cap (summary rows are tiny)
# v2 policies, derived from a table-growth census of a mature install.
# Retune for your own volumes; every threshold is a plain constant.
AUDIT_DAYS = 180          # audit_events is the fastest-growing audit table
STAGING_TERMINAL_DAYS = 30  # canonical-write staging: promoted/rejected/superseded archive; quarantined KEPT (judgment evidence)
EPISODES_DAYS = 730       # derived store (re-derivable from sources); ts is SOURCE time -- old backfill tail only
CONVO_DAYS = 90           # conversation_history (quote checks target recent sessions)
LREV_DAYS = 180           # learning_reviews telemetry
TELEMETRY_DAYS = 90       # job_heartbeats, bus_events, write_gate_snapshots; plan_runs KEEP ALL (audit spine)


def _table_exists(c, name):
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _ensure_log(c):
    """Evidence-trail table: one summary row per retention op per run."""
    c.execute("""CREATE TABLE IF NOT EXISTS retention_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,
        op TEXT,
        row_count TEXT,
        policy TEXT,
        changed_at TEXT DEFAULT (datetime('now')))""")
    if _table_exists(c, "_table_descriptions"):
        c.execute(
            "INSERT INTO _table_descriptions (table_name, tier, description, when_to_query, key_columns, category)"
            " VALUES ('retention_log','system',"
            "'One summary row per retention_pack op per run (the evidence trail for archive/delete policies).',"
            "'Audit of what retention archived or deleted, and when.',"
            "'run_id, op, row_count, policy, changed_at','system')"
            " ON CONFLICT(table_name) DO NOTHING")


def _summary_row(c, op, count, policy):
    c.execute(
        "INSERT INTO retention_log (run_id, op, row_count, policy)"
        " VALUES ('retention-pack', ?, ?, ?)",
        (op, str(count), policy))
    # Interleaved commit: _summary_row is the last statement of each numbered
    # retention op (internal-only helper, always called on `c` at an op
    # boundary), so committing here interleaves what would otherwise be a
    # single end-commit across the ops -- a crash at op N keeps ops 1..N-1
    # durable and shortens each op's write-lock hold.
    c.commit()


def run(actor="retention_pack:nightly", verbose=True):
    c = _db.connect(DB, timeout=120)
    c.execute("PRAGMA busy_timeout=120000")
    try:
        # Attribution is best-effort telemetry: a fresh install without the
        # audit-context singleton must not block retention.
        set_actor(c, actor, "retention_pack.py")
        _has_actor = True
    except sqlite3.Error:
        _has_actor = False
    ev = {}
    try:
        def count(sql):
            return c.execute(sql).fetchone()[0]

        _ensure_log(c)

        # 1. review_queue -> archive (move, id-stable)
        pre_arch = count("SELECT COUNT(*) FROM review_queue_archive")
        pre_live = count("SELECT COUNT(*) FROM review_queue")
        c.execute("""INSERT OR IGNORE INTO review_queue_archive
            SELECT * FROM review_queue
            WHERE status='resolved' AND resolved_at < datetime('now','-%d days')""" % RQ_DAYS)
        c.execute("""DELETE FROM review_queue
            WHERE status='resolved' AND resolved_at < datetime('now','-%d days')
            AND id IN (SELECT id FROM review_queue_archive)""" % RQ_DAYS)
        moved = count("SELECT COUNT(*) FROM review_queue_archive") - pre_arch
        ev["rq_archived"] = {"moved": moved,
                             "deleted_from_live": pre_live - count("SELECT COUNT(*) FROM review_queue")}
        _summary_row(c, "review_queue->archive", moved, "resolved>%dd" % RQ_DAYS)

        # 1b. archive-aware FK semantics: any live hard FK onto a parent we
        # archive gets an exemption entry so archived-parent refs count as
        # archive-resolvable, not true violations. No-op while no such FKs
        # exist (none in the shipped schema); the helper itself is optional.
        if db_gate is not None:
            ev["fk_exemptions_added"] = db_gate.register_archive_exemptions(
                c, "review_queue", "review_queue_archive")
        else:
            ev["fk_exemptions_added"] = ("skipped: db_gate helper not included in this "
                                         "starter kit (no hard FKs onto archived parents "
                                         "in the shipped schema)")

        # 2. cdc_log cap
        pre = count("SELECT COUNT(*) FROM cdc_log")
        c.execute("DELETE FROM cdc_log WHERE changed_at < datetime('now','-%d days')" % CDC_DAYS)
        ev["cdc_deleted"] = pre - count("SELECT COUNT(*) FROM cdc_log")
        _summary_row(c, "cdc_log cap", ev["cdc_deleted"], ">%dd" % CDC_DAYS)

        # 3. hook_health rollup + raw cap (hook_health_daily pre-exists with
        # date/call_count/error_count/avg_duration_ms/max_duration_ms columns)
        c.execute("""INSERT OR REPLACE INTO hook_health_daily
                (date, hook_name, event_type, call_count, error_count, avg_duration_ms, max_duration_ms)
            SELECT substr(ts,1,10), hook_name, COALESCE(event_type,''), COUNT(*),
                   SUM(CASE WHEN error_message IS NOT NULL AND error_message != '' THEN 1 ELSE 0 END),
                   AVG(duration_ms), MAX(duration_ms)
            FROM hook_health h
            WHERE substr(ts,1,10) < strftime('%Y-%m-%d','now')
            GROUP BY 1,2,3""")
        ev["hook_rollup_rows"] = count("SELECT COUNT(*) FROM hook_health_daily")
        pre = count("SELECT COUNT(*) FROM hook_health")
        c.execute("DELETE FROM hook_health WHERE ts < datetime('now','-%d days')" % HOOK_RAW_DAYS)
        ev["hook_raw_deleted"] = pre - count("SELECT COUNT(*) FROM hook_health")
        _summary_row(c, "hook_health rollup+cap", ev["hook_raw_deleted"], "raw>%dd; rollup daily" % HOOK_RAW_DAYS)

        # 4. observations expiry pruner
        ev["obs_expired_promoted"] = count(
            "SELECT COUNT(*) FROM observations WHERE expires_at < datetime('now') AND promoted=1")
        ev["obs_expired_unpromoted"] = count(
            "SELECT COUNT(*) FROM observations WHERE expires_at < datetime('now')"
            " AND (promoted IS NULL OR promoted=0)")
        pre = count("SELECT COUNT(*) FROM observations")
        c.execute("DELETE FROM observations WHERE expires_at < datetime('now')")
        ev["obs_deleted"] = pre - count("SELECT COUNT(*) FROM observations")
        _summary_row(c, "observations expiry prune", ev["obs_deleted"], "past expires_at")

        # 5. ingest_rejections cap
        pre = count("SELECT COUNT(*) FROM ingest_rejections")
        c.execute("DELETE FROM ingest_rejections WHERE rejected_at < datetime('now','-%d days')" % REJ_DAYS)
        ev["rejections_deleted"] = pre - count("SELECT COUNT(*) FROM ingest_rejections")
        _summary_row(c, "ingest_rejections cap", ev["rejections_deleted"], ">%dd" % REJ_DAYS)

        # 6. retention_log self-cap (summary rows are tiny; keep half a year)
        pre = count("SELECT COUNT(*) FROM retention_log")
        c.execute("DELETE FROM retention_log WHERE changed_at < datetime('now','-%d days')" % LOG_DAYS)
        ev["retention_log_deleted"] = pre - count("SELECT COUNT(*) FROM retention_log")
        _summary_row(c, "retention_log self-cap", ev["retention_log_deleted"], ">%dd" % LOG_DAYS)

        # ── v2 policies ───────────────────────────────────────────────────
        # 7. audit_events age cap
        pre = count("SELECT COUNT(*) FROM audit_events")
        c.execute("DELETE FROM audit_events WHERE ts < datetime('now','-%d days')" % AUDIT_DAYS)
        ev["audit_events_deleted"] = pre - count("SELECT COUNT(*) FROM audit_events")
        _summary_row(c, "audit_events cap", ev["audit_events_deleted"], ">%dd" % AUDIT_DAYS)

        # 8. canonical-write staging: terminal rows -> staging_archive
        # (id-stable move). Quarantined rows are KEPT (they are the judgment
        # queue's evidence).
        c.execute("""CREATE TABLE IF NOT EXISTS staging_archive AS
                     SELECT * FROM staging WHERE 0""")
        if _table_exists(c, "_table_descriptions"):
            c.execute(
                "INSERT INTO _table_descriptions (table_name, tier, description, when_to_query, key_columns, category)"
                " VALUES ('staging_archive','system',"
                "'Terminal (promoted/rejected/superseded) canonical-write staging rows older than %dd, moved by retention_pack. Quarantined rows never move.',"
                "'Audit of old canonical-write staging; recent rows live in staging.',"
                "'id, idempotency_key, target_table, status, promoted_at','system')"
                " ON CONFLICT(table_name) DO NOTHING" % STAGING_TERMINAL_DAYS)
        pre_arch = count("SELECT COUNT(*) FROM staging_archive")
        c.execute("""INSERT OR IGNORE INTO staging_archive
            SELECT * FROM staging
            WHERE status IN ('promoted','rejected','superseded')
              AND COALESCE(promoted_at, last_attempt_at, created_at) < datetime('now','-%d days')""" % STAGING_TERMINAL_DAYS)
        c.execute("""DELETE FROM staging
            WHERE status IN ('promoted','rejected','superseded')
              AND COALESCE(promoted_at, last_attempt_at, created_at) < datetime('now','-%d days')
              AND id IN (SELECT id FROM staging_archive)""" % STAGING_TERMINAL_DAYS)
        ev["staging_archived"] = count("SELECT COUNT(*) FROM staging_archive") - pre_arch
        _summary_row(c, "staging->archive", ev["staging_archived"], "terminal>%dd" % STAGING_TERMINAL_DAYS)

        # 9. episodes age cap (derived store; ts is SOURCE time, so this only
        # trims the ancient backfill tail, never recent activity)
        pre = count("SELECT COUNT(*) FROM episodes")
        c.execute("DELETE FROM episodes WHERE ts < datetime('now','-%d days')" % EPISODES_DAYS)
        ev["episodes_deleted"] = pre - count("SELECT COUNT(*) FROM episodes")
        _summary_row(c, "episodes cap", ev["episodes_deleted"], "source-ts>%dd (derived)" % EPISODES_DAYS)

        # 10. conversation_history cap
        pre = count("SELECT COUNT(*) FROM conversation_history")
        c.execute("DELETE FROM conversation_history WHERE timestamp < datetime('now','-%d days')" % CONVO_DAYS)
        ev["convo_deleted"] = pre - count("SELECT COUNT(*) FROM conversation_history")
        _summary_row(c, "conversation_history cap", ev["convo_deleted"], ">%dd" % CONVO_DAYS)

        # 11. review telemetry cap
        pre = count("SELECT COUNT(*) FROM learning_reviews")
        c.execute("DELETE FROM learning_reviews WHERE surfaced_at < datetime('now','-%d days')" % LREV_DAYS)
        ev["learning_reviews_deleted"] = pre - count("SELECT COUNT(*) FROM learning_reviews")
        _summary_row(c, "learning_reviews cap", ev["learning_reviews_deleted"], ">%dd" % LREV_DAYS)

        # 12. telemetry TTLs (plan_runs KEEP ALL -- the audit spine, and small).
        # Feature-detected: write_gate_snapshots only exists on installs that
        # add the write-gate scoreboard.
        for tbl, col in (("job_heartbeats", "started_at"), ("bus_events", "ts"),
                         ("write_gate_snapshots", "observed_at")):
            if not _table_exists(c, tbl):
                ev["%s_deleted" % tbl] = "skipped: table not present"
                continue
            pre = count("SELECT COUNT(*) FROM %s" % tbl)
            c.execute("DELETE FROM %s WHERE %s < datetime('now','-%d days')" % (tbl, col, TELEMETRY_DAYS))
            ev["%s_deleted" % tbl] = pre - count("SELECT COUNT(*) FROM %s" % tbl)
            _summary_row(c, "%s cap" % tbl, ev["%s_deleted" % tbl], ">%dd" % TELEMETRY_DAYS)

        c.commit()
    finally:
        if _has_actor:
            try:
                clear_actor(c)
            except sqlite3.Error:
                pass
        c.commit()
        c.close()
    if verbose:
        print(json.dumps(ev, indent=1))
    return ev


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    actor = sys.argv[sys.argv.index("--actor") + 1] if "--actor" in sys.argv else "retention_pack:cli"
    run(actor=actor)
