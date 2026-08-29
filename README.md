# ops-kit

ops-kit is the infrastructure layer of a personal operations system driven by Claude Code: one SQLite database as the source of truth for people, mail, chat, tasks and notes; search across all of it; a hook chain that logs sessions, injects context and gates outbound text; an MCP server that exposes the database to the assistant as tools; and a learning loop that turns mistakes into rules the assistant sees on every turn. It ships empty. There is no data in this repository, only structure and code.

I built the system it comes from in my own time, starting in March 2026, to run an operations job at a research nonprofit (events, judges, speakers, participants, five inboxes). This repository is the part that is not about that job. Everything specific to the employer stayed out; see [What is not here](#what-is-not-here).

## Contents

- [Problems it addresses](#problems-it-addresses)
- [What is in the box, in numbers](#what-is-in-the-box-in-numbers)
- [Quick start](#quick-start)
- [A first session](#a-first-session)
- [How it is put together](#how-it-is-put-together)
- [The same shape as a talent-sourcing product](#the-same-shape-as-a-talent-sourcing-product)
- [Design rules](#design-rules)
- [When not to use it](#when-not-to-use-it)
- [What is not here](#what-is-not-here)
- [Status and limitations](#status-and-limitations)
- [Origin and licence](#origin-and-licence)

## Problems it addresses

Each of these is a failure I had before the corresponding piece existed. The mechanism is what the kit does about it.

**The facts live in five places and the assistant sees none of them.** Mail, Discord, Slack, Signal, calendar and meeting notes are pulled into one database by the adapters in `brief/`, on a schedule or on demand. 88 tables, 27 full-text indexes, 9 vector tables. The assistant queries that database through `tools/query.py` (30 shortcuts plus raw SQL) or through the 13 MCP tools, instead of asking you to paste things in.

**Keyword search misses what vector search finds, and the reverse.** `search/rrf_search.py` fuses FTS5, sqlite-vec embeddings and graph adjacency with reciprocal-rank fusion, and `search/person_dossier.py` assembles everything known about one person (identities, relations, threads, tasks) in one call.

**The assistant forgets what it learned last week.** `learning/` stores rules as rows with a lifecycle, spaced-repetition scheduling (FSRS) and a residency tier. `hooks/context_injector.py` surfaces the relevant ones on every prompt; the top tier is rendered into an always-on block. `hooks/session_lifecycle.py` logs every session and harvests candidate learnings at the end, so the loop closes without a manual step.

**A model writes something into the database that nobody said.** Canonical writes go through one path, the steward bus in `comms/`: validate, resolve identity, upsert idempotently, stamp who asserted the fact and from which source, all inside a per-row savepoint under a registered actor. `hooks/safety_guard.py` blocks writes to targets you mark protected; the MCP write tool is confirm-gated.

**Outbound text sounds like a model.** `hooks/quality_gate.py` blocks a shared list of banned phrases and every form of em dash at write time (`hooks/slop_rules.py` is the one list both hooks import). `hooks/slop_stats.py` scores structural tells (rationale prose, reassurance beats, tricolons, uniform sentence rhythm) and warns. `hooks/pangram_check.py` is a client for a paid detector, for the rare draft that will be tested by its recipient. Nothing in the kit sends; comms tooling stops at a draft.

**Things fall through.** `tasks/task_manager.py` (36 subcommands) scores urgency by stakeholder tier and stage, runs a daily plan ritual (commit three to five items, then `focus` shows only those), tracks subtasks with contingencies, recurrence and dependencies, and sweeps git history to find items that were done without being closed. `comms/inbox_triage.py` puts every thread in a lane with a response-time target.

**The system breaks quietly.** `tools/selfcheck.py` runs nine checks in one command. `autonomy/` has a nightly job, a short-interval tick, a health runbook, drift detection, backups, a restore drill and a retention pack, meant for your OS scheduler once you trust them.

## What is in the box, in numbers

Measured on the repository at commit `a99636c`, nothing estimated.

| Item | Count |
|---|---|
| Files tracked | 104 |
| Python files, lines | 82 files, 39,553 lines |
| Database tables (`db/schema.sql`) | 88 tables, 27 FTS5 virtual tables, 1 view, 129 triggers, 177 indexes |
| Vector tables (`db/vec_schema.sql`) | 9 |
| Rows shipped | 0 |
| Claude Code hooks wired in `hooks/settings.example.json` | 11, across SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, PreCompact and SessionEnd |
| MCP tools (`core/mcp_server.py`) | 13 (`ops_find`, `ops_cross`, `ops_deep`, `ops_query`, `ops_tasks`, `ops_inbox`, `ops_health`, `ops_brief_ops`, `ops_sync`, `ops_faq`, `ops_fabric`, `ops_email_search`, `ops_write`) |
| `query.py` shortcuts | 30 |
| `task_manager.py` subcommands | 36 |
| Skills (`skills/`) | 8 |
| Install self-checks (`tools/selfcheck.py`) | 9 |
| External services required to run | 0 (LLM keys and chat bridges are optional and connect one at a time) |

Largest modules: `tasks/task_manager.py` 3,021 lines, `brief/brief.py` 2,911, `hooks/session_lifecycle.py` 1,544, `tools/query.py` 1,374, `comms/inbox_triage.py` 1,311, `core/mcp_server.py` 1,177.

## Quick start

Python 3.12 or newer. Tested on Windows; the code uses `pathlib` throughout and has no Windows-only dependency that I know of, but it has only ever run on one machine.

```
git clone https://github.com/kamil-blip/ops-kit
cd ops-kit
python -m venv .venv
.venv\Scripts\activate            # source .venv/bin/activate on macOS and Linux
pip install -r requirements.txt
python scripts/setup_paths.py     # prints: wrote ...ops-kit.pth, import check: paths.ROOT=..., OK
python db/init_db.py              # prints: init complete: both databases empty and healthy.
python tools/selfcheck.py         # prints: selfcheck: 9/9 passed
```

Then copy `config.example.toml` to `config.toml`, fill in your name, timezone and email addresses, and wire the hooks and the MCP server into your Claude Code settings. [INSTALL.md](INSTALL.md) has every step with its expected output; it is written so that your own Claude Code can execute it top to bottom and know when a step failed.

## A first session

On an empty database, these are the commands and what they print.

```
$ python tools/query.py schema
_audit_context (0): id, actor, source_ref, set_at
_table_descriptions (0): table_name, tier, description, when_to_query, key_columns, ...
... (224 lines: every table and virtual table with its row count and columns)

$ python tools/query.py "SELECT COUNT(*) FROM people"
COUNT(*)
0
(1 rows)

$ python tools/query.py learnings "install"
No active learnings match 'install'.

$ python tools/query.py dossier "Jane Doe"
No person found for 'Jane Doe'.

$ python tools/query.py health
=== System Health (Aug 29, 23:12 UTC) ===
SYNC FRESHNESS (target: <24h)
  emails            :      ?  UNKNOWN
  discord           :      ?  UNKNOWN
  ... (empty sections are normal on day one)
```

Connect one source in `config.toml`, run `python brief/daily_sync.py`, and the same commands start returning your own mail, contacts and threads. From there the loop is: morning `brief`, work inside Claude Code with the hooks capturing as you go, end the session with the `wrap-up` skill so the session log and learnings are written.

## How it is put together

```
db/          schema.sql, vec_schema.sql, init_db.py        the empty database and its initialiser
core/        _db, paths, config, validators, audit_actor,  the spine every module imports
             mcp_server
tools/       query.py, selfcheck.py                        the CLI and the install check
hooks/       11 wired hooks + slop_rules, slop_stats,      the Claude Code hook chain
             pangram_check, settings.example.json
search/      rrf_search, hybrid_search, person_dossier,    retrieval and the nine embedders
             cross_search, surface_context, embed_*
comms/       inbox_triage, faq_gate, comms_monitor,        triage, drafting for review, the steward
             steward_bus, steward_ledger, steward_resolver
brief/       brief.py, daily_sync, adapters               ingestion and the morning brief
tasks/       task_manager, stakeholder, next_moves        tasks and urgency
learning/    capture, retrieval, health, graduated_rules   the learning loop
autonomy/    nightly, tick, health_runbook, drift_check,   scheduled maintenance and recovery
             backup, restore_drill, retention_pack
memory/      MEMORY.md convention, memory_lifecycle        the assistant's cross-session memory
logging/     import_session, backfill_episodes, audit      session transcripts into the database
skills/      8 generic Claude Code skills                  wrap-up, daily-debrief, knowledge-ops, ...
interfaces/  whatsapp_bridge, transcribe                   optional chat bridge and voice notes
```

Data flow, in one line: adapters write raw rows; the steward promotes facts with provenance; search indexes them; hooks surface them to the assistant; the assistant writes back through the MCP write tool or the steward; the nightly job keeps the whole thing healthy.

## The same shape as a talent-sourcing product

I am sharing this repository with the headhunting team at 80,000 Hours as part of an application, so it is worth saying plainly where it overlaps with what they run and where it does not.

Their job descriptions (August 2026) describe the product like this: a hiring manager describes a role in a 15 to 30 minute call; Claude skills generate and adjust a rubric for candidate fit; an AI system searches a database of 16,000+ candidates and returns the top 100 to 300 leads; a headhunter filters those down to the ones worth sending; the hiring manager's feedback on the list feeds the next rubric. The Talent Database Lead posting adds the data side: grow the database, collect richer signals than a CV, and hold to a standard for responsible data use ("where did this come from, how are we allowed to use this data, and would we be comfortable explaining that use to the person concerned?").

The kit is the same pipeline with the domain removed. The mapping, component by component:

| Their step | What the kit has | Where |
|---|---|---|
| A candidate database, 16,000+ people | A people table with identity resolution (`person_emails`, `person_identities`, `person_identifiers`), typed attributes with source pointers, and a graph of people, organisations and relations with valid-from and valid-until dates | `db/schema.sql`: `people`, `attributes`, `entities`, `edges` |
| Richer signals than a CV | Observations captured from every tool output as you work (`hooks/people_manager.py`), episodes embedded for semantic search, a per-person dossier that assembles identities, relations, threads and open items in one call | `hooks/people_manager.py`, `search/person_dossier.py` |
| Rubrics as Claude skills | The same skill format (`SKILL.md` plus references), loaded on demand; the kit ships the generic ones and the pattern for a per-role rubric skill | `skills/` |
| Search the database for a brief | Keyword, vector and graph signals fused by reciprocal-rank fusion; cross-table search; a "who knows this organisation" graph walk | `search/rrf_search.py`, `search/hybrid_search.py`, `tools/query.py whoknows` |
| A human filters the list; nothing goes out automatically | Draft-first is enforced in code; the MCP write tool is confirm-gated; the steward bus is the only canonical write path | `comms/steward_bus.py`, `core/mcp_server.py` (`ops_write`) |
| Hiring-manager feedback improves the next search | The learning loop: feedback becomes a rule row with a lifecycle and spaced-repetition scheduling, surfaced on every prompt where it applies; draft-versus-final diffs are recorded for every reply a person edited | `learning/`, `comms_draft_outcomes` |
| Where did this come from, and are we allowed to use it | Every canonical fact carries an actor and a source reference; audit events on every write; an identifier denylist and a quarantine table for embeddings of people who should not be searchable | `audit_events`, `_audit_context`, `identifier_denylist`, `vec_entities_quarantine` |
| Growing the database from new sources | Adapters for mail, Discord, Beeper (Slack, Signal, WhatsApp), calendar and meeting notes, with sync state and ingest rejections recorded per source | `brief/`, `sync_state`, `ingest_rejections` |

What the kit does not have, and what the source system has that did not ship: a scoring step that applies a rubric to each candidate and writes a ranked list with per-criterion evidence. In the source system that exists as an LLM pre-screening layer, validated against human reviews before it was trusted, and it was too tied to its domain to export. Building it on this kit is a skill plus one table, and it is the first thing I would add for a sourcing use.

## Design rules

These are enforced in code, not in a policy document.

1. One database, one write path. Anything canonical goes through the steward bus with an actor and a source. Unattributed writes are rejected.
2. Draft first. No module sends email, chat or social. Drafts are produced for a person to send.
3. Warn, do not block, on statistical checks. The phrase and em-dash bans block; the structural linter and the detector only report, because statistical tells false-positive on plain and non-native prose.
4. Every failure becomes a rule. A mistake ends up as a learning row with when, then and because, and the hooks show it again when the situation recurs.
5. Fail open in hooks. A bug in a hook must never block the assistant; hooks catch their own exceptions and say so.
6. Nothing secret in the tree. Keys come from environment variables or the OS keyring under the `ops-kit` service; `config.toml`, `.env` and the databases are gitignored.

## When not to use it

- You want a hosted CRM or a team product. This is a single-operator, local-first system; there is no multi-user model and no server.
- You do not use Claude Code. The hooks, skills and MCP server assume it. The database, search and task manager work without it, but that is half the value.
- You want it to send things. It will not, by design. If you need automatic sending, you will be adding it yourself and removing a rule I would keep.
- You need the ingestion adapters to work out of the box for your stack. They cover Gmail (via gmail-to-sqlite), Discord, Beeper (Slack, Signal, WhatsApp), Google Calendar and Granola meeting notes. Anything else is a new adapter.
- You want a small dependency. It is 40,000 lines of Python. It was built to run one person's job end to end, not to be minimal.

## What is not here

- No data. `db/init_db.py` creates the databases empty and the self-check confirms zero rows.
- No credentials, and no place in the tree where one could sit.
- None of the employer-specific tooling from the source system (event, judge, speaker and participant pipelines, one-off scripts, outreach content) and none of the people it holds. A few column names from that domain remain on `people` (`is_judge`, `is_speaker`, `hackathons_participated`, `prize_total`); they are unused flags here, harmless to rename or ignore.
- No LLM claim-extraction layer. The source system runs two models over every new thread and gates their claims deterministically; that layer was too entangled with its data to ship. The stubs in `brief/` and `autonomy/` say so when reached and skip cleanly.

## Status and limitations

- Snapshot of a working system, exported August 2026 and re-verified from a clean virtual environment on 30 August 2026 (init, nine self-checks, the INSTALL steps as written, a privacy grep with zero hits).
- Single machine, single operator, Windows. Paths are handled with `pathlib`, but nothing here has run on macOS or Linux yet.
- Some modules are long (`task_manager.py` and `brief.py` are around 3,000 lines each) and lack a section map at the top. The nine `embed_*.py` scripts share boilerplate that belongs in `core/`. Both are known and listed in the audit notes rather than fixed.
- The code and comments were written with Claude Code in the loop, as the system itself is; the design, the rules and the mistakes that produced them are mine.

## Origin and licence

Carved out of a live production system, not written as a kit. Built by Kamil Alaa. MIT, see [LICENSE](LICENSE).
