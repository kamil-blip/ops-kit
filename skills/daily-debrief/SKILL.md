---
name: daily-debrief
description: Morning brief (canonical owner). Invoke for "daily brief", "what's open", or any morning sync. Uses the brief.py sync + classify + apply pipeline, the canonical briefing system that writes to ops.db. End-of-day / session-close logging lives in `wrap-up`.
---

# Daily Debrief & TODO Manager

## When to Trigger
- The operator says "daily brief" or "what's open"
- Morning start (runs as part of the daily brief)
- (End-of-day / session close: use `wrap-up` instead. This skill handles intake, not session closure.)

## Integration with Task Lifecycle
This skill is the INTAKE step of the day. When scanning channels reveals a task:
1. Follow the full lifecycle: LOG (task tracker) > ACKNOWLEDGE (reply to source) > CLASSIFY > EXECUTE > CLOSE > CONFIRM
2. Tasks found in debrief that aren't urgent: file them via `task_manager.py add`, then present to the operator for prioritization
3. Tasks that need a reply NOW: flag as urgent in the summary

**Command shorthand:** `brief.py` = `python brief/brief.py`, `query.py` = `python tools/query.py`, `task_manager.py` = `python tasks/task_manager.py`. Run from the repo root.

## Core Tool: brief.py

All sync and gathering runs through the brief orchestrator:
```
python brief/brief.py <command>
```

### Available Commands
| Command | What it does |
|---------|-------------|
| `sync` | Download new data from all configured sources (email, Discord, Beeper, email_tracker) |
| `sync --gmail` | Sync only gmail |
| `sync --beeper --beeper-priority 1` | Sync only critical beeper chats |
| `gather` | Show new items since last brief (human-readable) |
| `gather --json` | Full JSON output for classification |
| `gather --since "2026-04-14"` | Custom cutoff |
| `new-brief [--session-id ID]` | Create a briefing_reports row, prints ID |
| `apply BRIEF_ID` | Read classification JSON from stdin, write to DB |
| `close-brief ID "summary"` | Finalize brief with summary |
| `report` | Show latest brief report |
| `status` | Show sync freshness for all sources |
| `registry` | List beeper chat registry (which chats sync) |

Note: the claim-extraction/enrichment layer is NOT included in this starter kit. The extraction steps inside `brief.py sync` are no-op stubs that log and skip; sync, gather, classify, and apply all work without it.

### Data Sources

| Source | Synced via | Stored in |
|--------|----------|-----------|
| Gmail | brief/daily_sync.py | emails table |
| Discord (registered channels) | brief/daily_sync.py | discord_messages table |
| Signal (registry chats via Beeper; see `brief.py registry`) | brief.py sync --beeper (REST API) + brief/sync_beeper_local.py (full local DB) | beeper_messages table |
| Slack (registry chats via Beeper; see `brief.py registry`) | brief.py sync --beeper (REST API) + brief/sync_beeper_local.py (full local DB) | beeper_messages table |
| Email threads | comms/email_tracker.py sync | email_threads table |
| Meeting transcripts (Granola, optional) | brief/granola_sync.py, or Granola MCP manually in session | reference_docs table |

## Brief Workflow ("daily brief")

### Phase 0: Pre-check
```bash
brief.py status                    # Check freshness
query.py learnings "brief"         # Check for gotchas
query.py learnings "daily"
```

### Phase 1: Sync (download new data, no reasoning)
```bash
brief.py sync                      # All sources, ~2-3 minutes
```
If only specific sources are stale:
```bash
brief.py sync --gmail --beeper     # Targeted sync
```

### Phase 2: Gather (collect new items)
```bash
brief.py gather                    # Human-readable summary
brief.py gather --json > /tmp/gathered.json   # For classification
```

### Phase 3: Classify (automated via LLM API)

1. Create a brief record:
```bash
brief.py new-brief
```

2. Run the classifier (single pipeline):
```bash
brief.py gather --json | brief.py classify BRIEF_ID | brief.py apply BRIEF_ID
```

Or step by step:
```bash
brief.py gather --json > /tmp/gathered.json
cat /tmp/gathered.json | brief.py classify BRIEF_ID > /tmp/classified.json
cat /tmp/classified.json | brief.py apply BRIEF_ID
```

The classify command:
- Calls the configured LLM API in batches of 10 items
- Includes open/waiting action items for matching (top 50 by urgency)
- Includes active projects/events for context assignment
- Includes past classification corrections for few-shot learning
- Returns structured JSON per item: category, priority, matched_action_item, summary

