"""Continuous Learning Extraction: shared module for three hooks.

Imported by:
- state_tracker.py (PostToolUse) -> stage_signal()
- context_injector.py (UserPromptSubmit) -> surface_learnings()
- session_lifecycle.py (PreCompact) -> anchor_candidates()

Never runs standalone. All functions are best-effort (never crash the caller).
"""
import os
import re
import sys
from datetime import datetime, timezone

_hooks_dir = os.path.dirname(os.path.abspath(__file__))
if not sys.path or sys.path[0] != _hooks_dir:
    # Front-insert so hooks/config.py wins over core/config.py when this
    # module is imported in-process (script execution already has it first).
    sys.path.insert(0, _hooks_dir)

from config import get_conn, get_session_id


# ---------------------------------------------------------------------------
# Operator identity for auto-edge extraction (config-driven, empty default).
#
# The graph edges written by extract_edges() need a source node for "the
# operator". Set it in config.toml at the repo root:
#
#   [identity]
#   # FICTIONAL example:
#   # operator_entity_id = "person-1"
#
# or via env var OPS_OPERATOR_ENTITY_ID. When unset, edge extraction is
# skipped (nothing else in this module depends on it).
# ---------------------------------------------------------------------------
def _operator_entity_id():
    val = os.environ.get("OPS_OPERATOR_ENTITY_ID", "").strip()
    if val:
        return val
    try:
        import tomllib
        cfg_path = os.path.join(os.path.dirname(_hooks_dir), "config.toml")
        if os.path.exists(cfg_path):
            with open(cfg_path, "rb") as f:
                cfg = tomllib.load(f)
            return str((cfg.get("identity", {}) or {}).get("operator_entity_id", "") or "").strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Normalization for fuzzy candidate grouping
# ---------------------------------------------------------------------------
def _normalize_error(text):
    """Normalize error_summary for fuzzy grouping across sessions."""
    if not text:
        return ""
    t = text.lower().strip()
    t = re.sub(r"['\"].*?['\"]", "''", t)           # collapse quoted strings
    t = re.sub(r"/[\w./\\-]+", "<path>", t)          # collapse Unix paths
    t = re.sub(r"[a-z]:\\[\w.\\-]+", "<path>", t)     # collapse Windows paths
    t = re.sub(r"\b\d+\b", "<n>", t)                 # collapse numbers
    t = re.sub(r"\s+", " ", t)                        # normalize whitespace
    return t[:120]


# ---------------------------------------------------------------------------
# Table setup (lazy, once per process)
# ---------------------------------------------------------------------------
_table_ready = False


