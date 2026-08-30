"""Embed episodes into sqlite-vec for semantic search.

Episodes cover email/discord/messenger/observation grouped artifacts. They
already have FTS5 via episodes_fts but no semantic counterpart. This makes
"find all episodes about X" reach beyond exact-keyword match.

Usage:
    python embed_episodes.py                       # New only (incremental)
    python embed_episodes.py --all                 # Re-embed everything
    python embed_episodes.py --stats               # Show coverage
    python embed_episodes.py --search "sponsorship topic"

Requires: sqlite-vec, sentence-transformers
Model: BAAI/bge-small-en-v1.5 (384-dim, ~33MB, free, local) -- matches embed_emails.py
"""
import argparse
import io
import sqlite3
import struct
import sys
import time
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
VEC_DB = str(getattr(paths, "VEC_DB_PATH", Path(paths.DB_PATH).parent / "vec.db"))
MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMS = 384
BATCH_SIZE = 128


def get_db():
    import sqlite_vec
    db = _db.connect(DB, timeout=30)
    db.execute("PRAGMA busy_timeout = 30000")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    # 2-file layout: vec_* embedding tables live in data/vec.db, attached as vecdb.
    if not any(r[1] == 'vecdb' for r in db.execute('PRAGMA database_list')):
        db.execute("ATTACH DATABASE '%s' AS vecdb" % VEC_DB)
    db.enable_load_extension(False)
    return db


def ensure_table(db):
    # Note: vec_episodes can only be queried from connections that loaded
    # sqlite_vec (use _db.connect(vec=True) or this module's get_db). A raw
    # sqlite3.connect() against vec_episodes raises "no such module: vec0".
    # vec-fork repair: target the attached vec.db, not main. An unqualified
    # sqlite_master check + unqualified CREATE re-creates an empty shadow in
    # the main DB post-split and re-forks embeddings. Consult the attached
    # vecdb schema when present; fall back to main only if vec.db is genuinely
    # not attached.
    attached = {r[1] for r in db.execute("PRAGMA database_list")}
    schema = "vecdb" if "vecdb" in attached else "main"
    have = {r[0] for r in db.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}
    if "vec_episodes" not in have:
        db.execute(f"CREATE VIRTUAL TABLE {schema}.vec_episodes USING vec0(embedding float[{DIMS}])")
        print(f"Created {schema}.vec_episodes virtual table")


def build_text(row):
    """Build searchable text from an episode row.

    Use topic + first 800 chars of summary. Episodes without summary fall back
    to topic only, which still gives a useful signal. Direction/source/channel
    metadata is included as tag-style prefix to disambiguate near-duplicates
    (e.g. inbound vs outbound emails on the same topic).
    """
    topic = (row["topic"] or "").strip()
    summary = (row["summary"] or "")[:800].strip()
    kind = (row["kind"] or "").strip()
    channel = (row["channel"] or "").strip()
    direction = (row["direction"] or "").strip()
    parts = []
    tag_parts = [x for x in (kind, channel, direction) if x]
    if tag_parts:
        parts.append("[" + "/".join(tag_parts) + "]")
    if topic:
        parts.append(topic)
    if summary:
        parts.append(summary)
    return " ".join(parts)[:2000]


def serialize_vec(vec):
    return struct.pack(f"{DIMS}f", *vec.tolist())


def prune_orphans(db=None) -> int:
    """Delete vec rows whose episode is gone. No delete-sync existed originally
    (orphans once accumulated to ~15% index bloat), and the retention pass
    deletes aged episodes nightly, so without this the shadow grows
    unboundedly. vec_episodes.rowid == episodes.id."""
    own = db is None
    if own:
        db = get_db()
        ensure_table(db)
    n = db.execute(
        "DELETE FROM vec_episodes WHERE rowid NOT IN (SELECT id FROM episodes)"
    ).rowcount
    db.commit()
    if n:
        print(f"Pruned {n} orphan vec_episodes rows (episodes deleted/retained)")
    if own:
        db.close()
    return n


