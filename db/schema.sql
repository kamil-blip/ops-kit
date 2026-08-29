-- ops-kit empty schema: CREATE statements only, zero data rows by design.
-- CREATE statements only; zero data rows by design.


-- base tables
CREATE TABLE people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT UNIQUE,
            linkedin TEXT,
            seniority TEXT,
            location TEXT,
            career_stage TEXT,
            discord_user_id TEXT,
            discord_username TEXT,
            notes TEXT,
            sources TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        , country TEXT, education TEXT, experience TEXT, years_experience REAL, headline TEXT, summary TEXT, capability TEXT, research_interests TEXT, skills TEXT, cv_link TEXT, last_contact_date TEXT, total_emails INTEGER DEFAULT 0, total_discord_msgs INTEGER DEFAULT 0, hackathons_participated TEXT, projects_submitted INTEGER DEFAULT 0, projects_reviewed INTEGER DEFAULT 0, is_judge INTEGER DEFAULT 0, is_speaker INTEGER DEFAULT 0, is_mentor INTEGER DEFAULT 0, is_organizer INTEGER DEFAULT 0, is_winner INTEGER DEFAULT 0, is_fellow INTEGER DEFAULT 0, is_internal INTEGER DEFAULT 0, prize_total REAL DEFAULT 0, tags TEXT, interaction_count INTEGER DEFAULT 0, last_embedded_at DATETIME, is_real_person INTEGER DEFAULT 1, last_dossier_refresh_at DATETIME, summary_updated_at DATETIME, lifecycle_status TEXT, summary_pre_pipeline TEXT, summary_confidence TEXT DEFAULT 'unverified', merged_into INTEGER REFERENCES people(id));
CREATE TABLE emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gmail_message_id TEXT UNIQUE NOT NULL,
            thread_id TEXT,
            sender_email TEXT,
            sender_name TEXT,
            recipients_json TEXT,
            labels TEXT,
            subject TEXT,
            body TEXT,
            size INTEGER,
            timestamp DATETIME,
            is_read INTEGER,
            is_outgoing INTEGER,
            is_deleted INTEGER,
            person_id INTEGER, domain TEXT,
            FOREIGN KEY (person_id) REFERENCES people(id)
        );
CREATE TABLE discord_guilds (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        , imported_at DATETIME);
CREATE TABLE discord_channels (
            id TEXT PRIMARY KEY,
            name TEXT,
            guild_id TEXT, imported_at DATETIME, context_slug TEXT, channel_type TEXT DEFAULT 'guild', dm_recipient_id TEXT, dm_recipient_person_id INTEGER REFERENCES people(id), dm_recipient_username TEXT, group_dm_recipient_ids TEXT,
            FOREIGN KEY (guild_id) REFERENCES discord_guilds(id)
        );
CREATE TABLE discord_users (
            id TEXT PRIMARY KEY,
            username TEXT,
            discriminator TEXT,
            avatar TEXT,
            bot INTEGER DEFAULT 0,
            person_id INTEGER, imported_at DATETIME,
            FOREIGN KEY (person_id) REFERENCES people(id)
        );
CREATE TABLE discord_messages (
            id TEXT PRIMARY KEY,
            timestamp DATETIME,
            edited_timestamp DATETIME,
            content TEXT,
            pinned INTEGER DEFAULT 0,
            author_id TEXT,
            reply_to_id TEXT,
            channel_id TEXT,
            person_id INTEGER, domain TEXT, thread_id TEXT REFERENCES discord_threads(thread_id),
            FOREIGN KEY (author_id) REFERENCES discord_users(id),
            FOREIGN KEY (channel_id) REFERENCES discord_channels(id),
            FOREIGN KEY (person_id) REFERENCES people(id)
        );
CREATE TABLE sync_state (
            source TEXT PRIMARY KEY,
            last_sync DATETIME,
            last_id TEXT,
            count INTEGER DEFAULT 0
        , imported_at DATETIME);
CREATE TABLE person_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            last_seen DATETIME,
            source TEXT,
            is_primary INTEGER DEFAULT 0, email_type TEXT,
            FOREIGN KEY (person_id) REFERENCES people(id),
            UNIQUE(person_id, email)
        );
CREATE TABLE action_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'OPEN',
            priority TEXT NOT NULL DEFAULT 'P2',
            description TEXT NOT NULL,
            due_date TEXT,
            waiting_on TEXT,
            context TEXT,
            source TEXT,
            inserted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME
        , context_slug TEXT, last_checked_at DATETIME, depends_on TEXT, recurrence_rule TEXT, next_occurrence DATE, snoozed_until DATE, snooze_reason TEXT, resolution_note TEXT, urgency_score REAL DEFAULT 0, estimated_minutes INTEGER, context_tags TEXT, waiting_on_person_id INTEGER, email_thread_id TEXT, email_last_inbound_at TEXT, email_last_outbound_at TEXT, domain TEXT NOT NULL DEFAULT 'general', source_url TEXT, source_ref TEXT, source_id TEXT, source_person_id INTEGER REFERENCES people(id), about_person_id INTEGER REFERENCES people(id), project_id INTEGER, discord_message_id TEXT, beeper_message_id TEXT REFERENCES beeper_messages(id), granola_meeting_id TEXT, slack_message_id TEXT, source_type TEXT, org_entity_id TEXT REFERENCES entities(id), subtasks_json TEXT, last_status_change_at DATETIME, start_date DATE, partner_kind TEXT, is_manager_explicit INTEGER DEFAULT 0, stage_boost REAL DEFAULT 0, source_quote TEXT, stakeholder_tier INTEGER, template_task_id INTEGER REFERENCES template_tasks(id), creator_person_id INTEGER REFERENCES people(id), extracted_by TEXT, entity_id TEXT);
CREATE TABLE learnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            learning_id TEXT UNIQUE,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            priority TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'active',
            context TEXT,
            apply_when TEXT,
            promoted_to TEXT,
            source TEXT DEFAULT 'manual',
            inserted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        , content_hash TEXT, memory_type TEXT DEFAULT 'learning', superseded_by INTEGER, expires_at TEXT, fsrs_state INTEGER, fsrs_stability REAL, fsrs_difficulty REAL, fsrs_due TEXT, fsrs_last_review TEXT, fsrs_step INTEGER, tags TEXT, domain TEXT NOT NULL DEFAULT 'general', last_embedded_at TEXT, residency TEXT DEFAULT 'retrievable', graduated_at TEXT, occurrence_count INTEGER DEFAULT 1, canonical_id INTEGER REFERENCES learnings(id));
CREATE TABLE deferred_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            details TEXT NOT NULL,
            target_email TEXT,
            target_name TEXT,
            priority TEXT DEFAULT 'P0',
            status TEXT DEFAULT 'pending',
            source TEXT,
            inserted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME
        );
CREATE TABLE observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            subject_type TEXT DEFAULT 'general',
            person_id INTEGER,
            content TEXT NOT NULL,
            source TEXT,
            confidence TEXT DEFAULT 'high',
            inserted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, expires_at DATETIME, promoted INTEGER DEFAULT 0, content_hash TEXT, superseded_by INTEGER, next_review_due DATETIME, last_audited_at DATETIME, audit_state TEXT, audit_stability_days REAL DEFAULT 30, source_table TEXT, source_id TEXT, claim_id INTEGER,
            FOREIGN KEY (person_id) REFERENCES people(id)
        );
CREATE TABLE template_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phase INTEGER NOT NULL,
            phase_name TEXT NOT NULL,
            step_number TEXT NOT NULL,
            task_name TEXT NOT NULL,
            description TEXT,
            definition_of_done TEXT,
            dependencies TEXT,
            typical_duration_hrs REAL,
            assigned_role TEXT DEFAULT 'ops',
            tools_needed TEXT,
            ai_prompt TEXT,
            is_core INTEGER DEFAULT 1,
            last_validated_hackathon TEXT,
            notes TEXT,
            inserted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, verification_status TEXT DEFAULT "unverified", frequency TEXT DEFAULT "sequential", detail_level TEXT, automation_status TEXT, automation_notes TEXT, output_artifact TEXT, trigger_offset TEXT, domain TEXT NOT NULL DEFAULT 'general', cadence TEXT NOT NULL DEFAULT 'sequential', anchor TEXT,
            UNIQUE(phase, step_number)
        );
CREATE TABLE beeper_chats (
            id TEXT PRIMARY KEY,
            title TEXT,
            chat_type TEXT,
            account_id TEXT,
            network TEXT,
            last_activity DATETIME,
            unread_count INTEGER DEFAULT 0,
            is_archived INTEGER DEFAULT 0,
            participants_json TEXT,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        , source TEXT);
CREATE TABLE beeper_messages (
            id TEXT PRIMARY KEY,
            chat_id TEXT,
            sender_id TEXT,
            sender_name TEXT,
            network TEXT,
            text TEXT,
            timestamp DATETIME,
            is_outgoing INTEGER DEFAULT 0,
            message_type TEXT,
            person_id INTEGER,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP, reply_to_id TEXT, reply_thread_root_id TEXT, event_id TEXT, source TEXT, domain TEXT,
            FOREIGN KEY (chat_id) REFERENCES beeper_chats(id),
            FOREIGN KEY (person_id) REFERENCES people(id)
        );
CREATE TABLE bus_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    summary TEXT,
    details_json TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE hook_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hook_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    duration_ms REAL,
    error_message TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE session_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    date TEXT,
    title TEXT,
    summary TEXT,
    tasks_completed TEXT,
    files_changed TEXT,
    source_file TEXT,
    full_content TEXT,
    inserted_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source_file)
);
CREATE TABLE conversation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    display TEXT,
    project TEXT,
    timestamp TEXT,
    ts_human TEXT,
    inserted_at TEXT DEFAULT (datetime('now'))
, content_type TEXT DEFAULT 'general', content_confidence REAL DEFAULT 0.0);
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    event TEXT,
    tool TEXT,
    meta TEXT,
    cmd_preview TEXT,
    ts TEXT,
    ts_human TEXT,
    source_file TEXT,
    inserted_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE session_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE,
    project TEXT,
    first_message TEXT,
    message_count INTEGER DEFAULT 0,
    started_at TEXT,
    ended_at TEXT,
    duration_minutes REAL,
    inserted_at TEXT DEFAULT (datetime('now'))
, summary TEXT, actions_taken TEXT, files_changed TEXT, source TEXT, expires_at DATETIME, pinned INTEGER DEFAULT 0);
CREATE TABLE reference_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE,
    title TEXT,
    category TEXT,
    content TEXT,
    source_file TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
, url TEXT, doc_type TEXT, tags TEXT);
CREATE TABLE checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        task_name TEXT NOT NULL,
        intent TEXT,
        progress TEXT,
        waiting_on TEXT,
        next_steps TEXT,
        state_json TEXT,
        created_at DATETIME DEFAULT (datetime('now')),
        resumed_at DATETIME,
        status TEXT DEFAULT 'active'
    );
CREATE TABLE _table_descriptions (
        table_name TEXT PRIMARY KEY,
        tier TEXT NOT NULL CHECK(tier IN ('core', 'reference', 'system')),
        description TEXT NOT NULL,
        when_to_query TEXT NOT NULL,
        key_columns TEXT NOT NULL,
        example_queries TEXT,
        priority_note TEXT
    , embedding BLOB, category TEXT);
CREATE TABLE learning_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                tool_name TEXT,
                command_preview TEXT,
                error_summary TEXT,
                raw_context TEXT,
                staged_at TEXT DEFAULT (datetime('now')),
                promoted_to INTEGER REFERENCES learnings(id),
                dismissed INTEGER DEFAULT 0
            , normalized_summary TEXT);
CREATE TABLE hook_health_daily (
    date TEXT NOT NULL,
    hook_name TEXT NOT NULL,
    event_type TEXT,
    call_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    avg_duration_ms REAL DEFAULT 0,
    max_duration_ms REAL DEFAULT 0,
    PRIMARY KEY (date, hook_name, event_type)
);
CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL REFERENCES entities(id),
            target_id TEXT NOT NULL REFERENCES entities(id),
            relation TEXT NOT NULL,
            fact TEXT,
            valid_from TEXT,
            valid_until TEXT,
            recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
            confidence REAL DEFAULT 1.0,
            source TEXT, source_table TEXT);
CREATE TABLE learning_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            learning_id INTEGER NOT NULL REFERENCES learnings(id),
            session_id TEXT NOT NULL,
            rating INTEGER,
            surfaced_at TEXT NOT NULL,
            reviewed_at TEXT,
            fsrs_state_before INTEGER,
            fsrs_stability_before REAL,
            fsrs_difficulty_before REAL,
            fsrs_state_after INTEGER,
            fsrs_stability_after REAL,
            fsrs_difficulty_after REAL,
            source TEXT DEFAULT 'auto'
        , adherence TEXT, adherence_source TEXT, adherence_evidence TEXT, judged_at TEXT);
