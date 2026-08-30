"""Person dossier: everything we know about someone, in one call.

Pulls from: people, person_emails, emails, email_threads / thread_participants,
action_items, entities, edges.

Usage:
    python person_dossier.py "Jane Doe"
    python person_dossier.py "person@example.com"
"""
import io
import json
import sqlite3
import sys
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

# Windows encoding fix: reconfigure IN PLACE (never swap the stream object --
# replacing sys.stdout at import time discards the importer's unflushed output
# and breaks streams without .buffer; fatal for a stdio MCP host).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, io.UnsupportedOperation):
    pass

DB = str(paths.DB_PATH)

# Known shortnames -> full identity. Operator-editable: map the short names
# you actually type to the canonical full name in `people`.
ALIASES = {
    "example": "Example Full Name",  # FICTIONAL placeholder; replace with your own
}


def get_db():
    db = _db.connect(DB, timeout=10)
    db.execute("PRAGMA busy_timeout = 10000")
    db.row_factory = sqlite3.Row
    return db


def _table_exists(db, name):
    """Feature-detect an optional table/view so a section can degrade
    gracefully when the layer that populates it isn't installed (e.g. the
    claim-extraction staging tables, which are not part of this starter kit)."""
    try:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (name,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def resolve_query(query):
    """Resolve shortnames and aliases."""
    q = query.strip().lower()
    if q in ALIASES:
        return ALIASES[q]
    return query


def find_primary_person(db, query):
    """Find the single best-matching person record."""
    query = resolve_query(query)
    words = [w.strip() for w in query.split() if len(w.strip()) > 1]

    where_parts = []
    params = []
    for w in words:
        where_parts.append("(name LIKE ? OR email LIKE ?)")
        params.extend([f"%{w}%", f"%{w}%"])

    people = db.execute(f"""
        SELECT id, name, email, linkedin, headline AS affiliation, seniority, location, career_stage,
               discord_username, notes,
               interaction_count, created_at
        FROM people
        WHERE ({' AND '.join(where_parts)})
        AND name IS NOT NULL AND name != ''
        ORDER BY interaction_count DESC
        LIMIT 1
    """, params).fetchone()

    if not people:
        # Fallback: OR instead of AND
        where_parts_or = []
        params_or = []
        for w in words:
            where_parts_or.append("(name LIKE ? OR email LIKE ?)")
            params_or.extend([f"%{w}%", f"%{w}%"])
        people = db.execute(f"""
            SELECT id, name, email, linkedin, headline AS affiliation, seniority, location, career_stage,
                   discord_username, notes,
                   interaction_count, created_at
            FROM people
            WHERE ({' OR '.join(where_parts_or)})
            AND name IS NOT NULL AND name != ''
            ORDER BY interaction_count DESC
            LIMIT 1
        """, params_or).fetchone()

    return people


def get_all_emails_for_person(db, person):
    """Get all email addresses associated with a person."""
    emails = set()
    if person["email"]:
        for e in person["email"].split(","):
            emails.add(e.strip().lower())

    # Also from person_emails
    for r in db.execute("SELECT email FROM person_emails WHERE person_id = ?", (person["id"],)).fetchall():
        if r["email"]:
            emails.add(r["email"].strip().lower())

    # Find other people records with same email (transitive closure)
    if emails:
        ph = ",".join("?" * len(emails))
        for r in db.execute(f"SELECT email FROM person_emails WHERE email IN ({ph})", list(emails)).fetchall():
            if r["email"]:
                emails.add(r["email"].strip().lower())

    # Also search for other people records with the same name
    name = person["name"]
    if name:
        for r in db.execute("SELECT email FROM people WHERE name = ? AND email IS NOT NULL", (name,)).fetchall():
            for e in r["email"].split(","):
                emails.add(e.strip().lower())

    return emails


def _append_thread_context(db, lines, person, all_emails):
    """Show threads the person appears on and who introduced them. If a
    claim-extraction layer is installed (thread_claims_staging /
    thread_extractions_staging tables exist), also pull grounded-passing
    claims grouped by thread. Those tables are NOT part of this starter kit,
    so by default this section degrades to the pure thread rollup."""
    pid = person["id"]
    emails_lower = [e.lower() for e in all_emails if "@" in e]

    has_claims = _table_exists(db, "thread_claims_staging")
    has_extractions = _table_exists(db, "thread_extractions_staging")

    grounded_sub = (
        """(SELECT COUNT(*) FROM thread_claims_staging tc
            WHERE tc.thread_id=tp.thread_id
              AND tc.grounding_status='passed')"""
        if has_claims else "0"
    )
    ext_sub = (
        """(SELECT status FROM thread_extractions_staging x
            WHERE x.thread_id=tp.thread_id
            ORDER BY x.extracted_at DESC LIMIT 1)"""
        if has_extractions else "NULL"
    )

    # All threads this person appears on
    if emails_lower:
        placeholders = ",".join(["?"] * len(emails_lower))
        parts = db.execute(
            f"""SELECT tp.thread_id, tp.email_addr, tp.role, tp.message_count,
                       tp.sent_count, tp.intro_by_person_id,
                       (SELECT name FROM people WHERE id=tp.intro_by_person_id) intro_by_name,
                       et.subject, et.first_ts, et.last_inbound_ts,
                       {grounded_sub} grounded_claims,
                       {ext_sub} ext_status
                FROM thread_participants tp
                LEFT JOIN email_threads et ON et.thread_id=tp.thread_id
                WHERE tp.person_id=? OR tp.email_addr IN ({placeholders})
                ORDER BY COALESCE(et.last_inbound_ts, et.first_ts) DESC""",
            [pid] + emails_lower,
        ).fetchall()
    else:
        parts = db.execute(
            f"""SELECT tp.thread_id, tp.email_addr, tp.role, tp.message_count,
                      tp.sent_count, tp.intro_by_person_id,
                      (SELECT name FROM people WHERE id=tp.intro_by_person_id) intro_by_name,
                      et.subject, et.first_ts, et.last_inbound_ts,
                      {grounded_sub} grounded_claims,
                      {ext_sub} ext_status
               FROM thread_participants tp
               LEFT JOIN email_threads et ON et.thread_id=tp.thread_id
               WHERE tp.person_id=?
               ORDER BY COALESCE(et.last_inbound_ts, et.first_ts) DESC""",
            (pid,),
        ).fetchall()

    if not parts:
        return

    lines.append("## Thread Context")
    lines.append("")

    # Facts stated about this person across all threads (participant_facts).
    # Only available when a claim-extraction layer populates the staging table.
    if has_claims:
        fact_where = "subject_person_id=?"
        fact_params: list = [pid]
        if emails_lower:
            placeholders2 = ",".join(["?"] * len(emails_lower))
            fact_where += f" OR subject_email IN ({placeholders2})"
            fact_params += emails_lower
        facts = db.execute(
            f"""SELECT claim_text, evidence_gmail_id, evidence_quote, grounding_status,
                       status, thread_id, claim_type
                  FROM thread_claims_staging
                 WHERE ({fact_where})
                   AND claim_type IN ('participant_fact','intro')
                   AND grounding_status='passed'
              ORDER BY claim_type, thread_id""",
            fact_params,
        ).fetchall()
        if facts:
            lines.append(f"### Facts & intros stated about this person ({len(facts)})")
            for f in facts:
                tag = "[approved]" if f["status"] == "approved" else "[unreviewed]"
                prefix = "intro: " if f["claim_type"] == "intro" else ""
                lines.append(f"- {tag} {prefix}{f['claim_text']}")
                if f["evidence_quote"]:
                    q = (f["evidence_quote"] or "")[:140].replace("\n", " ")
                    lines.append(f"    source: thread {f['thread_id'][:12]} msg {f['evidence_gmail_id'][:10]} -- \"{q}\"")
            lines.append("")

    # Per-thread rollup
    lines.append(f"### Threads this person is on ({len(parts)})")
    for p in parts:
        subj = (p["subject"] or "(no subject)")[:80]
        counts = f"sent {p['sent_count']}/{p['message_count']}"
        intro = f" -- intro by {p['intro_by_name']}" if p["intro_by_name"] else ""
        gc = p["grounded_claims"] or 0
        if has_extractions or has_claims:
            ext = p["ext_status"] or "not extracted"
            lines.append(f"- thread {p['thread_id'][:12]} [{ext}, {gc} grounded claims]: {subj} ({counts}{intro})")
        else:
            lines.append(f"- thread {p['thread_id'][:12]}: {subj} ({counts}{intro})")

        # Per-thread summary + status from this person's perspective
        if has_claims and gc > 0:
            ts = db.execute(
                """SELECT claim_text, status FROM thread_claims_staging
                   WHERE thread_id=? AND claim_type='summary' AND grounding_status='passed'
                   LIMIT 1""",
                (p["thread_id"],),
            ).fetchone()
            if ts:
                lines.append(f"    summary: {ts['claim_text'][:200]}")
            tst = db.execute(
                """SELECT claim_text FROM thread_claims_staging
                   WHERE thread_id=? AND claim_type='status' AND grounding_status='passed'
                   LIMIT 1""",
                (p["thread_id"],),
            ).fetchone()
            if tst:
                lines.append(f"    status:  {tst['claim_text'][:200]}")
            # Commitments involving this person
            comms = db.execute(
                """SELECT claim_text FROM thread_claims_staging
                   WHERE thread_id=? AND claim_type='commitment' AND grounding_status='passed'
                     AND (subject_person_id=? OR subject_email IN (""" +
                (",".join(["?"] * len(emails_lower)) if emails_lower else "''") + """))""",
                [p["thread_id"], pid] + emails_lower,
            ).fetchall()
            for co in comms:
                lines.append(f"    commitment: {co['claim_text'][:200]}")
    lines.append("")

    # People this person has introduced
    introduced = db.execute(
        """SELECT DISTINCT tp.thread_id, tp.email_addr, tp.display_name,
                  (SELECT name FROM people WHERE id=tp.person_id) name,
                  et.subject
             FROM thread_participants tp
             LEFT JOIN email_threads et ON et.thread_id=tp.thread_id
             WHERE tp.intro_by_person_id=?""",
        (pid,),
    ).fetchall()
    if introduced:
        lines.append(f"### People introduced by {person['name']} ({len(introduced)})")
        for i in introduced:
            who = i["name"] or i["display_name"] or i["email_addr"]
            lines.append(f"- {who} <{i['email_addr']}> (on thread \"{(i['subject'] or '')[:60]}\")")
        lines.append("")


def dossier(query):
    """Build a complete dossier for a person."""
    db = get_db()
    lines = []

    # 1. Find primary person
    person = find_primary_person(db, query)
    if not person:
        return f"No person found for '{query}'."

    all_emails = get_all_emails_for_person(db, person)
    name = person["name"]

    # ═══ IDENTITY ═══
    lines.append(f"# {name}")
    lines.append("")

    info_parts = []
    if person["email"]:
        info_parts.append(f"**Email:** {person['email']}")
    if person["affiliation"]:
        info_parts.append(f"**Affiliation:** {person['affiliation']}")
    if person["location"]:
        info_parts.append(f"**Location:** {person['location']}")
    if person["linkedin"]:
        info_parts.append(f"**LinkedIn:** {person['linkedin']}")
    if person["career_stage"]:
        info_parts.append(f"**Career stage:** {person['career_stage']}")
    if person["seniority"]:
        info_parts.append(f"**Seniority:** {person['seniority']}")
    if person["discord_username"]:
        info_parts.append(f"**Discord:** {person['discord_username']}")
    lines.append(" | ".join(info_parts[:4]))
    for part in info_parts[4:]:
        lines.append(part)

    if len(all_emails) > 1:
        lines.append(f"**All known emails:** {', '.join(sorted(all_emails))}")

    # Bio (via the optional v_person_bios view, created by an attributes/EAV
    # layer if you install one). Additive surface: never break the dossier.
    try:
        bio = db.execute(
            "SELECT bio, bio_source, bio_updated_at FROM v_person_bios WHERE person_id = ?",
            (person["id"],)
        ).fetchone()
        if bio and bio["bio"]:
            lines.append("")
            lines.append("## Bio")
            lines.append(bio["bio"].strip())
            lines.append(f"*source: {bio['bio_source']} (as of {bio['bio_updated_at']})*")
    except Exception:  # noqa: BLE001, bio surface is additive, never break the dossier
        pass

    lines.append(f"**Interactions:** {person['interaction_count'] or 0}")
    lines.append("")

    # ═══ FABRIC GRAPH (entities + edges) ═══
    lines.append("## Knowledge Graph")
    lines.append("")

    entity_id = f"person-{person['id']}"
    # Single query for both directions (replaces 2 separate queries)
    all_edges = db.execute("""
        SELECT 'out' as dir, ed.relation, ed.fact, e2.name as peer_name, e2.type as peer_type,
               e2.id as peer_id, e2.status as peer_status, e2.data as peer_data,
               ed.valid_from, ed.valid_until, ed.confidence
        FROM edges ed JOIN entities e2 ON ed.target_id = e2.id
        WHERE ed.source_id = ?
        UNION ALL
        SELECT 'in' as dir, ed.relation, ed.fact, e1.name as peer_name, e1.type as peer_type,
               e1.id as peer_id, e1.status as peer_status, e1.data as peer_data,
               ed.valid_from, ed.valid_until, ed.confidence
        FROM edges ed JOIN entities e1 ON ed.source_id = e1.id
        WHERE ed.target_id = ?
        ORDER BY relation, peer_name
    """, (entity_id, entity_id)).fetchall()
    outgoing = [e for e in all_edges if e["dir"] == "out"]
    incoming = [e for e in all_edges if e["dir"] == "in"]

    # action_item, email_thread, and discord_thread peers get a separate,
    # structured rendering because they're more actionable than generic graph
    # nodes (status + priority + clickable links matter). Other peer types
    # stay in the compact comma list.
    def _split_actionable(edges):
        actionable_types = ("action_item", "email_thread", "discord_thread")
        actionable = [e for e in edges if e["peer_type"] in actionable_types]
        other = [e for e in edges if e["peer_type"] not in actionable_types]
        return actionable, other

    if outgoing:
        by_rel = {}
        for e in outgoing:
            by_rel.setdefault(e["relation"], []).append(e)
        for rel, edges in sorted(by_rel.items()):
            actionable, other = _split_actionable(edges)
            if other:
                targets = [f"{e['peer_name']} ({e['peer_type']})" for e in other]
                lines.append(f"- **{rel}**: {', '.join(targets)}")
            for e in actionable[:10]:
                lines.append(f"  - {rel} -> [{e['peer_type']}] {e['peer_name']}")
            if len(actionable) > 10:
                lines.append(f"    ...and {len(actionable) - 10} more")

    if incoming:
        by_rel = {}
        for e in incoming:
            by_rel.setdefault(e["relation"], []).append(e)
        # Render action_item/email_thread relations FIRST (most actionable signal)
        priority_rels = ["from_person", "about_person", "waiting_on", "with_person", "resolved_by", "derived_from"]
        ordered = [r for r in priority_rels if r in by_rel] + [r for r in by_rel if r not in priority_rels]
        for rel in ordered:
            edges = by_rel[rel]
            actionable, other = _split_actionable(edges)
            if other:
                sources = [f"{e['peer_name']} ({e['peer_type']})" for e in other]
                lines.append(f"- **{rel} (inbound)**: {', '.join(sources[:10])}")
                if len(sources) > 10:
                    lines.append(f"  ...and {len(sources) - 10} more")
            if actionable:
                import json as _j
                # Dedupe by peer_id (multiple edges with same target collapse to one row)
                seen = set()
                unique = []
                for e in actionable:
                    if e["peer_id"] in seen:
                        continue
                    seen.add(e["peer_id"])
                    unique.append(e)
                # Sort by most recent last_ts/timestamp from peer_data when available
                def _recency(edge):
                    try:
                        d = _j.loads(edge["peer_data"] or "{}")
                        return d.get("last_ts") or d.get("last_inbound_ts") or d.get("first_ts") or ""
                    except Exception:
                        return ""
                unique.sort(key=_recency, reverse=True)
                lines.append(f"- **{rel} (inbound, {len(unique)} items):**")
                for e in unique[:15]:
                    peer_type = e["peer_type"]
                    if peer_type == "action_item":
                        prio = "P?"
                        try:
                            d = _j.loads(e["peer_data"] or "{}")
                            prio = d.get("priority") or "P?"
                        except Exception:
                            pass
                        # Map entity.status ('active'/'resolved') back to action_item status from data
                        ai_status = "active"
                        try:
                            d = _j.loads(e["peer_data"] or "{}")
                            ai_status = d.get("status") or e["peer_status"]
                        except Exception:
                            ai_status = e["peer_status"]
                        tag = f"[{ai_status}/{prio}]"
                        lines.append(f"  - {tag} `{e['peer_id']}` {e['peer_name']}")
                    elif peer_type == "email_thread":
                        tid = e["peer_id"]
                        link = f"https://mail.google.com/mail/u/0/#inbox/{tid}" if tid else ""
                        # Add status hint
                        thr_status = "?"
                        try:
                            d = _j.loads(e["peer_data"] or "{}")
                            thr_status = d.get("status") or "?"
                        except Exception:
                            pass
                        lines.append(f"  - [{thr_status}] {e['peer_name']} {link}")
                    elif peer_type == "discord_thread":
                        # Pull thread metadata for rich rendering
                        msg_count = "?"
                        first_ts = ""
                        last_ts = ""
                        try:
                            d = _j.loads(e["peer_data"] or "{}")
                            msg_count = d.get("message_count") or "?"
                            first_ts = (d.get("first_ts") or "")[:10]
                            last_ts = (d.get("last_ts") or "")[:10]
                        except Exception:
                            pass
                        preview = (e["peer_name"] or "")[:70]
                        lines.append(f"  - [discord, {msg_count} msgs, {last_ts}] {preview}")
                if len(unique) > 15:
                    lines.append(f"    ...and {len(unique) - 15} more")

    # Colleagues: other people at same org
    if person["affiliation"] and person["affiliation"] != "--":
        colleagues = db.execute("""
            SELECT e1.name FROM edges ed1
            JOIN edges ed2 ON ed1.target_id = ed2.target_id AND ed2.relation = 'works_at'
            JOIN entities e1 ON ed2.source_id = e1.id
            WHERE ed1.source_id = ? AND ed1.relation = 'works_at'
            AND ed2.source_id != ?
            LIMIT 8
        """, (entity_id, entity_id)).fetchall()
        if colleagues:
            lines.append(f"- **Colleagues at {person['affiliation']}**: {', '.join(c['name'] for c in colleagues)}")

    if not outgoing and not incoming:
        lines.append("(no graph edges)")
    lines.append("")

    # ═══ EMAIL HISTORY ═══
    lines.append("## Email History")
    lines.append("")

    email_where_parts = []
    email_where_params = []
    for e in all_emails:
        if "," not in e:
            email_where_parts.append("sender_email LIKE ?")
            email_where_params.append(f"%{e}%")
            email_where_parts.append("body LIKE ?")
            email_where_params.append(f"%{e}%")
    # Also search by name in body
    email_where_parts.append("body LIKE ?")
    email_where_params.append(f"%{name}%")

    emails = db.execute(f"""
        SELECT timestamp, sender_name, subject, is_outgoing
        FROM emails WHERE {' OR '.join(email_where_parts)}
        ORDER BY timestamp
    """, email_where_params).fetchall()

    # Filter out calendar invites and noise
    SKIP_SUBJECTS = ["Accepted:", "Declined:", "Updated invitation", "Tentatively accepted:",
                     "has joined your meeting", "Thanks for Subscribing", "Invoice Receipt"]

    filtered_emails = []
    for e in emails:
        subj = e["subject"] or ""
        if any(subj.startswith(skip) or skip in subj for skip in SKIP_SUBJECTS):
            continue
        filtered_emails.append(e)

    calendar_count = len(emails) - len(filtered_emails)

    if filtered_emails:
        lines.append(f"({len(filtered_emails)} emails, {calendar_count} calendar/noise filtered)")
        lines.append("")
        lines.append(f"| Dir | Date | From | Subject |")
        lines.append(f"|-----|------|------|---------|")
        for e in filtered_emails:
            d = "OUT" if e["is_outgoing"] else "IN"
            subj = (e["subject"] or "").replace("\n", " ").replace("|", "/")[:70]
            sender = (e["sender_name"] or "?").replace("|", "/")
            lines.append(f"| {d} | {e['timestamp'][:16]} | {sender} | {subj} |")
    elif emails:
        lines.append(f"({len(emails)} emails, all calendar invites)")
    else:
        lines.append("(none)")
    lines.append("")

    # ═══ THREAD CONTEXT ═══
    _append_thread_context(db, lines, person, all_emails)

    # ═══ ACTION ITEMS ═══
    lines.append("## Action Items")
    lines.append("")

    actions = db.execute(
        "SELECT status, priority, description, waiting_on FROM action_items WHERE description LIKE ?",
        (f"%{name}%",)
    ).fetchall()

    if actions:
        for a in actions:
            w = f" [WAITING: {a['waiting_on']}]" if a["waiting_on"] else ""
            lines.append(f"- [{a['status']}] [{a['priority']}] {a['description'][:150]}{w}")
    else:
        # Try first name only
        first = name.split()[0]
        actions = db.execute(
            "SELECT status, priority, description, waiting_on FROM action_items WHERE description LIKE ?",
            (f"%{first}%",)
        ).fetchall()
        if actions:
            for a in actions:
                w = f" [WAITING: {a['waiting_on']}]" if a["waiting_on"] else ""
                lines.append(f"- [{a['status']}] [{a['priority']}] {a['description'][:150]}{w}")
        else:
            lines.append("(none)")
    lines.append("")

    # ═══ TASKS CREATED BY THEM ═══
    # creator_person_id = "who typed/originated this task" (vs source_person_id
    # = "who's the source of the message it came from").
    lines.append("## Tasks Created by Them")
    lines.append("")
    created = db.execute(
        "SELECT item_id, status, priority, description, extracted_by, "
        "  context_slug, due_date "
        "FROM action_items WHERE creator_person_id=? "
        "ORDER BY CASE status WHEN 'OPEN' THEN 0 WHEN 'WAITING' THEN 1 "
        "  WHEN 'BLOCKED' THEN 2 ELSE 3 END, inserted_at DESC LIMIT 30",
        (person["id"],),
    ).fetchall()
    if created:
        for a in created:
            via = f" via {a['extracted_by']}" if a["extracted_by"] else ""
            ctx = f" [{a['context_slug']}]" if a["context_slug"] else ""
            due = f" due={a['due_date']}" if a["due_date"] else ""
            lines.append(f"- {a['item_id']} [{a['status']}/{a['priority'] or '-'}]{ctx}{due}{via}: {(a['description'] or '')[:130]}")
    else:
        lines.append("(none, no action_items have creator_person_id set to this person)")
    lines.append("")

    # ═══ SURVEYS ═══
    # Reads `submitted_<kind>_survey` edges. Source-of-truth: edges table;
    # any survey-tracking layer you add can write them at sync time.
    lines.append("## Surveys")
    lines.append("")
    pent_id = f"person-{person['id']}"
    survey_edges = db.execute(
        "SELECT relation, fact, valid_from "
        "FROM edges WHERE source_id=? AND relation LIKE 'submitted_%_survey' "
        "ORDER BY valid_from DESC",
        (pent_id,),
    ).fetchall()
    if survey_edges:
        for s in survey_edges:
            kind = s["relation"].replace("submitted_", "").replace("_survey", "")
            when = (s["valid_from"] or "")[:10]
            fact = f", {s['fact']}" if s["fact"] else ""
            lines.append(f"- {when}  [{kind}]{fact}")
    else:
        lines.append("(none)")
    lines.append("")

    # ═══ DATA COMPLETENESS ═══
    sparse_fields = []
    for field in ("affiliation", "location", "linkedin", "seniority"):
        val = person[field]
        if not val or val in ("", "-"):
            sparse_fields.append(field)
    if sparse_fields:
        lines.append("")
        lines.append(f"## Data Gaps")
        lines.append(f"Missing: {', '.join(sparse_fields)}")

    db.close()
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
    else:
        query = " ".join(sys.argv[1:])
        print(dossier(query))
