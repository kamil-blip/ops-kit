"""Steward brain, the frontier-model-only judgment layer (LAW 0).

Every judgment call (verify a fact at its source, resolve an ambiguous identity,
infer intent, adjudicate a multi-model disagreement) goes through here, and the only
model allowed in the decision path is the configured frontier brain
(default `claude-opus-4-8`, set `[steward] brain_model` in config.toml). There is deliberately NO cheap-model fallback path in this
module, if the frontier brain is unreachable, `judge()` raises BrainUnavailable and
the caller leaves the item queued + alarms. A lesser/local model NEVER decides.

Two ways the brain runs:
  * Agentically, a Claude Code SYSTEM session (a frontier model) drains the judgment
    queue in-context and writes resolutions back through steward_bus. This is the
    primary path; it needs no API key.
  * Autonomously (opt-in), a headless scheduled tick calls judge() against the
    Anthropic API. OFF by default (STEWARD_AUTONOMOUS_BRAIN=1 to enable) so an
    unattended run never spends frontier quota or acts during a live event without
    the operator's say-so.

The judgment QUEUE reuses review_queue (queue_type in identity|intent|contradiction
|promotion), which carries trace_json. Resolving a judgment = reading the source,
deciding, writing the canonical fact via steward_bus, and marking the review_queue
row resolved. review_queue depth trending to zero each brain-driven tick is the
LAW 3 invariant.
"""
from __future__ import annotations
import json, os, sqlite3
from _db import connect

try:
    import config as _cfg
    DEFAULT_BRAIN_MODEL = _cfg.get("steward.brain_model", "claude-opus-4-8")
except Exception:  # config optional; fall back to the shipped default
    DEFAULT_BRAIN_MODEL = "claude-opus-4-8"

FRONTIER_PREFIXES = ("claude-opus", "claude-fable", "claude-mythos")


class BrainUnavailable(RuntimeError):
    """Raised when no frontier brain is reachable. The caller must leave the item
    pending + alarm, never substitute a cheap model."""


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def brain_model(conn=None) -> str:
    own = conn is None
    conn = conn or connect()
    try:
        r = conn.execute("SELECT value FROM steward_config WHERE key='brain_model'").fetchone()
        m = r[0] if r else DEFAULT_BRAIN_MODEL
        assert any(m.startswith(p) for p in FRONTIER_PREFIXES), \
            f"LAW 0 violation: brain_model={m!r} is not a frontier model"
        return m
    finally:
        if own:
            conn.close()


# ───────────────────────── judgment queue (review_queue) ─────────────────────────
def enqueue_judgment(conn, *, queue_type: str, payload: dict, trace_json: str | None,
                     queued_by: str, priority: int = 5, dedup_key: str | None = None) -> int | None:
    """Queue a judgment for the frontier brain. queue_type in identity|intent|contradiction|promotion.
    If dedup_key is given and a pending row already carries it (payload._dedup), this is a
    no-op (returns None), so re-running the producer never duplicates the queue."""
    if dedup_key is not None:
        payload = {**payload, "_dedup": dedup_key}
        existing = conn.execute(
            "SELECT id FROM review_queue WHERE status='pending' AND json_extract(payload,'$._dedup')=?",
            (dedup_key,)).fetchone()
        if existing:
            return None
    cur = conn.execute(
        "INSERT INTO review_queue(queue_type,payload,priority,status,surfaced_at,trace_json,queued_by,queued_at) "
        "VALUES(?,?,?,'pending',datetime('now'),?,?,datetime('now'))",
        (queue_type, json.dumps(payload, default=str), priority, trace_json, queued_by))
    conn.commit()
    return cur.lastrowid


def pending_judgments(conn, limit: int = 200) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM review_queue WHERE status='pending' ORDER BY priority, id LIMIT ?", (limit,)).fetchall()


def pending_depth(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0])


def resolve_judgment(conn, judgment_id: int, note: str) -> None:
    """Resolve a review_queue row.

    For queue_type='promotion' this ALSO writes back the linked claim-staging
    row's status in the SAME transaction (parsed from the leading verb of `note`),
    so a resolved promotion row can never leave its claim stuck at 'pending' -
    that staging-drift gap came from resolving the queue row without touching the
    claim. Verb->status map:
        reject_claim / resolve_no_action -> 'rejected'
        promote_claim                    -> 'approved' (steward promote path consumes it)
    Non-promotion rows and unknown verbs keep the prior queue-only behaviour.

    The claim-staging table belongs to the extraction layer, which is not included
    in this starter kit; when the table is absent, the write-back is skipped and
    only the queue row is resolved.
    """
    row = conn.execute(
        "SELECT queue_type, payload FROM review_queue WHERE id=?", (judgment_id,)).fetchone()
    conn.execute(
        "UPDATE review_queue SET status='resolved', resolved_at=datetime('now'), resolution_note=? WHERE id=?",
        (note[:1000], judgment_id))
    if row and row[0] == "promotion" and _table_exists(conn, "thread_claims_staging"):
        try:
            claim_id = int(json.loads(row[1]).get("claim_id"))
        except Exception:
            claim_id = None
        if claim_id is not None:
            verb = (note or "").split(":", 1)[0].strip().lower()
            if verb in ("reject_claim", "resolve_no_action"):
                conn.execute(
                    "UPDATE thread_claims_staging SET status='rejected', reviewed_at=datetime('now') "
                    "WHERE id=? AND status IN ('pending','approved')", (claim_id,))
            elif verb == "promote_claim":
                conn.execute(
                    "UPDATE thread_claims_staging SET status='approved', reviewed_at=datetime('now') "
                    "WHERE id=? AND status='pending'", (claim_id,))
    conn.commit()


# ───────────────────────── frontier judgment call (fail-closed) ─────────────────────────
def autonomous_enabled() -> bool:
    return os.environ.get("STEWARD_AUTONOMOUS_BRAIN", "") == "1"


def judge(prompt: str, *, system: str | None = None, schema: dict | None = None,
          effort: str = "high", max_tokens: int = 4000) -> dict | str:
    """Run one judgment on the frontier brain. Returns parsed JSON if a schema is given,
    else the text. Raises BrainUnavailable if the brain can't be reached, the caller
    MUST then leave the item queued + alarm. No cheap-model fallback exists here."""
    model = brain_model()
    try:
        import anthropic
    except ImportError as e:
        raise BrainUnavailable("anthropic SDK not installed") from e
    try:
        client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY / auth profile
    except Exception as e:  # noqa: BLE001
        raise BrainUnavailable(f"no Anthropic client: {e}") from e

    kwargs = dict(model=model, max_tokens=max_tokens,
                  thinking={"type": "adaptive"},
                  output_config={"effort": effort},
                  messages=[{"role": "user", "content": prompt}])
    if system:
        kwargs["system"] = system
    if schema:
        kwargs["output_config"] = {"effort": effort,
                                   "format": {"type": "json_schema", "schema": schema}}
    try:
        resp = client.messages.create(**kwargs)
    except Exception as e:  # noqa: BLE001  (auth / network / rate / refusal)
        raise BrainUnavailable(f"brain call failed: {e}") from e
    if getattr(resp, "stop_reason", None) == "refusal":
        raise BrainUnavailable("brain refused the request")
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
    if schema:
        return json.loads(text)
    return text


if __name__ == "__main__":
    c = connect()
    print("brain_model:", brain_model(c))
    print("autonomous_enabled:", autonomous_enabled())
    print("pending judgments:", pending_depth(c))
