"""drift_check.py, drift detector for the ops database.

Ten checks run against the ops DB. Findings are upserted into ``drift_alerts``;
re-firing the same alert_type updates fire_count + last_fired_at instead of
creating a new row (the partial UNIQUE INDEX on (alert_type) WHERE
resolved_at IS NULL enforces this). When a condition clears for 24h the
alert auto-resolves.

Checks:

1.  ``open_action_items_spike``
        Today's OPEN action_items > rolling 7-day avg * 1.3 (warn)
2.  ``ingest_rejections_burst``
        More than 10 ingest_rejections in last 24h (warn)
3.  ``org_alias_misflag``
        Any email from a configured org/operator sender with is_outgoing=0
        (crit). Disabled until [org] domain or [operator] emails is set in
        config.toml.
4.  ``episode_gap``
        Latest episode older than 24h (warn)
5.  ``discord_dm_stale``
        Newest Discord DM older than 72h (warn) / 7d (crit)
6.  ``handler_failures_streak``
        Any handler with 5+ consecutive failures in last 24h (crit)
7.  ``state_machine_violations``
        More than 0 state_machine_violations in last 24h (crit)
8.  ``fts_sync_drift``
        External-content FTS twins drifting from their base tables (warn)
9.  ``people_email_fragments_new``
        Fresh people rows whose email is a bare fragment without an @ (warn)
10. ``email_thread_reply_drift``
        email_threads reply-state cache disagrees with a recompute (warn/crit)

Suppressed alerts (``suppressed=1``) still surface in ``--all`` but are hidden
from ``--open`` and from the daily digest. Use ``drift_check.py suppress
<alert_type>`` to mute and ``drift_check.py unsuppress`` to unmute.

Daily reconcile: ``--reconcile`` runs all checks, then auto-opens one
remediation proposal in ``action_items_inbox`` per open, unsuppressed alert.
Idempotent: the drift signature ``drift_alerts:<row_id>:<alert_type>`` is
stored in the inbox row's ``source_ref``; the same signature never opens a
second row (and a still-pending remediation row for the same alert_type blocks
re-opens across resolve/re-fire cycles). Capped at RECONCILE_DAILY_CAP (10)
opens per UTC day. Wired into the nightly autonomy run
(off_session_nightly.py).

Invocation:
    python drift_check.py             # run all checks, write alerts, exit 0
    python drift_check.py --json      # JSON of fire/clear actions
    python drift_check.py --open      # list active alerts only
    python drift_check.py --all       # list every row
    python drift_check.py --reconcile # checks + open capped remediation rows
    python drift_check.py suppress <alert_type>
    python drift_check.py unsuppress <alert_type>
"""
from __future__ import annotations
import paths
import _db  # unified connector (busy_timeout + FK ON)
import config

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(str(paths.DB_PATH))

# Auto-resolve when a fired alert hasn't been seen for this many hours
RESOLVE_AFTER_HOURS = 24

# Spike multiplier on rolling average for open_action_items
ACTION_ITEM_SPIKE_RATIO = 1.3
ROLLING_DAYS = 7

# Burst threshold for ingest_rejections (per 24h)
INGEST_REJECTION_BURST = 10

# Episode gap threshold (hours)
EPISODE_GAP_HOURS = 24

# Consecutive handler failures
HANDLER_FAILURE_STREAK = 5

# Discord DM pipeline staleness (sync_discord_dms.py). Gets its own check
# because this pipeline once died silently for 12 days before anyone noticed.
DM_STALE_WARN_HOURS = 72
DM_STALE_CRIT_HOURS = 168

# Max remediation proposals auto-opened per UTC day. Hard guard in
# reconcile_alerts(); everything past the cap is reported as "cap_deferred"
# and picked up on a later day if the alert is still open.
RECONCILE_DAILY_CAP = 10
RECONCILE_SOURCE = "audit-drift-reconcile"  # "audit-" prefix = URL-exempt in validators


def _connect() -> sqlite3.Connection:
    # timeout/busy_timeout 30s: a shorter value lost races against the
    # off-session drain ticks once those started doing real work.
    conn = _db.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