CREATE TABLE email_threads (
    thread_id TEXT PRIMARY KEY,
    subject TEXT,
    first_ts TEXT,
    last_inbound_ts TEXT,
    last_outbound_ts TEXT,
    last_sender_email TEXT,
    person_id INTEGER,
    person_name_cached TEXT,
    status TEXT DEFAULT 'pending',  -- pending, replied, stale, expired, resolved_elsewhere, ignore, no_action_needed, archived
    resolution_note TEXT,
    resolved_at TEXT,
    resolved_by_thread_id TEXT,
    topic_tag TEXT,  -- e.g. payments, sponsor, partnership, ops, legal, other
    context_slug TEXT,
    message_count INTEGER,
    inbound_count INTEGER,
    outbound_count INTEGER,
    action_item_id TEXT,
    reviewed_at TEXT,
    inserted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, last_status_change_at DATETIME, lane TEXT, stakeholder_tier INTEGER, tier_base INTEGER, stage_boost INTEGER DEFAULT 0, freshness_sla_hours INTEGER, classification_source TEXT, auto_classified_at DATETIME,
    FOREIGN KEY (person_id) REFERENCES people(id)
);
CREATE TABLE episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ts TEXT NOT NULL,
    participants_json TEXT,
    topic TEXT,
    summary TEXT,
    content_hash TEXT UNIQUE,
    source_table TEXT NOT NULL,
    source_id INTEGER,
    context_slug TEXT,
    session_id TEXT,
    direction TEXT,
    channel TEXT,
    sentiment TEXT,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE email_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_email TEXT,
    recipient_name TEXT,
    subject TEXT,
    body_html TEXT NOT NULL,
    status TEXT DEFAULT 'draft',
    context_slug TEXT,
    action_item_id TEXT,
    thread_id TEXT,
    created_by TEXT DEFAULT 'cli',
    notes TEXT,
    created_at DATETIME,
    updated_at DATETIME
);
CREATE TABLE briefing_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date TEXT NOT NULL,
    sync_started_at TEXT,
    sync_completed_at TEXT,
    brief_started_at TEXT,
    brief_completed_at TEXT,
    sources_synced TEXT,
    items_classified INTEGER DEFAULT 0,
    items_created INTEGER DEFAULT 0,
    items_closed INTEGER DEFAULT 0,
    items_updated INTEGER DEFAULT 0,
    needs_reply_count INTEGER DEFAULT 0,
    auto_handled_count INTEGER DEFAULT 0,
    new_contacts INTEGER DEFAULT 0,
    summary TEXT,
    detail_json TEXT,
    session_id TEXT
);
CREATE TABLE classification_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id INTEGER REFERENCES briefing_reports(id),
    source TEXT NOT NULL,
    source_id TEXT,
    sender TEXT,
    sender_email TEXT,
    subject TEXT,
    received_at TEXT,
    body_snippet TEXT,
    actionable INTEGER DEFAULT 0,
    needs_reply INTEGER DEFAULT 0,
    matched_action_item TEXT,
    matched_confidence REAL,
    project TEXT,
    priority TEXT,
    category TEXT,
    deadline_detected TEXT,
    summary TEXT,
    reasoning TEXT,
    applied INTEGER DEFAULT 0,
    applied_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE classification_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    classification_id INTEGER NOT NULL REFERENCES classification_results(id),
    original_category TEXT,
    corrected_category TEXT,
    original_priority TEXT,
    corrected_priority TEXT,
    corrected_by TEXT NOT NULL DEFAULT "operator",
    corrected_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE calendar_events (
            id TEXT PRIMARY KEY,
            summary TEXT,
            start_time TEXT,
            end_time TEXT,
            all_day INTEGER DEFAULT 0,
            organizer TEXT,
            attendee_count INTEGER,
            my_response TEXT,
            has_video_link INTEGER DEFAULT 0,
            link TEXT,
            raw_json TEXT,
            fetched_at TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE workflow_routes (
    id INTEGER PRIMARY KEY,
    trigger_patterns TEXT NOT NULL,
    required_action TEXT NOT NULL,
    reason TEXT,
    category TEXT,
    priority INTEGER DEFAULT 5,
    active INTEGER DEFAULT 1,
    inserted_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
, domain TEXT NOT NULL DEFAULT 'general');
CREATE TABLE reference_doc_chunks (
    id INTEGER PRIMARY KEY,
    doc_slug TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    heading TEXT,
    content TEXT NOT NULL,
    char_offset INTEGER,
    inserted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (doc_slug) REFERENCES reference_docs(slug)
);
CREATE TABLE ingest_rejections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  target_table TEXT NOT NULL,
  rejected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  errors TEXT,
  row_json TEXT,
  context TEXT
);
CREATE TABLE _audit_context (
    id          INTEGER PRIMARY KEY CHECK(id = 1),
    actor       TEXT,
    source_ref  TEXT,
    set_at      DATETIME
);
CREATE TABLE sync_summaries (
    id                          INTEGER PRIMARY KEY,
    started_at                  DATETIME,
    finished_at                 DATETIME,
    duration_sec                REAL,
    -- ingest counts
    emails_new                  INTEGER DEFAULT 0,
    discord_new                 INTEGER DEFAULT 0,
    beeper_new                  INTEGER DEFAULT 0,
    granola_new                 INTEGER DEFAULT 0,
    -- action_item lifecycle (delta vs sync start)
    action_items_created        INTEGER DEFAULT 0,
    action_items_updated        INTEGER DEFAULT 0,
    action_items_closed_auto    INTEGER DEFAULT 0,
    -- person/dossier work
    people_updated              INTEGER DEFAULT 0,
    dossiers_refreshed          INTEGER DEFAULT 0,
    -- ingest health
    ingest_rejections           INTEGER DEFAULT 0,
    handler_failures            INTEGER DEFAULT 0,
    cascade_events              INTEGER DEFAULT 0,
    state_violations            INTEGER DEFAULT 0,
    -- triage queue
    triage_needed               TEXT,            -- JSON array of action_item.item_id
    -- failure / notes (dashboard regen output, sync warnings, etc.)
    sources_failed              TEXT,            -- comma list
    notes                       TEXT
);
CREATE TABLE drift_alerts (
    id              INTEGER PRIMARY KEY,
    alert_type      TEXT NOT NULL,
    severity        TEXT NOT NULL,        -- 'info' | 'warn' | 'crit'
    summary         TEXT,
    detail_json     TEXT,                 -- arbitrary diagnostic payload
    fire_count      INTEGER DEFAULT 1,
    first_fired_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_fired_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    resolved_at     DATETIME,
    suppressed      INTEGER DEFAULT 0     -- operator-set 'I know about this'
);
CREATE TABLE outbound_log (
    id                  INTEGER PRIMARY KEY,
    channel             TEXT NOT NULL,        -- 'email' | 'discord' | 'beeper' | 'social'
    recipient           TEXT,                 -- email, channel, or chat handle
    subject             TEXT,
    body_snippet        TEXT,                 -- first 500 chars
    sent_via            TEXT,                 -- 'ak_gmail' | 'manual' | 'metricool' | 'discord_bot'
    session_id          TEXT,
    gmail_message_id    TEXT,                 -- when channel='email'
    thread_id           TEXT,                 -- gmail thread or discord thread
    person_id           INTEGER,              -- resolved recipient if known
    timestamp           DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE thread_participants (
    id                      INTEGER PRIMARY KEY,
    thread_id               TEXT NOT NULL,
    person_id               INTEGER,                  -- NULL if unresolved
    email_addr              TEXT NOT NULL,            -- lowercase canonical
    display_name            TEXT,
    role                    TEXT,                     -- 'originator','replier','cc','bcc','mentioned'
    is_org_alias          INTEGER DEFAULT 0,
    first_message_id        INTEGER,                  -- emails.id first appearance
    last_message_id         INTEGER,
    message_count           INTEGER DEFAULT 0,
    sent_count              INTEGER DEFAULT 0,        -- messages this addr sent in thread
    intro_by_person_id      INTEGER,                  -- who added them
    intro_message_id        INTEGER,                  -- emails.id where intro happened
    first_seen_at           TEXT,
    last_seen_at            TEXT,
    created_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(thread_id, email_addr)
);
CREATE TABLE review_queue (
    id                  INTEGER PRIMARY KEY,
    queue_type          TEXT NOT NULL,             -- 'promotion' | 'merge' | 'contradiction'
    payload             TEXT NOT NULL,             -- JSON (claim_id, candidate_pair, edge diff, etc.)
    priority            INTEGER DEFAULT 5,
    status              TEXT DEFAULT 'pending',    -- 'pending' | 'resolved' | 'skipped'
    surfaced_at         DATETIME,
    resolved_at         DATETIME,
    resolution_note     TEXT,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
, trace_json TEXT, queued_by  TEXT, queued_at  DATETIME);
CREATE TABLE merge_candidates (
    id                INTEGER PRIMARY KEY,
    canonical_id      INTEGER NOT NULL REFERENCES people(id),
    duplicate_id      INTEGER NOT NULL REFERENCES people(id),
    signal            TEXT NOT NULL,     -- 'email_exact' | 'linkedin_exact' | 'fuzzy_name_domain' | 'flash_lite' ...
    confidence        REAL,
    evidence          TEXT,              -- JSON or plaintext
    sieve_run_id      INTEGER,
    status            TEXT DEFAULT 'pending',   -- 'pending' | 'applied' | 'rejected' | 'skipped'
    applied_at        DATETIME,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(canonical_id, duplicate_id)
);
CREATE TABLE faqs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    faq_id TEXT UNIQUE,                     -- e.g. FAQ-20260426-0001 for stable refs
    question_canonical TEXT,                -- only filled by human review
    answer_canonical TEXT,                  -- only filled by human review
    topic TEXT,                             -- short tag: 'submission', 'prizes', 'scheduling'
    scope TEXT NOT NULL DEFAULT 'general',  -- 'hackathon' | 'ops' | 'payments' | 'general' | ...
    context_slug TEXT,                    -- NULL = generic across hackathons
    status TEXT NOT NULL DEFAULT 'proposed',-- proposed|draft|approved|stale
    ask_count INTEGER NOT NULL DEFAULT 0,
    first_asked_at DATETIME,
    last_asked_at DATETIME,
    approved_by TEXT,
    approved_at DATETIME,
    superseded_by INTEGER,                  -- FK to another faqs.id when merged
    notes TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, audience TEXT DEFAULT 'participant', answer_placeholders INTEGER DEFAULT 0,
    FOREIGN KEY (superseded_by) REFERENCES faqs(id)
);
CREATE TABLE faq_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    faq_id INTEGER,                         -- NULL until clustered to a canonical FAQ
    source TEXT NOT NULL,                   -- 'email' | 'discord' | 'beeper' | 'slack'
    source_table TEXT NOT NULL,             -- 'emails' | 'discord_messages' | 'beeper_messages'
    source_row_id TEXT NOT NULL,            -- string PK of source row (since types vary)
    asked_by_person_id INTEGER,             -- FK to people.id, nullable
    asked_by_handle TEXT,                   -- raw sender id when person not resolved
    answered_by_person_id INTEGER,          -- who actually fielded the answer in chat
    answered_by_handle TEXT,
    is_authority INTEGER DEFAULT 0,         -- 1 if answerer was org team / authority
    raw_question TEXT NOT NULL,             -- VERBATIM from source. Never paraphrased.
    raw_answer TEXT,                        -- the in-channel reply, also verbatim
    asked_at DATETIME NOT NULL,
    context_url TEXT,                       -- deep link back to source if available
    detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    detector TEXT,                          -- 'gemini-flash-lite' etc
    confidence REAL,
    UNIQUE(source, source_row_id),
    FOREIGN KEY (faq_id) REFERENCES faqs(id) ON DELETE SET NULL,
    FOREIGN KEY (asked_by_person_id) REFERENCES people(id),
    FOREIGN KEY (answered_by_person_id) REFERENCES people(id)
);
CREATE TABLE faq_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    faq_id INTEGER NOT NULL,
    table_name TEXT NOT NULL,               -- 'hackathons','projects','people','reference_docs',...
    row_id TEXT NOT NULL,                   -- PK as string (slugs, ints, uuids all coexist)
    relation TEXT NOT NULL DEFAULT 'about', -- 'about','grounds_answer','contradicts',...
    notes TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(faq_id, table_name, row_id, relation),
    FOREIGN KEY (faq_id) REFERENCES faqs(id) ON DELETE CASCADE
);
CREATE TABLE action_items_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbox_id TEXT UNIQUE,                       -- AI-IN-YYYYMMDD-NNNN

    -- Provenance
    source TEXT NOT NULL,                       -- 'brief.classify', 'granola:slug', 'wrap-up', etc.
    source_evidence TEXT,                       -- 'emails:12345' or 'discord:msgid' or JSON
    evidence_quote TEXT,                        -- verbatim snippet from source
    classifier_confidence REAL,                 -- 0.0-1.0 when available

    -- Suggested fields (mirror action_items shape)
    suggested_description TEXT NOT NULL,
    suggested_priority TEXT,
    suggested_due_date TEXT,
    suggested_waiting_on TEXT,
    suggested_context_slug TEXT,
    suggested_context TEXT,
    suggested_context_tags TEXT,
    suggested_email_thread_id TEXT,

    -- Review state
    status TEXT NOT NULL DEFAULT 'pending',     -- pending|accepted|rejected|merged|deferred
    reviewed_at DATETIME,
    reviewed_by TEXT,                           -- 'operator' or 'auto-dedup' etc.
    rejection_reason TEXT,
    promoted_to_item_id TEXT,                   -- action_items.item_id if accepted
    merged_into_item_id TEXT,                   -- action_items.item_id if merged

    -- Timestamps
    proposed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
, source_url TEXT, source_ref TEXT, suggested_source_type TEXT, suggested_source_id TEXT, suggested_source_person_id INTEGER REFERENCES people(id), suggested_about_person_id INTEGER REFERENCES people(id), suggested_project_id INTEGER, suggested_discord_message_id TEXT, suggested_beeper_message_id TEXT, suggested_granola_meeting_id TEXT, suggested_slack_message_id TEXT, partner_kind TEXT, is_manager_explicit INTEGER DEFAULT 0, source_quote TEXT, stakeholder_tier INTEGER, suggested_creator_person_id INTEGER REFERENCES people(id), suggested_extracted_by TEXT);
CREATE TABLE action_item_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    item_id_text TEXT,
    person_id INTEGER NOT NULL REFERENCES people(id),
    relation TEXT NOT NULL,
    notes TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(target_kind, target_id, person_id, relation)
);
CREATE TABLE system_upgrades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upgrade_id TEXT UNIQUE NOT NULL,            -- SU-YYYYMMDD-NNN

    -- Core
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    rationale TEXT,                              -- WHY: why this upgrade matters
    next_action TEXT,                            -- specific concrete next step

    -- Lifecycle
    status TEXT NOT NULL DEFAULT 'PROPOSED'
        CHECK (status IN ('PROPOSED','PLANNED','IN_PROGRESS','BLOCKED','DONE','REJECTED','PARKED')),
    priority TEXT DEFAULT 'P2'
        CHECK (priority IN ('P0','P1','P2','P3','P4')),

    -- Categorization
    domain TEXT,                                 -- 'fabric','kanban','extraction','briefing','data-pipeline','memory','hooks','db','automation'
    component TEXT,                              -- specific subsystem, e.g. 'people-enrichment', 'kanban-ui'
    recurrence_rule TEXT,                        -- e.g. 'every 14d' for recurring maintenance

    -- People + project graph
    proposed_by_person_id INTEGER,
    owner_person_id INTEGER,                     -- usually the operator
    context_slug TEXT,                         -- when an upgrade is gated on a specific event
    project_id INTEGER,                          -- FK to projects.id when applicable

    -- Provenance
    source_type TEXT,                            -- 'session','brief','granola','manual','memory','transcript'
    source_ref TEXT,                             -- "session:abcdef" or "transcript:slug"
    source_id TEXT,                              -- specific row id within source_type
    source_url TEXT,                             -- canonical URL if any

    -- Linkage back to action_items / meeting_notes when relevant
    related_action_item_id TEXT,                 -- if the upgrade has a forcing action
    related_meeting_note_id TEXT,                -- if discussed in a meeting

    -- Result / followup
    completed_at DATETIME,
    blocked_reason TEXT,
    parked_until DATETIME,
    resolution_note TEXT,
    notes TEXT,

    -- Metadata
    inserted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE daily_plans (
  plan_date        DATE PRIMARY KEY,                    -- ISO date the plan covers
  item_ids_json    TEXT NOT NULL,                       -- JSON array of action_items.item_id
  status           TEXT NOT NULL DEFAULT 'active',      -- active | debriefed | abandoned
  created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  closed_at        DATETIME,
  notes            TEXT,
  completed_count  INTEGER DEFAULT 0,
  total_count      INTEGER DEFAULT 0
);
CREATE TABLE links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    src_table    TEXT NOT NULL,
    src_id       TEXT NOT NULL,
    dst_table    TEXT NOT NULL,
    dst_id       TEXT NOT NULL,
    relation     TEXT NOT NULL,
    fact         TEXT,
    valid_from   TEXT,
    valid_until  TEXT,
    confidence   REAL DEFAULT 1.0,
    source       TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);
CREATE TABLE faq_harvest_candidates (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,          -- email/discord/beeper
            source_row_id TEXT NOT NULL,
            thread_id TEXT,
            sender_handle TEXT,
            sender_name TEXT,
            asked_at TEXT,
            question_text TEXT NOT NULL,
            operator_answer TEXT,             -- nearest operator reply in same thread
            answer_source_row_id TEXT,
            context_url TEXT,
            cluster_id INTEGER,             -- assigned in cluster step
            is_representative INTEGER DEFAULT 0,
            extracted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source, source_row_id)
        );