def _ensure_table():
    global _table_ready
    if _table_ready:
        return
    try:
        conn = get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS learning_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                tool_name TEXT,
                command_preview TEXT,
                error_summary TEXT,
                raw_context TEXT,
                staged_at TEXT DEFAULT (datetime('now')),
                promoted_to INTEGER REFERENCES learnings(id),
                dismissed INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_lc_session
            ON learning_candidates(session_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_lc_pending
            ON learning_candidates(promoted_to, dismissed)
            WHERE promoted_to IS NULL AND dismissed = 0
        """)
        # Add normalized_summary column if missing (idempotent)
        try:
            conn.execute("ALTER TABLE learning_candidates ADD COLUMN normalized_summary TEXT")
            # Backfill only runs once (when column is first added)
            rows = conn.execute(
                "SELECT id, error_summary FROM learning_candidates WHERE normalized_summary IS NULL"
            ).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE learning_candidates SET normalized_summary = ? WHERE id = ?",
                    (_normalize_error(r["error_summary"]), r["id"])
                )
        except Exception:
            pass  # column already exists, no backfill needed
        # Prune expired candidates (>30 days, not promoted).
        # Extended from 7 to 30 days so the async daemon has time to drain
        # the queue between sessions.
        conn.execute("""
            DELETE FROM learning_candidates
            WHERE promoted_to IS NULL AND dismissed = 0
            AND staged_at < datetime('now', '-30 days')
        """)
        conn.commit()
        conn.close()
        _table_ready = True
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Error detection patterns
# ---------------------------------------------------------------------------
_ERROR_PATTERNS = [
    re.compile(r'\btraceback\b', re.IGNORECASE),
    re.compile(r'\bpermission denied\b', re.IGNORECASE),
    re.compile(r'\bno such file or directory\b', re.IGNORECASE),
    re.compile(r'\bconnection refused\b', re.IGNORECASE),
    re.compile(r'\bmodule\S*\s+not found\b', re.IGNORECASE),
    re.compile(r'\bsyntax ?error\b', re.IGNORECASE),
    re.compile(r'\bcommand not found\b', re.IGNORECASE),
    re.compile(r'\bENOENT\b'),
    re.compile(r'\bEACCES\b'),
    re.compile(r'\bOperationalError\b'),
    re.compile(r'\bKeyError\b'),
    re.compile(r'\bTypeError\b'),
    re.compile(r'\bValueError\b'),
    re.compile(r'\bAttributeError\b'),
    re.compile(r'\bImportError\b'),
    re.compile(r'\bFileNotFoundError\b'),
]

# Patterns that look like errors but aren't
_FALSE_POSITIVE_PATTERNS = [
    re.compile(r'\b0 errors?\b', re.IGNORECASE),
    re.compile(r'\bno errors?\b', re.IGNORECASE),
    re.compile(r'\berrors?[_.]', re.IGNORECASE),  # error_handler, errors.py
    re.compile(r'["\']error["\']', re.IGNORECASE),  # string literal "error"
    re.compile(r'\berror[A-Z]'),  # camelCase like errorMessage
    re.compile(r'HOOK ERROR', re.IGNORECASE),  # our own hooks' error output
]


def _get_text(data):
    """Extract readable text from tool output (mirrors state_tracker._get_text)."""
    out = data.get("tool_response") or data.get("tool_output", {})
    if isinstance(out, str):
        return out
    if isinstance(out, dict):
        parts = []
        for key in ("stdout", "stderr", "content"):
            val = out.get(key, "")
            if val:
                parts.append(val)
        return "\n".join(parts) if parts else str(out)
    return str(out) if out else ""


def _detect_error(data):
    """Returns (signal_type, error_summary) or (None, None)."""
    tool_name = data.get("tool_name", "")
    tool_response = data.get("tool_response") or data.get("tool_output", {})
    text = _get_text(data)

    if not text or len(text) < 10:
        return None, None

    # Check exit code for Bash (dict-style output)
    if tool_name == "Bash" and isinstance(tool_response, dict):
        exit_code = tool_response.get("exitCode") or tool_response.get("exit_code")
        if exit_code and int(exit_code) != 0:
            stderr = tool_response.get("stderr", "")
            summary = stderr.strip().split("\n")[-1][:80] if stderr else text.strip().split("\n")[-1][:80]
            return "error", summary

    # Scan first 2000 chars for error patterns
    scan_text = text[:2000]

    # Check for false positives first
    for fp in _FALSE_POSITIVE_PATTERNS:
        if fp.search(scan_text):
            return None, None

    # Check real error patterns
    for pattern in _ERROR_PATTERNS:
        match = pattern.search(scan_text)
        if match:
            # For Python tracebacks, the last line has the actual error
            lines = scan_text.strip().split("\n")
            last_line = lines[-1].strip()[:80] if lines else ""
            # Use last line if it looks like an exception, otherwise use matched line
            if re.match(r'^[\w.]*[A-Z]\w*(Error|Exception|Warning)', last_line):
                summary = last_line
            else:
                start = max(0, match.start() - 20)
                end = min(len(scan_text), match.end() + 60)
                summary = scan_text[start:end].strip().split("\n")[0][:80]
            return "error_pattern", summary

    return None, None


# ---------------------------------------------------------------------------
# Function 1: stage_signal (called from state_tracker PostToolUse)
# ---------------------------------------------------------------------------
def _stage_one(conn, sid, signal_type, tool_name, command_preview, error_summary, raw_context):
    """Insert a single learning candidate with dedup."""
    existing = conn.execute("""
        SELECT 1 FROM learning_candidates
        WHERE session_id = ? AND tool_name = ? AND error_summary = ?
        AND staged_at > datetime('now', '-5 minutes')
        LIMIT 1
    """, (sid, tool_name, error_summary)).fetchone()
    if existing:
        return
    conn.execute("""
        INSERT INTO learning_candidates
        (session_id, signal_type, tool_name, command_preview, error_summary, raw_context, normalized_summary)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (sid, signal_type, tool_name, command_preview, error_summary, raw_context, _normalize_error(error_summary)))


