"""Harvest recurring inbound questions from emails + Discord + Beeper.

Pipeline:
  1. extract: scan inbound channels for question-shaped messages, filter
     out transactional + internal + ack-only, find the operator's reply in
     the same thread, write candidates to faq_harvest_candidates table.
  2. cluster: embed candidate questions via BAAI/bge-small-en-v1.5,
     greedy-cluster by cosine similarity (threshold 0.78), pick the most
     central question per cluster as representative.
  3. review: print top clusters with suggested canonical answers (the
     best operator reply found in the cluster).
  4. promote N: write top-N clusters to faqs table with status='proposed',
     ready for the operator's manual approval. (Idempotent.)

Usage:
  python harvest_faqs.py extract                # fast scan over synced mail
  python harvest_faqs.py extract --source email
  python harvest_faqs.py cluster                # embeds + clusters
  python harvest_faqs.py review --top 30        # see top 30 clusters
  python harvest_faqs.py promote --top 20       # write top 20 to faqs (proposed)
  python harvest_faqs.py all                    # extract + cluster + review

After promote, run `embed_faqs.py --include-proposed` to make the new
FAQs semantically searchable.
"""
import argparse
import io
import re
import sqlite3
import struct
import sys
import time
from datetime import datetime, timezone
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
import config

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
CLUSTER_THRESHOLD = 0.78  # cosine similarity; tuned for FAQ harvesting

# Sender domains/addresses that are transactional and never produce FAQs.
# Generic SaaS-notification noise; extend with your own vendors via config:
#   [comms]
#   transactional_domains = ["billing.example-vendor.com"]   # FICTIONAL example
TRANSACTIONAL_DOMAINS = {
    "vercel.com", "github.com",
    "calendar.google.com", "drive-shares-dm-noreply",
    "eventbrite.com",
    "google.com", "googlegroups.com", "discord.com",
    "linkedin.com", "twitter.com", "facebook.com",
    "notion.so", "openai.com", "anthropic.com",
}
TRANSACTIONAL_DOMAINS |= {
    str(d).lower().strip() for d in (config.get("comms.transactional_domains", []) or [])
}
TRANSACTIONAL_LOCALS = {
    "no-reply", "noreply", "notification", "notifications", "alert", "alerts",
    "billing", "invoice", "invoices", "receipts", "do-not-reply", "donotreply",
    "support", "admin", "auto", "automated", "communications", "team", "info",
    "hello", "newsletter", "digest", "system", "noreply-",
}
# The operator's own org domain ([org] domain in config.toml): mail from
# colleagues is internal, not a participant FAQ. Empty = no org filter.
ORG_DOMAIN = str(config.get("org_domain") or "").lower().strip()
# Question-shaped indicators
QUESTION_OPENERS = (
    "how ", "what ", "when ", "where ", "why ", "who ", "can ", "could ",
    "should ", "would ", "will ", "do ", "does ", "is ", "are ", "was ",
    "were ", "may ", "might ", "any chance", "is it ", "are there ",
    "is there ", "do you ", "could you ", "would you ",
)
# Reject ack-only patterns
ACK_PATTERNS = (
    "thanks", "thank you", "ok", "okay", "sounds good", "got it",
    "perfect", "great", "awesome", "received",
)