Categories: noise, update_only, needs_reply, new_action, close_candidate

3. If classification needs correction afterward:
```bash
brief.py correct CLASSIFICATION_ID --category needs_reply --priority P1
```

### Phase 4: Apply (write back to DB)

Pipe classification JSON to apply:
```bash
echo '[{...classifications...}]' | brief.py apply BRIEF_ID
```

The apply command:
- Stores all classifications in the classification_results table
- Updates matched action items with context notes
- Creates new action items for unmatched actionable items
- Flags close candidates (does NOT auto-close)
- Tags needs_reply items with the @email context tag
- Updates briefing_reports counters

### Phase 5: Present (triage to the operator)

Structure the output as:

```
NEEDS REPLY (N items):
  [P1] Description (AI-xxx, days overdue)
  ...

AUTO-HANDLED (N items):
  - Sender: summary (action taken)
  ...

NEW ACTION ITEMS CREATED (N):
  AI-xxx: description
  ...

CLOSE CANDIDATES (N):
  AI-xxx: reason it might be done
  ...

FYI (N items, no action):
  ...
```

### Phase 6: Finalize
```bash
brief.py close-brief BRIEF_ID "N items classified, M replies needed, K auto-handled"
```

### Phase 7: Meeting transcripts (if any unsynced)
If you use Granola (or another meeting-notes tool with an MCP), check for recent meetings not yet in reference_docs:
```
mcp__granola__list_meetings          # recent meetings; or mcp__granola__query_granola_meetings for a filtered search
```
For each unsynced meeting:
1. Get the transcript via `mcp__granola__get_meeting_transcript(meeting_id)`
2. Extract action items for the operator
3. Store the transcript in reference_docs
4. Create action items

### Phase 8: Draft replies (ONLY when the operator picks an item)
Do NOT batch-draft all replies. Instead:
- Present the "needs reply" list
- The operator picks one: "do AI-xxx"
- Load thread context from the action item
- Draft the reply in the operator's voice (load the `humanizer` skill and any comms-style rules you keep)
- Show the draft, get explicit approval, only then send
- Mark the item done, next

## Sync-Only Mode ("sync only")

When the operator just needs a data refresh without the full brief:
```bash
brief.py sync
```
No reasoning, no triage, no presentation. Just download new data. Takes ~2-3 minutes.
Use cases:
- Mid-day refresh after receiving many messages
- Before starting a task that needs current data
- Quick check before ending the day

## Session Startup (Passive)

Other sessions that aren't doing a brief can check the latest brief:
```bash
brief.py report                    # What did the last brief find?
task_manager.py focus              # Current priority items (updated by brief)
```

## TODO Tracking Rules

### Where TODOs Live
- **Ops TODOs**: `query.py action_items OPEN` (P0/P1 items for active work)
- **External tracker tasks** (optional): your team's task tracker, if you use one
- **People follow-ups**: ops.db people table notes column
- **Brief-created items**: source field starts with "brief:"

### Adding TODOs
When something new comes up during a session:
- If it needs team visibility: create it in your team's tracker AND file it via `task_manager.py add`
- If it's a quick follow-up: `task_manager.py add` with the appropriate priority

**MANDATORY for human-sourced action items (manager ask, operator chat, partner message, message-from-anyone, etc):**

The `context` field MUST contain the originating quote verbatim, with channel + approximate timestamp, e.g.:
```
ORIGINAL ASK (the manager, Slack 11:29 AM, 2026-04-28):
"<exact quoted text>"
```
Also populate whichever source ID column applies: `source_url`, `discord_message_id`, `slack_message_id`, `beeper_message_id`, `email_thread_id`, `granola_meeting_id`. Cheap at creation time; sometimes impossible to reconstruct later (the bridge may not have captured the original message).

### Closing TODOs
When a TODO is done:
- UPDATE action_items SET status='DONE' in ops.db. Mark the tracker task done if tracked there.
- If it taught us something reusable: log it as a learning

## Reminder Triggers

At the start of every session, proactively check:
- Any TODOs with deadlines that have passed or are today
- Any "waiting on reply" items older than 3 days (suggest a nudge)
- Any tracker tasks due today

## Integration with Other Skills

| What happened | What to trigger |
|---------------|-----------------|
| New proposals landed in `action_items_inbox` | `triage-inbox` skill |
| A task revealed a reusable gotcha | log a learning (see `wrap-up` learning loop) |
| A persistent file or memory entry needs editing | `knowledge-ops` skill |
| Session is ending | `wrap-up` skill |