CREATE TABLE faq_harvest_clusters (
            id INTEGER PRIMARY KEY,
            representative_question TEXT,
            suggested_answer TEXT,
            n_occurrences INTEGER,
            sources TEXT,                  -- comma-separated: email,discord
            topic_guess TEXT,
            promoted_faq_id INTEGER,       -- if promoted, faqs.id
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE discord_threads (
    thread_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    channel_type TEXT,
    first_message_id TEXT NOT NULL,
    last_message_id TEXT NOT NULL,
    first_ts TEXT NOT NULL,
    last_ts TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 1,
    author_ids TEXT,
    topic_label TEXT,
    context_slug TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE plan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    phase_id TEXT NOT NULL,
    phase_name TEXT,
    phase_hash TEXT NOT NULL,
    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME,
    outcome TEXT CHECK(outcome IN ('DONE','SKIPPED_IDEMPOTENT','BLOCKED','FAILED','IN_PROGRESS','SCHEDULED') OR outcome IS NULL),
    idempotency_key TEXT,
    evidence_json TEXT,
    reversal_spec TEXT,
    applied_by_session TEXT,
    notes TEXT,
    UNIQUE(plan_id, phase_id, phase_hash)
);
CREATE TABLE vec_freshness (
    table_name       TEXT PRIMARY KEY,
    last_embedded_at DATETIME,
    rows_embedded    INTEGER,
    rows_pending     INTEGER,
    updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE plan_phase_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    phase        TEXT NOT NULL,
    status       TEXT,                 -- in_progress | done | failed | skipped
    started_at   DATETIME,
    completed_at DATETIME,
    counts_json  TEXT,
    notes        TEXT,
    UNIQUE(run_id, phase)
);
CREATE TABLE person_identities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id           INTEGER NOT NULL REFERENCES people(id),
    identity_type       TEXT NOT NULL,            -- email | name | discord | linkedin
    identity_value      TEXT NOT NULL,
    normalized_value    TEXT NOT NULL,
    disambiguation_risk TEXT NOT NULL DEFAULT 'low',  -- low | medium | high
    source              TEXT,                      -- people | person_emails | person_aliases
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(person_id, identity_type, normalized_value)
);
CREATE TABLE staging (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key    TEXT UNIQUE NOT NULL,   -- sha256(target_table|natural_key|payload)
    target_table       TEXT NOT NULL,          -- canonical table this write targets
    op                 TEXT NOT NULL DEFAULT 'upsert',  -- upsert | retire
    natural_key        TEXT,                   -- JSON of the natural-key fields
    payload            TEXT NOT NULL,          -- JSON column->value
    source_table       TEXT,                   -- provenance: origin row table
    source_id          TEXT,                   -- provenance: origin row id
    source_quote       TEXT,                   -- verbatim evidence span
    trace_json         TEXT,                   -- resolver trace (person/org/hackathon path)
    submitted_by       TEXT NOT NULL,          -- writer name (judge_tracker, ...)
    status             TEXT NOT NULL DEFAULT 'pending', -- pending|promoted|rejected|quarantined|superseded
    error              TEXT,                   -- rejection/quarantine reason
    resolved_person_id INTEGER,               -- resolver output when target involves a person
    canonical_id       TEXT,                   -- canonical row id after promotion (TEXT: entities.id is text)
    created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
    promoted_at        DATETIME,
    promoted_by        TEXT
, attempts INTEGER DEFAULT 0, last_attempt_at DATETIME);
CREATE TABLE cdc_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name  TEXT NOT NULL,
    row_id      TEXT,
    op          TEXT NOT NULL,    -- INSERT | UPDATE
    actor       TEXT,             -- from _audit_context.actor (NULL = unattributed)
    changed_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE cdc_cursor (
    consumer    TEXT PRIMARY KEY,  -- e.g. 'steward'
    last_cdc_id INTEGER NOT NULL DEFAULT 0,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE steward_health (
    id                  INTEGER PRIMARY KEY CHECK(id=1),
    last_tick_at        DATETIME,
    last_tick_status    TEXT,        -- ok | error | skipped_no_change | never
    last_tick_ms        INTEGER,
    ticks_total         INTEGER DEFAULT 0,
    last_error          TEXT,
    last_promoted       INTEGER DEFAULT 0,
    last_rejected       INTEGER DEFAULT 0,
    last_review_depth   INTEGER,
    last_staged_pending INTEGER,
    pid                 INTEGER,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE steward_config (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE comms_drafts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key      TEXT UNIQUE NOT NULL,   -- sha256(channel:thread:inbound_msg)
    channel              TEXT NOT NULL,          -- gmail | discord | beeper
    thread_id            TEXT,
    sender_person_id     INTEGER,
    sender_email         TEXT,
    inbound_msg_id       TEXT,
    inbound_snippet      TEXT,                   -- the inbound text the draft answers
    reply_needed         INTEGER,                -- 1 | 0 | NULL (ungated). Set by the Opus gate.
    gate_reason          TEXT,                   -- why reply_needed was set this way
    draft_text           TEXT,                   -- the proposed reply (NULL until drafted)
    context_snapshot_json TEXT,                  -- surface_context.py output used to draft
    status               TEXT NOT NULL DEFAULT 'pending_gate',
        -- pending_gate (polled, awaiting Opus gate) | skipped_no_reply (gate said no) |
        -- pending_draft (gate said yes, awaiting LLM draft) | draft (ready for the operator) |
        -- sent_by_operator (operator replied) | superseded
    notified             INTEGER DEFAULT 0,
    created_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE comms_monitor_health (
    id            INTEGER PRIMARY KEY CHECK(id=1),
    last_tick_at  DATETIME,
    status        TEXT,            -- ok | error | skipped_no_change | never
    ticks_total   INTEGER DEFAULT 0,
    drafts_made   INTEGER DEFAULT 0,
    last_error    TEXT,
    pid           INTEGER,
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE comms_draft_outcomes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id     INTEGER REFERENCES comms_drafts(id),
    final_text   TEXT,
    diff_json    TEXT,
    channel      TEXT,
    category     TEXT,
    captured_at  DATETIME DEFAULT CURRENT_TIMESTAMP
, bucket TEXT, ratio REAL, match_method TEXT, matched_email_id INTEGER);
CREATE TABLE consolidation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            canonical_id INTEGER NOT NULL REFERENCES learnings(id),
            member_id INTEGER NOT NULL REFERENCES learnings(id),
            cosine REAL,
            method TEXT DEFAULT 'knn_cluster',
            status TEXT DEFAULT 'proposed',         -- proposed | applied | rejected | conflict
            rationale TEXT,
            review_queue_id INTEGER,                -- set when a conflict is routed to review_queue
            proposed_at TEXT DEFAULT (datetime('now')),
            decided_at TEXT,
            UNIQUE(canonical_id, member_id)
        );
CREATE TABLE learning_health_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now')),
    active INTEGER, embedded INTEGER, embed_pct REAL,
    adherence_total INTEGER, adherence_judged INTEGER, adherence_pct REAL,
    violated INTEGER, followed INTEGER, na INTEGER,
    tier_a INTEGER, critical INTEGER,
    consolidation_proposed INTEGER, consolidation_conflict INTEGER,
    note TEXT, metrics_json TEXT
);
CREATE TABLE attributes (
  id INTEGER PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES entities(id),
  attr TEXT NOT NULL,
  value TEXT,
  value_type TEXT,
  ref_entity_id TEXT REFERENCES entities(id),
  valid_from DATETIME,
  valid_until DATETIME,
  status TEXT DEFAULT 'current',
  confidence REAL,
  scope TEXT,                 -- funding-tagging: org | event | global
  visibility TEXT,            -- funding-tagging: internal | public (funding received = internal)
  source_table TEXT,
  source_id TEXT,
  asserter_entity_id TEXT,
  inserted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE work_queue_archive (
    id INTEGER, handler TEXT, payload TEXT, priority INTEGER, status TEXT,
    retry_count INTEGER, max_retries INTEGER, error TEXT, result TEXT,
    created_at TEXT, claimed_at TEXT, finished_at TEXT, source_session TEXT,
    dedup_key TEXT, archived_at TEXT
);
CREATE TABLE work_queue_dedup_index (
    dedup_key TEXT, handler TEXT, archived_at TEXT
);
CREATE TABLE "work_queue" (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        handler TEXT NOT NULL,
        payload TEXT NOT NULL,
        priority INTEGER DEFAULT 5,
        status TEXT NOT NULL DEFAULT 'pending',
        retry_count INTEGER DEFAULT 0,
        max_retries INTEGER DEFAULT 3,
        error TEXT,
        result TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        claimed_at TEXT,
        finished_at TEXT,
        source_session TEXT,
        dedup_key TEXT
    );
CREATE TABLE vec_entities_quarantine (
                rowid_val INTEGER PRIMARY KEY,
                embedding BLOB,
                quarantined_at TEXT DEFAULT (datetime('now')),
                reason TEXT
            );
CREATE TABLE person_identifiers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id     INTEGER NOT NULL REFERENCES people(id),
    id_type       TEXT NOT NULL,      -- email | discord | linkedin | phone | other
    id_value      TEXT NOT NULL,
    id_value_norm TEXT NOT NULL,      -- lower/trim normalization ONLY
    source_table  TEXT,               -- person_emails | person_identities | discord_users
    source_id     TEXT,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(person_id, id_type, id_value_norm)
);
CREATE TABLE identifier_denylist (
    pattern    TEXT PRIMARY KEY,      -- exact value or LIKE pattern
    kind       TEXT NOT NULL CHECK(kind IN ('exact','like')),
    reason     TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE dead_letter (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT,                    -- e.g. 'cascade_tick'
    payload_json TEXT,
    error        TEXT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    retried_at   DATETIME,
    resolved_at  DATETIME
);
CREATE TABLE job_heartbeats (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    job                   TEXT NOT NULL,
    started_at            DATETIME,
    finished_at           DATETIME,
    exit_note             TEXT,
    rows_touched          INTEGER,
    expected_interval_min INTEGER,
    min_rows_expected     INTEGER
);
CREATE TABLE n4_kv (
            k TEXT PRIMARY KEY, v TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE "entities" (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN (
        'person','org','hackathon','event','topic','project',
        'action_item','email_thread','discord_channel','discord_thread',
        'granola_meeting','beeper_chat','slack_channel','template_task',
        'learning','faq','reference_doc'
    )),
    name TEXT NOT NULL,
    data TEXT,
    source TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    embedding BLOB
);
CREATE TABLE review_queue_archive (
    id                  INTEGER PRIMARY KEY,
    queue_type          TEXT NOT NULL,             -- 'promotion' | 'merge' | 'contradiction'
    payload             TEXT NOT NULL,             -- JSON (claim_id, candidate_pair, edge diff, etc.)
    priority            INTEGER DEFAULT 5,
    status              TEXT DEFAULT 'pending',    -- 'pending' | 'resolved' | 'skipped'
    surfaced_at         DATETIME,
    resolved_at         DATETIME,
    resolution_note     TEXT,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
, trace_json TEXT, queued_by  TEXT, queued_at  DATETIME);
CREATE TABLE nightly_step_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_stamp TEXT NOT NULL,
        step_name TEXT NOT NULL,
        started_at DATETIME NOT NULL,
        duration_ms INTEGER,
        status TEXT NOT NULL CHECK(status IN ('ok','failed','skipped')),
        error TEXT,
        rows INTEGER);
CREATE TABLE table_rowcounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at DATETIME NOT NULL DEFAULT (datetime('now')),
        table_name TEXT NOT NULL,
        rows INTEGER NOT NULL);
CREATE TABLE session_log_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        entry_date TEXT,
        title TEXT,
        problem TEXT,
        what_landed TEXT,
        contracts TEXT,
        gaps_closed TEXT,
        remaining TEXT,
        ref_slug TEXT,
        raw_md TEXT NOT NULL,
        inserted_at DATETIME DEFAULT (datetime('now')));
CREATE TABLE staging_archive(
  id INT,
  idempotency_key TEXT,
  target_table TEXT,
  op TEXT,
  natural_key TEXT,
  payload TEXT,
  source_table TEXT,
  source_id TEXT,
  source_quote TEXT,
  trace_json TEXT,
  submitted_by TEXT,
  status TEXT,
  error TEXT,
  resolved_person_id INT,
  canonical_id TEXT,
  created_at NUM,
  promoted_at NUM,
  promoted_by TEXT,
  attempts INT,
  last_attempt_at NUM
);
CREATE TABLE changelog_commit_ledger (
    commit_hash TEXT PRIMARY KEY,
    session_id TEXT,
    slug TEXT,
    claimed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE comms_prefilter_rules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type      TEXT NOT NULL CHECK(rule_type IN ('automated_sender','notification_pattern','unsubscribe_footer')),
    pattern        TEXT NOT NULL,
    channel        TEXT NOT NULL DEFAULT 'all',
    reason         TEXT,
    added_by       TEXT,
    min_occurrences INTEGER,
    active         INTEGER NOT NULL DEFAULT 0,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(rule_type, pattern, channel)
);

-- indexes
CREATE INDEX idx_emails_sender ON emails(sender_email);
CREATE INDEX idx_emails_timestamp ON emails(timestamp);
CREATE INDEX idx_emails_person ON emails(person_id);
CREATE INDEX idx_discord_users_person ON discord_users(person_id);
CREATE INDEX idx_discord_msgs_author ON discord_messages(author_id);
CREATE INDEX idx_discord_msgs_channel ON discord_messages(channel_id);
CREATE INDEX idx_discord_msgs_timestamp ON discord_messages(timestamp);
CREATE INDEX idx_discord_msgs_person ON discord_messages(person_id);
CREATE INDEX idx_person_emails_email ON person_emails(email);
CREATE INDEX idx_person_emails_person ON person_emails(person_id);
CREATE INDEX idx_action_status ON action_items(status);
CREATE INDEX idx_action_priority ON action_items(priority);
CREATE INDEX idx_deferred_status ON deferred_actions(status);
CREATE INDEX idx_obs_subject ON observations(subject);
CREATE INDEX idx_obs_person ON observations(person_id);
CREATE INDEX idx_obs_type ON observations(subject_type);
CREATE INDEX idx_beeper_chats_network ON beeper_chats(network);
CREATE INDEX idx_beeper_msgs_chat ON beeper_messages(chat_id);
CREATE INDEX idx_beeper_msgs_ts ON beeper_messages(timestamp);
CREATE INDEX idx_beeper_msgs_person ON beeper_messages(person_id);
CREATE INDEX idx_beeper_msgs_network ON beeper_messages(network);
CREATE INDEX idx_bus_events_ts ON bus_events(ts);
CREATE INDEX idx_bus_events_session ON bus_events(session_id);
CREATE INDEX idx_hook_health_ts ON hook_health(ts);
CREATE INDEX idx_lc_session
            ON learning_candidates(session_id);
CREATE INDEX idx_lc_pending
            ON learning_candidates(promoted_to, dismissed)
            WHERE promoted_to IS NULL AND dismissed = 0;
CREATE INDEX idx_edges_source ON edges(source_id);
CREATE INDEX idx_edges_target ON edges(target_id);
CREATE INDEX idx_edges_relation ON edges(relation);
CREATE UNIQUE INDEX idx_edges_dedup
        ON edges(source_id, target_id, relation, valid_from);
CREATE INDEX idx_edges_temporal ON edges(valid_from, valid_until);
CREATE INDEX idx_lr_session ON learning_reviews(session_id);
CREATE INDEX idx_lr_learning ON learning_reviews(learning_id);
CREATE INDEX idx_lr_unrated
            ON learning_reviews(learning_id, session_id) WHERE rating IS NULL;
CREATE INDEX idx_learnings_fsrs_due
            ON learnings(fsrs_due) WHERE status = 'active';
CREATE INDEX idx_et_status ON email_threads(status);
CREATE INDEX idx_et_person ON email_threads(person_id);
CREATE INDEX idx_et_topic ON email_threads(topic_tag);
CREATE INDEX idx_et_action ON email_threads(action_item_id);
CREATE INDEX idx_et_sender ON email_threads(last_sender_email);
CREATE INDEX idx_episodes_kind ON episodes(kind);
CREATE INDEX idx_episodes_ts ON episodes(ts);
CREATE INDEX idx_episodes_source ON episodes(source_table, source_id);
CREATE INDEX idx_episodes_hackathon ON episodes(context_slug);
CREATE INDEX idx_episodes_hash ON episodes(content_hash);
CREATE INDEX idx_audit_events_ts ON audit_events(ts);
CREATE INDEX idx_audit_events_session ON audit_events(session_id);
CREATE INDEX idx_action_items_hackathon ON action_items(context_slug);
CREATE INDEX idx_email_threads_hackathon ON email_threads(context_slug);
CREATE INDEX idx_briefing_reports_date 
    ON briefing_reports(brief_date DESC);
