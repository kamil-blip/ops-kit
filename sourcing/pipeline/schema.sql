-- Candidate pipeline schema. Generic: a "search" is anything you source people for
-- (a judging panel, a speaker line-up, a participant cohort); a "track" is the
-- sub-area inside it. No data ships with this file.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS searches (
    search        TEXT PRIMARY KEY,          -- short slug, e.g. "control-sprint-judges"
    description   TEXT NOT NULL,
    role_type     TEXT NOT NULL,             -- judge | speaker | participant | mentor
    tracks        TEXT,                      -- comma-separated sub-areas
    needed        INTEGER,                   -- how many confirmed people the search needs
    opens_at      TEXT,                      -- ISO date the work starts (assignments go out)
    closes_at     TEXT,                      -- ISO date the work is due
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS candidates (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT UNIQUE,
    org           TEXT,
    headline      TEXT,
    seniority     TEXT,                      -- senior | mid | junior | unknown
    fit_tracks    TEXT,                      -- comma-separated tracks this person can assess
    profile_url   TEXT,
    website       TEXT,
    source        TEXT,                      -- where the row came from: referral, past-pool, form, scrape, search
    source_ref    TEXT,                      -- who referred, which form, which query
    consent       TEXT,                      -- what the person agreed to: self-registered | referred | none-yet
    tags          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_candidates_name ON candidates(name);
CREATE INDEX IF NOT EXISTS idx_candidates_org ON candidates(org);

-- One row per (candidate, search, role). Each row is its own state machine.
CREATE TABLE IF NOT EXISTS candidate_roles (
    id            INTEGER PRIMARY KEY,
    candidate_id  INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    search        TEXT NOT NULL REFERENCES searches(search) ON DELETE CASCADE,
    role          TEXT NOT NULL,
    track         TEXT,
    status        TEXT NOT NULL DEFAULT 'prospect',
    contacted_by  TEXT,
    contacted_at  TEXT,
    confirmed_at  TEXT,
    delivered_at  TEXT,
    max_load      INTEGER,                   -- per-person cap on assigned work, if any
    notes         TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (candidate_id, search, role)
);

CREATE INDEX IF NOT EXISTS idx_roles_search_status ON candidate_roles(search, status);

-- Allowed transitions, seeded from pipeline/states.py by init_db.py.
CREATE TABLE IF NOT EXISTS role_transitions (
    from_status   TEXT NOT NULL,
    to_status     TEXT NOT NULL,
    PRIMARY KEY (from_status, to_status)
);

-- The database refuses a status change the state machine does not allow.
-- A NULL or unchanged old status is always allowed (initial classification).
CREATE TRIGGER IF NOT EXISTS trg_role_transition
BEFORE UPDATE OF status ON candidate_roles
WHEN OLD.status IS NOT NULL AND OLD.status <> NEW.status
     AND NOT EXISTS (SELECT 1 FROM role_transitions
                     WHERE from_status = OLD.status AND to_status = NEW.status)
BEGIN
    SELECT RAISE(ABORT, 'candidate_roles: transition not allowed by the state machine');
END;

-- Touch updated_at on any change, unless the caller set it explicitly.
CREATE TRIGGER IF NOT EXISTS trg_role_touch
AFTER UPDATE ON candidate_roles
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE candidate_roles SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- Every message in or out, so "did we actually tell them" is a query, not a memory.
CREATE TABLE IF NOT EXISTS outreach_log (
    id            INTEGER PRIMARY KEY,
    candidate_id  INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    search        TEXT REFERENCES searches(search) ON DELETE SET NULL,
    role          TEXT,
    direction     TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    channel       TEXT NOT NULL,             -- email | chat | call | form
    template_id   TEXT,
    subject       TEXT,
    summary       TEXT,                      -- one line: what was said or asked
    sent_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_outreach_candidate ON outreach_log(candidate_id, sent_at);

-- Identity and email checks before any cold message goes out.
CREATE TABLE IF NOT EXISTS verification_log (
    id            INTEGER PRIMARY KEY,
    candidate_id  INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    method        TEXT NOT NULL,             -- local-match | provider:<name> | manual
    query         TEXT,
    confidence    REAL,                      -- 0..1
    verdict       TEXT NOT NULL,             -- match | wrong-person-risk | no-match | unverified
    note          TEXT,
    checked_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Outreach templates with {{placeholders}}; rendered and linted by pipeline/templates.py.
CREATE TABLE IF NOT EXISTS templates (
    id            TEXT PRIMARY KEY,          -- scenario slug, e.g. judge-invite
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
