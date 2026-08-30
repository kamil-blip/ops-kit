---
name: wrap-up
description: End-of-session wrap-up AND end-of-day debrief (canonical owner). Logs all changes to ops.db, promotes learnings, and generates a session summary. Use when ending a work session OR when the operator says "wrap up", "end of day", "debrief", "done for now", "save progress", or "log this session". Morning intake / brief lives in `daily-debrief`.
allowed-tools: [Read, Write, Edit, Grep, Glob, Bash]
user-invocable: true
---

# Session Wrap-Up

## Routing

**Use this skill when:** Ending a work session, logging changes, generating session summaries, closing out the day.

**Do NOT use this skill when:**
- Mid-session task tracking → use TaskCreate/TaskUpdate
- Routing information to the right file or memory entry → use `knowledge-ops`
- Daily briefing/inbox sweep → use `daily-debrief` (brief.py pipeline)

---

Run at the end of every session that made persistent changes.

**Command shorthand:** `query.py` = `python platform/tools/query.py`, `task_manager.py` = `python platform/tasks/task_manager.py`. Run from the repo root.

## Data Architecture

**ops.db is the source of truth for all state.** Markdown files are design docs or human-readable exports, not state.

| Data | Source of truth |
|------|----------------|
| Action items | `action_items` table in ops.db |
| Deferred actions | `deferred_actions` table in ops.db |
| Session log | `audit_events` + `bus_events` tables |
| People/contacts | `people` + `person_emails` tables |
| Learnings | `learnings` table in ops.db |

**Still markdown (on disk):** skills (SKILL.md files) and design docs. State never lives in markdown.

**Useful convention (in ops.db reference_docs):** keep a running system-build-status doc and check it when infra changed this session:
```bash
query.py "SELECT content FROM reference_docs WHERE slug='system-build-status'"
```

## Steps

### 1. File Remaining Work

Before logging what was done, capture what's still open.

**CRITICAL: ad-hoc `INSERT INTO action_items` is banned during wrap-up.**

Two routes. Pick by whether the operator explicitly confirmed the item this session:

**(a) The operator confirmed it this session → canonical (direct to action_items):**
```bash
task_manager.py add "Description" --priority P1 --source wrap-up-confirmed --quote "operator 11:29: yes, file that"
```
`wrap-up-confirmed` is on the task gate's canonical-source whitelist. Use ONLY when the operator verbally said "yes, file this" in the wrap-up dialog.

**(b) Otherwise (default for everything wrap-up surfaces) → inbox for next-day triage:**
```bash
task_manager.py add "Description" --priority P2 --source wrap-up --quote "verbatim source line"
```
The non-canonical `wrap-up` source is routed through the gate to `action_items_inbox`. The operator triages later via the `triage-inbox` skill.

Reason: wrap-ups that insert direct-canonical pollute the focus list with dozens of low-signal items per month. `action_items` is human-curated only.

**MANDATORY for both routes:** populate `--quote` with the originating quote verbatim, and `--thread <mail_thread_id>` / source IDs where applicable. Cheap at creation; often impossible to reconstruct later.

**System tasks (infra / tech debt / fabric work) go to `system_upgrades`, not action_items** (if you keep that table; otherwise route them through the inbox like everything else):
```bash
query.py "INSERT INTO system_upgrades (upgrade_id, title, description, status, priority, domain) VALUES ('UPG-YYYYMMDD-SLUG', '...', '...', 'PROPOSED', 'P2', 'fabric')"
```

- Blocked items: note the blocker and who owns it in the context field.

### 2. Reconcile Action Items

Query bus_events for action_item_match events from this session:

```bash
query.py "SELECT summary FROM bus_events WHERE event_type = 'action_item_match' AND date(ts) = date('now') ORDER BY ts DESC"
```

