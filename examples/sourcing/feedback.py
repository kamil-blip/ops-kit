"""Record the hiring manager's feedback on the shortlist the way the kit records
everything it learns: as a learning row (WHEN / THEN / BECAUSE) written through
the steward bus, then show how the learning surfaces on the next similar brief.

    python examples/sourcing/feedback.py
"""
from __future__ import annotations

import json
import sqlite3
import sys

from _common import DEMO_ACTOR, HERE, connect

import steward_bus as bus

FEEDBACK = (
    "Hiring manager, one week after the list (fictional): the second candidate was strong and is "
    "interviewing. The first withdrew after the screening call; their current organisation made a "
    "counter-offer the same week. The next-in-line profile we would not have interviewed: the "
    "automation on their form was a leave tracker in Airtable, not the kind of pipeline we need."
)


def main(argv) -> int:
    conn = connect()
    conn.row_factory = sqlite3.Row
    sl = HERE / "shortlist.json"
    if not sl.is_file():
        print("no shortlist.json; run search.py first")
        return 1
    shortlist = json.loads(sl.read_text(encoding="utf-8"))
    sent = [r for r in shortlist["results"] if r["id"] in shortlist["send"]]
    names = ", ".join(r["name"] for r in sent)
    print("Feedback received:\n  " + FEEDBACK + "\n")

    desc = (
        "WHEN: shortlisting for a role where automation is a named must-have or a heavily weighted "
        "nice-to-have, and a candidate currently holds a similar role at a similar-sized organisation. "
        "THEN: before sending, ask each candidate for the one system they built (what it does, who uses it, "
        "how many steps it removed) and put that answer in the note, since a tool name on a form does not "
        "distinguish a leave tracker from a pipeline; and ask at the screening call whether they are "
        "actively looking, because a counter-offer from a similar org is the most common way a strong "
        "first pick is lost. "
        f"BECAUSE: fictional feedback on the demo search (candidates sent: {names}): the first pick withdrew "
        "after a counter-offer, the runner-up was judged not relevant on the depth of the automation, the "
        "second pick went to interview."
    )
    res = bus.write(
        conn, target_table="learnings", submitted_by=DEMO_ACTOR,
        natural_key={"learning_id": "LRN-DEMO-SOURCING-001"},
        payload={"learning_id": "LRN-DEMO-SOURCING-001",
                 "title": "Sourcing shortlist: ask for the one system a candidate built, and ask whether they are actively looking, before sending",
                 "description": desc,
                 "apply_when": "sourcing shortlist operations lead automation evidence counter-offer screening call rubric weights first ops hire small nonprofit",
                 "priority": "medium", "status": "active", "source": DEMO_ACTOR, "memory_type": "learning"},
        source_table="demo_hiring_manager_feedback", source_id="search-001", source_quote=FEEDBACK[:200])
    conn.commit()
    row = conn.execute("SELECT id, learning_id, status FROM learnings WHERE learning_id='LRN-DEMO-SOURCING-001'").fetchone()
    print(f"Learning written through the steward bus: id={row['id']} {row['learning_id']} status={row['status']} "
          f"(bus result: {res.get('status')})\n")

    # How it surfaces next time: the same retrieval the UserPromptSubmit hook runs
    # (hooks/context_injector.py -> learning_loop.surface_learnings -> learnings_retrieval.retrieve).
    import learnings_retrieval as lr
    prompt = "new brief: operations lead for a 10-person AI policy nonprofit, build the shortlist"
    hits = lr.retrieve(prompt, conn=conn, embed=False)
    print(f"On the next prompt like: {prompt!r}")
    print("the context injector would surface:")
    for line in lr.format_for_injection(hits):
        print("  " + line)
    if not hits:
        print("  (nothing yet; FTS index may need the trigger to fire on the next write)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
