"""Resolve-by-tracing — the identity layer of the Write-Audit-Publish spine.

Lifted out as a standalone importable. Resolves an incoming person / org
reference to an existing canonical id BEFORE creating anything. This
lookup-before-create on natural keys (via person_identities) — NOT foreign keys —
is the real duplicate-prevention.

Hard rules:
  * Never match a single-token (first-name-only) identity alone
    (disambiguation_risk='high'); require >=2 attribute match or context.
  * On an ambiguous email/name (the same value mapping to >1 person — these are
    un-merged duplicates), return the LOWEST existing person_id and record the
    ambiguity in the trace. Never mint a new person for a value that already
    resolves to one.
  * Create a person only when the source gives contact info (an email) and
    nothing matches; stamp new identities so the next lookup finds them.
"""
from __future__ import annotations
import hashlib, json, re, sqlite3
from typing import Optional

PERSONAL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com",
    "proton.me", "protonmail.com", "gmx.com", "qq.com", "163.com", "me.com",
    "live.com", "aol.com", "mail.com", "yandex.com", "fastmail.com", "pm.me",
}


# ---------- normalization (must match how person_identities was populated) ----------
def normalize_email(e: Optional[str]) -> Optional[str]:
    if not e:
        return None
    e = e.strip().lower()
    return e or None


def is_valid_primary_email(e: Optional[str]) -> bool:
    """A structurally-valid address safe to store is_primary=1. Rejects
    None/empty, mailto:, 'Name <addr>' display form, comma/semicolon multi-address,
    embedded spaces, no-@ or double-@, and a domain with no dot / leading-or-trailing
    dot. Pure function; the write-path demotes (keeps the address, drops is_primary)
    rather than raising, so the keep-rule stays human."""
    if not e:
        return False
    e = e.strip()
    if not e or e.lower().startswith("mailto:"):
        return False
    if any(c in e for c in "<>,; "):
        return False
    if e.count("@") != 1:
        return False
    local, _, domain = e.partition("@")
    if not local or not domain:
        return False
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return False
    return True


def normalize_name(n: Optional[str]) -> Optional[str]:
    if not n:
        return None
    n = re.sub(r"\s+", " ", n.strip().lower())
    return n or None


def name_risk(n: Optional[str]) -> str:
    norm = normalize_name(n)
    if not norm:
        return "high"
    toks = norm.split()
    if len(toks) <= 1:
        return "high"
    if len(toks) == 2:
        return "medium"
    return "low"


def email_domain(e: Optional[str]) -> Optional[str]:
    e = normalize_email(e)
    if e and "@" in e:
        return e.split("@", 1)[1]
    return None


def source_key(*parts) -> str:
    """sha256(source:thread:msg ...) — a stable per-fact idempotency seed."""
    raw = ":".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- person ----------
