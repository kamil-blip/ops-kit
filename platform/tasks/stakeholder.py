"""Stakeholder derivation for action_items.

Maps every item to a (stakeholder_tier, partner_kind, is_manager_explicit)
triple so the urgency scorer can weight the manager / partners / live events
properly.

Tier scale:
    0  Manager explicit-tag (highest)   - body explicitly addresses the operator with an ask
    1  Manager general                  - from / cc / mentioned
    2  Partner                          - external collaborator (role- or keyword-matched)
    3  Current/imminent event contact   - tied to an upcoming or running tracked event
    4  Other                            - internal infra, past contact, unknown

Stage boost (added in compute_urgency):
    T-0 to T-7  : +12     T-8 to T-14 : +8     T-15 to T-30 : +4
    Event live  : +16     T+0 to T+14 (post): +3    Otherwise   : 0

Everything identity- or domain-specific is config-driven (config.toml). With
an empty config every list is empty and the module degrades cleanly: no
manager tier fires, no partner keywords match, and the stage boost is 0.
Keys read (all optional):

    [contacts]
    manager_person_ids     = []   # people.id values for the manager tier
    manager_email_patterns = []   # substrings matched against sender email
    manager_name_tokens    = []   # lowercase first-name tokens, e.g. ["alexis"]  (FICTIONAL)
    manager_title_tokens   = []   # role titles that imply the manager, e.g. ["ceo"]

    [stakeholder]
    partner_roles          = []   # roles table values that mark a partner, e.g. ["advisor", "reviewer"]
    participant_roles      = ["participant"]
    partner_role_keywords  = []   # description keywords implying a partner, e.g. ["advisor", "panelist"]
    sponsor_phrases        = []   # phrases implying sponsor/partner-org, e.g. ["official partner", "sponsorship"]
    sponsor_org_tokens     = []   # partner-org names, e.g. ["acme institute"]  (FICTIONAL)
    internal_desc_hints    = []   # verbs marking internal process work, e.g. ["rebuild ", "regenerate", "audit "]
    events_table           = "events"        # optional table with slug/start_date/end_date columns
    roles_table            = "person_roles"  # optional table with person_id/role columns

The events and roles tables are feature-detected: when absent (the starter
kit ships neither), the stage boost and the role-based partner tier simply
degrade to 0 / no-match instead of erroring.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Iterable, Optional

try:
    import config  # core/config.py; on sys.path via the installer's .pth
except ImportError:
    # Direct-invocation fallback: walk up from this file to the repo root and
    # put core/ on sys.path (the installed venv normally does this via a .pth).
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(next(
        p / "core" for p in _Path(__file__).resolve().parents
        if (p / "core" / "config.py").is_file()
    )))
    import config


def _cfg_list(key: str, default: Optional[list] = None) -> list[str]:
    val = config.get(key, default if default is not None else [])
    if not isinstance(val, (list, tuple)):
        return []
    return [str(v).strip().lower() for v in val if str(v).strip()]


# ---------------------------------------------------------------------------
# Manager identity (Tier 0/1). All empty by default; fill config.toml.
# ---------------------------------------------------------------------------
MANAGER_PERSON_IDS: set[int] = {
    int(v) for v in (config.get("manager_person_ids") or []) if str(v).strip().isdigit()
}
MANAGER_EMAIL_PATTERNS: tuple[str, ...] = tuple(_cfg_list("contacts.manager_email_patterns"))
MANAGER_NAME_TOKENS: list[str] = _cfg_list("contacts.manager_name_tokens")
MANAGER_TITLE_TOKENS: list[str] = _cfg_list("contacts.manager_title_tokens")
# The operator's own name tokens ("@ <name>" in an item body counts as an
# explicit tag on the operator).
OPERATOR_NAME_TOKENS: list[str] = [
    t for t in str(config.get("operator_name") or "").lower().split() if len(t) > 1
]


def _compile_manager_patterns() -> tuple[list[re.Pattern], list[re.Pattern]]:
    explicit: list[re.Pattern] = []
    general: list[re.Pattern] = []
    for tok in MANAGER_NAME_TOKENS:
        t = re.escape(tok)
        explicit.append(re.compile(
            rf"{t}\s+(asked|wants|needs|says|tagged|requested|tagged me|pinged|mentioned)", re.I))
        explicit.append(re.compile(rf"^\s*{t}[:\s]", re.I))
        explicit.append(re.compile(rf"\bcc\s*[:=]\s*{t}", re.I))
        general.append(re.compile(rf"\b{t}\b", re.I))
    for tok in OPERATOR_NAME_TOKENS:
        explicit.append(re.compile(rf"@\s*{re.escape(tok)}\b", re.I))
    explicit.append(re.compile(r"@\s*you\b", re.I))
    for title in MANAGER_TITLE_TOKENS:
        general.append(re.compile(rf"\b{re.escape(title)}\b", re.I))
    return explicit, general


MANAGER_EXPLICIT_PATTERNS, MANAGER_GENERAL_PATTERNS = _compile_manager_patterns()

# ---------------------------------------------------------------------------
# Partner / sponsor vocabulary (Tier 2). Empty by default; operator-fillable.
# ---------------------------------------------------------------------------
PARTNER_ROLES: set[str] = set(_cfg_list("stakeholder.partner_roles"))
PARTICIPANT_ROLES: set[str] = set(_cfg_list("stakeholder.participant_roles", ["participant"]))
# Words that strongly imply a partner relationship even without a person link.
PARTNER_ROLE_KEYWORDS: list[str] = _cfg_list("stakeholder.partner_role_keywords")
SPONSOR_PHRASES: list[str] = _cfg_list("stakeholder.sponsor_phrases")
SPONSOR_ORG_TOKENS: list[str] = _cfg_list("stakeholder.sponsor_org_tokens")

INTERNAL_SOURCE_TYPES = {"internal", "session", "manual", "wrap-up-confirmed", "wrap-up", "umbrella"}
# Description hints marking internal process work (so "regenerate the X"
# doesn't masquerade as partner work). Generic verbs only, e.g.
# ["patch ", "audit ", "rebuild ", "regenerate", "investigate "].
INTERNAL_DESC_HINTS: list[str] = _cfg_list("stakeholder.internal_desc_hints")

# ---------------------------------------------------------------------------
# Optional domain tables (feature-detected; the starter kit ships neither).
# ---------------------------------------------------------------------------
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _cfg_table(key: str, default: str) -> str:
    val = str(config.get(key, "") or default)
    return val if _NAME_RE.match(val) else default


EVENTS_TABLE = _cfg_table("stakeholder.events_table", "events")
ROLES_TABLE = _cfg_table("stakeholder.roles_table", "person_roles")


def _table_exists(conn, name: str) -> bool:
    """Feature-detect a table/view; optional tables degrade gracefully."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)).fetchone()
    return row is not None


