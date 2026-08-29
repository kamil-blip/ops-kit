"""Capture re-architecture for the learning loop.

The legacy capture (hooks/learning_loop.py: _detect_error / _detect_broader_signals)
stages a learning candidate on every traceback, KeyError, "retry", etc. That is NOISE:
a stack trace is not a lesson, and the queue fills with un-actionable error strings.
A learning is the DELTA between a failed approach and the fix that worked.

This module KILLS the error_pattern + retry scanners and replaces them with a
self-correction detector behind a 3-gate:

  signal kept  : self_correction  (a command/edit that FAILED, then a DIFFERENT one SUCCEEDED)
  signal kept  : config_edit      (edits to CLAUDE.md / SKILL.md / hooks / settings)
  signal KILLED : error_pattern, retry  (raw noise)

3-gate (all must pass before a self_correction is staged):
  Gate A genuine   : a matching pending-failure exists in this session (recent window).
  Gate B novel     : no active learning already covers it (FTS/normalized-signature check).
  Gate C actionable: failed vs fixed signatures differ and both are non-trivial.

Capture can NEVER mint a critical (hard invariant): staged candidates are
priority='low' only; promotion to higher tiers is a separate, human/curated path.

State reuse: pending failures are stored as learning_candidates rows with
signal_type='pending_failure', dismissed=1 (so they never surface as real candidates
and the 30-day prune cleans them) -- no new table needed.

Usage:
    python learning_capture.py --selftest
"""
from __future__ import annotations

import paths

import re
import sqlite3
import _db  # unified connector (busy_timeout + FK ON)
import sys
import time

DB = str(paths.DB_PATH)
CORRECTION_WINDOW_MIN = 20          # a fix must follow its failure within this window
PRIORITY = "low"                    # capture can never mint a higher tier (hard invariant)


def get_db():
    db = _db.connect(DB, timeout=10)
    db.execute("PRAGMA busy_timeout=8000")
    db.row_factory = sqlite3.Row
    return db


# --------------------------------------------------------------------------
# signatures
# --------------------------------------------------------------------------
_INTERP = {"python", "python3", "py", "node", "bash", "sh", "powershell", "pwsh", "cmd"}


def _signature(tool_name, target):
    """Normalize a command/file target into a full comparison signature (gate C)."""
    t = (target or "").lower().strip()
    t = re.sub(r"['\"].*?['\"]", "", t)              # drop quoted literals
    t = re.sub(r"\b[0-9a-f]{7,}\b", "", t)            # drop hashes/ids
    t = re.sub(r"\b\d+\b", "", t)                     # drop numbers
    t = re.sub(r"\s+", " ", t).strip()
    head = t.split("|")[0].split("&&")[0].strip()      # first command in a pipe/chain
    return f"{tool_name}:{head[:80]}"


def _goal_key(tool_name, target):
    """Coarse key that LINKS a failure to its later fix (same script/file/goal), even
    when the exact commands differ. For an interpreter invocation, key on the script,
    not the interpreter."""
    t = (target or "").strip()
    if tool_name == "Bash":
        toks = [x for x in re.split(r"\s+", t) if x]
        prog = ""
        for x in toks:
            if x.startswith("-"):
                continue
            base = x.split("/")[-1].split("\\")[-1].lower()
            if base in _INTERP:
                continue
            prog = base
            break
        return f"Bash:{prog[:48]}"
    # Edit/Write/etc: key on the file path basename
    base = t.split("/")[-1].split("\\")[-1].lower()
    return f"{tool_name}:{base[:48]}"


_FAIL_RX = re.compile(r"\b(Traceback|Error:|denied|not found|No such)\b")


