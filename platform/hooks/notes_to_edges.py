"""PostToolUse hook: notes-column writes -> spawn a notes extractor.

Listens for INSERT / UPDATE statements that touch a notes / description
column on people or action_items. Fires a notes-extractor script in a
detached subprocess to extract entity references from the free text and
write graph edges with provenance.

The extractor itself is part of the extraction layer, which is NOT included
in this starter kit. Point OPS_NOTES_EXTRACTOR (env var) or config.toml
[hooks] notes_extractor at your own script to activate this hook; until
then, a matched write logs a one-line notice and no-ops.

Fail-open: any parse error or missing data -> exit silently.

Hook protocol: Claude Code passes JSON on stdin with the tool name + input.
We only read; we never block. Exit 0 always.

Matches one of:
  - Bash:  the command contains a `sqlite3 ... "UPDATE|INSERT ..."` or a
           python -c that runs SQL we can't parse cheaply; we still match
           on the SQL substring.
  - mcp__<server>__ops_query:  args.sql contains the SQL.
  - mcp__<server>__ops_write:  args.sql similar.
  (<server> defaults to "ops-mcp"; see _mcp_server_name below.)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

PYTHON = sys.executable
_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))


def _mcp_server_name() -> str:
    """MCP server name prefix from config (default 'ops-mcp').

    Override via env var OPS_MCP_SERVER_NAME or config.toml:

        [mcp]
        # FICTIONAL example:
        # server_name = "my-ops"
    """
    env = os.environ.get("OPS_MCP_SERVER_NAME", "").strip()
    if env:
        return env
    try:
        import tomllib
        cfg_path = os.path.join(os.path.dirname(_HOOKS_DIR), "config.toml")
        if os.path.exists(cfg_path):
            with open(cfg_path, "rb") as f:
                cfg = tomllib.load(f)
            name = str((cfg.get("mcp", {}) or {}).get("server_name", "") or "").strip()
            if name:
                return name
    except Exception:
        pass
    return "ops-mcp"


MCP_SERVER = _mcp_server_name()
MCP_QUERY_TOOL = f"mcp__{MCP_SERVER}__ops_query"
MCP_WRITE_TOOL = f"mcp__{MCP_SERVER}__ops_write"


def _extractor_path() -> str | None:
    """Resolve the notes-extractor script (None = not installed)."""
    p = os.environ.get("OPS_NOTES_EXTRACTOR", "").strip()
    if not p:
        try:
            import tomllib
            cfg_path = os.path.join(os.path.dirname(_HOOKS_DIR), "config.toml")
            if os.path.exists(cfg_path):
                with open(cfg_path, "rb") as f:
                    cfg = tomllib.load(f)
                p = str((cfg.get("hooks", {}) or {}).get("notes_extractor", "") or "").strip()
        except Exception:
            p = ""
    if p and os.path.exists(p):
        return p
    return None


TARGET_TABLES = {
    "people":       ("notes",       r"\bid\b\s*=\s*(\d+)"),
    "action_items": ("description", r"\bid\b\s*=\s*(\d+)"),
}

# Regex: catch UPDATE notes/description = ... WHERE id = N
UPDATE_RE = re.compile(
    r"UPDATE\s+(people|action_items)\s+SET\s+.*?(notes|description)\s*=\s*['\"](.*?)['\"]"
    r".*?WHERE\s+.*?\bid\s*=\s*(\d+)",
    re.IGNORECASE | re.DOTALL,
)

# Regex: catch INSERT INTO ... (..., notes, ...) VALUES (..., 'X', ...)
INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+(people|action_items)\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)",
    re.IGNORECASE,
)


def _spawn_extractor(table: str, row_id: int, source: str) -> None:
    """Detached subprocess. We don't wait."""
    extractor = _extractor_path()
    if extractor is None:
        # Honest no-op: don't fake success, say why nothing happened.
        sys.stderr.write(
            f"[notes_to_edges hook] notes write detected ({table} id={row_id}) but the "
            "extraction layer is not included in this starter kit; set OPS_NOTES_EXTRACTOR "
            "to activate edge extraction.\n"
        )
        return
    try:
        subprocess.Popen(
            [PYTHON, extractor, "extract", "--table", table, "--id", str(row_id), "--source", source],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0),
        )
    except Exception:
        pass  # fail-open


def _extract_sql(payload: dict) -> str:
    """Pull the SQL text out of the tool input."""
    tool = payload.get("tool_name") or payload.get("tool") or ""
    inp = payload.get("tool_input") or {}
    if tool.startswith(MCP_QUERY_TOOL) or tool.startswith(MCP_WRITE_TOOL):
        return (inp.get("sql") or inp.get("query") or "")
    if tool == "Bash":
        return inp.get("command", "")
    return ""


def main() -> int:
    # Entire body fail-open: this hook must never block a tool call.
    try:
        payload = json.load(sys.stdin)
        sql = _extract_sql(payload)
        if not sql or len(sql) < 30:
            return 0

        session_id = (payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or "unknown")[:16]
        source = f"session-postwrite:{session_id}"

        # UPDATE patterns
        for m in UPDATE_RE.finditer(sql):
            table = m.group(1)
            row_id = int(m.group(4))
            _spawn_extractor(table, row_id, source)
            return 0  # one spawn is enough; the extractor will read the latest row state

        # INSERT patterns: spawn once we can identify a newly inserted row id.
        # Without the lastrowid we can't reliably target the new row from a hook,
        # so we no-op INSERTs in v1. The next time the row is UPDATEd (or the
        # session writes a follow-up note) we'll catch it.
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