CREATE INDEX idx_classification_brief_id 
    ON classification_results(brief_id);
CREATE INDEX idx_classification_category 
    ON classification_results(category) WHERE applied = 0;
CREATE INDEX idx_classification_source 
    ON classification_results(source, source_id);
CREATE INDEX idx_classification_brief_applied ON classification_results(brief_id, applied);
CREATE INDEX idx_action_items_status_priority ON action_items(status, priority);
CREATE UNIQUE INDEX idx_rdc_slug_idx ON reference_doc_chunks(doc_slug, chunk_index);
CREATE INDEX idx_ingest_rejections_source
  ON ingest_rejections(source, rejected_at);
CREATE INDEX idx_ingest_rejections_target
  ON ingest_rejections(target_table, rejected_at);
CREATE INDEX idx_people_dossier_refresh
    ON people(last_dossier_refresh_at);
CREATE INDEX idx_sync_summaries_time
    ON sync_summaries(started_at);
CREATE UNIQUE INDEX idx_drift_alerts_open
    ON drift_alerts(alert_type)
    WHERE resolved_at IS NULL;
CREATE INDEX idx_drift_alerts_recent
    ON drift_alerts(last_fired_at DESC);
CREATE INDEX idx_drift_alerts_severity
    ON drift_alerts(severity, resolved_at);
CREATE INDEX idx_outbound_log_session
    ON outbound_log(session_id, timestamp);
CREATE INDEX idx_outbound_log_recipient
    ON outbound_log(recipient, timestamp);
CREATE INDEX idx_outbound_log_thread
    ON outbound_log(thread_id);
CREATE INDEX idx_outbound_log_time
    ON outbound_log(timestamp DESC);
CREATE INDEX idx_thread_participants_thread
    ON thread_participants(thread_id);
CREATE INDEX idx_thread_participants_person
    ON thread_participants(person_id);
CREATE INDEX idx_thread_participants_email
    ON thread_participants(email_addr);
CREATE INDEX idx_review_queue_status
    ON review_queue(status, queue_type, priority, created_at);
CREATE INDEX idx_review_queue_created
    ON review_queue(created_at);
CREATE INDEX idx_merge_candidates_status ON merge_candidates(status, confidence);
CREATE INDEX idx_merge_candidates_dup ON merge_candidates(duplicate_id);
CREATE INDEX idx_beeper_msgs_reply ON beeper_messages(reply_to_id);
CREATE INDEX idx_beeper_msgs_event ON beeper_messages(event_id);
CREATE INDEX idx_faqs_status ON faqs(status);
CREATE INDEX idx_faqs_scope ON faqs(scope);
CREATE INDEX idx_faqs_hackathon ON faqs(context_slug);
CREATE INDEX idx_faqs_topic ON faqs(topic);
CREATE INDEX idx_faq_occ_faq ON faq_occurrences(faq_id);
CREATE INDEX idx_faq_occ_source ON faq_occurrences(source, asked_at);
CREATE INDEX idx_faq_occ_asker ON faq_occurrences(asked_by_person_id);
CREATE INDEX idx_faq_occ_answerer ON faq_occurrences(answered_by_person_id);
CREATE INDEX idx_faq_occ_authority ON faq_occurrences(is_authority);
CREATE INDEX idx_faq_links_faq ON faq_links(faq_id);
CREATE INDEX idx_faq_links_target ON faq_links(table_name, row_id);
CREATE INDEX idx_action_items_domain ON action_items(domain) WHERE status IN ('OPEN','WAITING','BLOCKED');
CREATE INDEX idx_beeper_msgs_domain  ON beeper_messages(domain, timestamp);
CREATE INDEX idx_discord_msgs_domain ON discord_messages(domain, timestamp);
CREATE INDEX idx_emails_domain       ON emails(domain, timestamp);
CREATE INDEX idx_workflow_routes_domain ON workflow_routes(domain, active);
CREATE INDEX idx_aii_status ON action_items_inbox(status);
CREATE INDEX idx_aii_source ON action_items_inbox(source);
CREATE INDEX idx_aii_pending_proposed
    ON action_items_inbox(proposed_at)
    WHERE status='pending';
CREATE INDEX idx_aii_thread
    ON action_items_inbox(suggested_email_thread_id)
    WHERE suggested_email_thread_id IS NOT NULL;
CREATE INDEX idx_ai_source_person ON action_items(source_person_id);
CREATE INDEX idx_ai_about_person  ON action_items(about_person_id);
CREATE INDEX idx_ai_project       ON action_items(project_id);
CREATE INDEX idx_ai_source_type   ON action_items(source_type);
CREATE INDEX idx_ai_discord_msg   ON action_items(discord_message_id) WHERE discord_message_id IS NOT NULL;
CREATE INDEX idx_ai_beeper_msg    ON action_items(beeper_message_id)  WHERE beeper_message_id  IS NOT NULL;
CREATE INDEX idx_aii_sug_source_person ON action_items_inbox(suggested_source_person_id);
CREATE INDEX idx_aii_sug_about_person  ON action_items_inbox(suggested_about_person_id);
CREATE INDEX idx_aii_sug_project       ON action_items_inbox(suggested_project_id);
CREATE INDEX idx_aii_sug_source_type   ON action_items_inbox(suggested_source_type);
CREATE INDEX idx_aip_person ON action_item_people(person_id);
CREATE INDEX idx_aip_target ON action_item_people(target_kind, target_id);
CREATE INDEX idx_aip_relation ON action_item_people(relation);
CREATE INDEX idx_people_lifecycle ON people(lifecycle_status);
CREATE INDEX idx_action_items_org_entity_id
  ON action_items(org_entity_id, status);
CREATE INDEX idx_su_status      ON system_upgrades(status);
CREATE INDEX idx_su_priority    ON system_upgrades(priority);
CREATE INDEX idx_su_domain      ON system_upgrades(domain);
CREATE INDEX idx_su_owner       ON system_upgrades(owner_person_id);
CREATE INDEX idx_su_hackathon   ON system_upgrades(context_slug)
    WHERE context_slug IS NOT NULL;
CREATE INDEX idx_su_active      ON system_upgrades(status, priority)
    WHERE status NOT IN ('DONE','REJECTED');
CREATE INDEX idx_ai_last_status_change
    ON action_items(last_status_change_at)
 WHERE status NOT IN ('DONE', 'REMOVED', 'CANCELLED');
CREATE INDEX idx_et_last_status_change
    ON email_threads(last_status_change_at)
 WHERE status NOT IN ('resolved', 'resolved_elsewhere', 'no_action_needed', 'ignore');
CREATE INDEX idx_ai_start_date
    ON action_items(start_date)
 WHERE start_date IS NOT NULL AND status NOT IN ('DONE', 'REMOVED', 'CANCELLED');
CREATE INDEX idx_action_items_partner_kind ON action_items(partner_kind);
CREATE INDEX idx_action_items_stakeholder ON action_items(stakeholder_tier, status);
CREATE INDEX idx_daily_plans_status ON daily_plans(status, plan_date DESC);
CREATE INDEX idx_action_items_template_task_id ON action_items(template_task_id);
CREATE UNIQUE INDEX idx_links_unique ON links(src_table, src_id, dst_table, dst_id, relation);
CREATE INDEX idx_links_src ON links(src_table, src_id);
CREATE INDEX idx_links_dst ON links(dst_table, dst_id);
CREATE INDEX idx_links_relation ON links(relation);
CREATE INDEX idx_fhc_cluster ON faq_harvest_candidates(cluster_id);
CREATE INDEX idx_email_threads_lane ON email_threads(lane, status);
CREATE INDEX idx_email_threads_sla ON email_threads(stakeholder_tier, last_inbound_ts);
CREATE INDEX idx_email_threads_pending_inbound ON email_threads(status, last_inbound_ts);
CREATE INDEX idx_discord_channels_channel_type ON discord_channels(channel_type);
CREATE INDEX idx_discord_channels_dm_recipient_id ON discord_channels(dm_recipient_id);
CREATE INDEX idx_discord_channels_dm_recipient_person_id ON discord_channels(dm_recipient_person_id);
CREATE INDEX idx_discord_threads_channel ON discord_threads(channel_id, last_ts);
CREATE INDEX idx_discord_threads_hackathon ON discord_threads(context_slug);
CREATE INDEX idx_discord_threads_last_ts ON discord_threads(last_ts);
CREATE INDEX idx_discord_messages_thread_id ON discord_messages(thread_id);
CREATE INDEX idx_action_items_creator_person_id ON action_items(creator_person_id);
CREATE INDEX idx_action_items_extracted_by ON action_items(extracted_by);
CREATE INDEX idx_action_items_inbox_creator_person_id ON action_items_inbox(suggested_creator_person_id);
CREATE INDEX idx_action_items_inbox_extracted_by ON action_items_inbox(suggested_extracted_by);
CREATE INDEX idx_obs_next_review ON observations(next_review_due)
          WHERE superseded_by IS NULL;
CREATE INDEX idx_audit_events_event ON audit_events(event, ts);
CREATE INDEX idx_plan_runs_lookup ON plan_runs(plan_id, phase_id, outcome);
CREATE INDEX idx_plan_runs_started ON plan_runs(started_at);
CREATE INDEX idx_person_identities_norm ON person_identities(identity_type, normalized_value);
CREATE INDEX idx_person_identities_person ON person_identities(person_id);
CREATE INDEX idx_staging_status ON staging(status);
CREATE INDEX idx_staging_target ON staging(target_table, status);
CREATE INDEX idx_cdc_log_id ON cdc_log(id);
CREATE INDEX idx_comms_drafts_status ON comms_drafts(status);
CREATE INDEX idx_comms_drafts_thread ON comms_drafts(channel, thread_id);
CREATE INDEX idx_conslog_canonical ON consolidation_log(canonical_id);
CREATE INDEX idx_conslog_member ON consolidation_log(member_id);
CREATE INDEX idx_conslog_status ON consolidation_log(status);
CREATE INDEX idx_learning_reviews_lid ON learning_reviews(learning_id);
CREATE UNIQUE INDEX idx_attributes_current ON attributes(entity_id, attr) WHERE status='current';
CREATE INDEX idx_attributes_entity ON attributes(entity_id);
CREATE INDEX idx_attributes_attr ON attributes(attr, status);
CREATE INDEX idx_wq_dedup_index_key ON work_queue_dedup_index(dedup_key);
CREATE INDEX idx_work_queue_handler ON work_queue(handler, status);
CREATE INDEX idx_work_queue_status_priority ON work_queue(status, priority, created_at) WHERE status IN ('pending','in_progress');
CREATE UNIQUE INDEX uq_work_queue_dedup_active ON work_queue (dedup_key) WHERE status IN ('pending','in_progress');
CREATE INDEX idx_observations_claim_id ON observations(claim_id);
CREATE INDEX idx_obs_content_hash ON observations(content_hash);
CREATE INDEX idx_emails_thread_id ON emails(thread_id);
CREATE INDEX idx_action_items_entity_id ON action_items(entity_id);
CREATE INDEX idx_faq_harvest_candidates_thread_id ON faq_harvest_candidates(thread_id);
CREATE INDEX idx_discord_channels_context_slug ON discord_channels(context_slug);
CREATE INDEX idx_cdc_log_table_row ON cdc_log(table_name, row_id);
CREATE INDEX idx_cdc_log_changed_at ON cdc_log(changed_at);
CREATE INDEX idx_hook_health_hook_ts ON hook_health(hook_name, ts);
CREATE INDEX idx_hook_health_event_ts ON hook_health(event_type, ts);
CREATE INDEX idx_episodes_session ON episodes(session_id);
CREATE INDEX idx_observations_source ON observations(source_table, source_id);
CREATE INDEX idx_entities_type ON entities(type);
CREATE INDEX idx_entities_name ON entities(name);
CREATE INDEX idx_audit_events_source_file ON audit_events(source_file);
CREATE INDEX idx_rq_archive_resolved ON review_queue_archive(resolved_at);
CREATE INDEX idx_nsr_stamp ON nightly_step_runs(run_stamp);
CREATE INDEX idx_trc_table ON table_rowcounts(table_name, captured_at);

-- FTS5 mirrors
CREATE VIRTUAL TABLE "_table_descriptions_fts" USING fts5(
        table_name,
        description,
        when_to_query,
        key_columns,
        example_queries,
        content=_table_descriptions,
        content_rowid=rowid
    );
CREATE VIRTUAL TABLE reference_docs_fts 
    USING fts5(title, content, tags, slug UNINDEXED, content=reference_docs, content_rowid=id);
CREATE VIRTUAL TABLE action_items_fts USING fts5(
    description, context, waiting_on, source,
    content=action_items, content_rowid=rowid,
    tokenize="unicode61 remove_diacritics 1"
);

-- triggers
CREATE TRIGGER action_items_fts_ai AFTER INSERT ON action_items BEGIN
    INSERT INTO action_items_fts(rowid, description, context, waiting_on, source)
    VALUES (new.rowid, new.description, new.context, new.waiting_on, new.source);
END;
CREATE TRIGGER action_items_fts_ad AFTER DELETE ON action_items BEGIN
    INSERT INTO action_items_fts(action_items_fts, rowid, description, context, waiting_on, source)
    VALUES ('delete', old.rowid, old.description, old.context, old.waiting_on, old.source);
END;
CREATE TRIGGER action_items_fts_au AFTER UPDATE ON action_items BEGIN
    INSERT INTO action_items_fts(action_items_fts, rowid, description, context, waiting_on, source)
    VALUES ('delete', old.rowid, old.description, old.context, old.waiting_on, old.source);
    INSERT INTO action_items_fts(rowid, description, context, waiting_on, source)
    VALUES (new.rowid, new.description, new.context, new.waiting_on, new.source);
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE entities_fts USING fts5(
                name, data, source,
                content='entities',
                content_rowid='rowid'
            );

-- triggers
CREATE TRIGGER action_items_slug_canonical_ai
AFTER INSERT ON action_items
FOR EACH ROW
WHEN NEW.context_slug IS NOT NULL
  AND NEW.context_slug != ''
  AND (NEW.context_slug != LOWER(NEW.context_slug)
       OR instr(NEW.context_slug, ' ') > 0
       OR instr(NEW.context_slug, '_') > 0)
BEGIN
    UPDATE action_items
    SET context_slug = REPLACE(REPLACE(LOWER(NEW.context_slug), ' ', '-'), '_', '-')
    WHERE id = NEW.id;
END;
CREATE TRIGGER email_threads_slug_canonical_ai
AFTER INSERT ON email_threads
FOR EACH ROW
WHEN NEW.context_slug IS NOT NULL
  AND NEW.context_slug != ''
  AND (NEW.context_slug != LOWER(NEW.context_slug)
       OR instr(NEW.context_slug, ' ') > 0
       OR instr(NEW.context_slug, '_') > 0)
BEGIN
    UPDATE email_threads
    SET context_slug = REPLACE(REPLACE(LOWER(NEW.context_slug), ' ', '-'), '_', '-')
    WHERE thread_id = NEW.thread_id;
END;
CREATE TRIGGER reference_docs_slug_canonical_ai
AFTER INSERT ON reference_docs
FOR EACH ROW
WHEN NEW.slug IS NOT NULL
  AND NEW.slug != ''
  AND (NEW.slug != LOWER(NEW.slug)
       OR instr(NEW.slug, ' ') > 0
       OR instr(NEW.slug, '_') > 0)
BEGIN
    UPDATE reference_docs
    SET slug = REPLACE(REPLACE(LOWER(NEW.slug), ' ', '-'), '_', '-')
    WHERE id = NEW.id;
