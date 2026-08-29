"""Embed action_items into sqlite-vec for semantic task retrieval.

action_items (the operational backlog) had FTS5 (action_items_fts) but no
semantic index, so "what was that thing about paying the invoice" only matched
literal keywords. This embeds each item's description into the shared 384-dim
fabric so tasks are reachable by meaning alongside emails/people/docs.

Usage:
    python embed_action_items.py              # Embed un-embedded items
    python embed_action_items.py --all        # Re-embed everything
    python embed_action_items.py --stats      # Show coverage
    python embed_action_items.py --search "Q" # Quick semantic search test

Requires: sqlite-vec, sentence-transformers
Model: BAAI/bge-small-en-v1.5 (384-dim, ~33MB, free, local) -- matches embed_emails.py
"""
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
BATCH_SIZE = 256


def get_db():
    import sqlite_vec
    db = _db.connect(DB, timeout=90)
    db.execute("PRAGMA busy_timeout = 90000")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    # 2-file layout: vec_* embedding tables live in data/vec.db, attached as vecdb.
    if not any(r[1] == 'vecdb' for r in db.execute('PRAGMA database_list')):
        db.execute("ATTACH DATABASE '%s' AS vecdb" % VEC_DB)
    db.enable_load_extension(False)
    return db


def ensure_table(db):
    # Target the attached vec.db (2-file layout): an unqualified CREATE would
    # re-fork an empty shadow table into the main DB.
    attached = {r[1] for r in db.execute("PRAGMA database_list")}
    schema = "vecdb" if "vecdb" in attached else "main"
    have = {r[0] for r in db.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table'").fetchall()}
    if "vec_action_items" not in have:
        db.execute(f"CREATE VIRTUAL TABLE {schema}.vec_action_items USING vec0(embedding float[{DIMS}])")
        print(f"Created {schema}.vec_action_items virtual table")


def build_text(row):
    """description carries the task; context tags add light disambiguation.
    row = (id, item_id, description, context)."""
    desc = (row[2] or "").strip()
    context = (row[3] or "").strip()
    parts = [p for p in (desc, context) if p]
    return " ".join(parts)[:2000] if parts else desc


def serialize_vec(vec):
    return struct.pack(f"{DIMS}f", *vec.tolist())


def embed_all(reembed=False):
    db = get_db()
    ensure_table(db)

    if reembed:
        db.execute("DELETE FROM vec_action_items")
        db.commit()
        mode = "full re-embed"
    else:
        mode = "new only"

    existing = set()
    if not reembed:
        existing = {r[0] for r in db.execute("SELECT rowid FROM vec_action_items").fetchall()}

    rows = db.execute(
        "SELECT id, item_id, description, context FROM action_items "
        "WHERE description IS NOT NULL AND description != ''").fetchall()
    to_embed = [(r[0], build_text(r)) for r in rows if r[0] not in existing]

    if not to_embed:
        total = db.execute("SELECT COUNT(*) FROM vec_action_items").fetchone()[0]
        print(f"All {total} action-item embeddings up to date. Use --all to re-embed.")
        _stamp_freshness(db)
        db.close()
        return 0

    print(f"Embedding {len(to_embed)} action items ({mode})...")
    t0 = time.time()
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    print(f"Model loaded in {time.time() - t0:.1f}s")

    t0 = time.time()
    total = 0
    for i in range(0, len(to_embed), BATCH_SIZE):
        batch = to_embed[i:i + BATCH_SIZE]
        ids = [b[0] for b in batch]
        texts = [b[1] for b in batch]
        embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        for aid, emb in zip(ids, embeddings):
            db.execute("INSERT INTO vec_action_items(rowid, embedding) VALUES (?, ?)",
                       (aid, serialize_vec(emb)))
        total += len(batch)
        db.commit()  # commit per batch so an interruption resumes cleanly
        print(f"  {total}/{len(to_embed)} ({total/(time.time()-t0):.0f}/s)")

    elapsed = time.time() - t0
    print(f"Done: {total} action items embedded in {elapsed:.1f}s ({total/elapsed:.0f}/s)")
    print(f"vec_action_items: {db.execute('SELECT COUNT(*) FROM vec_action_items').fetchone()[0]} rows")
    _stamp_freshness(db)
    db.close()
    return total


def _stamp_freshness(db):
    """Embedder owns its vec_freshness ledger row (mirrors embed_learnings)."""
    try:
        vec = db.execute("SELECT COUNT(*) FROM vec_action_items").fetchone()[0]
        pending = db.execute(
            "SELECT COUNT(*) FROM action_items WHERE description IS NOT NULL AND description != '' "
            "AND id NOT IN (SELECT rowid FROM vec_action_items)").fetchone()[0]
        db.execute("INSERT OR REPLACE INTO vec_freshness(table_name,last_embedded_at,rows_embedded,rows_pending,updated_at) "
                   "VALUES ('action_items', datetime('now'), ?, ?, datetime('now'))", (vec, pending))
        db.commit()
        print(f"vec_freshness stamped: action_items embedded={vec} pending={pending}")
    except Exception as e:
        print(f"WARN vec_freshness stamp failed: {e}")


def stats():
    db = get_db()
    ensure_table(db)
    vec = db.execute("SELECT COUNT(*) FROM vec_action_items").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM action_items WHERE description IS NOT NULL AND description != ''").fetchone()[0]
    print(f"Action items (in scope): {total}")
    print(f"Embedded (vec_action_items): {vec}")
    print(f"Pending: {total - vec}")
    db.close()


def search(query, k=10):
    db = get_db()
    ensure_table(db)
    if db.execute("SELECT COUNT(*) FROM vec_action_items").fetchone()[0] == 0:
        print("No embeddings yet. Run: python embed_action_items.py")
        db.close()
        return
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    vec_bytes = serialize_vec(model.encode(query, normalize_embeddings=True))
    results = db.execute("""
        SELECT a.item_id, a.status, substr(a.description,1,60), va.distance
        FROM vec_action_items va JOIN action_items a ON a.id = va.rowid
        WHERE va.embedding MATCH ? AND k = ? ORDER BY va.distance
    """, (vec_bytes, k)).fetchall()
    print(f"Top {k} action items for: '{query}'")
    for r in results:
        print(f"  {r[3]:.4f}  [{r[1] or '-':<9}] {r[0] or '?':<12} {r[2]}")
    db.close()


def main():
    try:
        from job_heartbeat import heartbeat as _heartbeat
    except Exception:
        import contextlib as _cl
        def _heartbeat(job):
            return _cl.nullcontext(type("_HB", (), {"rows_touched": 0, "exit_note": None})())
    if len(sys.argv) < 2:
        with _heartbeat("ActionItemEmbedNightly") as hb:
            hb.rows_touched = embed_all() or 0
    elif sys.argv[1] == "--all":
        with _heartbeat("ActionItemEmbedNightly") as hb:
            hb.rows_touched = embed_all(reembed=True) or 0
    elif sys.argv[1] == "--stats":
        stats()
    elif sys.argv[1] == "--search":
        search(" ".join(sys.argv[2:]) if len(sys.argv) > 2 else "pay the invoice")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
