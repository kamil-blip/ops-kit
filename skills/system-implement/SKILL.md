---
name: system-implement
description: Execute system improvement plans and maintain the living system-build-status doc. Owns the implementation backlog, writes session entries, updates inventory and open items. The builder counterpart to system-rebuild (the architect).
allowed-tools: [Bash, Read, Write, Edit, Grep, Glob, Agent, TaskCreate, TaskUpdate, TaskList, TaskGet]
user-invocable: true
---

# System Implement

Execute system improvement work. Read plans, build them, record what happened.

**Relationship:** system-rebuild writes plans. system-implement executes them and writes back to system-build-status.

## When To Use

- "implement [feature]", "build [thing]", "pick up system work"
- "execute [plan slug]" (a plan from reference_docs)
- After system-rebuild produces an approved plan
- When working through the system-build-status open items
- Any session that changes hooks, search, DB schema, or infrastructure

## When NOT To Use

- Brainstorming or planning (use system-rebuild)
- Spec writing or architecture decisions (use system-rebuild)
- Day-to-day domain ops (your domain skills)
- Skill file edits only (knowledge-ops)

---

## 1. Orient

Every implementation session starts here. No exceptions.

### 1a. Load the backlog

```bash
# Read current open items and inventory
python tools/query.py "SELECT content FROM reference_docs WHERE slug='system-build-status'"

# Read a specific plan if executing one
python tools/query.py "SELECT content FROM reference_docs WHERE slug='[plan-slug]'"

# Check for related research
python tools/query.py "SELECT slug, title FROM reference_docs WHERE category='system-rebuild' AND doc_type IN ('research','plan','spec') ORDER BY updated_at DESC LIMIT 10"
```

**First run bootstrap:** if the `system-build-status` row doesn't exist yet, create it once:

```bash
python tools/query.py "INSERT INTO reference_docs (slug, title, category, content, updated_at, doc_type) VALUES ('system-build-status', 'System Build Status', 'system-rebuild', '# System Build Status' || char(10) || char(10) || '## CURRENT SYSTEM INVENTORY' || char(10) || char(10) || '## REMAINING OPEN ITEMS' || char(10), datetime('now'), 'status')"
```

### 1b. Check learnings

```bash
python tools/query.py learnings "[keyword for this work]"
```

### 1c. Identify scope

