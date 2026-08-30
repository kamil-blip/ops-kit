"""Command bus - the SINGLE canonical-write path.

Every canonical write funnels through here:
  validate -> resolve identity (lookup-before-create) -> idempotent UPSERT on the
  natural key -> stamp provenance (row source_table/source_id, plus the
  fact_provenance ledger when that table is installed) -> mark staging
  promoted/rejected+reason.

Two entrypoints:
  * write(...)        - synchronous stage+publish; what migrated writers call.
  * publish_pending() - drain staging (what the steward tick calls).

Every mutation runs inside a per-row SAVEPOINT under actor_scope('steward:bus'),
so a single bad row never rolls back the batch and the advisory write-gate sees
the steward actor on compliant writes.
"""
from __future__ import annotations
import json, re, sqlite3
from typing import Optional

import validators as V
import steward_resolver as R
import steward_staging as S
import steward_brain as BR
from audit_actor import actor_scope

ACTOR = "steward:bus"
# observation is NOT here: its person_id is optional (subject may be a thread/org), so a
# NULL subject must NOT quarantine - it writes with person_id=NULL and is re-attributed later.
PERSON_TARGETS = {"people", "person_emails", "person_identities"}
KNOWN_TARGETS = {"people", "edges", "entities",
                 "observation", "person_emails", "person_identities", "attribute"}

PEOPLE_WRITABLE = {
    "name","email","linkedin","seniority","location","career_stage","discord_user_id",
    "discord_username","notes","country","education","experience","years_experience","headline",
    "summary","capability","research_interests","skills","cv_link","headshot_path","tags",
    "lifecycle_status","is_real_person","last_contact_date",
}
# Identity columns are fill-only-when-NULL. A write that would CHANGE a populated
# identity value routes to review_queue, never applies.
IDENTITY_FILL_ONLY = ("email", "discord_user_id", "linkedin")
BUSY_TIMEOUT_FLOOR_MS = 30_000

# Judgments produced INSIDE publish_row's SAVEPOINT are deferred and enqueued only
# after the row commits - BR.enqueue_judgment commits internally, and a commit inside
# the savepoint would break per-row atomicity.
_DEFERRED_JUDGMENTS: list[dict] = []


def _defer_judgment(**kw):
    _DEFERRED_JUDGMENTS.append(kw)


def _ensure_busy_timeout(conn):
    """The bus must not trust callers to set busy_timeout (seen live: write()
    raised database-is-locked from steward_staging.submit until the caller set
    the pragma). Floor every connection the bus touches at 30s."""
    try:
        cur = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        if cur is None or int(cur) < BUSY_TIMEOUT_FLOOR_MS:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_FLOOR_MS}")
    except sqlite3.Error:
        pass


def _table_exists(conn, name: str) -> bool:
    """Feature-detect an optional table. Some provenance/staging tables belong to
    the extraction layer, which is not included in this starter kit; the bus
    degrades gracefully when they are absent."""
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


# ───────────────────────── provenance helpers ─────────────────────────
def _prov(conn, fact_type, fact_id, fact_column, prov, promoted_by=ACTOR, *,
          char_start=None, char_end=None, model_chain="steward", claim_id=None, confidence=None,
          asserter_person_id=None, asserter_kind=None):
    # The fact_provenance ledger is part of the extraction/claims layer, which is
    # not included in this starter kit. When the table is absent, skip the ledger
    # INSERT - canonical rows still carry source_table/source_id inline.
    if not _table_exists(conn, "fact_provenance"):
        return
    # source_id stored raw: fact_provenance.source_id is INTEGER-affinity but sqlite
    # accepts a message-id string too, so we never lose a non-numeric ref.
    # Asserter: WHO asserted this fact, distinct from WHERE it was recorded. When a
    # claim_id is present and no asserter was passed, resolve it to the sender of the
    # evidence email (thread_claims_staging.evidence_message_id -> emails.person_id).
    # Keyspace is people.id (INTEGER) -> column fact_provenance.asserter_person_id
    # (NOT entities). thread_claims_staging is populated by the extraction layer,
    # which is not included in this kit: an empty or absent table degrades to NULL.
    if asserter_person_id is None and claim_id is not None:
        try:
            row = conn.execute(
                "SELECT e.person_id FROM thread_claims_staging tcs "
                "JOIN emails e ON e.id = tcs.evidence_message_id "
                "WHERE tcs.id=? AND e.person_id IS NOT NULL LIMIT 1", (claim_id,)).fetchone()
            if row:
                asserter_person_id = row[0]
                asserter_kind = asserter_kind or "email_sender"
        except Exception:  # noqa: BLE001 - asserter resolution is best-effort, never block the write
            pass
    conn.execute(
        "INSERT INTO fact_provenance(fact_type,fact_id,fact_column,claim_id,source_table,source_id,"
        "source_char_start,source_char_end,source_quote,model_chain,confidence,"
        "asserter_person_id,asserter_kind,promoted_at,promoted_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?)",
        (fact_type, fact_id, fact_column, claim_id, prov.get("source_table"), prov.get("source_id"),
         char_start, char_end, prov.get("source_quote"), model_chain, confidence,
         asserter_person_id, asserter_kind, promoted_by),
    )


