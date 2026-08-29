-- FTS5 Sync Triggers Template
-- Copy and adapt for each new table with an FTS5 index.
-- Replace {TABLE} with the actual table name and {col1, col2, ...} with indexed columns.

-- Trigger: keep FTS5 in sync after INSERT
CREATE TRIGGER {TABLE}_fts_ai AFTER INSERT ON {TABLE} BEGIN
    INSERT INTO {TABLE}_fts(rowid, {col1}, {col2}, {col3})
    VALUES (new.rowid, new.{col1}, new.{col2}, new.{col3});
END;

-- Trigger: keep FTS5 in sync after DELETE
CREATE TRIGGER {TABLE}_fts_ad AFTER DELETE ON {TABLE} BEGIN
    INSERT INTO {TABLE}_fts({TABLE}_fts, rowid, {col1}, {col2}, {col3})
    VALUES ('delete', old.rowid, old.{col1}, old.{col2}, old.{col3});
END;

-- Trigger: keep FTS5 in sync after UPDATE
CREATE TRIGGER {TABLE}_fts_au AFTER UPDATE ON {TABLE} BEGIN
    INSERT INTO {TABLE}_fts({TABLE}_fts, rowid, {col1}, {col2}, {col3})
    VALUES ('delete', old.rowid, old.{col1}, old.{col2}, old.{col3});
    INSERT INTO {TABLE}_fts(rowid, {col1}, {col2}, {col3})
    VALUES (new.rowid, new.{col1}, new.{col2}, new.{col3});
END;

-- Example: people table
-- CREATE TRIGGER people_fts_ai AFTER INSERT ON people BEGIN
--     INSERT INTO people_fts(rowid, name, email, affiliation, notes)
--     VALUES (new.rowid, new.name, new.email, new.affiliation, new.notes);
-- END;
