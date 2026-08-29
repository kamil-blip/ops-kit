"""The task manager: add, urgency, resolve, snooze, focus."""
import re
import sqlite3
from datetime import date, timedelta

import pytest

import task_manager as tm

ID_RE = re.compile(r"AI-\d{8}-\d{3}")


def _add(desc, **kw):
    out = tm.add_item(desc, source="manual", **kw)
    m = ID_RE.search(str(out))
    assert m, f"add_item did not return an item id: {out!r}"
    return m.group(0)


def _row(item_id):
    c = tm.get_conn()
    try:
        return c.execute("SELECT * FROM action_items WHERE item_id=?", (item_id,)).fetchone()
    finally:
        c.close()


def test_add_creates_an_open_item():
    """add_item with the manual source writes a canonical OPEN row."""
    iid = _add("Confirm the projector booking with the venue manager")
    row = _row(iid)
    assert row is not None and row["status"] == "OPEN" and row["priority"] == "P2"


def test_urgency_prefers_p0_due_soon_over_p3_open_ended():
    """A P0 item due tomorrow scores higher than a P3 item with no due date."""
    soon = _add("Pay the venue deposit before the invoice deadline", priority="P0", due=(date.today() + timedelta(days=1)).isoformat())
    later = _add("Tidy the shared drive folder structure", priority="P3")
    tm.update_all_urgency_scores()
    assert _row(soon)["urgency_score"] > _row(later)["urgency_score"]


def test_resolve_marks_done_with_note():
    """resolve_item sets DONE, records the note and a completion time."""
    iid = _add("Send the thank-you note to the volunteer team")
    tm.resolve_item(iid, "sent Tuesday")
    row = _row(iid)
    assert row["status"] == "DONE" and row["resolution_note"] == "sent Tuesday" and row["completed_at"]


def test_snoozed_item_leaves_the_focus_list():
    """A snoozed item is hidden from focus until its snooze date."""
    iid = _add("Chase the late invoice with the finance contact")
    before = str(tm.get_focus_items(max_items=50, output_format="text"))
    assert iid in before
    tm.snooze_item(iid, (date.today() + timedelta(days=30)).isoformat(), "waiting on finance")
    after = str(tm.get_focus_items(max_items=50, output_format="text"))
    assert iid not in after


def test_unsnooze_brings_it_back():
    """unsnooze_item clears the snooze and the item reappears in focus."""
    iid = _add("Order name badges for the September workshop")
    tm.snooze_item(iid, (date.today() + timedelta(days=30)).isoformat(), "later")
    tm.unsnooze_item(iid)
    assert iid in str(tm.get_focus_items(max_items=50, output_format="text"))


def test_untrusted_source_is_routed_to_inbox_not_canonical():
    """An item from a non-canonical source lands in action_items_inbox for triage."""
    out = str(tm.add_item("Proposed by a script for triage review", source="wrap-up-2026-01-01", evidence_quote="verbatim line"))
    assert "inbox" in out.lower()
    c = tm.get_conn()
    try:
        n = c.execute("SELECT COUNT(*) FROM action_items WHERE description='Proposed by a script for triage review'").fetchone()[0]
    finally:
        c.close()
    assert n == 0
