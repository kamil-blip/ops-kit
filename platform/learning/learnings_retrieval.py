"""Hybrid learnings retrieval.

Replaces the keyword-overlap-only surfacing in hooks/learning_loop.py:surface_learnings
with a hybrid Reciprocal-Rank-Fusion of two arms plus a guaranteed Tier-A slot:

  1. FTS arm     : learnings_fts BM25 over the prompt's salient tokens (fast, no model).
  2. Vector arm  : sqlite-vec KNN over vec_learnings. vec0 MATCH returns L2 distance;
                   vectors are unit-normalized, so cosine = 1 - L2^2/2 exactly
                   (verified on real vectors). Corpus-size-gated binary quantization
                   pre-filters with Hamming then reranks by float cosine above
                   BINARY_THRESHOLD rows; below it, plain float KNN (faster at small n).
  3. Tier-A slot : residency='graduated' load-bearing rules. Relevance-gated in normal
                   operation (a graduated rule only takes a slot when it matches), but
                   the FALLBACK when an arm errors (so a sqlite-vec failure degrades to
                   Tier-A, never a crash).

Contract (fail-safety):
  - retrieve() NEVER raises. Any arm error is caught; the hot path degrades to FTS +
    Tier-A, or Tier-A only, or [].
  - The hot path never does a COLD model load (would blow the latency budget). The
    vector arm runs only with a caller-supplied query_vec, an already-warm model, or
    explicit embed=True (eval/CLI/daemon). One-shot hook processes degrade to FTS +
    Tier-A every time, which is correct and fast. A warm embedding service is the
    scale path (tracked separately).

Abstention: if both arms RAN successfully and matched nothing, retrieve() returns
[] (or only a relevant Tier-A rule) -- it never force-fits a low-relevance learning.

Usage:
    python learnings_retrieval.py --search "never auto send email"   # embed=True, full hybrid
    python learnings_retrieval.py --selftest                         # math + fail-safety + abstention
    python learnings_retrieval.py --stats
"""
from __future__ import annotations

import paths

import math
import re
import sqlite3
import struct
import sys
import time

DB = str(paths.DB_PATH)
MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMS = 384
RRF_K = 60                  # standard RRF constant
TOP_K = 3                   # surface at most 3 (matches the legacy hook)
# Two-tier admission: FTS matches (real keyword overlap) are always admitted; a
# VECTOR-ONLY match must clear MIN_COSINE. bge-small-en is anisotropic -- random/
# gibberish text still lands at ~0.64 cosine (HIGHER than coherent off-topic ~0.47),
# so the vector floor must sit above that gibberish band. Measured: on-topic >=0.69,
# gibberish <=0.64, off-topic <=0.47. 0.66 abstains on both noise classes while the
# FTS arm carries genuinely on-topic (keyword-bearing) prompts regardless.
MIN_COSINE = 0.66           # vector-only admission floor (abstention gate)
CAND_PER_ARM = 20           # candidates each arm contributes to fusion
BINARY_THRESHOLD = 20000    # above this many vec rows, use binary Hamming pre-filter
BINARY_PREFILTER = 200      # Hamming pre-filter width before float rerank

_MODEL = None               # lazy SentenceTransformer singleton (process-warm)


