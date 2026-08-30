"""PostToolUse hook: auto-surface context when reading email threads.

Fires after Bash tool calls. Detects `<your-cli> gmail thread <id>` (and
friends) and runs search/surface_context.py --fast on the captured stdout,
prepending the auto-surfaced context block (dossier + FAQ matches + graph
refs + template suggestion) to stderr so the model sees it without having
to remember to invoke surface_context manually.

This is the third + final guard for the "never missed" promise:
  1. Skill mandate (the comms/email skill's pre-draft step says run
     surface_context before drafting any reply)
  2. Memory (an auto-surface-before-drafting learning)
  3. THIS hook -- the harness runs surface_context for you automatically

Performance: surface_context --fast uses FTS-only FAQ lookup, no model
load. Typical latency ~0.1-0.3s. Hook timeout in settings.json is 10-15s,
which is more than enough headroom.

Fail-open: any error here is silently swallowed; the user's underlying
Bash output is unaffected.

Exit code: 0 (always). Output to stderr is the auto-surfaced block.
"""
import json
import os
import re
import subprocess
import sys

_hooks_dir = os.path.dirname(os.path.abspath(__file__))
if not sys.path or sys.path[0] != _hooks_dir:
    # Front-insert so hooks/config.py wins over core/config.py when this
    # module is imported in-process (script execution already has it first).
    sys.path.insert(0, _hooks_dir)

try:
    from health_monitor import timed, emit
except Exception:
    # Stub: don't break if health_monitor fails to import
    def timed(name):
        def deco(f):
            return f
        return deco
    def emit(*a, **kw):
        pass

PYTHON = sys.executable


def _surface_context_path():
    """Resolve search/surface_context.py via the shared path resolver, with a
    repo-relative fallback (hooks/ sits directly under the repo root)."""
    try:
        import paths
        return str(paths.ROOT / "search" / "surface_context.py")
    except Exception:
        return os.path.join(os.path.dirname(_hooks_dir), "search", "surface_context.py")


SURFACE_CONTEXT = _surface_context_path()

# Match a `... gmail thread <thread_id>` or `... gmail read <msg_id>` shaped
# Bash command from whatever email CLI you wire up. Thread/message IDs are
# Gmail-style hex (8-20 chars). Adjust to your own mail tool's read commands.
GMAIL_READ_RE = re.compile(
    r"\bgmail\s+(thread|read)\s+([0-9a-fA-F]{6,24})\b"
)

MAX_BODY_CHARS = 8000  # cap stdin to keep subprocess fast


def _extract_text(tool_response):
    """Pull stdout text from tool_response."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        for key in ("stdout", "content", "output"):
            val = tool_response.get(key, "")
            if val:
                return val
    return ""


@timed("auto_surface")
def run(event):
    tool_name = event.get("tool_name", "")
    if tool_name != "Bash":
        return
    tool_input = event.get("tool_input", {}) or {}
    cmd = tool_input.get("command", "") or ""
    m = GMAIL_READ_RE.search(cmd)
    if not m:
        return
    kind = m.group(1)        # "thread" or "read"
    target_id = m.group(2)   # gmail id

    if not os.path.exists(SURFACE_CONTEXT):
        return  # search layer not installed; nothing to surface

    # Pull the actual rendered thread/message body from the tool's stdout.
    text = _extract_text(event.get("tool_response") or event.get("tool_output", {}))
    if not text:
        return
    text = text[:MAX_BODY_CHARS]

    # Call surface_context --fast with the body piped via stdin and the
    # thread-id passed for thread-history enrichment.
    args = [PYTHON, SURFACE_CONTEXT, "--fast"]
    if kind == "thread":
        args.extend(["--thread-id", target_id])
    # 'read' is a single message; we don't have a thread-id, so just the body.
    try:
        result = subprocess.run(
            args,
            input=text,
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        emit("auto_surface_timeout", "surface_context >10s", {"cmd": cmd[:200]})
        return
    except Exception as e:
        emit("auto_surface_error", str(e)[:200], {"cmd": cmd[:200]})
        return

    if result.returncode != 0:
        emit("auto_surface_nonzero", f"exit={result.returncode}", {"cmd": cmd[:200]})
        return

    output = (result.stdout or "").strip()
    if not output:
        return

    # Detect noise-only output (no people, no questions, no refs detected).
    # In that case, skip the banner -- nothing useful to surface.
    if "(no entities" in output:
        return

    # Write the auto-surfaced block to stderr; Claude Code surfaces hook
    # stderr to the model as additional context.
    sys.stderr.write("\n")
    sys.stderr.write("=" * 80 + "\n")
    sys.stderr.write("[auto_surface hook] Context auto-surfaced from " +
                     f"`gmail {kind} {target_id}`:\n")
    sys.stderr.write("=" * 80 + "\n")
    sys.stderr.write(output + "\n")
    emit("auto_surface_fired", f"{kind}/{target_id}", {"chars": len(output)})


def main():
    try:
        data = json.load(sys.stdin)
        run(data)
    except Exception as e:
        # Fail-open: never break the user's workflow on hook error.
        sys.stderr.write(f"[auto_surface hook] error (swallowed): {e}\n")


if __name__ == "__main__":
    main()
    sys.exit(0)
