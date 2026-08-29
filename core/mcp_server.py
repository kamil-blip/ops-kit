"""Ops MCP Server -- search / dossier / task / write surface over the local ops DB.

Tool catalog (13):
  ops_find          RRF-fused search (FTS + vector + graph) across core tables
  ops_deep          person dossier / graph traversal / bio with provenance
  ops_write         store a learning, checkpoint/resume, status, confirm-gated bus writes
  ops_query         raw SQL + query.py shortcuts
  ops_tasks         task manager (focus / stale / overdue / snooze / resolve / ...)
  ops_cross         cross-table ranked search
  ops_email_search  FTS over ops DB emails + optional gmail-to-sqlite mirror
  ops_sync          daily_sync.py wrapper (pull fresh data from configured sources)
  ops_brief_ops     brief.py wrapper (status / gather / report / registry / new_brief / drift_check)
  ops_health        health snapshot (query.py health / drift_check / daily_digest)
  ops_inbox         multi-lane inbox triage wrapper
  ops_fabric        knowledge-graph internals (attributes EAV history, relation vocab, probes)
  ops_faq           canonical FAQ search + retrieval gate

Extraction / enrichment / event-pipeline tools from the original stack are not
included in this starter kit.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

# Sibling core/ modules (paths, _db, audit_actor) are importable even before the
# installer's .pth file puts every capability dir on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths
import _db  # unified connector (busy_timeout + FK ON)

if sys.platform == "win32":
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

# Large MCP results: override default 25K char truncation (v2.1.89+)
LARGE_RESULT_META = {"anthropic/maxResultSizeChars": 500000}

SCRIPTS_DIR = Path(__file__).resolve().parent
DB = str(paths.DB_PATH)
# gmail-to-sqlite mirror is an optional install prerequisite; empty = skip that
# source. paths.GMAIL_DB_PATH already resolves the OPS_GMAIL_DB env var and
# config.toml gmail_db_path, and is None when neither is set.
GMAIL_DB = str(getattr(paths, "GMAIL_DB_PATH", None) or "")
PYTHON = str(getattr(paths, "PYTHON", sys.executable))

# The export tree is organized by capability, so wrapped scripts live in
# different sibling dirs. Resolve script names against the repo root.
_REPO_ROOT = Path(
    getattr(paths, "ROOT", None)
    or getattr(paths, "OPS_ROOT", None)
    or Path(__file__).resolve().parent.parent
)
_SCRIPT_DIRS = ("core", "tools", "search", "comms", "brief", "tasks",
                "autonomy", "learning", "memory", "logging", "interfaces")

app = Server("ops-mcp")


def get_plain_db():
    db = _db.connect(DB, timeout=10)
    db.execute("PRAGMA busy_timeout = 10000")
    db.row_factory = sqlite3.Row
    return db


def _table_exists(conn, name: str) -> bool:
    """Feature-detect a table/view; several optional tables degrade gracefully."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)).fetchone()
    return row is not None


def _find_script(script_name: str):
    """Locate a wrapped script anywhere in the export's capability dirs."""
    local = SCRIPTS_DIR / script_name
    if local.is_file():
        return local
    for d in _SCRIPT_DIRS:
        cand = _REPO_ROOT / d / script_name
        if cand.is_file():
            return cand
    return None


def _run_script(script_name, *args):
    """Run a repo script and capture output."""
    script = _find_script(script_name)
    if script is None:
        return f"ERROR: script '{script_name}' not found under {_REPO_ROOT}"
    cmd = [PYTHON, str(script)] + [str(a) for a in args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                            cwd=str(script.parent), stdin=subprocess.DEVNULL)
    output = result.stdout.strip()
    if result.returncode != 0 and result.stderr:
        output += f"\nSTDERR: {result.stderr.strip()}"
    return output or "(no output)"