END;
CREATE TRIGGER action_items_slug_canonical_au
AFTER UPDATE OF context_slug ON action_items FOR EACH ROW
WHEN NEW.context_slug IS NOT NULL AND NEW.context_slug != ''
  AND (NEW.context_slug != LOWER(NEW.context_slug)
       OR instr(NEW.context_slug, ' ') > 0
       OR instr(NEW.context_slug, '_') > 0)
BEGIN
  UPDATE action_items
  SET context_slug = REPLACE(REPLACE(LOWER(NEW.context_slug), ' ', '-'), '_', '-')
  WHERE id = NEW.id;
END;
CREATE TRIGGER email_threads_slug_canonical_au
AFTER UPDATE OF context_slug ON email_threads FOR EACH ROW
WHEN NEW.context_slug IS NOT NULL AND NEW.context_slug != ''
  AND (NEW.context_slug != LOWER(NEW.context_slug)
       OR instr(NEW.context_slug, ' ') > 0
       OR instr(NEW.context_slug, '_') > 0)
BEGIN
  UPDATE email_threads
  SET context_slug = REPLACE(REPLACE(LOWER(NEW.context_slug), ' ', '-'), '_', '-')
  WHERE thread_id = NEW.thread_id;
END;
CREATE TRIGGER reference_docs_slug_canonical_au
AFTER UPDATE OF slug ON reference_docs FOR EACH ROW
WHEN NEW.slug IS NOT NULL AND NEW.slug != ''
  AND (NEW.slug != LOWER(NEW.slug)
       OR instr(NEW.slug, ' ') > 0
       OR instr(NEW.slug, '_') > 0)
BEGIN
  UPDATE reference_docs
  SET slug = REPLACE(REPLACE(LOWER(NEW.slug), ' ', '-'), '_', '-')
  WHERE id = NEW.id;
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE episodes_fts USING fts5(
    topic, summary,
    content=episodes, content_rowid=id
);

-- triggers
CREATE TRIGGER episodes_fts_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, topic, summary) VALUES (new.id, new.topic, new.summary);
END;
CREATE TRIGGER episodes_fts_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, topic, summary) VALUES ('delete', old.id, old.topic, old.summary);
END;
CREATE TRIGGER episodes_fts_au AFTER UPDATE OF topic, summary ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, topic, summary) VALUES ('delete', old.id, old.topic, old.summary);
    INSERT INTO episodes_fts(rowid, topic, summary) VALUES (new.id, new.topic, new.summary);
END;
CREATE TRIGGER reference_docs_fts_ai AFTER INSERT ON reference_docs BEGIN
    INSERT INTO reference_docs_fts(rowid, title, content, tags, slug)
    VALUES (new.rowid, new.title, new.content, new.tags, new.slug);
END;
CREATE TRIGGER reference_docs_fts_ad AFTER DELETE ON reference_docs BEGIN
    INSERT INTO reference_docs_fts(reference_docs_fts, rowid, title, content, tags, slug)
    VALUES ('delete', old.rowid, old.title, old.content, old.tags, old.slug);
END;
CREATE TRIGGER email_drafts_ts_insert
    AFTER INSERT ON email_drafts
    BEGIN
        UPDATE email_drafts SET created_at=datetime('now'), updated_at=datetime('now') WHERE id=NEW.id;
    END;
CREATE TRIGGER email_drafts_ts_update
    AFTER UPDATE ON email_drafts
    BEGIN
        UPDATE email_drafts SET updated_at=datetime('now') WHERE id=NEW.id;
    END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE workflow_routes_fts USING fts5(
    trigger_patterns,
    required_action,
    reason,
    content=workflow_routes,
    content_rowid=id
);
CREATE VIRTUAL TABLE email_threads_fts USING fts5(
            subject, person_name_cached, topic_tag, context_slug, status,
            content='email_threads',
            content_rowid='rowid'
        );

-- triggers
CREATE TRIGGER email_threads_fts_ai AFTER INSERT ON email_threads BEGIN
            INSERT INTO email_threads_fts(rowid, subject, person_name_cached, topic_tag, context_slug, status)
            VALUES (new.rowid, new.subject, new.person_name_cached, new.topic_tag, new.context_slug, new.status);
        END;
CREATE TRIGGER email_threads_fts_ad AFTER DELETE ON email_threads BEGIN
            INSERT INTO email_threads_fts(email_threads_fts, rowid, subject, person_name_cached, topic_tag, context_slug, status)
            VALUES ('delete', old.rowid, old.subject, old.person_name_cached, old.topic_tag, old.context_slug, old.status);
        END;
CREATE TRIGGER email_threads_fts_au AFTER UPDATE ON email_threads BEGIN
            INSERT INTO email_threads_fts(email_threads_fts, rowid, subject, person_name_cached, topic_tag, context_slug, status)
            VALUES ('delete', old.rowid, old.subject, old.person_name_cached, old.topic_tag, old.context_slug, old.status);
            INSERT INTO email_threads_fts(rowid, subject, person_name_cached, topic_tag, context_slug, status)
            VALUES (new.rowid, new.subject, new.person_name_cached, new.topic_tag, new.context_slug, new.status);
        END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE classification_results_fts USING fts5(
            source, sender, sender_email, subject, body_snippet, category, summary, reasoning,
            content='classification_results',
            content_rowid='rowid'
        );

-- triggers
CREATE TRIGGER classification_results_fts_ai AFTER INSERT ON classification_results BEGIN
            INSERT INTO classification_results_fts(rowid, source, sender, sender_email, subject, body_snippet, category, summary, reasoning)
            VALUES (new.rowid, new.source, new.sender, new.sender_email, new.subject, new.body_snippet, new.category, new.summary, new.reasoning);
        END;
CREATE TRIGGER classification_results_fts_ad AFTER DELETE ON classification_results BEGIN
            INSERT INTO classification_results_fts(classification_results_fts, rowid, source, sender, sender_email, subject, body_snippet, category, summary, reasoning)
            VALUES ('delete', old.rowid, old.source, old.sender, old.sender_email, old.subject, old.body_snippet, old.category, old.summary, old.reasoning);
        END;
CREATE TRIGGER classification_results_fts_au AFTER UPDATE ON classification_results BEGIN
            INSERT INTO classification_results_fts(classification_results_fts, rowid, source, sender, sender_email, subject, body_snippet, category, summary, reasoning)
            VALUES ('delete', old.rowid, old.source, old.sender, old.sender_email, old.subject, old.body_snippet, old.category, old.summary, old.reasoning);
            INSERT INTO classification_results_fts(rowid, source, sender, sender_email, subject, body_snippet, category, summary, reasoning)
            VALUES (new.rowid, new.source, new.sender, new.sender_email, new.subject, new.body_snippet, new.category, new.summary, new.reasoning);
        END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE edges_fts USING fts5(
            relation, fact, source,
            content='edges',
            content_rowid='rowid'
        );

-- triggers
CREATE TRIGGER edges_fts_ai AFTER INSERT ON edges BEGIN
            INSERT INTO edges_fts(rowid, relation, fact, source)
            VALUES (new.rowid, new.relation, new.fact, new.source);
        END;
CREATE TRIGGER edges_fts_ad AFTER DELETE ON edges BEGIN
            INSERT INTO edges_fts(edges_fts, rowid, relation, fact, source)
            VALUES ('delete', old.rowid, old.relation, old.fact, old.source);
        END;
CREATE TRIGGER edges_fts_au AFTER UPDATE ON edges BEGIN
            INSERT INTO edges_fts(edges_fts, rowid, relation, fact, source)
            VALUES ('delete', old.rowid, old.relation, old.fact, old.source);
            INSERT INTO edges_fts(rowid, relation, fact, source)
            VALUES (new.rowid, new.relation, new.fact, new.source);
        END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE briefing_reports_fts USING fts5(
            summary,
            content='briefing_reports',
            content_rowid='rowid'
        );

-- triggers
CREATE TRIGGER briefing_reports_fts_ai AFTER INSERT ON briefing_reports BEGIN
            INSERT INTO briefing_reports_fts(rowid, summary)
            VALUES (new.rowid, new.summary);
        END;
CREATE TRIGGER briefing_reports_fts_ad AFTER DELETE ON briefing_reports BEGIN
            INSERT INTO briefing_reports_fts(briefing_reports_fts, rowid, summary)
            VALUES ('delete', old.rowid, old.summary);
        END;
CREATE TRIGGER briefing_reports_fts_au AFTER UPDATE ON briefing_reports BEGIN
            INSERT INTO briefing_reports_fts(briefing_reports_fts, rowid, summary)
            VALUES ('delete', old.rowid, old.summary);
            INSERT INTO briefing_reports_fts(rowid, summary)
            VALUES (new.rowid, new.summary);
        END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE observations_fts USING fts5(
            subject, subject_type, content, source, confidence,
            content='observations',
            content_rowid='rowid'
        );

-- triggers
CREATE TRIGGER observations_fts_ai AFTER INSERT ON observations BEGIN
            INSERT INTO observations_fts(rowid, subject, subject_type, content, source, confidence) VALUES (new.rowid, new.subject, new.subject_type, new.content, new.source, new.confidence);
        END;
CREATE TRIGGER observations_fts_ad AFTER DELETE ON observations BEGIN
            INSERT INTO observations_fts(observations_fts, rowid, subject, subject_type, content, source, confidence) VALUES ('delete', old.rowid, old.subject, old.subject_type, old.content, old.source, old.confidence);
        END;
CREATE TRIGGER observations_fts_au AFTER UPDATE ON observations BEGIN
            INSERT INTO observations_fts(observations_fts, rowid, subject, subject_type, content, source, confidence) VALUES ('delete', old.rowid, old.subject, old.subject_type, old.content, old.source, old.confidence);
            INSERT INTO observations_fts(rowid, subject, subject_type, content, source, confidence) VALUES (new.rowid, new.subject, new.subject_type, new.content, new.source, new.confidence);
        END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE discord_messages_fts USING fts5(
            content, author_id, channel_id,
            content='discord_messages',
            content_rowid='rowid'
        );

-- triggers
CREATE TRIGGER discord_messages_fts_ai AFTER INSERT ON discord_messages BEGIN
            INSERT INTO discord_messages_fts(rowid, content, author_id, channel_id) VALUES (new.rowid, new.content, new.author_id, new.channel_id);
        END;
CREATE TRIGGER discord_messages_fts_ad AFTER DELETE ON discord_messages BEGIN
            INSERT INTO discord_messages_fts(discord_messages_fts, rowid, content, author_id, channel_id) VALUES ('delete', old.rowid, old.content, old.author_id, old.channel_id);
        END;
CREATE TRIGGER discord_messages_fts_au AFTER UPDATE ON discord_messages BEGIN
            INSERT INTO discord_messages_fts(discord_messages_fts, rowid, content, author_id, channel_id) VALUES ('delete', old.rowid, old.content, old.author_id, old.channel_id);
            INSERT INTO discord_messages_fts(rowid, content, author_id, channel_id) VALUES (new.rowid, new.content, new.author_id, new.channel_id);
        END;
CREATE TRIGGER workflow_routes_fts_ai AFTER INSERT ON workflow_routes BEGIN
    INSERT INTO workflow_routes_fts(rowid, trigger_patterns, required_action, reason)
    VALUES (new.id, new.trigger_patterns, new.required_action, new.reason);
END;
CREATE TRIGGER workflow_routes_fts_ad AFTER DELETE ON workflow_routes BEGIN
    INSERT INTO workflow_routes_fts(workflow_routes_fts, rowid, trigger_patterns, required_action, reason)
    VALUES ('delete', old.id, old.trigger_patterns, old.required_action, old.reason);
END;
CREATE TRIGGER workflow_routes_fts_au AFTER UPDATE ON workflow_routes BEGIN
    INSERT INTO workflow_routes_fts(workflow_routes_fts, rowid, trigger_patterns, required_action, reason)
    VALUES ('delete', old.id, old.trigger_patterns, old.required_action, old.reason);
    INSERT INTO workflow_routes_fts(rowid, trigger_patterns, required_action, reason)
    VALUES (new.id, new.trigger_patterns, new.required_action, new.reason);
END;
CREATE TRIGGER reference_docs_fts_au AFTER UPDATE OF title, content, tags, slug ON reference_docs BEGIN
    INSERT INTO reference_docs_fts(reference_docs_fts, rowid, title, content, tags, slug)
    VALUES ('delete', old.rowid, old.title, old.content, old.tags, old.slug);
    INSERT INTO reference_docs_fts(rowid, title, content, tags, slug)
    VALUES (new.rowid, new.title, new.content, new.tags, new.slug);
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE reference_doc_chunks_fts USING fts5(
    heading, content,
    content='reference_doc_chunks',
    content_rowid='id'
);

-- triggers
CREATE TRIGGER rdc_ai AFTER INSERT ON reference_doc_chunks BEGIN
    INSERT INTO reference_doc_chunks_fts(rowid, heading, content)
    VALUES (new.id, new.heading, new.content);
END;
CREATE TRIGGER rdc_ad AFTER DELETE ON reference_doc_chunks BEGIN
    INSERT INTO reference_doc_chunks_fts(reference_doc_chunks_fts, rowid, heading, content)
    VALUES ('delete', old.id, old.heading, old.content);
END;
CREATE TRIGGER rdc_au AFTER UPDATE ON reference_doc_chunks BEGIN
    INSERT INTO reference_doc_chunks_fts(reference_doc_chunks_fts, rowid, heading, content)
    VALUES ('delete', old.id, old.heading, old.content);
    INSERT INTO reference_doc_chunks_fts(rowid, heading, content)
    VALUES (new.id, new.heading, new.content);
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE faqs_fts USING fts5(
    question_canonical, answer_canonical, topic, notes,
    content=faqs, content_rowid=id,
    tokenize='porter unicode61'
);

-- triggers
CREATE TRIGGER faqs_fts_ai AFTER INSERT ON faqs BEGIN
    INSERT INTO faqs_fts(rowid, question_canonical, answer_canonical, topic, notes)
    VALUES (NEW.id, NEW.question_canonical, NEW.answer_canonical, NEW.topic, NEW.notes);
END;
CREATE TRIGGER faqs_fts_ad AFTER DELETE ON faqs BEGIN
    INSERT INTO faqs_fts(faqs_fts, rowid, question_canonical, answer_canonical, topic, notes)
    VALUES ('delete', OLD.id, OLD.question_canonical, OLD.answer_canonical, OLD.topic, OLD.notes);
END;
CREATE TRIGGER faqs_fts_au AFTER UPDATE ON faqs BEGIN
    INSERT INTO faqs_fts(faqs_fts, rowid, question_canonical, answer_canonical, topic, notes)
    VALUES ('delete', OLD.id, OLD.question_canonical, OLD.answer_canonical, OLD.topic, OLD.notes);
    INSERT INTO faqs_fts(rowid, question_canonical, answer_canonical, topic, notes)
    VALUES (NEW.id, NEW.question_canonical, NEW.answer_canonical, NEW.topic, NEW.notes);
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE faq_occurrences_fts USING fts5(
    raw_question, raw_answer,
    content=faq_occurrences, content_rowid=id,
    tokenize='porter unicode61'
);

-- triggers
CREATE TRIGGER faq_occ_fts_ai AFTER INSERT ON faq_occurrences BEGIN
    INSERT INTO faq_occurrences_fts(rowid, raw_question, raw_answer)
    VALUES (NEW.id, NEW.raw_question, NEW.raw_answer);
END;
CREATE TRIGGER faq_occ_fts_ad AFTER DELETE ON faq_occurrences BEGIN
    INSERT INTO faq_occurrences_fts(faq_occurrences_fts, rowid, raw_question, raw_answer)
    VALUES ('delete', OLD.id, OLD.raw_question, OLD.raw_answer);
