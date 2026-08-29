#!/usr/bin/env python3
"""Cross-table search: query people + emails + action items + learnings + chat in one call.

Runs FTS5 MATCH across all searchable tables, deduplicates people results,
and returns a unified ranked view.

Usage:
    python cross_search.py "genomics mentor"
    python cross_search.py "cloud credits" --verbose
    python cross_search.py "quarterly report deadline"
"""
import io
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

# Boolean role-flag columns on the people table rendered as [tag] labels in
# people results (legacy FTS path). CUSTOMIZE to the flag columns your schema
# tracks (keep in step with rrf_search.PEOPLE_ROLE_FLAGS); every column listed
# here must exist on the people table. Example additional entries:
#     ("is_reviewer", "reviewer"), ("is_speaker", "speaker"),
PEOPLE_ROLE_FLAGS: tuple[tuple[str, str], ...] = (
    ("is_mentor", "mentor"),
    ("is_organizer", "organizer"),
)
_PEOPLE_FLAG_COLS = "".join(f", p.{col}" for col, _ in PEOPLE_ROLE_FLAGS)


def get_db():
    db = _db.connect(DB, timeout=10)
    db.execute("PRAGMA busy_timeout = 10000")
    db.row_factory = sqlite3.Row
    return db


def _first_existing_table(db, names):
    """Feature-detect: return the first name that exists in sqlite_master.
    Used for the discord FTS mirror, whose canonical name is
    discord_messages_fts (a legacy 'discord_fts' alias never existed in some
    deployments, which left the discord section silently dead)."""
    for name in names:
        try:
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)
            ).fetchone():
                return name
        except sqlite3.Error:
            return None
    return None


def fts_query(db, table, columns_select, match_col, query, limit=10):
    """Run FTS5 MATCH and return results with rank."""
    try:
        sql = f"""SELECT {columns_select}, rank
                  FROM {table}
                  WHERE {match_col} MATCH ?
                  ORDER BY rank
                  LIMIT ?"""
        return db.execute(sql, (query, limit)).fetchall()
    except Exception:
        return []


def _search_rrf(query, limit=15):
    """RRF-fused cross-table view: every corpus fused over FTS + vector
    (+ PPR graph for people/entities) via rrf_search."""
    import rrf_search  # ImportError -> caller falls back to legacy FTS
    per = rrf_search.search_all(query, k=min(limit, 10))
    total = sum(len(v) for v in per.values())
    print(f"=== Cross search (RRF-fused): \"{query}\" ({total} hits) ===")
    for corpus in ("people", "entities", "learnings", "faqs", "docs", "actions",
                   "observations", "episodes", "emails", "discord"):
        rows = per.get(corpus) or []
        if not rows:
            continue
        print(f"\n-- {corpus} ({len(rows)}) --")
        for r in rows:
            print(f"  {r['rrf_score']:.5f}  {rrf_search.format_row(r)[:150]}")


