"""Validators for core DB insert paths (ingest hardening).

    validate_action_item(row)                     -> (ok, errors[])
    route_action_item(source)                     -> 'canonical' | 'inbox'
    propose_to_inbox(conn, source, description, ...) -> (inbox_id, skip_reason)
    log_rejection(conn, source, target, errors, row, context=None)

Every insert into action_items should route through the relevant validator.
Rejections land in the ingest_rejections table for observability.
`ensure_table(conn)` bootstraps that table idempotently.

Usage:
    from validators import (
        validate_action_item, log_rejection, ensure_table,
    )
    ensure_table(conn)
    ok, errors = validate_action_item(row)
    if not ok:
        log_rejection(conn, "brief.apply", "action_items", errors, row)
        continue
    conn.execute("INSERT INTO action_items ...", (...))
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

# audit_tools ships in logging/ (flat import via the installer's .pth).
# Degrade gracefully when that capability directory is not installed.
try:
    from audit_tools import OPERATOR_ALIASES, resolve_person  # noqa: F401
except Exception:
    OPERATOR_ALIASES: tuple = ()

    def resolve_person(_q: str) -> dict[str, Any] | None:  # type: ignore[misc]
        return None


def _load_operator_person_id() -> int | None:
    """The operator's own people.id for "the operator typed this" provenance.
    Set in config.toml:  [contacts] self_person_ids = [1]
    None until configured; provenance columns then stay NULL."""
    try:
        import config
        ids = config.get("contacts.self_person_ids", None) or []
        return int(ids[0]) if ids else None
    except Exception:
        return None


OPERATOR_PERSON_ID = _load_operator_person_id()


# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────
VALID_PRIORITIES = {"P0", "P1", "P2", "P3", "P4"}


def normalize_priority(p):
    """Return the canonical Pn form of a priority, else raise. None -> None.
    Strips + uppercases so 'p1' / ' P2 ' canonicalize; a BARE numeric
    ('1'/'2'/'3') or anything else -> ValueError (we do NOT guess a
    1 <-> P0/P1 mapping -- that is a human decision). This shared normalizer
    closes the write path where a `priority='P1'` filter silently drops a
    row storing a non-canonical value."""
    if p is None:
        return None
    s = str(p).strip().upper()
    if re.fullmatch(r"P[0-4]", s):
        return s
    raise ValueError("priority_needs_canonical:" + str(p))


VALID_AI_STATUSES = {
    "OPEN", "WAITING", "BLOCKED", "DONE", "CANCELLED", "REMOVED",
    "SNOOZED", "IN_PROGRESS",
}
MIN_DESCRIPTION_LEN = 20

# Shell-command head detector: a description that starts like a command line
# is a dispatcher bug, not a task.
_SHELL_PREFIX_RE = re.compile(
    r"^\s*(?:cd\s|python3?\s|git\s|SELECT\s|INSERT\s|UPDATE\s|DELETE\s|"
    r"ls\s|ls$|grep\s|curl\s|wget\s|pip\s|npm\s|bash\s|sh\s|sudo\s|"
    r"echo\s|cat\s|sqlite3\s|chmod\s|mkdir\s|rm\s|mv\s|cp\s|ssh\s|scp\s|"
    r"PRAGMA\s|\$\s|\{|\[|>\s|>>\s|\|)",
    re.IGNORECASE,
)

# Any non-manual source_url must look like a real link.
_VALID_SOURCE_URL_PATTERNS = [
    re.compile(r"^https://mail\.google\.com/mail/u/0/#inbox/[0-9a-f]+$"),
    re.compile(r"^https://discord\.com/channels/(@me|\d+)/\d+/\d+$"),
    re.compile(r"^https://notion\.so/[0-9a-f]+$"),
    re.compile(r"^https?://"),
    # Session provenance: explicit internal locator (NOT a web URL; resolved
    # against conversation_history). Only minted by build_source_url after
    # verify_session_quote passed.
    re.compile(r"^internal://conversation_history/[0-9a-f-]+$"),
]
_URL_EXEMPT_SOURCES = frozenset({
    "manual", "operator", "operator-verbal", "template_step",
    "inbox-promoted", "wrap-up-confirmed", "scan-confirmed",
    # Dated/operator-meta sources are also exempt. `startswith` matching:
    # "wrap-up-" covers wrap-up-confirmed AND wrap-up-2026-05-13 etc;
    # "audit-" covers audit-<topic>-<date> tags.
    "wrap-up-", "audit-",
})


# ────────────────────────────────────────────────────────────────────────
# Validators
# ────────────────────────────────────────────────────────────────────────
def validate_action_item(row: dict) -> tuple[bool, list[str]]:
    """Check an action_items row before insert. Returns (ok, errors).

    Rules:
      - description present, >= MIN_DESCRIPTION_LEN chars, not a shell head
      - priority (if set) in VALID_PRIORITIES
      - status (if set) in VALID_AI_STATUSES
      - waiting_on (if set, not the null literal) must resolve via audit_tools
    """
    errors: list[str] = []

    desc = (row.get("description") or "").strip()
    if not desc:
        errors.append("description_missing")
    else:
        if len(desc) < MIN_DESCRIPTION_LEN:
            errors.append(f"description_too_short(<{MIN_DESCRIPTION_LEN})")
        if _SHELL_PREFIX_RE.match(desc[:100]):
            errors.append("description_shell_command_pattern")

    priority = row.get("priority")
    if priority is not None:
        try:
            normalize_priority(priority)  # 'p1'/' P2 ' canonicalize; bare-numeric raises
        except ValueError:
            errors.append(f"priority_invalid:{priority}")

    status = row.get("status")
    if status is not None and str(status).upper() not in VALID_AI_STATUSES:
        errors.append(f"status_invalid:{status}")

    waiting_on = row.get("waiting_on")
    if waiting_on and str(waiting_on).strip().lower() not in ("null", "none", ""):
        try:
            resolved = resolve_person(str(waiting_on))
        except sqlite3.Error:
            # Uninitialized/absent DB: nothing can resolve, same verdict as
            # an unknown person (fresh-install safe).
            resolved = None
        if not resolved:
            errors.append(f"waiting_on_unresolvable:{waiting_on!r}")

    return (not errors), errors


# ────────────────────────────────────────────────────────────────────────
# Trust gate: routes action items to canonical table or inbox staging.
# ────────────────────────────────────────────────────────────────────────
# Sources whose items go directly to action_items (consciously assented to
# by the operator). Everything else routes to action_items_inbox for review.
CANONICAL_SOURCES = frozenset({
    "manual",                 # CLI: task_manager.py add
    "operator",               # alias for manual entry
    "operator-verbal",        # added during conversation at the operator's request
    "template_step",          # runbook/template step instantiation
    "inbox-promoted",         # promoted from action_items_inbox via review
    "wrap-up-confirmed",      # wrap-up tool only after the operator confirmed selection
})

# Patterns that should never become action items even as proposals.
# Filters out the most common LLM hallucination shapes from chat threads
# where the conversation is already live.
_REFUSE_PATTERNS = [
    re.compile(r"^\s*confirm\s+(the\s+)?(meeting|talk|time|details|format|duration)", re.I),
    re.compile(r"^\s*follow[\s-]up\s+with\s+\w+\s*$", re.I),
    re.compile(r"^\s*(critical\s+)?overdue\s+task", re.I),
    re.compile(r"^\s*(a\s+)?critical\s+overdue\s+task\s+for\s+(a\s+)?user\s*\(id", re.I),
]


def route_action_item(source: str | None) -> str:
    """Return 'canonical' if source is trusted, 'inbox' otherwise.

    Used by every action_items write path. The trust line is intentionally
    strict: only sources representing direct operator assent reach canonical.
    Auto-extraction (brief.py classify, meeting notes, handlers) lands in the
    inbox for human review.
    """
    if not source:
        return "inbox"
    s = str(source).strip().lower()
    return "canonical" if s in CANONICAL_SOURCES else "inbox"


def is_refused_proposal(description: str | None) -> tuple[bool, str | None]:
    """Pre-inbox filter for low-signal proposals. Returns (refused, pattern_name).

    These descriptions match common LLM hallucination shapes from live email
    threads where the action is already happening in-thread.
    """
    if not description:
        return True, "empty_description"
    text = description.strip()[:200]
    for pat in _REFUSE_PATTERNS:
        if pat.search(text):
            return True, pat.pattern
    return False, None


def _normalize_for_match(text: str) -> str:
    """Collapse whitespace + lowercase for forgiving substring match."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def is_fabricated_quote(
    conn: sqlite3.Connection,
    evidence_quote: str | None,
    source_type: str | None,
    source_id: str | None,
) -> tuple[bool, str | None]:
    """Return (fabricated, reason) if the evidence_quote is NOT a substring of
    the source body.

    Only checks when source_type and source_id are present and resolvable. If
    the source can't be fetched (unknown type, NULL id, row missing), returns
    (False, None): don't refuse on lack-of-evidence, only on contradicted
    evidence.

    Catches LLM hallucination patterns where the classifier invents a quote.
    """
    if not evidence_quote or not source_type or not source_id:
        return False, None
    quote = _normalize_for_match(evidence_quote)
    if len(quote) < 20:
        # Too short to verify reliably; let it through.
        return False, None

    body = None
    if source_type == "email":
        # source_id is the gmail thread_id. Pull all messages in thread.
        rows = conn.execute(
            "SELECT body FROM emails WHERE thread_id = ?", (source_id,)
        ).fetchall()
        body = " ".join(r["body"] if hasattr(r, "__getitem__") else r[0] for r in rows if r and (r["body"] if hasattr(r, "__getitem__") else r[0]))
    elif source_type == "discord":
        r = conn.execute(
            "SELECT content FROM discord_messages WHERE id = ?", (source_id,)
        ).fetchone()
        body = (r["content"] if hasattr(r, "__getitem__") else r[0]) if r else None
    elif source_type == "beeper":
        r = conn.execute(
            "SELECT text FROM beeper_messages WHERE id = ?", (source_id,)
        ).fetchone()
        body = (r["text"] if hasattr(r, "__getitem__") else r[0]) if r else None
    # granola / slack / notion: skip -- body lives elsewhere, not validated yet

    if not body:
        return False, None  # source unreachable; don't refuse

    if _normalize_for_match(quote) not in _normalize_for_match(body):
        return True, f"fabricated_quote:{source_type}:{source_id}"
    return False, None


