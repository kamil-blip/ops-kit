"""Hybrid search: vector + FTS5 + graph + recency + role for people discovery.

Usage:
    python hybrid_search.py "AI safety researcher"
    python hybrid_search.py "biosecurity expert Boston" --k 20
    python hybrid_search.py "mentor for a genomics workshop" --verbose
    python hybrid_search.py "ML safety at Acme AI" --explain
    python hybrid_search.py "someone who can review genomics projects" --no-expand --explain

Four retrieval sources: vector similarity, FTS5 text match, graph
retrieval (backward edge walk from query-matching entities), cross-table
retrieval (emails, chat messages). Plus a query-dependent role signal
(boosts people whose role flags match role words in the query; the
keyword -> column mapping is configurable via ROLE_KEYWORDS below).
Scored via Reciprocal Rank Fusion
(RRF): each signal produces a ranked list, combined as
  RRF(d) = sum of weight_i / (K + rank_i(d))
This is rank-based, so incomparable score scales across signals don't matter.

Query expansion (via Gemini Flash) extracts key domain terms and synonyms
from natural language queries before searching. Disable with --no-expand.

Requires: sqlite-vec, sentence-transformers (BAAI/bge-small-en-v1.5).
Cross-encoder rerank (--rerank, default on) needs cross-encoder/ms-marco-MiniLM-L-6-v2.
"""
import json
import math
import re
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

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

# Strip zero-width Unicode characters that cause cp1252 encoding crashes on Windows
_ZW_RE = re.compile(r'[\u200b\u200c\u200d\u200e\u200f\ufeff]')

DB = str(paths.DB_PATH)
VEC_DB = str(getattr(paths, "VEC_DB_PATH", Path(paths.DB_PATH).parent / "vec.db"))
MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMS = 384

# Keyring service name for the optional Gemini API key lookup (query
# expansion). Env vars GEMINI_API_KEY / GOOGLE_API_KEY take precedence.
KEYRING_SERVICE = "ops-kit"

# Cross-encoder reranker model. Pulled from HF on first use, cached locally.
RERANK_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RERANK_CANDIDATES = 30  # Rerank top-N RRF survivors before returning final k.

# RRF weights (used as multipliers in weighted RRF, not raw score weights).
# Semantic boosted to 0.40, recency reduced to 0.10 (recency was
# over-promoting recent unrelated contacts over domain experts).
W_SEMANTIC = 0.40
W_FTS = 0.30
W_RECENCY = 0.10
W_GRAPH = 0.10
W_ROLE = 0.10  # Query-dependent: only active when the query mentions a configured role word.
RRF_K = 60  # Standard RRF constant (Cormack et al.)

# Abstention: relative floor (30% of best RRF score, dynamic per query)
ABSTENTION_RATIO = 0.3

# Relation type weights for graph scoring. CUSTOMIZE to match the relation
# vocabulary in your edges table; unknown relations default to 1.0. Weight
# above 1.0 the relations that most strongly indicate expertise.
RELATION_WEIGHTS = {
    'works_at': 1.0,
    'researches': 1.5,
    'collaborated_with': 1.0,
    'organized': 1.0,
    'emailed': 1.0,
    'mentioned_in': 0.5,
    'participated_in': 0.5,
}

# Query keyword -> boolean role-flag column on the people table. CUSTOMIZE to
# the role flags your people table tracks; columns that don't exist in your
# schema are feature-detected and skipped. Example additional entries:
#     'reviewer': 'is_reviewer', 'speaker': 'is_speaker',
ROLE_KEYWORDS = {
    'mentor': 'is_mentor', 'mentors': 'is_mentor',
    'organizer': 'is_organizer', 'organizers': 'is_organizer',
}

# Non-person entity types considered during graph retrieval. CUSTOMIZE to
# match the type vocabulary in your entities table.
NON_PERSON_ENTITY_TYPES = ('org', 'topic', 'event')

# Recency half-life in days
RECENCY_HALF_LIFE = 30.0

# Stopwords for graph retrieval token extraction
STOPWORDS = frozenset({
    'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been',
    'has', 'have', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'can', 'who', 'what', 'which', 'that', 'this',
    'these', 'those', 'not', 'all', 'any', 'some', 'no', 'nor', 'so', 'very',
    'too', 'also', 'just',
})

# --- Query expansion via Gemini Flash ---

@dataclass
class ExpandedQuery:
    original: str
    key_terms: List[str] = field(default_factory=list)
    synonyms: List[str] = field(default_factory=list)
    intent: str = ""
    expanded_text: str = ""
    fts_query: str = ""

