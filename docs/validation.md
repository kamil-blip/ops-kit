# Validating a scoring layer before trusting it

A model that scores candidates or submissions is a measurement instrument. Before its output decides who gets a list, a seat or a prize, it needs the same treatment any instrument gets: compare it against the reference method on the same items, report the agreement, and decide what it is allowed to decide.

## The method

1. **Pick a calibration set with human reference scores.** Items that people already reviewed under the same rubric: a past panel's reviews, a past search with the hiring manager's feedback on each candidate, a past cohort with completion outcomes. A hundred items is enough to see the shape; thirty is enough to catch a broken rubric.
2. **Score the set blind with at least two model families.** Two families, not two sizes of one family, because disagreement between families is the cheapest signal you have for "this item needs a person".
3. **Report four numbers per model** (`sourcing/screening/validate.py`):
   - Spearman rank correlation of model total vs human total. This is the number that matters for ranking use. Anything the team will read top to bottom is a ranking problem.
   - Mean absolute error on the total, for calibration of the scale.
   - Per-criterion exact-match and within-one rates, to see which criterion the model cannot judge. A criterion with a within-one rate near chance should be dropped from the model's job and given to a person.
   - Share of items more than two points off, which is the list of items to read by hand.
4. **Report cross-model agreement:** Spearman, MAE and disagreements for every pair of models. Where the models disagree by more than two points, route to a person; where they agree and both are far from the human score, the rubric is the problem, not the model.
5. **Compare against human-versus-human agreement** on the same items where two reviewers overlap. A model that agrees with humans as well as humans agree with each other is doing the job; a model well below that is a pre-filter at best.
6. **Decide the model's job in writing.** Ranking for a person to filter, pre-screening to route items to the right reviewer, or nothing more than a first read. The decision is a policy, not a threshold, and it is written next to the rubric with the numbers that justified it.
7. **Re-run on every model change and every rubric change.** Keep the calibration set locked and versioned; when the set changes, say why.

## Measured on the source system

Measured on the source system, spring 2026, on one research sprint with 126 submissions of which 121 had human reviews: the stronger of two model families reached a Spearman rank correlation of 0.434 with the human reviewers (MAE 2.15 on a 1 to 5 scale summed over three criteria, cost USD 5.42 for the run); the weaker reached 0.211 (MAE 2.31, USD 2.62); the two models agreed with each other at 0.571. On a second sprint the two-model cross agreement was 0.708. The decision that followed from those numbers: model scores are used to match items to reviewers and to order the work, never to rank winners. A reviewer-bias correction (paired comparisons) became standard after comparing it with mean-centering and z-normalisation, with its limit written down: it corrects severity, not expertise.

## What the numbers do not tell you

- Agreement on a calibration set is agreement on that distribution. A rubric validated on technical submissions says nothing about candidates for an operations role.
- A high Spearman can hide a systematic gap: a model can rank well and still fail to identify the one item a domain expert would flag as unsafe or off-topic. Keep a person on the tails.
- Cost matters at scale and not at all in validation. Report it, then ignore it until the method is right.
- A model that scores well today drifts. The date of the validation is part of the result.

## The policy that came out of it

Model scores are used to match items to reviewers and to order a list for a person to read. They are not used to pick winners, place candidates or reject anyone on their own. That rule is not a caution about accuracy; it is what the participants and candidates are owed.
