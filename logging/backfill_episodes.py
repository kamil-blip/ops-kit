"""Backfill episodes table from emails + discord_messages + beeper_messages.

The episodes table is the bi-temporal interaction log (one row per message
event across channels); this script materializes it from the raw sync tables
and brings it current when no ongoing materializer is wired up.

Idempotent: uses content_hash to skip existing rows. Safe to re-run.

Usage:
    python backfill_episodes.py              # backfill since last episode ts
    python backfill_episodes.py --since 2026-01-01  # backfill since explicit date
    python backfill_episodes.py --check      # just report gaps, no writes
"""
from __future__ import annotations
import paths
import _db  # unified connector (busy_timeout + FK ON)

import argparse
import hashlib
import json
import sqlite3
import sys

DB_PATH = str(paths.DB_PATH)

# Source tables this script materializes into episodes.
SOURCES = ("emails", "discord_messages", "beeper_messages")


def _connect() -> sqlite3.Connection:
    conn = _db.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _latest_ts(conn: sqlite3.Connection, source_table: str) -> str | None:
    r = conn.execute(
        "SELECT MAX(ts) ts FROM episodes WHERE source_table=?", (source_table,)
    ).fetchone()
    return r["ts"] if r else None


def _default_since(conn: sqlite3.Connection) -> str:
    """Earliest of the per-source latest episode timestamps, so no source is
    left with a gap. Epoch start when any source has no episodes yet: the
    content-hash dedup makes a too-wide window safe, just slower."""
    placeholders = ",".join("?" for _ in SOURCES)
    rows = conn.execute(
        f"SELECT source_table, MAX(ts) AS ts FROM episodes "
        f"WHERE source_table IN ({placeholders}) GROUP BY source_table",
        SOURCES,
    ).fetchall()
    ts_values = [r["ts"] for r in rows]
    if len(rows) < len(SOURCES) or any(t is None for t in ts_values):
        return "1970-01-01"
    return min(ts_values)


def _existing_hashes(conn: sqlite3.Connection, source_table: str, since: str) -> set[str]:
    rows = conn.execute(
        "SELECT content_hash FROM episodes WHERE source_table=? AND ts >= ?",
        (source_table, since),
    ).fetchall()
    return {r["content_hash"] for r in rows if r["content_hash"]}


def backfill_emails(conn: sqlite3.Connection, since: str) -> int:
    existing = _existing_hashes(conn, "emails", since)
    rows = conn.execute(
        "SELECT id, thread_id, sender_email, sender_name, recipients_json, "
        "subject, timestamp, is_outgoing, person_id FROM emails WHERE timestamp > ?",
        (since,),
    ).fetchall()

    inserted = 0
    cur = conn.cursor()
    for r in rows:
        # content hash: id + timestamp + subject should be unique per email
        h = _hash(f"emails:{r['id']}:{r['timestamp']}:{r['subject'] or ''}")
        if h in existing:
            continue

        # Build participants_json from sender + recipients
        participants = []
        if r["sender_email"]:
            participants.append({
                "person_id": r["person_id"],
                "name": r["sender_name"] or r["sender_email"],
                "email": r["sender_email"],
                "role": "sender",
            })
        try:
            recips = json.loads(r["recipients_json"] or "{}")
            for key, role in (("to", "recipient"), ("cc", "cc"), ("bcc", "bcc")):
                for addr in recips.get(key, []) or []:
                    if addr.get("email"):
                        participants.append({
                            "person_id": None,
                            "name": addr.get("name") or addr.get("email"),
                            "email": addr["email"],
                            "role": role,
                        })
        except Exception:
            pass

        direction = "outbound" if r["is_outgoing"] else "inbound"
        cur.execute(
            """INSERT INTO episodes
            (kind, ts, participants_json, topic, summary, content_hash,
             source_table, source_id, direction, channel, recorded_at)
            VALUES (?, ?, ?, ?, '', ?, 'emails', ?, ?, 'gmail', CURRENT_TIMESTAMP)""",
            (
                "email",
                r["timestamp"],
                json.dumps(participants, ensure_ascii=False),
                r["subject"],
                h,
                r["id"],
                direction,
            ),
        )
        inserted += 1
    return inserted


