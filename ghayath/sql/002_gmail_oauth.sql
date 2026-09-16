-- ============================================================================
-- GHAYATH PERSONAL AI — Gmail provider storage
-- Migration: 002_gmail_oauth.sql
-- Reason: 001_init.sql has no server-side OAuth state or Gmail message mapping.
-- Tokens remain encrypted in integrations.config_enc; this migration stores only
-- hashed one-time state and provider message metadata (never OAuth tokens).
-- ============================================================================

BEGIN;

INSERT INTO integrations (id, provider, permissions)
VALUES ('gmail', 'gmail', '{"READ": true, "SEND": false, "MODIFY": false}')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE gmail_oauth_states (
    state_hash TEXT PRIMARY KEY
               CHECK (state_hash ~ '^[a-f0-9]{64}$'),
    user_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX gmail_oauth_states_expiry_idx ON gmail_oauth_states (expires_at);

CREATE TABLE gmail_messages (
    gmail_message_id TEXT PRIMARY KEY,
    thread_id        TEXT NOT NULL,
    email_message_id TEXT NOT NULL UNIQUE REFERENCES email_messages (id) ON DELETE CASCADE,
    labels           JSONB NOT NULL DEFAULT '[]',
    headers          JSONB NOT NULL DEFAULT '{}',
    attachments      JSONB NOT NULL DEFAULT '[]',
    snippet          TEXT,
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX gmail_messages_thread_idx ON gmail_messages (thread_id);
CREATE INDEX gmail_messages_synced_idx ON gmail_messages (synced_at DESC);

COMMIT;
