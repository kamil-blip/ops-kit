"""Embed email records into sqlite-vec for semantic search.

Usage:
    python embed_emails.py              # Embed all un-embedded emails
    python embed_emails.py --all        # Re-embed everything
    python embed_emails.py --stats      # Show embedding stats
    python embed_emails.py --search "invoice reminder"  # Quick test

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
    if "vec_emails" not in have:
        db.execute(f"CREATE VIRTUAL TABLE {schema}.vec_emails USING vec0(embedding float[{DIMS}])")
        print(f"Created {schema}.vec_emails virtual table")


def build_text(row):
    """Build searchable text from an email record."""
    parts = []
    sender = row["sender_name"] or row["sender_email"] or ""
    if sender:
        parts.append(f"From: {sender}")

    subject = row["subject"] or ""
    if subject:
        parts.append(f"Subject: {subject}")

    body = (row["body"] or "")[:600]
    if body:
        parts.append(body)

    return ". ".join(parts)[:1000] if parts else ""


def serialize_vec(vec):
    return struct.pack(f"{DIMS}f", *vec.tolist())


def embed_all(reembed=False):
    db = get_db()
    ensure_table(db)

    if reembed:
        db.execute("DELETE FROM vec_emails")
        db.commit()
        mode = "full re-embed"
    else:
        mode = "new only"

    existing = set()
    if not reembed:
        existing = {r[0] for r in db.execute("SELECT rowid FROM vec_emails").fetchall()}

    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT id, sender_name, sender_email, subject, body
        FROM emails
        WHERE subject IS NOT NULL AND subject != ''
    """).fetchall()

    to_embed = [(r["id"], build_text(r)) for r in rows if r["id"] not in existing]

    if not to_embed:
        total = db.execute("SELECT COUNT(*) FROM vec_emails").fetchone()[0]
        print(f"All {total} email embeddings up to date. Use --all to re-embed.")
        db.close()
        return

    print(f"Embedding {len(to_embed)} emails ({mode})...")

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
                "INSERT INTO vec_emails(rowid, embedding) VALUES (?, ?)",
                (eid, serialize_vec(emb))
            )

        total += len(batch)
        elapsed = time.time() - t0
        rate = total / elapsed if elapsed > 0 else 0
        print(f"  {total}/{len(to_embed)} ({rate:.0f}/s)")

    db.commit()
    elapsed = time.time() - t0
    print(f"Done: {total} emails embedded in {elapsed:.1f}s ({total/elapsed:.0f}/s)")

    count = db.execute("SELECT COUNT(*) FROM vec_emails").fetchone()[0]
    print(f"vec_emails: {count} rows")
    db.close()


def stats():
    db = get_db()
    ensure_table(db)
    vec_count = db.execute("SELECT COUNT(*) FROM vec_emails").fetchone()[0]
    email_count = db.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    print(f"Emails: {email_count}")
    print(f"Embedded: {vec_count} ({vec_count/email_count*100:.1f}%)" if email_count else "No emails")
    db.close()


def search(query, k=10):
    db = get_db()
    ensure_table(db)
    db.row_factory = sqlite3.Row

    vec_count = db.execute("SELECT COUNT(*) FROM vec_emails").fetchone()[0]
    if vec_count == 0:
        print("No embeddings yet. Run: python embed_emails.py")
        db.close()
        return

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)

    emb = model.encode(query, normalize_embeddings=True)
    vec_bytes = serialize_vec(emb)

    results = db.execute("""
        SELECT e.id, e.sender_name, e.sender_email, e.subject, e.timestamp, ve.distance
        FROM vec_emails ve
        JOIN emails e ON e.id = ve.rowid
        WHERE ve.embedding MATCH ?
        AND k = ?
        ORDER BY ve.distance
    """, (vec_bytes, k)).fetchall()

    print(f"Top {k} results for: '{query}'")
    print(f"{'Date':<20} {'From':<25} {'Subject':<50} {'Dist':>6}")
    print("-" * 105)
    for r in results:
        date = (r["timestamp"] or "")[:19]
        sender = (r["sender_name"] or r["sender_email"] or "")[:24]
        subj = (r["subject"] or "")[:49]
        print(f"{date:<20} {sender:<25} {subj:<50} {r['distance']:>6.4f}")

    db.close()


def main():
    if len(sys.argv) < 2:
        embed_all()
    elif sys.argv[1] == "--all":
        embed_all(reembed=True)
    elif sys.argv[1] == "--stats":
        stats()
    elif sys.argv[1] == "--search":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "invoice reminder"
        search(query)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
