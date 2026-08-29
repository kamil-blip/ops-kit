# Operator's System

## Common Pitfalls (read these first, highest value)
- Never fabricate URLs, data, or outputs. If unknown, say so.
- Never auto-send emails. Draft only; the operator reviews and sends. Always confirm with the operator first.
- Confirm exact scope before bulk sends. "Send to this person" does NOT mean "send to everyone."
- Never run destructive SQL (DELETE, DROP, UPDATE without WHERE) without a backup first.
- Never push to main/master without the operator's approval.
- Check learnings before DB queries: `query.py learnings "keyword"`
- New structured data goes to `data/ops.db`, not loose .md files. Memory .md files are an index and scaffolding only.
- When the operator opens with a specific task, skip email/chat scans. Full briefing only on explicit "brief"/"debrief".
- Event data, contact data, and numbers come from the DB at runtime, never from model memory.

## Database (source of truth)
**Path:** `data/ops.db` at the repo root (SQLite, WAL mode). Resolved by `core/paths.py`: the `OPS_ROOT` env var if set, else walking up from the module to the repo root. Always use `tools/query.py` or `paths.DB_PATH`; never hardcode an absolute path.

**Query tool (always use this):**
```bash
python tools/query.py "SQL HERE"
```

**Shortcuts:**
```
query.py schema                     # all tables
query.py schema people              # columns for one table
query.py people "name"              # search by name/email
query.py search "topic"             # hybrid vector+FTS search
query.py action_items OPEN          # WHERE status='OPEN'
query.py learnings "keyword"        # search active learnings
query.py map "keyword"              # find table by topic
query.py dossier "name"             # full person dossier
query.py cross "keyword"            # cross-table search
query.py preflight "task desc"      # check workflow routes for a task
query.py preflight --all            # list all workflow routes
query.py inbox                      # lane-based email triage status (also ops_inbox MCP)
query.py inbox --lane <lane>        # SLA breaches for one lane (lanes come from your config)
query.py inbox --breaches           # all SLA-breached threads
query.py threads "keyword"          # FTS5 email_threads search (subject+body via fts)
query.py dms [name-or-keyword]      # recent Discord DM threads (+FTS content hits when keyword given)
query.py faqs [topic]               # canonical FAQs + occurrence-pending count
query.py episodes "keyword"         # FTS episodes search (topic + summary). For semantic: search/embed_episodes.py --search
query.py health                     # system health snapshot
query.py explain AI-xxx             # action item timeline
```

**Task Manager** (`tasks/task_manager.py`):
```
task_manager.py focus               # Today's plan if committed; else top 7 by urgency
task_manager.py focus --ignore-plan # Show full backlog (ignore today's plan lock)
task_manager.py plan                # Show top-7 candidates (or today's plan if committed)
task_manager.py plan candidates [N] # Show top N urgency candidates
task_manager.py plan commit ID1,ID2,ID3   # Lock today (3-5 items max)
task_manager.py plan show           # Show today's locked plan + check status
task_manager.py plan reset          # Clear today's plan
task_manager.py plan debrief "notes"  # End-of-day debrief; counts done items
task_manager.py stale-review [10]   # Items >N days idle (no status change): forced decisions
task_manager.py archive AI-xxx "reason"   # Quick archive shorthand
task_manager.py archive-suggest [7 12]    # Low-tier + low-urgency stale candidates
task_manager.py archive-bulk ID1,ID2,...  # Bulk archive a comma-separated list
task_manager.py subtask add AI-xxx "step1 | step2 | step3" [--if "contingency on last step"]
task_manager.py subtask list AI-xxx       # Show all subtasks for an item
task_manager.py subtask done AI-xxx 2     # Mark step 2 as done (1-based)
task_manager.py subtask undone AI-xxx 2   # Reopen step 2
task_manager.py subtask remove AI-xxx 2   # Delete step 2
task_manager.py subtask note AI-xxx 3 "if X then Y"   # Set contingency on existing step 3
task_manager.py urgency             # Recalculate stakeholder + urgency for all open items
task_manager.py stale               # WAITING items needing check-in (>3 days)
task_manager.py overdue             # Items past due date
task_manager.py batch @email        # Batch items by context tag
task_manager.py snooze AI-xxx "YYYY-MM-DD" "reason"
task_manager.py resolve AI-xxx "note about what happened"
task_manager.py depend AI-xxx AI-yyy  # Set dependency chain
task_manager.py recur AI-xxx "every 3d"  # Set recurrence
task_manager.py context AI-xxx "@email,@vendor"  # Set context tags
task_manager.py autotag             # Auto-tag all untagged items
task_manager.py spawn               # Create next occurrence of recurring items
task_manager.py unblock             # Auto-unblock items whose deps are done
task_manager.py sweep               # Match git changes to open items (may be done)
task_manager.py sweep --commits 5   # Limit lookback to last 5 commits
```

