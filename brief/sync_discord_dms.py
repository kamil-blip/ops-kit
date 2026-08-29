"""sync_discord_dms.py, Sync the operator's Discord DM channels + messages into ops.db.

Why this exists separately from sync_discord():
  - sync_discord() uses the **bot token** which cannot access user DMs.
  - This script uses the **user token** to call /users/@me/channels + /channels/{id}/messages
    from the operator's perspective. DMs are 1-on-1 + small group chats.

Token: keyring.get_password(<keyring_service from config, default "ops-kit">,
"discord_user_token"). Store your Discord user token there at install time.

Usage:
  python sync_discord_dms.py            # incremental: only new messages per channel
  python sync_discord_dms.py --full     # refresh channel list + sync all DMs
  python sync_discord_dms.py --since 2026-01-01  # custom cutoff

Discord channel types we handle:
  type=1  DM (1-on-1)
  type=3  Group DM (the operator + 2 or more others)

Person linkage:
  After inserting a Discord user, we look up discord_users.person_id (set by the
  people-merger). DM channels get dm_recipient_person_id populated from the same
  source so future graph traversal works.
"""
from __future__ import annotations
import paths
import config
import argparse, json, keyring, sqlite3, sys, time, urllib.error, urllib.parse, urllib.request
import _db  # unified connector (busy_timeout + FK ON)
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = str(paths.DB_PATH)
API_BASE = "https://discord.com/api/v9/"
DEFAULT_CUTOFF = "2026-01-01"  # mirrors DISCORD_CUTOFF in daily_sync.py
USER_AGENT = "Mozilla/5.0"
SLEEP_BETWEEN_CHANNELS = 0.4  # courtesy delay
KEYRING_SERVICE = config.get("keyring_service")  # default "ops-kit"


def get_token() -> str:
    tok = keyring.get_password(KEYRING_SERVICE, "discord_user_token")
    if not tok:
        print(f"ERROR: no {KEYRING_SERVICE}/discord_user_token in keyring", file=sys.stderr)
        sys.exit(1)
    return tok


def api_get(token: str, path: str, retries: int = 3):
    url = urllib.parse.urljoin(API_BASE, path)
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    req.add_header("User-Agent", USER_AGENT)
    for attempt in range(retries):
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(json.loads(e.read().decode("utf-8")).get("retry_after", 1.0))
                except Exception:
                    pass
                time.sleep(retry_after + 0.2)
                continue
            elif e.code == 403:
                return None
            else:
                if attempt < retries - 1:
                    time.sleep(1.0)
                    continue
                print(f"  HTTP {e.code} for {path}: {e.reason}", file=sys.stderr)
                return None
        except urllib.error.URLError as e:
            print(f"  URLError {path}: {e}", file=sys.stderr)
            time.sleep(1.0)
    return None


def upsert_user(conn: sqlite3.Connection, author: dict) -> None:
    """Insert/update discord_users row. Does NOT overwrite person_id link if already set."""
    conn.execute(
        """INSERT INTO discord_users (id, username, discriminator, avatar, bot)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             username = excluded.username,
             discriminator = excluded.discriminator,
             avatar = excluded.avatar,
             bot = excluded.bot""",
        (
            author.get("id"),
            author.get("username", ""),
            author.get("discriminator", "0"),
            author.get("avatar", ""),
            1 if author.get("bot") else 0,
        ),
    )


def resolve_person_id(conn: sqlite3.Connection, discord_id: str) -> int | None:
    """Return people.id linked to this discord user via discord_users.person_id."""
    r = conn.execute(
        "SELECT person_id FROM discord_users WHERE id = ?", (discord_id,)
    ).fetchone()
    if r and r[0]:
        return r[0]
    # Fallback: try people.discord_user_id direct match.
    r = conn.execute(
        "SELECT id FROM people WHERE discord_user_id = ?", (discord_id,)
    ).fetchone()
    return r[0] if r else None


