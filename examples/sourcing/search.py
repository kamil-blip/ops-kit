"""Apply the rubric to every demo candidate and print a ranked shortlist with evidence.

    python examples/sourcing/search.py [--rubric path] [--top N]

Deterministic: gates first (a failed gate ends the evaluation with the reason),
then weighted points. Every score line carries the evidence quote and its source.
Writes shortlist.json next to this file.
"""
from __future__ import annotations

import json
import sqlite3
import sys

from _common import HERE, connect, demo_people

RUBRIC = HERE / "rubric-skill" / "rubric.json"


def load_candidate(conn, person) -> dict:
    pid = person["id"]
    ent = f"person-{pid}"
    attrs = {}
    for r in conn.execute(
        "SELECT attr, value, source_table, source_id, valid_from FROM attributes "
        "WHERE entity_id=? AND status='current'", (ent,)):
        attrs[r["attr"]] = {"value": r["value"], "source": f"{r['source_table']}:{r['source_id']}", "since": r["valid_from"]}
    obs = [dict(r) for r in conn.execute(
        "SELECT content, source_table, source_id FROM observations WHERE person_id=? ORDER BY id", (pid,))]
    org = conn.execute(
        "SELECT e.name, ed.valid_from FROM edges ed JOIN entities e ON e.id=ed.target_id "
        "WHERE ed.source_id=? AND ed.relation='works_at' ORDER BY ed.valid_from DESC LIMIT 1", (ent,)).fetchone()
    return {"id": pid, "name": person["name"], "headline": person["headline"], "location": person["location"],
            "org": org["name"] if org else None, "org_since": org["valid_from"] if org else None,
            "summary": person["summary"], "attrs": attrs, "observations": obs}


def evaluate(cand: dict, rubric: dict) -> dict:
    out = {"id": cand["id"], "name": cand["name"], "headline": cand["headline"], "org": cand["org"],
           "gates": [], "points": [], "passed_gates": True, "composite": 0, "max": rubric["max_points"]}
    for g in rubric["gates"]:
        a = cand["attrs"].get(g["attr"])
        if not g.get("evaluable", True) or a is None:
            out["gates"].append({"id": g["id"], "pass": False, "evidence": "no attribute on record", "source": None})
            out["passed_gates"] = False
            continue
        val = a["value"]
        if "numeric_range" in g:
            lo, hi = g["numeric_range"]
            try:
                ok = lo <= float(val) <= hi
            except ValueError:
                ok = False
        else:
            ok = val in g["pass_values"]
        out["gates"].append({"id": g["id"], "pass": ok, "evidence": f"{g['attr']}={val}", "source": a["source"]})
        if not ok:
            out["passed_gates"] = False
    if not out["passed_gates"]:
        return out
    total = 0
    for p in rubric["points"]:
        if p["attr"] == "observations":
            text = " ".join(o["content"] for o in cand["observations"]).lower()
            hits = [k for k in p["keywords"] if k in text]
            score = min(p["weight"], len(hits) * p["per_hit"])
            ev = "; ".join(o["content"] for o in cand["observations"])[:160] or "no observations"
            src = ", ".join(f"{o['source_table']}:{o['source_id']}" for o in cand["observations"]) or None
        else:
            a = cand["attrs"].get(p["attr"])
            val = (a or {}).get("value", "")
            src = (a or {}).get("source")
            ev = f"{p['attr']}={val}" if a else "no attribute on record"
            if p["scoring"] == "keyword":
                hits = [k for k in p["keywords"] if k in str(val).lower()]
                score = min(p["weight"], len(hits) * p["per_hit"])
            else:
                score = min(p["weight"], p["value_points"].get(val, 0))
        out["points"].append({"id": p["id"], "score": score, "weight": p["weight"], "evidence": ev, "source": src})
        total += score
    out["composite"] = total
    return out


def note_for(r: dict) -> str:
    if not r["passed_gates"]:
        failed = [g["id"] for g in r["gates"] if not g["pass"]]
        return "do not send: fails " + ", ".join(failed)
    weak = [p["id"] for p in r["points"] if p["score"] == 0]
    if r["composite"] >= 12:
        return "send: every must-have on record, strong on the nice-to-haves"
    if weak:
        return "send with a note: must-haves on record, nothing on " + ", ".join(weak)
    return "send with a note: must-haves on record, nice-to-haves partial"


def main(argv) -> int:
    rubric_path = RUBRIC
    top = 10
    if "--rubric" in argv:
        rubric_path = argv[argv.index("--rubric") + 1]
    if "--top" in argv:
        top = int(argv[argv.index("--top") + 1])
    rubric = json.load(open(rubric_path, encoding="utf-8"))
    conn = connect()
    conn.row_factory = sqlite3.Row
    people = demo_people(conn)
    if not people:
        print("no demo candidates in the database; run seed_candidates.py first")
        return 1
    results = [evaluate(load_candidate(conn, p), rubric) for p in people]
    results.sort(key=lambda r: (r["passed_gates"], r["composite"]), reverse=True)
    passing = [r for r in results if r["passed_gates"]]

    print(f"Brief: {rubric['role']}")
    print(f"Candidates evaluated: {len(results)}. Passed all {len(rubric['gates'])} gates: {len(passing)}. "
          f"Max points: {rubric['max_points']}.\n")
    for rank, r in enumerate(results[:top], start=1):
        head = f"{rank:2}. {r['name']}  [{r['composite']}/{r['max']}]  {r['headline']}"
        print(head)
        for g in r["gates"]:
            print(f"      gate {g['id']:28} {'pass' if g['pass'] else 'FAIL'}   {g['evidence']}  <{g['source']}>")
            if not g["pass"]:
                break
        for p in r["points"]:
            print(f"      +{p['score']}/{p['weight']} {p['id']:24} {p['evidence'][:90]}  <{p['source']}>")
        print(f"      note: {note_for(r)}\n")

    send = passing[: rubric["send_count"]]
    print("The human filter:")
    for r in send:
        print(f"  send: {r['name']} ({r['composite']}/{r['max']}), {r['org']}")
    if len(passing) > rubric["send_count"]:
        nxt = passing[rubric["send_count"]]
        print(f"  next in line, not sent: {nxt['name']} ({nxt['composite']}/{nxt['max']})")
    if send:
        least = min(send, key=lambda r: r["composite"])
        zero = [p["id"] for p in least["points"] if p["score"] == 0]
        print(f"  least confident: {least['name']}. Most likely way this is wrong: the gate evidence is "
              f"self-reported on an intake form; if the reference call contradicts it, the pick fails. "
              f"Also nothing on record for: {', '.join(zero) or 'none'}.")

    out = HERE / "shortlist.json"
    out.write_text(json.dumps({"rubric": str(rubric_path), "results": results,
                               "send": [r["id"] for r in send]}, indent=1), encoding="utf-8")
    print(f"\nshortlist written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