def resolve_person(
    conn: sqlite3.Connection,
    *,
    email: Optional[str] = None,
    discord_id: Optional[str] = None,
    name: Optional[str] = None,
    org: Optional[str] = None,
) -> dict:
    """Return {'person_id': int|None, 'matched_on': str|None, 'ambiguous': bool,
    'candidates': [...], 'trace': [...]}. Never creates."""
    trace: list[str] = []

    # 1. email (strongest)
    ne = normalize_email(email)
    if ne:
        ids = _persons_for_email(conn, ne)
        trace.append(f"email={ne} -> {ids}")
        if ids:
            return _result(min(ids), "email", ids, trace)

    # 2. discord id (people.discord_user_id; person_identities discord values are unreliable)
    if discord_id:
        r = conn.execute(
            "SELECT id FROM people WHERE discord_user_id=? ORDER BY id", (str(discord_id),)
        ).fetchall()
        ids = [x[0] for x in r]
        trace.append(f"discord={discord_id} -> {ids}")
        if ids:
            return _result(min(ids), "discord", ids, trace)

    # 3. name (+ org context if ambiguous or high-risk)
    nn = normalize_name(name)
    if nn:
        risk = name_risk(nn)
        cand = [x[0] for x in conn.execute(
            "SELECT DISTINCT person_id FROM person_identities WHERE identity_type='name' AND normalized_value=?",
            (nn,)).fetchall()]
        if not cand:
            cand = [x[0] for x in conn.execute(
                "SELECT id FROM people WHERE lower(trim(name))=?", (nn,)).fetchall()]
        trace.append(f"name={nn} risk={risk} -> {cand}")
        if len(cand) == 1 and risk != "high":
            # The payload's email reached this step UNRESOLVED (step 1 would have
            # returned otherwise). If the name-matched candidate already has known
            # emails and the payload email is not among them, that email is
            # DISCONFIRMING — returning the name match here is exactly the
            # bare-name-overwrite failure mode this guard exists for. Refuse;
            # surface candidates for merge-review. A candidate with NO known email
            # keeps the match (classic fill-when-NULL enrichment, guarded again at
            # the bus).
            if ne and _email_contradicts(conn, cand[0], ne):
                trace.append(f"name-only match person_id={cand[0]} DISCONFIRMED by unresolved email {ne}")
                return {"person_id": None, "matched_on": None, "ambiguous": True,
                        "candidates": sorted(set(cand)), "trace": trace,
                        "email_disconfirmed": True}
            return _result(cand[0], "name", cand, trace)
        if len(cand) >= 1 and org:
            ctx = _filter_by_context(conn, cand, org)
            trace.append(f"context(org={org}) -> {ctx}")
            if len(ctx) == 1:
                if ne and _email_contradicts(conn, ctx[0], ne):
                    trace.append(f"name+context match person_id={ctx[0]} DISCONFIRMED by unresolved email {ne}")
                    return {"person_id": None, "matched_on": None, "ambiguous": True,
                            "candidates": sorted(set(ctx)), "trace": trace,
                            "email_disconfirmed": True}
                return _result(ctx[0], "name+context", ctx, trace)
        if len(cand) == 1 and risk == "high" and org:
            # single high-risk token but context confirmed above already; otherwise refuse
            pass
    return {"person_id": None, "matched_on": None, "ambiguous": False, "candidates": [], "trace": trace}


def _email_contradicts(conn: sqlite3.Connection, pid: int, ne: str) -> bool:
    """True when person pid has >=1 known email and ne is not one of them.
    No known emails -> not contradicting (the fill-when-NULL enrichment case)."""
    known = set()
    for q, p in (
        ("SELECT lower(trim(email)) FROM people WHERE id=? AND email IS NOT NULL", (pid,)),
        ("SELECT lower(trim(email)) FROM person_emails WHERE person_id=? AND email IS NOT NULL", (pid,)),
        ("SELECT normalized_value FROM person_identities WHERE person_id=? AND identity_type='email'", (pid,)),
    ):
        for row in conn.execute(q, p).fetchall():
            if row[0]:
                known.add(row[0])
    return bool(known) and ne not in known


def _persons_for_email(conn: sqlite3.Connection, ne: str) -> list[int]:
    ids: set[int] = set()
    for q, p in (
        ("SELECT person_id FROM person_identities WHERE identity_type='email' AND normalized_value=?", (ne,)),
        ("SELECT person_id FROM person_emails WHERE lower(trim(email))=?", (ne,)),
        ("SELECT id FROM people WHERE lower(trim(email))=?", (ne,)),
    ):
        for row in conn.execute(q, p).fetchall():
            if row[0] is not None:
                ids.add(int(row[0]))
    return sorted(ids)


def _filter_by_context(conn, cand, org) -> list[int]:
    keep = []
    for pid in cand:
        ok = False
        if org:
            no = normalize_name(org)
            if conn.execute(
                "SELECT 1 FROM people WHERE id=? AND (lower(headline) LIKE ? OR lower(experience) LIKE ? OR lower(summary) LIKE ?)",
                (pid, f"%{no}%", f"%{no}%", f"%{no}%")).fetchone():
                ok = True
        if ok:
            keep.append(pid)
    return keep


def _result(pid: int, matched_on: str, ids: list, trace: list) -> dict:
    return {
        "person_id": pid, "matched_on": matched_on,
        "ambiguous": len(set(ids)) > 1, "candidates": sorted(set(ids)), "trace": trace,
    }


