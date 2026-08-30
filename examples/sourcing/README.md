# Sourcing demo: a talent search on the kit, end to end

This folder runs one candidate search end to end: a brief, a rubric written as a Claude Code skill, a search over the candidate database, a ranked shortlist with an evidence quote for every score, a human deciding who to send, and the hiring manager's feedback stored so it shapes the next search. It runs offline, needs no API key, and writes only to the kit's own tables through the kit's own write path.

Everything in it is fictional: the organisation, the brief, the twenty candidates, their employers, the feedback. Emails are on `example.org`.

## Run it

From the repository root, after `python db/init_db.py`:

```
python examples/sourcing/run_demo.py            # seed -> search -> feedback
python examples/sourcing/run_demo.py            # again: idempotent, nothing duplicates
python examples/sourcing/run_demo.py --reset    # removes every row the demo wrote
```

The three steps can also be run one at a time: `seed_candidates.py`, `search.py`, `feedback.py`.

## What each step does

**`brief.md`** is the brief for an Operations Lead at a 12-person AI governance nonprofit in London: four must-haves, four nice-to-haves, named traits, location and sponsorship.

**`rubric-skill/`** is a Claude Code skill (`SKILL.md`) that turns a brief into a rubric, and the rubric it produced for this brief (`rubric.json`): must-haves as gates, nice-to-haves as weighted points, the brief's own wording attached to each criterion, and a calibration log. The composite is computed by the script, never by a model.

**`seed_candidates.py`** writes twenty candidates through the steward bus (`comms/steward_bus.py`), the kit's single canonical write path. Each candidate becomes a `people` row, a `person_emails` row, a person entity, an org entity, a `works_at` edge with a `valid_from` date, eight typed attributes with a source pointer (`demo_intake_form:form-NNN`), and a reference-call observation. Every row is stamped with the actor `demo:sourcing`; the staging table records each write and its outcome, so a re-run is deduplicated by idempotency key rather than by luck.

**`search.py`** loads the rubric, pulls each candidate's attributes, observations and current employer from the database, applies the gates (a failed gate ends the evaluation with the reason), then the points, and prints the ranked list with the evidence and its source on every line. It then makes the headhunter's call: the two to send, the next in line, and the least confident pick with the most likely way it turns out wrong. The shortlist is written to `shortlist.json`.

**`feedback.py`** takes the (fictional) feedback from the person who wrote the brief and records it the way the kit records anything learned: a learning row in `WHEN / THEN / BECAUSE` form, written through the steward bus with the feedback as its source quote. It then runs the same retrieval the `UserPromptSubmit` hook runs (`learning/learnings_retrieval.py`) against a new, similar brief, to show the learning surfacing on the next search.

## Output

Real output of `run_demo.py` on a fresh database (2026-08-30), cut to the top two candidates, one rejected one, and the decisions. The full run prints all twenty.

