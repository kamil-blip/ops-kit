"""Validate a scoring layer against human reviews before trusting it.

Input: a CSV with one row per (item, scorer) with columns
    item_id, scorer, total[, <criterion columns...>]
where `scorer` is "human" for the human review and a model name otherwise.
Items with fewer than one human row are ignored.

Reports, per model:
    Spearman rank correlation of model total vs human total (the number that
    matters for ranking use), mean absolute error on the total, per-criterion
    exact-match and within-one rates, share of items where the model is more than
    2 points off, and cross-model agreement (Spearman + MAE + disagreements) for
    every pair of models.

    python sourcing/screening/validate.py --csv scores.csv
    python sourcing/screening/validate.py --synthetic          # fictional dataset, runs offline
"""
from __future__ import annotations

import argparse
import csv
import random
import statistics
import sys
from collections import defaultdict
from itertools import combinations


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys) or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None

    def rank(seq):
        order = sorted(enumerate(seq), key=lambda t: t[1])
        ranks = [0.0] * len(seq)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and order[j + 1][1] == order[i][1]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[order[k][0]] = avg
            i = j + 1
        return ranks

    rx, ry = rank(xs), rank(ys)
    n = len(rx)
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return 1 - (6 * d2) / (n * (n * n - 1))


def mae(xs, ys) -> float:
    return sum(abs(a - b) for a, b in zip(xs, ys)) / len(xs)


def load_csv(path: str) -> tuple[dict, list[str]]:
    rows = list(csv.DictReader(open(path, encoding="utf-8", newline="")))
    crits = [c for c in rows[0].keys() if c not in ("item_id", "scorer", "total")]
    data: dict[str, dict[str, dict]] = defaultdict(dict)  # scorer -> item -> row
    for r in rows:
        data[r["scorer"]][r["item_id"]] = {k: float(v) for k, v in r.items() if k not in ("item_id", "scorer") and v != ""}
    return data, crits


def synthetic(n_items=120, seed=7) -> tuple[dict, list[str]]:
    """Fictional dataset: a latent quality per item, a human review with noise, a
    better model tracking quality with moderate noise, a weaker model with more
    noise and a systematic lean toward the middle."""
    rng = random.Random(seed)
    crits = ["impact", "execution", "presentation"]
    data = defaultdict(dict)
    for i in range(n_items):
        q = {c: rng.uniform(1, 5) for c in crits}

        def obs(noise, shrink=0.0):
            out = {}
            for c in crits:
                v = q[c] * (1 - shrink) + 3 * shrink + rng.gauss(0, noise)
                out[c] = float(max(1, min(5, round(v))))
            out["total"] = sum(out[c] for c in crits)
            return out

        data["human"][f"item-{i:03}"] = obs(0.6)
        data["model-a"][f"item-{i:03}"] = obs(0.9)
        data["model-b"][f"item-{i:03}"] = obs(1.3, shrink=0.35)
    return data, crits


def report(data: dict, crits: list[str]) -> str:
    human = data.get("human", {})
    models = [m for m in data if m != "human"]
    lines = [f"items with human review: {len(human)}   models: {', '.join(models)}", ""]
    lines.append(f"{'model':10} {'n':>4} {'spearman':>9} {'MAE':>6} {'>2 off':>7}  " + "  ".join(f"{c[:10]:>10}" for c in crits))
    lines.append(" " * 42 + "  ".join(f"{'exact/±1':>10}" for _ in crits))
    for m in models:
        shared = [i for i in human if i in data[m]]
        if not shared:
            continue
        ht = [human[i]["total"] for i in shared]
        mt = [data[m][i]["total"] for i in shared]
        rho = spearman(mt, ht)
        far = sum(1 for a, b in zip(mt, ht) if abs(a - b) > 2)
        per = []
        for c in crits:
            pairs = [(data[m][i].get(c), human[i].get(c)) for i in shared if c in data[m][i] and c in human[i]]
            if not pairs:
                per.append(f"{'n/a':>10}")
                continue
            exact = sum(1 for a, b in pairs if a == b) / len(pairs)
            within = sum(1 for a, b in pairs if abs(a - b) <= 1) / len(pairs)
            per.append(f"{exact:4.0%}/{within:4.0%}".rjust(10))
        lines.append(f"{m:10} {len(shared):4} {rho if rho is None else f'{rho:.3f}':>9} {mae(mt, ht):6.2f} {far:7}  " + "  ".join(per))
    if len(models) >= 2:
        lines += ["", "cross-model agreement"]
        for a, b in combinations(models, 2):
            shared = [i for i in data[a] if i in data[b]]
            at = [data[a][i]["total"] for i in shared]
            bt = [data[b][i]["total"] for i in shared]
            far = sum(1 for x, y in zip(at, bt) if abs(x - y) > 2)
            rho = spearman(at, bt)
            lines.append(f"  {a} vs {b}: n={len(shared)} spearman={rho if rho is None else f'{rho:.3f}'} MAE={mae(at, bt):.2f} disagreements>2={far}")
    lines += ["", "reading the table: use a model for ranking only if its Spearman vs human is well above what",
              "two humans get against each other on the same items; use the per-criterion within-one rate to see",
              "which criterion the model cannot judge; cross-model disagreement marks the items to send to a person."]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare model scores with human reviews.")
    ap.add_argument("--csv", help="item_id, scorer, total, <criteria...>")
    ap.add_argument("--synthetic", action="store_true", help="run on a fictional dataset")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    if a.synthetic or not a.csv:
        if not a.synthetic:
            ap.error("pass --csv or --synthetic")
        data, crits = synthetic(seed=a.seed)
        print("synthetic dataset (fictional): 120 items, human + model-a + model-b\n")
    else:
        data, crits = load_csv(a.csv)
    print(report(data, crits))
    return 0


if __name__ == "__main__":
    sys.exit(main())
