"""
Brief system orchestrator.

Provides the sync + gather + classify + apply pipeline that powers the daily
briefing. The sync step downloads new data into ops.db. The gather step
outputs new items as JSON for a Claude session (or the `classify` subcommand)
to classify. The apply step writes classification results back to the DB and
updates action items.

NOTE (starter kit): the grounded claim-extraction / enrichment layer is NOT
included in this starter kit. The extraction steps inside `sync` are no-op
stubs that log and skip; sync, gather, classify, and apply all work without it.

Usage:
    python brief.py sync                        # Run all data syncs
    python brief.py sync --gmail                # Sync only gmail
    python brief.py sync --beeper-priority 1    # Only high-priority beeper chats
    python brief.py gather                      # New items since last brief (summary)
    python brief.py gather --json               # Full JSON output for classification
    python brief.py gather --since "2026-04-14" # Custom cutoff
    python brief.py classify BRIEF_ID           # Classify gathered items (JSON stdin)
    python brief.py apply BRIEF_ID              # Read classification JSON from stdin
    python brief.py new-brief                   # Create briefing_reports row, print ID
    python brief.py close-brief ID "summary"    # Finalize brief
    python brief.py report                      # Show latest brief
    python brief.py status                      # Show sync freshness
    python brief.py registry                    # List beeper chat registry
    python brief.py granola-check               # List synced transcript slugs
    python brief.py granola-check --json        # JSON list for diff
    python brief.py granola-store < meeting.json # Store meeting (JSON from stdin)
    python brief.py correct ID --category X     # Record classification correction
    python brief.py correct ID --priority Y     # Correct priority only

Configuration (config.toml; every key optional, blank disables the leg):
    [operator]  name / role / emails / discord_user_id / display_name
    [org]       name / description / email_aliases
    [keys]      keyring_service (default "ops-kit")
    [sync]      google_cli            path to an optional Google Workspace CLI
                                      script (calendar events + gmail label
                                      search); blank disables those legs
                gmail_triage_labels   list of Gmail label names your own email
                                      automation applies (pre-triage signal)
                google_chat_spaces    list of Google Chat space display names
                                      to pull summaries from
                google_chat_token_path  OAuth token file for the Chat API
    [beeper.chats."<chat-id>"]        chat registry; see `_load_beeper_registry`
"""
import sys
from pathlib import Path

try:
    import paths  # repo path resolver (core/paths.py); on sys.path via the installer's .pth
except ImportError:
    # Direct-invocation fallback: walk up from this file to the repo root and
    # put core/ on sys.path (the installed venv normally does this via a .pth).
    sys.path.insert(0, str(next(
        p / "core" for p in Path(__file__).resolve().parents
        if (p / "core" / "paths.py").is_file()
    )))
    import paths
import _db     # shared connector (busy_timeout + FK ON)
import config  # operator configuration loader (core/config.py)

import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")

# ─── Paths ───────────────────────────────────────────────────────────────────
UNIFIED_DB = Path(str(paths.DB_PATH))
PYTHON = str(paths.PYTHON)
MODULE_DIR = Path(__file__).resolve().parent          # brief/ (sibling sync scripts)
COMMS_DIR = Path(str(paths.ROOT)) / "comms"           # comms/ capability dir
SEARCH_DIR = Path(str(paths.ROOT)) / "search"         # search/ capability dir (embed_*)

# Ingest hardening: validators for every action_items write path.
# log_rejection drops malformed inserts into ingest_rejections.
from validators import (  # noqa: E402
    validate_action_item, log_rejection, ensure_table,
    route_action_item, propose_to_inbox, build_source_url,
)

# Structural prompt-injection reader for inbound classify items. Guarded so
# brief.py never fails to load if the module is absent; classify stamps each
# item's verdict and holds injection items out of the actionable lane.
try:
    import quarantine_reader as _quarantine_reader  # noqa: E402
except Exception:  # pragma: no cover - defensive
    _quarantine_reader = None


def _classify_quarantine(item: dict) -> dict:
    """Structural verdict over an inbound item's untrusted text (subject + body).
    Returns {'verdict','reasons'} acting on the enum only; 'clean' when the reader
    is unavailable or the item has no text (fail-open: never blocks a real item on
    an analyzer gap, but see the injection hold applied by the caller)."""
    if _quarantine_reader is None:
        return {"verdict": "clean", "reasons": []}
    try:
        text = "\n".join(
            str(item.get(k) or "")
            for k in ("subject", "body_snippet", "body", "text")
        ).strip()
        if not text:
            return {"verdict": "clean", "reasons": []}
        src = (item.get("source") or "inbound")
        res = _quarantine_reader.read_quarantined(text, purpose=f"inbound_{src}")
        return {"verdict": res.get("verdict", "clean"), "reasons": res.get("reasons", [])[:4]}
    except Exception:
        return {"verdict": "clean", "reasons": []}


BEEPER_BASE = "http://localhost:23373"

# ─── Operator config ─────────────────────────────────────────────────────────
OPERATOR_NAME = str(config.get("operator_name") or "").strip()
OPERATOR_EMAILS = {
    str(e).strip().lower() for e in (config.get("operator_emails") or []) if str(e).strip()
}
ORG_EMAIL_ALIASES = {
    str(e).strip().lower() for e in (config.get("org.email_aliases", []) or []) if str(e).strip()
}
OPERATOR_DISCORD_ID = str(config.get("operator.discord_user_id", "") or "").strip()
KEYRING_SERVICE = str(config.get("keyring_service") or "ops-kit")


def _operator_name_tokens() -> set:
    """Lowercased tokens that identify the operator in free text (name parts +
    display name). Empty until [operator] name is configured."""
    toks = set()
    for part in re.split(r"\s+", OPERATOR_NAME.lower()):
        if part:
            toks.add(part)
    dn = str(config.get("operator.display_name", "") or "").strip().lower()
    if dn:
        toks.add(dn)
    return toks


# ─── Beeper chat registry ────────────────────────────────────────────────────
def _load_beeper_registry() -> dict:
    """Chat registry from config.toml. Empty until the operator registers
    chats. Priority: 1 = always sync, 2 = daily, 3 = weekly/on-demand.

    Example (FICTIONAL ids/names; add one table per chat):

        [beeper.chats."!AbCdEf123:example.local-signal.localhost"]
        name = "Jane Doe (Signal DM)"
        network = "signal"
        priority = 1
    """
    reg = {}
    raw = config.section("beeper.chats")
    for cid, info in (raw or {}).items():
        if not isinstance(info, dict):
            continue
        try:
            pri = int(info.get("priority") or 3)
        except (TypeError, ValueError):
            pri = 3
        reg[str(cid)] = {
            "name": str(info.get("name") or cid),
            "network": str(info.get("network") or "").lower(),
            "priority": pri,
        }
    return reg


BEEPER_CHATS = _load_beeper_registry()


# ─── DB helpers ──────────────────────────────────────────────────────────────

def get_conn(readonly=False):
    if not UNIFIED_DB.exists():
        print(f"ERROR: DB not found at {UNIFIED_DB}", file=sys.stderr)
        sys.exit(1)
    conn = _db.connect(str(UNIFIED_DB), readonly=readonly, row_factory=sqlite3.Row)
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _table_exists(conn, name: str) -> bool:
    """Feature-detect an optional table so the pipeline degrades gracefully
    when a capability's tables are not installed."""
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
            (name,),
        ).fetchone() is not None
    except Exception:
        return False


# ─── Sync: core (gmail + discord + notion via daily_sync.py) ─────────────────

def _sync_timeout(kind="default"):
    """Subprocess timeout for sync legs. Configurable per kind via
    BRIEF_SYNC_TIMEOUT_<KIND> (or BRIEF_SYNC_TIMEOUT for all); discord-bearing
    runs default to 1800s (a fixed short budget kills mid-sweep), everything
    else to 600s."""
    default = 1800 if kind == "discord" else 600
    for key in (f"BRIEF_SYNC_TIMEOUT_{kind.upper()}", "BRIEF_SYNC_TIMEOUT"):
        val = os.environ.get(key)
        if val:
            try:
                return int(val)
            except ValueError:
                pass
    return default


def _record_sync_timing(source, elapsed_ms, rows, ok, run_id=None):
    """Append a sync_source_timings row. Best-effort telemetry: never raises --
    a timing-write failure must not fail the sync itself. This module is the
    table's sole writer, so it owns the DDL and creates it on first use
    (autonomy/daily_digest.py reads it, feature-detected)."""
    try:
        conn = get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_source_timings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id     TEXT,
                source     TEXT NOT NULL,
                elapsed_ms INTEGER,
                rows       INTEGER,
                ok         INTEGER,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
        conn.execute(
            "INSERT INTO sync_source_timings(run_id, source, elapsed_ms, rows, ok) VALUES (?,?,?,?,?)",
            (run_id, source, int(elapsed_ms) if elapsed_ms is not None else None,
             int(rows) if rows is not None else None, 1 if ok else 0))
        conn.commit()
        conn.close()
    except Exception:
        pass


def sync_core(sources=None):
    """Run daily_sync.py for core sources."""
    args = [PYTHON, str(MODULE_DIR / "daily_sync.py")]
    if sources:
        for s in sources:
            args.append(f"--{s}-only")

    print("[sync] Running daily_sync.py...")
    # discord-bearing runs get the long budget (daily_sync commits per channel,
    # so even a timeout keeps partial progress).
    _kind = "discord" if (not sources or "discord" in sources) else "default"
    _t0 = time.perf_counter()
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=_sync_timeout(_kind),
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    # Per-source timing telemetry (one row per sync_core dispatch).
    _record_sync_timing("+".join(sources) if sources else "core",
                        (time.perf_counter() - _t0) * 1000, None, result.returncode == 0)
    for line in result.stdout.strip().splitlines():
        print(f"  {line}")
    if result.returncode != 0 and result.stderr:
        for line in result.stderr.strip().splitlines()[-5:]:
            print(f"  ERROR: {line}", file=sys.stderr)
    return result.returncode == 0


# ─── Sync: beeper (filtered by registry) ─────────────────────────────────────

def _beeper_token():
    """Beeper Desktop access token: env BEEPER_TOKEN, then the configured
    keyring service, then the legacy 'claude-mcp' keyring entry."""
    tok = os.environ.get("BEEPER_TOKEN")
    if tok:
        return tok
    try:
        import keyring as kr
        return (kr.get_password(KEYRING_SERVICE, "BEEPER_TOKEN")
                or kr.get_password("claude-mcp", "BEEPER_TOKEN"))
    except Exception:
        return None


def beeper_get(token, path, params=None):
    url = f"{BEEPER_BASE}{path}"
    if params:
        qs = urllib.parse.urlencode(params)
        url += "?" + qs
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("  Beeper: token expired", file=sys.stderr)
        return None
    except urllib.error.URLError:
        print("  Beeper: Desktop not running", file=sys.stderr)
        return None


