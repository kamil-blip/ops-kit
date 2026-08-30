"""Embed learnings into sqlite-vec for hybrid (vector + FTS5) retrieval.

Mirrors embed_people.py. Closes the gap where learning surfacing used keyword
overlap only while vec_learnings sat stale/partial.

Usage:
    python embed_learnings.py              # Embed un-embedded active learnings
    python embed_learnings.py --all        # Re-embed everything
    python embed_learnings.py --incremental# Re-embed those changed since last embed
    python embed_learnings.py --stats      # Show embedding stats
    python embed_learnings.py --search "never auto-send email"  # Quick test search

Model: BAAI/bge-small-en-v1.5 (384-dim, local, free) -- the model already used by
all the vec_* tables, so vectors stay comparable across the corpus.
"""
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


def ensure_table(db):
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
    if "vec_learnings" not in have:
        db.execute(f"CREATE VIRTUAL TABLE {schema}.vec_learnings USING vec0(embedding float[{DIMS}])")
        print(f"Created {schema}.vec_learnings virtual table")


def _has_col(db, table, col):
    return any(r[1] == col for r in db.execute(f"PRAGMA table_info({table})").fetchall())


def build_text(row):
    """title + apply_when + description[:600] -- the actionable rule, not just the title.

    row = (id, title, description, priority, apply_when, context)
    """
    title = (row[1] or "").strip()
    apply_when = (row[4] or "").strip()
    context = (row[5] or "").strip()
    description = (row[2] or "").strip()[:600]
    parts = [p for p in (title, apply_when, context, description) if p]
    text = ". ".join(parts)
    return text[:1000] if text else title


def serialize_vec(vec):
    return struct.pack(f"{DIMS}f", *vec.tolist())


def _select_sql(incremental):
    base = ("SELECT id, title, description, priority, apply_when, context "
            "FROM learnings WHERE status='active' AND title IS NOT NULL AND title != ''")
    if incremental:
        base += " AND (last_embedded_at IS NULL OR updated_at > last_embedded_at)"
    return base


def embed_all(reembed=False, incremental=False):
    db = get_db()
    ensure_table(db)
    has_lea = _has_col(db, "learnings", "last_embedded_at")

    if reembed:
        db.execute("DELETE FROM vec_learnings")
        db.commit()
        mode = "full re-embed"
    elif incremental:
        if not has_lea:
            print("WARNING: learnings.last_embedded_at missing; add that column first. "
                  "Falling back to new-only.")
            incremental = False
            mode = "new only"
        else:
            mode = "incremental (changed since last embed)"
    else:
        mode = "new only"

    if incremental:
        rows = db.execute(_select_sql(True)).fetchall()
        for r in rows:
            try:
                db.execute("DELETE FROM vec_learnings WHERE rowid = ?", (r[0],))
            except Exception:
                pass
        db.commit()
    else:
        existing = set()
        if not reembed:
            existing = {r[0] for r in db.execute("SELECT rowid FROM vec_learnings").fetchall()}
        rows = db.execute(_select_sql(False)).fetchall()
        rows = [r for r in rows if r[0] not in existing]

    to_embed = [(r[0], build_text(r)) for r in rows]
    if not to_embed:
        total = db.execute("SELECT COUNT(*) FROM vec_learnings").fetchone()[0]
        print(f"All {total} learning embeddings up to date. Use --all to re-embed.")
        _stamp_freshness(db)
        db.close()
        return 0

    print(f"Embedding {len(to_embed)} learnings ({mode})...")
    t0 = time.time()
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    print(f"Model loaded in {time.time() - t0:.1f}s")

    t0 = time.time()
    total = 0
    embedded_ids = []
    for i in range(0, len(to_embed), BATCH_SIZE):
        batch = to_embed[i:i + BATCH_SIZE]
        ids = [b[0] for b in batch]
        texts = [b[1] for b in batch]
        embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        for lid, emb in zip(ids, embeddings):
            db.execute("INSERT INTO vec_learnings(rowid, embedding) VALUES (?, ?)",
                       (lid, serialize_vec(emb)))
            embedded_ids.append(lid)
        total += len(batch)
        db.commit()  # commit per batch so concurrent writers (e.g. a live run) never wait long
        print(f"  {total}/{len(to_embed)} ({total/(time.time()-t0):.0f}/s)")

    if has_lea:
        for lid in embedded_ids:
            db.execute("UPDATE learnings SET last_embedded_at = datetime('now') WHERE id = ?", (lid,))
        db.commit()

    elapsed = time.time() - t0
    print(f"Done: {total} learnings embedded in {elapsed:.1f}s ({total/elapsed:.0f}/s)")
    print(f"vec_learnings: {db.execute('SELECT COUNT(*) FROM vec_learnings').fetchone()[0]} rows")
    _stamp_freshness(db)
    db.close()
    return total


