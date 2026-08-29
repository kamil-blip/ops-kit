"""Embed entity records into entities.embedding for vector search.

Usage:
    python embed_entities.py              # Embed all un-embedded entities
    python embed_entities.py --all        # Re-embed everything
    python embed_entities.py --sync-vec   # Sync vec_entities index from stored blobs (no re-encode)
    python embed_entities.py --stats      # Show embedding stats
    python embed_entities.py --search "AI safety researcher"  # Quick test search

Writes both entities.embedding (blob) and the vec_entities vec0 index
(joined on entities.rowid by hybrid_search.graph_retrieve). Every embed run
also runs the vec sync pass, so the index cannot silently drift again.
Orphan vec rows (entity deleted) are quarantined to vec_entities_quarantine
before removal from the index, never silently destroyed.

Requires: sqlite-vec, sentence-transformers, onnxruntime
Model: BAAI/bge-small-en-v1.5 (384-dim, ~33MB, free, local)
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
    db = _db.connect(DB, timeout=30)
    db.execute("PRAGMA busy_timeout = 30000")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    # 2-file layout: vec_* embedding tables live in data/vec.db, attached as vecdb.
    if not any(r[1] == 'vecdb' for r in db.execute('PRAGMA database_list')):
        db.execute("ATTACH DATABASE '%s' AS vecdb" % VEC_DB)
    db.enable_load_extension(False)
    return db


def ensure_vec_table(db):
    """Create vec_entities virtual table in vec.db if it doesn't exist (mirrors embed_people)."""
    # vec-fork repair: target the attached vec.db, not main. An unqualified
    # sqlite_master check + unqualified CREATE re-creates an empty shadow in
    # the main DB post-split and re-forks embeddings. Consult the attached
    # vecdb schema when present; fall back to main only if vec.db is genuinely
    # not attached.
    attached = {r[1] for r in db.execute("PRAGMA database_list")}
    schema = "vecdb" if "vecdb" in attached else "main"
    have = {r[0] for r in db.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table'"
    ).fetchall()}
    if "vec_entities" not in have:
        db.execute(f"CREATE VIRTUAL TABLE {schema}.vec_entities USING vec0(embedding float[{DIMS}])")
        print(f"Created {schema}.vec_entities virtual table")


def upsert_vec(db, rowid, blob):
    """Insert or update one vec_entities index row (update-in-place, no delete)."""
    if db.execute("SELECT 1 FROM vec_entities WHERE rowid = ?", (rowid,)).fetchone():
        db.execute("UPDATE vec_entities SET embedding = ? WHERE rowid = ?", (blob, rowid))
    else:
        db.execute("INSERT INTO vec_entities(rowid, embedding) VALUES (?, ?)", (rowid, blob))


def sync_vec(db=None, verbose=True):
    """Incremental vec_entities catch-up. No model load, no re-encode.

    1. Insert index rows for embedded entities missing from vec_entities,
       reusing the stored entities.embedding blob.
    2. Quarantine orphan index rows (rowid no longer in entities) into
       vec_entities_quarantine, then remove them from the live index.
       Reversible: re-insert from the quarantine table.

    Returns (inserted, quarantined, skipped_bad_blob).
    """
    own_db = db is None
    if own_db:
        db = get_db()
    ensure_vec_table(db)

    vec_rowids = {r[0] for r in db.execute("SELECT rowid FROM vec_entities").fetchall()}
    ent_rows = db.execute(
        "SELECT rowid, embedding FROM entities WHERE embedding IS NOT NULL"
    ).fetchall()
    ent_rowids = {r[0] for r in ent_rows}

    inserted = skipped = 0
    for rowid, blob in ent_rows:
        if rowid in vec_rowids:
            continue
        if blob is None or len(blob) != DIMS * 4:
            skipped += 1
            continue
        db.execute("INSERT INTO vec_entities(rowid, embedding) VALUES (?, ?)", (rowid, blob))
        inserted += 1

    orphans = sorted(vec_rowids - ent_rowids)
    if orphans:
        db.execute("""
            CREATE TABLE IF NOT EXISTS vec_entities_quarantine (
                rowid_val INTEGER PRIMARY KEY,
                embedding BLOB,
                quarantined_at TEXT DEFAULT (datetime('now')),
                reason TEXT
            )""")
        for rowid in orphans:
            row = db.execute(
                "SELECT embedding FROM vec_entities WHERE rowid = ?", (rowid,)
            ).fetchone()
            db.execute(
                "INSERT OR REPLACE INTO vec_entities_quarantine(rowid_val, embedding, reason) "
                "VALUES (?, ?, ?)",
                (rowid, row[0] if row else None, "orphan: no matching entities.rowid"),
            )
            db.execute("DELETE FROM vec_entities WHERE rowid = ?", (rowid,))

    db.commit()
    if verbose:
        if inserted or orphans or skipped:
            msg = f"vec_entities sync: +{inserted} inserted, {len(orphans)} orphans quarantined"
            if skipped:
                msg += f", {skipped} skipped (bad blob length)"
            print(msg)
        else:
            print("vec_entities index in sync (nothing to do)")
    if own_db:
        db.close()
    return inserted, len(orphans), skipped


