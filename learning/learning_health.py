"""Learnings-system health.

One place to see whether the learning loop is actually healthy: adherence coverage,
embedding currency, Tier-A size + invariant, FSRS state distribution, and the
consolidation backlog. Writes timestamped snapshots so trends are visible, and
exposes a one-line summary for query.py health + daily_digest.

Usage:
    python learning_health.py                 # human-readable snapshot (also writes one)
    python learning_health.py --json
    python learning_health.py --line          # one-line summary (for digests)
    python learning_health.py --no-write       # print, don't persist a snapshot
    python learning_health.py --selftest
"""
import paths
import json
import sqlite3
import _db  # unified connector (busy_timeout + FK ON)
import sys

DB = str(paths.DB_PATH)
TIER_A_CAP = 40


def get_db(write=False):
    db = _db.connect(DB, timeout=30) if write else sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    return db


SNAP_DDL = """CREATE TABLE IF NOT EXISTS learning_health_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now')),
    active INTEGER, embedded INTEGER, embed_pct REAL,
    adherence_total INTEGER, adherence_judged INTEGER, adherence_pct REAL,
    violated INTEGER, followed INTEGER, na INTEGER,
    tier_a INTEGER, critical INTEGER,
    consolidation_proposed INTEGER, consolidation_conflict INTEGER,
    note TEXT, metrics_json TEXT
)"""

SNAP_FTS = "CREATE VIRTUAL TABLE IF NOT EXISTS learning_health_snapshots_fts USING fts5(note, content='learning_health_snapshots', content_rowid='id')"
SNAP_TRIGGERS = [
    "CREATE TRIGGER IF NOT EXISTS lhs_ai AFTER INSERT ON learning_health_snapshots BEGIN INSERT INTO learning_health_snapshots_fts(rowid, note) VALUES (new.id, new.note); END",
    "CREATE TRIGGER IF NOT EXISTS lhs_ad AFTER DELETE ON learning_health_snapshots BEGIN INSERT INTO learning_health_snapshots_fts(learning_health_snapshots_fts, rowid, note) VALUES('delete', old.id, old.note); END",
    "CREATE TRIGGER IF NOT EXISTS lhs_au AFTER UPDATE ON learning_health_snapshots BEGIN INSERT INTO learning_health_snapshots_fts(learning_health_snapshots_fts, rowid, note) VALUES('delete', old.id, old.note); INSERT INTO learning_health_snapshots_fts(rowid, note) VALUES (new.id, new.note); END",
]


def ensure_snapshot_table(db):
    db.execute(SNAP_DDL)
    db.execute(SNAP_FTS)
    for t in SNAP_TRIGGERS:
        db.execute(t)
    # register in the _table_descriptions catalog when that convention is present
    if _has(db, "_table_descriptions"):
        if not db.execute("SELECT 1 FROM _table_descriptions WHERE table_name='learning_health_snapshots'").fetchone():
            db.execute(
                """INSERT INTO _table_descriptions (table_name, tier, description, when_to_query, key_columns, example_queries, priority_note, category)
                   VALUES ('learning_health_snapshots','system',
                     'Timestamped learnings-system health snapshots (adherence coverage, embed currency, Tier-A, FSRS, consolidation backlog).',
                     'Trend the learning loop health over time.',
                     'ts, adherence_pct, embed_pct, tier_a, consolidation_conflict',
                     'SELECT ts, adherence_pct, embed_pct, tier_a FROM learning_health_snapshots ORDER BY id DESC LIMIT 10;',
                     'Written by learning_health.py.', 'learnings')"""
            )
        for tname in ("learning_health_snapshots_fts",):
            if not db.execute("SELECT 1 FROM _table_descriptions WHERE table_name=?", (tname,)).fetchone():
                db.execute(
                    "INSERT INTO _table_descriptions (table_name, tier, description, when_to_query, key_columns, category) "
                    "VALUES (?, 'system', 'FTS5 mirror of learning_health_snapshots notes.', 'search snapshot notes', 'note', 'learnings')",
                    (tname,),
                )
    db.commit()


