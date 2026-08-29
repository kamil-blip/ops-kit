"""FAQ retrieval gate.

gate(question_text) -> GateResult(tier, faq_id, score, ...)

Given an inbound question, embeds it (BAAI/bge-small-en-v1.5, 384-dim, the same
model + vec_faqs table used by search/embed_faqs.py / faq_lookup.py) and scores
it by cosine similarity against the APPROVED canonical FAQs. The cosine is
derived from the sqlite-vec L2 distance on unit-normalized vectors:
cos = 1 - dist^2/2.

PREREQUISITE: vec_faqs is populated by search/embed_faqs.py. Until that has
run (and until FAQs are approved), the gate escalates everything, which is the
safe default.

Three tiers (tunable: DRAFT_THRESHOLD / CITE_THRESHOLD / CITE_MARGIN):
    cos >= DRAFT_THRESHOLD (0.85) -> 'draft'       (strong, safe to pre-fill a body)
    CITE_THRESHOLD (0.74) <= cos  -> 'draft_cite'  (CITE-ONLY: name the FAQ + score,
                                                    never compose a reply body)
    cos <  CITE_THRESHOLD (0.74)  -> 'escalate'     (no confident canonical match)

CITE-ONLY BELOW 0.85: the draft_cite band does not sanction a composed answer
body. Between 0.74 and 0.85 the match is plausible but not safe to paste as a
reply, so the gate returns a CITATION only (matched question + canonical-answer
reference + confidence). The wrapper surfaces that for a human to confirm; it
must NOT turn it into an outbound body. Only 'draft' (>=0.85) composes.

MARGIN CHECK: a match is only cited when it clearly beats the runner-up:
(top_cos - second_cos) >= CITE_MARGIN (0.05). Near-ties (two FAQs almost
equally close) are ambiguous about WHICH canonical answer applies, so they
escalate instead of citing a coin-flip. The same margin downgrades a near-tie
in the draft band (>=0.85) to draft_cite rather than composing.

RISK-TOPIC OVERRIDE: if the question OR the matched FAQ touches money/time
commitments (payment, prize, deadline, reimbursement, refund, wire, invoice,
payout, credit), the gate NEVER returns 'draft' -- it is capped at 'draft_cite'
so a human always confirms figures that drift. A matched FAQ answer for these
topics is intentionally placeholder-ized ({{...}}), so the figures must be
resolved live, not read off a stale FAQ.

NO-NEW-PROMISES CONTRACT: the gate returns the CANONICAL answer text only.
Callers may surface it, cite it, or escalate -- they may NOT append new
commitments (dates, amounts, guarantees, "I will send X by Y") on top of it.
Any figure/date belongs in a {{placeholder}} resolved from the live
source-of-truth table for that topic, never invented at reply time.
`lint_no_new_promises(canonical_answer, proposed_reply)` enforces this: it flags
promissory/commitment phrasing that appears in the reply but not the canonical
answer, so a wrapper can block the send.

CLI:  python faq_gate.py "when do winners get announced?"
      python faq_gate.py --replay path/to/faq_replay_set.json
"""
from __future__ import annotations
import paths

import argparse
import io
import json
import os
import re
import sqlite3
import _db  # unified connector (busy_timeout + FK ON)
import struct
import sys
from dataclasses import dataclass

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# UTF-8 for Windows. reconfigure in place, do NOT rebind sys.stdout to a new
# TextIOWrapper: the replaced wrapper gets garbage-collected and close()s the
# shared underlying buffer, killing stdout for any process that imports more
# than one module doing this (e.g. the MCP server importing faq_gate + faq_lookup).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB = str(paths.DB_PATH)
MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMS = 384

# --- tunable knobs (replay set is calibrated against these) ------------------
DRAFT_THRESHOLD = 0.85   # >= -> 'draft' (compose a body)
CITE_THRESHOLD = 0.74    # >= -> 'draft_cite' (cite-only), else 'escalate'
CITE_MARGIN = 0.05       # top must beat second by this or the match escalates (near-tie guard)

