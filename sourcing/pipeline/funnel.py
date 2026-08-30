"""The numbers a sourcing team asks for, computed from the tables.

Per search and overall: candidates worked, contacted, confirmed, delivered, declined,
no reply, decline rate, conversion (confirmed / contacted), completion (delivered /
confirmed), average days from first contact to confirmation, and where confirmed
people came from (source mix).

Usage:
    python sourcing/pipeline/funnel.py [SEARCH] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _db  # noqa: E402
import tracker  # noqa: E402


def source_mix(conn, search: str | None) -> dict:
    where, params = ("AND r.search=?", (search,)) if search else ("", ())
    rows = conn.execute(
        "SELECT COALESCE(c.source,'unknown') AS source, COUNT(*) AS n FROM candidate_roles r "
        "JOIN candidates c ON c.id=r.candidate_id WHERE r.status IN ('confirmed','delivered','no_show','withdrew') "
        f"{where} GROUP BY 1 ORDER BY 2 DESC", params).fetchall()
    return {r["source"]: r["n"] for r in rows}


def overall(conn, search: str | None) -> dict:
    searches = [s["search"] for s in tracker.searches(conn, search)]
    per = [tracker.funnel_for(conn, s) for s in searches]
    tot = {k: sum(p[k] for p in per) for k in ("worked", "contacted", "confirmed", "delivered", "declined", "no_reply", "open")}
    tot["conversion"] = round(tot["confirmed"] / tot["contacted"], 3) if tot["contacted"] else None
    tot["completion"] = round(tot["delivered"] / tot["confirmed"], 3) if tot["confirmed"] else None
    tot["decline_rate"] = round(tot["declined"] / tot["contacted"], 3) if tot["contacted"] else None
    d = [p["days_to_confirm"] for p in per if p["days_to_confirm"] is not None]
    tot["days_to_confirm"] = round(sum(d) / len(d), 1) if d else None
    tot["confirmed_source_mix"] = source_mix(conn, search)
    return {"searches": per, "total": tot}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Funnel numbers per search and overall")
    ap.add_argument("search", nargs="?"); ap.add_argument("--json", action="store_true"); ap.add_argument("--db")
    a = ap.parse_args(argv)
    conn = _db.connect(a.db)
    out = overall(conn, a.search)
    if a.json:
        print(json.dumps(out, indent=2)); return 0
    t = out["total"]
    print(f"worked {t['worked']}  contacted {t['contacted']}  confirmed {t['confirmed']}  delivered {t['delivered']}  "
          f"declined {t['declined']}  no reply {t['no_reply']}  still open {t['open']}")
    pct = lambda v: f"{v:.0%}" if v is not None else "-"  # noqa: E731
    print(f"conversion {pct(t['conversion'])}  completion {pct(t['completion'])}  decline rate {pct(t['decline_rate'])}  "
          f"days to confirm {t['days_to_confirm'] if t['days_to_confirm'] is not None else '-'}")
    print("confirmed by source: " + ", ".join(f"{k} {v}" for k, v in t["confirmed_source_mix"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
