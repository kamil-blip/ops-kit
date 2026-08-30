# From a brief to a rubric in fifteen minutes

A rubric is the contract between the person who described the role (or the track) and the scorer that will apply it to a few hundred records. Written well, it is the thing that makes a model's list calibrated and a human's filtering fast. Written badly, it scores keyword density and calls it fit.

## The fifteen minutes

**Minute 0 to 3: read the brief as the requester wrote it.** Not as you would rewrite it. List the requirements in the order they gave them; the order is information about what they will notice first when the list arrives. Mark each one "must" or "nice" using their words, not your inference. If the brief says "helpful but not required", it is a nice-to-have with a low weight, however much you care about it.

**Minute 3 to 8: turn the musts into gates.** A gate is pass or fail, with evidence. "Has owned bookkeeping and payroll" is a gate; "strong finance background" is not, because nothing in a record can fail it. For each gate write the description in a way that says what evidence would pass it: a role title, a bullet naming the owned system, a number. Then list the signal phrases a record would contain if it passed. Keep the list literal; synonyms are cheap to add later and expensive to guess wrong now.

**Minute 8 to 12: turn the nice-to-haves into points criteria.** Each gets a weight (the brief's emphasis, not yours), a one-line description, signal phrases, and three anchors: what a 5 looks like, what a 3 looks like, what a 1 looks like. The anchors are what the scorer scores against, so they must describe evidence, not adjectives. "Built automations for several teams that ran unattended for months" is an anchor. "Excellent automation skills" is not.

**Minute 12 to 15: decide what the composite does not decide.** Location, availability, seniority band and conflicts of interest are usually better as gates or as sort keys than as weighted points, because a hiring manager reads them as filters. Write the note at the bottom of the rubric that says which ordering the scorer enforces regardless of weights: a record that passes all gates with 3s beats a record that fails one gate with 5s.

Save it as JSON (see `screening/rubrics/`). `screening/rubric.py` validates it and renders the prompt block; `screening/score.py` applies it.

## Rules the code enforces

- Evidence quote per criterion, verbatim from the record. A score without one is voided to 1 with confidence 1. This is the single rule that stops a model from inventing fit.
- The composite is computed in code from the per-criterion scores and the weights. The model never adds up its own numbers; it is bad at arithmetic and worse at weighting.
- Confidence per criterion (1 to 5). The human filter sorts by low confidence first, because that is where the model was guessing.
- Gates are never traded against points. Gate failures are reported below every pass.

## Calibrate before you trust it

Run the rubric on records where the outcome is already known: past searches with the requester's feedback, past panels where the reviewers did or did not deliver, past cohorts where the selected people did or did not finish. Score them blind, then compare (`screening/validate.py`). What you learn is usually one of three things: a gate is too literal (it fails good records that used different words, so add signals), a points criterion has no signal (the model scores it at random, so drop it or rewrite the anchors), or the weights do not match what the requester acted on (so change the weights, not the scores).

Keep every rubric versioned. When the model changes, re-run the calibration set; scores drift across model versions even when the rubric does not.

## Failure modes to watch

**Keyword density scored as competence.** A record that says "payroll" four times is not more likely to have run payroll than a record that says it once. The deterministic scorer in this repo has this weakness by design (it counts distinct signals, not repetitions, which helps but does not cure it); a model scorer has it in subtler form. The anchors and the evidence-quote rule are the defence; so is reading the top ten by hand every time.

**Drift across model versions.** The same rubric on a new model gives different distributions. Anything you validated is validated for one model at one point in time. Re-run the calibration set on every model change and keep the result next to the rubric.

**Self-preference for model-styled text.** Models rate polished, model-shaped writing higher than plain human writing that says the same thing. Records written by a model to fit the rubric will score well on the rubric. Quoted evidence helps; a second-source check (a personal site, a scholar profile, a lab page) helps more; asking for one concrete artefact helps most.

**Demographic and geographic drift.** Names, institutions and English fluency leak into scores. The rule here is mechanical: score the record's claims, quote the evidence, and audit the top and bottom of the list for anything that looks like a pattern of who rather than what.

**Gates that are secretly points.** "Interest in AI safety" written as a gate will fail the candidate the brief most wanted. Read the brief again.

**Overselling the batch to the reviewer.** A rubric is also a promise to the people you send records to. A reviewer told "top-tier submissions" who receives five weak ones withdraws, and that was a fit mistake on the sender's side.
