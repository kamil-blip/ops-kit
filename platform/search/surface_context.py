"""Auto-surface context for a text blob.

Use case: you read an inbound (email body, chat message, the operator's
prompt to their assistant). Run this BEFORE drafting a reply. It returns
the auto-context block you would otherwise grind through 5 SQL queries to
assemble:

  - Sender dossier (per person mentioned): last contact, recent threads,
    tags, open action items, 1-hop graph neighbors.
  - FAQ matches for every question detected in the text (hybrid FTS+semantic).
  - Event references (operator-configured alias -> slug) if any are mentioned.
  - Thread reference (last 3 messages on the same Gmail thread, if
    --thread-id is passed).
  - Suggested canonical reply-template slug based on detected intent.

Usage:
  cat email_body.txt | surface_context.py                 # text on stdin
  surface_context.py --text "Hi Jane, when is the submission deadline?"
  surface_context.py --email-id 12345                     # pull body from emails table
  surface_context.py --thread-id 18ab34cd56ef7890         # last 3 msgs on thread
  surface_context.py --json --text "..."                  # JSON output
  surface_context.py --names "Jane Doe,Sam Roe"           # explicit name list

Performance: ~3-5s including model load (cached after first call).
"""
import argparse
import io
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    import paths  # repo path resolver (core/paths.py); on sys.path via the installer's .pth
except ImportError:
    # Direct-invocation fallback: walk up from this file to the repo root and
    # put core/ on sys.path (the installed venv normally does this via a .pth).
    sys.path.insert(0, str(next(
        p / "core" for p in Path(__file__).resolve().parents
        if (p / "core" / "paths.py").is_file()
    )))
    import paths
import _db  # unified connector (busy_timeout + FK ON)
import config

# Quiet HF / transformers / tqdm progress bars BEFORE imports (they corrupt wrapped stdout)
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# UTF-8 wrap stdout/stderr early (before any model load)
# reconfigure IN PLACE (never swap the stream object -- replacing sys.stdout
# at import time discards the importer's unflushed output and breaks streams
# without .buffer; fatal for a stdio MCP host).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, io.UnsupportedOperation):
    pass

# faq_lookup ships in comms/ (flat import; the installer's .pth covers it).
# Soft import: FAQ matching degrades to a skip-note when the comms capability
# isn't installed, instead of breaking the whole surface pass.
try:
    from faq_lookup import lookup as faq_lookup
except ImportError:
    try:
        sys.path.insert(0, str(paths.ROOT / "comms"))
        from faq_lookup import lookup as faq_lookup
    except ImportError:
        faq_lookup = None

DB = str(paths.DB_PATH)

# Detect potential person names: 1-4 capitalized words in a row, not at sentence start.
# {0,3} allows a lone first name ("Hi Joe,"); single-token candidates are then
# FTS-disambiguated against people_fts in detect_names (kept only on a hit) so common
# capitalized words ("Update", "Meeting") don't slip through.
NAME_RE = re.compile(r"\b([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20}){0,3})\b")

# Sentence-end detection
SENT_END_RE = re.compile(r"(?<=[.!?])\s+")

# Question detection (lightweight opener heuristics)
QUESTION_OPENERS = (
    "how ", "what ", "when ", "where ", "why ", "who ", "can ", "could ",
    "should ", "would ", "will ", "do ", "does ", "is ", "are ", "was ",
    "were ", "may ", "might ",
)

# Common false-positive name fragments. Add your own org/brand name tokens
# here so the org name in signatures doesn't read as a person (FICTIONAL
# example: "Acme", "Acme Labs").
NAME_STOPWORDS = {
    "Hi", "Hey", "Hello", "Dear", "Thanks", "Thank", "Best", "Regards",
    "Sent", "From", "To", "Subject", "Date",
    "Cc", "Bcc", "Re", "Fwd", "AI", "ML",
    "Google", "Zoom", "Discord", "Slack", "LinkedIn", "Twitter", "Facebook",
    "GitHub", "YouTube",
}

# people.is_* boolean columns that are NOT roles (structural flags).
_NON_ROLE_FLAGS = {"is_real_person", "is_internal"}
_role_flag_cols = None