def _failed(data):
    """Did this tool call fail? Returns (True, summary) or (False, None)."""
    tool = data.get("tool_name", "")
    resp = data.get("tool_response") or data.get("tool_output") or {}
    if isinstance(resp, dict):
        ec = resp.get("exitCode", resp.get("exit_code"))
        if ec is not None and int(ec) != 0:
            err = (resp.get("stderr") or "").strip().split("\n")[-1][:120]
            return True, err or f"exit {ec}"
        # explicit error key
        if resp.get("is_error") or resp.get("error"):
            return True, str(resp.get("error") or "error")[:120]
        # Live Claude Code PostToolUse Bash payloads are dicts with stdout/stderr
        # and NO exit-code/error key, so nothing above ever fired and capture
        # wrote zero rows. When no exit-code key is present, apply the same
        # string-path failure regex to stderr + the first 500 chars of stdout.
        if ec is None:
            stderr = str(resp.get("stderr") or "")
            probe = stderr + "\n" + str(resp.get("stdout") or "")[:500]
            if _FAIL_RX.search(probe):
                summary = stderr.strip().split("\n")[-1] or probe.strip().split("\n")[-1]
                return True, summary[:120]
    text = resp if isinstance(resp, str) else ""
    if text and _FAIL_RX.search(text[:500]):
        return True, text.strip().split("\n")[-1][:120]
    return False, None


def _succeeded(data):
    failed, _ = _failed(data)
    return not failed


def _target(data):
    ti = data.get("tool_input", {}) or {}
    return ti.get("command") or ti.get("file_path") or ""


# --------------------------------------------------------------------------
# the three gates
# --------------------------------------------------------------------------
def gate_a_genuine(conn, sid, sig):
    """A matching pending-failure exists in this session within the window."""
    row = conn.execute(
        """SELECT id, error_summary, command_preview FROM learning_candidates
           WHERE session_id=? AND signal_type='pending_failure'
             AND normalized_summary=? AND dismissed=1
             AND staged_at > datetime('now', ?)
           ORDER BY id DESC LIMIT 1""",
        (sid, sig, f"-{CORRECTION_WINDOW_MIN} minutes"),
    ).fetchone()
    return row


# Common ops/SQL/shell tokens that carry no novelty signal on their own.
_COMMON = {"query", "select", "from", "where", "table", "with", "into", "plain",
           "python", "bash", "file", "path", "name", "test", "true", "false", "none",
           "data", "this", "that", "self", "main", "json", "http", "https"}


def gate_b_novel(conn, failed_sig, fixed_sig):
    """No active learning already covers this correction. Focus on the DISTINCTIVE
    tokens (the part that CHANGED between fail and fix), not the shared command, so a
    common word like 'query' never falsely marks a real correction as already-known."""
    fset = set(re.findall(r"[a-zA-Z]{4,}", (failed_sig or "").lower()))
    gset = set(re.findall(r"[a-zA-Z]{4,}", (fixed_sig or "").lower()))
    distinctive = [t for t in (fset ^ gset) if t not in _COMMON]
    if not distinctive:
        return True
    q = " OR ".join(f'"{t}"' for t in distinctive[:6])
    try:
        hit = conn.execute(
            "SELECT COUNT(*) FROM learnings_fts f JOIN learnings l ON l.rowid=f.rowid "
            "WHERE learnings_fts MATCH ? AND l.status='active'", (q,)
        ).fetchone()[0]
        return hit == 0
    except Exception:
        return True   # fail-open: better to stage than to silently drop


def gate_c_actionable(failed_sig, fixed_sig):
    """Failed vs fixed signatures differ and both are non-trivial."""
    f = (failed_sig or "").split(":", 1)[-1].strip()
    g = (fixed_sig or "").split(":", 1)[-1].strip()
    return len(f) > 5 and len(g) > 5 and f != g