# ---------------------------------------------------------------------------
# Broader signal patterns (B): decisions, retries, file edits
# ---------------------------------------------------------------------------
_DECISION_PATTERNS = [
    re.compile(r'\b(chose|decided|switching to|going with|approved|confirmed)\b', re.IGNORECASE),
]

_RETRY_PATTERN = re.compile(r'\b(retry|retrying|trying again|attempt \d)\b', re.IGNORECASE)


def _detect_broader_signals(data):
    """Detect non-error signals: decisions, retries, key file edits."""
    signals = []
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    text = _get_text(data)[:2000]

    # Retry detection (indicates a problem worth learning from)
    if _RETRY_PATTERN.search(text):
        summary = _RETRY_PATTERN.search(text).group(0)
        signals.append(("retry", summary))

    # Key file edits (CLAUDE.md, skills, hooks, config)
    if tool_name == "Edit":
        fp = tool_input.get("file_path", "")
        if any(k in fp for k in ("CLAUDE.md", "SKILL.md", "hooks/", "config", "settings.json")):
            signals.append(("config_edit", f"Edited {os.path.basename(fp)}"))

    return signals


def stage_signal(data):
    """Stage high-signal learning candidates from tool output.

    Capture re-architecture: the error_pattern + retry scanners are RETIRED
    (a stack trace is not a lesson; raw error strings flooded the queue).
    Capture is now learning_capture.classify_signal -- a self_correction (a
    failed approach followed by a DIFFERENT working one, behind a 3-gate:
    genuine / novel / actionable) or a config_edit. Capture can never mint a
    critical (staged priority='low'). If the capture module is unavailable,
    this call stages nothing rather than reintroducing noise.
    """
    _ensure_table()
    sid = get_session_id()
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    command_preview = (
        tool_input.get("command", "")
        or tool_input.get("file_path", "")
        or ""
    )[:100]
    raw_context = _get_text(data)[:500]

    try:
        conn = get_conn()
        try:
            # Flat import: the installer puts learning/ on sys.path via the
            # venv .pth file, so learning_capture resolves directly.
            import learning_capture as _lc
            for sig_type, sig_summary in _lc.classify_signal(data, conn, sid):
                # self_correction rows are inserted by classify_signal itself; only the
                # lightweight config_edit signal is staged here.
                if sig_type != "self_correction":
                    _stage_one(conn, sid, sig_type, tool_name, command_preview, sig_summary, raw_context)
        except Exception:
            pass  # capture unavailable -> stage nothing (no legacy error/retry fallback)

        conn.commit()
        conn.close()
    except Exception:
        pass

    # Edge extraction (C) - unchanged
    try:
        extract_edges(data)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# FSRS retrievability (inlined to avoid importing fsrs on every prompt)
# ---------------------------------------------------------------------------
_FSRS_DECAY = -0.1542
_FSRS_FACTOR = 0.9 ** (1.0 / _FSRS_DECAY) - 1  # ~0.980346


def _retrievability(stability, elapsed_days):
    """Power-law forgetting curve. Returns R in [0,1]. At t=S, R=0.9."""
    if not stability or stability <= 0 or elapsed_days < 0:
        return 0.9
    return (1 + _FSRS_FACTOR * elapsed_days / stability) ** _FSRS_DECAY