def _normalize_role(role: Optional[str]) -> str:
    if not role:
        return ""
    return role.strip().lower().replace("_", "-")


def _phrase_hits(phrase: str, desc_l: str) -> bool:
    """Multi-word phrases match as substrings; single words need a word
    boundary (so a short token can't fire inside a longer word)."""
    p = phrase.strip().lower()
    if not p:
        return False
    if " " in p or "-" in p:
        return p in desc_l
    return re.search(rf"\b{re.escape(p)}\b", desc_l) is not None


def _looks_manager(person_id: Optional[int], email: Optional[str]) -> bool:
    if person_id and person_id in MANAGER_PERSON_IDS:
        return True
    if email:
        e = email.lower()
        if any(p in e for p in MANAGER_EMAIL_PATTERNS):
            return True
    return False


def get_active_events(conn, now: Optional[datetime] = None, lookback_days: int = 14, lookahead_days: int = 60) -> dict[str, dict]:
    """Return {slug: {start_dt, end_dt}} for events that count as current/imminent.

    Reads the optional events table (config: [stakeholder] events_table;
    expected columns: slug, start_date, end_date). Returns {} when the table
    is absent, so the stage boost degrades to 0 on a fresh install.

    An event is active if:
      - end_date is within lookback_days behind now (post-event ops still warm)
      - start_date is within lookahead_days ahead of now (planning phase)
    """
    if now is None:
        now = datetime.now()
    if not _table_exists(conn, EVENTS_TABLE):
        return {}
    try:
        rows = conn.execute(
            f"SELECT slug, start_date, end_date FROM {EVENTS_TABLE} "
            "WHERE start_date IS NOT NULL OR end_date IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        # Table exists but doesn't have the expected columns: degrade to 0.
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        slug = row["slug"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
        start = (row["start_date"] if hasattr(row, "keys") else row[1]) or None
        end = (row["end_date"] if hasattr(row, "keys") else row[2]) or None
        try:
            start_dt = datetime.strptime(start[:10], "%Y-%m-%d") if start else None
        except ValueError:
            start_dt = None
        try:
            end_dt = datetime.strptime(end[:10], "%Y-%m-%d") if end else None
        except ValueError:
            end_dt = None
        anchor = start_dt or end_dt
        if not anchor:
            continue
        if end_dt is None:
            end_dt = start_dt
        days_to_start = (start_dt - now).days if start_dt else None
        days_after_end = (now - end_dt).days if end_dt else None
        active = False
        if days_to_start is not None and -1 <= days_to_start <= lookahead_days:
            active = True
        if days_after_end is not None and 0 <= days_after_end <= lookback_days:
            active = True
        if active:
            out[slug] = {"start_dt": start_dt, "end_dt": end_dt}
    return out


# Backward-compatible alias: callers written against the pre-refactor API
# (e.g. task_manager's urgency recompute) may import this name.
get_active_hackathons = get_active_events


def compute_stage_boost(slug: Optional[str], active_events: dict[str, dict], now: Optional[datetime] = None) -> float:
    """Return +0 to +16 boost based on how close we are to the event."""
    if not slug or slug not in active_events:
        return 0.0
    if now is None:
        now = datetime.now()
    info = active_events[slug]
    start = info.get("start_dt")
    end = info.get("end_dt") or start
    if not start:
        return 0.0
    if end and start <= now <= end:
        return 16.0  # event live, all-hands
    days_to_start = (start - now).days
    if 0 <= days_to_start <= 3:
        return 14.0  # final sprint
    if 4 <= days_to_start <= 7:
        return 12.0
    if 8 <= days_to_start <= 14:
        return 8.0
    if 15 <= days_to_start <= 30:
        return 4.0
    if 31 <= days_to_start <= 60:
        return 2.0
    days_after_end = (now - (end or start)).days
    if 0 <= days_after_end <= 14:
        return 3.0
    return 0.0


def _person_role_lookup(conn, person_ids: Iterable[int]) -> dict[int, list[str]]:
    """Roles per person from the optional roles table; {} when absent."""
    out: dict[int, list[str]] = {}
    ids = [pid for pid in person_ids if pid]
    if not ids or not _table_exists(conn, ROLES_TABLE):
        return out
    placeholders = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"SELECT person_id, role FROM {ROLES_TABLE} WHERE person_id IN ({placeholders})",
            ids,
        ).fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        pid = row["person_id"] if hasattr(row, "keys") else row[0]
        role = _normalize_role(row["role"] if hasattr(row, "keys") else row[1])
        out.setdefault(pid, []).append(role)
    return out


def _sender_email(conn, person_id: Optional[int]) -> Optional[str]:
    if not person_id:
        return None
    row = conn.execute("SELECT email FROM people WHERE id=?", (person_id,)).fetchone()
    if not row:
        return None
    return (row["email"] if hasattr(row, "keys") else row[0]) or None


def derive_stakeholder(item: dict, conn, active_events: Optional[dict[str, dict]] = None,
                       active_hackathons: Optional[dict[str, dict]] = None) -> tuple[int, str, int]:
    """Return (tier, partner_kind, is_manager_explicit) for an action item dict.

    Falls through tiers in priority order; first match wins.
    (`active_hackathons` is a deprecated alias for `active_events`.)
    """
    if active_events is None:
        active_events = active_hackathons
    description = (item.get("description") or "")
    desc_l = description.lower()

    # ------------------------------------------------------------------
    # Tier 0 / 1, the manager
    # ------------------------------------------------------------------
    sender_pid = item.get("source_person_id")
    sender_email = _sender_email(conn, sender_pid) if sender_pid else None
    manager_source = _looks_manager(sender_pid, sender_email)
    manager_explicit = False
    if manager_source:
        # The manager is the sender AND the description includes any
        # directly-addressed pattern
        for pat in MANAGER_EXPLICIT_PATTERNS:
            if pat.search(description):
                manager_explicit = True
                break
        # Also treat first-person ask language as explicit (e.g. "<manager> asked us to ...")
    if not manager_explicit:
        for pat in MANAGER_EXPLICIT_PATTERNS:
            if pat.search(description):
                manager_explicit = True
                break
    manager_named = any(tok in desc_l for tok in MANAGER_NAME_TOKENS)
    if manager_explicit and (manager_source or manager_named):
        return (0, "manager", 1)
    if manager_source:
        return (1, "manager", 0)
    # Mention without sender attribution, only Tier 1 if clearly directed
    if any(p.search(description) for p in MANAGER_GENERAL_PATTERNS) and ("ask" in desc_l or "needs" in desc_l or "want" in desc_l or "told" in desc_l):
        return (1, "manager", 0)

    # ------------------------------------------------------------------
    # Tier 2, Partner via the roles table (highest-confidence)
    # ------------------------------------------------------------------
    pids: list[int] = []
    for key in ("source_person_id", "about_person_id"):
        v = item.get(key)
        if v and v not in pids:
            pids.append(v)
    roles_by_pid = _person_role_lookup(conn, pids)
    partner_role_order = [r for r in _cfg_list("stakeholder.partner_roles")]  # config order = preference
    best_partner_role: Optional[str] = None
    has_participant_role_only = False
    for pid, roles in roles_by_pid.items():
        rset = set(roles)
        partner_match = rset & PARTNER_ROLES
        if partner_match:
            for preferred in partner_role_order:
                if preferred in partner_match:
                    best_partner_role = preferred
                    break
            else:
                best_partner_role = sorted(partner_match)[0]
            break
        if rset & PARTICIPANT_ROLES:
            has_participant_role_only = True
    if best_partner_role:
        return (2, best_partner_role, 0)

    # ------------------------------------------------------------------
    # Tier 2, Partner via description-keyword fallback
    # When the item is about a configured partner role even without a person
    # link. Check internal-process hints FIRST so "regenerate the X" doesn't
    # masquerade as partner.
    # ------------------------------------------------------------------
    is_internal_process = any(h in desc_l for h in INTERNAL_DESC_HINTS)
    if not is_internal_process:
        for kw in PARTNER_ROLE_KEYWORDS:
            if _phrase_hits(kw, desc_l):
                return (2, _normalize_role(kw) or "partner", 0)

    # Sponsor / partner-org check
    if item.get("source_type") in ("sponsor-money", "partner_ask"):
        return (2, "sponsor", 0)
    if item.get("org_entity_id"):
        return (2, "sponsor", 0)
    if not is_internal_process:
        for phrase in SPONSOR_PHRASES:
            if _phrase_hits(phrase, desc_l):
                return (2, "sponsor", 0)
        for token in SPONSOR_ORG_TOKENS:
            if _phrase_hits(token, desc_l):
                return (2, "sponsor", 0)

    # ------------------------------------------------------------------
    # Tier 3, Current / imminent event contact
    # An internal-process task TIED TO AN ACTIVE EVENT is still event-tier;
    # the "internal" label only kicks in when there is no active event link.
    # ------------------------------------------------------------------
    active = active_events if active_events is not None else get_active_events(conn)
    slug = item.get("context_slug") or ""
    if slug in active:
        return (3, "participant", 0)
    for s in active.keys():
        slug_words = s.replace("-", " ").lower()
        if slug_words in desc_l:
            return (3, "participant", 0)
    # Some action items mention the event by short name.
    # Match a short token against active event slug prefixes.
    active_prefixes = {s.split("-")[0] for s in active.keys()}
    for prefix in active_prefixes:
        if re.search(rf"\b{re.escape(prefix)}\b", desc_l):
            return (3, "participant", 0)

    # ------------------------------------------------------------------
    # Tier 4, Internal / unknown / past
    # ------------------------------------------------------------------
    src = (item.get("source_type") or "").lower()
    if src in INTERNAL_SOURCE_TYPES or is_internal_process:
        return (4, "internal", 0)
    if has_participant_role_only:
        return (4, "participant", 0)
    return (4, "unknown", 0)
