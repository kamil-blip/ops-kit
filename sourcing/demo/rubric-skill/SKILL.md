---
name: sourcing-rubric
description: Turn a role or track brief into a weighted candidate-fit rubric (must-haves as gates, nice-to-haves as points, an evidence quote per criterion, composite computed in code). Use when a headhunter or sourcing operator has a role brief and needs a rubric a search can apply and a hiring manager can audit.
allowed-tools: [Read, Write, Grep, Bash]
user-invocable: true
---

# Sourcing rubric

## When to use

You have a brief (a call transcript, an email, a job ad) and you need a rubric that (a) a script can apply to every candidate in the database, (b) a person can check line by line, and (c) can be tested against past searches where the outcome is known.

## What a rubric is here

A JSON file with two lists.

**Gates** are the must-haves. A candidate who fails any gate is out, whatever their points. Each gate names the attribute it reads, the values that pass, and the wording from the brief that justifies it. Gates exist because a hiring manager's "must" is not a preference; a list that ignores it wastes their time.

**Points** are the nice-to-haves and the traits. Each has a weight, the attribute or text it reads, and how it scores (exact value, keyword hit, or a numeric range). The composite is the weighted sum, computed by the script, never by a model. A model may propose a rubric; it does not score candidates in this kit.

Every criterion carries `evidence_from`: which attribute or text field the evidence quote is taken from, and the quote is printed next to the score with its source (the form, the CV, an email, a note). A score without a quote is not allowed; that is what makes the list auditable.

## Procedure

1. Read the brief. List every must-have the brief states, in its words. Quote them; do not paraphrase into something broader.
2. For each must-have, find the attribute in the database that carries it (see `platform/db/schema.sql`, the `attributes` table, and the vocabulary already in use). If no attribute exists, the gate cannot be evaluated: say so in the rubric as `"evaluable": false` rather than inventing a proxy.
3. List the nice-to-haves and traits. Give each a weight from 1 to 5. Sum of weights is the maximum composite. Keep the list short: five to eight point criteria is normal; twelve is a sign the brief was not understood.
4. Write `rubric.json` next to the brief. Run the search. Read the top ten by hand against the brief. Adjust weights only where the brief supports it, and note the change in `calibration_notes`.
5. Calibrate against known outcomes when you have them: for a past search with feedback on the outcome, apply the rubric and record how many of the candidates they rated well land in the top N. Keep those numbers in the rubric file; a rubric with no calibration history is a draft.
6. When feedback on the list arrives, record it as a learning (`WHEN / THEN / BECAUSE`) so it surfaces on the next similar brief. Do not silently edit the rubric; the learning is the audit trail for the change.

## What not to do

- Do not score mission alignment as a gate unless the brief says it is one. "Helpful but not required" is points, weight 1 or 2.
- Do not let keyword density stand in for competence. A keyword criterion is worth at most 2 points and needs the quote printed.
- Do not rank on a model's prose judgment. If a model is used to extract an attribute from a CV, the extraction carries a source quote and goes through the steward bus like any other fact.

## Output

`rubric.json` with `role`, `source_brief`, `gates[]`, `points[]`, `max_points`, `send_count`, `calibration_notes[]`. See the example next to this file.
