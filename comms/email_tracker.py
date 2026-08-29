"""
Email tracker: connects gmail-to-sqlite messages to ops.db action_items.

Solves the problem of manually scanning every thread to figure out who is
still waiting on us. Builds on top of the existing DBs via ATTACH DATABASE.

PREREQUISITE (external): the gmail-to-sqlite mirror. Install gmail-to-sqlite,
run its sync against your mailbox, then point config.toml [sync] gmail_db_path
(or the OPS_GMAIL_DB env var) at the resulting messages database. Every
subcommand that reads gmail.* exits with a clear message until that is set up.

Usage:
    python email_tracker.py inbox                              # all unreplied threads
    python email_tracker.py inbox --linked                     # only linked to action items
    python email_tracker.py inbox --days 7                     # last N days only
    python email_tracker.py link AI-xxx THREAD_ID              # link action item to thread (both directions)
    python email_tracker.py unlink AI-xxx                      # remove link (both directions)
    python email_tracker.py autoclose                          # surface items + threads with replies
    python email_tracker.py status AI-xxx                      # show thread state for item
    python email_tracker.py sync                               # refresh email_last_* fields
    python email_tracker.py set_status THREAD_ID STATUS        # set email_threads.status
    python email_tracker.py set_topic THREAD_ID TAG            # set email_threads.topic_tag
    python email_tracker.py set_context THREAD_ID SLUG         # set email_threads.context_slug
    python email_tracker.py set_person THREAD_ID PERSON_ID     # set email_threads.person_id
    python email_tracker.py autolink                          # auto-link items to threads
    python email_tracker.py autolink --dry-run                # preview without linking
    python email_tracker.py autolink --dry-run --verbose      # show keyword details

Design notes:
    `sync` refreshes email_last_inbound_at / email_last_outbound_at on action_items
    that are already linked. It does NOT reclassify new gmail rows into email_threads.
    Classification is a separate pipeline (see brief/daily_sync.py and the classifier).
    After a gmail sync, run the classifier explicitly if email_threads needs to pick
    up new rows.
"""
import paths

import argparse
import json
import os
import re
import sqlite3
import _db  # unified connector (busy_timeout + FK ON)
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

OPS_DB = Path(str(paths.DB_PATH))


def _load_toml_config() -> dict:
    """Parse <repo-root>/config.toml; {} when absent or malformed."""
    cfg = paths.ROOT / "config.toml"
    if cfg.is_file():
        try:
            import tomllib
            with open(cfg, "rb") as f:
                return tomllib.load(f)
        except Exception:
            pass
    return {}


_CFG = _load_toml_config()


def _load_operator_emails() -> set:
    """Every address that counts as "ours": the operator's own inboxes plus
    the shared org aliases they answer. Config-driven, empty by default.

    Sources, in order:
      1. OPERATOR_EMAIL_ALIASES env var (comma-separated) - overrides config
      2. config.toml: [operator] emails + [org] email_aliases
      3. empty set (no sender is treated as "ours" until configured)

    Example config.toml entries (FICTIONAL addresses, replace with your own):
      # [operator]
      # emails = ["jane@example.org"]
      # [org]
      # email_aliases = ["team@example.org", "support@example.org"]
    """
    env = os.environ.get("OPERATOR_EMAIL_ALIASES", "").strip()
    if env:
        return {a.strip().lower() for a in env.split(",") if a.strip()}
    raw = list((_CFG.get("operator", {}) or {}).get("emails", []) or [])
    raw += list((_CFG.get("org", {}) or {}).get("email_aliases", []) or [])
    return {str(a).strip().lower() for a in raw if str(a).strip()}


OPERATOR_EMAILS = _load_operator_emails()

# Operator display name from config.toml [operator] name; used only to strip
# greeting lines ("hi jane,") before acknowledgment matching.
OPERATOR_FIRST_NAME = (
    str((_CFG.get("operator", {}) or {}).get("name", "")).strip().split(" ")[0].lower()
)

# people.id values for the operator themselves (all their own aliases), so
# autolink never matches an action item back to the operator's own name.
# Set in config.toml:  [contacts] self_person_ids = [1]
_SELF_PERSON_IDS = set(
    (_CFG.get("contacts", {}) or {}).get("self_person_ids", []) or []
)

VALID_STATUSES = {
    "pending", "replied", "stale", "expired",
    "resolved_elsewhere", "ignore", "no_action_needed",
    "archived", "resolved", "no_inbound",
}

# --- Operator-tunable noise lists -------------------------------------------
# Generic SaaS/automation noise only. Extend these with your own newsletter
# senders, tooling domains, and automated subjects as they show up.