Present to the operator:
- **What I'm implementing:** (feature name, from which plan/research/open-item)
- **Files I expect to touch:** (list them)
- **Contracts:** (how we'll verify it works, stated upfront)
- **Estimated session scope:** (what will and won't land this session)

Get confirmation before writing code.

---

## 2. Implement

Standard engineering. Additional rules for this skill:

**DB table checklist:** If this session adds a new table, load system-rebuild Section 5 and follow its DB table checklist in full (migration number, schema, _table_descriptions, FTS5 + triggers, optional vec table, verification). Don't trust any step count quoted here; the checklist in system-rebuild is the source of truth and it grows.

**Destructive SQL:** Per CLAUDE.md, never run DELETE/DROP/UPDATE-without-WHERE without backing up first. If a migration partially fails, stop and assess before retrying.

### Track with tasks

Create a task per implementation step. Mark each done as you go. The operator sees progress in real time.

### Read before writing

Every file you'll edit: read it first. Check line numbers. Verify assumptions. This is the #1 cause of rework across prior sessions.

### Test as you go

Don't batch all verification to the end. After each discrete change, run the relevant contract check. Catch problems early.

---

## 2b. Long-running plan execution (>=4 hours, multi-session)

For routine sessions, Sections 1-2 cover it. When you pick up a plan that's long enough to risk context compaction mid-flight, or destructive enough that "I confirmed it" isn't trustworthy, switch to the bulletproof apparatus:

**Trigger conditions (any one):**
- The plan has >=3 phases with PRECHECK blocks.
- Mutations include bulk UPDATE / schema change / external send.
- Execution might cross sessions.
- The plan explicitly opts in (mentions `plan_runs`, `current_plan.json`, or this section).

**Top 7 patterns (write these into how you execute):**

1. **Single-MD-as-contract.** Read the plan in full. Don't paraphrase phase definitions. Phase content is checksummed in `plan_runs.phase_hash`: mid-flight edits invalidate prior `done` marks.
2. **Context-budget directive verbatim.** Front matter of every long-running plan should carry the canonical paragraph that counters context anxiety (see system-rebuild Section 4b item 2). If the plan author forgot it, paste it in before starting.
3. **`current_plan.json` on every phase entry.** Disk is the source of truth, not chat scrollback. Compaction-survival = the resumed session reads this file first, runs the in-flight phase's PRECHECK, continues or repairs.
4. **`plan_runs` + `bus_events` tables.** Per-phase row with `phase_hash`, `idempotency_key`, `outcome`, `evidence_json`, `reversal_spec`. Bus events emit per-step counters for audit-without-transcript.
5. **PRECHECK + POSTCHECK shell blocks per phase.** POSTCHECK exit 0 is the ONLY thing that promotes a phase to `DONE` in `plan_runs`. LLM self-attestation does not count.
6. **Idempotency keys per mutation.** `ikey = sha256(plan_id || phase_id || step_id || canonical(inputs))` written to the manifest BEFORE the side effect (outbox pattern). Re-runs are no-ops.
7. **Decision policy.** Reversible action → act. Irreversible (push, delete external, send email/chat message, payment) → halt and ask. No ambiguity at forks.

**Anti-patterns:**
- Markdown `[x]` ticks without a `plan_runs` row. Invisible under compaction.
- `UPDATE ... SET status='done'` without `WHERE status='pending'`. Re-runs flip resolved rows.
- LLM "I confirmed" in transcript. Only POSTCHECK commands count.
- One mega-transaction for all phases. Use savepoints per phase.

When the trigger conditions don't hold, skip this section: Sections 1-2 are sufficient for routine work.

---

## 3. Write Session Entry

**This section applies to EVERY system session, not just formal handoffs.** Any time the state of the system changes (new skill, new workflow_route, DB schema edit, file creation, migration, hook edit) Section 3 runs at the end. That includes quick-fix and medium-scope work handled inline in system-rebuild without a formal plan document. The logging does not depend on whether a handoff happened; it depends on whether state changed.

**Why mandatory:** if system-rebuild handles a change inline and never pings this section, the change lands in code but never in `system-build-status`. Future sessions grep the status doc, see no entry, and re-discover the change from scratch (this exact failure mode once left a whole shipped subsystem undocumented for days). Running Section 3 is how `system-build-status` stays a reliable snapshot.

After implementation is done (or at session end if work was partial), write a session entry to system-build-status.

### 3a. Format

Follow this exact structure. Do not invent new sections or change the heading format.

```markdown
---

## Session XXXXXX (YYYY-MM-DD): [Short Title]

**Problem:** [1-3 sentences. What gap, bug, or missing feature motivated this work.]

**What landed:**

*New files:*
- filename.py -- [one-line description of what it does]

*Changes:*
- filename.py -- [what changed and why]

*DB changes:*
- [table_name]: [what changed (new table, new column, new index, etc.)]

*Hook/skill changes:*
- [hook or skill name] -- [what changed]

**Contracts verified:**
- [x] [command that proves feature works] -- [result]
- [x] [another verification] -- [result]
- [ ] [anything that didn't pass, with explanation]

**Gaps closed (from [source doc slug]):**
- [item description]: [OLD STATE] -> DONE

**Remaining:**
- [anything new discovered or still open]

**Ref:** [changelog slug if detailed audit/changelog was written separately]
```

### 3b. Rules

- **Session ID** comes from the SESSION ID line in the system-reminder (6 hex chars, e.g., e5f6b4). Use it as-is.
- **Date** is today's date (YYYY-MM-DD).
- **Problem** must reference the source (plan slug, research slug, or open item number).
- **What landed** groups by: new files, changes, DB changes, hook/skill changes. Omit empty groups.
- **Contracts verified** uses checkboxes. Failed contracts get explanation, not deletion.
- **Gaps closed** references the source doc by slug. Uses the "OLD -> DONE" format from existing entries.
- **Remaining** only lists items discovered THIS session. Don't repeat the full backlog.
- **Ref** links to a changelog-* doc if you wrote one. Omit if no separate changelog.

### 3c. Where the entry goes

Session entries are **rows in `session_log_entries`**, not doc text. The doc keeps a one-line pointer per session. Two steps, both required:

1. **INSERT the row** (raw_md keeps the exact Section 3a format):

```python
conn.execute(
    "INSERT INTO session_log_entries (session_id, entry_date, title, problem, "
    "what_landed, contracts, gaps_closed, remaining, ref_slug, raw_md) "
    "VALUES (?,?,?,?,?,?,?,?,?,?)",
    (session_id, date, title, problem, what_landed, contracts,
     gaps_closed, remaining, ref_slug, full_entry_markdown))
row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
```

2. **Append ONE pointer line** to the end of `system-build-status` (newest-last):

```python
pointer = f"\n### Session {session_id} ({date}): {title} — session_log_entries id {row_id}"
conn.execute("UPDATE reference_docs SET content = content || ?, updated_at=datetime('now') "
             "WHERE slug='system-build-status'", (pointer,))
```

Search history with FTS over `session_log_entries`, or read one entry with
`SELECT raw_md FROM session_log_entries WHERE id=N`. Never paste full entries
into the doc itself: that shape once grew the status doc past the MCP query
tool's whole-read limit, which is why entries were split out into rows.

---

## 4. Update Status Sections

After writing the session entry, update these sections in system-build-status:

### 4a. CURRENT SYSTEM INVENTORY

Update counts and lists that changed. Common updates:
- Table count if new tables added
- Row counts for key tables
- FTS index count if new indexes
- Vec table rows if re-embedded
- Hook list if hooks added/changed
- Skill count if skills added/archived

Add "(Updated Session XXXXXX, YYYY-MM-DD)" to the section heading.

### 4b. REMAINING OPEN ITEMS

- Move completed items to "Gaps closed" in your session entry
- Add new items discovered during implementation
- Update status of partially-completed items
- If an item was intentionally deferred, move it to "Deferred (conscious decision, not a gap)" with reason

### 4c. Size check

Realistic cap: **300 KB**. Don't trust a size snapshot written into this file (dated snapshots go stale within weeks). Check the live size every time this section runs:

```bash
python tools/query.py "SELECT length(content) || ' bytes' FROM reference_docs WHERE slug='system-build-status'"
```

The 300 KB target reflects how the doc is actually used: a long, append-only changelog that's queried by slug + grepped by topic, not read top-to-bottom. An aspirational tiny cap will just get ignored; a realistic cap gets enforced.

When the doc exceeds 300 KB:
1. Pick the oldest contiguous N session entries (target: drop ~100 KB)
2. Bulk-write them to a `changelog-archive-<from-date>-<to-date>` reference_doc (single archive, not one-per-session)
3. Replace those entries in `system-build-status` with a single one-line pointer per entry: `### Session XXXXXX (date): [title] — archived in changelog-archive-...`
4. Re-verify size is back under 300 KB

For a SINGLE session with detailed audit results, large refactor logs, or technical findings >5 KB on its own: split into a `changelog-[slug]-[date]` doc + keep the summary inline + link with `**Ref:** changelog-[slug]`. Don't bury 8 KB of contract logs in the status doc when one summary line + a ref does the job.

---

## 5. Write Changelog (if needed)

For sessions with audit results, large refactors, or detailed technical findings, write a separate doc:

```bash
python tools/query.py "INSERT INTO reference_docs (slug, title, category, content, source_file, updated_at, doc_type, tags)
VALUES ('changelog-[slug]-[date]', '[Title]', 'system-rebuild', '[content]', NULL, datetime('now'), 'changelog', '[tags]')"
```

The changelog holds the full detail. The session entry in system-build-status holds the summary + ref link.

---

## 6. Implementation Backlog

The live backlog lives in **`system-build-status`** in `reference_docs`. Read it at session start:

```bash
python tools/query.py "SELECT content FROM reference_docs WHERE slug='system-build-status'"
```

The status doc carries the current REMAINING OPEN ITEMS section, plus per-session "Remaining" blocks. All sourced from the same DB row, kept up to date by Section 3 entries.

Do NOT carry a frozen snapshot of the backlog in this skill file. A snapshot kept here once listed a shipped table as NOT STARTED for a month after it landed. **Treat any backlog claim outside `system-build-status` as suspect.**

Related plan/research docs (read directly if executing):

```bash
python tools/query.py "SELECT slug, title, doc_type, updated_at FROM reference_docs WHERE category='system-rebuild' AND doc_type IN ('plan','research','spec') ORDER BY updated_at DESC LIMIT 20"
```

Track recurring blockers (external credentials, hardware, human input needed) as open items in the status doc, and re-verify each one before assuming it still blocks.

---

## 7. Quick Reference

```bash
# Read the living status doc
python tools/query.py "SELECT content FROM reference_docs WHERE slug='system-build-status'"

# Read a specific plan
python tools/query.py "SELECT content FROM reference_docs WHERE slug='[slug]'"

# List all system-rebuild docs
python tools/query.py "SELECT slug, doc_type, updated_at FROM reference_docs WHERE category='system-rebuild' ORDER BY updated_at DESC"

# Update system-build-status content
python tools/query.py "UPDATE reference_docs SET content=:content, updated_at=datetime('now') WHERE slug='system-build-status'"

# Insert a new changelog
python tools/query.py "INSERT INTO reference_docs (slug, title, category, content, updated_at, doc_type, tags) VALUES (...)"

# Check what's registered
python tools/query.py "SELECT table_name, description FROM _table_descriptions WHERE table_name='xxx'"

# Verify FTS5
python tools/query.py "SELECT * FROM {name}_fts WHERE {name}_fts MATCH 'test' LIMIT 5"
```