```
=== seed_candidates.py  ===
seeded (people before=0, after=20): {'people': 20, 'attributes': 160, 'edges': 20, 'observations': 20, 'quarantined': 0}

=== search.py  ===
Brief: Operations Lead, 12-person AI governance nonprofit, London (HYPOTHETICAL)
Candidates evaluated: 20. Passed all 4 gates: 7. Max points: 15.

 1. Chloe Bennett  [15/15]  Operations manager, 9-person AI safety field-building org
      gate owned_bookkeeping_payroll    pass   owned_bookkeeping_payroll=yes  <demo_intake_form:form-019>
      gate owned_hr_systems             pass   owned_hr_systems=yes  <demo_intake_form:form-019>
      gate uk_eligible                  pass   uk_eligibility=based_in_uk  <demo_intake_form:form-019>
      gate experience_range             pass   years_relevant_experience=3  <demo_intake_form:form-019>
      +4/4 automation               automation_tools=Airtable, Zapier, Claude, Python  <demo_intake_form:form-019>
      +3/3 offsite                  organised_multiday_offsite=yes  <demo_intake_form:form-019>
      +2/2 mission_interest         ai_safety_engagement=active  <demo_intake_form:form-019>
      +2/2 small_org                largest_org_size_owned=under_30  <demo_intake_form:form-019>
      +2/2 london                   uk_eligibility=based_in_uk  <demo_intake_form:form-019>
      +2/2 reliability_signal       Closed the books on time; audit passed; onboarding automation cut manual steps from 14 to   <demo_reference_call:form-019-ref1>
      note: send: every must-have on record, strong on the nice-to-haves

 2. Fatima Al-Sayed  [13/15]  Operations lead at a 30-person AI safety research org
      gate owned_bookkeeping_payroll    pass   owned_bookkeeping_payroll=yes  <demo_intake_form:form-011>
      gate owned_hr_systems             pass   owned_hr_systems=yes  <demo_intake_form:form-011>
      gate uk_eligible                  pass   uk_eligibility=based_in_uk  <demo_intake_form:form-011>
      gate experience_range             pass   years_relevant_experience=4  <demo_intake_form:form-011>
      +3/4 automation               automation_tools=Xero, Airtable, Zapier, Claude  <demo_intake_form:form-011>
      +3/3 offsite                  organised_multiday_offsite=yes  <demo_intake_form:form-011>
      +2/2 mission_interest         ai_safety_engagement=active  <demo_intake_form:form-011>
      +1/2 small_org                largest_org_size_owned=30_to_100  <demo_intake_form:form-011>
      +2/2 london                   uk_eligibility=based_in_uk  <demo_intake_form:form-011>
      +2/2 reliability_signal       Closed the books on time; audit passed with no findings; ran the 2026 retreat for 28 peopl  <demo_reference_call:form-011-ref1>
      note: send: every must-have on record, strong on the nice-to-haves

 ...

 8. Tomasz Wierzbicki  [0/15]  Freelance operations consultant for startups
      gate owned_bookkeeping_payroll    FAIL   owned_bookkeeping_payroll=no  <demo_intake_form:form-002>
      note: do not send: fails owned_bookkeeping_payroll, owned_hr_systems

The human filter:
  send: Chloe Bennett (15/15), Signal Field Collective
  send: Fatima Al-Sayed (13/15), Lattice Alignment Research
  next in line, not sent: Ada Okonkwo (12/15)
  least confident: Fatima Al-Sayed. Most likely way this is wrong: the gate evidence is self-reported on an intake form; if the reference call contradicts it, the pick fails. Also nothing on record for: none.

shortlist written: ...\examples\sourcing\shortlist.json

=== feedback.py  ===
Feedback received:
  Feedback one week after the list (fictional): the second candidate was strong and is interviewing. The first withdrew after the screening call; their current organisation made a counter-offer the same week. The next-in-line profile we would not have interviewed: the automation on their form was a leave tracker in Airtable, not the kind of pipeline we need.

Learning written through the steward bus: id=1 LRN-DEMO-SOURCING-001 status=active (bus result: promoted)

On the next prompt like: 'new brief: operations lead for a 10-person AI policy nonprofit, build the shortlist'
the context injector would surface:
  Related learning: Sourcing shortlist: ask for the one system a candidate built, and ask whether they are actively looking, before sending. WHEN: shortlisting for a role where automation is a named must-have or a heavily weighted nice-to-have, and a candidate currently holds a similar role at a similar-sized organisati

=== summary ===
brief -> rubric (rubric-skill/rubric.json) -> 20 fictional candidates seeded through the steward bus
-> gates then points with an evidence quote per criterion -> two sent, one flagged as the bet
-> hiring-manager feedback stored as a learning that surfaces on the next similar brief.
Everything above ran offline against data/ops.db. Re-run is idempotent; --reset removes it all.
```

A second run prints `people before=20, after=20` and the same counts; the staging table shows 261 promoted rows for the actor `demo:sourcing` and no duplicates. `--reset` leaves `people`, `attributes`, `edges`, `entities`, `observations`, `learnings` and `staging` at zero.

## What is honest about this and what is not

The scorer is deterministic: attribute values and keyword hits against weighted criteria, with the evidence quote printed. That is on purpose for a demo that must run without keys, and it is the part of the design that makes a list auditable. It is not the scoring the source system uses on real candidates. There, an LLM pre-screening layer reads the full profile against the rubric and writes per-criterion scores with quoted evidence, and it was only trusted after being validated against human reviews (two model families, rank correlation against 121 human-scored items, the weaker model discarded). That layer stayed with its data and did not ship in this kit. Rebuilding it here is a skill that produces the per-criterion JSON plus one table to hold it, with the same gate-then-points step on top.

The attributes are self-reported on a fictional intake form. The "least confident" line says so, because that is the real failure mode: a gate passed on a form claim that a reference call contradicts.

The learning surfaces through the kit's own hybrid retrieval (FTS5, with the vector arm disabled in the demo so no embedding model loads).

## Files

```
brief.md                 the brief (HYPOTHETICAL)
rubric-skill/SKILL.md    the skill that turns a brief into a rubric
rubric-skill/rubric.json the rubric for this brief
seed_candidates.py       20 fictional candidates through the steward bus; --reset removes them
search.py                gates, points, evidence, the human filter; writes shortlist.json
feedback.py              hiring-manager feedback as a learning; shows it surfacing
run_demo.py              the three steps in order
_common.py               paths, connection, demo tag
```