def get_conn():
    conn = _db.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


def _people_role_flag_columns(conn):
    """Boolean is_* role-flag columns that actually exist on the people table.

    Feature-detected so an operator can add or remove flag columns
    (is_mentor, is_reviewer, ...) without touching this module. Structural
    flags that aren't roles are excluded."""
    global _role_flag_cols
    if _role_flag_cols is None:
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(people)")]
        except sqlite3.Error:
            cols = []
        _role_flag_cols = sorted(
            c for c in cols if c.startswith("is_") and c not in _NON_ROLE_FLAGS
        )
    return _role_flag_cols


def _people_fts_hit(conn, token):
    try:
        return conn.execute("SELECT 1 FROM people_fts WHERE people_fts MATCH ? LIMIT 1",
                            (token,)).fetchone() is not None
    except Exception:
        # Fall back to a direct name prefix match if FTS is unavailable.
        try:
            return conn.execute("SELECT 1 FROM people WHERE name LIKE ? LIMIT 1",
                                (token + " %",)).fetchone() is not None or \
                   conn.execute("SELECT 1 FROM people WHERE name=? LIMIT 1", (token,)).fetchone() is not None
        except Exception:
            return False


def detect_names(text, limit=8, conn=None):
    """Heuristic name detection. Returns up to `limit` candidate names.

    Multi-word candidates pass the stopword/acronym filter as before. A lone
    single-token candidate ("Joe") is kept ONLY if it hits people_fts,
    so common capitalized words don't become false-positive names.
    """
    if not text:
        return []
    # Strip quoted reply tail
    cleaned = re.sub(r"^>.*$", "", text, flags=re.M)
    cleaned = re.sub(r"On .+? wrote:.*$", "", cleaned, flags=re.S)
    candidates = NAME_RE.findall(cleaned)
    own_conn = False
    seen = []
    for c in candidates:
        if c.isupper():  # all-caps acronym
            continue
        toks = c.strip().split()
        # Strip leading/trailing stopword tokens so "Hi Joe" -> "Joe" (greedy NAME_RE
        # merges the greeting in); drop the candidate if an interior token is a stopword.
        while toks and toks[0] in NAME_STOPWORDS:
            toks = toks[1:]
        while toks and toks[-1] in NAME_STOPWORDS:
            toks = toks[:-1]
        if not toks or any(t in NAME_STOPWORDS for t in toks):
            continue
        cn = " ".join(toks)
        if cn in seen:
            continue
        if len(toks) == 1:
            # single token: require a people_fts hit to keep it
            if conn is None:
                conn = get_conn(); own_conn = True
            if not _people_fts_hit(conn, cn):
                continue
        seen.append(cn)
    if own_conn:
        conn.close()
    return seen[:limit]


_GREETING_PREFIX_RE = re.compile(
    r"^(hi|hey|hello|dear|thanks|thank you,?|hi there|hey there)[\s,]+[A-Za-z]+,?\s+",
    re.IGNORECASE,
)
_ALSO_PREFIX_RE = re.compile(r"^(also|btw|and|oh,?|sorry,?|quick question[:,]?)\s+", re.IGNORECASE)


def _strip_question_prefix(s):
    """Remove greeting / opener prefixes that hurt semantic matching."""
    s = _GREETING_PREFIX_RE.sub("", s)
    s = _ALSO_PREFIX_RE.sub("", s)
    return s.strip()


def detect_questions(text, limit=4):
    """Extract candidate question sentences. Any sentence ending in '?' of
    reasonable length counts; we don't filter on openers because real
    questions can begin with anything ('Also, do you...', 'I'm wondering if...').
    Strips leading greetings/openers (Hi NAME, Also, BTW, etc.) so semantic
    matching against canonical FAQs isn't polluted by salutations.
    """
    if not text or "?" not in text:
        return []
    cleaned = re.sub(r"^>.*$", "", text, flags=re.M)
    cleaned = re.sub(r"On .+? wrote:.*$", "", cleaned, flags=re.S)
    sentences = SENT_END_RE.split(cleaned)
    questions = []
    for s in sentences:
        s = s.strip()
        if not s.endswith("?"):
            continue
        if not (8 <= len(s) <= 500):
            continue
        stripped = _strip_question_prefix(s)
        if 5 <= len(stripped) <= 500:
            questions.append(stripped)
        else:
            questions.append(s)  # keep original if strip would over-shrink
    return questions[:limit]


