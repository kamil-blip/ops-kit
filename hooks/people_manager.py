"""PostToolUse hook: comprehensive people intelligence capture.

Fires after every tool call. Extracts ALL person-related information from tool
output and upserts it to the ops database. Goal: build the richest possible
contact database automatically, without manual effort.

What it captures:
- Email addresses -> people + person_emails
- LinkedIn URLs -> people.linkedin
- Discord handles -> people.discord_username
- Affiliations/orgs/titles -> people.headline
- Any new info about existing people -> update in place
- Unstructured context -> observations table
- New people with limited info -> deferred_actions for enrichment

Design: extract aggressively, store everything, never delete. If we encounter
info that doesn't fit a column, it goes to observations.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

_hooks_dir = os.path.dirname(os.path.abspath(__file__))
if not sys.path or sys.path[0] != _hooks_dir:
    # Front-insert so hooks/config.py wins over core/config.py when this
    # module is imported in-process (script execution already has it first).
    sys.path.insert(0, _hooks_dir)

from config import get_conn, OWN_EMAILS, get_session_id, set_session_id
from health_monitor import timed, emit, log_audit

# === EXTRACTION PATTERNS ===

EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
LINKEDIN_RE = re.compile(r'(?:https?://)?(?:www\.)?linkedin\.com/in/([a-zA-Z0-9_-]+)', re.IGNORECASE)
TWITTER_RE = re.compile(r'(?:(?:https?://)?(?:www\.)?(?:twitter|x)\.com/([a-zA-Z0-9_]+))|(?:(?:^|\s)@([a-zA-Z0-9_]{2,15})(?=\s|$|\.|,))', re.IGNORECASE)
GITHUB_RE = re.compile(r'(?:https?://)?(?:www\.)?github\.com/([a-zA-Z0-9_-]+)', re.IGNORECASE)
DISCORD_RE = re.compile(r'(?:discord\s*(?:handle|username|user|id|tag)?)\s*[:=]\s*([a-zA-Z0-9_.]{2,32}(?:#\d{4})?)', re.IGNORECASE)
SCHOLAR_RE = re.compile(r'(?:https?://)?scholar\.google\.com/citations\?[^\s<>"]+', re.IGNORECASE)

# Location extraction (must not match education lines)
LOCATION_PATTERNS = [
    re.compile(r'"(?:location|city|country|based_in|based in|timezone)"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'(?:based in|located in|lives in|living in)\s+([A-Z][a-z]+(?:[\s,]+[A-Z][a-z]+){0,3}?)(?=\s*[.\n]|$)', re.IGNORECASE),
    re.compile(r'(?:Location|City|Country):\s*([^\n]{3,50})', re.IGNORECASE),
]

# Education extraction (single line only)
EDUCATION_PATTERNS = [
    re.compile(r'"(?:education|degree|university|school)"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'((?:PhD|Ph\.D|MSc|M\.S\.|MA|BA|BS|B\.S\.|MBA|MPhil|DPhil)\.?\s+(?:in\s+)?[A-Za-z\s]{0,30}?(?:from|at)\s+[A-Z][A-Za-z\s&]{2,40}?)(?=\s*[,.\n]|$)', re.IGNORECASE),
    re.compile(r'((?:PhD|Ph\.D|MSc|M\.S\.|MA|BA|BS|MBA|MPhil|DPhil)\.?\s+(?:in\s+)?[A-Za-z\s]{2,30}?(?:University|Institute|College|MIT|Stanford|Oxford|Cambridge|Harvard|Berkeley|ETH|CMU|Princeton|Yale|Cornell)[A-Za-z\s]{0,15}?)(?=\s*[,.\n]|$)', re.IGNORECASE),
]

# Research interests from plain text
RESEARCH_PATTERNS = [
    re.compile(r'"(?:research_interests|interests|expertise|specialization)"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'(?:research interests?|expertise|specializ(?:ation|es? in)|focus(?:es)? on|works on)[:=]\s*([^\n]{3,100})', re.IGNORECASE),
]

# Seniority/career stage
SENIORITY_PATTERNS = [
    re.compile(r'"(?:seniority|career_stage|level)"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'\b((?:Senior|Junior|Staff|Principal|Distinguished|Associate|Assistant|Full|Emeritus)\s+(?:Research(?:er)?|Professor|Scientist|Engineer|Fellow|Developer|Analyst))\b', re.IGNORECASE),
    re.compile(r'\b(PhD (?:Student|Candidate)|Postdoc(?:toral)?(?:\s+(?:Researcher|Fellow))?|Undergraduate|Graduate Student|Masters Student|Intern)\b', re.IGNORECASE),
]

# Role detection keywords for the people.is_* role flags. Adjust the
# vocabulary to whatever roles matter in your own domain.
ROLE_KEYWORDS = {
    "judge": re.compile(r'\b(?:judge|judging|reviewer|review(?:ing)?(?:\s+projects)?)\b', re.IGNORECASE),
    "speaker": re.compile(r'\b(?:speaker|speaking|talk(?:ing)?|present(?:ing|ation)?|keynote)\b', re.IGNORECASE),
    "mentor": re.compile(r'\b(?:mentor(?:ing)?|mentorship|advisor|advising)\b', re.IGNORECASE),
    "organizer": re.compile(r'\b(?:organiz(?:er|ing)|host(?:ing)?|local\s+organizer)\b', re.IGNORECASE),
}

# Name extraction: look for structured patterns
NAME_PATTERNS = [
    re.compile(r'"name"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'"sender_name"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'"author"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'(?:from|sender|author|to|cc|reviewer|judge|speaker|organizer)\s*[:=]\s*(?:")?([A-Z][a-z]+ (?:[A-Z][a-z]+ )?[A-Z][a-z]+)', re.IGNORECASE),
    re.compile(r'^([A-Z][a-z]+ (?:van |de |von )?[A-Z][a-z]+)\s*[,;]', re.MULTILINE),  # "Jane Doe," at line start
    re.compile(r'(?:Contact|Name):\s*([A-Z][a-z]+ (?:van |de |von )?[A-Z][a-z]+)', re.IGNORECASE),
    re.compile(r'^(?:Hi|Hey|Dear)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*[,!]', re.MULTILINE),
    re.compile(r'<([A-Z][a-z]+ (?:van |de |von )?[A-Z][a-z]+)>\s*<?[a-zA-Z0-9._%+-]+@', re.IGNORECASE),  # "Name <email>"
]

# Affiliation extraction (non-greedy, single line)
AFFILIATION_PATTERNS = [
    re.compile(r'"(?:affiliation|organization|org|company|institution)"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'(?:(?:Senior |Junior |Staff |Principal |Associate |Assistant )?(?:Research(?:er)?|Scientist|Engineer|Fellow|Professor|Analyst|Developer|Director|Manager|Lead|Coordinator)\s+(?:at|@)\s+)([A-Z][A-Za-z\s&]{2,40}?)(?=\s*[,.\n]|$)', re.IGNORECASE),
    re.compile(r'(?:from|at|with|works at|affiliated with)\s+([A-Z][A-Za-z\s&]{2,40}?)(?=\s*[,.\n]|$)', re.IGNORECASE),
]

# Title/role extraction
TITLE_PATTERNS = [
    re.compile(r'"(?:title|role|position|headline)"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'((?:Senior |Junior |Staff |Principal |Distinguished |Associate |Assistant |Full |Emeritus )?(?:Research(?:er)?|Scientist|Engineer|Fellow|Professor|Analyst|Developer|Director|Manager|Lead|Head|Founder|Co-founder|Coordinator)(?:\s+(?:of|at|in)\s+[A-Za-z\s&]{2,30})?)', re.IGNORECASE),
]

SKIP_PATHS = ["/memory/", "/skills/", "/rules/", "/hooks/", "/.claude/", "__pycache__", ".git/"]
# OWN_EMAILS comes from hooks/config.py (config.toml [identity] own_emails,
# empty by default) so the operator's own addresses never mint people rows.
SKIP_EMAILS = OWN_EMAILS | {
    "noreply@github.com", "notifications@github.com",
    "no-reply@slack.com", "feedback@slack.com",
    "noreply@discord.com", "noreply@medium.com",
    "mailer-daemon@", "postmaster@",
}
SKIP_GITHUB_USERS = {"anthropics", "anthropic", "openai", "google", "microsoft", "meta", "features", "settings", "about", "login", "signup", "explore", "topics", "trending", "collections", "events", "sponsors"}


# Generic mailbox local-parts are mailbox tokens, not people, REGARDLESS of
# domain (support@example-saas.com, noreply@vendor.com, ...). Exact-match
# SKIP_EMAILS + domain checks miss these, so _extract_emails kept them and the
# INSERT minted a person that read as real via COALESCE(is_real_person,1).
GENERIC_MAILBOX_LOCALPARTS = {
    "support", "noreply", "no-reply", "no_reply", "donotreply", "info", "admin",
    "hello", "team", "notifications", "notification", "contact", "help", "bounce",
    "mailer", "mailer-daemon", "postmaster",
}


def _is_skip_email(email):
    email = email.lower().strip()
    if email in SKIP_EMAILS:
        return True
    if any(email.endswith(d) for d in ["@google.com", "@github.com", "@noreply.github.com"]):
        return True
    local = email.split("@", 1)[0] if "@" in email else email
    if local in GENERIC_MAILBOX_LOCALPARTS:
        return True
    return False


def _extract_emails(text):
    if not text:
        return []
    found = set(EMAIL_RE.findall(text.lower()))
    return [e for e in found if not _is_skip_email(e)]


def _extract_name_near_email(text, email):
    """Try to find a person's name within 300 chars of their email."""
    email_pos = text.lower().find(email)
    if email_pos < 0:
        return None
    context = text[max(0, email_pos - 300):email_pos + 300]
    for pattern in NAME_PATTERNS:
        match = pattern.search(context)
        if match:
            name = match.group(1).strip()
            if len(name) > 2 and not name.lower().startswith(("http", "www", "the ")):
                return name
    return None


