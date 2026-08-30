"""Import Claude Code session JSONL files into the ops database (conversation_history).

Usage:
    python import_session.py <session_file.jsonl>
    python import_session.py  # imports all unimported sessions for this project

Claude Code stores session transcripts under ~/.claude/projects/<slug>/, where
<slug> is the project working directory with every non-alphanumeric character
replaced by '-'. The slug is derived at runtime from CLAUDE_PROJECT_DIR (set by
Claude Code hooks) or the current working directory, so run this from the repo
root of the project whose sessions you want to import.

Handles both old format (snapshot-based) and new format (top-level message).
"""
import paths
import _db  # unified connector (busy_timeout + FK ON)
import json
import os
import re
import sys
from datetime import datetime
from glob import glob

DB = str(paths.DB_PATH)


def project_slug(base: str | None = None) -> str:
    """Claude Code project-folder slug: the working directory with every
    non-alphanumeric character replaced by '-' (the same transformation
    Claude Code applies when naming ~/.claude/projects/ subfolders)."""
    base = base or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return re.sub(r"[^A-Za-z0-9]", "-", str(base))


def sessions_dir(base: str | None = None) -> str:
    """Directory holding this project's session JSONL files."""
    return os.path.join(os.path.expanduser("~/.claude/projects"), project_slug(base))


def get_session_id_from_path(path):
    return os.path.splitext(os.path.basename(path))[0]


def extract_text(content):
    """Extract text from message content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def extract_messages(jsonl_path):
    """Extract messages from a session JSONL file."""
    messages = []
    seen_ids = set()

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = entry.get("type", "")
            if msg_type not in ("user", "assistant", "summary"):
                continue

            msg_id = entry.get("uuid") or entry.get("messageId", "")
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            ts = entry.get("timestamp")
            session_id = entry.get("sessionId", "")

            # New format: message at top level
            message = entry.get("message", {})
            if message:
                role = message.get("role", msg_type)
                text = extract_text(message.get("content", ""))
            else:
                # Old format: snapshot-based
                snapshot = entry.get("snapshot", {})
                role = snapshot.get("role", msg_type)
                msg_obj = snapshot.get("message", {})
                text = extract_text(msg_obj.get("content", ""))
                if not ts:
                    ts = snapshot.get("timestamp")

            if text.strip():
                messages.append({
                    "role": role,
                    "text": text.strip()[:10000],
                    "timestamp": ts,
                    "msg_id": msg_id,
                    "session_id": session_id,
                })

    return messages


def import_session(db, jsonl_path, project: str | None = None):
    session_id = get_session_id_from_path(jsonl_path)
    project = project or project_slug()

    existing = db.execute(
        "SELECT COUNT(*) FROM conversation_history WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]

    if existing > 0:
        return 0, "already imported"

    messages = extract_messages(jsonl_path)
    if not messages:
        return 0, "no messages"

    imported = 0
    for msg in messages:
        ts = msg["timestamp"]
        ts_human = ""
        if ts:
            try:
                if isinstance(ts, (int, float)):
                    ts_human = datetime.fromtimestamp(ts / 1000).isoformat()
                else:
                    ts_human = str(ts)
            except (ValueError, TypeError, OSError):
                ts_human = str(ts)

        db.execute(
            """INSERT INTO conversation_history
            (session_id, display, project, timestamp, ts_human, inserted_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                f"[{msg['role']}] {msg['text']}",
                project,
                str(ts) if ts else "",
                ts_human,
                datetime.now().isoformat(),
            ),
        )
        imported += 1

    db.commit()
    return imported, "ok"


def main():
    db = _db.connect(DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    project = project_slug()

    if len(sys.argv) > 1:
        path = sys.argv[1]
        count, status = import_session(db, path, project=project)
        session_id = get_session_id_from_path(path)
        print(f"{session_id}: {count} messages ({status})")
    else:
        pattern = os.path.join(sessions_dir(), "*.jsonl")
        files = sorted(glob(pattern), key=os.path.getmtime)
        total_imported = 0
        total_new = 0

        for f in files:
            count, status = import_session(db, f, project=project)
            session_id = get_session_id_from_path(f)
            if count > 0:
                print(f"  {session_id}: {count} messages")
                total_new += 1
                total_imported += count
            elif status == "no messages":
                print(f"  {session_id}: {status}")

        existing = db.execute(
            "SELECT COUNT(DISTINCT session_id), COUNT(*) FROM conversation_history"
        ).fetchone()
        print(f"\nImported {total_imported} messages from {total_new} new sessions")
        print(f"Total in DB: {existing[0]} sessions, {existing[1]} messages")

    db.close()


if __name__ == "__main__":
    main()