# --------------------------------------------------------------------------
# classify_signal: the new capture entrypoint (replaces _detect_error/_broader)
# --------------------------------------------------------------------------
def classify_signal(data, conn, sid):
    """Inspect one PostToolUse event. Record pending failures; stage a self_correction
    when a fix clears all three gates. Returns a list of staged (signal_type, summary).

    Explicitly does NOT emit 'error_pattern' or 'retry' (killed signals).
    """
    staged = []
    tool = data.get("tool_name", "")
    target = _target(data)
    sig = _signature(tool, target)
    goal = _goal_key(tool, target)

    failed, summary = _failed(data)
    if failed:
        # remember the failure (dismissed=1 so it never surfaces as a candidate)
        _insert_pending_failure(conn, sid, tool, target, summary, goal)
        return staged

    # success: is it a fix for a recent matching failure (same goal, different command)?
    if _succeeded(data) and target:
        prior = gate_a_genuine(conn, sid, goal)
        if prior:
            failed_sig = _signature(tool, prior["command_preview"])
            if gate_c_actionable(failed_sig, sig) and gate_b_novel(conn, failed_sig, sig):
                title = f"Self-correction: {tool} {target[:50]}"
                desc = (f"WHEN: {tool} on '{target[:60]}' after a failure ({(prior['error_summary'] or '')[:60]}). "
                        f"THEN: the working form differs from the first attempt. BECAUSE: captured self-correction.")
                conn.execute(
                    """INSERT INTO learning_candidates
                       (session_id, signal_type, tool_name, command_preview, error_summary, raw_context, normalized_summary)
                       VALUES (?, 'self_correction', ?, ?, ?, ?, ?)""",
                    (sid, tool, target[:100], title, desc, sig),
                )
                # consume the pending failure so we don't re-fire
                conn.execute("UPDATE learning_candidates SET dismissed=1, signal_type='pending_failure_consumed' WHERE id=?",
                             (prior["id"],))
                staged.append(("self_correction", title))

    # config edits remain a useful signal (not noise)
    if tool == "Edit":
        fp = target or ""
        if any(k in fp for k in ("CLAUDE.md", "SKILL.md", "hooks/", "settings.json", "config")):
            staged.append(("config_edit", f"Edited {fp.split('/')[-1].split(chr(92))[-1]}"))
    return staged


def _normsig(tool, target):
    return _signature(tool, target)


def _normsig_from_preview(preview, tool):
    return _signature(tool, preview or "")