END;
CREATE TRIGGER faq_occ_fts_au AFTER UPDATE ON faq_occurrences BEGIN
    INSERT INTO faq_occurrences_fts(faq_occurrences_fts, rowid, raw_question, raw_answer)
    VALUES ('delete', OLD.id, OLD.raw_question, OLD.raw_answer);
    INSERT INTO faq_occurrences_fts(rowid, raw_question, raw_answer)
    VALUES (NEW.id, NEW.raw_question, NEW.raw_answer);
END;
CREATE TRIGGER faq_occ_attach_bump
AFTER UPDATE OF faq_id ON faq_occurrences
WHEN NEW.faq_id IS NOT NULL AND (OLD.faq_id IS NULL OR OLD.faq_id != NEW.faq_id)
BEGIN
    UPDATE faqs
    SET ask_count = ask_count + 1,
        last_asked_at = CASE
            WHEN last_asked_at IS NULL OR NEW.asked_at > last_asked_at
            THEN NEW.asked_at ELSE last_asked_at END,
        first_asked_at = CASE
            WHEN first_asked_at IS NULL OR NEW.asked_at < first_asked_at
            THEN NEW.asked_at ELSE first_asked_at END,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.faq_id;
END;
CREATE TRIGGER faq_occ_insert_bump
AFTER INSERT ON faq_occurrences
WHEN NEW.faq_id IS NOT NULL
BEGIN
    UPDATE faqs
    SET ask_count = ask_count + 1,
        last_asked_at = CASE
            WHEN last_asked_at IS NULL OR NEW.asked_at > last_asked_at
            THEN NEW.asked_at ELSE last_asked_at END,
        first_asked_at = CASE
            WHEN first_asked_at IS NULL OR NEW.asked_at < first_asked_at
            THEN NEW.asked_at ELSE first_asked_at END,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.faq_id;
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE beeper_messages_fts USING fts5(
    text, sender_name, network, chat_id,
    content=beeper_messages, content_rowid=rowid,
    tokenize='porter unicode61'
);

-- triggers
CREATE TRIGGER beeper_msgs_fts_ai AFTER INSERT ON beeper_messages BEGIN
    INSERT INTO beeper_messages_fts(rowid, text, sender_name, network, chat_id)
    VALUES (NEW.rowid, NEW.text, NEW.sender_name, NEW.network, NEW.chat_id);
END;
CREATE TRIGGER beeper_msgs_fts_ad AFTER DELETE ON beeper_messages BEGIN
    INSERT INTO beeper_messages_fts(beeper_messages_fts, rowid, text, sender_name, network, chat_id)
    VALUES ('delete', OLD.rowid, OLD.text, OLD.sender_name, OLD.network, OLD.chat_id);
END;
CREATE TRIGGER beeper_msgs_fts_au AFTER UPDATE ON beeper_messages BEGIN
    INSERT INTO beeper_messages_fts(beeper_messages_fts, rowid, text, sender_name, network, chat_id)
    VALUES ('delete', OLD.rowid, OLD.text, OLD.sender_name, OLD.network, OLD.chat_id);
    INSERT INTO beeper_messages_fts(rowid, text, sender_name, network, chat_id)
    VALUES (NEW.rowid, NEW.text, NEW.sender_name, NEW.network, NEW.chat_id);
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE action_items_inbox_fts USING fts5(
    suggested_description, evidence_quote, suggested_context,
    content=action_items_inbox, content_rowid=id,
    tokenize='porter unicode61'
);

-- triggers
CREATE TRIGGER aii_fts_ai AFTER INSERT ON action_items_inbox BEGIN
    INSERT INTO action_items_inbox_fts(rowid, suggested_description, evidence_quote, suggested_context)
    VALUES (NEW.id, NEW.suggested_description, NEW.evidence_quote, NEW.suggested_context);
END;
CREATE TRIGGER aii_fts_ad AFTER DELETE ON action_items_inbox BEGIN
    INSERT INTO action_items_inbox_fts(action_items_inbox_fts, rowid, suggested_description, evidence_quote, suggested_context)
    VALUES ('delete', OLD.id, OLD.suggested_description, OLD.evidence_quote, OLD.suggested_context);
END;
CREATE TRIGGER aii_fts_au AFTER UPDATE ON action_items_inbox BEGIN
    INSERT INTO action_items_inbox_fts(action_items_inbox_fts, rowid, suggested_description, evidence_quote, suggested_context)
    VALUES ('delete', OLD.id, OLD.suggested_description, OLD.evidence_quote, OLD.suggested_context);
    INSERT INTO action_items_inbox_fts(rowid, suggested_description, evidence_quote, suggested_context)
    VALUES (NEW.id, NEW.suggested_description, NEW.evidence_quote, NEW.suggested_context);
END;
CREATE TRIGGER aii_touch_updated
AFTER UPDATE ON action_items_inbox
FOR EACH ROW
WHEN OLD.updated_at = NEW.updated_at
BEGIN
    UPDATE action_items_inbox SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE system_upgrades_fts USING fts5(
    upgrade_id, title, description, rationale, next_action, domain, component, notes,
    content='system_upgrades', content_rowid='id',
    tokenize='porter unicode61'
);

-- triggers
CREATE TRIGGER su_fts_ai AFTER INSERT ON system_upgrades BEGIN
    INSERT INTO system_upgrades_fts(rowid, upgrade_id, title, description, rationale, next_action, domain, component, notes)
    VALUES (NEW.id, NEW.upgrade_id, NEW.title, NEW.description, NEW.rationale, NEW.next_action, NEW.domain, NEW.component, NEW.notes);
END;
CREATE TRIGGER su_fts_ad AFTER DELETE ON system_upgrades BEGIN
    INSERT INTO system_upgrades_fts(system_upgrades_fts, rowid, upgrade_id, title, description, rationale, next_action, domain, component, notes)
    VALUES ('delete', OLD.id, OLD.upgrade_id, OLD.title, OLD.description, OLD.rationale, OLD.next_action, OLD.domain, OLD.component, OLD.notes);
END;
CREATE TRIGGER su_fts_au AFTER UPDATE ON system_upgrades BEGIN
    INSERT INTO system_upgrades_fts(system_upgrades_fts, rowid, upgrade_id, title, description, rationale, next_action, domain, component, notes)
    VALUES ('delete', OLD.id, OLD.upgrade_id, OLD.title, OLD.description, OLD.rationale, OLD.next_action, OLD.domain, OLD.component, OLD.notes);
    INSERT INTO system_upgrades_fts(rowid, upgrade_id, title, description, rationale, next_action, domain, component, notes)
    VALUES (NEW.id, NEW.upgrade_id, NEW.title, NEW.description, NEW.rationale, NEW.next_action, NEW.domain, NEW.component, NEW.notes);
END;
CREATE TRIGGER su_touch_updated
AFTER UPDATE ON system_upgrades
FOR EACH ROW
WHEN OLD.updated_at = NEW.updated_at
BEGIN
    UPDATE system_upgrades SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;
CREATE TRIGGER trg_ai_status_change_stamp
AFTER UPDATE OF status ON action_items
FOR EACH ROW
WHEN OLD.status IS NOT NEW.status
BEGIN
    UPDATE action_items
       SET last_status_change_at = CURRENT_TIMESTAMP
     WHERE id = NEW.id;
END;
CREATE TRIGGER trg_et_status_change_stamp
AFTER UPDATE OF status ON email_threads
FOR EACH ROW
WHEN OLD.status IS NOT NEW.status
BEGIN
    UPDATE email_threads
       SET last_status_change_at = CURRENT_TIMESTAMP
     WHERE thread_id = NEW.thread_id;
END;
CREATE TRIGGER email_thread_slug_to_episodes
AFTER UPDATE OF context_slug ON email_threads
WHEN NEW.context_slug IS NOT NULL AND NEW.context_slug != ''
  AND (OLD.context_slug IS NULL OR OLD.context_slug != NEW.context_slug)
BEGIN
    UPDATE episodes
    SET context_slug = NEW.context_slug
    WHERE source_table = 'emails'
      AND (context_slug IS NULL OR context_slug = '')
      AND source_id IN (SELECT id FROM emails WHERE thread_id = NEW.thread_id);
END;
CREATE TRIGGER discord_thread_slug_to_episodes
AFTER UPDATE OF context_slug ON discord_threads
WHEN NEW.context_slug IS NOT NULL AND NEW.context_slug != ''
  AND (OLD.context_slug IS NULL OR OLD.context_slug != NEW.context_slug)
BEGIN
    UPDATE episodes
    SET context_slug = NEW.context_slug
    WHERE source_table = 'discord_messages'
      AND (context_slug IS NULL OR context_slug = '')
      AND CAST(source_id AS TEXT) IN (
          SELECT id FROM discord_messages WHERE thread_id = NEW.thread_id
      );
END;
CREATE TRIGGER discord_thread_insert_slug_to_episodes
AFTER INSERT ON discord_threads
WHEN NEW.context_slug IS NOT NULL AND NEW.context_slug != ''
BEGIN
    UPDATE episodes
    SET context_slug = NEW.context_slug
    WHERE source_table = 'discord_messages'
      AND (context_slug IS NULL OR context_slug = '')
      AND CAST(source_id AS TEXT) IN (
          SELECT id FROM discord_messages WHERE thread_id = NEW.thread_id
      );
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE people_fts USING fts5(
    name, headline, summary, notes,
    research_interests, tags, skills, capability,
    content=people, content_rowid=rowid,
    tokenize='porter unicode61 remove_diacritics 2'
);

-- triggers
CREATE TRIGGER people_fts_ai AFTER INSERT ON people BEGIN
    INSERT INTO people_fts(rowid, name, headline, summary, notes,
                           research_interests, tags, skills, capability)
    VALUES (new.rowid, new.name, new.headline, new.summary, new.notes,
            new.research_interests, new.tags, new.skills, new.capability);
END;
CREATE TRIGGER people_fts_ad AFTER DELETE ON people BEGIN
    INSERT INTO people_fts(people_fts, rowid, name, headline, summary, notes,
                           research_interests, tags, skills, capability)
    VALUES ('delete', old.rowid, old.name, old.headline, old.summary, old.notes,
            old.research_interests, old.tags, old.skills, old.capability);
END;
CREATE TRIGGER people_fts_au AFTER UPDATE OF
    name, headline, summary, notes, research_interests, tags, skills, capability
ON people BEGIN
    INSERT INTO people_fts(people_fts, rowid, name, headline, summary, notes,
                           research_interests, tags, skills, capability)
    VALUES ('delete', old.rowid, old.name, old.headline, old.summary, old.notes,
            old.research_interests, old.tags, old.skills, old.capability);
    INSERT INTO people_fts(rowid, name, headline, summary, notes,
                           research_interests, tags, skills, capability)
    VALUES (new.rowid, new.name, new.headline, new.summary, new.notes,
            new.research_interests, new.tags, new.skills, new.capability);
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE emails_fts USING fts5(
    subject, body,
    content=emails, content_rowid=rowid,
    tokenize='porter unicode61 remove_diacritics 2'
);

-- triggers
CREATE TRIGGER emails_fts_ai AFTER INSERT ON emails BEGIN
    INSERT INTO emails_fts(rowid, subject, body)
    VALUES (new.rowid, new.subject, new.body);
END;
CREATE TRIGGER emails_fts_ad AFTER DELETE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, body)
    VALUES ('delete', old.rowid, old.subject, old.body);
END;
CREATE TRIGGER emails_fts_au AFTER UPDATE OF subject, body ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, body)
    VALUES ('delete', old.rowid, old.subject, old.body);
    INSERT INTO emails_fts(rowid, subject, body)
    VALUES (new.rowid, new.subject, new.body);
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE learnings_fts USING fts5(
    title, description, apply_when, context, tags,
    content='learnings', content_rowid='rowid',
    tokenize='porter unicode61 remove_diacritics 2'
);

-- triggers
CREATE TRIGGER learnings_fts_ai AFTER INSERT ON learnings BEGIN
    INSERT INTO learnings_fts(rowid, title, description, apply_when, context, tags)
    VALUES (new.rowid, new.title, new.description, new.apply_when, new.context, new.tags);
END;
CREATE TRIGGER learnings_fts_ad AFTER DELETE ON learnings BEGIN
    INSERT INTO learnings_fts(learnings_fts, rowid, title, description, apply_when, context, tags)
    VALUES ('delete', old.rowid, old.title, old.description, old.apply_when, old.context, old.tags);
END;
CREATE TRIGGER learnings_fts_au AFTER UPDATE OF title, description, apply_when, context, tags ON learnings BEGIN
    INSERT INTO learnings_fts(learnings_fts, rowid, title, description, apply_when, context, tags)
    VALUES ('delete', old.rowid, old.title, old.description, old.apply_when, old.context, old.tags);
    INSERT INTO learnings_fts(rowid, title, description, apply_when, context, tags)
    VALUES (new.rowid, new.title, new.description, new.apply_when, new.context, new.tags);
END;
CREATE TRIGGER trg_action_items_canonical_source
BEFORE INSERT ON action_items
WHEN COALESCE(LOWER(NEW.source), '') NOT IN (
    'manual','operator','operator-verbal','template_step','inbox-promoted','wrap-up-confirmed'
)
BEGIN
  SELECT RAISE(FAIL,
    'action_items requires canonical source (manual/operator/operator-verbal/template_step/inbox-promoted/wrap-up-confirmed). Use task_manager.py add or validators.propose_to_inbox. '
  );
END;
CREATE TRIGGER discord_channels_to_entities_ai
AFTER INSERT ON discord_channels
WHEN NEW.channel_type IN ('dm','group_dm')
BEGIN
    INSERT INTO entities (id, type, name, data, source, status, created_at, updated_at)
    SELECT
        NEW.id,
        'discord_channel',
        coalesce(NEW.name, NEW.dm_recipient_username, 'DM ' || NEW.id),
        json_object(
            'channel_type', NEW.channel_type,
            'dm_recipient_id', NEW.dm_recipient_id,
            'dm_recipient_username', NEW.dm_recipient_username,
            'group_dm_recipient_ids', NEW.group_dm_recipient_ids,
            'context_slug', NEW.context_slug
        ),
        'discord_channels_table',
        'active',
        coalesce(NEW.imported_at, datetime('now')),
        coalesce(NEW.imported_at, datetime('now'))
    WHERE NOT EXISTS (SELECT 1 FROM entities WHERE id = NEW.id);

    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.id, 'person-' || NEW.dm_recipient_person_id, 'with_person', 'trigger:discord_channel_insert'
    WHERE NEW.dm_recipient_person_id IS NOT NULL
      AND EXISTS (SELECT 1 FROM entities WHERE id = 'person-' || NEW.dm_recipient_person_id)
      AND NOT EXISTS (SELECT 1 FROM edges
                      WHERE source_id = NEW.id
                        AND target_id = 'person-' || NEW.dm_recipient_person_id
                        AND relation = 'with_person');
END;
CREATE TRIGGER discord_channels_to_entities_au
AFTER UPDATE ON discord_channels
WHEN NEW.channel_type IN ('dm','group_dm')
  AND (
      coalesce(OLD.name,'') != coalesce(NEW.name,'')
      OR coalesce(OLD.dm_recipient_username,'') != coalesce(NEW.dm_recipient_username,'')
      OR coalesce(OLD.dm_recipient_person_id, 0) != coalesce(NEW.dm_recipient_person_id, 0)
      OR coalesce(OLD.context_slug,'') != coalesce(NEW.context_slug,'')
  )
