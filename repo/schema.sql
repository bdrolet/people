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

CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    sync_token  TEXT,
    last_run_at TIMESTAMPTZ,
    last_status TEXT
);

-- LinkedIn snapshot (docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §4).
-- connections/messages/recommendations are fully replaced by scripts/import_linkedin.py;
-- linkedin_imports is an append-only audit. No foreign keys to people: the link is advisory.

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
CREATE INDEX IF NOT EXISTS linkedin_connections_person_email_idx ON linkedin_connections (person_email);
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
