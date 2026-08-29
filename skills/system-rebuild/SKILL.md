---
name: system-rebuild
description: System infrastructure brainstorming, planning, and spec writing. The architect counterpart to system-implement (the builder). Use for new skills, DB schema design, memory architecture, hooks, skill consolidation, workflow redesign. Handles brainstorming (scaled to complexity), plan writing, DB table setup specs (FTS5 + _table_descriptions + triggers), and auto-registration in reference_docs; delegates memory file routing to knowledge-ops (Section 7). Hand off approved plans to system-implement for execution.
allowed-tools: [Bash, Read, Write, Edit, Grep, Glob, Agent, TaskCreate, TaskUpdate, TaskList]
user-invocable: true
---

# System Rebuild

The architect. Brainstorm, plan, spec, register. For execution, hand off to **system-implement**.

**Counterpart:** system-implement reads plans written here and executes them. It owns writing session entries to system-build-status and maintaining the implementation backlog.

## Routing

**Use this skill when:**
- System improvement work (new skills, DB changes, hooks, memory architecture)
- Creating or updating the improvement plan (reference_docs table)
- Adding new DB tables (requires FTS5 + _table_descriptions checklist)
- Modifying persistent files (memory/, skills/, rules files, MEMORY.md)
- Brainstorming system changes before implementing
- Planning multi-session work
- Skill consolidation, archiving, or restructuring

**Do NOT use this skill when:**
- Day-to-day domain ops (use your domain skills)
- Email drafting or any outbound comms (use your comms workflow)
- Quick file reads or lookups that don't change infrastructure

---

## 1. Scope Check

Assess complexity FIRST. This determines the process:

| Scope | Signal | Process |
|-------|--------|---------|
| **Quick fix** (< 5 min) | Fix a typo, update a path, rename a reference | Execute directly here. Register if structural (Section 8). **Still requires Section 3 logging via system-implement at exit.** |
| **Medium** (< 30 min) | Add a DB column, merge two skills, fix stale refs | Brief brainstorm (1-2 questions). Plan inline. Hand off to system-implement. |
| **New subsystem** (30+ min) | New skill, new DB table, new hook pipeline, workflow redesign | Full brainstorm. Write plan document. Hand off to system-implement for execution. |

**Quick fixes:** handle inline (Section 5 for DB specs, Section 8 for registration). The "quick" in "quick fix" is about the work, not the logging. If state changed (file, DB, skill, route, migration), the session entry still gets written. The MANDATORY logging rule below is not waived by scope. **Medium+ tasks:** brainstorm and plan here, then hand off to system-implement for execution.

**MANDATORY at every exit, no exceptions:** regardless of scope, every system-rebuild session that actually changes state (file edits, DB writes, skill creation, route insertion) ends by running **system-implement Section 3 (Write Session Entry)** to append a session entry to `system-build-status` and update the inventory. Quick fixes still need a logged entry. Skipping this is the single largest drift source between `system-build-status` and reality: a whole shipped subsystem once went undocumented for days because a session skipped it, and later sessions rediscovered the work from scratch.

---

## 2. Orient (medium+ scope)

Before any work:

```bash
# Check related work in the improvement plan table
python tools/query.py "SELECT slug, title, doc_type FROM reference_docs WHERE category='system-rebuild' AND (title LIKE '%keyword%' OR content LIKE '%keyword%') LIMIT 10"

# Check learnings for gotchas
python tools/query.py learnings "keyword"

# Check memory topic files for related entries
grep -ril "keyword" memory/
```

If related work exists, read it first. Don't duplicate or contradict existing plans.

### Verify the premise

When picking up a deferred item from `system-build-status`, re-verify the description against current DB state before starting. **Treat the deferred description as a hypothesis, not a fact.** Counts move while you're not looking; the wording from a session 4 weeks ago is rarely still literally true.

