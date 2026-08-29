"""rrf_search.py: reusable single-SQL RRF (FTS5 + sqlite-vec + graph) primitive
(pattern: Alex Garcia / sqlite-vec docs).

One parameterized CTE template gives every corpus fused FTS+vector search:
  fts_matches CTE + vec_matches CTE (+ optional graph_matches VALUES CTE from
  a personalized-PageRank graph signal), UNION ALL + GROUP BY SUM,
  score = sum(weight_i / (rrf_k + rank_i)), weights as bind params.
Falls back to FTS-only when the corpus has no vec table, the sqlite-vec
extension is unavailable, or the embedding model can't load.

Corpora: people, entities, emails, observations, episodes, learnings, faqs,
docs, doc_chunks, actions, discord.

This module is the backend of query.py search and the ops_find / ops_cross
MCP tools (core/mcp_server.py).
"""
from __future__ import annotations

import re
import sqlite3
import struct
import sys
import time
from pathlib import Path

try:
    import paths  # repo path resolver (core/paths.py); on sys.path via the installer's .pth
except ImportError:
    # Direct-invocation fallback: walk up from this file to the repo root and
    # put core/ on sys.path (the installed venv normally does this via a .pth).
    sys.path.insert(0, str(next(
        p / "core" for p in Path(__file__).resolve().parents
        if (p / "core" / "paths.py").is_file()
    )))
    import paths
import _db  # unified connector (busy_timeout + FK ON)

DB = str(paths.DB_PATH)
VEC_DB = str(paths.VEC_DB_PATH)
MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMS = 384
RRF_K = 20          # Lowered from the textbook 60 after retrieval benchmarks:
                    # 20 recovered multi-hop MRR dips with no hit@3 regression
                    # on any (engine, category) pair. Re-sweep on your own data
                    # via set_runtime_params before changing it.
POOL = 100          # per-signal candidate pool (over-fetch for fusion)
W_FTS, W_VEC, W_GRAPH = 1.0, 1.0, 0.8   # fusion weights; resolve at CALL time
                    # in rrf_search (see set_runtime_params), so per-call
                    # overrides and process-wide sweeps both work

sys.path.insert(0, str(Path(__file__).resolve().parent))

STOPWORDS = frozenset({
    'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of',
    'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been', 'has',
    'have', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should',
    'may', 'might', 'can', 'who', 'what', 'which', 'that', 'this', 'our',
    'we', 'us', 'me', 'my', 'you', 'your', 'they', 'them', 'their', 'it',
    'its', 'as', 'so', 'if', 'then', 'than', 'about', 'into', 'over', 'under',
})

# Boolean role-flag columns on the people table rendered as [tag] labels in
# people results. CUSTOMIZE to the flag columns your schema tracks (keep in
# step with hybrid_search.ROLE_KEYWORDS); every column listed here must exist
# on the people table. Example additional entries:
#     ("is_reviewer", "reviewer"), ("is_speaker", "speaker"),
PEOPLE_ROLE_FLAGS: tuple[tuple[str, str], ...] = (
    ("is_mentor", "mentor"),
    ("is_organizer", "organizer"),
)
_PEOPLE_FLAG_COLS = "".join(f", b.{col}" for col, _ in PEOPLE_ROLE_FLAGS)


def _people_tags(r: dict) -> str:
    tags = [label for col, label in PEOPLE_ROLE_FLAGS if r.get(col)]
    return f" [{', '.join(tags)}]" if tags else ""


