"""Identity and email verification before any cold message goes out.

The failure this exists for: a scraped candidate list where one row in five was
the wrong person of the same name (a different researcher, a different org, a
dead address). Every cold wave is now verified per person first.

Offline mode (default) matches a name and organisation against the local
candidates table and reports the confidence of the best match and whether a
second, close match exists (the wrong-person risk). Provider mode (`--provider exa`)
is a hook for an external lookup; it does nothing unless EXA_API_KEY is set, and
it only ever adds evidence for a person to review, never a decision.

Usage:
    python sourcing/pipeline/verify.py check --name "Ada Example" --org "Example Institute" [--email a@example.org] [--record]
    python sourcing/pipeline/verify.py batch --search SEARCH [--record]     verify every prospect in a search
    python sourcing/pipeline/verify.py log [--candidate ID]                  show the verification log
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _db  # noqa: E402

MATCH_AT = 0.85       # best-match ratio at or above this counts as a match
RISK_GAP = 0.08       # a second match within this gap of the best is wrong-person risk


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower().strip(), (b or "").lower().strip()).ratio()


def local_match(conn, name: str, org: str | None, email: str | None) -> dict:
    """Score every candidate against the query; return the verdict and the top matches."""
    scored = []
    for r in conn.execute("SELECT id, name, org, email, headline FROM candidates"):
        s_name = _ratio(name, r["name"])
        s_org = _ratio(org, r["org"]) if org and r["org"] else 0.0
        s_email = 1.0 if email and r["email"] and email.lower() == r["email"].lower() else 0.0
        score = max(s_email, 0.7 * s_name + 0.3 * s_org) if org else max(s_email, s_name)
        scored.append((round(score, 3), r))
    scored.sort(key=lambda x: -x[0])
    top = scored[:3]
    best = top[0][0] if top else 0.0
    second = top[1][0] if len(top) > 1 else 0.0
    if best >= MATCH_AT and (best - second) >= RISK_GAP:
        verdict = "match"
    elif best >= MATCH_AT:
        verdict = "wrong-person-risk"
    else:
        verdict = "no-match"
    return {
        "verdict": verdict, "confidence": best,
        "matches": [{"id": r["id"], "name": r["name"], "org": r["org"], "email": r["email"], "score": s} for s, r in top],
        "note": ("two candidates score alike; confirm with a second signal (personal site, publication, mutual contact)"
                 if verdict == "wrong-person-risk" else None),
    }


def provider_lookup(name: str, org: str | None) -> dict | None:
    """External evidence hook. Returns None when no key is configured."""
    key = os.environ.get("EXA_API_KEY")
    if not key:
        return None
    body = json.dumps({"query": f"{name} {org or ''}".strip(), "numResults": 3, "contents": {"highlights": True}}).encode()
    req = urllib.request.Request("https://api.exa.ai/search", data=body, method="POST",
                                 headers={"x-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception as e:  # network or auth failure: report, do not decide
        return {"error": f"{type(e).__name__}: {e}"}
    return {"results": [{"title": x.get("title"), "url": x.get("url")} for x in data.get("results", [])]}


def record(conn, candidate_id: int | None, method: str, query: str, res: dict) -> None:
    conn.execute("INSERT INTO verification_log (candidate_id, method, query, confidence, verdict, note) VALUES (?,?,?,?,?,?)",
                 (candidate_id, method, query, res.get("confidence"), res["verdict"], res.get("note")))
    conn.commit()


def check(conn, name, org, email, provider, do_record) -> dict:
    res = local_match(conn, name, org, email)
    if provider == "exa":
        ext = provider_lookup(name, org)
        res["provider"] = ext if ext is not None else "skipped: EXA_API_KEY not set"
    if do_record:
        cid = res["matches"][0]["id"] if res["matches"] and res["verdict"] != "no-match" else None
        record(conn, cid, "local-match" if provider != "exa" else "provider:exa", f"{name} | {org or ''} | {email or ''}", res)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Identity and email verification before cold outreach")
    ap.add_argument("--db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check"); p.add_argument("--name", required=True); p.add_argument("--org")
    p.add_argument("--email"); p.add_argument("--provider", choices=["exa"]); p.add_argument("--record", action="store_true")
    p = sub.add_parser("batch"); p.add_argument("--search", required=True); p.add_argument("--provider", choices=["exa"])
    p.add_argument("--record", action="store_true")
    p = sub.add_parser("log"); p.add_argument("--candidate", type=int)
    a = ap.parse_args(argv)
    conn = _db.connect(a.db)
    if a.cmd == "check":
        print(json.dumps(check(conn, a.name, a.org, a.email, a.provider, a.record), indent=2))
    elif a.cmd == "batch":
        rows = conn.execute("SELECT c.id, c.name, c.org, c.email FROM candidate_roles r JOIN candidates c ON c.id=r.candidate_id "
                            "WHERE r.search=? AND r.status='prospect'", (a.search,)).fetchall()
        counts = {}
        for r in rows:
            res = check(conn, r["name"], r["org"], r["email"], a.provider, a.record)
            counts[res["verdict"]] = counts.get(res["verdict"], 0) + 1
            print(f"{r['name']:26} {res['verdict']:18} {res['confidence']:.2f}")
        print(json.dumps(counts))
    elif a.cmd == "log":
        q = "SELECT v.checked_at, c.name, v.method, v.verdict, v.confidence, v.note FROM verification_log v LEFT JOIN candidates c ON c.id=v.candidate_id"
        params = ()
        if a.candidate:
            q += " WHERE v.candidate_id=?"; params = (a.candidate,)
        for r in conn.execute(q + " ORDER BY v.checked_at DESC", params):
            print(f"{r['checked_at']}  {str(r['name'] or '-'):26} {r['method']:14} {r['verdict']:18} {r['confidence'] or 0:.2f}  {r['note'] or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