# --------------------------------------------------------------------------
# connections / model
# --------------------------------------------------------------------------
def get_db(read_only=True):
    uri = f"file:{DB}?mode=ro" if read_only else DB
    db = sqlite3.connect(uri, uri=read_only, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    try:
        import sqlite_vec
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        # vec_* embedding tables live in a separate vec DB file, attached as vecdb
        db.execute("ATTACH DATABASE '%s' AS vecdb" % paths.VEC_DB_PATH) if not any(r[1] == 'vecdb' for r in db.execute('PRAGMA database_list')) else None
        db.enable_load_extension(False)
    except Exception:
        pass  # vector arm will no-op; FTS + Tier-A still work
    return db


def _get_model(allow_load):
    global _MODEL
    if _MODEL is None and allow_load:
        try:
            from sentence_transformers import SentenceTransformer
            _MODEL = SentenceTransformer(MODEL_NAME)
        except Exception:
            _MODEL = None
    return _MODEL


def query_vector(prompt, allow_load=False):
    """Return serialized float32 query vector, or None. allow_load gates the cold model
    load out of the hot path."""
    m = _get_model(allow_load)
    if m is None:
        return None
    try:
        v = m.encode(prompt, normalize_embeddings=True)
        return struct.pack(f"{DIMS}f", *list(v))
    except Exception:
        return None


# --------------------------------------------------------------------------
# arms
# --------------------------------------------------------------------------
_TOK = re.compile(r"[a-zA-Z]{3,}")


def _fts_query(prompt):
    toks = {t.lower() for t in _TOK.findall(prompt or "")}
    toks = [t for t in toks if t not in _STOP]
    if not toks:
        return None
    return " OR ".join(f'"{t}"' for t in sorted(toks)[:24])


_STOP = {"the", "and", "for", "you", "with", "this", "that", "have", "are", "not",
         "but", "from", "any", "can", "all", "use", "via", "per", "out", "into",
         "your", "our", "was", "has", "had", "its", "his", "her", "they", "them"}


def fts_arm(conn, prompt, limit=CAND_PER_ARM):
    """Return [(learning_id, rank_position)] from learnings_fts BM25, best first."""
    q = _fts_query(prompt)
    if not q:
        return []
    rows = conn.execute(
        """SELECT l.id AS lid
           FROM learnings_fts f JOIN learnings l ON l.rowid = f.rowid
           WHERE learnings_fts MATCH ? AND l.status='active'
           ORDER BY bm25(learnings_fts) LIMIT ?""",
        (q, limit),
    ).fetchall()
    return [(r["lid"], i) for i, r in enumerate(rows)]


def vector_arm(conn, qvec, limit=CAND_PER_ARM, use_binary=None):
    """Return [(learning_id, cosine)] via sqlite-vec KNN. cosine = 1 - L2^2/2.

    Above BINARY_THRESHOLD rows, pre-filter by Hamming on binary-quantized vectors
    then rerank the survivors by float cosine. Below it, plain float KNN.
    Filters out below-MIN_COSINE matches (abstention gate).
    """
    if not qvec:
        return []
    n = conn.execute("SELECT COUNT(*) FROM vec_learnings").fetchone()[0]
    if use_binary is None:
        use_binary = n > BINARY_THRESHOLD

    if use_binary:
        # Coarse Hamming pre-filter on binary quantization, then float rerank.
        pre = conn.execute(
            """SELECT rowid FROM vec_learnings
               ORDER BY vec_distance_hamming(
                   vec_quantize_binary(embedding), vec_quantize_binary(?)) LIMIT ?""",
            (qvec, BINARY_PREFILTER),
        ).fetchall()
        ids = [r["rowid"] for r in pre]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""SELECT vl.rowid AS lid, vec_distance_l2(vl.embedding, ?) AS d
                FROM vec_learnings vl
                JOIN learnings l ON l.id = vl.rowid
                WHERE vl.rowid IN ({placeholders}) AND l.status='active'""",
            [qvec] + ids,
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT vl.rowid AS lid, vl.distance AS d
               FROM vec_learnings vl JOIN learnings l ON l.id = vl.rowid
               WHERE vl.embedding MATCH ? AND k = ? AND l.status='active'
               ORDER BY vl.distance""",
            (qvec, limit * 3),
        ).fetchall()

    scored = []
    for r in rows:
        d = r["d"]
        cos = 1.0 - (d * d) / 2.0      # exact for unit-normalized vectors
        if cos >= MIN_COSINE:
            scored.append((r["lid"], cos))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


def tier_a(conn):
    """Active residency='graduated' load-bearing rules (criticality order). The Tier-A
    membership is curated + cap-bounded elsewhere; this just reads it."""
    rows = conn.execute(
        """SELECT id, title, priority FROM learnings
           WHERE status='active' AND residency='graduated'
           ORDER BY CASE LOWER(priority) WHEN 'critical' THEN 0 WHEN 'p0' THEN 1
                    WHEN 'p1' THEN 2 ELSE 3 END, id"""
    ).fetchall()
    return [r["id"] for r in rows]


# --------------------------------------------------------------------------
# fusion
# --------------------------------------------------------------------------
def rrf_fuse(arms):
    """arms = list of [(id, rank_or_score)]. Returns ordered [id] by RRF.
    Each arm is treated by RANK POSITION (0-based), so score scales differ across arms
    don't bias the fusion."""
    score = {}
    for arm in arms:
        for rank, (lid, _val) in enumerate(arm):
            score[lid] = score.get(lid, 0.0) + 1.0 / (RRF_K + rank)
    return [lid for lid, _s in sorted(score.items(), key=lambda kv: -kv[1])]


def _hydrate(conn, ids):
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, description, priority, residency FROM learnings WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    out = []
    for i in ids:
        r = by_id.get(i)
        if r:
            out.append({"id": r["id"], "title": r["title"] or "", "description": r["description"] or "",
                        "priority": (r["priority"] or "").lower(), "residency": r["residency"]})
    return out


