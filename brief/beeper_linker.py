"""
Shared matcher for beeper_messages.person_id <-> people.id.

Used by:
  - sync_beeper_local.py (the canonical Beeper ingest)
  - this module's `repair` subcommand: one-shot table-description refresh,
    network-casing normalize, and person_id backfill. (Absorbed a retired
    one-shot linkage-repair script.)

Tiered match strategy (per sender_id):
  Tier 0 - curated alias overrides (KNOWN_ALIASES)
  Tier 1 - exact full-name (accent-stripped, lowercased)
  Tier 2 - first+last token match (handles "Jane Doe" -> "Jane Anne Doe")
  Tier 2b - first+last loose intersection (handles formal-vs-display name diffs)
  Tier 3 - Beeper Matrix handle -> people.email handle (@jane:beeper.com -> jane@*)
  Tier 4 - unique single-first-name (only if exactly one person has it)

Plus a self-heal pass that propagates person_id across rows sharing a sender_id.
"""
from __future__ import annotations

import re
import sqlite3
import paths
import _db  # unified connector (busy_timeout + FK ON)
import unicodedata
from collections import defaultdict


def _norm(s: str | None) -> str:
    """Lowercase + NFKD strip accents. 'José García' -> 'jose garcia'."""
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).strip().lower()


# Alias overrides the heuristics can't resolve safely. The classic cases: one
# person with several people-table rows (a work address, a personal address,
# and a contacts-import artifact), or a chat network that uses a formal name
# ("Jane Anne Doe-Smith") while the canonical people row uses the display name.
# Keys are _norm()-formatted sender_names, values are the canonical people.id.
# To add: query.py people "<name>" -> pick the canonical id, append below.
KNOWN_ALIASES: dict[str, int] = {
    # FICTIONAL example:
    # "jane anne doe-smith": 42,
}


def self_heal_sender_links(conn: sqlite3.Connection) -> int:
    """Propagate person_id across rows that share a sender_id but where some
    rows are still NULL. Lets a single resolved sender_id heal all its messages.
    Returns rows updated."""
    rows = conn.execute(
        """
        SELECT sender_id, person_id, COUNT(*) AS n
        FROM beeper_messages
        WHERE person_id IS NOT NULL AND sender_id IS NOT NULL
        GROUP BY sender_id, person_id
        """
    ).fetchall()
    best: dict[str, tuple[int, int]] = {}  # sender_id -> (person_id, count)
    for sid, pid, n in rows:
        cur = best.get(sid)
        if cur is None or n > cur[1]:
            best[sid] = (pid, n)
    healed = 0
    for sid, (pid, _n) in best.items():
        healed += conn.execute(
            "UPDATE beeper_messages SET person_id = ? WHERE sender_id = ? AND person_id IS NULL",
            (pid, sid),
        ).rowcount
    conn.commit()
    return healed


