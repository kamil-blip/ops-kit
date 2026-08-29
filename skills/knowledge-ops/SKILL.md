---
name: knowledge-ops
description: Gatekeeper for all edits to memory topic files (memory/), skill files, MEMORY.md, and the runbook (template_tasks table). Invoke BEFORE writing to any persistent file (discipline routing: no auto-hook backs this; the model must remember to call it). Ensures information lands in the correct location, prevents duplication, enforces structural rules, and maintains cross-references.
allowed-tools: [Read, Write, Edit, Grep, Glob]
user-invocable: false
---

# Knowledge Management

## Routing

**Use this skill when:** Creating/editing memory topic files in `memory/`, skill files in `skills/`, rules/policy files, or the runbook (`template_tasks` table). Restructuring persistent data. Running monthly consolidation. Checking where information should go.

**Do NOT use this skill when:**
- Tracking tasks within a session → use TaskCreate/TaskUpdate
- Saving conversation-only context → don't persist it at all
- Reading files for reference → just read them, no gatekeeper needed
- Logging session changes → use `wrap-up` skill (changelog logging is hook-driven, no skill call needed)
- Brainstorming a new subsystem, DB table, or hook pipeline → use `system-rebuild` (which calls back here for any memory/skill edits)
- Writing a session entry to `system-build-status` → use `system-implement` Section 3

---

Route every piece of persistent information to the right file. This skill prevents structural drift when approaching tasks from novel angles.

## When This Triggers

Any time you're about to:
- Create or edit a topic file in `memory/`
- Create or edit a file in `skills/`
- Edit `memory/MEMORY.md` (the index)
- Edit the runbook: `python tools/query.py "SELECT * FROM template_tasks ORDER BY phase, step_number"`
- Edit any rules/policy file referenced from CLAUDE.md

**Stop and run through the routing decision below BEFORE writing.**

---

## 1. Routing Decision: Where Does This Information Go?

Ask: **"Who needs this, and when?"**

| If the information is... | It goes in... | Example |
|--------------------------|---------------|---------|
| Step-by-step instructions an external operator could follow for a recurring process | **Runbook** (`template_tasks` table) | "Step 3: Review the posts against this 10-point checklist" |
| Claude-specific workflow, platform config, or decision tree | **Skill** (`skills/[name]/SKILL.md`) | Platform IDs, auto-trigger rules, API quirks |
| Cross-session knowledge about people, processes, tools, or preferences | **Memory** (topic file in `memory/`) | A recurring workflow, CLI gotchas, the operator's feedback |
| Voice, tone rules, or org-wide policies | **Rules** (a rules file referenced from CLAUDE.md) | Banned-phrase list, escalation protocol |
| A pointer to a memory file | **MEMORY.md** (index only) | `- [project_orchestrator_pattern.md](project_orchestrator_pattern.md): Orchestrator pattern` |

### Decision Tree

```
Is this a step-by-step process an external operator should be able to run?
├── YES → Runbook (template_tasks table). Write it for the operator's OWN assistant: shared-cloud data (their tracker / docs / mail / calendar), inline templates, agent-executable ai_prompts. NO private-stack refs (this repo's DB / query.py / MCP / skills / scripts).
├── NO → Is this Claude-specific routing logic? (auto-trigger conditions, API keys, MCP config, skill body)
│   ├── YES → Skill file
│   └── NO → Is this reusable across sessions?
│       ├── YES → Memory topic file (check if one exists for this topic first)
│       └── NO → Don't persist it. It's conversation context.
```

---

## 2. Structural Rules

### Runbook (`template_tasks` table)

- **Runbook voice.** The runbook is bus-factor insurance: written for an external operator (or their own AI assistant) who does NOT have this repo. Do NOT reference the local DB (ops.db / query.py / the tables), your MCP server, your scripts, your skills, or internal IDs. Pull data from shared cloud tools (task tracker, shared docs, mail, calendar). Inline the actual templates. Workflow content mirrors what you actually do.
- Phase/step structure: `### X.Y Task Name` → Steps → Definition of Done. Two fields: description = the human playbook + inline templates + "done when"; ai_prompt = a paste-ready, agent-executable prompt for the operator's own assistant (shared-cloud tools only, zero private-stack refs).
- Include: exact commands, UI walkthroughs, copy-paste prompts with `[PLACEHOLDERS]`
- Use `[TODO: ...]` for unverified steps
- When updating: also review related action_items spawned from the runbook (`source LIKE 'template:X.Y'`) to close out completed work.

### Skills (`skills/[name]/SKILL.md`)