BEGIN
    INSERT INTO entities (id, type, name, data, source, status, created_at, updated_at)
    SELECT
        NEW.id, 'discord_channel',
        coalesce(NEW.name, NEW.dm_recipient_username, 'DM ' || NEW.id),
        json_object(
            'channel_type', NEW.channel_type,
            'dm_recipient_id', NEW.dm_recipient_id,
            'dm_recipient_username', NEW.dm_recipient_username,
            'group_dm_recipient_ids', NEW.group_dm_recipient_ids,
            'context_slug', NEW.context_slug
        ),
        'discord_channels_table', 'active',
        datetime('now'), datetime('now')
    WHERE NOT EXISTS (SELECT 1 FROM entities WHERE id = NEW.id);

    UPDATE entities
    SET name = coalesce(NEW.name, NEW.dm_recipient_username, 'DM ' || NEW.id),
        data = json_object(
            'channel_type', NEW.channel_type,
            'dm_recipient_id', NEW.dm_recipient_id,
            'dm_recipient_username', NEW.dm_recipient_username,
            'group_dm_recipient_ids', NEW.group_dm_recipient_ids,
            'context_slug', NEW.context_slug
        ),
        updated_at = datetime('now')
    WHERE id = NEW.id AND type = 'discord_channel';

    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.id, 'person-' || NEW.dm_recipient_person_id, 'with_person', 'trigger:discord_channel_update'
    WHERE NEW.dm_recipient_person_id IS NOT NULL
      AND EXISTS (SELECT 1 FROM entities WHERE id = 'person-' || NEW.dm_recipient_person_id)
      AND NOT EXISTS (SELECT 1 FROM edges
                      WHERE source_id = NEW.id
                        AND target_id = 'person-' || NEW.dm_recipient_person_id
                        AND relation = 'with_person');
END;
CREATE TRIGGER discord_users_to_group_dm_edges
AFTER UPDATE OF person_id ON discord_users
WHEN OLD.person_id IS NULL AND NEW.person_id IS NOT NULL
BEGIN
    INSERT INTO entities (id, type, name, data, source, status, created_at, updated_at)
    SELECT dc.id, 'discord_channel',
           coalesce(dc.name, 'Group DM ' || dc.id),
           json_object(
               'channel_type', dc.channel_type,
               'group_dm_recipient_ids', dc.group_dm_recipient_ids,
               'context_slug', dc.context_slug
           ),
           'group_dm_user_link_trigger', 'active',
           coalesce(dc.imported_at, datetime('now')),
           datetime('now')
    FROM discord_channels dc
    WHERE dc.channel_type = 'group_dm'
      AND (
          dc.group_dm_recipient_ids = NEW.id
          OR dc.group_dm_recipient_ids LIKE (NEW.id || ',%')
          OR dc.group_dm_recipient_ids LIKE ('%,' || NEW.id || ',%')
          OR dc.group_dm_recipient_ids LIKE ('%,' || NEW.id)
      )
      AND NOT EXISTS (SELECT 1 FROM entities e WHERE e.id = dc.id);

    INSERT INTO edges (source_id, target_id, relation, fact, source)
    SELECT dc.id, 'person-' || NEW.person_id, 'with_person',
           'Group DM participant ' || NEW.id,
           'trigger:discord_users_to_group_dm'
    FROM discord_channels dc
    WHERE dc.channel_type = 'group_dm'
      AND (
          dc.group_dm_recipient_ids = NEW.id
          OR dc.group_dm_recipient_ids LIKE (NEW.id || ',%')
          OR dc.group_dm_recipient_ids LIKE ('%,' || NEW.id || ',%')
          OR dc.group_dm_recipient_ids LIKE ('%,' || NEW.id)
      )
      AND EXISTS (SELECT 1 FROM entities WHERE id = 'person-' || NEW.person_id)
      AND NOT EXISTS (SELECT 1 FROM edges
                      WHERE source_id = dc.id
                        AND target_id = 'person-' || NEW.person_id
                        AND relation = 'with_person');
END;
CREATE TRIGGER action_items_to_entities_ai
AFTER INSERT ON action_items
WHEN NEW.item_id IS NOT NULL
BEGIN
    INSERT INTO entities (id, type, name, data, source, status, created_at, updated_at)
    SELECT
        NEW.item_id,
        'action_item',
        substr(replace(replace(coalesce(NEW.description, NEW.item_id), char(10),' '), char(13),' '), 1, 80),
        json_object('priority', NEW.priority, 'status', NEW.status, 'context_slug', NEW.context_slug, 'source', NEW.source, 'source_url', NEW.source_url),
        'action_items_table',
        CASE WHEN NEW.status IN ('DONE','REMOVED','CANCELLED','RESOLVED') THEN 'resolved' ELSE 'active' END,
        coalesce(NEW.inserted_at, datetime('now')),
        coalesce(NEW.updated_at, datetime('now'))
    WHERE NOT EXISTS (SELECT 1 FROM entities WHERE id = NEW.item_id);

    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.item_id, NEW.email_thread_id, 'derived_from', 'trigger:action_item_insert'
    WHERE NEW.email_thread_id IS NOT NULL AND EXISTS (SELECT 1 FROM entities WHERE id = NEW.email_thread_id)
      AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = NEW.item_id AND target_id = NEW.email_thread_id AND relation = 'derived_from');
    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.item_id, 'person-' || NEW.source_person_id, 'from_person', 'trigger:action_item_insert'
    WHERE NEW.source_person_id IS NOT NULL AND EXISTS (SELECT 1 FROM entities WHERE id = 'person-' || NEW.source_person_id)
      AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = NEW.item_id AND target_id = 'person-' || NEW.source_person_id AND relation = 'from_person');
    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.item_id, 'person-' || NEW.about_person_id, 'about_person', 'trigger:action_item_insert'
    WHERE NEW.about_person_id IS NOT NULL AND EXISTS (SELECT 1 FROM entities WHERE id = 'person-' || NEW.about_person_id)
      AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = NEW.item_id AND target_id = 'person-' || NEW.about_person_id AND relation = 'about_person');
    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.item_id, 'person-' || NEW.waiting_on_person_id, 'waiting_on', 'trigger:action_item_insert'
    WHERE NEW.waiting_on_person_id IS NOT NULL AND EXISTS (SELECT 1 FROM entities WHERE id = 'person-' || NEW.waiting_on_person_id)
      AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = NEW.item_id AND target_id = 'person-' || NEW.waiting_on_person_id AND relation = 'waiting_on');
    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.item_id, 'ctx-' || NEW.context_slug, 'for_hackathon', 'trigger:action_item_insert'
    WHERE NEW.context_slug IS NOT NULL AND EXISTS (SELECT 1 FROM entities WHERE id = 'ctx-' || NEW.context_slug)
      AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = NEW.item_id AND target_id = 'ctx-' || NEW.context_slug AND relation = 'for_hackathon');
END;
CREATE TRIGGER action_items_to_entities_au
AFTER UPDATE ON action_items
WHEN NEW.item_id IS NOT NULL
  AND (OLD.status != NEW.status OR OLD.description != NEW.description OR OLD.priority != NEW.priority)
BEGIN
    UPDATE entities
    SET status = CASE WHEN NEW.status IN ('DONE','REMOVED','CANCELLED','RESOLVED') THEN 'resolved' ELSE 'active' END,
        name = substr(replace(replace(coalesce(NEW.description, NEW.item_id), char(10),' '), char(13),' '), 1, 80),
        data = json_object('priority', NEW.priority, 'status', NEW.status, 'context_slug', NEW.context_slug, 'source', NEW.source, 'source_url', NEW.source_url),
        updated_at = datetime('now')
    WHERE id = NEW.item_id AND type = 'action_item';

    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.item_id, NEW.email_thread_id, 'derived_from', 'trigger:action_item_update'
    WHERE NEW.email_thread_id IS NOT NULL AND EXISTS (SELECT 1 FROM entities WHERE id = NEW.email_thread_id)
      AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = NEW.item_id AND target_id = NEW.email_thread_id AND relation = 'derived_from');
    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.item_id, 'person-' || NEW.about_person_id, 'about_person', 'trigger:action_item_update'
    WHERE NEW.about_person_id IS NOT NULL AND EXISTS (SELECT 1 FROM entities WHERE id = 'person-' || NEW.about_person_id)
      AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = NEW.item_id AND target_id = 'person-' || NEW.about_person_id AND relation = 'about_person');
    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.item_id, 'person-' || NEW.waiting_on_person_id, 'waiting_on', 'trigger:action_item_update'
    WHERE NEW.waiting_on_person_id IS NOT NULL AND EXISTS (SELECT 1 FROM entities WHERE id = 'person-' || NEW.waiting_on_person_id)
      AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = NEW.item_id AND target_id = 'person-' || NEW.waiting_on_person_id AND relation = 'waiting_on');
END;
CREATE TRIGGER email_threads_to_entities_ai
AFTER INSERT ON email_threads
WHEN NEW.thread_id IS NOT NULL
BEGIN
    INSERT INTO entities (id, type, name, data, source, status, created_at, updated_at)
    SELECT
        NEW.thread_id,
        'email_thread',
        substr(replace(replace(coalesce(NEW.subject, NEW.thread_id), char(10),' '), char(13),' '), 1, 80),
        json_object('status', NEW.status, 'person_id', NEW.person_id, 'context_slug', NEW.context_slug, 'action_item_id', NEW.action_item_id, 'last_inbound_ts', NEW.last_inbound_ts, 'last_outbound_ts', NEW.last_outbound_ts),
        'email_threads_table',
        CASE WHEN NEW.status IN ('resolved','resolved_elsewhere','no_action_needed','archived','done','closed') THEN 'resolved' ELSE 'active' END,
        coalesce(NEW.first_ts, datetime('now')),
        coalesce(NEW.updated_at, datetime('now'))
    WHERE NOT EXISTS (SELECT 1 FROM entities WHERE id = NEW.thread_id);

    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.thread_id, 'person-' || NEW.person_id, 'with_person', 'trigger:email_thread_insert'
    WHERE NEW.person_id IS NOT NULL AND EXISTS (SELECT 1 FROM entities WHERE id = 'person-' || NEW.person_id)
      AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = NEW.thread_id AND target_id = 'person-' || NEW.person_id AND relation = 'with_person');
    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.thread_id, 'ctx-' || NEW.context_slug, 'for_hackathon', 'trigger:email_thread_insert'
    WHERE NEW.context_slug IS NOT NULL AND EXISTS (SELECT 1 FROM entities WHERE id = 'ctx-' || NEW.context_slug)
      AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = NEW.thread_id AND target_id = 'ctx-' || NEW.context_slug AND relation = 'for_hackathon');
    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.thread_id, NEW.action_item_id, 'resolved_by', 'trigger:email_thread_insert'
    WHERE NEW.action_item_id IS NOT NULL AND EXISTS (SELECT 1 FROM entities WHERE id = NEW.action_item_id)
      AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = NEW.thread_id AND target_id = NEW.action_item_id AND relation = 'resolved_by');
END;
CREATE TRIGGER email_threads_to_entities_au
AFTER UPDATE ON email_threads
WHEN NEW.thread_id IS NOT NULL
  AND (OLD.status != NEW.status OR OLD.subject != NEW.subject OR OLD.action_item_id IS NOT NEW.action_item_id)
BEGIN
    INSERT INTO entities (id, type, name, data, source, status, created_at, updated_at)
    SELECT
        NEW.thread_id,
        'email_thread',
        substr(replace(replace(coalesce(NEW.subject, NEW.thread_id), char(10),' '), char(13),' '), 1, 80),
        json_object('status', NEW.status, 'person_id', NEW.person_id, 'context_slug', NEW.context_slug, 'action_item_id', NEW.action_item_id),
        'email_threads_table',
        CASE WHEN NEW.status IN ('resolved','resolved_elsewhere','no_action_needed','archived','done','closed') THEN 'resolved' ELSE 'active' END,
        coalesce(NEW.first_ts, datetime('now')),
        datetime('now')
    WHERE NOT EXISTS (SELECT 1 FROM entities WHERE id = NEW.thread_id);

    UPDATE entities
    SET status = CASE WHEN NEW.status IN ('resolved','resolved_elsewhere','no_action_needed','archived','done','closed') THEN 'resolved' ELSE 'active' END,
        name = substr(replace(replace(coalesce(NEW.subject, NEW.thread_id), char(10),' '), char(13),' '), 1, 80),
        data = json_object('status', NEW.status, 'person_id', NEW.person_id, 'context_slug', NEW.context_slug, 'action_item_id', NEW.action_item_id, 'last_inbound_ts', NEW.last_inbound_ts, 'last_outbound_ts', NEW.last_outbound_ts),
        updated_at = datetime('now')
    WHERE id = NEW.thread_id AND type = 'email_thread';

    INSERT INTO edges (source_id, target_id, relation, source)
    SELECT NEW.thread_id, NEW.action_item_id, 'resolved_by', 'trigger:email_thread_update'
    WHERE NEW.action_item_id IS NOT NULL AND EXISTS (SELECT 1 FROM entities WHERE id = NEW.action_item_id)
      AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = NEW.thread_id AND target_id = NEW.action_item_id AND relation = 'resolved_by');
END;
CREATE TRIGGER people_to_entities_ai
AFTER INSERT ON people
BEGIN
    INSERT INTO entities (id, type, name, data, source, status, created_at, updated_at)
    SELECT
        'person-' || NEW.id,
        'person',
        coalesce(NEW.name, NEW.email, 'person-' || NEW.id),
        json_object('people_id', NEW.id, 'email', NEW.email, 'headline', NEW.headline),
        'people_table',
        'active',
        coalesce(NEW.created_at, datetime('now')),
        datetime('now')
    WHERE NOT EXISTS (SELECT 1 FROM entities WHERE id = 'person-' || NEW.id);
END;
CREATE TRIGGER emails_reopen_thread_ai
AFTER INSERT ON emails
WHEN NEW.is_outgoing = 0 AND NEW.thread_id IS NOT NULL
BEGIN
    UPDATE email_threads
       SET status = 'pending',
           updated_at = datetime('now'),
           last_status_change_at = datetime('now'),
           resolution_note = COALESCE(resolution_note,'') || ' [auto-reopened on new inbound]'
     WHERE thread_id = NEW.thread_id
       AND status IN ('resolved','resolved_elsewhere','no_action_needed','expired',
                      'info_captured','stale','replied','no_inbound');
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE staging_fts USING fts5(
    payload, trace_json, error, submitted_by, content='staging', content_rowid='id'
);

-- triggers
CREATE TRIGGER staging_fts_ai AFTER INSERT ON staging BEGIN
    INSERT INTO staging_fts(rowid,payload,trace_json,error,submitted_by)
    VALUES(new.id,new.payload,new.trace_json,new.error,new.submitted_by);
END;
CREATE TRIGGER staging_fts_ad AFTER DELETE ON staging BEGIN
    INSERT INTO staging_fts(staging_fts,rowid,payload,trace_json,error,submitted_by)
    VALUES('delete',old.id,old.payload,old.trace_json,old.error,old.submitted_by);
END;
CREATE TRIGGER staging_fts_au AFTER UPDATE ON staging BEGIN
    INSERT INTO staging_fts(staging_fts,rowid,payload,trace_json,error,submitted_by)
    VALUES('delete',old.id,old.payload,old.trace_json,old.error,old.submitted_by);
    INSERT INTO staging_fts(rowid,payload,trace_json,error,submitted_by)
    VALUES(new.id,new.payload,new.trace_json,new.error,new.submitted_by);
END;
CREATE TRIGGER cdc_people_ai AFTER INSERT ON people BEGIN
  INSERT INTO cdc_log(table_name,row_id,op,actor)
  VALUES('people',new.id,'INSERT',(SELECT actor FROM _audit_context WHERE id=1));
END;
CREATE TRIGGER cdc_people_au AFTER UPDATE ON people BEGIN
  INSERT INTO cdc_log(table_name,row_id,op,actor)
  VALUES('people',new.id,'UPDATE',(SELECT actor FROM _audit_context WHERE id=1));
