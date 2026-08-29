"""Centralized configuration for all hooks.

Single source of truth for paths, DB connections, session IDs, and constants.
Imported by health_monitor.py and individual hooks.

Operator-specific values (own email aliases, internal domains) come from the
repo-root config.toml or env vars; nothing personal is hardcoded here.
"""
import os
import sqlite3
import sys

try:
    import _db  # unified connector (busy_timeout + FK ON)
except ImportError:
    _db = None  # fail-open: hooks must never crash on a missing module

HOME = os.path.expanduser("~")
_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))


def _fallback_root():
    """OPS_ROOT env var, else the repo root (hooks/ sits directly under it).

    Mirrors core/paths.py's resolution so the hooks stay usable even when the
    core package is not importable yet (e.g. mid-install).
    """
    env = os.environ.get("OPS_ROOT", "").strip()
    if env:
        return env
    return os.path.dirname(_HOOKS_DIR)


# Resolve the DB path via the shared resolver (core/paths.py) so a data-dir
# move is a 1-line change. Hooks must never crash on a missing module, so any
# failure falls back to the same env-var/repo-walk resolution paths.py uses.
try:
    from paths import DB_PATH as _DB_PATH
    _OPS_DB = str(_DB_PATH)
except Exception:
    _OPS_DB = os.path.join(_fallback_root(), "data", "ops.db")

try:
    from paths import ROOT as _ROOT
    _PROJECT_DIR = str(_ROOT)
except Exception:
    _PROJECT_DIR = _fallback_root()


def _project_slug(root):
    """Claude Code slugifies the project cwd into a folder name by replacing
    ':', '\\' and '/' with '-'; the per-project memory dir lives under it."""
    import re as _re
    return _re.sub(r"[:\\/]", "-", str(root))


PATHS = {
    # The ops database (data/ops.db under the repo root, via core/paths.py).
    # Key name kept for compatibility with hooks that import PATHS directly.
    "unified_db": _OPS_DB,
    "project_dir": _PROJECT_DIR,
    # Claude Code's per-project auto-memory directory for this repo.
    "memory_dir": os.path.join(
        HOME, ".claude", "projects", _project_slug(_PROJECT_DIR), "memory"
    ),
    "hooks_dir": _HOOKS_DIR,
    "python": sys.executable,
}

# ---------------------------------------------------------------------------
# Operator identity (empty by default -- fill in at setup)
#
# OWN_EMAILS: every address the operator sends from (personal inbox + shared
#   org aliases the operator answers). Used by hooks to tell inbound from
#   outbound and to skip self-capture.
# INTERNAL_EMAIL_DOMAINS: org domains whose addresses count as internal
#   colleagues rather than external contacts.
#
# Configure in config.toml at the repo root (see config.example.toml):
#
#   [operator]
#   # emails = ["operator@example.org"]        # FICTIONAL example
#
#   [org]
#   # domain = "example.org"
#   # email_aliases = ["team@example.org"]
#
# or via env vars OPS_OWN_EMAILS / OPS_INTERNAL_DOMAINS (comma-separated).
# ---------------------------------------------------------------------------
OWN_EMAILS = set()
INTERNAL_EMAIL_DOMAINS = ()


def _load_identity_config():
    global OWN_EMAILS, INTERNAL_EMAIL_DOMAINS
    emails = set()
    domains = []
    try:
        cfg_path = os.path.join(_PROJECT_DIR, "config.toml")
        if os.path.exists(cfg_path):
            import tomllib
            with open(cfg_path, "rb") as f:
                cfg = tomllib.load(f)
            operator = cfg.get("operator", {}) or {}
            org = cfg.get("org", {}) or {}
            emails |= {
                str(e).strip().lower()
                for e in (operator.get("emails") or []) if str(e).strip()
            }
            emails |= {
                str(e).strip().lower()
                for e in (org.get("email_aliases") or []) if str(e).strip()
            }
            dom = str(org.get("domain") or "").strip().lower().lstrip("@")
            if dom:
                domains.append(dom)
    except Exception:
        pass  # fail-open: hooks must never crash on a bad/missing config file
    try:
        emails |= {
            e.strip().lower()
            for e in os.environ.get("OPS_OWN_EMAILS", "").split(",") if e.strip()
        }
        domains += [
            d.strip().lower().lstrip("@")
            for d in os.environ.get("OPS_INTERNAL_DOMAINS", "").split(",") if d.strip()
        ]
    except Exception:
        pass
    OWN_EMAILS = emails
    INTERNAL_EMAIL_DOMAINS = tuple(dict.fromkeys(domains))


_load_identity_config()

# Session ID: 6 hex chars, cached per process
_session_id = None


def set_session_id(full_id):
    """Prime the cache from a hook JSON payload's session_id field.
    Call from the hook dispatcher before any hook logic runs. No-op if called
    with empty string; idempotent if called twice with the same value."""
    global _session_id
    if not full_id:
        return
    _session_id = str(full_id).replace("-", "")[:6]


def get_session_id():
    """Get or generate a 6-char session ID. Cached after first call.

    Resolution order:
    1. set_session_id() cache (primed from stdin JSON by the hook dispatcher).
    2. CLAUDE_SESSION_ID env var (Claude Code does NOT set this currently).
    3. Random 24-bit fallback (means the dispatcher forgot to call set_session_id).
    """
    global _session_id
    if _session_id:
        return _session_id
    full_id = os.environ.get("CLAUDE_SESSION_ID", "")
    if full_id:
        _session_id = full_id.replace("-", "")[:6]
    else:
        import random
        _session_id = format(random.getrandbits(24), "06x")
    return _session_id


def get_conn(busy_timeout_ms: int = 5000):
    """Return a WAL-mode SQLite connection to the ops DB with Row factory.

    busy_timeout_ms: PreToolUse hooks running inside a 5s harness budget must
    pass a sub-2s value so DB contention can't eat the whole budget.
    """
    if _db is not None:
        conn = _db.connect(PATHS["unified_db"], timeout=max(1, busy_timeout_ms / 1000))
    else:
        # fallback loses only PRAGMA foreign_keys=ON; the pragmas below cover the rest
        conn = sqlite3.connect(PATHS["unified_db"], timeout=max(1, busy_timeout_ms / 1000))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=%d" % int(busy_timeout_ms))
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-65536")      # 64MB
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")     # 256MB
    return conn