- Frontmatter: `name` + `description` (description is the trigger, be specific)
- Body: workflows, checklists, platform rules, decision trees
- MAY reference the runbook ("See template_tasks Phase 3 for the full process")
- MAY reference memory files for detailed context
- Keep under 500 lines. Split to `references/` if longer.
- Skills are for Claude (workflow + routing). The runbook is the playbook a human operator, or their own AI assistant, executes.

### Memory (`memory/` topic files)

- One topic file per subject, named by type: `feedback_*.md`, `project_*.md`, `reference_*.md`
- One source of truth per topic. Search existing files before creating: `grep -ril "keyword" memory/`
- After creating a new file: add one index line to `memory/MEMORY.md`
- MEMORY.md must stay under 150 lines (index only, no content)

### Rules (policy files)

- Org-wide policies, voice guidelines, operational protocols
- Shared across all contexts; referenced from CLAUDE.md
- Rarely changed. Discuss with the operator before editing.

---

## 3. Pre-Edit Checklist

Run through this EVERY time before writing to a persistent file:

1. **Does an entry already cover this topic?** Search `memory/`, `skills/`, and the rules files first. Update the existing entry rather than creating a new one.
2. **Is this the right file type?** (See routing table above)
3. **Runbook edits: operator-portable?** No private-stack refs (local DB / MCP / skills / scripts / internal IDs); data from shared cloud tools; templates inlined; ai_prompts agent-executable for the operator's own assistant; "done when" = a shared artifact.
4. **Skill edits: does the runbook need a parallel update?** If you're adding a checklist to a skill, the same content may belong in the runbook ai_prompt too.
5. **Memory edits: is MEMORY.md still under 150 lines?** If adding a new file, check the index length. Move detailed content out of MEMORY.md into topic files if needed.
6. **Cross-references intact?** If you renamed or moved a file, grep for old references and update them.
7. **Side effects?** If the edit changes anything referenced elsewhere (counts, dates, statuses, paths), find and update those references too.

---

## 4. File Size Limits

Hard limits to prevent bloat. Check before and after editing:

| File type | Max lines | Max chars | Action if exceeded |
|-----------|-----------|-----------|-------------------|
| MEMORY.md (index) | 150 | 10K | Move content to topic files, keep only pointers |
| Memory topic files | 500 | 20K | Split into sub-topics or archive |
| Skill files | 500 | 20K | Move reference material to `skills/[name]/references/` |
| Rules files | 200 | 10K | Keep focused. Split if covering multiple policies |

**Quick check command:**
```bash
wc -l skills/*/SKILL.md memory/*.md
```

---

## Lifecycle Stages

Every piece of knowledge follows this lifecycle:

```
CREATE → PROMOTE → REFINE → ARCHIVE → FORGET
           ↑          |
           └──────────┘  (refine loops)
```

### CREATE
**Trigger:** New entity, fact, or observation enters the system.
**Sources:** Bulk import, email detection, manual add, conversation extraction.
- Graph entities: INSERT INTO `entities` via tools/query.py (set `status='reference'` for bulk-imported, un-contacted entities)
- Memory file: check routing decision first, create the topic file, add to MEMORY.md index

### PROMOTE
**Trigger:** First real interaction with a reference entity.
**Automation:** the `people_manager.py` PostToolUse hook auto-promotes on outbound email.
- Graph entity status changes from `reference` → `active`
- Promote when: you send them an email, they confirm a role on a project, you have a real conversation
- Do NOT promote just because you read their name in a list or bulk import