def _resolve_identity(conn, payload, nk, create_ok):
    """Return (person_id, trace_list). Looks in nk then payload for email/name/discord."""
    trace0 = []
    # nk 'id' is accepted as an explicit people-id pin. Callers naturally write
    # {"id": 913} (the table's own PK name); when it was silently discarded,
    # resolution fell through to payload identity keys - so a POISONED
    # discord_user_id could redirect an explicit-id correction to the wrong
    # person (proven live). An explicit pin outranks every payload identity key;
    # conflicting keys are treated as corrections.
    pid = nk.get("person_id") or nk.get("id") or payload.get("person_id")
    if pid:
        # A caller-supplied person_id is only trusted if the people row exists in
        # this same txn - otherwise it would create a silent orphan (person_emails /
        # identity rows pointing at nobody). Fall through to identifier resolution,
        # which resolves-or-quarantines.
        try:
            pid_i = int(pid)
        except (TypeError, ValueError):
            pid_i = None
        if pid_i is not None and conn.execute("SELECT 1 FROM people WHERE id=?", (pid_i,)).fetchone():
            return pid_i, [f"person_id given={pid}"]
        trace0 = [f"orphan_ref:person_id:{pid} - given id not in people; falling back to identifier resolution"]
    email = nk.get("email") or payload.get("email")
    name = nk.get("name") or payload.get("name")
    discord = nk.get("discord_id") or payload.get("discord_user_id") or payload.get("discord_id")
    org = payload.get("org") or payload.get("organization")
    if create_ok and email:
        res = R.get_or_create_person(conn, email=email, name=name, discord_id=discord, org=org)
    else:
        res = R.resolve_person(conn, email=email, discord_id=discord, name=name, org=org)
    # A name match disconfirmed by a mismatched email created a NEW person
    # (ambiguous/new) - surface the collision for merge-review instead of silence.
    if res.get("email_disconfirmed"):
        _defer_judgment(
            queue_type="identity",
            payload={"kind": "name_collision_email_disconfirm",
                     "created_person_id": res.get("person_id") if res.get("created") else None,
                     "name": name, "email": email,
                     "name_match_candidates": res.get("candidates", [])},
            trace_json=json.dumps(res.get("trace", []), default=str),
            queued_by="steward:bus",
            dedup_key=f"namecollision:{R.normalize_name(name)}:{R.normalize_email(email)}")
    return res.get("person_id"), trace0 + res.get("trace", [])


# ───────────────────────── referential integrity ─────────────────────────
# Every bus write that references an entities-keyspace id must resolve in the SAME
# transaction: mint the referent through the bus's own entity-creation path
# (_h_entities, inside publish_row's actor_scope, so the mint is actor-stamped) when
# it is derivable, else reject the write to the review queue. NO silent orphans -
# FK orphans were recreated at write time and cleaned reactively many times before this.
_REF_MINT_SOURCE = "steward:ref-mint"


def _entity_ref_exists(conn, ref) -> bool:
    return conn.execute("SELECT 1 FROM entities WHERE id=?", (ref,)).fetchone() is not None


def _mint_entity_ref(conn, ref, prov, hint=None) -> bool:
    """Mint a missing entities referent via _h_entities. Only derivable ids mint:
      * caller hint payload['ref_entities'][ref] = {'type':..., 'name':...}
      * person-<people.id>  -> from the people row (canonical person entity id)
    Returns False when underivable (caller then rejects to review)."""
    etype = name = None
    if isinstance(hint, dict) and hint.get("type") and hint.get("name"):
        etype, name = hint["type"], hint["name"]
    else:
        m = re.fullmatch(r"person-(\d+)", ref)
        if m:
            row = conn.execute("SELECT COALESCE(name,email) FROM people WHERE id=?",
                               (int(m.group(1)),)).fetchone()
            if row and row[0]:
                etype, name = "person", row[0]
    if not (etype and name):
        return False
    _h_entities(conn, {"id": ref, "type": etype, "name": name, "source": _REF_MINT_SOURCE},
                {}, prov, None)
    return True


def _require_entity_refs(conn, target, refs, prov, payload):
    """refs: [(role, ref), ...]. Verify each referent exists in entities within this
    transaction; mint-or-reject. Raises _Reject(['unresolved_ref', ...]) which the
    publish loop routes to quarantine + identity judgment (dead-letter/review)."""
    hints = payload.get("ref_entities") if isinstance(payload.get("ref_entities"), dict) else {}
    missing = []
    for role, ref in refs:
        if ref is None or ref == "":
            continue
        ref = str(ref)
        if _entity_ref_exists(conn, ref):
            continue
        if _mint_entity_ref(conn, ref, prov, hints.get(ref)):
            continue
        missing.append(f"orphan_ref:{role}:{ref}")
    if missing:
        raise _Reject(target, ["unresolved_ref"] + missing)