# ── individual checks ─────────────────────────────────────────────────────
def _check_action_item_spike(conn: sqlite3.Connection) -> dict | None:
    today = conn.execute(
        "SELECT COUNT(*) AS n FROM action_items WHERE status = 'OPEN'"
    ).fetchone()["n"]

    rows = conn.execute(
        """
        SELECT DATE(inserted_at) AS day, COUNT(*) AS n
          FROM action_items
         WHERE inserted_at >= date('now', ?)
         GROUP BY day
        """,
        (f"-{ROLLING_DAYS} days",),
    ).fetchall()
    if not rows:
        return None
    avg = sum(r["n"] for r in rows) / len(rows)
    threshold = avg * ACTION_ITEM_SPIKE_RATIO

    if avg < 5 or today <= threshold:
        return None
    return {
        "alert_type": "open_action_items_spike",
        "severity": "warn",
        "summary": f"OPEN action_items={today} > {ACTION_ITEM_SPIKE_RATIO}×avg{int(avg)} (last {ROLLING_DAYS}d)",
        "detail": {"today": today, "rolling_avg": avg, "threshold": threshold},
    }


def _check_ingest_rejections(conn: sqlite3.Connection) -> dict | None:
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM ingest_rejections WHERE rejected_at >= datetime('now', '-1 day')"
    ).fetchone()["n"]
    if n <= INGEST_REJECTION_BURST:
        return None
    return {
        "alert_type": "ingest_rejections_burst",
        "severity": "warn",
        "summary": f"{n} ingest_rejections in last 24h (threshold {INGEST_REJECTION_BURST})",
        "detail": {"count_24h": n, "threshold": INGEST_REJECTION_BURST},
    }


# Outbound semantics: is_outgoing=1 means "someone at the org sent this" --
# ANY sender at the configured org domain plus every configured operator
# address. A colleague's reply counts as us having answered. Source of truth
# for the WRITER is daily_sync._correct_is_outgoing -- keep the two in sync.
def _org_sender_predicate() -> tuple[str, list]:
    """SQL predicate matching senders that must always carry is_outgoing=1.

    Built from config.toml: [org] domain (any sender at that domain) plus
    [operator] emails (personal addresses that also count as "us"). Returns
    ("", []) when neither is configured, which disables the misflag check.

    Example config.toml (FICTIONAL values):
      # [org]
      # domain = "example.org"
      # [operator]
      # emails = ["jane.doe@personal-mail.example"]
    """
    domain = str(config.get("org_domain") or "").strip().lower().lstrip("@")
    aliases = [str(a).strip().lower() for a in (config.get("operator_emails") or []) if str(a).strip()]
    clauses: list[str] = []
    params: list = []
    if domain:
        clauses.append("LOWER(sender_email) LIKE ?")
        params.append(f"%@{domain}")
    if aliases:
        clauses.append("LOWER(sender_email) IN (%s)" % ",".join("?" * len(aliases)))
        params.extend(aliases)
    if not clauses:
        return "", []
    return "(" + " OR ".join(clauses) + ")", params


