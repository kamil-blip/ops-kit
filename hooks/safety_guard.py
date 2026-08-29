"""PreToolUse hook: Safety guard with allow / block / defer decisions.

Outputs JSON to stdout (v2.1.89+ protocol):
  {"decision": "block", "reason": "..."}  — hard stop
  {"decision": "defer", "reason": "..."}  — model decides with context
  (no output + exit 0)                    — allow

Hard blocks:
  - Notion writes to protected DBs (matched by database ID)
  - Raw SQL DELETE/DROP/TRUNCATE without WHERE (via sqlite3 CLI)

Deferred to model:
  - Notion writes matching protected name fragments (common false positives)
  - SQL UPDATE without WHERE clause (might be intentional)

All protected/writable database lists ship EMPTY: populate them with your own
workspace's IDs at setup (see the commented examples below).
"""
import json
import os
import re
import sys

_hooks_dir = os.path.dirname(os.path.abspath(__file__))
if not sys.path or sys.path[0] != _hooks_dir:
    # Front-insert so hooks/config.py wins over core/config.py when this
    # module is imported in-process (script execution already has it first).
    sys.path.insert(0, _hooks_dir)

from config import set_session_id
from health_monitor import timed, emit


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Decision helpers (JSON stdout protocol)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _block(reason: str):
    """Hard block. Tool call is rejected."""
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def _defer(reason: str):
    """Defer to model. Model sees the reason and decides with full context."""
    print(json.dumps({"decision": "defer", "reason": reason}))
    sys.exit(0)