def fts_query(raw: str) -> str:
    """Build FTS5 query from raw input. Sanitizes special chars, joins with OR."""
    clean = raw.replace('"', "").replace("'", "").replace("*", "")
    clean = clean.replace("(", "").replace(")", "").replace(":", "")
    words = [w.strip() for w in clean.split() if len(w.strip()) > 1]
    if not words:
        return f'"{raw}"'
    return " OR ".join(f'"{w}"*' for w in words)


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="ops_find",
            description="RRF-fused search (FTS + vector + graph PPR) across people, learnings, action items, emails, docs, discord, entities, observations, episodes, faqs. First call warms the embedding model (~15s), then <500ms. Use for any lookup, person search, or 'what do we know about X'.",
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string", "description": "Search term (name, topic, keyword)"},
                "tables": {"type": "string", "description": "Comma-separated table filter: people,learnings,actions,emails,docs,discord. Default: all.", "default": "all"},
                "limit": {"type": "integer", "default": 10}
            }, "required": ["query"]}
        ),
        Tool(
            name="ops_deep",
            description="Deep lookup: full person dossier OR multi-hop graph traversal. Use 'dossier' mode for everything about a person (contact info, emails, edges, observations). Use 'graph' mode to explore entity connections.",
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string", "description": "Person name/email (dossier) or entity name (graph)"},
                "mode": {"type": "string", "enum": ["dossier", "graph", "bio"], "default": "dossier", "description": "dossier: full person profile. graph: multi-hop entity traversal. bio: current bio only (EAV-first, with provenance)."},
                "depth": {"type": "integer", "default": 2, "description": "Graph mode only: max hops (1-3)"},
                "entity_type": {"type": "string", "description": "Graph mode only: filter by type (person, org, topic)"}
            }, "required": ["query"]}
        ),
        Tool(
            name="ops_write",
            description="Write operations: store a learning, save/resume a checkpoint, get system status, or make a confirm-gated steward_bus canonical write (bus_observation / bus_people).",
            inputSchema={"type": "object", "properties": {
                "action": {"type": "string", "enum": ["store", "checkpoint", "resume", "status", "bus_observation", "bus_people"], "description": "store: save a learning. checkpoint: save task state. resume: load last checkpoint. status: system counts. bus_observation/bus_people: canonical write via steward_bus (confirm-gated; confirm=false returns a dry-run resolution preview)."},
                "content": {"type": "string", "description": "store: the learning text. checkpoint: ignored."},
                "title": {"type": "string", "description": "store: short title for the learning."},
                "task_name": {"type": "string", "description": "checkpoint/resume: task name."},
                "intent": {"type": "string", "description": "checkpoint: what you're trying to do."},
                "progress": {"type": "string", "description": "checkpoint: what's done so far."},
                "waiting_on": {"type": "string", "description": "checkpoint: what's blocking."},
                "next_steps": {"type": "string", "description": "checkpoint: what to do next."},
                "payload": {"type": "object", "description": "bus_*: the write payload (e.g. observation content/person_id, people fields)."},
                "natural_key": {"type": "object", "description": "bus_*: natural key for identity resolution (email/name/person_id...)."},
                "source_table": {"type": "string", "description": "bus_*: provenance source table (REQUIRED to apply)."},
                "source_id": {"type": "string", "description": "bus_*: provenance source row id (REQUIRED to apply)."},
                "source_quote": {"type": "string", "description": "bus_*: verbatim evidence span."},
                "submitted_by": {"type": "string", "description": "bus_*: writer tag (default ops_mcp:bus)."},
                "confirm": {"type": "boolean", "description": "bus_*: MUST be true to actually write; false/absent returns a preview only.", "default": False}
            }, "required": ["action"]}
        ),
        Tool(
            name="ops_query",
            description="Execute raw SQL against the ops DB. Returns up to 50 rows as formatted text. Use for counts, joins, aggregations, or any query not covered by other tools. Supports query.py shortcuts: 'schema', 'schema <table>', 'learnings <keyword>', 'map <keyword>', 'people <name>', 'search <term>', 'dossier <name>', 'cross <term>'.",
            inputSchema={"type": "object", "properties": {
                "sql": {"type": "string", "description": "SQL query or shortcut (e.g. 'schema people', 'SELECT COUNT(*) FROM people')"},
                "params": {"type": "array", "items": {"type": "string"}, "description": "Bind parameters for ? placeholders", "default": []},
            }, "required": ["sql"]}
        ),
        Tool(
            name="ops_tasks",
            description="Task manager for action_items. Commands: focus (daily top 7), stale (WAITING >3d), overdue, batch (by context tag), snooze/unsnooze, resolve, context (set tags), depend/undepend, checkin, autotag, spawn, unblock.",
            inputSchema={"type": "object", "properties": {
                "command": {"type": "string", "enum": ["focus", "stale", "overdue", "batch", "snooze", "unsnooze", "resolve", "context", "depend", "undepend", "checkin", "autotag", "spawn", "unblock"], "description": "Task manager command"},
                "item_id": {"type": "string", "description": "Action item ID (e.g. AI-20260405-077). Required for snooze/unsnooze/resolve/context/depend/undepend/checkin."},
                "value": {"type": "string", "description": "snooze: date (YYYY-MM-DD). resolve: note. context: comma-separated tags. depend: depends-on item ID. batch: context tag. checkin: note."},
                "reason": {"type": "string", "description": "snooze: reason for snoozing."},
            }, "required": ["command"]}
        ),
        Tool(
            name="ops_cross",
            description="Cross-table search: queries people + emails + action items + learnings + discord in one call. Returns unified ranked view. Good for broad 'what do we know about X' queries.",
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string", "description": "Search term"},
            }, "required": ["query"]}
        ),
        Tool(
            name="ops_email_search",
            description="Search emails across the ops DB and an optional gmail-to-sqlite mirror. FTS search on subject, sender, body. Returns recent matches with timestamps.",
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string", "description": "Search term (name, subject, keyword)"},
                "source": {"type": "string", "enum": ["both", "unified", "gmail"], "default": "both", "description": "Which DB to search. Default: both."},
                "limit": {"type": "integer", "default": 15},
            }, "required": ["query"]}
        ),
        Tool(
            name="ops_sync",
            description="Run daily_sync.py to pull fresh data from configured sources (Gmail, Discord, Beeper, Notion, Granola, Google Drive, Calendar). Default runs all sources. Pass sources list to limit.",
            inputSchema={"type": "object", "properties": {
                "sources": {"type": "array", "items": {"type": "string", "enum": ["gmail", "discord", "beeper", "notion", "granola", "drive", "calendar"]}, "description": "Limit to specific sources (e.g. ['gmail','discord']). Omit = all."},
            }}
        ),
        Tool(
            name="ops_brief_ops",
            description="brief.py subcommands. status (sync freshness), gather (show new items since last brief), report (latest brief summary), registry (beeper chat registry), new_brief (create briefing row + print id), drift_check (manual drift scan). For classify/apply use brief.py manually since those need stdin JSON piping.",
            inputSchema={"type": "object", "properties": {
                "command": {"type": "string", "enum": ["status", "gather", "report", "registry", "new_brief", "drift_check"], "description": "brief.py subcommand"},
            }, "required": ["command"]}
        ),
        Tool(
            name="ops_health",
            description="System health snapshot. Commands: summary (query.py health -- sync freshness + action items + ingest + drift), drift (drift_check.py stand-alone), digest (daily_digest.py --print -- yesterday's INGESTED/HEALTH/DRIFT snapshot).",
            inputSchema={"type": "object", "properties": {
                "command": {"type": "string", "enum": ["summary", "drift", "digest"], "description": "Health operation"},
            }, "required": ["command"]}
        ),
        Tool(
            name="ops_inbox",
            description="Multi-lane email inbox triage. Commands: status (lane breakdown + SLA breach counts), breaches (list SLA-breached threads, oldest first; optional lane filter), daily (JSON payload with all lanes + items for dashboard consumption), classify (re-classify all threads -- slow, use sparingly), classify_new (incremental on new inbound), classify_person (re-classify one person's threads), reconcile (auto-close false-positive open items where outbound > last_inbound), scan-asks (surface commitment candidates not yet tracked; numbered candidate table for triage). Lane names and SLA tiers come from your inbox_triage configuration.",
            inputSchema={"type": "object", "properties": {
                "command": {"type": "string", "enum": ["status", "breaches", "daily", "classify", "classify_new", "classify_person", "reconcile", "scan-asks"], "description": "Inbox triage command."},
                "lane": {"type": "string", "description": "breaches: filter to one lane (lane names are defined in your inbox_triage config)"},
                "person_id": {"type": "integer", "description": "classify_person: people.id whose threads to re-classify"},
                "dry_run": {"type": "boolean", "default": False, "description": "reconcile / classify: preview without writing"},
                "since_hours": {"type": "integer", "default": 24, "description": "scan-asks: lookback window in hours"},
                "sources": {"type": "string", "default": "commitments", "description": "scan-asks: comma-separated source list"},
                "format": {"type": "string", "enum": ["json", "markdown"], "default": "json", "description": "scan-asks: output shape"},
                "limit": {"type": "integer", "default": 200, "description": "scan-asks: max candidates"},
            }, "required": ["command"]}
        ),
        Tool(
            name="ops_fabric",
            description="Knowledge-graph internals, read-only. Commands: attributes (bi-temporal EAV history for a person/entity, optional attr filter), vocab (relation vocabulary: canonical mapping for one relation, or top relations by edge count), probes (run a batch of labeled read-only SQL probes in one call; each returns value vs target PASS/FAIL - use this instead of N ops_query round-trips).",
            inputSchema={"type": "object", "properties": {
                "command": {"type": "string", "enum": ["attributes", "vocab", "probes"], "description": "Fabric surface to read"},
                "query": {"type": "string", "description": "attributes: person name/email/id or entity id. vocab: relation name (omit to list top relations)."},
                "attr": {"type": "string", "description": "attributes: filter to one attribute (e.g. bio)"},
                "probes": {"type": "array", "items": {"type": "object", "properties": {
                    "label": {"type": "string"},
                    "sql": {"type": "string", "description": "SELECT/WITH only; first cell of first row is the value"},
                    "target": {"description": "optional expected value; PASS/FAIL computed when present"}
                }, "required": ["label", "sql"]}, "description": "probes: the batch to run"},
            }, "required": ["command"]}
        ),
        Tool(
            name="ops_faq",
            description="Canonical FAQ surface. Commands: search (FTS over canonical FAQs), gate (retrieval-gate tier for an inbound question: draft / draft_cite / escalate, with matched FAQ + score; risk topics such as payments and deadlines never tier above draft_cite), get (one FAQ by id, placeholders visible).",
            inputSchema={"type": "object", "properties": {
                "command": {"type": "string", "enum": ["search", "gate", "get"], "description": "FAQ operation"},
                "query": {"type": "string", "description": "search/gate: the question text. get: faq id."},
            }, "required": ["command", "query"]}
        ),
    ]


