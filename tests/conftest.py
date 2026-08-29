"""Shared fixtures: a temporary ops.db and vec.db built from the shipped schemas.

The kit resolves its database path once, at import time, from the OPS_DATA_DIR
environment variable (core/paths.py). So this file sets that variable to a
fresh temporary directory BEFORE any kit module is imported, then builds both
databases there with the same functions db/init_db.py uses. Every test in the
session runs against that pair of files; tests that need a pristine schema
build their own copy with `fresh_schema`.

The hooks directory is deliberately not put on sys.path: hooks/config.py and
core/config.py share a module name. Hook tests run the hooks as subprocesses,
which is how Claude Code runs them.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_DATA = Path(tempfile.mkdtemp(prefix="ops-kit-tests-"))

os.environ["OPS_ROOT"] = str(ROOT)
os.environ["OPS_DATA_DIR"] = str(_DATA)
os.environ["PYTHONIOENCODING"] = "utf-8"

for _d in ("db", "core", "tools", "comms", "search", "tasks", "learning", "autonomy"):
    p = str(ROOT / _d)
    if p not in sys.path:
        sys.path.insert(0, p)

import init_db  # noqa: E402  (db/init_db.py)

SCHEMA = ROOT / "db" / "schema.sql"
VEC_SCHEMA = ROOT / "db" / "vec_schema.sql"


def build_databases(data_dir: Path) -> tuple[Path, Path]:
    """Create ops.db and vec.db in data_dir from the shipped schemas."""
    data_dir.mkdir(parents=True, exist_ok=True)
    ops, vec = data_dir / "ops.db", data_dir / "vec.db"
    for p in (ops, vec):
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(p) + suffix)
            if f.exists():
                f.unlink()
    conn = init_db.apply_sql(str(ops), str(SCHEMA))
    conn.close()
    conn = init_db.apply_sql(str(vec), str(VEC_SCHEMA), need_vec=True)
    conn.close()
    return ops, vec


OPS_DB, VEC_DB = build_databases(_DATA)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_DATA, ignore_errors=True)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return _DATA


@pytest.fixture(scope="session")
def db_path() -> Path:
    return OPS_DB


@pytest.fixture
def conn(db_path):
    """Plain sqlite3 connection to the session database (row_factory=Row)."""
    c = sqlite3.connect(str(db_path), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    yield c
    c.close()


@pytest.fixture
def fresh_schema(tmp_path):
    """A brand-new ops.db built from db/schema.sql in an isolated directory."""
    ops = tmp_path / "ops.db"
    c = init_db.apply_sql(str(ops), str(SCHEMA))
    yield c
    c.close()


@pytest.fixture(scope="session")
def env() -> dict:
    """Environment for subprocesses: the same data dir the tests use."""
    e = dict(os.environ)
    e["OPS_ROOT"] = str(ROOT)
    e["OPS_DATA_DIR"] = str(_DATA)
    e["PYTHONIOENCODING"] = "utf-8"
    return e


@pytest.fixture(scope="session")
def py() -> str:
    return sys.executable