def link_senders(conn: sqlite3.Connection, verbose: bool = False) -> dict[str, int]:
    """Run the full match cascade. Returns {reason: rows_updated}.
    Idempotent: only fills NULL person_id."""
    people = conn.execute(
        "SELECT id, name, email FROM people WHERE name IS NOT NULL AND name != ''"
    ).fetchall()

    by_full_name: dict[str, int] = {}
    by_first_two: dict[str, list[int]] = defaultdict(list)
    by_first_name: dict[str, list[int]] = defaultdict(list)
    by_last_name: dict[str, list[int]] = defaultdict(list)
    by_email_handle: dict[str, list[int]] = defaultdict(list)

    for pid, name, email in people:
        nlow = _norm(name)
        by_full_name.setdefault(nlow, pid)
        tokens = [t for t in re.split(r"\s+", nlow) if t]
        if len(tokens) >= 2:
            by_first_two[f"{tokens[0]} {tokens[-1]}"].append(pid)
            by_first_two[f"{tokens[0]} {tokens[1]}"].append(pid)
            by_last_name[tokens[-1]].append(pid)
        if tokens:
            by_first_name[tokens[0]].append(pid)
        if email:
            handle = _norm(email.split("@", 1)[0])
            if handle:
                by_email_handle[handle].append(pid)

    senders = conn.execute(
        """
        SELECT DISTINCT sender_id, sender_name, network
        FROM beeper_messages
        WHERE is_outgoing = 0
          AND person_id IS NULL
          AND sender_name IS NOT NULL
          AND sender_name != ''
        """
    ).fetchall()

    matches: list[tuple[int, str, str]] = []
    for sid, sname, network in senders:
        nlow = _norm(sname)
        if not nlow or len(nlow) < 2:
            continue

        if nlow in KNOWN_ALIASES:
            matches.append((KNOWN_ALIASES[nlow], sid, "alias"))
            continue

        pid = by_full_name.get(nlow)
        if pid:
            matches.append((pid, sid, "exact"))
            continue

        tokens = [t for t in re.split(r"\s+", nlow) if t]
        if len(tokens) >= 2:
            cands = by_first_two.get(f"{tokens[0]} {tokens[-1]}", [])
            if len(set(cands)) == 1:
                matches.append((cands[0], sid, "first+last"))
                continue
            cands2 = by_first_two.get(f"{tokens[0]} {tokens[1]}", [])
            if len(set(cands2)) == 1:
                matches.append((cands2[0], sid, "first2"))
                continue
            both = set(by_last_name.get(tokens[-1], [])) & set(by_first_name.get(tokens[0], []))
            if len(both) == 1:
                matches.append((both.pop(), sid, "first+last-loose"))
                continue

        if network == "Beeper (Matrix)":
            m = re.match(r"^@([^:]+):beeper\.com$", sid or "")
            if m:
                cands = by_email_handle.get(_norm(m.group(1)), [])
                if len(set(cands)) == 1:
                    matches.append((cands[0], sid, "matrix-handle"))
                    continue

        if len(tokens) == 1:
            cands = by_first_name.get(tokens[0], [])
            if len(set(cands)) == 1:
                matches.append((cands[0], sid, "unique-first"))
                continue

    by_reason: dict[str, int] = defaultdict(int)
    for pid, sid, reason in matches:
        n = conn.execute(
            "UPDATE beeper_messages SET person_id = ? WHERE sender_id = ? AND person_id IS NULL",
            (pid, sid),
        ).rowcount
        by_reason[reason] += n
    conn.commit()

    if verbose:
        for reason, n in sorted(by_reason.items()):
            print(f"  {reason}: {n} rows")
    return dict(by_reason)


# ======================================================================
# One-shot repair surface -- absorbed from a retired linkage-repair script.
# Refreshes _table_descriptions, normalizes network casing, and backfills
# beeper_messages.person_id. Idempotent; safe to re-run.
# ======================================================================

BEEPER_MESSAGES_DESC = (
    "Messages from the local Beeper Desktop app on this device. "
    "Sources merged through Beeper: Slack (your workspace), Signal, WhatsApp, "
    "LinkedIn DMs, and Beeper's native Matrix rooms. "
    "Slack and Signal live ONLY here -- not in your email tables, not in Discord. "
    "Synced via brief/sync_beeper_local.py from Beeper Desktop's local SQLite "
    "(%APPDATA%\\BeeperTexts\\index.db, read-only). "
    "person_id FK joins to people.id (backfilled + maintained by beeper_linker.py: "
    "the `repair` subcommand and link_senders). "
    "FTS5 mirror: beeper_messages_fts."
)

BEEPER_CHATS_DESC = (
    "Chat rooms from the local Beeper Desktop app on this device. "
    "Each row is one Slack channel, Signal thread, WhatsApp group, LinkedIn DM, or Matrix room. "
    "Source field is 'local-db' (Beeper Desktop's local SQLite). "
    "Synced via brief/sync_beeper_local.py. Joins: beeper_messages.chat_id -> beeper_chats.id."
)