For each match:
1. Verify: was the work actually completed? (Don't blindly trust fuzzy match.)
2. If yes: UPDATE the action_items row:
   ```sql
   UPDATE action_items SET status='DONE', completed_at=datetime('now'), updated_at=datetime('now')
   WHERE item_id = ?
   ```
3. If no (false positive): skip.
4. WAITING items where a reply came in: UPDATE status to 'OPEN', clear waiting_on.

New work discovered? Use `task_manager.py add` with the right `--source` flag (see Step 1). Never raw SQL INSERT.

### 3. Gather Changes

The `audit_events` table already captured everything via hooks. Query it:

```sql
SELECT event, tool, source_file, cmd_preview, ts
FROM audit_events WHERE session_id = ? AND date(ts) = date('now')
ORDER BY ts
```

This replaces manual scanning of files.

### 3.5. Learning loop

#### Review learning candidates from this session
```bash
query.py "SELECT id, signal_type, tool_name, command_preview, error_summary FROM learning_candidates WHERE promoted_to IS NULL AND dismissed = 0 ORDER BY staged_at DESC LIMIT 20"
```
For each candidate:
- If it reveals a real learning: promote it by creating a WHEN-THEN-BECAUSE learning (see below), then mark promoted:
  `UPDATE learning_candidates SET promoted_to = NEW_LEARNING_ID WHERE id = CANDIDATE_ID`
- If it's noise or already known: dismiss it:
  `UPDATE learning_candidates SET dismissed = 1 WHERE id = CANDIDATE_ID`

#### FSRS review (optional override)
Learnings surfaced this session are auto-rated Good(3) at session end. Override if any were wrong or too obvious:
```sql
SELECT lr.id, l.title, lr.rating, l.fsrs_stability, l.fsrs_due
FROM learning_reviews lr JOIN learnings l ON l.id = lr.learning_id
WHERE lr.session_id = 'SESSION_ID' ORDER BY lr.surfaced_at
```
- **Again(1)** for wrong/outdated: `UPDATE learning_reviews SET rating = 1 WHERE id = X`
- **Easy(4)** for too obvious: `UPDATE learning_reviews SET rating = 4 WHERE id = X`
- The Stop hook computes FSRS state updates using whatever rating is set.

#### Review
Look back at this session (also check candidates above):
- Commands that errored or were slow
- Skills that didn't fire when they should have
- Questions you had to ask the operator that you should have known
- Patterns that worked well (capture successes too)
- **Workflow route gaps:** Did you improvise when a skill/check existed? Did you use stale data? If yes, add a route to `workflow_routes` so the hook catches it next time:
  ```sql
  query.py "INSERT INTO workflow_routes (trigger_patterns, required_action, reason, category, priority) VALUES ('keywords', 'SKILL: x. VERIFY: y.', 'What went wrong', 'category', 3)"
  ```

#### Check for existing learnings
For each potential learning, search with 2-3 keyword variants:
```bash
query.py learnings "keyword"
```
(The shorthand includes status='active' and expires_at filters. For raw SQL when needed: `query.py "SELECT id, title, description FROM learnings WHERE status = 'active' AND (expires_at IS NULL OR expires_at > datetime('now')) AND apply_when LIKE '%keyword%' LIMIT 10"`)

#### Add new learnings (WHEN-THEN-BECAUSE format is mandatory)

First, get next available ID for today:
```bash
query.py "SELECT MAX(CAST(SUBSTR(learning_id, -3) AS INTEGER)) FROM learnings WHERE learning_id LIKE 'LRN-' || strftime('%Y%m%d', 'now') || '%'"
```
Then use the next number (e.g., if MAX is 5, use 006):
```sql
INSERT INTO learnings (learning_id, title, description, apply_when, priority, status, memory_type, source, inserted_at, updated_at)
VALUES (
    'LRN-YYYYMMDD-NNN',
    'Short title',
    'WHEN: [exact trigger]. THEN: [exact steps]. BECAUSE: [the incident].',
    'keyword phrases for LIKE search',
    'medium',
    'active',
    'learning',
    'wrap-up',
    datetime('now'),
    datetime('now')
);
```

#### Update existing (contradiction = supersede old)
```sql
UPDATE learnings SET status = 'superseded', superseded_by = NEW_ID, updated_at = datetime('now') WHERE id = OLD_ID;
```

#### Temporary learnings (deadline-bound)
Same as above but add `expires_at`:
```sql
INSERT INTO learnings (learning_id, title, description, apply_when, priority, status, memory_type, source, expires_at, inserted_at, updated_at)
VALUES (
    'LRN-YYYYMMDD-NNN',
    'Title',
    'WHEN-THEN-BECAUSE',
    'keywords',
    'medium',
    'active',
    'learning',
    'wrap-up',
    '2026-04-27',
    datetime('now'),
    datetime('now')
);
```

#### Expire old learnings
```sql
UPDATE learnings SET status = 'expired', updated_at = datetime('now') WHERE expires_at < datetime('now') AND status = 'active';
```

#### Review learnings health
```sql
-- Oldest active learnings (may be stale)
SELECT id, title, inserted_at FROM learnings
WHERE status = 'active' AND (expires_at IS NULL OR expires_at > datetime('now'))
ORDER BY inserted_at ASC LIMIT 10;
```
Show to the operator: "These are the oldest active learnings. Any stale ones to supersede?"

### 4. Changelog

**The `session_lifecycle.py` Stop hook auto-generates a changelog fragment** from the `audit_events` table. You do NOT need to manually write one.

The auto-generated changelog is stored in ops.db reference_docs (category='changelog').
Query: `query.py "SELECT content FROM reference_docs WHERE category='changelog' AND slug LIKE '%SESSION_ID%'"`

**Do NOT write changelog .md files.** The DB is the source of truth.

### 5. Note Process Gaps (lightweight)

If this session discovered a new repeatable process or gotcha, capture it as a learning with the relevant keywords in `apply_when` so it surfaces next time the same work comes up.

### 6. Flag Stale References

Quick scan: did any edit change something referenced elsewhere?
- Counts (roster sizes, item totals)
- Dates or deadlines
- Status changes (confirmed/pending/dropped)
- File renames or moves

If yes, update the downstream references.

### 7. Log Session to ops.db

Two things to persist:

**a) Session summary (what was accomplished):**
```sql
INSERT INTO session_logs (session_id, date, title, summary)
VALUES ('SESSION_ID', 'YYYY-MM-DD', 'Short title', 'Full summary with actions, files changed, items closed')
```

**b) Full conversation history (the detailed record):**
```bash
python platform/logging/import_session.py
```
This imports the JSONL session file into the `conversation_history` table. Handles both old (snapshot) and new (top-level message) formats. Skips already-imported sessions. Every user and assistant message is stored with timestamps.

Combined with `session_summary` (auto-captured metadata), this gives a complete picture: what was discussed (conversation_history) and what was done (session_logs).

### 7b. Capture Toil (continuous capture)

Before summarizing, ask: did this session repeat manual work a script/schedule should own (re-runs, hand-dedupes, per-person rituals, log babysitting)? If yes, file each observation for triage:

```bash
task_manager.py add "Automate: <what was done by hand and how often it recurs>" --priority P3 --source wrap-up --quote "<where the repetition showed up this session>"
```

Skip when nothing repetitive happened. No filler captures.

### 8. Generate Summary

Output a concise summary for the operator:

```
## Session Summary

**What got done:**
- [Bullet list of completed work]

**DB updates:** [action items closed/created, people added]
**External actions:** [emails drafted, posts, tracker updates]

**Open threads:**
- [Anything started but not finished]

**Next session should:**
- [1-2 priority items for the next session]
```

Keep it short. The operator can query the DB for details.