# risk topics: figures/commitments that drift => never auto-draft
RISK_RX = re.compile(
    r"\b(payment|pay|paid|payout|prize|reimburs|refund|wire|swift|invoice|"
    r"deadline|due date|credits?|stipend|bank|compensat)\b", re.I)
RISK_TOPICS = {"prizes", "payments", "deadline"}

# promissory phrasing for the no-new-promises lint
PROMISE_RX = re.compile(
    r"\b(i(?:'| wi)ll|we(?:'| wi)ll|i'll|we'll|i promise|we promise|guarantee|"
    r"guaranteed|by (?:mon|tue|wed|thu|fri|sat|sun|next|tomorrow|end of)|"
    r"you will receive|we will send|i will send|no later than|within \d+)\b", re.I)


@dataclass
class GateResult:
    tier: str            # draft | draft_cite | escalate
    faq_id: str | None
    score: float         # cosine similarity to the matched FAQ (0 if none)
    answer: str | None   # canonical answer text ONLY (no appended promises)
    matched_question: str | None
    risk_capped: bool    # True if risk-topic override downgraded draft->draft_cite
    second_score: float = 0.0   # cosine of the runner-up FAQ (0 if none) -- margin check input
    margin: float = 0.0         # score - second_score (near-tie guard)
    cite_only: bool = False     # True in the draft_cite band: cite the FAQ, never compose a body

    def as_tuple(self):
        """Primary contract: (tier, faq_id, score)."""
        return (self.tier, self.faq_id, self.score)