# ---------------------------------------------------------------------------
# Function 2: surface_learnings (called from context_injector UserPromptSubmit)
# ---------------------------------------------------------------------------
def _surface_keyword_legacy(prompt_words, conn, sid, now, now_iso):
    """The legacy keyword-overlap + FSRS path. Kept as the fail-open fallback for the
    hybrid retriever (learnings_retrieval.retrieve)."""
    results = []
    rows = conn.execute("""
        SELECT id, title, description, priority, apply_when,
               fsrs_stability, fsrs_due, fsrs_last_review
        FROM learnings
        WHERE status = 'active'
        AND (expires_at IS NULL OR expires_at > datetime('now'))
        AND (fsrs_due IS NULL OR fsrs_due <= ?)
    """, (now_iso,)).fetchall()
    matches = []
    for row in rows:
        apply_words = set(
            w.lower() for w in re.findall(r'[a-zA-Z]{4,}', (row["title"] or "") + " " + (row["apply_when"] or ""))
        )
        overlap = len(prompt_words & apply_words)
        if overlap >= 2:
            stability = row["fsrs_stability"] or 7.0
            last_review = row["fsrs_last_review"]
            if last_review:
                try:
                    lr_dt = datetime.fromisoformat(last_review)
                    if lr_dt.tzinfo is None:
                        lr_dt = lr_dt.replace(tzinfo=timezone.utc)
                    elapsed = (now - lr_dt).total_seconds() / 86400.0
                except (ValueError, TypeError):
                    elapsed = stability
            else:
                elapsed = stability
            R = _retrievability(stability, elapsed)
            matches.append({
                "id": row["id"], "title": row["title"] or "",
                "description": row["description"] or "",
                "priority": (row["priority"] or "").lower(),
                "stability": stability, "score": (1.0 - R) * overlap,
            })
    matches.sort(key=lambda m: -m["score"])
    for m in matches[:3]:
        is_rule = m["priority"] == "critical" and (m["stability"] or 0) > 14
        prefix = "Rule" if is_rule else "Related learning"
        desc = m["description"].strip().replace("\n", " ")[:180]
        results.append(f"{prefix}: {m['title']}. {desc}" if desc else f"{prefix}: {m['title']}")
        _track_surfaced(conn, m["id"], sid, now_iso)
    return results


def surface_learnings(prompt_words, conn):
    """Return context strings for matching learnings + the pending-candidate count.

    Hybrid retrieval (learnings_retrieval.retrieve = FTS5 BM25 + sqlite-vec RRF +
    relevance-gated Tier-A slot, fail-open). The hot path passes no query_vec and does
    NOT load the embedding model (FTS + Tier-A only, fast); the vector arm engages in
    warm/long-lived contexts. On ANY error the legacy keyword path runs, so surfacing is
    never worse than before. retrieve() itself never raises.
    """
    results = []
    try:
        sid = get_session_id()
        now = datetime.now(timezone.utc)
        now_iso = now.strftime("%Y-%m-%d %H:%M:%S")

        used_hybrid = False
        try:
            # Flat import: learning/ is on sys.path via the venv .pth file.
            import learnings_retrieval as _lr
            hits = _lr.retrieve(" ".join(sorted(prompt_words)), conn=conn, query_vec=None, embed=False)
            for line, h in zip(_lr.format_for_injection(hits), hits):
                results.append(line)
                _track_surfaced(conn, h["id"], sid, now_iso)
            used_hybrid = True
        except Exception:
            used_hybrid = False

        if not used_hybrid:
            results = _surface_keyword_legacy(prompt_words, conn, sid, now, now_iso)

        count = conn.execute("""
            SELECT COUNT(*) FROM learning_candidates
            WHERE session_id = ? AND promoted_to IS NULL AND dismissed = 0
        """, (sid,)).fetchone()[0]
        if count > 0:
            results.append(f"Note: {count} learning candidate(s) staged this session (review at wrap-up)")
    except Exception:
        pass

    return results


