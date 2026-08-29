"""Seed 20 fictional candidates for the sourcing demo through the kit's canonical
write path (the steward bus), so every row carries an actor and a source.

    python examples/sourcing/seed_candidates.py          # seed (idempotent)
    python examples/sourcing/seed_candidates.py --reset  # remove everything the demo wrote

All names, employers and facts are invented. Emails are on example.org.
Each candidate becomes: a people row (tagged demo:sourcing), a person_emails row,
a person entity (minted by the bus as person-<id>), an org entity, a works_at edge
with valid_from, typed attributes with a source pointer, and one or two observations.
"""
from __future__ import annotations

import sys

from _common import DEMO_ACTOR, DEMO_TAG, connect, demo_people

import steward_bus as bus

SOURCE_TABLE = "demo_intake_form"   # the fictional intake form every attribute cites
FORM_DATE = "2026-08"

# name, email, headline, location, uk_eligibility, years, bookkeeping, hr, automation,
# offsite, ai_safety, org_size, org (fictional), summary, observations
CANDIDATES = [
    ("Ada Okonkwo", "ada.okonkwo@example.org", "Operations Manager at a 25-person policy research nonprofit", "London, UK", "based_in_uk", 5, "yes", "yes", "Xero, Airtable, Zapier", "yes", "some", "under_30", "Northbank Policy Institute", "Ran finance and people ops for a 25-person policy nonprofit for four years; moved bookkeeping to Xero and built the leave tracker in Airtable.", ["Closed the books on time every month for three years, audit passed with no findings."]),
    ("Tomasz Wierzbicki", "tomasz.w@example.org", "Freelance operations consultant for startups", "Lisbon, Portugal", "willing_to_relocate", 4, "no", "no", "Airtable, Zapier, Make, Claude", "partial", "some", "under_30", "Independent", "Built Airtable and Zapier systems for six startups; has never owned payroll or benefits end to end.", ["Clients describe the automations as reliable; one client noted the handover documentation was thin."]),
    ("Priya Raman", "priya.raman@example.org", "Finance and HR lead at a 40-person biotech startup", "Cambridge, UK", "based_in_uk", 6, "yes", "yes", "Xero, Gusto exports to Sheets", "no", "none", "30_to_100", "Helix Analytics Ltd", "Owns bookkeeping, payroll and benefits at a 40-person biotech; no automation beyond spreadsheets.", ["Reliable on deadlines; prefers established tools over new ones."]),
    ("Daniel Achebe", "daniel.achebe@example.org", "Researcher at an AI policy think tank", "Berkeley, USA", "willing_to_relocate", 4, "no", "no", "Notion", "yes", "active", "30_to_100", "Center for Model Governance", "Policy researcher who organised two 100-person conferences at university; no financial systems experience.", ["Deeply engaged in AI governance; strong writer."]),
    ("Mei-Ling Chao", "meiling.chao@example.org", "Co-founder and operations lead, 4-person AI governance education nonprofit", "Cambridge, UK", "based_in_uk", 2, "yes", "partial", "Zapier, ChatGPT", "yes", "active", "under_30", "Governance Bridge CIC", "Co-founded a 4-person nonprofit: did the books, ran payroll for three staff, organised two multi-day events and the fundraising.", ["Books were clean at the first annual filing; benefits enrolment was outsourced to an accountant."]),
    ("Samuel Ortiz", "samuel.ortiz@example.org", "Operations associate at a 200-person edtech company", "Manchester, UK", "based_in_uk", 3, "no", "yes", "Workday, Zapier", "no", "none", "over_100", "Brightline Learning plc", "Runs HR systems for a 200-person company; finance sits with a separate team.", ["Detail-oriented; has not worked in a small organisation."]),
    ("Hannah Fischer", "hannah.fischer@example.org", "Executive assistant and office manager, 15-person research lab", "Berlin, Germany", "willing_to_relocate", 7, "yes", "yes", "Excel", "yes", "some", "under_30", "Institut für Systemforschung gGmbH", "Seven years running a 15-person lab's admin: bookkeeping, payroll, contracts, the annual retreat.", ["Colleagues call her the reason the lab runs; no automation experience beyond spreadsheets."]),
    ("Kwame Mensah", "kwame.mensah@example.org", "Chief of staff at a 10-person climate nonprofit", "London, UK", "based_in_uk", 5, "yes", "yes", "Airtable, Zapier, Python scripts", "yes", "none", "under_30", "Tidewater Climate Trust", "Chief of staff who owns finance, people and the offsite at a 10-person nonprofit; automated grant reporting with Airtable and Python.", ["On time, no errors, audit passed 2025."]),
    ("Yuki Tanaka", "yuki.tanaka@example.org", "Programme coordinator at a 60-person foundation", "Oxford, UK", "based_in_uk", 3, "no", "no", "Airtable", "yes", "active", "30_to_100", "Alder Foundation", "Coordinates programmes and events; finance and HR are separate teams.", ["Organised the 2025 grantee retreat for 40 people; reliable."]),
    ("Lucas Moreau", "lucas.moreau@example.org", "Bookkeeper, then operations manager at a 20-person design agency", "Bristol, UK", "based_in_uk", 8, "yes", "yes", "Xero, Zapier", "partial", "none", "under_30", "Studio Meridian", "Eight years from bookkeeper to operations manager; owns payroll, benefits and Xero.", ["Calm under deadline; no interest in the mission expressed."]),
    ("Fatima Al-Sayed", "fatima.alsayed@example.org", "Operations lead at a 30-person AI safety research org", "London, UK", "based_in_uk", 4, "yes", "yes", "Xero, Airtable, Zapier, Claude", "yes", "active", "30_to_100", "Lattice Alignment Research", "Owns finance, HR and the annual retreat at a 30-person AI safety org; built the expense pipeline in Airtable with an LLM step.", ["Closed the books on time; audit passed with no findings; ran the 2026 retreat for 28 people."]),
    ("Oliver Grant", "oliver.grant@example.org", "Management consultant", "London, UK", "based_in_uk", 5, "no", "no", "Excel, PowerPoint", "no", "some", "over_100", "Harcourt Advisory", "Strategy consultant; no hands-on operations ownership.", ["Strong analytical writer; has never run a system."]),
    ("Sofia Petrova", "sofia.petrova@example.org", "Operations and finance manager, 18-person nonprofit", "Edinburgh, UK", "based_in_uk", 4, "yes", "yes", "Xero, Notion", "no", "some", "under_30", "Northlight Trust", "Runs finance and people ops for an 18-person charity; has not organised an offsite.", ["Reliable; clean books; reported a payroll error she caught before it went out."]),
    ("Ibrahim Diallo", "ibrahim.diallo@example.org", "People operations specialist, 300-person fintech", "Dublin, Ireland", "willing_to_relocate", 6, "no", "yes", "Workday, Zapier", "no", "none", "over_100", "Cormorant Payments", "Owns benefits and leave for a 300-person company; finance is separate.", ["Process-driven; large-company habits."]),
    ("Elena Rossi", "elena.rossi@example.org", "Founder's associate at a 6-person AI governance startup", "London, UK", "based_in_uk", 2, "yes", "yes", "Airtable, Zapier, GPT", "partial", "active", "under_30", "Clearsight Governance Ltd", "Does everything operational at a 6-person startup including the books and the two-person payroll.", ["Two years in; the books were reviewed by an accountant quarterly with minor corrections."]),
    ("Noah Kim", "noah.kim@example.org", "Events manager, conference company", "London, UK", "based_in_uk", 5, "no", "no", "Airtable", "yes", "none", "30_to_100", "Summit Works Ltd", "Runs 20 events a year; no finance or HR ownership.", ["Excellent logistics; not the profile for finance."]),
    ("Amara Nwosu", "amara.nwosu@example.org", "Operations lead, 12-person global health nonprofit", "Remote, Nigeria", "needs_sponsorship_outside_uk", 5, "yes", "yes", "Xero, Zapier", "yes", "some", "under_30", "Meridian Health Initiative", "Owns finance, HR and the annual retreat at a 12-person nonprofit; not UK-based and no relocation stated.", ["Audit passed 2024 and 2025."]),
    ("Jonas Lindqvist", "jonas.lindqvist@example.org", "Finance manager, 50-person software company", "Stockholm, Sweden", "willing_to_relocate", 9, "yes", "no", "Fortnox, Excel", "no", "none", "30_to_100", "Vinterhavn AB", "Nine years in finance; HR sits with a separate team; above the experience range.", ["Precise; slow to adopt new tools."]),
    ("Chloe Bennett", "chloe.bennett@example.org", "Operations manager, 9-person AI safety field-building org", "Remote, UK", "based_in_uk", 3, "yes", "yes", "Airtable, Zapier, Claude, Python", "yes", "active", "under_30", "Signal Field Collective", "Runs finance, people and two retreats a year at a 9-person org; automated onboarding with Zapier and an LLM step.", ["Closed the books on time; audit passed; onboarding automation cut manual steps from 14 to 3."]),
    ("Rafael Costa", "rafael.costa@example.org", "Junior operations assistant", "London, UK", "based_in_uk", 1, "partial", "no", "Notion, Zapier", "no", "some", "under_30", "Harbour Analytics", "One year assisting with bookkeeping data entry; has not owned payroll.", ["Keen; under the experience range."]),
]


