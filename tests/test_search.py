"""Search: FTS-fed RRF ranking over people, and the dossier's not-found path."""
import sqlite3

import pytest

import _db
import paths
import person_dossier
import rrf_search
from audit_actor import actor_scope

PEOPLE = [
    ("Ada Fictional", "ada.f@example.org", "quantum error correction researcher"),
    ("Ben Fictional", "ben.f@example.org", "operations lead for a climate nonprofit"),
    ("Cy Fictional", "cy.f@example.org", "compiler engineer, verification tooling"),
]


@pytest.fixture(scope="module", autouse=True)
def seeded():
    c = _db.connect(str(paths.DB_PATH))
    with actor_scope(c, "test:search", source_ref="pytest"):
        for name, email, headline in PEOPLE:
            c.execute("INSERT OR IGNORE INTO people (name, email, headline) VALUES (?, ?, ?)", (name, email, headline))
    c.commit()
    c.close()


def test_fts_expr_builds_a_match_expression():
    """A free-text query becomes a non-empty FTS5 MATCH expression."""
    assert rrf_search.fts_expr("quantum correction")


def test_rrf_ranks_keyword_match_first():
    """The person whose headline matches the query outranks the others."""
    rows = rrf_search.rrf_search("people", "quantum", k=5)
    names = [r["name"] for r in rows]
    assert names and names[0] == "Ada Fictional", names
    assert "Ben Fictional" not in names


def test_rrf_returns_empty_for_no_match():
    """A query that matches nothing returns an empty list, not an error."""
    assert rrf_search.rrf_search("people", "xylophone", k=5) == []


def test_rrf_scores_are_descending():
    """Fused scores come back sorted, best first."""
    rows = rrf_search.rrf_search("people", "fictional", k=5)
    scores = [r["rrf_score"] for r in rows]
    assert len(rows) >= 3 and scores == sorted(scores, reverse=True)


def test_dossier_unknown_person_does_not_raise():
    """Asking for someone who is not in the database returns the not-found text."""
    out = person_dossier.dossier("Nobody Atall")
    assert "No person found" in out


def test_dossier_known_person_mentions_their_headline():
    """A dossier for a seeded person includes what the database knows about them."""
    out = person_dossier.dossier("Ada Fictional")
    assert "Ada Fictional" in out
    assert "quantum" in out.lower()


def test_vector_arm_skips_cleanly_without_a_model():
    """With no embeddings stored, the vector branch is skipped and FTS still answers."""
    pytest.importorskip("sqlite_vec")
    rows = rrf_search.rrf_search("people", "compiler", k=3)
    assert rows and rows[0]["name"] == "Cy Fictional"
