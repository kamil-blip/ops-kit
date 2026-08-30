"""Apply a rubric to records and print a ranked shortlist with evidence.

Two scorers, one output schema:

  deterministic (default)  Looks for each criterion's signal phrases in the record,
                           quotes the sentence that contains the match as evidence,
                           and maps the number of distinct signals found to a 1-5
                           score. Gates pass when at least one gate signal is
                           present. Offline, reproducible, and obviously limited:
                           it measures whether the record talks about the thing,
                           not whether the person did it well. It exists so the
                           pipeline runs end to end without a key and so a model
                           scorer has a baseline to beat.
  --model anthropic|openai  Sends prompt.md with the rubric and the record, parses
                           the JSON reply, and validates every evidence quote
                           against the record (a quote that is not in the record
                           empties the score to 1 with confidence 1). Only runs if
                           ANTHROPIC_API_KEY or OPENAI_API_KEY is set; otherwise
                           falls back to deterministic and says so.

Either way the composite is computed by rubric.py, records failing any gate are
listed after every record that passes, and the output JSON has the same shape.

Usage:
    python sourcing/screening/score.py --rubric sourcing/screening/rubrics/example-ops-generalist-role.json \\
        --records sourcing/screening/examples/candidates.json [--out shortlist.json] [--model anthropic]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from screening.rubric import Rubric, load_rubric  # noqa: E402

PROMPT_PATH = Path(__file__).parent / "prompt.md"
INJECTION_RE = re.compile(r"(ignore (the|this|all) (rubric|instructions?)|rate (this|me) (candidate )?5|score (this|me) (a )?5|system prompt|disregard)", re.I)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def find_evidence(text: str, signals: tuple[str, ...]) -> tuple[list[str], str]:
    """Return (distinct signals found, the first sentence that contains one)."""
    low = text.lower()
    found = [s for s in signals if s.lower() in low]
    quote = ""
    if found:
        for sent in sentences(text):
            if any(s.lower() in sent.lower() for s in found):
                quote = sent[:240]
                break
    return found, quote


def deterministic(rubric: Rubric, record: dict) -> dict:
    text = record.get("text", "")
    words = len(re.findall(r"[A-Za-z][A-Za-z'-]*", text))
    anomalies = []
    if INJECTION_RE.search(text):
        anomalies.append("instruction-like text inside the record; ignored for scoring")
    if words < 25:
        anomalies.append(f"record is {words} words; too short to assess")
    gates, gates_passed = {}, True
    for c in rubric.gates:
        found, quote = find_evidence(text, c.signals)
        ok = bool(found) and words >= 25
        gates[c.id] = {"pass": ok, "evidence": quote, "signals": found}
        gates_passed &= ok
    scores = {}
    for c in rubric.points:
        found, quote = find_evidence(text, c.signals)
        n = len(found)
        score = 1 if n == 0 else 3 if n == 1 else 4 if n == 2 else 5
        conf = 1 if n == 0 else 3 if n == 1 else 4
        if words < 25:
            score, conf = 1, 1
        scores[c.id] = {"score": score, "confidence": conf, "evidence": quote,
                        "rationale": f"{n} signal(s) present: {', '.join(found)}" if found else "no signal present"}
    return {"gates": gates, "gates_passed": gates_passed, "scores": scores, "anomalies": anomalies,
            "assumptions": [], "red_flags": [], "green_flags": [], "summary": "", "scorer": "deterministic"}


def model_score(rubric: Rubric, record: dict, provider: str) -> dict | None:
    """Call a provider if a key exists; return parsed JSON or None to fall back."""
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    body = prompt.split("```", 2)[1] if "```" in prompt else prompt
    filled = body.replace("{rubric_block}", rubric.to_prompt_block()).replace("{record}", record.get("text", ""))
    try:
        if provider == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic  # noqa: PLC0415
            client = anthropic.Anthropic()
            msg = client.messages.create(model=os.environ.get("SCORE_MODEL", "claude-sonnet-5"), max_tokens=1500,
                                         messages=[{"role": "user", "content": filled}])
            raw = msg.content[0].text
        elif provider == "openai" and os.environ.get("OPENAI_API_KEY"):
            from openai import OpenAI  # noqa: PLC0415
            client = OpenAI()
            r = client.chat.completions.create(model=os.environ.get("SCORE_MODEL", "gpt-5-mini"),
                                               messages=[{"role": "user", "content": filled}])
            raw = r.choices[0].message.content
        else:
            return None
    except Exception as e:  # provider or network failure: fall back, never crash the run
        print(f"  model scorer unavailable ({type(e).__name__}); using deterministic", file=sys.stderr)
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    # evidence must be a verbatim substring of the record; otherwise the score is void
    text = record.get("text", "")
    for cid, s in out.get("scores", {}).items():
        ev = (s.get("evidence") or "").strip()
        if not ev or ev not in text:
            s["score"], s["confidence"] = 1, 1
            s["rationale"] = "evidence quote missing or not found in record; score voided"
    out["scorer"] = f"model:{provider}"
    return out


def score_records(rubric: Rubric, records: list[dict], provider: str | None) -> list[dict]:
    results = []
    for rec in records:
        res = model_score(rubric, rec, provider) if provider else None
        if res is None:
            res = deterministic(rubric, rec)
        composite = rubric.composite({k: v["score"] for k, v in res["scores"].items()})
        low_conf = [k for k, v in res["scores"].items() if v.get("confidence", 1) <= 2]
        results.append({"id": rec.get("id"), "label": rec.get("label", rec.get("id")), "composite": composite,
                        "low_confidence": low_conf, **res})
    results.sort(key=lambda r: (not r["gates_passed"], -r["composite"]))
    return results


def render(rubric: Rubric, results: list[dict]) -> str:
    lines = [f"Rubric: {rubric.name}", f"Records: {len(results)}  scorer: {results[0]['scorer'] if results else 'n/a'}", ""]
    for rank, r in enumerate(results, 1):
        flag = "PASS " if r["gates_passed"] else "GATE FAIL"
        lines.append(f"{rank:2}. {r['label']:28} composite {r['composite']:5.1f}  {flag}")
        for c in rubric.gates:
            g = r["gates"][c.id]
            lines.append(f"      gate {c.id:20} {'pass' if g['pass'] else 'FAIL'}  {g['evidence'][:90]!r}")
        for c in rubric.points:
            s = r["scores"][c.id]
            lines.append(f"      {c.id:25} {s['score']}/5 conf {s['confidence']}  {s['evidence'][:80]!r}")
        if r["anomalies"]:
            lines.append(f"      anomalies: {'; '.join(r['anomalies'])}")
        if r["low_confidence"]:
            lines.append(f"      check by hand: {', '.join(r['low_confidence'])}")
    passing = [r for r in results if r["gates_passed"]]
    lines.append("")
    if passing:
        top = passing[:2]
        lines.append("Would send: " + ", ".join(t["label"] for t in top))
        least = min(top, key=lambda t: min(v["confidence"] for v in t["scores"].values()))
        lines.append(f"Least confident of those: {least['label']} (lowest per-criterion confidence); the likely way it is wrong is the criterion marked 'check by hand'.")
    else:
        lines.append("Would send: none; every record failed a gate. Widen the search or revisit the gates with the hiring manager.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Score records against a rubric and print a ranked shortlist.")
    ap.add_argument("--rubric", required=True)
    ap.add_argument("--records", required=True, help="JSON list of {id, label, text}")
    ap.add_argument("--model", choices=["anthropic", "openai"], help="use a model scorer if its API key is set")
    ap.add_argument("--out", help="write the full results as JSON")
    a = ap.parse_args(argv)
    rubric = load_rubric(a.rubric)
    records = json.loads(Path(a.records).read_text(encoding="utf-8"))
    if a.model and not os.environ.get({"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}[a.model]):
        print(f"no key for {a.model}; using the deterministic scorer", file=sys.stderr)
    results = score_records(rubric, records, a.model)
    print(render(rubric, results))
    if a.out:
        Path(a.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
