"""FAQ lookup with hybrid FTS + semantic search.

Given an inbound question, returns the best canonical FAQ match plus all
verbatim past occurrences. The email-drafting workflow's FAQ-First step calls
this BEFORE composing a reply.

Two lookup modes (default = hybrid):
  - FTS: BM25 ranking on tokenized stems (fast, exact-keyword)
  - Semantic: cosine distance on BAAI/bge-small-en-v1.5 (~384-dim, local)
  - Hybrid: both runs, RRF-fused

Semantic mode requires vec_faqs, populated by search/embed_faqs.py (it lives
in the separate vec.db of the 2-file layout). Empty vec_faqs = no semantic
hits, FTS still works.

Usage:
  CLI:   python faq_lookup.py "How do I submit my project?"
  CLI:   python faq_lookup.py --json "where is the deadline"
  CLI:   python faq_lookup.py --mode semantic "do I need to code"
  CLI:   python faq_lookup.py --mode fts "submission deadline"
  Lib:   from faq_lookup import lookup; hits = lookup(conn, question)

Returns up to N hits with:
  - approved canonical FAQ (if any) -- eligible for auto-draft pre-fill
  - top verbatim occurrences -- raw_question/raw_answer for context
  - faq_links -- grounding rows (events / projects / people)
"""
import paths
import argparse
import json
import os
import re
import sqlite3
import _db  # unified connector (busy_timeout + FK ON)
import struct
import sys

# Quiet HF / transformers progress bars BEFORE any import that loads them
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

# Semantic distance thresholds (cosine distance, lower = closer):
#  < SEMANTIC_STRONG -> strong match, eligible to surface as canonical hit
#  < SEMANTIC_WEAK   -> weak match, surface as context but flag confidence
#  >= SEMANTIC_WEAK  -> miss, do not surface
SEMANTIC_STRONG = 0.65
SEMANTIC_WEAK = 0.85


def _fts_query_from_question(q: str) -> str:
    stop = {"the", "a", "an", "to", "for", "of", "in", "on", "at", "by",
            "is", "are", "was", "were", "be", "been", "being", "have", "has",
            "had", "do", "does", "did", "i", "you", "we", "they", "he", "she",
            "it", "this", "that", "with", "and", "or", "but", "if", "as",
            "how", "what", "where", "when", "who", "why", "which", "can",
            "could", "should", "would", "will", "from", "my", "your"}
    # Tokens: words starting with a letter; strip leading non-alpha (e.g. "r2007" -> "2007"
    # would still match the regex, but we want to keep pure-word tokens only).
    tokens = re.findall(r"[A-Za-z][A-Za-z]+", q.lower())  # alpha-only -- no digits in tokens
    salient = [t for t in tokens if t not in stop and len(t) > 2]
    if not salient:
        salient = tokens[:3] or ["faq"]
    # Quote each token to force literal interpretation. FTS5 reads bare tokens like
    # "2007" or "phdr" as column references, which errors when no such column exists.
    quoted = [f'"{t}"' for t in salient[:8]]
    return " OR ".join(quoted) or "*"


_model_cache = None


def _get_model():
    global _model_cache
    if _model_cache is None:
        from sentence_transformers import SentenceTransformer
        _model_cache = SentenceTransformer(MODEL_NAME)
    return _model_cache


def _get_vec_conn():
    """Open a connection with sqlite-vec loaded and vec.db attached."""
    import sqlite_vec
    conn = _db.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # vec_* virtual tables live in the separate vec.db (2-file layout).
    if not any(r[1] == "vecdb" for r in conn.execute("PRAGMA database_list")):
        conn.execute("ATTACH DATABASE '%s' AS vecdb" % paths.VEC_DB_PATH)
    return conn


def _serialize_vec(vec):
    return struct.pack(f"{DIMS}f", *vec.tolist())


def lookup_fts(conn: sqlite3.Connection, question: str, top_n: int = 5):
    """FTS-only lookup. Returns list of dicts with score (lower = better)."""
    fts_q = _fts_query_from_question(question)
    rows = list(conn.execute("""
        SELECT f.id, f.faq_id, f.question_canonical, f.answer_canonical,
               f.topic, f.scope, f.context_slug, f.ask_count, f.status,
               bm25(faqs_fts) AS score
        FROM faqs_fts
        JOIN faqs f ON f.id = faqs_fts.rowid
        WHERE faqs_fts MATCH ?
        ORDER BY score
        LIMIT ?
    """, (fts_q, top_n)))
    return [
        {"id": r[0], "faq_id": r[1], "question": r[2], "answer": r[3],
         "topic": r[4], "scope": r[5], "context_slug": r[6],
         "ask_count": r[7], "status": r[8], "fts_score": r[9],
         "fts_query": fts_q}
        for r in rows
    ]


