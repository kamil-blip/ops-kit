# Outreach rules, each learned from a mistake

Written as WHEN / THEN / BECAUSE so the rule and its origin stay together. Names and events are removed; the incidents are real.

**Recruit two to three times what you need.**
WHEN planning a panel of N. THEN contact 2N to 3N people, and set the follow-up cadence before the first message goes out. BECAUSE across a year of panels the decline rate ran between a third and a half of everyone contacted, and the acceptance rate per search ranged from about a third to three quarters depending on how warm the pool was. A panel recruited at 1x is a panel that arrives short.

**An acceptance is not a confirmed candidate until the row exists.**
WHEN replying "welcome aboard" to an acceptance. THEN write the confirmed status in the same sitting as the reply, and cross-check every "welcome aboard" thread against the roster before assignments go out. BECAUSE six people who had accepted by email were never seated on one panel: the roster was built from the database while their acceptances sat in the inbox, and two of them had been filed under the wrong person's row. The fix is the state machine in this repository and the `reconcile` check that looks for confirmed rows with no recorded acceptance.

**Verify identity and address per person before any cold wave.**
WHEN a candidate list comes from a scrape, a name-based lookup or a data provider, especially for common names. THEN run per-person identity and email verification before the first message and record the result. BECAUSE a 44-row cold list had nine rows (20%) pointing at the wrong person: same name, different researcher or a dead address. Nine wrong-person emails in one wave is a reputation cost that does not show up in the funnel numbers.

**`invited` is not a state.**
WHEN recording outreach. THEN use `sent` for a message that went out and a reply state for the answer; never a vague "invited". BECAUSE the vague bucket held people who had never been written to next to people who had accepted, and both were reported as the same thing.

**Never retract an accepted candidate.**
WHEN a confirmed judge turns out to be a weaker fit than expected. THEN control it at assignment time: least technical sub-area only, never the sole reviewer on a project, bio not presented as a technical credential, the reason written into the row's notes. BECAUSE telling someone they are on the panel and then removing them costs far more than one weak reviewer does, and the assignment step already has the levers to contain it.

**Never oversell a batch.**
WHEN briefing a judge on their assignments. THEN describe the submissions as they are, including that some will be thin, and set the expectation that a short review with a low score and a one-line note is a complete review. BECAUSE a judge who was told the batch was strong and found it was not withdrew over the workload, and that withdrawal was a fit signal about the briefing, not about the judge.

**Keynote-tier people get a speaking invite, not an asynchronous judging ask.**
WHEN a candidate is senior enough that their name would headline the event. THEN invite them to speak, with a slot fitted to their calendar; do not ask them to review eight submissions on a rubric. BECAUSE the judging ask reads as a mismatch to them and closes the door on the talk.

**Referral brokers recycle.**
WHEN a third party sends candidates. THEN vet every one as if cold; check whether they were declined for an earlier search. BECAUSE brokers resend the same people to every event, including ones a previous panel turned down.

**Read the thread before any reminder.**
WHEN about to nudge someone. THEN read the last message in their thread first. BECAUSE reminders have gone out to people who had already replied, and one apology went to someone who had never been dropped, because the check used a four-day window against a three-week-old acceptance. No date cap on the check; read the roles and the record.

**One message, the human length.**
WHEN writing any invite or nudge. THEN one history line (why them), the ask, two facts they need to decide, an easy out, and nothing that argues for the ask. BECAUSE the messages that get replies are the ones that read like a person wrote them in two minutes, and a reader who suspects a message was machine-written stops reading it as a message from you. `sourcing/pipeline/templates.py` lints for the phrases that give that away.

## Cadence that works

- Day 0: invite (`sent`).
- Day 5 to 7: one follow-up, two sentences, then `no_reply` if nothing comes back. A second follow-up rarely converts and costs goodwill.
- Out-of-office bounce (`sent-ooo`): note the return date, retry after it.
- Soft decline: do not re-ask for this search; they are the first names on the list for the next one.
- Interested or tentative: answer the same day with the two facts they asked for, then ask for the yes.

## What to log

Every message in or out goes in `outreach_log`, one line each, so "did we tell them" and "did they actually say yes" are queries rather than memories. The `reconcile` command in `tracker.py` reads that log.