# Session-level cache for expansion results (avoids repeated API calls)
_expand_cache: dict = {}

EXPAND_PROMPT = """Extract structured search intent from this people-search query. Return ONLY valid JSON, no markdown.

Query: "{query}"

Return JSON with:
- "key_terms": the 1-3 most important domain-specific terms (nouns, fields, technologies, org names). Drop generic words like "someone", "person", "expert", "researcher", "review", "projects".
- "synonyms": 3-8 related terms/phrases that experts in this area might have in their profile (alternative names for the field, sub-disciplines, related technologies, acronyms).
- "intent": one word describing what kind of person is sought (e.g. "expert", "reviewer", "mentor", "speaker", "collaborator", "advisor").

Example for "someone who can review genomics projects":
{{"key_terms": ["genomics"], "synonyms": ["bioinformatics", "computational biology", "gene sequencing", "genome analysis", "NGS", "transcriptomics"], "intent": "reviewer"}}

Example for "ML safety researcher at Acme AI":
{{"key_terms": ["ML safety", "Acme AI"], "synonyms": ["machine learning safety", "AI alignment", "AI safety", "RLHF", "interpretability"], "intent": "researcher"}}"""


def query_expand(query: str, verbose: bool = False) -> ExpandedQuery:
    """Use Gemini Flash to extract key terms and synonyms from a natural language query.

    Falls back silently to the original query if Gemini is unavailable or fails.
    Results are cached per query string for the session.
    """
    # Check cache first
    if query in _expand_cache:
        if verbose:
            print(f"\nQuery expansion (cached): {query}")
        return _expand_cache[query]

    # Build a no-expansion fallback
    fallback = _build_fallback(query)

    try:
        import os
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        if not api_key:
            try:
                import keyring as kr
                api_key = (kr.get_password(KEYRING_SERVICE, "GEMINI_API_KEY")
                           or kr.get_password(KEYRING_SERVICE, "google_ai_api_key")
                           or "")
            except Exception:
                pass
        if not api_key:
            if verbose:
                print("\nQuery expansion: no Gemini API key found, skipping")
            _expand_cache[query] = fallback
            return fallback

        from google import genai as genai_client
        client = genai_client.Client(
            api_key=api_key,
            http_options={"timeout": 15000},  # 15s timeout in ms (API minimum is 10s)
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=EXPAND_PROMPT.format(query=query),
            config=genai_client.types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=1024,
                thinking_config=genai_client.types.ThinkingConfig(thinking_budget=0),
            ),
        )

        # Parse JSON from response
        text = response.text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        data = json.loads(text)

        key_terms = data.get("key_terms", [])
        synonyms = data.get("synonyms", [])
        intent = data.get("intent", "")

        # Build expanded embedding text: key terms weighted by repetition + synonyms + intent
        parts = []
        for t in key_terms:
            parts.extend([t] * 3)  # repeat key terms for embedding weight
        parts.extend(synonyms)
        if intent:
            parts.append(intent)
        expanded_text = " ".join(parts) if parts else query

        # Build FTS query: key_terms boosted (exact phrase), synonyms as OR alternatives
        fts_parts = []
        for t in key_terms:
            clean = ''.join(c for c in t if c.isalnum() or c == ' ')
            if clean:
                fts_parts.append(f'"{clean}"')
        for s in synonyms:
            clean = ''.join(c for c in s if c.isalnum() or c == ' ')
            if clean:
                fts_parts.append(f'"{clean}"')
        # Also include original tokens that aren't stopwords
        raw_tokens = [w for w in query.lower().split() if len(w) >= 3 and w not in STOPWORDS]
        for t in raw_tokens:
            cleaned = ''.join(c for c in t if c.isalnum())
            if len(cleaned) >= 3:
                fts_parts.append(f'"{cleaned}"')
        # Deduplicate while preserving order
        seen = set()
        unique_fts = []
        for p in fts_parts:
            low = p.lower()
            if low not in seen:
                seen.add(low)
                unique_fts.append(p)
        fts_query = " OR ".join(unique_fts) if unique_fts else ""

        result = ExpandedQuery(
            original=query,
            key_terms=key_terms,
            synonyms=synonyms,
            intent=intent,
            expanded_text=expanded_text,
            fts_query=fts_query,
        )

        if verbose:
            print(f"\nQuery expansion:")
            print(f"  key_terms: {key_terms}")
            print(f"  synonyms: {synonyms}")
            print(f"  intent: {intent}")
            print(f"  FTS query: {fts_query[:120]}{'...' if len(fts_query) > 120 else ''}")

        _expand_cache[query] = result
        return result

    except Exception as e:
        if verbose:
            print(f"\nQuery expansion failed ({type(e).__name__}: {e}), using original query")
        _expand_cache[query] = fallback
        return fallback


