"""Where every candidate stands, and who needs a follow-up today.

Usage:
    python pipeline/tracker.py status [SEARCH]      funnel per search: worked, contacted,
                                                    confirmed, delivered, declined, rates
    python pipeline/tracker.py chase [SEARCH]       follow-up list in priority tiers
    python pipeline/tracker.py stale [--days 7]     non-terminal rows untouched for N days
    python pipeline/tracker.py reconcile [SEARCH]   cross-checks between the tables

The funnel definitions (also used by funnel.py):
    worked      every candidate_roles row for the search, whatever its state
    contacted   any state past prospect, except removed
    confirmed   confirmed, delivered, no_show or withdrew (they said yes at some point)
    delivered   delivered
    declined    declined, soft_declined, withdrew
    conversion  confirmed / contacted
    completion  delivered / confirmed, once the search's closes_at has passed
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _db  # noqa: E402
import states  # noqa: E402

CONTACTED = tuple(sorted(states.CONTACTED_STATES))
CONFIRMED = tuple(sorted(states.CONFIRMED_OR_LATER))
DECLINED = tuple(sorted(states.DECLINE_STATES))
NON_TERMINAL = tuple(sorted(states.ALL_STATES - states.TERMINAL_STATES - {"declined", "removed", "bounced"}))


def _q(n: int) -> str:
    return ",".join("?" * n)


def _fmt(s, width: int) -> str:
    s = "" if s is None else str(s)
    return (s[: width - 1] + "~") if len(s) > width else s.ljust(width)


def searches(conn, search: str | None):
    if search:
        rows = conn.execute("SELECT * FROM searches WHERE search=?", (search,)).fetchall()
        if not rows:
            raise SystemExit(f"unknown search: {search}")
        return rows
    return conn.execute("SELECT * FROM searches ORDER BY created_at").fetchall()


def funnel_for(conn, search: str) -> dict:
    def count(where: str, params=()):
        return conn.execute(f"SELECT COUNT(*) FROM candidate_roles WHERE search=? AND {where}",
                            (search, *params)).fetchone()[0]
    worked = count("1=1")
    contacted = count(f"status IN ({_q(len(CONTACTED))})", CONTACTED)
    confirmed = count(f"status IN ({_q(len(CONFIRMED))})", CONFIRMED)
    delivered = count("status = 'delivered'")
    declined = count(f"status IN ({_q(len(DECLINED))})", DECLINED)
    no_reply = count("status = 'no_reply'")
    open_ = count(f"status IN ({_q(len(states.ACTIVE_STATES))})", tuple(sorted(states.ACTIVE_STATES)))
    days = [r[0] for r in conn.execute(
        "SELECT julianday(confirmed_at) - julianday(contacted_at) FROM candidate_roles "
        "WHERE search=? AND confirmed_at IS NOT NULL AND contacted_at IS NOT NULL", (search,))]
    return {
        "search": search, "worked": worked, "contacted": contacted, "confirmed": confirmed,
        "delivered": delivered, "declined": declined, "no_reply": no_reply, "open": open_,
        "conversion": round(confirmed / contacted, 3) if contacted else None,
        "completion": round(delivered / confirmed, 3) if confirmed else None,
        "days_to_confirm": round(sum(days) / len(days), 1) if days else None,
    }


def cmd_status(conn, search: str | None) -> None:
    rows = searches(conn, search)
    print(f"{'search':28} {'need':>4} {'worked':>6} {'contact':>7} {'confirm':>7} {'deliver':>7} "
          f"{'decline':>7} {'noreply':>7} {'conv':>6} {'compl':>6} {'d2c':>5}")
    for s in rows:
        f = funnel_for(conn, s["search"])
        conv = f"{f['conversion']:.0%}" if f["conversion"] is not None else "-"
        compl = f"{f['completion']:.0%}" if f["completion"] is not None else "-"
        d2c = f"{f['days_to_confirm']:.1f}" if f["days_to_confirm"] is not None else "-"
        print(f"{_fmt(s['search'], 28)} {s['needed'] or '-':>4} {f['worked']:>6} {f['contacted']:>7} "
              f"{f['confirmed']:>7} {f['delivered']:>7} {f['declined']:>7} {f['no_reply']:>7} {conv:>6} {compl:>6} {d2c:>5}")
        if s["needed"] and f["confirmed"] < s["needed"]:
            gap = s["needed"] - f["confirmed"]
            print(f"  {gap} short of target; at this conversion, contact about "
                  f"{int(gap / f['conversion']) + 1 if f['conversion'] else gap * 3} more")
    stale = conn.execute(
        f"SELECT COUNT(*) FROM candidate_roles WHERE status IN ({_q(len(NON_TERMINAL))}) "
        f"AND julianday('now') - julianday(updated_at) > 7", NON_TERMINAL).fetchone()[0]
    print(f"\nstale: {stale} non-terminal rows untouched for more than 7 days (run `chase`)")


def cmd_chase(conn, search: str | None) -> None:
    where, params = ("AND r.search=?", (search,)) if search else ("", ())
    base = ("SELECT c.name, c.email, r.search, r.role, r.status, "
            "CAST(julianday('now') - julianday(r.updated_at) AS INTEGER) AS days_ago "
            "FROM candidate_roles r JOIN candidates c ON c.id = r.candidate_id WHERE 1=1 ")
    tiers = [
        ("1. replied interested or tentative, not yet confirmed: answer them today",
         "AND r.status IN ('interested','tentative')", ()),
        ("2. confirmed, nothing delivered, search closed: check in",
         "AND r.status='confirmed' AND EXISTS (SELECT 1 FROM searches s WHERE s.search=r.search "
         "AND s.closes_at IS NOT NULL AND s.closes_at < date('now'))", ()),
        ("3. sent or contacted more than 5 days ago, no reply: one follow-up, then no_reply",
         "AND r.status IN ('sent','contacted') AND julianday('now') - julianday(r.updated_at) > 5", ()),
        ("4. out-of-office bounce: retry after the return date",
         "AND r.status='sent-ooo'", ()),
        ("5. soft decline: re-approach for the next search, not this one",
         "AND r.status='soft_declined'", ()),
    ]
    total = 0
    for title, cond, extra in tiers:
        rows = conn.execute(base + cond + " " + where + " ORDER BY days_ago DESC", (*extra, *params)).fetchall()
        print(f"--- {title} [{len(rows)}]")
        for r in rows:
            print(f"  {_fmt(r['name'], 26)} {_fmt(r['email'], 30)} {_fmt(r['search'], 22)} "
                  f"{_fmt(r['role'], 8)} {_fmt(r['status'], 13)} {r['days_ago']:>3}d")
        total += len(rows)
    print(f"\n{total} people need a touch")


def cmd_stale(conn, days: int) -> None:
    rows = conn.execute(
        f"SELECT c.name, c.email, r.search, r.role, r.status, "
        f"CAST(julianday('now') - julianday(r.updated_at) AS INTEGER) AS days_ago "
        f"FROM candidate_roles r JOIN candidates c ON c.id=r.candidate_id "
        f"WHERE r.status IN ({_q(len(NON_TERMINAL))}) AND julianday('now') - julianday(r.updated_at) > ? "
        f"ORDER BY days_ago DESC", (*NON_TERMINAL, days)).fetchall()
    print(f"non-terminal rows untouched for more than {days} days: {len(rows)}")
    for r in rows:
        print(f"  {_fmt(r['name'], 26)} {_fmt(r['search'], 22)} {_fmt(r['role'], 8)} "
              f"{_fmt(r['status'], 13)} {r['days_ago']:>3}d")


def cmd_reconcile(conn, search: str | None) -> None:
    where, params = ("AND r.search=?", (search,)) if search else ("", ())
    checks = [
        ("confirmed without a recorded inbound acceptance (did they really say yes, or did we assume?)",
         "SELECT c.name, r.search, r.role FROM candidate_roles r JOIN candidates c ON c.id=r.candidate_id "
         "WHERE r.status IN ('confirmed','delivered') AND NOT EXISTS (SELECT 1 FROM outreach_log o "
         "WHERE o.candidate_id=r.candidate_id AND o.search=r.search AND o.direction='in') " + where),
        ("confirmed but no outbound message logged (they were never told)",
         "SELECT c.name, r.search, r.role FROM candidate_roles r JOIN candidates c ON c.id=r.candidate_id "
         "WHERE r.status IN ('confirmed','delivered') AND NOT EXISTS (SELECT 1 FROM outreach_log o "
         "WHERE o.candidate_id=r.candidate_id AND o.search=r.search AND o.direction='out') " + where),
        ("delivered without confirmed_at (state skipped)",
         "SELECT c.name, r.search, r.role FROM candidate_roles r JOIN candidates c ON c.id=r.candidate_id "
         "WHERE r.status='delivered' AND r.confirmed_at IS NULL " + where),
        ("same person active in two roles of one search (double-booked)",
         "SELECT c.name, r.search, GROUP_CONCAT(r.role) FROM candidate_roles r JOIN candidates c ON c.id=r.candidate_id "
         f"WHERE r.status IN ({_q(len(states.ACTIVE_STATES))}) " + where +
         " GROUP BY r.candidate_id, r.search HAVING COUNT(*) > 1"),
        ("cold outreach sent with no verification on record",
         "SELECT c.name, r.search, r.role FROM candidate_roles r JOIN candidates c ON c.id=r.candidate_id "
         "WHERE c.source IN ('scrape','search') AND r.status NOT IN ('prospect') AND NOT EXISTS "
         "(SELECT 1 FROM verification_log v WHERE v.candidate_id=c.id) " + where),
        ("dead state present (should never be written)",
         "SELECT c.name, r.search, r.role FROM candidate_roles r JOIN candidates c ON c.id=r.candidate_id "
         "WHERE r.status='invited' " + where),
    ]
    problems = 0
    for title, sql in checks:
        p = list(params) + (list(sorted(states.ACTIVE_STATES)) if "ACTIVE" in title else [])
        if "double-booked" in title:
            rows = conn.execute(sql, (*sorted(states.ACTIVE_STATES), *params)).fetchall()
        else:
            rows = conn.execute(sql, params).fetchall()
        print(f"--- {title} [{len(rows)}]")
        for r in rows:
            print("  " + " | ".join(str(x) for x in r))
        problems += len(rows)
    print(f"\n{problems} issue(s)" if problems else "\nclean")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Candidate pipeline tracker")
    ap.add_argument("--db", help="database path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status", help="funnel per search"); p.add_argument("search", nargs="?")
    p = sub.add_parser("chase", help="who needs a follow-up, by tier"); p.add_argument("search", nargs="?")
    p = sub.add_parser("stale", help="rows in the dead zone"); p.add_argument("--days", type=int, default=7)
    p = sub.add_parser("reconcile", help="cross-check the tables"); p.add_argument("search", nargs="?")
    a = ap.parse_args(argv)
    conn = _db.connect(a.db)
    if a.cmd == "status":
        cmd_status(conn, a.search)
    elif a.cmd == "chase":
        cmd_chase(conn, a.search)
    elif a.cmd == "stale":
        cmd_stale(conn, a.days)
    elif a.cmd == "reconcile":
        cmd_reconcile(conn, a.search)
    return 0


if __name__ == "__main__":
    sys.exit(main())