def detect_event_slugs(text):
    """Match text against operator-configured event/topic aliases with word
    boundaries. Aliases live in config.toml under [events.aliases] as an
    alias -> slug table. FICTIONAL example:

        [events.aliases]
        "spring summit" = "spring-summit-2027"
        "summit" = "spring-summit-2027"

    Short aliases (under 3 chars) are skipped: word-boundary matching alone
    can't keep two-letter tokens from false-positiving inside other words.
    Ships empty; returns [] until the operator configures aliases."""
    aliases = config.section("events").get("aliases", {}) or {}
    hits = []
    for alias, slug in aliases.items():
        alias = (str(alias) or "").strip()
        if len(alias) < 3:
            continue
        # Word-boundary regex, case-insensitive
        pattern = r"\b" + re.escape(alias) + r"\b"
        if re.search(pattern, text, re.IGNORECASE):
            hits.append(slug)
    return list(dict.fromkeys(hits))


def resolve_person(conn, name):
    """Resolve a name string to a people row. Tries:
      1. exact match on people.name (case-insensitive)
      2. people_fts AND-tokens
      3. people_fts OR-tokens (lower precision fallback)
    Returns the best match or None.
    """
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", name) if t]
    if not tokens:
        return None

    role_cols = _people_role_flag_columns(conn)
    role_select = "".join(f", p.{c}" for c in role_cols)

    # Try exact name match first (cheap, high-precision)
    row = conn.execute(f"""
        SELECT p.id, p.name, p.email, p.headline,
               p.last_contact_date, p.tags, p.notes{role_select}
        FROM people p
        WHERE LOWER(p.name) = LOWER(?)
        LIMIT 1
    """, (name.strip(),)).fetchone()
    if row:
        d = dict(row)
        d["score"] = 0.0
        return d

    # FTS5 fallback with AND, then OR.
    fts_q_and = " AND ".join(tokens)
    fts_q_or = " OR ".join(tokens)
    for fts_q in (fts_q_and, fts_q_or):
        try:
            row = conn.execute(f"""
                SELECT p.id, p.name, p.email, p.headline,
                       p.last_contact_date, p.tags, p.notes{role_select},
                       bm25(people_fts) AS score
                FROM people_fts
                JOIN people p ON p.id = people_fts.rowid
                WHERE people_fts MATCH ?
                ORDER BY score ASC LIMIT 1
            """, (fts_q,)).fetchone()
            if row:
                return dict(row)
        except sqlite3.Error:
            continue
    return None


def graph_neighbors(conn, person_id, limit=8):
    """1-hop edge traversal from a person entity.

    Returns connections grouped by relation type. The entity_id for a person
    in the entities/edges graph is 'person-<id>'. Edges can be outgoing
    (source=person-X, target=Y) or incoming (source=Y, target=person-X).
    """
    eid = f"person-{person_id}"
    neighbors = []
    try:
        # Outgoing edges
        for r in conn.execute("""
            SELECT e.relation, e.target_id, e.fact, e.valid_from,
                   COALESCE(en.name, e.target_id) AS target_name,
                   en.type AS target_type
            FROM edges e
            LEFT JOIN entities en ON en.id = e.target_id
            WHERE e.source_id = ?
            ORDER BY e.valid_from DESC NULLS LAST, e.id DESC
            LIMIT ?
        """, (eid, limit)).fetchall():
            neighbors.append({
                "direction": "out",
                "relation": r[0],
                "other_id": r[1],
                "other_name": r[4],
                "other_type": r[5],
                "fact": (r[2] or "")[:120],
                "valid_from": r[3],
            })
        # Incoming edges
        for r in conn.execute("""
            SELECT e.relation, e.source_id, e.fact, e.valid_from,
                   COALESCE(en.name, e.source_id) AS source_name,
                   en.type AS source_type
            FROM edges e
            LEFT JOIN entities en ON en.id = e.source_id
            WHERE e.target_id = ?
            ORDER BY e.valid_from DESC NULLS LAST, e.id DESC
            LIMIT ?
        """, (eid, limit)).fetchall():
            neighbors.append({
                "direction": "in",
                "relation": r[0],
                "other_id": r[1],
                "other_name": r[4],
                "other_type": r[5],
                "fact": (r[2] or "")[:120],
                "valid_from": r[3],
            })
    except sqlite3.Error:
        pass
    return neighbors