# ===========================================
# ops_find: fused search across core tables
# ===========================================

def _find(query: str, tables: str = "all", limit: int = 10) -> str:
    """RRF-fused (FTS + vector + PPR graph) search via rrf_search.
    Falls back to the legacy pure-FTS path if the primitive is unavailable."""
    try:
        import rrf_search
    except Exception:
        return _find_fts_legacy(query, tables, limit)
    corpus_map = {  # section name -> rrf corpus (order = output order)
        "people": "people", "learnings": "learnings", "actions": "actions",
        "emails": "emails", "docs": "docs", "discord": "discord",
        "entities": "entities", "observations": "observations",
        "episodes": "episodes", "faqs": "faqs",
    }
    default_sections = ["people", "learnings", "actions", "emails", "docs",
                        "discord", "entities", "observations", "episodes", "faqs"]
    selected = (default_sections if tables == "all"
                else [t.strip() for t in tables.split(",") if t.strip() in corpus_map])
    try:
        per = rrf_search.search_all(query, k=limit,
                                    corpora=tuple(corpus_map[s] for s in selected))
    except Exception:
        return _find_fts_legacy(query, tables, limit)
    # sections stay grouped, but the section whose best hit scores highest
    # leads -- a thematic/doc query should not open with 10 weak people rows.
    def _top(sec):
        rows = per.get(corpus_map[sec]) or []
        return rows[0]["rrf_score"] if rows else -1.0
    lines = []
    for section in sorted(selected, key=_top, reverse=True):
        rows = per.get(corpus_map[section]) or []
        cap = min(limit, 5) if section in ("discord", "observations", "episodes") else limit
        lines.extend(rrf_search.format_row(r) for r in rows[:cap])
    if not lines:
        return f"No results for '{query}'."
    return f"=== Find: \"{query}\" ({len(lines)} results, RRF-fused) ===\n" + "\n".join(lines)


def _find_fts_legacy(query: str, tables: str = "all", limit: int = 10) -> str:
    db = get_plain_db()
    fq = fts_query(query)
    lines = []
    selected = set(t.strip() for t in tables.split(",")) if tables != "all" else {"people", "learnings", "actions", "emails", "docs", "discord"}

    if "people" in selected:
        try:
            rows = db.execute("""
                SELECT p.name, p.email, p.headline AS affiliation, p.location
                FROM people_fts pf JOIN people p ON p.rowid = pf.rowid
                WHERE people_fts MATCH ? ORDER BY pf.rank LIMIT ?
            """, (fq, limit)).fetchall()
            for r in rows:
                lines.append(f"[person] {r['name']} | {r['email'] or ''} | {r['affiliation'] or ''} | {r['location'] or ''}")
        except Exception:
            pass

    if "learnings" in selected:
        try:
            for r in db.execute("""
                SELECT l.id, l.title, l.description
                FROM learnings_fts lf JOIN learnings l ON l.rowid = lf.rowid
                WHERE learnings_fts MATCH ? AND l.status = 'active'
                ORDER BY lf.rank LIMIT ?
            """, (fq, limit)).fetchall():
                lines.append(f"[learning #{r['id']}] {r['title'] or '(no title)'}: {(r['description'] or '')[:150]}")
        except Exception:
            pass

    if "actions" in selected:
        try:
            for r in db.execute("""
                SELECT ai.status, ai.priority, ai.description, ai.waiting_on
                FROM action_items_fts af JOIN action_items ai ON ai.rowid = af.rowid
                WHERE action_items_fts MATCH ? ORDER BY af.rank LIMIT ?
            """, (fq, min(limit, 10))).fetchall():
                w = f" [WAITING: {r['waiting_on']}]" if r["waiting_on"] else ""
                lines.append(f"[action:{r['status']}:{r['priority']}] {r['description'][:120]}{w}")
        except Exception:
            pass

    if "emails" in selected:
        try:
            for r in db.execute("""
                SELECT e.sender_name, e.sender_email, e.subject, e.timestamp, e.is_outgoing
                FROM emails_fts ef JOIN emails e ON e.rowid = ef.rowid
                WHERE emails_fts MATCH ? ORDER BY ef.rank LIMIT ?
            """, (fq, limit)).fetchall():
                d = "OUT" if r["is_outgoing"] else "IN"
                lines.append(f"[email:{d}] {r['timestamp'][:16]} | {r['sender_name'] or r['sender_email'] or '?'} | {(r['subject'] or '')[:80]}")
        except Exception:
            pass

    if "docs" in selected:
        try:
            for r in db.execute("""
                SELECT rd.slug, rd.title, rd.category
                FROM reference_docs_fts rf JOIN reference_docs rd ON rd.id = rf.rowid
                WHERE reference_docs_fts MATCH ? AND rd.doc_type IS NOT 'archive'
                ORDER BY rf.rank LIMIT ?
            """, (fq, limit)).fetchall():
                lines.append(f"[doc:{r['category']}] {r['title'] or r['slug']}")
        except Exception:
            pass

    if "discord" in selected:
        try:
            # discord_fts may not exist in a fresh install; degrade to LIKE.
            if _table_exists(db, "discord_fts"):
                rows = db.execute("""
                    SELECT dm.content, dm.timestamp, du.username, dc.name as channel
                    FROM discord_fts df
                    JOIN discord_messages dm ON dm.rowid = df.rowid
                    LEFT JOIN discord_users du ON dm.author_id = du.id
                    LEFT JOIN discord_channels dc ON dm.channel_id = dc.id
                    WHERE discord_fts MATCH ? ORDER BY df.rank LIMIT ?
                """, (fq, min(limit, 5))).fetchall()
            else:
                rows = db.execute("""
                    SELECT dm.content, dm.timestamp, du.username, dc.name as channel
                    FROM discord_messages dm
                    LEFT JOIN discord_users du ON dm.author_id = du.id
                    LEFT JOIN discord_channels dc ON dm.channel_id = dc.id
                    WHERE dm.content LIKE ? ORDER BY dm.timestamp DESC LIMIT ?
                """, (f"%{query}%", min(limit, 5))).fetchall()
            for r in rows:
                content = (r["content"] or "")[:100].replace("\n", " ")
                lines.append(f"[discord] #{r['channel'] or '?'} | {r['username'] or '?'} | {r['timestamp'][:16]} | {content}")
        except Exception:
            pass

    db.close()
    if not lines:
        return f"No results for '{query}'."
    return f"=== Find: \"{query}\" ({len(lines)} results) ===\n" + "\n".join(lines)