def get_or_create_person(
    conn: sqlite3.Connection,
    *,
    email: Optional[str] = None,
    name: Optional[str] = None,
    discord_id: Optional[str] = None,
    org: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Resolve; if no match AND we have an email, create. Returns the resolve dict
    plus 'created': bool. Caller must have set the audit actor (steward_bus does).
    Stamps the new email+name into person_identities so the next lookup matches."""
    res = resolve_person(conn, email=email, discord_id=discord_id, name=name, org=org)
    if res["person_id"] is not None:
        res["created"] = False
        return res
    ne = normalize_email(email)
    if not ne:
        res["created"] = False
        res["trace"].append("no-match + no-email -> refuse create (quarantine)")
        return res
    # create
    cols = {"name": name, "email": ne, "discord_user_id": str(discord_id) if discord_id else None}
    if extra:
        cols.update({k: v for k, v in extra.items() if v is not None})
    keys = [k for k in cols if cols[k] is not None]
    cur = conn.execute(
        f"INSERT INTO people({','.join(keys)},created_at,updated_at) VALUES({','.join('?' for _ in keys)},datetime('now'),datetime('now'))",
        [cols[k] for k in keys],
    )
    pid = cur.lastrowid
    # stamp identities
    conn.execute(
        "INSERT OR IGNORE INTO person_identities(person_id,identity_type,identity_value,normalized_value,disambiguation_risk,source) "
        "VALUES(?,?,?,?,?,?)", (pid, "email", email, ne, "low", "steward_resolver"))
    if name:
        conn.execute(
            "INSERT OR IGNORE INTO person_identities(person_id,identity_type,identity_value,normalized_value,disambiguation_risk,source) "
            "VALUES(?,?,?,?,?,?)", (pid, "name", name, normalize_name(name), name_risk(name), "steward_resolver"))
    conn.execute(
        "INSERT OR IGNORE INTO person_emails(person_id,email,source,is_primary) VALUES(?,?,?,?)",
        (pid, ne, "steward_resolver", 1 if is_valid_primary_email(ne) else 0))
    # keep the derived identifier floor in step (same rule as steward_bus._sync_identifier_floor)
    conn.execute(
        "INSERT OR IGNORE INTO person_identifiers(person_id,id_type,id_value,id_value_norm,source_table) "
        "VALUES(?,?,?,?,'steward_resolver')", (pid, "email", email, ne))
    if discord_id:
        conn.execute(
            "INSERT OR IGNORE INTO person_identifiers(person_id,id_type,id_value,id_value_norm,source_table) "
            "VALUES(?,?,?,?,'steward_resolver')", (pid, "discord", str(discord_id), str(discord_id).strip().lower()))
    res["person_id"] = pid
    res["created"] = True
    res["matched_on"] = "created"
    res["trace"].append(f"created person_id={pid}")
    return res


# ---------- org ----------
def resolve_org(conn: sqlite3.Connection, *, name: Optional[str] = None, domain: Optional[str] = None) -> dict:
    trace = []
    nn = normalize_name(name)
    if nn:
        r = conn.execute(
            "SELECT id FROM entities WHERE type='org' AND lower(trim(name))=? ORDER BY id LIMIT 1", (nn,)
        ).fetchone()
        trace.append(f"org name={nn} -> {r[0] if r else None}")
        if r:
            return {"entity_id": r[0], "matched_on": "name", "trace": trace}
    if domain:
        d = email_domain(domain) or domain.strip().lower()
        if d not in PERSONAL_DOMAINS:
            r = conn.execute(
                "SELECT id FROM entities WHERE type='org' AND lower(data) LIKE ? ORDER BY id LIMIT 1", (f"%{d}%",)
            ).fetchone()
            trace.append(f"org domain={d} -> {r[0] if r else None}")
            if r:
                return {"entity_id": r[0], "matched_on": "domain", "trace": trace}
    return {"entity_id": None, "matched_on": None, "trace": trace}


if __name__ == "__main__":
    from _db import connect
    c = connect()
    print("self-test:")
    sample = c.execute(
        "SELECT email FROM people WHERE email IS NOT NULL AND email LIKE '%@%' ORDER BY id LIMIT 1").fetchone()
    if sample:
        print("  known email:", sample[0], "->", resolve_person(c, email=sample[0]))
    else:
        print("  (people table is empty; skipping known-email check)")
    print("  unknown email:", resolve_person(c, email="definitely-nobody-zzz@nowhere.example"))
