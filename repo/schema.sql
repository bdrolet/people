CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS people (
    email                 TEXT PRIMARY KEY,
    display_name          TEXT,
    first_seen            TIMESTAMPTZ NOT NULL,
    last_seen             TIMESTAMPTZ,
    last_contacted        TIMESTAMPTZ,
    message_count         INT  NOT NULL DEFAULT 0,
    my_response_count     INT  NOT NULL DEFAULT 0,
    relationship_label    TEXT,
    notes                 TEXT,
    eligible              BOOLEAN NOT NULL DEFAULT FALSE,
    automated             BOOLEAN NOT NULL DEFAULT FALSE,
    google_resource_name  TEXT UNIQUE,
    google_etag           TEXT,
    google_deleted_at     TIMESTAMPTZ,
    hubspot_contact_id    TEXT UNIQUE,
    hubspot_synced_at     TIMESTAMPTZ,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS people_last_interaction_idx
    ON people (GREATEST(COALESCE(last_seen, 'epoch'::timestamptz), COALESCE(last_contacted, 'epoch'::timestamptz)) DESC);
CREATE INDEX IF NOT EXISTS people_display_name_trgm_idx ON people USING gin (display_name gin_trgm_ops);

-- Editable Google contact fields
-- (docs/superpowers/specs/2026-09-24-contact-field-edits-design.md §4).
-- All four are Google-owned and refreshed from it; the three typed columns are a
-- derived index over google_fields, not a separate source.
ALTER TABLE people
  ADD COLUMN IF NOT EXISTS phone_numbers TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS company       TEXT,
  ADD COLUMN IF NOT EXISTS job_title     TEXT,
  ADD COLUMN IF NOT EXISTS google_fields JSONB NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS people_phone_numbers_idx ON people USING gin (phone_numbers);
CREATE INDEX IF NOT EXISTS people_google_fields_idx ON people USING gin (google_fields);
CREATE INDEX IF NOT EXISTS people_company_trgm_idx  ON people USING gin (company gin_trgm_ops);

CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    sync_token  TEXT,
    last_run_at TIMESTAMPTZ,
    last_status TEXT
);

-- LinkedIn snapshot (docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §4).
-- connections/messages/recommendations are fully replaced by scripts/import_linkedin.py;
-- linkedin_imports is an append-only audit. person_email is a soft link to people,
-- backed by an FK (ON DELETE SET NULL, added below) that proves the address exists
-- without guaranteeing it is the right person; matching accuracy still comes from
-- re-matching every import. A full people rebuild must use DELETE FROM people, not
-- TRUNCATE, which fails on a referenced table (or, with CASCADE, would wipe this
-- snapshot too).