Real drift patterns seen in practice:
- A duplicate-count noted a month earlier had grown 3x by re-check time
- A "43 non-standard IDs" cleanup item had grown 8x
- A "this needs to be built" item was already built and running
- A "consolidate N skills" item had partially resolved through organic drift

For every deferred item with a number in it: re-run the count first. For every deferred item naming a file or function: grep for it first. For every "this needs to be built" item: check the file doesn't already exist.

If the premise changed, update the system-build-status REMAINING OPEN ITEMS entry (correct the number, mark closed, or upgrade the description) before doing the work.

---

## 3. Brainstorm (medium+ scope)

### Process

1. **Explore context.** Read relevant files, check DB state, understand what exists today.
2. **Ask questions one at a time.** Prefer multiple choice when possible. Don't overwhelm.
3. **Propose 2-3 approaches.** With tradeoffs. Lead with your recommendation and why.
4. **Get approval.** Don't implement until the operator confirms the approach.

### Question Patterns for System Work

Use these to surface hidden requirements:

| Question | What it catches |
|----------|----------------|
| "What's the trigger? When does this fire / get invoked?" | Missing activation logic |
| "What's the output? What does 'done' look like?" | Vague success criteria |
| "What already exists? Can we extend rather than create?" | Unnecessary duplication |
| "What breaks if this fails? What's the blast radius?" | Missing error handling |
| "Who else touches this? Cross-references, downstream?" | Stale reference risk |
| "What tables are affected?" (check _table_descriptions) | Missing DB registration |
| "What skills reference this?" (grep skills/) | Broken skill routing |

### Principles

- **YAGNI.** Cut unnecessary features. Don't design for hypothetical future needs.
- **One source of truth.** Don't duplicate info across files. Reference, don't repeat.
- **Contracts over audits.** Define verified outputs upfront. Done when contracts pass.
- **Implement correctly the first time.** Read code before changing. Verify assumptions. Zero rework beats fast rework.

---

## 4. Plan (new subsystem scope)

Write a single plan document. Not separate spec + plan + execution docs.

**Path:** Directly into the ops.db `reference_docs` table (category='system-rebuild'). No .md file on disk.

### Template

```markdown
# [Title] — [Date]

**Session type:** [Plan only | Plan + implement | Quick fix]
**Related:** `python tools/query.py "SELECT slug FROM reference_docs WHERE slug IN ('related-1','related-2')"`

---

## Priority N: [Name]

### Goal
[One sentence]

### Current State
[What exists now, with file paths]

### Changes
[Detailed changes with exact file paths and line numbers where possible]

### DB Changes (if any)
[Schema + FTS5 + triggers + _table_descriptions — see Section 5]

### Contracts
- [ ] [Testable verified output — a command that proves it works]
- [ ] [Another testable output]

---

## Implementation Order
[Session A: ..., Session B: ..., with complexity notes]
```

### Plan Rules