_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _vec_conn():
    import sqlite_vec
    conn = _db.connect(DB, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # vec_* virtual tables live in the separate vec.db (2-file layout).
    if not any(r[1] == "vecdb" for r in conn.execute("PRAGMA database_list")):
        conn.execute("ATTACH DATABASE '%s' AS vecdb" % paths.VEC_DB_PATH)
    return conn


def _is_risky(question: str, faq_row) -> bool:
    if RISK_RX.search(question or ""):
        return True
    if faq_row:
        if (faq_row["topic"] or "") in RISK_TOPICS:
            return True
        if RISK_RX.search((faq_row["question_canonical"] or "") + " " +
                          (faq_row["answer_canonical"] or "")):
            return True
    return False


def gate(question_text: str) -> GateResult:
    """Score a question against approved canonical FAQs. See module docstring."""
    conn = _vec_conn()
    conn.row_factory = sqlite3.Row
    try:
        n = conn.execute("SELECT COUNT(*) FROM vec_faqs").fetchone()[0]
        if not n or not (question_text or "").strip():
            return GateResult("escalate", None, 0.0, None, None, False)
        emb = _get_model().encode(question_text, normalize_embeddings=True)
        vb = struct.pack(f"{DIMS}f", *emb.tolist())
        rows = conn.execute("""
            SELECT f.faq_id, f.question_canonical, f.answer_canonical, f.topic,
                   vf.distance
            FROM vec_faqs vf JOIN faqs f ON f.id = vf.rowid
            WHERE vf.embedding MATCH ? AND k = 8 AND f.status = 'approved'
            ORDER BY vf.distance
            LIMIT 2
        """, (vb,)).fetchall()
    finally:
        conn.close()

    if not rows:
        return GateResult("escalate", None, 0.0, None, None, False)

    row = rows[0]
    cos = 1.0 - (row["distance"] ** 2) / 2.0
    cos2 = (1.0 - (rows[1]["distance"] ** 2) / 2.0) if len(rows) > 1 else 0.0
    margin = round(cos - cos2, 4)

    # Base tier from thresholds.
    if cos >= DRAFT_THRESHOLD:
        tier = "draft"
    elif cos >= CITE_THRESHOLD:
        tier = "draft_cite"
    else:
        tier = "escalate"

    # Margin / near-tie guard: a coin-flip between two close FAQs is unsafe.
    #   - in the cite band, near-ties escalate (don't cite the wrong FAQ)
    #   - in the draft band, near-ties downgrade to cite-only (don't compose a coin-flip)
    if tier == "draft_cite" and margin < CITE_MARGIN:
        tier = "escalate"
    elif tier == "draft" and margin < CITE_MARGIN:
        tier = "draft_cite"

    # Risk-topic override: money/time figures always need a human, never auto-draft.
    risk_capped = False
    if tier == "draft" and _is_risky(question_text, row):
        tier = "draft_cite"
        risk_capped = True

    # Cite-only for the whole draft_cite band: no composed body below DRAFT_THRESHOLD.
    cite_only = (tier == "draft_cite")

    return GateResult(tier, row["faq_id"], round(cos, 4),
                      row["answer_canonical"] if tier != "escalate" else None,
                      row["question_canonical"] if tier != "escalate" else None,
                      risk_capped, second_score=round(cos2, 4), margin=margin,
                      cite_only=cite_only)


def lint_no_new_promises(canonical_answer: str, proposed_reply: str) -> list[str]:
    """Flag commitment phrasing present in proposed_reply but not the canonical
    answer. Non-empty result => the reply adds a new promise the gate never
    sanctioned; a send wrapper should block. Returns the offending snippets."""
    canon = (canonical_answer or "").lower()
    hits = []
    for m in PROMISE_RX.finditer(proposed_reply or ""):
        phrase = m.group(0)
        if phrase.lower() not in canon:
            s = max(0, m.start() - 20)
            e = min(len(proposed_reply), m.end() + 20)
            hits.append(proposed_reply[s:e].strip())
    return hits


def format_citation(gr: GateResult) -> str:
    """Cite-only rendering for the draft_cite band: a citation the human
    verifies, NOT an outbound reply body. Names the matched FAQ, the canonical
    answer to check against, and the confidence. Never appended to a send path."""
    conf = f"{gr.score:.2f}" if gr.score else "n/a"
    lines = [
        f"[cite-only, confidence {conf}] no auto-draft below {DRAFT_THRESHOLD:.2f}.",
        f"Closest canonical FAQ ({gr.faq_id}): {gr.matched_question or '(none)'}",
        f"Canonical answer to verify against: {(gr.answer or '(none)').strip()}",
        "Human confirms this FAQ fits the thread before any reply is sent.",
    ]
    if gr.risk_capped:
        lines.append("Risk topic (money/time): figures must be resolved live, not read off the FAQ.")
    return "\n".join(lines)


def _replay(path: str) -> int:
    data = json.load(open(path, encoding="utf-8"))
    ok = 0
    print(f"{'exp':<11} {'got':<11} {'cos':>6}  question")
    print("-" * 80)
    for item in data:
        r = gate(item["question"])
        match = r.tier == item["expected_tier"]
        ok += match
        flag = "" if match else "  <-- MISMATCH"
        print(f"{item['expected_tier']:<11} {r.tier:<11} {r.score:>6.3f}  "
              f"{item['question'][:44]}{flag}")
    print("-" * 80)
    print(f"REPLAY: {ok}/{len(data)} correct tier "
          f"(DRAFT_THRESHOLD={DRAFT_THRESHOLD}, CITE_THRESHOLD={CITE_THRESHOLD})")
    return ok == len(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?")
    ap.add_argument("--replay", help="path to replay-set JSON")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.replay:
        sys.exit(0 if _replay(args.replay) else 1)
    if not args.question:
        ap.error("provide a question or --replay")
    r = gate(args.question)
    if args.json:
        print(json.dumps(r.__dict__, indent=1, default=str))
    else:
        print(f"tier={r.tier} faq_id={r.faq_id} score={r.score} "
              f"second={r.second_score} margin={r.margin} "
              f"risk_capped={r.risk_capped} cite_only={r.cite_only}")
        if r.cite_only:
            print(format_citation(r))
        elif r.matched_question:
            print(f"  matched: {r.matched_question}")
            print(f"  answer:  {(r.answer or '')[:200]}")


if __name__ == "__main__":
    main()
