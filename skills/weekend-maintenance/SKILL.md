---
name: weekend-maintenance
description: Weekly system maintenance + quality check the operator runs on weekends. Walks through 7 phases — sync, action-item audit, people audit, merge sweep, system-upgrades review, classifier corrections, wrap. Use when the operator says "weekend maintenance", "weekly cleanup", "weekend pass", "quality check", or "run the weekend audit". Manual only — not scheduled. Pre-requisites are loose (skips phases when nothing to do).
allowed-tools: [Read, Write, Edit, Grep, Glob, Bash, Agent]
user-invocable: true
---

# Weekend Maintenance

## When to invoke

- The operator says "weekend maintenance", "weekly cleanup", "quality check", "weekend pass", "run the weekend audit", or any close variant.
- Saturday/Sunday and the operator opens the session asking to clean up.

**Do NOT auto-invoke.** Manual only. The skill walks the operator through decisions; it does not run unattended.

## What this owns

The weekly cleanup ritual. Goal: keep `action_items` shareable (work-deliverable only), keep the people graph sourced (no hallucinated facts), keep duplicates merged, keep the system_upgrades backlog warm, and keep the brief.py classifier learning from corrections.

## Pre-flight

```bash
python brief/brief.py status                     # are sources stale?
python tools/query.py learnings "weekend"        # any new gotchas?
python tools/query.py "SELECT slug, title, updated_at FROM reference_docs WHERE slug LIKE 'people-audit-%' ORDER BY updated_at DESC LIMIT 3"
```

## The 7 phases

Run in order. Each phase is independently skippable if there's nothing flagged.

### Phase 1 — Sync (5 min, often skipped)

If `brief.py status` shows any source stale (>4h for mail, >24h for chat sources):

```bash
python brief/brief.py sync
```

If everything is FRESH, skip Phase 1.

### Phase 2 — Action-item audit (10 min)

Identify items that don't belong in `action_items`:

```bash
# Items missing context_slug or source_type
python tools/query.py "SELECT item_id, priority, context_slug, source_type, substr(description,1,90) FROM action_items WHERE status IN ('OPEN','WAITING','BLOCKED') AND (context_slug IS NULL OR context_slug='' OR source_type IS NULL OR source_type='')"

# Mega-items (descriptions longer than 1000 chars are usually multi-task buckets)
python tools/query.py "SELECT item_id, priority, length(description) AS L, substr(description,1,80) FROM action_items WHERE status IN ('OPEN','WAITING','BLOCKED') AND length(description) > 1000 ORDER BY L DESC"

# Misclassification keyword scan (tune the keyword list to your own recurring leak patterns)
python tools/query.py "SELECT item_id, priority, substr(description,1,90) FROM action_items WHERE status IN ('OPEN','WAITING','BLOCKED') AND (lower(description) LIKE '%talking points%' OR lower(description) LIKE '%meeting notes%' OR lower(description) LIKE '%data maintenance%' OR lower(description) LIKE '%recurring ops%')"
```

For each flagged row, decide:
- **Keep** → backfill missing `context_slug` / `source_type`. Use `task_manager.py context AI-xxx "..."`.
- **Move to `system_upgrades`** → infra/automation/data-pipeline work. Insert a row in `system_upgrades`, mark the action item REMOVED with a forwarding note.
- **Not a task** → talking points, agendas, prep notes, recaps belong in `reference_docs` (category='notes'). Insert there, mark the item REMOVED with a forwarding note.
- **Split** → mega-item, write a one-off Python script that inserts N child action items + closes the parent DONE with a `'split into N children'` note.

Also drain the proposal inbox as part of this phase: run `task_manager.py inbox list --limit 15` and, if anything is pending, invoke the `triage-inbox` skill (its "Weekly maintenance pass" trigger points here).

### Phase 3 — People audit (10-15 min)

Re-run the people-fact provenance check on people linked to current OPEN/WAITING/BLOCKED items:

```bash
python tools/query.py "SELECT DISTINCT waiting_on_person_id FROM action_items WHERE status IN ('OPEN','WAITING','BLOCKED') AND waiting_on_person_id IS NOT NULL"
```

For each linked person:
- Check the `sources` column is populated. If empty but `headline`/`summary` are set → flag as unsourced.
- Check `lifecycle_status` is correct (active = recent contact; legacy = no interaction this year).
- Check the `observations` table has rows linking facts to their sources.
- Search for duplicates by name token: `SELECT id, name, email, lifecycle_status FROM people WHERE name LIKE '%FirstName%' AND id != X`.

