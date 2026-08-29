"""Task management utility for the action_items table in ops.db.

Provides urgency scoring, smart surfacing, snooze/defer, dependency tracking,
recurrence, context batching, and resolution notes.

Usage:
    python task_manager.py focus                  # Smart daily focus (top 5-7 items)
    python task_manager.py urgency                # Recalculate all urgency scores
    python task_manager.py snooze AI-xxx "2026-04-10" "Waiting for replies"
    python task_manager.py unsnooze AI-xxx
    python task_manager.py depend AI-xxx AI-yyy   # AI-xxx depends on AI-yyy
    python task_manager.py undepend AI-xxx AI-yyy
    python task_manager.py recur AI-xxx "every 3d"
    python task_manager.py resolve AI-xxx "Reply received, invoice processed"
    python task_manager.py context AI-xxx "@email,@notion"
    python task_manager.py batch @email           # Show all items with context tag
    python task_manager.py stale                  # Show WAITING items past check-in date
    python task_manager.py estimate AI-xxx 15     # Set estimated minutes
    python task_manager.py overdue                # Show overdue items
    python task_manager.py surface                # Raw surfacing data (for hooks)
    python task_manager.py link-people            # Backfill waiting_on_person_id FK
    python task_manager.py reply-check            # Scan gmail for replies to WAITING items
    python task_manager.py dedup                  # Detect duplicate action items
    python task_manager.py sweep                  # Match git changes to open items
    python task_manager.py sweep --commits 5      # Limit to last 5 commits
    python task_manager.py sweep --threshold 0.6  # Adjust similarity threshold
    python task_manager.py autotag                # Auto-tag all untagged items
    python task_manager.py spawn                  # Create next occurrence of recurring items
    python task_manager.py unblock                # Auto-unblock items whose deps are done
    python task_manager.py checkin AI-xxx "note"   # Check in on a WAITING item
    python task_manager.py triage                 # Items needing human decision (snooze/cancel/re-date)
    python task_manager.py velocity               # Resolution velocity (7d/14d/30d)
"""
import paths
import io
import json
import math
import os
import sqlite3
import _db  # shared DB connector (busy_timeout + FK ON)
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
# Writes to action_items cascade into `entities` (mirror triggers), and the
# blocking write_gate ABORTs on a NULL actor. Stamp the actor around each
# mutating write so gated writes succeed.
from audit_actor import actor_scope, set_actor, clear_actor

# Windows encoding fix: reconfigure IN PLACE (never swap the stream object --
# replacing sys.stdout at import time discards the importer's unflushed output
# and breaks streams without .buffer; fatal for a stdio MCP host).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, io.UnsupportedOperation):
    pass

DB = str(paths.DB_PATH)


def get_conn():
    conn = _db.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _operator_username() -> str:
    """Review-stamp identity for inbox actions ([operator] name in config.toml)."""
    try:
        import tomllib
        cfg = paths.ROOT / "config.toml"
        if cfg.is_file():
            with open(cfg, "rb") as f:
                name = (tomllib.load(f).get("operator") or {}).get("name")
            if name and str(name).strip():
                return str(name).strip()
    except Exception:
        pass
    return "operator"


OPERATOR_USERNAME = _operator_username()


def _norm_prio(p):
    """Canonicalize a priority at a BYPASS INSERT (the spawn/recur paths
    don't call validate_action_item). Tolerant -- a legacy bare-numeric parent
    priority falls back to itself rather than crashing the batch; validate_action_item
    is the strict gate on the canonical add path. Closes the path for future callers."""
    try:
        from validators import normalize_priority
        return normalize_priority(p)
    except Exception:  # noqa: BLE001
        return p


# ---------------------------------------------------------------------------
# Urgency scoring -- stakeholder-weighted
# ---------------------------------------------------------------------------
# Tier weights drive the order. Priority is now a residual operator override.
#
# Tier 0 (manager explicit-tag): +30
# Tier 1 (manager general from/cc/mention with ask): +20
# Tier 2 (external partner / collaborator): +15
# Tier 3 (contact on a current or imminent project/event): +10
# Tier 4 (other/internal/unknown/past): +2
TIER_WEIGHTS = {0: 30.0, 1: 20.0, 2: 15.0, 3: 10.0, 4: 2.0}

# Emergency keywords in the description signal a critical item; small extra bump.
EMERGENCY_KEYWORDS = (
    "behind on signups", "way behind", "blocking", "blocker",
    "urgent", "critical", "asap", "must launch", "must ship",
    "deadline today", "deadline is today",
)


def compute_urgency(row, now=None):
    """Stakeholder-first urgency.

    Order of magnitude:
      base   = tier_weight (2..30) + stage_boost (0..14) + priority residual (0..6)
      bump   = overdue curve, due-soon, stale-waiting (each capped)
      penalty = BLOCKED, dependency
      age    = tiny tail-end weight; >10d items get a small bump only (real signal
               is the new stale review queue, not score creep)
    """
    if now is None:
        now = datetime.now()

    score = 0.0

    tier = row.get("stakeholder_tier")
    if tier is None:
        tier = 4
    score += TIER_WEIGHTS.get(int(tier), 2.0)

    stage = row.get("stage_boost") or 0
    score += float(stage)

    # Priority is now a residual operator-override (P0 still beats P3 within same tier)
    priority_map = {"P0": 6, "P1": 3, "P2": 1, "P3": 0}
    score += priority_map.get(row["priority"] or "P2", 1)

    # Overdue / due-soon
    if row["due_date"]:
        try:
            due = datetime.strptime(row["due_date"][:10], "%Y-%m-%d")
            days_until = (due - now).days
            if days_until < 0:
                score += min(math.sqrt(abs(days_until)) * 4, 15)
            elif days_until <= 3:
                score += (3 - days_until) * 1
        except ValueError:
            pass

    # Stale WAITING (still load-bearing)
    if row["status"] == "WAITING":
        check_date = row.get("last_checked_at") or row.get("updated_at") or row.get("inserted_at")
        if check_date:
            try:
                last = datetime.fromisoformat(check_date)
                days_stale = (now - last).days
                if days_stale > 2:
                    score += min((days_stale - 2) * 1.5, 12)
            except (ValueError, TypeError):
                pass

    if row["status"] == "BLOCKED":
        score -= 3

    # Age: small tail bump only. Real handling is the stale review queue.
    if row.get("inserted_at"):
        try:
            created = datetime.fromisoformat(row["inserted_at"])
            age_days = (now - created).days
            score += min(age_days * 0.05, 4)
        except (ValueError, TypeError):
            pass

    if row.get("depends_on"):
        score -= 1

    desc = (row.get("description") or "").lower()
    if any(kw in desc for kw in EMERGENCY_KEYWORDS):
        score += 5

    return round(max(score, 0), 2)


def update_all_urgency_scores():
    """Refresh stakeholder_tier (if null), stage_boost, and urgency_score for every active item."""
    import stakeholder as _sh
    # The active-events API is optional in a minimal stakeholder build; the
    # stage boost degrades to 0 when it is absent (e.g. no events table yet).
    _get_active = (getattr(_sh, "get_active_events", None)
                   or getattr(_sh, "get_active_hackathons", None))
    _stage_boost = getattr(_sh, "compute_stage_boost", None)

    conn = get_conn()
    now = datetime.now()
    active = _get_active(conn, now=now) if _get_active else {}
    set_actor(conn, "task_manager:urgency")

    rows = conn.execute("""
        SELECT id, item_id, status, priority, due_date, waiting_on,
               last_checked_at, updated_at, inserted_at, depends_on,
               description, source_type, source_person_id, about_person_id,
               context_slug, org_entity_id,
               stakeholder_tier, partner_kind, is_manager_explicit
        FROM action_items
        WHERE status IN ('OPEN', 'WAITING', 'BLOCKED')
    """).fetchall()

    updated = 0
    for row in rows:
        item = dict(row)
        # Always re-derive (cheap; keeps tagging in sync with code changes + new role data)
        tier, kind, manager_exp = _sh.derive_stakeholder(item, conn, active)
        if (item.get("stakeholder_tier") != tier or
                item.get("partner_kind") != kind or
                bool(item.get("is_manager_explicit")) != bool(manager_exp)):
            conn.execute(
                "UPDATE action_items SET stakeholder_tier=?, partner_kind=?, is_manager_explicit=? WHERE id=?",
                (tier, kind, manager_exp, item["id"]),
            )
        item["stakeholder_tier"] = tier
        item["partner_kind"] = kind
        item["is_manager_explicit"] = manager_exp
        # Recompute stage boost every run (event dates can shift)
        stage_boost = (_stage_boost(item.get("context_slug"), active, now=now)
                       if _stage_boost else 0)
        if (item.get("stage_boost") or 0) != stage_boost:
            conn.execute(
                "UPDATE action_items SET stage_boost=? WHERE id=?",
                (stage_boost, item["id"]),
            )
            item["stage_boost"] = stage_boost
        score = compute_urgency(item, now)
        conn.execute(
            "UPDATE action_items SET urgency_score = ? WHERE id = ?",
            (score, item["id"]),
        )
        updated += 1

    conn.commit()
    clear_actor(conn)
    conn.close()
    return updated