def build_text(row):
    """Build searchable text from an entity record.

    row: (id, name, type, data)
    """
    name = row[1] or ""
    etype = row[2] or ""
    data = row[3] or ""
    if data and len(data) > 500:
        data = data[:500]
    if data:
        return f"{name} ({etype}): {data}"
    return f"{name} ({etype})"


def serialize_vec(vec):
    """Convert numpy array to bytes for sqlite storage."""
    return struct.pack(f"{DIMS}f", *vec.tolist())


def embed_all(reembed=False):
    db = get_db()
    ensure_vec_table(db)
    # write-gate (blocking-ready): embedding bookkeeping on the canonical table
    # must carry an actor or the audit triggers abort it. Set/clear via try/finally,
    # NEVER bare set_actor: _audit_context is a GLOBAL singleton; an uncleared
    # actor waves through every other writer's NULL-actor writes (learned from
    # a past incident). The finally lives in _embed_all_impl's caller below.
    from audit_actor import set_actor, clear_actor
    set_actor(db, "writer:embed_entities", source_ref="embed-pass")
    try:
        return _embed_all_impl(db, reembed)
    finally:
        try:
            clear_actor(db)
            db.commit()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass


def _embed_all_impl(db, reembed):

    if reembed:
        db.execute("UPDATE entities SET embedding = NULL")
        db.commit()
        mode = "full re-embed"
    else:
        mode = "new only"

    rows = db.execute(
        "SELECT id, name, type, data, rowid FROM entities "
        "WHERE embedding IS NULL OR LENGTH(embedding) <= 4"
    ).fetchall()

    if not rows:
        sync_vec(db)  # catch up the vec index even when nothing needs encoding
        total = db.execute(
            "SELECT COUNT(*) FROM entities WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        print(f"All {total} entity embeddings up to date. Use --all to re-embed.")
        return  # close + actor-clear happen in embed_all's finally

    to_embed = [(r[0], r[4], build_text(r)) for r in rows]
    print(f"Embedding {len(to_embed)} entities ({mode})...")

    t0 = time.time()
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    print(f"Model loaded in {time.time() - t0:.1f}s")

    t0 = time.time()
    total = 0
    for i in range(0, len(to_embed), BATCH_SIZE):
        batch = to_embed[i:i + BATCH_SIZE]
        ids = [(b[0], b[1]) for b in batch]  # (id, rowid)
        texts = [b[2] for b in batch]

        embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)

        for (eid, erowid), emb in zip(ids, embeddings):
            blob = serialize_vec(emb)
            db.execute(
                "UPDATE entities SET embedding = ? WHERE id = ?",
                (blob, eid)
            )
            upsert_vec(db, erowid, blob)

        total += len(batch)
        db.commit()

        if total % 1000 < BATCH_SIZE:
            elapsed = time.time() - t0
            rate = total / elapsed
            print(f"  {total}/{len(to_embed)} ({rate:.0f}/s)")

    elapsed = time.time() - t0
    print(f"Done: {total} entities embedded in {elapsed:.1f}s ({total/elapsed:.0f}/s)")

    sync_vec(db)  # pick up any rows embedded earlier but missing from the vec index

    count = db.execute(
        "SELECT COUNT(*) FROM entities WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    vec_count = db.execute("SELECT COUNT(*) FROM vec_entities").fetchone()[0]
    print(f"Entities with embeddings: {count}")
    print(f"vec_entities: {vec_count} rows")
    # close + actor-clear happen in embed_all's finally


def stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    embedded = db.execute(
        "SELECT COUNT(*) FROM entities WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    print(f"Entities: {total}")
    print(f"Embedded: {embedded} ({embedded/total*100:.1f}%)")

    try:
        vec_count = db.execute("SELECT COUNT(*) FROM vec_entities").fetchone()[0]
        missing = db.execute("""
            SELECT COUNT(*) FROM entities e
            LEFT JOIN vec_entities_rowids v ON e.rowid = v.rowid
            WHERE e.embedding IS NOT NULL AND v.rowid IS NULL
        """).fetchone()[0]
        orphans = db.execute("""
            SELECT COUNT(*) FROM vec_entities_rowids v
            LEFT JOIN entities e ON v.rowid = e.rowid
            WHERE e.rowid IS NULL
        """).fetchone()[0]
        drift = "" if (missing == 0 and orphans == 0) else "  <- run --sync-vec"
        print(f"vec_entities index: {vec_count} rows, {missing} missing, {orphans} orphans{drift}")
    except Exception as exc:
        print(f"vec_entities index: unavailable ({exc})")

    # Breakdown by type
    rows = db.execute("""
        SELECT type, COUNT(*) as total,
               SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) as embedded
        FROM entities GROUP BY type ORDER BY total DESC
    """).fetchall()
    print(f"\n{'Type':<15} {'Total':>6} {'Embedded':>9} {'%':>6}")
    print("-" * 40)
    for r in rows:
        pct = r[2] / r[1] * 100 if r[1] > 0 else 0
        print(f"{r[0]:<15} {r[1]:>6} {r[2]:>9} {pct:>5.1f}%")

    import os
    db_size = os.path.getsize(DB) / (1024 * 1024)
    print(f"\nDB size: {db_size:.1f} MB")
    db.close()


def search(query, k=10):
    """Brute-force cosine similarity search over entity embeddings."""
    import numpy as np

    db = get_db()
    embedded = db.execute(
        "SELECT COUNT(*) FROM entities WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    if embedded == 0:
        print("No embeddings yet. Run: python embed_entities.py")
        db.close()
        return

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)

    query_emb = model.encode(query, normalize_embeddings=True)

    rows = db.execute(
        "SELECT id, name, type, embedding FROM entities WHERE embedding IS NOT NULL"
    ).fetchall()

    results = []
    for r in rows:
        emb = np.array(struct.unpack(f"{DIMS}f", r[3]))
        sim = float(np.dot(query_emb, emb))
        results.append((r[0], r[1], r[2], sim))

    results.sort(key=lambda x: x[3], reverse=True)

    print(f"Top {k} results for: '{query}'")
    print(f"{'Name':<40} {'Type':<12} {'Score':>6}")
    print("-" * 62)
    for r in results[:k]:
        print(f"{(r[1] or ''):<40} {(r[2] or ''):<12} {r[3]:>6.4f}")

    db.close()


def main():
    if len(sys.argv) < 2:
        embed_all()
    elif sys.argv[1] == "--all":
        embed_all(reembed=True)
    elif sys.argv[1] == "--sync-vec":
        sync_vec()
    elif sys.argv[1] == "--stats":
        stats()
    elif sys.argv[1] == "--search":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "AI safety researcher"
        search(query)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
