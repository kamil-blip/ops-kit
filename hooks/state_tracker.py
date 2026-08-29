"""PostToolUse + Stop hook: tracks action item progress and state.

Consolidates action-item progress matching + a periodic Stop-time status check.
All reads/writes go to the ops DB.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

_hooks_dir = os.path.dirname(os.path.abspath(__file__))
if not sys.path or sys.path[0] != _hooks_dir:
    # Front-insert so hooks/config.py wins over core/config.py when this
    # module is imported in-process (script execution already has it first).
    sys.path.insert(0, _hooks_dir)

from config import get_conn, get_session_id, set_session_id, INTERNAL_EMAIL_DOMAINS
from health_monitor import timed, emit

_counter_file = os.path.expanduser("~/.claude/.state_tracker_counter")

# --- Async worker emit helpers (lazy import, fail-open) --------------------
# Precompiled regexes used by the emit block below.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_NAME_LIKE_RE = re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")

# Email suffixes treated as internal (the operator's own org): excluded from
# participant lists so observations track external contacts, not colleagues.
# Configure [org] domain in config.toml (see hooks/config.py).
_INTERNAL_SUFFIXES = tuple("@" + d for d in INTERNAL_EMAIL_DOMAINS)


def _is_internal(email_lower: str) -> bool:
    return bool(_INTERNAL_SUFFIXES) and email_lower.endswith(_INTERNAL_SUFFIXES)


def _safe_emit(handler_name, payload, dedup_key=None, priority=5):
    """Fire-and-forget emit into a background work_queue.

    A background worker layer is NOT part of this starter kit: unless the
    operator installs a work_queue module (exposing emit_event) on the
    import path, this emit is a silent no-op by design. The hook must never
    block or crash on a missing/broken worker, so any exception is swallowed.
    """
    try:
        from work_queue import emit_event  # noqa: PLC0415
        emit_event(handler_name, payload, priority=priority, dedup_key=dedup_key)
    except Exception:
        # Never block the hook on worker failures (or on there being no worker).
        pass


def _get_text(data):
    out = data.get("tool_response") or data.get("tool_output", {})
    if isinstance(out, str):
        return out
    if isinstance(out, dict):
        parts = []
        for key in ("stdout", "stderr", "content"):
            val = out.get(key, "")
            if val:
                parts.append(val)
        return "\n".join(parts) if parts else str(out)
    return str(out) if out else ""


@timed("state_tracker")
def main(data):
    tool_input = data.get("tool_input", {})
    text = _get_text(data)
    command = tool_input.get("command", "")
    combined = f"{command} {text}"[:3000]

    if not combined or len(combined) < 30:
        return

    conn = get_conn()

    # Match against high-priority open action items only (P0/P1/P2)
    # Capped at 30 to stay within timeout budget
    items = conn.execute("""
        SELECT id, item_id, status, priority, description, waiting_on, context
        FROM action_items WHERE status IN ('OPEN', 'WAITING', 'BLOCKED')
        AND priority IN ('P0', 'P1', 'P2')
        ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 END
        LIMIT 30
    """).fetchall()

    combined_lower = combined.lower()
    for item in items:
        desc = (item['description'] or '').lower()
        waiting = (item['waiting_on'] or '').lower()
        context = (item['context'] or '').lower()

        score = 0
        words = re.findall(r'\b[a-z]{3,}\b', desc)
        for i in range(len(words) - 1):
            if f"{words[i]} {words[i+1]}" in combined_lower:
                score += 1
        if waiting and len(waiting) > 3 and waiting in combined_lower:
            score += 3
        for email in re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', context):
            if email in combined_lower:
                score += 5

        if score >= 3:
            emit("action_item_match", f"Match: {item['description'][:80]}", {"item_id": item['item_id'], "score": score})
            print(f"Possible completion: {item['description'][:80]}", flush=True)
            # Async: ask a link_action_item handler to verify the match and
            # update email_threads.action_item_id if the signal is real.
            _safe_emit(
                "link_action_item",
                {
                    "item_id": item["item_id"],
                    "score": score,
                    "text": combined[:2000],
                },
                dedup_key=f"link_{item['item_id']}_{get_session_id()}",
                priority=5,
            )

    conn.close()

    # --- Async worker emits --------------------------------------------------
    # NOTE: entity extraction from raw tool output was removed deliberately.
    # Shell-command / tool-result-repr junk burned live LLM calls without ever
    # carrying signal; real comms sources (emails, transcripts, chat) should
    # reach entity extraction via their own sync handlers, never via tool
    # output.

    # log_interaction: detect email, Discord, Slack, Beeper interactions.
    # Record observations + infer edges. Also enrich recipients.
    try:
        tool_name = data.get("tool_name", "") or ""
        tool_lower = tool_name.lower()
        cmd_lower = command.lower() if command else ""

        # Determine interaction kind from tool/command context.
        # email_out matches genuine SENDS only: an earlier looser matcher
        # ("gmail" anywhere in the tool name, "send"+"email" anywhere in the
        # command) also matched pure reads (get_thread, search_emails) and
        # routine SQL (SELECT sender_email), flooding observations with junk.
        # Inbound mail is captured by the sync pipeline, not from tool output,
        # so there is deliberately no email_in branch here.
        _GMAIL_SEND_TOOLS = (
            "mcp__gmail__send_email", "mcp__gmail__reply_to_email",
            "mcp__gmail__reply_all", "mcp__gmail__forward_email",
            "mcp__gmail__send_draft",
        )
        interaction_kind = None
        if any(t in tool_lower for t in _GMAIL_SEND_TOOLS):
            interaction_kind = "email_out"
        elif "beeper" in tool_lower:
            interaction_kind = "discord"  # Beeper bridges Slack/Discord/Signal
        elif "slack" in tool_lower:
            interaction_kind = "discord"  # observations model treats as same
        elif "discord" in tool_lower:
            interaction_kind = "discord"
        elif "granola" in tool_lower or "meeting" in cmd_lower:
            interaction_kind = "meeting"

        if interaction_kind:
            # Extract unique participants from emails in the text
            seen_emails = set()
            participants = []
            for email_addr in _EMAIL_RE.findall(combined):
                email_lower = email_addr.lower()
                if email_lower in seen_emails:
                    continue
                seen_emails.add(email_lower)
                if not _is_internal(email_lower):
                    participants.append(email_lower)
            # Also try name-like tokens for non-email channels
            if not participants and interaction_kind in ("discord", "meeting"):
                for m in _NAME_LIKE_RE.finditer(combined):
                    participants.append(m.group(0))
                    if len(participants) >= 5:
                        break

            # dedup_key: stable hashlib digest (built-in hash() is
            # salt-randomized per process, so it never collides across hook
            # invocations). Collapses bulk-loop bursts (hundreds of rows/hour)
            # into one row per session+kind+topic.
            import hashlib as _hl
            _topic = (command or tool_name or "")[:200]
            _safe_emit(
                "log_interaction",
                {
                    "kind": interaction_kind,
                    "source_session": get_session_id(),
                    "participants": participants[:10],
                    "topic": _topic,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "context_ref": {"tool_name": tool_name},
                },
                dedup_key="log_%s_%s_%s" % (
                    interaction_kind, get_session_id(),
                    _hl.sha1(_topic.encode("utf-8", "replace")).hexdigest()[:16]),
                priority=5,
            )
            # Auto-enrich email recipients for next interaction
            for email_lower in participants:
                if "@" in email_lower:
                    _safe_emit(
                        "enrich_person",
                        {"email": email_lower},
                        dedup_key=f"enrich_{email_lower}_{get_session_id()}",
                        priority=3,
                    )
    except Exception:
        pass

    # categorize_action_item: if tool output mentions creating/updating action items
    try:
        if "action_item" in combined_lower and any(
            w in combined_lower for w in ("insert", "created", "new item", "ai-202")
        ):
            # Find recently created uncategorized items
            conn2 = get_conn()
            uncategorized = conn2.execute("""
                SELECT item_id, description FROM action_items
                WHERE priority IS NULL
                  AND created_at > datetime('now', '-1 hour')
                ORDER BY created_at DESC LIMIT 3
            """).fetchall()
            conn2.close()
            for row in uncategorized:
                _safe_emit(
                    "categorize_action_item",
                    {
                        "item_id": row["item_id"],
                        "description": (row["description"] or "")[:500],
                        "source": "state_tracker_auto",
                    },
                    dedup_key=f"cat_{row['item_id']}_{get_session_id()}",
                    priority=4,
                )
    except Exception:
        pass

    # extract_action_items producer retired: automatic action-item extraction
    # from raw tool output was superseded by a dedicated extraction pipeline
    # (not included in this starter kit), and the emit was removed at the
    # source. Two lessons baked into that removal, kept here for anyone
    # reintroducing it: (1) Bash commands must NEVER fire extraction; shell
    # text has no reliable thread id and substring triggers mis-fire
    # constantly ("read" inside "thread_id", "reaching", "ready").
    # (2) source_ref must be a real thread/message id parsed from tool_input,
    # never the raw command or tool name; fire with source_ref=None rather
    # than lying.

    # Learning signal detection
    try:
        from learning_loop import stage_signal
        stage_signal(data)
    except Exception:
        pass


@timed("state_tracker_stop")
def on_stop(data):
    """Runs on Stop event (every 5th invocation)."""
    count = 0
    try:
        if os.path.exists(_counter_file):
            with open(_counter_file, "r") as f:
                count = int(f.read().strip() or "0")
        count += 1
        with open(_counter_file, "w") as f:
            f.write(str(count))
    except Exception:
        count = 1

    if count % 5 != 0:
        return

    warnings = []
    conn = get_conn()

    # Deferred queue size
    pending = conn.execute("SELECT COUNT(*) FROM deferred_actions WHERE status = 'pending'").fetchone()[0]
    if pending >= 3:
        warnings.append(f"DEFERRED QUEUE: {pending} actions pending.")

    # Overdue P0 items
    today = datetime.now().strftime("%Y-%m-%d")
    overdue = conn.execute("""
        SELECT description FROM action_items
        WHERE status = 'OPEN' AND priority = 'P0' AND due_date < ?
        LIMIT 1
    """, (today,)).fetchone()
    if overdue:
        warnings.append(f"OVERDUE P0: {overdue['description'][:50]}")

    conn.close()

    # Heartbeat
    emit("heartbeat", "Session active", {"run_count": count})

    if warnings:
        print("\n".join(warnings), file=sys.stderr)


if __name__ == "__main__":
    try:
        data = json.load(sys.stdin)
        # Prime session_id from Claude Code payload so audit_events, observations,
        # and work_queue dedup keys share the same session_id across hooks.
        set_session_id(data.get("session_id", "") or data.get("sessionId", ""))
        # The hook is registered for both PostToolUse and Stop
        # Distinguish by checking if tool_name exists
        if data.get("tool_name"):
            main(data)
        else:
            on_stop(data)
    except Exception as e:
        print(f"HOOK ERROR (state_tracker): {e}", file=sys.stderr)
    sys.exit(0)
