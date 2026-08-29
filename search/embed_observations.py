"""Embed observations into sqlite-vec for semantic search.

observations are the canonical fact store (subject + content, person-scoped).
They had FTS but no semantic vector counterpart and were missing from the
vec_freshness ledger (found as a large embedder coverage gap in an audit).
This mirrors embed_episodes.py exactly.

Usage:
    python embed_observations.py            # New only (incremental)
    python embed_observations.py --all      # Re-embed everything
    python embed_observations.py --stats
    python embed_observations.py --search "sponsorship interest"

Requires: sqlite-vec, sentence-transformers. Model: BAAI/bge-small-en-v1.5 (384-dim, local, free).
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
BATCH_SIZE = 256


def get_db():
    import sqlite_vec
    db = _db.connect(DB, timeout=60)
    db.execute("PRAGMA busy_timeout = 60000")
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
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}
    if "vec_observations" not in have:
        db.execute(f"CREATE VIRTUAL TABLE {schema}.vec_observations USING vec0(embedding float[{DIMS}])")
        print(f"Created {schema}.vec_observations virtual table")


def build_text(row):
    subj = (row["subject"] or "").strip()
    st = (row["subject_type"] or "").strip()
    content = (row["content"] or "").strip()[:1500]
    parts = []
    if st:
        parts.append(f"[{st}]")
    if subj:
        parts.append(subj + ":")
    if content:
        parts.append(content)
    return " ".join(parts)[:2000]


def serialize_vec(vec):
    return struct.pack(f"{DIMS}f", *vec.tolist())


def embed_all(reembed: bool = False):
    db = get_db()
    ensure_table(db)
    if reembed:
        db.execute("DELETE FROM vec_observations")
        db.commit()
    existing = set()
    if not reembed:
        existing = {r[0] for r in db.execute("SELECT rowid FROM vec_observations")}
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, subject, subject_type, content FROM observations WHERE content IS NOT NULL AND content != ''"
    ).fetchall()
    targets = [r for r in rows if r["id"] not in existing]
    print(f"Total observations: {len(rows)}; to embed: {len(targets)} ({'full' if reembed else 'incremental'})", flush=True)
    if not targets:
        return
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    t0 = time.time()
    inserted = 0
    for bs in range(0, len(targets), BATCH_SIZE):
        batch = targets[bs:bs + BATCH_SIZE]
        texts = [build_text(r) for r in batch]
        embs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        for r, emb in zip(batch, embs):
            db.execute("INSERT OR REPLACE INTO vec_observations(rowid, embedding) VALUES (?, ?)",
                       (r["id"], serialize_vec(emb)))
        inserted += len(batch)
        if inserted % (BATCH_SIZE * 4) == 0:
            db.commit()
            el = time.time() - t0
            print(f"  {inserted}/{len(targets)} embedded ({inserted/max(el,0.1):.0f}/s, {el:.0f}s)", flush=True)
    db.commit()
    print(f"Done. {inserted} observations embedded in {time.time()-t0:.1f}s", flush=True)


def stats():
    db = get_db()
    total = db.execute("SELECT count(*) FROM observations WHERE content IS NOT NULL AND content!=''").fetchone()[0]
    try:
        emb = db.execute("SELECT count(*) FROM vec_observations").fetchone()[0]
    except sqlite3.OperationalError:
        emb = 0
    print(f"observations (with content): {total}")
    print(f"embedded (vec_observations): {emb}")
    print(f"coverage: {emb/total*100:.1f}%" if total else "n/a")


def search(query: str, k: int = 10):
    db = get_db()
    ensure_table(db)
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    emb = model.encode(query, normalize_embeddings=True)
    rows = db.execute(
        "SELECT o.id, o.subject, o.subject_type, substr(IFNULL(o.content,''),1,120) s, v.distance "
        "FROM vec_observations v JOIN observations o ON o.id=v.rowid "
        "WHERE v.embedding MATCH ? AND k=? ORDER BY v.distance",
        (serialize_vec(emb), k)).fetchall()
    print(f"\nTop {k} for {query!r}:")
    for r in rows:
        print(f"  [{r[4]:.3f}] {(r[2] or '?'):<10} {(r[1] or '?')[:40]:<40} | {r[3]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--search")
    ap.add_argument("--k", type=int, default=10)
    a = ap.parse_args()
    if a.stats:
        stats()
    elif a.search:
        search(a.search, a.k)
    else:
        embed_all(reembed=a.all)


if __name__ == "__main__":
    main()