# ===========================================
# ops_deep: dossier or graph traversal
# ===========================================

def _deep_dossier(query: str) -> str:
    from person_dossier import dossier
    return dossier(query)


def _deep_bio(query: str) -> str:
    """Current bio for a person (EAV-first via v_person_bios) + provenance chain."""
    db = get_plain_db()
    try:
        if not _table_exists(db, "v_person_bios"):
            return "v_person_bios view not present in this DB (bio mode unavailable)."
        like = f"%{query}%"
        row = db.execute(
            "SELECT person_id, name, bio, bio_source, bio_updated_at FROM v_person_bios "
            "WHERE name LIKE ? OR email LIKE ? OR CAST(person_id AS TEXT) = ? LIMIT 1",
            (like, like, query.strip())).fetchone()
        if not row:
            return f"No bio found for '{query}' (v_person_bios)."
        lines = [f"# Bio: {row[1]} (person {row[0]})", "", row[2].strip(), "",
                 f"source: {row[3]}  |  as of: {row[4]}"]
        if _table_exists(db, "attributes"):
            hist = db.execute(
                "SELECT source_table, status, valid_from, COALESCE(valid_until,'') FROM attributes "
                "WHERE entity_id = 'person-' || ? AND attr='bio' ORDER BY valid_from DESC LIMIT 6",
                (row[0],)).fetchall()
            if hist:
                lines.append("")
                lines.append("history (attributes, bi-temporal):")
                for h in hist:
                    lines.append(f"  - [{h[1]}] from {h[0]}: {h[2]}" + (f" -> {h[3]}" if h[3] else ""))
        return "\n".join(lines)
    finally:
        db.close()


def _deep_graph(entity_name: str, depth: int = 2, entity_type: str = None) -> str:
    db = get_plain_db()
    depth = min(depth, 3)
    like = f"%{entity_name}%"

    if entity_type:
        start = db.execute(
            "SELECT id, name, type FROM entities WHERE name LIKE ? AND type = ? AND status='active' LIMIT 1",
            (like, entity_type)
        ).fetchone()
    else:
        start = db.execute(
            "SELECT id, name, type FROM entities WHERE name LIKE ? AND status='active' LIMIT 1",
            (like,)
        ).fetchone()

    if not start:
        db.close()
        return f"No entity matching '{entity_name}'" + (f" (type={entity_type})" if entity_type else "")

    results = db.execute("""
        WITH RECURSIVE hops(entity_id, entity_name, entity_type, hop, path, relation) AS (
            SELECT id, name, type, 0, id, ''
            FROM entities WHERE id = ?
            UNION ALL
            SELECT e2.id, e2.name, e2.type, h.hop + 1,
                   h.path || ' -> ' || e2.id, ed.relation
            FROM hops h
            JOIN edges ed ON ed.source_id = h.entity_id
            JOIN entities e2 ON ed.target_id = e2.id
            WHERE h.hop < ? AND h.path NOT LIKE '%' || e2.id || '%'
            AND e2.status = 'active'
            UNION ALL
            SELECT e2.id, e2.name, e2.type, h.hop + 1,
                   h.path || ' -> ' || e2.id, ed.relation
            FROM hops h
            JOIN edges ed ON ed.target_id = h.entity_id
            JOIN entities e2 ON ed.source_id = e2.id
            WHERE h.hop < ? AND h.path NOT LIKE '%' || e2.id || '%'
            AND e2.status = 'active'
        )
        SELECT DISTINCT entity_name, entity_type, hop, relation
        FROM hops WHERE hop > 0
        ORDER BY hop, entity_type, entity_name
    """, (start['id'], depth, depth)).fetchall()
    db.close()

    lines = [f"Graph: {start['name']} ({start['type']}), depth={depth}"]
    if results:
        current_hop = 0
        for r in results:
            if r['hop'] != current_hop:
                current_hop = r['hop']
                lines.append(f"\n--- Hop {current_hop} ---")
            lines.append(f"  [{r['relation']}] {r['entity_name']} ({r['entity_type']})")
        lines.append(f"\n({len(results)} entities within {depth} hops)")
    else:
        lines.append("No connections found.")
    return "\n".join(lines)


# ===========================================
# ops_write: store, checkpoint, resume, status
# ===========================================

def _count(db, sql: str):
    try:
        return db.execute(sql).fetchone()[0]
    except sqlite3.Error:
        return None


