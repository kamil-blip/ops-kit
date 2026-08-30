"""Memory lifecycle management: TTL expiry, dedup, learning decay, incremental embeddings.

Usage:
    python memory_lifecycle.py expire          # Delete expired rows
    python memory_lifecycle.py dedup           # Remove duplicate observations/learnings
    python memory_lifecycle.py decay           # Archive unused learnings (pool-size triggered)
    python memory_lifecycle.py decay --force   # Run decay regardless of pool size
    python memory_lifecycle.py decay --dry-run # Show what would archive, don't write
    python memory_lifecycle.py embed           # Re-embed changed people records
    python memory_lifecycle.py promote OBS_ID  # Promote an observation (prevent expiry)
    python memory_lifecycle.py pin SESSION_ID  # Pin a session summary (prevent expiry)
    python memory_lifecycle.py status          # Show lifecycle stats
    python memory_lifecycle.py all             # Run expire + dedup + decay + embed

TTL policy:
    session_summary: 24 hours (pin to keep)
    observations: 7 days (promote to keep)
    learnings: decay on memory pressure (pool > 400), never criticals/high
    bus_events/hook_health/audit_events: 90 days (handled by the retention
    policies in autonomy/retention_pack.py, not by this module)
"""
import paths
import hashlib
import sqlite3
import _db  # unified connector (busy_timeout + FK ON)
import sys
from datetime import datetime
from pathlib import Path

DB = str(paths.DB_PATH)
# Embeddings live in a separate vec.db next to the main DB (2-file layout).
VEC_DB = str(getattr(paths, "VEC_DB_PATH", Path(paths.DB_PATH).parent / "vec.db"))
DIMS = 384
SEMANTIC_THRESHOLD = 0.85  # Cosine similarity threshold for semantic dedup
JACCARD_THRESHOLD = 0.6    # Token overlap threshold


def get_db(with_vec=False):
    db = _db.connect(DB, timeout=30)
    db.execute("PRAGMA busy_timeout = 30000")
    db.row_factory = sqlite3.Row
    if with_vec:
        import sqlite_vec
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        if not any(r[1] == 'vecdb' for r in db.execute('PRAGMA database_list')):
            db.execute("ATTACH DATABASE '%s' AS vecdb" % VEC_DB)
        db.enable_load_extension(False)
    return db