# ───────────────────────── per-target upsert handlers ─────────────────────────
def _h_people(conn, payload, nk, prov, pid):
    fields = {k: v for k, v in payload.items() if k in PEOPLE_WRITABLE and v is not None}
    if not fields:
        return pid, "noop"
    # Identity columns fill only when NULL/empty. A conflicting change to a
    # populated value is dropped from this write and routed to review_queue with
    # old/new + source (deferred past the savepoint) - never silently applied.
    id_cols = [k for k in IDENTITY_FILL_ONLY if k in fields]
    if id_cols:
        cur = dict(zip(id_cols, conn.execute(
            f"SELECT {','.join(id_cols)} FROM people WHERE id=?", (pid,)).fetchone()))
        for k in id_cols:
            old = str(cur[k]).strip() if cur[k] is not None else ""
            new = str(fields[k]).strip()
            if not old:
                continue  # fill-when-NULL allowed
            if old.lower() == new.lower():
                fields.pop(k)  # no-op
            else:
                _defer_judgment(
                    queue_type="identity",
                    payload={"kind": "identity_conflict", "person_id": pid, "column": k,
                             "old": cur[k], "new": fields[k],
                             "source": {sk: prov.get(sk) for sk in ("source_table", "source_id", "source_quote")}},
                    trace_json=None, queued_by=ACTOR,
                    dedup_key=f"idconflict:{pid}:{k}:{new.lower()}")
                fields.pop(k)
        if not fields:
            return pid, "noop"
    sets, vals = ["updated_at=datetime('now')"], []
    for k, v in fields.items():
        sets.append(f"{k}=COALESCE(?,{k})"); vals.append(v)  # additive: never blank a populated field
    vals.append(pid)
    conn.execute(f"UPDATE people SET {','.join(sets)} WHERE id=?", vals)
    for k in fields:
        _prov(conn, "people_field", pid, k, prov)
    return pid, "update"


def _h_edges(conn, payload, nk, prov, pid):
    src = nk.get("source_id") or payload.get("source_id")
    tgt = nk.get("target_id") or payload.get("target_id")
    rel = nk.get("relation") or payload.get("relation")
    vf = nk.get("valid_from") or payload.get("valid_from")
    if not (src and tgt and rel):
        raise _Reject("edges", ["edge_missing_src_tgt_rel"])
    if not vf:
        raise _Reject("edges", ["edge_missing_valid_from"])  # valid_from is REQUIRED
    # Both endpoints must exist in entities in this same txn (mint-or-reject).
    _require_entity_refs(conn, "edges", [("source_id", src), ("target_id", tgt)], prov, payload)
    srcref = f"{prov.get('source_table')}:{prov.get('source_id')}" if prov.get("source_table") else payload.get("source")
    existing = conn.execute(
        "SELECT id FROM edges WHERE source_id=? AND target_id=? AND relation=? AND valid_from=?",
        (src, tgt, rel, vf)).fetchone()
    if existing:
        conn.execute("UPDATE edges SET source_table=COALESCE(?,source_table), source=COALESCE(?,source), "
                     "fact=COALESCE(?,fact), confidence=COALESCE(?,confidence) WHERE id=?",
                     (prov.get("source_table"), srcref, payload.get("fact"), payload.get("confidence"), existing[0]))
        rid = existing[0]; action = "update"
    else:
        cur = conn.execute(
            "INSERT INTO edges(source_id,target_id,relation,fact,valid_from,valid_until,recorded_at,confidence,source,source_table) "
            "VALUES(?,?,?,?,?,?,datetime('now'),?,?,?)",
            (src, tgt, rel, payload.get("fact"), vf, payload.get("valid_until"),
             payload.get("confidence"), srcref, prov.get("source_table")))
        rid = cur.lastrowid; action = "insert"
    _prov(conn, "edge", rid, None, prov)
    return rid, action


def _h_edges_retire(conn, payload, nk, prov):
    """op='retire': set valid_until on an existing edge (no delete). Resolve by id, or by
    the (src,tgt,rel,valid_from) natural key. Carries actor+provenance via _prov so the
    cdc_log row is never NULL-actor."""
    eid = nk.get("id") or payload.get("id")
    until = payload.get("valid_until") or nk.get("valid_until")
    if not eid:
        src = nk.get("source_id") or payload.get("source_id")
        tgt = nk.get("target_id") or payload.get("target_id")
        rel = nk.get("relation") or payload.get("relation")
        vf = nk.get("valid_from") or payload.get("valid_from")
        if src and tgt and rel and vf:
            row = conn.execute(
                "SELECT id FROM edges WHERE source_id=? AND target_id=? AND relation=? AND valid_from=?",
                (src, tgt, rel, vf)).fetchone()
            eid = row[0] if row else None
    if not eid:
        raise _Reject("edges", ["retire_missing_edge_id"])
    n = conn.execute(
        "UPDATE edges SET valid_until=COALESCE(?,datetime('now')) WHERE id=? AND (valid_until IS NULL OR valid_until='')",
        (until, eid)).rowcount
    _prov(conn, "edge", eid, None, prov)
    return eid, ("retired" if n else "already_retired")