def _extract_linkedin(text):
    return LINKEDIN_RE.findall(text) if text else []


def _extract_twitter(text):
    if not text:
        return []
    handles = set()
    for m in TWITTER_RE.finditer(text):
        handle = m.group(1) or m.group(2)
        if handle and handle.lower() not in ("the", "and", "for", "from", "with", "this", "that", "here"):
            handles.add(handle.lower())
    return list(handles)


def _extract_github(text):
    if not text:
        return []
    users = set()
    for m in GITHUB_RE.finditer(text):
        user = m.group(1).lower()
        if user not in SKIP_GITHUB_USERS and len(user) > 1:
            users.add(user)
    return list(users)


def _extract_discord(text):
    return DISCORD_RE.findall(text) if text else []


def _extract_affiliations(text):
    if not text:
        return []
    results = []
    for p in AFFILIATION_PATTERNS:
        for m in p.finditer(text):
            aff = m.group(1).strip() if m.lastindex else m.group(0).strip()
            if len(aff) > 2 and len(aff) < 80:
                results.append(aff)
    return results


def _extract_titles(text):
    if not text:
        return []
    results = []
    for p in TITLE_PATTERNS:
        for m in p.finditer(text):
            title = m.group(1).strip() if m.lastindex and m.group(1) else m.group(0).strip()
            if len(title) > 2 and len(title) < 100:
                results.append(title)
    return results


