---
name: triage-inbox
description: Walk the operator through pending action_items_inbox proposals one batch at a time. Use after `brief.py sync` shows new proposals, or when the operator says "triage inbox", "review proposals", "clear inbox", or "what's pending triage". Promotes, rejects, merges, or defers each one based on the operator's call.
---

# Triage Inbox Skill

## When to invoke

- The operator says "triage inbox", "review proposals", "clear the pending", "what's in the inbox queue"
- After `brief.py sync` reports new inbox proposals

## Why this exists

The `action_items_inbox` table holds auto-extracted proposals from `brief.py` and the email/chat/transcript handlers. None reach the live `action_items` table without human review. Without a regular drain, proposals pile up into the hundreds and the review becomes impossible.

The rule behind it (three persistence layers, no crossover): `action_items` holds only contextual / variable / human-curated asks. `template_tasks` holds recurring runbook steps. `system_upgrades` holds infra/tech-debt work. Everything auto-extracted starts in the inbox until the operator approves.

## Workflow

### Step 1 — Pull the batch

```
python tasks/task_manager.py inbox list --limit 15
```

This lists up to 15 pending proposals grouped by source (`ORDER BY source, proposed_at DESC`, so newest first within each source group, NOT newest 15 overall) with `inbox_id`, source, age, suggested description, evidence quote.

### Step 2 — Pre-filter obvious decay

Before asking the operator, identify and **bulk-reject** items that are clearly stale:

- Items >30 days old with no evidence_quote
- Items with `source LIKE 'demoted:%'` older than 14 days (already demoted once, not re-promoted)
- Items whose description matches the auto-refuse patterns below (they would be refused again today)

```
task_manager.py inbox bulk-reject "demoted:auto-extracted from email" "stale auto-extracted, aged-out"
```

### Step 3 — Present the rest via AskUserQuestion

Group remaining proposals (typically 5-10 after pre-filter) into one `AskUserQuestion` call as a multi-select question:

```
Question: "Which of these proposals should I promote to action items?"
Options:
  - [AI-IN-20260516-0002] Share intro emails for the two new contacts (source: extract_action_items, quote: ...)
  - [AI-IN-20260516-0001] Confirm the Q3 budget with the finance team (source: extract_action_items, quote: ...)
  - ...
```

Selected → promote with `inbox accept`. Unselected → reject as "not actionable" (single bulk-reject by inbox_id list).

For each selected, follow up if priority is unclear or it should merge into an existing item.

### Step 4 — Apply

For each promote:
```
task_manager.py inbox accept AI-IN-... --priority P2
```

For each reject:
```
task_manager.py inbox reject AI-IN-... "reason"
```

For each merge into an existing canonical item:
```
task_manager.py inbox merge AI-IN-... AI-20260518-...
```

### Step 5 — Report

After the batch, report counts: N promoted, M rejected, K merged, plus what's still pending. If pending > 20, schedule another triage batch later.

## Pre-filter heuristics (skip the obvious junk)

**Auto-reject candidates:**
- `suggested_description` starts with a formulaic "Confirm the meeting/talk/time/details/format/duration..." phrasing (the extractor's refusal filter targets exactly these)
- `suggested_description` is empty or <20 chars
- Same `email_thread_id` already has an OPEN action_item (the inbox dedup should have caught this; if it didn't, the inbox row is stale)
- `source LIKE 'demoted:auto-extracted from email%'` — these are model-written ad-hoc captures, low signal

**Auto-defer candidates:**
- Items from a meeting-transcript source with no `evidence_quote` populated — defer 7 days

## Anti-patterns

- **Don't** present a hundred proposals in one giant question. Batch by source or by date.
- **Don't** silently accept-all or reject-all without showing the evidence to the operator. The whole point is human judgment.
- **Don't** create new ad-hoc Python scripts that write directly to `action_items` — every extractor / handler must propose into the inbox via task_manager's gated insert path. The triage workflow only works if all auto-extraction goes through the inbox.

## When NOT to triage

- Email SLA-lane triage (`comms/inbox_triage.py` / the `ops_inbox` MCP tool, surfaced via `query.py inbox`) is a different thing — that's email lane classification, owned by `daily-debrief`, not action_item proposals. Disambiguate by phrase: **"inbox triage"** (email SLA lanes → `ops_inbox` / `daily-debrief`) vs **"triage inbox" / "review proposals"** (these `action_items_inbox` proposals → this skill). Don't confuse them.
- Mid-session, mid-other-task: only invoke when the operator explicitly asks. Don't interrupt other work.

## Related

- `task_manager.py add` routes non-canonical sources into the inbox automatically; only canonical sources (manual, operator-direct, template_step, inbox-promoted, wrap-up-confirmed) write straight to `action_items`
- `task_manager.py demote-untrusted` — pulls already-created action_items with bad sources back into the inbox
- Three persistence layers rule: action_items = human-curated, template_tasks = recurring runbook steps, system_upgrades = infra/tech-debt. Record it as a learning in your own `learnings` table so it surfaces on retrieval.