def _check_alias_misflag(conn: sqlite3.Connection) -> dict | None:
    # Mirrors daily_sync._correct_is_outgoing EXACTLY, including the Google-
    # Group DMARC-rewrite exception: "'X' via Group" from our own group address
    # is inbound-by-design, not a misflag.
    predicate, params = _org_sender_predicate()
    if not predicate:
        return None  # nothing configured yet; check disabled
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
          FROM emails
         WHERE {predicate}
           AND is_outgoing = 0
           AND is_deleted = 0
           AND COALESCE(sender_name, '') NOT LIKE "%' via %"
        """,
        params,
    ).fetchone()
    n = int(row["n"])
    if n == 0:
        return None
    return {
        "alert_type": "org_alias_misflag",
        "severity": "crit",
        "summary": f"{n} emails from org senders marked is_outgoing=0 (ingest invariant violated)",
        "detail": {"count": n, "rule": "any org-domain sender + configured operator emails"},
    }


def _check_episode_gap(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT MAX(ts) AS latest FROM episodes").fetchone()
    if not row or not row["latest"]:
        return {
            "alert_type": "episode_gap",
            "severity": "warn",
            "summary": "episodes table empty (no episodes ever materialized)",
            "detail": {"latest": None},
        }
    try:
        latest = datetime.fromisoformat(row["latest"].replace("Z", "+00:00"))
        if latest.tzinfo is not None:
            latest = latest.replace(tzinfo=None)
    except ValueError:
        return None
    gap_h = (datetime.now(timezone.utc).replace(tzinfo=None) - latest).total_seconds() / 3600
    if gap_h <= EPISODE_GAP_HOURS:
        return None
    return {
        "alert_type": "episode_gap",
        "severity": "warn",
        "summary": f"episodes stale by {gap_h:.1f}h (threshold {EPISODE_GAP_HOURS}h)",
        "detail": {"latest": row["latest"], "gap_hours": gap_h},
    }


def _check_discord_dm_stale(conn: sqlite3.Connection) -> dict | None:
    """No DM message newer than 72h -> warn, 7d -> crit.

    Reads the freshest message in dm/group_dm channels (data truth), plus the
    sync_state 'discord_dms' health marker for the probable cause. Suppression
    is handled generically via drift_alerts.suppressed.
    """
    row = conn.execute(
        """
        SELECT MAX(m.timestamp) AS latest
          FROM discord_messages m
          JOIN discord_channels c ON c.id = m.channel_id
         WHERE c.channel_type IN ('dm','group_dm')
        """
    ).fetchone()
    if not row or not row["latest"]:
        return {
            "alert_type": "discord_dm_stale",
            "severity": "crit",
            "summary": "no DM messages in discord_messages at all (pipeline never ran?)",
            "detail": {"latest": None},
        }
    try:
        latest = datetime.fromisoformat(row["latest"].replace("Z", "+00:00"))
        if latest.tzinfo is not None:
            latest = latest.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None
    gap_h = (datetime.now(timezone.utc).replace(tzinfo=None) - latest).total_seconds() / 3600
    if gap_h <= DM_STALE_WARN_HOURS:
        return None
    health = conn.execute(
        "SELECT last_sync, last_id FROM sync_state WHERE source = 'discord_dms'"
    ).fetchone()
    keyring_service = config.get("keyring_service") or "ops-kit"
    return {
        "alert_type": "discord_dm_stale",
        "severity": "crit" if gap_h > DM_STALE_CRIT_HOURS else "warn",
        "summary": (
            f"newest Discord DM message is {gap_h/24:.1f}d old "
            f"(warn >{DM_STALE_WARN_HOURS}h, crit >{DM_STALE_CRIT_HOURS}h); "
            f"sync health: {health['last_id'] if health else 'no sync_state row'}"
        ),
        "detail": {
            "latest_dm_ts": row["latest"],
            "gap_hours": round(gap_h, 1),
            "sync_state": dict(health) if health else None,
            "renewal": (
                "run: python brief/sync_discord_dms.py --since <date>; "
                f"if 401, renew keyring {keyring_service}/discord_user_token"
            ),
        },
    }


def _check_handler_failures(conn: sqlite3.Connection) -> dict | None:
    rows = conn.execute(
        """
        SELECT handler, COUNT(*) AS n
          FROM work_queue
         WHERE status = 'failed'
           AND COALESCE(finished_at, claimed_at, created_at) >= datetime('now', '-1 day')
         GROUP BY handler
         HAVING n >= ?
         ORDER BY n DESC
        """,
        (HANDLER_FAILURE_STREAK,),
    ).fetchall()
    if not rows:
        return None
    streaks = {r["handler"]: r["n"] for r in rows}
    worst = max(streaks.values())
    return {
        "alert_type": "handler_failures_streak",
        "severity": "crit",
        "summary": f"{len(streaks)} handler(s) with >={HANDLER_FAILURE_STREAK} failures in last 24h (worst {worst})",
        "detail": {"per_handler": streaks},
    }


def _check_state_violations(conn: sqlite3.Connection) -> dict | None:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
              FROM ingest_rejections
             WHERE rejected_at >= datetime('now', '-1 day')
               AND (errors LIKE '%invalid state%' OR errors LIKE '%state transition%')
            """
        ).fetchone()
        n = int(row["n"]) if row else 0
    except sqlite3.OperationalError:
        n = 0
    if n == 0:
        return None
    return {
        "alert_type": "state_machine_violations",
        "severity": "crit",
        "summary": f"{n} state-machine violation(s) blocked in last 24h",
        "detail": {"count_24h": n},
    }