def _allow():
    """Allow. No output needed."""
    sys.exit(0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GUARD: Notion DB protection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Protected DB IDs — NEVER write to these. Typical candidates: databases kept
# in sync by an external automation (Zapier, Make, a custom integration),
# where a manual write corrupts the sync. Empty by default.
# FICTIONAL example:
# PROTECTED_DBS = {
#     "00000000-0000-0000-0000-000000000001": "Contacts DB (automation-managed)",
# }
PROTECTED_DBS = {}

# Writable DB IDs — writes to these are OK. Empty by default.
# FICTIONAL example:
# WRITABLE_DBS = {
#     "00000000-0000-0000-0000-000000000002": "Tasks DB",
# }
WRITABLE_DBS = {}

# Protected DB name fragments (catch commands that use names instead of IDs).
# These get DEFERRED, not blocked, because name matches false-positive often.
# FICTIONAL example:
# PROTECTED_NAMES = ["contacts", "signups"]
PROTECTED_NAMES = []

# Name fragments that EXEMPT a match above (writable DBs whose names overlap
# a protected fragment). FICTIONAL example:
# ALLOWED_NAME_CONTEXTS = ["tasks db"]
ALLOWED_NAME_CONTEXTS = []

WRITE_OPS = ["update", "create", "write", "patch", "delete", "append"]

# Substrings identifying YOUR Notion CLI wrapper in Bash commands, so CLI
# writes get the same guard as MCP writes. Empty by default (the MCP guard
# below still applies). FICTIONAL example:
# NOTION_CLI_MARKERS = ["my-notion-cli", "notionctl"]
NOTION_CLI_MARKERS = []

NOTION_MCP_WRITE_TOOLS = [
    "mcp__claude_ai_Notion__notion-update-page",
    "mcp__claude_ai_Notion__notion-create-pages",
    "mcp__claude_ai_Notion__notion-create-database",
    "mcp__claude_ai_Notion__notion-create-comment",
    "mcp__claude_ai_Notion__notion-update-data-source",
    "mcp__claude_ai_Notion__notion-update-view",
    "mcp__claude_ai_Notion__notion-move-pages",
    "mcp__claude_ai_Notion__notion-duplicate-page",
]


def _writable_names() -> str:
    """Human-readable list of configured writable DBs for block messages."""
    return ", ".join(sorted(WRITABLE_DBS.values())) or "(none configured)"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GUARD: SQL safety
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# DROP/TRUNCATE: always destructive, WHERE is irrelevant
SQL_DROP = re.compile(r'\b(DROP\s+TABLE|TRUNCATE\s+TABLE)\b', re.IGNORECASE)
# DELETE FROM: destructive only without WHERE
SQL_DELETE = re.compile(r'\bDELETE\s+FROM\b', re.IGNORECASE)
# UPDATE: handles quoted/schema-qualified table names (e.g. "my table", schema.table)
SQL_UPDATE = re.compile(r'\bUPDATE\s+[\w."]+\s+SET\b', re.IGNORECASE)
SQL_WHERE = re.compile(r'\bWHERE\b', re.IGNORECASE)


def _is_sqlite3_cli(command: str) -> bool:
    """True if sqlite3 is used as a CLI command, not inside Python/script code.

    Splits on pipe/chain operators and checks if any segment starts with sqlite3.
    This avoids false positives from 'python -c "import sqlite3; ..."' where
    SQL keywords appear in string literals but aren't raw SQL.
    """
    for segment in re.split(r'[|;&]+', command.lower()):
        if segment.strip().startswith("sqlite3"):
            return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GUARD: Outbound Gmail content quality
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GMAIL_OUTBOUND_TOOLS = {
    "mcp__gmail__send_email",
    "mcp__gmail__reply_to_email",
    "mcp__gmail__reply_all",
    "mcp__gmail__forward_email",
    "mcp__gmail__draft_email",
    "mcp__gmail__update_draft",
    "mcp__gmail__send_draft",
}

# Banned phrases per the operator's communication-style hard-ban list
# (anti-AI-slop policy: outbound mail should read as human-written).
# Stored lowercase, matched via substring on combined body+htmlBody+subject.
from slop_rules import SLOP_BANNED as BANNED_PHRASES  # one list for both hooks


def check_gmail_outbound(tool_input: dict):
    """Inspect outbound Gmail body + htmlBody + subject.
    Returns (category, sample, context_snippet) on violation, or None.
    """
    body = tool_input.get("body", "") or ""
    html_body = tool_input.get("htmlBody", "") or ""
    subject = tool_input.get("subject", "") or ""
    combined = body + "\n" + html_body + "\n" + subject

    # Em dash (U+2014) — hard ban
    if "—" in combined:
        idx = combined.find("—")
        ctx = combined[max(0, idx - 35):idx + 35].replace("\n", " ")
        return "em_dash", "—", ctx

    # En dash (U+2013) — hard ban (often pasted in from sources)
    if "–" in combined:
        idx = combined.find("–")
        ctx = combined[max(0, idx - 35):idx + 35].replace("\n", " ")
        return "en_dash", "–", ctx

    # Banned phrases
    combined_lower = combined.lower()
    for phrase in BANNED_PHRASES:
        if phrase in combined_lower:
            idx = combined_lower.find(phrase)
            ctx = combined[max(0, idx - 25):idx + len(phrase) + 25].replace("\n", " ")
            return "banned_phrase", phrase, ctx

    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_for_protected_ids(text: str) -> str | None:
    """Check if text contains any protected DB ID. Returns DB name or None."""
    for db_id, db_name in PROTECTED_DBS.items():
        if db_id in text or db_id.replace("-", "") in text:
            return db_name
    return None


def check_for_protected_names(text: str) -> str | None:
    """Check if text contains protected DB name fragments. Returns matched name or None."""
    text_lower = text.lower()
    for name in PROTECTED_NAMES:
        if name in text_lower and not any(w in text_lower for w in ALLOWED_NAME_CONTEXTS):
            return name
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main guard logic
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@timed("safety_guard")
def run(event):
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})

    # ── GUARD: Bash commands ──
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        cmd_lower = command.lower()

        # Notion CLI write guard (only fires if NOTION_CLI_MARKERS configured)
        if any(m in cmd_lower for m in NOTION_CLI_MARKERS):
            is_write = any(op in cmd_lower for op in WRITE_OPS)
            if is_write:
                # Protected DB by ID → BLOCK (definite match)
                db_name = check_for_protected_ids(command)
                if db_name:
                    emit("safety_blocked", f"Notion write to {db_name}", {"tool": "Bash", "db": db_name})
                    _block(
                        f"Notion write to {db_name}. "
                        f"This database is managed by an external automation. Manual writes corrupt data. "
                        f"Writable databases: {_writable_names()}."
                    )

                # Protected DB by name → DEFER (high false positive rate)
                matched = check_for_protected_names(command)
                if matched:
                    emit("safety_deferred", f"Notion write name match '{matched}'", {"tool": "Bash", "match": matched})
                    _defer(
                        f"Possible Notion write to protected database (matched: '{matched}'). "
                        f"Protected databases are managed by an external automation. "
                        f"Writable: {_writable_names()}. "
                        f"Proceed only if you're certain this targets a writable DB."
                    )

        # SQL safety guard (raw sqlite3 CLI commands only, not Python imports)
        if _is_sqlite3_cli(command):
            # DROP/TRUNCATE: always block (WHERE is never valid for these)
            if SQL_DROP.search(command):
                emit("safety_blocked", "DROP/TRUNCATE SQL", {"tool": "Bash", "cmd": command[:200]})
                _block(
                    "DROP/TRUNCATE detected. These are always destructive. "
                    "Requires explicit approval."
                )
            # DELETE FROM without WHERE: block
            if SQL_DELETE.search(command) and not SQL_WHERE.search(command):
                emit("safety_blocked", "DELETE without WHERE", {"tool": "Bash", "cmd": command[:200]})
                _block(
                    "DELETE FROM without WHERE clause. "
                    "This would delete ALL rows. Add a WHERE clause or get explicit approval."
                )
            if SQL_UPDATE.search(command) and not SQL_WHERE.search(command):
                emit("safety_deferred", "SQL UPDATE without WHERE", {"tool": "Bash", "cmd": command[:200]})
                _defer(
                    "SQL UPDATE without WHERE clause detected. "
                    "This would update ALL rows. Proceed only if that's the intent."
                )

        # Note: recipient-allowlist rules (e.g. "payments mail goes only to the
        # vendor's team address") belong in CLAUDE.md / the comms skill
        # checklist, not in a Bash pattern match. A previous pattern-based
        # version fired on benign SQL/DB writes that merely mentioned the
        # vendor name alongside common verbs, so it was removed.

    # ── GUARD: Notion MCP tools ──
    elif tool_name in NOTION_MCP_WRITE_TOOLS:
        input_str = json.dumps(tool_input)

        # Protected DB by ID → BLOCK
        db_name = check_for_protected_ids(input_str)
        if db_name:
            emit("safety_blocked", f"Notion MCP write to {db_name}", {"tool": tool_name, "db": db_name})
            _block(
                f"Notion MCP write targeting {db_name}. "
                f"This database is managed by an external automation. Manual writes corrupt data. "
                f"Writable databases: {_writable_names()}."
            )

        # Protected DB by name → DEFER
        matched = check_for_protected_names(input_str)
        if matched:
            emit("safety_deferred", f"Notion MCP write name match '{matched}'", {"tool": tool_name, "match": matched})
            _defer(
                f"Notion MCP write may target protected database (matched: '{matched}'). "
                f"Protected databases are managed by an external automation. "
                f"Writable: {_writable_names()}. "
                f"Proceed only if targeting a writable database."
            )

    # ── GUARD: Outbound Gmail content quality ──
    elif tool_name in GMAIL_OUTBOUND_TOOLS:
        violation = check_gmail_outbound(tool_input)
        if violation:
            category, sample, context = violation
            emit("safety_blocked", f"Gmail outbound: {category}", {"tool": tool_name, "sample": sample[:60]})
            if category == "em_dash":
                _block(
                    "Em dash (U+2014) detected in outbound Gmail body. "
                    f"Context: '...{context}...'\n"
                    "Replace with comma, period, colon, or parentheses. "
                    "See the operator's communication-style hard bans."
                )
            elif category == "en_dash":
                _block(
                    "En dash (U+2013) detected in outbound Gmail body. "
                    f"Context: '...{context}...'\n"
                    "Replace with hyphen, comma, or 'to' for ranges."
                )
            elif category == "banned_phrase":
                _block(
                    f"Banned phrase '{sample}' detected in outbound Gmail body. "
                    f"Context: '...{context}...'\n"
                    "Rewrite per the operator's communication-style hard-ban list."
                )

    _allow()