def get_conn():
    conn = _db.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS faq_harvest_candidates (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,          -- email/discord/beeper
            source_row_id TEXT NOT NULL,
            thread_id TEXT,
            sender_handle TEXT,
            sender_name TEXT,
            asked_at TEXT,
            question_text TEXT NOT NULL,
            operator_answer TEXT,          -- nearest operator reply in same thread
            answer_source_row_id TEXT,
            context_url TEXT,
            cluster_id INTEGER,             -- assigned in cluster step
            is_representative INTEGER DEFAULT 0,
            extracted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source, source_row_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fhc_cluster ON faq_harvest_candidates(cluster_id)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS faq_harvest_clusters (
            id INTEGER PRIMARY KEY,
            representative_question TEXT,
            suggested_answer TEXT,
            n_occurrences INTEGER,
            sources TEXT,                  -- comma-separated: email,discord
            topic_guess TEXT,
            promoted_faq_id INTEGER,       -- if promoted, faqs.id
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def is_transactional(sender_email, sender_name):
    if not sender_email:
        return True
    se = sender_email.lower().strip()
    if "@" not in se:
        return True
    local, _, domain = se.partition("@")
    if domain in TRANSACTIONAL_DOMAINS:
        return True
    # Internal org mail (colleagues answering, not asking)
    if ORG_DOMAIN and domain == ORG_DOMAIN:
        return True
    # Transactional locals
    if any(local == tl or local.startswith(tl + "-") or local.startswith(tl + "+")
           for tl in TRANSACTIONAL_LOCALS):
        return True
    # Catch-all bots
    if "bot" in local or "system" in local:
        return True
    return False


def is_question_shaped(text):
    """Heuristic: text contains a real question, not just '?' as punctuation."""
    if not text or "?" not in text:
        return False
    t = text.strip().lower()
    # Strip quoted reply chains (Gmail-style "> ...")
    t = re.sub(r"^>.*$", "", t, flags=re.M)
    # Find question sentences
    sentences = re.split(r"(?<=[.!?])\s+", t)
    for s in sentences:
        s = s.strip()
        if not s.endswith("?"):
            continue
        if len(s) < 12 or len(s) > 600:
            continue
        if any(s.startswith(q) for q in QUESTION_OPENERS):
            return True
        # Embedded question (e.g. "I'm wondering, when is the deadline?")
        if " when " in s or " how " in s or " what " in s or " can " in s:
            return True
    return False


def extract_question_sentence(text):
    """Pull the most-likely question sentence from a body."""
    if not text:
        return None
    # Remove quoted reply
    cleaned = re.sub(r"^>.*$", "", text, flags=re.M)
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    candidates = []
    for s in sentences:
        s = s.strip()
        if not s.endswith("?"):
            continue
        if 12 <= len(s) <= 600:
            candidates.append(s)
    if not candidates:
        return None
    # Pick the one with strongest opener
    for c in candidates:
        cl = c.lower()
        if any(cl.startswith(q) for q in QUESTION_OPENERS):
            return c
    return candidates[0]


def is_ack_only(text):
    if not text:
        return True
    t = text.strip().lower()[:200]
    if len(t) < 20:
        return True
    return any(t.startswith(a) for a in ACK_PATTERNS) and "?" not in t


def find_operator_reply_in_thread(conn, thread_id, after_ts):
    """Find the operator's first outbound reply on a thread after a given timestamp."""
    if not thread_id:
        return None, None
    row = conn.execute("""
        SELECT id, body
        FROM emails
        WHERE thread_id = ?
          AND is_outgoing = 1
          AND timestamp > ?
          AND body IS NOT NULL
          AND LENGTH(body) > 30
        ORDER BY timestamp ASC
        LIMIT 1
    """, (thread_id, after_ts or "")).fetchone()
    if not row:
        return None, None
    body = row[1] or ""
    # Strip quoted reply tail
    body = re.sub(r"\nOn .+? wrote:.*$", "", body, flags=re.S)
    body = body.strip()[:1500]
    return row[0], body


def extract_from_emails(conn, limit=None):
    """Pull inbound participant questions with answers."""
    print("[extract] scanning emails...")
    t0 = time.time()
    sql = """
        SELECT id, thread_id, sender_email, sender_name, subject, body, timestamp
        FROM emails
        WHERE is_outgoing = 0
          AND body IS NOT NULL
          AND LENGTH(body) BETWEEN 50 AND 5000
          AND body LIKE '%?%'
        ORDER BY timestamp DESC
    """
    if limit:
        sql += f" LIMIT {limit}"
    rows = conn.execute(sql).fetchall()
    print(f"[extract] {len(rows)} candidate rows from emails")

    n_inserted = 0
    n_skipped_transactional = 0
    n_skipped_not_question = 0
    n_skipped_ack = 0
    n_no_answer = 0

    for (eid, thread_id, sender_email, sender_name, subject, body, ts) in rows:
        if is_transactional(sender_email, sender_name):
            n_skipped_transactional += 1
            continue
        if is_ack_only(body):
            n_skipped_ack += 1
            continue
        if not is_question_shaped(body):
            n_skipped_not_question += 1
            continue
        question = extract_question_sentence(body)
        if not question:
            n_skipped_not_question += 1
            continue
        # Find the operator's reply in the same thread
        answer_id, answer_body = find_operator_reply_in_thread(conn, thread_id, ts)
        if not answer_body:
            n_no_answer += 1
            # Still record, but with no answer
        try:
            conn.execute("""
                INSERT OR IGNORE INTO faq_harvest_candidates
                  (source, source_row_id, thread_id, sender_handle, sender_name,
                   asked_at, question_text, operator_answer, answer_source_row_id)
                VALUES ('email', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(eid), thread_id, sender_email, sender_name,
                  ts, question, answer_body, str(answer_id) if answer_id else None))
            if conn.total_changes:
                n_inserted += 1
        except sqlite3.Error as ex:
            print(f"[extract] insert error: {ex}")
    conn.commit()
    print(f"[extract] inserted: {n_inserted}  "
          f"skipped transactional: {n_skipped_transactional}  "
          f"not-question: {n_skipped_not_question}  "
          f"ack-only: {n_skipped_ack}  "
          f"no-answer: {n_no_answer}  "
          f"in {time.time()-t0:.1f}s")


def _resolve_operator_discord_ids(conn):
    """The operator's Discord user id(s), used to exclude their messages from
    questions AND treat their messages as the candidate answer.

    Resolution order:
      1. [operator] discord_user_id in config.toml (authoritative when set).
      2. people-table lookup by the configured operator name/aliases, e.g.
         [operator] name = "Jane Doe"   # FICTIONAL example
    """
    ids = set()
    direct = str(config.get("operator.discord_user_id", "") or "").strip()
    if direct:
        ids.add(direct)
    names = [config.get("operator_name")] + list(config.get("operator.aliases", []) or [])
    for n in names:
        n = str(n or "").strip()
        if not n:
            continue
        try:
            for r in conn.execute(
                "SELECT discord_user_id FROM people WHERE LOWER(name) = ?",
                (n.lower(),),
            ).fetchall():
                if r[0]:
                    ids.add(str(r[0]))
        except sqlite3.Error:
            pass  # people table may lack the column; config value still works
    return ids


def extract_from_discord(conn, limit=None):
    """Pull Discord help-desk-style questions. JOIN with discord_channels for
    name filtering, discord_users for author name. The operator's
    discord_user_id comes from config ([operator] discord_user_id) or a
    people-table lookup by the configured operator name."""
    print("[extract] scanning Discord...")
    t0 = time.time()

    operator_ids = _resolve_operator_discord_ids(conn)
    if not operator_ids:
        print("[extract] no operator Discord id configured "
              "(set [operator] discord_user_id or name in config.toml); "
              "questions will be harvested without matched answers")
    operator_id_list = list(operator_ids) or ["unknown"]

    # Find candidate "help-like" channels by name (substring match on discord_channels.name)
    help_channel_ids = [r[0] for r in conn.execute("""
        SELECT id FROM discord_channels
        WHERE LOWER(name) LIKE '%help%'
           OR LOWER(name) LIKE '%question%'
           OR LOWER(name) LIKE '%support%'
           OR LOWER(name) LIKE '%general%'
           OR LOWER(name) LIKE '%chat%'
    """).fetchall()]
    if not help_channel_ids:
        print("[extract] no help-like channels found; skipping discord")
        return
    print(f"[extract] discord help channels: {len(help_channel_ids)}")

    placeholders = ",".join("?" * len(help_channel_ids))
    sql = f"""
        SELECT m.id, m.channel_id, m.author_id, m.content, m.timestamp
        FROM discord_messages m
        WHERE m.content LIKE '%?%'
          AND LENGTH(m.content) BETWEEN 30 AND 2000
          AND m.channel_id IN ({placeholders})
          AND m.author_id NOT IN ({",".join("?" * len(operator_id_list))})
        ORDER BY m.timestamp DESC
    """
    params = help_channel_ids + operator_id_list
    if limit:
        sql += f" LIMIT {int(limit)}"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        print(f"[extract] discord query failed: {e}; skipping")
        return
    print(f"[extract] {len(rows)} discord candidate rows")

    n_inserted = 0
    for (mid, channel_id, author_id, content, ts) in rows:
        if is_ack_only(content):
            continue
        if not is_question_shaped(content):
            continue
        question = extract_question_sentence(content)
        if not question:
            continue
        # Discord answer: heuristic -- next message in same channel from the
        # operator within 2 hours
        ans_row = conn.execute(f"""
            SELECT id, content FROM discord_messages
            WHERE channel_id = ?
              AND timestamp > ?
              AND timestamp < datetime(?, '+2 hours')
              AND author_id IN ({",".join("?" * len(operator_id_list))})
            ORDER BY timestamp ASC
            LIMIT 1
        """, [channel_id, ts, ts] + operator_id_list).fetchone()
        ans_body = ans_row[1] if ans_row else None
        ans_id = ans_row[0] if ans_row else None
        # Resolve author display name from discord_users
        author_row = conn.execute(
            "SELECT username FROM discord_users WHERE id=?", (str(author_id),)
        ).fetchone()
        author_name = author_row[0] if author_row else None
        try:
            conn.execute("""
                INSERT OR IGNORE INTO faq_harvest_candidates
                  (source, source_row_id, thread_id, sender_handle, sender_name,
                   asked_at, question_text, operator_answer, answer_source_row_id)
                VALUES ('discord', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(mid), str(channel_id), str(author_id), author_name,
                  ts, question, ans_body, str(ans_id) if ans_id else None))
            if conn.total_changes:
                n_inserted += 1
        except sqlite3.Error:
            pass
    conn.commit()
    print(f"[extract] discord inserted: {n_inserted} in {time.time()-t0:.1f}s")


def extract_from_beeper(conn, limit=None):
    """Pull Beeper Slack-only inbound questions.

    Filters:
      - network = 'slack' (case-insensitive) -- ONLY Slack-bridged messages
        by default. WhatsApp/Signal/LinkedIn chats are typically personal,
        non-FAQ contexts, so they are deliberately excluded.
      - is_outgoing = 0 (inbound, not from the operator)
      - text LIKE '%?%', LENGTH BETWEEN 30 and 2000
      - sender_name is not empty (real chat, not system events)

    Answer match: same chat_id, is_outgoing=1, within 2 hours after question.
    """
    print("[extract] scanning Beeper (Slack-only)...")
    t0 = time.time()
    sql = """
        SELECT id, chat_id, sender_id, sender_name, network, text, timestamp
        FROM beeper_messages
        WHERE LOWER(network) = 'slack'
          AND is_outgoing = 0
          AND text IS NOT NULL
          AND LENGTH(text) BETWEEN 30 AND 2000
          AND text LIKE '%?%'
          AND sender_name IS NOT NULL
          AND sender_name != ''
        ORDER BY timestamp DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as e:
        print(f"[extract] beeper query failed: {e}; skipping")
        return
    print(f"[extract] {len(rows)} beeper-slack candidate rows")

    n_inserted = 0
    for (mid, chat_id, sender_id, sender_name, network, text, ts) in rows:
        if is_ack_only(text):
            continue
        if not is_question_shaped(text):
            continue
        question = extract_question_sentence(text)
        if not question:
            continue
        # Find the operator's reply in the same chat within 2 hours
        ans_row = conn.execute("""
            SELECT id, text FROM beeper_messages
            WHERE chat_id = ?
              AND is_outgoing = 1
              AND timestamp > ?
              AND timestamp < datetime(?, '+2 hours')
              AND text IS NOT NULL
              AND LENGTH(text) > 30
            ORDER BY timestamp ASC
            LIMIT 1
        """, (chat_id, ts, ts)).fetchone()
        ans_body = ans_row[1] if ans_row else None
        ans_id = ans_row[0] if ans_row else None
        try:
            conn.execute("""
                INSERT OR IGNORE INTO faq_harvest_candidates
                  (source, source_row_id, thread_id, sender_handle, sender_name,
                   asked_at, question_text, operator_answer, answer_source_row_id)
                VALUES ('beeper', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(mid), str(chat_id), str(sender_id), sender_name,
                  ts, question, ans_body, str(ans_id) if ans_id else None))
            if conn.total_changes:
                n_inserted += 1
        except sqlite3.Error:
            pass
    conn.commit()
    print(f"[extract] beeper-slack inserted: {n_inserted} in {time.time()-t0:.1f}s")


def serialize_vec(vec):
    return struct.pack(f"{DIMS}f", *vec.tolist())


def cluster_candidates(conn):
    """Embed candidate questions, greedy-cluster by cosine similarity."""
    print("[cluster] loading candidates...")
    rows = conn.execute("""
        SELECT id, question_text FROM faq_harvest_candidates
        WHERE question_text IS NOT NULL AND question_text != ''
    """).fetchall()
    if not rows:
        print("[cluster] no candidates; run extract first")
        return
    print(f"[cluster] {len(rows)} candidates to embed")

    from sentence_transformers import SentenceTransformer
    import numpy as np
    model = SentenceTransformer(MODEL_NAME)

    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    embs = model.encode(texts, show_progress_bar=False, normalize_embeddings=True,
                        batch_size=128)
    embs = np.asarray(embs, dtype=np.float32)

    # Greedy cosine clustering: O(n*k) where k = number of clusters
    print(f"[cluster] running greedy clustering at threshold {CLUSTER_THRESHOLD}...")
    cluster_centers = []  # list of (cluster_id, centroid_vec, member_count, sum_vec)
    assignments = [None] * len(ids)
    for i in range(len(ids)):
        e = embs[i]
        best = -1
        best_sim = -1.0
        for ci, (cid, centroid, count, _sumv) in enumerate(cluster_centers):
            sim = float(np.dot(e, centroid))
            if sim > best_sim:
                best_sim = sim
                best = ci
        if best >= 0 and best_sim >= CLUSTER_THRESHOLD:
            cid, centroid, count, sumv = cluster_centers[best]
            sumv2 = sumv + e
            count2 = count + 1
            centroid2 = sumv2 / np.linalg.norm(sumv2)
            cluster_centers[best] = (cid, centroid2, count2, sumv2)
            assignments[i] = cid
        else:
            new_cid = len(cluster_centers) + 1
            cluster_centers.append((new_cid, e.copy(), 1, e.copy()))
            assignments[i] = new_cid

    print(f"[cluster] {len(cluster_centers)} clusters formed")

    # Write back
    for idx, cid in zip(ids, assignments):
        conn.execute("UPDATE faq_harvest_candidates SET cluster_id=? WHERE id=?", (cid, idx))

    # Pick representative per cluster (closest to centroid)
    print("[cluster] picking representatives...")
    conn.execute("UPDATE faq_harvest_candidates SET is_representative=0")
    # For each cluster, find the member with embedding closest to centroid
    by_cluster = {}
    for i, (rowid, cid) in enumerate(zip(ids, assignments)):
        by_cluster.setdefault(cid, []).append((rowid, embs[i]))
    for cid, members in by_cluster.items():
        cidx = next(ci for ci, (c, _, _, _) in enumerate(cluster_centers) if c == cid)
        centroid = cluster_centers[cidx][1]
        # Closest member
        best_id = None
        best_sim = -1.0
        for rowid, emb in members:
            s = float(np.dot(emb, centroid))
            if s > best_sim:
                best_sim = s
                best_id = rowid
        if best_id is not None:
            conn.execute("UPDATE faq_harvest_candidates SET is_representative=1 WHERE id=?", (best_id,))
    conn.commit()

    # Rebuild faq_harvest_clusters table
    print("[cluster] writing cluster summary...")
    conn.execute("DELETE FROM faq_harvest_clusters")
    for cid, members in sorted(by_cluster.items(), key=lambda x: -len(x[1])):
        # Representative question + best available operator answer
        rep_row = conn.execute("""
            SELECT question_text, operator_answer FROM faq_harvest_candidates
            WHERE cluster_id = ? AND is_representative = 1
        """, (cid,)).fetchone()
        rep_q = rep_row[0] if rep_row else None
        rep_a = rep_row[1] if rep_row else None
        # If rep has no answer, find any cluster member with an answer
        if not rep_a:
            any_a_row = conn.execute("""
                SELECT operator_answer FROM faq_harvest_candidates
                WHERE cluster_id = ? AND operator_answer IS NOT NULL
                ORDER BY LENGTH(operator_answer) DESC LIMIT 1
            """, (cid,)).fetchone()
            if any_a_row:
                rep_a = any_a_row[0]
        # Sources
        src_row = conn.execute("""
            SELECT GROUP_CONCAT(DISTINCT source) FROM faq_harvest_candidates
            WHERE cluster_id = ?
        """, (cid,)).fetchone()
        srcs = src_row[0] if src_row else ""
        conn.execute("""
            INSERT INTO faq_harvest_clusters
              (id, representative_question, suggested_answer, n_occurrences, sources)
            VALUES (?, ?, ?, ?, ?)
        """, (cid, rep_q, rep_a, len(members), srcs))
    conn.commit()
    print(f"[cluster] done: {len(by_cluster)} clusters written")


def review(conn, top=30):
    rows = conn.execute("""
        SELECT id, representative_question, suggested_answer, n_occurrences, sources, promoted_faq_id
        FROM faq_harvest_clusters
        ORDER BY n_occurrences DESC, id ASC
        LIMIT ?
    """, (top,)).fetchall()
    if not rows:
        print("No clusters. Run extract + cluster first.")
        return
    for r in rows:
        cid, q, a, n, srcs, promoted = r
        flag = " [PROMOTED]" if promoted else ""
        print("=" * 80)
        print(f"CLUSTER {cid}{flag}  n={n}  sources={srcs}")
        print(f"  Q: {(q or '')[:150]}")
        if a:
            print(f"  A: {a[:300]}")
        else:
            print("  A: (no operator reply found in any cluster member)")


def promote(conn, top=20):
    """Promote top N clusters (by n_occurrences) to faqs with status='proposed'."""
    rows = conn.execute("""
        SELECT id, representative_question, suggested_answer, n_occurrences, sources
        FROM faq_harvest_clusters
        WHERE promoted_faq_id IS NULL
          AND n_occurrences >= 2
          AND suggested_answer IS NOT NULL
          AND LENGTH(suggested_answer) > 50
        ORDER BY n_occurrences DESC, id ASC
        LIMIT ?
    """, (top,)).fetchall()
    if not rows:
        print("No promotable clusters (need n>=2 and a real answer).")
        return
    import hashlib
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_promoted = 0
    n_skipped = 0
    for cid, q, a, n, srcs in rows:
        # Idempotent faq_id: hash of representative question. Same question text
        # across re-clusters maps to same FAQ, so re-promote is a no-op.
        qhash = hashlib.sha1((q or '').strip().lower().encode()).hexdigest()[:10]
        faq_id = f"FAQ-HARVEST-{qhash}"
        # Skip if already exists
        if conn.execute("SELECT 1 FROM faqs WHERE faq_id=?", (faq_id,)).fetchone():
            conn.execute("UPDATE faq_harvest_clusters SET promoted_faq_id=(SELECT id FROM faqs WHERE faq_id=?) WHERE id=?", (faq_id, cid))
            n_skipped += 1
            continue
        # Crude topic guess from keyword
        ql = (q or "").lower()
        topic = "general"
        if "deadline" in ql or "when" in ql or "due" in ql: topic = "deadline"
        elif "submit" in ql or "submission" in ql: topic = "submission"
        elif "review" in ql or "feedback" in ql: topic = "review"
        elif "payment" in ql or "invoice" in ql or "reimburse" in ql: topic = "payments"
        elif "team" in ql or "solo" in ql: topic = "team"
        elif "remote" in ql or "in-person" in ql: topic = "format"
        elif "signup" in ql or "register" in ql: topic = "signup"
        cur = conn.execute("""
            INSERT INTO faqs
              (faq_id, status, topic, scope, question_canonical, answer_canonical,
               ask_count, notes, created_at, updated_at)
            VALUES (?, 'proposed', ?, 'general', ?, ?, ?, ?, ?, ?)
        """, (faq_id, topic, q, a, n,
              f"Harvested from cluster {cid}, sources={srcs}. Review and promote to 'approved' after the operator's sign-off.",
              now, now))
        new_id = cur.lastrowid
        conn.execute("UPDATE faq_harvest_clusters SET promoted_faq_id=? WHERE id=?", (new_id, cid))
        n_promoted += 1
    conn.commit()
    print(f"Promoted {n_promoted} clusters to faqs (status='proposed')")
    print("Next: run `embed_faqs.py --include-proposed` to make them semantically searchable.")
    print("Then review proposed FAQs via `query.py 'SELECT * FROM faqs WHERE status=\\\"proposed\\\"'`")


def stats(conn):
    n_cand = conn.execute("SELECT COUNT(*) FROM faq_harvest_candidates").fetchone()[0]
    n_with_ans = conn.execute("SELECT COUNT(*) FROM faq_harvest_candidates WHERE operator_answer IS NOT NULL").fetchone()[0]
    n_clusters = conn.execute("SELECT COUNT(*) FROM faq_harvest_clusters").fetchone()[0]
    n_promoted = conn.execute("SELECT COUNT(*) FROM faq_harvest_clusters WHERE promoted_faq_id IS NOT NULL").fetchone()[0]
    print(f"Harvest stats:")
    print(f"  candidates: {n_cand} ({n_with_ans} with operator answer)")
    print(f"  clusters: {n_clusters}")
    print(f"  promoted: {n_promoted}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_e = sub.add_parser("extract")
    p_e.add_argument("--source", choices=["email", "discord", "beeper", "all"], default="all")
    p_e.add_argument("--limit", type=int)
    sub.add_parser("cluster")
    p_r = sub.add_parser("review")
    p_r.add_argument("--top", type=int, default=30)
    p_p = sub.add_parser("promote")
    p_p.add_argument("--top", type=int, default=20)
    sub.add_parser("stats")
    p_a = sub.add_parser("all")
    p_a.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    conn = get_conn()
    ensure_tables(conn)

    if args.cmd == "extract":
        if args.source in ("email", "all"):
            extract_from_emails(conn, args.limit)
        if args.source in ("discord", "all"):
            extract_from_discord(conn, args.limit)
        if args.source in ("beeper", "all"):
            extract_from_beeper(conn, args.limit)
        stats(conn)
    elif args.cmd == "cluster":
        cluster_candidates(conn)
        stats(conn)
    elif args.cmd == "review":
        review(conn, args.top)
    elif args.cmd == "promote":
        promote(conn, args.top)
    elif args.cmd == "stats":
        stats(conn)
    elif args.cmd == "all":
        extract_from_emails(conn)
        extract_from_discord(conn)
        extract_from_beeper(conn)
        cluster_candidates(conn)
        review(conn, args.top)

    conn.close()


if __name__ == "__main__":
    main()