def _check_fts_sync_drift(conn: sqlite3.Connection) -> dict | None:
    """External-content FTS twins drifting from their base tables.

    Derives (base, fts) pairs from sqlite_master by parsing content='...' out of
    each CREATE VIRTUAL TABLE ... fts5 statement, then compares row counts.
    Alerts when any twin drifts >2% (or >100 rows where pct would hide it).
    This is the exact class of silent rot an audit once found on entities_fts
    (over a thousand docs invisible to every search)."""
    import re as _re
    pairs = []
    for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND sql LIKE 'CREATE VIRTUAL TABLE%fts5%' AND sql LIKE '%content=%'"
    ).fetchall():
        m = _re.search(r"content\s*=\s*'([^']+)'", sql or "")
        if m and m.group(1):
            pairs.append((m.group(1), name))
    drifted = {}
    for base, fts in pairs:
        try:
            nb = conn.execute(f"SELECT COUNT(*) FROM {base}").fetchone()[0]
            nf = conn.execute(f"SELECT COUNT(*) FROM {fts}").fetchone()[0]
        except sqlite3.Error:
            continue  # base dropped or fts unreadable; schema sweeps own that
        gap = abs(nb - nf)
        if gap and (gap > 100 or (nb and gap / nb > 0.02)):
            drifted[fts] = {"base": nb, "fts": nf, "gap": nb - nf}
    if not drifted:
        return None
    worst = max(drifted.items(), key=lambda kv: abs(kv[1]["gap"]))
    return {
        "alert_type": "fts_sync_drift",
        "severity": "warn",
        "summary": f"{len(drifted)} FTS twins out of sync with base; worst {worst[0]} gap {worst[1]['gap']:+d}",
        "detail": drifted,
    }