def sync_beeper_local_shell(args=None) -> bool:
    """Shell out to sync_beeper_local.py for the full local-DB ingest path.

    sync_beeper_local.py reads Beeper Desktop's local SQLite directly (faster
    and more complete than the REST API). The inline `sync_beeper` below still
    runs first for the registry-priority freshness pass; this wrapper covers
    the long-tail historical sweep and any chats not in the registry.

    History: sync_beeper_local is the canonical full-history source. The
    REST-API inline `sync_beeper` is retained for fast polling against
    priority chats (some chats only update via the API).
    """
    print("\n[sync] Beeper local-DB (full sweep)...")
    cmd = [PYTHON, str(MODULE_DIR / "sync_beeper_local.py")]
    if args:
        cmd.extend(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_sync_timeout("beeper"),
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONUTF8": "1"},
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  Beeper local sweep failed: {e}", file=sys.stderr)
        return False
    for line in result.stdout.strip().splitlines()[-15:]:
        print(f"  {line}")
    if result.returncode != 0 and result.stderr:
        for line in result.stderr.strip().splitlines()[-5:]:
            print(f"  ERROR: {line}", file=sys.stderr)
    return result.returncode == 0


def sync_beeper(max_priority=2):
    """Sync only registered Beeper chats at or above priority threshold via the REST API."""
    print(f"\n[sync] Beeper (priority <= {max_priority}, REST API)...")

    if not BEEPER_CHATS:
        print("  No chats registered ([beeper.chats] in config.toml); skipping REST pass")
        return True  # not configured is a documented skip, not a failure

    token = _beeper_token()
    if not token:
        print("  No BEEPER_TOKEN in env or keyring, skipping")
        return False

    conn = get_conn()

    # Ensure tables exist
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS beeper_chats (
            id TEXT PRIMARY KEY, title TEXT, chat_type TEXT,
            account_id TEXT, network TEXT, last_activity DATETIME,
            unread_count INTEGER DEFAULT 0, is_archived INTEGER DEFAULT 0,
            participants_json TEXT, fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS beeper_messages (
            id TEXT PRIMARY KEY, chat_id TEXT, sender_id TEXT,
            sender_name TEXT, network TEXT, text TEXT,
            timestamp DATETIME, is_outgoing INTEGER DEFAULT 0,
            message_type TEXT, person_id INTEGER,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    chats_to_sync = {
        cid: info for cid, info in BEEPER_CHATS.items()
        if info["priority"] <= max_priority
    }

    total_new = 0
    chats_reached = 0
    for cid, info in chats_to_sync.items():
        data = beeper_get(token, f"/v1/chats/{cid}/messages", {"limit": "50"})
        if not data:
            continue
        chats_reached += 1

        items = data.get("items", data) if isinstance(data, dict) else data
        if not items or not isinstance(items, list):
            continue

        chat_new = 0
        for msg in items:
            mid = msg.get("id", "")
            sender = msg.get("sender", {})
            try:
                cur = conn.execute(
                    "SELECT 1 FROM beeper_messages WHERE id = ?", (mid,)
                )
                if cur.fetchone():
                    continue
                conn.execute("""
                    INSERT INTO beeper_messages
                    (id, chat_id, sender_id, sender_name, network, text, timestamp,
                     is_outgoing, message_type, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    mid, cid, sender.get("id", ""),
                    sender.get("fullName", sender.get("displayText", "")),
                    # lowercase at ingest: mixed-case network values split every
                    # exact-match query in two
                    (info["network"] or "").lower(), msg.get("text", ""), msg.get("timestamp"),
                    1 if sender.get("isSelf") else 0, msg.get("type", "text"),
                ))
                chat_new += 1
            except sqlite3.IntegrityError:
                pass

        if chat_new:
            total_new += chat_new
            print(f"  {info['name']}: {chat_new} new")

    # Only record sync if we actually reached Beeper
    if chats_reached > 0:
        conn.execute("""
            INSERT INTO sync_state (source, last_sync, count)
            VALUES ('beeper_brief', CURRENT_TIMESTAMP, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_sync = CURRENT_TIMESTAMP, count = excluded.count
        """, (total_new,))
    else:
        print("  WARNING: No chats reachable, sync_state not updated")
    conn.commit()
    conn.close()

    print(f"  Beeper: {total_new} new messages from {chats_reached}/{len(chats_to_sync)} chats")
    return chats_reached > 0


# ─── Sync: calendar + gmail labels (optional external Google CLI) ────────────

def _google_cli() -> str:
    """Path to an optional Google Workspace CLI script that supports
    `calendar events --json --days N` and `gmail search <query> --json -n N`.
    Configure via env OPS_GOOGLE_CLI or [sync] google_cli; blank disables
    the calendar and gmail-label legs."""
    return str(os.environ.get("OPS_GOOGLE_CLI")
               or config.get("sync.google_cli", "") or "").strip()


def sync_calendar():
    """Fetch upcoming calendar events via the configured Google CLI and store in DB."""
    print("\n[sync] Calendar (next 3 days)...")
    cli = _google_cli()
    if not cli:
        print("  Calendar: not configured ([sync] google_cli in config.toml); skipping")
        return True  # documented skip, not a failure

    result = subprocess.run(
        [PYTHON, cli, "calendar", "events", "--json", "--days", "3"],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    if result.returncode != 0:
        print("  Calendar: CLI failed", file=sys.stderr)
        if result.stderr:
            for line in result.stderr.strip().splitlines()[-3:]:
                print(f"  ERROR: {line}", file=sys.stderr)
        return False

    try:
        data = json.loads(result.stdout)
        events = data if isinstance(data, list) else data.get("items", data.get("events", []))
    except json.JSONDecodeError:
        print("  Calendar: JSON parse error", file=sys.stderr)
        return False

    conn = get_conn()

    # Create table if needed
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_events (
            id TEXT PRIMARY KEY,
            summary TEXT,
            start_time TEXT,
            end_time TEXT,
            all_day INTEGER DEFAULT 0,
            organizer TEXT,
            attendee_count INTEGER,
            my_response TEXT,
            has_video_link INTEGER DEFAULT 0,
            link TEXT,
            raw_json TEXT,
            fetched_at TEXT DEFAULT (datetime('now'))
        )
    """)

    stored = 0
    for ev in events:
        eid = ev.get("id") or ev.get("link", "")
        if not eid:
            continue
        conn.execute("""
            INSERT OR REPLACE INTO calendar_events
            (id, summary, start_time, end_time, all_day, organizer,
             attendee_count, my_response, has_video_link, link, raw_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            eid, ev.get("summary", ""), ev.get("start", ""), ev.get("end", ""),
            1 if ev.get("all_day") else 0, ev.get("organizer", ""),
            ev.get("attendee_count", 0), ev.get("my_response", ""),
            1 if ev.get("has_video_link") else 0, ev.get("link", ""),
            json.dumps(ev, ensure_ascii=False),
        ))
        stored += 1

    conn.execute("""
        INSERT INTO sync_state (source, last_sync, count)
        VALUES ('calendar', CURRENT_TIMESTAMP, ?)
        ON CONFLICT(source) DO UPDATE SET
            last_sync = CURRENT_TIMESTAMP, count = excluded.count
    """, (stored,))
    conn.commit()
    conn.close()

    print(f"  Calendar: {stored} events stored")
    return True


def sync_gmail_triage_labels():
    """Count recent emails carrying the operator's own pre-triage Gmail labels
    (labels an external email automation applies; configure the list via
    [sync] gmail_triage_labels)."""
    print("\n[sync] Gmail triage labels...")
    cli = _google_cli()
    labels = [str(l) for l in (config.get("sync.gmail_triage_labels", []) or []) if str(l).strip()]
    if not cli or not labels:
        print("  Gmail labels: not configured ([sync] google_cli + gmail_triage_labels); skipping")
        return True

    total = 0
    for label in labels:
        result = subprocess.run(
            [PYTHON, cli, "gmail", "search", f"label:{label} newer_than:3d", "--json", "-n", "20"],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        if result.returncode != 0:
            continue

        try:
            data = json.loads(result.stdout)
            items = data if isinstance(data, list) else data.get("items", data.get("messages", []))
        except json.JSONDecodeError:
            continue

        count = len(items)
        if count:
            print(f"  {label}: {count} emails")
            total += count

    print(f"  Gmail labels: {total} tagged emails found")
    return True


# ─── Sync: Google Chat (optional summary spaces) ─────────────────────────────

def sync_google_chat():
    """Fetch messages from Google Chat spaces (e.g. where an external email
    automation pushes summaries). Configure the space display names via
    [sync] google_chat_spaces.

    Requires Google Chat API scope (chat.messages.readonly) on an OAuth token:
      1. pip install google-api-python-client google-auth
      2. Enable Google Chat API in Google Cloud Console
      3. Add scope chat.messages.readonly to the OAuth consent screen
      4. Save the authorized-user token JSON at the configured token path
    """
    print("\n[sync] Google Chat...")

    space_names = [str(s) for s in (config.get("sync.google_chat_spaces", []) or []) if str(s).strip()]
    if not space_names:
        print("  Google Chat: not configured ([sync] google_chat_spaces); skipping")
        return True

    token_path = str(config.get("sync.google_chat_token_path", "") or "").strip() \
        or "~/.config/ops-kit/google_chat_token.json"
    chat_token_path = Path(os.path.expanduser(token_path))
    if not chat_token_path.exists():
        print(f"  Google Chat: no token at {chat_token_path}; see sync_google_chat() docstring")
        return False

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(chat_token_path),
            ["https://www.googleapis.com/auth/chat.messages.readonly",
             "https://www.googleapis.com/auth/chat.spaces.readonly"])

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            chat_token_path.write_text(creds.to_json())

        service = build("chat", "v1", credentials=creds)

        # List spaces to find the configured ones
        wanted = set(space_names)
        spaces = service.spaces().list(filter='spaceType = "SPACE"').execute()
        space_map = {}
        for sp in spaces.get("spaces", []):
            name = sp.get("displayName", "")
            if name in wanted:
                space_map[name] = sp["name"]  # e.g., "spaces/AAAA..."

        if not space_map:
            print("  No configured spaces found. Available spaces:")
            for sp in spaces.get("spaces", [])[:10]:
                print(f"    {sp.get('displayName', '?')}")
            return False

        conn = get_conn()

        # Create table if needed
        conn.execute("""
            CREATE TABLE IF NOT EXISTS google_chat_messages (
                id TEXT PRIMARY KEY,
                space_name TEXT,
                space_display TEXT,
                sender TEXT,
                text TEXT,
                timestamp TEXT,
                fetched_at TEXT DEFAULT (datetime('now'))
            )
        """)

        total = 0
        for display_name, space_id in space_map.items():
            messages = service.spaces().messages().list(
                parent=space_id, pageSize=25,
                orderBy="createTime desc"
            ).execute()

            for msg in messages.get("messages", []):
                mid = msg.get("name", "")
                conn.execute("""
                    INSERT OR IGNORE INTO google_chat_messages
                    (id, space_name, space_display, sender, text, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    mid, space_id, display_name,
                    msg.get("sender", {}).get("displayName", ""),
                    msg.get("text", ""),
                    msg.get("createTime", ""),
                ))
                total += 1

            print(f"  {display_name}: {len(messages.get('messages', []))} messages")

        conn.execute("""
            INSERT INTO sync_state (source, last_sync, count)
            VALUES ('google_chat', CURRENT_TIMESTAMP, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_sync = CURRENT_TIMESTAMP, count = excluded.count
        """, (total,))
        conn.commit()
        conn.close()
        return True

    except ImportError:
        print("  pip install google-api-python-client google-auth")
        return False
    except Exception as e:
        print(f"  Google Chat error: {e}", file=sys.stderr)
        return False


# ─── Sync: email tracker ────────────────────────────────────────────────────

def sync_email_tracker():
    """Refresh inbound/outbound timestamps on linked action items."""
    print("\n[sync] email_tracker sync...")
    tracker = COMMS_DIR / "email_tracker.py"
    if not tracker.exists():
        print("  email_tracker.py not installed (comms capability); skipping")
        return True
    result = subprocess.run(
        [PYTHON, str(tracker), "sync"],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    for line in result.stdout.strip().splitlines():
        print(f"  {line}")
    if result.returncode != 0 and result.stderr:
        for line in result.stderr.strip().splitlines()[-5:]:
            print(f"  ERROR: {line}", file=sys.stderr)
    return result.returncode == 0


# ═══════════════════════════════════════════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

def sync_granola():
    """Pull new Granola meetings via the REST API (api.granola.ai), store as
    reference_docs, and stamp `url = granola:<uuid>` for dedup. Auth lives in
    the Granola desktop app's stored-accounts.json and is auto-refreshed.

    If the OAuth token has been revoked or the desktop app is signed out,
    fetch silently fails and we fall back to reporting freshness only.
    Open Granola desktop to refresh."""
    print("\n[sync] Granola (transcripts)...")
    new_count = 0
    try:
        import granola_sync  # sibling module (brief/granola_sync.py)
        counts = granola_sync.sync_all(verbose=True)
        new_count = counts.get("new", 0)
    except Exception as e:
        print(f"  granola fetch failed: {e}")
        print("  (Open Granola desktop briefly to refresh OAuth, or run `granola_sync.py token` to debug.)")

    conn = get_conn()
    row = conn.execute("""
        SELECT COUNT(*) AS n, MAX(updated_at) AS latest
        FROM reference_docs
        WHERE slug LIKE 'transcript-%'
    """).fetchone()
    n = row["n"] or 0
    latest = row["latest"] or ""
    stale = False
    if latest:
        try:
            latest_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
            age_h = (datetime.now() - latest_dt.replace(tzinfo=None)).total_seconds() / 3600
            stale = age_h > 48
            print(f"  Stored transcripts: {n} (latest {latest}, {age_h:.1f}h old, +{new_count} this run)")
        except Exception:
            print(f"  Stored transcripts: {n} (latest {latest}, +{new_count} this run)")
    else:
        print(f"  Stored transcripts: {n} (no latest timestamp)")

    conn.execute("""
        INSERT INTO sync_state (source, last_sync, count)
        VALUES ('granola', CURRENT_TIMESTAMP, ?)
        ON CONFLICT(source) DO UPDATE SET last_sync = CURRENT_TIMESTAMP, count = excluded.count
    """, (n,))
    conn.commit()
    conn.close()
    return not stale


# ─── Extraction stubs ────────────────────────────────────────────────────────
# The grounded claim-extraction layer (thread/chat/transcript extractors, the
# promotion gate, and the claim-review judge) is NOT included in this starter
# kit. These stubs keep the sync pipeline's shape (and CLI flags) intact while
# logging clearly that the step was skipped, so nothing pretends to succeed.

def _extraction_stub(step: str) -> bool:
    print(f"[sync] {step}: extraction layer not included in this starter kit; skipping.")
    return True


def sync_thread_extractions(**_kwargs):
    """Stub: grounded email-thread extraction is part of the extraction layer."""
    return _extraction_stub("thread extraction")


def sync_beeper_extractions(**_kwargs):
    """Stub: grounded chat extraction is part of the extraction layer."""
    return _extraction_stub("beeper chat extraction")


def sync_discord_extractions(**_kwargs):
    """Stub: grounded Discord-thread extraction is part of the extraction layer."""
    return _extraction_stub("discord thread extraction")


def sync_granola_extractions(**_kwargs):
    """Stub: grounded transcript extraction is part of the extraction layer."""
    return _extraction_stub("transcript extraction")


def sync_fact_check(**_kwargs):
    """Stub: the web fact-check pass is part of the extraction layer."""
    return _extraction_stub("fact-check")


def _spawn_background_slow(args):
    """Start brief.py sync-slow as a detached process and return immediately.
    Log goes to <data>/logs/brief-slow-<ts>.log so you can tail it.
    """
    logs_dir = Path(str(paths.DATA_DIR)) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir / f"brief-slow-{ts}.log"

    cmd = [
        PYTHON, str(MODULE_DIR / "brief.py"), "sync-slow",
        "--beeper-priority", str(args.beeper_priority),
    ]

    log_file = open(log_path, "w", encoding="utf-8")
    creationflags = 0
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    p = subprocess.Popen(
        cmd, stdout=log_file, stderr=subprocess.STDOUT,
        creationflags=creationflags, close_fds=True, env=env,
    )
    print(f"\n[sync] Background slow-sync started (PID {p.pid}, log: {log_path})")
    return p.pid


def cmd_sync_slow(args):
    """Run everything EXCEPT gmail. Intended as the detached background half
    of the two-phase sync."""
    print(f"[sync-slow] starting at {datetime.now().isoformat()}")
    start = time.time()
    failures = []

    # Non-gmail core syncs. Separate sync_core calls: --notion-only takes
    # daily_sync's early-return path, so combining the flags into one
    # invocation would skip discord.
    try:
        if not sync_core(["discord"]):
            failures.append("core")
    except Exception as e:
        print(f"  core failed: {e}"); failures.append("core")
    try:
        if not sync_core(["notion"]):
            failures.append("notion")
    except Exception as e:
        print(f"  notion failed: {e}"); failures.append("notion")

    # Beeper chat sync (REST API for priority chats + full local-DB sweep)
    try:
        if not sync_beeper(max_priority=args.beeper_priority):
            failures.append("beeper")
    except Exception as e:
        print(f"  beeper failed: {e}"); failures.append("beeper")
    try:
        if not sync_beeper_local_shell():
            failures.append("beeper_local")
    except Exception as e:
        print(f"  beeper_local failed: {e}"); failures.append("beeper_local")

    # Email tracker
    try:
        if not sync_email_tracker():
            failures.append("email_tracker")
    except Exception as e:
        print(f"  email_tracker failed: {e}"); failures.append("email_tracker")

    # Calendar + Google Chat + Gmail labels + Granola
    for fn, name in (
        (sync_calendar, "calendar"),
        (sync_google_chat, "google_chat"),
        (sync_gmail_triage_labels, "gmail_labels"),
        (sync_granola, "granola"),
    ):
        try:
            fn()
        except Exception as e:
            print(f"  {name} failed: {e}"); failures.append(name)

    # Extraction backfill + promotion gate: stubbed (extraction layer not
    # included in this starter kit). The stubs log and skip.
    sync_thread_extractions()
    sync_beeper_extractions()
    sync_discord_extractions()
    sync_granola_extractions()
    _extraction_stub("promotion gate")
    if getattr(args, "with_fact_check", False):
        sync_fact_check()

    # Auto-decay aged-out inbox proposals (>30 days pending with no triage)
    try:
        conn = get_conn()
        n_aged = conn.execute("""
            UPDATE action_items_inbox
               SET status='rejected',
                   rejection_reason='auto-aged-out: >30 days pending with no triage',
                   reviewed_at=CURRENT_TIMESTAMP, reviewed_by='auto-decay'
             WHERE status='pending' AND proposed_at < datetime('now','-30 days')
        """).rowcount
        conn.commit()
        conn.close()
        if n_aged:
            print(f"[sync-slow] Auto-rejected {n_aged} aged-out inbox proposals (>30d)")
    except Exception as e:
        print(f"[sync-slow] auto-decay failed: {e}")

    elapsed = time.time() - start
    print(f"[sync-slow] done in {elapsed:.0f}s. failures={failures}")
    return 0 if not failures else 1


def _incremental_embed_and_freshness():
    """Embed the delta for each vec table (incremental = new rows only) and refresh the
    vec_freshness ledger. Capped per table so a sync never stalls."""
    import subprocess as _sp
    here = str(SEARCH_DIR)
    pyexe = sys.executable
    # embed_learnings.py supports --incremental (re-embed rows changed since last embed,
    # not just brand-new ones) so an edited learning's vector tracks its text.
    extra_args = {"embed_learnings.py": ["--incremental"]}
    for script in ("embed_observations.py", "embed_episodes.py", "embed_emails.py", "embed_entities.py",
                   "embed_people.py", "embed_learnings.py",
                   "embed_reference_doc_chunks.py", "embed_action_items.py"):
        path = os.path.join(here, script)
        if not os.path.exists(path):
            continue
        try:
            _sp.run([pyexe, path] + extra_args.get(script, []), cwd=here, timeout=240, capture_output=True)
        except Exception:
            pass  # best-effort; a slow/locked embed must not break the sync
    # refresh vec_freshness ledger
    refresh_vec_freshness()


# Embedder scope predicate per source table. rows_pending is a true anti-join
# WITHIN the embedder's scope -- raw-minus-embedded lies in both directions
# (ghost vec rows mask missing source rows; archived faqs / inactive learnings
# count as forever-pending).
_VEC_SCOPES = {
    "observations": ("vec_observations", "1=1", "id"),
    "episodes": ("vec_episodes", "(topic IS NOT NULL OR summary IS NOT NULL)", "id"),
    "emails": ("vec_emails", "subject IS NOT NULL AND subject != ''", "id"),
    "entities": ("vec_entities", "1=1", "rowid"),
    "people": ("vec_people", "1=1", "id"),
    "learnings": ("vec_learnings", "status='active' AND title IS NOT NULL AND title != ''", "id"),
    # faqs: embedder is APPROVED-ONLY (vec_faqs mirrors status='approved'
    # exactly). A wider scope counts proposed/draft as forever-pending, so the
    # ledger would report a permanent phantom backlog. Match the embedder scope
    # so pending is honest.
    "faqs": ("vec_faqs", "status = 'approved'", "id"),
    # The system's own docs + operational backlog join the fabric.
    "reference_doc_chunks": ("vec_reference_doc_chunks", "content IS NOT NULL AND content != ''", "id"),
    "action_items": ("vec_action_items", "description IS NOT NULL AND description != ''", "id"),
}


def refresh_vec_freshness():
    """Scope-aware vec_freshness refresh. vec_* tables live in vec.db and need
    the sqlite_vec extension; this attaches vec.db, loads vec0, and computes
    pending as an anti-join inside each embedder's scope predicate."""
    try:
        import sqlite_vec
        conn = get_conn()
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("ATTACH DATABASE '%s' AS vecdb" % paths.VEC_DB_PATH)
        conn.execute("""CREATE TABLE IF NOT EXISTS vec_freshness (
            table_name TEXT PRIMARY KEY, last_embedded_at DATETIME, rows_embedded INTEGER,
            rows_pending INTEGER, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        for tbl, (vec, scope, pk) in _VEC_SCOPES.items():
            try:
                emb = conn.execute(f"SELECT COUNT(*) FROM vecdb.{vec}").fetchone()[0]
                pending = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE {scope} "
                    f"AND {pk} NOT IN (SELECT rowid FROM vecdb.{vec})").fetchone()[0]
                conn.execute(
                    "INSERT INTO vec_freshness(table_name,last_embedded_at,rows_embedded,rows_pending,updated_at) "
                    "VALUES(?,datetime('now'),?,?,datetime('now')) "
                    "ON CONFLICT(table_name) DO UPDATE SET last_embedded_at=datetime('now'), "
                    "rows_embedded=excluded.rows_embedded, rows_pending=excluded.rows_pending, updated_at=datetime('now')",
                    (tbl, emb, pending))
            except Exception as e:
                print(f"  vec_freshness {tbl} skipped: {e}")
        conn.commit(); conn.close()
    except Exception as e:
        print(f"  vec_freshness refresh failed: {e}")


def cmd_sync(args):
    """Run data syncs.

    Default behavior: TWO-PHASE.
      Phase 1 (foreground, fast): gmail sync + inbox triage. Target <5 min so
         the operator can start working.
      Phase 2 (background, detached): everything else -- beeper, calendar,
         Google Chat, granola. Runs as a detached subprocess; log in
         <data>/logs/.

    Flags:
      --full           : run everything inline, no spawn (legacy behavior)
      --no-background  : fast only, skip background phase
    """
    # Legacy path: inline full sync (no background).
    if args.full:
        return _cmd_sync_full_inline(args)

    try:
        from sync_summaries import SyncSummary  # noqa: PLC0415
    except Exception:
        SyncSummary = None  # type: ignore[assignment]

    start = time.time()
    failures = []
    ss = SyncSummary(notes="fast-sync").__enter__() if SyncSummary else None

    print(f"[sync] Phase 1 (foreground): gmail")

    # Phase 1: Gmail only
    if not sync_core(["gmail"]):
        failures.append("core-gmail")

    # Phase 1: grounded extraction of newly-ingested threads is stubbed
    # (extraction layer not included in this starter kit).
    if not args.skip_extractions:
        sync_thread_extractions()

    # Inbox triage: classify new threads + reconcile false-positive open AIs.
    # Cheap (touches only delta threads). Idempotent.
    try:
        import inbox_triage  # noqa: PLC0415
        res = inbox_triage.classify_new(verbose=True)
        print(f"  inbox_triage: {res}")
        # Post-hoc auto-close (replied flip + participant stale).
        try:
            stale_res = inbox_triage.auto_close_stale(dry_run=False)
            print(f"  inbox_triage auto-close-stale: {stale_res}")
        except Exception as e:
            print(f"  inbox_triage auto-close-stale failed: {e}")
            failures.append("inbox_triage_auto_close_stale")
        # Chat-draft resolution: flip chat drafts we've since answered.
        try:
            import comms_monitor  # noqa: PLC0415
            n = comms_monitor.reconcile_chat_replies(dry_run=False)
            print(f"  reconcile_chat_replies: {n} chat drafts superseded")
            # Learn-from-edits: now that fresh SENT mail has landed, diff each new
            # draft against the reply it answered and record a comms_draft_outcomes row.
            oc = comms_monitor.capture_new_outcomes()
            print(f"  capture_new_outcomes: +{oc['inserted']} outcomes {oc['by_bucket']}")
        except Exception as e:
            print(f"  reconcile_chat_replies failed: {e}")
            failures.append("reconcile_chat_replies")
    except Exception as e:
        print(f"  inbox_triage failed: {e}")
        failures.append("inbox_triage")

    # Record fast-sync completion
    conn = get_conn()
    conn.execute("""
        INSERT INTO sync_state (source, last_sync, count)
        VALUES ('brief_sync_fast', CURRENT_TIMESTAMP, 0)
        ON CONFLICT(source) DO UPDATE SET last_sync = CURRENT_TIMESTAMP
    """)
    conn.commit()
    conn.close()

    # Incremental embed: embed only the delta so semantic search stays fresh
    # and episodes never go STALE. Best-effort + capped; never fails the sync.
    try:
        _incremental_embed_and_freshness()
    except Exception as e:
        print(f"  incremental embed failed (non-fatal): {e}")

    # Drift check on every fast sync: if the detector only runs in the
    # full-sync path it goes silent for weeks once fast sync becomes the
    # default. Mirrors the call in _cmd_sync_full_inline; best-effort.
    if ss is not None:
        try:
            from drift_check import run_checks as _drift  # noqa: PLC0415
            dr = _drift()
            if dr.get("fired"):
                ss.add_note("drift fired: " + ",".join(dr["fired"]))
            if dr.get("resolved"):
                ss.add_note("drift resolved: " + ",".join(dr["resolved"]))
        except Exception as e:
            ss.add_note(f"drift: error {e!s:.120}")

    elapsed = time.time() - start
    print(f"\n[sync] Phase 1 done in {elapsed:.0f}s. failures={failures}")

    # Inbox triage nudge: only show when there are recent proposals to look at.
    try:
        conn = get_conn()
        new_recent = conn.execute(
            "SELECT COUNT(*) FROM action_items_inbox "
            "WHERE status='pending' AND proposed_at >= datetime('now','-3 days')"
        ).fetchone()[0]
        total_pending = conn.execute(
            "SELECT COUNT(*) FROM action_items_inbox WHERE status='pending'"
        ).fetchone()[0]
        conn.close()
        if new_recent > 0:
            print(f"[sync] >> {new_recent} new inbox proposals (3-day window); {total_pending} total pending.")
            print(f"[sync] >>    invoke skill `triage-inbox` or run `task_manager.py inbox` to review.")
    except Exception:
        pass  # nudge is best-effort

    if ss:
        ss.__exit__(None, None, None)

    # Phase 2: spawn detached background for everything else
    if not args.no_background:
        _spawn_background_slow(args)
    else:
        print("[sync] --no-background set; skipping Phase 2 (run `brief.py sync-slow` when ready)")

    return 0 if not failures else 1


def _cmd_sync_full_inline(args):
    """Legacy inline full sync (what `--full` gives you)."""
    try:
        from sync_summaries import SyncSummary  # noqa: PLC0415
    except Exception:
        SyncSummary = None  # type: ignore[assignment]

    start = time.time()
    failures = []
    notes_init = "skip-flags=" + ",".join(
        n for n, v in [
            ("gmail", args.gmail), ("discord", args.discord), ("notion", args.notion),
            ("beeper", args.beeper), ("email_tracker", args.email_tracker),
            ("calendar", args.calendar), ("granola", args.granola),
        ] if v
    ) if any([args.gmail, args.discord, args.notion, args.beeper,
              args.email_tracker, args.calendar, args.granola]) else "full"
    ss = SyncSummary(notes=notes_init).__enter__() if SyncSummary else None

    # --beeper-priority implies --beeper
    if args.beeper_priority != 2:
        args.beeper = True
    any_flag = args.gmail or args.discord or args.notion or args.beeper or args.email_tracker or args.calendar or args.granola
    do_all = not any_flag

    if do_all:
        if not sync_core():
            failures.append("core")
    else:
        core = []
        if args.gmail:
            core.append("gmail")
        if args.discord:
            core.append("discord")
        if args.notion:
            core.append("notion")
        if core:
            if not sync_core(core):
                failures.append("core")

    if do_all or args.beeper:
        if not sync_beeper(max_priority=args.beeper_priority):
            failures.append("beeper")
        if not sync_beeper_local_shell():
            failures.append("beeper_local")

    if do_all or args.email_tracker:
        if not sync_email_tracker():
            failures.append("email_tracker")

    # Calendar (always on full sync)
    if do_all or args.calendar:
        if not sync_calendar():
            failures.append("calendar")

    # Google Chat (always on full sync)
    if do_all:
        sync_google_chat()

    # Gmail triage labels (always on full sync)
    if do_all:
        sync_gmail_triage_labels()

    # Granola transcript freshness (always on full sync, or --granola)
    if do_all or args.granola:
        sync_granola()

    # Extraction stages: stubbed (extraction layer not included).
    if (do_all or args.extractions) and not args.skip_extractions:
        sync_thread_extractions()
        sync_beeper_extractions()
        if getattr(args, "with_fact_check", False):
            sync_fact_check()

    # Record sync completion (with failure info)
    conn = get_conn()
    conn.execute("""
        INSERT INTO sync_state (source, last_sync, count)
        VALUES ('brief_sync', CURRENT_TIMESTAMP, 0)
        ON CONFLICT(source) DO UPDATE SET last_sync = CURRENT_TIMESTAMP
    """)
    conn.commit()
    conn.close()

    # Drift check (best-effort; sync still succeeds if it fails).
    if ss is not None:
        for f in failures:
            ss.add_failure(f)
        try:
            from drift_check import run_checks as _drift  # noqa: PLC0415
            dr = _drift()
            if dr.get("fired"):
                ss.add_note("drift fired: " + ",".join(dr["fired"]))
            if dr.get("resolved"):
                ss.add_note("drift resolved: " + ",".join(dr["resolved"]))
        except Exception as e:
            ss.add_note(f"drift: error {e!s:.120}")
        # NOTE: the periodic claim re-audit that ran here belongs to the
        # extraction layer, which is not included in this starter kit.
        ss.__exit__(None, None, None)

    elapsed = time.time() - start
    if failures:
        print(f"\n[sync] Done in {elapsed:.1f}s (WARNINGS: {', '.join(failures)} had errors)")
    else:
        print(f"\n[sync] Done in {elapsed:.1f}s")


def cmd_gather(args):
    """Collect new items since last brief and output for classification."""
    conn = get_conn(readonly=True)

    if args.since:
        since = args.since
    else:
        last_brief = conn.execute(
            "SELECT MAX(brief_date) FROM briefing_reports"
        ).fetchone()[0]
        since = last_brief or (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d")

    # Already-classified source IDs (scoped to recent briefs for performance)
    already_ids = set()   # (source, source_id) pairs
    already_threads = set()  # gmail thread_ids
    for row in conn.execute("""
        SELECT source, source_id, subject FROM classification_results
        WHERE created_at > datetime('now', '-14 days')
    """).fetchall():
        already_ids.add((row["source"], row["source_id"]))
    # Also collect classified thread_ids from email items
    for row in conn.execute("""
        SELECT cr.source_id, e.thread_id FROM classification_results cr
        JOIN emails e ON e.gmail_message_id = cr.source_id
        WHERE cr.source = 'gmail' AND cr.created_at > datetime('now', '-14 days')
          AND e.thread_id IS NOT NULL
    """).fetchall():
        already_threads.add(row["thread_id"])

    items = []

    # v2 routing: filter personal/family-domain emails out of the brief unless
    # --include-personal flag is set. NULL/general/work/public always pass.
    routing_v2 = os.environ.get("ROUTING_V2", "0") == "1"
    include_personal = getattr(args, "include_personal", False)
    domain_filter_sql = ""
    if routing_v2 and not include_personal:
        domain_filter_sql = " AND COALESCE(e.domain,'general') IN ('work','public','general')"

    # ── Emails ────────────────────────────────────────────────────────────
    emails = conn.execute(f"""
        SELECT e.gmail_message_id, e.thread_id, e.sender_name, e.sender_email,
               e.subject, SUBSTR(e.body, 1, 500) as snippet, e.timestamp,
               e.is_outgoing, e.labels, e.person_id,
               p.name as person_name
        FROM emails e
        LEFT JOIN people p ON e.person_id = p.id
        WHERE e.timestamp > ?{domain_filter_sql}
        ORDER BY e.timestamp DESC
    """, (since,)).fetchall()

    # Group by thread, keep newest per thread
    threads = {}
    for e in emails:
        tid = e["thread_id"] or e["gmail_message_id"]
        if tid not in threads:
            threads[tid] = dict(e)

    for tid, latest in threads.items():
        # Skip if any message in this thread was already classified
        if tid in already_threads or ("gmail", latest["gmail_message_id"]) in already_ids:
            continue

        # Thread context: last 3 messages for the classifier
        # Use thread_id if real, fall back to message_id match
        if latest["thread_id"]:
            ctx_rows = conn.execute("""
                SELECT sender_name, sender_email, SUBSTR(body, 1, 300) as snippet,
                       timestamp, is_outgoing
                FROM emails WHERE thread_id = ?
                ORDER BY timestamp DESC LIMIT 3
            """, (tid,)).fetchall()
        else:
            ctx_rows = conn.execute("""
                SELECT sender_name, sender_email, SUBSTR(body, 1, 300) as snippet,
                       timestamp, is_outgoing
                FROM emails WHERE gmail_message_id = ?
            """, (latest["gmail_message_id"],)).fetchall()
        thread_ctx = [
            {
                "from": r["sender_name"] or r["sender_email"] or "",
                "is_us": bool(r["is_outgoing"]),
                "snippet": (r["snippet"] or "")[:200],
                "when": r["timestamp"],
            }
            for r in reversed(list(ctx_rows))
        ]

        items.append({
            "source": "gmail",
            "source_id": latest["gmail_message_id"],
            "thread_id": tid,
            "sender": latest["sender_name"] or "",
            "sender_email": latest["sender_email"] or "",
            "subject": latest["subject"] or "",
            "body_snippet": (latest["snippet"] or "")[:400],
            "received_at": latest["timestamp"],
            "is_outgoing": bool(latest["is_outgoing"]),
            "person_id": latest["person_id"],
            "person_name": latest["person_name"],
            "labels": latest["labels"] or "",
            "thread_context": thread_ctx,
        })

    # ── Discord ───────────────────────────────────────────────────────────
    # Split into two queries so DMs don't get crowded out by high-volume guild
    # channels under one shared LIMIT. Each gets its own headroom.
    # The operator's own discord user id: filter out self-authored messages so
    # the classifier only sees inbound (matches the Beeper is_outgoing=0
    # filter). When unconfigured, the sentinel never matches (no self-filter).
    operator_discord_id = OPERATOR_DISCORD_ID or "__unset__"

    discord_guild_msgs = conn.execute("""
        SELECT dm.id, dm.content, dm.timestamp, dm.author_id,
               du.username, dc.name as channel_name, dc.channel_type,
               dg.name as guild_name, dc.context_slug
        FROM discord_messages dm
        LEFT JOIN discord_users du ON dm.author_id = du.id
        LEFT JOIN discord_channels dc ON dm.channel_id = dc.id
        LEFT JOIN discord_guilds dg ON dc.guild_id = dg.id
        WHERE dm.timestamp > ?
          AND (dc.channel_type IS NULL OR dc.channel_type='guild')
          AND dm.author_id != ?
        ORDER BY dm.timestamp DESC
        LIMIT 200
    """, (since, operator_discord_id)).fetchall()

    discord_dm_msgs = conn.execute("""
        SELECT dm.id, dm.content, dm.timestamp, dm.author_id,
               du.username, dc.name as channel_name, dc.channel_type,
               dc.dm_recipient_username, dc.dm_recipient_person_id,
               dc.group_dm_recipient_ids, p.name as recipient_name
        FROM discord_messages dm
        LEFT JOIN discord_users du ON dm.author_id = du.id
        LEFT JOIN discord_channels dc ON dm.channel_id = dc.id
        LEFT JOIN people p ON p.id = dc.dm_recipient_person_id
        WHERE dm.timestamp > ?
          AND dc.channel_type IN ('dm','group_dm')
          AND dm.author_id != ?
        ORDER BY dm.timestamp DESC
        LIMIT 100
    """, (since, operator_discord_id)).fetchall()

    for msg in discord_guild_msgs:
        if ("discord", msg["id"]) in already_ids:
            continue
        items.append({
            "source": "discord",
            "source_id": msg["id"],
            "channel_type": "guild",
            "sender": msg["username"] or "",
            "channel": msg["channel_name"] or "",
            "guild": msg["guild_name"] or "",
            "context_slug": msg["context_slug"],
            "body_snippet": (msg["content"] or "")[:400],
            "received_at": msg["timestamp"],
        })

    for msg in discord_dm_msgs:
        if ("discord", msg["id"]) in already_ids:
            continue
        # Group DM channels carry no single dm_recipient_username; fall back to
        # the channel name which sync_discord_dms.py sets to "Group: a, b, c".
        if msg["channel_type"] == "group_dm":
            dm_label = msg["channel_name"] or "group_dm"
            person_id = None
        else:
            dm_label = msg["dm_recipient_username"] or msg["channel_name"] or "dm"
            person_id = msg["dm_recipient_person_id"]
        items.append({
            "source": "discord",
            "source_id": msg["id"],
            "channel_type": msg["channel_type"],
            "sender": msg["username"] or "",
            "channel": msg["channel_name"] or "",
            "guild": None,
            "dm_with": dm_label,
            "dm_with_person_id": person_id,
            "dm_with_person_name": msg["recipient_name"],
            "body_snippet": (msg["content"] or "")[:400],
            "received_at": msg["timestamp"],
        })

    # ── Beeper (Signal + Slack + ...) ─────────────────────────────────────
    beeper_msgs = conn.execute("""
        SELECT bm.id, bm.chat_id, bm.sender_name, bm.network,
               bm.text, bm.timestamp, bm.is_outgoing,
               bc.title as chat_name
        FROM beeper_messages bm
        LEFT JOIN beeper_chats bc ON bm.chat_id = bc.id
        WHERE bm.timestamp > ? AND bm.is_outgoing = 0
        ORDER BY bm.timestamp DESC
        LIMIT 200
    """, (since,)).fetchall()

    for msg in beeper_msgs:
        src = f"beeper_{msg['network'] or 'unknown'}"
        if (src, msg["id"]) in already_ids:
            continue
        chat_info = BEEPER_CHATS.get(msg["chat_id"], {})
        items.append({
            "source": src,
            "source_id": msg["id"],
            "sender": msg["sender_name"] or "",
            "chat_name": chat_info.get("name") or msg["chat_name"] or "",
            "body_snippet": (msg["text"] or "")[:400],
            "received_at": msg["timestamp"],
            "network": msg["network"] or "",
        })

    # ── Few-shot correction examples (for --json output) ───────────────
    few_shot_examples = []
    if args.json:
        corrections = conn.execute("""
            SELECT cc.original_category, cc.corrected_category,
                   cc.original_priority, cc.corrected_priority,
                   cr.source, cr.sender, cr.subject, cr.body_snippet,
                   cr.summary
            FROM classification_corrections cc
            JOIN classification_results cr ON cc.classification_id = cr.id
            ORDER BY cc.corrected_at DESC
            LIMIT 20
        """).fetchall()
        for c in corrections:
            example = {
                "source": c["source"],
                "sender": c["sender"],
                "subject": c["subject"],
                "body_snippet": (c["body_snippet"] or "")[:200],
                "summary": c["summary"],
            }
            if c["original_category"] != c["corrected_category"]:
                example["wrong_category"] = c["original_category"]
                example["correct_category"] = c["corrected_category"]
            if c["original_priority"] != c["corrected_priority"]:
                example["wrong_priority"] = c["original_priority"]
                example["correct_priority"] = c["corrected_priority"]
            few_shot_examples.append(example)

    conn.close()

    # ── Output ────────────────────────────────────────────────────────────
    counts = {}
    for item in items:
        src = item["source"]
        counts[src] = counts.get(src, 0) + 1

    output = {
        "since": since,
        "gathered_at": datetime.now().isoformat(),
        "counts": counts,
        "total": len(items),
        "items": items,
    }

    if few_shot_examples:
        output["few_shot_examples"] = few_shot_examples
        output["few_shot_note"] = (
            "These are past classification corrections. Use them to calibrate: "
            "when you see similar items, prefer the corrected category/priority."
        )

    if args.json:
        json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(f"\n=== Gathered Items (since {since}) ===")
        for src, cnt in sorted(counts.items()):
            print(f"  {src}: {cnt}")
        print(f"  TOTAL: {len(items)}")

        if items:
            print()
            for i, item in enumerate(items[:30], 1):
                src = item["source"]
                sender = item.get("sender") or item.get("sender_email") or "?"
                label = item.get("subject") or item.get("chat_name") or item.get("channel") or ""
                ts = (item.get("received_at") or "")[:16]
                direction = " OUT" if item.get("is_outgoing") else "  IN" if item.get("source") == "gmail" else "    "
                print(f"  {i:2d}. [{src:15s}] {ts} |{direction}| {sender[:20]:20s} | {label[:50]}")
            if len(items) > 30:
                print(f"  ... and {len(items) - 30} more")


def cmd_apply(args):
    """Read classification JSON from stdin, write to DB, update action items."""
    brief_id = args.brief_id

    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"ERROR: Invalid JSON on stdin: {e}", file=sys.stderr)
        sys.exit(1)

    items = data if isinstance(data, list) else data.get("classifications", [])
    if not items:
        print("No classification items to apply.")
        return

    conn = get_conn()
    counts = {"updated": 0, "created": 0, "flagged_close": 0, "noise": 0, "rejected": 0}
    ensure_table(conn)

    try:
        for item in items:
            conn.execute("""
                INSERT OR IGNORE INTO classification_results
                (brief_id, source, source_id, sender, sender_email, subject,
                 received_at, body_snippet, actionable, needs_reply,
                 matched_action_item, matched_confidence, project, priority,
                 category, deadline_detected, summary, reasoning)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                brief_id,
                item.get("source", ""), item.get("source_id", ""),
                item.get("sender", ""), item.get("sender_email", ""),
                item.get("subject", ""), item.get("received_at", ""),
                item.get("body_snippet", ""),
                1 if item.get("actionable") else 0,
                1 if item.get("needs_reply") else 0,
                item.get("matched_action_item"),
                item.get("matched_confidence"),
                item.get("project"), item.get("priority"),
                item.get("category", "noise"),
                item.get("deadline_detected"),
                item.get("summary", ""), item.get("reasoning", ""),
            ))

            cat = item.get("category", "noise")
            matched = item.get("matched_action_item")

            if cat == "noise" or not item.get("actionable"):
                counts["noise"] += 1
                continue

            today = datetime.now().strftime("%Y-%m-%d")

            if matched and cat in ("update_only", "needs_reply"):
                note = f"[Brief {today}] {item.get('summary', '')}"
                conn.execute("""
                    UPDATE action_items SET
                        context = CASE
                            WHEN context IS NULL OR context = '' THEN ?
                            ELSE context || char(10) || ?
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE item_id = ?
                """, (note, note, matched))

                if cat == "needs_reply":
                    conn.execute("""
                        UPDATE action_items SET
                            context_tags = CASE
                                WHEN context_tags IS NULL OR context_tags = '' THEN '@email'
                                WHEN context_tags NOT LIKE '%@email%' THEN context_tags || ',@email'
                                ELSE context_tags
                            END
                        WHERE item_id = ?
                    """, (matched,))

                counts["updated"] += 1

            elif cat == "new_action" and not matched:
                src_id = item.get("source_id", "")
                src_kind = item.get("source", "")
                # Stamp the model into the source tag so the trust gate + downstream
                # queries can distinguish which classifier generated the proposal.
                # Mirrored into extracted_by below for fast SQL filtering.
                model_label = (getattr(args, "model", None) or "gemini").strip().lower()
                source_tag = f"brief.classify.{model_label}"  # e.g. brief.classify.gemini
                extracted_by_label = f"brief-classify-{model_label}"

                # Description for validation
                desc = item.get("summary") or item.get("subject") or "New item from brief"
                ai_row = {
                    "description": desc,
                    "priority": item.get("priority", "P2"),
                    "status": "OPEN",
                    "waiting_on": item.get("waiting_on"),
                }
                ok, errors = validate_action_item(ai_row)
                if not ok:
                    log_rejection(
                        conn, "brief.apply", "action_items", errors, item,
                        context=f"category={cat} source={src_kind}",
                    )
                    counts["rejected"] += 1
                    continue

                # Build provenance: structured ref + clickable URL + person FK
                source_ref = None
                source_url = None
                source_type = None
                source_id_val = None
                source_person_id = None
                email_thread_id = None
                discord_message_id = None
                beeper_message_id = None
                granola_meeting_id = None

                if src_kind == "email" and src_id:
                    source_type = "email"
                    source_id_val = src_id
                    source_ref = f"email:{src_id}"
                    email_thread_id = src_id
                    # Look up sender person_id from emails table
                    p = conn.execute(
                        "SELECT person_id FROM emails WHERE thread_id=? AND is_outgoing=0 "
                        "ORDER BY timestamp ASC LIMIT 1",
                        (src_id,),
                    ).fetchone()
                    if p and p["person_id"]:
                        source_person_id = p["person_id"]
                elif src_kind == "discord" and src_id:
                    source_type = "discord"
                    source_id_val = src_id
                    discord_message_id = src_id
                    # Discord refs need guild + channel; pull from row if possible
                    dm = conn.execute(
                        "SELECT channel_id, person_id FROM discord_messages WHERE id=?",
                        (src_id,),
                    ).fetchone()
                    if dm:
                        source_ref = f"discord:{dm['channel_id']}:{src_id}"
                        if dm["person_id"]:
                            source_person_id = dm["person_id"]
                elif src_kind == "beeper" and src_id:
                    source_type = "beeper"
                    source_id_val = src_id
                    beeper_message_id = src_id
                    bm = conn.execute(
                        "SELECT person_id FROM beeper_messages WHERE id=?",
                        (src_id,),
                    ).fetchone()
                    if bm and bm["person_id"]:
                        source_person_id = bm["person_id"]
                elif src_kind == "granola" and src_id:
                    source_type = "granola"
                    source_id_val = src_id
                    granola_meeting_id = src_id
                    source_ref = f"granola:{src_id}"

                if source_ref:
                    source_url = build_source_url(source_ref)

                # Route through the trust gate
                route = route_action_item(source_tag)
                if route == "inbox":
                    # Gate the brief.classify auto-extraction path. Default off:
                    # LLM-proposed action items only enter the inbox when the
                    # operator explicitly enables it.
                    if os.environ.get("ENABLE_AUTO_EXTRACTION", "0") != "1":
                        counts.setdefault("auto_extraction_disabled", 0)
                        counts["auto_extraction_disabled"] += 1
                        continue
                    # Anti-fabrication self-check at the classifier level: BEFORE
                    # we even propose, verify the LLM-extracted summary actually
                    # appears in the source body. This is the same check that
                    # propose_to_inbox runs internally; calling it here lets us
                    # count classifier-level hallucinations as a quality metric.
                    proposed_quote = (item.get("summary") or "")[:500] or None
                    if proposed_quote and source_type and source_id_val:
                        try:
                            from validators import is_fabricated_quote as _ifq
                            fab, fab_reason = _ifq(conn, proposed_quote, source_type, source_id_val)
                            if fab:
                                counts.setdefault("classifier_hallucinations", 0)
                                counts["classifier_hallucinations"] += 1
                                counts.setdefault("noise", 0)
                                counts["noise"] += 1
                                # Skip propose_to_inbox entirely: quote is fabricated.
                                continue
                        except Exception:
                            pass  # non-fatal; let propose_to_inbox do its own check

                    inbox_id, skip_reason = propose_to_inbox(
                        conn, source_tag, ai_row["description"],
                        priority=ai_row["priority"],
                        waiting_on=ai_row["waiting_on"],
                        context_slug=item.get("project", "") or None,
                        context_tags="@email" if item.get("needs_reply") else None,
                        email_thread_id=email_thread_id,
                        source_evidence=f"{src_kind}:{src_id}" if src_id else None,
                        evidence_quote=proposed_quote,
                        classifier_confidence=item.get("confidence"),
                        source_ref=source_ref,
                        source_url=source_url,
                        source_type=source_type,
                        source_id=source_id_val,
                        source_person_id=source_person_id,
                        discord_message_id=discord_message_id,
                        beeper_message_id=beeper_message_id,
                        granola_meeting_id=granola_meeting_id,
                        extracted_by=extracted_by_label,
                    )
                    if skip_reason:
                        counts["noise"] += 1
                    else:
                        counts.setdefault("inbox_proposed", 0)
                        counts["inbox_proposed"] += 1
                        # Auto-draft pipeline: if this inbox proposal has a
                        # resolvable question and a strong FAQ match, write a
                        # reply draft for the operator's review
                        # (status=auto-suggested, not auto-sent).
                        if email_thread_id and item.get("needs_reply"):
                            try:
                                from auto_draft import try_draft_from_inbox
                                dr = try_draft_from_inbox(conn, inbox_id)
                                if dr.get("status") == "drafted":
                                    counts.setdefault("auto_drafts", 0)
                                    counts["auto_drafts"] += 1
                            except Exception:
                                # Auto-draft failures are non-fatal; the inbox
                                # row was already written.
                                pass
                    continue

                # CANONICAL path (rare for brief): keep legacy insert
                max_row = conn.execute("""
                    SELECT MAX(CAST(SUBSTR(item_id, 15) AS INTEGER)) as mx
                    FROM action_items WHERE item_id LIKE ?
                """, (f"AI-{today.replace('-', '')}-%",)).fetchone()
                next_seq = (max_row["mx"] or 0) + counts["created"] + 1
                item_id = f"AI-{today.replace('-', '')}-{next_seq:03d}"
                while conn.execute("SELECT 1 FROM action_items WHERE item_id = ?", (item_id,)).fetchone():
                    next_seq += 1
                    item_id = f"AI-{today.replace('-', '')}-{next_seq:03d}"
                conn.execute("""
                    INSERT INTO action_items
                    (item_id, status, priority, description, source, context_tags,
                     context_slug, source_type, source_id, source_url, source_ref,
                     source_person_id, email_thread_id, discord_message_id,
                     beeper_message_id, granola_meeting_id,
                     inserted_at, updated_at)
                    VALUES (?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """, (
                    item_id, ai_row["priority"], ai_row["description"], source_tag,
                    "@email" if item.get("needs_reply") else "",
                    item.get("project", ""),
                    source_type, source_id_val, source_url, source_ref,
                    source_person_id, email_thread_id, discord_message_id,
                    beeper_message_id, granola_meeting_id,
                ))
                counts["created"] += 1

            elif cat == "close_candidate" and matched:
                note = f"[Brief {today}] CLOSE CANDIDATE: {item.get('summary', '')}"
                conn.execute("""
                    UPDATE action_items SET
                        context = CASE
                            WHEN context IS NULL OR context = '' THEN ?
                            ELSE context || char(10) || ?
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE item_id = ?
                """, (note, note, matched))
                counts["flagged_close"] += 1

        # ── Post-apply: link email thread_ids to matched action items ────
        threads_linked = 0
        for item in items:
            if item.get("source") != "gmail" or not item.get("matched_action_item"):
                continue
            # Look up thread_id from emails table
            source_id = item.get("source_id", "")
            if not source_id:
                continue
            email_row = conn.execute(
                "SELECT thread_id FROM emails WHERE gmail_message_id = ?",
                (source_id,),
            ).fetchone()
            if not email_row or not email_row["thread_id"]:
                continue
            thread_id = email_row["thread_id"]
            matched_ai = item["matched_action_item"]

            # Only link if action item doesn't already have a thread
            ai_row = conn.execute(
                "SELECT email_thread_id FROM action_items WHERE item_id = ?",
                (matched_ai,),
            ).fetchone()
            if ai_row and not ai_row["email_thread_id"]:
                # Get inbound/outbound timestamps
                ts_row = conn.execute(
                    "SELECT MAX(CASE WHEN is_outgoing = 0 THEN timestamp END) AS last_in, "
                    "       MAX(CASE WHEN is_outgoing = 1 THEN timestamp END) AS last_out "
                    "FROM emails WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE action_items SET email_thread_id = ?, "
                    "email_last_inbound_at = ?, email_last_outbound_at = ?, "
                    "updated_at = datetime('now') WHERE item_id = ?",
                    (thread_id, ts_row["last_in"] if ts_row else None,
                     ts_row["last_out"] if ts_row else None, matched_ai),
                )
                threads_linked += 1

        if threads_linked:
            print(f"  email_tracker: {threads_linked} thread(s) linked to action items", file=sys.stderr)

        # ── Post-apply: stamp last_checked_at on all touched action items ─
        touched_ids = set()
        for item in items:
            m = item.get("matched_action_item")
            if m:
                touched_ids.add(m)
        if touched_ids:
            placeholders = ",".join("?" for _ in touched_ids)
            conn.execute(
                f"UPDATE action_items SET last_checked_at = datetime('now') "
                f"WHERE item_id IN ({placeholders})",
                list(touched_ids),
            )
            print(f"  last_checked_at: stamped on {len(touched_ids)} action item(s)", file=sys.stderr)

        counts["threads_linked"] = threads_linked
        counts["items_checked"] = len(touched_ids)

        # ── Post-apply: flag noise items from VIP senders ────────────
        # VIP = anyone on the configured [contacts] vip list. If the classifier
        # marked them as noise, surface them so the operator can spot-check.
        noise_items = [i for i in items if i.get("category") == "noise" and i.get("sender_email")]
        if noise_items:
            vip_emails = {
                str(v).strip().lower() for v in (config.get("vip_contacts") or []) if str(v).strip()
            }

            flagged = []
            for item in noise_items:
                email = (item.get("sender_email") or "").lower()
                if email and email in vip_emails:
                    flagged.append(item)

            # Layer 2: keyword check - noise items with action-oriented language
            # Only flag if keywords appear in the BODY (not subject, to avoid
            # project-name false hits) and sender is a real person (not an
            # automated platform sender).
            _ACTION_KEYWORDS = {
                "confirm", "confirmed", "accept", "accepted", "decline", "declined",
                "invoice", "payment", "deadline", "urgent", "asap",
                "approve", "approved", "signed", "contract",
                "winner", "sponsorship", "grant",
            }
            _AUTO_SENDERS = {"noreply", "no-reply", "notifications", "zapier", "notion",
                             "slack", "google", "github", "stripe", "linkedin", "calendly"}
            for item in noise_items:
                if item in flagged:
                    continue
                sender_email = (item.get("sender_email") or "").lower()
                # Skip automated senders
                if any(a in sender_email for a in _AUTO_SENDERS):
                    continue
                # Skip our own outbound emails showing up as noise
                if sender_email in OPERATOR_EMAILS or sender_email in ORG_EMAIL_ALIASES:
                    continue
                body = (item.get("body_snippet") or "").lower()
                hits = _ACTION_KEYWORDS & set(body.split())
                if len(hits) >= 2:  # require 2+ action keywords to reduce false positives
                    flagged.append(item)
                    item["_flag_reason"] = f"action keywords in body: {', '.join(sorted(hits)[:3])}"

            # Layer 3: recent conversation - if a real person (not automated) replied
            # to a thread we started in last 7 days, their reply probably isn't noise
            for item in noise_items:
                if item in flagged:
                    continue
                email = (item.get("sender_email") or "").lower()
                if not email:
                    continue
                # Skip automated senders and our own addresses
                if any(a in email for a in _AUTO_SENDERS):
                    continue
                if email in OPERATOR_EMAILS or email in ORG_EMAIL_ALIASES:
                    continue
                # Check if this is a reply to our outbound (thread has our outbound + their inbound)
                thread_id = item.get("thread_id")
                if thread_id:
                    our_reply = conn.execute("""
                        SELECT 1 FROM emails
                        WHERE thread_id = ? AND is_outgoing = 1
                        AND timestamp > datetime('now', '-7 days')
                        LIMIT 1
                    """, (thread_id,)).fetchone()
                    if our_reply:
                        flagged.append(item)
                        item["_flag_reason"] = "reply in active thread we participated in"

            if flagged:
                counts["vip_noise_flagged"] = len(flagged)
                print(f"\n  *** POSSIBLY MISCATEGORIZED: {len(flagged)} noise item(s) flagged ***", file=sys.stderr)
                for f in flagged:
                    sender = f.get("sender") or f.get("sender_email", "")
                    subj = (f.get("subject") or "")[:60]
                    src_id = f.get("source_id", "")
                    reason = f.get("_flag_reason", "VIP sender")
                    print(f"    [{f.get('source','?')}] {sender}: {subj}", file=sys.stderr)
                    print(f"      Reason: {reason}", file=sys.stderr)
                    cr = conn.execute(
                        "SELECT id FROM classification_results WHERE source_id = ? AND brief_id = ? LIMIT 1",
                        (src_id, brief_id)
                    ).fetchone()
                    if cr:
                        print(f"      Fix: brief.py correct {cr['id']}", file=sys.stderr)

        # Mark all as applied
        conn.execute("""
            UPDATE classification_results SET applied = 1, applied_at = datetime('now')
            WHERE brief_id = ? AND applied = 0
        """, (brief_id,))

        # Update briefing_reports counters
        conn.execute("""
            UPDATE briefing_reports SET
                items_classified = ?,
                items_created = ?,
                items_updated = ?,
                items_closed = ?,
                needs_reply_count = ?,
                auto_handled_count = ?
            WHERE id = ?
        """, (
            len(items), counts["created"], counts["updated"],
            counts["flagged_close"],
            sum(1 for i in items if i.get("needs_reply")),
            counts["noise"] + counts["updated"],
            brief_id,
        ))

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"ERROR applying classifications: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    print(json.dumps(counts, indent=2))


def cmd_correct(args):
    """
    Record a correction for a classification result.
    Usage: brief.py correct CLASSIFICATION_ID --category X --priority Y
    At least one of category or priority must be provided.
    """
    cid = args.classification_id
    conn = get_conn()

    try:
        row = conn.execute(
            "SELECT id, category, priority, source, sender, subject, body_snippet "
            "FROM classification_results WHERE id = ?",
            (cid,),
        ).fetchone()
        if not row:
            print(f"Classification {cid} not found")
            return

        orig_cat = row["category"]
        orig_pri = row["priority"]
        new_cat = args.category or orig_cat
        new_pri = args.priority or orig_pri

        if new_cat == orig_cat and new_pri == orig_pri:
            print("No changes specified (category and priority unchanged)")
            return

        # Record the correction
        conn.execute("""
            INSERT INTO classification_corrections
            (classification_id, original_category, corrected_category,
             original_priority, corrected_priority)
            VALUES (?, ?, ?, ?, ?)
        """, (cid, orig_cat, new_cat, orig_pri, new_pri))

        # Update the classification_results row itself
        conn.execute("""
            UPDATE classification_results SET category = ?, priority = ?
            WHERE id = ?
        """, (new_cat, new_pri, cid))

        conn.commit()

        changes = []
        if new_cat != orig_cat:
            changes.append(f"category: {orig_cat} -> {new_cat}")
        if new_pri != orig_pri:
            changes.append(f"priority: {orig_pri} -> {new_pri}")
        print(f"Corrected classification {cid}: {', '.join(changes)}")
        print(f"  Source: {row['source']} | {row['sender']} | {(row['subject'] or '')[:50]}")
    finally:
        conn.close()


def cmd_new_brief(args):
    """Create a briefing_reports row and print its ID."""
    conn = get_conn()
    conn.execute("""
        INSERT INTO briefing_reports (brief_date, brief_started_at, session_id)
        VALUES (date('now'), datetime('now'), ?)
    """, (args.session_id or "",))
    brief_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    print(brief_id)


def cmd_close_brief(args):
    """Finalize a brief with summary text."""
    conn = get_conn()
    conn.execute("""
        UPDATE briefing_reports SET
            brief_completed_at = datetime('now'),
            summary = ?
        WHERE id = ?
    """, (args.summary, args.brief_id))
    conn.commit()
    conn.close()
    print(f"Brief {args.brief_id} closed.")


def cmd_report(args):
    """Show latest brief report."""
    conn = get_conn(readonly=True)

    if args.date:
        row = conn.execute(
            "SELECT * FROM briefing_reports WHERE brief_date = ? ORDER BY id DESC LIMIT 1",
            (args.date,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM briefing_reports ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if not row:
        print("No briefing reports found.")
        conn.close()
        return

    print(f"\n=== Brief: {row['brief_date']} ===")
    print(f"  Started:      {row['brief_started_at']}")
    print(f"  Completed:    {row['brief_completed_at'] or 'IN PROGRESS'}")
    print(f"  Classified:   {row['items_classified'] or 0}")
    print(f"  Created:      {row['items_created'] or 0}")
    print(f"  Updated:      {row['items_updated'] or 0}")
    print(f"  Close cands:  {row['items_closed'] or 0}")
    print(f"  Needs reply:  {row['needs_reply_count'] or 0}")
    print(f"  Auto-handled: {row['auto_handled_count'] or 0}")

    if row["summary"]:
        print(f"\n{row['summary']}")

    cats = conn.execute("""
        SELECT category, COUNT(*) as cnt FROM classification_results
        WHERE brief_id = ? GROUP BY category ORDER BY cnt DESC
    """, (row["id"],)).fetchall()
    if cats:
        print(f"\n  By category:")
        for c in cats:
            print(f"    {c['category'] or 'uncategorized':20s} {c['cnt']}")

    conn.close()


def cmd_status(args):
    """Show sync freshness and brief state."""
    conn = get_conn(readonly=True)
    now = datetime.now()

    print(f"\n=== Sync Status ({now.strftime('%Y-%m-%d %H:%M')}) ===\n")

    for src_name in ["gmail", "beeper_local", "beeper_brief", "calendar", "google_chat", "brief_sync_fast", "discord_dms"]:
        row = conn.execute(
            "SELECT last_sync, last_id, count FROM sync_state WHERE source = ?",
            (src_name,),
        ).fetchone()
        if row and row["last_sync"]:
            try:
                ts = datetime.strptime(row["last_sync"][:19], "%Y-%m-%d %H:%M:%S")
                hours = (now - ts).total_seconds() / 3600
                tag = "FRESH" if hours < 4 else "STALE" if hours < 24 else "VERY STALE"
                # discord_dms carries an ok/error health marker in last_id;
                # a failed pipeline must be visible here, not just in drift.
                health = ""
                if src_name == "discord_dms" and row["last_id"] and row["last_id"] != "ok":
                    tag = "FAILING"
                    health = f"  !! {row['last_id'][:60]}"
                print(f"  {src_name:20s} {row['last_sync'][:16]}  ({hours:.0f}h ago)  [{tag}]{health}")
            except ValueError:
                print(f"  {src_name:20s} {row['last_sync'][:16]}")
        else:
            print(f"  {src_name:20s} NEVER")

    dc = conn.execute("""
        SELECT COUNT(*) as ch, MAX(last_sync) as newest
        FROM sync_state
        WHERE source LIKE 'discord_%'
          AND source NOT LIKE 'discord_dm_%' AND source != 'discord_dms'
    """).fetchone()
    if dc and dc["ch"] and dc["newest"]:
        try:
            dc_ts = datetime.strptime(dc["newest"][:19], "%Y-%m-%d %H:%M:%S")
            dc_hours = (now - dc_ts).total_seconds() / 3600
            dc_tag = "FRESH" if dc_hours < 4 else "STALE" if dc_hours < 24 else "VERY STALE"
        except ValueError:
            dc_tag = "?"
            dc_hours = 0
        print(f"  {'discord':20s} {dc['newest'][:16]}  ({dc_hours:.0f}h ago, {dc['ch']} ch)  [{dc_tag}]")

    brief = conn.execute(
        "SELECT brief_date, brief_completed_at, items_classified "
        "FROM briefing_reports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    status = "NEVER"
    if brief:
        done = "DONE" if brief["brief_completed_at"] else "IN PROGRESS"
        status = f"{brief['brief_date']} ({brief['items_classified'] or 0} items) [{done}]"
    print(f"\n  Last brief: {status}")

    print(f"\n  Action items:")
    for row in conn.execute(
        "SELECT status, COUNT(*) as cnt FROM action_items GROUP BY status ORDER BY cnt DESC"
    ).fetchall():
        print(f"    {row['status']:12s} {row['cnt']}")

    # Surface the trust-gate inbox so the triage backlog is always visible.
    inbox_pending = conn.execute(
        "SELECT COUNT(*) c FROM action_items_inbox WHERE status='pending'"
    ).fetchone()["c"]
    inbox_rejected = conn.execute(
        "SELECT COUNT(*) c FROM action_items_inbox WHERE status='rejected'"
    ).fetchone()["c"]
    if inbox_pending or inbox_rejected:
        oldest = conn.execute(
            "SELECT CAST(julianday('now') - julianday(MIN(proposed_at)) AS INTEGER) d "
            "FROM action_items_inbox WHERE status='pending'"
        ).fetchone()["d"] or 0
        print(f"\n  Inbox: {inbox_pending} pending (oldest {oldest}d), {inbox_rejected} auto-rejected")
        if inbox_pending > 50:
            print(f"    !! triage with `task_manager.py inbox list`")

    # Auto-drafts surfaced from the FAQ pipeline (auto_draft.py)
    drafts_pending = conn.execute(
        "SELECT COUNT(*) c FROM email_drafts WHERE status = 'auto-suggested'"
    ).fetchone()["c"]
    if drafts_pending:
        print(f"\n  Auto-drafts: {drafts_pending} reply drafts ready for review")
        print(f"    review: `task_manager.py drafts list`")

    conn.close()


def cmd_registry(args):
    """List the Beeper chat registry."""
    if not BEEPER_CHATS:
        print("No chats registered. Add [beeper.chats.\"<chat-id>\"] tables to config.toml")
        print("(see the _load_beeper_registry docstring in brief.py for the shape).")
        return
    by_net = {}
    for cid, info in BEEPER_CHATS.items():
        net = info["network"] or "unknown"
        if net not in by_net:
            by_net[net] = []
        by_net[net].append((info["priority"], info["name"], cid))

    for net in sorted(by_net):
        print(f"\n  {net.upper()} ({len(by_net[net])} chats):")
        for pri, name, cid in sorted(by_net[net]):
            print(f"    P{pri} {name}")


# ─── Granola meeting sync ────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")[:60]


def cmd_granola_check(args):
    """List existing transcript slugs so Claude knows what's already synced."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT slug, title, updated_at
        FROM reference_docs
        WHERE slug LIKE 'transcript-%'
        ORDER BY slug DESC
    """).fetchall()
    conn.close()

    print(f"\n=== Existing Transcripts ({len(rows)}) ===\n")
    for r in rows:
        # Extract date from slug: transcript-YYYY-MM-DD-...
        slug = r["slug"]
        date_part = slug[11:21] if len(slug) > 21 else ""
        print(f"  {date_part}  {slug}")
        if r["title"]:
            print(f"           {r['title'][:70]}")

    if args.json:
        data = [{"slug": r["slug"], "title": r["title"], "date": r["slug"][11:21]}
                for r in rows]
        print()
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        print()


def cmd_granola_store(args):
    """
    Store a Granola meeting in reference_docs and extract action items.
    Reads JSON from stdin with fields:
      meeting_id, title, date, participants, notes, transcript, action_items
    """
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON on stdin: {e}", file=sys.stderr)
        sys.exit(1)

    meetings = data if isinstance(data, list) else [data]

    conn = get_conn()
    stored = 0
    ai_created = 0
    operator_tokens = _operator_name_tokens()

    try:
        for meeting in meetings:
            title = meeting.get("title", "Untitled meeting")
            date_str = meeting.get("date", "")
            # Parse date to YYYY-MM-DD
            if date_str:
                try:
                    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    date_ymd = dt.strftime("%Y-%m-%d")
                except ValueError:
                    date_ymd = date_str[:10]
            else:
                date_ymd = datetime.now().strftime("%Y-%m-%d")

            slug = f"transcript-{date_ymd}-{_slugify(title)}"

            # Collision handling: append -2, -3, etc. if slug exists
            existing = conn.execute(
                "SELECT 1 FROM reference_docs WHERE slug = ?", (slug,)
            ).fetchone()
            if existing:
                # Try suffixed slugs
                for suffix in range(2, 10):
                    candidate = f"{slug}-{suffix}"
                    if not conn.execute(
                        "SELECT 1 FROM reference_docs WHERE slug = ?", (candidate,)
                    ).fetchone():
                        slug = candidate
                        existing = None
                        break
                if existing:
                    print(f"  SKIP (exists, all suffixes taken): {slug}")
                    continue

            # Build content (bold labels to match existing transcript format)
            participants = meeting.get("participants", [])
            if isinstance(participants, list):
                participants_str = ", ".join(participants)
            else:
                participants_str = str(participants)

            notes = meeting.get("notes", "")
            transcript = meeting.get("transcript", "")

            content_parts = [
                f"# {title}",
                f"**Date:** {date_ymd}",
                f"**Participants:** {participants_str}",
                f"**Source:** Granola (meeting_id: {meeting.get('meeting_id', 'unknown')})",
            ]
            if notes:
                content_parts.append(f"\n## Notes\n{notes}")
            if transcript:
                content_parts.append(f"\n## Transcript\n{transcript}")

            content = "\n".join(content_parts)

            conn.execute("""
                INSERT INTO reference_docs (slug, title, category, content, source_file, updated_at, doc_type, tags)
                VALUES (?, ?, 'transcript', ?, 'granola', datetime('now'), 'meeting_transcript', 'granola,meeting')
            """, (slug, title, content))
            stored += 1
            print(f"  STORED: {slug}")

            # Extract action items
            action_items = meeting.get("action_items", [])
            for ai in action_items:
                desc = ai.get("description", "").strip()
                if not desc:
                    continue

                # Only create items assigned to the operator or unassigned
                # (operator identity comes from [operator] name/display_name).
                assignee = (ai.get("assignee") or "").lower()
                if (assignee and assignee not in ("", "me", "us")
                        and not any(t in assignee for t in operator_tokens)):
                    continue

                priority = ai.get("priority", "P2")
                due_date = ai.get("due_date")

                # Validate before insert (ingest hardening)
                ai_row = {
                    "description": desc, "priority": priority, "status": "OPEN",
                    "waiting_on": ai.get("waiting_on"),
                }
                ok, errors = validate_action_item(ai_row)
                if not ok:
                    ensure_table(conn)
                    log_rejection(
                        conn, "brief.granola_store", "action_items",
                        errors, ai, context=f"meeting={title} ({date_ymd})",
                    )
                    continue

                # Route through gate: granola is non-canonical, lands in inbox
                # for triage. Auto-extraction is gated off by default; enable
                # explicitly with ENABLE_AUTO_EXTRACTION=1.
                if os.environ.get("ENABLE_AUTO_EXTRACTION", "0") != "1":
                    print(f"  SKIP: auto_extraction_disabled | {desc[:60]}")
                    continue
                try:
                    inbox_id, skip = propose_to_inbox(
                        conn, 'granola', desc,
                        priority=priority, due_date=due_date,
                        evidence_quote=desc[:500],
                        source_type='granola', source_id=slug,
                        source_ref=f'granola:{slug}',
                        granola_meeting_id=slug,
                    )
                    if skip:
                        print(f"  SKIP: {skip} | {desc[:60]}")
                    else:
                        ai_created += 1
                        print(f"  INBOX: {inbox_id} | {desc[:60]}")
                except Exception as _e:
                    print(f"  ERROR proposing to inbox: {_e}")

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"ERROR storing meetings: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    print(f"\nGranola store: {stored} meeting(s), {ai_created} action item(s)")


# ─── Classify: LLM batch classification ──────────────────────────────────────

# The {{...}} placeholders are interpolated from config.toml at runtime:
# {{OPERATOR_NAME}} <- [operator] name, {{OPERATOR_ROLE}} <- [operator] role,
# {{ORG_NAME}} <- [org] name, {{ORG_DESCRIPTION}} <- [org] description.
CLASSIFY_SYSTEM_PROMPT_TEMPLATE = """You are classifying incoming messages for {{OPERATOR_NAME}}, {{OPERATOR_ROLE}} at {{ORG_NAME}}{{ORG_DESCRIPTION}}. Your job: for each message, determine if it's actionable, what category it falls into, whether it matches an existing action item, and its priority.

CATEGORIES:
- noise: newsletters, automated notifications, receipts, event-platform registrations, no-reply senders. No action needed.
- update_only: someone replied with info that updates an existing action item but needs no reply from {{OPERATOR_NAME}}. E.g. "Here's the file" or "Confirmed, see you there."
- needs_reply: a real person is waiting for {{OPERATOR_NAME}}'s response **via this channel**. E.g. a question, a request, a follow-up by email/Discord/Slack.
- new_action: something actionable that doesn't match any existing item. Needs a new action item created.
- close_candidate: evidence that an existing action item may be done. E.g. payment confirmed, person replied with what was requested.

PLATFORM NOTIFICATION EMAILS (always category=noise; never needs_reply):
- Notion comment notifications: sender notify@mail.notion.so or @mail.notion.so. Even if the comment tags {{OPERATOR_NAME}} and asks for action, the answer lives in Notion (not email). If {{OPERATOR_NAME}} needs to act, that happens in Notion directly. Do NOT classify as needs_reply.
- GitHub mention notifications: @notifications.github.com. Same logic.
- Slack notification emails: @slack.com when subject is "X mentioned you" or similar. Same logic.
- Calendar/event-platform/Zoom reminders: noise unless they contain a deadline change for an existing action item.

PRIORITY (importance-based, not urgency):
- P1: mission-critical, financial/legal, blocks others, key stakeholder
- P2: strategically important, partner/sponsor related, core project operations
- P3: standard operational, routine correspondence
- P4: informational only, newsletters, automated

PRE-TRIAGED LABELS (if an item carries labels from the operator's own email automation, trust them as strong signals):
- Payment/invoice-related labels: set priority P1 minimum.
- Support-question labels: classify as needs_reply.
- Digest/summary labels: noise unless they contain actionable content.

DISCORD CHANNEL TYPE SIGNAL (use channel_type field on each item):
- channel_type="guild": general server channel; treat as standard signal. Most banter is noise; direct questions are needs_reply.
- channel_type="dm": 1-on-1 Discord DM to {{OPERATOR_NAME}}. dm_with field names the sender. DMs are HIGHER signal than guild messages -- someone reached out directly. Bias toward needs_reply / new_action unless it's a one-line acknowledgment.
- channel_type="group_dm": small group Discord chat. dm_with field is "Group: name1, name2". Treat like a DM (higher signal); often coordination among insiders.

MATCHING RULES:
- Match by person name/email AND topic overlap
- If the sender matches waiting_on for an action item, that's a strong match signal
- Confidence 0.8+ = auto-link, 0.5-0.8 = flag for review, <0.5 = no match
- If latest message in thread is FROM {{OPERATOR_NAME}} (is_outgoing=true), it's likely noise or close_candidate (we already replied)

OUTPUT FORMAT: Return a JSON array. Each element:
{
  "index": 0,
  "source": "gmail",
  "source_id": "msg_id",
  "actionable": true/false,
  "needs_reply": true/false,
  "matched_action_item": "AI-XXXXXXXX-NNN" or null,
  "matched_confidence": 0.0-1.0,
  "project": "project-or-event-slug" or "ops" or null,
  "priority": "P1"/"P2"/"P3"/"P4",
  "category": "noise"/"update_only"/"needs_reply"/"new_action"/"close_candidate",
  "deadline_detected": "YYYY-MM-DD" or null,
  "summary": "One sentence summary of what this is and what action is needed"
}

Return ONLY the JSON array, no markdown fences, no explanation."""


def _classify_system_prompt() -> str:
    """Render the classify system prompt with the operator's identity from
    config.toml. Sensible generic fallbacks when unconfigured."""
    name = OPERATOR_NAME or "the operator"
    role = str(config.get("operator.role", "") or "").strip() or "operations coordinator"
    org = str(config.get("org.name", "") or "").strip() or "their organization"
    desc = str(config.get("org.description", "") or "").strip()
    desc = f" ({desc})" if desc else ""
    return (CLASSIFY_SYSTEM_PROMPT_TEMPLATE
            .replace("{{OPERATOR_NAME}}", name)
            .replace("{{OPERATOR_ROLE}}", role)
            .replace("{{ORG_NAME}}", org)
            .replace("{{ORG_DESCRIPTION}}", desc))


def _clean_text(s):
    """Remove surrogates and non-UTF8 chars that break the API."""
    if not s:
        return s
    return s.encode("utf-8", errors="replace").decode("utf-8")


def _load_priority_examples(conn, limit: int = 12):
    """Pull recent priority changes from a Kanban-style UI as few-shot examples
    (optional priority_examples table; returns [] when absent).

    Dedup by (task_sender, task_project, old_priority, new_priority) so we
    don't blow up the prompt with repeats. Newest-first within each dedup
    group. Returns a list of dicts with keys: title, sender, project, old, new.
    """
    try:
        rows = conn.execute(
            """SELECT task_title, task_sender, task_project,
                      old_priority, new_priority, MAX(created_at) AS created_at
                 FROM priority_examples
                WHERE created_at >= datetime('now', '-90 days')
                GROUP BY COALESCE(task_sender,''), COALESCE(task_project,''),
                         old_priority, new_priority
                ORDER BY created_at DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    except Exception:
        return []
    return [
        dict(
            title=(r[0] or "")[:80],
            sender=r[1] or "",
            project=r[2] or "",
            old=r[3],
            new=r[4],
        )
        for r in rows
    ]


def _load_active_contexts(conn):
    """Active projects/events for the classifier's context. Feature-detected:
    reads an optional `projects` table (slug + name); returns [] when the
    operator hasn't set one up."""
    if not _table_exists(conn, "projects"):
        return []
    try:
        return [dict(r) for r in conn.execute(
            "SELECT slug, name FROM projects "
            "WHERE COALESCE(status,'active') = 'active' LIMIT 10"
        ).fetchall()]
    except Exception:
        return []


def _build_classify_prompt(items, action_items, contexts, corrections, priority_examples=None):
    """Build the user prompt with items to classify + context."""
    parts = []
    operator = OPERATOR_NAME or "the operator"

    # Active projects/events
    if contexts:
        parts.append("ACTIVE PROJECTS/EVENTS:")
        for h in contexts:
            parts.append(f"  - {h['slug']}: {h.get('name', h['slug'])}")
        parts.append("")

    # Open action items for matching
    if action_items:
        parts.append("OPEN/WAITING ACTION ITEMS (match incoming messages against these):")
        for ai in action_items:
            waiting = f" [WAITING ON: {ai['waiting_on']}]" if ai["waiting_on"] else ""
            tags = f" {ai['context_tags']}" if ai.get("context_tags") else ""
            parts.append(f"  {ai['item_id']} [{ai['priority']}] {ai['description'][:120]}{waiting}{tags}")
        parts.append("")

    # Few-shot corrections
    if corrections:
        parts.append("PAST CORRECTIONS (learn from these):")
        for c in corrections:
            parts.append(f"  - Item from {c.get('sender','?')}: was classified as {c['original_category']}/{c['original_priority']}, corrected to {c['corrected_category']}/{c['corrected_priority']}")
        parts.append("")

    # Few-shot priority examples (manual UI overrides)
    if priority_examples:
        parts.append(f"PAST PRIORITY OVERRIDES ({operator}'s manual corrections: use these to calibrate priority):")
        for pe in priority_examples:
            who = pe['sender'] or 'unknown'
            proj = pe['project'] or 'ops'
            parts.append(f"  - '{pe['title']}' from {who} [{proj}]: was {pe['old']}, corrected to {pe['new']}")
        parts.append("")

    # Items to classify
    parts.append(f"CLASSIFY THESE {len(items)} ITEMS:")
    parts.append("")
    for i, item in enumerate(items):
        parts.append(f"--- Item {i} ---")
        parts.append(f"Source: {item['source']}")
        parts.append(f"Source ID: {item.get('source_id', '')}")
        if item.get("sender"):
            parts.append(f"From: {_clean_text(item['sender'])} <{item.get('sender_email', '')}>")
        if item.get("subject"):
            parts.append(f"Subject: {_clean_text(item['subject'])}")
        if item.get("chat_name"):
            parts.append(f"Chat: {_clean_text(item['chat_name'])}")
        if item.get("channel"):
            parts.append(f"Channel: {_clean_text(item['channel'])} ({item.get('guild', '')})")
        if item.get("is_outgoing"):
            parts.append("Direction: OUTGOING (sent by the operator)")
        parts.append(f"Date: {item.get('received_at', '')}")
        if item.get("labels"):
            parts.append(f"Labels: {item['labels']}")
        snippet = _clean_text(item.get("body_snippet", ""))
        if snippet:
            parts.append(f"Content: {snippet[:300]}")
        # Thread context for emails
        if item.get("thread_context"):
            parts.append("Thread history:")
            for tc in item["thread_context"]:
                who = operator if tc.get("is_us") else _clean_text(tc.get("from", "?"))
                parts.append(f"  [{tc.get('when', '')[:16]}] {who}: {_clean_text(tc.get('snippet', ''))[:150]}")
        parts.append("")

    return "\n".join(parts)


def _keyring_api_key(names):
    """First hit among env vars / keyring entries in `names` (list of key
    names, checked as env first then under the configured keyring service)."""
    for n in names:
        val = os.environ.get(n)
        if val:
            return val
    try:
        import keyring as kr
        for n in names:
            val = kr.get_password(KEYRING_SERVICE, n)
            if val:
                return val
    except Exception:
        pass
    return ""


def _call_anthropic(system_prompt, user_prompt, model, max_tokens=16384):
    """Minimal Anthropic messages call for claude-* classifier models."""
    try:
        import anthropic
    except ImportError:
        print("ERROR: pip install anthropic (or use a gemini-* model)", file=sys.stderr)
        sys.exit(1)
    api_key = _keyring_api_key(["ANTHROPIC_API_KEY"])
    if not api_key:
        print("ERROR: No Anthropic API key found (env ANTHROPIC_API_KEY or keyring)", file=sys.stderr)
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model, max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )


def cmd_classify(args):
    """Classify gathered items via the configured LLM. Reads gather JSON from
    stdin. Both providers work: gemini-* models use the google-genai SDK,
    claude-* models use the anthropic SDK."""
    brief_id = args.brief_id
    model = getattr(args, "model", None) or "gemini-2.5-flash-lite"
    system_prompt = _classify_system_prompt()

    # Read gather JSON
    try:
        gather_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON on stdin: {e}", file=sys.stderr)
        sys.exit(1)

    items = gather_data.get("items", [])
    if not items:
        print("No items to classify.")
        return

    # Load context from DB (read-only, never blocked by writers)
    conn = get_conn(readonly=True)

    action_items = [dict(r) for r in conn.execute("""
        SELECT item_id, status, priority, description, waiting_on, context_slug, context_tags
        FROM action_items WHERE status IN ('OPEN', 'WAITING')
        ORDER BY urgency_score DESC LIMIT 50
    """).fetchall()]

    contexts = _load_active_contexts(conn)

    corrections = []
    try:
        corrections = [dict(r) for r in conn.execute("""
            SELECT original_category, corrected_category, original_priority, corrected_priority
            FROM classification_corrections
            ORDER BY corrected_at DESC LIMIT 20
        """).fetchall()]
    except sqlite3.OperationalError:
        pass  # Table may not exist yet

    priority_examples = _load_priority_examples(conn, limit=12)

    conn.close()

    # Batch items (10 per API call)
    batch_size = 10
    all_classifications = []

    # genai only imported on the gemini path; claude-* models use the
    # anthropic SDK via _call_anthropic.
    client = None
    if not model.startswith("claude"):
        from google import genai
        api_key = _keyring_api_key(["GEMINI_API_KEY", "GOOGLE_API_KEY"])
        if not api_key:
            print("ERROR: No Gemini API key found (env GEMINI_API_KEY/GOOGLE_API_KEY or keyring)", file=sys.stderr)
            sys.exit(1)
        client = genai.Client(api_key=api_key)

    total_batches = (len(items) + batch_size - 1) // batch_size
    print(f"[classify] {len(items)} items in {total_batches} batches...", file=sys.stderr)

    for batch_idx in range(0, len(items), batch_size):
        batch = items[batch_idx:batch_idx + batch_size]
        batch_num = batch_idx // batch_size + 1

        user_prompt = _build_classify_prompt(batch, action_items, contexts, corrections, priority_examples)

        try:
            if model.startswith("claude"):
                response_text = _call_anthropic(
                    system_prompt, user_prompt, model, max_tokens=16384).strip()
            else:
                from google import genai
                response = client.models.generate_content(
                    model=model,
                    contents=f"{system_prompt}\n\n{user_prompt}",
                    config=genai.types.GenerateContentConfig(
                        response_mime_type="application/json",
                        max_output_tokens=16384,
                        thinking_config=genai.types.ThinkingConfig(thinking_budget=2048),
                    ),
                )
                response_text = response.text.strip()

            parsed = json.loads(response_text)
            # The model may wrap the array in an object; normalize to list
            batch_results = parsed if isinstance(parsed, list) else parsed.get("classifications", [parsed])

            # Enrich with source fields from original items
            for cr in batch_results:
                idx = cr.get("index")
                if idx is None:
                    # The model omitted the index field. Skip enrichment to
                    # avoid silently copying fields from the wrong item.
                    print(f"  Warning: classification result missing 'index' field, skipping enrichment", file=sys.stderr)
                    continue
                if idx < len(batch):
                    orig = batch[idx]
                    cr["source"] = cr.get("source") or orig.get("source", "")
                    cr["source_id"] = cr.get("source_id") or orig.get("source_id", "")
                    cr["sender"] = cr.get("sender", orig.get("sender", ""))
                    cr["sender_email"] = orig.get("sender_email", "")
                    cr["subject"] = orig.get("subject", "")
                    cr["received_at"] = orig.get("received_at", "")
                    cr["body_snippet"] = orig.get("body_snippet", "")[:200]
                    cr["thread_id"] = orig.get("thread_id")
                    # Stamp the structural injection verdict on the
                    # classification and HOLD injection items out of the actionable
                    # lane (an inbound directing the assistant is never auto-actioned).
                    _q = _classify_quarantine(orig)
                    cr["quarantine_verdict"] = _q["verdict"]
                    if _q["reasons"]:
                        cr["quarantine_reasons"] = _q["reasons"]
                    if _q["verdict"] == "injection":
                        cr["quarantine_hold"] = True
                        cr["actionable"] = False
                        cr["needs_reply"] = False

            all_classifications.extend(batch_results)
            print(f"  Batch {batch_num}/{total_batches}: {len(batch_results)} classified", file=sys.stderr)

        except json.JSONDecodeError as e:
            print(f"  Batch {batch_num}: JSON parse error: {e}", file=sys.stderr)
            print(f"  Raw response: {response_text[:300]}", file=sys.stderr)
            for idx, orig in enumerate(batch):
                all_classifications.append({
                    "index": idx, "source": orig.get("source", ""),
                    "source_id": orig.get("source_id", ""),
                    "actionable": False, "category": "noise",
                    "priority": "P4", "summary": "Classification failed, marked as noise",
                    "needs_reply": False, "matched_action_item": None,
                })
        except Exception as e:
            print(f"  Batch {batch_num}: API error: {e}", file=sys.stderr)
            for idx, orig in enumerate(batch):
                all_classifications.append({
                    "index": idx, "source": orig.get("source", ""),
                    "source_id": orig.get("source_id", ""),
                    "actionable": False, "category": "noise",
                    "priority": "P4", "summary": f"API error: {e}",
                    "needs_reply": False, "matched_action_item": None,
                })

    # Clean surrogates from all string fields before output
    def _clean_obj(obj):
        if isinstance(obj, str):
            return _clean_text(obj)
        if isinstance(obj, dict):
            return {k: _clean_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_clean_obj(v) for v in obj]
        return obj

    # Output as JSON (can be piped to apply)
    output = _clean_obj({"classifications": all_classifications, "brief_id": brief_id})
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    print()

    # Print summary
    cats = {}
    for c in all_classifications:
        cat = c.get("category", "unknown")
        cats[cat] = cats.get(cat, 0) + 1
    print(f"\n[classify] Summary:", file=sys.stderr)
    for cat, cnt in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {cnt}", file=sys.stderr)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Brief system orchestrator")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("sync", help="Run data syncs")
    s.add_argument("--gmail", action="store_true")
    s.add_argument("--discord", action="store_true")
    s.add_argument("--notion", action="store_true")
    s.add_argument("--beeper", action="store_true")
    s.add_argument("--email-tracker", action="store_true", dest="email_tracker")
    s.add_argument("--calendar", action="store_true")
    s.add_argument("--granola", action="store_true", help="Check Granola transcript freshness only")
    s.add_argument("--beeper-priority", type=int, default=2, choices=[1, 2, 3],
                   dest="beeper_priority",
                   help="Max priority to sync (1=critical, 2=daily, 3=all)")
    s.add_argument("--extractions", action="store_true",
                   help="(No-op) The extraction layer is not included in this starter kit")
    s.add_argument("--skip-extractions", action="store_true",
                   help="Skip the (stubbed) extraction stages entirely")
    s.add_argument("--with-fact-check", action="store_true",
                   help="(No-op) The fact-check pass is part of the extraction layer, not included")
    s.add_argument("--full", action="store_true",
                   help="Run everything inline (legacy; no background spawn)")
    s.add_argument("--no-background", action="store_true",
                   help="Fast phase only; do not spawn background slow phase")

    ss_parser = sub.add_parser("sync-slow", help="Run the slow/background half of a two-phase sync")
    ss_parser.add_argument("--beeper-priority", type=int, default=2, choices=[1, 2, 3], dest="beeper_priority")
    ss_parser.add_argument("--with-fact-check", action="store_true",
                           help="(No-op) fact-check is part of the extraction layer, not included")

    g = sub.add_parser("gather", help="Collect new items since last brief")
    g.add_argument("--since", help="Cutoff date or datetime")
    g.add_argument("--json", action="store_true", help="Full JSON output")
    g.add_argument("--include-personal", action="store_true",
                   help="Include personal/family domain emails (default: work + public + general only when ROUTING_V2=1).")

    cl = sub.add_parser("classify", help="Classify gathered items via LLM (JSON from stdin)")
    cl.add_argument("brief_id", type=int)
    cl.add_argument("--model", default="gemini-2.5-flash-lite",
                    help="Classifier model (default gemini-2.5-flash-lite; any "
                         "gemini-* or claude-* id works -- both providers supported)")

    a = sub.add_parser("apply", help="Apply classifications (JSON from stdin)")
    a.add_argument("brief_id", type=int)
    a.add_argument("--model", default="gemini",
                   help="Which classifier produced the JSON (e.g. gemini|claude). "
                        "Stamped onto action_items_inbox.suggested_extracted_by as "
                        "'brief-classify-<model>' so historical attribution is queryable per model.")

    n = sub.add_parser("new-brief", help="Create briefing_reports row, print ID")
    n.add_argument("--session-id", default="", dest="session_id")

    c = sub.add_parser("close-brief", help="Finalize brief with summary")
    c.add_argument("brief_id", type=int)
    c.add_argument("summary")

    r = sub.add_parser("report", help="Show latest brief report")
    r.add_argument("--date", help="YYYY-MM-DD")

    sub.add_parser("status", help="Show sync freshness")
    sub.add_parser("registry", help="Show Beeper chat registry")
    sub.add_parser("drift-check", help="Run the drift detector now")

    gc = sub.add_parser("granola-check", help="List existing transcript slugs")
    gc.add_argument("--json", action="store_true", help="JSON output")

    sub.add_parser("granola-store", help="Store Granola meeting (JSON from stdin)")

    co = sub.add_parser("correct", help="Record a classification correction")
    co.add_argument("classification_id", type=int)
    co.add_argument("--category", help="Corrected category")
    co.add_argument("--priority", help="Corrected priority")

    args = p.parse_args()

    def _cmd_drift_check(_args):
        from drift_check import run_checks  # noqa: PLC0415
        result = run_checks()
        if not result["actions"]:
            print("[drift-check] no drift detected")
        for at, action in result["actions"]:
            print(f"  {action:9s}  {at}")
        return 0

    dispatch = {
        "sync": cmd_sync,
        "sync-slow": cmd_sync_slow,
        "gather": cmd_gather,
        "classify": cmd_classify,
        "apply": cmd_apply,
        "new-brief": cmd_new_brief,
        "close-brief": cmd_close_brief,
        "report": cmd_report,
        "status": cmd_status,
        "registry": cmd_registry,
        "drift-check": _cmd_drift_check,
        "granola-check": cmd_granola_check,
        "granola-store": cmd_granola_store,
        "correct": cmd_correct,
    }

    fn = dispatch.get(args.command)
    if fn:
        fn(args)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