# corpus -> registry row. vec=None means FTS-only (fallback path).
# id_expr: SQL expression on base alias b that equals the graph entity id
# (enables the PPR signal); None = no graph signal for that corpus.
CORPORA: dict[str, dict] = {
    "people": {
        "base": "people", "fts": "people_fts", "vec": "vec_people",
        "where": "COALESCE(b.is_real_person,0)=1 AND b.merged_into IS NULL",
        "cols": "b.id, b.name, b.email, b.headline AS affiliation, b.location" + _PEOPLE_FLAG_COLS,
        "id_expr": "'person-'||b.id",
        "fmt": lambda r: f"[person] {r['name']}"
               + _people_tags(r)
               + f" | {r['email'] or ''} | {r['affiliation'] or ''} | {r['location'] or ''}",
    },
    "entities": {
        "base": "entities", "fts": "entities_fts", "vec": "vec_entities",
        "where": "b.status NOT IN ('merged','archived','junk')",
        "cols": "b.id, b.name, b.type",
        "id_expr": "b.id",
        "fmt": lambda r: f"[entity:{r['type']}] {r['name']} ({r['id']})",
    },
    "emails": {
        "base": "emails", "fts": "emails_fts", "vec": "vec_emails",
        "where": "1=1",
        "cols": "b.id, b.sender_name, b.sender_email, b.subject, b.timestamp, b.is_outgoing",
        "id_expr": None,
        "fmt": lambda r: f"[email:{'OUT' if r['is_outgoing'] else 'IN'}] {(r['timestamp'] or '')[:16]} | "
                         f"{r['sender_name'] or r['sender_email'] or '?'} | {(r['subject'] or '')[:80]}",
    },
    "observations": {
        "base": "observations", "fts": "observations_fts", "vec": "vec_observations",
        "where": "1=1",
        "cols": "b.id, b.subject, b.person_id, b.content",
        "id_expr": None,
        "fmt": lambda r: f"[obs] {(r['subject'] or '')[:40]} | {(r['content'] or '')[:120]}",
    },
    "episodes": {
        "base": "episodes", "fts": "episodes_fts", "vec": "vec_episodes",
        "where": "1=1",
        "cols": "b.id, b.kind, b.ts, b.topic, b.summary",
        "id_expr": None,
        "fmt": lambda r: f"[episode:{r['kind']}] {(r['ts'] or '')[:16]} | {(r['topic'] or '')[:50]} | {(r['summary'] or '')[:90]}",
    },
    "learnings": {
        "base": "learnings", "fts": "learnings_fts", "vec": "vec_learnings",
        "where": "b.status = 'active'",
        "cols": "b.id, b.title, b.description",
        "id_expr": None,
        "fmt": lambda r: f"[learning #{r['id']}] {r['title'] or '(no title)'}: {(r['description'] or '')[:150]}",
    },
    "faqs": {
        "base": "faqs", "fts": "faqs_fts", "vec": "vec_faqs",
        "where": "b.status = 'approved'",
        "cols": "b.faq_id, b.question_canonical, b.answer_canonical",
        "id_expr": None,
        "fmt": lambda r: f"[faq {r['faq_id']}] {r['question_canonical']} :: {(r['answer_canonical'] or '')[:120]}",
    },
    "docs": {
        "base": "reference_docs", "fts": "reference_docs_fts", "vec": None,  # chunk-level vec lives in doc_chunks
        "where": "b.doc_type IS NOT 'archive'",
        "cols": "b.slug, b.title, b.category",
        "id_expr": None,
        "fmt": lambda r: f"[doc:{r['category']}] {r['title'] or r['slug']} ({r['slug']})",
    },
    "doc_chunks": {
        "base": "reference_doc_chunks", "fts": "reference_doc_chunks_fts", "vec": "vec_reference_doc_chunks",
        "where": "1=1",
        "cols": "b.doc_slug, b.heading, b.content",
        "id_expr": None,
        "fmt": lambda r: f"[chunk:{r['doc_slug']}] {(r['heading'] or '')[:40]} | {(r['content'] or '')[:110]}",
    },
    "actions": {
        "base": "action_items", "fts": "action_items_fts", "vec": "vec_action_items",
        "where": "1=1",
        "cols": "b.id, b.status, b.priority, b.description, b.context_slug, b.waiting_on",
        "id_expr": None,
        "fmt": lambda r: f"[action:{r['status']}:{r['priority']}] {(r['description'] or '')[:120]}"
                         + (f" ({r['context_slug']})" if r["context_slug"] else "")
                         + (f" [WAITING: {r['waiting_on']}]" if r["waiting_on"] else ""),
    },
    "discord": {
        # NB: the FTS mirror is discord_messages_fts. A legacy 'discord_fts'
        # name floated around older callers and never existed, which left
        # their discord sections silently dead. Use the real name.
        "base": "discord_messages", "fts": "discord_messages_fts", "vec": None,
        "where": "1=1",
        "cols": "b.id, b.content, b.timestamp, b.author_id",
        "id_expr": None,
        "fmt": lambda r: f"[discord] {(r['timestamp'] or '')[:16]} | {(r['content'] or '')[:100]}",
    },
}

_model = None
_model_failed = False
_tls = __import__("threading").local()
_embed_cache: dict[str, bytes | None] = {}
_pool_singleton = None


def _pool():
    """Persistent executor: worker threads keep their thread-local connections
    (ATTACH + extension load are per-connection costs worth amortizing)."""
    global _pool_singleton
    if _pool_singleton is None:
        from concurrent.futures import ThreadPoolExecutor
        _pool_singleton = ThreadPoolExecutor(max_workers=6,
                                             thread_name_prefix="rrf")
    return _pool_singleton