NOISE_DOMAINS = {
    "mailer-daemon", "noreply", "no-reply", "notify@", "payments-noreply",
    "mail.notion.so", "google.com", "googlepayments",
    "linkedin.com", "slack.com", "github.com", "stripe.com",
    "atlassian.net", "substack.com", "patreon.com",
}

NOISE_SUBJECTS = {
    "Delivery Status Notification", "Out of Office", "Moderator's spam",
    "Newsletter", "Verification expired", "receipt", "invoice from",
    "Payment received", "Accepted:",
    "Welcome to", "You're in!",  # Onboarding
    "New registration for",  # Event-platform auto
}

# Subjects/senders where we've explicitly decided no reply is needed
NOISE_CONTAINS = {
    "spam report", "spamdigest", "onboarding tasks",
    "made updates",
    "updated invitation:", "invitation from", "accepted:",
    "declined:", "tentative:",  # Calendar responses
    "document shared with you",  # Google Docs share
    "added you to",  # Sharing notifications
    "you have an invoice",  # Invoice notifications
    "payment received",
}

# Short inbound messages that are just acknowledgments / no action needed
ACK_PATTERNS = {
    "thanks", "thank you", "sounds good", "got it", "great",
    "perfect", "awesome", "ok", "okay", "cool", "noted",
    "will do", "see you", "appreciate", "cheers",
}


def _greeting_variants() -> list:
    """Greeting prefixes to strip before ack matching, derived from the
    configured operator name (e.g. "hi jane,"). Empty when no name is set."""
    if not OPERATOR_FIRST_NAME:
        return []
    out = []
    for g in ("hi", "hey", "hello", "thanks", "thank you"):
        out.append(f"{g} {OPERATOR_FIRST_NAME},")
        out.append(f"{g} {OPERATOR_FIRST_NAME}")
    return out


def is_ack_message(body: str) -> bool:
    """Detect short acknowledgment messages that don't need a reply."""
    if not body:
        return False
    # Strip quoted content
    lines = []
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if stripped.startswith("On ") and "wrote:" in stripped:
            break
        if stripped.startswith("-----"):
            break
        if "Original Message" in stripped:
            break
        lines.append(stripped)
    content = " ".join(lines).strip().lower()
    # Remove common greetings addressed to the operator
    for greeting in _greeting_variants():
        content = content.replace(greeting, "").strip()
    # Strip punctuation for matching
    stripped = content.replace(".", "").replace("!", "").replace(",", "").strip()
    if not stripped:
        return True
    # If the whole message is short AND starts with an ack pattern
    if len(stripped) < 80:
        for p in ACK_PATTERNS:
            if stripped.startswith(p):
                return True
    return False


def _require_gmail_db() -> Path:
    """Resolve the external gmail-to-sqlite database, or exit with the
    prerequisite instructions. See the module docstring."""
    if paths.GMAIL_DB_PATH is None:
        sys.exit(
            "email_tracker: no gmail-to-sqlite database configured.\n"
            "Set [sync] gmail_db_path in config.toml or the OPS_GMAIL_DB env var.\n"
            "(External prerequisite: the gmail-to-sqlite mirror; see INSTALL.md.)"
        )
    p = Path(str(paths.GMAIL_DB_PATH))
    if not p.is_file():
        sys.exit(
            f"email_tracker: gmail-to-sqlite database not found at {p}.\n"
            "Run the gmail-to-sqlite sync first (see INSTALL.md)."
        )
    return p