def _track_surfaced(conn, learning_id, session_id, now_iso):
    """Record that a learning was surfaced, deduplicated per learning per session."""
    try:
        existing = conn.execute(
            "SELECT 1 FROM learning_reviews WHERE learning_id = ? AND session_id = ? LIMIT 1",
            (learning_id, session_id)
        ).fetchone()
        if not existing:
            conn.execute("""
                INSERT INTO learning_reviews (learning_id, session_id, surfaced_at)
                VALUES (?, ?, ?)
            """, (learning_id, session_id, now_iso))
            conn.commit()
    except Exception:
        pass  # Table might not exist yet (pre-migration)


# ---------------------------------------------------------------------------
# Function 3: anchor_candidates (called from session_lifecycle PreCompact)
# ---------------------------------------------------------------------------
def anchor_candidates(anchor, conn, sid):
    """Add pending learning candidates to the session anchor before compaction."""
    try:
        rows = conn.execute("""
            SELECT signal_type, tool_name, command_preview, error_summary, raw_context
            FROM learning_candidates
            WHERE session_id = ? AND promoted_to IS NULL AND dismissed = 0
            ORDER BY staged_at DESC LIMIT 10
        """, (sid,)).fetchall()

        if rows:
            anchor["learning_candidates"] = [
                {
                    "signal_type": r["signal_type"],
                    "tool_name": r["tool_name"],
                    "command_preview": r["command_preview"],
                    "error_summary": r["error_summary"],
                }
                for r in rows
            ]
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Function 4: extract_edges (C) - auto-create edges from tool output
# Called from stage_signal on every PostToolUse
# ---------------------------------------------------------------------------

# Email pattern for detecting person interactions
_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# Throttle: only run edge extraction every N tool calls per session
_edge_call_count = 0
_EDGE_EXTRACT_EVERY = 5  # check every 5th tool call


def extract_edges(data):
    """Extract person/org edges from tool output (email sends, chat, etc.).

    Requires operator_entity_id to be configured (see _operator_entity_id);
    skipped otherwise, since edges need a source node for the operator.
    """
    global _edge_call_count
    _edge_call_count += 1
    if _edge_call_count % _EDGE_EXTRACT_EVERY != 0:
        return

    operator_id = _operator_entity_id()
    if not operator_id:
        return  # not configured yet: nothing to anchor edges to

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    text = _get_text(data)[:3000]
    command = tool_input.get("command", "")

    # Only process Bash commands that involve email/chat/people lookups
    if tool_name != "Bash":
        return
    if not any(k in command for k in ("gmail", "discord", "query.py people", "query.py dossier")):
        return

    try:
        conn = get_conn()

        # Extract emails from both command and output
        emails = set(_EMAIL_RE.findall(text) + _EMAIL_RE.findall(command))
        for email in list(emails)[:10]:  # cap at 10 per call
            # Use indexed person_emails lookup instead of LIKE scan on entities.data
            person = conn.execute("""
                SELECT e.id FROM person_emails pe
                JOIN people p ON p.id = pe.person_id
                JOIN entities e ON e.name = p.name AND e.type = 'person'
                WHERE pe.email = ?
                LIMIT 1
            """, (email,)).fetchone()
            if not person:
                continue

            # Determine interaction type from command
            if "gmail send" in command or "gmail reply" in command or "send_email" in command:
                relation = "emailed"
            elif "discord send" in command:
                relation = "messaged"
            else:
                relation = "referenced"

            # Upsert: only create if no recent edge (within 24h)
            existing = conn.execute("""
                SELECT 1 FROM edges
                WHERE source_id = ? AND target_id = ? AND relation = ?
                AND recorded_at > datetime('now', '-24 hours')
            """, (operator_id, person['id'], relation)).fetchone()

            if not existing:
                conn.execute("""
                    INSERT INTO edges (source_id, target_id, relation, fact, valid_from, recorded_at, confidence, source)
                    VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), 0.7, 'auto_extract')
                """, (operator_id, person['id'], relation, f"operator {relation} {email}"))

        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Function 5: promote_candidates (A) - auto-promote recurring error patterns
