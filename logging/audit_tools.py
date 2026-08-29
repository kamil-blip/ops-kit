"""Audit tools: give agents DB-join truth instead of grep-on-truncated-body.

Tools (use these, not grep):
    resolve_person(query)    -> email/name/discord to {id, name, aliases[], roles, last_contact}
    get_thread(thread_id)    -> full bodies + participants resolved + outbound by alias list
    cross_search(person_id)  -> emails + discord + beeper + session_logs
    fresh_context(item_id)   -> "what's the actual latest state of this action item?"
    operator_aliases()       -> canonical operator/org sender list (for is_outgoing sanity)

Why this exists: audit agents working from JSON dumps miss people-email joins
(e.g. the FICTIONAL Jane Doe = jane@example.org), truncated signatures, and
frozen context fields. These tools query the live DB with joins.

Usage:
    from audit_tools import resolve_person, get_thread, cross_search
    p = resolve_person("Jane Doe")  # also works with email or discord handle
    t = get_thread(p["latest_thread_id"])
    x = cross_search(p["id"], days=14)
"""
from __future__ import annotations
import paths
import _db  # unified connector (busy_timeout + FK ON)

import json
import os
import sqlite3
from typing import Any

DB_PATH = str(paths.DB_PATH)


def _load_operator_aliases() -> tuple[str, ...]:
    """Operator/org sender aliases: every address that counts as "us" when
    deciding whether a message is outbound. Config-driven, empty by default.

    Sources, in order:
      1. OPERATOR_EMAIL_ALIASES env var (comma-separated) - overrides config
      2. <repo-root>/config.toml: the union of [operator] emails (your own
         addresses) and [org] email_aliases (shared org addresses you answer)
      3. empty tuple (nothing is treated as operator-outbound until configured)

    Example config.toml entries (FICTIONAL addresses, replace with your own):
      # [operator]
      # emails = ["jane@example.org"]
      # [org]
      # email_aliases = ["team@example.org", "support@example.org"]
    """
    env = os.environ.get("OPERATOR_EMAIL_ALIASES", "").strip()
    if env:
        return tuple(a.strip().lower() for a in env.split(",") if a.strip())
    cfg = paths.ROOT / "config.toml"
    if cfg.is_file():
        try:
            import tomllib
            with open(cfg, "rb") as f:
                data = tomllib.load(f)
            raw = list((data.get("operator", {}) or {}).get("emails", []) or [])
            raw += list((data.get("org", {}) or {}).get("email_aliases", []) or [])
            seen: list[str] = []
            for a in raw:
                a = str(a).strip().lower()
                if a and a not in seen:
                    seen.append(a)
            return tuple(seen)
        except Exception:
            pass
    return ()


OPERATOR_ALIASES = _load_operator_aliases()


def operator_aliases() -> tuple[str, ...]:
    """Canonical operator/org sender emails. An email with sender_email in this
    list should always be treated as outgoing from the operator's org, regardless
    of the is_outgoing flag (which depends on the Gmail SENT label on the
    operator's own mailbox and misses shared/alias inboxes)."""
    return OPERATOR_ALIASES


def _connect() -> sqlite3.Connection:
    conn = _db.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _role_flags(row: sqlite3.Row) -> list[str]:
    """Generic role flags: any truthy boolean people.is_* column becomes a role
    tag (e.g. is_internal -> 'internal'). Schema-driven, so whatever role-flag
    columns the operator's people table carries are surfaced without hardcoding
    a domain vocabulary here."""
    return [k[3:] for k in row.keys() if k.startswith("is_") and row[k]]