END;
CREATE TRIGGER cdc_edges_ai AFTER INSERT ON edges BEGIN
  INSERT INTO cdc_log(table_name,row_id,op,actor)
  VALUES('edges',new.id,'INSERT',(SELECT actor FROM _audit_context WHERE id=1));
END;
CREATE TRIGGER review_queue_trace_required BEFORE INSERT ON review_queue
WHEN NEW.trace_json IS NULL
BEGIN SELECT RAISE(ABORT, 'review_queue requires trace_json (no queue without a trace)'); END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE comms_drafts_fts USING fts5(
    inbound_snippet, draft_text, sender_email, gate_reason,
    content='comms_drafts', content_rowid='id'
);

-- triggers
CREATE TRIGGER comms_drafts_fts_ai AFTER INSERT ON comms_drafts BEGIN
    INSERT INTO comms_drafts_fts(rowid,inbound_snippet,draft_text,sender_email,gate_reason)
    VALUES(new.id,new.inbound_snippet,new.draft_text,new.sender_email,new.gate_reason);
END;
CREATE TRIGGER comms_drafts_fts_ad AFTER DELETE ON comms_drafts BEGIN
    INSERT INTO comms_drafts_fts(comms_drafts_fts,rowid,inbound_snippet,draft_text,sender_email,gate_reason)
    VALUES('delete',old.id,old.inbound_snippet,old.draft_text,old.sender_email,old.gate_reason);
END;
CREATE TRIGGER comms_drafts_fts_au AFTER UPDATE ON comms_drafts BEGIN
    INSERT INTO comms_drafts_fts(comms_drafts_fts,rowid,inbound_snippet,draft_text,sender_email,gate_reason)
    VALUES('delete',old.id,old.inbound_snippet,old.draft_text,old.sender_email,old.gate_reason);
    INSERT INTO comms_drafts_fts(rowid,inbound_snippet,draft_text,sender_email,gate_reason)
    VALUES(new.id,new.inbound_snippet,new.draft_text,new.sender_email,new.gate_reason);
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE consolidation_log_fts USING fts5(rationale, method, status, content='consolidation_log', content_rowid='id');

-- triggers
CREATE TRIGGER consolidation_log_ai AFTER INSERT ON consolidation_log BEGIN
        INSERT INTO consolidation_log_fts(rowid, rationale, method, status) VALUES (new.id, new.rationale, new.method, new.status);
    END;
CREATE TRIGGER consolidation_log_ad AFTER DELETE ON consolidation_log BEGIN
        INSERT INTO consolidation_log_fts(consolidation_log_fts, rowid, rationale, method, status) VALUES('delete', old.id, old.rationale, old.method, old.status);
    END;
CREATE TRIGGER consolidation_log_au AFTER UPDATE ON consolidation_log BEGIN
        INSERT INTO consolidation_log_fts(consolidation_log_fts, rowid, rationale, method, status) VALUES('delete', old.id, old.rationale, old.method, old.status);
        INSERT INTO consolidation_log_fts(rowid, rationale, method, status) VALUES (new.id, new.rationale, new.method, new.status);
    END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE learning_health_snapshots_fts USING fts5(note, content='learning_health_snapshots', content_rowid='id');

-- triggers
CREATE TRIGGER lhs_ai AFTER INSERT ON learning_health_snapshots BEGIN INSERT INTO learning_health_snapshots_fts(rowid, note) VALUES (new.id, new.note); END;
CREATE TRIGGER lhs_ad AFTER DELETE ON learning_health_snapshots BEGIN INSERT INTO learning_health_snapshots_fts(learning_health_snapshots_fts, rowid, note) VALUES('delete', old.id, old.note); END;
CREATE TRIGGER lhs_au AFTER UPDATE ON learning_health_snapshots BEGIN INSERT INTO learning_health_snapshots_fts(learning_health_snapshots_fts, rowid, note) VALUES('delete', old.id, old.note); INSERT INTO learning_health_snapshots_fts(rowid, note) VALUES (new.id, new.note); END;
CREATE TRIGGER cdc_edges_au AFTER UPDATE ON edges BEGIN
  INSERT INTO cdc_log(table_name,row_id,op,actor)
  VALUES('edges',new.id,'UPDATE',(SELECT actor FROM _audit_context WHERE id=1));
END;
CREATE TRIGGER cdc_people_ad AFTER DELETE ON people BEGIN
  INSERT INTO cdc_log(table_name,row_id,op,actor)
  VALUES('people',old.id,'DELETE',(SELECT actor FROM _audit_context WHERE id=1));
END;
CREATE TRIGGER cdc_edges_ad AFTER DELETE ON edges BEGIN
  INSERT INTO cdc_log(table_name,row_id,op,actor)
  VALUES('edges',old.id,'DELETE',(SELECT actor FROM _audit_context WHERE id=1));
END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE person_identifiers_fts USING fts5(
    id_value_norm, content='person_identifiers', content_rowid='id'
);

-- triggers
CREATE TRIGGER person_identifiers_fts_ai AFTER INSERT ON person_identifiers BEGIN
  INSERT INTO person_identifiers_fts(rowid, id_value_norm) VALUES (new.id, new.id_value_norm);
END;
CREATE TRIGGER person_identifiers_fts_ad AFTER DELETE ON person_identifiers BEGIN
  INSERT INTO person_identifiers_fts(person_identifiers_fts, rowid, id_value_norm) VALUES('delete', old.id, old.id_value_norm);
END;
CREATE TRIGGER person_identifiers_fts_au AFTER UPDATE ON person_identifiers BEGIN
  INSERT INTO person_identifiers_fts(person_identifiers_fts, rowid, id_value_norm) VALUES('delete', old.id, old.id_value_norm);
  INSERT INTO person_identifiers_fts(rowid, id_value_norm) VALUES (new.id, new.id_value_norm);
END;
CREATE TRIGGER gate_people_ai BEFORE INSERT ON people
WHEN (SELECT value FROM steward_config WHERE key='write_gate_mode')='blocking'
 AND (SELECT actor FROM _audit_context WHERE id=1) IS NULL
BEGIN SELECT RAISE(ABORT,'write_gate(blocking): NULL-actor people INSERT'); END;
CREATE TRIGGER gate_people_au BEFORE UPDATE ON people
WHEN (SELECT value FROM steward_config WHERE key='write_gate_mode')='blocking'
 AND (SELECT actor FROM _audit_context WHERE id=1) IS NULL
BEGIN SELECT RAISE(ABORT,'write_gate(blocking): NULL-actor people UPDATE'); END;
CREATE TRIGGER gate_edges_ai BEFORE INSERT ON edges
WHEN (SELECT value FROM steward_config WHERE key='write_gate_mode')='blocking'
 AND (SELECT actor FROM _audit_context WHERE id=1) IS NULL
BEGIN SELECT RAISE(ABORT,'write_gate(blocking): NULL-actor edges INSERT'); END;
CREATE TRIGGER gate_edges_au BEFORE UPDATE ON edges
WHEN (SELECT value FROM steward_config WHERE key='write_gate_mode')='blocking'
 AND (SELECT actor FROM _audit_context WHERE id=1) IS NULL
BEGIN SELECT RAISE(ABORT,'write_gate(blocking): NULL-actor edges UPDATE'); END;
CREATE TRIGGER mc_self_pair_guard_bi BEFORE INSERT ON merge_candidates
WHEN NEW.canonical_id = NEW.duplicate_id
BEGIN SELECT RAISE(ABORT, 'merge_candidates: canonical_id==duplicate_id self-pair rejected'); END;
CREATE TRIGGER mc_self_pair_guard_bu BEFORE UPDATE ON merge_candidates
WHEN NEW.canonical_id = NEW.duplicate_id
 AND (OLD.canonical_id <> NEW.canonical_id OR OLD.duplicate_id <> NEW.duplicate_id)
BEGIN SELECT RAISE(ABORT, 'merge_candidates: canonical_id==duplicate_id self-pair rejected'); END;
CREATE TRIGGER gate_edges_ad BEFORE DELETE ON edges
WHEN (SELECT value FROM steward_config WHERE key='write_gate_mode')='blocking'
 AND (SELECT actor FROM _audit_context WHERE id=1) IS NULL
BEGIN SELECT RAISE(ABORT,'write_gate(blocking): NULL-actor edges DELETE'); END;
CREATE TRIGGER gate_people_ad BEFORE DELETE ON people
WHEN (SELECT value FROM steward_config WHERE key='write_gate_mode')='blocking'
 AND (SELECT actor FROM _audit_context WHERE id=1) IS NULL
BEGIN SELECT RAISE(ABORT,'write_gate(blocking): NULL-actor people DELETE'); END;
CREATE TRIGGER entities_fts_ai AFTER INSERT ON entities BEGIN
    INSERT INTO entities_fts(rowid, name, data, source)
    VALUES (new.rowid, new.name, new.data, new.source);
END;
CREATE TRIGGER entities_fts_ad AFTER DELETE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, name, data, source)
    VALUES ('delete', old.rowid, old.name, old.data, old.source);
END;
CREATE TRIGGER entities_fts_au AFTER UPDATE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, name, data, source)
    VALUES ('delete', old.rowid, old.name, old.data, old.source);
    INSERT INTO entities_fts(rowid, name, data, source)
    VALUES (new.rowid, new.name, new.data, new.source);
END;
CREATE TRIGGER cdc_entities_ai AFTER INSERT ON entities BEGIN
  INSERT INTO cdc_log(table_name,row_id,op,actor)
  VALUES('entities',new.id,'INSERT',(SELECT actor FROM _audit_context WHERE id=1));
END;
CREATE TRIGGER cdc_entities_au AFTER UPDATE ON entities BEGIN
  INSERT INTO cdc_log(table_name,row_id,op,actor)
  VALUES('entities',new.id,'UPDATE',(SELECT actor FROM _audit_context WHERE id=1));
END;
CREATE TRIGGER cdc_entities_ad AFTER DELETE ON entities BEGIN
  INSERT INTO cdc_log(table_name,row_id,op,actor)
  VALUES('entities',old.id,'DELETE',(SELECT actor FROM _audit_context WHERE id=1));
END;
CREATE TRIGGER gate_entities_ai BEFORE INSERT ON entities
WHEN (SELECT value FROM steward_config WHERE key='write_gate_mode')='blocking'
 AND (SELECT actor FROM _audit_context WHERE id=1) IS NULL
BEGIN SELECT RAISE(ABORT,'write_gate(blocking): NULL-actor entities INSERT'); END;
CREATE TRIGGER gate_entities_au BEFORE UPDATE ON entities
WHEN (SELECT value FROM steward_config WHERE key='write_gate_mode')='blocking'
 AND (SELECT actor FROM _audit_context WHERE id=1) IS NULL
BEGIN SELECT RAISE(ABORT,'write_gate(blocking): NULL-actor entities UPDATE'); END;
CREATE TRIGGER gate_entities_ad BEFORE DELETE ON entities
WHEN (SELECT value FROM steward_config WHERE key='write_gate_mode')='blocking'
 AND (SELECT actor FROM _audit_context WHERE id=1) IS NULL
BEGIN SELECT RAISE(ABORT,'write_gate(blocking): NULL-actor entities DELETE'); END;

-- FTS5 mirrors
CREATE VIRTUAL TABLE session_log_entries_fts USING fts5(
        title, raw_md, content='session_log_entries', content_rowid='id');

-- triggers
CREATE TRIGGER session_log_entries_ai
        AFTER INSERT ON session_log_entries BEGIN
            INSERT INTO session_log_entries_fts(rowid, title, raw_md)
            VALUES (new.id, new.title, new.raw_md);
        END;
CREATE TRIGGER session_log_entries_ad
        AFTER DELETE ON session_log_entries BEGIN
            INSERT INTO session_log_entries_fts(session_log_entries_fts, rowid, title, raw_md)
            VALUES ('delete', old.id, old.title, old.raw_md);
        END;
CREATE TRIGGER session_log_entries_au
        AFTER UPDATE ON session_log_entries BEGIN
            INSERT INTO session_log_entries_fts(session_log_entries_fts, rowid, title, raw_md)
            VALUES ('delete', old.id, old.title, old.raw_md);
            INSERT INTO session_log_entries_fts(rowid, title, raw_md)
            VALUES (new.id, new.title, new.raw_md);
        END;
CREATE TRIGGER _table_descriptions_fts_ai AFTER INSERT ON _table_descriptions BEGIN
        INSERT INTO _table_descriptions_fts(rowid, table_name, description, when_to_query, key_columns, example_queries)
        VALUES (new.rowid, new.table_name, new.description, new.when_to_query, new.key_columns, new.example_queries);
    END;
CREATE TRIGGER _table_descriptions_fts_ad AFTER DELETE ON _table_descriptions BEGIN
        INSERT INTO _table_descriptions_fts(_table_descriptions_fts, rowid, table_name, description, when_to_query, key_columns, example_queries)
        VALUES ('delete', old.rowid, old.table_name, old.description, old.when_to_query, old.key_columns, old.example_queries);
    END;
CREATE TRIGGER _table_descriptions_fts_au AFTER UPDATE ON _table_descriptions BEGIN
        INSERT INTO _table_descriptions_fts(_table_descriptions_fts, rowid, table_name, description, when_to_query, key_columns, example_queries)
        VALUES ('delete', old.rowid, old.table_name, old.description, old.when_to_query, old.key_columns, old.example_queries);
        INSERT INTO _table_descriptions_fts(rowid, table_name, description, when_to_query, key_columns, example_queries)
        VALUES (new.rowid, new.table_name, new.description, new.when_to_query, new.key_columns, new.example_queries);
    END;

-- views
CREATE VIEW v_inbox_breaches AS
SELECT et.thread_id, et.lane, et.stakeholder_tier, et.subject,
       et.last_inbound_ts, et.last_outbound_ts, et.freshness_sla_hours,
       CAST((julianday('now') - julianday(et.last_inbound_ts)) * 24 AS INTEGER) AS age_hours,
       et.person_id, et.person_name_cached, et.context_slug, et.last_sender_email
  FROM email_threads et
 WHERE et.status = 'pending'
   AND et.freshness_sla_hours IS NOT NULL
   AND et.last_inbound_ts IS NOT NULL
   -- pending threads older than 30 days are treated as expired
   -- (auto_close_stale flips them to status='stale'), not live breaches.
   AND et.last_inbound_ts >= datetime('now','-30 days')
   AND (et.last_outbound_ts IS NULL OR et.last_inbound_ts > et.last_outbound_ts)
   AND (julianday('now') - julianday(et.last_inbound_ts)) * 24 > et.freshness_sla_hours
   -- automated senders and calendar RSVPs never need a reply; extend this list with your own vendors
   -- Luma usr-*@user.luma-mail.com relays are MASKED HUMANS and stay in.
   AND lower(COALESCE(et.last_sender_email,'')) NOT LIKE '%no-reply%'
   AND lower(COALESCE(et.last_sender_email,'')) NOT LIKE '%noreply%'
   AND lower(COALESCE(et.last_sender_email,'')) NOT LIKE '%donotreply%'
   AND lower(COALESCE(et.last_sender_email,'')) NOT LIKE '%do-not-reply%'
   AND lower(COALESCE(et.last_sender_email,'')) NOT IN (
        'notify@mail.notion.so', 'calendar-notification@google.com')
   AND lower(COALESCE(et.last_sender_email,'')) NOT LIKE '%@mail.notion.so'
   AND lower(COALESCE(et.last_sender_email,'')) NOT LIKE '%calendar-server.bounces.google.com'
   AND et.subject NOT LIKE 'Accepted:%'
   AND et.subject NOT LIKE 'Declined:%'
   AND et.subject NOT LIKE 'Tentative:%'
   AND et.subject NOT LIKE 'Invitation:%'
   AND et.subject NOT LIKE 'Updated invitation%'
   AND et.subject NOT LIKE 'Canceled event%'
 ORDER BY et.stakeholder_tier, age_hours DESC;
