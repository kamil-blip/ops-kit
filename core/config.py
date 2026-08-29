"""Operator configuration loader for the ops-kit kit.

Reads <ROOT>/config.toml (copy config.example.toml to config.toml and fill it
in). Stdlib only (tomllib). A missing or malformed config never raises:
callers get documented defaults and the system degrades gracefully.

Documented keys (flat name -> where config.example.toml keeps it):
  operator_name        str    display name of the operator      [operator] name
  operator_emails      list   every address that counts as
                              "ours" (own inbox + shared
                              aliases the operator answers)     [operator] emails
  org_domain           str    the operator's org email domain   [org] domain
  vip_contacts         list   addresses given the priority
                              inbox lane                        [contacts] vip
  keyring_service      str    OS keyring service name for API
                              keys (default: "ops-kit")         [keys] keyring_service
  manager_person_ids   list   people.id values for the
                              operator's manager(s); drives
                              stakeholder/urgency scoring       [contacts] manager_person_ids
  discord_guild_ids    list   Discord guild (server) ids to
                              sync                              [sync] discord_guild_ids
  notion_databases     table  Notion database id mapping for
                              sync                              [sync] notion_databases
  drafts_dir           str    where outbound drafts are written
                              (relative resolves against ROOT)  [paths] drafts_dir
  gmail_db_path        str    optional external gmail-to-sqlite
                              mirror (paths.py: GMAIL_DB_PATH)  [sync] gmail_db_path

Both spellings work: get("operator_emails") and get("operator.emails") return
the same value. Dotted keys address any other section directly.

Example config.toml (FICTIONAL values; see config.example.toml for the full
commented template):

    [operator]
    name = "Jane Doe"
    emails = ["jane@example.org", "ops@example.org"]

    [org]
    domain = "example.org"

    [contacts]
    vip = ["director@example.org"]
    manager_person_ids = [1]

    [keys]
    keyring_service = "ops-kit"

Usage:

    import config
    aliases = config.get("operator_emails")          # [] until configured
    service = config.get("keyring_service")          # "ops-kit" default
    dbs = config.section("notion_databases")         # {} until configured
"""
from __future__ import annotations

from pathlib import Path

try:
    from paths import ROOT
except ImportError:
    # Flat-import fallback: paths.py sits next to this file.
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from paths import ROOT

CONFIG_PATH: Path = ROOT / "config.toml"

# Documented flat key -> its home in the sectioned config.example.toml layout.
_ALIASES: dict = {
    "operator_name": "operator.name",
    "operator_emails": "operator.emails",
    "org_domain": "org.domain",
    "vip_contacts": "contacts.vip",
    "keyring_service": "keys.keyring_service",
    "manager_person_ids": "contacts.manager_person_ids",
    "discord_guild_ids": "sync.discord_guild_ids",
    "notion_databases": "sync.notion_databases",
    "drafts_dir": "paths.drafts_dir",
    "gmail_db_path": "sync.gmail_db_path",
}

# Empty-but-usable defaults so callers never need their own None-guards.
DEFAULTS: dict = {
    "operator_name": "",
    "operator_emails": [],
    "org_domain": "",
    "vip_contacts": [],
    "keyring_service": "ops-kit",
    "manager_person_ids": [],
    "discord_guild_ids": [],
    "notion_databases": {},
    "drafts_dir": "",
    "gmail_db_path": "",
}

_MISSING = object()
_cache: dict | None = None


def load(force: bool = False) -> dict:
    """Parse config.toml once and cache it. force=True re-reads from disk."""
    global _cache
    if _cache is not None and not force:
        return _cache
    data: dict = {}
    if CONFIG_PATH.is_file():
        try:
            import tomllib
            with open(CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            data = {}  # a malformed config must never break callers
    _cache = data
    return data


def _walk(key: str):
    """Follow a dotted path through the parsed config; _MISSING if absent."""
    node = load()
    for part in str(key).split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return _MISSING
    return node


def get(key: str, default=_MISSING):
    """Config lookup by flat documented key or dotted path.

    Resolution: the key's own path in config.toml; else its documented
    section alias (e.g. "operator_emails" -> [operator] emails); else the
    explicit `default` argument if given; else DEFAULTS for documented keys;
    else None. Values present in config.toml are returned as-is even when
    empty ("" / []): an intentionally blanked key does not fall through to
    a default.
    """
    val = _walk(key)
    if val is _MISSING and key in _ALIASES:
        val = _walk(_ALIASES[key])
    if val is not _MISSING:
        return val
    if default is not _MISSING:
        return default
    return DEFAULTS.get(key)


def section(name: str) -> dict:
    """Return a config table (e.g. [sync].notion_databases or any [section])
    as a dict; {} if absent or not a table."""
    val = get(name, {})
    return val if isinstance(val, dict) else {}


def drafts_dir() -> Path | None:
    """Resolved drafts directory (relative values resolve against ROOT);
    None until configured."""
    val = get("drafts_dir")
    if not val:
        return None
    p = Path(val)
    return p if p.is_absolute() else ROOT / p