def _h_retire(conn, target, payload, nk, prov):
    if target == "edges":
        return _h_edges_retire(conn, payload, nk, prov)
    raise _Reject(target, [f"retire_unsupported_target:{target}"])


def _h_entities(conn, payload, nk, prov, pid):
    eid = nk.get("id") or payload.get("id")
    if not eid:
        raise _Reject("entities", ["entity_id_required"])
    existing = conn.execute("SELECT id FROM entities WHERE id=?", (eid,)).fetchone()
    data = payload.get("data")
    if isinstance(data, (dict, list)):
        data = json.dumps(data)
    if existing:
        conn.execute("UPDATE entities SET name=COALESCE(?,name), data=COALESCE(?,data), "
                     "status=COALESCE(?,status), updated_at=datetime('now') WHERE id=?",
                     (payload.get("name"), data, payload.get("status"), eid))
        action = "update"
    else:
        # Minting guard: role-account names (slack, postmaster, info, noreply, ...)
        # were repeatedly minted as person/org entities before the junk-flag sweep.
        # The names live in identifier_denylist (reason 'junk-entity-name');
        # never mint them again.
        name_norm = (payload.get("name") or "").lower().strip()
        if payload.get("type", "org") in ("person", "org") and name_norm and conn.execute(
                "SELECT 1 FROM identifier_denylist WHERE kind='exact' AND pattern=? "
                "AND reason LIKE '%junk-entity-name%'", (name_norm,)).fetchone():
            raise _Reject("entities", [f"junk_entity_name:{name_norm}"])
        conn.execute("INSERT INTO entities(id,type,name,data,source,status,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,datetime('now'),datetime('now'))",
                     (eid, payload.get("type","org"), payload.get("name"), data,
                      payload.get("source","steward"), payload.get("status","active")))
        action = "insert"
    return eid, action


def _h_observation(conn, payload, nk, prov, pid):
    import hashlib
    pid = pid or payload.get("person_id")  # optional; observations may have NULL subject person
    if pid is not None:
        # A NON-NULL person ref must resolve in this txn. NULL stays allowed
        # (subject may be a thread/org; re-attributed later) - only a dangling ref rejects.
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            raise _Reject("observation", ["unresolved_person", f"orphan_ref:person_id:{pid}"])
        if not conn.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
            raise _Reject("observation", ["unresolved_person", f"orphan_ref:person_id:{pid}"])
    content = payload.get("content")
    if not content:
        raise _Reject("observation", ["observation_content_required"])
    # observations.subject is NOT NULL, but upstream sinks may submit subject=None
    # when the claim's person was unresolved -> the raw INSERT would die on the
    # constraint and quarantine rows. Derive the subject here (pid -> people.name;
    # else claim -> subject person/email); an underivable subject is a terminal
    # *named* reject, never a constraint crash.
    subject = payload.get("subject")
    if not subject:
        if pid:
            row = conn.execute("SELECT name FROM people WHERE id=?", (pid,)).fetchone()
            subject = row[0] if row and row[0] else None
        if not subject and payload.get("claim_id") and _table_exists(conn, "thread_claims_staging"):
            # Claims staging belongs to the extraction layer (not included in this
            # starter kit); when absent the subject simply stays underivable.
            row = conn.execute(
                "SELECT COALESCE(p.name, NULLIF(TRIM(tcs.subject_email),'')) "
                "FROM thread_claims_staging tcs LEFT JOIN people p ON p.id=tcs.subject_person_id "
                "WHERE tcs.id=?", (payload.get("claim_id"),)).fetchone()
            subject = row[0] if row and row[0] else None
        if not subject:
            raise _Reject("observation", ["observation_subject_underivable"])
        payload = dict(payload, subject=subject)
    # A shell-command / non-person string must never land in observations.subject
    # (this is the WRITER guard at the sink; the data was cleaned once already).
    # Reuse the validators shell-prefix detector; catches BOTH a payload-provided
    # subject and a derived one -> terminal *named* reject.
    _subj = payload.get("subject")
    if _subj:
        try:
            from validators import _SHELL_PREFIX_RE as _SPR
        except Exception:  # noqa: BLE001
            _SPR = None
        if _SPR is not None and _SPR.match(str(_subj)[:100]):
            raise _Reject("observation", ["observation_subject_shell_command"])
    chash = payload.get("content_hash") or hashlib.sha256(f"{pid}:{content}".encode()).hexdigest()[:32]
    promoted = 1 if payload.get("promoted") else 0
    existing = conn.execute(
        "SELECT id FROM observations WHERE content_hash=? AND superseded_by IS NULL LIMIT 1", (chash,)).fetchone()
    if existing:
        rid = existing[0]; action = "update"
        if promoted:  # promotion implies TTL protection (memory_lifecycle keeps promoted rows)
            conn.execute("UPDATE observations SET promoted=1 WHERE id=? AND (promoted IS NULL OR promoted=0)", (rid,))
        conn.execute("UPDATE observations SET updated_at=datetime('now'), source_table=COALESCE(?,source_table), "
                     "source_id=COALESCE(?,source_id) WHERE id=?",
                     (prov.get("source_table"), str(prov.get("source_id")) if prov.get("source_id") else None, rid))
    else:
        cur = conn.execute(
            "INSERT INTO observations(subject,subject_type,person_id,content,source,confidence,inserted_at,"
            "content_hash,promoted,source_table,source_id,claim_id) VALUES(?,?,?,?,?,?,datetime('now'),?,?,?,?,?)",
            (payload.get("subject"), payload.get("subject_type","person"), pid, content,
             payload.get("source","steward"), payload.get("confidence"), chash, promoted,
             prov.get("source_table"), str(prov.get("source_id")) if prov.get("source_id") else None,
             payload.get("claim_id")))
        rid = cur.lastrowid; action = "insert"
    _prov(conn, "observation", rid, None, prov,
          promoted_by=payload.get("promoted_by", ACTOR),
          char_start=payload.get("source_char_start"), char_end=payload.get("source_char_end"),
          model_chain=payload.get("model_chain", "steward"), claim_id=payload.get("claim_id"),
          confidence=payload.get("prov_confidence"))
    # Surface the STORED row's person_id so write()'s result matches the row
    # (the dedup/update path keeps the old subject, so the resolved person of the
    # stored row can differ from this write's input).
    stored_pid = conn.execute("SELECT person_id FROM observations WHERE id=?", (rid,)).fetchone()[0]
    return rid, action, stored_pid


