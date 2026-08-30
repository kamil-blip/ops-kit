"""
Daily sync: Pull fresh data from all sources into ops.db.
Run at session start or as part of the daily briefing.

Sources:
1. Gmail (incremental via gmail-to-sqlite; optional -- needs [sync] gmail_db_path)
2. Discord guilds (incremental, only new messages since last sync; optional --
   needs [sync] discord_guild_ids + a bot token in the keyring)
3. Discord DMs (subprocess: sync_discord_dms.py; needs a user token)
4. Episodes materializer (subprocess: logging/backfill_episodes.py)
5. Notion (recently modified rows via an external Notion-query CLI; optional --
   needs [sync] notion_query_cmd + notion_databases, see sync_notion below)

Every source degrades gracefully when unconfigured: the sync prints a one-line
"not configured, skipping" note instead of failing.

Usage: python daily_sync.py [--full] [--gmail-only] [--discord-only] [--notion-only]
"""
import paths
import config

import json
import keyring
import os
import re
import sqlite3
import subprocess
import sys

import _db
import steward_bus as _BUS  # route people creates through the canonical writer bus
from audit_actor import actor_scope  # attribute derivative direct touches (non-NULL cdc actor)
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = str(paths.DB_PATH)
GMAIL_DB = str(paths.GMAIL_DB_PATH) if paths.GMAIL_DB_PATH else None
GMAIL_SQLITE_DIR = str(paths.GMAIL_DATA_DIR) if paths.GMAIL_DATA_DIR else None
PYTHON = str(paths.PYTHON)

DISCORD_API = "https://discord.com/api/v9/"
# Ignore messages older than this when backfilling a newly-discovered channel.
# Override in config.toml: [sync] discord_cutoff = "YYYY-MM-DD".
DISCORD_CUTOFF = str(config.get("sync.discord_cutoff") or "2024-01-01")

# OS keyring service holding API tokens (see config.example.toml [keys]).
KEYRING_SERVICE = config.get("keyring_service") or "ops-kit"

# Every address that counts as "ours": the operator's own inboxes plus shared
# org aliases the operator answers from. Filled from config.toml:
#   [operator] emails = [...]   and   [org] email_aliases = [...]
# FICTIONAL example: {"jane@example.org", "team@example.org"}
OPERATOR_EMAIL_ALIASES = (
    {str(a).lower() for a in (config.get("operator_emails") or [])}
    | {str(a).lower() for a in (config.get("org.email_aliases") or [])}
)
# The org's email domain ([org] domain, e.g. "example.org"): ANY sender at this
# domain counts as outbound (a colleague's reply means we answered the thread).
ORG_DOMAIN = str(config.get("org_domain") or "").lower().strip().lstrip("@")