def _check_people_email_fragments(conn: sqlite3.Connection) -> dict | None:
    """People rows whose email holds a bare word fragment (no @), the
    name-split ingest damage class. name==email misses some known victims, so
    the structural probe is email-without-@. Fires only on rows CREATED in the
    last 14d (fresh ingest damage); legacy fragments predating the window are
    pending merge decisions and must not re-fire daily."""
    try:
        rows = conn.execute(
            """
            SELECT id, name, email, created_at FROM people
             WHERE TRIM(COALESCE(email,'')) != '' AND email NOT LIKE '%@%'
               AND merged_into IS NULL
               AND created_at >= datetime('now', '-14 days')
             ORDER BY created_at DESC LIMIT 25
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    total = conn.execute(
        "SELECT COUNT(*) FROM people WHERE TRIM(COALESCE(email,'')) != '' "
        "AND email NOT LIKE '%@%' AND merged_into IS NULL"
    ).fetchone()[0]
    return {
        "alert_type": "people_email_fragments_new",
        "severity": "warn",
        "summary": f"{len(rows)} people row(s) created in last 14d with fragment emails (no @), name-split ingest damage recurring; {total} total live fragments",
        "detail": {"new_rows": [dict(r) for r in rows], "total_live_fragments": total,
                   "fix": "inspect the import batch; repair via merge proposals, not direct edits"},
    }


def _check_email_thread_reply_drift(conn: sqlite3.Connection) -> dict | None:
    """email_threads reply-state cache vs recompute. daily_sync refreshes only
    its `touched` thread set, so a bug in that set-building once left a third
    of the rows stale and required a hand-rebuild. A small residue is normal
    churn between the nightly refresh and live inserts; warn above that, crit
    when it is clearly systemic. The fix is re-running daily_sync's aggregate
    UPDATE over the drifted thread_ids."""
    try:
        rows = conn.execute(
            """
            WITH recomputed AS (
              SELECT t.thread_id,
                (SELECT MAX(timestamp) FROM emails e WHERE e.thread_id=t.thread_id AND e.is_outgoing=0 AND e.labels NOT LIKE '%DRAFT%') AS r_in_ts,
                (SELECT COUNT(*) FROM emails e WHERE e.thread_id=t.thread_id AND e.is_outgoing=0 AND e.labels NOT LIKE '%DRAFT%') AS r_in_n,
                (SELECT COUNT(*) FROM emails e WHERE e.thread_id=t.thread_id AND e.is_outgoing=1 AND e.labels NOT LIKE '%DRAFT%') AS r_out_n,
                COALESCE(
                  (SELECT sender_email FROM emails e WHERE e.thread_id=t.thread_id AND e.is_outgoing=0 AND e.labels NOT LIKE '%DRAFT%' ORDER BY e.timestamp DESC LIMIT 1),
                  (SELECT sender_email FROM emails e WHERE e.thread_id=t.thread_id AND e.labels NOT LIKE '%DRAFT%' ORDER BY e.timestamp DESC LIMIT 1)) AS r_sender,
                t.last_inbound_ts, t.inbound_count, t.outbound_count, t.last_sender_email
              FROM email_threads t
            )
            SELECT thread_id,
                   (last_inbound_ts IS NOT r_in_ts) AS d_ts,
                   (inbound_count IS NOT r_in_n) AS d_in,
                   (outbound_count IS NOT r_out_n) AS d_out,
                   (last_sender_email IS NOT r_sender) AS d_sender
              FROM recomputed
             WHERE last_inbound_ts IS NOT r_in_ts OR inbound_count IS NOT r_in_n
                OR outbound_count IS NOT r_out_n OR last_sender_email IS NOT r_sender
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    n = len(rows)
    if n <= 50:  # normal churn between the nightly refresh and live inserts
        return None
    fields = {"sender": sum(r["d_sender"] for r in rows), "inbound_ts": sum(r["d_ts"] for r in rows),
              "inbound_count": sum(r["d_in"] for r in rows), "outbound_count": sum(r["d_out"] for r in rows)}
    return {
        "alert_type": "email_thread_reply_drift",
        "severity": "crit" if n > 200 else "warn",
        "summary": f"{n} email_threads rows disagree with recomputed reply-state (by field: {fields})",
        "detail": {"drift_count": n, "per_field": fields,
                   "sample_thread_ids": [r["thread_id"] for r in rows[:15]],
                   "fix": "re-run daily_sync.py's aggregate UPDATE over the drifted thread_ids"},
    }


CHECKS = [
    _check_action_item_spike,
    _check_ingest_rejections,
    _check_alias_misflag,
    _check_episode_gap,
    _check_discord_dm_stale,
    _check_handler_failures,
    _check_state_violations,
    _check_fts_sync_drift,
    _check_people_email_fragments,
    _check_email_thread_reply_drift,
]


# ── upsert / resolve logic ────────────────────────────────────────────────
def _upsert_alert(conn: sqlite3.Connection, alert: dict) -> str:
    existing = conn.execute(
        "SELECT id, fire_count FROM drift_alerts WHERE alert_type = ? AND resolved_at IS NULL",
        (alert["alert_type"],),
    ).fetchone()
    # Callers pass connections with or without row_factory=Row; index
    # positionally so a plain-tuple row can't TypeError.
    existing_id = existing[0] if existing else None
    detail_json = json.dumps(alert.get("detail") or {})
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
    if existing:
        conn.execute(
            """
            UPDATE drift_alerts
               SET last_fired_at = ?, severity = ?, summary = ?, detail_json = ?,
                   fire_count = fire_count + 1
             WHERE id = ?
            """,
            (now, alert["severity"], alert["summary"], detail_json, existing_id),
        )
        # If severity escalated to crit on an existing open alert and no
        # post-mortem stub exists yet, draft one now.
        if alert["severity"] == "crit":
            _ensure_postmortem(conn, alert, first_fired_at=now)
        return "updated"
    conn.execute(
        """
        INSERT INTO drift_alerts (alert_type, severity, summary, detail_json,
                                  first_fired_at, last_fired_at)
        VALUES (?,?,?,?,?,?)
        """,
        (alert["alert_type"], alert["severity"], alert["summary"], detail_json, now, now),
    )
    if alert["severity"] == "crit":
        _ensure_postmortem(conn, alert, first_fired_at=now)
    return "fired"


# ── post-mortem auto-writer ───────────────────────────────────────────────
POSTMORTEM_TEMPLATE_PATH = Path(str(paths.ROOT / "templates" / "postmortem_template.md"))


def _ensure_postmortem(conn: sqlite3.Connection, alert: dict, first_fired_at: str) -> None:
    """Draft a post-mortem stub into reference_docs for any crit alert.

    Idempotent: slug ``postmortem-{YYYYMMDD}-{alert_type}`` is the natural key;
    second call on the same alert on the same day is a no-op. The operator
    fills in root cause / fix / prevention after the incident.
    """
    day = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d")
    # One slug scheme only: alert_type carries underscores while other writers
    # hyphenate, which once forked the idempotency key and spawned duplicate
    # same-day stubs. Normalize to hyphens here.
    slug = f"postmortem-{day}-{alert['alert_type'].replace('_', '-')}"

    # Skip if already exists
    row = conn.execute(
        "SELECT id FROM reference_docs WHERE slug = ?", (slug,)
    ).fetchone()
    if row:
        return

    detail = alert.get("detail") or {}
    affected_tables = ", ".join(detail.get("tables", [])) or "(see detail_json)"
    affected_items = ", ".join(detail.get("action_items", [])) or "(none identified)"
    user_facing = "yes" if alert["severity"] == "crit" else "unclear"

    if POSTMORTEM_TEMPLATE_PATH.exists():
        template = POSTMORTEM_TEMPLATE_PATH.read_text(encoding="utf-8")
    else:
        template = (
            "# Post-mortem: {alert_type}\n\n"
            "**Date:** {first_fired_at}\n"
            "**Severity:** {severity}\n\n"
            "## What happened\n{summary}\n\n"
            "## Root cause\n(to be filled)\n"
        )

    filled = template.format(
        alert_type=alert["alert_type"],
        first_fired_at=first_fired_at,
        severity=alert["severity"],
        resolved_at="still open",
        summary=alert["summary"],
        affected_tables=affected_tables,
        affected_action_items=affected_items,
        user_facing=user_facing,
        related_learnings="(none yet)",
        date=day,
    )

    title = f"Post-mortem: {alert['alert_type']} ({day})"
    # OR IGNORE: a crit alert that fires repeatedly on the same day must not
    # crash the whole drift run on the second postmortem attempt.
    conn.execute(
        """
        INSERT OR IGNORE INTO reference_docs (slug, title, category, doc_type, content, updated_at, tags)
        VALUES (?, ?, 'postmortem', 'postmortem', ?, ?, 'auto-generated,drift,crit')
        """,
        (slug, title, filled, first_fired_at),
    )


def _auto_resolve(conn: sqlite3.Connection, fired_types: set[str]) -> list[str]:
    """Mark alerts resolved if condition cleared and not seen for RESOLVE_AFTER_HOURS."""
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=RESOLVE_AFTER_HOURS)).isoformat(timespec="seconds") + "Z"
    open_rows = conn.execute(
        "SELECT id, alert_type, last_fired_at FROM drift_alerts WHERE resolved_at IS NULL"
    ).fetchall()
    resolved = []
    for r in open_rows:
        if r["alert_type"] in fired_types:
            continue
        if r["last_fired_at"] and r["last_fired_at"] > cutoff:
            continue
        conn.execute(
            "UPDATE drift_alerts SET resolved_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z", r["id"]),
        )
        resolved.append(r["alert_type"])
    return resolved