# ---------------------------------------------------------------------------
# Smart surfacing (the killer feature)
# ---------------------------------------------------------------------------
def get_focus_items(max_items=7, output_format="text", include_personal=False):
    """Get the daily focus list: the 5-7 most important items right now.

    Selection logic:
    1. Overdue items (always first)
    2. Items with meetings/deadlines today
    3. Highest urgency score
    4. WAITING items where check-in is due (>3 days stale)
    5. Quick wins (<15 min estimated)

    Excludes snoozed items.

    When ROUTING_V2=1 (env), also excludes personal/family-domain items unless
    include_personal=True. NULL/general/work/public domains always surface.
    """
    conn = get_conn()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    routing_v2 = os.environ.get("ROUTING_V2", "0") == "1"
    domain_filter = (
        " AND COALESCE(domain,'general') IN ('work','public','general')"
        if routing_v2 and not include_personal else ""
    )

    # Make sure stakeholder + stage boost + urgency are fresh (idempotent + cheap)
    update_all_urgency_scores()

    rows = conn.execute(f"""
        SELECT id, item_id, status, priority, description, due_date, waiting_on,
               last_checked_at, updated_at, inserted_at, depends_on,
               snoozed_until, estimated_minutes, context_tags, urgency_score,
               COALESCE(domain,'general') as domain,
               source_url, source_ref, source_type, source_person_id,
               about_person_id, context_slug,
               stakeholder_tier, partner_kind, is_manager_explicit,
               stage_boost, source_quote, org_entity_id, subtasks_json
        FROM action_items
        WHERE status IN ('OPEN', 'WAITING', 'BLOCKED')
          AND (snoozed_until IS NULL OR snoozed_until <= ?)
          {domain_filter}
        ORDER BY urgency_score DESC
    """, (today,)).fetchall()

    scored = []
    for row in rows:
        score = compute_urgency(dict(row), now)
        scored.append((score, dict(row)))

    conn.commit()

    # Sort by score descending. Urgency already encodes stakeholder + stage + overdue + staleness,
    # so the highest-scored item is the right item to surface, regardless of category.
    scored.sort(key=lambda x: -x[0])

    focus = scored[:max_items]
    remaining = len(scored) - len(focus)

    conn.close()

    if output_format == "json":
        return json.dumps({
            "focus": [{"score": s, "item_id": i.get("item_id", ""), "status": i["status"],
                       "priority": i.get("priority", ""), "description": i["description"][:120],
                       "due_date": i.get("due_date", ""), "waiting_on": i.get("waiting_on", ""),
                       "context_tags": i.get("context_tags", "")}
                      for s, i in focus],
            "remaining": remaining,
            "total_active": len(scored),
        }, indent=2)

    # Inbox pending count (surfaces it so review isn't forgotten)
    conn2 = get_conn()
    inbox_pending = conn2.execute(
        "SELECT COUNT(*) c FROM action_items_inbox WHERE status='pending'"
    ).fetchone()["c"]
    inbox_oldest_days = conn2.execute(
        "SELECT CAST(julianday('now') - julianday(MIN(proposed_at)) AS INTEGER) d "
        "FROM action_items_inbox WHERE status='pending'"
    ).fetchone()["d"] if inbox_pending else 0
    drafts_pending = conn2.execute(
        "SELECT COUNT(*) c FROM email_drafts WHERE status = 'auto-suggested'"
    ).fetchone()["c"]
    conn2.close()

    # Text output
    lines = []
    if inbox_pending > 50:
        lines.append(
            f"!! INBOX BACKLOG: {inbox_pending} pending proposals (oldest {inbox_oldest_days}d) -> "
            f"triage with `task_manager.py inbox list`"
        )
    elif inbox_pending > 0:
        lines.append(f"INBOX: {inbox_pending} pending proposals -> review with `task_manager.py inbox`")
    if drafts_pending > 0:
        lines.append(
            f">> AUTO-DRAFTS: {drafts_pending} reply drafts ready for review -> "
            f"`task_manager.py drafts list`"
        )
    # RECURRING? banner: a task resolved >=3 times by the same short title with
    # no recurrence_rule is probably recurring -- suggest `recur`. Fail-open.
    try:
        rec_cands = conn.execute("""
            SELECT substr(lower(trim(description)),1,40) AS k, COUNT(*) AS n
              FROM action_items
             WHERE status IN ('DONE','RESOLVED','COMPLETED')
               AND (recurrence_rule IS NULL OR recurrence_rule='')
               AND description IS NOT NULL AND length(trim(description))>=8
             GROUP BY k HAVING n >= 3
             ORDER BY n DESC LIMIT 3""").fetchall()
        for rc in rec_cands:
            lines.append(
                f"RECURRING? \"{(rc[0] or '').strip()[:40]}\" resolved {rc[1]}x with no recurrence -> "
                f"consider `task_manager.py recur <item> \"every Nd\"`"
            )
    except Exception:
        pass
    lines.append(f"DAILY FOCUS ({len(focus)} items, {remaining} more in backlog):")

    for score, item in focus:
        status = item["status"]
        priority = item.get("priority", "P2")
        desc = (item["description"] or "")[:100]
        item_id = item.get("item_id") or f"#{item['id']}"
        due = item.get("due_date", "")
        waiting = item.get("waiting_on", "")

        flags = []
        if due:
            try:
                due_dt = datetime.strptime(due[:10], "%Y-%m-%d")
                if due_dt.date() < now.date():
                    flags.append("OVERDUE")
                elif due_dt.strftime("%Y-%m-%d") == today:
                    flags.append("DUE TODAY")
            except ValueError:
                pass
        if status == "WAITING" and not any(f in flags for f in ["OVERDUE"]):
            flags.append(f"WAITING on {waiting}" if waiting else "WAITING")
        if status == "BLOCKED":
            flags.append("BLOCKED")
        if item.get("estimated_minutes") and item["estimated_minutes"] <= 15:
            flags.append(f"~{item['estimated_minutes']}min")

        # Stakeholder tag - the highest-signal field, goes first
        tier = item.get("stakeholder_tier")
        kind = item.get("partner_kind") or ""
        manager_explicit = item.get("is_manager_explicit")
        if tier == 0 or manager_explicit:
            tier_tag = "T0 MANAGER-TAG"
        elif tier == 1:
            tier_tag = "T1 MANAGER"
        elif tier == 2:
            tier_tag = f"T2 {kind.upper()}" if kind else "T2 PARTNER"
        elif tier == 3:
            tier_tag = "T3 event"
        else:
            tier_tag = "T4 other"
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"  [{tier_tag}] [{priority}] {desc}{flag_str}")
        lines.append(f"        {item_id} | urgency: {score}")
        # Source quote, if we captured one -- this is the actual sentence that triggered the item
        if item.get("source_quote"):
            quote = item["source_quote"][:160].replace("\n", " ").strip()
            lines.append(f"        QUOTE: \"{quote}\"")
        # Provenance (source link)
        prov_bits = []
        if item.get("source_type"):
            prov_bits.append(item["source_type"])
        if item.get("source_url"):
            prov_bits.append(item["source_url"])
        elif item.get("source_ref"):
            prov_bits.append(item["source_ref"])
        if prov_bits:
            lines.append(f"        SRC: {' | '.join(prov_bits)}")
        # Who + context
        people_bits = []
        if item.get("source_person_id"):
            people_bits.append(f"from pid={item['source_person_id']}")
        if item.get("about_person_id") and item["about_person_id"] != item.get("source_person_id"):
            people_bits.append(f"about pid={item['about_person_id']}")
        if item.get("context_slug"):
            stage_b = item.get("stage_boost") or 0
            stage_str = f" stage+{int(stage_b)}" if stage_b else ""
            people_bits.append(f"#{item['context_slug']}{stage_str}")
        if people_bits:
            lines.append(f"        WHO: {' | '.join(people_bits)}")
        # Subtasks + contingency notes
        steps = _load_subtasks(item.get("subtasks_json"))
        if steps:
            done_n = sum(1 for s in steps if s["done"])
            lines.append(f"        STEPS ({done_n}/{len(steps)}):")
            for idx, s in enumerate(steps, 1):
                chk = "[x]" if s["done"] else "[ ]"
                note = f"   (if: {s['note']})" if s["note"] else ""
                lines.append(f"          {idx}. {chk} {s['text'][:90]}{note}")

    if remaining > 0:
        lines.append(f"\n  + {remaining} more items (run: task_manager.py urgency)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Snooze / Defer
# ---------------------------------------------------------------------------
def _touch_checked(conn, item_id):
    """Update last_checked_at for a WAITING item when it's interacted with."""
    conn.execute(
        "UPDATE action_items SET last_checked_at = datetime('now') WHERE item_id = ? AND status = 'WAITING'",
        (item_id,)
    )


def checkin_item(item_id, note=""):
    """Mark a WAITING item as checked without resolving it."""
    conn = get_conn()
    updates = "last_checked_at = datetime('now'), updated_at = datetime('now')"
    params = [item_id]
    if note:
        updates = "last_checked_at = datetime('now'), updated_at = datetime('now'), resolution_note = ?"
        params = [note, item_id]
    result = conn.execute(
        f"UPDATE action_items SET {updates} WHERE item_id = ? AND status = 'WAITING'",
        params
    )
    if result.rowcount == 0:
        conn.close()
        return f"No WAITING item found: {item_id}"
    conn.commit()
    conn.close()
    return f"Checked in on {item_id}" + (f" ({note})" if note else "")


def snooze_item(item_id, until_date, reason=""):
    conn = get_conn()
    with actor_scope(conn, "task_manager:snooze", source_ref=item_id):
        result = conn.execute(
            "UPDATE action_items SET snoozed_until = ?, snooze_reason = ?, last_checked_at = datetime('now'), updated_at = datetime('now') WHERE item_id = ?",
            (until_date, reason, item_id)
        )
    if result.rowcount == 0:
        conn.close()
        return f"No item found with item_id '{item_id}'"
    conn.commit()
    conn.close()
    return f"Snoozed {item_id} until {until_date}" + (f" ({reason})" if reason else "")


def unsnooze_item(item_id):
    conn = get_conn()
    with actor_scope(conn, "task_manager:unsnooze", source_ref=item_id):
        result = conn.execute(
            "UPDATE action_items SET snoozed_until = NULL, snooze_reason = NULL, updated_at = datetime('now') WHERE item_id = ?",
            (item_id,)
        )
    if result.rowcount == 0:
        conn.close()
        return f"No item found with item_id '{item_id}'"
    conn.commit()
    conn.close()
    return f"Unsnoozed {item_id}"


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
def add_dependency(item_id, depends_on_id):
    conn = get_conn()
    row = conn.execute("SELECT depends_on FROM action_items WHERE item_id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return f"No item found: {item_id}"

    current = row["depends_on"] or ""
    deps = [d.strip() for d in current.split(",") if d.strip()]
    if depends_on_id not in deps:
        deps.append(depends_on_id)

    with actor_scope(conn, "task_manager:add_dependency", source_ref=item_id):
        conn.execute(
            "UPDATE action_items SET depends_on = ?, updated_at = datetime('now') WHERE item_id = ?",
            (",".join(deps), item_id)
        )
    conn.commit()
    conn.close()
    return f"{item_id} now depends on: {', '.join(deps)}"


def remove_dependency(item_id, depends_on_id):
    conn = get_conn()
    row = conn.execute("SELECT depends_on FROM action_items WHERE item_id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return f"No item found: {item_id}"

    current = row["depends_on"] or ""
    deps = [d.strip() for d in current.split(",") if d.strip() and d.strip() != depends_on_id]

    with actor_scope(conn, "task_manager:remove_dependency", source_ref=item_id):
        conn.execute(
            "UPDATE action_items SET depends_on = ?, updated_at = datetime('now') WHERE item_id = ?",
            (",".join(deps) if deps else None, item_id)
        )
    conn.commit()
    conn.close()
    return f"Removed dependency {depends_on_id} from {item_id}"


def check_unblocked():
    """Find items whose dependencies are all resolved. Auto-unblock them."""
    conn = get_conn()
    items_with_deps = conn.execute("""
        SELECT item_id, depends_on FROM action_items
        WHERE status IN ('OPEN', 'WAITING', 'BLOCKED')
          AND depends_on IS NOT NULL AND depends_on != ''
    """).fetchall()

    done_ids = set()
    done_rows = conn.execute("SELECT item_id FROM action_items WHERE status = 'DONE'").fetchall()
    for r in done_rows:
        if r["item_id"]:
            done_ids.add(r["item_id"])

    unblocked = []
    set_actor(conn, "task_manager:check_unblocked")
    for item in items_with_deps:
        deps = [d.strip() for d in item["depends_on"].split(",") if d.strip()]
        if all(d in done_ids for d in deps):
            conn.execute(
                "UPDATE action_items SET depends_on = NULL, updated_at = datetime('now') WHERE item_id = ?",
                (item["item_id"],)
            )
            unblocked.append(item["item_id"])

    conn.commit()
    clear_actor(conn)
    conn.close()
    return unblocked


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------
def set_recurrence(item_id, rule):
    """Set recurrence rule. Examples: 'every 3d', 'weekly Mon', 'monthly 1'."""
    conn = get_conn()
    with actor_scope(conn, "task_manager:set_recurrence", source_ref=item_id):
        result = conn.execute(
            "UPDATE action_items SET recurrence_rule = ?, updated_at = datetime('now') WHERE item_id = ?",
            (rule, item_id)
        )
    if result.rowcount == 0:
        conn.close()
        return f"No item found: {item_id}"
    conn.commit()
    conn.close()
    return f"Set recurrence for {item_id}: {rule}"


def _parse_recurrence(rule):
    """Parse recurrence rule into timedelta or next date."""
    rule = rule.strip().lower()
    if rule.startswith("every "):
        part = rule[6:].strip()
        if part.endswith("d"):
            return timedelta(days=int(part[:-1]))
        elif part.endswith("w"):
            return timedelta(weeks=int(part[:-1]))
        elif part.endswith("h"):
            return timedelta(hours=int(part[:-1]))
    elif rule.startswith("weekly"):
        return timedelta(weeks=1)
    elif rule.startswith("daily"):
        return timedelta(days=1)
    elif rule.startswith("monthly"):
        return timedelta(days=30)
    return None


def spawn_recurring():
    """Check completed items with recurrence rules, create next occurrence."""
    conn = get_conn()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    recurring_done = conn.execute("""
        SELECT * FROM action_items
        WHERE status = 'DONE'
          AND recurrence_rule IS NOT NULL AND recurrence_rule != ''
          AND (next_occurrence IS NULL OR next_occurrence <= ?)
    """, (today,)).fetchall()

    created = []
    set_actor(conn, "task_manager:spawn_recurring")
    for item in recurring_done:
        delta = _parse_recurrence(item["recurrence_rule"])
        if not delta:
            continue

        # Calculate next due date
        base = item["completed_at"] or item["due_date"] or today
        try:
            base_dt = datetime.fromisoformat(base) if "T" in base else datetime.strptime(base[:10], "%Y-%m-%d")
        except ValueError:
            base_dt = now

        next_due = (base_dt + delta).strftime("%Y-%m-%d")

        # Generate new item_id
        new_id = f"{item['item_id']}-R{next_due.replace('-', '')}"

        # Check if already spawned
        existing = conn.execute("SELECT 1 FROM action_items WHERE item_id = ?", (new_id,)).fetchone()
        if existing:
            continue

        conn.execute("""
            INSERT INTO action_items (item_id, status, priority, description, due_date,
                                      waiting_on, context, source, context_slug,
                                      recurrence_rule, context_tags, estimated_minutes,
                                      inserted_at, updated_at)
            VALUES (?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """, (new_id, _norm_prio(item["priority"]), item["description"], next_due,
              item["waiting_on"], item["context"],
              f"Recurring from {item['item_id']}", item["context_slug"],
              item["recurrence_rule"], item["context_tags"], item["estimated_minutes"]))

        # Update parent's next_occurrence
        conn.execute(
            "UPDATE action_items SET next_occurrence = ? WHERE item_id = ?",
            (next_due, item["item_id"])
        )
        created.append(new_id)

    conn.commit()
    clear_actor(conn)
    conn.close()
    return created


# ---------------------------------------------------------------------------
# Resolution notes
# ---------------------------------------------------------------------------
def resolve_item(item_id, note):
    conn = get_conn()
    with actor_scope(conn, "task_manager:resolve", source_ref=item_id):
        result = conn.execute(
            """UPDATE action_items
               SET resolution_note = ?,
                   status = 'DONE',
                   completed_at = datetime('now'),
                   updated_at = datetime('now'),
                   snoozed_until = NULL,
                   snooze_reason = NULL
               WHERE item_id = ?""",
            (note, item_id)
        )
    if result.rowcount == 0:
        conn.close()
        return f"No item found: {item_id}"

    conn.commit()
    conn.close()

    # Side effects: unblock dependents and spawn recurrences
    unblocked = check_unblocked()
    spawned = spawn_recurring()

    parts = [f"Resolved {item_id} -> DONE"]
    if unblocked:
        parts.append(f"Unblocked: {', '.join(unblocked)}")
    if spawned:
        parts.append(f"Spawned: {', '.join(spawned)}")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Context tags
# ---------------------------------------------------------------------------
def set_context(item_id, tags):
    conn = get_conn()
    with actor_scope(conn, "task_manager:set_context", source_ref=item_id):
        result = conn.execute(
            "UPDATE action_items SET context_tags = ?, updated_at = datetime('now') WHERE item_id = ?",
            (tags, item_id)
        )
    if result.rowcount == 0:
        conn.close()
        return f"No item found: {item_id}"
    _touch_checked(conn, item_id)
    conn.commit()
    conn.close()
    return f"Context tags for {item_id}: {tags}"


def batch_by_context(tag):
    """Get all active items with a specific context tag."""
    conn = get_conn()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT item_id, status, priority, description, due_date, waiting_on, urgency_score
        FROM action_items
        WHERE status IN ('OPEN', 'WAITING', 'BLOCKED')
          AND context_tags LIKE ?
          AND (snoozed_until IS NULL OR snoozed_until <= ?)
        ORDER BY urgency_score DESC
    """, (f"%{tag}%", today)).fetchall()
    conn.close()

    if not rows:
        return f"No active items with context tag '{tag}'"

    lines = [f"BATCH: {tag} ({len(rows)} items)"]
    for r in rows:
        lines.append(f"  [{r['priority'] or 'P2'}] {(r['description'] or '')[:90]}")
        lines.append(f"        {r['item_id']} | urgency: {r['urgency_score']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stale / overdue views
# ---------------------------------------------------------------------------
def show_stale():
    """Show WAITING items that haven't been checked in >3 days."""
    conn = get_conn()
    now = datetime.now()
    rows = conn.execute("""
        SELECT item_id, priority, description, waiting_on,
               last_checked_at, updated_at, inserted_at
        FROM action_items
        WHERE status = 'WAITING'
    """).fetchall()
    conn.close()

    stale = []
    for r in rows:
        check_date = r["last_checked_at"] or r["updated_at"] or r["inserted_at"]
        if check_date:
            try:
                last = datetime.fromisoformat(check_date)
                days = (now - last).days
                if days >= 3:
                    stale.append((days, dict(r)))
            except (ValueError, TypeError):
                pass

    if not stale:
        return "No stale WAITING items (all checked within 3 days)"

    stale.sort(key=lambda x: -x[0])
    lines = [f"STALE WAITING ITEMS ({len(stale)} need check-in):"]
    for days, item in stale:
        lines.append(f"  [{item['priority'] or 'P2'}] {(item['description'] or '')[:80]}")
        lines.append(f"        {item['item_id']} | {days}d since last check | waiting on: {item['waiting_on'] or '?'}")
    return "\n".join(lines)


def show_overdue():
    """Show items past their due date."""
    conn = get_conn()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT item_id, priority, description, due_date, status, waiting_on
        FROM action_items
        WHERE status IN ('OPEN', 'WAITING', 'BLOCKED')
          AND due_date IS NOT NULL AND due_date < ?
        ORDER BY due_date ASC
    """, (today,)).fetchall()
    conn.close()

    if not rows:
        return "No overdue items"

    lines = [f"OVERDUE ({len(rows)} items):"]
    for r in rows:
        lines.append(f"  [{r['priority'] or 'P2'}] [{r['status']}] {(r['description'] or '')[:80]}")
        lines.append(f"        {r['item_id']} | due: {r['due_date']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Estimate
# ---------------------------------------------------------------------------
def set_estimate(item_id, minutes):
    conn = get_conn()
    result = conn.execute(
        "UPDATE action_items SET estimated_minutes = ?, updated_at = datetime('now') WHERE item_id = ?",
        (int(minutes), item_id)
    )
    if result.rowcount == 0:
        conn.close()
        return f"No item found: {item_id}"
    conn.commit()
    conn.close()
    return f"Estimate for {item_id}: {minutes} minutes"


# ---------------------------------------------------------------------------
# Email reply monitor (cross-reference gmail-to-sqlite)
# ---------------------------------------------------------------------------
# Optional gmail-to-sqlite mirror; None until configured ([sync] gmail_db_path
# in config.toml, or the OPS_GMAIL_DB env var).
GMAIL_DB = str(paths.GMAIL_DB_PATH) if paths.GMAIL_DB_PATH else None


def reply_check():
    """Scan gmail-to-sqlite for replies from people we're WAITING on.

    For each WAITING item with an email in waiting_on, checks if any
    incoming email arrived from that address since last_checked_at.
    """
    import re, json as _json
    if not GMAIL_DB:
        return ("reply-check needs a gmail-to-sqlite mirror: set gmail_db_path "
                "in config.toml (or OPS_GMAIL_DB) to enable it")
    conn = get_conn()

    items = conn.execute("""
        SELECT item_id, waiting_on, last_checked_at, updated_at, inserted_at
        FROM action_items
        WHERE status = 'WAITING'
          AND waiting_on IS NOT NULL AND waiting_on != ''
    """).fetchall()

    # Extract emails from waiting_on
    to_check = []
    for item in items:
        m = re.search(r'[\w.+-]+@[\w.-]+\.\w+', item["waiting_on"] or "")
        if m:
            since = item["last_checked_at"] or item["updated_at"] or item["inserted_at"]
            to_check.append((item["item_id"], m.group(0).lower(), since))

    if not to_check:
        conn.close()
        return "No WAITING items with email addresses to check"

    # Query gmail-to-sqlite
    try:
        gmail = sqlite3.connect(GMAIL_DB)
    except Exception:
        conn.close()
        return f"Cannot open gmail DB: {GMAIL_DB}"

    replies = []
    for item_id, email, since in to_check:
        rows = gmail.execute("""
            SELECT sender, subject, timestamp FROM messages
            WHERE is_outgoing = 0 AND timestamp > ? AND sender LIKE ?
            ORDER BY timestamp DESC LIMIT 3
        """, (since, f'%{email}%')).fetchall()

        for row in rows:
            try:
                sender = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
                sender_email = (sender.get("email") or "").lower()
            except (ValueError, AttributeError):
                continue
            if sender_email == email:
                replies.append({
                    "item_id": item_id,
                    "from": email,
                    "subject": row[1],
                    "date": row[2],
                })
                # Update last_checked_at since we've now verified
                conn.execute(
                    "UPDATE action_items SET last_checked_at = datetime('now') WHERE item_id = ?",
                    (item_id,)
                )
                break  # One match is enough per item

    gmail.close()
    conn.commit()
    conn.close()

    if not replies:
        return f"Checked {len(to_check)} WAITING items - no new replies found"

    lines = [f"REPLIES FOUND ({len(replies)} of {len(to_check)} checked):"]
    for r in replies:
        lines.append(f"  {r['item_id']} <- {r['from']}")
        lines.append(f"        Subject: {r['subject'][:80]}")
        lines.append(f"        Date: {r['date']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Person linking (waiting_on -> waiting_on_person_id)
# ---------------------------------------------------------------------------
def _resolve_person_id(conn, waiting_on_text):
    """Try to match waiting_on text to a people.id. Returns int or None."""
    if not waiting_on_text:
        return None
    import re
    # Try email in parentheses: "Name (email@domain.com)"
    m = re.search(r'\(([^)]+@[^)]+)\)', waiting_on_text)
    if m:
        email = m.group(1).strip().lower()
        row = conn.execute("SELECT id FROM people WHERE LOWER(email) = ? LIMIT 1", (email,)).fetchone()
        if row:
            return row["id"]
    # Try name match (skip org-like strings)
    name = re.sub(r'\s*\(.*?\)', '', waiting_on_text).strip()
    # Non-person placeholder strings that show up in waiting_on; extend with
    # your own vendor/team labels (e.g. 'payments team', 'vendor support').
    skip = {'none', 'n/a', 'tbd', 'team', 'support'}
    if name and len(name) > 3 and '@' not in name and name.lower() not in skip:
        row = conn.execute("SELECT id FROM people WHERE LOWER(name) = ? LIMIT 1", (name.lower(),)).fetchone()
        if row:
            return row["id"]
    return None


def link_people():
    """Backfill waiting_on_person_id for all items missing it."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, item_id, waiting_on FROM action_items
        WHERE waiting_on IS NOT NULL AND waiting_on != ''
          AND waiting_on_person_id IS NULL
    """).fetchall()
    matched = 0
    for row in rows:
        pid = _resolve_person_id(conn, row["waiting_on"])
        if pid:
            conn.execute("UPDATE action_items SET waiting_on_person_id = ? WHERE id = ?", (pid, row["id"]))
            matched += 1
    conn.commit()
    conn.close()
    return matched, len(rows)


# ---------------------------------------------------------------------------
# Hook-friendly surface (for session_lifecycle.py)
# ---------------------------------------------------------------------------
def surface_for_hook(max_items=7):
    """Return structured focus data for session start hook. JSON output."""
    return get_focus_items(max_items=max_items, output_format="json")


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
def _word_set(text):
    """Extract lowercase word tokens for fuzzy matching."""
    import re
    return set(re.findall(r'[a-z0-9]+', (text or "").lower())) - {
        'the', 'a', 'an', 'and', 'or', 'for', 'to', 'in', 'of', 'on', 'is', 'with', 'from',
        'at', 'by', 'as', 'it', 'be', 'not', 'no', 'has', 'was', 'are', 'this', 'that',
    }


def find_duplicates(threshold=0.55):
    """Detect potential duplicate action items among active items.

    Uses Jaccard similarity on description word tokens.
    Returns pairs with similarity >= threshold.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT item_id, description, waiting_on, due_date
        FROM action_items
        WHERE status IN ('OPEN', 'WAITING', 'BLOCKED')
    """).fetchall()
    conn.close()

    items = [(dict(r), _word_set(r["description"])) for r in rows]
    dupes = []

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a_item, a_words = items[i]
            b_item, b_words = items[j]
            if not a_words or not b_words:
                continue
            intersection = a_words & b_words
            union = a_words | b_words
            sim = len(intersection) / len(union) if union else 0
            if sim >= threshold:
                dupes.append((sim, a_item, b_item))

    dupes.sort(key=lambda x: -x[0])

    if not dupes:
        return "No potential duplicates found"

    lines = [f"POTENTIAL DUPLICATES ({len(dupes)} pairs, threshold {threshold}):"]
    for sim, a, b in dupes:
        lines.append(f"  {sim:.0%} match:")
        lines.append(f"    {a['item_id']}: {(a['description'] or '')[:70]}")
        lines.append(f"    {b['item_id']}: {(b['description'] or '')[:70]}")
    return "\n".join(lines)


def _sweep_words(text):
    """Extract keywords for sweep matching. Stricter than _word_set:
    filters out numbers, short tokens (<3 chars), and domain-common words."""
    import re
    tokens = set(re.findall(r'[a-z]{3,}', (text or "").lower()))
    noise = {
        # English function words
        'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can', 'had',
        'her', 'was', 'one', 'our', 'out', 'has', 'get', 'set', 'may', 'its',
        'let', 'say', 'she', 'too', 'use', 'way', 'who', 'how', 'any', 'new',
        'now', 'also', 'been', 'call', 'each', 'from', 'have', 'into', 'just',
        'like', 'make', 'many', 'more', 'most', 'much', 'must', 'name', 'only',
        'over', 'some', 'such', 'take', 'than', 'that', 'them', 'then', 'this',
        'very', 'when', 'what', 'will', 'with', 'about', 'after', 'being',
        'could', 'every', 'first', 'their', 'there', 'these', 'thing', 'think',
        'those', 'until', 'where', 'which', 'while', 'would', 'other', 'still',
        'should', 'before',
        # Domain-common: words that appear in almost every diff AND action
        # item in this workspace (extend with your own org's noise words)
        'email', 'discord', 'notion',
        'project', 'review', 'update', 'check', 'send', 'create',
        'add', 'org', 'run', 'per', 'via', 'follow',
    }
    return tokens - noise


def sweep_completed(num_commits=20, threshold=0.50, max_results=15):
    """Match recent git changes against open action items to find items that may already be done.

    Uses one-directional coverage: fraction of item keywords found in diff text.
    Proper noun matches (person names) get a bonus. Domain-common words filtered.
    """
    import re
    num_commits = max(1, num_commits)
    repo_root = str(paths.ROOT)
    run_kw = dict(capture_output=True, text=True, errors="replace", cwd=repo_root)

    # Determine a valid diff base (HEAD~N may exceed history)
    try:
        result = subprocess.run(["git", "rev-list", "--count", "HEAD"], **run_kw)
    except FileNotFoundError:
        return "SWEEP: git not found on PATH."
    if result.returncode != 0:
        return "SWEEP: not a git repository or git error."
    total = result.stdout.strip()
    max_back = max(int(total) - 1, 0) if total.isdigit() else num_commits
    lookback = min(num_commits, max_back)
    if lookback == 0:
        return "SWEEP: Not enough git history to compare."
    diff_base = f"HEAD~{lookback}"

    # Gather git context
    commits = subprocess.run(
        ["git", "log", f"-{num_commits}", "--pretty=%s", "--no-merges"], **run_kw
    ).stdout
    stat = subprocess.run(
        ["git", "diff", diff_base, "--stat"], **run_kw
    ).stdout
    diff = subprocess.run(
        ["git", "diff", diff_base, "--", "*.html", "*.md", "*.py", "*.json", "*.csv"],
        **run_kw
    ).stdout
    truncated = len(diff) > 100_000
    if truncated:
        diff = diff[:100_000]

    raw_text = f"{commits}\n{stat}\n{diff}"
    diff_words = _sweep_words(raw_text)

    if not diff_words:
        return "SWEEP: No git changes found to match against."

    # Get open items
    conn = get_conn()
    rows = conn.execute("""
        SELECT item_id, description, status, waiting_on, due_date
        FROM action_items
        WHERE status IN ('OPEN', 'WAITING')
    """).fetchall()
    conn.close()

    if not rows:
        return "SWEEP: No open/waiting items to check."

    candidates = []
    for row in rows:
        desc = row["description"] or ""
        item_words = _sweep_words(desc)
        if len(item_words) < 3:
            continue  # Too few meaningful keywords

        # Coverage: what fraction of item keywords appear in diff
        matched = item_words & diff_words
        coverage = len(matched) / len(item_words)

        # Proper noun bonus: multi-word names in description found in diff
        multi_names = re.findall(r'\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+)\b', desc)
        name_bonus = 0.0
        matched_names = []
        for nm in multi_names:
            if nm in raw_text:
                name_bonus += 0.15
                matched_names.append(nm)
        name_bonus = min(name_bonus, 0.3)

        score = min(coverage + name_bonus, 1.0)

        if score >= threshold:
            candidates.append({
                "score": score,
                "item_id": row["item_id"],
                "description": desc,
                "matched": sorted(matched),
                "matched_names": matched_names,
                "total": len(item_words),
            })

    candidates.sort(key=lambda x: -x["score"])

    if not candidates:
        return f"SWEEP: No matches above {threshold:.0%} threshold (checked {len(rows)} items against last {num_commits} commits)."

    shown = candidates[:max_results]
    hidden = len(candidates) - len(shown)
    lines = [f"SWEEP: {len(candidates)} items may be done (showing top {len(shown)}, last {num_commits} commits):"]
    if truncated:
        lines.append("  (diff truncated at 100KB; some changes not checked)")
    for c in shown:
        pct = int(c["score"] * 100)
        desc_trunc = c["description"][:70]
        kw_list = ", ".join(c["matched"][:8])
        extra = f", +{len(c['matched'])-8} more" if len(c["matched"]) > 8 else ""
        match_info = f"{kw_list}{extra} ({len(c['matched'])}/{c['total']} keywords)"
        if c["matched_names"]:
            match_info += f" + name: {', '.join(c['matched_names'])}"
        lines.append(f"  [{pct:>2}%] {c['item_id']}  {desc_trunc}")
        lines.append(f"     Matched: {match_info}")

    if hidden:
        lines.append(f"\n  + {hidden} more above threshold (run with --max-results 50 to see all)")
    lines.append("")
    lines.append('Resolve these? Run: task_manager.py resolve <item_id> "note"')
    return "\n".join(lines)


def check_duplicate_before_insert(description, waiting_on=None, threshold=0.5):
    """Pre-insert check: warn if a similar active item already exists.

    Returns list of (similarity, existing_item_id, existing_description) or empty list.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT item_id, description, waiting_on
        FROM action_items
        WHERE status IN ('OPEN', 'WAITING', 'BLOCKED')
    """).fetchall()
    conn.close()

    new_words = _word_set(description)
    if waiting_on:
        new_words |= _word_set(waiting_on)
    if not new_words:
        return []

    matches = []
    for row in rows:
        existing_words = _word_set(row["description"])
        if row["waiting_on"]:
            existing_words |= _word_set(row["waiting_on"])
        if not existing_words:
            continue
        intersection = new_words & existing_words
        union = new_words | existing_words
        sim = len(intersection) / len(union) if union else 0
        if sim >= threshold:
            matches.append((sim, row["item_id"], row["description"]))

    matches.sort(key=lambda x: -x[0])
    return matches


# ---------------------------------------------------------------------------
# Auto-context tagging (bulk)
# ---------------------------------------------------------------------------
def auto_tag_contexts():
    """Auto-assign context tags based on description keywords."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, item_id, description, waiting_on
        FROM action_items
        WHERE status IN ('OPEN', 'WAITING', 'BLOCKED')
          AND (context_tags IS NULL OR context_tags = '')
    """).fetchall()

    # Generic starter vocabulary; extend with your own lanes, e.g.
    # "@vendor": ["vendorname", "invoice"] for a payments-vendor tag.
    tag_rules = {
        "@email": ["email", "send", "reply", "follow-up", "follow up", "outreach", "invite"],
        "@discord": ["discord", "announcement", "help-desk"],
        "@notion": ["notion", "database", "page"],
        "@payments": ["payment", "invoice"],
        "@call": ["meeting", "call", "sync", "prep"],
        "@website": ["website", "web page"],
        "@social": ["social", "post", "campaign"],
        "@slack": ["slack"],
    }

    tagged = 0
    for row in rows:
        desc = (row["description"] or "").lower()
        waiting = (row["waiting_on"] or "").lower()
        combined = desc + " " + waiting

        tags = []
        for tag, keywords in tag_rules.items():
            if any(kw in combined for kw in keywords):
                tags.append(tag)

        if tags:
            conn.execute(
                "UPDATE action_items SET context_tags = ?, updated_at = datetime('now') WHERE id = ?",
                (",".join(tags), row["id"])
            )
            tagged += 1

    conn.commit()
    conn.close()
    return tagged


# ---------------------------------------------------------------------------
# Triage (bulk review of stale items)
# ---------------------------------------------------------------------------
def show_triage():
    """Show items needing human decision: snooze, cancel, re-date, or keep.

    Matches:
    - OPEN + overdue > 7 days
    - OPEN + no due date + created > 14 days ago + no recent activity
    - WAITING + last_checked > 7 days ago
    """
    conn = get_conn()
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT item_id, status, priority, description, due_date, waiting_on,
               last_checked_at, updated_at, inserted_at, context_tags, urgency_score
        FROM action_items
        WHERE status IN ('OPEN', 'WAITING')
          AND (snoozed_until IS NULL OR snoozed_until <= ?)
    """, (today_str,)).fetchall()
    conn.close()

    overdue_items = []
    dateless_items = []
    stale_waiting_items = []

    for row in rows:
        r = dict(row)
        # Category 1: OPEN + overdue > 7 days
        if r["status"] == "OPEN" and r["due_date"]:
            try:
                due = datetime.strptime(r["due_date"][:10], "%Y-%m-%d")
                days_overdue = (now - due).days
                if days_overdue > 7:
                    r["_days_overdue"] = days_overdue
                    r["_category"] = "overdue"
                    overdue_items.append(r)
            except ValueError:
                pass

        # Category 2: OPEN + no due date + created > 14 days ago + no activity
        elif r["status"] == "OPEN" and not r["due_date"]:
            created = r["inserted_at"]
            if created:
                try:
                    created_dt = datetime.fromisoformat(created)
                    age = (now - created_dt).days
                    if age > 14:
                        last_activity = r["updated_at"] or r["inserted_at"]
                        last_dt = datetime.fromisoformat(last_activity)
                        inactive_days = (now - last_dt).days
                        if inactive_days > 7:
                            r["_age_days"] = age
                            r["_inactive_days"] = inactive_days
                            r["_category"] = "dateless"
                            dateless_items.append(r)
                except (ValueError, TypeError):
                    pass

        # Category 3: WAITING + last_checked > 7 days ago
        if r["status"] == "WAITING":
            check_date = r["last_checked_at"] or r["updated_at"] or r["inserted_at"]
            if check_date:
                try:
                    last = datetime.fromisoformat(check_date)
                    days_stale = (now - last).days
                    if days_stale > 7:
                        r["_days_stale"] = days_stale
                        r["_category"] = "stale_waiting"
                        stale_waiting_items.append(r)
                except (ValueError, TypeError):
                    pass

    all_items = []
    for item in overdue_items + dateless_items + stale_waiting_items:
        score = item.get("urgency_score") or 0
        all_items.append((score, item))
    all_items.sort(key=lambda x: -x[0])

    if not all_items:
        return "Triage: nothing needs review right now"

    lines = []
    for score, item in all_items:
        item_id = item.get("item_id", "?")
        priority = item.get("priority", "P2")
        created = (item.get("inserted_at") or "")[:10]
        due = item.get("due_date", "")
        tags = item.get("context_tags") or ""
        desc = (item.get("description") or "")[:90]

        header_parts = [f"[{item_id}]", priority]
        if created:
            header_parts.append(f"| Created {created}")
        if due:
            header_parts.append(f"| Due {due[:10]}")
            if item.get("_days_overdue"):
                header_parts.append(f"({item['_days_overdue']}d overdue)")
        elif item.get("_age_days"):
            header_parts.append(f"| No due date ({item['_age_days']}d old)")
        if item.get("_days_stale"):
            header_parts.append(f"| WAITING {item['_days_stale']}d stale")
        if tags:
            header_parts.append(f"| {tags}")

        lines.append("  " + " ".join(header_parts))
        lines.append(f"    {desc}")

        last_activity = item.get("updated_at") or item.get("inserted_at") or "unknown"
        lines.append(f"    Last activity: {last_activity[:10]}")
        lines.append("")

    n_overdue = len(overdue_items)
    n_dateless = len(dateless_items)
    n_stale = len(stale_waiting_items)
    total = len(all_items)

    summary = [f"Triage: {total} items need review ({n_overdue} overdue, {n_dateless} dateless, {n_stale} stale waiting)"]
    summary.extend(lines)
    summary.append('Run: task_manager.py snooze AI-xxx "2026-04-20" "reason"')
    summary.append('Run: task_manager.py resolve AI-xxx "cancelled: no longer relevant"')

    return "\n".join(summary)


# ---------------------------------------------------------------------------
# Velocity tracking
# ---------------------------------------------------------------------------
def show_velocity():
    """Show resolution velocity over 7, 14, and 30 day windows."""
    conn = get_conn()
    now = datetime.now()

    windows = {"7d": 7, "14d": 14, "30d": 30}
    counts = {}

    for label, days in windows.items():
        cutoff = (now - timedelta(days=days)).isoformat()
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM action_items
            WHERE status IN ('DONE', 'RESOLVED')
              AND (completed_at >= ? OR (completed_at IS NULL AND updated_at >= ?))
        """, (cutoff, cutoff)).fetchone()
        counts[label] = row["cnt"]

    # Average time to resolve (for items with both inserted_at and completed_at)
    avg_row = conn.execute("""
        SELECT AVG(julianday(completed_at) - julianday(inserted_at)) as avg_days
        FROM action_items
        WHERE status IN ('DONE', 'RESOLVED')
          AND completed_at IS NOT NULL AND inserted_at IS NOT NULL
    """).fetchone()
    avg_days = avg_row["avg_days"] if avg_row["avg_days"] else 0

    # Currently active count for context
    active_row = conn.execute("""
        SELECT COUNT(*) as cnt FROM action_items
        WHERE status IN ('OPEN', 'WAITING', 'BLOCKED')
    """).fetchone()
    active = active_row["cnt"]

    conn.close()

    weekly_rate = counts["7d"]
    lines = [
        f"Velocity: {weekly_rate} items/week (last 7d: {counts['7d']}, 14d: {counts['14d']}, 30d: {counts['30d']})",
        f"Avg time to resolve: {avg_days:.1f} days",
        f"Currently active: {active} items",
    ]

    if weekly_rate > 0 and active > 0:
        weeks_to_clear = active / weekly_rate
        lines.append(f"At current pace: ~{weeks_to_clear:.1f} weeks to clear backlog (ignoring new items)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Inbox: review pending proposals from auto-extraction sources
# ---------------------------------------------------------------------------
# Trust gate (validators.route_action_item) sends untrusted-source items to
# action_items_inbox instead of canonical action_items. These commands let
# the operator triage the inbox: list, view, accept, reject, merge, defer.

UNTRUSTED_SOURCE_PATTERNS = (
    "wrap-up", "session-", "session_", "granola", "audit-", "audit_",
    "thread-", "email-sync", "auto-extracted", "discord:",
    "follow_up_", "brief.classify", "extract_action_items",
)


def _is_untrusted_source(source: str | None) -> bool:
    if not source:
        return True
    s = str(source).strip().lower()
    if s in ("manual", "operator", "operator-verbal", "template_step",
             "inbox-promoted", "wrap-up-confirmed"):
        return False
    return any(s.startswith(p) or p in s for p in UNTRUSTED_SOURCE_PATTERNS)


def _person_name(conn, person_id):
    if not person_id:
        return None
    r = conn.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
    return r["name"] if r else f"pid={person_id}"


def inbox_list(status="pending", limit=50, source_filter=None):
    """Show pending inbox proposals, grouped by source."""
    conn = get_conn()
    sql = """
        SELECT inbox_id, source, suggested_priority, suggested_due_date,
               suggested_description, source_url, suggested_source_type,
               suggested_source_person_id, suggested_about_person_id,
               proposed_at, evidence_quote, classifier_confidence
        FROM action_items_inbox
        WHERE status = ?
    """
    params = [status]
    if source_filter:
        sql += " AND source LIKE ?"
        params.append(f"%{source_filter}%")
    sql += " ORDER BY source, proposed_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        conn.close()
        return f"No {status} proposals in inbox."

    out = [f"INBOX ({len(rows)} {status} proposals):"]
    cur_source = None
    for r in rows:
        if r["source"] != cur_source:
            cur_source = r["source"]
            out.append(f"\n  ── {cur_source} ──")
        src_name = _person_name(conn, r["suggested_source_person_id"])
        about_name = _person_name(conn, r["suggested_about_person_id"])
        meta = []
        if r["suggested_priority"]:
            meta.append(r["suggested_priority"])
        if r["suggested_due_date"]:
            meta.append(f"due {r['suggested_due_date']}")
        if src_name:
            meta.append(f"from {src_name}")
        if about_name and about_name != src_name:
            meta.append(f"about {about_name}")
        meta_str = " | ".join(meta) if meta else ""
        out.append(f"  {r['inbox_id']}  [{meta_str}]")
        out.append(f"    {(r['suggested_description'] or '')[:140]}")
        if r["source_url"]:
            out.append(f"    -> {r['source_url']}")
    conn.close()
    return "\n".join(out)


def inbox_view(inbox_id):
    """Show full detail for one inbox proposal."""
    conn = get_conn()
    r = conn.execute(
        "SELECT * FROM action_items_inbox WHERE inbox_id=?", (inbox_id,),
    ).fetchone()
    if not r:
        conn.close()
        return f"Not found: {inbox_id}"
    out = [
        f"INBOX ITEM {r['inbox_id']}  status={r['status']}",
        f"  Source:       {r['source']}",
        f"  Source URL:   {r['source_url'] or '(none)'}",
        f"  Source ref:   {r['source_ref'] or '(none)'}",
        f"  Source type:  {r['suggested_source_type'] or '(none)'}",
        f"  Source ID:    {r['suggested_source_id'] or '(none)'}",
        f"  Confidence:   {r['classifier_confidence']}",
        f"  Proposed at:  {r['proposed_at']}",
        "",
        f"  Suggested priority: {r['suggested_priority']}",
        f"  Suggested due:      {r['suggested_due_date']}",
        f"  Suggested waiting:  {r['suggested_waiting_on']}",
        f"  Suggested context:  {r['suggested_context_slug']}",
        "",
        "  DESCRIPTION:",
        f"    {r['suggested_description']}",
    ]
    if r["evidence_quote"]:
        out.append("")
        out.append("  EVIDENCE QUOTE:")
        out.append(f"    {r['evidence_quote']}")
    src_name = _person_name(conn, r["suggested_source_person_id"])
    about_name = _person_name(conn, r["suggested_about_person_id"])
    out.append("")
    out.append("  PEOPLE GRAPH:")
    if src_name:
        out.append(f"    source:  {src_name} (pid={r['suggested_source_person_id']})")
    if about_name:
        out.append(f"    about:   {about_name} (pid={r['suggested_about_person_id']})")
    extras = conn.execute(
        "SELECT person_id, relation FROM action_item_people "
        "WHERE target_kind='inbox' AND item_id_text=? ORDER BY relation",
        (inbox_id,),
    ).fetchall()
    for p in extras:
        nm = _person_name(conn, p["person_id"])
        out.append(f"    {p['relation']:8s}: {nm} (pid={p['person_id']})")
    conn.close()
    return "\n".join(out)


def inbox_accept(inbox_id, priority_override=None, due_override=None):
    """Promote an inbox proposal to canonical action_items."""
    conn = get_conn()
    r = conn.execute(
        "SELECT * FROM action_items_inbox WHERE inbox_id=? AND status='pending'",
        (inbox_id,),
    ).fetchone()
    if not r:
        conn.close()
        return f"Not found or not pending: {inbox_id}"

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    max_row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(item_id, 15) AS INTEGER)) AS mx "
        "FROM action_items WHERE item_id LIKE ?",
        (f"AI-{today}-%",),
    ).fetchone()
    next_seq = (max_row["mx"] or 0) + 1
    item_id = f"AI-{today}-{next_seq:03d}"
    while conn.execute("SELECT 1 FROM action_items WHERE item_id=?", (item_id,)).fetchone():
        next_seq += 1
        item_id = f"AI-{today}-{next_seq:03d}"

    # Carry stakeholder + source_quote forward so the canonical row is ready
    # for ranking the moment it lands (no need to wait for next urgency recompute).
    source_quote = r["evidence_quote"] if "evidence_quote" in r.keys() else None
    inbox_tier = r["stakeholder_tier"] if "stakeholder_tier" in r.keys() else None
    inbox_kind = r["partner_kind"] if "partner_kind" in r.keys() else None
    inbox_manager = r["is_manager_explicit"] if "is_manager_explicit" in r.keys() else 0

    # Carry provenance from inbox row through to canonical action_item.
    inbox_creator = r["suggested_creator_person_id"] if "suggested_creator_person_id" in r.keys() else None
    inbox_extracted_by = r["suggested_extracted_by"] if "suggested_extracted_by" in r.keys() else None

    # The action_items->entities mirror trigger writes entities, which the
    # blocking write gate rejects without an actor. Stamp the whole promote.
    from audit_actor import actor_scope
    with actor_scope(conn, "task_manager:inbox_accept", source_ref=item_id):
        cur = conn.execute(
            """
        INSERT INTO action_items
          (item_id, status, priority, description, due_date, waiting_on,
           waiting_on_person_id, context_slug, context, context_tags,
           email_thread_id, source, source_type, source_id, source_url,
           source_ref, source_person_id, about_person_id, project_id,
           discord_message_id, beeper_message_id, granola_meeting_id,
           slack_message_id, source_quote,
           stakeholder_tier, partner_kind, is_manager_explicit,
           creator_person_id, extracted_by,
           inserted_at, updated_at)
        VALUES (?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'inbox-promoted', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            item_id,
            priority_override or r["suggested_priority"] or "P2",
            r["suggested_description"],
            due_override or r["suggested_due_date"],
            r["suggested_waiting_on"],
            None,  # waiting_on_person_id resolved later by link-people
            r["suggested_context_slug"],
            f"Promoted from {r['inbox_id']} (source: {r['source']})",
            r["suggested_context_tags"],
            r["suggested_email_thread_id"],
            r["suggested_source_type"], r["suggested_source_id"],
            r["source_url"], r["source_ref"],
            r["suggested_source_person_id"], r["suggested_about_person_id"],
            r["suggested_project_id"], r["suggested_discord_message_id"],
            r["suggested_beeper_message_id"], r["suggested_granola_meeting_id"],
            r["suggested_slack_message_id"],
            source_quote,
            inbox_tier, inbox_kind, inbox_manager,
            inbox_creator, inbox_extracted_by,
        ),
    )
    new_pk = cur.lastrowid

    # Copy people graph from inbox -> canonical
    extras = conn.execute(
        "SELECT person_id, relation, notes FROM action_item_people "
        "WHERE target_kind='inbox' AND item_id_text=?",
        (r["inbox_id"],),
    ).fetchall()
    for p in extras:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO action_item_people "
                "(target_kind, target_id, item_id_text, person_id, relation, notes) "
                "VALUES ('canonical', ?, ?, ?, ?, ?)",
                (new_pk, item_id, p["person_id"], p["relation"], p["notes"]),
            )
        except sqlite3.OperationalError:
            pass

    conn.execute(
        "UPDATE action_items_inbox SET status='accepted', reviewed_at=CURRENT_TIMESTAMP, "
        "reviewed_by=?, promoted_to_item_id=? WHERE inbox_id=?",
        (OPERATOR_USERNAME, item_id, inbox_id),
    )
    conn.commit()
    conn.close()
    return f"Promoted {inbox_id} -> {item_id}"


