"""Initialize the ops-kit databases from the shipped empty schemas.

Creates (next to the repo root, in data/):
  data/ops.db  -- main database (WAL) from db/schema.sql
  data/vec.db  -- vector database from db/vec_schema.sql (needs sqlite-vec)

Safe to re-run: refuses to touch an existing non-empty ops.db unless --force.
Verifies: PRAGMA integrity_check == ok, foreign_key_check empty, and every
table has 0 rows. Exit 0 = healthy empty install.
"""
import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")


def apply_sql(db_path: str, sql_path: str, need_vec: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    if need_vec:
        try:
            import sqlite_vec
        except ImportError:
            print("ERROR: sqlite-vec not installed (pip install sqlite-vec)", file=sys.stderr)
            sys.exit(2)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(open(sql_path, encoding="utf-8").read())
    conn.commit()
    return conn


def verify(conn: sqlite3.Connection, label: str) -> int:
    integ = conn.execute("PRAGMA integrity_check(1)").fetchone()[0]
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    # Shadow tables of FTS5/vec0 virtual tables hold bootstrap metadata rows
    # even in a healthy empty install; exclude them from the 0-row check.
    virtuals = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL TABLE%'")]
    nonzero = []
    for t in tables:
        if any(t != v and t.startswith(v + "_") for v in virtuals):
            continue  # virtual-table shadow (e.g. *_fts_data, vec_*_info)
        try:
            if conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]:
                nonzero.append(t)
        except sqlite3.OperationalError:
            pass  # virtual-table internals
    ok = integ == "ok" and not fk and not nonzero
    print(f"{label}: tables={len(tables)} integrity={integ} fk_violations={len(fk)} "
          f"nonzero_tables={nonzero or 0} -> {'OK' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="recreate even if ops.db exists")
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    ops = os.path.join(DATA, "ops.db")
    vec = os.path.join(DATA, "vec.db")
    if os.path.exists(ops) and not args.force:
        conn = sqlite3.connect(ops)
        n = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        conn.close()
        if n:
            print(f"{ops} already initialized ({n} tables). Use --force to recreate.")
            return 0
    for p in (ops, vec):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(p + suffix):
                os.remove(p + suffix)

    rc = 0
    conn = apply_sql(ops, os.path.join(HERE, "schema.sql"))
    rc |= verify(conn, "ops.db")
    conn.close()
    conn = apply_sql(vec, os.path.join(HERE, "vec_schema.sql"), need_vec=True)
    rc |= verify(conn, "vec.db")
    conn.close()
    if rc == 0:
        print("init complete: both databases empty and healthy.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