def run_checks() -> dict:
    """Run every check, upsert alerts, auto-resolve. Returns action summary."""
    conn = _connect()
    try:
        fired: list[dict] = []
        actions: list[tuple[str, str]] = []  # (alert_type, action)
        for check in CHECKS:
            try:
                alert = check(conn)
            except Exception as e:
                actions.append((check.__name__, f"check_error:{e}"))
                continue
            if alert is None:
                continue
            fired.append(alert)
            actions.append((alert["alert_type"], _upsert_alert(conn, alert)))

        fired_types = {a["alert_type"] for a in fired}
        resolved = _auto_resolve(conn, fired_types)
        for t in resolved:
            actions.append((t, "resolved"))

        conn.commit()
        return {
            "fired": [a["alert_type"] for a in fired],
            "actions": actions,
            "resolved": resolved,
            "open": _list_open(conn),
        }
    finally:
        conn.close()


# ── daily reconcile, auto-open remediation proposals ─────────────────────
def reconcile_alerts(conn: sqlite3.Connection) -> dict:
    """Open one action_items_inbox remediation proposal per open, unsuppressed
    drift alert. Caller owns the transaction/commit.

    Guards (in order):
      1. signature idempotency, source_ref ``drift_alerts:<id>:<type>`` already
         proposed once (ANY status incl. rejected) => never opens twice.
      2. pending pile-up, a still-pending remediation row for the same
         alert_type (earlier open-window) blocks a new open.
      3. daily cap, at most RECONCILE_DAILY_CAP opens per UTC day, counted
         from rows already inserted today with source=RECONCILE_SOURCE.
    """
    try:
        from validators import propose_to_inbox  # shared inbox writer (comms layer)
    except ImportError:
        print("drift_check: validators module not installed; reconcile skipped", file=sys.stderr)
        return {
            "opened": [],
            "skipped": [("*", "validators_module_missing")],
            "daily_cap": RECONCILE_DAILY_CAP,
            "opened_today_total": 0,
        }

    opened: list[dict] = []
    skipped: list[tuple[str, str]] = []

    today_opened = conn.execute(
        "SELECT COUNT(*) AS n FROM action_items_inbox "
        "WHERE source = ? AND DATE(proposed_at) = DATE('now')",
        (RECONCILE_SOURCE,),
    ).fetchone()["n"]

    rows = conn.execute(
        """
        SELECT id, alert_type, severity, summary, fire_count, first_fired_at
          FROM drift_alerts
         WHERE resolved_at IS NULL AND suppressed = 0
         ORDER BY CASE severity WHEN 'crit' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                  last_fired_at DESC
        """
    ).fetchall()

    for r in rows:
        signature = f"drift_alerts:{r['id']}:{r['alert_type']}"

        # Guard 1: this exact signature already opened a proposal once.
        if conn.execute(
            "SELECT 1 FROM action_items_inbox WHERE source = ? AND source_ref = ?",
            (RECONCILE_SOURCE, signature),
        ).fetchone():
            skipped.append((r["alert_type"], "already_opened"))
            continue

        # Guard 2: a pending remediation row for this alert_type still awaits
        # triage (earlier open-window), don't stack a second one.
        if conn.execute(
            "SELECT 1 FROM action_items_inbox "
            "WHERE source = ? AND status = 'pending' AND source_ref LIKE ?",
            (RECONCILE_SOURCE, f"drift_alerts:%:{r['alert_type']}"),
        ).fetchone():
            skipped.append((r["alert_type"], "pending_duplicate"))
            continue

        # Guard 3 (CAP): never more than RECONCILE_DAILY_CAP opens per UTC day.
        if today_opened >= RECONCILE_DAILY_CAP:
            skipped.append((r["alert_type"], "cap_deferred"))
            continue

        priority = {"crit": "P1", "warn": "P2"}.get(r["severity"], "P3")
        desc = (
            f"Drift remediation: {r['alert_type']} ({r['severity']}). "
            f"{r['summary']} Fired {r['fire_count']}x since {r['first_fired_at']}. "
            f"Auto-opened by drift_check --reconcile; diagnostics in "
            f"drift_alerts id={r['id']} detail_json."
        )
        inbox_id, skip_reason = propose_to_inbox(
            conn,
            RECONCILE_SOURCE,
            desc,
            priority=priority,
            context_tags="@system,@drift",
            source_ref=signature,
        )
        if inbox_id:
            today_opened += 1
            opened.append(
                {"inbox_id": inbox_id, "alert_type": r["alert_type"], "signature": signature}
            )
        else:
            skipped.append((r["alert_type"], f"refused:{skip_reason}"))

    return {
        "opened": opened,
        "skipped": skipped,
        "daily_cap": RECONCILE_DAILY_CAP,
        "opened_today_total": today_opened,
    }


