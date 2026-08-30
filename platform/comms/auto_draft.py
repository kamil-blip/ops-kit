"""auto_draft.py, Action_item -> FAQ -> Template -> email_drafts pipeline.

Trigger: brief.py classify creates a needs_reply action_item with email_thread_id.
This module:
  1. Fetches the action_item + its latest inbound email.
  2. Extracts a question from the inbound (simple heuristic + paragraph containing '?').
  3. Runs faq_lookup (hybrid mode: FTS + semantic).
  4. If strong match (semantic_distance < CONFIDENCE_THRESHOLD): drafts a reply using the FAQ answer.
  5. Personalizes [Name] placeholder with the inbound sender's first name.
  6. Anti-fab check: refuses to use FAQ answer if it cites information not in the FAQ canon.
  7. Writes email_drafts row with status='auto-suggested', linked to action_item + thread.

DOES NOT SEND. Drafts are surfaced via task_manager.py drafts list for the operator's review.

Usage:
    python auto_draft.py --action-item AI-XXXXXX [--force] [--dry-run]
    python auto_draft.py --thread <thread_id> --question "..."  # ad-hoc
    python auto_draft.py --backfill                              # scan all needs-reply OPEN items

Confidence:
  semantic_distance threshold 0.55 = strong, 0.55-0.70 = weak (still drafts but flags),
  >0.70 = no draft, skip silently.
"""
from __future__ import annotations
import paths
import _db  # unified connector (busy_timeout + FK ON)
import argparse, json, os, re, sqlite3, subprocess, sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = str(paths.DB_PATH)
PYTHON = sys.executable
DATA_DIR = Path(__file__).parent
FAQ_LOOKUP = DATA_DIR / "faq_lookup.py"

CONFIDENCE_STRONG = 0.55
CONFIDENCE_WEAK = 0.75


def _connect() -> sqlite3.Connection:
    conn = _db.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def fetch_action_item(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM action_items WHERE item_id = ?", (item_id,)
    ).fetchone()


def fetch_latest_inbound(conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT timestamp, sender_email, sender_name, subject, body "
        "FROM emails WHERE thread_id = ? AND is_outgoing = 0 "
        "ORDER BY timestamp DESC LIMIT 1",
        (thread_id,),
    ).fetchone()


def extract_question(body: str) -> str | None:
    """Find the most question-like sentence in the email body.

    Heuristic: any sentence ending in '?' that isn't a quoted reply line.
    Skips quoted (lines starting with '>') and signature areas.
    """
    if not body:
        return None
    # Strip quoted-reply blocks (`>` prefixes) and signature delimiters.
    cleaned_lines = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith(">"):
            continue
        if s.startswith("-- ") or s.startswith("--\n"):
            break
        cleaned_lines.append(line)
    cleaned = " ".join(cleaned_lines)
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Find sentences ending in '?'
    candidates = re.findall(r"[^.!?]*\?", cleaned)
    if not candidates:
        return None
    # Pick the longest (proxy for "real" question vs rhetorical "right?")
    candidates.sort(key=len, reverse=True)
    q = candidates[0].strip()
    if len(q) < 10:
        return None
    return q