def upsert_dm_channel(conn: sqlite3.Connection, ch: dict) -> None:
    """Insert/update a DM or group_dm row in discord_channels."""
    ch_id = ch["id"]
    ch_type = ch.get("type")
    recipients = ch.get("recipients", [])

    if ch_type == 1:
        # 1-on-1 DM
        if not recipients:
            return
        recip = recipients[0]
        person_id = resolve_person_id(conn, recip.get("id"))
        conn.execute(
            """INSERT INTO discord_channels (id, name, guild_id, channel_type,
                                              dm_recipient_id, dm_recipient_username,
                                              dm_recipient_person_id, imported_at)
               VALUES (?, ?, NULL, 'dm', ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 channel_type = 'dm',
                 dm_recipient_id = excluded.dm_recipient_id,
                 dm_recipient_username = excluded.dm_recipient_username,
                 dm_recipient_person_id = COALESCE(excluded.dm_recipient_person_id,
                                                    discord_channels.dm_recipient_person_id)""",
            (
                ch_id,
                f"DM: {recip.get('username') or recip.get('global_name') or recip.get('id')}",
                recip.get("id"),
                recip.get("username") or recip.get("global_name"),
                person_id,
            ),
        )
    elif ch_type == 3:
        # Group DM
        recip_ids = ",".join(r.get("id", "") for r in recipients)
        names = ", ".join(r.get("username") or r.get("global_name") or r.get("id") for r in recipients)
        conn.execute(
            """INSERT INTO discord_channels (id, name, guild_id, channel_type,
                                              group_dm_recipient_ids, imported_at)
               VALUES (?, ?, NULL, 'group_dm', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 channel_type = 'group_dm',
                 group_dm_recipient_ids = excluded.group_dm_recipient_ids""",
            (ch_id, f"Group: {names[:80]}", recip_ids),
        )


def sync_messages_for_channel(conn: sqlite3.Connection, token: str, ch_id: str, cutoff: str,
                                max_pages: int = 50) -> int:
    """Pull new messages from a DM channel into discord_messages, paginating
    while a full 100-message page is returned (means more available after).

    max_pages cap prevents runaway pulls on a one-shot backfill of an enormous
    DM history; default 50 pages = 5000 msgs per channel per sync call.

    Uses sync_state.last_id keyed as 'discord_dm_{ch_id}' (separate from guild sync
    state so the two cursors don't collide).
    """
    state_key = f"discord_dm_{ch_id}"
    last = conn.execute(
        "SELECT last_id FROM sync_state WHERE source = ?", (state_key,)
    ).fetchone()
    after_id = (last[0] if last and last[0] else "0")

    total_new = 0
    pages = 0
    while pages < max_pages:
        params = urllib.parse.urlencode({"after": after_id, "limit": 100})
        messages = api_get(token, f"channels/{ch_id}/messages?{params}")
        if not messages:
            break
        page_new = 0
        latest_id = after_id
        for msg in reversed(messages):
            if msg.get("type") not in (0, 19):
                continue
            ts = msg.get("timestamp", "")
            if ts[:10] < cutoff:
                continue
            author = msg.get("author", {})
            upsert_user(conn, author)
            reply_to = None
            if msg.get("type") == 19 and msg.get("referenced_message"):
                reply_to = msg["referenced_message"].get("id")
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO discord_messages
                       (id, timestamp, edited_timestamp, content, pinned, author_id, reply_to_id, channel_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        msg["id"], msg["timestamp"], msg.get("edited_timestamp"),
                        msg.get("content"), 1 if msg.get("pinned") else 0,
                        author.get("id"), reply_to, ch_id,
                    ),
                )
                if conn.total_changes:
                    page_new += 1
            except sqlite3.Error as e:
                print(f"  msg insert error in {ch_id}: {e}", file=sys.stderr)
            if msg["id"] > latest_id:
                latest_id = msg["id"]

        # Persist cursor every page so a crash doesn't lose progress.
        if latest_id and latest_id != after_id:
            conn.execute(
                """INSERT INTO sync_state (source, last_id, last_sync)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(source) DO UPDATE SET
                     last_id = excluded.last_id,
                     last_sync = excluded.last_sync""",
                (state_key, latest_id),
            )
            conn.commit()

        total_new += page_new
        # If we got fewer than 100 results, we've reached the head.
        if len(messages) < 100:
            break
        after_id = latest_id
        pages += 1
        time.sleep(0.3)  # courtesy delay between pages

    return total_new


