"""End-to-end demo on fictional data: two searches, 30 candidates, the whole funnel.

Everything here is invented. Names are generated, addresses are on example.org,
organisations are generic descriptions. The rows are tagged `demo` so `--reset`
can remove them and nothing else.

Usage:
    python pipeline/demo.py            seed (idempotent), walk the states, print status, chase, funnel
    python pipeline/demo.py --reset    remove the demo rows
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _db  # noqa: E402
import funnel  # noqa: E402
import init_db  # noqa: E402
import states  # noqa: E402
import templates  # noqa: E402
import tracker  # noqa: E402
import verify  # noqa: E402

FIRST = ["Ada", "Bao", "Carmen", "Dev", "Elif", "Farid", "Greta", "Hugo", "Ines", "Jonas", "Kofi", "Lena",
         "Mira", "Nadia", "Omar", "Priya", "Quinn", "Rafael", "Sana", "Tomas", "Uma", "Viktor", "Wen", "Ximena",
         "Yara", "Zed", "Aisha", "Bram", "Chiara", "Dario"]
LAST = ["Example", "Sample", "Placeholder", "Fictional", "Demo", "Specimen"]
ORGS = ["a 30-person biosecurity nonprofit", "a university AI lab", "an evals startup", "a policy think tank",
        "a frontier lab safety team", "an independent research group", "a governance fellowship programme"]
TRACKS = {"control-sprint-judges": ["control protocols", "evals", "interpretability"],
          "governance-speakers": ["compute governance", "standards", "incident reporting"]}
SOURCES = ["past-pool", "referral", "form", "search", "scrape"]

SEARCHES = [
    ("control-sprint-judges", "Judges for a two-day AI control research sprint", "judge",
     ",".join(TRACKS["control-sprint-judges"]), 12, "2026-03-23", "2026-03-29"),
    ("governance-speakers", "Speakers for the governance track of a hackathon", "speaker",
     ",".join(TRACKS["governance-speakers"]), 4, "2026-03-20", "2026-03-22"),
]


def seed(conn) -> tuple[int, int]:
    rng = random.Random(7)
    conn.executemany("INSERT OR IGNORE INTO searches (search, description, role_type, tracks, needed, opens_at, closes_at) "
                     "VALUES (?,?,?,?,?,?,?)", SEARCHES)
    new_people = 0
    for i in range(30):
        name = f"{FIRST[i]} {LAST[i % len(LAST)]}"
        email = f"{FIRST[i].lower()}.{LAST[i % len(LAST)].lower()}{i}@example.org"
        search = SEARCHES[0][0] if i < 22 else SEARCHES[1][0]
        role = "judge" if i < 22 else "speaker"
        track = rng.choice(TRACKS[search])
        cur = conn.execute(
            "INSERT OR IGNORE INTO candidates (name, email, org, headline, seniority, fit_tracks, source, source_ref, consent, tags) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, email, rng.choice(ORGS), f"researcher, {track}", rng.choice(["senior", "mid", "junior"]),
             track, rng.choice(SOURCES), "demo seed", "referred", "demo"))
        new_people += cur.rowcount
        cid = conn.execute("SELECT id FROM candidates WHERE email=?", (email,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO candidate_roles (candidate_id, search, role, track, status) VALUES (?,?,?,?,'prospect')",
                     (cid, search, role, track))
    conn.commit()
    return new_people, 30


def walk(conn) -> None:
    """Move the demo rows through realistic paths. Only rows still at prospect are touched."""
    rng = random.Random(11)
    rows = conn.execute("SELECT r.candidate_id, r.search, r.role, c.name, c.email FROM candidate_roles r "
                        "JOIN candidates c ON c.id=r.candidate_id WHERE c.tags='demo' AND r.status='prospect'").fetchall()
    for r in rows:
        cid, search, role = r["candidate_id"], r["search"], r["role"]
        # verify before the cold message, record it
        res = verify.check(conn, r["name"], None, r["email"], None, True)
        if res["verdict"] == "wrong-person-risk":
            conn.execute("UPDATE candidate_roles SET notes='verification: second close match, held' WHERE candidate_id=? AND search=?", (cid, search))
        states.transition(conn, cid, search, role, "contacted", by="demo")
        conn.execute("INSERT INTO outreach_log (candidate_id, search, role, direction, channel, template_id, subject, summary) "
                     "VALUES (?,?,?,?,?,?,?,?)", (cid, search, role, "out", "email", f"{role}-invite", "invite", "cold invite sent"))
        states.transition(conn, cid, search, role, "sent")
        path = rng.choices(["confirm", "decline", "no_reply", "interested", "ooo", "soft"], [45, 20, 15, 10, 5, 5])[0]
        if path == "confirm":
            conn.execute("INSERT INTO outreach_log (candidate_id, search, role, direction, channel, summary) VALUES (?,?,?,?,?,?)",
                         (cid, search, role, "in", "email", "accepted"))
            states.transition(conn, cid, search, role, "confirmed")
            conn.execute("INSERT INTO outreach_log (candidate_id, search, role, direction, channel, summary) VALUES (?,?,?,?,?,?)",
                         (cid, search, role, "out", "email", "welcome aboard, details to follow"))
            if rng.random() < 0.85:
                states.transition(conn, cid, search, role, "delivered")
            elif rng.random() < 0.5:
                states.transition(conn, cid, search, role, "withdrew", note="pulled out the week before")
        elif path == "decline":
            conn.execute("INSERT INTO outreach_log (candidate_id, search, role, direction, channel, summary) VALUES (?,?,?,?,?,?)",
                         (cid, search, role, "in", "email", "declined, busy"))
            states.transition(conn, cid, search, role, "declined")
        elif path == "no_reply":
            states.transition(conn, cid, search, role, "no_reply")
        elif path == "interested":
            conn.execute("INSERT INTO outreach_log (candidate_id, search, role, direction, channel, summary) VALUES (?,?,?,?,?,?)",
                         (cid, search, role, "in", "email", "interested, asked about timing"))
            states.transition(conn, cid, search, role, "interested")
        elif path == "ooo":
            states.transition(conn, cid, search, role, "sent-ooo")
        else:
            states.transition(conn, cid, search, role, "soft_declined", note="not this time, next one")
    # one deliberate data-quality problem for reconcile to find: a confirmed row with no inbound acceptance
    already = conn.execute("SELECT 1 FROM candidate_roles WHERE notes LIKE '%never logged%' LIMIT 1").fetchone()
    r = None if already else conn.execute(
        "SELECT r.candidate_id, r.search, r.role FROM candidate_roles r JOIN candidates c ON c.id=r.candidate_id "
        "WHERE c.tags='demo' AND r.status='no_reply' LIMIT 1").fetchone()
    if r:
        states.transition(conn, r["candidate_id"], r["search"], r["role"], "contacted")
        states.transition(conn, r["candidate_id"], r["search"], r["role"], "confirmed", note="confirmed on a call, never logged")
    # make the dates look like a real month rather than one second (one statement, so the
    # touch trigger, which fires only when updated_at is left unchanged, does not reset it)
    conn.execute("UPDATE candidate_roles SET "
                 "confirmed_at = CASE WHEN confirmed_at IS NULL THEN NULL "
                 "  ELSE datetime(contacted_at, '-14 days', '+' || (abs(random()) % 6 + 1) || ' days') END, "
                 "contacted_at = datetime(contacted_at, '-14 days'), "
                 "updated_at = datetime(updated_at, '-' || (abs(random()) % 12) || ' days') "
                 "WHERE candidate_id IN (SELECT id FROM candidates WHERE tags='demo') AND contacted_at IS NOT NULL "
                 "AND julianday('now') - julianday(contacted_at) < 0.01")
    conn.commit()


def reset(conn) -> int:
    n = conn.execute("DELETE FROM candidates WHERE tags='demo'").rowcount  # cascades to roles, outreach, verification
    conn.execute("DELETE FROM searches WHERE search IN (?, ?)", (SEARCHES[0][0], SEARCHES[1][0]))
    conn.commit()
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fictional end-to-end demo of the candidate pipeline")
    ap.add_argument("--reset", action="store_true"); ap.add_argument("--db")
    a = ap.parse_args(argv)
    init_db.init(a.db)
    conn = _db.connect(a.db)
    if a.reset:
        print(f"removed {reset(conn)} demo candidate(s) and their rows")
        return 0
    templates.load(conn)
    new, total = seed(conn)
    print(f"seeded: {new} new candidate(s), {total} demo rows present\n")
    walk(conn)
    print("== status =="); tracker.cmd_status(conn, None)
    print("\n== chase =="); tracker.cmd_chase(conn, None)
    print("\n== reconcile =="); tracker.cmd_reconcile(conn, None)
    print("\n== funnel =="); funnel.main(["--db", a.db] if a.db else [])
    print("\n== a rendered invite (lint runs on every render) ==")
    out = templates.render(conn, "judge-invite", {
        "first_name": "Ada", "history_line": "You reviewed for us in March and your notes were the most useful on the panel.",
        "search_name": "Control Research Sprint", "dates": "23 to 25 May", "what_they_build": "evaluation protocols for untrusted models",
        "website_url": "https://example.org/sprint", "projects_per_judge": "5 to 8", "hours": "2 to 3",
        "review_start": "Monday 26 May", "review_deadline": "Sunday 1 June", "sender_name": "K."})
    print(f"Subject: {out['subject']}\n{out['body']}lint: {out['lint'] or 'clean'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
