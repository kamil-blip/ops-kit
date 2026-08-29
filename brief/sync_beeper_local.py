"""
Sync Beeper Desktop's local SQLite directly into ops.db.

No auth required: reads %APPDATA%\\BeeperTexts\\index.db in mode=ro.
Reading the local DB replaces the localhost HTTP-API + keyring-token
approach, which breaks whenever the token expires.

Sources:
  - threads          ->  beeper_chats
  - mx_room_messages ->  beeper_messages
  - participants     ->  sender_name lookup (no separate table; resolved at insert)

CLI:
  python sync_beeper_local.py            # full sync (excludes WhatsApp by default)
  python sync_beeper_local.py --dry-run  # report counts, don't write
  python sync_beeper_local.py --since 2026-04-20  # incremental
  python sync_beeper_local.py --network whatsapp  # only this network
  python sync_beeper_local.py --exclude ''  # include WhatsApp (override default)
  python sync_beeper_local.py --exclude "WhatsApp,Instagram"  # multi-network skip

Notes:
  - PK is mx_room_messages.eventID (stable Matrix event ID).
  - If you ever synced via Beeper's HTTP API instead, those rows use short
    numeric IDs (msg.id, e.g. "9340") and will coexist with the long event
    IDs. Run a one-off dedup pass to merge by
    (chat_id, timestamp, sender_id, text) if that applies to you.
"""
import paths
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
import _db


OPS_DB = str(paths.DB_PATH)
BEEPER_DB = os.path.expandvars(r"%APPDATA%\BeeperTexts\index.db")


# Per-chat exclusion: group chats that are out of scope for this system but
# get noisy. Append a chat's thread ID when the operator tags it as
# out-of-scope. Excluded at both the chat upsert and the message ingest paths
# (so existing rows are also pruned by the chat-delete pass; see
# prune_excluded_chats below).
CHAT_EXCLUDE_IDS: set[str] = {
    # FICTIONAL example (a Matrix room id as Beeper stores it):
    # "!AbCdEfGhIjKlMnOpQrStUvWx:example_bridge.local-signal.localhost",  # some noisy group
}


NETWORK_MAP = [
    ("local-whatsapp", "WhatsApp"),
    ("local-signal", "Signal"),
    ("local-telegram", "Telegram"),
    ("local-linkedin", "LinkedIn"),
    ("local-facebook", "Facebook"),
    ("local-gvoice", "GoogleVoice"),
    ("local-instagram", "Instagram"),
    ("slackgo", "Slack"),
]


def network_from_accountid(aid: str) -> str:
    if not aid:
        return "Unknown"
    for prefix, name in NETWORK_MAP:
        if aid.startswith(prefix):
            return name
    if aid in ("hungryserv", "$other", "$space"):
        return "Beeper (Matrix)"
    return aid


