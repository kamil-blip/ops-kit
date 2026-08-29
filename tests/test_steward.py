"""The single write path: actor attribution, provenance stamping, idempotency."""
import sqlite3

import pytest

import _db
import paths
import steward_bus
from audit_actor import actor_scope


@pytest.fixture(scope="module", autouse=True)
def blocking_gate(db_path):
    """The write gate is advisory until steward_config says blocking; these tests turn it on."""
    c = sqlite3.connect(str(db_path))
    c.execute("INSERT OR REPLACE INTO steward_config (key, value) VALUES ('write_gate_mode', 'blocking')")
    c.commit(); c.close()
    yield
    c = sqlite3.connect(str(db_path))
    c.execute("DELETE FROM steward_config WHERE key='write_gate_mode'")
    c.commit(); c.close()


@pytest.fixture
def kconn():
    c = _db.connect(str(paths.DB_PATH))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_raw_insert_without_actor_is_rejected(kconn):
    """With the gate set to blocking, a people write outside actor_scope is refused."""
    with pytest.raises(sqlite3.IntegrityError, match="NULL-actor"):
        kconn.execute("INSERT INTO people (name, email) VALUES ('No Actor', 'noactor@example.org')")


def test_insert_inside_actor_scope_is_attributed(kconn):
    """The same write inside actor_scope succeeds and the CDC log records the actor."""
    with actor_scope(kconn, "test:steward", source_ref="pytest"):
        kconn.execute("INSERT INTO people (name, email) VALUES ('Scoped Writer', 'scoped@example.org')")
    kconn.commit()
    pid = kconn.execute("SELECT id FROM people WHERE email='scoped@example.org'").fetchone()[0]
    row = kconn.execute("SELECT actor FROM cdc_log WHERE table_name='people' AND row_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    assert row is not None and row["actor"] == "test:steward"


def test_bus_write_promotes_and_stamps_provenance(kconn):
    """steward_bus.write stages, publishes and records source_table/source_id on the row."""
    res = steward_bus.write(
        kconn, target_table="people",
        payload={"name": "Bus Fictional", "email": "bus@example.org", "headline": "test subject"},
        natural_key={"email": "bus@example.org"}, submitted_by="pytest",
        source_table="emails", source_id="msg-fictional-1", source_quote="Bus Fictional wrote in")
    assert res["status"] == "promoted", res
    st = kconn.execute("SELECT status, submitted_by, source_table FROM staging WHERE id=?", (res["staging_id"],)).fetchone()
    assert st["status"] == "promoted" and st["submitted_by"] == "pytest" and st["source_table"] == "emails"
    person = kconn.execute("SELECT id, name FROM people WHERE email='bus@example.org'").fetchone()
    assert person is not None and person["name"] == "Bus Fictional"


def test_bus_write_is_idempotent(kconn):
    """The same payload written twice yields one person and a dedup on the second call."""
    kw = dict(target_table="people",
              payload={"name": "Twice Fictional", "email": "twice@example.org"},
              natural_key={"email": "twice@example.org"}, submitted_by="pytest",
              source_table="emails", source_id="msg-fictional-2")
    first = steward_bus.write(kconn, **kw)
    second = steward_bus.write(kconn, **kw)
    assert first["status"] == "promoted"
    assert second.get("dedup") is True or second["status"] == "promoted"
    n = kconn.execute("SELECT COUNT(*) FROM people WHERE email='twice@example.org'").fetchone()[0]
    assert n == 1


def test_bus_rejects_unknown_target(kconn):
    """A write to a table the bus does not own is rejected, not applied."""
    res = steward_bus.write(kconn, target_table="not_a_table", payload={"x": 1}, submitted_by="pytest")
    assert res["status"] == "rejected"
    assert "unknown_target" in res["error"]