def connect():
    """Connect to ops.db with the gmail-to-sqlite mirror attached."""
    conn = _db.connect(OPS_DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(f"ATTACH DATABASE '{_require_gmail_db()}' AS gmail")
    conn.row_factory = sqlite3.Row
    return conn


def is_noise(sender_json: str, subject: str) -> bool:
    """Heuristic noise filter for automated senders."""
    try:
        sender = json.loads(sender_json)
        email = (sender.get("email", "") or "").lower()
    except Exception:
        email = str(sender_json).lower()

    # Exclude our own sent emails mislabeled as inbound
    if email in OPERATOR_EMAILS:
        return True

    for d in NOISE_DOMAINS:
        if d in email:
            return True
    subj = (subject or "").lower()
    if subject:
        for s in NOISE_SUBJECTS:
            if subject.startswith(s):
                return True
        for s in NOISE_CONTAINS:
            if s.lower() in subj:
                return True
    return False


def sender_label(sender_json: str) -> str:
    try:
        s = json.loads(sender_json)
        return s.get("name") or s.get("email") or ""
    except Exception:
        return str(sender_json)[:40]


def get_unreplied_threads(days: int = 14, linked_only: bool = False):
    """
    Return threads where the last message is inbound (someone waiting on us).
    Excludes noise. Optionally filters to only threads linked to action items.
    """
    conn = connect()
    c = conn.cursor()

    # Find the latest message per thread, filter to inbound-last
    sql = """
        WITH last_per_thread AS (
            SELECT thread_id, MAX(timestamp) AS last_ts
            FROM gmail.messages
            WHERE is_deleted = 0
              AND timestamp >= datetime('now', ? || ' days')
            GROUP BY thread_id
        ),
        latest AS (
            SELECT m.thread_id, m.message_id, m.sender, m.subject,
                   m.timestamp, m.is_outgoing, m.body
            FROM gmail.messages m
            INNER JOIN last_per_thread l
                ON m.thread_id = l.thread_id AND m.timestamp = l.last_ts
            WHERE m.is_deleted = 0
        )
        SELECT latest.*, ai.item_id, ai.status AS ai_status, ai.priority,
               et.status AS et_status
        FROM latest
        LEFT JOIN action_items ai ON ai.email_thread_id = latest.thread_id
        LEFT JOIN email_threads et ON et.thread_id = latest.thread_id
        WHERE latest.is_outgoing = 0
          AND COALESCE(et.status, 'pending') NOT IN
              ('replied', 'resolved', 'resolved_elsewhere', 'no_action_needed', 'ignore')
        ORDER BY latest.timestamp DESC
    """
    c.execute(sql, (f"-{days}",))
    rows = [dict(r) for r in c.fetchall()]

    # Noise filter
    rows = [r for r in rows if not is_noise(r["sender"], r["subject"] or "")]
    # Acknowledgment filter
    rows = [r for r in rows if not is_ack_message(r.get("body") or "")]

    if linked_only:
        rows = [r for r in rows if r["item_id"]]

    conn.close()
    return rows


def link_action_item(item_id: str, thread_id: str):
    """Link an action item to a gmail thread + snapshot inbound/outbound times."""
    conn = connect()
    c = conn.cursor()

    # Verify action item exists
    c.execute("SELECT item_id FROM action_items WHERE item_id = ?", (item_id,))
    if not c.fetchone():
        print(f"Action item {item_id} not found")
        conn.close()
        return False

    # Get thread info
    c.execute(
        """SELECT MAX(CASE WHEN is_outgoing = 0 THEN timestamp END) AS last_in,
                  MAX(CASE WHEN is_outgoing = 1 THEN timestamp END) AS last_out
           FROM gmail.messages WHERE thread_id = ? AND is_deleted = 0""",
        (thread_id,),
    )
    row = c.fetchone()
    if not row:
        print(f"Thread {thread_id} not found")
        conn.close()
        return False

    c.execute(
        """UPDATE action_items
           SET email_thread_id = ?,
               email_last_inbound_at = ?,
               email_last_outbound_at = ?,
               updated_at = datetime('now')
           WHERE item_id = ?""",
        (thread_id, row["last_in"], row["last_out"], item_id),
    )

    # Back-link: write email_threads.action_item_id in the same transaction.
    # Also clears any stale action_item_id that other threads may be holding for
    # this item so the one-to-one invariant survives re-links.
    c.execute(
        """UPDATE email_threads
           SET action_item_id = NULL, updated_at = datetime('now')
           WHERE action_item_id = ? AND thread_id != ?""",
        (item_id, thread_id),
    )
    c.execute(
        """UPDATE email_threads
           SET action_item_id = ?, updated_at = datetime('now')
           WHERE thread_id = ?""",
        (item_id, thread_id),
    )
    back_rows = c.rowcount
    conn.commit()
    print(f"Linked {item_id} -> {thread_id}")
    print(f"  Last inbound:  {row['last_in']}")
    print(f"  Last outbound: {row['last_out']}")
    if back_rows == 0:
        print(f"  WARNING: thread {thread_id} not in email_threads table; back-link skipped")
    else:
        print(f"  Back-link: email_threads.action_item_id = {item_id}")
    conn.close()
    return True


def unlink_action_item(item_id: str):
    conn = connect()
    c = conn.cursor()
    c.execute(
        """UPDATE action_items
           SET email_thread_id = NULL,
               email_last_inbound_at = NULL,
               email_last_outbound_at = NULL,
               updated_at = datetime('now')
           WHERE item_id = ?""",
        (item_id,),
    )
    ai_rows = c.rowcount
    c.execute(
        """UPDATE email_threads
           SET action_item_id = NULL, updated_at = datetime('now')
           WHERE action_item_id = ?""",
        (item_id,),
    )
    et_rows = c.rowcount
    conn.commit()
    print(f"Unlinked {item_id} (action_items={ai_rows}, email_threads={et_rows})")
    conn.close()


def sync_linked():
    """Refresh email_last_* fields for all linked action items."""
    conn = connect()
    c = conn.cursor()
    c.execute(
        """SELECT item_id, email_thread_id FROM action_items
           WHERE email_thread_id IS NOT NULL AND status IN ('OPEN','WAITING')"""
    )
    rows = c.fetchall()
    updated = 0
    for r in rows:
        c.execute(
            """SELECT MAX(CASE WHEN is_outgoing = 0 THEN timestamp END) AS last_in,
                      MAX(CASE WHEN is_outgoing = 1 THEN timestamp END) AS last_out
               FROM gmail.messages WHERE thread_id = ? AND is_deleted = 0""",
            (r["email_thread_id"],),
        )
        t = c.fetchone()
        if t:
            c.execute(
                """UPDATE action_items
                   SET email_last_inbound_at = ?,
                       email_last_outbound_at = ?
                   WHERE item_id = ?""",
                (t["last_in"], t["last_out"], r["item_id"]),
            )
            updated += 1
    conn.commit()
    conn.close()
    print(f"Synced {updated} linked items")


def autoclose():
    """
    Surface two kinds of auto-close candidates (no auto-resolve, just print):

    Section 1 - linked action_items: OPEN/WAITING items with email_thread_id set
    whose latest gmail message is outbound.

    Section 2 - orphan pending threads: email_threads.status='pending' where we
    have already replied (last_outbound_ts > last_inbound_ts, or inbound_count=0
    with outbound_count>0) but the thread is NOT linked to any action_item. These
    are the threads that should be re-classified to 'replied' or 'no_action_needed'.
    """
    conn = connect()
    c = conn.cursor()
    c.execute(
        """SELECT ai.item_id, ai.status, ai.priority, ai.description,
                  ai.email_thread_id,
                  (SELECT timestamp FROM gmail.messages
                   WHERE thread_id = ai.email_thread_id AND is_deleted = 0
                   ORDER BY timestamp DESC LIMIT 1) AS latest_ts,
                  (SELECT is_outgoing FROM gmail.messages
                   WHERE thread_id = ai.email_thread_id AND is_deleted = 0
                   ORDER BY timestamp DESC LIMIT 1) AS latest_out
           FROM action_items ai
           WHERE ai.email_thread_id IS NOT NULL
             AND ai.status IN ('OPEN', 'WAITING')"""
    )
    rows = c.fetchall()
    item_candidates = [r for r in rows if r["latest_out"] == 1]

    print("=== Section 1: linked action_items with outbound reply ===")
    if not item_candidates:
        print("  None.")
    else:
        print(f"  {len(item_candidates)} item(s). Review and resolve:")
        for r in item_candidates:
            desc = (r["description"] or "")[:70]
            print(f"    [{r['status']}] {r['item_id']} | {desc}")
            print(f"        Latest message: {(r['latest_ts'] or '')[:16]} (outbound)")
            print(f"        Run: task_manager.py resolve {r['item_id']} \"reply sent\"")

    # Section 2: orphan pending threads.
    # Exclude threads already linked from the action_items side so we don't
    # double-print items already shown in Section 1.
    c.execute(
        """SELECT et.thread_id, et.subject, et.topic_tag, et.context_slug,
                  et.person_name_cached, et.last_inbound_ts, et.last_outbound_ts,
                  et.inbound_count, et.outbound_count
           FROM email_threads et
           WHERE et.status = 'pending'
             AND (
                 (et.last_outbound_ts IS NOT NULL
                  AND et.last_inbound_ts IS NOT NULL
                  AND et.last_outbound_ts > et.last_inbound_ts)
                 OR
                 (COALESCE(et.inbound_count, 0) = 0
                  AND COALESCE(et.outbound_count, 0) > 0)
             )
             AND et.thread_id NOT IN (
                 SELECT email_thread_id FROM action_items
                 WHERE email_thread_id IS NOT NULL
             )
           ORDER BY et.last_outbound_ts DESC"""
    )
    thread_candidates = c.fetchall()

    print()
    print("=== Section 2: pending email_threads, outbound-last, no action_item ===")
    if not thread_candidates:
        print("  None.")
    else:
        print(f"  {len(thread_candidates)} thread(s). Re-classify to replied or no_action_needed:")
        for r in thread_candidates:
            subj = (r["subject"] or "")[:60]
            person = (r["person_name_cached"] or "")[:25]
            tag = r["topic_tag"] or "-"
            slug = r["context_slug"] or "-"
            last_out = (r["last_outbound_ts"] or "")[:16]
            print(f"    {r['thread_id']} | {tag:20} | {slug:25} | {person:25} | {subj}")
            print(f"        last_outbound={last_out} in={r['inbound_count']} out={r['outbound_count']}")
            print(f"        Run: email_tracker.py set_status {r['thread_id']} replied")

    total = len(item_candidates) + len(thread_candidates)
    print()
    print(f"Total candidates: {total} ({len(item_candidates)} items + {len(thread_candidates)} threads)")
    conn.close()


def _thread_exists(c, thread_id: str) -> bool:
    c.execute("SELECT 1 FROM email_threads WHERE thread_id = ?", (thread_id,))
    return c.fetchone() is not None


def set_thread_status(thread_id: str, status: str):
    if status not in VALID_STATUSES:
        print(f"Invalid status: {status}")
        print(f"Allowed: {', '.join(sorted(VALID_STATUSES))}")
        return False
    conn = connect()
    c = conn.cursor()
    if not _thread_exists(c, thread_id):
        print(f"Thread {thread_id} not found in email_threads")
        conn.close()
        return False
    c.execute(
        """UPDATE email_threads
           SET status = ?, updated_at = datetime('now')
           WHERE thread_id = ?""",
        (status, thread_id),
    )
    conn.commit()
    print(f"set_status {thread_id} -> {status} ({c.rowcount} row)")
    conn.close()
    return True


def set_thread_topic(thread_id: str, tag: str):
    conn = connect()
    c = conn.cursor()
    if not _thread_exists(c, thread_id):
        print(f"Thread {thread_id} not found in email_threads")
        conn.close()
        return False
    c.execute(
        """UPDATE email_threads
           SET topic_tag = ?, updated_at = datetime('now')
           WHERE thread_id = ?""",
        (tag, thread_id),
    )
    conn.commit()
    print(f"set_topic {thread_id} -> {tag} ({c.rowcount} row)")
    conn.close()
    return True


def set_thread_context(thread_id: str, slug: str):
    """Set email_threads.context_slug: a free-form project/event/category tag
    the operator uses to group threads (no registry table is enforced)."""
    slug = (slug or "").strip()
    if not slug:
        print("Empty context slug")
        return False
    conn = connect()
    c = conn.cursor()
    if not _thread_exists(c, thread_id):
        print(f"Thread {thread_id} not found in email_threads")
        conn.close()
        return False
    c.execute(
        """UPDATE email_threads
           SET context_slug = ?, updated_at = datetime('now')
           WHERE thread_id = ?""",
        (slug, thread_id),
    )
    conn.commit()
    print(f"set_context {thread_id} -> {slug} ({c.rowcount} row)")
    conn.close()
    return True


def set_thread_person(thread_id: str, person_id: int):
    conn = connect()
    c = conn.cursor()
    c.execute("SELECT name FROM people WHERE id = ?", (person_id,))
    row = c.fetchone()
    if not row:
        print(f"person_id {person_id} not in people table")
        conn.close()
        return False
    if not _thread_exists(c, thread_id):
        print(f"Thread {thread_id} not found in email_threads")
        conn.close()
        return False
    c.execute(
        """UPDATE email_threads
           SET person_id = ?, person_name_cached = ?, updated_at = datetime('now')
           WHERE thread_id = ?""",
        (person_id, row["name"], thread_id),
    )
    conn.commit()
    print(f"set_person {thread_id} -> {person_id} ({row['name']}) ({c.rowcount} row)")
    conn.close()
    return True


def show_status(item_id: str):
    """Show the thread state for a single action item."""
    conn = connect()
    c = conn.cursor()
    c.execute(
        """SELECT ai.*, m.subject
           FROM action_items ai
           LEFT JOIN gmail.messages m ON m.message_id = (
               SELECT message_id FROM gmail.messages
               WHERE thread_id = ai.email_thread_id AND is_deleted = 0
               ORDER BY timestamp DESC LIMIT 1
           )
           WHERE ai.item_id = ?""",
        (item_id,),
    )
    r = c.fetchone()
    if not r:
        print(f"Not found: {item_id}")
        return

    print(f"Item: {r['item_id']}")
    print(f"Status: {r['status']} / {r['priority']}")
    print(f"Description: {(r['description'] or '')[:100]}")
    print(f"Thread: {r['email_thread_id'] or '(none)'}")
    if r["email_thread_id"]:
        print(f"Subject: {r['subject']}")
        print(f"Last inbound:  {r['email_last_inbound_at']}")
        print(f"Last outbound: {r['email_last_outbound_at']}")
        ball = "on us (inbound last)" if (
            r["email_last_inbound_at"] and
            (not r["email_last_outbound_at"] or
             r["email_last_inbound_at"] > r["email_last_outbound_at"])
        ) else "on them (outbound last)"
        print(f"Ball: {ball}")
    conn.close()


def print_inbox(rows, show_body: bool = False):
    if not rows:
        print("Inbox is clear.")
        return

    now = datetime.now()
    print(f"\n{len(rows)} unreplied thread(s):\n")
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00").split("+")[0])
            days = (now - ts).days
        except Exception:
            days = "?"
        linked = f"[{r['item_id']}]" if r["item_id"] else "[unlinked]"
        name = sender_label(r["sender"])[:25]
        subj = (r["subject"] or "")[:60]
        print(f"  {days}d {linked:22} {name:25} | {subj}")
        if show_body and r.get("body"):
            body = (r["body"] or "").strip().split("\n")[0][:100]
            print(f"         >> {body}")
    print()


# --- Stop words for keyword extraction ---------------------------------------

STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "be", "as", "was", "are",
    "this", "that", "will", "not", "all", "has", "had", "have", "been",
    "do", "does", "did", "can", "could", "would", "should", "may",
    "if", "so", "no", "up", "out", "about", "into", "its", "we", "us",
    "our", "they", "them", "their", "he", "she", "his", "her",
    # Common filler/request words
    "please", "would", "just", "let", "know", "like", "want", "take",
    "make", "time", "some", "one", "any", "use", "per", "see",
    # Domain-common words that aren't distinctive enough to match on
    "email", "send", "follow", "update", "check", "need", "get",
    "reply", "still", "yet", "also", "new", "first", "next",
    "apr", "mar", "jun", "jul",
}


