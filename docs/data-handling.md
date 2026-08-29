# Data provenance, consent and acceptable use

The test for any source in a talent database: where did this come from, how are we allowed to use it, and would we be comfortable explaining that use to the person concerned? This document is how the system this repository was carved from answers those three questions, mechanism by mechanism, and where it still falls short.

## Where did this come from

Every canonical fact about a person carries three things: the actor that wrote it, a source reference, and, for anything extracted from a message, the verbatim quote it was extracted from with its position in the source. Writes that arrive without an actor are rejected at the write path, not flagged afterwards.

Facts are tagged by how they were established:

- stated by the person themselves (a form, a reply, a bio they sent)
- extracted from a message by a model and promoted automatically because the claim is a literal quote, or because two independent model families agreed
- promoted by a person after review
- re-verified against the source at a later date

The distinction matters for use. A self-stated seniority or interest is usable for matching. A model-inferred one is a lead to check, not a fact to send.

## How are we allowed to use it

The system does not answer this with a policy document; it answers it with what it refuses to do.

- **Draft first.** No code path sends email, chat or social posts. Every outbound message is a draft a person sends.
- **Public posts use only person-supplied facts.** Nothing about a judge, speaker or partner (name, title, affiliation, photo, talk detail) goes into a public post or image unless the person gave it to us directly. Web lookups and hand-typed affiliations are not sources for public use. This rule exists because a hand-typed affiliation once went into a published image and was wrong.
- **Protected stores are blocked by id.** The tables and external databases a person's record must not be written to from the assistant are listed in a guard that runs before every tool call.
- **Some data is excluded from ingestion entirely.** Personal messaging accounts are not synced. Messages from a personal chat network that had been ingested were purged rather than kept "just in case".
- **Contributed content is anonymised by default** when it is used outside the context it was given in.
- **An identifier denylist and an embedding quarantine** keep people who asked not to be findable, or whose identity could not be verified, out of search results.

## Would we be comfortable explaining it to the person

The live case. In spring 2026 a partner organisation's advisor raised the consent question on a talent pipeline built from hackathon participants: what had participants agreed to when they signed up, and could their data be shared onward. The honest answer at the time was that the only consent captured was a checkbox for a six-month follow-up. No participant data was shared. The change proposed and adopted: a data-use notice on the signup form and two separate consent checkboxes, one for future contact about opportunities, one for sharing with third parties, so that "how are we allowed to use it" is answered at the point of collection rather than reconstructed later.

The rule that came out of it: a candidate's record can hold what they told us and what we verified, and it can be used for the purpose they were told about. Sharing onward needs its own consent, captured, not inferred.

## What is still missing

Three things, stated so they are not mistaken for solved:

1. **A written acceptable-use document.** The rules above live in code (guards, hooks) and in the system's learning rows. A one-page document a partner could read does not exist yet.
2. **A value-of-new-data metric.** The system records where every lead came from, but it does not yet compute what a new source or a new lead was worth in outcomes. Closest analogues today: conversion and completion per search and per source, and the wrong-person rate on cold lists.
3. **Enrichment at scale.** A cold-contact enricher exists (search plus profile headline, with a confidence score and review before promotion) and has never been run at scale; per-person verification is still done by hand.

## Legal basis, in one paragraph

For a candidate database run in the UK, the ordinary lawful basis for holding and using professional contact data for recruitment is legitimate interests, not consent, with the operative test being the person's reasonable expectations given how the data was collected. Consent is the basis for anything beyond that expectation, sharing onward in particular. Any data-sharing arrangement with another organisation should state purpose, what is shared, security, and what each party tells the person. That is the vocabulary this document is written to satisfy.