CREATE TABLE IF NOT EXISTS linkedin_connections (
    profile_url         TEXT PRIMARY KEY,
    first_name          TEXT,
    last_name           TEXT,
    full_name           TEXT NOT NULL,
    email               TEXT,
    company             TEXT,
    position            TEXT,
    connected_on        DATE,
    person_email        TEXT,
    match_method        TEXT,
    message_count       INT  NOT NULL DEFAULT 0,
    my_message_count    INT  NOT NULL DEFAULT 0,
    last_message_at     TIMESTAMPTZ,
    last_my_message_at  TIMESTAMPTZ,
    snapshot_at         TIMESTAMPTZ NOT NULL
);
-- Guarded on person_email still existing: the person-identity migration below
-- drops that column (and, with it, this index), so this must stay a no-op
-- after that (spec 2026-09-24-person-identity-design.md §4.1).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'linkedin_connections' AND column_name = 'person_email') THEN
        CREATE INDEX IF NOT EXISTS linkedin_connections_person_email_idx ON linkedin_connections (person_email);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS linkedin_connections_name_trgm_idx ON linkedin_connections USING gin (full_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS linkedin_connections_company_trgm_idx ON linkedin_connections USING gin (company gin_trgm_ops);
CREATE INDEX IF NOT EXISTS linkedin_connections_position_trgm_idx ON linkedin_connections USING gin (position gin_trgm_ops);

CREATE TABLE IF NOT EXISTS linkedin_messages (
    id                      BIGSERIAL PRIMARY KEY,
    conversation_id         TEXT NOT NULL,
    conversation_title      TEXT,
    sender_name             TEXT,
    sender_profile_url      TEXT,
    recipient_names         TEXT,
    recipient_profile_urls  TEXT[] NOT NULL DEFAULT '{}',
    sent_at                 TIMESTAMPTZ NOT NULL,
    subject                 TEXT,
    content                 TEXT,
    folder                  TEXT,
    from_me                 BOOLEAN NOT NULL,
    snapshot_at             TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS linkedin_messages_conversation_idx ON linkedin_messages (conversation_id, sent_at);
CREATE INDEX IF NOT EXISTS linkedin_messages_participants_idx ON linkedin_messages USING gin ((recipient_profile_urls || ARRAY[sender_profile_url]));

CREATE TABLE IF NOT EXISTS linkedin_recommendations (
    id              BIGSERIAL PRIMARY KEY,
    direction       TEXT NOT NULL CHECK (direction IN ('given', 'received')),
    first_name      TEXT,
    last_name       TEXT,
    full_name       TEXT NOT NULL,
    company         TEXT,
    job_title       TEXT,
    text            TEXT,
    status          TEXT,
    created_on      DATE,
    profile_url     TEXT,
    snapshot_at     TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS linkedin_imports (
    id                       BIGSERIAL PRIMARY KEY,
    snapshot_at              TIMESTAMPTZ NOT NULL,
    source                   TEXT NOT NULL,
    connections              INT NOT NULL,
    messages                 INT NOT NULL,
    recommendations_given    INT NOT NULL,
    recommendations_received INT NOT NULL,
    matched_by_email         INT NOT NULL,
    matched_by_name          INT NOT NULL
);

-- iMessage snapshot (docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md §4).
-- Upserted incrementally by scripts/import_imessage.py; message text is stored here
-- but is never served by people-api.

CREATE TABLE IF NOT EXISTS imessage_handles (
    handle                 TEXT PRIMARY KEY,   -- E.164 phone or lowercased email, see §5.3
    display_name           TEXT,               -- from the matched Google Contact
    google_resource_name   TEXT,
    person_email           TEXT REFERENCES people(email) ON DELETE SET NULL,
    match_method           TEXT,               -- 'email' | 'google' | NULL
    message_count          INT NOT NULL DEFAULT 0,   -- 1:1 chats only
    my_message_count       INT NOT NULL DEFAULT 0,   -- 1:1 chats only
    last_message_at        TIMESTAMPTZ,
    last_my_message_at     TIMESTAMPTZ,
    group_message_count    INT NOT NULL DEFAULT 0,   -- all messages in group chats this handle is in
    last_group_message_at  TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Guarded on person_email still existing: the person-identity migration below
-- drops that column (and, with it, this index), so this must stay a no-op
-- after that (spec 2026-09-24-person-identity-design.md §4.1).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'imessage_handles' AND column_name = 'person_email') THEN
        CREATE INDEX IF NOT EXISTS imessage_handles_person_email_idx ON imessage_handles (person_email);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS imessage_handles_google_idx ON imessage_handles (google_resource_name);
CREATE INDEX IF NOT EXISTS imessage_handles_name_trgm_idx ON imessage_handles USING gin (display_name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS imessage_chats (
    chat_guid            TEXT PRIMARY KEY,
    display_name         TEXT,               -- group name, if set
    is_group             BOOLEAN NOT NULL,
    participant_handles  TEXT[] NOT NULL DEFAULT '{}',   -- normalized, excluding Ben
    last_message_at      TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS imessage_chats_participants_idx ON imessage_chats USING gin (participant_handles);

CREATE TABLE IF NOT EXISTS imessage_messages (
    guid             TEXT PRIMARY KEY,     -- chat.db message.guid; upsert key
    chat_guid        TEXT NOT NULL,
    sender_handle    TEXT,                 -- normalized; NULL when from_me
    from_me          BOOLEAN NOT NULL,
    sent_at          TIMESTAMPTZ NOT NULL,
    text             TEXT,                 -- NULL if undecodable or retracted; never served by people-api
    service          TEXT,                 -- 'iMessage' | 'SMS' | 'RCS'
    has_attachments  BOOLEAN NOT NULL DEFAULT FALSE,
    edited_at        TIMESTAMPTZ,
    retracted        BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS imessage_messages_chat_idx ON imessage_messages (chat_guid, sent_at);
CREATE INDEX IF NOT EXISTS imessage_messages_sender_idx ON imessage_messages (sender_handle);

CREATE TABLE IF NOT EXISTS imessage_imports (
    id                  BIGSERIAL PRIMARY KEY,
    ran_at              TIMESTAMPTZ NOT NULL,
    mode                TEXT NOT NULL CHECK (mode IN ('incremental', 'full')),
    max_rowid           BIGINT NOT NULL,     -- chat.db message.ROWID watermark for the next run
    messages_upserted   INT NOT NULL,
    messages_deleted    INT NOT NULL,        -- --full only
    undecoded           INT NOT NULL,
    handles             INT NOT NULL,
    matched_by_email    INT NOT NULL,
    matched_by_google   INT NOT NULL,
    linked_to_people    INT NOT NULL,
    chats               INT NOT NULL
);

-- Backfill the same constraint onto the LinkedIn snapshot (spec §4, Migration).
-- Guarded on person_email still existing: the person-identity migration below
-- drops that column, and this block must stay a no-op after that (spec
-- 2026-09-24-person-identity-design.md §4.1).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'linkedin_connections' AND column_name = 'person_email') THEN
        ALTER TABLE linkedin_connections
            ADD CONSTRAINT linkedin_connections_person_email_fkey
            FOREIGN KEY (person_email) REFERENCES people(email) ON DELETE SET NULL;
    END IF;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Person identity: surrogate id, nullable unique email (spec §4.1).
-- Order matters and is not obvious: the child foreign keys reference
-- people(email), backed by the primary-key index, so DROP CONSTRAINT
-- people_pkey fails with dependent_objects_still_exist while they exist.
-- The children must move off person_email before the primary key swaps,
-- and their new person_id foreign keys are added after id becomes the
-- primary key (a foreign key needs a unique or primary-key target).

-- 1. Surrogate key column, populated for every existing row.
ALTER TABLE people ADD COLUMN IF NOT EXISTS id BIGSERIAL;

-- 2. Children gain person_id and are backfilled through the old text link.
--    No FK yet: people.id is not unique until step 3.
ALTER TABLE imessage_handles     ADD COLUMN IF NOT EXISTS person_id BIGINT;
ALTER TABLE linkedin_connections ADD COLUMN IF NOT EXISTS person_id BIGINT;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'imessage_handles' AND column_name = 'person_email') THEN
        UPDATE imessage_handles h SET person_id = p.id
          FROM people p WHERE h.person_email = p.email AND h.person_id IS NULL;
        ALTER TABLE imessage_handles DROP COLUMN person_email;   -- drops its FK too
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'linkedin_connections' AND column_name = 'person_email') THEN
        UPDATE linkedin_connections c SET person_id = p.id
          FROM people p WHERE c.person_email = p.email AND c.person_id IS NULL;
        ALTER TABLE linkedin_connections DROP COLUMN person_email;
    END IF;
END $$;

-- 3. Now nothing depends on the email PK index: swap the key.
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'people'::regclass AND i.indisprimary AND a.attname = 'email'
    ) THEN
        ALTER TABLE people DROP CONSTRAINT people_pkey;
        ALTER TABLE people ADD PRIMARY KEY (id);
        ALTER TABLE people ALTER COLUMN email DROP NOT NULL;
        ALTER TABLE people ADD CONSTRAINT people_email_key UNIQUE (email);
    END IF;
END $$;

-- 4. With people.id a primary key, the children can reference it.
DO $$ BEGIN
    ALTER TABLE imessage_handles ADD CONSTRAINT imessage_handles_person_id_fkey
        FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE linkedin_connections ADD CONSTRAINT linkedin_connections_person_id_fkey
        FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 5. Every person must be reachable by something.
DO $$ BEGIN
    ALTER TABLE people ADD CONSTRAINT people_has_an_identifier
        CHECK (email IS NOT NULL OR cardinality(phone_numbers) > 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS imessage_handles_person_id_idx     ON imessage_handles (person_id);
CREATE INDEX IF NOT EXISTS linkedin_connections_person_id_idx ON linkedin_connections (person_id);