def _fail_closed_check(data):
    """Minimal, dependency-free re-inspection of the raw payload for the GATED
    categories only. Returns a block-reason string if the crashed call is
    gated (Notion write to a PROTECTED_DBS id / sqlite3 DROP-TRUNCATE / sqlite3
    DELETE-without-WHERE / Gmail outbound with an em-or-en-dash), else None. Never
    raises. Used ONLY from main()'s except handler to fail closed on a guard crash."""
    try:
        tool = data.get("tool_name", "") or ""
        ti = data.get("tool_input", {})
        if not isinstance(ti, dict):
            return None
        if tool in NOTION_MCP_WRITE_TOOLS:
            s = json.dumps(ti)
            for db_id, name in PROTECTED_DBS.items():
                if db_id in s or db_id.replace("-", "") in s:
                    return f"safety_guard crashed on a gated-category call (Notion write to {name}); failing closed"
        if tool == "Bash":
            cmd = str(ti.get("command", "") or "")
            if _is_sqlite3_cli(cmd):
                if SQL_DROP.search(cmd):
                    return "safety_guard crashed on a gated-category call (sqlite3 DROP/TRUNCATE); failing closed"
                if SQL_DELETE.search(cmd) and not SQL_WHERE.search(cmd):
                    return "safety_guard crashed on a gated-category call (sqlite3 DELETE without WHERE); failing closed"
        if tool in GMAIL_OUTBOUND_TOOLS:
            combined = (str(ti.get("body", "") or "") + "\n"
                        + str(ti.get("htmlBody", "") or "") + "\n"
                        + str(ti.get("subject", "") or ""))
            if "—" in combined or "–" in combined:
                return "safety_guard crashed on a gated-category call (Gmail outbound with an em/en-dash); failing closed"
    except Exception:  # noqa: BLE001 - the fail-closed check must never itself raise
        return None
    return None


def main():
    raw = None
    data = None
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw else {}
        # Prime session_id so safety_blocked/safety_deferred bus_events get
        # tagged with the turn's session_id instead of a random fallback.
        set_session_id(data.get("session_id", "") or data.get("sessionId", ""))
        run(data)
    except Exception as e:
        # FAIL CLOSED for the gated categories. A crash in run() (bad payload,
        # regex/emit failure) used to exit 0 == ALLOW, silently permitting the
        # very actions the guard blocks. Now: if the crashed call is gated, BLOCK;
        # otherwise preserve allow-on-error so a hook bug never bricks unrelated calls.
        # Override: env SAFETY_GUARD_FAILOPEN=1 restores pure allow-on-error.
        if os.environ.get("SAFETY_GUARD_FAILOPEN") != "1":
            try:
                if data is None and raw:
                    data = json.loads(raw)
            except Exception:  # noqa: BLE001
                data = None
            reason = _fail_closed_check(data) if isinstance(data, dict) else None
            if reason:
                print(json.dumps({"decision": "block", "reason": reason}))
                sys.exit(0)
        print(f"HOOK ERROR (safety_guard.py): {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