def write_sync_state(conn: sqlite3.Connection, status: str, count: int = 0) -> None:
    """Global freshness/health marker for this pipeline (source='discord_dms').

    last_id carries 'ok' or 'error: ...' so brief.py status and drift_check
    can tell a healthy-but-quiet night from a silently dead pipeline. The
    per-channel cursors keep their own 'discord_dm_{id}' rows.
    """
    conn.execute(
        """INSERT INTO sync_state (source, last_sync, last_id, count)
           VALUES ('discord_dms', datetime('now'), ?, ?)
           ON CONFLICT(source) DO UPDATE SET
             last_sync = excluded.last_sync,
             last_id = excluded.last_id,
             count = excluded.count""",
        (status[:200], count),
    )
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="Refresh channel list before sync")
    ap.add_argument("--since", default=DEFAULT_CUTOFF, help="Cutoff date YYYY-MM-DD")
    ap.add_argument("--channel", help="Only sync this channel id (test mode)")
    args = ap.parse_args()

    token = get_token()
    conn = _db.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")

    print(f"[DM Sync] Starting at {datetime.now().isoformat()[:19]}")
    print(f"  cutoff: {args.since}")

    # 1. Refresh channel list (always, DM list is small and changes rarely).
    print("  Fetching DM channel list via /users/@me/channels...")
    channels = api_get(token, "users/@me/channels")
    if not channels:
        print("  ERROR: could not fetch DM channel list", file=sys.stderr)
        write_sync_state(conn, "error: could not fetch DM channel list (token dead? 401)")
        return 1
    dm_count = sum(1 for c in channels if c.get("type") == 1)
    group_count = sum(1 for c in channels if c.get("type") == 3)
    print(f"  Found {dm_count} DMs + {group_count} group DMs")

    # One bad channel must never kill the whole run (a single upsert collision
    # once silently aborted every nightly sync for days).
    upsert_failures = []
    for ch in channels:
        if ch.get("type") in (1, 3):
            try:
                upsert_dm_channel(conn, ch)
            except sqlite3.Error as e:
                upsert_failures.append((ch["id"], str(e)))
                print(f"  channel upsert FAILED {ch['id']}: {e}", file=sys.stderr)
    conn.commit()

    # 2. Sync messages per channel.
    if args.channel:
        target_ids = [args.channel]
    else:
        target_ids = [ch["id"] for ch in channels if ch.get("type") in (1, 3)]

    total_new = 0
    channels_with_new = 0
    for i, ch_id in enumerate(target_ids):
        try:
            new = sync_messages_for_channel(conn, token, ch_id, args.since)
        except Exception as e:
            print(f"  channel {ch_id} sync error: {e}", file=sys.stderr)
            continue
        if new > 0:
            channels_with_new += 1
            total_new += new
            # Don't spam, only print channels with new activity.
            ch_info = conn.execute(
                "SELECT name FROM discord_channels WHERE id = ?", (ch_id,)
            ).fetchone()
            ch_name = ch_info[0] if ch_info else ch_id
            # This print sits outside the per-channel try/except, so a
            # headless-stdout OSError (Errno 22, Invalid argument -- unicode
            # channel name to a redirected handle) used to abort the entire
            # run mid-sync. A print failure must never kill it.
            try:
                print(f"    +{new:3d}  {ch_name[:60]}")
            except OSError:
                pass
        # Periodically commit so a crash doesn't lose everything.
        if (i + 1) % 25 == 0:
            conn.commit()
        time.sleep(SLEEP_BETWEEN_CHANNELS)

    conn.commit()
    if upsert_failures:
        write_sync_state(
            conn,
            f"ok-with-errors: {len(upsert_failures)} channel upsert failures, first {upsert_failures[0][0]}: {upsert_failures[0][1]}",
            total_new,
        )
    else:
        write_sync_state(conn, "ok", total_new)
    conn.close()
    print(f"[DM Sync] Done. {total_new} new messages across {channels_with_new} DM channels.")
    if upsert_failures:
        print(f"[DM Sync] WARNING: {len(upsert_failures)} channel upserts failed (see stderr)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    # Headless runs redirect stdout to a non-console handle; a unicode channel
    # name then raised OSError [Errno 22] / UnicodeEncodeError. Force UTF-8
    # with replacement so output can never abort the sync.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - record fatal errors before dying loud
        try:
            _c = _db.connect(DB_PATH, timeout=60)
            _c.execute("PRAGMA busy_timeout=60000")
            write_sync_state(_c, f"error: {type(e).__name__}: {e}")
            _c.close()
        except Exception:  # noqa: BLE001
            pass
        raise