def _stamp_freshness(db):
    """The embedder owns its vec_freshness ledger row (previously only the
    brief sync refreshed it, so the ledger went stale whenever sync paused)."""
    try:
        vec = db.execute("SELECT COUNT(*) FROM vec_learnings").fetchone()[0]
        pending = db.execute(
            "SELECT COUNT(*) FROM learnings WHERE status='active' AND id NOT IN (SELECT rowid FROM vec_learnings)"
        ).fetchone()[0]
        db.execute("INSERT OR REPLACE INTO vec_freshness(table_name,last_embedded_at,rows_embedded,rows_pending,updated_at) "
                   "VALUES ('learnings', datetime('now'), ?, ?, datetime('now'))", (vec, pending))
        db.commit()
        print(f"vec_freshness stamped: learnings embedded={vec} pending={pending}")
    except Exception as e:
        print(f"WARN vec_freshness stamp failed: {e}")


def stats():
    db = get_db()
    ensure_table(db)
    vec = db.execute("SELECT COUNT(*) FROM vec_learnings").fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM learnings WHERE status='active'").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
    print(f"Learnings: {total} total / {active} active")
    print(f"Embedded (vec_learnings): {vec}")
    # active rows with no embedding = retrieval-invisible
    missing = db.execute(
        "SELECT COUNT(*) FROM learnings WHERE status='active' AND id NOT IN (SELECT rowid FROM vec_learnings)"
    ).fetchone()[0]
    print(f"Active rows with NO embedding (retrieval-invisible): {missing}")
    db.close()


def search(query, k=10):
    db = get_db()
    ensure_table(db)
    if db.execute("SELECT COUNT(*) FROM vec_learnings").fetchone()[0] == 0:
        print("No embeddings yet. Run: python embed_learnings.py --all")
        db.close()
        return
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    vec_bytes = serialize_vec(model.encode(query, normalize_embeddings=True))
    results = db.execute("""
        SELECT l.learning_id, l.priority, substr(l.title,1,60), vl.distance
        FROM vec_learnings vl JOIN learnings l ON l.id = vl.rowid
        WHERE vl.embedding MATCH ? AND k = ? ORDER BY vl.distance
    """, (vec_bytes, k)).fetchall()
    print(f"Top {k} for: '{query}'")
    for r in results:
        print(f"  {r[3]:.4f}  [{r[1] or '-':<8}] {r[0]:<22} {r[2]}")
    db.close()


def main():
    # Embed runs stamp a LearningsEmbedNightly heartbeat (fail-open;
    # stats/search are read-only and intentionally do NOT stamp liveness).
    try:
        from job_heartbeat import heartbeat as _heartbeat
    except Exception:
        import contextlib as _cl
        def _heartbeat(job):
            return _cl.nullcontext(type("_HB", (), {"rows_touched": 0, "exit_note": None})())
    if len(sys.argv) < 2:
        with _heartbeat("LearningsEmbedNightly") as hb:
            hb.rows_touched = embed_all() or 0
    elif sys.argv[1] == "--all":
        with _heartbeat("LearningsEmbedNightly") as hb:
            hb.rows_touched = embed_all(reembed=True) or 0
    elif sys.argv[1] == "--incremental":
        with _heartbeat("LearningsEmbedNightly") as hb:
            hb.rows_touched = embed_all(incremental=True) or 0
    elif sys.argv[1] == "--stats":
        stats()
    elif sys.argv[1] == "--search":
        search(" ".join(sys.argv[2:]) if len(sys.argv) > 2 else "never auto-send email")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