def summary(db=None):
    own = db is None
    if own:
        db = get_db()
    try:
        def n(sql, p=()):
            r = db.execute(sql, p).fetchone()
            return (r[0] if r and r[0] is not None else 0)
        active = n("SELECT COUNT(*) FROM learnings WHERE status='active'")
        # embedded requires sqlite_vec; degrade gracefully
        try:
            import sqlite_vec
            db.enable_load_extension(True); sqlite_vec.load(db); db.enable_load_extension(False)
            # vec_* embedding tables live in a separate vec DB file, attached as vecdb
            db.execute("ATTACH DATABASE '%s' AS vecdb" % paths.VEC_DB_PATH) if not any(r[1] == 'vecdb' for r in db.execute('PRAGMA database_list')) else None
            embedded = n("SELECT COUNT(*) FROM vec_learnings")
        except Exception:
            embedded = None
        adh_total = n("SELECT COUNT(*) FROM learning_reviews")
        adh_judged = n("SELECT COUNT(*) FROM learning_reviews WHERE adherence IS NOT NULL")
        violated = n("SELECT COUNT(*) FROM learning_reviews WHERE adherence='violated'")
        followed = n("SELECT COUNT(*) FROM learning_reviews WHERE adherence IN ('followed','followed_pinned')")
        na = n("SELECT COUNT(*) FROM learning_reviews WHERE adherence IN ('na','na_pinned')")
        tier_a = n("""SELECT COUNT(*) FROM learnings WHERE status='active' AND priority IN ('critical','P0','P1')
                      AND (SELECT COUNT(*) FROM learning_reviews lr WHERE lr.learning_id=learnings.id) >= 5""")
        critical = n("SELECT COUNT(*) FROM learnings WHERE status='active' AND priority='critical'")
        cons_prop = n("SELECT COUNT(*) FROM consolidation_log WHERE status='proposed'") if _has(db, "consolidation_log") else 0
        cons_conf = n("SELECT COUNT(*) FROM consolidation_log WHERE status='conflict'") if _has(db, "consolidation_log") else 0
        fsrs = {str(r[0]): r[1] for r in db.execute(
            "SELECT fsrs_state, COUNT(*) FROM learnings WHERE status='active' GROUP BY fsrs_state")}
        return {
            "active": active, "embedded": embedded,
            "embed_pct": round(100.0 * embedded / active, 1) if (embedded and active) else (100.0 if embedded == active else None),
            "adherence_total": adh_total, "adherence_judged": adh_judged,
            "adherence_pct": round(100.0 * adh_judged / adh_total, 2) if adh_total else 0.0,
            "violated": violated, "followed": followed, "na": na,
            "tier_a": tier_a, "tier_a_cap": TIER_A_CAP, "tier_a_ok": tier_a <= TIER_A_CAP,
            "critical": critical,
            "consolidation_proposed": cons_prop, "consolidation_conflict": cons_conf,
            "fsrs_state_distribution": fsrs,
        }
    finally:
        if own:
            db.close()


def _has(db, t):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


def line(db=None):
    s = summary(db)
    emb = f"{s['embed_pct']}%" if s["embed_pct"] is not None else "?"
    return (f"LEARNINGS: {s['active']} active, embed {emb}, adherence {s['adherence_pct']}% "
            f"({s['adherence_judged']}/{s['adherence_total']}), Tier-A {s['tier_a']}/{s['tier_a_cap']}, "
            f"consolidation {s['consolidation_proposed']}p/{s['consolidation_conflict']}c")


def snapshot(db=None):
    own = db is None
    if own:
        db = get_db(write=True)
    try:
        ensure_snapshot_table(db)
        s = summary(db)
        note = line(db)
        db.execute(
            """INSERT INTO learning_health_snapshots
               (active, embedded, embed_pct, adherence_total, adherence_judged, adherence_pct,
                violated, followed, na, tier_a, critical, consolidation_proposed, consolidation_conflict, note, metrics_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s["active"], s["embedded"], s["embed_pct"], s["adherence_total"], s["adherence_judged"],
             s["adherence_pct"], s["violated"], s["followed"], s["na"], s["tier_a"], s["critical"],
             s["consolidation_proposed"], s["consolidation_conflict"], note, json.dumps(s)),
        )
        db.commit()
        return s
    finally:
        if own:
            db.close()


def selftest():
    ok = True
    s = summary()
    # adherence write path is live: >0 judged (expects a system that has been
    # running; on a fresh empty DB this check reports FAIL until reviews accrue)
    print(f"  [adherence] judged={s['adherence_judged']} (>0 expected) {'OK' if s['adherence_judged']>0 else 'FAIL'}")
    ok &= s["adherence_judged"] > 0
    # Tier-A invariant holds
    print(f"  [tier-A] {s['tier_a']} <= cap {s['tier_a_cap']}: {s['tier_a_ok']}")
    ok &= s["tier_a_ok"]
    # snapshot writes + reads back
    db = get_db(write=True)
    before = db.execute("SELECT COUNT(*) FROM learning_health_snapshots").fetchone()[0] if _has(db, "learning_health_snapshots") else 0
    snapshot(db)
    after = db.execute("SELECT COUNT(*) FROM learning_health_snapshots").fetchone()[0]
    db.close()
    print(f"  [snapshot] rows {before} -> {after}: {'OK' if after==before+1 else 'FAIL'}")
    ok &= (after == before + 1)
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


def main():
    a = sys.argv[1:]
    if a[:1] == ["--selftest"]:
        sys.exit(0 if selftest() else 1)
    if a[:1] == ["--line"]:
        print(line()); return
    s = snapshot() if "--no-write" not in a else summary()
    if "--json" in a:
        print(json.dumps(s, indent=2)); return
    print(line())
    print(f"  FSRS states: {s['fsrs_state_distribution']}")
    print(f"  violated {s['violated']} / followed {s['followed']} / na {s['na']}")


if __name__ == "__main__":
    main()