# Called from wrap-up skill or directly
# ---------------------------------------------------------------------------
def promote_candidates(conn=None, auto=False):
    """Review and promote learning candidates. Returns summary of actions taken.

    If auto=True, promotes candidates with normalized error seen 2+ times.
    Otherwise returns candidates for human review.
    """
    close_conn = False
    if conn is None:
        conn = get_conn()
        close_conn = True

    results = {"promoted": [], "dismissed": [], "pending": []}

    try:
        # Find recurring error patterns (normalized summary seen 2+ times)
        if auto:
            recurring = conn.execute("""
                SELECT normalized_summary, tool_name, COUNT(*) as cnt,
                       GROUP_CONCAT(id) as ids,
                       MIN(error_summary) as sample_error
                FROM learning_candidates
                WHERE promoted_to IS NULL AND dismissed = 0
                  AND signal_type NOT IN ('error_pattern', 'retry', 'error')
                  AND normalized_summary IS NOT NULL AND normalized_summary != ''
                GROUP BY normalized_summary, tool_name
                HAVING cnt >= 2
            """).fetchall()

            for row in recurring:
                # Auto-create a learning
                sample = row['sample_error'] or row['normalized_summary']
                title = f"Recurring: {row['tool_name']} - {sample[:60]}"

                # Check if learning already exists
                existing = conn.execute(
                    "SELECT id FROM learnings WHERE title = ? AND status = 'active'",
                    (title,)
                ).fetchone()

                if existing:
                    # Dismiss all candidates, already learned
                    ids = row['ids'].split(',')
                    for cid in ids:
                        conn.execute("UPDATE learning_candidates SET dismissed = 1 WHERE id = ?", (int(cid),))
                    results["dismissed"].append(title)
                    continue

                # Get next learning ID
                from datetime import datetime
                today = datetime.now().strftime("%Y%m%d")
                max_id = conn.execute(
                    "SELECT MAX(CAST(SUBSTR(learning_id, -3) AS INTEGER)) FROM learnings WHERE learning_id LIKE ?",
                    (f'LRN-{today}%',)
                ).fetchone()[0] or 0

                learning_id = f"LRN-{today}-{max_id + 1:03d}"
                desc = (
                    f"WHEN: {row['tool_name']} produces '{sample[:80]}'. "
                    f"THEN: Check command/path before running. "
                    f"BECAUSE: Seen {row['cnt']} times across sessions."
                )
                conn.execute("""
                    INSERT INTO learnings (learning_id, title, description, apply_when, priority, status, memory_type, source, inserted_at, updated_at)
                    VALUES (?, ?, ?, ?, 'low', 'active', 'learning', 'auto_promote', datetime('now'), datetime('now'))
                """, (learning_id, title, desc, f"{row['tool_name']} {sample[:40]}"))

                new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                # Initialize FSRS state for new learning
                now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("""
                    UPDATE learnings SET
                        fsrs_state = 2, fsrs_stability = 3.0, fsrs_difficulty = 5.0,
                        fsrs_due = ?, fsrs_last_review = ?
                    WHERE id = ?
                """, (now_iso, now_iso, new_id))

                # Mark candidates as promoted
                ids = row['ids'].split(',')
                for cid in ids:
                    conn.execute("UPDATE learning_candidates SET promoted_to = ? WHERE id = ?", (new_id, int(cid)))

                results["promoted"].append(f"{learning_id}: {title}")

        # Get remaining pending candidates for human review
        pending = conn.execute("""
            SELECT id, signal_type, tool_name, error_summary, command_preview
            FROM learning_candidates
            WHERE promoted_to IS NULL AND dismissed = 0
            ORDER BY staged_at DESC LIMIT 20
        """).fetchall()

        results["pending"] = [
            {"id": r["id"], "type": r["signal_type"], "tool": r["tool_name"],
             "error": r["error_summary"], "cmd": r["command_preview"]}
            for r in pending
        ]

        conn.commit()
    except Exception as e:
        print(f"promote_candidates error: {e}", file=sys.stderr)

    if close_conn:
        conn.close()

    return results