def _sync_identifier_floor(conn, pid, id_type, value):
    """Dual-write into person_identifiers (the derived identifier floor) so merge
    detection never lags a steward identity write. Floor types only (no names).
    The nightly re-derive pass is the catch-up net for anything that slips past."""
    if id_type not in ("email", "discord", "linkedin", "phone"):
        return
    if not (value and value.strip()):
        return
    conn.execute(
        "INSERT OR IGNORE INTO person_identifiers (person_id, id_type, id_value, id_value_norm, source_table) "
        "VALUES (?,?,?,?,'steward_bus')", (pid, id_type, value, value.strip().lower()))


def _h_person_emails(conn, payload, nk, prov, pid):
    email = R.normalize_email(nk.get("email") or payload.get("email"))
    if not email:
        raise _Reject("person_emails", ["email_required"])
    is_primary = payload.get("is_primary", 0)
    if is_primary and not R.is_valid_primary_email(email):  # demote invalid, still store
        is_primary = 0
    conn.execute("INSERT OR IGNORE INTO person_emails(person_id,email,source,is_primary) VALUES(?,?,?,?)",
                 (pid, email, payload.get("source","steward"), is_primary))
    conn.execute("INSERT OR IGNORE INTO person_identities(person_id,identity_type,identity_value,normalized_value,disambiguation_risk,source) "
                 "VALUES(?,?,?,?,?,?)", (pid, "email", email, email, "low", "steward"))
    _sync_identifier_floor(conn, pid, "email", email)
    rid = conn.execute("SELECT id FROM person_emails WHERE person_id=? AND email=?", (pid, email)).fetchone()[0]
    return rid, "upsert"


def _h_person_identities(conn, payload, nk, prov, pid):
    it = nk.get("identity_type") or payload.get("identity_type")
    val = nk.get("identity_value") or payload.get("identity_value")
    if not (it and val):
        raise _Reject("person_identities", ["identity_type_value_required"])
    norm = R.normalize_email(val) if it == "email" else R.normalize_name(val)
    risk = R.name_risk(val) if it == "name" else payload.get("disambiguation_risk","low")
    conn.execute("INSERT OR IGNORE INTO person_identities(person_id,identity_type,identity_value,normalized_value,disambiguation_risk,source) "
                 "VALUES(?,?,?,?,?,?)", (pid, it, val, norm, risk, payload.get("source","steward")))
    _sync_identifier_floor(conn, pid, it, val)
    rid = conn.execute("SELECT id FROM person_identities WHERE person_id=? AND identity_type=? AND normalized_value=?",
                       (pid, it, norm)).fetchone()[0]
    return rid, "upsert"


def _h_learnings(conn, payload, nk, prov, pid):
    """Publish a learning PROPOSAL (status='proposed' by default). Idempotent on
    content_hash/learning_id. Used by the comms monitor's learn-from-tweaks path so a
    learning is never written directly by a non-steward writer."""
    import hashlib
    desc = payload.get("description")
    if not desc:
        raise _Reject("learnings", ["learning_description_required"])
    chash = payload.get("content_hash") or hashlib.sha256(desc.encode()).hexdigest()[:32]
    lid = payload.get("learning_id") or f"LRN-PROP-{chash[:12]}"
    existing = conn.execute("SELECT id FROM learnings WHERE learning_id=? OR content_hash=?", (lid, chash)).fetchone()
    if existing:
        return existing[0], "exists"
    cur = conn.execute(
        "INSERT INTO learnings(learning_id,title,description,priority,status,apply_when,source,content_hash,memory_type,inserted_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (lid, payload.get("title"), desc, payload.get("priority", "P3"), payload.get("status", "proposed"),
         payload.get("apply_when"), payload.get("source", "comms_monitor"), chash, payload.get("memory_type", "proposal")))
    return cur.lastrowid, "insert"