def _extract_scholar(text):
    return SCHOLAR_RE.findall(text) if text else []


def _extract_location(text):
    if not text:
        return None
    for p in LOCATION_PATTERNS:
        m = p.search(text)
        if m:
            loc = m.group(1).strip()
            if 3 < len(loc) < 80:
                return loc
    return None


def _extract_education(text):
    if not text:
        return None
    for p in EDUCATION_PATTERNS:
        m = p.search(text)
        if m:
            edu = m.group(1).strip() if m.lastindex else m.group(0).strip()
            if len(edu) > 3:
                return edu[:200]
    return None


def _extract_seniority(text):
    if not text:
        return None
    for p in SENIORITY_PATTERNS:
        m = p.search(text)
        if m:
            return m.group(1).strip()
    return None


def _detect_roles(text):
    """Detect if context suggests this person holds one of the tracked roles."""
    roles = {}
    if not text:
        return roles
    for role, pattern in ROLE_KEYWORDS.items():
        if pattern.search(text):
            roles[f"is_{role}"] = 1
    return roles


def _get_output_text(data):
    out = data.get("tool_response") or data.get("tool_output", {})
    if isinstance(out, str):
        return out
    if isinstance(out, dict):
        return out.get("stdout", "") or out.get("content", "") or out.get("text", "") or json.dumps(out)
    return str(out) if out else ""