def _correct_is_outgoing(sender_email: str, raw_flag, sender_name: str | None = None) -> int:
    """Override gmail-to-sqlite's is_outgoing flag.

    Semantics: is_outgoing=1 means "someone from our org sent this" -- ANY
    sender at the configured org domain, plus the operator's personal aliases.
    A colleague's reply counts as us having answered the thread. Until
    [org] domain / [operator] emails are configured, everything is inbound.

    EXCEPT Google-Group DMARC rewrites: when an external sender's domain
    enforces DMARC, the Group rewrites From: to the group address and sets the
    display name to "'Original Name' via <Group>". That mail is INBOUND even
    though sender_email is one of our aliases. In the original deployment
    thousands of rows were misflagged this way, burying real inbound interest.
    """
    if not sender_email:
        return int(bool(raw_flag))
    if "' via " in (sender_name or ""):
        return 0
    s = sender_email.lower()
    if ORG_DOMAIN and s.endswith("@" + ORG_DOMAIN):
        return 1
    return 1 if s in OPERATOR_EMAIL_ALIASES else 0


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def sync_gmail():
    """Run gmail-to-sqlite incremental sync, then merge into ops.db."""
    print("\n[Gmail] Running incremental sync...")
    if not GMAIL_DB or not GMAIL_SQLITE_DIR:
        print("  Gmail mirror not configured ([sync] gmail_db_path in config.toml "
              "or OPS_GMAIL_DB env var); skipping.")
        return
    start = time.time()

    # Run gmail-to-sqlite sync (external tool; needs the operator's own Gmail
    # OAuth setup -- see INSTALL.md)
    result = subprocess.run(
        [PYTHON, "-m", "gmail_to_sqlite.main", "sync",
         "--data-dir", GMAIL_SQLITE_DIR, "--workers", "8"],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONUTF8": "1"}
    )

    if result.returncode != 0:
        print(f"  Gmail sync error: {result.stderr[-200:]}")
        return

    # Merge new emails into ops.db
    gmail_conn = _db.connect(GMAIL_DB, row_factory=sqlite3.Row)
    ops = _db.connect(DB)

    # Get last synced timestamp
    last_sync = ops.execute(
        "SELECT last_sync FROM sync_state WHERE source = 'gmail'"
    ).fetchone()
    last_ts = last_sync[0] if last_sync else "1970-01-01"

    # Get new emails since last sync
    new_emails = gmail_conn.execute(
        "SELECT * FROM messages WHERE last_indexed > ?", (last_ts,)
    ).fetchall()

    # Advance the watermark to the max last_indexed we actually saw (SAME clock
    # as messages.last_indexed), not CURRENT_TIMESTAMP. gmail-to-sqlite stamps
    # last_indexed in LOCAL time; a UTC CURRENT_TIMESTAMP watermark can sit
    # hours behind it (your UTC offset), so already-ingested rows get
    # re-processed every run.
    max_last_indexed = max((r["last_indexed"] for r in new_emails if r["last_indexed"]),
                           default=last_ts)

    added = 0
    skipped_drafts = 0
    # Collect the thread_ids actually ingested THIS sync. An earlier version
    # refreshed threads by a wall-clock window (emails.timestamp >= now-2h), so
    # any email merged >2h after it was sent/received (the normal case when
    # sync runs manually) never refreshed its thread aggregate and
    # reply-detection went stale forever.
    touched: set = set()
    for row in new_emails:
        # Skip Gmail drafts. A draft is not sent comms. Ingesting it flags
        # is_outgoing=1 (the operator is the author), which counts as outbound
        # in the email_threads aggregates below and falsely flips the thread to
        # 'replied' -- so triage stops surfacing it and a reader concludes "we
        # already replied" when nothing was sent. When the draft is eventually
        # sent, Gmail assigns a fresh message_id with the SENT label, so it
        # still gets ingested as a real outbound.
        if "DRAFT" in (row["labels"] or ""):
            skipped_drafts += 1
            continue

        sender = json.loads(row["sender"]) if row["sender"] else {}
        sender_email = sender.get("email", "")
        sender_name = sender.get("name", "")

        # Find or create person
        person_id = None
        if sender_email:
            result = ops.execute("SELECT id FROM people WHERE email = ?", (sender_email,)).fetchone()
            if result:
                person_id = result[0]
                # Update last seen (derivative touch, no asserted fact -> attributed direct write)
                with actor_scope(ops, "daily_sync:gmail-touch"):
                    ops.execute("UPDATE people SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (person_id,))
            else:
                # ROUTE create-on-first-seen through the bus: get_or_create_person
                # dedups + stamps provenance (source emails/message_id) + a
                # non-NULL cdc actor. Falls back to a direct insert so a sync
                # never stalls.
                person_id = None
                try:
                    _res = _BUS.write(
                        ops, target_table="people",
                        payload={"name": sender_name or sender_email.split("@")[0],
                                 "email": sender_email, "sources": "gmail"},
                        submitted_by="daily_sync:gmail",
                        source_table="emails", source_id=row["message_id"])
                    person_id = _res.get("person_id")
                except Exception:
                    person_id = None
                if not person_id:
                    try:
                        # people is write-gated: the fallback create must carry
                        # an actor or the write gate aborts.
                        with actor_scope(ops, "daily_sync:gmail-fallback-person"):
                            ops.execute(
                                "INSERT INTO people (name, email, sources, created_at) VALUES (?, ?, 'gmail', CURRENT_TIMESTAMP)",
                                (sender_name or sender_email.split("@")[0], sender_email)
                            )
                        person_id = ops.execute("SELECT last_insert_rowid()").fetchone()[0]
                    except sqlite3.IntegrityError:
                        result = ops.execute("SELECT id FROM people WHERE email = ?", (sender_email,)).fetchone()
                        if result:
                            person_id = result[0]

        # Upsert email
        outgoing_flag = _correct_is_outgoing(sender_email, row["is_outgoing"], sender_name)
        # Inbound inserts can fire a reopen trigger -> email_threads status flip
        # -> entities mirror UPDATE; the write gate (blocking) aborts that
        # cascade unless an actor is set on this connection.
        with actor_scope(ops, "daily_sync:gmail"):
            ops.execute("""
                INSERT OR IGNORE INTO emails
                (gmail_message_id, thread_id, sender_email, sender_name, recipients_json,
                 labels, subject, body, size, timestamp, is_read, is_outgoing, is_deleted, person_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["message_id"], row["thread_id"], sender_email, sender_name,
                row["recipients"], row["labels"], row["subject"], row["body"],
                row["size"], row["timestamp"], row["is_read"], outgoing_flag,
                row["is_deleted"], person_id
            ))
        if ops.execute("SELECT changes()").fetchone()[0] and row["thread_id"]:
            touched.add(row["thread_id"])
        added += 1

        # Update person_emails recency
        if sender_email and person_id:
            ops.execute("""
                INSERT INTO person_emails (person_id, email, last_seen, source)
                VALUES (?, ?, ?, 'gmail')
                ON CONFLICT(person_id, email) DO UPDATE SET
                    last_seen = MAX(COALESCE(person_emails.last_seen, ''), excluded.last_seen)
            """, (person_id, sender_email, row["timestamp"]))

    # Health check: detect silent sync failures
    if added == 0:
        # Check if MAX(timestamp) in emails table advanced
        max_ts_after = ops.execute(
            "SELECT MAX(timestamp) FROM emails"
        ).fetchone()[0]
        print(f"  WARNING: Gmail sync completed but no new emails merged.")
        print(f"  Latest email timestamp: {max_ts_after}")
        print(f"  Token may be expired or API quota exhausted.")
        # Don't update sync_state on zero-data sync so staleness is visible
        ops.commit()
        gmail_conn.close()
        ops.close()
        elapsed = time.time() - start
        print(f"  Gmail: 0 new emails in {elapsed:.1f}s (sync_state NOT updated)")
        return

    # Update sync state
    ops.execute("""
        INSERT INTO sync_state (source, last_sync, count)
        VALUES ('gmail', ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            last_sync = excluded.last_sync,
            count = sync_state.count + excluded.count
    """, (max_last_indexed, added))

    # Refresh email_threads aggregates for exactly the threads ingested this
    # sync (see the `touched` note above: a wall-clock window misses anything
    # merged late and leaves threads stale). last_sender_email = latest INBOUND
    # sender, falling back to latest any-direction sender (mirrors the inbox
    # triage backfill's preference; DRAFT filter belt-and-braces).
    refreshed = 0
    for tid in touched:
        ops.execute("""
            UPDATE email_threads
               SET last_outbound_ts = (SELECT MAX(timestamp) FROM emails WHERE thread_id=? AND is_outgoing=1 AND labels NOT LIKE '%DRAFT%'),
                   last_inbound_ts  = (SELECT MAX(timestamp) FROM emails WHERE thread_id=? AND is_outgoing=0 AND labels NOT LIKE '%DRAFT%'),
                   inbound_count    = (SELECT COUNT(*) FROM emails WHERE thread_id=? AND is_outgoing=0 AND labels NOT LIKE '%DRAFT%'),
                   outbound_count   = (SELECT COUNT(*) FROM emails WHERE thread_id=? AND is_outgoing=1 AND labels NOT LIKE '%DRAFT%'),
                   message_count    = (SELECT COUNT(*) FROM emails WHERE thread_id=? AND labels NOT LIKE '%DRAFT%'),
                   last_sender_email = COALESCE(
                       (SELECT sender_email FROM emails WHERE thread_id=? AND is_outgoing=0 AND labels NOT LIKE '%DRAFT%' ORDER BY timestamp DESC LIMIT 1),
                       (SELECT sender_email FROM emails WHERE thread_id=? AND labels NOT LIKE '%DRAFT%' ORDER BY timestamp DESC LIMIT 1)),
                   updated_at       = CURRENT_TIMESTAMP
             WHERE thread_id = ?
        """, (tid, tid, tid, tid, tid, tid, tid, tid))
        refreshed += ops.execute("SELECT changes()").fetchone()[0]

    ops.commit()
    gmail_conn.close()
    ops.close()

    elapsed = time.time() - start
    print(f"  Gmail: {added} new emails merged ({skipped_drafts} drafts skipped) + {refreshed} thread aggregates refreshed in {elapsed:.1f}s")


def discord_get(token, path, retries=3):
    """Discord API GET with rate limit handling."""
    url = urllib.parse.urljoin(DISCORD_API, path)
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("User-Agent", "ops-kit-sync/1.0")
    for attempt in range(retries):
        try:
            resp = urllib.request.urlopen(req)
            return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = json.loads(e.read().decode("utf-8")).get("retry_after", 1)
                time.sleep(float(retry_after) + 0.1)
                continue
            elif e.code == 403:
                return None
            else:
                if attempt < retries - 1:
                    time.sleep(1)
                    continue
                return None
    return None


# Guilds to auto-discover. Fill in config.toml:
#   [sync] discord_guild_ids = ["123456789012345678"]   (FICTIONAL example)
DISCORD_GUILDS = tuple(str(g) for g in (config.get("discord_guild_ids") or []))
# Discord channel `type` integers -> our role. 0=text,5=announce,2=voice,13=stage = message-bearing.
# 15=forum,16=media = THREAD-ONLY parents (GET messages returns empty). 4=category,14=directory = skip.
_MSG_CHANNEL_TYPES = {0, 5, 2, 13}
_FORUM_TYPES = {15, 16}
_THREAD_PARENT_TYPES = {0, 5, 15, 16}


def discover_channels(token, conn):
    """GET /guilds/{id}/channels for every configured guild (one call each, not paginated);
    bucket by integer type; UPSERT into discord_channels with a STRING channel_type label and
    return a list of (id, type_int) for every channel so the caller can branch forum vs
    message-bearing."""
    discovered = []
    for gid in DISCORD_GUILDS:
        chans = discord_get(token, f"guilds/{gid}/channels")
        if not isinstance(chans, list):
            continue
        for ch in chans:
            t = ch.get("type")
            if t in (4, 14):  # category / directory: skip
                continue
            label = "forum" if t in _FORUM_TYPES else "guild"
            conn.execute(
                "INSERT INTO discord_channels (id, name, guild_id, channel_type, imported_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, guild_id=excluded.guild_id, "
                "channel_type=excluded.channel_type",
                (ch["id"], ch.get("name", ""), gid, label))
            discovered.append((ch["id"], t))
    conn.commit()
    return discovered


def get_active_threads(token, guild_id):
    """GET /guilds/{id}/threads/active -- ONE call (response = {threads, members}; no pagination).
    Returns the list of active thread objects (forum posts + channel threads)."""
    data = discord_get(token, f"guilds/{guild_id}/threads/active")
    return data.get("threads", []) if isinstance(data, dict) else []


def get_archived_threads(token, parent_id, max_pages=20):
    """GET /channels/{id}/threads/archived/public?limit=100, paginate with before=<ISO8601
    archive_timestamp of the LAST thread> while has_more. Returns archived thread objects."""
    out, before = [], None
    for _ in range(max_pages):
        path = f"channels/{parent_id}/threads/archived/public?limit=100"
        if before:
            path += f"&before={urllib.parse.quote(before)}"
        data = discord_get(token, path)
        if not isinstance(data, dict):
            break
        threads = data.get("threads", [])
        out.extend(threads)
        if not data.get("has_more") or not threads:
            break
        # cursor is the archive_timestamp (ISO8601) of the last thread in this page
        meta = threads[-1].get("thread_metadata", {})
        before = meta.get("archive_timestamp")
        if not before:
            break
    return out


def _ingest_target_messages(token, conn, target_id, backfill=False):
    """Fetch + store messages for one message-bearing target (channel or thread).
    Incremental: after=<stored cursor>. Backfill (new target): page with before until <100.
    Returns count of new messages. Idempotent on snowflake ids."""
    cur = conn.execute("SELECT last_id FROM sync_state WHERE source=?", (f"discord_{target_id}",)).fetchone()
    have_cursor = bool(cur and cur[0] and cur[0] != "0")
    new_count, latest_id = 0, (cur[0] if have_cursor else "0")

    def _store(messages):
        nonlocal new_count, latest_id
        for msg in messages:
            if msg.get("type") not in (0, 19):
                continue
            if (msg.get("timestamp", "")[:10]) < DISCORD_CUTOFF:
                continue
            author = msg.get("author", {})
            aid = author.get("id", "")
            # ON CONFLICT (not INSERT OR REPLACE) so re-syncing a user never nulls
            # person_id / imported_at -- REPLACE deletes+reinserts and wipes the
            # person link (this clobbered dozens of links during a real backfill).
            conn.execute("INSERT INTO discord_users (id, username, discriminator, avatar, bot) "
                         "VALUES (?, ?, ?, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET username=excluded.username, "
                         "discriminator=excluded.discriminator, avatar=excluded.avatar, bot=excluded.bot",
                         (aid, author.get("username", ""), author.get("discriminator", "0"),
                          author.get("avatar", ""), 1 if author.get("bot") else 0))
            reply_to = msg["referenced_message"].get("id") if (msg.get("type") == 19 and msg.get("referenced_message")) else None
            try:
                conn.execute("INSERT OR IGNORE INTO discord_messages "
                             "(id, timestamp, edited_timestamp, content, pinned, author_id, reply_to_id, channel_id) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (msg["id"], msg["timestamp"], msg.get("edited_timestamp"), msg.get("content"),
                              1 if msg.get("pinned") else 0, aid, reply_to, target_id))
                if conn.execute("SELECT changes()").fetchone()[0]:
                    new_count += 1
            except sqlite3.Error:
                pass
            if len(msg["id"]) > len(latest_id) or (len(msg["id"]) == len(latest_id) and msg["id"] > latest_id):
                latest_id = msg["id"]

    if have_cursor and not backfill:
        msgs = discord_get(token, f"channels/{target_id}/messages?after={latest_id}&limit=100")
        if isinstance(msgs, list) and msgs:
            _store(list(reversed(msgs)))
    else:
        # backfill: page backward with before=<oldest id in page> until a short page
        before = None
        for _ in range(200):  # hard cap (~20k msgs/target) so a hot channel can't loop forever
            path = f"channels/{target_id}/messages?limit=100" + (f"&before={before}" if before else "")
            msgs = discord_get(token, path)
            if not isinstance(msgs, list) or not msgs:
                break
            _store(msgs)
            before = msgs[-1]["id"]  # oldest in this page (Discord returns newest-first)
            if len(msgs) < 100:
                break

    if new_count > 0 or not have_cursor:
        conn.execute("INSERT INTO sync_state (source, last_sync, last_id, count) VALUES (?, CURRENT_TIMESTAMP, ?, ?) "
                     "ON CONFLICT(source) DO UPDATE SET last_sync=CURRENT_TIMESTAMP, last_id=excluded.last_id, "
                     "count=COALESCE(sync_state.count,0)+excluded.count",
                     (f"discord_{target_id}", latest_id, new_count))
        conn.commit()
    return new_count


def sync_discord(backfill_new=True, include_archived=False):
    """Discord sync with auto-discovery + forum/thread handling.
    (1) discover channels in the configured guilds; (2) active threads; (3) [manual full
    runs only] archived public threads of forum/text parents; (4) ingest messages for every
    message-bearing target (text/announce/voice channels + every thread) with a per-target
    cursor.
    include_archived defaults to False: the SCHEDULED incremental run does channels (cursor) +
    ACTIVE threads only (fast, covers live content). A forum can have hundreds of archived
    threads, so the deep archived backfill is an explicit opt-in (include_archived=True) to keep
    the scheduled tick from holding the write lock for many minutes."""
    print("\n[Discord] Running sync (auto-discovery + forums/threads)...")
    if not DISCORD_GUILDS:
        print("  No Discord guilds configured ([sync] discord_guild_ids in config.toml); skipping.")
        return
    start = time.time()
    token = keyring.get_password(KEYRING_SERVICE, "discord_bot_token")
    if not token:
        print(f"  No Discord bot token found (keyring: {KEYRING_SERVICE} / discord_bot_token)")
        return
    conn = _db.connect(DB)

    discovered = discover_channels(token, conn)
    print(f"  discovered {len(discovered)} channels across {len(DISCORD_GUILDS)} guilds")

    # message-bearing channels (text/announce/voice/stage) and forum/text parents for thread discovery
    msg_channels = [cid for cid, t in discovered if t in _MSG_CHANNEL_TYPES]
    thread_parents = [cid for cid, t in discovered if t in _THREAD_PARENT_TYPES]

    # collect threads: active (per guild) + archived public (per parent)
    threads = {}
    for gid in DISCORD_GUILDS:
        for th in get_active_threads(token, gid):
            threads[th["id"]] = th
    forum_thread_ingested = 0
    if include_archived:
        for pid in thread_parents:
            for th in get_archived_threads(token, pid):
                threads.setdefault(th["id"], th)
    print(f"  found {len(threads)} threads (active{' + archived' if include_archived else ''})")

    # Register every thread as a channel row FIRST: discord_messages.channel_id has a
    # FK to discord_channels, and a thread message's channel_id IS the thread id -- so the
    # thread must exist in discord_channels or the message INSERT orphans the FK.
    for tid, th in threads.items():
        conn.execute(
            "INSERT OR IGNORE INTO discord_channels(id,name,guild_id,channel_type,imported_at) "
            "VALUES(?,?,?,'thread',CURRENT_TIMESTAMP)",
            (tid, (th.get("name") or f"thread-{tid}"), th.get("guild_id")))
    conn.commit()

    targets = list(dict.fromkeys(msg_channels + list(threads.keys())))
    total_new = 0
    for tid in targets:
        try:
            n = _ingest_target_messages(token, conn, tid, backfill=backfill_new)
            total_new += n
            if n and tid in threads:
                forum_thread_ingested += 1
            # Commit per channel/thread so a wrapper timeout keeps partial
            # progress + cursors instead of rolling back the whole sweep
            # (tens-of-thousands-of-message re-syncs hurt).
            conn.commit()
        except Exception as e:  # noqa: BLE001 -- one bad target must not abort the sweep
            print(f"  target {tid} error: {str(e)[:80]}")
    conn.commit()
    conn.close()
    print(f"  [Discord] {total_new} new messages across {len(targets)} targets, "
          f"{forum_thread_ingested} threads w/ new msgs ({time.time()-start:.0f}s)")

    elapsed = time.time() - start
    print(f"  Discord: {total_new} new messages in {elapsed:.1f}s")


def _ensure_notion_tables(conn):
    """notion_exports / notion_sync are not part of the core starter schema;
    this module is their sole writer, so it owns their DDL and creates them on
    first use (only when the operator has configured a Notion sync)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notion_exports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_zip TEXT,
            csv_filename TEXT,
            row_data JSON,
            imported_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notion_sync (
            database_id TEXT PRIMARY KEY,
            database_name TEXT,
            last_sync DATETIME,
            rows_synced INTEGER DEFAULT 0
        )""")


def _notion_extract_field(data, *keys):
    """Return the first non-empty value among keys."""
    for key in keys:
        val = data.get(key, "")
        if val and str(val).strip() and str(val).strip().lower() not in ("n/a", "none", "null", ""):
            return str(val).strip()
    return None


def _notion_enrich_fields(conn, props, type_label, nx_id):
    """Post-ingest enrichment leg: extract structured people fields from one
    freshly-inserted notion_exports row and route them through the steward bus
    (source_table='notion_exports', source_id=<row id>).

    Branches are keyed on the TYPE label (second element of each
    [sync] notion_databases config entry). Only a generic contact/people branch
    ships; the column names below are examples from a typical contacts/CRM
    database -- adapt them to your own Notion schema, and add branches for your
    own database types following the same _emit pattern.

    Discord *linkage* (matching discord_users rows to people by name) is
    deliberately NOT here -- that is a chat-linkage concern owned by the
    beeper/discord linkage path, not Notion field extraction.
    """
    if not isinstance(props, dict):
        return 0

    def _emit(pid, updates):
        if not updates:
            return 0
        _BUS.write(conn, target_table="people", payload=updates,
                   natural_key={"person_id": pid},
                   submitted_by="daily_sync:notion-enrich",
                   source_table="notion_exports", source_id=nx_id)
        return 1

    written = 0

    if str(type_label).lower() in ("people", "person", "contact", "contacts"):
        email = _notion_extract_field(props, "Email", "email", "Email Address")
        if email and "@" in email:
            person = conn.execute("SELECT id FROM people WHERE email = ?", (email.lower().strip(),)).fetchone()
            if person:
                updates = {}
                linkedin = _notion_extract_field(props, "LinkedIn", "linkedin", "Link to CV")
                if linkedin and "linkedin.com" in linkedin:
                    updates["linkedin"] = linkedin
                affiliation = _notion_extract_field(props, "Affiliation", "Headline")
                if affiliation:
                    updates["headline"] = affiliation[:200]
                location = _notion_extract_field(props, "Location")
                if location:
                    updates["location"] = location[:100]
                discord = _notion_extract_field(props, "Discord handle", "discord", "Discord username")
                if discord:
                    updates["discord_username"] = discord[:50]
                career = _notion_extract_field(props, "Career stage", "career_stage")
                if career:
                    updates["career_stage"] = career[:200]
                written += _emit(person[0], updates)

    return written


# External Notion-query command template. Each element is str.format-ed with
# {db_id}. The command must print a JSON array of row objects (or an object
# with a "results" list), each row carrying id / title / modified_time (or
# last_edited_time) / properties. Fill in config.toml -- FICTIONAL example:
#   [sync]
#   notion_query_cmd = ["python", "/path/to/notion_dump.py", "database", "{db_id}", "--json", "-n", "100"]
# Any CLI that meets this contract works (a small script over the official
# Notion API is ~30 lines; see INSTALL.md).
NOTION_QUERY_CMD = [str(x) for x in (config.get("sync.notion_query_cmd") or [])]


def sync_notion():
    """Pull recently modified Notion rows via the configured external CLI.

    Applies a client-side 3-day modified_time filter and an exact-row dedupe
    (an edited page re-inserts as a new version; unchanged pages are skipped),
    then extracts emails -> people upserts and runs the per-row enrichment leg.
    """
    print("\n[Notion] Syncing recently modified rows...")

    # Databases to sync. Fill in config.toml -- FICTIONAL example ids:
    #   [sync.notion_databases]
    #   "00000000-0000-0000-0000-000000000001" = ["Contacts", "contact"]
    #   "00000000-0000-0000-0000-000000000002" = ["Tasks", "task"]
    raw_dbs = config.get("notion_databases") or {}
    databases = {}
    for db_id, val in raw_dbs.items():
        if isinstance(val, (list, tuple)) and len(val) >= 2:
            databases[str(db_id)] = (str(val[0]), str(val[1]))
        elif isinstance(val, str):
            databases[str(db_id)] = (val, val)

    if not NOTION_QUERY_CMD or not databases:
        print("  Notion sync not configured ([sync] notion_query_cmd + notion_databases "
              "in config.toml); skipping.")
        return

    start = time.time()
    conn = _db.connect(DB)
    _ensure_notion_tables(conn)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_rows = 0
    for db_id, (db_name, type_label) in databases.items():
        try:
            cmd = [part.format(db_id=db_id) for part in NOTION_QUERY_CMD]
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=120,
                encoding="utf-8", errors="replace",
                env={**os.environ, "PYTHONUTF8": "1"}
            )
            if result.returncode != 0:
                print(f"  {db_name}: query command failed: {(result.stderr or result.stdout or '')[-200:]}")
                continue

            rows = json.loads(result.stdout)
            if not isinstance(rows, list):
                rows = rows.get("results", [])
            # client-side last-3-days filter + skip rows already stored verbatim
            rows = [r for r in rows
                    if str(r.get("modified_time") or r.get("last_edited_time") or "9999") >= cutoff]

            for row in rows:
                # Store as notion_exports row. Keep page id + modified_time
                # alongside properties so versions are distinguishable, and
                # skip byte-identical rows (unchanged page re-fetched).
                props = row.get("properties", row)
                stored = {"id": row.get("id"), "title": row.get("title"),
                          "modified_time": row.get("modified_time"),
                          "properties": props} if isinstance(row, dict) and row.get("id") else props
                row_json = json.dumps(stored, ensure_ascii=False, sort_keys=True)
                if conn.execute("SELECT 1 FROM notion_exports WHERE row_data = ? LIMIT 1", (row_json,)).fetchone():
                    continue
                conn.execute("""
                    INSERT INTO notion_exports (source_zip, csv_filename, row_data)
                    VALUES (?, ?, ?)
                """, (f"notion_sync_{datetime.now().strftime('%Y%m%d')}", f"{type_label}_sync", row_json))
                nx_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]  # provenance source_id for routed people creates
                total_rows += 1

                # Extract emails and upsert people
                row_str = json.dumps(props)
                emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', row_str)
                for email in set(emails):
                    email = email.lower()
                    existing = conn.execute("SELECT id FROM people WHERE email = ?", (email,)).fetchone()
                    if existing:
                        with actor_scope(conn, "daily_sync:notion-touch"):
                            conn.execute("UPDATE people SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (existing[0],))
                    else:
                        # ROUTE Notion people creates through the bus: provenance
                        # source notion_exports/rowid + non-NULL actor.
                        try:
                            _BUS.write(conn, target_table="people",
                                       payload={"name": email.split("@")[0], "email": email,
                                                "sources": f"notion_sync:{db_name}"},
                                       submitted_by="daily_sync:notion",
                                       source_table="notion_exports", source_id=nx_id)
                        except Exception:
                            # people is write-gated: fallback create needs an actor.
                            with actor_scope(conn, "daily_sync:notion-fallback-person"):
                                conn.execute("""
                                    INSERT OR IGNORE INTO people (name, email, sources, created_at)
                                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                                """, (email.split("@")[0], email, f"notion_sync:{db_name}"))

                # Post-ingest enrichment: extract structured people fields from
                # this fresh notion_exports row. Best-effort; never break
                # ingestion.
                try:
                    _notion_enrich_fields(conn, props, type_label, nx_id)
                except Exception:
                    pass

            # Update sync tracking
            conn.execute("""
                INSERT INTO notion_sync (database_id, database_name, last_sync, rows_synced)
                VALUES (?, ?, CURRENT_TIMESTAMP, ?)
                ON CONFLICT(database_id) DO UPDATE SET
                    last_sync = CURRENT_TIMESTAMP,
                    rows_synced = notion_sync.rows_synced + excluded.rows_synced
            """, (db_id, db_name, len(rows)))

            print(f"  {db_name}: {len(rows)} rows synced")

        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"  {db_name}: error - {e}")
            continue

    conn.commit()
    conn.close()

    elapsed = time.time() - start
    print(f"  Notion: {total_rows} total rows in {elapsed:.1f}s")


def print_summary():
    """Print sync summary."""
    conn = _db.connect(DB)
    print("\n=== Ops DB Summary ===")
    for t in ["people", "emails", "discord_messages", "person_emails",
              "action_items", "learnings", "observations", "notion_exports"]:
        if not _table_exists(conn, t):
            continue
        c = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
        print(f"  {t}: {c:,}")

    # Show recent syncs
    syncs = conn.execute("""
        SELECT source, last_sync, count FROM sync_state
        WHERE source IN ('gmail', 'discord_total')
        ORDER BY last_sync DESC
    """).fetchall()
    print("\n  Last syncs:")
    for s, ts, c in syncs:
        print(f"    {s}: {ts} ({c} total)")

    if _table_exists(conn, "notion_sync"):
        notion_syncs = conn.execute("SELECT database_name, last_sync, rows_synced FROM notion_sync ORDER BY last_sync DESC").fetchall()
        for name, ts, c in notion_syncs:
            print(f"    Notion/{name}: {ts} ({c} rows)")

    db_size = os.path.getsize(DB) / (1024 * 1024)
    print(f"\n  DB size: {db_size:.1f} MB")
    conn.close()


# Populated by sync steps that must not fail silently; nonzero exit at the end.
SYNC_FAILURES: list[str] = []


def main():
    args = sys.argv[1:]
    do_all = not args or "--full" in args

    # Notion-only liveness (a scheduled task can run `daily_sync.py --notion-only`
    # for a daily Notion pull). Smoke = heartbeat row only. Fail-open import so
    # observability never breaks the sync (job_heartbeat is optional).
    if "--notion-only" in args:
        try:
            from job_heartbeat import heartbeat as _heartbeat
        except Exception:
            import contextlib as _cl
            def _heartbeat(job):
                return _cl.nullcontext(type("_HB", (), {"rows_touched": 0, "exit_note": None})())
        if "--smoke" in args:
            with _heartbeat("NotionDailySync") as hb:
                hb.exit_note = "smoke"
            print("smoke run: NotionDailySync heartbeat row written, Notion NOT touched")
            return
        with _heartbeat("NotionDailySync"):
            sync_notion()
            print_summary()
        return

    print(f"=== Daily Sync ===")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if do_all or "--gmail-only" in args:
        sync_gmail()

    if do_all or "--discord-only" in args:
        sync_discord()

    if do_all or "--discord-dms-only" in args or "--discord-only" in args:
        # Discord DM sync uses the user token (different from guild bot token).
        # Lives in its own script so we keep the bot vs user concerns separate.
        # Failures PROPAGATE (exit 2 at the end): a dead DM pipeline once went
        # unnoticed for 12 days when this block swallowed the crash.
        try:
            print("\n[Discord DMs] Running incremental sync...")
            r = subprocess.run(
                [PYTHON, str(Path(__file__).parent / "sync_discord_dms.py")],
                capture_output=True, text=True, timeout=600,
                env={**os.environ, "PYTHONUTF8": "1"},
            )
            for line in (r.stdout or "").strip().splitlines()[-5:]:
                print(f"  {line}")
            if r.returncode != 0:
                SYNC_FAILURES.append(f"discord_dms (exit {r.returncode})")
                print(f"  ERROR: sync_discord_dms.py exited {r.returncode}", file=sys.stderr)
                for line in (r.stderr or "no stderr").strip().splitlines()[-5:]:
                    print(f"  ERROR: {line}", file=sys.stderr)
        except Exception as e:
            SYNC_FAILURES.append(f"discord_dms ({e})")
            print(f"  Discord DM sync failed: {e}", file=sys.stderr)

    # After any Discord sync (guild or DM), incrementally re-segment messages
    # into threads. Thread reconstruction is NOT included in this starter kit;
    # feature-detect a local reconstructor script so one can be dropped in
    # later. (A reconstructor should only touch messages with thread_id IS
    # NULL, so it stays a cheap no-op when there's nothing new.)
    if do_all or "--discord-only" in args or "--discord-dms-only" in args:
        _reconstructor = Path(__file__).parent / "reconstruct_discord_threads.py"
        if _reconstructor.is_file():
            try:
                print("\n[Discord Threads] Incremental reconstruction...")
                r = subprocess.run(
                    [PYTHON, str(_reconstructor)],
                    capture_output=True, text=True, timeout=300,
                    env={**os.environ, "PYTHONUTF8": "1"},
                )
                for line in (r.stdout or "").strip().splitlines()[-3:]:
                    print(f"  {line}")
            except Exception as e:
                print(f"  Discord thread reconstruction failed: {e}")
        else:
            print("\n[Discord Threads] reconstructor not included in this starter kit; skipping.")

    # Episodes materializer: keeps the episodes table current from
    # emails/discord/beeper. Idempotent (content_hash). It began life as a
    # one-off backfill with no scheduled continuation, which is how the
    # episodes table silently went stale -- hence this scheduled leg.
    if do_all:
        try:
            print("\n[Episodes] Incremental materialize...")
            r = subprocess.run(
                [PYTHON, str(paths.ROOT / "logging" / "backfill_episodes.py")],
                capture_output=True, text=True, timeout=600,
                env={**os.environ, "PYTHONUTF8": "1"},
            )
            for line in (r.stdout or "").strip().splitlines()[-4:]:
                print(f"  {line}")
            if r.returncode != 0:
                SYNC_FAILURES.append(f"episodes (exit {r.returncode})")
                for line in (r.stderr or "no stderr").strip().splitlines()[-3:]:
                    print(f"  ERROR: {line}", file=sys.stderr)
        except Exception as e:
            SYNC_FAILURES.append(f"episodes ({e})")
            print(f"  Episodes materialize failed: {e}", file=sys.stderr)

    # Notion rides in the full daily flow when configured. (--notion-only
    # alone takes the early-return heartbeat path above; this block is the
    # full-run leg.)
    if do_all:
        sync_notion()

    print_summary()
    print(f"\nDone: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if SYNC_FAILURES:
        print(f"\nSYNC FAILURES ({len(SYNC_FAILURES)}): {', '.join(SYNC_FAILURES)}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
