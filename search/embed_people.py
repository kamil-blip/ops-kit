"""Embed people records into sqlite-vec for hybrid search.

Usage:
    python embed_people.py              # Embed all un-embedded people
    python embed_people.py --all        # Re-embed everything
    python embed_people.py --stats      # Show embedding stats
    python embed_people.py --search "AI safety researcher"  # Quick test search

Requires: sqlite-vec, sentence-transformers, onnxruntime
Model: BAAI/bge-small-en-v1.5 (384-dim, ~33MB, free, local)
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

from audit_actor import actor_scope

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
    """Create vec_people virtual table in vec.db if it doesn't exist."""
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
    if "vec_people" not in have:
        db.execute(f"CREATE VIRTUAL TABLE {schema}.vec_people USING vec0(embedding float[{DIMS}])")
        print(f"Created {schema}.vec_people virtual table")


def build_text(row):
    """Build searchable text from a people record."""
    parts = []
    name = row[1] or ""
    if name:
        parts.append(name)

    affiliation = row[4] or ""
    if affiliation:
        parts.append(f"at {affiliation}")

    seniority = row[5] or ""
    if seniority:
        parts.append(seniority)

    location = row[6] or ""
    if location:
        parts.append(f"in {location}")

    career_stage = row[7] or ""
    if career_stage:
        parts.append(career_stage)

    headline = row[8] or ""
    if headline:
        parts.append(headline)

    education = row[9] or ""
    if education:
        parts.append(education)

    research_interests = row[10] or ""
    if research_interests:
        parts.append(f"interests: {research_interests}")

    capability = row[11] or ""
    if capability:
        parts.append(capability)

    notes = row[12] or ""
    if notes and len(notes) < 500:
        parts.append(notes)

    text = ". ".join(parts)
    return text[:1000] if text else name  # Cap at 1000 chars


def serialize_vec(vec):
    """Convert numpy array to bytes for sqlite-vec."""
    return struct.pack(f"{DIMS}f", *vec.tolist())


def embed_all(reembed=False, incremental=False):
    db = get_db()
    ensure_table(db)
    # write-gate: actor set/cleared via try/finally, NEVER bare set_actor --
    # _audit_context is a GLOBAL singleton; an uncleared actor waves through
    # every other writer's NULL-actor writes (learned from a past incident).
    from audit_actor import set_actor, clear_actor
    set_actor(db, "writer:embed_people", source_ref="embed-pass")
    try:
        return _embed_all_impl(db, reembed, incremental)
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


def _embed_all_impl(db, reembed, incremental):

    if reembed:
        db.execute("DELETE FROM vec_people")
        db.commit()
        mode = "full re-embed"
    elif incremental:
        mode = "incremental (changed since last embed)"
    else:
        mode = "new only"

    # Determine which people to embed
    if incremental:
        # Re-embed people whose data changed since last embedding
        rows = db.execute("""
            SELECT id, name, email, linkedin, headline AS affiliation, seniority, location,
                   career_stage, headline, education, research_interests, capability, notes
            FROM people
            WHERE name IS NOT NULL AND name != ''
            AND (last_embedded_at IS NULL OR updated_at > last_embedded_at)
        """).fetchall()
        # Delete old embeddings for these people so we can re-insert
        stale_ids = [r[0] for r in rows]
        if stale_ids:
            for sid in stale_ids:
                try:
                    db.execute("DELETE FROM vec_people WHERE rowid = ?", (sid,))
                except Exception:
                    pass
            db.commit()
    else:
        existing = set()
        if not reembed:
            existing = {r[0] for r in db.execute("SELECT rowid FROM vec_people").fetchall()}
        rows = db.execute("""
            SELECT id, name, email, linkedin, headline AS affiliation, seniority, location,
                   career_stage, headline, education, research_interests, capability, notes
            FROM people
            WHERE name IS NOT NULL AND name != ''
        """).fetchall()
        rows = [r for r in rows if r[0] not in existing]

    to_embed = [(r[0], build_text(r)) for r in rows]

    if not to_embed:
        total_embedded = db.execute("SELECT COUNT(*) FROM vec_people").fetchone()[0]
        print(f"All {total_embedded} people embeddings up to date. Use --all to re-embed.")
        return  # close + actor-clear happen in embed_all's finally

    print(f"Embedding {len(to_embed)} people ({mode})...")

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

        for pid, emb in zip(ids, embeddings):
            db.execute(
                "INSERT INTO vec_people(rowid, embedding) VALUES (?, ?)",
                (pid, serialize_vec(emb))
            )
            embedded_ids.append(pid)

        total += len(batch)
        elapsed = time.time() - t0
        rate = total / elapsed
        print(f"  {total}/{len(to_embed)} ({rate:.0f}/s)")

    # Mark as embedded (attribute the cdc rows so they aren't NULL-actor)
    with actor_scope(db, "embed_people:last_embedded"):
        for pid in embedded_ids:
            db.execute("UPDATE people SET last_embedded_at = datetime('now') WHERE id = ?", (pid,))

    db.commit()
    elapsed = time.time() - t0
    print(f"Done: {total} people embedded in {elapsed:.1f}s ({total/elapsed:.0f}/s)")

    count = db.execute("SELECT COUNT(*) FROM vec_people").fetchone()[0]
    print(f"vec_people: {count} rows")
    # close + actor-clear happen in embed_all's finally


def stats():
    db = get_db()
    ensure_table(db)
    vec_count = db.execute("SELECT COUNT(*) FROM vec_people").fetchone()[0]
    people_count = db.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    print(f"People: {people_count}")
    print(f"Embedded: {vec_count} ({vec_count/people_count*100:.1f}%)")
    if vec_count > 0:
        import os
        db_size = os.path.getsize(DB) / (1024 * 1024)
        print(f"DB size: {db_size:.1f} MB")
    db.close()


def search(query, k=10):
    """Quick vector search for testing."""
    db = get_db()
    ensure_table(db)

    vec_count = db.execute("SELECT COUNT(*) FROM vec_people").fetchone()[0]
    if vec_count == 0:
        print("No embeddings yet. Run: python embed_people.py")
        db.close()
        return

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)

    emb = model.encode(query, normalize_embeddings=True)
    vec_bytes = serialize_vec(emb)

    results = db.execute("""
        SELECT p.id, p.name, p.headline AS affiliation, p.location, vp.distance
        FROM vec_people vp
        JOIN people p ON p.id = vp.rowid
        WHERE vp.embedding MATCH ?
        AND k = ?
        ORDER BY vp.distance
    """, (vec_bytes, k)).fetchall()

    print(f"Top {k} results for: '{query}'")
    print(f"{'Name':<30} {'Affiliation':<30} {'Location':<20} {'Dist':>6}")
    print("-" * 90)
    for r in results:
        print(f"{(r[1] or ''):<30} {(r[2] or ''):<30} {(r[3] or ''):<20} {r[4]:>6.4f}")

    db.close()


def main():
    if len(sys.argv) < 2:
        embed_all()
    elif sys.argv[1] == "--all":
        embed_all(reembed=True)
    elif sys.argv[1] == "--incremental":
        embed_all(incremental=True)
    elif sys.argv[1] == "--stats":
        stats()
    elif sys.argv[1] == "--search":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "AI safety researcher"
        search(query)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