def _write(arguments: dict) -> str:
    action = arguments["action"]

    if action == "store":
        import hashlib
        import steward_bus as _sb
        db = get_plain_db()
        content = arguments.get("content", "")
        title = arguments.get("title") or content[:80]
        h = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
        existing = db.execute("SELECT id FROM learnings WHERE content_hash = ?", (h,)).fetchone()
        if existing:
            db.close()
            return f"Duplicate of #{existing[0]}, skipped."
        # Write through the bus so the learning carries actor='steward:bus'
        # (was a raw INSERT). _h_learnings is idempotent on content_hash + honors status.
        res = _sb.write(
            db, target_table="learnings",
            payload={"title": title, "description": content, "content_hash": h,
                     "status": "active", "memory_type": "learning", "priority": "P2",
                     "source": "ops_mcp:store"},
            submitted_by="ops_mcp:store", source_table="mcp",
            source_id=str(arguments.get("session_id") or ""))
        rid = res.get("canonical_id")
        db.close()
        return f"Stored #{rid}: '{title}'"

    elif action in ("bus_observation", "bus_people"):
        # Confirm-gated steward_bus front door. Without confirm=true nothing is
        # staged or written -- only a resolution preview.
        import json as _json
        import steward_bus as _sb
        target = "observation" if action == "bus_observation" else "people"
        payload = arguments.get("payload") or {}
        if isinstance(payload, str):
            payload = _json.loads(payload)
        nk = arguments.get("natural_key")
        if isinstance(nk, str) and nk:
            nk = _json.loads(nk)
        if not arguments.get("confirm"):
            db = get_plain_db()
            try:
                pid, trace = _sb._resolve_identity(db, payload, nk or {}, False)
            finally:
                db.close()
            return ("CONFIRM GATE -- nothing written. Re-call with confirm=true to apply.\n"
                    f"target={target}\nresolved_person_id={pid}\ntrace={trace}\n"
                    f"payload={_json.dumps(payload, default=str)[:600]}")
        if not (arguments.get("source_table") and arguments.get("source_id")):
            return "REFUSED: bus writes require source_table + source_id provenance."
        db = get_plain_db()
        try:
            res = _sb.write(db, target_table=target, payload=payload, natural_key=nk,
                            submitted_by=arguments.get("submitted_by") or "ops_mcp:bus",
                            source_table=arguments.get("source_table"),
                            source_id=arguments.get("source_id"),
                            source_quote=arguments.get("source_quote"))
        finally:
            db.close()
        return _json.dumps(res, default=str)

    elif action == "checkpoint":
        from audit_actor import actor_scope
        db = get_plain_db()
        # Checkpoints are session scratch (not a graph entity), so wrap the raw
        # write in actor_scope rather than a full bus migration -- the write
        # still carries actor='ops_mcp:checkpoint' for the audit context.
        with actor_scope(db, "ops_mcp:checkpoint"):
            db.execute("""
                INSERT INTO checkpoints (task_name, intent, progress, waiting_on, next_steps)
                VALUES (?, ?, ?, ?, ?)
            """, (arguments.get("task_name", "unnamed"), arguments.get("intent"),
                  arguments.get("progress"), arguments.get("waiting_on"), arguments.get("next_steps")))
            db.commit()
            cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.close()
        return f"Checkpoint #{cid} saved."

    elif action == "resume":
        from audit_actor import actor_scope
        db = get_plain_db()
        task = arguments.get("task_name")
        if task:
            row = db.execute("SELECT * FROM checkpoints WHERE task_name LIKE ? AND status='active' ORDER BY created_at DESC LIMIT 1", (f"%{task}%",)).fetchone()
        else:
            row = db.execute("SELECT * FROM checkpoints WHERE status='active' ORDER BY created_at DESC LIMIT 1").fetchone()
        if row:
            with actor_scope(db, "ops_mcp:checkpoint"):
                db.execute("UPDATE checkpoints SET resumed_at = datetime('now') WHERE id = ?", (row["id"],))
                db.commit()
            lines = [f"Checkpoint #{row['id']}: {row['task_name']}"]
            for f in ["intent", "progress", "waiting_on", "next_steps"]:
                if row[f]: lines.append(f"  {f}: {row[f]}")
            db.close()
            return "\n".join(lines)
        db.close()
        return "No active checkpoint found."

    elif action == "status":
        db = get_plain_db()
        stats = {
            "learnings": _count(db, "SELECT COUNT(*) FROM learnings WHERE status='active'"),
            "people": _count(db, "SELECT COUNT(*) FROM people WHERE is_real_person=1"),
            "action_items_open": _count(db, "SELECT COUNT(*) FROM action_items WHERE status='OPEN'"),
            "action_items_waiting": _count(db, "SELECT COUNT(*) FROM action_items WHERE status='WAITING'"),
            "entities": _count(db, "SELECT COUNT(*) FROM entities WHERE status='active'"),
            "edges": _count(db, "SELECT COUNT(*) FROM edges"),
            "emails": _count(db, "SELECT COUNT(*) FROM emails"),
            "db_size_mb": round(os.path.getsize(DB) / (1024 * 1024), 1),
        }
        db.close()
        return json.dumps(stats, indent=2)

    return f"Unknown action: {action}"


# ===========================================
# ops_query: raw SQL execution
# ===========================================

DANGEROUS_SQL = re.compile(r'\b(DROP|DELETE|ALTER|TRUNCATE)\b', re.IGNORECASE)

def _query(sql: str, params: list = None) -> str:
    sql = sql.strip()
    params = params or []

    # Shortcuts (delegate to query.py)
    lower = sql.lower()
    if lower == "schema":
        return _run_script("query.py", "schema")
    if lower.startswith("schema "):
        return _run_script("query.py", "schema", sql.split(None, 1)[1])
    if lower.startswith("learnings "):
        return _run_script("query.py", "learnings", sql.split(None, 1)[1])
    if lower.startswith("map "):
        return _run_script("query.py", "map", sql.split(None, 1)[1])
    if lower.startswith("people "):
        return _run_script("query.py", "people", sql.split(None, 1)[1])
    if lower.startswith("search "):
        return _run_script("query.py", "search", sql.split(None, 1)[1])
    if lower.startswith("dossier "):
        return _run_script("query.py", "dossier", sql.split(None, 1)[1])
    if lower.startswith("cross "):
        return _run_script("query.py", "cross", sql.split(None, 1)[1])

    # Safety check for destructive SQL
    if DANGEROUS_SQL.search(sql) and "WHERE" not in sql.upper():
        return f"BLOCKED: destructive SQL without WHERE clause: {sql[:100]}"

    db = get_plain_db()
    try:
        cursor = db.execute(sql, params)
        if cursor.description:
            cols = [d[0] for d in cursor.description]
            rows = cursor.fetchmany(50)
            if not rows:
                return "(0 rows)"
            lines = [" | ".join(cols)]
            lines.append("-" * len(lines[0]))
            for row in rows:
                lines.append(" | ".join(str(v) if v is not None else "" for v in row))
            total = cursor.fetchone()
            if total:
                lines.append(f"... (showing 50 of 50+ rows)")
            else:
                lines.append(f"({len(rows)} rows)")
            return "\n".join(lines)
        else:
            db.commit()
            return f"OK ({cursor.rowcount} rows affected)"
    finally:
        db.close()