def resolve_person(query: str) -> dict[str, Any] | None:
    """Resolve a person by email / name / discord_username to canonical DB row.

    Tries, in order:
      1. exact match on people.email
      2. exact match on person_emails.email (alias)
      3. exact match on people.discord_username
      4. FTS match on people_fts.name (top result)

    Returns dict with id, name, primary_email, all aliases, role flags,
    last_contact_date, and latest_thread_id (most recent email thread).
    Returns None if no match.
    """
    conn = _connect()
    try:
        q = query.strip()
        q_lower = q.lower()
        row = None

        # 1. people.email exact
        row = conn.execute("SELECT * FROM people WHERE LOWER(email)=? LIMIT 1", (q_lower,)).fetchone()

        # 2. person_emails alias
        if not row:
            pe = conn.execute("SELECT person_id FROM person_emails WHERE LOWER(email)=? LIMIT 1", (q_lower,)).fetchone()
            if pe:
                row = conn.execute("SELECT * FROM people WHERE id=?", (pe["person_id"],)).fetchone()

        # 3. discord_username
        if not row:
            row = conn.execute(
                "SELECT * FROM people WHERE LOWER(discord_username)=? LIMIT 1", (q_lower,)
            ).fetchone()

        # 4. FTS on name
        if not row:
            try:
                fts = conn.execute(
                    "SELECT rowid FROM people_fts WHERE people_fts MATCH ? ORDER BY rank LIMIT 1",
                    (f'"{q}"',),
                ).fetchone()
                if fts:
                    row = conn.execute("SELECT * FROM people WHERE id=?", (fts["rowid"],)).fetchone()
            except sqlite3.OperationalError:
                pass

        # 5. LIKE fallback on name
        if not row:
            row = conn.execute(
                "SELECT * FROM people WHERE name LIKE ? ORDER BY total_emails DESC LIMIT 1",
                (f"%{q}%",),
            ).fetchone()

        if not row:
            return None

        # Collect aliases
        aliases = [row["email"]] if row["email"] else []
        alias_rows = conn.execute(
            "SELECT email, is_primary FROM person_emails WHERE person_id=? ORDER BY is_primary DESC, "
            "(email LIKE '%@%.%' AND email NOT LIKE '% %' AND email NOT LIKE '%,%' AND email NOT LIKE '%<%' AND email NOT LIKE '%mailto%' AND email NOT LIKE '%@%@%') DESC, "
            "(lower(email) = lower(COALESCE((SELECT email FROM people WHERE id=person_emails.person_id),''))) DESC, "
            "(COALESCE(email_type,'') != 'team') DESC, id /* deterministic tiebreak */",
            (row["id"],),
        ).fetchall()
        for a in alias_rows:
            if a["email"] and a["email"] not in aliases:
                aliases.append(a["email"])

        # Latest email thread
        latest_thread = None
        if aliases:
            placeholders = ",".join("?" for _ in aliases)
            like_clauses = " OR ".join(["recipients_json LIKE ?"] * len(aliases))
            params = tuple(aliases) + tuple(f"%{a}%" for a in aliases)
            tr = conn.execute(
                f"SELECT thread_id, MAX(timestamp) ts FROM emails "
                f"WHERE sender_email IN ({placeholders}) OR {like_clauses} "
                f"GROUP BY thread_id ORDER BY ts DESC LIMIT 1",
                params,
            ).fetchone()
            if tr:
                latest_thread = tr["thread_id"]

        return {
            "id": row["id"],
            "name": row["name"],
            "primary_email": row["email"],
            "aliases": aliases,
            "discord_username": row["discord_username"],
            "headline": row["headline"],
            "roles": _role_flags(row),
            "last_contact_date": row["last_contact_date"],
            "total_emails": row["total_emails"],
            "latest_thread_id": latest_thread,
            "tags": row["tags"],
        }
    finally:
        conn.close()