def _h_attribute(conn, payload, nk, prov, pid):
    """Typed attribute on an entity (entities keyspace, TEXT id) - NOT a person row.
    Supersession: flip the current row to status='historical' (set valid_until), then
    insert the new current row. The idx_attributes_current partial unique index makes
    supersede-then-insert race-safe inside the per-row SAVEPOINT. 'attribute' is NOT in
    PERSON_TARGETS, so pid is unused; the subject is the caller-supplied entity_id."""
    ent = nk.get("entity_id") or payload.get("entity_id")
    attr = nk.get("attr") or payload.get("attr")
    val = payload.get("value")
    if not (ent and attr):
        raise _Reject("attribute", ["attribute_entity_attr_required"])
    # Subject entity + optional ref_entity must exist in this txn (mint-or-reject).
    refs = [("entity_id", ent)]
    if payload.get("ref_entity_id"):
        refs.append(("ref_entity_id", payload.get("ref_entity_id")))
    _require_entity_refs(conn, "attribute", refs, prov, payload)
    conn.execute(
        "UPDATE attributes SET status='historical', valid_until=datetime('now'), updated_at=datetime('now') "
        "WHERE entity_id=? AND attr=? AND status='current'", (ent, attr))
    cur = conn.execute(
        "INSERT INTO attributes(entity_id,attr,value,value_type,ref_entity_id,valid_from,status,"
        "confidence,scope,visibility,source_table,source_id,asserter_entity_id,inserted_at,updated_at) "
        "VALUES(?,?,?,?,?,COALESCE(?,datetime('now')),'current',?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (ent, attr, val, payload.get("value_type"), payload.get("ref_entity_id"),
         payload.get("valid_from"), payload.get("confidence"),
         payload.get("scope"), payload.get("visibility"),
         prov.get("source_table"), str(prov.get("source_id")) if prov.get("source_id") else None,
         # asserter comes from the caller's payload; prov only ever carries
         # source_table/source_id/source_quote (the old prov.get() was always
         # NULL, breaking bi-temporal attribution)
         payload.get("asserter_entity_id") or prov.get("asserter_entity_id")))
    rid = cur.lastrowid
    _prov(conn, "attribute", rid, attr, prov,
          asserter_person_id=payload.get("asserter_person_id"),
          asserter_kind=payload.get("asserter_kind"))
    return rid, "insert"


HANDLERS = {
    "people": _h_people, "edges": _h_edges, "entities": _h_entities,
    "observation": _h_observation, "person_emails": _h_person_emails,
    "person_identities": _h_person_identities, "learnings": _h_learnings,
    "attribute": _h_attribute,
}


class _Reject(Exception):
    def __init__(self, target, errors):
        self.target = target; self.errors = errors
        super().__init__(f"{target}: {errors}")