# ===========================================
# ops_tasks: task manager operations
# ===========================================

def _tasks(arguments: dict) -> str:
    from task_manager import (
        get_focus_items, show_stale, show_overdue, batch_by_context,
        snooze_item, unsnooze_item, resolve_item, set_context,
        add_dependency, remove_dependency, checkin_item,
        auto_tag_contexts, spawn_recurring, check_unblocked,
    )

    cmd = arguments["command"]
    item_id = arguments.get("item_id", "")
    value = arguments.get("value", "")
    reason = arguments.get("reason", "")

    if cmd == "focus":
        return get_focus_items()
    elif cmd == "stale":
        return show_stale()
    elif cmd == "overdue":
        return show_overdue()
    elif cmd == "batch":
        return batch_by_context(value or "@email")
    elif cmd == "snooze":
        if not item_id or not value:
            return "Need item_id and value (date YYYY-MM-DD)"
        return snooze_item(item_id, value, reason)
    elif cmd == "unsnooze":
        if not item_id:
            return "Need item_id"
        return unsnooze_item(item_id)
    elif cmd == "resolve":
        if not item_id:
            return "Need item_id"
        return resolve_item(item_id, value or "Resolved via MCP")
    elif cmd == "context":
        if not item_id or not value:
            return "Need item_id and value (comma-separated tags)"
        return set_context(item_id, value)
    elif cmd == "depend":
        if not item_id or not value:
            return "Need item_id and value (depends-on item ID)"
        return add_dependency(item_id, value)
    elif cmd == "undepend":
        if not item_id or not value:
            return "Need item_id and value (depends-on item ID to remove)"
        return remove_dependency(item_id, value)
    elif cmd == "checkin":
        if not item_id:
            return "Need item_id"
        return checkin_item(item_id, value or "")
    elif cmd == "autotag":
        return auto_tag_contexts()
    elif cmd == "spawn":
        spawned = spawn_recurring()
        return f"Spawned: {', '.join(spawned)}" if spawned else "No recurring items to spawn"
    elif cmd == "unblock":
        unblocked = check_unblocked()
        return f"Unblocked: {', '.join(unblocked)}" if unblocked else "No items to unblock"
    return f"Unknown task command: {cmd}"


# ===========================================
# ops_cross: cross-table search via subprocess
# ===========================================

def _cross(query: str) -> str:
    return _run_script("cross_search.py", query)


# ===========================================
# ops_email_search: search emails across DBs
# ===========================================

def _email_search(query: str, source: str = "both", limit: int = 15) -> str:
    fq = fts_query(query)
    lines = []

    if source in ("both", "unified"):
        db = get_plain_db()
        try:
            rows = db.execute("""
                SELECT e.sender_name, e.sender_email, e.subject, e.timestamp,
                       e.is_outgoing, e.recipients_json
                FROM emails_fts ef JOIN emails e ON e.rowid = ef.rowid
                WHERE emails_fts MATCH ? ORDER BY e.timestamp DESC LIMIT ?
            """, (fq, limit)).fetchall()
            for r in rows:
                d = "OUT" if r["is_outgoing"] else "IN"
                sender = r["sender_name"] or r["sender_email"] or "?"
                recip = ""
                if r["recipients_json"]:
                    try:
                        recips = json.loads(r["recipients_json"])
                        if isinstance(recips, list) and recips:
                            recip = recips[0] if isinstance(recips[0], str) else recips[0].get("email", "")
                    except (json.JSONDecodeError, AttributeError):
                        recip = str(r["recipients_json"])[:40]
                lines.append(f"[unified:{d}] {(r['timestamp'] or '')[:16]} | {sender} -> {recip} | {(r['subject'] or '')[:80]}")
        except Exception as e:
            lines.append(f"[unified error: {e}]")
        finally:
            db.close()

    if source in ("both", "gmail") and GMAIL_DB and os.path.exists(GMAIL_DB):
        try:
            gdb = sqlite3.connect(GMAIL_DB, timeout=10)
            gdb.row_factory = sqlite3.Row
            # gmail-to-sqlite uses a messages table with subject, from_, to_, date
            rows = gdb.execute("""
                SELECT subject, "from", "to", date, snippet
                FROM messages
                WHERE subject LIKE ? OR "from" LIKE ? OR snippet LIKE ?
                ORDER BY date DESC LIMIT ?
            """, (f"%{query}%", f"%{query}%", f"%{query}%", limit)).fetchall()
            for r in rows:
                lines.append(f"[gmail] {(r['date'] or '')[:16]} | {r['from'] or '?'} -> {r['to'] or '?'} | {(r['subject'] or '')[:80]}")
            gdb.close()
        except Exception as e:
            lines.append(f"[gmail error: {e}]")

    if not lines:
        return f"No emails matching '{query}'"
    return f"=== Email search: \"{query}\" ({len(lines)} results) ===\n" + "\n".join(lines)


# ===========================================
# ops_sync / brief_ops / health
# ===========================================

def _sync(arguments: dict) -> str:
    sources = arguments.get("sources") or []
    args = []
    for s in sources:
        if s == "gmail":
            args.append("--gmail-only")
        elif s == "discord":
            args.append("--discord-only")
        elif s == "beeper":
            args.append("--beeper-only")
        elif s == "notion":
            args.append("--notion-only")
        elif s == "granola":
            args.append("--granola-only")
        elif s == "drive":
            args.append("--drive-only")
        elif s == "calendar":
            args.append("--calendar-only")
    return _run_script("daily_sync.py", *args)


def _brief_ops(arguments: dict) -> str:
    cmd = arguments.get("command", "status")
    mapping = {
        "status": ["status"],
        "gather": ["gather"],
        "report": ["report"],
        "registry": ["registry"],
        "new_brief": ["new-brief"],
        "drift_check": ["drift-check"],
    }
    if cmd not in mapping:
        return f"Unknown command: {cmd}"
    return _run_script("brief.py", *mapping[cmd])


def _health(arguments: dict) -> str:
    cmd = arguments.get("command", "summary")
    if cmd == "summary":
        return _run_script("query.py", "health")
    if cmd == "drift":
        return _run_script("drift_check.py")
    if cmd == "digest":
        return _run_script("daily_digest.py", "--print")
    return f"Unknown command: {cmd}"