def backfill_discord(conn: sqlite3.Connection, since: str) -> int:
    existing = _existing_hashes(conn, "discord_messages", since)
    # discord_messages schema: id, channel_id, channel_name?, author_id, author_name?, content, timestamp
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(discord_messages)").fetchall()}
    name_col = "author_name" if "author_name" in cols else ("author_username" if "author_username" in cols else None)
    chan_col = "channel_name" if "channel_name" in cols else None
    select_cols = "id, channel_id, author_id, content, timestamp"
    if name_col:
        select_cols += f", {name_col}"
    if chan_col:
        select_cols += f", {chan_col}"

    rows = conn.execute(
        f"SELECT {select_cols} FROM discord_messages WHERE timestamp > ?",
        (since,),
    ).fetchall()

    inserted = 0
    cur = conn.cursor()
    for r in rows:
        rd = dict(r)
        h = _hash(f"discord:{rd['id']}:{rd['timestamp']}")
        if h in existing:
            continue

        author_name = rd.get(name_col) if name_col else rd.get("author_id") or ""
        channel_name = rd.get(chan_col) if chan_col else rd.get("channel_id") or ""
        participants = [{
            "person_id": None,
            "name": author_name,
            "discord_id": rd["author_id"],
            "role": "sender",
        }]
        content = (rd["content"] or "")[:200]
        cur.execute(
            """INSERT INTO episodes
            (kind, ts, participants_json, topic, summary, content_hash,
             source_table, source_id, direction, channel, recorded_at)
            VALUES (?, ?, ?, ?, '', ?, 'discord_messages', ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                "discord_message",
                rd["timestamp"],
                json.dumps(participants, ensure_ascii=False),
                f"#{channel_name}: {content}",
                h,
                rd["id"],
                "inbound",
                channel_name or "discord",
            ),
        )
        inserted += 1
    return inserted


def backfill_beeper(conn: sqlite3.Connection, since: str) -> int:
    try:
        rows = conn.execute(
            "SELECT id, chat_id, sender_id, sender_name, network, text, timestamp, is_outgoing, person_id "
            "FROM beeper_messages WHERE timestamp > ?",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"  [skip beeper: {e}]")
        return 0

    existing = _existing_hashes(conn, "beeper_messages", since)
    inserted = 0
    cur = conn.cursor()
    for r in rows:
        h = _hash(f"beeper:{r['id']}:{r['timestamp']}")
        if h in existing:
            continue
        participants = [{
            "person_id": r["person_id"],
            "name": r["sender_name"] or r["sender_id"],
            "handle": r["sender_id"],
            "role": "sender",
        }]
        direction = "outbound" if r["is_outgoing"] else "inbound"
        content = (r["text"] or "")[:200]
        cur.execute(
            """INSERT INTO episodes
            (kind, ts, participants_json, topic, summary, content_hash,
             source_table, source_id, direction, channel, recorded_at)
            VALUES (?, ?, ?, ?, '', ?, 'beeper_messages', ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                "beeper_message",
                r["timestamp"],
                json.dumps(participants, ensure_ascii=False),
                f"{r['network']}: {content}",
                h,
                # beeper_messages.id is TEXT (numeric legacy ids + Matrix event strings);
                # store it verbatim so source_id resolves back to the message. An earlier
                # `hash(r["id"]) & 0xFFFFFFFF` fallback used Python's per-process-salted
                # hash, producing unreversible source_ids.
                r["id"],
                direction,
                r["network"] or "beeper",
            ),
        )
        inserted += 1
    return inserted


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since", help="ISO timestamp (default: last episode ts per source)")
    p.add_argument("--check", action="store_true", help="report only, no writes")
    args = p.parse_args()

    conn = _connect()
    try:
        print("Episode gaps (latest ts per source):")
        for src in SOURCES + ("observations",):
            latest_ep = _latest_ts(conn, src)
            try:
                src_col = "timestamp"
                r = conn.execute(f"SELECT MAX({src_col}) ts FROM {src}").fetchone()
                latest_src = r["ts"] if r else None
            except sqlite3.OperationalError:
                latest_src = None
            print(f"  {src:<20} episodes={latest_ep}  source={latest_src}")

        if args.check:
            return 0

        since = args.since or _default_since(conn)
        print(f"\nBackfilling since {since}...")
        n_emails = backfill_emails(conn, since)
        print(f"  emails: +{n_emails}")
        n_discord = backfill_discord(conn, since)
        print(f"  discord: +{n_discord}")
        n_beeper = backfill_beeper(conn, since)
        print(f"  beeper: +{n_beeper}")
        conn.commit()
        print(f"\nTotal inserted: {n_emails + n_discord + n_beeper}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
