"""Shared SQLite connector for ops.db. Standardizes timeout, busy_timeout,
WAL-friendly pragmas, and (when needed) sqlite-vec loading. Use this instead
of raw `sqlite3.connect(...)` to prevent SQLITE_BUSY under concurrent writers.

Usage:
    from _db import connect
    conn = connect()                   # default: timeout=30, busy_timeout=30000

    conn = connect(vec=True)           # also loads sqlite-vec extension

    conn = connect(readonly=True)      # opens read-only (uri mode)

vec_* virtual tables (vec_episodes, vec_emails, etc.) REQUIRE vec=True. A
default connection that touches them raises OperationalError "no such module:
vec0". FTS-based search paths do not need it; only direct SELECTs on the
vec_* tables need the vec-loaded path. The embed_* scripts load sqlite_vec
themselves.

History: earlier revisions patched every caller with `timeout=30` +
`PRAGMA busy_timeout=30000` inline. This module collapses that pattern so
callers don't have to remember it.
"""
from __future__ import annotations
import sqlite3
import time
from pathlib import Path

try:
    from paths import DB_PATH as _DB_PATH, ROOT as _ROOT
except ImportError:
    # Flat-import fallback: paths.py sits next to this file in core/.
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from paths import DB_PATH as _DB_PATH, ROOT as _ROOT
DEFAULT_DB = str(_DB_PATH)
DEFAULT_TIMEOUT = 30
DEFAULT_BUSY_TIMEOUT_MS = 30_000

# DB-level destructive-DDL guard. A default-ON sqlite authorizer on read-write
# connections DENIES `DROP TABLE <crown-jewel>` -- the accidental
# `DROP TABLE people` class of disaster. Scoped narrowly so it NEVER breaks a
# legit caller: ALTER is allowed (ADD COLUMN is ubiquitous and the authorizer
# cannot distinguish a safe ADD from a destructive DROP COLUMN), and DROP of any
# non-core / scratch / temp / vec table is allowed. A migration/backup that must
# drop a core table passes allow_destructive=True (or sets the env escape hatch
# OPS_DB_ALLOW_DESTRUCTIVE=1). Read-only connections skip it (can't write, and
# they are the hot read paths). DELETE/UPDATE-without-WHERE are intentionally NOT
# here: the authorizer is called at prepare time and never sees the WHERE clause,
# so it cannot tell a full-table wipe from a scoped delete -- that stays covered
# by the backup-before-destructive discipline and the text-level safety rules.
import os as _os

# Extend this set with your own crown-jewel tables as the system grows.
PROTECTED_CORE_TABLES = {
    "people", "emails", "discord_messages", "beeper_messages", "beeper_chats",
    "action_items", "action_items_inbox", "learnings", "entities", "edges",
    "observations", "reference_docs", "template_tasks",
    "plan_runs", "schema_migrations", "cdc_log", "bus_events", "memory_files",
    "session_log_entries", "review_queue", "merge_candidates", "person_emails",
    "email_threads", "episodes",
}


def _make_ddl_guard():
    _drop_table = getattr(sqlite3, "SQLITE_DROP_TABLE", 11)

    def _guard(action, arg1, arg2, dbname, trigger):
        # fast path: only DROP TABLE of a protected core table is ever denied
        if action == _drop_table and (arg1 or "").lower() in PROTECTED_CORE_TABLES:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return _guard