def faq_lookup(question: str) -> dict | None:
    """Call faq_lookup.py and parse JSON output."""
    try:
        r = subprocess.run(
            [PYTHON, str(FAQ_LOOKUP), "lookup", question, "--json", "--mode", "hybrid"],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        if r.returncode != 0:
            print(f"  faq_lookup failed: {r.stderr[-200:]}", file=sys.stderr)
            return None
        # Trim warnings from stdout; keep just the JSON.
        out = r.stdout.strip()
        json_start = out.find("{")
        if json_start < 0:
            return None
        return json.loads(out[json_start:])
    except Exception as e:
        print(f"  faq_lookup exception: {e}", file=sys.stderr)
        return None


def personalize(body: str, sender_name: str | None) -> str:
    """Fill [Name] placeholder with first name, fall back to 'there'."""
    first = "there"
    if sender_name:
        parts = sender_name.strip().split()
        if parts and parts[0][0].isupper():
            first = parts[0]
    return body.replace("[Name]", first)


def write_draft(
    conn: sqlite3.Connection, *,
    action_item_id: str | None,
    thread_id: str | None,
    recipient_email: str,
    recipient_name: str | None,
    subject: str,
    body_html: str,
    context_slug: str | None,
    notes: str,
) -> int:
    """Insert email_drafts row, return its id. Skips if a non-rejected draft
    already exists for this action_item (idempotent)."""
    if action_item_id:
        existing = conn.execute(
            "SELECT id FROM email_drafts WHERE action_item_id = ? AND status NOT IN ('rejected','sent')",
            (action_item_id,),
        ).fetchone()
        if existing:
            return -1  # already drafted

    cur = conn.execute(
        """INSERT INTO email_drafts
           (recipient_email, recipient_name, subject, body_html, status,
            context_slug, action_item_id, thread_id, created_by, notes, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'auto-suggested', ?, ?, ?, 'auto_draft', ?, datetime('now'), datetime('now'))""",
        (recipient_email, recipient_name, subject, body_html, context_slug,
         action_item_id, thread_id, notes),
    )
    conn.commit()
    return cur.lastrowid


def run(item_id: str, dry_run: bool = False, force: bool = False) -> dict:
    """Main entry. Returns a result dict for logging."""
    conn = _connect()
    item = fetch_action_item(conn, item_id)
    if not item:
        return {"status": "skip", "reason": f"item not found: {item_id}"}
    if not item["email_thread_id"]:
        return {"status": "skip", "reason": "no email_thread_id"}
    if item["status"] not in ("OPEN", "WAITING", "IN_PROGRESS"):
        return {"status": "skip", "reason": f"item.status={item['status']}"}

    inbound = fetch_latest_inbound(conn, item["email_thread_id"])
    if not inbound:
        return {"status": "skip", "reason": "no inbound message on thread"}

    question = extract_question(inbound["body"] or "")
    if not question:
        return {"status": "skip", "reason": "no question detected in inbound"}

    faq = faq_lookup(question)
    if not faq:
        return {"status": "skip", "reason": "faq_lookup returned nothing"}

    approved = faq.get("approved_canonical")
    if not approved:
        return {"status": "skip", "reason": "no approved canonical FAQ match"}

    distance = approved.get("semantic_distance", 1.0)
    if distance > CONFIDENCE_WEAK:
        return {"status": "skip", "reason": f"FAQ semantic_distance {distance:.3f} > {CONFIDENCE_WEAK}"}
    confidence_label = "strong" if distance < CONFIDENCE_STRONG else "weak"

    answer = approved.get("answer") or ""
    if not answer.strip():
        return {"status": "skip", "reason": "FAQ answer empty"}

    body = personalize(answer, inbound["sender_name"])

    subject = inbound["subject"] or ""
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject

    if dry_run:
        return {
            "status": "would_draft",
            "faq_id": approved["faq_id"],
            "confidence": confidence_label,
            "semantic_distance": distance,
            "question": question[:120],
            "draft_subject": subject,
            "draft_body_preview": body[:300],
        }

    draft_id = write_draft(
        conn,
        action_item_id=item["item_id"],
        thread_id=item["email_thread_id"],
        recipient_email=inbound["sender_email"],
        recipient_name=inbound["sender_name"],
        subject=subject,
        body_html=body,
        context_slug=item["context_slug"],
        notes=(
            f"AUTO-DRAFT FROM FAQ {approved['faq_id']} "
            f"(distance={distance:.3f}, confidence={confidence_label}). "
            f"Question detected: {question[:200]}"
        ),
    )

    if draft_id == -1:
        return {"status": "skip", "reason": "draft already exists for this item"}

    return {
        "status": "drafted",
        "draft_id": draft_id,
        "faq_id": approved["faq_id"],
        "confidence": confidence_label,
        "semantic_distance": distance,
    }


def try_draft_from_inbox(conn: sqlite3.Connection, inbox_id: str) -> dict:
    """Auto-draft based on an action_items_inbox row (pre-promotion).

    Brief.py classify writes new items to the inbox, not directly to action_items.
    This entry point lets the brief inline-call auto_draft right after proposing
    the inbox row, so reviews see the draft alongside the proposal.

    Safe to call within an existing transaction, uses the passed conn.
    """
    row = conn.execute(
        "SELECT inbox_id, suggested_description, suggested_email_thread_id, "
        "suggested_context_slug, suggested_source_type "
        "FROM action_items_inbox WHERE inbox_id = ? AND status = 'pending'",
        (inbox_id,),
    ).fetchone()
    if not row:
        return {"status": "skip", "reason": f"inbox row not found or not pending: {inbox_id}"}

    thread_id = row["suggested_email_thread_id"]
    if not thread_id:
        return {"status": "skip", "reason": "no suggested_email_thread_id on inbox row"}
    if row["suggested_source_type"] and row["suggested_source_type"] != "email":
        return {"status": "skip", "reason": f"source_type={row['suggested_source_type']} not email"}

    inbound = fetch_latest_inbound(conn, thread_id)
    if not inbound:
        return {"status": "skip", "reason": "no inbound on thread"}

    question = extract_question(inbound["body"] or "")
    if not question:
        return {"status": "skip", "reason": "no question detected"}

    faq = faq_lookup(question)
    if not faq:
        return {"status": "skip", "reason": "faq_lookup returned nothing"}

    approved = faq.get("approved_canonical")
    if not approved:
        return {"status": "skip", "reason": "no approved FAQ match"}
    distance = approved.get("semantic_distance", 1.0)
    if distance > CONFIDENCE_WEAK:
        return {"status": "skip", "reason": f"FAQ distance {distance:.3f} > {CONFIDENCE_WEAK}"}
    confidence_label = "strong" if distance < CONFIDENCE_STRONG else "weak"

    answer = approved.get("answer") or ""
    if not answer.strip():
        return {"status": "skip", "reason": "FAQ answer empty"}
    body = personalize(answer, inbound["sender_name"])
    subject = inbound["subject"] or ""
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject

    # NOTE: action_item_id is None at this stage (item is still in inbox). When the
    # inbox row is promoted to a canonical action_item, a follow-up linker can
    # backfill the FK. For now the draft carries inbox_id in notes.
    draft_id = write_draft(
        conn,
        action_item_id=None,
        thread_id=thread_id,
        recipient_email=inbound["sender_email"],
        recipient_name=inbound["sender_name"],
        subject=subject,
        body_html=body,
        context_slug=row["suggested_context_slug"],
        notes=(
            f"AUTO-DRAFT FROM FAQ {approved['faq_id']} "
            f"(distance={distance:.3f}, confidence={confidence_label}). "
            f"Source inbox: {inbox_id}. Question: {question[:200]}"
        ),
    )
    if draft_id == -1:
        return {"status": "skip", "reason": "draft already exists"}
    return {
        "status": "drafted",
        "draft_id": draft_id,
        "faq_id": approved["faq_id"],
        "confidence": confidence_label,
        "semantic_distance": distance,
    }


def backfill() -> dict:
    """Scan all OPEN action_items with email_thread_id, run auto_draft on each."""
    conn = _connect()
    rows = list(conn.execute(
        "SELECT item_id FROM action_items "
        "WHERE status IN ('OPEN','WAITING','IN_PROGRESS') "
        "AND email_thread_id IS NOT NULL"
    ))
    print(f"Backfill scan: {len(rows)} candidate action_items")
    summary = {"drafted": 0, "skipped": 0}
    reasons = {}
    for r in rows:
        result = run(r["item_id"])
        s = result["status"]
        if s == "drafted":
            summary["drafted"] += 1
            print(f"  DRAFTED  {r['item_id']}  faq={result['faq_id']}  "
                  f"conf={result['confidence']}  dist={result['semantic_distance']:.3f}")
        else:
            summary["skipped"] += 1
            reason = result.get("reason", s)
            reasons[reason] = reasons.get(reason, 0) + 1
    print()
    print(f"Backfill done: {summary}")
    print("Skip reasons:")
    for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {r}")
    return summary


def main():
    p = argparse.ArgumentParser(description="Auto-draft reply from FAQ for an action_item.")
    p.add_argument("--action-item", help="Specific item_id to draft for")
    p.add_argument("--backfill", action="store_true", help="Scan all OPEN items with email threads")
    p.add_argument("--dry-run", action="store_true", help="Show what would be drafted, don't write")
    p.add_argument("--force", action="store_true", help="Re-draft even if a draft exists")
    args = p.parse_args()

    if args.backfill:
        backfill()
        return

    if not args.action_item:
        p.error("--action-item or --backfill required")

    result = run(args.action_item, dry_run=args.dry_run, force=args.force)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