def search(query, verbose=False, limit=15):
    # RRF-fused backend (FTS + vector + PPR graph) when available.
    try:
        return _search_rrf(query, limit=limit)
    except ImportError:
        pass  # legacy pure-FTS path below

    db = get_db()

    # Sanitize: wrap each token in quotes for safe FTS5
    tokens = query.strip().split()
    if not tokens:
        print("No query provided.")
        return
    fts_q = " ".join(f'"{t}"' for t in tokens)

    results = []

    # ── 1. People (via people_fts) ──
    people = fts_query(
        db, "people_fts", "rowid, name, headline, summary", "people_fts", fts_q, limit
    )
    people_rowids = list(dict.fromkeys(p["rowid"] for p in people))  # dedupe, preserve order
    if people_rowids:
        placeholders = ",".join("?" * len(people_rowids))
        full_people = {r["rid"]: r for r in db.execute(
            f"SELECT p.rowid AS rid, p.id, p.name, p.email, p.headline AS affiliation, p.location{_PEOPLE_FLAG_COLS} FROM people p WHERE p.rowid IN ({placeholders})",
            people_rowids,
        ).fetchall()}
    else:
        full_people = {}
    rank_map = {p["rowid"]: abs(p["rank"]) for p in people}
    for pid in people_rowids:
        full = full_people.get(pid)
        if full:
            tags = [label for col, label in PEOPLE_ROLE_FLAGS if full[col]]
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            results.append({
                "source": "people",
                "rank": rank_map.get(pid, 10),
                "person_id": full["id"],
                "line": f"{full['name']}{tag_str} | {full['email'] or '-'} | {full['affiliation'] or '-'} | {full['location'] or '-'}",
            })

    # ── 2. Emails (via emails_fts) ──
    emails = fts_query(
        db, "emails_fts", "rowid, subject, body", "emails_fts", fts_q, limit
    )
    email_rowids = [e["rowid"] for e in emails]
    if email_rowids:
        placeholders = ",".join("?" * len(email_rowids))
        full_emails = {r["rid"]: r for r in db.execute(
            f"SELECT rowid AS rid, sender_name, sender_email, subject, timestamp, is_outgoing FROM emails WHERE rowid IN ({placeholders})",
            email_rowids,
        ).fetchall()}
    else:
        full_emails = {}
    email_ranks = {e["rowid"]: abs(e["rank"]) for e in emails}
    for rid in email_rowids:
        full = full_emails.get(rid)
        if full:
            direction = "OUT" if full["is_outgoing"] else "IN"
            subj = (full["subject"] or "")[:80]
            results.append({
                "source": "emails",
                "rank": email_ranks.get(rid, 10) * 1.5,
                "line": f"[{direction}] {(full['timestamp'] or '')[:16]} | {full['sender_name'] or full['sender_email']} | {subj}",
            })

    # ── 3. Action Items (via action_items_fts) ──
    actions = fts_query(
        db, "action_items_fts", "rowid, description, context", "action_items_fts", fts_q, limit
    )
    action_rowids = [a["rowid"] for a in actions]
    if action_rowids:
        placeholders = ",".join("?" * len(action_rowids))
        full_actions = {r["rid"]: r for r in db.execute(
            f"SELECT rowid AS rid, status, priority, description, waiting_on FROM action_items WHERE rowid IN ({placeholders})",
            action_rowids,
        ).fetchall()}
    else:
        full_actions = {}
    action_ranks = {a["rowid"]: abs(a["rank"]) for a in actions}
    for rid in action_rowids:
        full = full_actions.get(rid)
        if full:
            w = f" [WAITING: {full['waiting_on']}]" if full["waiting_on"] else ""
            results.append({
                "source": "action_items",
                "rank": action_ranks.get(rid, 10) * 0.8,
                "line": f"[{full['status']}] [{full['priority']}] {full['description'][:120]}{w}",
            })

    # ── 4. Learnings (via learnings_fts) ──
    lrns = fts_query(
        db, "learnings_fts", "rowid, title, description", "learnings_fts", fts_q, limit
    )
    lrn_rowids = [l["rowid"] for l in lrns]
    if lrn_rowids:
        placeholders = ",".join("?" * len(lrn_rowids))
        full_lrns = {r["rid"]: r for r in db.execute(
            f"SELECT rowid AS rid, title, description, status FROM learnings WHERE rowid IN ({placeholders}) AND status = 'active'",
            lrn_rowids,
        ).fetchall()}
    else:
        full_lrns = {}
    lrn_ranks = {l["rowid"]: abs(l["rank"]) for l in lrns}
    for rid in lrn_rowids:
        full = full_lrns.get(rid)
        if full:
            results.append({
                "source": "learnings",
                "rank": lrn_ranks.get(rid, 10) * 0.9,
                "line": f"{full['title']}: {full['description'][:120]}",
            })

    # ── 5. Discord (feature-detected FTS mirror) ──
    discord_fts_table = _first_existing_table(
        db, ("discord_messages_fts", "discord_fts"))
    discord = fts_query(
        db, discord_fts_table, "rowid, content", discord_fts_table, fts_q, 5
    ) if discord_fts_table else []
    discord_rowids = [d["rowid"] for d in discord]
    if discord_rowids:
        placeholders = ",".join("?" * len(discord_rowids))
        full_discord = {r["rid"]: r for r in db.execute(
            f"""SELECT dm.rowid AS rid, dm.content, dm.timestamp, du.username, dc.name as channel
                FROM discord_messages dm
                LEFT JOIN discord_users du ON dm.author_id = du.id
                LEFT JOIN discord_channels dc ON dm.channel_id = dc.id
                WHERE dm.rowid IN ({placeholders})""",
            discord_rowids,
        ).fetchall()}
    else:
        full_discord = {}
    discord_ranks = {d["rowid"]: abs(d["rank"]) for d in discord}
    for rid in discord_rowids:
        full = full_discord.get(rid)
        if full:
            content = (full["content"] or "")[:100].replace("\n", " ")
            results.append({
                "source": "discord",
                "rank": discord_ranks.get(rid, 10) * 2.0,
                "line": f"#{full['channel'] or '?'} | {full['username'] or '?'} | {(full['timestamp'] or '')[:16]} | {content}",
            })

    # ── Sort and display ──
    results.sort(key=lambda r: r["rank"])

    if not results:
        print(f"No results for '{query}'.")
        db.close()
        return

    # Group by source for display
    print(f"=== Cross-search: \"{query}\" ({len(results)} results) ===\n")

    current_source = None
    for r in results[:limit]:
        if r["source"] != current_source:
            current_source = r["source"]
            label = {
                "people": "PEOPLE",
                "emails": "EMAILS",
                "action_items": "ACTION ITEMS",
                "learnings": "LEARNINGS",
                "discord": "DISCORD",
            }.get(current_source, current_source.upper())
            print(f"-- {label} --")
        prefix = f"  [{r['rank']:.1f}] " if verbose else "  "
        print(f"{prefix}{r['line']}")

    # Also show counts per source
    from collections import Counter
    counts = Counter(r["source"] for r in results)
    print(f"\n({' | '.join(f'{v} {k}' for k, v in counts.most_common())})")

    db.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    if not args:
        print(__doc__)
    else:
        query = " ".join(args)
        search(query, verbose=verbose)
