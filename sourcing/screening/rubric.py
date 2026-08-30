"""Rubric schema and loader.

A rubric turns a hiring-manager brief (or a research-sprint track) into something
a scorer can apply the same way to every record. Two kinds of criteria:

  gate    A must-have. Fails the record outright if no evidence is found. Gates
          are never traded off against points; a record that fails any gate is
          reported below every record that passes.
  points  A nice-to-have or a quality dimension, scored 1 to 5, weighted.

Every criterion carries `signals`: phrases the deterministic scorer looks for and
that a model scorer is told to quote. Every score must come with a verbatim
evidence quote from the record; a score without evidence is invalid and is
replaced by 1 with confidence 1. The composite is computed here, in code, from
the per-criterion scores. The model never adds up its own numbers.

Usage as a library:

    from screening.rubric import load_rubric
    rubric = load_rubric("sourcing/screening/rubrics/example-ops-generalist-role.json")
    rubric.composite({"automation": 4, "mission": 3})
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SCALE_MIN, SCALE_MAX = 1, 5


@dataclass(frozen=True)
class Criterion:
    id: str
    name: str
    kind: str                      # "gate" or "points"
    description: str
    weight: float = 1.0            # points criteria only
    signals: tuple[str, ...] = ()  # phrases that count as evidence
    anchors: dict = field(default_factory=dict)  # {"5": "...", "3": "...", "1": "..."}

    def __post_init__(self):
        if self.kind not in ("gate", "points"):
            raise ValueError(f"criterion {self.id}: kind must be gate or points, got {self.kind!r}")
        if self.weight <= 0:
            raise ValueError(f"criterion {self.id}: weight must be positive")


@dataclass(frozen=True)
class Rubric:
    name: str
    brief: str
    criteria: tuple[Criterion, ...]
    notes: str = ""

    @property
    def gates(self) -> list[Criterion]:
        return [c for c in self.criteria if c.kind == "gate"]

    @property
    def points(self) -> list[Criterion]:
        return [c for c in self.criteria if c.kind == "points"]

    def composite(self, scores: dict[str, int]) -> float:
        """Weighted mean of points-criteria scores, mapped to 0..100.

        Missing criteria count as the scale minimum. Gates are not part of the
        composite; a failed gate is reported separately by the scorer.
        """
        total_w = sum(c.weight for c in self.points)
        if total_w == 0:
            return 0.0
        acc = 0.0
        for c in self.points:
            s = scores.get(c.id, SCALE_MIN)
            s = max(SCALE_MIN, min(SCALE_MAX, int(s)))
            acc += c.weight * (s - SCALE_MIN) / (SCALE_MAX - SCALE_MIN)
        return round(100.0 * acc / total_w, 1)

    def to_prompt_block(self) -> str:
        """Render the rubric as the block the model prompt expects."""
        lines = [f"RUBRIC: {self.name}", "", "BRIEF:", self.brief.strip(), ""]
        if self.gates:
            lines.append("GATES (must-haves; fail any of these and stop scoring):")
            for c in self.gates:
                lines.append(f"- [{c.id}] {c.name}: {c.description}")
            lines.append("")
        lines.append("POINTS (score 1 to 5 each, quote the evidence):")
        for c in self.points:
            lines.append(f"- [{c.id}] {c.name} (weight {c.weight:g}): {c.description}")
            for level in ("5", "3", "1"):
                if level in c.anchors:
                    lines.append(f"    {level}: {c.anchors[level]}")
        if self.notes:
            lines += ["", "NOTES:", self.notes.strip()]
        return "\n".join(lines)


def load_rubric(path: str | Path) -> Rubric:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    crits = []
    seen = set()
    for raw in data["criteria"]:
        c = Criterion(
            id=raw["id"], name=raw["name"], kind=raw["kind"], description=raw["description"],
            weight=float(raw.get("weight", 1.0)), signals=tuple(raw.get("signals", [])),
            anchors={str(k): v for k, v in raw.get("anchors", {}).items()},
        )
        if c.id in seen:
            raise ValueError(f"duplicate criterion id {c.id}")
        seen.add(c.id)
        crits.append(c)
    return Rubric(name=data["name"], brief=data["brief"], criteria=tuple(crits), notes=data.get("notes", ""))


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Validate a rubric JSON file and print it as the prompt block.")
    ap.add_argument("rubric", nargs="*", help="rubric JSON path(s); default: the ops example")
    a = ap.parse_args()
    for p in a.rubric or [Path(__file__).parent / "rubrics" / "example-ops-generalist-role.json"]:
        r = load_rubric(p)
        print(r.to_prompt_block())
        all_fives = {c.id: 5 for c in r.points}
        print(f"\n{len(r.gates)} gates, {len(r.points)} points criteria; composite of all-5s = {r.composite(all_fives)}")
    sys.exit(0)
