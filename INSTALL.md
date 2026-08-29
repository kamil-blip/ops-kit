# INSTALL: ops-kit

Written for **your Claude Code** to execute. Paste this file at it (or say
"set up the ops-kit from INSTALL.md") and work top to bottom. Every step
ends with a check and its expected result; do not move on while a check fails.

## 1. Prerequisites

- Python 3.12+ (`python --version` → `3.12.x` or newer)
- git (only if you received this as a repo rather than a zip)
- Claude Code installed and working in a terminal
- SQLite ships inside Python; no separate install needed

Optional, connect later (the system runs fully offline without them):
- An LLM API key (Gemini, OpenAI, or Anthropic) for classification/embeddings
- Beeper Desktop (chat bridge), a Discord token, a Gmail sync via
  gmail-to-sqlite, Granola (meeting notes)
- faster-whisper for voice-note transcription

## 2. Place the repo + create the environment

```
cd <where you want it>            # git clone here
cd ops-kit
python -m venv .venv
.venv\Scripts\activate            # (Windows; on mac/linux: source .venv/bin/activate)
pip install -r requirements.txt
python scripts/setup_paths.py
```

CHECK: `setup_paths.py` prints `wrote ...ops-kit.pth`, then
`import check: paths.ROOT=<this directory>` and `OK`.

## 3. Initialize the empty databases

```
python db/init_db.py
```

CHECK: output ends with `init complete: both databases empty and healthy.`
and shows `integrity=ok fk_violations=0 nonzero_tables=0` for both `ops.db`
and `vec.db`. Re-running prints "already initialized" (that's fine).

## 4. Configuration

```
copy config.example.toml config.toml     # (cp on mac/linux)
```

Open `config.toml` and fill in, at minimum, `[operator] name`, `timezone`,
and `emails`. Leave anything you don't use blank — blank means disabled.
API keys do NOT go in this file: put them in environment variables or the OS
keyring under service `ops-kit` (e.g. `keyring set ops-kit GEMINI_API_KEY`).

CHECK: `python -c "import config; print(config.get('operator.name'))"` prints
your name (run from the repo root with the venv active).

## 5. Wire Claude Code (hooks + MCP)

1. Open `hooks/settings.example.json`. Merge its `hooks` block into your
   Claude Code settings (`~/.claude/settings.json`), replacing `$PYTHON` with
   your venv's python path and the relative hook paths with absolute ones.
2. Register the MCP server: add `core/mcp_server.py` as an MCP server named
   `ops-mcp` in your Claude Code MCP config, launched with the venv python.
3. Set the environment variable `OPS_ROOT` to this directory (user-level env
   var), so hooks launched by Claude Code resolve paths without guessing.

CHECK 1: start a new Claude Code session in this directory; the SessionStart
hook prints a session banner (no traceback).
CHECK 2: in that session, the `ops_find` MCP tool answers a probe like
`ops_find("test")` with a well-formed empty result (0 hits, no error).

## 6. First-run sanity checks

One command runs every check below and prints PASS/FAIL per step:

```
python tools/selfcheck.py
```

CHECK: ends with `selfcheck: 9/9 passed`, exit 0. Probe rows are cleaned up;
one backup file is left under `data/backups/`.

The individual checks, if you want to run them by hand (repo root, venv
active, expected results in brackets):

1. `python tools/query.py schema` → [lists every table incl. FTS shadow tables
   (about 220 lines on a fresh install), exits 0]
2. `python tools/query.py "SELECT COUNT(*) FROM people"` → [0]
3. Memory round-trip: insert one `reference_docs` row and read it back through
   `tools/query.py` → [the content comes back]
4. Learning loop: `python learning/learning_capture.py --selftest` →
   [`SELFTEST PASS`]; then `python tools/query.py learnings "<a word from a
   learning you inserted>"` → [that learning comes back]
5. Health: `python tools/query.py health` (or
   `python autonomy/health_runbook.py health`) → [prints a health summary;
   empty sections are normal on day 1]
6. Backup + restore drill: make a backup with
   `_db.make_backup("install", db_path=paths.DB_PATH)` (selfcheck does this),
   then `python autonomy/restore_drill.py --backup <that file>` →
   [`restore-drill PASS`]
7. Wrap-up skill: end your Claude Code session with "wrap up" → [a
   session_logs row exists: `python tools/query.py "SELECT COUNT(*) FROM
   session_logs"` → 1+]

## 7. Day-to-day

- **Feed it your life**: connect one source at a time in `config.toml` (start
  with email or your chat bridge), then run `python brief/daily_sync.py` and
  `python brief/brief.py sync`. Everything lands in `data/ops.db`.
- **The loop**: morning → `brief.py gather` / your daily-debrief skill;
  during work → Claude Code with the hooks does capture automatically;
  end of session → the wrap-up skill logs everything and harvests learnings.
- **Autonomy**: once comfortable, schedule `autonomy/off_session_nightly.py`
  (daily) and `autonomy/off_session_tick.py` (every few minutes) with your OS
  scheduler. They maintain the DB, back it up, and drain queues while you sleep.
- **Safety**: this kit never sends anything anywhere. Comms tooling stops at
  drafts for your review. Keep it that way.

If a check fails, read the error with your Claude Code and fix forward; the
schema and code are yours now.