def person_dossier(conn, person_id, limit_threads=3, limit_actions=3):
    """Quick dossier for a person: recent threads, open action items, graph neighbors."""
    dossier = {}

    # Recent thread subjects: pull person's emails (people.email + person_emails)
    emails = [r[0] for r in conn.execute(
        "SELECT email FROM people WHERE id=? UNION SELECT email FROM person_emails WHERE person_id=?",
        (person_id, person_id)
    ).fetchall() if r[0]]
    if emails:
        placeholders = ",".join("?" * len(emails))
        threads = conn.execute(f"""
            SELECT DISTINCT subject, MAX(timestamp) AS ts, thread_id
            FROM emails
            WHERE (sender_email IN ({placeholders})
                   OR EXISTS (SELECT 1 FROM emails e2 WHERE e2.id=emails.id
                              AND ({" OR ".join("e2.recipients_json LIKE '%' || ? || '%'" for _ in emails)})))
              AND subject IS NOT NULL
            GROUP BY subject
            ORDER BY ts DESC LIMIT ?
        """, emails + emails + [limit_threads]).fetchall()
        dossier["recent_threads"] = [dict(r) for r in threads]
    else:
        dossier["recent_threads"] = []

    # Open action items (about_person_id is the canonical "who is this about" link)
    actions = conn.execute("""
        SELECT id, item_id, description, status, priority, due_date, urgency_score
        FROM action_items
        WHERE (about_person_id = ? OR source_person_id = ? OR waiting_on_person_id = ?)
          AND status NOT IN ('DONE', 'CANCELLED', 'RESOLVED', 'REMOVED')
        ORDER BY COALESCE(urgency_score, 0) DESC, priority ASC
        LIMIT ?
    """, (person_id, person_id, person_id, limit_actions)).fetchall()
    dossier["open_action_items"] = [dict(r) for r in actions]

    # Graph neighbors (1-hop on edges table). Surfaces orgs, events,
    # teammates, and affiliation links that would otherwise stay implicit.
    dossier["graph_neighbors"] = graph_neighbors(conn, person_id, limit=10)

    return dossier