# --------------------------------------------------------------------------
# public entrypoint
# --------------------------------------------------------------------------
def retrieve(prompt, conn=None, query_vec=None, k=TOP_K, embed=False, budget_ms=100):
    """Hybrid retrieve. NEVER raises. Returns up to k learning dicts.

    Tier-A slot: when a graduated rule is relevant (appears in either arm's candidate
    pool), reserve it the last slot. If an arm ERRORS (degraded), fall back to Tier-A.
    If both arms ran clean and matched nothing -> [] (abstention).
    """
    own = conn is None
    results = []
    try:
        if own:
            conn = get_db(read_only=True)
        t0 = time.monotonic()
        degraded = False

        # FTS arm
        try:
            fts = fts_arm(conn, prompt)
        except Exception:
            fts, degraded = [], True

        # Vector arm (only with a vector we can get cheaply, unless embed=True)
        qvec = query_vec
        if qvec is None:
            allow_load = embed
            if (time.monotonic() - t0) * 1000 < budget_ms or embed:
                try:
                    qvec = query_vector(prompt, allow_load=allow_load)
                except Exception:
                    qvec = None
        try:
            vec = vector_arm(conn, qvec) if qvec else []
        except Exception:
            vec, degraded = [], True

        cand_ids = {lid for lid, _ in fts} | {lid for lid, _ in vec}
        fused = rrf_fuse([fts, vec])

        # Relevance-gated Tier-A: a graduated rule that actually matched.
        ta = tier_a(conn)
        relevant_ta = [t for t in ta if t in cand_ids]

        if not fused and not relevant_ta:
            # both arms clean-but-empty -> abstain; arm error -> Tier-A fallback
            results = _hydrate(conn, ta[:k]) if degraded else []
            return results

        ordered = []
        if relevant_ta:
            # reserve the last slot for the top relevant Tier-A rule
            head = [i for i in fused if i != relevant_ta[0]][: max(0, k - 1)]
            ordered = head + [relevant_ta[0]]
        else:
            ordered = fused[:k]
        # de-dup, cap
        seen, final = set(), []
        for i in ordered:
            if i not in seen:
                seen.add(i); final.append(i)
            if len(final) >= k:
                break
        results = _hydrate(conn, final)
        return results
    except Exception:
        # last resort: Tier-A only, never raise
        try:
            return _hydrate(conn, tier_a(conn)[:k])
        except Exception:
            return []
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def format_for_injection(results):
    """Render to the hook's surfacing lines. 'Rule:' for load-bearing, else 'Related learning:'."""
    out = []
    for m in results:
        is_rule = m.get("residency") == "graduated" or (
            m.get("priority") == "critical")
        prefix = "Rule" if is_rule else "Related learning"
        desc = (m.get("description") or "").strip().replace("\n", " ")[:180]
        out.append(f"{prefix}: {m['title']}. {desc}" if desc else f"{prefix}: {m['title']}")
    return out


# --------------------------------------------------------------------------
# self-test (math + fail-safety + abstention)
# --------------------------------------------------------------------------
def selftest():
    ok = True
    conn = get_db(read_only=True)

    # 1. cosine identity cos = 1 - L2^2/2 on two real vectors
    rows = conn.execute("SELECT embedding FROM vec_learnings LIMIT 2").fetchall()
    if len(rows) == 2:
        a = struct.unpack(f"{DIMS}f", rows[0]["embedding"])
        b = struct.unpack(f"{DIMS}f", rows[1]["embedding"])
        dot = sum(x * y for x, y in zip(a, b))
        l2sq = sum((x - y) ** 2 for x, y in zip(a, b))
        ident = abs(dot - (1 - l2sq / 2)) < 1e-4
        print(f"  [math] cos={dot:.6f} 1-L2^2/2={1-l2sq/2:.6f} identity={ident}")
        ok &= ident

    # 2. fail-safety: a broken connection must degrade to Tier-A, never raise
    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("simulated sqlite-vec failure")
        def close(self):
            pass
    try:
        r = retrieve("never auto send email", conn=_Boom())
        print(f"  [fail-safety] broken-conn retrieve returned {len(r)} (no raise) OK")
    except Exception as e:
        print(f"  [fail-safety] RAISED {e} -- FAIL"); ok = False

    # 3. abstention: gibberish, arms clean -> empty (no force-fit)
    r = retrieve("zzqx wwfk qqplmth vvbnzz", conn=conn, embed=True)
    abstained = len(r) == 0
    print(f"  [abstention] gibberish -> {len(r)} results (expect 0) {'OK' if abstained else 'FAIL'}")
    ok &= abstained

    # 4. real recall: a known rule should surface for its query
    #    (only meaningful once the learnings table has content; on a fresh empty
    #    DB this reports 0 results, which is the correct abstention behavior)
    r = retrieve("never auto send email confirm before sending", conn=conn, embed=True)
    hit = any("send" in (x["title"] + x["description"]).lower() for x in r)
    print(f"  [recall] 'auto send email' -> {len(r)} results, on-topic hit={hit}")
    for x in r:
        print(f"           [{x['priority']:8}] {x['title'][:60]}")
    ok &= (len(r) > 0 and hit)

    conn.close()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


def stats():
    conn = get_db(read_only=True)
    print("active learnings:", conn.execute("SELECT COUNT(*) FROM learnings WHERE status='active'").fetchone()[0])
    print("vec_learnings:", conn.execute("SELECT COUNT(*) FROM vec_learnings").fetchone()[0])
    print("tier-A (graduated):", len(tier_a(conn)))
    print(f"binary-quant active: {conn.execute('SELECT COUNT(*) FROM vec_learnings').fetchone()[0] > BINARY_THRESHOLD} (threshold {BINARY_THRESHOLD})")
    conn.close()


def main():
    a = sys.argv[1:] or ["--stats"]
    if a[0] == "--selftest":
        sys.exit(0 if selftest() else 1)
    elif a[0] == "--search":
        q = " ".join(a[1:]) or "never auto send email"
        for line in format_for_injection(retrieve(q, embed=True)):
            print(" ", line)
    else:
        stats()


if __name__ == "__main__":
    main()