def _extract_keywords(text: str) -> set:
    """Extract significant keywords from text, filtering stop words."""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]+", text)
    return {t.lower() for t in tokens if len(t) > 2 and t.lower() not in STOP_WORDS}


# Non-name phrases that look like "First Last". Operator-tunable: add the
# capitalized two-word phrases from your own domain that keep false-matching.
_NON_NAMES = {
    # Geography / topics that pattern-match as names
    "global south", "latin america", "middle east", "united states",
    # Tooling phrases
    "google sheet", "google form", "google doc", "loom video",
    "job candidate",
    # Common ops/meeting phrases
    "follow up", "action item", "open items", "next step",
    "help desk", "slack channel", "discord server",
    "zoom call", "office hours", "board member", "team lead",
}


def _extract_person_names(text: str) -> list:
    """
    Extract likely person names from action item text.
    Requires at least First Last (2 capitalized words) to avoid generic matches.
    Supports hyphenated surnames, e.g. (fictional) "Jane Doe", "Anna Smith-Jones",
    "Maria Garcia Lopez".
    """
    names = []
    # Capitalized word groups, allowing hyphens within words
    for m in re.finditer(
        r"\b([A-Z][a-z]+(?:-[A-Z][a-z]+)*(?:\s+[A-Z][a-z]+(?:-[A-Z][a-z]+)*)+)\b",
        text,
    ):
        candidate = m.group(1)
        if candidate.lower() not in _NON_NAMES:
            names.append(candidate)
    return names


