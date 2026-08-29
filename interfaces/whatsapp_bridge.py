#!/usr/bin/env python3
"""WhatsApp <-> Claude Code bridge helper.

The operator's WhatsApp note-to-self chat is a command channel: messages the
operator sends there are prompts for the always-on Claude Code session, which
polls via `poll`, does the work, replies into the same chat (prefixed with the
BOT_MARKER so the poller never treats its own replies as prompts), and
advances the cursor with `ack`.

Voice notes are downloaded through Beeper Desktop's local API
(POST /v1/assets/download resolves localmxc:// URLs to a file on disk) and
transcribed offline with faster-whisper. Model 'small' matches
interfaces/transcribe.py.

Configuration:
    WHATSAPP_BRIDGE_CHAT_ID   REQUIRED: the Beeper chat id of the note-to-self
                              chat (or pass --chat-id / a positional chat id).
                              Find yours via Beeper Desktop's local API
                              (GET /v1/chats).
                              # FICTIONAL example: WHATSAPP_BRIDGE_CHAT_ID=1234
    BEEPER_TOKEN              Beeper Desktop access token; falls back to the
                              keyring entry ('claude-mcp', 'BEEPER_TOKEN').

Usage:
    whatsapp_bridge.py poll [chat]          # JSON list of unprocessed prompts (voice pre-transcribed)
    whatsapp_bridge.py watch [secs] [chat]  # block, print one JSON line per NEW prompt (default 5s tick)
    whatsapp_bridge.py ack <sortKey> [chat] # mark everything up to sortKey as handled
    whatsapp_bridge.py status [chat]        # cursor + chat sanity check
    whatsapp_bridge.py warm                 # pre-download the whisper model
    (any command also accepts --chat-id <id>)

`watch` is the event-driven front end: run it under Claude Code's Monitor tool
(persistent) so each new message wakes the session instead of polling on a timer.
It keeps an in-memory high-water mark, so a prompt that is emitted but not yet
acked is never re-emitted or re-transcribed on the next tick.

Cursor lives in the ops DB's sync_state table (source='whatsapp_bridge',
last_id=sortKey). Replies are sent by the session via the Beeper MCP's
send_message tool (MCP-first), not here.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

import keyring

import paths

sys.stdout.reconfigure(encoding="utf-8")  # cp1252 console chokes on emoji

BEEPER_BASE = "http://localhost:23373"
CHAT_ID_ENV = "WHATSAPP_BRIDGE_CHAT_ID"
BOT_MARKER = "\U0001f916"  # robot emoji -- every bridge reply starts with this
DB = str(paths.DB_PATH)
WHISPER_MODEL = "small"


def _default_chat_id() -> str:
    cid = os.environ.get(CHAT_ID_ENV, "").strip()
    if not cid:
        raise SystemExit(
            f"No chat id configured. Set the {CHAT_ID_ENV} env var to your "
            "note-to-self Beeper chat id, or pass --chat-id / a positional "
            "chat id. List chats via Beeper Desktop's local API (GET /v1/chats)."
        )
    return cid


def _token() -> str:
    tok = os.environ.get("BEEPER_TOKEN") or keyring.get_password("claude-mcp", "BEEPER_TOKEN")
    if not tok:
        raise SystemExit("No BEEPER_TOKEN in env or keyring")
    return tok


def _api(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BEEPER_BASE + path, data=data, method="POST" if body is not None else "GET",
        headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _conn():
    import sqlite3
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    return c


def _source_for(chat_id: str) -> str:
    """Cursor namespace per chat. The primary note-to-self chat (the one
    configured in WHATSAPP_BRIDGE_CHAT_ID) keeps the plain 'whatsapp_bridge'
    source; any other chat (e.g. a back-and-forth thread) gets its own
    'whatsapp_bridge:<id>' cursor so two watchers never clobber each other."""
    primary = os.environ.get(CHAT_ID_ENV, "").strip()
    return "whatsapp_bridge" if primary and str(chat_id) == primary else f"whatsapp_bridge:{chat_id}"


def get_cursor(source: str = "whatsapp_bridge") -> int:
    c = _conn()
    row = c.execute("SELECT last_id FROM sync_state WHERE source=?", (source,)).fetchone()
    c.close()
    return int(row[0]) if row and row[0] else 0


def set_cursor(sort_key: int, source: str = "whatsapp_bridge") -> None:
    c = _conn()
    c.execute(
        "INSERT INTO sync_state (source, last_sync, count, last_id) "
        "VALUES (?, CURRENT_TIMESTAMP, 1, ?) "
        "ON CONFLICT(source) DO UPDATE SET last_sync=CURRENT_TIMESTAMP, "
        "count=sync_state.count+1, last_id=excluded.last_id",
        (source, str(sort_key)),
    )
    c.commit()
    c.close()


def _transcribe(local_path: str) -> str:
    from faster_whisper import WhisperModel
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(local_path, vad_filter=True, beam_size=5)
    return " ".join(s.text.strip() for s in segments)


def _download(src_url: str) -> str:
    res = _api("/v1/assets/download", {"url": src_url})
    return res["srcURL"].removeprefix("file:///").replace("/", "\\")


def _collect(cursor: int, chat_id: str) -> list[dict]:
    """Fetch the chat and return actionable prompts with sortKey > cursor.

    Shared by poll() (cursor = DB cursor) and watch() (cursor = in-memory
    high-water mark, so a message that has been emitted but not yet acked is
    never re-fetched / re-transcribed on the next tick)."""
    data = _api(f"/v1/chats/{chat_id}/messages?limit=50")
    items = data.get("items", data if isinstance(data, list) else [])
    prompts = []
    for m in items:
        try:
            sort_key = int(m.get("sortKey", 0))
        except (TypeError, ValueError):
            continue
        if sort_key <= cursor:
            continue
        if m.get("isDeleted") or m.get("isHidden"):
            continue
        text = (m.get("text") or "").strip()
        # Our sent replies come back HTML-wrapped (<p> + marker), so strip tags
        # before the marker check or the loop answers its own echoes.
        import re
        plain = re.sub(r"<[^>]+>", "", text).strip()
        if plain.startswith(BOT_MARKER):
            continue  # our own reply
        text = plain
        mtype = (m.get("extra") or {}).get("type") or m.get("type") or "TEXT"
        if mtype == "NOTICE":
            continue
        entry = {"sortKey": sort_key, "timestamp": m.get("timestamp"), "type": mtype}
        atts = m.get("attachments") or []
        voice = [a for a in atts if a.get("isVoiceNote") or a.get("type") == "audio"]
        if voice:
            path = _download(voice[0]["srcURL"])
            entry["transcript"] = _transcribe(path)
            entry["audio_path"] = path
        elif text:
            entry["text"] = text
        else:
            continue  # stickers/images/etc. -- nothing actionable yet
        prompts.append(entry)
    prompts.sort(key=lambda e: e["sortKey"])
    return prompts


def poll(chat_id: str | None = None) -> list[dict]:
    chat_id = chat_id or _default_chat_id()
    return _collect(get_cursor(_source_for(chat_id)), chat_id)


def watch(interval: float = 5.0, chat_id: str | None = None) -> int:
    """Block forever; print one compact JSON line per NEW prompt as it lands.

    Designed to sit under Claude Code's Monitor tool: each printed line becomes
    one notification that wakes the session, which does the work, replies via
    the Beeper MCP's send_message tool, and acks. No LLM runs while this idles,
    so watching costs nothing until an actual message arrives. Transient Beeper
    outages (Desktop closed, API blip) are swallowed so the watch survives.
    """
    import time
    chat_id = chat_id or _default_chat_id()
    src = _source_for(chat_id)
    hwm = get_cursor(src)
    while True:
        try:
            prompts = _collect(hwm, chat_id)
        except Exception:
            time.sleep(interval)
            continue
        for p in prompts:
            print(json.dumps(p, ensure_ascii=False), flush=True)
            hwm = max(hwm, p["sortKey"])
        time.sleep(interval)


def main() -> int:
    args = sys.argv[1:]
    chat_flag = None
    if "--chat-id" in args:
        i = args.index("--chat-id")
        if i + 1 >= len(args):
            raise SystemExit("--chat-id requires a value")
        chat_flag = args[i + 1]
        del args[i:i + 2]

    cmd = args[0] if args else "poll"

    def chat_for(pos: int) -> str:
        # precedence: --chat-id > positional > WHATSAPP_BRIDGE_CHAT_ID env
        if chat_flag:
            return chat_flag
        if len(args) > pos:
            return args[pos]
        return _default_chat_id()

    if cmd == "poll":
        chat = chat_for(1)
        print(json.dumps(poll(chat), ensure_ascii=False, indent=1))
    elif cmd == "ack":
        if len(args) < 2:
            raise SystemExit("usage: whatsapp_bridge.py ack <sortKey> [chat]")
        sort_key = int(args[1])
        chat = chat_for(2)
        set_cursor(sort_key, _source_for(chat))
        print(f"cursor[{_source_for(chat)}] -> {sort_key}")
    elif cmd == "status":
        chat = chat_for(1)
        print(json.dumps({"cursor": get_cursor(_source_for(chat)), "chat_id": chat,
                          "pending": len(poll(chat))}, indent=1))
    elif cmd == "watch":
        interval = float(args[1]) if len(args) > 1 else 5.0
        chat = chat_for(2)
        return watch(interval, chat)
    elif cmd == "warm":
        from faster_whisper import WhisperModel
        WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("model cached")
    else:
        raise SystemExit(f"unknown command: {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
