"""Single source of truth for repo paths.

Resolution order for the repo root (ROOT):
  1. OPS_ROOT environment variable
  2. Walk up from this file to the first directory containing
     config.example.toml or a db/ directory (the repo-root markers).

Everything else derives from ROOT:
  DATA_DIR        <ROOT>/data          (override: OPS_DATA_DIR env var)
  DB_PATH         <DATA_DIR>/ops.db    (the canonical SQLite database, WAL mode)
  VEC_DB_PATH     <DATA_DIR>/vec.db    (sqlite-vec embedding store, 2-file layout)
  BACKUPS_DIR     <DATA_DIR>/backups
  PLAN_STATE_DIR  <DATA_DIR>/plan-state
  DRAFTS_DIR      where outbound draft files land (env OPS_DRAFTS_DIR or
                  config.toml drafts_dir; else <DATA_DIR>/drafts)
  GMAIL_DB_PATH   optional external gmail-to-sqlite mirror; None when not
                  configured (env OPS_GMAIL_DB or config.toml gmail_db_path)
  PYTHON          interpreter for scheduled-task launchers spawning children
                  (env OPS_PYTHON or config.toml python; else sys.executable)

Stdlib only. No side effects on import beyond reading the optional
<ROOT>/config.toml. Any future move of the data directory is a one-line
change (or an env var).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT_MARKERS = ("config.example.toml", "db")


def _resolve_root() -> Path:
    env = os.environ.get("OPS_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config.example.toml").is_file() or (parent / "db").is_dir():
            return parent
    # Last resort: assume this file lives one level below the repo root
    # (e.g. <root>/core/paths.py).
    return here.parent.parent


ROOT: Path = _resolve_root()
DATA_DIR: Path = Path(os.environ.get("OPS_DATA_DIR") or (ROOT / "data"))
DB_PATH: Path = DATA_DIR / "ops.db"
VEC_DB_PATH: Path = DATA_DIR / "vec.db"
BACKUPS_DIR: Path = DATA_DIR / "backups"
PLAN_STATE_DIR: Path = DATA_DIR / "plan-state"


def _read_config() -> dict:
    """Minimal read of <ROOT>/config.toml (full loader lives in config.py;
    this module stays dependency-free to avoid import cycles)."""
    cfg_path = ROOT / "config.toml"
    if not cfg_path.is_file():
        return {}
    try:
        import tomllib
        with open(cfg_path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}  # a malformed config must never break callers


_cfg = _read_config()


def _cfg_value(key: str, sections: tuple[str, ...] = ("paths", "sync")):
    """Look up a key at the top level, then under the given config sections
    (config.example.toml keeps path-ish keys under [paths] and [sync])."""
    val = _cfg.get(key)
    if val:
        return val
    for name in sections:
        section = _cfg.get(name)
        if isinstance(section, dict) and section.get(key):
            return section[key]
    return None


def _resolve_gmail_db() -> Path | None:
    env = os.environ.get("OPS_GMAIL_DB")
    if env:
        return Path(env)
    val = _cfg_value("gmail_db_path")
    return Path(val) if val else None


# External gmail-to-sqlite mirror (lives outside the repo tree). Optional:
# None when the operator has not set it up.
GMAIL_DB_PATH: Path | None = _resolve_gmail_db()
GMAIL_DATA_DIR: Path | None = GMAIL_DB_PATH.parent if GMAIL_DB_PATH else None


def _resolve_drafts_dir() -> Path:
    """Directory where outbound draft files land (scanned by the session
    changelog hook). Relative config values resolve against ROOT."""
    env = os.environ.get("OPS_DRAFTS_DIR")
    if env:
        return Path(env)
    val = _cfg_value("drafts_dir")
    if val:
        p = Path(val)
        return p if p.is_absolute() else ROOT / p
    return DATA_DIR / "drafts"


DRAFTS_DIR: Path = _resolve_drafts_dir()


def _resolve_python() -> Path:
    """Interpreter for scheduled-task launchers / orchestrators spawning
    children. Overridable because scheduled tasks and stubs may run under a
    different interpreter than the one child jobs must spawn."""
    env = os.environ.get("OPS_PYTHON")
    if env:
        return Path(env)
    val = _cfg_value("python")
    if val:
        return Path(val)
    return Path(sys.executable)


PYTHON: Path = _resolve_python()
