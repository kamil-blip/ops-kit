"""The learning loop: a rule goes in, comes back by keyword, and can be made always-on."""
import subprocess

import pytest

import graduated_rules
import learnings_retrieval

LID = "LRN-TEST-001"
TITLE = "Zebrafish tanks need the pump checked before a weekend"
DESC = "WHEN: a zebrafish tank is left over a weekend. THEN: check the pump on Friday. BECAUSE: one failed once."


@pytest.fixture(scope="module", autouse=True)
def learning(db_path):
    import sqlite3
    c = sqlite3.connect(str(db_path))
    c.execute("INSERT OR IGNORE INTO learnings (learning_id, title, description, apply_when, priority, status, memory_type, source) "
              "VALUES (?, ?, ?, 'zebrafish tank pump weekend', 'medium', 'active', 'learning', 'pytest')",
              (LID, TITLE, DESC))
    c.commit()
    c.close()


def test_query_cli_finds_learning_by_keyword(py, env, repo_root):
    """`query.py learnings <word>` returns the captured rule."""
    r = subprocess.run([py, str(repo_root / "tools" / "query.py"), "learnings", "zebrafish"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "Zebrafish tanks" in r.stdout


def test_graduated_rendering_includes_top_tier_rule(db_path):
    """Setting residency='graduated' puts the rule into the always-on block."""
    import sqlite3
    c = sqlite3.connect(str(db_path))
    c.execute("UPDATE learnings SET residency='graduated' WHERE learning_id=?", (LID,))
    c.commit()
    c.close()
    rows = graduated_rules.fetch()
    assert any(r[1] == LID for r in rows)
    block = graduated_rules.render(rows)
    assert "Zebrafish tanks" in block
    assert graduated_rules.BEGIN in block


def test_retrieval_never_raises_and_surfaces_the_graduated_rule(db_path):
    """retrieve() returns the always-on rule for a matching prompt without raising."""
    results = learnings_retrieval.retrieve("the zebrafish tank over the weekend", embed=False)
    assert isinstance(results, list)
    assert any((r.get("learning_id") == LID) or (r.get("title") == TITLE) for r in results), results


def test_format_for_injection_marks_graduated_rules():
    """A graduated learning renders with the 'Rule:' prefix; an ordinary one does not."""
    rule = {"title": TITLE, "description": DESC, "residency": "graduated", "priority": "medium"}
    plain = {"title": "Ordinary note", "description": "", "residency": "retrievable", "priority": "medium"}
    lines = learnings_retrieval.format_for_injection([rule, plain])
    assert lines[0].startswith("Rule: " + TITLE)
    assert lines[1] == "Related learning: Ordinary note"
    assert learnings_retrieval.format_for_injection([]) == []