def _inbox(arguments: dict) -> str:
    cmd = arguments.get("command", "status")
    if cmd == "status":
        return _run_script("inbox_triage.py", "status")
    if cmd == "breaches":
        args = ["breaches"]
        if arguments.get("lane"):
            args += ["--lane", arguments["lane"]]
        return _run_script("inbox_triage.py", *args)
    if cmd == "daily":
        return _run_script("inbox_triage.py", "daily")
    if cmd == "classify":
        args = ["classify", "--all"]
        if arguments.get("dry_run"):
            args.append("--dry-run")
        return _run_script("inbox_triage.py", *args)
    if cmd == "classify_new":
        return _run_script("inbox_triage.py", "classify-new")
    if cmd == "classify_person":
        pid = arguments.get("person_id")
        if pid is None:
            return "ERROR: classify_person requires person_id"
        args = ["classify", "--person", str(pid)]
        if arguments.get("dry_run"):
            args.append("--dry-run")
        return _run_script("inbox_triage.py", *args)
    if cmd == "reconcile":
        args = ["reconcile-replies"]
        if arguments.get("dry_run"):
            args.append("--dry-run")
        return _run_script("inbox_triage.py", *args)
    if cmd == "scan-asks" or cmd == "scan_asks":
        # Surfaces commitment candidates not yet tracked as action items.
        args = ["scan-asks", "--since", str(arguments.get("since_hours", 24))]
        fmt = arguments.get("format", "json")
        args += ["--format", fmt]
        if arguments.get("sources"):
            args += ["--sources", arguments["sources"]]
        if arguments.get("limit"):
            args += ["--limit", str(arguments["limit"])]
        return _run_script("inbox_triage.py", *args)
    return f"Unknown command: {cmd}"