**Urgency model:** Stakeholder-first. Who is asking matters more than a hand-set priority.
- Tier 0 manager explicit-tag: +30   Tier 1 manager general: +20
- Tier 2 external partner: +15
- Tier 3 contact on a current/imminent project or event: +10
- Tier 4 other/internal/unknown/past: +2
- Optional event-stage boost (only if you track events in a table; degrades to 0 when absent)
- Emergency keywords ("way behind", "urgent", "critical", "asap"): +5
- Priority (P0-P3) is a residual operator override (0-6), not the main signal
- Derivation lives in `tasks/stakeholder.py`; manager identity, partner keywords, and org tokens are config-driven (fill them in at setup). Re-runs on every urgency recompute, so tuning takes effect immediately.
- Stakeholder columns on `action_items`: stakeholder_tier, partner_kind, is_manager_explicit, stage_boost, source_quote

**Daily plan ritual:** Stop parallel-day overload.
1. Morning: `task_manager.py plan` to see top-7 candidates
2. Commit 3-5: `task_manager.py plan commit ID1,ID2,ID3`
3. `task_manager.py focus` then shows ONLY today's plan (locks attention)
4. End of day: `task_manager.py plan debrief "notes about what didn't get done"`

**Subtasks + contingencies:** Each action item can carry an ordered checklist with "if X then Y" notes on each step. Use `subtask add` to populate, `subtask done N` to tick off. `subtasks_json` field on action_items; rendered automatically in focus + plan output. The session-start hook prompts `PLAN NOT SET` when no plan is committed for today.

**Brief System** (`brief/brief.py`):
```
brief.py sync                          # Download new data from configured sources
brief.py sync --gmail --beeper         # Sync specific sources only
brief.py gather                        # Show new items since last brief
brief.py gather --json                 # Full JSON for classification
brief.py classify BRIEF_ID             # Classify gathered items via LLM API (stdin)
brief.py status                        # Show sync freshness + last brief
brief.py report                        # Show latest brief summary
brief.py new-brief                     # Create briefing_reports row, print ID
brief.py apply BRIEF_ID                # Apply classification JSON from stdin
brief.py close-brief ID "summary"      # Finalize brief
brief.py registry                      # List chat registry (which chats sync)
brief.py drift-check                   # Manual drift scan (auto-fires at end of sync)
```
Note: the claim-extraction/enrichment layer is NOT included in this starter kit. The extraction steps inside `brief.py sync` are no-op stubs that log and skip; sync, gather, classify, and apply all work without it.

**FTS5 search (use MATCH, not LIKE):**
```sql
SELECT * FROM emails_fts WHERE emails_fts MATCH 'keyword' ORDER BY rank LIMIT 20
SELECT * FROM people_fts WHERE people_fts MATCH 'name' ORDER BY rank LIMIT 10
```

**Key tables:** people, person_emails, emails, email_threads, discord_messages, beeper_messages, action_items, learnings, edges, entities, episodes, observations, reference_docs, workflow_routes. All ship empty; they fill from your own life. COUNT(*) is truth.

## Identity

**This system is the operator's personal Claude Code.** ops.db, skills, memory, workflows, hooks: the operator's infrastructure. It stays with the operator across jobs. The current employer is the context, not the customer of this system.

**User:** [Operator name], [timezone].
**Currently employed at:** [org, role]. Fill in so "we" in tasks resolves correctly.

When a task says "we" (e.g., "we need to send invoices"), that's usually the org. Distinguish from "the system" / "my Claude Code" / "my tools": those are personal.

## System Vision ("The Fabric")

Two pillars (active systems):
1. **The Fabric**: knowledge graph of people, orgs, events, and topics (people/entities/edges tables) with bi-temporal tracking. Grows from your own emails, chats, and notes; goes beyond any one employer.
2. **Learning Loop**: continuous extraction during execution (learnings table, session changelogs, action items). Every session leaves the system slightly smarter.

## Workflows

**Sessions are split by task domain. One session = one charter.**

| Charter | Covers | Loads first | Hard line |
|---------|--------|-------------|-----------|
| TRIAGE/BRIEF | morning brief, triage, day plan, wrap-up | `daily-debrief` / `wrap-up` | pure manager: route, never execute |

Add your own charter rows as your domains emerge (e.g. a client-delivery or event-ops charter). Each row needs a "loads first" skill and one hard line.
Cross-domain work discovered mid-session: hand off via `action_items` tagged `@comms` / `@system` / `@triage`, don't absorb it.

