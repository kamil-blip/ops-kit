"""Reviewer-to-item assignment as a mixed-integer linear programme (scipy HiGHS).

The same solver assigns judges to submissions after a research sprint or, with the
words changed, candidates to searches. Inputs:

    reviewers  id, tier (senior|mid|junior), tracks (set), capacity (max items),
               conflicts (set of organisation tokens; any item sharing one is
               forbidden for this reviewer)
    items      id, track, coverage (reviewers required, usually 2), orgs (tokens
               of the submitting organisations), priority (0..1, higher first)

Hard constraints:
    no assignment across a conflict of interest (the variable does not exist)
    per-reviewer load <= capacity
Soft constraints, each with a slack variable that carries a large penalty so the
solver only violates it when the roster makes it impossible, and reports it:
    every item reaches its coverage
    every item has at least one track-capable reviewer
    every item has at least one non-junior reviewer
    per-reviewer load >= a floor when the reviewer is used at all (load band)
Objective: maximise fit (track match, seniority on high-priority items) minus
slack penalties.

Output: assignments plus a coverage report: uncovered items, items without a
senior or track-capable reviewer, load histogram, conflict count (always 0 by
construction). Exit code 1 if any slack was used, so a wrapper can stop and ask.

    python screening/assign.py --demo             # 40 fictional items, 12 fictional reviewers
    python screening/assign.py --reviewers r.json --items i.json [--out assignments.json]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass, field

LOAD_MIN, PENALTY_COVER, PENALTY_TRACK, PENALTY_SENIOR, PENALTY_LOADMIN = 3, 1000, 300, 300, 50


@dataclass
class Reviewer:
    id: str
    tier: str
    tracks: set = field(default_factory=set)
    capacity: int = 8
    conflicts: set = field(default_factory=set)


@dataclass
class Item:
    id: str
    track: str
    coverage: int = 2
    orgs: set = field(default_factory=set)
    priority: float = 0.5


def conflicted(r: Reviewer, it: Item) -> bool:
    return bool(r.conflicts & it.orgs)


def fit(r: Reviewer, it: Item) -> float:
    f = 1.0
    if it.track in r.tracks:
        f += 50
    if r.tier == "senior":
        f += 20 * it.priority
    elif r.tier == "mid":
        f += 8 * it.priority
    return f


def solve(reviewers: list[Reviewer], items: list[Item]) -> dict:
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp

    pairs = [(ri, ii) for ri, r in enumerate(reviewers) for ii, it in enumerate(items) if not conflicted(r, it)]
    idx = {p: k for k, p in enumerate(pairs)}
    n_x = len(pairs)
    # slack columns: cover[i], track[i], senior[i], loadmin[r]
    s_cover = {ii: n_x + k for k, ii in enumerate(range(len(items)))}
    s_track = {ii: n_x + len(items) + k for k, ii in enumerate(range(len(items)))}
    s_senior = {ii: n_x + 2 * len(items) + k for k, ii in enumerate(range(len(items)))}
    s_load = {ri: n_x + 3 * len(items) + k for k, ri in enumerate(range(len(reviewers)))}
    used = {ri: n_x + 3 * len(items) + len(reviewers) + k for k, ri in enumerate(range(len(reviewers)))}  # binary "reviewer used"
    n = n_x + 3 * len(items) + 2 * len(reviewers)

    c = np.zeros(n)
    for (ri, ii), k in idx.items():
        c[k] = -fit(reviewers[ri], items[ii])
    for ii in range(len(items)):
        c[s_cover[ii]] = PENALTY_COVER
        c[s_track[ii]] = PENALTY_TRACK
        c[s_senior[ii]] = PENALTY_SENIOR
    for ri in range(len(reviewers)):
        c[s_load[ri]] = PENALTY_LOADMIN

    rows, lb, ub = [], [], []

    def add(coeffs, lo, hi):
        row = np.zeros(n)
        for col, v in coeffs:
            row[col] += v
        rows.append(row); lb.append(lo); ub.append(hi)

    for ii, it in enumerate(items):
        cols = [(idx[(ri, ii)], 1) for ri in range(len(reviewers)) if (ri, ii) in idx]
        add(cols + [(s_cover[ii], 1)], it.coverage, np.inf)           # coverage with slack
        add(cols, -np.inf, it.coverage)                                # never more than needed
        tr = [(idx[(ri, ii)], 1) for ri in range(len(reviewers)) if (ri, ii) in idx and it.track in reviewers[ri].tracks]
        add(tr + [(s_track[ii], 1)], 1, np.inf)                        # one track-capable
        sr = [(idx[(ri, ii)], 1) for ri in range(len(reviewers)) if (ri, ii) in idx and reviewers[ri].tier != "junior"]
        add(sr + [(s_senior[ii], 1)], 1, np.inf)                       # one non-junior
    for ri, r in enumerate(reviewers):
        load = [(idx[(ri, ii)], 1) for ii in range(len(items)) if (ri, ii) in idx]
        add(load, -np.inf, r.capacity)                                 # capacity
        add(load + [(used[ri], -r.capacity)], -np.inf, 0)              # used=1 if any load
        add(load + [(used[ri], -LOAD_MIN), (s_load[ri], 1)], 0, np.inf)  # load >= LOAD_MIN*used, with slack

    integrality = np.ones(n)
    for cols in (s_cover, s_track, s_senior, s_load):
        for col in cols.values():
            integrality[col] = 0
    xl, xu = np.zeros(n), np.ones(n)
    for cols in (s_cover, s_track, s_senior, s_load):
        for col in cols.values():
            xu[col] = np.inf
    res = milp(c, constraints=[LinearConstraint(np.array(rows), np.array(lb), np.array(ub))],
               integrality=integrality, bounds=Bounds(xl, xu))
    if res.x is None:
        raise RuntimeError(f"solver failed: {res.message}")
    x = res.x
    assignments = [(reviewers[ri].id, items[ii].id) for (ri, ii), k in idx.items() if x[k] > 0.5]
    per_item = Counter(i for _, i in assignments)
    per_rev = Counter(r for r, _ in assignments)
    uncovered = [it.id for it in items if per_item[it.id] < it.coverage]
    no_track = [it.id for ii, it in enumerate(items) if x[s_track[ii]] > 0.5]
    no_senior = [it.id for ii, it in enumerate(items) if x[s_senior[ii]] > 0.5]
    coi = sum(1 for r, i in assignments if conflicted(next(rv for rv in reviewers if rv.id == r), next(it for it in items if it.id == i)))
    return {"assignments": assignments, "uncovered": uncovered, "no_track_capable": no_track, "no_senior": no_senior,
            "load": dict(per_rev), "coi": coi, "objective": float(res.fun), "status": res.message}


def report(reviewers, items, out) -> str:
    load_hist = Counter(out["load"].get(r.id, 0) for r in reviewers)
    lines = [f"reviewers {len(reviewers)}  items {len(items)}  assignments {len(out['assignments'])}  status: {out['status']}",
             f"coverage gaps: {len(out['uncovered'])}  items without a track-capable reviewer: {len(out['no_track_capable'])}  "
             f"items without a non-junior reviewer: {len(out['no_senior'])}  conflicts of interest: {out['coi']}",
             "load histogram (items per reviewer: count of reviewers): " + ", ".join(f"{k}: {v}" for k, v in sorted(load_hist.items()))]
    for r in reviewers:
        mine = [i for rv, i in out["assignments"] if rv == r.id]
        lines.append(f"  {r.id:12} {r.tier:6} tracks={','.join(sorted(r.tracks)):18} cap={r.capacity} -> {len(mine):2}  {' '.join(mine)}")
    if out["uncovered"]:
        lines.append("uncovered: " + ", ".join(out["uncovered"]))
    return "\n".join(lines)


def demo(seed=11):
    rng = random.Random(seed)
    tracks = ["settings", "protocols", "redteam"]
    orgs = [f"org-{k}" for k in range(8)]
    reviewers = []
    for k in range(12):
        tier = "senior" if k < 3 else "mid" if k < 8 else "junior"
        reviewers.append(Reviewer(id=f"rev-{k:02}", tier=tier, tracks=set(rng.sample(tracks, rng.choice([1, 1, 2]))),
                                  capacity=rng.choice([6, 7, 8]), conflicts={rng.choice(orgs)} if rng.random() < 0.5 else set()))
    items = [Item(id=f"item-{k:02}", track=rng.choice(tracks), coverage=2, orgs={rng.choice(orgs)}, priority=rng.random()) for k in range(40)]
    return reviewers, items


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Assign reviewers to items under coverage, load, seniority and conflict constraints.")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--reviewers"); ap.add_argument("--items"); ap.add_argument("--out")
    a = ap.parse_args(argv)
    if a.demo:
        reviewers, items = demo()
        print("demo (fictional): 12 reviewers, 40 items, coverage 2 each\n")
    else:
        if not (a.reviewers and a.items):
            ap.error("pass --demo or both --reviewers and --items")
        reviewers = [Reviewer(id=r["id"], tier=r["tier"], tracks=set(r.get("tracks", [])), capacity=r.get("capacity", 8), conflicts=set(r.get("conflicts", [])))
                     for r in json.load(open(a.reviewers, encoding="utf-8"))]
        items = [Item(id=i["id"], track=i["track"], coverage=i.get("coverage", 2), orgs=set(i.get("orgs", [])), priority=i.get("priority", 0.5))
                 for i in json.load(open(a.items, encoding="utf-8"))]
    out = solve(reviewers, items)
    print(report(reviewers, items, out))
    if a.out:
        json.dump(out, open(a.out, "w", encoding="utf-8"), indent=2)
        print(f"\nwrote {a.out}")
    return 1 if (out["uncovered"] or out["no_track_capable"] or out["no_senior"]) else 0


if __name__ == "__main__":
    sys.exit(main())