def lookup_semantic(question: str, top_n: int = 5):
    """Semantic-only lookup via sqlite-vec. Returns list with distance."""
    conn = _get_vec_conn()
    vec_count = conn.execute("SELECT COUNT(*) FROM vec_faqs").fetchone()[0]
    if vec_count == 0:
        conn.close()
        return []
    model = _get_model()
    emb = model.encode(question, normalize_embeddings=True)
    vec_bytes = _serialize_vec(emb)
    rows = list(conn.execute("""
        SELECT f.id, f.faq_id, f.question_canonical, f.answer_canonical,
               f.topic, f.scope, f.context_slug, f.ask_count, f.status,
               vf.distance
        FROM vec_faqs vf
        JOIN faqs f ON f.id = vf.rowid
        WHERE vf.embedding MATCH ? AND k = ?
        ORDER BY vf.distance
    """, (vec_bytes, top_n)))
    conn.close()
    return [
        {"id": r[0], "faq_id": r[1], "question": r[2], "answer": r[3],
         "topic": r[4], "scope": r[5], "context_slug": r[6],
         "ask_count": r[7], "status": r[8], "semantic_distance": r[9]}
        for r in rows
    ]


def lookup_hybrid(conn: sqlite3.Connection, question: str, top_n: int = 5):
    """Hybrid lookup: FTS + semantic, RRF-fused.

    RRF formula: score(d) = sum_i (w_i / (K + rank_i(d)))
    Higher RRF = better. Distance/BM25 are converted via rank, not raw scores.
    """
    K = 60  # standard RRF constant
    W_SEM = 0.55  # semantic gets slightly higher weight (better at intent)
    W_FTS = 0.45

    fts_hits = lookup_fts(conn, question, top_n=top_n * 2)
    sem_hits = lookup_semantic(question, top_n=top_n * 2)

    by_id = {}
    for rank, h in enumerate(fts_hits):
        d = by_id.setdefault(h["id"], h.copy())
        d["fts_rank"] = rank
        d["rrf"] = d.get("rrf", 0.0) + W_FTS / (K + rank)
    for rank, h in enumerate(sem_hits):
        d = by_id.setdefault(h["id"], h.copy())
        d["semantic_rank"] = rank
        d["semantic_distance"] = h.get("semantic_distance")
        d["rrf"] = d.get("rrf", 0.0) + W_SEM / (K + rank)

    fused = sorted(by_id.values(), key=lambda x: -x.get("rrf", 0))
    return fused[:top_n]


def lookup(conn: sqlite3.Connection, question: str, top_n: int = 5,
           mode: str = "hybrid") -> dict:
    """Main lookup entry point.

    Modes:
      - 'hybrid' (default): FTS + semantic, RRF-fused
      - 'fts': FTS only (faster, no model load)
      - 'semantic': semantic only (requires model load)
    """
    out = {"input": question, "mode": mode,
           "approved_canonical": None, "draft_canonical": [],
           "verbatim_occurrences": [], "links": [],
           "confidence": "none"}

    if mode == "fts":
        hits = lookup_fts(conn, question, top_n)
    elif mode == "semantic":
        hits = lookup_semantic(question, top_n)
    else:
        hits = lookup_hybrid(conn, question, top_n)

    # Find best APPROVED hit
    approved = [h for h in hits if h.get("status") == "approved"]
    if approved:
        best = approved[0]
        out["approved_canonical"] = best
        out["draft_canonical"] = [h for h in approved[1:]]

        # Confidence: based on semantic_distance if available, else FTS heuristic
        sd = best.get("semantic_distance")
        if sd is not None:
            if sd < SEMANTIC_STRONG:
                out["confidence"] = "strong"
            elif sd < SEMANTIC_WEAK:
                out["confidence"] = "weak"
            else:
                out["confidence"] = "miss"
        else:
            # FTS-only mode: confidence based on whether we got a hit
            out["confidence"] = "weak"

    # Drafts/proposed (lower-priority surface for human review)
    drafts_proposed = [h for h in hits if h.get("status") in ("draft", "proposed")]
    if not out["approved_canonical"] and drafts_proposed:
        for h in drafts_proposed[:top_n]:
            out["draft_canonical"].append(h)

    # Verbatim occurrences (raw asks). Useful even if canonical exists.
    fts_q = _fts_query_from_question(question)
    for r in conn.execute("""
        SELECT o.id, o.faq_id, o.source, o.raw_question, o.raw_answer,
               o.asked_at, o.is_authority, o.answered_by_handle,
               bm25(faq_occurrences_fts) AS score
        FROM faq_occurrences_fts
        JOIN faq_occurrences o ON o.id = faq_occurrences_fts.rowid
        WHERE faq_occurrences_fts MATCH ?
        ORDER BY score
        LIMIT ?
    """, (fts_q, top_n)):
        out["verbatim_occurrences"].append({
            "id": r[0], "faq_id": r[1], "source": r[2], "raw_question": r[3],
            "raw_answer": r[4], "asked_at": r[5], "is_authority": r[6],
            "answered_by": r[7], "score": r[8],
        })

    # Links (grounding rows) for the canonical FAQ if present.
    if out["approved_canonical"]:
        for r in conn.execute("""
            SELECT table_name, row_id, relation FROM faq_links WHERE faq_id = ?
        """, (out["approved_canonical"]["id"],)):
            out["links"].append({"table": r[0], "row_id": r[1], "relation": r[2]})

    return out