def _has_table(conn, name):
    """Feature-detect an optional table (starter schemas may not ship it)."""
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except Exception:
        return False


# === PERSON UPSERT LOGIC ===

def _upsert_person(conn, email, updates, tool_name, source_snippet=None):
    """Find or create a person by email, then apply any new info.

    updates is a dict of column_name: value. Only non-empty values that
    improve on existing data are written. Every field update gets an
    observation recording the source.

    source_snippet: first ~150 chars of the text that triggered the extraction.
    """
    # Find existing person
    row = conn.execute("SELECT * FROM people WHERE email = ?", (email,)).fetchone()
    if not row:
        # Check person_emails table
        pe = conn.execute("SELECT person_id FROM person_emails WHERE email = ?", (email,)).fetchone()
        if pe:
            row = conn.execute("SELECT * FROM people WHERE id = ?", (pe["person_id"],)).fetchone()

    # Role columns: always set to 1, never back to 0
    ROLE_COLS = {"is_judge", "is_speaker", "is_mentor", "is_organizer", "is_winner", "is_fellow"}

    # Build source reference for observations
    src_ref = f"regex:{tool_name}"
    src_context = (source_snippet or "")[:150].replace("\n", " ").strip()

    if row:
        pid = row["id"]
        # Update fields: empty fields get new values, role flags only go to 1
        set_parts = []
        values = []
        fields_changed = []
        for col, val in updates.items():
            if col in ROLE_COLS:
                if val and not row[col]:
                    set_parts.append(f"{col} = ?")
                    values.append(val)
                    fields_changed.append((col, val))
            elif val:
                existing = row[col] if col in row.keys() else None
                if not existing or existing == email.split("@")[0]:
                    set_parts.append(f"{col} = ?")
                    values.append(val)
                    fields_changed.append((col, val))
        if set_parts:
            set_parts.append("updated_at = CURRENT_TIMESTAMP")
            sql = f"UPDATE people SET {', '.join(set_parts)} WHERE id = ?"
            values.append(pid)
            conn.execute(sql, values)
            # Log what changed as observation
            if fields_changed and src_context:
                changes = ", ".join(f"{c}={v}" for c, v in fields_changed[:5])
                _add_observation(conn, pid, f"[{src_ref}] Updated: {changes}. Source: \"{src_context}\"", src_ref)
    else:
        # Create new person
        name = updates.get("name", email.split("@")[0])
        # Drift control: if a person with the same lowercase name already exists,
        # log a candidate to merge_candidates so a merge-review pass can pick it
        # up. Don't block creation -- different humans can share names -- but
        # surface the signal.
        try:
            existing = conn.execute(
                "SELECT id, email FROM people WHERE LOWER(name) = LOWER(?) AND name != '' LIMIT 1",
                (name,),
            ).fetchone()
        except Exception:
            existing = None
        conn.execute(
            "INSERT INTO people (name, email, sources, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (name, email, f"hook:{tool_name}")
        )
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        if existing:
            # Stash a merge_candidates row for later review. Use a low
            # confidence (0.55) because exact-name match is weak signal without
            # corroborating evidence (org, last_contact overlap, etc.).
            # Schema: canonical_id (lower id = older = canonical) + duplicate_id.
            try:
                canonical, dup = sorted([pid, existing["id"]])
                conn.execute(
                    """INSERT OR IGNORE INTO merge_candidates
                       (canonical_id, duplicate_id, signal, evidence, confidence, status, created_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', datetime('now'))""",
                    (
                        canonical,
                        dup,
                        f"exact_name_at_insert:hook:{tool_name}",
                        f"new_pid={pid} ({email}) vs existing_pid={existing['id']} ({existing['email']})",
                        0.55,
                    ),
                )
            except sqlite3.OperationalError:
                # Table may not exist in a trimmed-down install; non-fatal.
                pass
        # Apply all updates
        set_parts = []
        values = []
        fields_set = []
        for col, val in updates.items():
            if val and col != "name":
                set_parts.append(f"{col} = ?")
                values.append(val)
                fields_set.append((col, val))
        if set_parts:
            set_parts.append("updated_at = CURRENT_TIMESTAMP")
            sql = f"UPDATE people SET {', '.join(set_parts)} WHERE id = ?"
            values.append(pid)
            conn.execute(sql, values)
        # Log creation with all extracted fields as observation
        if src_context:
            fields_summary = ", ".join(f"{c}={v}" for c, v in fields_set[:5])
            _add_observation(conn, pid, f"[{src_ref}] Created with: name={name}, {fields_summary}. Source: \"{src_context}\"", src_ref)

    # Always upsert person_emails and update last_contact_date
    now_iso = datetime.now().isoformat()
    conn.execute("""
        INSERT INTO person_emails (person_id, email, last_seen, source)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(person_id, email) DO UPDATE SET last_seen = MAX(person_emails.last_seen, excluded.last_seen)
    """, (pid, email, now_iso, f"hook:{tool_name}"))

    conn.execute("UPDATE people SET last_contact_date = ?, interaction_count = COALESCE(interaction_count, 0) + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (now_iso, pid))

    return pid


_TELEMETRY_SOURCES = ("log_interaction", "link_action_item")


def _add_observation(conn, person_id, content, source):
    """Add an observation about a person if it's not a duplicate."""
    if not content or len(content) < 5:
        return
    if source and source.startswith(_TELEMETRY_SOURCES):
        # Operation log, not a person fact: route to audit_events so the
        # observations table stays facts-only.
        conn.execute(
            "INSERT INTO audit_events (session_id, event, tool, meta, ts, inserted_at) "
            "VALUES (?, 'hook_telemetry', ?, ?, datetime('now'), datetime('now'))",
            (get_session_id(), source,
             json.dumps({"person_id": person_id, "content": content[:300]})),
        )
        return
    # Check for near-duplicate (same person, same first 50 chars)
    existing = conn.execute(
        "SELECT id FROM observations WHERE person_id = ? AND content LIKE ?",
        (person_id, content[:50] + "%")
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO observations (subject, subject_type, person_id, content, source, confidence, inserted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("", "person", person_id, content[:500], source, "auto", datetime.now().isoformat())
        )


def _ensure_entity(conn, pid, name, email, updates=None):
    """Auto-create a person entity in the entities table if one doesn't exist."""
    entity_id = f"person-{pid}"
    existing = conn.execute("SELECT id FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if existing:
        return
    # Skip if flagged as non-person (project title, team aggregate, etc.)
    row = conn.execute(
        "SELECT COALESCE(is_real_person, 1) FROM people WHERE id = ?", (pid,)
    ).fetchone()
    if row and row[0] == 0:
        return
    try:
        data = {"people_id": pid, "email": email}
        if updates:
            for key in ("affiliation", "location", "linkedin", "headline", "seniority"):
                if updates.get(key):
                    data[key] = updates[key]
        conn.execute(
            "INSERT INTO entities (id, type, name, data, source, status, created_at, updated_at) "
            "VALUES (?, 'person', ?, ?, 'people_manager', 'active', datetime('now'), datetime('now'))",
            (entity_id, name, json.dumps(data))
        )
    except Exception:
        pass  # Never block the hook


# Rate limit: max 1 async enrichment per hook invocation
_enrichment_fired = False


def _async_enrich(person_id, email, context_text):
    """No-op: the enrichment/extraction layer is not included in this starter kit.

    Rationale from the system this kit was extracted from: an earlier version
    fired a worker that wrote directly to people.headline, people.linkedin,
    people.location etc. Direct-write enrichment duplicated and polluted the
    canonical people.* columns without provenance, so it was disabled in favor
    of a provenance-gated pipeline (grounded claims -> observations -> a
    promotion gate decides what lands in people.*). If you add your own
    enrichment, write observations with source metadata, not people.* columns.
    The deferred_actions row queued by _queue_enrichment still records that
    this person needs enrichment.
    """
    return  # no-op


def _queue_enrichment(conn, person_id, email, name):
    """Queue a deferred action for new people so enrichment isn't forgotten."""
    existing = conn.execute(
        "SELECT id FROM deferred_actions WHERE target_email = ? AND action_type = 'enrich_person' AND status = 'pending'",
        (email,)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO deferred_actions (action_type, details, target_email, target_name, priority, status, inserted_at) VALUES (?, ?, ?, ?, 'P2', 'pending', ?)",
            ("enrich_person", f"New person: {name or email}. Look up LinkedIn, affiliation, bio.", email, name, datetime.now().isoformat())
        )


# === DEFERRED QUEUE API ===

def queue_action(action, details, target_email=None, target_name=None, priority="P0"):
    try:
        conn = get_conn()
        conn.execute(
            "INSERT INTO deferred_actions (action_type, details, target_email, target_name, priority, status, inserted_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (action, details, target_email, target_name, priority, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"QUEUE ERROR: {e}", file=sys.stderr)


def read_pending():
    try:
        conn = get_conn()
        rows = conn.execute("SELECT * FROM deferred_actions WHERE status = 'pending' ORDER BY inserted_at").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def mark_done(action, target_email=None):
    try:
        conn = get_conn()
        if target_email:
            conn.execute("UPDATE deferred_actions SET status='done', completed_at=? WHERE status='pending' AND action_type=? AND target_email=?",
                         (datetime.now().isoformat(), action, target_email))
        else:
            conn.execute("UPDATE deferred_actions SET status='done', completed_at=? WHERE status='pending' AND action_type=?",
                         (datetime.now().isoformat(), action))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"QUEUE MARK ERROR: {e}", file=sys.stderr)


def is_known_email(email):
    if not email:
        return False
    email = email.strip().lower()
    if email in OWN_EMAILS:
        return True
    try:
        conn = get_conn()
        row = conn.execute("SELECT id FROM people WHERE email = ?", (email,)).fetchone()
        if not row:
            row = conn.execute("SELECT person_id FROM person_emails WHERE email = ?", (email,)).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


# === AUDIT LOGGING (runs before early returns) ===

def _audit_and_remind(tool_name, tool_input, file_path):
    """Log audit events. Runs before people capture early returns."""
    try:
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            cmd_lower = cmd.lower()
            event_type = "bash_cmd"
            meta = {}

            if "gmail" in cmd_lower and ("send" in cmd_lower or "draft" in cmd_lower):
                event_type = "email_send" if "send" in cmd_lower else "email_draft"
                to_match = re.search(r'(?:--to\s+|gmail\s+send\s+)(\S+@\S+)', cmd)
                if to_match:
                    meta["recipient"] = to_match.group(1)
            elif "notion" in cmd_lower and any(w in cmd_lower for w in ["update", "create"]):
                event_type = "notion_write"
                m = re.search(r'(?:page|db|database|task)\s+([A-Za-z0-9-]{8,})', cmd)
                if m:
                    meta["object"] = m.group(1)
            elif "discord" in cmd_lower and "send" in cmd_lower:
                event_type = "discord_send"
                m = re.search(r'discord\s+send\s+(\S+)', cmd_lower)
                if m:
                    meta["object"] = m.group(1)
            elif any(w in cmd_lower for w in ["schedule_post", "social_post"]):
                event_type = "social_post"

            log_audit(event_type, tool=tool_name, cmd_preview=cmd[:200], meta=meta)
            if event_type in ("email_send", "notion_write", "discord_send", "social_post"):
                # Structured summary: every bus row must carry actor + object +
                # outcome; a bare f-string used to write empty "event: "
                # summaries whenever no recipient parsed.
                target = meta.get("recipient") or meta.get("object") or cmd.strip()[:80]
                emit(event_type, f"{event_type} -> {target} [attempted]",
                     details={"actor": "people_manager:bash", "object": target,
                              "outcome": "attempted", "cmd_preview": cmd[:200]})

        elif tool_name in ("Write", "Edit"):
            fp = file_path.lower()
            event_type = "file_write"
            if "action-items" in fp or "action_items" in fp:
                event_type = "action_item_update"
            elif "/memory/" in fp:
                event_type = "memory_update"
            elif "changelog" in fp:
                event_type = "changelog_update"
            elif fp.endswith(".html") and "email" in fp:
                event_type = "email_draft_file"
            log_audit(event_type, tool=tool_name, file_path=file_path)

        elif tool_name and tool_name.startswith("mcp__"):
            event_type = "mcp_call"
            tn_lower = tool_name.lower()
            obj = None
            if "schedule" in tn_lower and "post" in tn_lower:
                event_type = "social_post"
            elif "notion" in tn_lower and any(w in tn_lower for w in ["update", "create"]):
                event_type = "notion_write"
            elif "gmail" in tn_lower and any(w in tn_lower for w in
                                             ("send_email", "reply_to_email", "reply_all",
                                              "forward_email", "send_draft")):
                # MCP email sends bypass Bash, so they'd never reach the bus
                # without this branch; log them explicitly.
                event_type = "email_send"
                to = tool_input.get("to")
                obj = ", ".join(to) if isinstance(to, list) else (to or tool_input.get("threadId"))
            log_audit(event_type, tool=tool_name)

            if event_type in ("email_send", "notion_write", "social_post"):
                target = obj or tool_input.get("page_id") or tool_input.get("id") or tool_name
                emit(event_type, f"{event_type} -> {target} [attempted]",
                     details={"actor": "people_manager:mcp", "object": str(target)[:120],
                              "outcome": "attempted", "tool": tool_name})
    except Exception:
        pass  # Never block the hook


# === MAIN HOOK ===

@timed("people_manager")
def main(data):
    global _enrichment_fired
    _enrichment_fired = False  # Reset per invocation
    tool_name = data.get("tool_name", "")
    text = _get_output_text(data)
    tool_input = data.get("tool_input", {})
    file_path = (tool_input.get("file_path", "") or tool_input.get("command", "")).replace("\\", "/")

    # Audit logging runs FIRST (before early returns) so we never miss events
    _audit_and_remind(tool_name, tool_input, file_path)

    # Skip small outputs and internal files
    if not text or len(text) < 20:
        return
    if any(skip in file_path for skip in SKIP_PATHS):
        return

    # === EXTRACT EVERYTHING ===
    external_emails = _extract_emails(text)
    linkedin_profiles = _extract_linkedin(text)
    discord_handles = _extract_discord(text)
    affiliations = _extract_affiliations(text)
    titles = _extract_titles(text)
    scholar_links = _extract_scholar(text)

    # Nothing to capture?
    has_people_data = (external_emails or linkedin_profiles or discord_handles)
    if not has_people_data:
        # Still do audit logging below, but skip people capture
        pass
    else:
        try:
            conn = get_conn()
            captured_new = 0
            updated_existing = 0

            # people/person_emails may be write-gated (audit triggers reading
            # _audit_context). Set the audit actor INSIDE one immediate
            # transaction: actor_scope() around BEGIN IMMEDIATE is race-broken
            # (a retry rolls the actor back), so set -> mutate -> clear must
            # share the txn.
            _locked = False
            for _attempt in range(3):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    _locked = True
                    break
                except Exception:
                    import time as _time
                    _time.sleep(0.3 * (_attempt + 1))
            if not _locked:
                conn.close()
                raise RuntimeError("DB busy; capture skipped this fire (fail-open)")
            # Feature-detect the audit-actor singleton (trimmed installs may
            # not carry the CDC/audit trigger layer).
            _has_audit_ctx = _has_table(conn, "_audit_context")
            if _has_audit_ctx:
                conn.execute(
                    "UPDATE _audit_context SET actor=?, source_ref=?, set_at=CURRENT_TIMESTAMP WHERE id=1",
                    ("hook:people_manager", (tool_name or "")[:80]),
                )

            for email in external_emails[:30]:
                name = _extract_name_near_email(text, email)

                # Build updates dict with everything we found near this email
                updates = {}
                if name:
                    updates["name"] = name

                # Try to associate LinkedIn with this person
                # Look for LinkedIn URL near the email
                email_pos = text.lower().find(email)
                if email_pos >= 0:
                    context = text[max(0, email_pos - 500):email_pos + 500]

                    li = LINKEDIN_RE.search(context)
                    if li:
                        updates["linkedin"] = f"https://linkedin.com/in/{li.group(1)}"

                    disc = DISCORD_RE.search(context)
                    if disc:
                        updates["discord_username"] = disc.group(1)

                    for p in TITLE_PATTERNS:
                        m = p.search(context)
                        if m:
                            title = m.group(1).strip() if m.lastindex and m.group(1) else m.group(0).strip()
                            if len(title) > 2:
                                updates["headline"] = title
                                break

                    if "headline" not in updates:
                        for p in AFFILIATION_PATTERNS:
                            m = p.search(context)
                            if m:
                                aff = m.group(1).strip() if m.lastindex else m.group(0).strip()
                                if len(aff) > 2:
                                    updates["headline"] = aff
                                    break

                    loc = _extract_location(context)
                    if loc:
                        updates["location"] = loc

                    edu = _extract_education(context)
                    if edu:
                        updates["education"] = edu

                    sen = _extract_seniority(context)
                    if sen:
                        updates["seniority"] = sen

                    # Research interests
                    for p in RESEARCH_PATTERNS:
                        m = p.search(context)
                        if m:
                            updates["research_interests"] = m.group(1).strip()[:200]
                            break

                    # Detect roles (see ROLE_KEYWORDS)
                    roles = _detect_roles(context)
                    updates.update(roles)

                # Check if person exists before upsert (to count new vs updated)
                is_new = not is_known_email(email)

                # Build source snippet for provenance tracking
                source_snippet = context[:150] if email_pos >= 0 else text[:150]

                pid = _upsert_person(conn, email, updates, tool_name, source_snippet=source_snippet)

                if is_new:
                    captured_new += 1
                    _ensure_entity(conn, pid, name or email.split("@")[0], email, updates)
                    _queue_enrichment(conn, pid, email, name)
                    # Enrichment layer is a no-op stub in this starter kit
                    if len(text) > 200:
                        _async_enrich(pid, email, text[:8000])
                elif updates:
                    updated_existing += 1

                # If there's extra context that doesn't fit columns, add as observation
                # Look for structured JSON data with extra fields
                if email_pos >= 0:
                    obs_context = text[max(0, email_pos - 200):email_pos + 200]
                    # Look for research interests, skills, etc.
                    for field in ["research_interests", "skills", "expertise", "publications", "papers"]:
                        pattern = re.compile(rf'"{field}"\s*:\s*"([^"]+)"', re.IGNORECASE)
                        m = pattern.search(obs_context)
                        if m:
                            val = m.group(1).strip()
                            if field in ("research_interests", "skills"):
                                # These are actual columns, update directly
                                conn.execute(f"UPDATE people SET {field} = COALESCE(NULLIF(?, ''), {field}), updated_at = CURRENT_TIMESTAMP WHERE id = ?", (val, pid))
                            else:
                                _add_observation(conn, pid, f"{field}: {val}", f"hook:{tool_name}")

            if _has_audit_ctx:
                conn.execute(
                    "UPDATE _audit_context SET actor=NULL, source_ref=NULL, set_at=NULL WHERE id=1"
                )
            conn.commit()
            conn.close()

            if captured_new > 0 or updated_existing > 0:
                parts = []
                if captured_new:
                    parts.append(f"{captured_new} new")
                if updated_existing:
                    parts.append(f"{updated_existing} updated")
                emit("people_captured", f"{' + '.join(parts)} from {tool_name}")

        except Exception as e:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
            print(f"people_manager capture error: {e}", file=sys.stderr)

    # Audit logging now runs in _audit_and_remind() before early returns


if __name__ == "__main__":
    try:
        data = json.load(sys.stdin)
        # Prime session_id from Claude Code payload so audit_events/observations
        # written by this hook share the same session_id as other hooks in the turn.
        set_session_id(data.get("session_id", "") or data.get("sessionId", ""))
        main(data)
    except Exception as e:
        print(f"HOOK ERROR (people_manager): {e}", file=sys.stderr)
    sys.exit(0)
