"""Next-Move Menu (shadow phase).

Read-only aggregation over existing tables; the ONLY write is one
next_moves_offered bus_events row per render. NO hook integration, NO pick
loop in this phase (those come later, gated on shadow hit rates).

Candidate sources (v1):
  1. Charter batches: open action_items grouped by context_tags @domain
     (items with no charter tag fall back to the optional
     action_items.context_slug for a per-project bucket, else "misc").
  2. Inbox breach lanes: inbox_triage.daily_payload() lanes with breached>0.
  3. Triage feed: action_items_inbox pending.
  4. Continuity card: latest session_logs entry ("continue: ...").

Menu rules: slot 1 reserved for the highest-urgency actionable (non-WAITING)
T0/T1 item when one exists; WAITING items never in slot 1; max 2 candidates
per charter in the top 5; stale sync data tagged and demoted.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import paths  # noqa: F401 -- core/paths.py; on sys.path via the installer's .pth
except ImportError:
    # Direct-invocation fallback: walk up from this file to the repo root and
    # put core/ on sys.path (the installed venv normally does this via a .pth).
    sys.path.insert(0, str(next(
        p / "core" for p in Path(__file__).resolve().parents
        if (p / "core" / "paths.py").is_file()
    )))
    import paths  # noqa: F401
from _db import connect

# Flat ranking constants (shadow phase: constants in code, no stored weights)
BREACH_BONUS = 8.0
CONTINUITY_BONUS = 6.0
TRIAGE_BONUS = 4.0
STALE_SYNC_PENALTY = 10.0
STALE_SYNC_HOURS = 30.0

# Inbox lane -> session charter. Lane names come from your inbox_triage
# config; anything unmapped defaults to COMMS.
LANE_TO_CHARTER = {
    "vip": "COMMS", "partner": "COMMS", "payment": "COMMS",
    "shared_inbox": "COMMS", "participant": "COMMS",
}
# context_tags @domain -> session charter. Add your own charter rows as your
# domains emerge (e.g. "@events": "EVENT-OPS").
DOMAIN_TO_CHARTER = {
    "@comms": "COMMS", "@system": "SYSTEM", "@triage": "TRIAGE/BRIEF",
}


def _sync_age_hours(conn) -> float | None:
    row = conn.execute(
        "SELECT (julianday('now') - julianday(last_sync)) * 24 FROM sync_state "
        "WHERE source='gmail'").fetchone()
    return round(row[0], 1) if row and row[0] is not None else None


def _has_column(conn, table: str, col: str) -> bool:
    try:
        return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


def build_candidates(conn) -> tuple[list[dict], list[str]]:
    headers: list[str] = []
    cands: list[dict] = []

    sync_age = _sync_age_hours(conn)
    inbox_stale = sync_age is not None and sync_age > STALE_SYNC_HOURS
    if inbox_stale:
        headers.append(f"inbox data {sync_age}h old, run brief.py sync first "
                       "(inbox-derived cards demoted)")

    # 1. Charter batches from open action_items. context_slug is optional
    #    (feature-detected): without it, untagged items pool under "misc".
    has_ctx = _has_column(conn, "action_items", "context_slug")
    ctx_col = ", context_slug" if has_ctx else ""
    rows = conn.execute(f"""
        SELECT item_id AS id, description AS task, status,
               urgency_score AS urgency, stakeholder_tier, context_tags{ctx_col}
          FROM action_items WHERE status IN ('OPEN','WAITING','BLOCKED')
    """).fetchall()
    batches: dict[str, list] = {}
    for r in rows:
        tags = (r["context_tags"] or "")
        charter = next((c for d, c in DOMAIN_TO_CHARTER.items() if d in tags), None)
        ctx_slug = r["context_slug"] if has_ctx else None
        key = charter or (("context:" + ctx_slug) if ctx_slug else "misc")
        batches.setdefault(str(key), []).append(r)
    for key, members in batches.items():
        actionable = [m for m in members if m["status"] == "OPEN"]
        waiting = [m for m in members if m["status"] != "OPEN"]
        pool = actionable or waiting
        top = max(pool, key=lambda m: m["urgency"] or 0)
        score = float(top["urgency"] or 0)
        cands.append({
            "move_key": f"batch:{key}",
            "charter": key.split(":")[0] if ":" in key else key,
            "title": f"{key}: {len(actionable)} open"
                     + (f" (+{len(waiting)} waiting)" if waiting else ""),
            "top_item": f"{top['id']} {(top['task'] or '')[:70]}",
            "score": round(score, 1),
            "actionable": bool(actionable),
            "tier": top["stakeholder_tier"],
            "why": {"max_member_urgency": score, "members": len(members)},
        })

    # 2. Inbox breach lanes
    try:
        import inbox_triage
        payload = inbox_triage.daily_payload()
        for lane, info in (payload.get("lanes") or {}).items():
            breached = info.get("breached", 0) if isinstance(info, dict) else 0
            if not breached:
                continue
            score = BREACH_BONUS + min(breached, 10)
            if inbox_stale:
                score -= STALE_SYNC_PENALTY
            cands.append({
                "move_key": f"comms:breach:{lane}",
                "charter": LANE_TO_CHARTER.get(lane, "COMMS"),
                "title": f"inbox lane '{lane}': {breached} SLA-breached threads",
                "top_item": "",
                "score": round(score, 1),
                "actionable": True,
                "tier": 2,
                "why": {"breached": breached, "breach_bonus": BREACH_BONUS,
                        "stale_penalty": STALE_SYNC_PENALTY if inbox_stale else 0},
            })
    except Exception as exc:  # candidate source failure must not kill the menu
        headers.append(f"(breach source unavailable: {str(exc)[:60]})")

    # 3. Triage feed
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM action_items_inbox WHERE status='pending'").fetchone()[0]
        if n:
            cands.append({
                "move_key": "triage:inbox-proposals",
                "charter": "TRIAGE/BRIEF",
                "title": f"triage {n} pending action_items_inbox proposals",
                "top_item": "",
                "score": round(TRIAGE_BONUS + min(n, 10) * 0.5, 1),
                "actionable": True,
                "tier": 3,
                "why": {"pending": n, "triage_bonus": TRIAGE_BONUS},
            })
    except sqlite3.Error:
        pass

    # 4. Continuity card
    row = conn.execute(
        "SELECT date, title FROM session_logs ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        cands.append({
            "move_key": "continue:last-session",
            "charter": "ANY",
            "title": f"continue: {(row['title'] or '')[:70]} ({row['date']})",
            "top_item": "",
            "score": CONTINUITY_BONUS,
            "actionable": True,
            "tier": 3,
            "why": {"continuity_bonus": CONTINUITY_BONUS},
        })

    return cands, headers


def compose_menu(cands: list[dict]) -> list[dict]:
    ranked = sorted(cands, key=lambda c: c["score"], reverse=True)
    menu: list[dict] = []
    # Slot 1: highest-urgency ACTIONABLE tier 0/1 candidate, if any
    slot1 = next((c for c in ranked
                  if c["actionable"] and c.get("tier") is not None and c["tier"] <= 1), None)
    if slot1:
        menu.append(slot1)
    per_charter: dict[str, int] = {}
    for c in ranked:
        if c in menu:
            continue
        ch = c["charter"]
        if per_charter.get(ch, 0) >= 2:
            continue
        menu.append(c)
        per_charter[ch] = per_charter.get(ch, 0) + 1
        if len(menu) >= 5:
            break
    return menu


def _log_offer(conn, menu, headers) -> None:
    try:
        conn.execute(
            "INSERT INTO bus_events (session_id, event_type, summary, details_json, ts) "
            "VALUES (?, 'next_moves_offered', ?, ?, ?)",
            ("next_moves_cli",
             "next -> " + "; ".join(m["move_key"] for m in menu) + " [offered]",
             json.dumps({"actor": "task_manager:next", "outcome": "offered",
                         "menu": menu, "headers": headers}, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
    except sqlite3.Error:
        pass


def render(why_n: int | None = None) -> str:
    conn = connect(row_factory=sqlite3.Row)
    try:
        cands, headers = build_candidates(conn)
        menu = compose_menu(cands)
        if why_n is not None:
            if 1 <= why_n <= len(menu):
                m = menu[why_n - 1]
                lines = [f"why #{why_n}: {m['title']}",
                         f"  move_key: {m['move_key']}  score: {m['score']}"]
                for k, v in m["why"].items():
                    lines.append(f"  {k}: {v}")
                return "\n".join(lines)
            return f"no slot {why_n} in the current menu ({len(menu)} slots)"
        _log_offer(conn, menu, headers)
        lines = ["NEXT MOVES (shadow; reply is informational only, no pick loop yet)"]
        lines += [f"  ! {h}" for h in headers]
        for i, m in enumerate(menu, 1):
            extra = f"  [{m['top_item']}]" if m["top_item"] else ""
            lines.append(f"  {i}. ({m['score']:>5}) {m['title']}{extra}")
        lines.append("  (say what you want to work on; `next why N` explains a slot)")
        return "\n".join(lines)
    finally:
        conn.close()