def _build_fallback(query: str) -> ExpandedQuery:
    """Build a no-expansion ExpandedQuery from the raw query (used as fallback)."""
    raw_tokens = [w for w in query.lower().split() if len(w) >= 3 and w not in STOPWORDS]
    clean_tokens = []
    for t in raw_tokens:
        cleaned = ''.join(c for c in t if c.isalnum())
        if len(cleaned) >= 3:
            clean_tokens.append(f'"{cleaned}"')
    fts_query = " OR ".join(clean_tokens) if clean_tokens else ""
    return ExpandedQuery(
        original=query,
        expanded_text=query,
        fts_query=fts_query,
    )


def get_db():
    import sqlite_vec
    db = _db.connect(DB, timeout=30)
    db.execute("PRAGMA busy_timeout = 30000")
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    # 2-file layout: vec_* embedding tables live in data/vec.db, attached as vecdb.
    if not any(r[1] == 'vecdb' for r in db.execute('PRAGMA database_list')):
        db.execute("ATTACH DATABASE '%s' AS vecdb" % VEC_DB)
    db.enable_load_extension(False)
    return db


def _table_exists(db, name):
    """Feature-detect an optional table/view/FTS index so a retrieval source
    can degrade gracefully when the mirror isn't installed (e.g. discord_fts)."""
    try:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (name,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _people_role_columns(db):
    """Role-flag columns from ROLE_KEYWORDS that actually exist on people.

    Feature-detected so an operator can map keywords to columns their own
    schema doesn't have (yet) without breaking the candidate query."""
    try:
        existing = {r[1] for r in db.execute("PRAGMA table_info(people)")}
    except sqlite3.Error:
        return []
    return sorted({c for c in ROLE_KEYWORDS.values() if c in existing})


def serialize_vec(vec):
    return struct.pack(f"{DIMS}f", *vec.tolist())


def recency_score(last_interaction_date):
    """Exponential decay with 30-day half-life. Returns 0.0-1.0."""
    if not last_interaction_date:
        return 0.0
    try:
        if isinstance(last_interaction_date, str):
            # Handle various date formats
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(last_interaction_date[:26], fmt)
                    break
                except ValueError:
                    continue
            else:
                return 0.0
        else:
            dt = last_interaction_date
        days_ago = (datetime.now() - dt).total_seconds() / 86400
        return math.exp(-math.log(2) * days_ago / RECENCY_HALF_LIFE)
    except Exception:
        return 0.0


def _entity_query_relevance(entity_name, entity_data, query_tokens):
    """Score how relevant an entity is to the query. Returns 0.0, 0.5, or 1.0."""
    name_lower = (entity_name or "").lower()
    name_words = set(name_lower.split())

    # Exact token match (FTS-like) -> 1.0
    if query_tokens & name_words:
        return 1.0

    # Substring match in name or data -> 0.5
    data_lower = (entity_data or "").lower()
    searchable = name_lower + " " + data_lower
    for qt in query_tokens:
        if qt in searchable:
            return 0.5
        for nw in name_words:
            if len(nw) >= 3 and (nw in qt or qt in nw):
                return 0.5

    return 0.0


def graph_scores(db, person_ids, query):
    """Compute graph-based relevance for a batch of people.

    Single JOIN query over edges + entities. Returns (scores_dict, edges_dict).
    scores_dict: {person_id: 0.0-1.0}
    edges_dict: {person_id: [(relation, entity_name, relevance)]}
    """
    if not person_ids:
        return {}, {}

    # Map integer people.id -> edge source_id format
    edge_map = {f"person-{pid}": pid for pid in person_ids}
    placeholders = ",".join("?" * len(edge_map))

    rows = db.execute(f"""
        SELECT e.source_id, e.relation, e.confidence, ent.name, ent.data
        FROM edges e
        JOIN entities ent ON e.target_id = ent.id
        WHERE e.source_id IN ({placeholders})
        AND e.valid_until IS NULL
    """, list(edge_map.keys())).fetchall()

    query_tokens = set(w for w in query.lower().split() if len(w) >= 3)
    raw_scores = {}   # {pid: float}
    edge_details = {} # {pid: [(relation, entity_name, relevance)]}

    for row in rows:
        pid = edge_map.get(row["source_id"])
        if pid is None:
            continue

        relevance = _entity_query_relevance(row["name"], row["data"], query_tokens)
        if relevance == 0.0:
            continue

        confidence = row["confidence"] if row["confidence"] is not None else 1.0
        rel_weight = RELATION_WEIGHTS.get(row["relation"], 1.0)
        raw_scores[pid] = raw_scores.get(pid, 0.0) + relevance * rel_weight * confidence

        if pid not in edge_details:
            edge_details[pid] = []
        edge_details[pid].append((row["relation"], row["name"], relevance))

    # Normalize to 0.0-1.0 by max across candidates
    if raw_scores:
        max_s = max(raw_scores.values())
        if max_s > 0:
            raw_scores = {pid: s / max_s for pid, s in raw_scores.items()}

    return raw_scores, edge_details


def graph_retrieve(db, query, verbose=False, query_embedding=None):
    """Retrieve person IDs by walking edges backward from query-matching entities.

    1. Extract query tokens (3+ chars, skip stopwords)
    2. Find non-person entities matching tokens OR semantically similar via vec_entities
    3. Walk edges backward: who has edges pointing at those entities?
    4. Return set of person IDs (integers) to add to the candidate pool

    Uses both token matching (small non-person entity scan) and vector
    similarity (vec_entities).
    """
    tokens = set(w for w in query.lower().split() if len(w) >= 3 and w not in STOPWORDS)
    if not tokens and query_embedding is None:
        return set()

    type_placeholders = ",".join("?" * len(NON_PERSON_ENTITY_TYPES))

    # Token-based matching: scan non-person entities
    entities = db.execute(f"""
        SELECT id, name, data FROM entities
        WHERE type IN ({type_placeholders})
    """, list(NON_PERSON_ENTITY_TYPES)).fetchall()

    matching_ids = []
    matched_names = []
    for ent in entities:
        relevance = _entity_query_relevance(ent["name"], ent["data"], tokens)
        if relevance > 0:
            matching_ids.append(ent["id"])
            matched_names.append((ent["name"], relevance))

    # Vector-based matching: find semantically similar entities via vec_entities
    if query_embedding is not None:
        try:
            vec_bytes = serialize_vec(query_embedding)
            vec_ents = db.execute(f"""
                SELECT ve.rowid, ve.distance, e.id, e.name, e.type
                FROM vec_entities ve
                JOIN entities e ON ve.rowid = e.rowid
                WHERE ve.embedding MATCH ?
                AND k = 20
                AND e.type IN ({type_placeholders})
                ORDER BY ve.distance
            """, [vec_bytes] + list(NON_PERSON_ENTITY_TYPES)).fetchall()

            for ve in vec_ents:
                sim = max(0, 1.0 - ve["distance"])
                if sim >= 0.45 and ve["id"] not in matching_ids:
                    matching_ids.append(ve["id"])
                    matched_names.append((ve["name"], sim))
        except Exception:
            pass  # vec_entities may not exist in all environments

    if not matching_ids:
        if verbose:
            print(f"\nGraph retrieval: {len(tokens)} tokens -> 0 entities matched")
        return set()

    # Walk edges backward: find people pointing at matched entities
    placeholders = ",".join("?" * len(matching_ids))
    rows = db.execute(f"""
        SELECT DISTINCT source_id FROM edges
        WHERE target_id IN ({placeholders})
        AND valid_until IS NULL
        AND source_id LIKE 'person-%'
    """, matching_ids).fetchall()

    person_ids = set()
    for row in rows:
        try:
            pid = int(row["source_id"].split("-", 1)[1])
            person_ids.add(pid)
        except (ValueError, IndexError):
            continue

    if verbose:
        print(f"\nGraph retrieval: {len(tokens)} tokens -> "
              f"{len(matching_ids)} entities -> {len(person_ids)} people")
        for name, rel in matched_names[:10]:
            print(f"  entity: {name} (relevance: {rel})")

    return person_ids


def cross_table_retrieve(db, query, verbose=False):
    """Find people through activity: emails sent, chat messages posted.

    Searches content across related tables via FTS5, traces back to
    person_id. Complements vector/FTS (profile search) and graph retrieval
    (entity edges) by finding people through what they *did*, not who they *are*.
    """
    tokens = set(w for w in query.lower().split() if len(w) >= 3 and w not in STOPWORDS)
    if not tokens:
        return set()

    # Sanitize tokens for FTS5 (alphanumeric only, re-filter length)
    fts_tokens = set()
    for t in tokens:
        clean = ''.join(c for c in t if c.isalnum())
        if len(clean) >= 3:
            fts_tokens.add(clean)

    person_ids = set()
    source_counts = {"email": 0, "discord": 0}

    # 1. Emails: people who sent/received emails mentioning query terms
    if fts_tokens:
        fts_query = " OR ".join(f'"{t}"' for t in fts_tokens)
        try:
            rows = db.execute("""
                SELECT DISTINCT e.person_id
                FROM emails_fts f
                JOIN emails e ON e.rowid = f.rowid
                WHERE emails_fts MATCH ?
                AND e.person_id IS NOT NULL
                ORDER BY f.rank
                LIMIT 30
            """, (fts_query,)).fetchall()
            for r in rows:
                person_ids.add(r["person_id"])
                source_counts["email"] += 1
        except Exception:
            pass

        # 2. Discord: people who discussed query terms. The FTS mirror is
        # optional -- feature-detect and skip when not installed. Canonical
        # name is discord_messages_fts; 'discord_fts' is a legacy alias some
        # deployments used.
        discord_fts = next((t for t in ("discord_messages_fts", "discord_fts")
                            if _table_exists(db, t)), None)
        if discord_fts:
            try:
                rows = db.execute(f"""
                    SELECT DISTINCT d.person_id
                    FROM {discord_fts} f
                    JOIN discord_messages d ON d.rowid = f.rowid
                    WHERE {discord_fts} MATCH ?
                    AND d.person_id IS NOT NULL
                    ORDER BY f.rank
                    LIMIT 30
                """, (fts_query,)).fetchall()
                for r in rows:
                    person_ids.add(r["person_id"])
                    source_counts["discord"] += 1
            except Exception:
                pass

    if verbose:
        print(f"\nCross-table retrieval: {len(tokens)} tokens -> "
              f"{len(person_ids)} people "
              f"(email:{source_counts['email']}, "
              f"discord:{source_counts['discord']})")

    return person_ids


_rerank_model = None
def _get_reranker():
    """Lazy-load the cross-encoder; reuse across calls in the same process."""
    global _rerank_model
    if _rerank_model is None:
        from sentence_transformers import CrossEncoder
        _rerank_model = CrossEncoder(RERANK_MODEL_NAME)
    return _rerank_model


def _person_rerank_doc(person, summary=None):
    """Build the document text the cross-encoder scores against the query."""
    parts = []
    if person.get("name"):
        parts.append(person["name"])
    if person.get("headline"):
        parts.append(person["headline"])
    if person.get("location"):
        parts.append(person["location"])
    if summary:
        parts.append(summary[:600])  # cap; cross-encoder truncates at 512 tokens anyway
    return ". ".join(p for p in parts if p)


def hybrid_search(query, k=10, verbose=False, explain=False, expand=True, rerank=True):
    """Run hybrid search: vector + FTS5 + graph + cross-table + RRF scoring + cross-encoder rerank.

    Scoring uses Reciprocal Rank Fusion (RRF): each signal produces an
    independent ranked list, then RRF_score(d) = sum of weight_i / (K + rank_i).
    This is rank-based, so incomparable scales across signals don't matter.

    If rerank=True (default), the top RERANK_CANDIDATES RRF survivors are
    re-scored with a cross-encoder (cross-encoder/ms-marco-MiniLM-L-6-v2)
    against the query, and the final top-k is sorted by rerank score.
    The abstention floor still applies on the pre-rerank RRF score.

    If expand=True (default), uses Gemini Flash to extract key terms and
    synonyms from the query before searching. Disable with expand=False.
    """
    db = get_db()
    t0 = time.time()

    # 0. Query expansion (extract key terms + synonyms via Gemini Flash)
    expanded = query_expand(query, verbose=verbose) if expand else _build_fallback(query)

    # 1. Vector search (top 5*k candidates, capped at 200)
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    # Use expanded text for richer embedding when available
    embed_text = expanded.expanded_text if expanded.expanded_text else query
    emb = model.encode(embed_text, normalize_embeddings=True)
    vec_bytes = serialize_vec(emb)

    vec_k = min(k * 5, 200)  # Over-fetch for RRF (needs larger pools than weighted sum)
    fts_k = min(k * 5, 200)  # Separate FTS limit (cheap query, more candidates = better RRF)
    vec_results = db.execute("""
        SELECT vp.rowid as person_id, vp.distance
        FROM vec_people vp
        WHERE vp.embedding MATCH ?
        AND k = ?
        ORDER BY vp.distance
    """, (vec_bytes, vec_k)).fetchall()

    # Convert vector distances to 0-1 similarity scores
    # sqlite-vec returns cosine distance for normalized vectors; similarity = 1 - distance
    vec_scores = {}
    for r in vec_results:
        sim = max(0, 1.0 - r["distance"])
        vec_scores[r["person_id"]] = sim

    # 2. FTS5 search (top 5*k candidates, capped at 200)
    # Uses expanded FTS query (key_terms + synonyms + original tokens) when
    # expansion is active, otherwise falls back to OR-joined original tokens.
    fts_scores = {}
    try:
        fts_query = expanded.fts_query
        if fts_query:
            fts_results = db.execute("""
                SELECT rowid, rank
                FROM people_fts
                WHERE people_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, fts_k)).fetchall()

            if fts_results:
                # FTS rank is negative (more negative = better match)
                min_rank = min(r["rank"] for r in fts_results)
                max_rank = max(r["rank"] for r in fts_results)
                rank_range = max_rank - min_rank if max_rank != min_rank else 1.0
                for r in fts_results:
                    # Normalize: best rank -> 1.0, worst -> 0.0
                    fts_scores[r["rowid"]] = (max_rank - r["rank"]) / rank_range
    except Exception:
        pass  # FTS may fail on complex queries

    # 2b. Graph retrieval (backward edge walk from query-matching entities)
    # Pass query embedding so graph_retrieve can also use vec_entities similarity.
    # When expanded, build an enriched query string with key_terms + synonyms for
    # better entity matching.
    graph_query = query
    if expanded.key_terms:
        graph_query = query + " " + " ".join(expanded.key_terms + expanded.synonyms)
    graph_retrieved = graph_retrieve(db, graph_query, verbose=verbose, query_embedding=emb)

    # 2c. Cross-table retrieval (emails, chat messages)
    # Enrich with key_terms + synonyms for broader activity matching.
    cross_query = query
    if expanded.key_terms:
        cross_query = query + " " + " ".join(expanded.key_terms + expanded.synonyms[:3])
    cross_retrieved = cross_table_retrieve(db, cross_query, verbose=verbose)

    # 3. Merge candidate sets (vector + FTS + graph + cross-table)
    all_ids = (set(vec_scores.keys()) | set(fts_scores.keys())
               | graph_retrieved | cross_retrieved)

    if not all_ids:
        if verbose:
            print("No results found.")
        db.close()
        return []

    # 4. Fetch people details for all candidates. Role-flag columns are
    # feature-detected (only ROLE_KEYWORDS columns that exist are selected).
    # The name filters are data hygiene: exclude org/team records and
    # machine-generated entries misfiled as people.
    role_cols = _people_role_columns(db)
    role_select = "".join(f", p.{c}" for c in role_cols)
    placeholders = ",".join("?" * len(all_ids))
    people = db.execute(f"""
        SELECT p.id, p.name, p.email, p.headline AS affiliation, p.location, p.career_stage,
               p.interaction_count, p.headline, p.summary{role_select},
               COALESCE(p.last_contact_date, pe.last_seen, p.updated_at) as last_active
        FROM people p
        LEFT JOIN person_emails pe ON pe.person_id = p.id
        WHERE p.id IN ({placeholders})
        AND COALESCE(p.is_real_person, 0) = 1
        AND length(p.name) BETWEEN 4 AND 50
        AND p.name NOT LIKE '%, PBC'
        AND p.name NOT LIKE '% Team'
        AND p.name NOT GLOB '*[.]*[.]*'
        GROUP BY p.id
    """, list(all_ids)).fetchall()

    people_map = {p["id"]: p for p in people}

    # 5. Graph scoring (single batch query over edges + entities)
    valid_ids = [pid for pid in all_ids if pid in people_map]
    g_scores, g_edges = graph_scores(db, valid_ids, query)

    # 6. Collect raw signal values for all candidates
    raw = {}  # {pid: {signal: value}}
    for pid in all_ids:
        person = people_map.get(pid)
        if not person:
            continue
        raw[pid] = {
            'vec': vec_scores.get(pid, 0.0),
            'fts': fts_scores.get(pid, 0.0),
            'rec': recency_score(person["last_active"]),
            'graph': g_scores.get(pid, 0.0),
        }

    # 7. Build per-signal ranked lists for RRF (best first)
    # Built from source signal dicts, not the merged raw dict, so all
    # signal-found candidates are ranked (even worst-scoring FTS matches).
    _sort = lambda d: sorted(d.items(), key=lambda x: x[1], reverse=True)
    ranked = {
        'vec': _sort(vec_scores),
        'fts': _sort(fts_scores),
        'rec': _sort({pid: s['rec'] for pid, s in raw.items() if s['rec'] > 0}),
        'graph': _sort(g_scores),
    }

    # 7b. Role signal (query-dependent: active only when the query mentions a
    # configured role word AND the mapped column exists in this schema)
    query_lower = query.lower()
    present_role_cols = set(role_cols)
    active_role_cols = set()
    for kw, col in ROLE_KEYWORDS.items():
        if kw in query_lower and col in present_role_cols:
            active_role_cols.add(col)

    role_active = bool(active_role_cols)
    if role_active:
        matching = [(pid, 1.0) for pid in raw
                    if people_map.get(pid) and any(people_map[pid][c] for c in active_role_cols)]
        matching_pids = {m[0] for m in matching}
        non_matching = [(pid, 0.0) for pid in raw if pid not in matching_pids]
        ranked['role'] = matching + non_matching

    # Signal weights for RRF
    weights = {
        'vec': W_SEMANTIC,
        'fts': W_FTS,
        'rec': W_RECENCY,
        'graph': W_GRAPH,
    }
    if role_active:
        weights['role'] = W_ROLE
    else:
        weights['vec'] = W_SEMANTIC + W_ROLE  # redistribute to semantic

    # 8. Compute RRF scores: score(d) = sum of weight_i / (K + rank_i(d))
    rank_lookup = {}
    for signal, items in ranked.items():
        rank_lookup[signal] = {pid: i + 1 for i, (pid, _) in enumerate(items)}

    scored = []
    for pid, scores in raw.items():
        person = people_map.get(pid)
        if not person:
            continue

        rrf_total = 0.0
        rrf_detail = {}
        for signal, weight in weights.items():
            rl = rank_lookup.get(signal, {})
            rank = rl.get(pid)
            if rank is not None:
                contribution = weight / (RRF_K + rank)
            else:
                # Not found by this signal: zero contribution (standard RRF)
                contribution = 0.0
            rrf_total += contribution
            rrf_detail[signal] = (rank, contribution)

        scored.append({
            "id": pid,
            "name": _ZW_RE.sub('', person["name"] or ''),
            "email": person["email"],
            "affiliation": _ZW_RE.sub('', person["affiliation"] or ''),
            "location": _ZW_RE.sub('', person["location"] or ''),
            "career_stage": person["career_stage"],
            "headline": _ZW_RE.sub('', person["headline"] or ''),
            "summary": _ZW_RE.sub('', person["summary"] or ''),
            "role_flags": {c: person[c] for c in role_cols if person[c]},
            "interaction_count": person["interaction_count"],
            "score": rrf_total,
            "rrf_detail": rrf_detail,
            "s_vec": scores['vec'],
            "s_fts": scores['fts'],
            "s_rec": scores['rec'],
            "s_graph": scores['graph'],
            "s_rerank": None,
        })

    # 9. Sort and apply relative abstention floor (30% of best RRF score)
    scored.sort(key=lambda x: x["score"], reverse=True)
    if scored:
        floor = scored[0]["score"] * ABSTENTION_RATIO
        survivors = [r for r in scored if r["score"] >= floor]
    else:
        survivors = []

    # 9b. Cross-encoder rerank over top RERANK_CANDIDATES survivors.
    # The reranker reads name + headline + location + summary and produces a
    # query-document relevance score (BERT logit). Higher is better.
    # Final top-k is sorted by rerank score when rerank=True.
    rerank_pool = survivors[:RERANK_CANDIDATES]
    if rerank and rerank_pool:
        try:
            ce = _get_reranker()
            pairs = [(query, _person_rerank_doc(r, r.get("summary"))) for r in rerank_pool]
            ce_scores = ce.predict(pairs)
            for r, s in zip(rerank_pool, ce_scores):
                r["s_rerank"] = float(s)
            rerank_pool.sort(key=lambda x: x["s_rerank"], reverse=True)
        except Exception as e:
            if verbose or explain:
                print(f"(rerank skipped: {e})")
    results = rerank_pool[:k]

    elapsed = time.time() - t0

    # 10. Output
    if explain:
        rerank_note = "rerank ON" if rerank else "rerank OFF"
        print(f"\nQuery: '{query}' ({elapsed:.2f}s, {len(results)}/{len(scored)} candidates, {rerank_note})")
        if expanded.key_terms:
            print(f"Expansion: key={expanded.key_terms}, syn={expanded.synonyms}, intent={expanded.intent}")
        elif expand:
            print("Expansion: none (fallback to original tokens)")
        else:
            print("Expansion: disabled (--no-expand)")
        print(f"RRF K={RRF_K}, weights: " + ", ".join(f"{s}={w:.2f}" for s, w in weights.items()))
        if role_active:
            print(f"Role signal ACTIVE (detected: {', '.join(active_role_cols)})")
        print()
        for i, r in enumerate(results, 1):
            rerank_str = f", rerank: {r['s_rerank']:+.3f}" if r.get("s_rerank") is not None else ""
            print(f"#{i} {r['name']} (rrf: {r['score']:.5f}{rerank_str})")
            detail = r['rrf_detail']
            parts = []
            for signal in ['vec', 'fts', 'rec', 'graph'] + (['role'] if role_active else []):
                if signal in detail:
                    rank, contrib = detail[signal]
                    if rank is not None:
                        parts.append(f"{signal}: rank {rank} ({contrib:.5f})")
                    else:
                        parts.append(f"{signal}: -")
            print(f"    {', '.join(parts)}")
            print(f"    [{r['affiliation'] or '-'} | {r['location'] or '-'}]")
        if len(scored) > len(results):
            print(f"\n({len(scored) - len(results)} below floor or beyond top-{k})")
    elif verbose:
        sig_hdr = " rRole" if role_active else ""
        print(f"\nQuery: '{query}' ({elapsed:.2f}s, {len(results)}/{len(scored)} above floor, RRF k={RRF_K})")
        print(f"{'Name':<28} {'Affiliation':<22} {'Location':<15} {'Score':>7} {'rVec':>5} {'rFTS':>5} {'rRec':>5} {'rGrp':>5}{sig_hdr}")
        print("-" * (110 + (6 if role_active else 0)))
        for r in results:
            d = r['rrf_detail']
            def _rk(sig):
                rank = d.get(sig, (None, 0))[0]
                return f"{rank:>5}" if rank is not None else "    -"
            role_str = f" {_rk('role')}" if role_active else ""
            print(f"{(r['name'] or '')[:27]:<28} {(r['affiliation'] or '')[:21]:<22} {(r['location'] or '')[:14]:<15} "
                  f"{r['score']:>7.5f} {_rk('vec')} {_rk('fts')} {_rk('rec')} {_rk('graph')}{role_str}")

        has_edges = {r["id"]: g_edges[r["id"]] for r in results if r["id"] in g_edges}
        if has_edges:
            print("\nGraph edges:")
            for pid, edges in has_edges.items():
                name = next((r["name"] for r in results if r["id"] == pid), f"#{pid}")
                parts = [f"{rel}->{ent} ({rel_score:.1f})" for rel, ent, rel_score in edges]
                print(f"  {name}: {', '.join(parts)}")

        if len(scored) > len(results):
            print(f"\n({len(scored) - len(results)} below abstention floor)")
    else:
        print(f"Top {len(results)} for '{query}' ({elapsed:.2f}s, RRF)")
        print(f"{'Name':<30} {'Affiliation':<25} {'Location':<20} {'Score':>8}")
        print("-" * 87)
        for r in results:
            print(f"{(r['name'] or '')[:29]:<30} {(r['affiliation'] or '')[:24]:<25} "
                  f"{(r['location'] or '')[:19]:<20} {r['score']:>8.5f}")

    db.close()
    return results


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return

    query_parts = []
    k = 10
    verbose = False
    explain = False
    expand = True
    rerank = True

    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--k" and i + 1 < len(sys.argv):
            k = int(sys.argv[i + 1])
            i += 2
        elif sys.argv[i] == "--verbose":
            verbose = True
            i += 1
        elif sys.argv[i] == "--explain":
            explain = True
            i += 1
        elif sys.argv[i] == "--no-expand":
            expand = False
            i += 1
        elif sys.argv[i] == "--no-rerank":
            rerank = False
            i += 1
        else:
            query_parts.append(sys.argv[i])
            i += 1

    query = " ".join(query_parts)
    hybrid_search(query, k=k, verbose=verbose, explain=explain, expand=expand, rerank=rerank)


if __name__ == "__main__":
    main()
