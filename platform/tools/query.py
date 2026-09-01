"""Quick query tool for ops.db. Handles encoding and pretty-printing.

Usage:
    python query.py "SELECT * FROM action_items WHERE status='OPEN' LIMIT 5"
    python query.py action_items          # shorthand: SELECT * FROM table LIMIT 20
    python query.py action_items open     # shorthand: WHERE status='OPEN'
    python query.py people "jane"         # shorthand: WHERE name LIKE '%jane%' OR email LIKE '%jane%'
    python query.py schema                # show all tables and columns
    python query.py schema action_items   # show columns for one table
    python query.py dossier "Jane Doe"    # full person dossier (identity, graph, emails, actions)
    python query.py search "AI safety researcher"  # hybrid vector+FTS search
    python query.py cross "travel grant reviewer"  # cross-table search (people+emails+actions+learnings+discord)
    python query.py map                   # show table descriptions grouped by tier
    python query.py map "email"           # search table descriptions for keyword
    python query.py route "who sent emails"  # smart table routing (FTS5 + embeddings + RRF)
    python query.py learnings "email"     # search active learnings by keyword
    python query.py preflight "task desc" # check workflow routes for a task
    python query.py inbox                 # email triage status (comms/inbox_triage.py)
    python query.py threads "keyword"     # FTS5 email_threads search (subject+body)
    python query.py dms [name-or-keyword] # recent Discord DM threads (+FTS content hits)
    python query.py faqs [topic]          # canonical FAQs + occurrence-pending count
    python query.py episodes "keyword"    # FTS episodes search (topic + summary)
    python query.py sessions "keyword"    # FTS over per-session changelog entries
    python query.py rebuild [keyword]     # list/search system-rebuild docs
    python query.py edges "person name"   # show all edges for a person with temporal info
    python query.py graph "entity"        # show all connected entities with relations and dates
    python query.py enrich                # sync people table -> entities + update contact counts
    python query.py vemail "query"        # semantic vector search on emails
    python query.py hop "entity" [depth]  # multi-hop graph traversal (default 2 hops)
    python query.py whoknows "org name"   # find people connected to an org (via graph)
    python query.py chunks "RRF scoring"  # search reference_doc_chunks_fts for section-level matches
    python query.py health                # system health snapshot
    python query.py explain <item-id>     # full timeline for one action_item
"""
import io
import math
import sqlite3
import struct
import sys
from pathlib import Path

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

_ROOT = Path(paths.ROOT)
DB = str(paths.DB_PATH)
# Embedding tables live in a sidecar DB (two-file layout); attach when available.
_VEC_DB = str(getattr(paths, "VEC_DB_PATH", None) or (Path(paths.DB_PATH).parent / "vec.db"))
TRACE_LOG = str(_ROOT / "data" / "query_trace.log")


def _tool(*parts):
    """Resolve a sibling capability script. Capability directories live under
    platform/ since the August 2026 reorganisation; fall back to the root for
    a flat layout."""
    under_platform = _ROOT.joinpath("platform", *parts)
    if under_platform.exists():
        return str(under_platform)
    return str(_ROOT.joinpath(*parts))


def _trace_callback(statement):
    """Log every SQL statement to trace file for usage analysis."""
    import re
    from datetime import datetime
    # Extract table names from FROM/INTO/UPDATE clauses
    tables = set()
    for match in re.finditer(r'\b(?:FROM|JOIN|INTO|UPDATE)\s+\[?(\w+)\]?', statement, re.IGNORECASE):
        t = match.group(1)
        if t.lower() not in ('sqlite_master', 'pragma_table_info', '_table_meta'):
            tables.add(t)
    if tables:
        ts = datetime.now().isoformat()
        try:
            with open(TRACE_LOG, "a", encoding="utf-8") as f:
                for t in tables:
                    f.write(f"{ts}\t{t}\n")
        except Exception:
            pass


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    conn = _db.connect(DB)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    conn.set_trace_callback(_trace_callback)
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        if not any(r[1] == 'vecdb' for r in conn.execute('PRAGMA database_list')):
            conn.execute("ATTACH DATABASE '%s' AS vecdb" % _VEC_DB)
    except Exception:
        pass  # sqlite-vec optional, needed for vec_* queries

    try:
        _run(conn)
    finally:
        conn.close()