# ───────────────────────── publish ─────────────────────────
def publish_row(conn, staging_id: int, *, create_ok: bool = True,
                batch_claims: Optional[dict] = None, defer_commit: bool = False) -> dict:
    # When defer_commit=True the caller (publish_pending) commits in batches. The
    # per-row SAVEPOINT 'pub' still isolates each row (a bad row's ROLLBACK TO pub
    # + quarantine UPDATE happen before the batch boundary, and a later failure
    # never rolls back an earlier promoted row), so quarantine semantics are
    # preserved while fsync churn drops from ~1/row to ~1/100.
    _ensure_busy_timeout(conn)
    del _DEFERRED_JUDGMENTS[:]
    row = conn.execute("SELECT * FROM staging WHERE id=?", (staging_id,))
    cols = [d[0] for d in row.description]
    r = dict(zip(cols, row.fetchone()))
    if r["status"] == "promoted":
        # The dedup path must surface the resolved person too - this early return
        # once returned person_id:null while the stored row had a resolved person.
        return {"status": "promoted", "canonical_id": r["canonical_id"], "dedup": True,
                "person_id": r["resolved_person_id"]}
    target = r["target_table"]
    payload = json.loads(r["payload"]) if r["payload"] else {}
    nk = json.loads(r["natural_key"]) if r["natural_key"] else {}
    prov = {"source_table": r["source_table"], "source_id": r["source_id"], "source_quote": r["source_quote"]}
    # Advisory fake-provenance guard: a canonical write whose source_table is not a
    # real ingest table is a fake-provenance smell. ADVISORY: LOG it (so the backlog
    # is measurable for a later backfill) but DO NOT reject - some legitimate
    # writers still emit these. The hard reject is a separate STAGED step, gated on
    # the source backfill.
    _FAKE_SOURCE_TABLES = frozenset({"confirmed_by_third_party", "confirmed_directly_at",
                                     "confirmed_at", "people", "contacted_by"})
    # 'people' is a fake-source smell on a canonical status write, but a LEGITIMATE
    # same-entity provenance anchor for enrichment writes (enrichment writers store
    # people/observation rows anchored to the person record itself). Exempt those so
    # the advisory log stays a clean backfill signal instead of flagging successful
    # self-anchored enrichments.
    _anchor_ok = (target == "observation" or prov.get("source_table") == target)
    if prov.get("source_table") in _FAKE_SOURCE_TABLES and not _anchor_ok:
        try:
            V.log_rejection(conn, r["submitted_by"] or "steward", target,
                            [f"advisory_fake_source_table:{prov.get('source_table')}"], payload,
                            context=f"staging:{staging_id}")
        except Exception:  # noqa: BLE001 - advisory only, never block on a logging failure
            pass
        # advisory: fall through and continue (do NOT reject this run)
    if target not in HANDLERS:
        conn.execute("UPDATE staging SET status='rejected', error=? WHERE id=?",
                     (f"unknown_target:{target}", staging_id))
        if not defer_commit:
            conn.commit()
        return {"status": "rejected", "error": f"unknown_target:{target}"}

    conn.execute("UPDATE staging SET attempts=COALESCE(attempts,0)+1, last_attempt_at=datetime('now') WHERE id=?", (staging_id,))
    trace = []
    conn.execute("SAVEPOINT pub")
    try:
        op = (r.get("op") or "upsert")
        with actor_scope(conn, ACTOR, source_ref=f"staging:{staging_id}"):
            pid = None
            if op == "retire":
                canonical_id, action = _h_retire(conn, target, payload, nk, prov)
            else:
                if target in PERSON_TARGETS:
                    pid, trace = _resolve_identity(conn, payload, nk, create_ok)
                    if pid is None:
                        raise _Reject(target, ["unresolved_person"])
                    # Within-batch uniqueness - two rows of one ingest batch
                    # resolving to the SAME person with DIFFERENT emails is a
                    # roster-rotation shape seen live; the second row must not
                    # apply silently.
                    if batch_claims is not None:
                        ne = R.normalize_email(nk.get("email") or payload.get("email"))
                        prev = batch_claims.get(pid)
                        if prev is not None and ne and prev != ne:
                            raise _Reject(target, [
                                "batch_duplicate_person",
                                f"person_id:{pid} already written this batch with email {prev}; this row carries {ne}"])
                        if ne and prev is None:
                            batch_claims[pid] = ne
                res_h = HANDLERS[target](conn, payload, nk, prov, pid)
                if isinstance(res_h, tuple) and len(res_h) == 3:
                    canonical_id, action, h_pid = res_h
                    pid = h_pid if h_pid is not None else pid
                else:
                    canonical_id, action = res_h
        conn.execute("RELEASE pub")
        conn.execute(
            "UPDATE staging SET status='promoted', canonical_id=?, resolved_person_id=?, promoted_at=datetime('now'), "
            "promoted_by=?, trace_json=COALESCE(trace_json,?) WHERE id=?",
            (str(canonical_id), pid, ACTOR, json.dumps(trace), staging_id))
        if not defer_commit:
            conn.commit()
        # drain judgments produced inside the savepoint (enqueue commits internally)
        for kw in _DEFERRED_JUDGMENTS:
            try:
                BR.enqueue_judgment(conn, **kw)
            except Exception:  # noqa: BLE001 - never fail a committed write on queue trouble
                pass
        del _DEFERRED_JUDGMENTS[:]
        return {"status": "promoted", "canonical_id": canonical_id, "action": action, "person_id": pid}
    except _Reject as rej:
        conn.execute("ROLLBACK TO pub"); conn.execute("RELEASE pub")
        del _DEFERRED_JUDGMENTS[:]  # savepoint rolled back; deferred refs are stale
        V.log_rejection(conn, r["submitted_by"] or "steward", target, rej.errors, payload, context=f"staging:{staging_id}")
        # Fail-closed, no silent drop: an unresolvable identity is QUARANTINED (raw
        # payload kept) and routed to the judgment queue; a validation failure is
        # REJECTED (terminal, fixable by a corrected re-submit).
        if any(e in rej.errors for e in ("unresolved_person", "unresolved_ref", "batch_duplicate_person")):
            # unresolved_ref: a write referenced an entities/people id that does not
            # exist and could not be minted - dead-letter to the review queue, same
            # convention as an unresolved person. dedup_key so a re-armed retry of the
            # same staging row never duplicates the judgment.
            conn.execute("UPDATE staging SET status='quarantined', error=?, trace_json=COALESCE(trace_json,?) WHERE id=?",
                         (json.dumps(rej.errors)[:1000], json.dumps(trace), staging_id))
            try:
                BR.enqueue_judgment(conn, queue_type="identity",
                                    payload={"staging_id": staging_id, "target": target, "nk": nk,
                                             "payload": payload, "errors": rej.errors},
                                    trace_json=json.dumps(trace), queued_by=r["submitted_by"] or "steward:bus",
                                    dedup_key=f"staging:{staging_id}")
            except Exception:  # noqa: BLE001 - never let queueing failure lose the row
                pass
            if not defer_commit:
                conn.commit()
            return {"status": "quarantined", "error": rej.errors}
        conn.execute("UPDATE staging SET status='rejected', error=? WHERE id=?",
                     (json.dumps(rej.errors)[:1000], staging_id))
        if not defer_commit:
            conn.commit()
        return {"status": "rejected", "error": rej.errors}
    except Exception as exc:
        conn.execute("ROLLBACK TO pub"); conn.execute("RELEASE pub")
        del _DEFERRED_JUDGMENTS[:]
        # Unexpected failure: quarantine (keep raw payload + error), never drop.
        conn.execute("UPDATE staging SET status='quarantined', error=? WHERE id=?",
                     (f"exception:{str(exc)[:500]}", staging_id))
        if not defer_commit:
            conn.commit()
        return {"status": "error", "error": str(exc)[:500]}