**Workflow routes (automatic):** The `UserPromptSubmit` hook matches every message against the `workflow_routes` table and surfaces required skills/pre-checks as "Required workflow:" lines. Follow these before starting work. The table ships EMPTY: add a route whenever a workflow gets skipped or a new skill pattern emerges. Manage routes: `query.py preflight --all` to list, `query.py preflight "task"` to test matching.

**Once per session (first DB query):** run `query.py map "keyword"` to find the right table.
**Before any outbound comms:** run `query.py learnings "keyword"` to check for gotchas.

## Key People (fill in at setup)
- **[Manager name]**: [title]. The operator's manager (drives Tier 0/1 urgency; mirror this in `tasks/stakeholder.py` config).
- **[Coworker name]**: [title].

## Safety Rules (also enforce via settings.json deny rules and `hooks/safety_guard.py`)
- External DBs/CMSs (e.g. Notion): list your WRITABLE databases and your PROTECTED (never-write) databases in `hooks/safety_guard.py`. Ships empty; anything not allowlisted is protected by default.
- Automation platforms (e.g. Zapier): read-only always.
- Payments: only to your payment vendor's verified address ([fill in the one approved address]); never to any other address on the vendor's domain.
- VIP contacts: route through the agreed human channel, never direct automated outreach.

## Tools

**MCP-first rule:** If an MCP tool wraps the operation, USE IT instead of the underlying CLI. MCP tools are purpose-tuned, faster (one tool call vs subprocess + parse), and run inside the context window. Don't grind through Bash + query.py when one `mcp__ops-mcp__*` call does the same thing.

**ops-mcp catalog (all wrap an underlying script in this repo):**
| MCP tool | Wraps | Use for |
|---|---|---|
| `ops_find` | search/rrf_search.py (+ inline FTS fallback) | Fast cross-table search (people, learnings, action items, emails, docs, chat) |
| `ops_cross` | query.py cross | Unified "what do we know about X" ranked view |
| `ops_deep` | search/person_dossier.py + graph | Full person dossier, graph walk, or bio mode (EAV-first current bio + provenance) |
| `ops_query` | tools/query.py | Raw SQL when the MCP shape doesn't fit (e.g. window functions, custom joins) |
| `ops_email_search` | ops.db + gmail-to-sqlite | FTS over both email DBs |
| `ops_tasks` | tasks/task_manager.py | focus / stale / overdue / snooze / resolve / etc |
| `ops_inbox` | comms/inbox_triage.py | status / breaches / daily / classify / classify_new / classify_person / reconcile / scan-asks |
| `ops_brief_ops` | brief/brief.py | status / gather / report / drift_check / registry / new_brief (classify/apply still CLI: needs stdin JSON) |
| `ops_health` | query.py health / drift_check / daily_digest | summary / drift / digest |
| `ops_sync` | brief/daily_sync.py | Pull fresh data from any source list |
| `ops_fabric` | core/mcp_server.py (direct SQL) | Fabric internals: attributes EAV history, relation vocab, batched read-only `probes` (use for multi-check health passes instead of N ops_query calls) |
| `ops_faq` | comms/faq_gate.py + faq_lookup.py | Canonical FAQ search, retrieval-gate tier (draft/draft_cite/escalate) for inbound questions |
| `ops_write` | core/mcp_server.py + comms/steward_bus.py | store a learning / checkpoint / resume / status, plus confirm-gated canonical writes (bus_observation / bus_people). Writes: confirm with the operator before invoking |

**Other MCP servers:** register whatever you use (email, chat, web search, browser automation) in your own Claude Code settings. This kit assumes at minimum an email MCP or the gmail-to-sqlite adapter for mail data.

**When to bypass MCP and use CLI directly:**
- The MCP wrapper doesn't cover the operation (write paths, migrations, custom SQL shapes)
- A batch / loop where subprocess overhead is negligible
- stdin piping (e.g. `brief.py classify` consumes JSON from stdin)

**Email (draft-first, always):** whatever email tool you wire up, default to creating a DRAFT for review. Sending happens only after the operator's explicit approval on that specific message. Nothing in this repo auto-sends, and nothing you add should either.

## Durability tools
- `query.py health`: system snapshot (sync freshness, action items, ingest, drift alerts)
- `query.py explain AI-xxx`: action item timeline (related people, thread, next action)
- `brief.py drift-check`: manual drift scan. Auto-fires at the end of `brief.py sync`.
- `python autonomy/daily_digest.py --print`: yesterday's INGESTED/HEALTH/DRIFT/TRIAGE snapshot. First session of the day shows this automatically.
- `python autonomy/restore_drill.py`: prove backups actually restore. Run monthly.
