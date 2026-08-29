# ops-kit

![tests](https://github.com/kamil-blip/ops-kit/actions/workflows/tests.yml/badge.svg)

ops-kit is the system I use to **find people for AI safety work and get them to show up**: the judges, speakers and participants of research hackathons. It is **a headhunting funnel applied to field-building**. For every hackathon and every track, I **write an ideal candidate profile**, **source candidates against that profile** from a database and a rubric, **vet each one**, and **reach out to get them to participate**. Every state change is recorded, **conversion and delivery are measured per search**, and what the research lead says about the list changes the next profile.

The goal behind it: **give mid-career and senior professionals a cheap first test of working on AI safety**. A weekend sprint with a real problem, real judges from the field and a real review is a lower bar than a fellowship or a job application, and for many people it is **the first time they engage with the field at all**. Sourcing is the machinery that decides who gets invited and makes sure they arrive.

This repository is the generic, data-free version of that machinery, plus the infrastructure it runs on (a database with provenance, search, a Claude Code hook chain, a learning loop). It ships empty: no people, no events, no credentials. Built in my own time from March 2026 to run my job at a research nonprofit; everything specific to the employer stayed out.

## Contents

- [The funnel](#the-funnel)
- [What it produced, January to August 2026](#what-it-produced-january-to-august-2026)
- [Run it](#run-it)
- [How a search runs](#how-a-search-runs)
- [Rubrics, scoring and validation](#rubrics-scoring-and-validation)
- [Assignment](#assignment)
- [Where the leads come from, and what is counterfactual](#where-the-leads-come-from-and-what-is-counterfactual)
- [Data provenance, consent and acceptable use](#data-provenance-consent-and-acceptable-use)
- [The infrastructure underneath](#the-infrastructure-underneath)
- [Tests](#tests)
- [What is not here](#what-is-not-here)
- [Origin and licence](#origin-and-licence)

## The funnel

Sourcing people for a hackathon has the same shape as sourcing candidates for a hire: a brief, a profile, a database, a ranked list, a human filter, outreach, tracking, feedback. This is the version in this repository.

| Step | In this repository |
|---|---|
| Brief | What a sprint and each track needs: topics, seniority, conflicts to avoid |
| **Ideal candidate profile** | One per hackathon and per track: seniority tier, track-fit signals, where to look, exclusions (`docs/profiles.md`, `screening/rubrics/`) |
| **Sourcing against the profile** | Database search plus a rubric scorer with an evidence quote per criterion (`search/`, `screening/score.py`) |
| Vetting | Each lead checked against the guide: track fit, availability, seniority, identity, red flags (`docs/vetting-guide.md`, `pipeline/verify.py`) |
| **Outreach** | Personalised invites with an interpolation lint and a banned-phrase check (`pipeline/templates.py`) |
| Tracking | A state machine per candidate per search; who needs a follow-up, who confirmed but has not delivered (`pipeline/tracker.py`, `pipeline/funnel.py`) |
| Feedback | Delivery and completion per search feed the next profile; comments become learnings (`learning/`, `examples/sourcing/feedback.py`) |
| Assignment | Reviewers matched to work under coverage, load and conflict constraints (`screening/assign.py`) |

## What it produced, January to August 2026

Measured on the source system's database on 28 and 30 August 2026, restricted to the eight sprints that ran in that period. Every figure is a count from a named table; nothing is estimated.

| Measure | Value |
|---|---|
| Sprints sourced for | 8 (a ninth in preparation), one fellowship alongside |
| Candidates worked (distinct people, judge and speaker searches) | 568 |
| Confirmed judges | 250 |
| Confirmed speakers | 81 |
| Acceptance rate, per search | 32% to 75% |
| Judges who delivered their reviews, per search | 82% to 94% |
| Average days from first contact to confirmation, per search | 2.7 to 6.0 |
| Organisations represented among confirmed judges, on record | 43 (a floor; the graph has an organisation edge for 59 of the 250) |
| Wrong-person rate caught in one scraped cold list, before verification became mandatory | 9 of 44 (20%) |
| Submissions pre-screened with a rubric and two model families | 754 |
| Rubric scoring validated against human reviews | 121 projects; rank correlation 0.43 for the better model, 0.21 for the weaker, 0.57 between the two |
| Assignments produced by the solver across three sprints | 1,732; largest run 770 assignments over 104 reviewers, zero conflicts, zero uncovered items |
| Partner, co-organiser, funder and sponsor organisations | 40 |
| Local hubs and venues | 16 hubs, 41 venues in 34 cities |
| Submitted projects and reviews recorded | 1,023 projects, 1,913 reviews |

What these numbers are not: a participant count (signups are not stored per event in the database the way judges are), or a claim that every confirmed judge was new to the field. The section on counterfactual value below says what is measured and what is not.

## Run it

Python 3.12 or newer. Everything runs offline on fictional data; no key is needed for anything below.

```
git clone https://github.com/kamil-blip/ops-kit
cd ops-kit
python -m venv .venv && .venv\Scripts\activate     # source .venv/bin/activate on macOS and Linux
pip install -r requirements.txt

python pipeline/demo.py                # two fictional searches, 30 candidates walked through the states, funnel + chase + reconcile
python screening/score.py --rubric screening/rubrics/example-ops-generalist-role.json --records screening/examples/candidates.json
python screening/assign.py --demo      # 40 fictional items, 12 fictional reviewers, solved with coverage and conflict constraints
python screening/validate.py --synthetic
python screening/bias.py --demo

python scripts/setup_paths.py && python db/init_db.py && python examples/sourcing/run_demo.py
                                       # the same search on the full database: candidates written through the
                                       # provenance-stamped write path, shortlist with evidence, feedback stored as a learning
python tools/selfcheck.py              # nine install checks
pytest -q                              # 60 tests
```

Real output of each command is in the docs and READMEs next to it.

## How a search runs

1. **Brief.** The research lead writes what the sprint needs per track. I ask the same questions a headhunter asks a hiring manager: what does a strong reviewer or participant for this track look like, who must not be on it, how many do we need, by when.
2. **Ideal candidate profile.** One per hackathon and per track, for judges, speakers and participants: seniority tier (senior, mid, junior, each mapped to what they can be assigned), the signals that indicate track fit, where such people are found (past judges, co-organiser referrals, hub organisers, inbound mail, cold lists, partner programmes), and exclusions. `docs/profiles.md`.
3. **Sourcing candidates against the profile.** The database is searched against each profile. A scorer applies the rubric and attaches a verbatim evidence quote to every criterion score; anything the scorer cannot quote, it cannot score. The list comes back ranked. Must-haves are gates and fail loudly; nice-to-haves are points. This is the step that decides who gets invited to each hackathon.
4. **Vetting.** Every lead is checked against `docs/vetting-guide.md`. The guide is specific about where the signal is: for AI safety researchers, LinkedIn is weak; a MATS page, a personal site, the Alignment Forum or Scholar is where the evidence sits. Identity and email are verified per person before any cold wave, because a scraped list once matched the wrong individual in 9 of 44 rows.
5. **Outreach to get them to participate.** Templates rendered per person, linted for unresolved placeholders and banned phrases; history first, two bullets, one ask, an easy out. Recruit two to three times the number needed; expect a third to half to decline. The rules learned from failures are in `docs/outreach.md`, each with when, then and because.
6. **Tracking.** Every reply moves the candidate's state; illegal transitions are refused by a trigger. The tracker lists who needs a follow-up, who is in the dead zone, and who confirmed but has not delivered. An acceptance is not a confirmed candidate until the row exists. `docs/states.md`.
7. **Feedback.** Delivery and completion per search go back into the profile, and the research lead's comments on the list are stored as learnings the assistant surfaces on the next similar brief.

## Rubrics, scoring and validation

A rubric is criteria with weights; must-haves are gates, nice-to-haves are points; each criterion score requires an evidence quote; the composite is computed in code, never by the model. Two fictional rubrics ship: one for matching people to a technical research track, one for a generalist operations search with financial and HR systems ownership as gates. `screening/prompt.md` is the scoring prompt with the reason for each rule, including a pre-check for instruction-like text inside a record. `docs/rubrics.md` is the 15-minute method for turning a brief into a rubric and the failure modes to watch (keyword density scored as competence, drift across model versions, self-preference for model-styled text).

The scorer was not trusted until it was measured. Method in `docs/validation.md`: score a set that humans have already reviewed, compute rank correlation and mean absolute error per model, compute agreement between two model families, look at cost per item, and decide what the scores may be used for. On the source system the better model reached a rank correlation of 0.43 with human reviewers on 121 projects, the weaker 0.21, and the two models agreed with each other at 0.57 (0.71 on a second event). The decision that followed: scores are used to match work to reviewers, never to pick winners. `screening/validate.py --synthetic` runs the same analysis on generated data so the method is visible without the data.

## Assignment

Once a reviewer pool is confirmed, matching reviewers to items is a constrained optimisation, not a spreadsheet. `screening/assign.py` is a mixed-integer linear programme (scipy, HiGHS) with coverage floors per item, load bands per reviewer, at least one senior track-capable reviewer on every item, and hard conflict-of-interest exclusions. On the source system the largest run placed 770 assignments over 104 reviewers with zero conflicts and no uncovered items. `screening/bias.py` corrects for reviewer severity with paired comparisons and says in its docstring what it cannot correct (expertise).

## Where the leads come from, and what is counterfactual

Every confirmed judge in the source system carries a `contacted_by` and a source. For 2026: 129 of the 250 were contacted by me directly; 20 came through a referral broker; 17 were captured automatically from inbound mail by the system; 12 came from local hub organisers; 5 from a partner programme's recommendations; 6 from the research lead. Signups are attributed by a fixed UTM scheme across 531 published posts, so the channel that brought a participant is known. `pipeline/funnel.py` prints the confirmed-by-source mix for every search.

What is measured: conversion and completion per search, per source, per channel. What is not measured yet, and would be the first thing I would build for a sourcing team: a metric for the value of a new lead or a new data source, the counterfactual question. Which confirmed judges would not have been reached through the existing pool; which participants would not have found the sprint, or the field, otherwise. The attribution is in the tables; the metric on top of it is not. Judges from 43 organisations on record, including several frontier labs and safety organisations, is a coverage statement, not a counterfactual one, and I would not present it as more than that.

## Data provenance, consent and acceptable use

The question a talent database has to be able to answer for any source: where did this come from, how are we allowed to use it, and would we be comfortable explaining that use to the person concerned. `docs/data-handling.md` is how this system answers it: every canonical fact carries an actor and a source reference (`comms/steward_bus.py`, `audit_events`); facts a person stated about themselves are marked as such; an identifier denylist and an embedding quarantine keep people who should not be searchable out of search; contributed content is anonymised by default; personal messaging accounts are excluded from ingestion; and nothing about a person goes into a public post unless the person supplied it. It also records the one live case where a consent question was raised on a talent pipeline by a partner's advisor, and what changed as a result, and it lists the three things still missing.

## The infrastructure underneath

The funnel above runs on a single-operator system driven by Claude Code, included here so that the demos run on the real tables rather than on mocks.

| Layer | What it is | Where |
|---|---|---|
| Database | One SQLite file, 88 tables, 27 full-text indexes, 9 vector tables, 129 triggers; created empty by `db/init_db.py` | `db/`, `core/` |
| Ingestion | Adapters that pull mail, Discord, Slack and Signal, calendar and meeting notes into the database, with sync state and ingest rejections per source | `brief/` |
| Search | Keyword, vector and graph signals fused with reciprocal-rank fusion; per-person dossiers; "who knows this organisation" graph walks | `search/`, `tools/query.py` (30 shortcuts) |
| Write path | The steward bus: validate, resolve identity, upsert idempotently, stamp actor and source; unattributed writes rejected | `comms/steward_*.py`, `core/audit_actor.py` |
| Assistant interface | 11 Claude Code hooks (session logging, learnings injected per prompt, a write guard, outbound text gates) and a 13-tool MCP server | `hooks/`, `core/mcp_server.py` |
| Learning loop | Rules with a lifecycle and spaced repetition, harvested at the end of every session, surfaced when they apply | `learning/`, `memory/` |
| Tasks and triage | Urgency by stakeholder and stage, a daily plan, subtasks; inbox lanes with response targets | `tasks/`, `comms/inbox_triage.py` |
| Durability | Nightly job, health runbook, drift detection, backup and restore drill, retention; a nine-step selfcheck | `autonomy/`, `tools/selfcheck.py` |

Design rules enforced in code rather than in a policy: one database and one write path; draft first, nothing sends; statistical checks warn, phrase and em-dash bans block; every failure becomes a written rule; hooks fail open; nothing secret in the tree.

## Tests

`pytest -q` runs 60 tests on a fresh temporary database: the schema applies and its triggers fire; the steward rejects unattributed writes and stamps attributed ones; the quality gate blocks banned phrases and em dashes; the structural linter flags cushioning and rationale prose; search ranks a keyword match first; the task manager scores urgency and hides snoozed items; a captured learning is retrieved and rendered; the selfcheck passes; and every sourcing and screening demo runs offline, twice, and resets. CI runs the suite on Ubuntu and Windows on every push.

## What is not here

- No data. Every candidate, search, score and assignment in this repository is fictional and labelled as such. The databases are created empty.
- No credentials. The optional model path in `screening/score.py` reads a key from the environment; there is no other place for one.
- Not the employer's tooling as deployed: event names, people, templates as sent, and one-off scripts stayed where they belong. A few column names from that domain remain on `people` (`is_judge`, `is_speaker`, `hackathons_participated`, `prize_total`); they are unused flags here.
- Not the model-based claim extraction layer of the source system (two models over every new thread, deterministic promotion gate); it was too tied to its data to ship. The stubs say so when reached.

## Origin and licence

Built by Kamil Alaa to run the hackathon programme at a research nonprofit from January 2026, alongside the job rather than as the job. Carved into this repository in August 2026 and re-verified from a clean environment on 30 August 2026. MIT, see [LICENSE](LICENSE).