Write the audit report to `reference_docs` with slug `people-audit-YYYY-MM-DD` so it's searchable later. Reuse the previous audit's report as the template.

### Phase 4 — Merge sweep (5-10 min)

```bash
python tools/query.py "SELECT status, signal, COUNT(*) FROM merge_candidates GROUP BY status, signal"
python tools/query.py "SELECT id, canonical_id, duplicate_id, signal, confidence FROM merge_candidates WHERE status='pending' AND confidence >= 0.9 ORDER BY confidence DESC LIMIT 30"
```

For each high-confidence pending candidate, inspect BOTH people rows and every table that references the duplicate id before touching anything. A merge plan must list: the FK remaps (action_items, person_emails, edges, observations, thread/chat links), which row survives, and what alias/email rows get written.

If the plan looks clean (no surprising FK touches, the alias makes sense), apply it: back up the DB first, remap the FKs to the canonical id, set `merged_into` on the duplicate row, mark the candidate `status='applied'`. Never hard-delete the duplicate.

For low-confidence pairs (0.7-0.9), inspect and either approve, reject (mark `status='rejected'`), or leave for next week. Fuzzy/non-exact merges are human-gated: show the operator, don't auto-apply.

### Phase 5 — System upgrades review (5 min)

```bash
python tools/query.py "SELECT upgrade_id, status, priority, title FROM system_upgrades WHERE status NOT IN ('DONE','REJECTED') ORDER BY priority, upgrade_id"
```

For each PROPOSED/PLANNED row:
- Has the rationale held up since last week?
- Should priority bump (more urgency now)?
- Should priority drop (lower stakes than thought)?
- Ready to start (move PLANNED → IN_PROGRESS) and hand to `system-implement`?

Adjust with `UPDATE system_upgrades SET status=?, priority=?, updated_at=CURRENT_TIMESTAMP WHERE upgrade_id=?`.

### Phase 6 — Classifier corrections (5-10 min)

```bash
# Recent classifications that look wrong
python tools/query.py "SELECT id, brief_id, sender, subject, category, priority, summary FROM classification_results WHERE created_at >= date('now','-7 days') ORDER BY created_at DESC LIMIT 30"

# Past corrections (few-shot pool)
python tools/query.py "SELECT classification_id, original_category, corrected_category, original_priority, corrected_priority, corrected_at FROM classification_corrections ORDER BY corrected_at DESC LIMIT 10"
```

For any classification that should have been different:

```bash
python brief/brief.py correct CLASSIFICATION_ID --category noise --priority P4
```

Aim for at least 2-3 corrections per week so the few-shot pool stays warm. If you spot a recurring miscategorization rule (e.g. a CMS's comment notifications → noise), edit `CLASSIFY_SYSTEM_PROMPT` in `brief/brief.py` directly.

### Phase 7 — Wrap

Write a session log entry summarizing what changed:

```bash
python tools/query.py "INSERT INTO session_logs (session_id, date, title, summary, source_file, inserted_at) VALUES (?, date('now'), 'Weekend maintenance YYYY-MM-DD', ?, 'skill:weekend-maintenance', datetime('now'))"
```

Counts to include in the summary:
- action_items audited / migrated to system_upgrades / moved to reference_docs / split
- people audited / unsourced flags / merges applied
- system_upgrades reviewed / status changes
- classifications corrected
- new learnings recorded

Then run the `wrap-up` skill or just stop. The session lifecycle hook records the session automatically.

## What this skill does NOT do

- **Implement system upgrades.** Phase 5 only reviews and re-prioritizes. Hand off to `system-implement` for build work.
- **Outbound comms.** If the audit surfaces a needs-reply that wasn't handled, queue it but don't send. Drafting and sending belong in a COMMS session.
- **Schedule itself.** Manual invocation only.

## Time budget

Total: 30-60 min on a clean week, up to 2 hours after a busy week. If you don't have 30 min, skip Phase 3 (people audit) and Phase 6 (classifier corrections) — those degrade gracefully. Phases 2 and 4 are the load-bearing ones.

## Reference

- Latest people audit: `python tools/query.py "SELECT content FROM reference_docs WHERE slug LIKE 'people-audit-%' ORDER BY updated_at DESC LIMIT 1"`
- Tables governed: action_items, system_upgrades, people, merge_candidates, classification_results, classification_corrections, reference_docs (Phase 3 audit reports + Phase 2 note parking), session_logs (Phase 7 wrap entry), learnings (read in pre-flight, new entries counted in wrap), observations (read in Phase 3 people audit)
