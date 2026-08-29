# Candidate states

One row in `candidate_roles` is one person in one search in one role, and each row is its own state machine. The allowed moves are defined once, in `pipeline/states.py`, copied into the `role_transitions` table by `init_db.py`, and enforced by a trigger: an update that is not in the table is refused by the database, not just by the code.

## The diagram

```
prospect ──> contacted ──> sent ──┬──> interested ──┬──> confirmed ──┬──> delivered   (terminal)
    │                        │    │                 │                 ├──> no_show     (terminal)
    │                        │    ├──> tentative ───┤                 ├──> withdrew ──> contacted
    │                        │    │                 │                 └──> declined ──> contacted
    │                        │    ├──> sent-ooo ────┤
    │                        │    ├──> bounced ─────┼──> contacted
    │                        │    ├──> no_reply ────┤
    │                        │    ├──> soft_declined┤
    │                        │    └──> declined ────┘
    └──> removed ──> contacted
```

Any reply state can also go straight to `confirmed` or `declined`; `sent` can return to `contacted` when a second channel is used. The full edge list is `python pipeline/states.py`.

## What each state means

| State | Meaning | Counts as |
|---|---|---|
| `prospect` | identified, nothing sent yet | worked |
| `contacted` | written to once, any channel | contacted |
| `sent` | the formal invite went out | contacted |
| `sent-ooo` | invite met an out-of-office reply; retry after the return date | contacted |
| `bounced` | the address is dead; find another or remove | contacted |
| `interested` | positive reply, not yet committed; answer today | contacted |
| `tentative` | ambiguous reply; ask the one question that resolves it | contacted |
| `no_reply` | follow-up sent, nothing back; the search moves on | contacted |
| `soft_declined` | "not now, maybe later"; first on the list next time | contacted, declined |
| `confirmed` | committed, the row exists, the person has been told | confirmed |
| `delivered` | did the work; terminal | confirmed, delivered |
| `no_show` | confirmed but did not turn up; terminal | confirmed |
| `withdrew` | confirmed, then pulled out | confirmed, declined |
| `declined` | said no | declined |
| `removed` | taken out by us: fit, conflict, duplicate | neither |

Never written: `invited`. It is a dead state kept out on purpose (see docs/outreach.md).

## Timestamps set by the transition helper

`states.transition()` sets `contacted_at` on the first move past `prospect`, `confirmed_at` on the first move into `confirmed` (or later), and `delivered_at` on `delivered`, each only once. Days-to-confirm in the funnel is `confirmed_at - contacted_at`.

## How the funnel reads the states

| Funnel line | States |
|---|---|
| worked | all rows |
| contacted | everything except `prospect` and `removed` |
| confirmed | `confirmed`, `delivered`, `no_show`, `withdrew` |
| delivered | `delivered` |
| declined | `declined`, `soft_declined`, `withdrew` |
| conversion | confirmed / contacted |
| completion | delivered / confirmed, once the search has closed |