def embed_all(reembed: bool = False):
    db = get_db()
    ensure_table(db)
    prune_orphans(db)

    if reembed:
        db.execute("DELETE FROM vec_episodes")
        db.commit()

    existing = set()
    if not reembed:
        existing = {r[0] for r in db.execute("SELECT rowid FROM vec_episodes")}

    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, kind, topic, summary, channel, direction FROM episodes WHERE (topic IS NOT NULL OR summary IS NOT NULL)"
    ).fetchall()
    targets = [r for r in rows if r["id"] not in existing]
    # Skip-reason taxonomy: every episode NOT embedded must land in a named
    # bucket, so "missing" in an anti-join audit is explainable.
    total_all = db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    out_of_scope = db.execute(
        "SELECT COUNT(*) FROM episodes WHERE topic IS NULL AND summary IS NULL").fetchone()[0]
    empty_text = sum(1 for r in targets if not build_text(r).strip())
    print(f"Total episodes: {total_all}; in scope (topic/summary): {len(rows)}; "
          f"out of scope (no topic AND no summary): {out_of_scope}; "
          f"already embedded: {len(rows) - len(targets)}; to embed: {len(targets)} "
          f"(of which empty-text: {empty_text}) "
          f"({'full re-embed' if reembed else 'incremental'})")
    if not targets:
        return

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)

    t0 = time.time()
    inserted = 0
    for batch_start in range(0, len(targets), BATCH_SIZE):
        batch = targets[batch_start:batch_start + BATCH_SIZE]
        texts = [build_text(r) for r in batch]
        embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        for r, emb in zip(batch, embeddings):
            db.execute(
                "INSERT OR REPLACE INTO vec_episodes(rowid, embedding) VALUES (?, ?)",
                (r["id"], serialize_vec(emb)),
            )
        inserted += len(batch)
        if inserted % (BATCH_SIZE * 10) == 0:
            db.commit()
            elapsed = time.time() - t0
            rate = inserted / max(elapsed, 0.1)
            print(f"  {inserted}/{len(targets)} embedded ({rate:.0f}/s, elapsed {elapsed:.0f}s)")
    db.commit()
    print(f"\nDone. {inserted} episodes embedded in {time.time()-t0:.1f}s")


def stats():
    db = get_db()
    total_eps = db.execute("SELECT count(*) FROM episodes WHERE topic IS NOT NULL OR summary IS NOT NULL").fetchone()[0]
    try:
        embedded = db.execute("SELECT count(*) FROM vec_episodes").fetchone()[0]
    except sqlite3.OperationalError:
        embedded = 0
    print(f"Episodes (with topic or summary): {total_eps}")
    print(f"Embedded (vec_episodes): {embedded}")
    print(f"Coverage: {embedded/total_eps*100:.1f}%" if total_eps else "n/a")


def search(query: str, k: int = 10):
    db = get_db()
    ensure_table(db)
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    emb = model.encode(query, normalize_embeddings=True)
    vec_bytes = serialize_vec(emb)
    rows = db.execute(
        """SELECT e.id, e.kind, e.topic, e.ts, e.channel, e.direction,
                  substr(IFNULL(e.summary,''), 1, 120) AS s,
                  v.distance
           FROM vec_episodes v JOIN episodes e ON e.id = v.rowid
           WHERE v.embedding MATCH ? AND k = ?
           ORDER BY v.distance""",
        (vec_bytes, k),
    ).fetchall()
    print(f"\nTop {k} for {query!r}:")
    for r in rows:
        print(f"  [{r[7]:.3f}] {r[1]:<10} {r[3]:<11} {(r[2] or '?')[:50]:<50} | {r[6]:<8} {r[5]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="Re-embed everything")
    ap.add_argument("--stats", action="store_true", help="Show coverage")
    ap.add_argument("--prune", action="store_true", help="Delete orphan vec rows only")
    ap.add_argument("--search", help="Run a semantic search query")
    ap.add_argument("--k", type=int, default=10, help="Top-k for --search")
    args = ap.parse_args()

    if args.stats:
        stats()
    elif args.prune:
        prune_orphans()
    elif args.search:
        search(args.search, args.k)
    else:
        embed_all(reembed=args.all)


if __name__ == "__main__":
    main()
