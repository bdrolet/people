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