def run_reconcile() -> dict:
    """Nightly entrypoint: refresh alert state (run_checks), then open capped
    remediation proposals. BEGIN IMMEDIATE + retry/backoff for the write txn."""
    import time  # noqa: PLC0415

    result = run_checks()  # own connection; commits alert upserts/resolves
    conn = _connect()
    try:
        rec: dict = {}
        for attempt in range(6):
            try:
                conn.execute("BEGIN IMMEDIATE")
                rec = reconcile_alerts(conn)
                conn.commit()
                break
            except sqlite3.OperationalError as e:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                if ("locked" in str(e).lower() or "busy" in str(e).lower()) and attempt < 5:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise
        result["reconcile"] = rec
        return result
    finally:
        conn.close()


def _list_open(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, alert_type, severity, summary, fire_count,
               first_fired_at, last_fired_at, suppressed
          FROM drift_alerts
         WHERE resolved_at IS NULL
         ORDER BY CASE severity WHEN 'crit' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                  last_fired_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _list_all(conn: sqlite3.Connection, n: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM drift_alerts
         ORDER BY COALESCE(resolved_at, last_fired_at) DESC
         LIMIT ?
        """,
        (n,),
    ).fetchall()
    return [dict(r) for r in rows]


def _suppress(alert_type: str, suppress: bool) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE drift_alerts SET suppressed = ? WHERE alert_type = ? AND resolved_at IS NULL",
            (1 if suppress else 0, alert_type),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _cli() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]

    if not args:
        result = run_checks()
        for at, action in result["actions"]:
            print(f"  {action:9s}  {at}")
        if not result["actions"]:
            print("  no drift detected")
        return 0

    if args[0] == "--json":
        print(json.dumps(run_checks(), indent=2, default=str))
        return 0

    if args[0] == "--reconcile":
        result = run_reconcile()
        rec = result.get("reconcile", {})
        for o in rec.get("opened", []):
            print(f"  opened    {o['alert_type']:30s} inbox_id={o['inbox_id']} sig={o['signature']}")
        for at, why in rec.get("skipped", []):
            print(f"  skipped   {at:30s} {why}")
        if not rec.get("opened") and not rec.get("skipped"):
            print("  no open alerts to reconcile")
        print(
            f"  total: opened={len(rec.get('opened', []))} "
            f"skipped={len(rec.get('skipped', []))} "
            f"opened_today={rec.get('opened_today_total', 0)}/{rec.get('daily_cap', RECONCILE_DAILY_CAP)}"
        )
        return 0

    if args[0] == "--open":
        conn = _connect()
        try:
            rows = _list_open(conn)
        finally:
            conn.close()
        if not rows:
            print("(no open alerts)")
            return 0
        for r in rows:
            mark = "[SUP] " if r["suppressed"] else ""
            print(f"  {r['severity']:4s} {mark}{r['alert_type']:30s} fires={r['fire_count']:>3} {r['summary']}")
        return 0

    if args[0] == "--all":
        n = int(args[1]) if len(args) > 1 else 50
        conn = _connect()
        try:
            rows = _list_all(conn, n)
        finally:
            conn.close()
        print(json.dumps(rows, indent=2, default=str))
        return 0

    if args[0] == "suppress" and len(args) > 1:
        n = _suppress(args[1], True)
        print(f"suppressed {n} row(s) for {args[1]}")
        return 0

    if args[0] == "unsuppress" and len(args) > 1:
        n = _suppress(args[1], False)
        print(f"unsuppressed {n} row(s) for {args[1]}")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