def epoch_ms_to_iso(ms) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add columns we need that don't exist on the original schema. Idempotent."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(beeper_messages)")}
    if "reply_to_id" not in cols:
        conn.execute("ALTER TABLE beeper_messages ADD COLUMN reply_to_id TEXT")
    if "reply_thread_root_id" not in cols:
        conn.execute("ALTER TABLE beeper_messages ADD COLUMN reply_thread_root_id TEXT")
    if "event_id" not in cols:
        conn.execute("ALTER TABLE beeper_messages ADD COLUMN event_id TEXT")
    if "source" not in cols:
        conn.execute("ALTER TABLE beeper_messages ADD COLUMN source TEXT")

    chat_cols = {row[1] for row in conn.execute("PRAGMA table_info(beeper_chats)")}
    if "source" not in chat_cols:
        conn.execute("ALTER TABLE beeper_chats ADD COLUMN source TEXT")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_beeper_msgs_reply ON beeper_messages(reply_to_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_beeper_msgs_event ON beeper_messages(event_id)")
    conn.commit()


def load_participant_names(beeper: sqlite3.Connection) -> dict:
    """Map (room_id, participant_id) -> display name. Used to populate sender_name."""
    out = {}
    for row in beeper.execute("SELECT room_id, id, full_name, nickname FROM participants"):
        room_id, pid, full, nick = row
        name = full or nick or ""
        out[(room_id, pid)] = name
    return out


def sync_chats(beeper: sqlite3.Connection, conn: sqlite3.Connection,
               network_filter: str | None, exclude_networks: set[str],
               dry_run: bool) -> tuple[int, int]:
    """Upsert thread metadata into beeper_chats."""
    written = 0
    excluded = 0
    for row in beeper.execute("SELECT threadID, accountID, thread, timestamp FROM threads"):
        thread_id, account_id, thread_json, ts_ms = row
        if thread_id in CHAT_EXCLUDE_IDS:
            excluded += 1
            continue
        network = network_from_accountid(account_id)
        if network_filter and network.lower() != network_filter.lower():
            continue
        if network.lower() in exclude_networks:
            excluded += 1
            continue

        try:
            t = json.loads(thread_json) if thread_json else {}
        except json.JSONDecodeError:
            t = {}

        title = t.get("title") or ""
        chat_type = t.get("type") or ""
        last_activity = epoch_ms_to_iso(ts_ms)
        unread_count = t.get("unreadCount", 0) or 0
        is_archived = 1 if t.get("isArchived") else 0

        if dry_run:
            written += 1
            continue

        conn.execute(
            """
            INSERT INTO beeper_chats
              (id, title, chat_type, account_id, network, last_activity,
               unread_count, is_archived, participants_json, fetched_at, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'local-db')
            ON CONFLICT(id) DO UPDATE SET
              title = excluded.title,
              chat_type = excluded.chat_type,
              account_id = excluded.account_id,
              network = excluded.network,
              last_activity = excluded.last_activity,
              unread_count = excluded.unread_count,
              is_archived = excluded.is_archived,
              fetched_at = CURRENT_TIMESTAMP,
              source = 'local-db'
            """,
            (thread_id, title, chat_type, account_id, network, last_activity,
             unread_count, is_archived, "{}"),
        )
        written += 1
    if not dry_run:
        conn.commit()
    return written, excluded


def sync_messages(beeper: sqlite3.Connection, conn: sqlite3.Connection,
                  participant_names: dict, since_iso: str | None,
                  network_filter: str | None, exclude_networks: set[str],
                  dry_run: bool, per_network: dict | None = None) -> tuple[int, int]:
    """Upsert messages from mx_room_messages into beeper_messages.
    per_network: optional dict collecting inserted counts per network for the
    attempt/success ledger."""
    inserted = 0
    skipped = 0

    # Skip 'HIDDEN' rows: Beeper-internal bookkeeping events (isHidden=1) that
    # store the stub literal "m.room.message" in their text field. ~24% of
    # mx_room_messages are these on a typical install.
    # 'MEMBERSHIP' rows: room join/leave noise; not a chat message.
    sql = """
        SELECT m.eventID, m.roomID, m.senderContactID, m.timestamp, m.type,
               m.isSentByMe, m.inReplyToID, m.replyThreadRootID, m.message,
               t.accountID
        FROM mx_room_messages m
        LEFT JOIN threads t ON t.threadID = m.roomID
        WHERE m.isDeleted = 0
          AND m.type NOT IN ('HIDDEN', 'MEMBERSHIP')
    """
    params = []
    if since_iso:
        try:
            dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
            since_ms = int(dt.timestamp() * 1000)
            sql += " AND m.timestamp >= ?"
            params.append(since_ms)
        except ValueError:
            print(f"WARN: invalid --since {since_iso}, ignoring", file=sys.stderr)

    sql += " ORDER BY m.timestamp"

    for row in beeper.execute(sql, params):
        (event_id, room_id, sender_contact_id, ts_ms, mtype,
         is_sent_by_me, reply_to_id, reply_thread_root_id, msg_json, account_id) = row

        if room_id in CHAT_EXCLUDE_IDS:
            skipped += 1
            continue
        network = network_from_accountid(account_id or "")
        if network_filter and network.lower() != network_filter.lower():
            continue
        if network.lower() in exclude_networks:
            skipped += 1
            continue
        if per_network is not None:
            per_network.setdefault(network, 0)

        try:
            msg = json.loads(msg_json) if msg_json else {}
        except json.JSONDecodeError:
            msg = {}

        text = msg.get("text") or ""
        sender_name = participant_names.get((room_id, sender_contact_id), "") or ""
        timestamp_iso = epoch_ms_to_iso(ts_ms)
        msg_type_lc = (mtype or "").lower() or "text"

        if dry_run:
            inserted += 1
            continue

        try:
            cur = conn.execute(
                """
                INSERT INTO beeper_messages
                  (id, chat_id, sender_id, sender_name, network, text, timestamp,
                   is_outgoing, message_type, fetched_at, reply_to_id,
                   reply_thread_root_id, event_id, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, 'local-db')
                ON CONFLICT(id) DO UPDATE SET
                  text = excluded.text,
                  reply_to_id = excluded.reply_to_id,
                  reply_thread_root_id = excluded.reply_thread_root_id,
                  fetched_at = CURRENT_TIMESTAMP,
                  source = 'local-db'
                """,
                (event_id, room_id, sender_contact_id, sender_name,
                 (network or "").lower(),
                 text, timestamp_iso, 1 if is_sent_by_me else 0, msg_type_lc,
                 reply_to_id, reply_thread_root_id, event_id),
            )
            if cur.rowcount:
                inserted += 1
                if per_network is not None:
                    per_network[network] = per_network.get(network, 0) + 1
            else:
                skipped += 1
        except sqlite3.Error as e:
            skipped += 1
            print(f"WARN: insert failed for {event_id}: {e}", file=sys.stderr)

    if not dry_run:
        conn.commit()
    return inserted, skipped


def prune_excluded_chats(conn: sqlite3.Connection, dry_run: bool) -> tuple[int, int]:
    """Delete rows for CHAT_EXCLUDE_IDS so they don't linger from prior syncs."""
    if not CHAT_EXCLUDE_IDS:
        return (0, 0)
    placeholders = ",".join("?" * len(CHAT_EXCLUDE_IDS))
    ids = list(CHAT_EXCLUDE_IDS)
    msg_count = conn.execute(
        f"SELECT COUNT(*) FROM beeper_messages WHERE chat_id IN ({placeholders})",
        ids,
    ).fetchone()[0]
    chat_count = conn.execute(
        f"SELECT COUNT(*) FROM beeper_chats WHERE id IN ({placeholders})",
        ids,
    ).fetchone()[0]
    if dry_run:
        return (chat_count, msg_count)
    conn.execute(
        f"DELETE FROM beeper_messages WHERE chat_id IN ({placeholders})", ids
    )
    conn.execute(
        f"DELETE FROM beeper_chats WHERE id IN ({placeholders})", ids
    )
    conn.commit()
    return (chat_count, msg_count)


def update_sync_state(conn: sqlite3.Connection, msg_count: int) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (source, last_sync, count)
        VALUES ('beeper_local', CURRENT_TIMESTAMP, ?)
        ON CONFLICT(source) DO UPDATE SET
          last_sync = CURRENT_TIMESTAMP,
          count = excluded.count
        """,
        (msg_count,),
    )
    conn.commit()


# All networks the ledger tracks; a network excluded from a run still gets no
# attempt row for it (deliberate: exclusion is config, not failure).
LEDGER_NETWORKS = ["WhatsApp", "Signal", "Slack", "Telegram", "LinkedIn",
                   "Facebook", "GoogleVoice", "Instagram", "Beeper (Matrix)"]


def stamp_network_ledger(conn: sqlite3.Connection, kind: str,
                         networks: list, counts: dict | None = None) -> None:
    """Per-network attempt/success ledger: sync_state rows
    'beeper:<net>:attempt' stamped at run start, 'beeper:<net>:success' at
    clean completion. Health reads attempt-vs-success divergence instead of
    message recency, so a QUIET channel is no longer indistinguishable from a
    FAILING one."""
    for net in networks:
        conn.execute(
            """INSERT INTO sync_state (source, last_sync, count) VALUES (?, CURRENT_TIMESTAMP, ?)
               ON CONFLICT(source) DO UPDATE SET last_sync=CURRENT_TIMESTAMP, count=excluded.count""",
            (f"beeper:{net.lower()}:{kind}", (counts or {}).get(net, 0)))
    conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report counts only")
    ap.add_argument("--since", help="ISO datetime; only sync messages >= this")
    ap.add_argument("--network", help="filter by network: WhatsApp|Signal|Slack|...")
    ap.add_argument(
        "--exclude",
        default="WhatsApp",
        help=(
            "comma-separated networks to skip (case-insensitive). "
            "Default: 'WhatsApp'. Pass --exclude '' to include everything."
        ),
    )
    args = ap.parse_args()
    exclude_networks = {
        n.strip().lower() for n in (args.exclude or "").split(",") if n.strip()
    }
    if args.network and args.network.lower() in exclude_networks:
        exclude_networks.discard(args.network.lower())

    if not os.path.exists(BEEPER_DB):
        print(f"ERROR: Beeper local DB not found at {BEEPER_DB}", file=sys.stderr)
        sys.exit(1)

    print(f"=== Beeper Local Sync ===")
    print(f"Source: {BEEPER_DB}")
    print(f"Target: {OPS_DB}")
    print(f"Started: {datetime.now().strftime('%H:%M:%S')}")
    if args.dry_run:
        print("MODE: dry-run (no writes)")

    beeper = sqlite3.connect(f"file:{BEEPER_DB}?mode=ro", uri=True)
    conn = _db.connect()
    conn.execute("PRAGMA journal_mode=WAL")

    if not args.dry_run:
        ensure_schema(conn)

    participant_names = load_participant_names(beeper)
    print(f"Loaded {len(participant_names)} participant name mappings")

    if exclude_networks:
        print(f"Excluding networks: {sorted(exclude_networks)}")
    if CHAT_EXCLUDE_IDS:
        print(f"Excluding {len(CHAT_EXCLUDE_IDS)} chat IDs (operator-excluded group chats)")

    pruned_chats, pruned_msgs = prune_excluded_chats(conn, args.dry_run)
    if pruned_chats or pruned_msgs:
        verb = "would prune" if args.dry_run else "pruned"
        print(f"Excluded-chat cleanup: {verb} {pruned_chats} chats, {pruned_msgs} messages")

    chats_n, chats_excluded = sync_chats(
        beeper, conn, args.network, exclude_networks, args.dry_run
    )
    print(
        f"Chats: {chats_n} {'would write' if args.dry_run else 'upserted'}"
        f"{f', {chats_excluded} excluded' if chats_excluded else ''}"
    )

    # Which networks does this run cover? (filter/exclusions applied)
    run_networks = [n for n in LEDGER_NETWORKS
                    if n.lower() not in exclude_networks
                    and (not args.network or n.lower() == args.network.lower())]
    if not args.dry_run:
        stamp_network_ledger(conn, "attempt", run_networks)

    per_network: dict = {}
    inserted, skipped = sync_messages(
        beeper, conn, participant_names, args.since, args.network,
        exclude_networks, args.dry_run, per_network=per_network,
    )
    print(f"Messages: {inserted} {'would write' if args.dry_run else 'inserted/updated'}, {skipped} skipped")

    if not args.dry_run:
        update_sync_state(conn, inserted)
        stamp_network_ledger(conn, "success", run_networks, per_network)
        print(f"\n=== Stats ===")
        for net in LEDGER_NETWORKS:
            count = conn.execute(
                "SELECT COUNT(*) FROM beeper_messages WHERE network = ?", (net,)
            ).fetchone()[0]
            if count:
                print(f"  {net}: {count:,}")
        total_chats = conn.execute("SELECT COUNT(*) FROM beeper_chats").fetchone()[0]
        total_msgs = conn.execute("SELECT COUNT(*) FROM beeper_messages").fetchone()[0]
        print(f"  Total: {total_chats} chats, {total_msgs:,} messages")

    conn.close()
    beeper.close()
    print(f"\nDone: {datetime.now().strftime('%H:%M:%S')}")


if __name__ == "__main__":
    main()
