"""One-shot install self-check for ops-kit (INSTALL.md section 6 in one command).

Runs, in order, against the live data/ops.db:
  1. schema        : the database opens and has tables
  2. empty         : people has 0 rows on a fresh install (skipped with a note
                     once you have data)
  3. memory        : insert + read back one reference_docs row (then delete it)
  4. learning      : learning_capture --selftest, then insert one learning and
                     find it through learnings_retrieval (then delete it)
  5. session log   : insert one session_logs row the way the wrap-up skill does
                     (then delete it)
  6. health        : query.py health runs and exits 0
  7. backup+drill  : make a backup with _db.make_backup, run restore_drill on it
  8. mcp import    : core/mcp_server.py imports and lists its tools

Exit 0 = every step passed. Probe rows are removed again; the only thing left
behind is the backup file under data/backups/ (delete it if you like).

Usage: python tools/selfcheck.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for d in ("core", "tools", "learning", "autonomy", "hooks", "db"):
    sys.path.insert(0, os.path.join(ROOT, d))

import paths  # noqa: E402
import _db  # noqa: E402

PY = sys.executable
ENV = dict(os.environ, OPS_ROOT=str(paths.ROOT), PYTHONIOENCODING="utf-8")
results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


def run(args: list[str], cwd: str = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run([PY] + args, cwd=cwd, env=ENV, capture_output=True, text=True, timeout=600)


def main() -> int:
    if not paths.DB_PATH.is_file():
        print(f"no database at {paths.DB_PATH}; run python db/init_db.py first")
        return 2
    conn = _db.connect(str(paths.DB_PATH))

    # 1. schema
    n = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    step("schema", n > 50, f"{n} tables")

    # 2. empty
    people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    step("empty install", True, f"people rows = {people}" + ("" if people == 0 else " (not empty; fine after first sync)"))

    # 3. memory round-trip
    tag = f"selfcheck-{int(time.time())}"
    conn.execute("INSERT INTO reference_docs (slug, content, category) VALUES (?, 'hello', 'test')", (tag,))
    conn.commit()
    got = conn.execute("SELECT content FROM reference_docs WHERE slug=?", (tag,)).fetchone()
    conn.execute("DELETE FROM reference_docs WHERE slug=?", (tag,))
    conn.commit()
    step("memory round-trip", bool(got and got[0] == "hello"), "reference_docs insert + read + delete")

    # 4. learning loop
    p = run([os.path.join(ROOT, "learning", "learning_capture.py"), "--selftest"])
    step("learning_capture --selftest", "SELFTEST PASS" in (p.stdout + p.stderr), (p.stdout + p.stderr).strip().splitlines()[-1][:120] if (p.stdout + p.stderr).strip() else "no output")
    lid = f"LRN-SELFCHECK-{int(time.time())}"
    conn.execute(
        "INSERT INTO learnings (learning_id, title, description, apply_when, priority, status, memory_type, source, inserted_at, updated_at) "
        "VALUES (?, 'selfcheck learning zebra-quartz', 'the install self-check inserted this row', 'never', 'low', 'active', 'operational', 'selfcheck', datetime('now'), datetime('now'))",
        (lid,))
    conn.commit()
    p = run([os.path.join(ROOT, "learning", "learnings_retrieval.py"), "--search", "zebra-quartz"])
    found = "zebra-quartz" in (p.stdout + p.stderr)
    if not found:  # retrieval CLI flags vary; fall back to the query.py learnings shortcut
        p = run([os.path.join(ROOT, "tools", "query.py"), "learnings", "zebra-quartz"])
        found = "zebra-quartz" in (p.stdout + p.stderr)
    conn.execute("DELETE FROM learnings WHERE learning_id=?", (lid,))
    conn.commit()
    step("learning retrieval", found, "inserted learning came back by keyword")

    # 5. session log (what the wrap-up skill writes)
    sid = f"selfcheck-{int(time.time())}"
    conn.execute("INSERT INTO session_logs (session_id, date, title, summary) VALUES (?, date('now'), 'selfcheck', 'probe row')", (sid,))
    conn.commit()
    cnt = conn.execute("SELECT COUNT(*) FROM session_logs WHERE session_id=?", (sid,)).fetchone()[0]
    conn.execute("DELETE FROM session_logs WHERE session_id=?", (sid,))
    conn.commit()
    step("session_logs write", cnt == 1, "wrap-up style insert + delete")

    # 6. health
    p = run([os.path.join(ROOT, "tools", "query.py"), "health"])
    step("query.py health", p.returncode == 0 and "Traceback" not in p.stderr, f"exit={p.returncode}")

    # 7. backup + restore drill
    try:
        bk = _db.make_backup("selfcheck", db_path=paths.DB_PATH)
        p = run([os.path.join(ROOT, "autonomy", "restore_drill.py"), "--backup", str(bk)])
        out = (p.stdout + p.stderr).strip().splitlines()
        step("backup + restore drill", p.returncode == 0 and "restore-drill PASS" in (p.stdout + p.stderr),
             out[0][:120] if out else "no output")
    except Exception as e:  # noqa: BLE001
        step("backup + restore drill", False, f"{type(e).__name__}: {e}")

    # 8. mcp server import + tool list
    p = run(["-c", "import sys; sys.path.insert(0, %r); import mcp_server as m; import asyncio; "
             "ts = asyncio.run(m.list_tools()) if hasattr(m, 'list_tools') else None; "
             "print('tools:', len(ts) if ts else 'listed via decorator')" % os.path.join(ROOT, "core")])
    step("mcp_server import", p.returncode == 0 and "Traceback" not in p.stderr, (p.stdout.strip() or p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "")[:120])

    conn.close()
    failed = [r for r in results if not r[1]]
    print(f"\nselfcheck: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