def verify_session_quote(conn, session_id: str | None, quote: str | None) -> bool:
    """Session-provenance gate. True only when `quote` appears VERBATIM
    (whitespace-normalized) in conversation_history for `session_id` (short
    6-8 hex prefixes match the stored full ids). A promise made in a chat
    session is citable provenance only if the exact words exist:
    paraphrases and fabricated quotes reject."""
    if not session_id or not quote or not str(quote).strip():
        return False
    norm_q = re.sub(r"\s+", " ", str(quote)).strip().lower()
    if len(norm_q) < 8:
        return False  # too short to be meaningful evidence
    rows = conn.execute(
        "SELECT display FROM conversation_history WHERE session_id LIKE ?",
        (str(session_id).strip() + "%",)).fetchall()
    for (display,) in rows:
        if display and norm_q in re.sub(r"\s+", " ", display).lower():
            return True
    return False


def build_source_url(source_ref: str | None) -> str | None:
    """Render a clickable URL from a structured source_ref.

    Accepted formats:
      email:<gmail_thread_id>            -> https://mail.google.com/mail/u/0/#inbox/<id>
      gmail:<gmail_thread_id>            -> same
      discord:<guild_id>:<channel_id>:<message_id>
                                          -> https://discord.com/channels/.../...
      discord:<channel_id>:<message_id>  -> same with @me as guild placeholder
      notion:<page_id>                   -> https://notion.so/<id_no_dashes>
      session:<session_id>               -> internal://conversation_history/<id>
                                            (NOT a web URL: explicit internal
                                            locator; resolve with SELECT display
                                            FROM conversation_history WHERE
                                            session_id LIKE '<id>%')
      url:<full_url>                     -> <full_url>  (passthrough)
      <full_url>                          -> <full_url>  (passthrough)

    Returns None for unrecognized formats.
    """
    if not source_ref:
        return None
    s = str(source_ref).strip()
    if not s:
        return None
    if s.startswith(("http://", "https://")):
        return s
    if ":" not in s:
        return None
    kind, _, rest = s.partition(":")
    kind = kind.lower()
    if kind in ("email", "gmail") and rest:
        return f"https://mail.google.com/mail/u/0/#inbox/{rest}"
    if kind == "discord" and rest:
        parts = rest.split(":")
        if len(parts) == 3:
            g, c, m = parts
            return f"https://discord.com/channels/{g}/{c}/{m}"
        if len(parts) == 2:
            c, m = parts
            return f"https://discord.com/channels/@me/{c}/{m}"
    if kind == "notion" and rest:
        return f"https://notion.so/{rest.replace('-', '')}"
    if kind == "session" and rest:
        return f"internal://conversation_history/{rest}"
    if kind == "url" and rest:
        return rest
    return None