def connect(
    path: str | Path = DEFAULT_DB,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    vec: bool = False,
    readonly: bool = False,
    row_factory: type | None = None,
    allow_destructive: bool = False,
) -> sqlite3.Connection:
    """Open a connection with standard pragmas.

    Args:
        path: DB path. Defaults to ops.db.
        timeout: Python-side connection timeout in seconds.
        busy_timeout_ms: SQLite busy timeout in milliseconds.
        vec: If True, also load the sqlite-vec extension.
        readonly: If True, open in read-only URI mode.
        row_factory: Optional row factory (e.g. sqlite3.Row).
    """
    path_str = str(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path_str}?mode=ro", uri=True, timeout=timeout)
    else:
        # Create-refusal guard: a wrong-path connect used to silently MINT a
        # 0-byte DB stub at the bad path. Refuse to create any non-canonical
        # ops.db; everything else (existing DBs, other names, :memory:) is
        # untouched.
        p = Path(path_str)
        if (p.name == "ops.db" and not p.exists()
                and str(p.resolve()) != str(Path(DEFAULT_DB).resolve())):
            raise RuntimeError(
                f"_db.connect refused to CREATE a non-canonical ops.db at {p}; "
                f"canonical is {DEFAULT_DB}.")
        conn = sqlite3.connect(path_str, timeout=timeout)
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    # Durability spine: enforce referential integrity at write time so broken
    # refs (a merge that orphaned a child row, an edge pointing at a missing
    # entity) raise instead of silently rotting the graph. Only affects new
    # writes on this connection; pre-existing violations are tolerated until
    # touched.
    conn.execute("PRAGMA foreign_keys=ON")
    # Durability pragmas: cut fsync churn under WAL. synchronous=NORMAL is
    # crash-safe under WAL (only loses the very last transaction on power loss,
    # not the DB); larger cache + mmap reduce read I/O as the DB grows.
    # RW path only; harmless no-ops on RO (which cannot write anyway).
    if not readonly:
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-262144")
        conn.execute("PRAGMA mmap_size=268435456")
    # Install the destructive-DDL guard on RW connections unless explicitly
    # waived. Fail-open (never break connect if the authorizer API is
    # unavailable). Readonly conns skip it: they cannot write anyway.
    if not readonly and not allow_destructive and _os.environ.get("OPS_DB_ALLOW_DESTRUCTIVE") != "1":
        try:
            conn.set_authorizer(_make_ddl_guard())
        except Exception:  # noqa: BLE001 - fail-open
            pass
    if row_factory is not None:
        conn.row_factory = row_factory
    if vec:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        # 2-file layout: vec_* tables live in data/vec.db. Unqualified names
        # resolve main -> attached, so callers need no edits. Only attach when
        # targeting the primary DB, and never fail the connect.
        # NOTE: ATTACH silently CREATES an empty vec.db when the file is
        # missing, which would fork a phantom vec store. Guard on existence
        # first and warn loudly to stderr instead of creating it; still never
        # raise (read paths must keep working even when the vec store is
        # genuinely absent).
        try:
            if Path(path_str).resolve() == Path(str(DEFAULT_DB)).resolve():
                vec_path = Path(str(DEFAULT_DB)).parent / "vec.db"
                if vec_path.exists():
                    conn.execute(f"ATTACH DATABASE '{vec_path.as_posix()}' AS vecdb")
                else:
                    import sys as _sys
                    print(
                        f"WARNING _db.connect: vec store missing at {vec_path}; "
                        "vec_* tables unavailable (not attaching -- ATTACH would "
                        "create an empty DB and re-fork the store)",
                        file=_sys.stderr,
                    )
        except sqlite3.Error as _exc:
            import sys as _sys
            print(f"WARNING _db.connect: ATTACH vec.db failed: {_exc}", file=_sys.stderr)
    return conn


def make_backup(tag: str, *, db_path: str | Path = DEFAULT_DB) -> Path:
    """Create a DB backup in data/backups/, never at data/ top level.

    Backup-flow convention: every script that wants a safety copy calls this
    instead of shutil.copy'ing the DB into data/. Uses VACUUM INTO (consistent
    snapshot, works while WAL is active).

    Returns the Path of the created backup file.
    """
    import datetime as _dt
    import re as _re

    safe_tag = _re.sub(r"[^A-Za-z0-9_-]+", "-", tag).strip("-") or "untagged"
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backups_dir = Path(db_path).parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    # prefix derives from the db filename: a hardcoded prefix would misname
    # backups of any other DB (e.g. vec.db)
    stem = Path(db_path).stem or "db"
    dest = backups_dir / f"{stem}.pre-{safe_tag}-{stamp}.db"
    conn = sqlite3.connect(str(db_path), timeout=DEFAULT_TIMEOUT)
    try:
        conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    return dest


def snapshot_db(label: str, *, db_path: str | Path = DEFAULT_DB, keep_last: int = 3) -> Path:
    """One call to (a) make a consistent VACUUM-INTO snapshot with the standard
    naming, (b) SELF-PROTECT it when label starts with 'pre-night' (so an
    overnight runner's pre-run snapshot lands in data/backups/protected.txt and
    can never age out), and (c) best-effort prune (newest keep_last per family +
    all protected, quarantine the rest, age out old files) via the optional
    tools/rotate_backups.py helper. Returns the snapshot Path. Never raises on
    a prune failure (the snapshot is what matters)."""
    dest = make_backup(label, db_path=db_path)
    backups_dir = Path(db_path).parent / "backups"
    if str(label).startswith("pre-night"):
        prot = backups_dir / "protected.txt"
        try:
            existing = set()
            if prot.exists():
                existing = {ln.strip() for ln in prot.read_text(encoding="utf-8").splitlines()
                            if ln.strip() and not ln.strip().startswith("#")}
            if dest.name not in existing:
                with prot.open("a", encoding="utf-8") as fh:
                    fh.write(dest.name + "\n")
        except OSError:
            pass
    try:  # best-effort prune; no-op when the rotation helper is absent
        import subprocess as _sp
        import sys as _sys
        rot = Path(str(_ROOT)) / "tools" / "rotate_backups.py"
        if rot.exists():
            _sp.run([_sys.executable, str(rot)], capture_output=True, timeout=600)
    except Exception:  # noqa: BLE001
        pass
    return dest


def immediate(conn, fn, tries: int = 6):
    """Run fn(conn) inside a BEGIN IMMEDIATE transaction and COMMIT, retrying on
    SQLITE_BUSY. The one commit-safety wrapper for every plan-state write group.
    A reached .close() after an uncommitted execute() silently loses the write;
    this helper makes the commit guaranteed.

    Returns fn(conn)'s return value. Raises RuntimeError after the retry budget.
    """
    for i in range(tries):
        try:
            conn.execute("BEGIN IMMEDIATE")
            out = fn(conn)
            conn.commit()
            return out
        except sqlite3.OperationalError:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("BEGIN IMMEDIATE retry budget exhausted")
