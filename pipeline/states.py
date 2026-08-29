"""The candidate state machine.

One row in `candidate_roles` is one instance: a person, a search, a role. The
transition table below is the only place the allowed moves are defined; `init_db.py`
copies it into the `role_transitions` table and a trigger refuses anything else.

Two rules that came from mistakes:

1. `invited` is not a state. Outreach that went out is `sent`; outreach that was
   answered is one of the reply states. A vague "invited" bucket hid people who
   had never been written to and people who had accepted, in the same list.

2. An acceptance is not a confirmed candidate until the row says so. Replying
   "welcome aboard" by email and updating the database are one action, done in the
   same sitting. A roster built from the database while acceptances sat in an inbox
   left people confirmed on paper and absent from the work.

States:
    prospect        identified, not yet written to
    contacted       written to once (any channel)
    sent            the formal invite went out
    sent-ooo        the invite bounced off an out-of-office reply
    bounced         the address is dead
    interested      replied positively, not yet committed
    tentative       replied ambiguously
    no_reply        time elapsed, nothing back
    soft_declined   "not now, maybe later"; distinct from declined
    confirmed       committed; the row exists and the person has been told
    delivered       did the work (terminal)
    no_show         confirmed but did not turn up (terminal)
    declined        said no
    withdrew        confirmed, then pulled out
    removed         taken out by us (fit, conflict, duplicate)
"""
from __future__ import annotations

import sqlite3

TRANSITIONS: dict[str, frozenset[str]] = {
    "prospect":      frozenset({"contacted", "removed"}),
    "contacted":     frozenset({"sent", "sent-ooo", "bounced", "interested", "tentative",
                                "soft_declined", "no_reply", "confirmed", "declined", "removed"}),
    "sent":          frozenset({"contacted", "sent-ooo", "bounced", "interested", "tentative",
                                "soft_declined", "no_reply", "confirmed", "declined"}),
    "sent-ooo":      frozenset({"contacted", "interested", "tentative", "soft_declined",
                                "no_reply", "confirmed", "declined", "removed"}),
    "bounced":       frozenset({"contacted", "removed"}),
    "interested":    frozenset({"confirmed", "declined", "withdrew", "removed"}),
    "tentative":     frozenset({"interested", "confirmed", "declined", "withdrew", "removed"}),
    "no_reply":      frozenset({"contacted", "declined", "removed"}),
    "soft_declined": frozenset({"contacted", "declined", "removed"}),
    "confirmed":     frozenset({"delivered", "no_show", "withdrew", "declined", "removed"}),
    "declined":      frozenset({"contacted"}),
    "removed":       frozenset({"contacted"}),
    "delivered":     frozenset(),
    "no_show":       frozenset(),
    "withdrew":      frozenset({"contacted"}),
}

ALL_STATES: frozenset[str] = frozenset(TRANSITIONS)
TERMINAL_STATES: frozenset[str] = frozenset({"delivered", "no_show"})
ACTIVE_STATES: frozenset[str] = frozenset({
    "prospect", "contacted", "sent", "sent-ooo", "interested", "tentative", "confirmed",
})
DECLINE_STATES: frozenset[str] = frozenset({"declined", "soft_declined", "withdrew"})
CONFIRMED_OR_LATER: frozenset[str] = frozenset({"confirmed", "delivered", "no_show", "withdrew"})
CONTACTED_STATES: frozenset[str] = ALL_STATES - {"prospect", "removed"}
DEAD_STATES: frozenset[str] = frozenset({"invited"})  # never write these


def is_valid_transition(from_status: str | None, to_status: str) -> bool:
    """True when the move is allowed. An empty old status accepts any state."""
    if to_status in DEAD_STATES or to_status not in ALL_STATES:
        return False
    if not from_status or from_status == to_status:
        return True
    return to_status in TRANSITIONS.get(from_status, frozenset())


def transition_rows() -> list[tuple[str, str]]:
    return sorted((f, t) for f, tos in TRANSITIONS.items() for t in tos)


def transition(conn: sqlite3.Connection, candidate_id: int, search: str, role: str,
               to_status: str, note: str | None = None, by: str | None = None) -> None:
    """Move one row to `to_status`, validating in Python before the trigger does.

    Sets contacted_at, confirmed_at and delivered_at when the corresponding state
    is reached for the first time. Raises ValueError on an illegal move.
    """
    row = conn.execute(
        "SELECT status FROM candidate_roles WHERE candidate_id=? AND search=? AND role=?",
        (candidate_id, search, role)).fetchone()
    if row is None:
        raise ValueError(f"no candidate_roles row for candidate {candidate_id} in {search}/{role}")
    if not is_valid_transition(row["status"], to_status):
        raise ValueError(f"illegal transition {row['status']} -> {to_status}")
    stamps = []
    if to_status in CONTACTED_STATES:
        stamps.append("contacted_at = COALESCE(contacted_at, datetime('now'))")
    if to_status in CONFIRMED_OR_LATER:
        stamps.append("confirmed_at = COALESCE(confirmed_at, datetime('now'))")
    if to_status == "delivered":
        stamps.append("delivered_at = COALESCE(delivered_at, datetime('now'))")
    if by:
        stamps.append("contacted_by = COALESCE(contacted_by, :by)")
    if note:
        stamps.append("notes = COALESCE(notes || char(10), '') || :note")
    sql = "UPDATE candidate_roles SET status = :to" + ("".join(", " + s for s in stamps)) + \
          " WHERE candidate_id = :cid AND search = :search AND role = :role"
    conn.execute(sql, {"to": to_status, "by": by, "note": note, "cid": candidate_id,
                       "search": search, "role": role})


if __name__ == "__main__":
    import json
    print(json.dumps({
        "states": sorted(ALL_STATES),
        "terminal": sorted(TERMINAL_STATES),
        "active": sorted(ACTIVE_STATES),
        "edges": len(transition_rows()),
        "dead_states_never_written": sorted(DEAD_STATES),
    }, indent=2))