def _get_conn() -> tuple[sqlite3.Connection, bool]:
    """Thread-local connection with vec.db attached when sqlite-vec loads.
    Thread-local so search_all can fan corpora out across a thread pool
    (sqlite releases the GIL during KNN scans)."""
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        return conn, _tls.vec_ok
    db = _db.connect(DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    db.row_factory = sqlite3.Row
    try:
        import sqlite_vec
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        db.execute(f"ATTACH DATABASE '{VEC_DB}' AS vecdb")
        _tls.vec_ok = True
    except Exception:
        _tls.vec_ok = False  # FTS-only fallback
    _tls.conn = db
    return db, _tls.vec_ok


def _embed(query: str):
    """Query embedding, memoized per query string (search_all hits every corpus
    with the same query; encoding costs ~150ms)."""
    global _model, _model_failed
    if query in _embed_cache:
        return _embed_cache[query]
    if _model_failed:
        return None
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(MODEL_NAME)
        except Exception:
            _model_failed = True
            return None
    emb = _model.encode(query, normalize_embeddings=True)
    blob = struct.pack(f"{DIMS}f", *emb.tolist())
    if len(_embed_cache) > 256:
        _embed_cache.clear()
    _embed_cache[query] = blob
    return blob


def fts_expr(query: str) -> str | None:
    """NL query -> safe FTS5 OR-of-prefix expression."""
    tokens = [w for w in re.findall(r"[A-Za-z0-9@.\-]+", query.lower())
              if len(w) >= 2 and w not in STOPWORDS]
    if not tokens:
        return None
    return " OR ".join(f'"{t}"*' for t in tokens[:24])


def _vec_has(db: sqlite3.Connection, vec_table: str) -> bool:
    try:
        return bool(db.execute(
            "SELECT 1 FROM vecdb.sqlite_master WHERE name=?", (vec_table,)).fetchone())
    except sqlite3.Error:
        return False


def rrf_search(corpus: str, query: str, k: int = 10,
               graph_ranks: dict[str, int] | None = None,
               w_fts: float | None = None, w_vec: float | None = None,
               w_graph: float | None = None) -> list[dict]:
    """Fused search over one corpus. Returns rows (dicts incl. rrf_score, _corpus).

    Weights resolve at CALL time. They used to bind as def-time defaults, so a
    sweep that patched the module constants swept nothing; keep it this way."""
    w_fts = W_FTS if w_fts is None else w_fts
    w_vec = W_VEC if w_vec is None else w_vec
    w_graph = W_GRAPH if w_graph is None else w_graph
    spec = CORPORA[corpus]
    db, vec_ok = _get_conn()
    fq = fts_expr(query)
    emb = _embed(query) if (vec_ok and spec["vec"] and _vec_has(db, spec["vec"])) else None

    branches, params = [], {}
    ctes = []
    if fq:
        ctes.append(f"""fts_matches AS (
            SELECT f.rowid AS rid, ROW_NUMBER() OVER (ORDER BY f.rank) AS r
            FROM {spec['fts']} f WHERE {spec['fts']} MATCH :fq LIMIT :pool)""")
        branches.append("SELECT rid, :w_fts / (:rrf_k + r) AS w FROM fts_matches")
        params["fq"] = fq
        params["w_fts"] = w_fts
    if emb is not None:
        ctes.append(f"""vec_matches AS (
            SELECT v.rowid AS rid, ROW_NUMBER() OVER (ORDER BY v.distance) AS r
            FROM vecdb.{spec['vec']} v WHERE v.embedding MATCH :emb AND k = :pool)""")
        branches.append("SELECT rid, :w_vec / (:rrf_k + r) AS w FROM vec_matches")
        params["emb"] = emb
        params["w_vec"] = w_vec
    if graph_ranks and spec["id_expr"]:
        pairs = list(graph_ranks.items())[:POOL]
        values = ",".join(f"(:g{i},{int(rank)})" for i, (_, rank) in enumerate(pairs))
        for i, (eid, _) in enumerate(pairs):
            params[f"g{i}"] = eid
        ctes.append(f"""graph_matches AS (
            SELECT b.rowid AS rid, v.column2 AS r
            FROM (VALUES {values}) v JOIN {spec['base']} b ON {spec['id_expr']} = v.column1)""")
        branches.append("SELECT rid, :w_graph / (:rrf_k + r) AS w FROM graph_matches")
        params["w_graph"] = w_graph
    if not branches:
        return []

    sql = f"""WITH {', '.join(ctes)},
        fused AS (SELECT rid, SUM(w) AS score FROM ({' UNION ALL '.join(branches)}) GROUP BY rid)
        SELECT {spec['cols']}, fu.score AS rrf_score
        FROM fused fu JOIN {spec['base']} b ON b.rowid = fu.rid
        WHERE {spec['where']}
        ORDER BY fu.score DESC LIMIT :k"""
    params.update({"pool": POOL, "rrf_k": RRF_K, "k": k})
    rows = []
    for attempt in range(3):
        try:
            rows = db.execute(sql, params).fetchall()
            break
        except sqlite3.OperationalError as ex:
            if "no such table" in str(ex) and attempt == 0 and fq and emb is None:
                return []  # corpus fts missing entirely
            raise
        except sqlite3.DatabaseError:
            if attempt == 2:
                raise
            time.sleep(0.4)  # transient under concurrent FTS write bursts
    out = []
    for r in rows:
        d = dict(r)
        d["_corpus"] = corpus
        out.append(d)
    return out


def set_runtime_params(rrf_k: int | None = None, w_fts: float | None = None,
                       w_vec: float | None = None, w_graph: float | None = None) -> dict:
    """Documented runtime override for sweeps/benches. All four resolve at
    CALL time inside rrf_search, so this affects every engine that routes
    through this module in-process (query.py search, ops_find). Returns the
    effective values. NOTE: process-wide; production callers should pass
    per-call weights to search_all/rrf_search instead."""
    global RRF_K, W_FTS, W_VEC, W_GRAPH
    if rrf_k is not None:
        RRF_K = int(rrf_k)
    if w_fts is not None:
        W_FTS = float(w_fts)
    if w_vec is not None:
        W_VEC = float(w_vec)
    if w_graph is not None:
        W_GRAPH = float(w_graph)
    return {"rrf_k": RRF_K, "w_fts": W_FTS, "w_vec": W_VEC, "w_graph": W_GRAPH}


def format_row(row: dict) -> str:
    return CORPORA[row["_corpus"]]["fmt"](row)


def search_all(query: str, k: int = 10,
               corpora: tuple = ("people", "entities", "emails", "observations",
                                 "episodes", "learnings", "faqs", "docs", "actions"),
               use_graph: bool = True,
               w_fts: float | None = None, w_vec: float | None = None,
               w_graph: float | None = None) -> dict[str, list[dict]]:
    """Multi-corpus fused search; PPR graph signal feeds people+entities.
    Backend of query.py search. Corpora fan out across a persistent thread
    pool (per-thread connections survive across calls; sqlite releases the
    GIL during scans), PPR computed concurrently and joined by the
    graph-aware corpora only."""
    ex = _pool()
    _embed(query)  # encode once up front so pool threads share the cache

    def _ppr():
        # Optional graph-ranking module (personalized PageRank over the edges
        # table). Not part of this starter kit; if absent, the graph signal
        # is skipped and search degrades gracefully to FTS + vector.
        try:
            import graph_rank
            return graph_rank.ppr_ranks(query, pool=POOL)
        except Exception:
            return None

    results: dict[str, list[dict]] = {}
    ppr_fut = ex.submit(_ppr) if use_graph else None
    graph_aware = [c for c in corpora if CORPORA[c]["id_expr"]]
    plain = [c for c in corpora if not CORPORA[c]["id_expr"]]

    def run(c, granks):
        try:
            return c, rrf_search(c, query, k=k, graph_ranks=granks,
                                 w_fts=w_fts, w_vec=w_vec, w_graph=w_graph)
        except sqlite3.Error:
            return c, []

    plain_futs = [ex.submit(run, c, None) for c in plain]
    graph_ranks = ppr_fut.result() if ppr_fut else None
    graph_futs = [ex.submit(run, c, graph_ranks) for c in graph_aware]
    for f in plain_futs + graph_futs:
        c, rows = f.result()
        results[c] = rows
    return {c: results.get(c, []) for c in corpora}


def search_all_ranked(query: str, k: int = 10, **kw) -> list[dict]:
    """Flat interleaved view: corpora ordered by their strongest hit's score
    (the corpus that answers best leads), round-robin within. This is what
    query.py search prints."""
    per = search_all(query, k=k, **kw)
    order = sorted((c for c in per),
                   key=lambda c: per[c][0]["rrf_score"] if per[c] else -1.0,
                   reverse=True)
    flat, idx = [], 0
    while len(flat) < k * len(order):
        added = False
        for c in order:
            rows = per.get(c, [])
            if idx < len(rows):
                flat.append(rows[idx])
                added = True
        if not added:
            break
        idx += 1
    return flat


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "genomics mentor"
    t0 = time.perf_counter()
    per = search_all(q, k=5)
    ms = (time.perf_counter() - t0) * 1000
    print(f"search_all('{q}') in {ms:.0f}ms")
    for c, rows in per.items():
        if not rows:
            continue
        print(f"\n-- {c} --")
        for r in rows:
            print(f"  {r['rrf_score']:.5f}  {format_row(r)[:140]}")