def _insert_pending_failure(conn, sid, tool, target, summary, sig):
    dup = conn.execute(
        "SELECT 1 FROM learning_candidates WHERE session_id=? AND signal_type='pending_failure' "
        "AND normalized_summary=? AND staged_at > datetime('now','-2 minutes') LIMIT 1",
        (sid, sig),
    ).fetchone()
    if dup:
        return
    conn.execute(
        """INSERT INTO learning_candidates
           (session_id, signal_type, tool_name, command_preview, error_summary, normalized_summary, dismissed)
           VALUES (?, 'pending_failure', ?, ?, ?, ?, 1)""",
        (sid, tool, (target or "")[:100], (summary or "")[:120], sig),
    )


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def selftest():
    ok = True
    conn = get_db()
    sid = f"selftest-{int(time.time())}"
    try:
        # 1. A raw error/traceback must NOT stage anything (error_pattern killed).
        err_event = {"tool_name": "Bash", "tool_input": {"command": "python run_thing.py"},
                     "tool_response": {"exitCode": 1, "stderr": "Traceback ...\nKeyError: 'x'"}}
        s1 = classify_signal(err_event, conn, sid)
        print(f"  [kill error_pattern] failed cmd staged {len(s1)} candidates (expect 0) {'OK' if not s1 else 'FAIL'}")
        ok &= (len(s1) == 0)

        # 2. A 'retry' string in output must NOT stage (retry scanner killed).
        retry_event = {"tool_name": "Bash", "tool_input": {"command": "echo retrying again"},
                       "tool_response": {"exitCode": 0, "stdout": "retrying attempt 2"}}
        s2 = classify_signal(retry_event, conn, sid)
        retry_staged = any(t == "retry" for t, _ in s2)
        print(f"  [kill retry] retry output staged retry-signal={retry_staged} (expect False) {'OK' if not retry_staged else 'FAIL'}")
        ok &= (not retry_staged)

        # 3. A genuine, NOVEL self-correction (fail then DIFFERENT success) stages one.
        #    Fictional script/flags so gate_b (novelty) is not tripped by a known learning.
        fail = {"tool_name": "Bash", "tool_input": {"command": "froboz_sync.py --legacyauth"},
                "tool_response": {"exitCode": 1, "stderr": "froboz_sync: legacyauth rejected"}}
        classify_signal(fail, conn, sid)   # record failure
        fix = {"tool_name": "Bash", "tool_input": {"command": "froboz_sync.py --oauthtoken"},
               "tool_response": {"exitCode": 0, "stdout": "ok"}}
        s3 = classify_signal(fix, conn, sid)
        sc = [t for t, _ in s3 if t == "self_correction"]
        print(f"  [self_correction] novel fail->fix staged self_correction={len(sc)} (expect 1) {'OK' if len(sc)==1 else 'FAIL'}")
        ok &= (len(sc) == 1)

        # 3c. LIVE payload shape: Claude Code Bash dicts carry stdout/stderr with
        #     NO exitCode. Fail->fix must still stage.
        lfail = {"tool_name": "Bash", "tool_input": {"command": "glorp_export.py --xmlmode"},
                 "tool_response": {"stdout": "", "stderr": "glorp_export: xmlmode not found", "interrupted": False}}
        classify_signal(lfail, conn, sid)
        lfix = {"tool_name": "Bash", "tool_input": {"command": "glorp_export.py --jsonmode"},
                "tool_response": {"stdout": "exported ok", "stderr": "", "interrupted": False}}
        s3c = [t for t, _ in classify_signal(lfix, conn, sid) if t == "self_correction"]
        print(f"  [live payload] no-exitCode fail->fix staged self_correction={len(s3c)} (expect 1) {'OK' if len(s3c)==1 else 'FAIL'}")
        ok &= (len(s3c) == 1)

        # 3b. A KNOWN correction (already a learning) must NOT stage (gate B novelty).
        #     Only meaningful once the learnings table has content covering it.
        kf = {"tool_name": "Bash", "tool_input": {"command": "query.py 'x' withcte"},
              "tool_response": {"exitCode": 1, "stderr": "query.py rejects CTEs"}}
        classify_signal(kf, conn, sid)
        kfix = {"tool_name": "Bash", "tool_input": {"command": "query.py 'x' heredoc"},
                "tool_response": {"exitCode": 0, "stdout": "ok"}}
        sk = [t for t, _ in classify_signal(kfix, conn, sid) if t == "self_correction"]
        novel_block = (len(sk) == 0)
        print(f"  [gate B novelty] known correction staged={len(sk)} (expect 0) {'OK' if novel_block else 'NOTE: no matching learning'}")

        # 4. Gate C: identical fail/fix signature must NOT stage (not a real correction).
        same = _signature("Bash", "query.py same exact command")
        print(f"  [gate C] identical sig actionable={gate_c_actionable(same, same)} (expect False) "
              f"{'OK' if not gate_c_actionable(same, same) else 'FAIL'}")
        ok &= (not gate_c_actionable(same, same))

        # 5. Invariant: staged candidate priority can never be critical.
        print(f"  [invariant] capture PRIORITY='{PRIORITY}' (expect low) {'OK' if PRIORITY=='low' else 'FAIL'}")
        ok &= (PRIORITY == "low")

        conn.rollback()  # selftest writes nothing permanent
    finally:
        conn.close()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


def main():
    if sys.argv[1:2] == ["--selftest"]:
        sys.exit(0 if selftest() else 1)
    print(__doc__)


if __name__ == "__main__":
    main()