def seed(conn) -> dict:
    stats = {"people": 0, "attributes": 0, "edges": 0, "observations": 0, "quarantined": 0}
    claims: dict = {}
    for i, c in enumerate(CANDIDATES, start=1):
        (name, email, headline, location, uk, years, book, hr, auto, offsite, ai, org_size, org, summary, obs) = c
        src_id = f"form-{i:03d}"
        prov = dict(source_table=SOURCE_TABLE, source_id=src_id, source_quote=f"self-reported intake form, {FORM_DATE}")
        res = bus.write(conn, target_table="people", submitted_by=DEMO_ACTOR, natural_key={"email": email},
                        payload={"name": name, "email": email, "headline": headline, "location": location,
                                 "years_experience": years, "summary": summary, "tags": DEMO_TAG,
                                 "lifecycle_status": "demo", "is_real_person": 0},
                        batch_claims=claims, **prov)
        if res.get("status") != "promoted":
            stats["quarantined"] += 1
            continue
        pid = res["person_id"]
        stats["people"] += 1
        bus.write(conn, target_table="person_emails", submitted_by=DEMO_ACTOR,
                  natural_key={"person_id": pid, "email": email},
                  payload={"person_id": pid, "email": email, "is_primary": 1, "source": DEMO_ACTOR}, **prov)
        org_id = "org-demo-" + "".join(ch if ch.isalnum() else "-" for ch in org.lower()).strip("-")
        bus.write(conn, target_table="entities", submitted_by=DEMO_ACTOR, natural_key={"id": org_id},
                  payload={"id": org_id, "type": "org", "name": org, "source": DEMO_ACTOR,
                           "data": {"fictional": True, "demo": DEMO_TAG}}, **prov)
        e = bus.write(conn, target_table="edges", submitted_by=DEMO_ACTOR,
                      natural_key={"source_id": f"person-{pid}", "target_id": org_id, "relation": "works_at",
                                   "valid_from": f"{2026 - years}-01-01"},
                      payload={"fact": f"{name} works at {org} ({headline})", "confidence": 0.9}, **prov)
        stats["edges"] += 1 if e.get("status") == "promoted" else 0
        attrs = {
            "owned_bookkeeping_payroll": book, "owned_hr_systems": hr, "automation_tools": auto,
            "organised_multiday_offsite": offsite, "ai_safety_engagement": ai,
            "largest_org_size_owned": org_size, "uk_eligibility": uk, "years_relevant_experience": str(years),
        }
        for attr, val in attrs.items():
            a = bus.write(conn, target_table="attribute", submitted_by=DEMO_ACTOR,
                          natural_key={"entity_id": f"person-{pid}", "attr": attr},
                          payload={"entity_id": f"person-{pid}", "attr": attr, "value": val, "value_type": "text",
                                   "confidence": 0.8, "scope": "global", "visibility": "internal",
                                   "asserter_entity_id": f"person-{pid}"}, **prov)
            stats["attributes"] += 1 if a.get("status") == "promoted" else 0
        for j, text in enumerate(obs, start=1):
            o = bus.write(conn, target_table="observation", submitted_by=DEMO_ACTOR,
                          natural_key={"person_id": pid, "content": text},
                          payload={"person_id": pid, "subject": name, "content": text, "source": DEMO_ACTOR,
                                   "confidence": "medium"},
                          source_table="demo_reference_call", source_id=f"{src_id}-ref{j}",
                          source_quote="reference call note (fictional)")
            stats["observations"] += 1 if o.get("status") == "promoted" else 0
    conn.commit()
    return stats


