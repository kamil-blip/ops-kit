"""Verified-restore drill.

The highest-value durability gap in most personal systems: backups get made
nightly but a RESTORE is never once exercised, so "we have backups" stays an
untested claim. This drill actually restores the newest backup to a scratch
path and validates it:

  1. copy the newest ops.db backup to a scratch path (exercises the restore)
  2. open the COPY read-only
  3. PRAGMA integrity_check  -> must be 'ok'
  4. PRAGMA foreign_key_check -> must be empty
  5. row-count sanity vs live (key tables within a loose tolerance band,
     so a truncated / half-written backup is caught; a fresh empty install
     where both sides are 0 passes as consistent)

Emits a PASS/FAIL report record (appended to a JSON log + printed). READ-ONLY
with respect to the live DB: it only ever COPIES and READS backups, never
writes ops.db. Intended to run weekly via your scheduler.

Usage:
  python restore_drill.py                 # drill the newest backup
  python restore_drill.py --backup PATH   # drill a specific backup
  python restore_drill.py --root PATH     # repo root override (data/ under it)
  python restore_drill.py --json          # machine-readable result
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import datetime

import paths

# key tables sampled for the row-count sanity band
SANITY_TABLES = ["people", "emails", "observations", "entities", "edges"]
# loose band: a valid backup has at least half and at most 1.5x live rows
LOW_FRAC, HIGH_FRAC = 0.5, 1.5


def _resolve_paths(root: str | None = None) -> tuple[str, str, str]:
    """(live_db, backups_dir, report_path). Defaults come from paths.py
    (OPS_ROOT / OPS_DATA_DIR aware); --root overrides with <root>/data."""
    if root:
        data = os.path.join(os.path.abspath(root), "data")
        return (os.path.join(data, "ops.db"),
                os.path.join(data, "backups"),
                os.path.join(data, "plan-state", "restore_drill_report.json"))
    return (str(paths.DB_PATH),
            str(paths.BACKUPS_DIR),
            str(paths.PLAN_STATE_DIR / "restore_drill_report.json"))


def newest_backup(backups_dir: str) -> str | None:
    """Newest ops*.db under the backups dir, excluding quarantine subdirs and
    the live-name files."""
    cands = []
    for p in glob.glob(os.path.join(backups_dir, "ops*.db")):
        if "_quarantine" in p.replace("\\", "/"):
            continue
        if os.path.basename(p) == "ops.db":
            continue
        cands.append(p)
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def _counts(db_path: str) -> dict:
    """Read-only per-table row counts; all-None when the DB can't be opened."""
    uri = f"file:{db_path.replace(chr(92), '/')}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error:
        return {t: None for t in SANITY_TABLES}
    try:
        out = {}
        for t in SANITY_TABLES:
            try:
                out[t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                out[t] = None
        return out
    finally:
        con.close()


def run_drill(backup: str | None = None, root: str | None = None) -> dict:
    live_db, backups_dir, _report = _resolve_paths(root)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bak = backup or newest_backup(backups_dir)
    result = {"run_at": now, "backup": bak, "verdict": "FAIL", "checks": {}}
    if not bak or not os.path.exists(bak):
        result["checks"]["backup_found"] = False
        return result
    result["checks"]["backup_found"] = True
    result["backup_size"] = os.path.getsize(bak)

    scratch = os.path.join(tempfile.gettempdir(), "ops_restore_drill.db")
    if os.path.exists(scratch):
        os.remove(scratch)
    shutil.copy2(bak, scratch)  # <- the actual "restore"
    try:
        con = sqlite3.connect(f"file:{scratch.replace(chr(92), '/')}?mode=ro", uri=True, timeout=60)
        try:
            integ = con.execute("PRAGMA integrity_check").fetchone()[0]
            fk = con.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            con.close()
        result["checks"]["integrity_check"] = integ
        result["checks"]["fk_violations"] = len(fk)

        restored = _counts(scratch)
        live = _counts(live_db)
        rc = {}
        rowcount_ok = True
        for t in SANITY_TABLES:
            rv, lv = restored.get(t), live.get(t)
            # both-zero is consistent (fresh empty install); otherwise both
            # sides must be populated and inside the tolerance band, so a
            # truncated backup of a populated DB is caught.
            ok = (rv is not None and lv is not None
                  and ((rv == 0 and lv == 0)
                       or (rv > 0 and lv > 0 and LOW_FRAC * lv <= rv <= HIGH_FRAC * lv)))
            rc[t] = {"restored": rv, "live": lv, "ok": ok}
            rowcount_ok = rowcount_ok and ok
        result["checks"]["rowcounts"] = rc
        result["checks"]["rowcount_sanity"] = rowcount_ok

        result["verdict"] = "PASS" if (integ == "ok" and not fk and rowcount_ok) else "FAIL"
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass
    return result


def _append_report(result: dict, report_path: str) -> None:
    log = []
    if os.path.exists(report_path):
        try:
            log = json.load(open(report_path, encoding="utf-8"))
            if not isinstance(log, list):
                log = []
        except Exception:  # noqa: BLE001
            log = []
    log.append(result)
    log = log[-50:]  # keep the last 50 drills
    try:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=1)
    except Exception:  # noqa: BLE001
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="verified-restore drill")
    ap.add_argument("--backup", default=None)
    ap.add_argument("--root", default=None,
                    help="repo root override; live DB/backups/report resolve under <root>/data")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = run_drill(args.backup, root=args.root)
    _append_report(result, _resolve_paths(args.root)[2])
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        c = result["checks"]
        print(f"restore-drill {result['verdict']}: backup={os.path.basename(result.get('backup') or '?')}")
        print(f"  integrity_check : {c.get('integrity_check')}")
        print(f"  fk_violations   : {c.get('fk_violations')}")
        print(f"  rowcount_sanity : {c.get('rowcount_sanity')}")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