def _fabric_resolve_entity(db, query: str):
    """Person name/email/numeric id/entity id -> (entity_id, display_name) or (None, None)."""
    q = (query or "").strip()
    if not q:
        return None, None
    if re.match(r"^(person|org|topic|channel)-", q):
        row = db.execute("SELECT id, name FROM entities WHERE id=?", (q,)).fetchone()
        return (row[0], row[1]) if row else (q, q)
    if q.isdigit():
        row = db.execute("SELECT id, name FROM people WHERE id=?", (int(q),)).fetchone()
        if row:
            return f"person-{row[0]}", row[1]
    like = f"%{q}%"
    row = db.execute(
        "SELECT p.id, p.name FROM people p LEFT JOIN person_emails pe ON pe.person_id=p.id "
        "WHERE p.merged_into IS NULL AND (p.name LIKE ? OR p.email LIKE ? OR pe.email LIKE ?) "
        "ORDER BY p.interaction_count DESC LIMIT 1", (like, like, like)).fetchone()
    if row:
        return f"person-{row[0]}", row[1]
    row = db.execute(
        "SELECT id, name FROM entities WHERE name LIKE ? AND status='active' LIMIT 1", (like,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


_PROBE_SAFE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


def _faq(arguments: dict) -> str:
    cmd = arguments.get("command")
    q = (arguments.get("query") or "").strip()
    db = get_plain_db()
    try:
        if cmd == "get":
            row = db.execute(
                "SELECT id, question_canonical, answer_canonical, topic FROM faqs WHERE id=?",
                (int(q),)).fetchone()
            if not row:
                return f"No FAQ id {q}"
            return (f"# FAQ {row[0]} [{row[3] or 'general'}]\nQ: {row[1]}\nA: {row[2]}\n"
                    f"(placeholders like {{{{deadline}}}} resolve at render time)")
        if cmd == "search":
            rows = db.execute(
                "SELECT f.id, f.question_canonical, substr(f.answer_canonical,1,200), f.topic "
                "FROM faqs_fts ft JOIN faqs f ON f.id=ft.rowid WHERE faqs_fts MATCH ? "
                "ORDER BY rank LIMIT 8", (q,)).fetchall()
            if not rows:
                return f"No FAQ match for '{q}'"
            return "\n".join(f"[{r[0]}] ({r[3] or 'general'}) {r[1]}\n    {r[2]}" for r in rows)
        if cmd == "gate":
            try:
                from faq_gate import gate
            except ImportError:
                return "NOT READY: faq_gate.py not importable (is comms/ on sys.path?); use command=search meanwhile"
            res = gate(q)
            tier, faq_id, score = res.as_tuple()
            ans = ""
            if faq_id is not None:
                cap = " [risk-capped]" if res.risk_capped else ""
                ans = (f"\nMatched FAQ {faq_id}: {res.matched_question}{cap}"
                       f"\nCanonical answer: {res.answer}")
            return (f"tier={tier} score={score:.2f}{ans}\n"
                    f"(draft = safe to auto-draft; draft_cite = draft with FAQ citation; "
                    f"escalate = needs the operator. NO-NEW-PROMISES: only the canonical answer may be restated.)")
        return f"Unknown ops_faq command: {cmd}"
    finally:
        db.close()


def _fabric(arguments: dict) -> str:
    cmd = arguments.get("command")
    db = get_plain_db()
    try:
        if cmd == "attributes":
            if not _table_exists(db, "attributes"):
                return "attributes table not present in this DB."
            eid, name = _fabric_resolve_entity(db, arguments.get("query", ""))
            if not eid:
                return f"No entity resolved for '{arguments.get('query')}'"
            where, params = "entity_id=?", [eid]
            if arguments.get("attr"):
                where += " AND attr=?"
                params.append(arguments["attr"])
            rows = db.execute(
                f"SELECT attr, status, substr(COALESCE(value,''),1,300), value_type, source_table, "
                f"COALESCE(confidence,''), valid_from, COALESCE(valid_until,'') "
                f"FROM attributes WHERE {where} ORDER BY attr, valid_from DESC LIMIT 80", params).fetchall()
            if not rows:
                return f"No attributes for {name} ({eid})."
            lines = [f"# Attributes: {name} ({eid})  [{len(rows)} rows, bi-temporal]"]
            cur_attr = None
            for a, st, val, vt, src, conf, vf, vu in rows:
                if a != cur_attr:
                    cur_attr = a
                    lines.append(f"\n## {a}")
                span = f"{vf}" + (f" -> {vu}" if vu else " -> now")
                lines.append(f"  [{st}] ({span}, from {src}" + (f", conf {conf}" if conf else "") + f"): {val}")
            return "\n".join(lines)

        if cmd == "vocab":
            if not _table_exists(db, "relation_vocab"):
                return "relation_vocab table not present in this DB."
            rel = (arguments.get("query") or "").strip()
            if rel:
                row = db.execute(
                    "SELECT relation, canonical, is_canonical, COALESCE(deprecated_since,''), COALESCE(notes,'') "
                    "FROM relation_vocab WHERE relation=? OR canonical=?", (rel, rel)).fetchall()
                if not row:
                    return f"'{rel}' not in relation_vocab."
                lines = [f"relation | canonical | is_canonical | deprecated | notes"]
                lines += [" | ".join(str(c) for c in r) for r in row[:40]]
                n = db.execute("SELECT COUNT(*) FROM edges WHERE relation=?", (rel,)).fetchone()[0]
                lines.append(f"live edges with relation='{rel}': {n}")
                return "\n".join(lines)
            rows = db.execute(
                "SELECT e.relation, COUNT(*) n, COALESCE(v.canonical, '(UNMAPPED)') "
                "FROM edges e LEFT JOIN relation_vocab v ON v.relation=e.relation "
                "GROUP BY e.relation ORDER BY n DESC LIMIT 40").fetchall()
            lines = ["relation | edges | canonical (top 40)"]
            lines += [f"{r[0]} | {r[1]} | {r[2]}" for r in rows]
            unmapped = db.execute(
                "SELECT COUNT(DISTINCT e.relation) FROM edges e "
                "LEFT JOIN relation_vocab v ON v.relation=e.relation WHERE v.relation IS NULL").fetchone()[0]
            lines.append(f"distinct relations not in vocab: {unmapped}")
            return "\n".join(lines)

        if cmd == "probes":
            probes = arguments.get("probes") or []
            if not probes:
                return "ERROR: probes command requires a non-empty probes array"
            if len(probes) > 40:
                return "ERROR: max 40 probes per call"
            lines = ["label | value | target | verdict"]
            fails = 0
            for p in probes:
                label = str(p.get("label", "?"))[:60]
                sql = p.get("sql", "")
                if not _PROBE_SAFE.match(sql):
                    lines.append(f"{label} | REJECTED (SELECT/WITH only) | |")
                    fails += 1
                    continue
                try:
                    row = db.execute(sql).fetchone()
                    val = row[0] if row else None
                except sqlite3.Error as e:
                    lines.append(f"{label} | ERR {str(e)[:80]} | | FAIL")
                    fails += 1
                    continue
                if "target" in p and p["target"] is not None:
                    ok = str(val) == str(p["target"]) or val == p["target"]
                    lines.append(f"{label} | {val} | {p['target']} | {'PASS' if ok else 'FAIL'}")
                    fails += 0 if ok else 1
                else:
                    lines.append(f"{label} | {val} | | ")
            lines.append(f"\n{len(probes)} probes, {fails} FAIL/ERR")
            return "\n".join(lines)

        return f"Unknown command: {cmd}"
    finally:
        db.close()


# ===========================================
# Tool dispatcher
# ===========================================

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "ops_find":
            text = _find(arguments["query"], arguments.get("tables", "all"), arguments.get("limit", 10))
            return CallToolResult(content=[TextContent(type="text", text=text)], _meta=LARGE_RESULT_META)

        elif name == "ops_deep":
            mode = arguments.get("mode", "dossier")
            if mode == "graph":
                text = _deep_graph(arguments["query"], arguments.get("depth", 2), arguments.get("entity_type"))
            elif mode == "bio":
                text = _deep_bio(arguments["query"])
            else:
                text = _deep_dossier(arguments["query"])
            return CallToolResult(content=[TextContent(type="text", text=text)], _meta=LARGE_RESULT_META)

        elif name == "ops_write":
            text = _write(arguments)
            return [TextContent(type="text", text=text)]

        elif name == "ops_query":
            text = _query(arguments["sql"], arguments.get("params"))
            return CallToolResult(content=[TextContent(type="text", text=text)], _meta=LARGE_RESULT_META)

        elif name == "ops_tasks":
            text = _tasks(arguments)
            return [TextContent(type="text", text=text)]

        elif name == "ops_cross":
            text = _cross(arguments["query"])
            return CallToolResult(content=[TextContent(type="text", text=text)], _meta=LARGE_RESULT_META)

        elif name == "ops_email_search":
            text = _email_search(arguments["query"], arguments.get("source", "both"), arguments.get("limit", 15))
            return CallToolResult(content=[TextContent(type="text", text=text)], _meta=LARGE_RESULT_META)

        elif name == "ops_sync":
            text = _sync(arguments)
            return CallToolResult(content=[TextContent(type="text", text=text)], _meta=LARGE_RESULT_META)

        elif name == "ops_brief_ops":
            text = _brief_ops(arguments)
            return CallToolResult(content=[TextContent(type="text", text=text)], _meta=LARGE_RESULT_META)

        elif name == "ops_health":
            text = _health(arguments)
            return CallToolResult(content=[TextContent(type="text", text=text)], _meta=LARGE_RESULT_META)

        elif name == "ops_inbox":
            text = _inbox(arguments)
            return CallToolResult(content=[TextContent(type="text", text=text)], _meta=LARGE_RESULT_META)

        elif name == "ops_fabric":
            text = _fabric(arguments)
            return CallToolResult(content=[TextContent(type="text", text=text)], _meta=LARGE_RESULT_META)

        elif name == "ops_faq":
            text = _faq(arguments)
            return CallToolResult(content=[TextContent(type="text", text=text)], _meta=LARGE_RESULT_META)

        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def schema_smoke() -> int:
    """Startup schema smoke -- assert every table/view this server queries
    exists in the ops DB (catches migrations renaming/dropping under the MCP).
    Run: python mcp_server.py --smoke  (exit 0 = all required present).
    Optional tables (feature-detected at runtime) only warn."""
    import sqlite3 as _sq
    required = [
        "action_items", "action_items_fts", "checkpoints",
        "discord_channels", "discord_messages", "discord_users", "edges",
        "emails", "emails_fts", "entities", "faqs", "faqs_fts", "learnings",
        "learnings_fts", "people", "people_fts", "person_emails",
        "reference_docs",
    ]
    optional = [
        "attributes", "v_person_bios", "relation_vocab", "observations",
        "reference_docs_fts", "discord_fts",
    ]
    conn = _sq.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view','virtual table') "
            "UNION SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%'")}
        missing = [t for t in required if t not in have]
        missing_opt = [t for t in optional if t not in have]
    finally:
        conn.close()
    if missing_opt:
        print("schema smoke note -- optional (degraded, not fatal):", ", ".join(missing_opt))
    if missing:
        print("SCHEMA SMOKE FAIL -- missing:", ", ".join(missing))
        return 1
    print(f"SCHEMA SMOKE OK -- {len(required)}/{len(required)} required tables/views present")
    return 0


if __name__ == "__main__":
    if "--smoke" in sys.argv:
        raise SystemExit(schema_smoke())
    import asyncio
    asyncio.run(main())