def content_hash(text):
    """SHA256 prefix for exact dedup."""
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def jaccard_similarity(text_a, text_b):
    """Token-level Jaccard similarity."""
    if not text_a or not text_b:
        return 0.0
    tokens_a = set(text_a.lower().split())
    tokens_b = set(text_b.lower().split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def expire(db):
    """Delete rows past their TTL."""
    now = datetime.now().isoformat()

    # Session summaries (1-day TTL, skip pinned)
    expired_sessions = db.execute(
        "SELECT COUNT(*) FROM session_summary WHERE expires_at < ? AND (pinned IS NULL OR pinned = 0)",
        (now,)
    ).fetchone()[0]
    if expired_sessions > 0:
        db.execute(
            "DELETE FROM session_summary WHERE expires_at < ? AND (pinned IS NULL OR pinned = 0)",
            (now,)
        )
        print(f"  Expired {expired_sessions} session summaries")
    else:
        print("  No expired session summaries")

    # Observations (7-day TTL, skip promoted)
    expired_obs = db.execute(
        "SELECT COUNT(*) FROM observations WHERE expires_at < ? AND (promoted IS NULL OR promoted = 0)",
        (now,)
    ).fetchone()[0]
    if expired_obs > 0:
        db.execute(
            "DELETE FROM observations WHERE expires_at < ? AND (promoted IS NULL OR promoted = 0)",
            (now,)
        )
        print(f"  Expired {expired_obs} observations")
    else:
        print("  No expired observations")

    db.commit()
    return expired_sessions + expired_obs


def dedup_table(db, table, content_col, type_col=None):
    """Three-layer dedup on a table.

    Layer 1: Exact hash match (content_hash column)
    Layer 2: Jaccard token overlap >= threshold
    Layer 3: (semantic similarity via embeddings - future, requires per-row embeddings)
    """
    rows = db.execute(
        f"SELECT id, {content_col}, content_hash"
        + (f", {type_col}" if type_col else "")
        + f" FROM {table} ORDER BY id"
    ).fetchall()

    if len(rows) < 2:
        print(f"  {table}: {len(rows)} rows, nothing to dedup")
        return 0

    # Layer 1: exact hash dedup
    seen_hashes = {}
    to_delete = set()
    for r in rows:
        h = r["content_hash"]
        if h and h in seen_hashes:
            to_delete.add(r["id"])
        elif h:
            seen_hashes[h] = r["id"]

    # Layer 2: Jaccard dedup on remaining rows
    remaining = [r for r in rows if r["id"] not in to_delete]
    for i in range(len(remaining)):
        if remaining[i]["id"] in to_delete:
            continue
        for j in range(i + 1, len(remaining)):
            if remaining[j]["id"] in to_delete:
                continue
            # Only compare within same type if type_col exists
            if type_col and remaining[i][type_col] != remaining[j][type_col]:
                continue
            sim = jaccard_similarity(
                remaining[i][content_col],
                remaining[j][content_col]
            )
            if sim >= JACCARD_THRESHOLD:
                # Keep the older one (lower id), delete the newer
                to_delete.add(remaining[j]["id"])

    if to_delete:
        placeholders = ",".join("?" * len(to_delete))
        db.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", list(to_delete))
        db.commit()

    print(f"  {table}: removed {len(to_delete)} duplicates ({len(rows)} -> {len(rows) - len(to_delete)})")
    return len(to_delete)


def dedup(db):
    """Run dedup on observations and learnings."""
    total = 0
    total += dedup_table(db, "observations", "content", "subject")
    total += dedup_table(db, "learnings", "description")
    return total


# Memory-pressure decay: only archive when pool exceeds this, matching MemGPT's
# threshold-triggered consolidation rather than wall-clock schedules.
DECAY_TRIGGER_POOL_SIZE = 400
PROTECTED_PRIORITIES = ("critical", "high", "P0", "P1", "HIGH")


def decay(db, force=False, dry_run=False):
    """Archive unused learnings when the active pool exceeds threshold.

    Criteria A (never surfaced): no row in learning_reviews AND inserted_at older
      than 60 days AND priority not in protected set.
    Criteria B (surfaced but forgotten): fsrs_last_review older than 90 days
      AND priority not in protected set.

    Protected priorities (critical/high/P0/P1) NEVER decay; they graduate via
    the separate promote_to_permanent pipeline instead.

    Trigger: only runs when active pool > DECAY_TRIGGER_POOL_SIZE unless --force.
    """
    # Tier A self-maintain: regenerate the always-on graduated-rules block in
    # the local instructions file when residency changed. Skip-if-unchanged +
    # exception-safe, so it never churns the file or breaks decay. Runs on the
    # SessionStart 20h tick (and on any `memory_lifecycle.py decay|all`
    # invocation), independent of the pool-pressure gate below.
    if not dry_run:
        try:
            import graduated_rules
            graduated_rules.regenerate(quiet=True)
        except Exception:
            pass

    active = db.execute(
        "SELECT COUNT(*) FROM learnings WHERE status='active'"
    ).fetchone()[0]

    print(f"  active pool: {active}")
    if not force and active <= DECAY_TRIGGER_POOL_SIZE:
        print(f"  pool below trigger ({DECAY_TRIGGER_POOL_SIZE}), skipping decay")
        return 0

    placeholders = ",".join("?" * len(PROTECTED_PRIORITIES))

    # Criteria A: never surfaced + stale insert
    a_rows = db.execute(f"""
        SELECT l.id, l.learning_id, l.title, l.priority, l.inserted_at
        FROM learnings l
        WHERE l.status = 'active'
          AND l.priority NOT IN ({placeholders})
          AND l.inserted_at < datetime('now', '-60 days')
          AND NOT EXISTS (
              SELECT 1 FROM learning_reviews lr WHERE lr.learning_id = l.id
          )
    """, PROTECTED_PRIORITIES).fetchall()

    # Criteria B: surfaced once but not reviewed recently
    b_rows = db.execute(f"""
        SELECT l.id, l.learning_id, l.title, l.priority, l.fsrs_last_review
        FROM learnings l
        WHERE l.status = 'active'
          AND l.priority NOT IN ({placeholders})
          AND l.fsrs_last_review IS NOT NULL
          AND l.fsrs_last_review < datetime('now', '-90 days')
    """, PROTECTED_PRIORITIES).fetchall()

    # Criteria C (burn-in): medium/low priority, never surfaced, >14 days old.
    # Tighter than A because medium/low pile up faster and rarely prove useful.
    # A 14-day never-surfaced learning has missed every matching session for two
    # weeks; continuing to hold it costs context noise without upside.
    c_rows = db.execute("""
        SELECT l.id, l.learning_id, l.title, l.priority, l.inserted_at
        FROM learnings l
        WHERE l.status = 'active'
          AND l.priority IN ('medium','low','P2','P3')
          AND l.inserted_at < datetime('now', '-14 days')
          AND NOT EXISTS (
              SELECT 1 FROM learning_reviews lr WHERE lr.learning_id = l.id
          )
    """).fetchall()

    a_ids = [r["id"] for r in a_rows]
    b_ids = [r["id"] for r in b_rows]
    c_ids = [r["id"] for r in c_rows]
    all_ids = set(a_ids) | set(b_ids) | set(c_ids)

    print(f"  criteria A (never surfaced, >60d old):        {len(a_ids)}")
    print(f"  criteria B (stale review, >90d):              {len(b_ids)}")
    print(f"  criteria C (med/low, never surfaced, >14d):   {len(c_ids)}")
    print(f"  total to archive:                             {len(all_ids)}")

    if dry_run:
        print("  (dry-run, no writes)")
        for r in (a_rows + b_rows + c_rows)[:10]:
            print(f"    [{r['priority']}] {r['learning_id']}: {r['title']}")
        return len(all_ids)

    if not all_ids:
        return 0

    now = datetime.now().isoformat(timespec="seconds")
    for rid in a_ids:
        db.execute(
            "UPDATE learnings SET status='archived', "
            "source = COALESCE(source,'') || ' | decayed:never-surfaced@' || ?, "
            "updated_at=datetime('now') WHERE id=?",
            (now, rid),
        )
    for rid in b_ids:
        if rid in a_ids:
            continue
        db.execute(
            "UPDATE learnings SET status='archived', "
            "source = COALESCE(source,'') || ' | decayed:stale-review@' || ?, "
            "updated_at=datetime('now') WHERE id=?",
            (now, rid),
        )
    seen = set(a_ids) | set(b_ids)
    for rid in c_ids:
        if rid in seen:
            continue
        db.execute(
            "UPDATE learnings SET status='archived', "
            "source = COALESCE(source,'') || ' | decayed:burn-in-zombie@' || ?, "
            "updated_at=datetime('now') WHERE id=?",
            (now, rid),
        )
    db.commit()
    print(f"  archived {len(all_ids)} learnings")
    return len(all_ids)


def embed_incremental(db):
    """Re-embed people records that changed since last embedding."""
    # Find people who need re-embedding
    to_embed = db.execute("""
        SELECT COUNT(*) FROM people
        WHERE (last_embedded_at IS NULL OR updated_at > last_embedded_at)
        AND name IS NOT NULL AND name != ''
    """).fetchone()[0]

    if to_embed == 0:
        print("  All people embeddings up to date")
        return 0

    print(f"  {to_embed} people need re-embedding...")

    # Delegate to embed_people.py (it handles incremental by default).
    # Located via sys.path (the installer's .pth puts every capability dir on
    # it), then run as a subprocess so the embedding model's memory is released
    # when the batch finishes.
    import importlib.util
    import subprocess
    spec = importlib.util.find_spec("embed_people")
    if spec is None or not spec.origin:
        print("  embed_people module not found on sys.path; skipping re-embed")
        return 0
    result = subprocess.run(
        [sys.executable, spec.origin, "--incremental"],
        capture_output=True, text=True, timeout=600
    )
    if result.stdout:
        for line in result.stdout.strip().split("\n"):
            print(f"    {line}")
    if result.returncode != 0 and result.stderr:
        # Filter out warnings
        for line in result.stderr.strip().split("\n"):
            if "Warning" not in line and "LOAD REPORT" not in line:
                print(f"    ERR: {line}")

    return to_embed


def promote(db, obs_id):
    """Promote an observation (remove TTL)."""
    db.execute(
        "UPDATE observations SET promoted = 1, expires_at = NULL WHERE id = ?",
        (obs_id,)
    )
    db.commit()
    row = db.execute("SELECT id, subject, content FROM observations WHERE id = ?", (obs_id,)).fetchone()
    if row:
        print(f"  Promoted observation #{obs_id}: {row['subject']} - {(row['content'] or '')[:60]}")
    else:
        print(f"  Observation #{obs_id} not found")


def pin(db, session_id):
    """Pin a session summary (remove TTL)."""
    db.execute(
        "UPDATE session_summary SET pinned = 1, expires_at = NULL WHERE session_id = ?",
        (session_id,)
    )
    changes = db.execute("SELECT changes()").fetchone()[0]
    db.commit()
    if changes > 0:
        print(f"  Pinned session {session_id}")
    else:
        print(f"  Session {session_id} not found")


def status(db):
    """Show lifecycle stats."""
    now = datetime.now().isoformat()

    print("=== Memory Lifecycle Status ===\n")

    # Session summaries
    total = db.execute("SELECT COUNT(*) FROM session_summary").fetchone()[0]
    pinned = db.execute("SELECT COUNT(*) FROM session_summary WHERE pinned = 1").fetchone()[0]
    expired = db.execute(
        "SELECT COUNT(*) FROM session_summary WHERE expires_at < ? AND (pinned IS NULL OR pinned = 0)",
        (now,)
    ).fetchone()[0]
    active = total - pinned - expired
    print(f"session_summary: {total} total | {pinned} pinned | {active} active | {expired} expired")

    # Observations
    total = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    promoted = db.execute("SELECT COUNT(*) FROM observations WHERE promoted = 1").fetchone()[0]
    expired = db.execute(
        "SELECT COUNT(*) FROM observations WHERE expires_at < ? AND (promoted IS NULL OR promoted = 0)",
        (now,)
    ).fetchone()[0]
    active = total - promoted - expired
    print(f"observations:    {total} total | {promoted} promoted | {active} active | {expired} expired")

    # Learnings
    total = db.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
    print(f"learnings:       {total} total | all permanent")

    # Embeddings
    try:
        db_vec = get_db(with_vec=True)
        embedded = db_vec.execute("SELECT COUNT(*) FROM vec_people").fetchone()[0]
        db_vec.close()
    except Exception:
        embedded = "?"
    people = db.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    stale = db.execute("""
        SELECT COUNT(*) FROM people
        WHERE (last_embedded_at IS NULL OR updated_at > last_embedded_at)
        AND name IS NOT NULL AND name != ''
    """).fetchone()[0]
    print(f"embeddings:      {embedded}/{people} people | {stale} stale")

    # Dedup candidates
    obs_dupes = 0
    obs_rows = db.execute("SELECT content_hash, COUNT(*) as c FROM observations WHERE content_hash IS NOT NULL GROUP BY content_hash HAVING c > 1").fetchall()
    obs_dupes = sum(r["c"] - 1 for r in obs_rows)
    learn_dupes = 0
    learn_rows = db.execute("SELECT content_hash, COUNT(*) as c FROM learnings WHERE content_hash IS NOT NULL GROUP BY content_hash HAVING c > 1").fetchall()
    learn_dupes = sum(r["c"] - 1 for r in learn_rows)
    print(f"exact dupes:     {obs_dupes} observations, {learn_dupes} learnings")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    action = sys.argv[1]

    if action == "status":
        db = get_db()
        status(db)
        db.close()
    elif action == "expire":
        db = get_db()
        expire(db)
        db.close()
    elif action == "dedup":
        db = get_db()
        dedup(db)
        db.close()
    elif action == "decay":
        force = "--force" in sys.argv
        dry_run = "--dry-run" in sys.argv
        db = get_db()
        decay(db, force=force, dry_run=dry_run)
        db.close()
    elif action == "embed":
        db = get_db()
        embed_incremental(db)
        db.close()
    elif action == "promote":
        if len(sys.argv) < 3:
            print("Usage: python memory_lifecycle.py promote OBS_ID")
            return
        db = get_db()
        promote(db, int(sys.argv[2]))
        db.close()
    elif action == "pin":
        if len(sys.argv) < 3:
            print("Usage: python memory_lifecycle.py pin SESSION_ID")
            return
        db = get_db()
        pin(db, sys.argv[2])
        db.close()
    elif action == "all":
        db = get_db()
        print("--- Expire ---")
        expire(db)
        print("\n--- Dedup ---")
        dedup(db)
        print("\n--- Decay ---")
        decay(db)
        db.close()
        # Embed needs vec, separate connection
        print("\n--- Embed ---")
        db = get_db()
        embed_incremental(db)
        db.close()
        print("\n--- Status ---")
        db = get_db()
        status(db)
        db.close()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