### REFINE
**Trigger:** New information about an existing entity, or existing info becomes outdated.
- Affiliation change: invalidate old `works_at` edge (`valid_until = today`), create new one (`valid_from = today`), update entity `data.affiliation`. Both edges preserved for timeline.
- Memory file: UPDATE the existing file (don't create duplicates), note what changed
- If file content exceeds 20K chars: split by subtopic

### ARCHIVE
**Trigger:** Entity or knowledge is no longer actively relevant but has historical value.
- Graph: `python tools/query.py "UPDATE entities SET status='archived', updated_at=datetime('now') WHERE id='<entity-id>'"` (relationships preserved)
- Memory files: move to `memory/archive/` and remove from the MEMORY.md index; DB docs: `reference_docs` with `category='archived'`
- Never delete outright (archive preserves the audit trail)
- When: person unresponsive 1yr+, event/project completed 30+ days ago, event-specific files after wrap-up

### FORGET
**Trigger:** Explicit request only. NEVER automatic.
- Hard delete for: confirmed duplicates, data entry errors, wrong person
- Memory file: delete the file + remove from MEMORY.md (only during monthly consolidation, only 6+ months archived)
- **Never forget:** learnings, relationship history, audit trail, anything the operator explicitly said to remember

### Automated Triggers

| Event | Stage | Automation |
|-------|-------|------------|
| Bulk contact import | CREATE | Import creates `reference` entities |
| Outbound email sent | PROMOTE | `people_manager.py` hook promotes recipient |
| Contact confirms a role | PROMOTE + REFINE | Update status + relationships |
| Affiliation change | REFINE | Manual update + invalidate old relationship |
| Error fixed | CREATE | Hook reminds to log a learning |
| Event/project ends + 30 days | ARCHIVE | Monthly consolidation flags candidates |
| Monthly consolidation | ARCHIVE + FORGET | See Section 6 below |

---

## 5. Memory Staleness & Decay

Not all memory ages equally. Use these rules to keep the system fresh:

### Staleness Categories

| Category | Example | Shelf life | Action when stale |
|----------|---------|-----------|-------------------|
| Event-specific | files tied to one event or project cycle | Until wrap-up complete | Move to `memory/archive/` or reference_docs category='archived' |
| Operational notes | action_items table (P0/P1 items) | Until the next cycle | Mark DONE, create new items for the next cycle |
| Process docs | `project_*.md` files | 6 months | Review, update, or archive |
| Learnings | `learnings` table entries | Indefinite (if promoted) | Archive only if superseded |
| Feedback | `feedback_*.md` files | Until absorbed into learnings | Archive after migration |
| Reference data | `reference_*.md` files | Until tools change | Update when tool versions change |

### Staleness Signals

- **File not referenced in 3+ sessions** → candidate for archive
- **Information contradicts current skill/CLAUDE.md** → update or delete
- **Event date has passed by 30+ days** → archive event-specific files
- **Tool/API has changed** → update reference docs immediately

### Archive Rules

- Move memory files to `memory/archive/`; DB docs to `reference_docs` with `category='archived'`
- Remove from the MEMORY.md index
- Never delete outright (archive preserves the audit trail)
- During monthly consolidation: review archived entries, delete only if older than 6 months and truly obsolete

---

## 6. Monthly Consolidation Pass

Run monthly (or after each major event/project cycle, whichever comes first):

1. **Check file sizes.** Run the size check (Section 4). Split or archive anything over limits.
2. **Archive stale entries.** Use staleness rules (Section 5).
3. **Merge overlapping entries.** If two topic files cover the same subject, merge into one and delete the other.
4. **Compress old notes.** Detailed notes from 2+ cycles ago: summarize to key lessons only.
5. **Prune MEMORY.md.** Remove entries for archived files. Keep under 150 lines.
6. **Review the learnings table.** Promote active learnings that are proven. Archive obsolete ones.
7. **Check for orphaned cross-references.** Grep for `-> filename.md` patterns that point to deleted/moved files.
8. **Review audit logs.** Query the `audit_events` and `bus_events` tables via tools/query.py for frequent-edit patterns (files touched often might need better structure).
9. **Clean changelog entries.** `DELETE FROM reference_docs WHERE category='changelog' AND updated_at < date('now', '-60 days')`.
10. **Update "Last updated" dates** on files that were touched.
11. **Graph: archive stale entities.** `python tools/query.py "SELECT id, name FROM entities WHERE status='active' AND updated_at < date('now', '-90 days')"` — review and archive if truly inactive.
12. **Graph: merge duplicate entities.** Search by name for duplicates, merge edges, delete the duplicate.
13. **Graph: verify active entities.** Spot-check 10 active entities for data currency.

## 7. Common Mistakes This Skill Prevents

| Mistake | What should happen instead |
|---------|---------------------------|
| Hardcoding setup-specific IDs/tools into the runbook | The runbook is operator-portable: say "schedule it in your social tool", not a private account ID or one of this repo's skill names |
| Creating a new memory file when one exists for that topic | Search first, update the existing file |
| Adding a checklist to a skill but not the runbook | Both may need it: skill version (workflow + routing), runbook ai_prompt version (the invocation recipe for that step) |
| Putting conversation-specific state in memory | Don't persist ephemeral info. Tasks and plans are for current-session tracking |
| Runbook referencing this repo's DB / MCP / skills | The runbook is for the operator's own assistant (shared cloud tools, inline templates, agent-executable ai_prompts); never name ops.db / query.py / your MCP / your skills |
| Dumping full content into MEMORY.md | MEMORY.md is an index. One line per file with a brief description |
| Editing rules files without discussing with the operator | Rules are org-wide policy. Confirm before changing |

---

## 8. After Every Edit

1. **Re-read the file you just edited.** Does it still make sense as a whole? No orphaned sections?
2. **Check for duplicates.** Did you just write something that already exists in another file?
3. **Update cross-references.** If the edit changes anything referenced elsewhere (counts, dates, statuses), find and update those too.
4. **Change tracking.** Changes are tracked automatically by the `session_lifecycle.py` hook. No manual logging needed.