def _run(conn):
    arg1 = sys.argv[1]
    arg2 = sys.argv[2] if len(sys.argv) > 2 else None

    # Self-service debug commands, handled by the health module in autonomy/.
    if arg1 in ("health", "explain"):
        conn.close()
        import subprocess
        cmd = [sys.executable, _tool("autonomy", "health_runbook.py")] + sys.argv[1:]
        return subprocess.run(cmd).returncode

    # These routed to the extraction/promotion pipeline in the source system.
    if arg1 in ("promotions", "staging", "extract-stats"):
        print("extraction layer not included in this starter kit")
        return

    # Preflight mode: check workflow routes for a task
    if arg1 == "preflight":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query:
            print('Usage: python query.py preflight "task description"')
            print('       python query.py preflight "send email to reviewers"')
            print('       python query.py preflight --all  (list all routes)')
            return
        if query == "--all":
            rows = conn.execute(
                "SELECT id, trigger_patterns, required_action, reason, category, priority "
                "FROM workflow_routes WHERE active = 1 ORDER BY category, priority"
            ).fetchall()
            if not rows:
                print("No workflow routes defined.")
                return
            current_cat = None
            for r in rows:
                cat = r["category"] or "uncategorized"
                if cat != current_cat:
                    print(f"\n[{cat.upper()}]")
                    current_cat = cat
                print(f"  #{r['id']} (P{r['priority']}) triggers: {r['trigger_patterns']}")
                print(f"    action: {r['required_action']}")
                if r["reason"]:
                    print(f"    why: {r['reason']}")
            return
        # Word overlap matching (same logic as the prompt-routing hook)
        import re
        stop_words = {
            "the", "and", "for", "that", "this", "with", "from", "what", "how", "can",
            "please", "lets", "about", "need", "want", "know", "check", "also", "like",
            "just", "have", "been", "will", "should", "could", "would", "into", "some",
        }
        prompt_words = set(re.findall(r'[a-zA-Z]{3,}', query.lower())) - stop_words
        prompt_tokens = set(query.lower().split())
        rows = conn.execute(
            "SELECT trigger_patterns, required_action, reason, category, priority "
            "FROM workflow_routes WHERE active = 1 ORDER BY priority"
        ).fetchall()
        matches = []
        for r in rows:
            triggers = r["trigger_patterns"] or ""
            trigger_words = set(re.findall(r'[a-zA-Z]{3,}', triggers.lower())) - stop_words
            trigger_tokens = set(triggers.lower().split())
            overlap = len(prompt_words & trigger_words)
            exact = len(prompt_tokens & trigger_tokens)
            score = overlap + exact
            if score >= 2:
                matches.append((r, score))
        if not matches:
            print("No matching workflow routes.")
            return
        matches.sort(key=lambda x: (-x[1], x[0]["priority"]))
        for r, score in matches[:5]:
            cat = r["category"] or ""
            print(f"[{cat}] (score={score}) {r['required_action']}")
            if r["reason"]:
                print(f"  why: {r['reason']}")
            print()
        return

    # Inbox triage shortcut
    if arg1 == "inbox":
        conn.close()
        import subprocess
        # Default to status; pass-through flags
        forwarded = sys.argv[2:]
        if not forwarded:
            forwarded = ["status"]
        elif forwarded[0] == "--breaches":
            forwarded = ["breaches"] + forwarded[1:]
        elif forwarded[0] == "--lane":
            forwarded = ["breaches"] + forwarded
        cmd = [sys.executable, _tool("comms", "inbox_triage.py")] + forwarded
        return subprocess.run(cmd).returncode

    # Dossier mode: everything we know about a person
    if arg1 == "dossier":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query:
            print('Usage: python query.py dossier "person name or email"')
            return
        conn.close()
        import subprocess
        subprocess.run([sys.executable, _tool("search", "person_dossier.py"), query])
        return

    # Search mode: RRF-fused multi-corpus backend (rrf_search.py: FTS + vector +
    # PPR graph per corpus). --people routes to the legacy people-only
    # hybrid_search deep engine (query expansion + rerank).
    if arg1 == "search":
        flags = {"--explain", "--verbose", "-v", "--people"}
        query_words = [a for a in sys.argv[2:] if a not in flags]
        query = " ".join(query_words)
        if not query:
            print('Usage: python query.py search "your search query" [--people|--explain]')
            return
        conn.close()  # close before subprocess
        import subprocess
        if "--people" in sys.argv or "--explain" in sys.argv:
            cmd = [sys.executable, _tool("search", "hybrid_search.py"), query, "--verbose"]
            if "--explain" in sys.argv:
                cmd.append("--explain")
        else:
            cmd = [sys.executable, _tool("search", "rrf_search.py"), query]
        subprocess.run(cmd)
        return

    # Cross-table search mode (FTS5 across people+emails+action_items+learnings+discord)
    if arg1 == "cross":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query:
            print('Usage: python query.py cross "your search query"')
            return
        conn.close()
        import subprocess
        cmd = [sys.executable, _tool("search", "cross_search.py"), query]
        if "--verbose" in sys.argv or "-v" in sys.argv:
            cmd.append("--verbose")
        subprocess.run(cmd)
        return

    # Route mode: smart table routing with FTS5 + embeddings + RRF
    if arg1 == "route":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query:
            print('Usage: python query.py route "your question"')
            return
        _route(conn, query)
        return

    # Map mode: show table descriptions to help find the right table
    if arg1 == "map":
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_table_descriptions'"
        ).fetchone()
        if not exists:
            print("No _table_descriptions table. Seed it before using map.")
            return

        if arg2:
            like = f"%{arg2}%"
            rows = conn.execute(
                "SELECT table_name, tier, description, when_to_query, priority_note "
                "FROM _table_descriptions "
                "WHERE description LIKE ? OR when_to_query LIKE ? OR priority_note LIKE ? OR table_name LIKE ? "
                "ORDER BY CASE tier WHEN 'core' THEN 0 WHEN 'reference' THEN 1 ELSE 2 END",
                (like, like, like, like)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT table_name, tier, description, when_to_query, priority_note "
                "FROM _table_descriptions "
                "ORDER BY CASE tier WHEN 'core' THEN 0 WHEN 'reference' THEN 1 ELSE 2 END"
            ).fetchall()

        if not rows:
            print(f"No tables match '{arg2}'." if arg2 else "No table descriptions found.")
        else:
            current_tier = None
            for r in rows:
                if r['tier'] != current_tier:
                    current_tier = r['tier']
                    print(f"\n=== {current_tier.upper()} ===")
                note = f"  ** {r['priority_note']}" if r['priority_note'] else ""
                desc = (r['description'] or '')[:80]
                when = (r['when_to_query'] or '')[:80]
                print(f"  {r['table_name']}: {desc}")
                print(f"    When: {when}{note}")
            print(f"\n({len(rows)} tables)")

        # Also show undescribed tables
        if not arg2:
            all_tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '\\_%' ESCAPE '\\' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' AND name NOT LIKE 'vec_%' "
                "ORDER BY name"
            ).fetchall()
            described = {r['table_name'] for r in rows}
            undescribed = [t['name'] for t in all_tables if t['name'] not in described]
            if undescribed:
                print(f"\n=== UNDESCRIBED ({len(undescribed)}) ===")
                for t in undescribed:
                    count = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                    print(f"  {t} ({count} rows)")
        return

    # Learnings mode: LIKE search with expiry and status filtering
    if arg1 == "learnings":
        query_text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query_text or not query_text.strip():
            print('Usage: python query.py learnings "keyword"')
            return

        like = f"%{query_text}%"
        rows = conn.execute(
            "SELECT id, title, description FROM learnings "
            "WHERE (title LIKE ? OR description LIKE ? OR apply_when LIKE ?) "
            "AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > datetime('now')) "
            "ORDER BY updated_at DESC LIMIT 5",
            (like, like, like)
        ).fetchall()

        if rows:
            for r in rows:
                print(f"[{r['id']}] {r['title']}")
                print(f"    {(r['description'] or '')[:120]}")
            print(f"\n({len(rows)} learnings)")
        else:
            print(f"No active learnings match '{query_text}'.")
        return

    # Shortcut: threads "keyword", FTS5 on email_threads
    if arg1 == "threads":
        query_text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query_text.strip():
            print('Usage: python query.py threads "keyword"')
            return
        rows = conn.execute(
            """SELECT et.thread_id, et.subject, et.last_sender_email,
                      COALESCE(et.last_inbound_ts, et.last_outbound_ts) AS last_ts,
                      et.status, et.lane
               FROM email_threads_fts f
               JOIN email_threads et ON et.rowid = f.rowid
               WHERE email_threads_fts MATCH ?
               ORDER BY rank
               LIMIT 15""",
            (query_text,),
        ).fetchall()
        if not rows:
            print(f"No threads match {query_text!r}.")
            return
        for r in rows:
            print(f"  [{(r['lane'] or '-'):<10}] {(r['status'] or '-'):<10} {(r['last_ts'] or '')[:10]} {r['subject'][:55] if r['subject'] else '(no subject)'}")
            print(f"      last sender: {r['last_sender_email'] or '?'} | thread {r['thread_id']}")
        print(f"\n({len(rows)} threads)")
        return

    # Shortcut: dms [name-or-keyword], recent Discord DM threads + FTS content hits
    if arg1 == "dms":
        query_text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        name_like = f"%{query_text}%"
        rows = conn.execute(
            """SELECT t.thread_id, t.last_ts, t.message_count, t.topic_label,
                      c.dm_recipient_username, c.name AS chan_name,
                      p.name AS person_name
               FROM discord_threads t
               JOIN discord_channels c ON c.id = t.channel_id
               LEFT JOIN people p ON p.id = c.dm_recipient_person_id
               WHERE t.channel_type IN ('dm','group_dm')
                 AND (? = '' OR c.dm_recipient_username LIKE ? OR p.name LIKE ? OR c.name LIKE ?)
               ORDER BY t.last_ts DESC
               LIMIT 15""",
            (query_text, name_like, name_like, name_like),
        ).fetchall()
        if rows:
            print("Recent DM threads:")
            for r in rows:
                who = r["person_name"] or r["dm_recipient_username"] or r["chan_name"] or "?"
                print(f"  {(r['last_ts'] or '')[:16]}  {who[:30]:<30} {r['message_count'] or 0:>4} msgs  {(r['topic_label'] or '')[:40]}")
                print(f"      thread {r['thread_id']}")
        else:
            print(f"No DM threads match {query_text!r}.")
        # FTS over DM message content (skip when no keyword given)
        if query_text.strip():
            try:
                hits = conn.execute(
                    """SELECT m.timestamp, m.content, c.dm_recipient_username, c.name AS chan_name
                       FROM discord_messages_fts f
                       JOIN discord_messages m ON m.rowid = f.rowid
                       JOIN discord_channels c ON c.id = m.channel_id
                       WHERE c.channel_type IN ('dm','group_dm')
                         AND discord_messages_fts MATCH ?
                       ORDER BY rank LIMIT 10""",
                    (query_text,),
                ).fetchall()
            except sqlite3.OperationalError:
                hits = []  # keyword not FTS-safe (e.g. odd punctuation); name matches above still shown
            if hits:
                print("\nDM content matches:")
                for h in hits:
                    who = h["dm_recipient_username"] or h["chan_name"] or "?"
                    print(f"  {(h['timestamp'] or '')[:16]}  [{who[:20]}] {(h['content'] or '')[:90]}")
        return

    # Shortcut: faqs [topic], list approved/proposed FAQs, optional topic filter
    if arg1 == "faqs":
        topic = sys.argv[2] if len(sys.argv) > 2 else None
        params = ()
        sql = """SELECT faq_id, topic, status, ask_count, last_asked_at,
                        substr(question_canonical, 1, 70) AS q
                 FROM faqs WHERE status IN ('approved','proposed','draft')"""
        if topic:
            sql += " AND topic LIKE ?"
            params = (f"%{topic}%",)
        sql += " ORDER BY status, ask_count DESC LIMIT 30"
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            print(f"No FAQs found{f' for topic {topic!r}' if topic else ''}.")
            return
        print(f"{'faq_id':<22} {'status':<10} {'topic':<14} {'asks':>5}  question")
        print("-" * 100)
        for r in rows:
            print(f"{r['faq_id']:<22} {r['status']:<10} {(r['topic'] or '-'):<14} {(r['ask_count'] or 0):>5}  {(r['q'] or '')}")
        # Also show pending un-clustered occurrences count
        pending = conn.execute("SELECT count(*) FROM faq_occurrences WHERE faq_id IS NULL").fetchone()[0]
        print(f"\n({len(rows)} canonical FAQs; {pending} un-clustered occurrences pending cluster review)")
        return

    # Shortcut: episodes "keyword", hybrid FTS + (when available) semantic on episodes
    if arg1 == "episodes":
        query_text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query_text.strip():
            print('Usage: python query.py episodes "keyword"')
            return
        rows = conn.execute(
            """SELECT e.id, e.kind, e.topic, e.ts, e.direction, e.channel,
                      substr(IFNULL(e.summary, ''), 1, 80) AS s
               FROM episodes_fts f JOIN episodes e ON e.id = f.rowid
               WHERE episodes_fts MATCH ?
               ORDER BY rank LIMIT 15""",
            (query_text,),
        ).fetchall()
        if not rows:
            print(f"No episodes match {query_text!r}.")
            return
        for r in rows:
            print(f"  [{r['kind']:<10}] {r['ts'][:10] if r['ts'] else '?':<11} {r['direction'] or '-':<8} {(r['topic'] or '?')[:50]:<50}")
            if r['s']:
                print(f"      {r['s']}")
        print(f"\n({len(rows)} episodes; use embed_episodes.py --search for semantic)")
        return

    # Shortcut: sessions "keyword", FTS over session_log_entries (per-session
    # changelog rows split out of the build-status doc)
    if arg1 == "sessions":
        query_text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query_text.strip():
            rows = conn.execute(
                """SELECT id, session_id, entry_date, substr(IFNULL(title,''),1,80) AS t
                   FROM session_log_entries ORDER BY id DESC LIMIT 15""").fetchall()
            print("Latest session entries (query.py sessions \"keyword\" to search):")
        else:
            rows = conn.execute(
                """SELECT e.id, e.session_id, e.entry_date, substr(IFNULL(e.title,''),1,80) AS t
                   FROM session_log_entries_fts f JOIN session_log_entries e ON e.id = f.rowid
                   WHERE session_log_entries_fts MATCH ?
                   ORDER BY rank LIMIT 15""", (query_text,)).fetchall()
            if not rows:
                print(f"No session entries match {query_text!r}.")
                return
        for r in rows:
            print(f"  [{r['id']:>3}] {r['session_id'] or '?':<14} {(r['entry_date'] or '?')[:10]:<11} {r['t']}")
        print(f"\n({len(rows)} entries; full text: SELECT raw_md FROM session_log_entries WHERE id=N)")
        return

    # FSRS spaced repetition stats
    if arg1 == "fsrs-stats":
        stats = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN fsrs_state IS NOT NULL THEN 1 ELSE 0 END) as has_fsrs,
                SUM(CASE WHEN fsrs_due IS NULL THEN 1 ELSE 0 END) as no_fsrs,
                SUM(CASE WHEN fsrs_due <= datetime('now') THEN 1 ELSE 0 END) as due_now,
                SUM(CASE WHEN fsrs_due > datetime('now') THEN 1 ELSE 0 END) as not_due,
                AVG(fsrs_stability) as avg_s,
                MIN(fsrs_stability) as min_s,
                MAX(fsrs_stability) as max_s,
                AVG(fsrs_difficulty) as avg_d
            FROM learnings WHERE status = 'active'
        """).fetchone()

        print(f"FSRS Stats: {stats['total']} active learnings")
        print(f"  Due now:       {stats['due_now'] or 0}")
        print(f"  Not yet due:   {stats['not_due'] or 0}")
        print(f"  No FSRS state: {stats['no_fsrs'] or 0}")
        if stats["avg_s"]:
            print(f"  Stability:     avg={stats['avg_s']:.1f}d, min={stats['min_s']:.1f}d, max={stats['max_s']:.1f}d")
            print(f"  Difficulty:    avg={stats['avg_d']:.1f}")

        # Stability distribution
        buckets = conn.execute("""
            SELECT
                CASE
                    WHEN fsrs_stability <= 1 THEN '<=1d'
                    WHEN fsrs_stability <= 3 THEN '1-3d'
                    WHEN fsrs_stability <= 7 THEN '3-7d'
                    WHEN fsrs_stability <= 14 THEN '7-14d'
                    WHEN fsrs_stability <= 30 THEN '14-30d'
                    ELSE '>30d'
                END as bucket,
                COUNT(*) as cnt
            FROM learnings WHERE status = 'active' AND fsrs_stability IS NOT NULL
            GROUP BY bucket ORDER BY MIN(fsrs_stability)
        """).fetchall()
        if buckets:
            print(f"\n  Stability distribution:")
            for b in buckets:
                bar = "#" * min(b["cnt"], 40)
                print(f"    {b['bucket']:>6s}: {b['cnt']:3d} {bar}")

        # Recent reviews
        try:
            reviews = conn.execute("""
                SELECT l.title, lr.rating, lr.reviewed_at, lr.source,
                       lr.fsrs_stability_before as s_before, lr.fsrs_stability_after as s_after
                FROM learning_reviews lr
                JOIN learnings l ON l.id = lr.learning_id
                WHERE lr.rating IS NOT NULL
                ORDER BY lr.reviewed_at DESC LIMIT 10
            """).fetchall()
            if reviews:
                rating_names = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}
                print(f"\n  Recent reviews:")
                for r in reviews:
                    rn = rating_names.get(r["rating"], "?")
                    s_info = ""
                    if r["s_before"] and r["s_after"]:
                        s_info = f" S:{r['s_before']:.1f}->{r['s_after']:.1f}d"
                    src = f" ({r['source']})" if r["source"] else ""
                    print(f"    [{rn}]{src} {r['title'][:55]}{s_info}")
        except Exception:
            pass  # Table might not exist yet

        return

    # Rebuild mode: list/search system-rebuild docs
    if arg1 == "rebuild":
        query_text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""

        if query_text and query_text.strip():
            like = f"%{query_text}%"
            rows = conn.execute(
                "SELECT slug, title, doc_type, "
                "substr(content, max(1, instr(lower(content), lower(?))-50), 150) as context "
                "FROM reference_docs WHERE category='system-rebuild' "
                "AND (content LIKE ? OR title LIKE ? OR tags LIKE ?) "
                "ORDER BY doc_type, slug",
                (query_text, like, like, like)
            ).fetchall()

            if rows:
                for r in rows:
                    print(f"[{r['doc_type']}] {r['slug']}: {r['title']}")
                    ctx = (r['context'] or '').replace('\n', ' ').strip()
                    if ctx:
                        print(f"    ...{ctx}...")
                print(f"\n({len(rows)} docs matching '{query_text}')")
            else:
                print(f"No system-rebuild docs match '{query_text}'.")
        else:
            rows = conn.execute(
                "SELECT slug, title, doc_type, tags, length(content) as chars "
                "FROM reference_docs WHERE category='system-rebuild' "
                "ORDER BY doc_type, slug"
            ).fetchall()

            if rows:
                print("slug | title | doc_type | tags | chars")
                print("-" * 80)
                for r in rows:
                    tags = (r['tags'] or '')[:40]
                    print(f"{r['slug']} | {r['title']} | {r['doc_type']} | {tags} | {r['chars']}")
                print(f"\n({len(rows)} system-rebuild docs)")
            else:
                print("No system-rebuild docs found.")
        return

    # Chunks mode: section-level search within reference_docs
    if arg1 == "chunks":
        query_text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query_text or not query_text.strip():
            print('Usage: python query.py chunks "search term"')
            print('Searches reference_doc_chunks_fts for section-level matches.')
            return

        # Build FTS5 query: split words, join with OR for broad matching
        words = [w for w in query_text.split() if len(w) > 1]
        fts_query = " OR ".join(words) if words else query_text

        try:
            rows = conn.execute("""
                SELECT c.heading, substr(c.content, 1, 300) as snippet, c.doc_slug,
                       c.chunk_index, length(c.content) as chunk_len
                FROM reference_doc_chunks c
                JOIN reference_doc_chunks_fts fts ON c.id = fts.rowid
                WHERE reference_doc_chunks_fts MATCH ?
                ORDER BY rank
                LIMIT 10
            """, (fts_query,)).fetchall()
        except Exception as e:
            print(f"FTS5 error: {e}")
            return

        if rows:
            for r in rows:
                heading = r['heading'] or '(no heading)'
                snippet = (r['snippet'] or '').replace('\n', ' ').strip()
                if len(snippet) > 200:
                    snippet = snippet[:200] + '...'
                print(f"[{r['doc_slug']}] {heading}")
                print(f"  chunk #{r['chunk_index']} ({r['chunk_len']} chars): {snippet}")
                print()
            print(f"({len(rows)} chunks matching '{query_text}')")
            print(f'Read full chunk: query.py "SELECT content FROM reference_doc_chunks WHERE doc_slug=\'SLUG\' AND chunk_index=N"')
        else:
            print(f"No chunks matching '{query_text}'.")
        return

    # Vector email search
    if arg1 == "vemail":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query:
            print("Usage: query.py vemail \"search query\"")
            return
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            if not any(r[1] == 'vecdb' for r in conn.execute('PRAGMA database_list')):
                conn.execute("ATTACH DATABASE '%s' AS vecdb" % _VEC_DB)
            conn.enable_load_extension(False)
        except Exception:
            print("sqlite-vec not available. Run: pip install sqlite-vec")
            return

        vec_count = conn.execute("SELECT COUNT(*) FROM vec_emails").fetchone()[0]
        if vec_count == 0:
            print("No embeddings. Run: python embed_emails.py")
            return

        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("BAAI/bge-small-en-v1.5")
        emb = model.encode(query, normalize_embeddings=True)
        vec_bytes = struct.pack(f"384f", *emb.tolist())

        results = conn.execute("""
            SELECT e.id, e.sender_name, e.sender_email, e.subject, e.timestamp, ve.distance
            FROM vec_emails ve
            JOIN emails e ON e.id = ve.rowid
            WHERE ve.embedding MATCH ? AND k = 10
            ORDER BY ve.distance
        """, (vec_bytes,)).fetchall()

        print(f"{'Date':<20} {'From':<25} {'Subject':<50} {'Dist':>6}")
        print("-" * 105)
        for r in results:
            date = (r["timestamp"] or "")[:19]
            sender = (r["sender_name"] or r["sender_email"] or "")[:24]
            subj = (r["subject"] or "")[:49]
            print(f"{date:<20} {sender:<25} {subj:<50} {r['distance']:>6.4f}")
        return

    # Edges mode: show all edges for a person
    if arg1 == "edges":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query:
            # Summary stats
            rows = conn.execute(
                "SELECT relation, count(*) as cnt FROM edges GROUP BY relation ORDER BY cnt DESC"
            ).fetchall()
            print("relation | count")
            print("-" * 40)
            for r in rows:
                print(f"{r['relation']} | {r['cnt']}")
            total = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
            print(f"\n({total} total edges)")
            return

        like = f"%{query}%"
        rows = conn.execute("""
            SELECT e.relation, e.fact, e.valid_from, e.valid_until, e.confidence, e.source,
                   COALESCE(t.name, e.target_id) as target_name,
                   COALESCE(s.name, e.source_id) as source_name
            FROM edges e
            LEFT JOIN entities s ON e.source_id = s.id
            LEFT JOIN entities t ON e.target_id = t.id
            WHERE s.name LIKE ? OR t.name LIKE ? OR e.fact LIKE ?
            ORDER BY e.valid_from DESC
        """, (like, like, like)).fetchall()
        if rows:
            for r in rows:
                until = f" -> {r['valid_until']}" if r['valid_until'] else " (current)"
                print(f"[{r['relation']}] {r['source_name']} -> {r['target_name']}")
                print(f"  {r['valid_from']}{until} | conf={r['confidence']} | src={r['source']}")
                if r['fact']:
                    print(f"  {r['fact']}")
                print()
            print(f"({len(rows)} edges)")
        else:
            print(f"No edges matching '{query}'.")
        return

    # Graph mode: show all connections for an entity
    if arg1 == "graph":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query:
            print("Usage: query.py graph \"entity name or id\"")
            return

        like = f"%{query}%"
        rows = conn.execute("""
            SELECT e.relation, e.valid_from, e.valid_until,
                   COALESCE(s.name, e.source_id) as source_name, s.type as source_type,
                   COALESCE(t.name, e.target_id) as target_name, t.type as target_type
            FROM edges e
            LEFT JOIN entities s ON e.source_id = s.id
            LEFT JOIN entities t ON e.target_id = t.id
            WHERE s.name LIKE ? OR t.name LIKE ? OR e.source_id LIKE ? OR e.target_id LIKE ?
            ORDER BY e.relation, e.valid_from DESC
        """, (like, like, like, like)).fetchall()
        if rows:
            current_rel = None
            for r in rows:
                if r['relation'] != current_rel:
                    current_rel = r['relation']
                    print(f"\n=== {current_rel} ===")
                until = r['valid_until'] or 'current'
                print(f"  {r['source_name']} ({r['source_type']}) -> {r['target_name']} ({r['target_type']})  [{r['valid_from']} .. {until}]")
            print(f"\n({len(rows)} edges)")
        else:
            print(f"No graph connections matching '{query}'.")
        return

    # Multi-hop graph traversal
    if arg1 == "hop":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query:
            print('Usage: query.py hop "entity name" [depth]')
            return

        # Parse optional depth
        parts = query.rsplit(" ", 1)
        depth = 2
        if len(parts) == 2 and parts[1].isdigit():
            depth = int(parts[1])
            query = parts[0]

        like = f"%{query}%"
        # Find starting entity
        start = conn.execute(
            "SELECT id, name, type FROM entities WHERE name LIKE ? AND status='active' LIMIT 1",
            (like,)
        ).fetchone()
        if not start:
            print(f"No entity matching '{query}'.")
            return

        print(f"Graph traversal from: {start['name']} ({start['type']}), depth={depth}")
        print()

        # Recursive CTE for multi-hop traversal
        results = conn.execute("""
            WITH RECURSIVE hops(entity_id, entity_name, entity_type, hop, path, relation) AS (
                -- Seed: starting entity
                SELECT id, name, type, 0, id, ''
                FROM entities WHERE id = ?

                UNION ALL

                -- Outgoing edges
                SELECT e2.id, e2.name, e2.type, h.hop + 1,
                       h.path || ' -> ' || e2.id, ed.relation
                FROM hops h
                JOIN edges ed ON ed.source_id = h.entity_id
                JOIN entities e2 ON ed.target_id = e2.id
                WHERE h.hop < ? AND h.path NOT LIKE '%' || e2.id || '%'
                AND e2.status = 'active'

                UNION ALL

                -- Incoming edges
                SELECT e2.id, e2.name, e2.type, h.hop + 1,
                       h.path || ' -> ' || e2.id, ed.relation
                FROM hops h
                JOIN edges ed ON ed.target_id = h.entity_id
                JOIN entities e2 ON ed.source_id = e2.id
                WHERE h.hop < ? AND h.path NOT LIKE '%' || e2.id || '%'
                AND e2.status = 'active'
            )
            SELECT DISTINCT entity_name, entity_type, hop, relation
            FROM hops
            WHERE hop > 0
            ORDER BY hop, entity_type, entity_name
        """, (start['id'], depth, depth)).fetchall()

        if results:
            current_hop = 0
            for r in results:
                if r['hop'] != current_hop:
                    current_hop = r['hop']
                    print(f"--- Hop {current_hop} ---")
                print(f"  [{r['relation']}] {r['entity_name']} ({r['entity_type']})")
            print(f"\n({len(results)} entities within {depth} hops)")
        else:
            print("No connections found.")
        return

    # Discord thread viewer: show messages + participants + linked entities for one thread.
    # discord_threads.thread_id format = "<channel_id>-<first_message_id>"; users may pass
    # just the first_message_id, the channel_id, or the full thread_id.
    if arg1 == "discord_thread":
        query = sys.argv[2] if len(sys.argv) > 2 else ""
        if not query:
            print('Usage: python query.py discord_thread <thread_id|first_msg_id|channel_id> [limit]')
            return
        try:
            limit = int(sys.argv[3]) if len(sys.argv) > 3 else 50
        except ValueError:
            limit = 50
        # Resolve: exact thread_id, OR thread_id ending in this id, OR most recent
        # thread on a channel matching this id.
        t = conn.execute(
            "SELECT * FROM discord_threads WHERE thread_id=? "
            "OR thread_id LIKE ? OR thread_id LIKE ? "
            "ORDER BY first_ts DESC LIMIT 1",
            (query, f"%-{query}", f"{query}-%"),
        ).fetchone()
        if not t:
            print(f"No discord_thread matching '{query}'. Try a thread_id, first_message_id, or channel_id prefix.")
            return
        ch = conn.execute(
            "SELECT id, name, channel_type, dm_recipient_username, group_dm_recipient_ids "
            "FROM discord_channels WHERE id=?",
            (t["channel_id"],),
        ).fetchone()
        ch_name = (ch["name"] if ch else None) or ch["dm_recipient_username"] if ch else None or "(unknown)"
        ch_type = ch["channel_type"] if ch else "?"
        print(f"=== discord_thread {t['thread_id']} ===")
        print(f"  channel    : {ch_name} [{ch_type}] (id={t['channel_id']})")
        print(f"  window     : {t['first_ts']}  ..  {t['last_ts']}")
        print(f"  messages   : {t['message_count']}")
        print(f"  status     : {t['status']}")
        if t["topic_label"]:
            print(f"  topic      : {t['topic_label']}")
        # Participants from author_ids CSV -> usernames + person links
        author_ids = [a.strip() for a in (t["author_ids"] or "").split(",") if a.strip()]
        if author_ids:
            placeholders = ",".join("?" * len(author_ids))
            users = conn.execute(
                f"SELECT du.id, du.username, du.person_id, p.name AS person_name "
                f"FROM discord_users du LEFT JOIN people p ON p.id=du.person_id "
                f"WHERE du.id IN ({placeholders})",
                author_ids,
            ).fetchall()
            print(f"\n-- Participants ({len(users)}) --")
            for u in users:
                pid_str = f"  -> person-{u['person_id']} ({u['person_name']})" if u["person_id"] else ""
                print(f"  @{u['username'] or '?':<24} (id={u['id']}){pid_str}")
        # Linked entities (entity id pattern: entities.id = thread_id for discord_thread rows)
        ents = conn.execute(
            "SELECT relation, target_id, "
            "(SELECT name FROM entities WHERE id=edges.target_id) AS target_name, "
            "(SELECT type FROM entities WHERE id=edges.target_id) AS target_type "
            "FROM edges WHERE source_id=?",
            (t["thread_id"],),
        ).fetchall()
        if ents:
            print(f"\n-- Graph edges ({len(ents)}) --")
            for e in ents:
                print(f"  [{e['relation']}] -> {e['target_name'] or e['target_id']} ({e['target_type'] or '?'})")
        # Messages
        msgs = conn.execute(
            "SELECT dm.id, dm.timestamp, dm.author_id, dm.content, du.username "
            "FROM discord_messages dm LEFT JOIN discord_users du ON du.id=dm.author_id "
            "WHERE dm.thread_id=? ORDER BY dm.timestamp ASC LIMIT ?",
            (t["thread_id"], limit),
        ).fetchall()
        print(f"\n-- Messages (showing {len(msgs)} of {t['message_count']}) --")
        for m in msgs:
            who = m["username"] or m["author_id"] or "?"
            ts = (m["timestamp"] or "")[:19]
            body = (m["content"] or "").replace("\n", " ")[:200]
            print(f"  [{ts}] @{who:<20} {body}")
        if t["message_count"] > limit:
            print(f"  ... +{t['message_count'] - limit} more (pass higher limit as arg 3)")
        return

    # Who knows: find people connected to an org
    if arg1 == "whoknows":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not query:
            print('Usage: query.py whoknows "org name"')
            return

        like = f"%{query}%"
        # Find the org entity
        org = conn.execute(
            "SELECT id, name FROM entities WHERE type='org' AND name LIKE ? AND status='active' LIMIT 1",
            (like,)
        ).fetchone()

        if not org:
            # Try matching in people.headline directly
            people = conn.execute("""
                SELECT DISTINCT p.name, p.email, p.headline AS affiliation, p.interaction_count
                FROM people p
                WHERE p.headline LIKE ? AND p.name IS NOT NULL AND p.name != ''
                ORDER BY p.interaction_count DESC NULLS LAST
                LIMIT 20
            """, (like,)).fetchall()
            if people:
                print(f"People affiliated with '{query}' (from people table):")
                for p in people:
                    ic = p['interaction_count'] or 0
                    print(f"  {p['name']} <{p['email']}> ({p['affiliation']}) [{ic} interactions]")
                print(f"\n({len(people)} people)")
            else:
                print(f"No org or people matching '{query}'.")
            return

        print(f"People connected to: {org['name']}")
        print()

        # Direct: works_at edges
        direct = conn.execute("""
            SELECT e.name, e.type, ed.relation, ed.valid_from, ed.valid_until
            FROM edges ed
            JOIN entities e ON ed.source_id = e.id
            WHERE ed.target_id = ? AND e.status = 'active'
            ORDER BY ed.relation, e.name
        """, (org['id'],)).fetchall()

        if direct:
            print("=== Direct connections ===")
            for r in direct:
                until = f" (until {r['valid_until']})" if r['valid_until'] else ""
                print(f"  [{r['relation']}] {r['name']}{until}")

        # 2nd degree: people who share an event/project with org members.
        # Tune the relation list to your own edge vocabulary.
        second = conn.execute("""
            SELECT DISTINCT e3.name, ed2.relation, e2.name as via_entity
            FROM edges ed1
            JOIN edges ed2 ON ed1.source_id = ed2.source_id AND ed2.target_id != ed1.target_id
            JOIN entities e3 ON ed1.source_id = e3.id
            JOIN entities e2 ON ed2.target_id = e2.id
            WHERE ed1.target_id = ? AND e3.status = 'active'
            AND ed2.relation IN ('participated_in', 'attended', 'collaborated_with')
            AND e3.name NOT IN (SELECT e.name FROM edges ed JOIN entities e ON ed.source_id = e.id WHERE ed.target_id = ?)
            LIMIT 20
        """, (org['id'], org['id'])).fetchall()

        if second:
            print(f"\n=== Also connected via shared events ===")
            for r in second:
                print(f"  {r['name']} [{r['relation']} {r['via_entity']}]")

        total = len(direct) + len(second)
        print(f"\n({total} connections)")
        return

    # Enrich mode: sync people -> entities + update contact counts
    if arg1 == "enrich":
        import json as _json

        # Step 1: Update contact data from emails
        res1 = conn.execute("""
            UPDATE people SET
                last_contact_date = (SELECT MAX(e.timestamp) FROM emails e WHERE e.person_id = people.id),
                total_emails = (SELECT COUNT(*) FROM emails e WHERE e.person_id = people.id),
                updated_at = datetime('now')
            WHERE id IN (SELECT DISTINCT person_id FROM emails WHERE person_id IS NOT NULL)
        """)
        conn.commit()
        print(f"Updated {res1.rowcount} people from emails")

        # Step 2: Update discord counts
        res2 = conn.execute("""
            UPDATE people SET
                total_discord_msgs = (SELECT COUNT(*) FROM discord_messages d WHERE d.author_id = people.discord_user_id),
                updated_at = datetime('now')
            WHERE discord_user_id IS NOT NULL AND discord_user_id != ''
        """)
        conn.commit()
        print(f"Updated {res2.rowcount} people from discord")

        # Step 3: Recompute interaction_count
        conn.execute("""
            UPDATE people SET
                interaction_count = COALESCE(total_emails, 0) + COALESCE(total_discord_msgs, 0),
                updated_at = datetime('now')
            WHERE total_emails > 0 OR total_discord_msgs > 0
        """)
        conn.commit()

        # Step 4: Sync people -> entities
        enriched = 0
        fields = ('location', 'linkedin', 'seniority', 'headline',
                  'career_stage', 'country', 'research_interests', 'tags',
                  'last_contact_date', 'interaction_count', 'total_emails', 'total_discord_msgs')
        cols_sql = ", ".join(fields)

        for ent in conn.execute("SELECT id, data FROM entities WHERE type='person'").fetchall():
            d = _json.loads(ent['data']) if ent['data'] else {}
            pid = d.get('people_id')
            if not pid:
                continue
            person = conn.execute(f"SELECT {cols_sql} FROM people WHERE id=?", (pid,)).fetchone()
            if not person:
                continue
            changed = False
            for key in fields:
                val = person[key]
                if val and str(val).strip() and str(val) != '-' and (not d.get(key) or key in ('last_contact_date', 'interaction_count', 'total_emails', 'total_discord_msgs')):
                    d[key] = val if not isinstance(val, int) else val
                    changed = True
            if changed:
                conn.execute("UPDATE entities SET data=?, updated_at=datetime('now') WHERE id=?",
                             (_json.dumps(d), ent['id']))
                enriched += 1

        conn.commit()
        print(f"Synced {enriched} entities from people table")
        print(f"People with interactions: {conn.execute('SELECT count(*) FROM people WHERE interaction_count > 0').fetchone()[0]}")
        return

    # Hook performance snapshot
    if arg1 == "hookhealth":
        # Last 24h: per-hook stats
        rows = conn.execute("""
            SELECT hook_name, event_type,
                   COUNT(*) AS calls,
                   ROUND(AVG(duration_ms), 1) AS avg_ms,
                   ROUND(MAX(duration_ms), 1) AS max_ms,
                   SUM(CASE WHEN error_message IS NOT NULL AND error_message != '' THEN 1 ELSE 0 END) AS errors
            FROM hook_health
            WHERE replace(ts,'T',' ') > datetime('now', '-1 day')  -- ts is ISO-T; normalize before compare
            GROUP BY hook_name, event_type
            ORDER BY calls DESC
        """).fetchall()
        if rows:
            print("=== Last 24h Hook Performance ===")
            print(f"{'Hook':<25} {'Event':<15} {'Calls':>6} {'Avg ms':>8} {'Max ms':>8} {'Errors':>7}")
            print("-" * 75)
            for r in rows:
                print(f"{r['hook_name']:<25} {r['event_type']:<15} {r['calls']:>6} {r['avg_ms']:>8} {r['max_ms']:>8} {r['errors']:>7}")
        else:
            print("No hook_health data in last 24h.")

        # Daily aggregates (last 7 days)
        daily = conn.execute("""
            SELECT date, hook_name,
                   SUM(call_count) AS calls,
                   ROUND(AVG(avg_duration_ms), 1) AS avg_ms,
                   ROUND(MAX(max_duration_ms), 1) AS peak_ms,
                   SUM(error_count) AS errors
            FROM hook_health_daily
            WHERE date > date('now', '-7 days')
            GROUP BY date, hook_name
            ORDER BY date DESC, calls DESC
        """).fetchall()
        if daily:
            print("\n=== Daily Aggregates (7d) ===")
            print(f"{'Date':<12} {'Hook':<25} {'Calls':>6} {'Avg ms':>8} {'Peak ms':>9} {'Errors':>7}")
            print("-" * 72)
            for r in daily:
                print(f"{r['date']:<12} {r['hook_name']:<25} {r['calls']:>6} {r['avg_ms']:>8} {r['peak_ms']:>9} {r['errors']:>7}")
        return

    # links view: everything connected to a row in either direction
    if arg1 == "links":
        target = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not target:
            # Summary
            rows = conn.execute(
                "SELECT relation, COUNT(*) AS cnt FROM links GROUP BY relation ORDER BY cnt DESC"
            ).fetchall()
            print(f"{'relation':<25} count")
            print("-" * 40)
            for r in rows:
                print(f"{r['relation']:<25} {r['cnt']}")
            total = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            print(f"\n({total} total links)")
            return
        print(f"=== Links touching '{target}' ===")
        out = conn.execute("""
            SELECT 'outgoing' AS dir, src_table, src_id, relation, dst_table, dst_id, source, confidence
            FROM links WHERE src_id = ?
            UNION ALL
            SELECT 'incoming' AS dir, src_table, src_id, relation, dst_table, dst_id, source, confidence
            FROM links WHERE dst_id = ?
            ORDER BY dir, relation
        """, (target, target)).fetchall()
        for r in out:
            arrow = '->' if r['dir'] == 'outgoing' else '<-'
            print(f"  [{r['relation']}] {r['src_table']}/{r['src_id']} {arrow} {r['dst_table']}/{r['dst_id']}  (conf={r['confidence']}, src={r['source']})")
        print(f"\n({len(out)} links)")
        return

    if arg1 == "schema":
        if arg2:
            cols = conn.execute(f"PRAGMA table_info([{arg2}])").fetchall()
            print(f"{arg2}:")
            for c in cols:
                print(f"  {c['name']} ({c['type']}){' PK' if c['pk'] else ''}")
        else:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            for t in tables:
                name = t[0]
                count = conn.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
                cols = [c['name'] for c in conn.execute(f"PRAGMA table_info([{name}])").fetchall()]
                print(f"{name} ({count}): {', '.join(cols)}")
        return

    # Raw SQL mode
    if arg1.upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE")):
        sql = " ".join(sys.argv[1:])
        cursor = conn.execute(sql)
        if sql.strip().upper().startswith("SELECT"):
            rows = cursor.fetchall()
            if rows:
                keys = rows[0].keys()
                print(" | ".join(keys))
                print("-" * 80)
                for r in rows:
                    vals = [("" if r[k] is None else str(r[k])).replace("\n", " ")[:120] for k in keys]
                    print(" | ".join(vals))
                print(f"\n({len(rows)} rows)")
            else:
                print("(0 rows)")
        else:
            conn.commit()
            print(f"({cursor.rowcount} rows affected)")
        return

    # Table shorthand mode
    table = arg1
    # Verify table exists
    exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists:
        print(f"No table '{table}'. Use: python query.py schema")
        return

    if not arg2:
        rows = conn.execute(f"SELECT * FROM {table} LIMIT 20").fetchall()
    elif arg2.upper() in ("OPEN", "WAITING", "BLOCKED", "DONE"):
        rows = conn.execute(f"SELECT * FROM {table} WHERE status = ? LIMIT 30", (arg2.upper(),)).fetchall()
    elif arg2 == "pending":
        rows = conn.execute(f"SELECT * FROM {table} WHERE status = 'pending' LIMIT 30").fetchall()
    else:
        # Search mode: match against name/email/description/details
        like = f"%{arg2}%"
        cols = [c['name'] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        search_cols = [c for c in cols if c in ("name", "email", "description", "details", "title", "task_name", "subject", "content", "summary")]
        if search_cols:
            where = " OR ".join(f"{c} LIKE ?" for c in search_cols)
            rows = conn.execute(f"SELECT * FROM {table} WHERE {where} LIMIT 20", [like] * len(search_cols)).fetchall()
        else:
            rows = conn.execute(f"SELECT * FROM {table} LIMIT 20").fetchall()

    if rows:
        keys = rows[0].keys()
        # Skip very wide columns for display
        display_keys = [k for k in keys if k not in ("body", "content", "row_data", "description", "context", "details_json", "meta_json", "participants_json")]
        print(" | ".join(display_keys))
        print("-" * 80)
        for r in rows:
            vals = [("" if r[k] is None else str(r[k])).replace("\n", " ")[:120] for k in display_keys]
            print(" | ".join(vals))
        print(f"\n({len(rows)} rows)")
    else:
        print("(0 rows)")


def _cosine_sim(a_blob, b_blob):
    """Cosine similarity between two float32 BLOB vectors."""
    n = len(a_blob) // 4
    a = struct.unpack(f"{n}f", a_blob)
    b = struct.unpack(f"{n}f", b_blob)
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def add_edge(conn, source_id, target_id, relation, fact=None, valid_from=None, source_label=None, confidence=1.0):
    """Insert edge with bi-temporal tracking. Auto-closes existing open edge for same triple."""
    from datetime import datetime
    existing = conn.execute(
        "SELECT id FROM edges WHERE source_id=? AND target_id=? AND relation=? AND valid_until IS NULL",
        (source_id, target_id, relation)
    ).fetchone()
    if existing:
        conn.execute("UPDATE edges SET valid_until=datetime('now') WHERE id=?", (existing['id'],))
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, fact, valid_from, recorded_at, confidence, source) "
        "VALUES (?,?,?,?,?,datetime('now'),?,?)",
        (source_id, target_id, relation, fact, valid_from or datetime.now().isoformat(), confidence, source_label)
    )
    conn.commit()


def _route(conn, query):
    """Smart table routing: FTS5 + embedding cosine similarity + RRF."""
    TOP_N = 5
    RRF_K = 60

    # Tier 1: FTS5 keyword match
    fts_scores = {}
    try:
        fts_rows = conn.execute(
            "SELECT table_name, rank FROM _table_descriptions_fts "
            "WHERE _table_descriptions_fts MATCH ? ORDER BY rank LIMIT 15",
            (query,),
        ).fetchall()
        for i, r in enumerate(fts_rows):
            fts_scores[r["table_name"]] = i + 1  # rank position (1-based)
    except Exception:
        # FTS5 MATCH can fail on syntax errors; try individual words
        words = [w for w in query.split() if len(w) > 2]
        if words:
            fts_query = " OR ".join(words)
            try:
                fts_rows = conn.execute(
                    "SELECT table_name, rank FROM _table_descriptions_fts "
                    "WHERE _table_descriptions_fts MATCH ? ORDER BY rank LIMIT 15",
                    (fts_query,),
                ).fetchall()
                for i, r in enumerate(fts_rows):
                    fts_scores[r["table_name"]] = i + 1
            except Exception:
                pass

    # Tier 2: Embedding cosine similarity
    embed_scores = {}
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("BAAI/bge-small-en-v1.5")
        q_emb = model.encode(query, normalize_embeddings=True)
        q_blob = struct.pack(f"384f", *q_emb.tolist())

        rows = conn.execute(
            "SELECT table_name, embedding FROM _table_descriptions WHERE embedding IS NOT NULL"
        ).fetchall()

        sims = []
        for r in rows:
            sim = _cosine_sim(q_blob, r["embedding"])
            sims.append((r["table_name"], sim))
        sims.sort(key=lambda x: x[1], reverse=True)

        for i, (name, sim) in enumerate(sims):
            embed_scores[name] = i + 1  # rank position (1-based)
    except ImportError:
        pass  # sentence-transformers not installed; FTS-only fallback

    # Tier 3: Reciprocal Rank Fusion
    all_tables = set(fts_scores) | set(embed_scores)
    rrf = {}
    for t in all_tables:
        score = 0.0
        if t in fts_scores:
            score += 1.0 / (RRF_K + fts_scores[t])
        if t in embed_scores:
            score += 1.0 / (RRF_K + embed_scores[t])
        rrf[t] = score

    ranked = sorted(rrf.items(), key=lambda x: x[1], reverse=True)[:TOP_N]

    if not ranked:
        print(f'No tables match: "{query}"')
        return

    # Output
    print(f'Routing: "{query}"\n')
    for i, (table_name, score) in enumerate(ranked, 1):
        row = conn.execute(
            "SELECT description, key_columns, example_queries, category "
            "FROM _table_descriptions WHERE table_name=?",
            (table_name,),
        ).fetchone()

        desc = (row["description"] or "") if row else ""
        key_cols = (row["key_columns"] or "") if row else ""
        examples = (row["example_queries"] or "") if row else ""
        category = (row["category"] or "") if row else ""

        # Get row count
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM [{table_name}]").fetchone()[0]
        except Exception:
            count = "?"

        # Get schema
        schema_cols = conn.execute(f"PRAGMA table_info([{table_name}])").fetchall()
        col_list = ", ".join(f"{c['name']} {c['type']}" for c in schema_cols)

        print(f"  {i}. {table_name} (score: {score:.3f}, {category}), {desc}")
        print(f"     Rows: {count}")
        print(f"     Key columns: {key_cols}")
        print(f"     Schema: {col_list}")
        if examples:
            print(f"     Example: {examples[:200]}")
        print()

    # Tier 4: Reference docs search
    _route_reference_docs(conn, query)

    print('Use: query.py "SELECT ..." to run a query.')


def _route_reference_docs(conn, query):
    """Search reference_docs_fts and print matching documents."""
    # Check that the FTS table exists
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='reference_docs_fts'"
    ).fetchone()
    if not exists:
        return

    # Build FTS5 query: split words, join with OR
    words = [w for w in query.split() if len(w) > 2]
    if not words:
        words = query.split()
    if not words:
        return
    fts_query = " OR ".join(words)

    doc_rows = []
    try:
        doc_rows = conn.execute(
            "SELECT rd.slug, rd.title, rd.doc_type, rd.category, rd.tags "
            "FROM reference_docs rd "
            "JOIN reference_docs_fts fts ON rd.id = fts.rowid "
            "WHERE reference_docs_fts MATCH ? "
            "ORDER BY rank LIMIT 3",
            (fts_query,),
        ).fetchall()
    except Exception:
        pass

    if doc_rows:
        print("  Relevant docs:")
        for r in doc_rows:
            doc_type = r["doc_type"] or "doc"
            slug = r["slug"]
            title = r["title"] or slug
            print(f"  - [{doc_type}] {slug}: {title}")
            print(f'    Read: query.py "SELECT content FROM reference_docs WHERE slug=\'{slug}\'"')
        print()


if __name__ == "__main__":
    main()