def thread_context(conn, thread_id, n=3):
    """Return the last N messages on a thread."""
    rows = conn.execute("""
        SELECT id, sender_name, sender_email, subject, timestamp,
               SUBSTR(body, 1, 300) AS snippet, is_outgoing
        FROM emails
        WHERE thread_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (thread_id, n)).fetchall()
    return [dict(r) for r in rows]


# Intent -> suggested canonical template slug. Slugs name reference_docs
# entries the operator maintains (their own reply templates); rename them to
# match your template registry. Ships with generic starter intents; extend
# with your own domain's vocabulary.
INTENT_RULES = [
    # (regex pattern (case-insensitive), suggested template slug, reason)
    (r"would.*you.*be.*interested|would.*be.*interested.*in|inviting.*you.*to|like.*to.*invite",
     "invite-outreach-template", "invitation/outreach language"),
    (r"thanks.*for.*the.*invit|yes.*i'?d.*love.*to|happy.*to.*(help|join|take part)|count.*me.*in",
     "invite-accept-reply-template", "acceptance language"),
    (r"unfortunately.*can'?t|can'?t.*make.*it.*this.*time|won'?t.*be.*able.*to|have.*to.*decline",
     "invite-decline-reply-template", "decline language"),
    (r"when.*is.*the.*deadline|what.*is.*the.*timeline|when.*will.*(you|it).*be.*announc",
     "faq-answer-template", "FAQ-shaped: deadline/timeline question"),
    (r"remote.*or.*in.?person|is.*(it|this).*online|where.*is.*it.*held",
     "faq-answer-template", "FAQ-shaped: format/location question"),
    (r"am.*i.*eligible|do.*i.*need.*(experience|a.*background)|who.*can.*(join|apply|participate)",
     "faq-answer-template", "FAQ-shaped: eligibility question"),
    (r"team.*size|solo.*participate|max.*team",
     "faq-answer-template", "FAQ-shaped: team question"),
    # FICTIONAL domain example: uncomment and adapt to your own vocabulary.
    # (r"sponsor.*ship|partnership.*opportunit|co.?organize",
    #  "partner-outreach-template", "partner intent"),
]


def suggest_template(text):
    suggestions = []
    tl = text.lower()
    for pattern, slug, reason in INTENT_RULES:
        if re.search(pattern, tl):
            suggestions.append({"slug": slug, "reason": reason})
    return suggestions


def surface(text, thread_id=None, names=None, top_faqs=3, top_names=4, fast=False):
    """Main entry point. Returns a structured context dict.

    Args:
        text: blob to analyze.
        thread_id: optional Gmail thread_id; if set, pulls last 3 thread msgs.
        names: optional comma-separated explicit name list (overrides detection).
        top_faqs: max FAQ hits per question.
        top_names: max people resolved.
        fast: when True, skip semantic FAQ lookup and use FTS-only.
            Saves ~12s model load; used by the auto-surface PostToolUse hook.
    """
    conn = get_conn()
    out = {
        "input_chars": len(text or ""),
        "names": [],
        "questions": [],
        "faq_matches": [],
        "events": [],
        "templates_suggested": [],
        "thread": [],
        "drafting_notes": [],
        "fast_mode": fast,
    }
    if not text:
        return out

    # 1) Names
    name_candidates = names.split(",") if names else detect_names(text, limit=top_names * 2)
    name_candidates = [n.strip() for n in name_candidates if n.strip()]
    resolved = []
    seen_ids = set()
    role_cols = _people_role_flag_columns(conn)
    for nc in name_candidates:
        match = resolve_person(conn, nc)
        if match and match["id"] not in seen_ids:
            seen_ids.add(match["id"])
            dossier = person_dossier(conn, match["id"])
            resolved.append({
                "name_in_text": nc,
                "person": {
                    "id": match["id"],
                    "name": match["name"],
                    "email": match["email"],
                    "headline": match.get("headline"),
                    "role_flags": {c: True for c in role_cols if match.get(c)},
                    "last_contact": match.get("last_contact_date"),
                    "tags": match.get("tags"),
                },
                "dossier": dossier,
            })
        if len(resolved) >= top_names:
            break
    out["names"] = resolved

    # 2) Questions + FAQ matches.
    # Hybrid (default) loads the sentence-transformer model (~12s first time);
    # tqdm/HF chatter corrupts wrapped stdout, so we pre-warm in fully
    # redirected mode. Fast mode skips this entirely and uses FTS only.
    import contextlib
    questions = detect_questions(text)
    lookup_mode = "fts" if fast else "hybrid"
    if questions and not fast and faq_lookup is not None:
        try:
            from faq_lookup import _get_model
            with open(os.devnull, "w") as devnull:
                with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
                    _get_model()  # idempotent, cached after first call
        except Exception:
            pass
    for q in questions:
        if faq_lookup is None:
            faq_hits = {"error": "faq_lookup unavailable (comms/ capability not installed); FAQ matching skipped"}
        else:
            try:
                faq_hits = faq_lookup(conn, q, top_n=top_faqs, mode=lookup_mode)
            except Exception as e:
                faq_hits = {"error": str(e)}
        out["questions"].append({"question": q, "faq_lookup": faq_hits})

    # 3) Event refs (from the operator's [events.aliases] config)
    out["events"] = detect_event_slugs(text)

    # 4) Template suggestion
    out["templates_suggested"] = suggest_template(text)

    # 5) Thread context
    if thread_id:
        out["thread"] = thread_context(conn, thread_id, n=3)

    # 6) Drafting notes (sanity flags)
    notes = []
    if not out["names"]:
        notes.append("No known person matched. Either truly new contact, or name didn't match (try --names \"First Last\" to force).")
    cold_cutoff = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    for n in out["names"]:
        last = n["person"]["last_contact"]
        if last and last < cold_cutoff:
            notes.append(f"{n['person']['name']}: last contact >12mo ago ({last}). Treat as cold, not warm.")
        if n["dossier"]["open_action_items"]:
            notes.append(f"{n['person']['name']}: has {len(n['dossier']['open_action_items'])} open action items. Check before sending.")
    if out["questions"] and not any(q["faq_lookup"].get("approved_canonical") for q in out["questions"] if isinstance(q.get("faq_lookup"), dict)):
        notes.append("Questions detected but no APPROVED FAQ matched. Consider faq_lookup.py log to capture this as a new proposed FAQ.")
    out["drafting_notes"] = notes

    conn.close()
    return out


def format_human(ctx):
    lines = []
    lines.append("=" * 80)
    lines.append("AUTO-SURFACED CONTEXT")
    lines.append("=" * 80)
    if ctx.get("events"):
        lines.append(f"\nEvents mentioned: {', '.join(ctx['events'])}")
    if ctx["names"]:
        lines.append("\n--- PEOPLE ---")
        for n in ctx["names"]:
            p = n["person"]
            d = n["dossier"]
            # Render role flags (is_mentor -> "mentor", etc.)
            flags = [c[3:] for c, v in (p.get("role_flags") or {}).items() if v]
            flag_str = f" [{'/'.join(flags)}]" if flags else ""
            lines.append(f"\n{p['name']}{flag_str} ({p['email']}). {p['headline'] or 'no headline'}")
            lines.append(f"  Last contact: {p['last_contact'] or 'never'}  Tags: {p['tags'] or '-'}")
            if d["recent_threads"]:
                lines.append("  Recent threads:")
                for t in d["recent_threads"][:3]:
                    lines.append(f"    [{(t['ts'] or '')[:10]}] {(t['subject'] or '')[:60]}")
            if d["open_action_items"]:
                lines.append("  OPEN action items:")
                for a in d["open_action_items"][:3]:
                    pri = a.get('priority') or '-'
                    desc = (a.get('description') or '')[:60]
                    lines.append(f"    [{pri}] {desc} (due {a.get('due_date') or '-'})")
            if d.get("graph_neighbors"):
                # Group by relation type for a tidy summary
                from collections import defaultdict
                grouped = defaultdict(list)
                for gn in d["graph_neighbors"]:
                    grouped[gn["relation"]].append(gn)
                lines.append("  Graph (1-hop edges):")
                # Show most-significant relations first. Generic starter
                # vocabulary: customize to the relation types your edges
                # table actually uses.
                priority_relations = ["works_at", "affiliated_with", "organized",
                                      "authored", "teammate", "participated_in",
                                      "introduced_by", "contacted"]
                shown = set()
                for rel in priority_relations:
                    if rel in grouped and rel not in shown:
                        items = grouped[rel][:3]
                        names = [g["other_name"][:35] for g in items]
                        suffix = f" (+{len(grouped[rel])-3} more)" if len(grouped[rel]) > 3 else ""
                        lines.append(f"    [{rel:18}] {', '.join(names)}{suffix}")
                        shown.add(rel)
                # Any remaining relations not in priority list
                for rel, items in grouped.items():
                    if rel in shown:
                        continue
                    names = [g["other_name"][:35] for g in items[:2]]
                    suffix = f" (+{len(items)-2} more)" if len(items) > 2 else ""
                    lines.append(f"    [{rel:18}] {', '.join(names)}{suffix}")

    if ctx["questions"]:
        lines.append("\n--- QUESTIONS DETECTED ---")
        for qi, q in enumerate(ctx["questions"], 1):
            lines.append(f"\nQ{qi}: {q['question'][:120]}")
            r = q.get("faq_lookup", {})
            if not isinstance(r, dict):
                continue
            a = r.get("approved_canonical")
            if a:
                conf = r.get("confidence", "?")
                sd = a.get("semantic_distance")
                sd_str = f" sem={sd:.3f}" if sd is not None else ""
                lines.append(f"   -> APPROVED FAQ {a['faq_id']} (conf={conf}{sd_str})")
                lines.append(f"      A: {(a['answer'] or '')[:200]}")
            elif r.get("draft_canonical"):
                lines.append(f"   -> Proposed/draft hits ({len(r['draft_canonical'])} nearby, none approved):")
                for d in r["draft_canonical"][:3]:
                    sd = d.get("semantic_distance")
                    sd_str = f"sem={sd:.3f}" if sd is not None else ""
                    fid = d.get("faq_id") or "(no id)"
                    lines.append(f"      [{d.get('status', '?')}] {fid} {sd_str}")
                    lines.append(f"         Q: {(d.get('question') or '')[:120]}")
                    if d.get("answer"):
                        lines.append(f"         A: {d['answer'][:200]}")
                lines.append("      NOTE: These are PROPOSED canonical FAQs (harvested, not yet approved). Treat as drafting context only -- they need the operator's review before promoting to 'approved' status. Do NOT auto-fill in your reply.")
            else:
                lines.append("   -> No FAQ match. Log via `faq_lookup.py log` after replying.")

    if ctx["thread"]:
        lines.append("\n--- THREAD (last 3 messages) ---")
        for m in ctx["thread"]:
            direction = "OUT" if m["is_outgoing"] else "IN "
            lines.append(f"  [{direction} {(m['timestamp'] or '')[:16]}] {m['sender_name'] or m['sender_email']}: "
                         f"{(m['subject'] or '')[:50]}")
            snippet = (m["snippet"] or "").replace("\n", " ")[:160]
            lines.append(f"      {snippet}")

    if ctx["templates_suggested"]:
        lines.append("\n--- CANONICAL TEMPLATE SUGGESTIONS ---")
        for t in ctx["templates_suggested"]:
            lines.append(f"  {t['slug']}  ({t['reason']})")
        lines.append("  Pull: tools/query.py \"SELECT content FROM reference_docs WHERE slug='<slug>'\"")

    if ctx["drafting_notes"]:
        lines.append("\n--- DRAFTING NOTES ---")
        for note in ctx["drafting_notes"]:
            lines.append(f"  - {note}")

    if not (ctx["names"] or ctx["questions"] or ctx["events"]):
        lines.append("\n(no entities, questions, or event refs detected)")

    lines.append("\n" + "=" * 80)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="Text to analyze (else read from stdin)")
    ap.add_argument("--email-id", type=int, help="Pull body from emails table")
    ap.add_argument("--thread-id", help="Pull last 3 messages from this thread. If --text is empty, also use the latest INBOUND message body as the analysis text.")
    ap.add_argument("--names", help="Comma-separated explicit name list (skip auto-detect)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--top-faqs", type=int, default=3)
    ap.add_argument("--fast", action="store_true", help="FTS-only FAQ lookup; skips ~12s model load. Used by the PostToolUse auto-surface hook.")
    args = ap.parse_args()

    text = args.text
    # Precedence: --text > stdin (live tool output) > --email-id > --thread-id
    # Stdin wins over DB so the auto-surface hook can pass live mail-tool
    # output even when the local emails table lags.
    if not text:
        try:
            if not sys.stdin.isatty():
                text = sys.stdin.read()
        except Exception:
            pass
    if not text and args.email_id:
        conn = get_conn()
        row = conn.execute("SELECT body, thread_id FROM emails WHERE id=?", (args.email_id,)).fetchone()
        if row:
            text = row["body"]
            if not args.thread_id:
                args.thread_id = row["thread_id"]
        conn.close()
    if not text and args.thread_id:
        conn = get_conn()
        row = conn.execute("""
            SELECT body FROM emails
            WHERE thread_id=? AND is_outgoing=0 AND body IS NOT NULL
            ORDER BY timestamp DESC LIMIT 1
        """, (args.thread_id,)).fetchone()
        if row:
            text = row["body"]
        conn.close()

    ctx = surface(text, thread_id=args.thread_id, names=args.names, top_faqs=args.top_faqs, fast=args.fast)
    out = json.dumps(ctx, indent=2, default=str) if args.json else format_human(ctx)
    # sentence-transformers' tqdm closes Python-side stdout wrappers; write
    # directly to fd 1 to bypass the broken wrappers entirely.
    os.write(1, (out + "\n").encode("utf-8", errors="replace"))


if __name__ == "__main__":
    main()
