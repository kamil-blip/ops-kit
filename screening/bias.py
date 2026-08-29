"""Reviewer bias correction by paired comparison.

Why paired comparison: it only compares reviewers who scored the same item, so
a harsh-looking reviewer who happened to draw hard items is not penalised. A
reviewer's bias is the mean of (own score minus peer score) over every shared
item. Adjusted score = raw score minus bias, clamped to the scale. Reviewers with
fewer than `min_pairs` shared items get no adjustment and are flagged INSUFFICIENT.

Limit, stated once and worth repeating to anyone who uses it: this corrects
severity (a reviewer who scores everything one point low), not expertise (a
reviewer who cannot tell a strong submission from a weak one). A reviewer with
no signal gets a bias near zero and an adjusted score that is still noise. Use it
to make rankings fairer across reviewers, not to rescue a panel that lacked
domain fit; that has to be fixed at assignment time.

    python screening/bias.py --demo
    python screening/bias.py --csv reviews.csv     # item_id, reviewer, score
"""
from __future__ import annotations

import argparse
import csv
import random
import statistics
import sys
from collections import defaultdict


def paired_bias(reviews: list[dict], min_pairs: int = 2) -> dict[str, dict]:
    by_item = defaultdict(dict)
    for r in reviews:
        by_item[r["item_id"]][r["reviewer"]] = float(r["score"])
    diffs = defaultdict(list)
    for item, scores in by_item.items():
        if len(scores) < 2:
            continue
        for rev, s in scores.items():
            peers = [v for k, v in scores.items() if k != rev]
            diffs[rev].append(s - statistics.mean(peers))
    out = {}
    for rev in {r["reviewer"] for r in reviews}:
        d = diffs.get(rev, [])
        if len(d) < min_pairs:
            out[rev] = {"bias": None, "pairs": len(d), "confidence": 0.2, "class": "INSUFFICIENT"}
            continue
        b = statistics.mean(d)
        out[rev] = {"bias": round(b, 3), "pairs": len(d), "confidence": round(min(1.0, 0.3 + 0.07 * len(d)), 2),
                    "class": "HARSH" if b < -0.5 else "LENIENT" if b > 0.5 else "NEUTRAL"}
    return out


def adjust(reviews: list[dict], bias: dict, lo: float, hi: float) -> dict[str, float]:
    """Adjusted mean score per item."""
    by_item = defaultdict(list)
    for r in reviews:
        b = bias.get(r["reviewer"], {}).get("bias")
        s = float(r["score"]) - (b or 0.0)
        by_item[r["item_id"]].append(max(lo, min(hi, s)))
    return {k: round(statistics.mean(v), 2) for k, v in by_item.items()}


def demo(seed=3):
    """Fictional panel: known true biases, recovered from the scores."""
    rng = random.Random(seed)
    true_bias = {"rev-a": -1.2, "rev-b": 0.0, "rev-c": 0.9, "rev-d": 0.3, "rev-e": -0.4, "rev-f": 0.0}
    revs = list(true_bias)
    items = {f"item-{k:02}": rng.uniform(4, 14) for k in range(60)}
    reviews = []
    for item, q in items.items():
        for rev in rng.sample(revs, 3):
            reviews.append({"item_id": item, "reviewer": rev, "score": max(3, min(15, round(q + true_bias[rev] + rng.gauss(0, 0.6))))})
    return reviews, true_bias, items


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Paired-comparison reviewer bias and adjusted rankings.")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--csv", help="item_id, reviewer, score")
    ap.add_argument("--scale", default="3,15", help="min,max of the total score scale")
    ap.add_argument("--min-pairs", type=int, default=2)
    a = ap.parse_args(argv)
    lo, hi = (float(x) for x in a.scale.split(","))
    true_bias = None
    if a.demo:
        reviews, true_bias, items = demo()
        print("demo (fictional): 60 items, 6 reviewers, 3 reviews per item, scale 3 to 15; true biases shown relative to the panel mean, which is what paired comparison can recover\n")
    elif a.csv:
        reviews = list(csv.DictReader(open(a.csv, encoding="utf-8", newline="")))
    else:
        ap.error("pass --demo or --csv")
    bias = paired_bias(reviews, a.min_pairs)
    if true_bias:
        mean_tb = statistics.mean(true_bias.values())
        true_bias = {k: v - mean_tb for k, v in true_bias.items()}
    print(f"{'reviewer':10} {'pairs':>5} {'bias':>7} {'conf':>5}  class" + ("      true bias" if true_bias else ""))
    for rev, b in sorted(bias.items()):
        bs = "n/a" if b["bias"] is None else f"{b['bias']:+.2f}"
        extra = f"      {true_bias[rev]:+.2f}" if true_bias else ""
        print(f"{rev:10} {b['pairs']:5} {bs:>7} {b['confidence']:5.2f}  {b['class']}{extra}")
    adj = adjust(reviews, bias, lo, hi)
    raw = defaultdict(list)
    for r in reviews:
        raw[r["item_id"]].append(float(r["score"]))
    moved = sorted(adj, key=lambda k: abs(adj[k] - statistics.mean(raw[k])), reverse=True)[:5]
    print("\nlargest adjustments (item: raw mean -> adjusted mean)")
    for k in moved:
        print(f"  {k}: {statistics.mean(raw[k]):5.2f} -> {adj[k]:5.2f}")
    print("\nlimit: this corrects severity, not expertise. A reviewer with no signal gets a bias near zero and stays noise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
