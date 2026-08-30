"""PreToolUse hook: consolidated quality gate.

CHECK 1 - Slop detection: blocks Write/Edit/Bash/outbound-MCP calls carrying
          banned AI phrases or em dashes.
CHECK 2 - Fabrication check: warns on unverified emails, phones, Notion URLs
          (anything human-facing that could be made up must trace to the DB).

Exit codes:
  0 = allow (warnings may be printed to stderr)
  2 = block
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

from config import get_conn, set_session_id
from health_monitor import timed, emit

# Optionally shared with a proxy-side enforcer: anchored send-command predicate
# + verbatim-quote exemption. Fail-open: if no slop_rules module is installed,
# fall back to the built-in behavior shapes below.
try:
    from slop_rules import strip_quoted as _strip_quoted, is_send_command as _is_send_command
except Exception:  # noqa: BLE001
    def _strip_quoted(text):
        return text

    def _is_send_command(cmd):
        return any(m in (cmd or "") for m in ("gmail send", "discord send", "gmail draft"))

# ---------------------------------------------------------------------------
# CHECK 1 constants: Slop detection
# ---------------------------------------------------------------------------
from slop_rules import SLOP_BANNED, EM_DASH_PATTERNS  # one list for both hooks

# Legitimate terms that happen to contain a banned word (matched text is
# removed before scanning). Empty by default. FICTIONAL example:
# SLOP_ALLOWED_CONTEXTS = ["Robust Statistics Track"]
SLOP_ALLOWED_CONTEXTS = []

# Files to skip slop check (internal files, not outbound content)
SLOP_SKIP_PATTERNS = [
    "/.claude/",
    "/memory/",
    "/rules/",
    "/skills/",
    "/reference/",
    ".jsonl",
    ".json",
    ".csv",
    ".py",
    ".sh",
    ".md",
]

# Override skip patterns for known outbound-content directories (paths where
# drafted content IS destined for a human audience). Add your own conventions.
FORCE_SLOP_PATHS = [
    '/outbound/',
    '/website-content',
    'announcement',
]

# ---------------------------------------------------------------------------
# CHECK 2 constants: Fabrication check
# ---------------------------------------------------------------------------
# Phone numbers that are always OK to mention (the operator's own numbers,
# emergency numbers). Empty by default. FICTIONAL example:
# PHONE_WHITELIST = {"+15555550100", "911"}
PHONE_WHITELIST = set()

EMAIL_RE = re.compile(r'[\w.+-]+@[\w.-]+\.\w{2,}')
PHONE_RE = re.compile(
    r'(?:'
    r'\+\d[\d\s\-()]{7,}'
    r'|\(\d{3}\)\s*\d{3}[\-\s]\d{4}'
    r'|\b\d{3}[\-\s]\d{3}[\-\s]\d{4}\b'
    r')'
)
DATE_RE = re.compile(
    r'(?:'
    r'\d{4}[\-/]\d{1,2}[\-/]\d{1,2}'
    r'|\d{1,2}[\-/]\d{1,2}[\-/]\d{4}'
    r')'
)

NOTION_URL_RE = re.compile(r'https?://(?:www\.)?notion\.so/\S+')
NOTION_ID_RE = re.compile(r'[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}', re.I)

_known_emails = None


def load_known_emails():
    """Load known emails from the ops DB (people + person_emails tables)."""
    global _known_emails
    if _known_emails is not None:
        return _known_emails
    _known_emails = set()
    try:
        conn = get_conn(busy_timeout_ms=1500)  # PreToolUse budget is 5s; keep DB waits short
        rows = conn.execute(
            "SELECT DISTINCT email FROM people WHERE email IS NOT NULL "
            "UNION "
            "SELECT DISTINCT email FROM person_emails WHERE email IS NOT NULL"
        ).fetchall()
        for row in rows:
            email = row[0]
            if email:
                _known_emails.add(email.strip().lower())
        conn.close()
    except Exception as e:
        print(f"QUALITY GATE: Could not load emails from the ops DB: {e}", file=sys.stderr)
    return _known_emails


def normalize_phone(p):
    return re.sub(r'[\s\-()]', '', p)


# ---------------------------------------------------------------------------
# Main gate
# ---------------------------------------------------------------------------
@timed("quality_gate")
def run(event):
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    file_path = (tool_input.get("file_path", "") or "").replace("\\", "/")

    # --- Determine content to check ---
    content = ""
    is_bash = False
    run_slop = False

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        # anchored predicate: a plain substring marker would classify commands
        # that merely MENTION 'gmail send' (greps, audits) as sends
        if _is_send_command(cmd):
            content = cmd
            is_bash = True
        else:
            return  # nothing to check

        # Canonical-template reminder for outbound email drafts/sends.
        # Nudge only, never blocks. Together with the comms skill's own routing
        # and the auto-surface hook, this is a redundant guard against
        # free-form drafting.
        if "gmail" in cmd:
            print(
                "OUTBOUND DRAFT REMINDER\n"
                "  - Did you pull a canonical template from your template registry\n"
                "    (reference_docs) instead of drafting free-form?\n"
                "  - Did you run the context-surfacing tool (search/surface_context.py)\n"
                "    on the inbound first? (auto-dossier + FAQ check)\n"
                "  - If skipping for an internal/edge case, OK; otherwise pull a template.",
                file=sys.stderr,
            )
            emit("template_reminder_fired", "canonical-template reminder shown", {"cmd": cmd[:200]})
    elif tool_name in ("Write", "Edit"):
        content = tool_input.get("content", "") or tool_input.get("new_string", "")
    elif tool_name.startswith("mcp__metricool__") and ("post_schedule" in tool_name or "update_schedule" in tool_name):
        # Metricool post/update: extract text from nested info object
        info = tool_input.get("info", {})
        if isinstance(info, dict):
            content = info.get("text", "")
        elif isinstance(info, str):
            content = info
        else:
            content = json.dumps(info)  # fallback: scan the whole blob
        if content:
            run_slop = True
    elif tool_name.startswith("mcp__gmail__") and any(
            k in tool_name for k in ("draft_email", "send_email", "update_draft",
                                     "reply_to_email", "reply_all", "forward_email")):
        # Gmail MCP outbound: subject + body fields carry the human-facing text
        content = " ".join(
            str(tool_input.get(k, "")) for k in
            ("subject", "body", "htmlBody", "additionalMessage") if tool_input.get(k)
        )
        if content:
            run_slop = True
    else:
        return  # not a tool we check

    if not content:
        return

    # ===================================================================
    # CHECK 1: Slop detection
    # ===================================================================
    if is_bash:
        run_slop = True
    elif file_path:
        should_check = file_path.endswith((".txt", ".html"))
        if should_check:
            run_slop = True
            for skip in SLOP_SKIP_PATTERNS:
                if skip in file_path:
                    run_slop = False
                    break

        # Override skip patterns for known outbound content directories
        file_path_norm = file_path.replace('\\', '/').lower()
        force_check = any(p in file_path_norm for p in FORCE_SLOP_PATHS)
        if force_check:
            run_slop = True

    if run_slop:
        # verbatim-quote exemption: quoted source material (e.g. reviewer
        # feedback quoted in a notification email) may legitimately contain
        # banned words / em dashes; scan only the unquoted body
        content_scan = _strip_quoted(content)
        content_lower = content_scan.lower()
        for allowed in SLOP_ALLOWED_CONTEXTS:
            content_lower = content_lower.replace(allowed.lower(), "")
        for phrase in SLOP_BANNED:
            if phrase.lower() in content_lower:
                emit("comms_blocked", f"Slop detected: '{phrase}'", {"tool": tool_name, "file": file_path})
                print(f"AI slop detected: '{phrase}'. Rewrite to sound human.", file=sys.stderr)
                sys.exit(2)
        for pattern in EM_DASH_PATTERNS:
            if pattern in content_scan:
                emit("comms_blocked", f"Em dash detected", {"tool": tool_name, "file": file_path})
                print(f"Em dash detected ('{pattern}'). Use commas, periods, or colons instead.", file=sys.stderr)
                sys.exit(2)

        # CHECK 1b: structural / rhythm tells (warn only, never blocks).
        # The phrase list above catches vocabulary; slop_stats.py scores the
        # structure detectors key on (markdown residue, tricolons, uniform
        # sentence length, participle tails). Statistical tells false-positive
        # on plain prose, so this layer prints and lets the write through.
        try:
            from slop_stats import analyze as _slop_analyze, format_report as _slop_report
            _res = _slop_analyze(content_scan)
            if _res.get("score", 0) >= 30:
                print(_slop_report(_res, "(warn only)"), file=sys.stderr)
        except Exception:
            pass

    # ===================================================================
    # CHECK 2: Fabrication check
    # ===================================================================
    run_fabrication = True
    if not is_bash:
        if not file_path:
            run_fabrication = False
        elif file_path.endswith((".json", ".csv")):
            run_fabrication = False
        elif "social-media" in file_path.lower():
            run_fabrication = False
    if "[PLACEHOLDER]" in content:
        run_fabrication = False

    if run_fabrication:
        warnings = []

        # Check emails
        found_emails = EMAIL_RE.findall(content)
        if found_emails:
            known = load_known_emails()
            safe_suffixes = {"@example.com", "@example.org", "@placeholder.com"}
            for email in found_emails:
                email_lower = email.lower()
                if email_lower in known:
                    continue
                if any(email_lower.endswith(s) for s in safe_suffixes):
                    continue
                warnings.append(f"Unverified email: {email}. Is this real?")

        # Check phone numbers
        found_phones = PHONE_RE.findall(content)
        if found_phones:
            for phone in found_phones:
                stripped = phone.strip()
                normalized = normalize_phone(stripped)
                if any(normalize_phone(w) in normalized or normalized in normalize_phone(w)
                       for w in PHONE_WHITELIST):
                    continue
                if DATE_RE.search(stripped):
                    continue
                if (not stripped.startswith("+")
                        and re.fullmatch(r'\d+', normalized)
                        and len(normalized) > 12):
                    continue
                warnings.append(f"Unverified phone number: {stripped}. Is this real?")

        # Check Notion URLs
        notion_urls = NOTION_URL_RE.findall(content)
        for url in notion_urls:
            url = url.rstrip('",)\'>')
            ids = NOTION_ID_RE.findall(url)
            if not ids:
                warnings.append(f"Notion URL with no valid ID: {url}. Is this fabricated?")

        if warnings:
            print("FABRICATION CHECK WARNING", file=sys.stderr)
            print("=" * 40, file=sys.stderr)
            for w in warnings:
                print(f"  {w}", file=sys.stderr)
            print("", file=sys.stderr)
            print("Verify these are real before proceeding.", file=sys.stderr)
            # Warn only, don't block


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    try:
        data = json.load(sys.stdin)
        # Prime session_id from Claude Code payload so any bus/audit writes from
        # this hook (e.g. emit("comms_blocked", ...)) share the turn's session_id.
        set_session_id(data.get("session_id", "") or data.get("sessionId", ""))
        run(data)
    except Exception as e:
        print(f"HOOK ERROR (quality_gate.py): {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
    sys.exit(0)
