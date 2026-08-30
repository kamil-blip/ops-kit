"""Embed canonical FAQ records into sqlite-vec for semantic search.

Usage:
    python embed_faqs.py                # Embed all un-embedded approved FAQs
    python embed_faqs.py --all          # Re-embed everything
    python embed_faqs.py --stats        # Show embedding stats
    python embed_faqs.py --search "Q"   # Quick semantic search test
    python embed_faqs.py --include-proposed   # Also embed status='proposed' FAQs

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
BATCH_SIZE = 64  # FAQs are small; embed in smaller batches


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
    if "vec_faqs" not in have:
        db.execute(f"CREATE VIRTUAL TABLE {schema}.vec_faqs USING vec0(embedding float[{DIMS}])")
        print(f"Created {schema}.vec_faqs virtual table")


def build_text(row):
    """Build searchable text from a FAQ record.

    We embed question + first 400 chars of answer together. The question
    drives match precision; the answer adds disambiguation when two questions
    have similar wording but different scope (e.g. staff vs attendee
    'when does the event start').
    """
    q = (row["question_canonical"] or "").strip()
    a = (row["answer_canonical"] or "")[:400].strip()
    topic = (row["topic"] or "").strip()
    scope = (row["scope"] or "").strip()
    parts = []
    if topic or scope:
        parts.append(f"[{topic}/{scope}]" if topic and scope else f"[{topic or scope}]")
    if q:
        parts.append(q)
    if a:
        parts.append(a)
    return " ".join(parts)[:1500]


def serialize_vec(vec):
    return struct.pack(f"{DIMS}f", *vec.tolist())


def embed_all(reembed=False, include_proposed=False):
    db = get_db()
    ensure_table(db)

    if reembed:
        db.execute("DELETE FROM vec_faqs")
        db.commit()
        mode = "full re-embed"
    else:
        mode = "new only"

    existing = set()
    if not reembed:
        existing = {r[0] for r in db.execute("SELECT rowid FROM vec_faqs").fetchall()}

    statuses = "'approved'"
    if include_proposed:
        statuses = "'approved','proposed','draft'"

    db.row_factory = sqlite3.Row
    rows = db.execute(f"""
        SELECT id, faq_id, question_canonical, answer_canonical, topic, scope
        FROM faqs
        WHERE status IN ({statuses})
          AND question_canonical IS NOT NULL
          AND question_canonical != ''
    """).fetchall()

    to_embed = [(r["id"], build_text(r)) for r in rows if r["id"] not in existing]

    if not to_embed:
        total = db.execute("SELECT COUNT(*) FROM vec_faqs").fetchone()[0]
        print(f"All {total} FAQ embeddings up to date. Use --all to re-embed.")
        db.close()
        return

    print(f"Embedding {len(to_embed)} FAQs ({mode})...")

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
        for eid, emb in zip(ids, embeddings):
            db.execute(
                "INSERT INTO vec_faqs(rowid, embedding) VALUES (?, ?)",
                (eid, serialize_vec(emb))
            )
        total += len(batch)
        elapsed = time.time() - t0
        rate = total / elapsed if elapsed > 0 else 0
        print(f"  {total}/{len(to_embed)} ({rate:.0f}/s)")

    db.commit()
    elapsed = time.time() - t0
    print(f"Done: {total} FAQs embedded in {elapsed:.1f}s ({total/elapsed:.0f}/s)")

    count = db.execute("SELECT COUNT(*) FROM vec_faqs").fetchone()[0]
    print(f"vec_faqs: {count} rows")
    db.close()


def stats():
    db = get_db()
    ensure_table(db)
    vec_count = db.execute("SELECT COUNT(*) FROM vec_faqs").fetchone()[0]
    approved = db.execute("SELECT COUNT(*) FROM faqs WHERE status='approved'").fetchone()[0]
    proposed = db.execute("SELECT COUNT(*) FROM faqs WHERE status='proposed'").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM faqs").fetchone()[0]
    print(f"FAQs total: {total}")
    print(f"  approved: {approved}")
    print(f"  proposed: {proposed}")
    print(f"  embedded: {vec_count}")
    db.close()


def search(query, k=5):
    db = get_db()
    ensure_table(db)
    db.row_factory = sqlite3.Row

    vec_count = db.execute("SELECT COUNT(*) FROM vec_faqs").fetchone()[0]
    if vec_count == 0:
        print("No embeddings yet. Run: python embed_faqs.py")
        db.close()
        return

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    emb = model.encode(query, normalize_embeddings=True)
    vec_bytes = serialize_vec(emb)

    results = db.execute("""
        SELECT f.faq_id, f.question_canonical, f.status, f.topic, vf.distance
        FROM vec_faqs vf
        JOIN faqs f ON f.id = vf.rowid
        WHERE vf.embedding MATCH ? AND k = ?
        ORDER BY vf.distance
    """, (vec_bytes, k)).fetchall()

    print(f"Top {k} FAQs (semantic match) for: {query!r}")
    print(f"{'faq_id':<22} {'topic':<14} {'dist':>6}  question")
    print("-" * 100)
    for r in results:
        dist = r["distance"]
        print(f"{r['faq_id']:<22} {r['topic'] or '':<14} {dist:>6.4f}  {(r['question_canonical'] or '')[:60]}")
    db.close()


def main():
    if len(sys.argv) < 2:
        embed_all()
    elif sys.argv[1] == "--all":
        include_p = "--include-proposed" in sys.argv
        embed_all(reembed=True, include_proposed=include_p)
    elif sys.argv[1] == "--stats":
        stats()
    elif sys.argv[1] == "--search":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "when does the event start"
        search(query)
    elif sys.argv[1] == "--include-proposed":
        embed_all(include_proposed=True)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