def format_human(result: dict) -> str:
    lines = [f"Q: {result['input']}",
             f"Mode: {result['mode']}",
             f"Confidence: {result['confidence']}",
             ""]
    a = result.get("approved_canonical")
    if a:
        rrf = a.get('rrf')
        rrf_str = f"RRF={rrf:.4f} " if rrf is not None else ""
        sd = a.get('semantic_distance')
        sd_str = f"sem={sd:.3f} " if sd is not None else ""
        fts = a.get('fts_score')
        fts_str = f"fts={fts:.2f} " if fts is not None else ""
        lines.append(f"APPROVED FAQ {a['faq_id']} ({rrf_str}{sd_str}{fts_str}):")
        lines.append(f"  Q: {a['question']}")
        lines.append(f"  A: {(a['answer'] or '')[:400]}{'...' if len(a.get('answer') or '') > 400 else ''}")
        if result["links"]:
            lines.append(f"  links: {result['links']}")
        lines.append("")
    elif result["draft_canonical"]:
        lines.append("No approved match. Drafts/proposed nearby:")
        for d in result["draft_canonical"][:3]:
            status = d.get("status", "approved")
            lines.append(f"  [{status}] {(d.get('question') or '(no canonical yet)')[:80]}")
        lines.append("")
    else:
        lines.append("No canonical match. Will create new proposed occurrence on log.")
        lines.append("")

    if result["verbatim_occurrences"]:
        lines.append(f"Past asks ({len(result['verbatim_occurrences'])}):")
        for o in result["verbatim_occurrences"][:5]:
            auth = " [AUTH]" if o.get("is_authority") else ""
            ans = ((o.get("raw_answer") or "")[:80]).replace("\n", " ")
            lines.append(f"  [{o['source']:7s}] {(o['raw_question'] or '')[:80]}")
            if ans:
                lines.append(f"      ans: {ans}{auth}")
    return "\n".join(lines)


def log_miss(conn: sqlite3.Connection, source: str, source_row_id: str,
             raw_question: str, asked_at: str = None,
             asked_by_handle: str = "") -> int:
    """Insert a new proposed faq_occurrence for a question that had no match.
    Idempotent on (source, source_row_id). Returns id, or None if existed."""
    if not asked_at:
        from datetime import datetime, timezone
        asked_at = datetime.now(tz=timezone.utc).isoformat()
    try:
        cur = conn.execute("""
            INSERT INTO faq_occurrences
              (source, source_table, source_row_id, raw_question, asked_at,
               asked_by_handle, detector, confidence)
            VALUES (?, ?, ?, ?, ?, ?, 'manual-on-miss', 1.0)
        """, (source, f"{source}_messages" if source != "email" else "emails",
              source_row_id, raw_question, asked_at, asked_by_handle))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def main():
    # Detect bare positional (legacy invocation)
    raw_args = sys.argv[1:]
    if raw_args and raw_args[0] not in ("lookup", "log", "-h", "--help"):
        raw_args = ["lookup"] + raw_args

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    p_look = sub.add_parser("lookup", help="(default) search FAQ for a question")
    p_look.add_argument("question")
    p_look.add_argument("--json", action="store_true")
    p_look.add_argument("--top", type=int, default=5)
    p_look.add_argument("--mode", choices=["hybrid", "fts", "semantic"], default="hybrid")

    p_log = sub.add_parser("log", help="log a new proposed occurrence after a miss")
    p_log.add_argument("--source", required=True, choices=["email", "discord", "beeper", "slack"])
    p_log.add_argument("--source-id", required=True, help="source row primary key")
    p_log.add_argument("--asked-at", help="ISO timestamp; defaults to now")
    p_log.add_argument("--handle", default="", help="raw sender handle/email")
    p_log.add_argument("question")

    args = ap.parse_args(raw_args)

    conn = _db.connect(DB)

    if args.cmd == "log":
        new_id = log_miss(conn, args.source, args.source_id, args.question,
                          args.asked_at, args.handle)
        if new_id:
            print(f"Logged proposed faq_occurrence id={new_id}")
        else:
            print("Already logged (UNIQUE on source+source_id)")
        return

    result = lookup(conn, args.question, args.top, mode=args.mode)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_human(result))


if __name__ == "__main__":
    main()
