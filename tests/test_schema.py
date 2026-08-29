"""The shipped schema: applies clean, starts empty, and its triggers behave."""
import sqlite3

import pytest

CORE_TABLES = ["people", "person_emails", "entities", "edges", "learnings",
               "action_items", "email_threads", "audit_events", "staging", "_audit_context"]


def _tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_schema_applies_and_integrity_ok(fresh_schema):
    """db/schema.sql applies to an empty file and passes integrity_check."""
    assert fresh_schema.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert fresh_schema.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("table", CORE_TABLES)
def test_core_table_exists(fresh_schema, table):
    """Every table the kit's modules depend on is in the schema."""
    assert table in _tables(fresh_schema)


def test_fresh_database_has_zero_rows(fresh_schema):
    """A fresh install ships no data: init_db's own verifier reports OK."""
    assert init_db_verify(fresh_schema) == 0


def init_db_verify(c):
    import init_db
    return init_db.verify(c, "test")


def test_fts_shadow_tables_exist_for_declared_indexes(fresh_schema):
    """Each FTS5 virtual table has its content shadow tables created."""
    virtuals = [r[0] for r in fresh_schema.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL TABLE%'")]
    assert "people_fts" in virtuals
    tables = _tables(fresh_schema)
    for v in virtuals:
        assert f"{v}_data" in tables or f"{v}_info" in tables, f"no shadow tables for {v}"


def test_people_insert_populates_fts(fresh_schema):
    """Inserting a person makes them findable through people_fts (trigger-fed)."""
    c = fresh_schema
    c.execute("INSERT OR REPLACE INTO _audit_context (id, actor, source_ref) VALUES (1, 'test:schema', 'pytest')")
    c.execute("INSERT INTO people (name, email, headline) VALUES ('Ada Fictional', 'ada@example.org', 'quantum error correction')")
    c.commit()
    hit = c.execute("SELECT rowid FROM people_fts WHERE people_fts MATCH 'quantum'").fetchall()
    assert len(hit) == 1


def test_context_link_trigger_fires_when_entity_exists(fresh_schema):
    """An action item whose context_slug matches a ctx- entity gets an edge to it."""
    c = fresh_schema
    c.execute("INSERT OR REPLACE INTO _audit_context (id, actor, source_ref) VALUES (1, 'test:schema', 'pytest')")
    c.execute("INSERT INTO entities (id, type, name) VALUES ('ctx-sprint-x', 'event', 'Sprint X')")
    c.execute("INSERT INTO action_items (item_id, description, context_slug, source) "
              "VALUES ('AI-TEST-001', 'book the room', 'sprint-x', 'manual')")
    c.commit()
    edge = c.execute("SELECT relation FROM edges WHERE source_id='AI-TEST-001' AND target_id='ctx-sprint-x'").fetchone()
    assert edge is not None


def test_context_link_trigger_silent_when_entity_missing(fresh_schema):
    """No ctx- entity, no edge: the trigger must not invent a target."""
    c = fresh_schema
    c.execute("INSERT OR REPLACE INTO _audit_context (id, actor, source_ref) VALUES (1, 'test:schema', 'pytest')")
    c.execute("INSERT INTO action_items (item_id, description, context_slug, source) "
              "VALUES ('AI-TEST-002', 'call the venue', 'nowhere', 'manual')")
    c.commit()
    edges = c.execute("SELECT COUNT(*) FROM edges WHERE source_id='AI-TEST-002' AND target_id LIKE 'ctx-%'").fetchone()[0]
    assert edges == 0


def test_write_gate_blocks_unattributed_people_insert(fresh_schema):
    """With write_gate_mode=blocking, a people INSERT with no actor is refused."""
    c = fresh_schema
    c.execute("INSERT OR REPLACE INTO steward_config (key, value) VALUES ('write_gate_mode', 'blocking')")
    c.execute("DELETE FROM _audit_context")
    with pytest.raises(sqlite3.IntegrityError, match="write_gate"):
        c.execute("INSERT INTO people (name, email) VALUES ('Ghost Writer', 'ghost@example.org')")