def _lookup_person_ids(conn, names: list) -> set:
    """Look up person IDs from a list of full name strings."""
    person_ids = set()
    for name in names:
        # Only match on full names (require space), and use exact-ish matching
        if " " not in name:
            continue
        parts = name.split()
        # Match: first name starts with first part, last name starts with last part
        first, last = parts[0], parts[-1]
        rows = conn.execute(
            "SELECT id FROM people WHERE name LIKE ? AND name LIKE ? LIMIT 3",
            (f"{first}%", f"%{last}%"),
        ).fetchall()
        for r in rows:
            if r["id"] not in _SELF_PERSON_IDS:
                person_ids.add(r["id"])
    return person_ids


def autolink(dry_run: bool = False, verbose: bool = False):
    """
    Auto-link unlinked OPEN/WAITING action items to email threads.

    Scoring:
      - Person ID match (waiting_on_person_id or extracted names): 0.45
      - Subject keyword overlap (Jaccard): up to 0.35
      - Context slug match: 0.15
      - Recency (thread active near item creation): 0.05

    Thresholds:
      - 0.7+: auto-link (unless dry_run)
      - 0.4-0.7: print for manual review
    """
    conn = connect()

    # Get unlinked OPEN/WAITING action items
    items = conn.execute("""
        SELECT item_id, description, waiting_on, waiting_on_person_id,
               context_slug, inserted_at
        FROM action_items
        WHERE status IN ('OPEN', 'WAITING')
          AND email_thread_id IS NULL
    """).fetchall()

    # Get all email threads with enough data to match
    threads = conn.execute("""
        SELECT thread_id, subject, person_id, person_name_cached,
               context_slug, last_inbound_ts, last_outbound_ts
        FROM email_threads
        WHERE subject IS NOT NULL AND subject != ''
    """).fetchall()

    # Pre-compute thread keyword sets
    thread_kw = {}
    for t in threads:
        thread_kw[t["thread_id"]] = _extract_keywords(t["subject"] or "")

    auto_linked = 0
    review_candidates = []
    claimed_threads = {}  # thread_id -> (item_id, score) to prevent duplicates

    print(f"\n=== Autolink: {len(items)} unlinked items, {len(threads)} threads ===\n")

    for item in items:
        desc = item["description"] or ""
        waiting_on = item["waiting_on"] or ""
        full_text = f"{desc} {waiting_on}"

        # Extract signals from action item
        item_keywords = _extract_keywords(full_text)
        person_names = _extract_person_names(full_text)
        person_ids = set()

        if item["waiting_on_person_id"] and item["waiting_on_person_id"] not in _SELF_PERSON_IDS:
            person_ids.add(item["waiting_on_person_id"])
        person_ids |= _lookup_person_ids(conn, person_names)
        person_ids -= _SELF_PERSON_IDS

        item_ctx = item["context_slug"] or ""
        item_date = item["inserted_at"] or ""

        best_score = 0.0
        best_thread = None
        best_breakdown = {}

        for t in threads:
            score = 0.0
            breakdown = {}

            # 1a. Person ID match (0.45) - strongest signal
            if person_ids and t["person_id"]:
                if t["person_id"] in person_ids:
                    score += 0.45
                    breakdown["person_id"] = 0.45

            # 1b. Person name in subject fallback (0.35) - when thread has no person_id
            if not breakdown.get("person_id") and person_names:
                subj_lower = (t["subject"] or "").lower()
                for pname in person_names:
                    # Check if full name (first + last) appears in subject
                    if " " in pname and pname.lower() in subj_lower:
                        score += 0.35
                        breakdown["name_in_subj"] = 0.35
                        break

            # 2. Subject keyword overlap - Jaccard (up to 0.35)
            t_kw = thread_kw[t["thread_id"]]
            if item_keywords and t_kw:
                intersection = item_keywords & t_kw
                union = item_keywords | t_kw
                jaccard = len(intersection) / len(union) if union else 0
                kw_score = min(jaccard * 1.4, 1.0) * 0.35  # Scale up small overlaps
                if kw_score > 0.02:
                    score += kw_score
                    breakdown["keywords"] = round(kw_score, 3)
                    if verbose:
                        breakdown["kw_overlap"] = sorted(intersection)[:5]

            # 3. Context slug match (0.15)
            if item_ctx and t["context_slug"] and item_ctx == t["context_slug"]:
                score += 0.15
                breakdown["context"] = 0.15

            # 4. Recency (0.05) - thread active within 14 days of item creation
            if item_date and t["last_inbound_ts"]:
                try:
                    item_dt = datetime.fromisoformat(item_date[:19])
                    thread_dt = datetime.fromisoformat(t["last_inbound_ts"][:19])
                    days_diff = abs((item_dt - thread_dt).days)
                    if days_diff <= 14:
                        recency = (14 - days_diff) / 14 * 0.05
                        score += recency
                        breakdown["recency"] = round(recency, 3)
                except (ValueError, TypeError):
                    pass

            if score > best_score:
                best_score = score
                best_thread = t
                best_breakdown = breakdown

        if best_score < 0.4:
            continue

        tid = best_thread["thread_id"]

        # Thread dedup: skip if a higher-scoring item already claimed this thread
        if tid in claimed_threads:
            prev_item, prev_score = claimed_threads[tid]
            if prev_score >= best_score:
                continue  # prior claim wins

        desc_short = desc[:60]
        subj = (best_thread["subject"] or "")[:50]
        person = best_thread["person_name_cached"] or ""

        if best_score >= 0.7:
            claimed_threads[tid] = (item["item_id"], best_score)
            if dry_run:
                print(f"  [AUTO {best_score:.2f}] {item['item_id']} -> {tid}")
                print(f"    Item: {desc_short}")
                print(f"    Thread: {subj} ({person})")
                print(f"    Scores: {best_breakdown}")
            else:
                # Clear stale back-links (match link_action_item behavior)
                conn.execute(
                    """UPDATE email_threads
                       SET action_item_id = NULL, updated_at = datetime('now')
                       WHERE action_item_id = ? AND thread_id != ?""",
                    (item["item_id"], tid),
                )
                # Get email timestamps from gmail.messages
                ts_row = conn.execute(
                    """SELECT MAX(CASE WHEN is_outgoing = 0 THEN timestamp END) AS last_in,
                              MAX(CASE WHEN is_outgoing = 1 THEN timestamp END) AS last_out
                       FROM gmail.messages WHERE thread_id = ? AND is_deleted = 0""",
                    (tid,),
                ).fetchone()
                last_in = ts_row["last_in"] if ts_row else None
                last_out = ts_row["last_out"] if ts_row else None

                # Link action_item -> thread with timestamps
                conn.execute(
                    """UPDATE action_items
                       SET email_thread_id = ?,
                           email_last_inbound_at = ?,
                           email_last_outbound_at = ?,
                           updated_at = datetime('now')
                       WHERE item_id = ?""",
                    (tid, last_in, last_out, item["item_id"]),
                )
                # Back-link: thread -> action_item
                conn.execute(
                    """UPDATE email_threads
                       SET action_item_id = ?, updated_at = datetime('now')
                       WHERE thread_id = ?""",
                    (item["item_id"], tid),
                )
                auto_linked += 1
                print(f"  LINKED [{best_score:.2f}] {item['item_id']} -> {tid}")
                print(f"    Item: {desc_short}")
                print(f"    Thread: {subj} ({person})")
                print(f"    Scores: {best_breakdown}")
        else:
            review_candidates.append({
                "item_id": item["item_id"],
                "desc": desc_short,
                "thread_id": tid,
                "subject": subj,
                "person": person,
                "score": best_score,
                "breakdown": best_breakdown,
            })

    if not dry_run:
        conn.commit()

    if review_candidates:
        print(f"\n--- Manual Review ({len(review_candidates)} candidates, 0.4-0.7) ---\n")
        for c in sorted(review_candidates, key=lambda x: -x["score"]):
            print(f"  [{c['score']:.2f}] {c['item_id']} -> {c['thread_id']}")
            print(f"    Item: {c['desc']}")
            print(f"    Thread: {c['subject']} ({c['person']})")
            print(f"    Scores: {c['breakdown']}")
            print(f"    Run: email_tracker.py link {c['item_id']} {c['thread_id']}")
            print()

    print(f"\nAutolink: {auto_linked} linked, {len(review_candidates)} for review")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Email thread tracker")
    sub = parser.add_subparsers(dest="cmd")

    p_inbox = sub.add_parser("inbox", help="List unreplied threads")
    p_inbox.add_argument("--linked", action="store_true")
    p_inbox.add_argument("--days", type=int, default=14)
    p_inbox.add_argument("--body", action="store_true")

    p_link = sub.add_parser("link", help="Link action item to thread")
    p_link.add_argument("item_id")
    p_link.add_argument("thread_id")

    p_unlink = sub.add_parser("unlink", help="Unlink action item")
    p_unlink.add_argument("item_id")

    sub.add_parser("sync", help="Sync all linked items")
    sub.add_parser("autoclose", help="Find items with replies to auto-close")

    p_auto = sub.add_parser("autolink", help="Auto-link action items to email threads")
    p_auto.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Show matches without linking")
    p_auto.add_argument("--verbose", action="store_true",
                        help="Show keyword overlap details")

    p_status = sub.add_parser("status", help="Show thread state for item")
    p_status.add_argument("item_id")

    p_sstat = sub.add_parser("set_status", help="Set email_threads.status")
    p_sstat.add_argument("thread_id")
    p_sstat.add_argument("status")

    p_stop = sub.add_parser("set_topic", help="Set email_threads.topic_tag")
    p_stop.add_argument("thread_id")
    p_stop.add_argument("tag")

    p_sctx = sub.add_parser("set_context", help="Set email_threads.context_slug (free-form project/event tag)")
    p_sctx.add_argument("thread_id")
    p_sctx.add_argument("slug")

    p_spers = sub.add_parser("set_person", help="Set email_threads.person_id")
    p_spers.add_argument("thread_id")
    p_spers.add_argument("person_id", type=int)

    args = parser.parse_args()

    if args.cmd == "inbox":
        rows = get_unreplied_threads(days=args.days, linked_only=args.linked)
        print_inbox(rows, show_body=args.body)
    elif args.cmd == "link":
        link_action_item(args.item_id, args.thread_id)
    elif args.cmd == "unlink":
        unlink_action_item(args.item_id)
    elif args.cmd == "sync":
        sync_linked()
    elif args.cmd == "autoclose":
        autoclose()
    elif args.cmd == "autolink":
        autolink(dry_run=args.dry_run, verbose=args.verbose)
    elif args.cmd == "status":
        show_status(args.item_id)
    elif args.cmd == "set_status":
        set_thread_status(args.thread_id, args.status)
    elif args.cmd == "set_topic":
        set_thread_topic(args.thread_id, args.tag)
    elif args.cmd == "set_context":
        set_thread_context(args.thread_id, args.slug)
    elif args.cmd == "set_person":
        set_thread_person(args.thread_id, args.person_id)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
