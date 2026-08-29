# ops-kit

The generic infrastructure of a personal operations system driven by Claude Code. One SQLite database as the source of truth, search over everything in it, a hook chain that keeps the assistant honest, an MCP server that exposes the system as tools, and a learning loop so mistakes turn into rules. It ships empty. There is no data in this repo, only structure.

I built the system this was carved from in my own time, starting in March 2026, to run my day job (operations for a research nonprofit: events, judges, speakers, participants, a lot of email). This repo is the part of it that is not about that job: the plumbing any operator could put under their own work. Everything specific to my employer stayed out.

## What you get

**One database.** `data/ops.db` holds people, emails, chat messages, tasks, learnings, episodes and reference docs, with FTS5 indexes on all of them. A second file, `data/vec.db`, holds sqlite-vec embeddings. `tools/query.py` is the CLI for both: raw SQL plus shortcuts like `schema`, `people`, `search`, `dossier`, `cross`, `learnings`, `health`.

**Search.** Keyword, vector and graph signals fused with reciprocal-rank fusion (`search/rrf_search.py`, `search/hybrid_search.py`). Person dossiers and cross-table lookups sit on top. Embedders exist for every table that carries text.

**The hook chain.** Claude Code hooks in `hooks/`: a session lifecycle that logs every session and harvests learnings at the end, a safety guard that blocks writes to places you mark protected, a quality gate that stops AI-sounding phrasing and em dashes in outbound text before they get written, a prompt-injection scanner on tool output, a context injector that surfaces relevant learnings on every prompt, and a people capture hook that files new contacts into the database as you work. Two extra layers sit next to the quality gate: `slop_stats.py` scores structural tells in prose (warn only) and `pangram_check.py` is a client for a paid detector you can run on a draft before it goes to someone who might run one themselves.

**The MCP server.** `core/mcp_server.py` exposes the system as `ops_*` tools (find, cross, deep, query, tasks, inbox, health, brief, and a confirm-gated write), so the assistant queries the database in-context instead of shelling out.

**Learning loop.** Learnings are rows with a lifecycle, spaced-repetition scheduling and a residency tier. The ones that matter every turn get rendered into an always-on rules block. `learning/` holds capture, retrieval and health checks; `memory/` holds the file convention for the assistant's cross-session memory.

**Tasks.** A task manager with urgency scoring by stakeholder tier and stage, a daily plan ritual (commit three to five items, focus shows only those), subtasks with contingencies, recurrence, dependencies and a sweep that matches git changes to open items.

**Inbox and comms.** Lane-based triage with SLAs, a FAQ retrieval gate for inbound questions, a monitor that drafts replies for review, and a single write path (the steward bus) that validates, resolves identity and stamps provenance on every canonical write. Nothing in this repo sends anything. Comms tooling stops at drafts.

**Brief and sync.** Adapters that pull email, Discord, Beeper (Slack, Signal, WhatsApp), calendar and meeting notes into the database, and a morning brief that classifies what needs you.

**Autonomy and durability.** A nightly maintenance job, a short-interval off-session tick, health checks, drift detection, backups, a restore drill and a retention pack. Schedule them with your OS scheduler once you trust the system.

**Skills.** A small set of generic Claude Code skills (`wrap-up`, `daily-debrief`, `knowledge-ops`, `triage-inbox`, `weekend-maintenance`, `system-rebuild`, `system-implement`, `humanizer`). The domain skills that run my actual job are not here.

## What is deliberately not here

No data of any kind: the databases are created empty by `db/init_db.py`. No credentials: keys come from environment variables or the OS keyring. None of the event, judge, speaker or participant tooling from the system this came from, and none of the people it holds. No one-off scripts. No campaign or outreach content.

## Running it

Open [INSTALL.md](INSTALL.md). It is written for your Claude Code to execute top to bottom, and every step ends with a check and its expected result. Python 3.12, a venv, `pip install -r requirements.txt`, `python db/init_db.py`, `python tools/selfcheck.py` (nine checks, all should pass on a fresh install), then wire the hooks and the MCP server into your Claude Code settings. The whole thing runs offline; LLM keys and chat bridges are optional and connect one at a time in `config.toml`.

## Where it came from

This was carved out of a live production system, not written from scratch as a kit. A few modules were too tied to the original's domain to ship (the LLM claim-extraction and enrichment layer is the main one). The schema still carries a few column names from that domain (`is_judge`, `is_speaker`, `hackathons_participated`, `prize_total` on `people`); they are unused flags here, harmless to rename or ignore. Where a shipped module depends on one of those, the stub says so out loud when you hit it and skips cleanly. Some code paths have only ever run on one machine, mine, so expect to adapt a path or two. If something looks wrong, it probably is; fix forward, the code is yours.

Built by Kamil. MIT, see LICENSE.