def write(conn, *, target_table: str, payload: dict, submitted_by: str,
          natural_key: Optional[dict] = None, op: str = "upsert",
          source_table: Optional[str] = None, source_id=None, source_quote: Optional[str] = None,
          create_ok: bool = True, batch_claims: Optional[dict] = None) -> dict:
    """Synchronous stage+publish. The path migrated writers call instead of a direct INSERT.
    Loop callers ingesting a batch should pass one shared batch_claims={} so within-batch
    duplicate person resolution routes to review instead of applying silently."""
    _ensure_busy_timeout(conn)
    st = S.submit(conn, target_table=target_table, payload=payload, natural_key=natural_key, op=op,
                  submitted_by=submitted_by, source_table=source_table, source_id=source_id, source_quote=source_quote)
    res = publish_row(conn, st["staging_id"], create_ok=create_ok, batch_claims=batch_claims)
    res["staging_id"] = st["staging_id"]
    return res


def publish_pending(conn, *, limit: int = 2000, create_ok: bool = True,
                    commit_every: int = 100) -> dict:
    _ensure_busy_timeout(conn)
    rows = conn.execute("SELECT id FROM staging WHERE status='pending' ORDER BY id LIMIT ?", (limit,)).fetchall()
    out = {"promoted": 0, "rejected": 0, "quarantined": 0, "error": 0, "total": len(rows)}
    claims: dict = {}  # one batch = one publish_pending drain
    # Batch the per-row commits (commit_every=100). Per-row quarantine isolation is
    # preserved by publish_row's SAVEPOINT 'pub' (defer_commit=True skips only the
    # final conn.commit, not the savepoint rollback+quarantine UPDATE).
    for i, (sid,) in enumerate(rows):
        res = publish_row(conn, sid, create_ok=create_ok, batch_claims=claims, defer_commit=True)
        s = res["status"]
        out[s if s in out else "error"] += 1
        if (i + 1) % commit_every == 0:
            conn.commit()
    conn.commit()
    return out


def _cli():
    """Bus front door. Maps directly onto write()."""
    import argparse
    ap = argparse.ArgumentParser(
        description="steward_bus front door - the single canonical-write path")
    sub = ap.add_subparsers(dest="cmd")
    w = sub.add_parser("write", help="stage+publish one canonical write")
    w.add_argument("--target", required=True, choices=sorted(HANDLERS))
    w.add_argument("--payload", required=True, help="JSON object")
    w.add_argument("--natural-key", default=None, help="JSON object")
    w.add_argument("--submitted-by", required=True)
    w.add_argument("--source-table", default=None)
    w.add_argument("--source-id", default=None)
    w.add_argument("--source-quote", default=None)
    w.add_argument("--op", default="upsert")
    w.add_argument("--db", default=None, help="override DB path (scratch testing)")
    w.add_argument("--no-create", action="store_true", help="create_ok=False (resolve-only identity)")
    w.add_argument("--dry-run", action="store_true",
                   help="resolve + report only; nothing staged or written")
    args = ap.parse_args()
    if args.cmd != "write":
        print("bus targets:", sorted(HANDLERS))
        return 0
    from _db import connect
    conn = connect(args.db) if args.db else connect()
    payload = json.loads(args.payload)
    nk = json.loads(args.natural_key) if args.natural_key else None
    if args.dry_run:
        del _DEFERRED_JUDGMENTS[:]
        pid, trace = _resolve_identity(conn, payload, nk or {}, create_ok=False)
        del _DEFERRED_JUDGMENTS[:]  # dry-run enqueues nothing
        print(json.dumps({"dry_run": True, "target": args.target,
                          "resolved_person_id": pid, "trace": trace}, indent=1, default=str))
        return 0
    res = write(conn, target_table=args.target, payload=payload,
                submitted_by=args.submitted_by, natural_key=nk, op=args.op,
                source_table=args.source_table, source_id=args.source_id,
                source_quote=args.source_quote, create_ok=not args.no_create)
    print(json.dumps(res, indent=1, default=str))
    return 0 if res.get("status") == "promoted" else 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli())