def update_table_descriptions(conn: sqlite3.Connection, dry_run: bool = False) -> None:
    print("[1/3] Updating _table_descriptions...")
    rows = [
        ("beeper_messages", BEEPER_MESSAGES_DESC,
         "Searching Slack/Signal/WhatsApp/LinkedIn message content or history with a person.",
         "id, chat_id, sender_id, sender_name, network, text, timestamp, is_outgoing, person_id"),
        ("beeper_chats", BEEPER_CHATS_DESC,
         "Listing chat rooms/threads per network, or resolving a chat_id to its title.",
         "id, title, chat_type, network, last_activity"),
    ]
    if dry_run:
        print(f"      would upsert {len(rows)} descriptions (dry-run)")
        return
    for tbl, desc, when, key_cols in rows:
        # Upsert (not bare UPDATE) so a fresh install gets the rows too.
        conn.execute(
            """INSERT INTO _table_descriptions
                 (table_name, tier, description, when_to_query, key_columns)
               VALUES (?, 'core', ?, ?, ?)
               ON CONFLICT(table_name) DO UPDATE SET
                 description = excluded.description,
                 when_to_query = excluded.when_to_query,
                 key_columns = excluded.key_columns""",
            (tbl, desc, when, key_cols),
        )
    conn.commit()
    print(f"      upserted {len(rows)} descriptions")


def normalize_network_casing(conn: sqlite3.Connection, dry_run: bool = False) -> None:
    print("[2/3] Normalizing network casing...")
    fixes = [("signal", "Signal"), ("slack", "Slack")]
    total = 0
    for old, new in fixes:
        if dry_run:
            n_msgs = conn.execute(
                "SELECT COUNT(*) FROM beeper_messages WHERE network = ?", (old,)
            ).fetchone()[0]
            n_chats = conn.execute(
                "SELECT COUNT(*) FROM beeper_chats WHERE network = ?", (old,)
            ).fetchone()[0]
        else:
            n_msgs = conn.execute(
                "UPDATE beeper_messages SET network = ? WHERE network = ?", (new, old)
            ).rowcount
            n_chats = conn.execute(
                "UPDATE beeper_chats SET network = ? WHERE network = ?", (new, old)
            ).rowcount
        total += n_msgs + n_chats
        print(f"      {old!r} -> {new!r}: {n_msgs} messages, {n_chats} chats")
    if not dry_run:
        conn.commit()
    print(f"      {'would normalize' if dry_run else 'normalized'} {total} rows total")


def backfill_person_id(conn: sqlite3.Connection, dry_run: bool = False) -> None:
    print("[3/3] Backfilling beeper_messages.person_id...")
    if dry_run:
        null_ct = conn.execute(
            "SELECT COUNT(*) FROM beeper_messages "
            "WHERE person_id IS NULL AND is_outgoing = 0 "
            "AND sender_name IS NOT NULL AND sender_name != ''"
        ).fetchone()[0]
        print(f"      would attempt to link {null_ct} unlinked inbound rows (dry-run)")
        return
    healed_pre = self_heal_sender_links(conn)
    by_reason = link_senders(conn, verbose=False)
    healed_post = self_heal_sender_links(conn)
    print(f"      heal-pre:  {healed_pre} rows")
    for reason, n in sorted(by_reason.items()):
        print(f"      {reason}: {n} rows")
    print(f"      heal-post: {healed_post} rows")
    print(f"      total: {sum(by_reason.values()) + healed_pre + healed_post} rows")


def _repair(dry_run: bool = False) -> None:
    """Open ops.db and run the full repair pass (descriptions, casing, backfill)."""
    conn = _db.connect(str(paths.DB_PATH), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        update_table_descriptions(conn, dry_run=dry_run)
        normalize_network_casing(conn, dry_run=dry_run)
        backfill_person_id(conn, dry_run=dry_run)
        print("\nDone (dry-run, no writes)." if dry_run else "\nDone.")
    finally:
        conn.close()


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Beeper sender<->people linkage tools"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_repair = sub.add_parser(
        "repair",
        help="One-shot beeper_* repair: table descriptions, network casing, person_id backfill",
    )
    p_repair.add_argument(
        "--dry-run", action="store_true",
        help="Report would-be changes and write nothing",
    )
    args = parser.parse_args(argv)
    if args.command == "repair":
        _repair(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