VALID_PEOPLE_RELATIONS = frozenset({
    "source", "about", "affects", "involves", "cc",
})


def link_people(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: int,
    item_id_text: str | None,
    people_relations: list[tuple[int, str]] | None,
) -> int:
    """Insert action_item_people rows for an item. Returns count inserted.

    target_kind: 'canonical' or 'inbox'
    people_relations: list of (person_id, relation) tuples. Relation must be
                      in VALID_PEOPLE_RELATIONS. Duplicates are deduped via
                      INSERT OR IGNORE on the unique constraint.
    """
    if not people_relations or target_kind not in ("canonical", "inbox"):
        return 0
    inserted = 0
    for person_id, relation in people_relations:
        if not person_id or relation not in VALID_PEOPLE_RELATIONS:
            continue
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO action_item_people "
                "(target_kind, target_id, item_id_text, person_id, relation) "
                "VALUES (?, ?, ?, ?, ?)",
                (target_kind, int(target_id), item_id_text, int(person_id), relation),
            )
            if cur.rowcount > 0:
                inserted += 1
        except sqlite3.OperationalError:
            pass
    return inserted


def _infer_provenance(source: str | None, source_person_id: int | None) -> tuple[int | None, str]:
    """Derive (creator_person_id, extracted_by) from the free-text source tag.

    Auto-extracted content credits the person in the source; operator-typed
    paths credit OPERATOR_PERSON_ID (None until configured). Falls back to
    ('unknown-manual', OPERATOR_PERSON_ID) when no pattern matches.
    """
    if not source:
        return (OPERATOR_PERSON_ID, "unknown-manual")
    s = source.strip().lower()
    # Auto-extracted from third-party content -> creator is the person in the source.
    if s.startswith("granola") or s.startswith("transcript-") or s.startswith("meeting:") or "auto-extracted from meeting" in s:
        return (source_person_id, "granola-mcp")
    if s.startswith("brief") or s.startswith("daily-debrief") or "auto-extracted from email" in s or s.startswith("classifier"):
        # Distinguish model-suffixed tags: "brief.classify.<model>" ->
        # "brief-classify-<model>". Anything else collapses to the plain
        # "brief-classify" bucket.
        if s.startswith("brief.classify."):
            model = s[len("brief.classify."):].strip()
            model = model.replace(".", "-").replace("_", "-")
            if model:
                return (source_person_id, f"brief-classify-{model}")
        return (source_person_id, "brief-classify")
    if s.startswith("discord"):
        return (source_person_id, "discord-classify")
    if s.startswith("slack"):
        return (source_person_id, "slack-classify")
    if s.startswith("beeper"):
        return (source_person_id, "beeper-classify")
    # Operator-typed paths.
    if s.startswith("wrap-up") or s.startswith("session-") or s.startswith("session_") or s == "session" or s == "claude-session":
        return (OPERATOR_PERSON_ID, "wrap-up")
    if (s.startswith("manual") or s.startswith("operator") or s.startswith("audit-")
            or s.startswith("thread-review-") or s.startswith("email")
            or s == "partner_ask" or s.startswith("system-upgrade")):
        return (OPERATOR_PERSON_ID, "operator-manual")
    if s.startswith("template:"):
        return (None, "template-spawn")
    return (OPERATOR_PERSON_ID, "unknown-manual")