def reset(conn) -> dict:
    """Remove every row the demo wrote. Order matters for foreign keys."""
    rows = demo_people(conn)
    pids = [r["id"] for r in rows]
    ents = [f"person-{p}" for p in pids]
    n = {"people": len(pids)}
    q = ",".join("?" * len(pids)) or "NULL"
    qe = ",".join("?" * len(ents)) or "NULL"
    n["observations"] = conn.execute(f"DELETE FROM observations WHERE person_id IN ({q})", pids).rowcount
    n["attributes"] = conn.execute(f"DELETE FROM attributes WHERE entity_id IN ({qe})", ents).rowcount
    n["edges"] = conn.execute(f"DELETE FROM edges WHERE source_id IN ({qe}) OR target_id LIKE 'org-demo-%'", ents).rowcount
    n["entities"] = conn.execute(f"DELETE FROM entities WHERE id IN ({qe}) OR id LIKE 'org-demo-%'", ents).rowcount
    for t in ("person_emails", "person_identities", "person_identifiers"):
        try:
            conn.execute(f"DELETE FROM {t} WHERE person_id IN ({q})", pids)
        except Exception:  # noqa: BLE001  table shape varies; the people delete below is what matters
            pass
    conn.execute(f"DELETE FROM people WHERE id IN ({q})", pids)
    n["staging"] = conn.execute("DELETE FROM staging WHERE submitted_by=?", (DEMO_ACTOR,)).rowcount
    n["learnings"] = conn.execute("DELETE FROM learnings WHERE source=?", (DEMO_ACTOR,)).rowcount
    conn.commit()
    return n


def main(argv) -> int:
    conn = connect()
    conn.row_factory = __import__("sqlite3").Row
    if "--reset" in argv:
        print("reset:", reset(conn))
        return 0
    before = len(demo_people(conn))
    stats = seed(conn)
    after = len(demo_people(conn))
    print(f"seeded (people before={before}, after={after}): {stats}")
    if stats["quarantined"]:
        print("some rows were quarantined; see review_queue and staging.error")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