def get_thread(thread_id: str, include_bodies: bool = True) -> dict[str, Any]:
    """Full thread with all messages, resolved participants, outbound by alias list.

    Returns:
        {
          "thread_id": str,
          "subject": str,           # from latest message
          "participants": [{email, person_id, name}],
          "messages": [{timestamp, sender_email, sender_person_id, is_operator_outbound, subject, body}]
        }

    `is_operator_outbound` is computed from sender_email in OPERATOR_ALIASES
    (not the `is_outgoing` flag, which is unreliable for aliases).
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT gmail_message_id, thread_id, timestamp, sender_email, sender_name, "
            "recipients_json, subject, body, is_outgoing, person_id "
            "FROM emails WHERE thread_id=? ORDER BY timestamp",
            (thread_id,),
        ).fetchall()
        if not rows:
            return {"thread_id": thread_id, "error": "thread not found", "messages": []}

        subject = rows[-1]["subject"]
        messages = []
        participant_emails: set[str] = set()
        for r in rows:
            sender = (r["sender_email"] or "").lower()
            participant_emails.add(sender)
            try:
                recips = json.loads(r["recipients_json"] or "{}")
                for key in ("to", "cc", "bcc"):
                    for addr in recips.get(key, []) or []:
                        if addr.get("email"):
                            participant_emails.add(addr["email"].lower())
            except Exception:
                pass

            is_operator = sender in OPERATOR_ALIASES
            msg = {
                "gmail_message_id": r["gmail_message_id"],
                "timestamp": r["timestamp"],
                "sender_email": r["sender_email"],
                "sender_person_id": r["person_id"],
                "is_operator_outbound": is_operator,
                "is_outgoing_raw": r["is_outgoing"],
                "subject": r["subject"],
            }
            if include_bodies:
                msg["body"] = r["body"]
            messages.append(msg)

        # Resolve participants
        resolved = []
        for email in sorted(participant_emails):
            p = resolve_person(email)
            resolved.append(
                {
                    "email": email,
                    "person_id": p["id"] if p else None,
                    "name": p["name"] if p else None,
                    "is_operator": email in OPERATOR_ALIASES,
                }
            )

        return {
            "thread_id": thread_id,
            "subject": subject,
            "message_count": len(messages),
            "participants": resolved,
            "messages": messages,
        }
    finally:
        conn.close()


def cross_search(person_query: int | str, days: int = 30) -> dict[str, Any]:
    """Pull all interactions with a person across emails, discord, beeper, session logs.

    Args:
        person_query: person_id (int) OR email/name (str, resolved via resolve_person)
        days: look-back window (default 30)

    Returns:
        {person, emails[], discord[], beeper[], session_logs[], counts}
    """
    conn = _connect()
    try:
        if isinstance(person_query, int):
            row = conn.execute("SELECT * FROM people WHERE id=?", (person_query,)).fetchone()
            person = dict(row) if row else None
            if not person:
                return {"error": f"person_id {person_query} not found"}
            aliases = [row["email"]] if row["email"] else []
            alias_rows = conn.execute(
                "SELECT email FROM person_emails WHERE person_id=?", (person_query,)
            ).fetchall()
            for a in alias_rows:
                if a["email"] and a["email"] not in aliases:
                    aliases.append(a["email"])
            discord_id = row["discord_user_id"]
            name = row["name"]
        else:
            p = resolve_person(str(person_query))
            if not p:
                return {"error": f"no match for {person_query!r}"}
            aliases = p["aliases"]
            discord_id = p["discord_username"]
            name = p["name"]
            person = p

        emails = []
        if aliases:
            placeholders = ",".join("?" for _ in aliases)
            like_clauses = " OR ".join(["recipients_json LIKE ?"] * len(aliases))
            params = tuple(aliases) + tuple(f"%{a}%" for a in aliases) + (days,)
            rows = conn.execute(
                f"SELECT timestamp, sender_email, subject, substr(body,1,600) body, thread_id, is_outgoing "
                f"FROM emails WHERE (sender_email IN ({placeholders}) OR {like_clauses}) "
                f"AND timestamp > datetime('now', '-' || ? || ' days') "
                f"ORDER BY timestamp DESC LIMIT 50",
                params,
            ).fetchall()
            emails = [dict(r) for r in rows]

        discord = []
        if discord_id:
            rows = conn.execute(
                "SELECT timestamp, channel_id, content FROM discord_messages "
                "WHERE author_id=? AND timestamp > datetime('now', '-' || ? || ' days') "
                "ORDER BY timestamp DESC LIMIT 50",
                (discord_id, days),
            ).fetchall()
            discord = [dict(r) for r in rows]

        # Beeper: search by name since phone numbers aren't joined
        beeper = []
        try:
            rows = conn.execute(
                "SELECT timestamp, sender_handle, content FROM beeper_messages "
                "WHERE (LOWER(sender_handle) LIKE ? OR LOWER(content) LIKE ?) "
                "AND timestamp > datetime('now', '-' || ? || ' days') "
                "ORDER BY timestamp DESC LIMIT 20",
                (f"%{name.lower()}%", f"%{name.lower()}%", days),
            ).fetchall()
            beeper = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            pass

        session_logs = []
        try:
            rows = conn.execute(
                "SELECT session_id, timestamp, kind, substr(topic,1,400) topic "
                "FROM session_logs WHERE topic LIKE ? "
                "AND timestamp > datetime('now', '-' || ? || ' days') "
                "ORDER BY timestamp DESC LIMIT 20",
                (f"%{name}%", days),
            ).fetchall()
            session_logs = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            pass

        return {
            "person": person,
            "emails": emails,
            "discord": discord,
            "beeper": beeper,
            "session_logs": session_logs,
            "counts": {
                "emails": len(emails),
                "discord": len(discord),
                "beeper": len(beeper),
                "session_logs": len(session_logs),
            },
        }
    finally:
        conn.close()


def fresh_context(item_id: str, days: int = 21) -> dict[str, Any]:
    """For an action item, synthesize what happened lately.

    Looks at related_people (if known), pulls latest inbound/outbound emails,
    discord, session_logs. Returns a structured timeline; the caller (the
    operator or an LLM) decides whether to write a new context string.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT item_id, status, description, context, waiting_on, source, updated_at "
            "FROM action_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if not row:
            return {"error": f"item {item_id} not found"}

        waiting_on = row["waiting_on"]
        candidate = None
        if waiting_on:
            candidate = resolve_person(waiting_on)

        # Try to pull related_people from action_item_people table if it exists
        related = []
        try:
            rows = conn.execute(
                "SELECT person_id FROM action_item_people WHERE item_id=?", (item_id,)
            ).fetchall()
            related = [r["person_id"] for r in rows]
        except sqlite3.OperationalError:
            pass

        timeline = {"candidate": candidate, "related_people_ids": related, "cross": {}}
        if candidate:
            timeline["cross"][candidate["name"]] = cross_search(candidate["id"], days=days)
        for pid in related:
            res = cross_search(pid, days=days)
            if isinstance(res, dict) and "person" in res and res["person"]:
                nm = res["person"].get("name") or str(pid)
                timeline["cross"][nm] = res

        return {
            "item_id": item_id,
            "status": row["status"],
            "description": row["description"],
            "current_context": row["context"],
            "updated_at": row["updated_at"],
            "timeline": timeline,
        }
    finally:
        conn.close()


def _cli() -> None:
    """python audit_tools.py resolve <query>
    python audit_tools.py thread <thread_id>
    python audit_tools.py cross <person_id_or_query> [days]
    python audit_tools.py fresh <item_id> [days]
    """
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 3:
        print(_cli.__doc__)
        return
    cmd = sys.argv[1]
    arg = sys.argv[2]
    if cmd == "resolve":
        print(json.dumps(resolve_person(arg), indent=2, default=str))
    elif cmd == "thread":
        t = get_thread(arg, include_bodies=True)
        # Trim bodies for CLI readability
        for m in t.get("messages", []):
            if "body" in m:
                m["body"] = (m["body"] or "")[:500]
        print(json.dumps(t, indent=2, default=str))
    elif cmd == "cross":
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        try:
            q: int | str = int(arg)
        except ValueError:
            q = arg
        print(json.dumps(cross_search(q, days=days), indent=2, default=str))
    elif cmd == "fresh":
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 21
        print(json.dumps(fresh_context(arg, days=days), indent=2, default=str))
    else:
        print(_cli.__doc__)


if __name__ == "__main__":
    _cli()