- **No placeholders.** No "TBD", "TODO", "implement later", "add appropriate error handling".
- **Exact file paths.** Every change names the file and ideally the line range.
- **Contracts are testable.** Each contract is verified with a command (grep, SQL query, file existence check).
- **Single document.** The plan IS the spec. One file, not three.
- **No placeholders.** (Yes, twice. This is the #1 cause of rework.)
- **No unsolicited communication steps.** Plans, specs, and sign-off rituals must NOT include "optional but recommended: post a summary to X" or any soft outreach. If the plan's prohibitions ban outbound, the sign-off can't smuggle it back in.

### 4b. Long-running plan template (>=4 hours / multi-session)

When the plan you're writing meets ANY of these triggers, use the heavier bulletproof apparatus instead of the basic template above:

- >=3 phases that each need a PRECHECK before starting
- Mutations include bulk UPDATE / schema change / external send / batch model calls
- Execution might cross sessions (compaction risk)
- The cost of a wrong "done" claim is high (data loss, silent skip, double-apply)

The plan you draft must include:

1. **Reading-order contract** at the top: "Read this file in full before doing anything. Resume from disk state, not chat scrollback."
2. **Mission + context-budget directive verbatim** — paste the canonical paragraph that counters context anxiety:
   > Your context window will be automatically compacted as it approaches its limit, allowing you to continue working indefinitely from where you left off. Therefore, do not stop tasks early due to token budget concerns. As you approach your token budget limit, save your current progress and state to memory before the context window refreshes. Always be as persistent and autonomous as possible. Never artificially stop any task early regardless of context remaining.
3. **Success criteria** — numeric, each with a verify command.
4. **Hard prohibitions** — including "no outbound communication" if applicable.
5. **Decision policy** — reversible/irreversible matrix removing ambiguity at forks.
6. **JSON feature list** — `{phases: [{id, name, passes:false, reversible}]}`. Authoritative state; flipped only after POSTCHECK.
7. **Bootstrap phase** that creates `plan_runs` + `bus_events` tables (if not present) and writes `data/plan-state/current_plan.json` to disk for compaction-survival.
8. **Per-phase structure** (every phase): GOAL, IDEMPOTENCY KEY, REVERSIBLE flag with rollback spec, PRECHECK shell block, DO steps, POSTCHECK shell block, BUS EVENT counters.
9. **Sign-off ritual** — internal-only (DB writes + skill log). NO outbound comms.
10. **Rollback procedures** per failure mode.

**For shorter plans (single-session, <=3 hours, reversible-only):** the basic template above is sufficient. Don't add the apparatus for a quick fix; it's overkill below the threshold.

### 4c. Skip-proof acceptance model (multi-hour DRAIN / FIX runs)

A long-running plan can be structurally sound and STILL drain nothing if it lets the executing session treat "queued / proposed / a report exists" as done. The failure mode is the ACCEPTANCE MODEL, not persistence language: an overnight run can carry a loud "NO DEFERRING" law and still bypass it, because it defined success as "propose into a review queue + STOP" and every POSTCHECK passed on "counted as backlog." Louder prose does not fix this; a rewritten acceptance contract + a per-turn enforcement hook does.

**Bake these EIGHT rules into any DRAIN/FIX plan (each closes an escape hatch):**
1. **DONE IS A NUMBER, not a state word.** Every phase ends with a shell/SQL command that prints a count vs a literal target. Banned as completion signals: "queued / counted / proposed / diagnose-only / builds cleanly / imports / a report exists / returns sane output."
2. **CLOSED human-gated predicate is the ONLY permitted defer** (five classes: outbound-published facts; financial; VIP/manager/partner-committing; fuzzy/non-exact merges; irreversible/invasive structural ops). Everything reversible-and-not-in-the-list resolves AT SOURCE and APPLIES this run. No third "optional / next-pass / diagnose-only" category.
3. **BLOCKED is machine-defined** — a named PRECHECK exits non-zero for an external cause (missing credential, external 403, edit-protected file). Never self-judged ("large / slow / uncertain" is not a block).
4. **Two-gate POSTCHECK** for queue-bearing phases: Gate A = proposal+evidence for 100% of in-scope rows; Gate B (the one that PROMOTES) = the auto-applicable subset is APPLIED and re-queried so only human-gated rows remain.
5. **Re-derive + FREEZE baselines at Phase 0** (the deferred description is a hypothesis — counts drift; re-run the SQL). Close-out diffs frozen-vs-final and FAILS on any non-human-gated metric that didn't move.
6. **Large builds get a committed reversible milestone** (module + runs on >=1 real input + a test + a resume note), never propose-only. Only the genuinely-invasive cutover defers.
7. **A read-only EVALUATOR subagent** re-runs every POSTCHECK before the completion promise; the session may not self-declare done.
8. **The intelligence layer exists FIRST**: if a judgment can't be resolved headless, build/invoke the agentic drainer before the drain phases.

**THE WAKE-UP INJECTION APPARATUS (per-turn enforcement that survives compaction — a contract pasted once gets compacted away after ~200 messages).** Build these two hooks when you start running long unattended plans:
- `data/plan-state/current_plan.json` carries a `live_checks` array: `[{label, sql (a SELECT-only scalar), target, human_gated}]`, and the run sets `plan_runs.outcome='IN_PROGRESS'` at phase entry (the un-spoofable "run active" DB anchor; flip to `DONE` at phase exit).
- A **plan-run injector hook** (UserPromptSubmit): when a run is active, re-injects the live remaining-work counts + "you may NOT stop while any non-human-gated count > 0" on EVERY turn. Fires even on one-word answers (it ignores prompt text). No-op + invisible when no run is active.
- A **plan-run stop gate hook** (Stop): when the model tries to stop with non-human-gated work pending, returns `{"decision":"block","reason":...}` to FORCE continuation — an external check decides "done," not the model. Safety: a stall-release after N stuck stops + a hard block cap so it can never jam a session shut.
- Keep both hooks outside the model's edit surface (install them by hand once), then every future overnight run is gated. Wire into `settings.json` `UserPromptSubmit` + `Stop` chains.
- Hook gotchas: a Stop hook blocks on `decision:block` or exit code **2** (exit 1 is a silent no-op); honor the `stop_hook_active` loop guard; put feedback in `reason`, NOT `additionalContext` (version-dependent on Stop); the Stop gate's measurement branch must NOT fail-open on a real red check (only on "couldn't measure").

---

## 5. DB Table Checklist

**MANDATORY when any plan adds a new table to ops.db.** Include all of these in the plan document so system-implement can execute without ambiguity:

```
- [ ] 0. Pick an unused migration number:
         ls db/migrations/00*.sql | tail -3
         -- Create db/migrations/ with your first migration if it doesn't exist yet.
         -- Watch for parallel same-numbered migrations from concurrent sessions;
         -- SQLite tolerates it, humans don't.
- [ ] 1. CREATE TABLE with proper schema
- [ ] 2. Register in _table_descriptions:
         INSERT INTO _table_descriptions
           (table_name, tier, description, when_to_query, key_columns, example_queries, category)
         VALUES (?, ?, ?, ?, ?, ?, ?)
         -- tier: 'core' | 'operational' | 'reference' | 'system'
         -- category: people | comms | ops | system | analytics | reference
- [ ] 3. CREATE FTS5 virtual table (if table has text columns):
         CREATE VIRTUAL TABLE {name}_fts USING fts5(
             col1, col2, col3,
             content='{name}',
             content_rowid='rowid'
         )
- [ ] 4. Populate FTS5:
         INSERT INTO {name}_fts(rowid, col1, col2, col3)
         SELECT rowid, col1, col2, col3 FROM {name}
- [ ] 5. CREATE 3 sync triggers:
         -- See references/fts5-triggers-template.sql for exact SQL
- [ ] 6. (Optional) CREATE vec_{name} virtual table for semantic search:
         -- Only when the table has long-form text suitable for embeddings
         -- (descriptions, bios, emails, observations, FAQs). Skip for pure-structure tables.
         CREATE VIRTUAL TABLE vec_{name} USING vec0(
             rowid INTEGER PRIMARY KEY,
             embedding FLOAT[1024]
         );
         -- Then write search/embed_{name}.py (mirror search/embed_people.py).
         -- Existing vec_* tables: vec_people, vec_entities, vec_emails, vec_learnings, vec_observations, vec_faqs.
- [ ] 7. Add query.py shortcut (if frequently queried)
- [ ] 8. Verify: query.py schema {name} returns columns
- [ ] 9. Verify: FTS5 MATCH query returns results
- [ ] 10. (If step 6) Verify: vec_{name} populated; semantic search returns results via embed_{name}.py --incremental
```

**Why:** Every table must be discoverable via `query.py map` and searchable via FTS5. Tables without `_table_descriptions` entries are invisible to the routing system. Tables without FTS5 indexes can't be searched with MATCH. Tables that hold long-form text and skip the `vec_*` step never benefit from semantic search even when callers reach for it.

---

## 6. Hand Off to system-implement

Once a plan is approved, execution belongs in **system-implement**. That skill handles task tracking, contract verification, session entries to system-build-status, and status doc updates.

**Handoff is not optional for logging.** Even when you execute a quick fix or medium-scope change inline here (without a full plan doc), you must still run **system-implement Section 3 (Write Session Entry)** before the session ends. Section 8 below covers plan/spec/doc registration in `reference_docs`; Section 3 of system-implement covers the live session entry in `system-build-status`. Both are required — one does not substitute for the other.

**Checklist at every rebuild-session exit:**
- [ ] If this session added a plan/spec/research doc → registered in `reference_docs` (Section 8a).
- [ ] If this session changed state (files, DB, skills, routes, migrations) → session entry appended to `system-build-status` via system-implement Section 3, inventory counts updated (system-implement Section 4).
- [ ] If this session renamed/moved anything → cross-reference scan (Section 8c).

---

## 7. Memory & Skill File Routing

Routing rules + pre-edit checklist live exclusively in `knowledge-ops` (single source of truth). Before modifying any persistent file (memory topic file, skill, runbook entry, rules file, MEMORY.md), invoke `knowledge-ops` for the file-type taxonomy, duplication guard, and pre-edit checklist.

---

## 8. Register

After completing a plan, spec, or research document (architect outputs). For session entries and changelogs after implementation, see system-implement Sections 3-5.

### 8a. Register in reference_docs

```sql
INSERT INTO reference_docs (slug, title, category, content, source_file, updated_at, doc_type, tags)
VALUES (
    'slug-name',                    -- kebab-case, unique
    'Human-Readable Title',         -- descriptive
    'system-rebuild',               -- category (usually system-rebuild for this skill)
    '<full document content>',      -- paste the doc content
    NULL,                           -- no source file; content lives in DB only
    datetime('now'),
    'plan',                         -- plan | spec | research | audit | strategy | status
    'tag1,tag2'
);
```

### 8b. Update MEMORY.md (if needed)

Only if the work represents a new active project or significant system change. Add one line to the relevant section:

```markdown
- [Title](filename.md) -- one-line description
```

### 8c. Cross-Reference Scan

```bash
# After any rename, move, or deletion:
grep -r "old-name" skills/ memory/ CLAUDE.md --include="*.md"
```

Fix every stale reference found. This is NOT optional.

---

## 9. Quick Reference

### File Locations

| What | Where |
|------|-------|
| Plans, specs, research | `reference_docs` table (category='system-rebuild') |
| Skill files | `skills/[name]/SKILL.md` |
| Memory entries | `memory/` topic files |
| Memory index | `memory/MEMORY.md` |
| Rules (policy files) | wherever CLAUDE.md points; keep one canonical location |
| Database | `data/ops.db` (resolve via `core/paths.py`, never hardcode) |
| Improvement plan table | `reference_docs` table (category='system-rebuild') |

### Key Commands

```bash
# Query improvement plan table
python tools/query.py "SELECT slug, title, doc_type FROM reference_docs WHERE category='system-rebuild'"

# Check table descriptions
python tools/query.py "SELECT table_name, description FROM _table_descriptions WHERE table_name='xxx'"

# Check learnings before any work
python tools/query.py learnings "keyword"

# Verify FTS5 works
python tools/query.py "SELECT * FROM {name}_fts WHERE {name}_fts MATCH 'test' LIMIT 5"

# Cross-reference scan
grep -r "old-name" skills/ memory/ CLAUDE.md --include="*.md"
```