def propose_to_inbox(
    conn: sqlite3.Connection,
    source: str,
    description: str,
    *,
    priority: str | None = None,
    due_date: str | None = None,
    waiting_on: str | None = None,
    context_slug: str | None = None,
    context: str | None = None,
    context_tags: str | None = None,
    email_thread_id: str | None = None,
    source_evidence: str | None = None,
    evidence_quote: str | None = None,
    classifier_confidence: float | None = None,
    source_ref: str | None = None,
    source_url: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    source_person_id: int | None = None,
    about_person_id: int | None = None,
    project_id: int | None = None,
    discord_message_id: str | None = None,
    beeper_message_id: str | None = None,
    granola_meeting_id: str | None = None,
    slack_message_id: str | None = None,
    creator_person_id: int | None = None,
    extracted_by: str | None = None,
    people_relations: list[tuple[int, str]] | None = None,
) -> tuple[str | None, str | None]:
    """Insert one proposal into action_items_inbox.

    Returns (inbox_id, skip_reason). skip_reason is set when the proposal
    is refused (pattern match, dedup, etc.) and no row was inserted.

    Dedup: if email_thread_id matches an existing OPEN canonical action_item,
    the proposal is dropped (the existing item already covers this thread).
    """
    refused, pat = is_refused_proposal(description)
    if refused:
        return None, f"refused_pattern:{pat}"

    # Hardening gates: description length, context length, and source_url
    # validity. These reject contamination earlier than the quote-grounding
    # check below.
    if not description or len(description.strip()) < 20:
        return None, "description_too_short"
    if context is not None and 0 < len(context.strip()) < 50:
        return None, "context_too_short"
    src_lower = (source or "").strip().lower()
    exempt = any(src_lower.startswith(s) for s in _URL_EXEMPT_SOURCES)
    if not exempt:
        if not source_url:
            return None, "source_url_missing"
        if not any(p.match(source_url) for p in _VALID_SOURCE_URL_PATTERNS):
            return None, f"source_url_invalid_pattern:{source_url[:80]}"

    # source_type required when source_id IS present (and vice versa).
    # Non-exempt sources should always carry a typed reference; exempt
    # (manual/operator/etc.) may have neither.
    if (source_type and not source_id) or (source_id and not source_type):
        return None, "source_type_id_mismatch: source_type required when source_id present (and vice versa)"
    if not exempt and not source_type and not source_id:
        return None, "source_type_required: non-exempt source needs (source_type, source_id)"

    # Shape gate. email_thread_id and source_url must look like real ids/urls.
    # Refuse anything containing shell metacharacters, whitespace, or shell
    # prefixes: those signal a dispatcher that stamped a bash command as the ref.
    _BAD_REF_CHARS = " |;`\"'\\<>\n\t"
    if email_thread_id and isinstance(email_thread_id, str):
        if (any(c in email_thread_id for c in _BAD_REF_CHARS)
                or len(email_thread_id) > 80
                or _SHELL_PREFIX_RE.match(email_thread_id)):
            return None, "bad_email_thread_id_shape"
    if source_url and isinstance(source_url, str):
        # Real URLs don't contain spaces or pipes after the scheme.
        tail = source_url.split("://", 1)[-1]
        if any(c in tail for c in " |;`\"'\\<>\n\t"):
            return None, "bad_source_url_shape"

    # Mandatory evidence_quote when source is identifiable. Empty/too-short
    # quote = LLM hallucinated obligation without provenance. Skipped when
    # source_type+source_id are both missing (no source to quote from).
    if source_type and source_id:
        q = (evidence_quote or "").strip()
        if len(q) < 20:
            return None, "missing_evidence_quote: source identified but no verbatim quote saved"

    # Anti-fabrication: if the classifier provided an evidence_quote AND a
    # resolvable source, verify the quote actually appears in the source body.
    # Refuses LLM hallucinated quotes that the regex patterns miss.
    fab_refused, fab_reason = is_fabricated_quote(
        conn, evidence_quote, source_type, source_id
    )
    if fab_refused:
        return None, fab_reason

    # Dedup against canonical OPEN items by email_thread_id
    if email_thread_id:
        dup = conn.execute(
            "SELECT item_id FROM action_items "
            "WHERE email_thread_id = ? AND status IN ('OPEN','WAITING','BLOCKED') "
            "LIMIT 1",
            (email_thread_id,),
        ).fetchone()
        if dup:
            return None, f"dedup_canonical:{dup['item_id'] if hasattr(dup,'__getitem__') else dup[0]}"

    # Dedup against pending inbox by email_thread_id
    if email_thread_id:
        dup = conn.execute(
            "SELECT inbox_id FROM action_items_inbox "
            "WHERE suggested_email_thread_id = ? AND status='pending' "
            "LIMIT 1",
            (email_thread_id,),
        ).fetchone()
        if dup:
            return None, f"dedup_inbox:{dup['inbox_id'] if hasattr(dup,'__getitem__') else dup[0]}"

    # Generate inbox_id: AI-IN-YYYYMMDD-NNNN
    from datetime import datetime as _dt, timezone
    today = _dt.now(timezone.utc).strftime("%Y%m%d")
    max_row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(inbox_id, 18) AS INTEGER)) AS mx "
        "FROM action_items_inbox WHERE inbox_id LIKE ?",
        (f"AI-IN-{today}-%",),
    ).fetchone()
    next_seq = (max_row["mx"] if hasattr(max_row, "__getitem__") and max_row["mx"] else 0) + 1
    inbox_id = f"AI-IN-{today}-{next_seq:04d}"
    while conn.execute(
        "SELECT 1 FROM action_items_inbox WHERE inbox_id=?", (inbox_id,)
    ).fetchone():
        next_seq += 1
        inbox_id = f"AI-IN-{today}-{next_seq:04d}"

    # Auto-derive source_url from source_ref if not given explicitly
    if not source_url and source_ref:
        source_url = build_source_url(source_ref)

    # Derive stakeholder tier at capture time so the inbox row carries the same
    # signal the focus output will compute. update_all_urgency_scores re-derives
    # later as code/keywords evolve; this seeds it correctly from day 1.
    stakeholder_tier = None
    partner_kind = None
    is_manager_explicit = 0
    try:
        from stakeholder import derive_stakeholder, get_active_events
        _proxy = {
            "description": description,
            "source_person_id": source_person_id,
            "about_person_id": about_person_id,
            "context_slug": context_slug,
            "source_type": source_type,
            "org_entity_id": None,
        }
        _active = get_active_events(conn)
        stakeholder_tier, partner_kind, is_manager_explicit = derive_stakeholder(
            _proxy, conn, active_events=_active,
        )
    except Exception:
        pass

    # Infer creator + extracted_by from source tag if caller didn't pass them.
    if extracted_by is None or creator_person_id is None:
        inf_creator, inf_extracted = _infer_provenance(source, source_person_id)
        if creator_person_id is None:
            creator_person_id = inf_creator
        if extracted_by is None:
            extracted_by = inf_extracted

    # Infer source_type from whichever channel-specific id IS populated. Without
    # this, downstream tools (auto_draft, is_fabricated_quote, drafts list)
    # can't resolve the source body.
    if not source_type:
        if email_thread_id:
            source_type = "email"
        elif discord_message_id:
            source_type = "discord"
        elif beeper_message_id:
            source_type = "beeper"
        elif slack_message_id:
            source_type = "slack"
        elif granola_meeting_id:
            source_type = "granola"
        elif source_ref:
            ref_low = source_ref.lower()
            if ref_low.startswith(("email:", "gmail:")):
                source_type = "email"
            elif ref_low.startswith("discord:"):
                source_type = "discord"
            elif ref_low.startswith("beeper:"):
                source_type = "beeper"
            elif ref_low.startswith("granola:"):
                source_type = "granola"
            elif ref_low.startswith("slack:"):
                source_type = "slack"
        elif source:
            src_low = source.lower()
            if src_low.startswith("discord"):
                source_type = "discord"
            elif src_low.startswith("granola"):
                source_type = "granola"
            elif src_low.startswith("beeper"):
                source_type = "beeper"
            elif src_low.startswith("slack"):
                source_type = "slack"
            elif src_low.startswith(("brief", "email")):
                source_type = "email"
            elif src_low.startswith(("wrap-up", "manual", "operator")):
                source_type = "manual"

    cur = conn.execute(
        """
        INSERT INTO action_items_inbox
          (inbox_id, source, source_evidence, evidence_quote,
           classifier_confidence, suggested_description, suggested_priority,
           suggested_due_date, suggested_waiting_on, suggested_context_slug,
           suggested_context, suggested_context_tags, suggested_email_thread_id,
           source_ref, source_url,
           suggested_source_type, suggested_source_id,
           suggested_source_person_id, suggested_about_person_id,
           suggested_project_id, suggested_discord_message_id,
           suggested_beeper_message_id, suggested_granola_meeting_id,
           suggested_slack_message_id,
           stakeholder_tier, partner_kind, is_manager_explicit, source_quote,
           suggested_creator_person_id, suggested_extracted_by,
           status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (
            inbox_id, source, source_evidence, evidence_quote,
            classifier_confidence, description.strip(), priority,
            due_date, waiting_on, context_slug,
            context, context_tags, email_thread_id,
            source_ref, source_url,
            source_type, source_id,
            source_person_id, about_person_id,
            project_id, discord_message_id,
            beeper_message_id, granola_meeting_id,
            slack_message_id,
            stakeholder_tier, partner_kind, is_manager_explicit, evidence_quote,
            creator_person_id, extracted_by,
        ),
    )
    inbox_pk = cur.lastrowid

    # Wire people graph (source/about auto-linked + any extras passed in)
    auto_links: list[tuple[int, str]] = []
    if source_person_id:
        auto_links.append((source_person_id, "source"))
    if about_person_id and about_person_id != source_person_id:
        auto_links.append((about_person_id, "about"))
    if people_relations:
        auto_links.extend(people_relations)
    if auto_links:
        link_people(conn, "inbox", inbox_pk, inbox_id, auto_links)

    return inbox_id, None


# ────────────────────────────────────────────────────────────────────────
# Rejection logger + table bootstrap
# ────────────────────────────────────────────────────────────────────────
def log_rejection(
    conn: sqlite3.Connection,
    source: str,
    target_table: str,
    errors: list[str],
    row_data: dict,
    context: str | None = None,
) -> int | None:
    """Insert one row into ingest_rejections. Returns rowid or None on failure."""
    try:
        cur = conn.execute(
            "INSERT INTO ingest_rejections "
            "(source, target_table, errors, row_json, context) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                source,
                target_table,
                json.dumps(errors),
                json.dumps(row_data, default=str)[:4000],
                context,
            ),
        )
        return cur.lastrowid
    except sqlite3.OperationalError:
        return None


INGEST_REJECTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_rejections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  target_table TEXT NOT NULL,
  rejected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  errors TEXT,
  row_json TEXT,
  context TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_rejections_source
  ON ingest_rejections(source, rejected_at);
CREATE INDEX IF NOT EXISTS idx_ingest_rejections_target
  ON ingest_rejections(target_table, rejected_at);
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    """Idempotently create ingest_rejections + indexes."""
    conn.executescript(INGEST_REJECTIONS_SCHEMA)
    conn.commit()


# ────────────────────────────────────────────────────────────────────────
# CLI (bootstrap + smoke test)
# ────────────────────────────────────────────────────────────────────────
def _selftest() -> int:
    """Smoke-test validators (all example data FICTIONAL). Returns 0 on all-pass."""
    cases_ai: list[tuple[str, dict, bool, str | None]] = [
        ("valid_item",
         {"description": "Send the signed contract back to the vendor by Friday",
          "priority": "P1"}, True, None),
        ("too_short",
         {"description": "do it", "priority": "P1"}, False,
         "description_too_short"),
        ("shell_pattern",
         {"description": "cd repo && python tools/query.py action_items OPEN"},
         False, "description_shell_command_pattern"),
        ("bad_priority",
         {"description": "Something reasonable to do for the team",
          "priority": "URGENT"}, False, "priority_invalid"),
        ("bad_status",
         {"description": "Something reasonable to do for the team",
          "status": "PENDING"}, False, "status_invalid"),
        ("waiting_on_bad",
         {"description": "Follow up on venue confirmation for the event",
          "waiting_on": "Zorgblatt xyzzy@nowhere.invalid"}, False,
         "waiting_on_unresolvable"),
    ]

    passed = 0
    failed: list[str] = []
    print("validate_action_item:")
    for name, row, expect_ok, expect_err_prefix in cases_ai:
        ok, errors = validate_action_item(row)
        if ok != expect_ok:
            failed.append(f"{name}: ok={ok} errors={errors}")
            print(f"  FAIL  {name}: ok={ok} errors={errors}")
        elif not expect_ok and expect_err_prefix and not any(
            e.startswith(expect_err_prefix) for e in errors
        ):
            failed.append(f"{name}: no error startswith {expect_err_prefix}")
            print(f"  FAIL  {name}: errors={errors}")
        else:
            print(f"  PASS  {name}")
            passed += 1

    # Selftest cases for the propose_to_inbox gates.
    print("\npropose_to_inbox gates:")
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.executescript("""
        CREATE TABLE action_items (item_id TEXT, email_thread_id TEXT, status TEXT);
        CREATE TABLE action_items_inbox (
            inbox_id TEXT PRIMARY KEY, source TEXT, source_evidence TEXT,
            evidence_quote TEXT, classifier_confidence REAL,
            suggested_description TEXT, suggested_priority TEXT,
            suggested_due_date TEXT, suggested_waiting_on TEXT,
            suggested_context_slug TEXT, suggested_context TEXT,
            suggested_context_tags TEXT, suggested_email_thread_id TEXT,
            source_ref TEXT, source_url TEXT,
            suggested_source_type TEXT, suggested_source_id TEXT,
            suggested_source_person_id INTEGER, suggested_about_person_id INTEGER,
            suggested_project_id INTEGER, suggested_discord_message_id TEXT,
            suggested_beeper_message_id TEXT, suggested_granola_meeting_id TEXT,
            suggested_slack_message_id TEXT,
            stakeholder_tier INTEGER, partner_kind TEXT, is_manager_explicit INTEGER,
            source_quote TEXT, suggested_creator_person_id INTEGER,
            suggested_extracted_by TEXT, status TEXT);
    """)
    cases_inbox = [
        ("missing_url",
         {"source": "brief.classify.modelname",
          "description": "Send the payment form to the winning team this week",
          "source_url": None},
         "source_url_missing"),
        ("bad_url_shell",
         {"source": "brief.classify.modelname",
          "description": "Send the payment form to the winning team this week",
          "source_url": "grep -r winner ./inbox --include '*.eml'"},
         "source_url_invalid_pattern"),
        ("manual_no_url_ok",
         {"source": "manual",
          "description": "Send the payment form to the winning team this week",
          "source_url": None},
         None),
        ("short_desc",
         {"source": "manual", "description": "do it", "source_url": None},
         "description_too_short"),
    ]
    for name, kwargs, expect_reason in cases_inbox:
        try:
            inbox_id, reason = propose_to_inbox(mem, **kwargs)
        except Exception as e:  # pragma: no cover - shouldn't hit in selftest
            failed.append(f"{name}: exception {type(e).__name__}: {e}")
            print(f"  FAIL  {name}: exception {e}")
            continue
        if expect_reason is None:
            if reason is None:
                print(f"  PASS  {name} (inbox_id={inbox_id})")
                passed += 1
            else:
                failed.append(f"{name}: unexpected refusal {reason}")
                print(f"  FAIL  {name}: refusal={reason}")
        else:
            if reason and reason.startswith(expect_reason):
                print(f"  PASS  {name} (reason={reason})")
                passed += 1
            else:
                failed.append(f"{name}: expected {expect_reason} got {reason}")
                print(f"  FAIL  {name}: got reason={reason}")
    mem.close()

    print(f"\nValidator selftest: {passed} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", action="store_true",
                        help="Create ingest_rejections table if missing, then exit")
    parser.add_argument("--selftest", action="store_true",
                        help="Run validator smoke tests")
    args = parser.parse_args()

    if args.bootstrap:
        import _db
        conn = _db.connect()
        conn.row_factory = sqlite3.Row
        ensure_table(conn)
        cnt = conn.execute("SELECT COUNT(*) c FROM ingest_rejections").fetchone()["c"]
        print(f"ingest_rejections ready. rows={cnt}")
        conn.close()
        sys.exit(0)

    if args.selftest:
        sys.exit(_selftest())

    parser.print_help()
    sys.exit(0)
