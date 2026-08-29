"""Render the always-on (Tier A) learnings into CLAUDE.local.md.

The load-bearing rules (learnings.residency='graduated') are written into an
auto-managed, marker-delimited block in <repo-root>/CLAUDE.local.md -- a
workspace file that is auto-loaded into context every turn and is gitignored,
so generated content never pollutes git history and never touches the
hand-curated CLAUDE.md.

Membership is set by the residency flag (curated), NOT by this script:
    UPDATE learnings SET residency='graduated' WHERE id=...   # promote
    UPDATE learnings SET residency='demoted'   WHERE id=...   # remove
Re-run this after any membership change (or on a weekly cron) to regenerate.

Usage:
    python graduated_rules.py            # regenerate the block in CLAUDE.local.md (backup first)
    python graduated_rules.py --dry-run  # print the block, write nothing
    python graduated_rules.py --stats    # show graduated count + current block state
"""
import paths
import datetime
import os
import re
import shutil
import _db  # unified connector (busy_timeout + FK ON)
import sys

DB = str(paths.DB_PATH)
TARGET = str(paths.ROOT / "CLAUDE.local.md")
BEGIN = "<!-- BEGIN-GRADUATED-RULES auto-generated; edit membership via learnings.residency, not here -->"
END = "<!-- END-GRADUATED-RULES -->"
MAX_LINE = 200  # keep each rule to one tight line


def _clean(title):
    t = " ".join((title or "").split()).strip().rstrip(".")
    if t:
        t = t[0].upper() + t[1:]
    return (t[:MAX_LINE] + ".") if t else ""


def _rule_lines(text):
    """The id-tagged rule lines only -- used to detect real changes, ignoring the
    header timestamp so a periodic re-run doesn't churn the file every tick."""
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("<!-- L:")]


def fetch():
    c = _db.connect(DB, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    rows = c.execute("""
        SELECT id, learning_id, priority, title
        FROM learnings
        WHERE status='active' AND residency='graduated'
        ORDER BY CASE LOWER(priority) WHEN 'critical' THEN 0 WHEN 'p0' THEN 1
                 WHEN 'p1' THEN 2 ELSE 3 END, id
    """).fetchall()
    c.close()
    return rows


def render(rows):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [BEGIN]
    out.append(f"## Always-On Learnings ({len(rows)} load-bearing rules, auto-generated {ts})")
    out.append("Surfaced every turn because skipping them causes real errors regardless of task. "
               "Do not hand-edit; change membership via `learnings.residency` then re-run `graduated_rules.py`.")
    for lid, learning_id, prio, title in rows:
        out.append(f"<!-- L:{lid} --> - {_clean(title)}")
    out.append(END)
    return "\n".join(out)


def apply(block, dry_run=False):
    existing = ""
    if os.path.exists(TARGET):
        with open(TARGET, "r", encoding="utf-8") as f:
            existing = f.read()

    pat = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    if pat.search(existing):
        new = pat.sub(lambda _: block, existing)
    elif existing.strip():
        new = existing.rstrip() + "\n\n" + block + "\n"
    else:
        new = block + "\n"

    if dry_run:
        print(block)
        print(f"\n[dry-run] would write {len(new)} bytes to {TARGET}")
        return False

    m = pat.search(existing)
    if m and _rule_lines(m.group(0)) == _rule_lines(block):
        print("Graduated-rules unchanged; no write.")
        return False

    if os.path.exists(TARGET):
        shutil.copy(TARGET, TARGET + ".bak")
    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"Wrote {len(new)} bytes to {TARGET} (backup: CLAUDE.local.md.bak)")
    return True


def regenerate(quiet=True):
    """Importable, exception-safe entry point for periodic maintenance.

    Writes only when the graduated set changed (skip-if-unchanged), so it can fire
    on every SessionStart-decay tick without churning CLAUDE.local.md or its .bak.
    """
    try:
        rows = fetch()
        if not rows:
            return False
        if quiet:
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                return apply(render(rows), dry_run=False)
        return apply(render(rows), dry_run=False)
    except Exception:
        return False


def stats():
    rows = fetch()
    print(f"graduated (always-on): {len(rows)}")
    has = False
    if os.path.exists(TARGET):
        with open(TARGET, encoding="utf-8") as f:
            has = BEGIN in f.read()
    print(f"block present in CLAUDE.local.md: {has}")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--stats":
        stats()
        return
    # GraduatedRules liveness heartbeat (fail-open when job_heartbeat is absent)
    try:
        from job_heartbeat import heartbeat as _heartbeat
    except Exception:
        import contextlib as _cl
        def _heartbeat(job):
            return _cl.nullcontext(type("_HB", (), {"rows_touched": 0, "exit_note": None})())
    with _heartbeat("GraduatedRules") as hb:
        rows = fetch()
        if not rows:
            print("No graduated learnings (residency='graduated'). Nothing to render.")
            hb.exit_note = "ok: no graduated rows"
            return
        apply(render(rows), dry_run=(arg == "--dry-run"))
        hb.rows_touched = len(rows)


if __name__ == "__main__":
    main()