def inbox_reject(inbox_id, reason):
    conn = get_conn()
    cur = conn.execute(
        "UPDATE action_items_inbox SET status='rejected', reviewed_at=CURRENT_TIMESTAMP, "
        "reviewed_by=?, rejection_reason=? WHERE inbox_id=? AND status='pending'",
        (OPERATOR_USERNAME, reason, inbox_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return f"Not found or not pending: {inbox_id}"
    return f"Rejected {inbox_id}: {reason}"


def inbox_merge(inbox_id, target_item_id):
    """Merge an inbox proposal into an existing canonical item (adds evidence)."""
    conn = get_conn()
    inbox_row = conn.execute(
        "SELECT * FROM action_items_inbox WHERE inbox_id=? AND status='pending'",
        (inbox_id,),
    ).fetchone()
    if not inbox_row:
        conn.close()
        return f"Not found or not pending: {inbox_id}"
    target = conn.execute(
        "SELECT id, item_id, context FROM action_items WHERE item_id=?",
        (target_item_id,),
    ).fetchone()
    if not target:
        conn.close()
        return f"Target item not found: {target_item_id}"

    note = (
        f"[merged from {inbox_id} {datetime.now(timezone.utc).isoformat(timespec='seconds')}Z] "
        f"source={inbox_row['source']} "
        f"evidence={(inbox_row['evidence_quote'] or '')[:200]}"
    )
    new_context = ((target["context"] or "") + "\n" + note).strip()
    conn.execute(
        "UPDATE action_items SET context=?, updated_at=CURRENT_TIMESTAMP WHERE item_id=?",
        (new_context, target_item_id),
    )
    # Carry over people links
    extras = conn.execute(
        "SELECT person_id, relation FROM action_item_people "
        "WHERE target_kind='inbox' AND item_id_text=?",
        (inbox_id,),
    ).fetchall()
    for p in extras:
        conn.execute(
            "INSERT OR IGNORE INTO action_item_people "
            "(target_kind, target_id, item_id_text, person_id, relation) "
            "VALUES ('canonical', ?, ?, ?, ?)",
            (target["id"], target_item_id, p["person_id"], p["relation"]),
        )
    conn.execute(
        "UPDATE action_items_inbox SET status='merged', reviewed_at=CURRENT_TIMESTAMP, "
        "reviewed_by=?, merged_into_item_id=? WHERE inbox_id=?",
        (OPERATOR_USERNAME, target_item_id, inbox_id),
    )
    conn.commit()
    conn.close()
    return f"Merged {inbox_id} into {target_item_id}"


def inbox_defer(inbox_id, reason="defer"):
    conn = get_conn()
    cur = conn.execute(
        "UPDATE action_items_inbox SET status='deferred', reviewed_at=CURRENT_TIMESTAMP, "
        "reviewed_by=?, rejection_reason=? WHERE inbox_id=? AND status='pending'",
        (OPERATOR_USERNAME, reason, inbox_id),
    )
    conn.commit()
    conn.close()
    return f"Deferred {inbox_id}" if cur.rowcount else f"Not found: {inbox_id}"


def inbox_bulk_reject(source_pattern, reason):
    """Reject all pending proposals from a source pattern."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE action_items_inbox SET status='rejected', reviewed_at=CURRENT_TIMESTAMP, "
        "reviewed_by=?, rejection_reason=? WHERE status='pending' AND source LIKE ?",
        (OPERATOR_USERNAME, reason, f"%{source_pattern}%"),
    )
    conn.commit()
    conn.close()
    return f"Bulk-rejected {cur.rowcount} proposals matching '{source_pattern}'"


# ---------------------------------------------------------------------------
# Manual entry: direct CLI add (always canonical, source='manual')
# ---------------------------------------------------------------------------
def add_item(description, priority="P2", due=None, waiting=None,
             context_slug=None, source_url=None, thread=None,
             about_person_id=None, source_person_id=None,
             source="manual", evidence_quote=None, source_session=None):
    """Add a single action item via CLI.

    Source contract (per validators.CANONICAL_SOURCES):
      - source='manual' (default): direct write to action_items. For operator CLI use only.
      - source in {operator, operator-verbal, template_step, inbox-promoted, wrap-up-confirmed}: direct write.
      - Any other source (wrap-up, granola, email, session-*, audit-*, etc.): routed
        through validators.propose_to_inbox so it lands in action_items_inbox for triage.

    This enforces the gate: model-written ad-hoc scripts that pass non-canonical
    source will go to the inbox, not pollute action_items. Run
    `task_manager.py inbox` or invoke skill triage-inbox to review.
    """
    try:
        from validators import (
            validate_action_item, build_source_url,
            route_action_item, propose_to_inbox,
        )
    except ImportError:
        return ("REJECTED: the validators module (input gate) is not installed "
                "in this build; `add` requires it")

    row = {"description": description.strip(), "priority": priority,
           "status": "OPEN", "waiting_on": waiting}
    ok, errors = validate_action_item(row)
    if not ok:
        return f"REJECTED: {errors}"

    src_ref = None
    if thread:
        src_ref = f"email:{thread}"
    else:
        # Provenance auto-stamp: manual/verbal adds used to carry no source ids,
        # violating the 'every action item needs
        # source_url + ids' rule. Stamp at least the creating session (env) or
        # the creation moment so every item stays traceable; explicit
        # --source-session below still overrides with the stronger form.
        _sid = os.environ.get("CLAUDE_SESSION_ID", "").replace("-", "")[:6]
        from datetime import datetime as _dt, timezone as _tz
        src_ref = (f"session:{_sid}" if _sid
                   else f"manual:{_dt.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")

    # Session provenance: a promise made in a chat session is a valid provenance
    # class ONLY with a verbatim quote that actually appears in that session's
    # conversation_history -- fabricated/paraphrased quotes REJECT. Routes through
    # the inbox gate like every non-canonical source.
    if source_session:
        from validators import verify_session_quote
        if not evidence_quote:
            return "REJECTED: --source-session requires --quote (verbatim from that session)"
        conn = get_conn()
        found = verify_session_quote(conn, source_session, evidence_quote)
        conn.close()
        if not found:
            return (f"REJECTED: quote not found verbatim in conversation_history for "
                    f"session {source_session} (paraphrases don't count -- copy the exact text)")
        source = f"session-{source_session}"
        src_ref = f"session:{source_session}"

    if not source_url and src_ref:
        source_url = build_source_url(src_ref)

    # Gate: non-canonical sources go to inbox instead of action_items.
    if route_action_item(source) == "inbox":
        conn = get_conn()
        inbox_id, skip_reason = propose_to_inbox(
            conn, source, description.strip(),
            priority=priority, due_date=due, waiting_on=waiting,
            context_slug=context_slug,
            email_thread_id=thread,
            evidence_quote=evidence_quote or description.strip()[:500],
            source_ref=src_ref, source_url=source_url,
            source_type="session" if source_session else ("email" if thread else None),
            source_id=source_session or thread,
            source_person_id=source_person_id,
            about_person_id=about_person_id,
        )
        conn.commit()
        conn.close()
        if skip_reason:
            return f"REJECTED: {skip_reason}"
        return f"Proposed {inbox_id} to action_items_inbox (source={source}, routed via gate). Run `task_manager.py inbox` or invoke triage-inbox skill to review."

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    conn = get_conn()
    max_row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(item_id, 15) AS INTEGER)) AS mx "
        "FROM action_items WHERE item_id LIKE ?",
        (f"AI-{today}-%",),
    ).fetchone()
    next_seq = (max_row["mx"] or 0) + 1
    item_id = f"AI-{today}-{next_seq:03d}"
    while conn.execute("SELECT 1 FROM action_items WHERE item_id=?", (item_id,)).fetchone():
        next_seq += 1
        item_id = f"AI-{today}-{next_seq:03d}"

    # The action_items->entities mirror trigger writes entities, which the
    # blocking write gate rejects without an actor. Stamp the whole insert.
    from audit_actor import actor_scope
    with actor_scope(conn, "task_manager:add", source_ref=item_id):
        conn.execute(
            """
            INSERT INTO action_items
              (item_id, status, priority, description, due_date, waiting_on,
               context_slug, source, source_type, source_id, source_url,
               source_ref, source_person_id, about_person_id, email_thread_id,
               inserted_at, updated_at)
            VALUES (?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (item_id, priority, description.strip(), due, waiting, context_slug,
             source, "email" if thread else None, thread, source_url, src_ref,
             source_person_id, about_person_id, thread),
        )
    conn.commit()
    conn.close()
    return f"Created {item_id} (source={source})"


# ---------------------------------------------------------------------------
# Bulk maintenance: archive-stale, demote-untrusted
# ---------------------------------------------------------------------------
def archive_stale(dry_run=False):
    """Archive stale items per sunset rules:
       - P3 + last_checked > 21d → ARCHIVED
       - P2 + no due + no waiting + last_checked > 30d → ARCHIVED
       - past due > 30d + no activity → ARCHIVED
    """
    conn = get_conn()
    now_iso = datetime.now(timezone.utc).isoformat()
    candidates = conn.execute("""
        SELECT item_id, priority, status, due_date, waiting_on, last_checked_at,
               substr(description,1,80) as desc
        FROM action_items
        WHERE status IN ('OPEN','WAITING','BLOCKED')
          AND (
            (priority='P3' AND (last_checked_at IS NULL OR last_checked_at < datetime('now','-21 days')))
            OR
            (priority='P2' AND (due_date IS NULL OR due_date='') AND (waiting_on IS NULL OR waiting_on='')
              AND (last_checked_at IS NULL OR last_checked_at < datetime('now','-30 days')))
            OR
            (due_date IS NOT NULL AND due_date < date('now','-30 days') AND (last_checked_at IS NULL OR last_checked_at < datetime('now','-14 days')))
          )
    """).fetchall()
    if dry_run:
        out = [f"DRY RUN: would archive {len(candidates)} items"]
        for r in candidates[:30]:
            out.append(f"  [{r['priority']}] {r['item_id']:28s} {r['desc'][:70]}")
        if len(candidates) > 30:
            out.append(f"  ... +{len(candidates)-30} more")
        conn.close()
        return "\n".join(out)
    n = 0
    for r in candidates:
        conn.execute(
            "UPDATE action_items SET status='REMOVED', "
            "resolution_note=COALESCE(resolution_note,'') || ?, "
            "updated_at=CURRENT_TIMESTAMP WHERE item_id=?",
            (f" [auto-archived {now_iso}: stale per sunset rule]", r["item_id"]),
        )
        n += 1
    conn.commit()
    conn.close()
    return f"Archived {n} stale items"


def demote_untrusted(dry_run=False):
    """Move existing OPEN/WAITING items with untrusted sources to inbox.

    Used once after the gate is installed to clean up the legacy backlog.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM action_items
        WHERE status IN ('OPEN','WAITING','BLOCKED')
    """).fetchall()
    candidates = [r for r in rows if _is_untrusted_source(r["source"])]
    if dry_run:
        out = [f"DRY RUN: would demote {len(candidates)} of {len(rows)} active items to inbox"]
        from collections import Counter
        sources = Counter(r["source"] or "(none)" for r in candidates)
        for src, n in sources.most_common(20):
            out.append(f"  {n:4d}  {src[:80]}")
        conn.close()
        return "\n".join(out)
    n = 0
    for r in candidates:
        # Insert a manual proposal in inbox referencing the original item
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        max_row = conn.execute(
            "SELECT MAX(CAST(SUBSTR(inbox_id, 18) AS INTEGER)) AS mx "
            "FROM action_items_inbox WHERE inbox_id LIKE ?",
            (f"AI-IN-{today}-%",),
        ).fetchone()
        next_seq = (max_row["mx"] or 0) + 1
        inbox_id = f"AI-IN-{today}-{next_seq:04d}"
        while conn.execute("SELECT 1 FROM action_items_inbox WHERE inbox_id=?", (inbox_id,)).fetchone():
            next_seq += 1
            inbox_id = f"AI-IN-{today}-{next_seq:04d}"
        conn.execute(
            """
            INSERT INTO action_items_inbox
              (inbox_id, source, source_evidence, evidence_quote,
               suggested_description, suggested_priority, suggested_due_date,
               suggested_waiting_on, suggested_context_slug,
               suggested_context, suggested_context_tags,
               suggested_email_thread_id, source_url, source_ref,
               suggested_source_type, suggested_source_id,
               status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                inbox_id, f"demoted:{r['source']}",
                f"action_items.item_id={r['item_id']}",
                (r["description"] or "")[:500],
                r["description"], r["priority"], r["due_date"],
                r["waiting_on"], r["context_slug"],
                r["context"], r["context_tags"], r["email_thread_id"],
                r["source_url"] if "source_url" in r.keys() else None,
                r["source_ref"] if "source_ref" in r.keys() else None,
                r["source_type"] if "source_type" in r.keys() else None,
                r["source_id"] if "source_id" in r.keys() else None,
            ),
        )
        # Mark the canonical row as DEMOTED so it disappears from focus/overdue
        conn.execute(
            "UPDATE action_items SET status='REMOVED', "
            "resolution_note=COALESCE(resolution_note,'') || ?, "
            "updated_at=CURRENT_TIMESTAMP WHERE item_id=?",
            (f" [demoted to {inbox_id} for review]", r["item_id"]),
        )
        n += 1
    conn.commit()
    conn.close()
    return f"Demoted {n} untrusted-source items to inbox for review"


# ---------------------------------------------------------------------------
# Daily plan -- locked 3-5 item commitment for today
# ---------------------------------------------------------------------------
def _plan_date():
    return datetime.now().strftime("%Y-%m-%d")


def get_today_plan(conn=None):
    """Return today's plan row as a dict, or None if not committed yet."""
    close = conn is None
    if conn is None:
        conn = get_conn()
    r = conn.execute(
        "SELECT * FROM daily_plans WHERE plan_date=?", (_plan_date(),)
    ).fetchone()
    if close:
        conn.close()
    if not r:
        return None
    d = dict(r)
    try:
        d["item_ids"] = json.loads(d.get("item_ids_json") or "[]")
    except json.JSONDecodeError:
        d["item_ids"] = []
    return d


def plan_show():
    """Print today's plan + completion status."""
    conn = get_conn()
    plan = get_today_plan(conn)
    if not plan:
        conn.close()
        return ("No plan committed for today.\n"
                "  Run: task_manager.py plan  (shows candidates)\n"
                "       task_manager.py plan commit ID1,ID2,ID3  (locks the day)")
    item_ids = plan["item_ids"]
    if not item_ids:
        conn.close()
        return f"Plan for {plan['plan_date']} is empty."
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT item_id, status, priority, description, stakeholder_tier, partner_kind, "
        f"       subtasks_json, source_quote "
        f"FROM action_items WHERE item_id IN ({placeholders})",
        item_ids,
    ).fetchall()
    by_id = {r["item_id"]: r for r in rows}
    done = sum(1 for r in rows if r["status"] in ("DONE", "REMOVED"))
    out = [f"PLAN {plan['plan_date']} ({done}/{len(item_ids)} done)  [{plan['status']}]"]
    for iid in item_ids:
        r = by_id.get(iid)
        if not r:
            out.append(f"  ?  {iid}  (not found in DB)")
            continue
        check = "[x]" if r["status"] in ("DONE", "REMOVED") else "[ ]"
        tier = r["stakeholder_tier"]
        kind = (r["partner_kind"] or "").upper()
        tier_tag = "T0 MANAGER-TAG" if tier == 0 else \
                   f"T{tier} {kind}" if tier is not None else "T?"
        desc = (r["description"] or "")[:90]
        out.append(f"  {check} [{tier_tag}] [{r['priority'] or 'P?'}] {iid} -- {desc}")
        if r["source_quote"]:
            q = r["source_quote"][:140].replace("\n", " ").strip()
            out.append(f"        QUOTE: \"{q}\"")
        steps = _load_subtasks(r["subtasks_json"])
        if steps:
            sdone = sum(1 for s in steps if s["done"])
            out.append(f"        STEPS ({sdone}/{len(steps)}):")
            for i, s in enumerate(steps, 1):
                chk = "[x]" if s["done"] else "[ ]"
                note = f"   (if: {s['note']})" if s["note"] else ""
                out.append(f"          {i}. {chk} {s['text'][:90]}{note}")
    conn.close()
    return "\n".join(out)


def plan_candidates(max_items=7):
    """Show the top urgency candidates so the operator can commit a subset."""
    update_all_urgency_scores()
    conn = get_conn()
    rows = conn.execute(
        "SELECT item_id, priority, urgency_score, description, "
        "stakeholder_tier, partner_kind, context_slug "
        "FROM action_items WHERE status IN ('OPEN','WAITING','BLOCKED') "
        "  AND (snoozed_until IS NULL OR snoozed_until <= date('now')) "
        "ORDER BY urgency_score DESC LIMIT ?",
        (max_items,),
    ).fetchall()
    conn.close()
    out = [f"CANDIDATES (top {len(rows)} by urgency)",
           "Commit with: task_manager.py plan commit ID1,ID2,ID3  (pick 3-5)"]
    for r in rows:
        tier = r["stakeholder_tier"]
        kind = (r["partner_kind"] or "").upper()
        tier_tag = "T0 MANAGER-TAG" if tier == 0 else \
                   f"T{tier} {kind}" if tier is not None else "T?"
        slug = r["context_slug"] or ""
        slug_str = f"  #{slug}" if slug else ""
        out.append(
            f"  {r['urgency_score']:6.2f}  [{tier_tag}] [{r['priority'] or 'P?'}] "
            f"{r['item_id']}{slug_str}"
        )
        out.append(f"          {(r['description'] or '')[:110]}")
    return "\n".join(out)


def plan_commit(item_id_csv: str, replace: bool = True):
    """Lock today's plan to the given list of item_ids (comma-separated)."""
    ids = [s.strip() for s in item_id_csv.split(",") if s.strip()]
    if not ids:
        return "No item_ids provided."
    if len(ids) > 7:
        return f"Refusing to commit {len(ids)} items. Keep it to 3-5 (max 7) so the day stays focused."
    conn = get_conn()
    # Validate ids exist
    placeholders = ",".join("?" * len(ids))
    found = conn.execute(
        f"SELECT item_id FROM action_items WHERE item_id IN ({placeholders})", ids
    ).fetchall()
    found_ids = {r["item_id"] for r in found}
    missing = [i for i in ids if i not in found_ids]
    if missing:
        conn.close()
        return f"Unknown item_id(s): {', '.join(missing)}"
    today = _plan_date()
    existing = get_today_plan(conn)
    payload = json.dumps(ids)
    if existing and not replace:
        conn.close()
        return f"Plan for {today} already exists. Use --replace to overwrite."
    if existing:
        conn.execute(
            "UPDATE daily_plans SET item_ids_json=?, total_count=?, status='active' "
            "WHERE plan_date=?",
            (payload, len(ids), today),
        )
    else:
        conn.execute(
            "INSERT INTO daily_plans(plan_date, item_ids_json, total_count) VALUES (?, ?, ?)",
            (today, payload, len(ids)),
        )
    conn.commit()
    conn.close()
    return f"Committed plan for {today}: {len(ids)} items.\n" + plan_show()


def plan_reset():
    conn = get_conn()
    conn.execute("DELETE FROM daily_plans WHERE plan_date=?", (_plan_date(),))
    conn.commit()
    conn.close()
    return f"Plan for {_plan_date()} cleared."


def plan_debrief(notes: str = ""):
    """Close out today's plan: count completed items, store notes."""
    conn = get_conn()
    plan = get_today_plan(conn)
    if not plan:
        conn.close()
        return "No plan to debrief."
    ids = plan["item_ids"]
    if not ids:
        conn.close()
        return "Plan is empty."
    placeholders = ",".join("?" * len(ids))
    done = conn.execute(
        f"SELECT COUNT(*) AS c FROM action_items "
        f"WHERE item_id IN ({placeholders}) AND status IN ('DONE','REMOVED')",
        ids,
    ).fetchone()
    done_count = done["c"]
    conn.execute(
        "UPDATE daily_plans SET status='debriefed', closed_at=CURRENT_TIMESTAMP, "
        "completed_count=?, notes=? WHERE plan_date=?",
        (done_count, notes or None, _plan_date()),
    )
    conn.commit()
    conn.close()
    return f"Debriefed plan: {done_count}/{len(ids)} done." + (f"\n  notes: {notes}" if notes else "")


# ---------------------------------------------------------------------------
# Stale review -- forced decisions on items >10d old without movement
# ---------------------------------------------------------------------------
def stale_review(min_age_days: int = 10, max_items: int = 30):
    """Items >N days old that haven't been touched. Each needs a decision:
    archive | escalate | snooze | resolve.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT item_id, status, priority, description,
               stakeholder_tier, partner_kind, context_slug,
               julianday('now') - julianday(inserted_at) AS age_d,
               julianday('now') - julianday(COALESCE(last_status_change_at, inserted_at)) AS idle_d,
               urgency_score, source_url, source_ref, due_date
        FROM action_items
        WHERE status IN ('OPEN','WAITING','BLOCKED')
          AND julianday('now') - julianday(COALESCE(last_status_change_at, inserted_at)) >= ?
          AND (snoozed_until IS NULL OR snoozed_until <= date('now'))
        ORDER BY idle_d DESC, urgency_score DESC
        LIMIT ?
    """, (min_age_days, max_items)).fetchall()
    conn.close()
    out = [f"STALE REVIEW ({len(rows)} items >{min_age_days}d idle)",
           "Decide per item: task_manager.py snooze ID DATE 'reason' | resolve ID 'note' | archive ID"]
    for r in rows:
        tier = r["stakeholder_tier"]
        kind = (r["partner_kind"] or "").upper()
        tier_tag = "T0 MANAGER-TAG" if tier == 0 else \
                   f"T{tier} {kind}" if tier is not None else "T?"
        out.append(
            f"  age={r['age_d']:4.1f}d idle={r['idle_d']:4.1f}d  [{tier_tag}] [{r['priority'] or 'P?'}]  {r['item_id']}"
        )
        out.append(f"    {(r['description'] or '')[:110]}")
        if r["source_url"] or r["source_ref"]:
            out.append(f"    SRC: {r['source_url'] or r['source_ref']}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Subtasks + per-step contingencies
# ---------------------------------------------------------------------------
# subtasks_json structure: list of {"text": "...", "done": bool, "note": "..."}
#   - text: what to do
#   - done: completion flag
#   - note: contingency / "what to do if X" inline annotation
# Numbering is 1-based when shown to humans, 0-based when stored internally.
def _load_subtasks(raw):
    if not raw:
        return []
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            return []
        return [
            {"text": str(i.get("text", "")), "done": bool(i.get("done")), "note": str(i.get("note") or "")}
            for i in items if isinstance(i, dict)
        ]
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


def _save_subtasks(conn, item_id, steps):
    with actor_scope(conn, "task_manager:subtasks", source_ref=item_id):
        conn.execute(
            "UPDATE action_items SET subtasks_json=?, updated_at=CURRENT_TIMESTAMP WHERE item_id=?",
            (json.dumps(steps), item_id),
        )


def subtask_list(item_id):
    conn = get_conn()
    r = conn.execute("SELECT subtasks_json, description FROM action_items WHERE item_id=?", (item_id,)).fetchone()
    if not r:
        conn.close()
        return f"No item: {item_id}"
    steps = _load_subtasks(r["subtasks_json"])
    out = [f"{item_id}  {(r['description'] or '')[:80]}"]
    if not steps:
        out.append("  (no subtasks)")
    else:
        for i, s in enumerate(steps, 1):
            chk = "[x]" if s["done"] else "[ ]"
            note = f"  (if: {s['note']})" if s["note"] else ""
            out.append(f"  {i}. {chk} {s['text']}{note}")
    conn.close()
    return "\n".join(out)


def subtask_add(item_id, text, contingency=""):
    """Add one or more subtasks. text can be 'a | b | c' to add multiple at once."""
    conn = get_conn()
    r = conn.execute("SELECT subtasks_json FROM action_items WHERE item_id=?", (item_id,)).fetchone()
    if not r:
        conn.close()
        return f"No item: {item_id}"
    steps = _load_subtasks(r["subtasks_json"])
    parts = [p.strip() for p in text.split("|")]
    added = 0
    for i, p in enumerate(parts):
        if not p:
            continue
        # contingency applies to the LAST step in a pipe-batch
        note = contingency if (i == len(parts) - 1) else ""
        steps.append({"text": p, "done": False, "note": note})
        added += 1
    _save_subtasks(conn, item_id, steps)
    conn.commit()
    conn.close()
    return f"Added {added} subtask(s) to {item_id}\n" + subtask_list(item_id)


def subtask_done(item_id, index_1based):
    conn = get_conn()
    r = conn.execute("SELECT subtasks_json FROM action_items WHERE item_id=?", (item_id,)).fetchone()
    if not r:
        conn.close()
        return f"No item: {item_id}"
    steps = _load_subtasks(r["subtasks_json"])
    i = int(index_1based) - 1
    if i < 0 or i >= len(steps):
        conn.close()
        return f"Step {index_1based} out of range (have {len(steps)})"
    steps[i]["done"] = True
    _save_subtasks(conn, item_id, steps)
    conn.commit()
    conn.close()
    return f"Marked step {index_1based} done on {item_id}\n" + subtask_list(item_id)


def subtask_undone(item_id, index_1based):
    conn = get_conn()
    r = conn.execute("SELECT subtasks_json FROM action_items WHERE item_id=?", (item_id,)).fetchone()
    if not r:
        conn.close()
        return f"No item: {item_id}"
    steps = _load_subtasks(r["subtasks_json"])
    i = int(index_1based) - 1
    if i < 0 or i >= len(steps):
        conn.close()
        return f"Step {index_1based} out of range"
    steps[i]["done"] = False
    _save_subtasks(conn, item_id, steps)
    conn.commit()
    conn.close()
    return f"Reopened step {index_1based} on {item_id}"


def subtask_remove(item_id, index_1based):
    conn = get_conn()
    r = conn.execute("SELECT subtasks_json FROM action_items WHERE item_id=?", (item_id,)).fetchone()
    if not r:
        conn.close()
        return f"No item: {item_id}"
    steps = _load_subtasks(r["subtasks_json"])
    i = int(index_1based) - 1
    if i < 0 or i >= len(steps):
        conn.close()
        return f"Step {index_1based} out of range"
    removed = steps.pop(i)
    _save_subtasks(conn, item_id, steps)
    conn.commit()
    conn.close()
    return f"Removed step {index_1based} ({removed['text'][:60]}) from {item_id}"


def subtask_note(item_id, index_1based, contingency):
    """Set or replace the contingency note on an existing step."""
    conn = get_conn()
    r = conn.execute("SELECT subtasks_json FROM action_items WHERE item_id=?", (item_id,)).fetchone()
    if not r:
        conn.close()
        return f"No item: {item_id}"
    steps = _load_subtasks(r["subtasks_json"])
    i = int(index_1based) - 1
    if i < 0 or i >= len(steps):
        conn.close()
        return f"Step {index_1based} out of range"
    steps[i]["note"] = contingency
    _save_subtasks(conn, item_id, steps)
    conn.commit()
    conn.close()
    return f"Set contingency on step {index_1based}: {contingency}"


def archive_suggest(min_idle_days: int = 7, max_urgency: float = 12.0):
    """List items that are strong archive candidates.

    Heuristic: low-tier (T4) + low urgency + idle >= N days. Also surfaces:
    - P3 items idle >14d (overtaken)
    - Items >30d old that never moved out of OPEN
    Tier 0/1/2/3 items are NEVER auto-suggested for archive -- those need human triage.
    """
    conn = get_conn()
    update_all_urgency_scores()  # make sure scores reflect current state
    rows = conn.execute("""
        SELECT item_id, status, priority, description,
               stakeholder_tier, partner_kind, context_slug,
               julianday('now') - julianday(inserted_at) AS age_d,
               julianday('now') - julianday(COALESCE(last_status_change_at, inserted_at)) AS idle_d,
               urgency_score
        FROM action_items
        WHERE status IN ('OPEN','WAITING','BLOCKED')
          AND (snoozed_until IS NULL OR snoozed_until <= date('now'))
          AND (
             (stakeholder_tier = 4 AND urgency_score < ?
                AND julianday('now') - julianday(COALESCE(last_status_change_at, inserted_at)) >= ?)
             OR (priority = 'P3' AND julianday('now') - julianday(COALESCE(last_status_change_at, inserted_at)) >= 14)
             OR (julianday('now') - julianday(inserted_at) >= 30 AND urgency_score < 15)
          )
        ORDER BY urgency_score ASC, idle_d DESC
    """, (max_urgency, min_idle_days)).fetchall()
    conn.close()
    out = [f"ARCHIVE CANDIDATES ({len(rows)} items)",
           f"Criteria: T4 + idle >= {min_idle_days}d + urgency < {max_urgency:.0f}",
           "         OR P3 idle >= 14d",
           "         OR age >= 30d + urgency < 15",
           "",
           "Approve in bulk: task_manager.py archive-bulk ID1,ID2,...",
           ""]
    for r in rows:
        tier = r["stakeholder_tier"]
        kind = (r["partner_kind"] or "").upper()
        tier_tag = "T0" if tier == 0 else f"T{tier} {kind}" if tier is not None else "T?"
        out.append(
            f"  urgency={r['urgency_score']:5.1f}  age={r['age_d']:4.1f}d idle={r['idle_d']:4.1f}d  "
            f"[{tier_tag}] [{r['priority'] or 'P?'}]  {r['item_id']}"
        )
        out.append(f"    {(r['description'] or '')[:110]}")
    return "\n".join(out)


def archive_bulk(item_id_csv: str, reason: str = "bulk archive (stale, low urgency)"):
    ids = [s.strip() for s in item_id_csv.split(",") if s.strip()]
    if not ids:
        return "No item_ids provided."
    conn = get_conn()
    placeholders = ",".join("?" * len(ids))
    found = conn.execute(
        f"SELECT item_id FROM action_items WHERE item_id IN ({placeholders}) "
        f"AND status IN ('OPEN','WAITING','BLOCKED')", ids,
    ).fetchall()
    found_ids = {r["item_id"] for r in found}
    missing = [i for i in ids if i not in found_ids]
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    archived = 0
    set_actor(conn, "task_manager:archive_bulk")
    for iid in found_ids:
        conn.execute(
            "UPDATE action_items SET status='REMOVED', "
            "resolution_note=COALESCE(resolution_note,'') || ?, "
            "updated_at=CURRENT_TIMESTAMP WHERE item_id=?",
            (f" [archived {now_iso}: {reason}]", iid),
        )
        archived += 1
    conn.commit()
    clear_actor(conn)
    conn.close()
    msg = f"Archived {archived} items."
    if missing:
        msg += f"\nNot found / not active: {', '.join(missing)}"
    return msg


def archive_item(item_id: str, reason: str = "manually archived during stale-review"):
    conn = get_conn()
    r = conn.execute("SELECT 1 FROM action_items WHERE item_id=?", (item_id,)).fetchone()
    if not r:
        conn.close()
        return f"No item found: {item_id}"
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with actor_scope(conn, "task_manager:archive", source_ref=item_id):
        conn.execute(
            "UPDATE action_items SET status='REMOVED', "
            "resolution_note=COALESCE(resolution_note,'') || ?, "
            "updated_at=CURRENT_TIMESTAMP WHERE item_id=?",
            (f" [archived {now_iso}: {reason}]", item_id),
        )
    conn.commit()
    conn.close()
    return f"Archived {item_id}: {reason}"


# ---------------------------------------------------------------------------
# Drafts (email_drafts auto-suggested by auto_draft.py)
# ---------------------------------------------------------------------------
def drafts_list():
    """List all email_drafts pending review (status=auto-suggested or approved)."""
    conn = get_conn()
    rows = list(conn.execute(
        "SELECT id, recipient_email, subject, status, action_item_id, thread_id, "
        "       substr(notes, 1, 100) AS notes_short, created_at "
        "FROM email_drafts WHERE status IN ('auto-suggested','approved') "
        "ORDER BY created_at DESC"
    ))
    conn.close()
    if not rows:
        return "(no drafts pending)"
    out = [f"{len(rows)} draft(s) pending:"]
    for r in rows:
        out.append(f"  #{r['id']:3d}  [{r['status']:14s}]  {r['recipient_email'][:30]:30s}  {(r['subject'] or '')[:50]}")
        if r["action_item_id"]:
            out.append(f"        -> action_item: {r['action_item_id']}")
        if r["thread_id"]:
            out.append(f"        -> thread: https://mail.google.com/mail/u/0/#inbox/{r['thread_id']}")
        out.append(f"        notes: {r['notes_short']}")
    return "\n".join(out)


def drafts_show(draft_id: int):
    """Show full draft contents + source FAQ + action_item link."""
    conn = get_conn()
    r = conn.execute(
        "SELECT * FROM email_drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    conn.close()
    if not r:
        return f"No draft id={draft_id}"
    lines = [
        f"Draft #{r['id']} [{r['status']}]",
        f"To:     {r['recipient_name']} <{r['recipient_email']}>",
        f"Subject: {r['subject']}",
        f"Thread: https://mail.google.com/mail/u/0/#inbox/{r['thread_id']}" if r["thread_id"] else "",
        f"Action item: {r['action_item_id']}" if r["action_item_id"] else "",
        f"Context: {r['context_slug'] or '-'}",
        f"Created by: {r['created_by']} at {r['created_at']}",
        f"Notes: {r['notes']}",
        "",
        "--- BODY ---",
        r["body_html"] or "(empty)",
    ]
    return "\n".join(l for l in lines if l != "")


def drafts_approve(draft_id: int):
    """Mark draft as 'approved': ready to send."""
    conn = get_conn()
    r = conn.execute("SELECT id, status FROM email_drafts WHERE id = ?", (draft_id,)).fetchone()
    if not r:
        conn.close()
        return f"No draft id={draft_id}"
    if r["status"] not in ("auto-suggested", "draft"):
        conn.close()
        return f"Draft #{draft_id} status={r['status']}, cannot approve."
    conn.execute(
        "UPDATE email_drafts SET status = 'approved', updated_at = datetime('now') WHERE id = ?",
        (draft_id,),
    )
    conn.commit()
    conn.close()
    return f"Draft #{draft_id} approved. Send it with your mail tool, or copy/paste from `task_manager.py drafts show {draft_id}`."


def drafts_reject(draft_id: int, reason: str = ""):
    conn = get_conn()
    r = conn.execute("SELECT id, status FROM email_drafts WHERE id = ?", (draft_id,)).fetchone()
    if not r:
        conn.close()
        return f"No draft id={draft_id}"
    stamp = datetime.now().strftime("%Y-%m-%d")
    new_notes_suffix = f"\n\n[REJECTED {stamp}] {reason}" if reason else f"\n\n[REJECTED {stamp}]"
    conn.execute(
        "UPDATE email_drafts SET status='rejected', notes = notes || ?, updated_at = datetime('now') WHERE id = ?",
        (new_notes_suffix, draft_id),
    )
    conn.commit()
    conn.close()
    return f"Draft #{draft_id} rejected. Reason: {reason or '(no reason given)'}"


def drafts_sent(draft_id: int):
    """Mark draft as sent. Propagates to linked action_item (status=DONE) and
    email_thread (status=replied) via the existing bidirectional sync.
    Call this after the email actually went out."""
    conn = get_conn()
    r = conn.execute("SELECT id, action_item_id, thread_id, status FROM email_drafts WHERE id = ?", (draft_id,)).fetchone()
    if not r:
        conn.close()
        return f"No draft id={draft_id}"
    conn.execute(
        "UPDATE email_drafts SET status='sent', updated_at=datetime('now') WHERE id = ?",
        (draft_id,),
    )
    # Propagate to action_item
    closed_items = []
    if r["action_item_id"]:
        conn.execute(
            "UPDATE action_items SET status='DONE', resolution_note='Reply sent via auto_draft #" + str(draft_id) + "', "
            "completed_at=datetime('now'), updated_at=datetime('now') "
            "WHERE item_id = ? AND status IN ('OPEN','WAITING','IN_PROGRESS')",
            (r["action_item_id"],),
        )
        closed_items.append(r["action_item_id"])
    # Propagate to email_thread
    if r["thread_id"]:
        conn.execute(
            "UPDATE email_threads SET status='replied', updated_at=datetime('now') "
            "WHERE thread_id = ? AND status NOT IN ('resolved','replied','no_action_needed')",
            (r["thread_id"],),
        )
    conn.commit()
    conn.close()
    msg = f"Draft #{draft_id} marked sent."
    if closed_items:
        msg += f" Closed action_item: {closed_items[0]}."
    return msg


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0].lower()

    if cmd == "next":
        # Next-Move Menu (shadow): read-only render; only write is the
        # next_moves_offered bus event.
        import next_moves
        why_n = None
        if len(args) >= 3 and args[1].lower() == "why":
            try:
                why_n = int(args[2])
            except ValueError:
                print("usage: task_manager.py next [why N]")
                return
        print(next_moves.render(why_n=why_n))
        return

    if cmd == "focus":
        include_personal = "--include-personal" in args
        ignore_plan = "--ignore-plan" in args
        positional = [a for a in args[1:] if not a.startswith("--")]
        max_items = int(positional[0]) if positional else 7
        # If a daily plan is committed for today, focus = the plan (locks attention)
        if not ignore_plan:
            plan = get_today_plan()
            if plan and plan.get("item_ids"):
                print(plan_show())
                print()
                print("To see full backlog: task_manager.py focus --ignore-plan")
                return
        print(get_focus_items(max_items=max_items, include_personal=include_personal))

    elif cmd == "plan":
        if len(args) == 1:
            # Default: show candidates if no plan yet, otherwise show today's plan
            if get_today_plan():
                print(plan_show())
            else:
                print(plan_candidates())
        elif args[1] == "show":
            print(plan_show())
        elif args[1] == "candidates":
            n = int(args[2]) if len(args) > 2 else 7
            print(plan_candidates(max_items=n))
        elif args[1] == "commit" and len(args) >= 3:
            print(plan_commit(",".join(args[2:])))
        elif args[1] == "reset":
            print(plan_reset())
        elif args[1] == "debrief":
            notes = " ".join(args[2:]) if len(args) > 2 else ""
            print(plan_debrief(notes))
        else:
            print("plan subcommands: show | candidates [N] | commit ID1,ID2,... | reset | debrief 'notes'")

    elif cmd == "stale-review":
        positional = [a for a in args[1:] if not a.startswith("--")]
        min_age = int(positional[0]) if positional else 10
        print(stale_review(min_age_days=min_age))

    elif cmd == "archive-suggest":
        positional = [a for a in args[1:] if not a.startswith("--")]
        min_idle = int(positional[0]) if positional else 7
        max_u = float(positional[1]) if len(positional) > 1 else 12.0
        print(archive_suggest(min_idle_days=min_idle, max_urgency=max_u))

    elif cmd == "archive-bulk" and len(args) >= 2:
        reason = "bulk archive (stale, low urgency)"
        # Allow --reason "..." override
        if "--reason" in args:
            i = args.index("--reason")
            if i + 1 < len(args):
                reason = args[i + 1]
        ids_csv = args[1]
        print(archive_bulk(ids_csv, reason=reason))

    elif cmd == "drafts" and len(args) >= 2:
        sub = args[1].lower()
        if sub == "list":
            print(drafts_list())
        elif sub == "show" and len(args) >= 3:
            print(drafts_show(int(args[2])))
        elif sub == "approve" and len(args) >= 3:
            print(drafts_approve(int(args[2])))
        elif sub == "reject" and len(args) >= 3:
            reason = " ".join(args[3:]) if len(args) > 3 else ""
            print(drafts_reject(int(args[2]), reason=reason))
        elif sub == "sent" and len(args) >= 3:
            print(drafts_sent(int(args[2])))
        else:
            print("drafts subcommands: list | show ID | approve ID | reject ID 'reason' | sent ID")
    elif cmd == "drafts":
        print(drafts_list())

    elif cmd == "archive" and len(args) >= 2:
        reason = " ".join(args[2:]) if len(args) > 2 else "manually archived"
        print(archive_item(args[1], reason))

    elif cmd == "subtask" and len(args) >= 2:
        sub = args[1]
        if sub == "list" and len(args) >= 3:
            print(subtask_list(args[2]))
        elif sub == "add" and len(args) >= 4:
            text = args[3]
            contingency = ""
            if "--if" in args:
                ix = args.index("--if")
                if ix + 1 < len(args):
                    contingency = args[ix + 1]
            print(subtask_add(args[2], text, contingency))
        elif sub == "done" and len(args) >= 4:
            print(subtask_done(args[2], args[3]))
        elif sub == "undone" and len(args) >= 4:
            print(subtask_undone(args[2], args[3]))
        elif sub == "remove" and len(args) >= 4:
            print(subtask_remove(args[2], args[3]))
        elif sub == "note" and len(args) >= 5:
            print(subtask_note(args[2], args[3], " ".join(args[4:])))
        else:
            print("subtask subcommands:")
            print("  list ITEM")
            print("  add ITEM 'step text [| step2 | step3]' [--if 'contingency for last step']")
            print("  done ITEM N        # mark step N complete (1-based)")
            print("  undone ITEM N      # reopen step N")
            print("  remove ITEM N      # delete step N")
            print("  note ITEM N 'if X then Y'   # set/replace contingency on step N")

    elif cmd == "urgency":
        n = update_all_urgency_scores()
        print(f"Updated urgency scores for {n} items")
        # Also show top 10
        conn = get_conn()
        top = conn.execute("""
            SELECT item_id, priority, urgency_score, description
            FROM action_items WHERE status IN ('OPEN', 'WAITING', 'BLOCKED')
            ORDER BY urgency_score DESC LIMIT 10
        """).fetchall()
        conn.close()
        for r in top:
            print(f"  [{r['priority'] or 'P2'}] {r['urgency_score']:6.2f} | {(r['description'] or '')[:80]}")

    elif cmd == "snooze" and len(args) >= 3:
        reason = args[3] if len(args) > 3 else ""
        print(snooze_item(args[1], args[2], reason))

    elif cmd == "unsnooze" and len(args) >= 2:
        print(unsnooze_item(args[1]))

    elif cmd == "depend" and len(args) >= 3:
        print(add_dependency(args[1], args[2]))

    elif cmd == "undepend" and len(args) >= 3:
        print(remove_dependency(args[1], args[2]))

    elif cmd == "recur" and len(args) >= 3:
        print(set_recurrence(args[1], " ".join(args[2:])))

    elif cmd == "resolve" and len(args) >= 3:
        print(resolve_item(args[1], " ".join(args[2:])))

    elif cmd == "context" and len(args) >= 3:
        print(set_context(args[1], args[2]))

    elif cmd == "batch" and len(args) >= 2:
        print(batch_by_context(args[1]))

    elif cmd == "stale":
        print(show_stale())

    elif cmd == "overdue":
        print(show_overdue())

    elif cmd == "estimate" and len(args) >= 3:
        print(set_estimate(args[1], args[2]))

    elif cmd == "surface":
        print(surface_for_hook(int(args[1]) if len(args) > 1 else 7))

    elif cmd == "autotag":
        n = auto_tag_contexts()
        print(f"Auto-tagged {n} items")

    elif cmd == "spawn":
        created = spawn_recurring()
        if created:
            print(f"Spawned {len(created)} recurring items: {', '.join(created)}")
        else:
            print("No recurring items to spawn")

    elif cmd == "unblock":
        unblocked = check_unblocked()
        if unblocked:
            print(f"Unblocked {len(unblocked)} items: {', '.join(unblocked)}")
        else:
            print("No items to unblock")

    elif cmd == "reply-check":
        print(reply_check())

    elif cmd == "dedup":
        threshold = float(args[1]) if len(args) > 1 else 0.55
        print(find_duplicates(threshold))

    elif cmd == "sweep":
        nc = 20
        th = 0.50
        mr = 15
        if "--commits" in args:
            idx = args.index("--commits")
            if idx + 1 < len(args):
                nc = int(args[idx + 1])
        if "--threshold" in args:
            idx = args.index("--threshold")
            if idx + 1 < len(args):
                th = float(args[idx + 1])
        if "--max-results" in args:
            idx = args.index("--max-results")
            if idx + 1 < len(args):
                mr = int(args[idx + 1])
        print(sweep_completed(num_commits=nc, threshold=th, max_results=mr))

    elif cmd == "checkin" and len(args) >= 2:
        note = " ".join(args[2:]) if len(args) > 2 else ""
        print(checkin_item(args[1], note))

    elif cmd == "triage":
        print(show_triage())

    elif cmd == "velocity":
        print(show_velocity())

    elif cmd == "link-people":
        matched, total = link_people()
        print(f"Linked {matched}/{total} items to people records")

    # ─── Inbox triage ─────────────────────────────────────────────────────
    elif cmd == "inbox":
        sub = args[1] if len(args) > 1 else "list"
        # Treat any flag-leading arg, status word, or no arg as a list query.
        list_statuses = {"list", "pending", "accepted", "rejected", "merged", "deferred"}
        is_list_call = (
            sub.startswith("--") or sub in list_statuses
        )
        if is_list_call:
            status = sub if sub in {"pending", "accepted", "rejected", "merged", "deferred"} else "pending"
            limit = 50
            src_filter = None
            if "--source" in args:
                idx = args.index("--source")
                if idx + 1 < len(args):
                    src_filter = args[idx + 1]
            if "--limit" in args:
                idx = args.index("--limit")
                if idx + 1 < len(args):
                    limit = int(args[idx + 1])
            print(inbox_list(status=status, limit=limit, source_filter=src_filter))
        elif sub.startswith("AI-IN-"):
            print(inbox_view(sub))
        elif sub == "view" and len(args) >= 3:
            print(inbox_view(args[2]))
        elif sub == "accept" and len(args) >= 3:
            prio = None
            due = None
            if "--priority" in args:
                idx = args.index("--priority")
                if idx + 1 < len(args):
                    prio = args[idx + 1].upper()
            if "--due" in args:
                idx = args.index("--due")
                if idx + 1 < len(args):
                    due = args[idx + 1]
            print(inbox_accept(args[2], priority_override=prio, due_override=due))
        elif sub == "reject" and len(args) >= 4:
            print(inbox_reject(args[2], " ".join(args[3:])))
        elif sub == "merge" and len(args) >= 4:
            print(inbox_merge(args[2], args[3]))
        elif sub == "defer" and len(args) >= 3:
            reason = " ".join(args[3:]) if len(args) > 3 else "defer"
            print(inbox_defer(args[2], reason))
        elif sub == "bulk-reject" and len(args) >= 4:
            print(inbox_bulk_reject(args[2], " ".join(args[3:])))
        else:
            print("Usage: task_manager.py inbox [list|view|accept|reject|merge|defer|bulk-reject] [args]")
            print("  inbox                              # list pending")
            print("  inbox AI-IN-...                    # view one")
            print("  inbox accept AI-IN-... [--priority P1] [--due 2026-04-30]")
            print("  inbox reject AI-IN-... <reason>")
            print("  inbox merge AI-IN-... AI-...       # merge proposal into existing canonical")
            print("  inbox defer AI-IN-... [reason]")
            print("  inbox bulk-reject <source-pattern> <reason>")
            print("  inbox list --source granola --limit 20")

    elif cmd == "add" and len(args) >= 2:
        # Parse: add "description" [--priority P1] [--due 2026-04-30] [--waiting "Name"]
        # [--context slug] [--thread <gmail_thread_id>] [--source-url URL]
        # [--about-pid N] [--source-pid N]
        desc = None
        kw = {"priority": "P2", "due": None, "waiting": None,
              "context_slug": None, "source_url": None, "thread": None,
              "about_person_id": None, "source_person_id": None,
              "source": "manual", "evidence_quote": None}
        i = 1
        while i < len(args):
            a = args[i]
            if a == "--priority" and i + 1 < len(args):
                kw["priority"] = args[i + 1].upper()
                i += 2
            elif a == "--due" and i + 1 < len(args):
                kw["due"] = args[i + 1]
                i += 2
            elif a == "--waiting" and i + 1 < len(args):
                kw["waiting"] = args[i + 1]
                i += 2
            elif a == "--context" and i + 1 < len(args):
                kw["context_slug"] = args[i + 1]
                i += 2
            elif a == "--source-url" and i + 1 < len(args):
                kw["source_url"] = args[i + 1]
                i += 2
            elif a == "--thread" and i + 1 < len(args):
                kw["thread"] = args[i + 1]
                i += 2
            elif a == "--about-pid" and i + 1 < len(args):
                kw["about_person_id"] = int(args[i + 1])
                i += 2
            elif a == "--source-pid" and i + 1 < len(args):
                kw["source_person_id"] = int(args[i + 1])
                i += 2
            elif a == "--source" and i + 1 < len(args):
                kw["source"] = args[i + 1]
                i += 2
            elif a == "--quote" and i + 1 < len(args):
                kw["evidence_quote"] = args[i + 1]
                i += 2
            elif a == "--source-session" and i + 1 < len(args):
                # Session-provenance class. Quote is verified VERBATIM against
                # conversation_history for that session.
                kw["source_session"] = args[i + 1]
                i += 2
            elif desc is None:
                desc = a
                i += 1
            else:
                i += 1
        if not desc:
            print("Usage: task_manager.py add \"description\" [--priority P1] [--due ...] [--waiting ...] [--thread <gmail_id>] [--about-pid N] [--source <tag>] [--quote \"verbatim\"]")
            print("       Default source=manual goes direct to action_items (operator CLI use).")
            print("       Non-canonical sources (wrap-up, granola, email, session-*, audit-*) route to inbox.")
            return
        print(add_item(desc, **kw))

    elif cmd == "morning":
        # Single-pane morning view: inbox count + focus + overdue + stale waiting
        conn_m = get_conn()
        ib = conn_m.execute(
            "SELECT COUNT(*) c, "
            "SUM(CASE WHEN suggested_priority='P0' THEN 1 ELSE 0 END) p0, "
            "SUM(CASE WHEN suggested_priority='P1' THEN 1 ELSE 0 END) p1 "
            "FROM action_items_inbox WHERE status='pending'"
        ).fetchone()
        active_cnt = conn_m.execute(
            "SELECT COUNT(*) c FROM action_items WHERE status IN ('OPEN','WAITING','BLOCKED')"
        ).fetchone()["c"]
        conn_m.close()
        out = []
        out.append("=" * 72)
        out.append("MORNING SYNC " + datetime.now().strftime("%Y-%m-%d %H:%M UTC"))
        out.append("=" * 72)
        out.append(f"Active canonical: {active_cnt}")
        out.append(f"Inbox pending:    {ib['c']} (P0={ib['p0'] or 0}, P1={ib['p1'] or 0})")
        out.append("")
        out.append("--- DAILY FOCUS ---")
        out.append(get_focus_items(max_items=7))
        out.append("")
        out.append("--- OVERDUE ---")
        out.append(show_overdue())
        out.append("")
        out.append("--- STALE WAITING ---")
        out.append(show_stale())
        out.append("")
        out.append("Suggested next steps:")
        out.append("  1. brief.py sync                          # pull new comms")
        out.append("  2. task_manager.py inbox                  # triage proposals from sync + backlog")
        out.append("  3. task_manager.py focus                  # work the canonical list")
        out.append("  4. task_manager.py archive-stale --dry-run  # see what would auto-archive")
        print("\n".join(out))

    elif cmd == "archive-stale":
        print(archive_stale(dry_run="--dry-run" in args))

    elif cmd == "demote-untrusted":
        print(demote_untrusted(dry_run="--dry-run" in args))

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
