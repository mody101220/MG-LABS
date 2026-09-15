-- ============================================================================
-- GHAYATH PERSONAL AI — Core Database Schema
-- File    : 001_init.sql
-- Target  : PostgreSQL 14+
-- Contract: docs/api-contract-specification-v1.0.md
--
-- Conventions
--   * IDs are TEXT with domain prefixes (app generates prefix + ULID):
--       usr_ prj_ ts_ mem_ cmd_ exec_ appr_ auto_ notf_ inc_ conv_
--       msg_ (email)  wmsg_ (whatsapp)  draft_  repo_  iss_  evt_  ver_  tok_
--     audit_logs is the only exception: BIGINT identity (append-only sequence).
--   * All timestamps are TIMESTAMPTZ, stored in UTC (display conversion is the
--     client's job).
--   * Enumerations are CHECK constraints (migration-friendly, no CREATE TYPE).
--   * Flexible payloads (plans, triggers, actions, checks, event payloads) are
--     JSONB — their exact shape is contract-defined in openapi.yaml.
--   * updated_at is maintained by trigger (bottom of file).
--   * audit_logs is IMMUTABLE: no UPDATE/DELETE grants for the app role plus a
--     trigger that rejects any mutation.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- Identity & Auth
-- ----------------------------------------------------------------------------

CREATE TABLE users (
    id            TEXT PRIMARY KEY
                  CHECK (id ~ '^usr_[a-z0-9]+$'),
    email         TEXT NOT NULL,
    name          TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'VIEWER'
                  CHECK (role IN ('OWNER', 'AGENT', 'VIEWER')),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_unique ON users (lower(email));

-- Refresh tokens: opaque, stored hashed, rotated on every use.
CREATE TABLE auth_tokens (
    id           TEXT PRIMARY KEY
                 CHECK (id ~ '^tok_[a-z0-9]+$'),
    user_id      TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash   TEXT NOT NULL UNIQUE,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX auth_tokens_user_idx ON auth_tokens (user_id);

-- Conversations: context continuity for agent commands.
CREATE TABLE conversations (
    id         TEXT PRIMARY KEY
               CHECK (id ~ '^conv_[a-z0-9]+$'),
    user_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX conversations_user_idx ON conversations (user_id);

CREATE TABLE conversation_messages (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'agent', 'system')),
    content         TEXT NOT NULL,
    command_id      TEXT, -- set when the message is an agent command (cmd_...)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX conv_messages_conv_idx ON conversation_messages (conversation_id, id);

-- ----------------------------------------------------------------------------
-- Projects
-- ----------------------------------------------------------------------------

CREATE TABLE projects (
    id               TEXT PRIMARY KEY
                     CHECK (id ~ '^prj_[a-z0-9]+$'),
    name             TEXT NOT NULL,
    slug             TEXT NOT NULL UNIQUE,
    description      TEXT,
    status           TEXT NOT NULL DEFAULT 'ACTIVE'
                     CHECK (status IN ('ACTIVE', 'ON_HOLD', 'PAUSED', 'COMPLETED', 'ARCHIVED')),
    priority         TEXT NOT NULL DEFAULT 'P2'
                     CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    version          TEXT NOT NULL DEFAULT '0.1',
    repository_url   TEXT,
    deployment_url   TEXT,
    next_action      TEXT,
    last_activity_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX projects_status_idx ON projects (status);

-- Project <-> external resource links (repository, deployment, dashboard...).
CREATE TABLE project_integrations (
    project_id TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    provider   TEXT NOT NULL CHECK (provider IN ('whatsapp', 'email', 'github', 'other')),
    resource   TEXT NOT NULL, -- e.g. 'owner/repository' or a deployment URL
    metadata   JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, provider, resource)
);

-- ----------------------------------------------------------------------------
-- Tasks & Verifications
-- ----------------------------------------------------------------------------

CREATE TABLE tasks (
    id           TEXT PRIMARY KEY
                 CHECK (id ~ '^ts_[a-z0-9]+$'),
    project_id   TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    description  TEXT,
    priority     TEXT NOT NULL CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    status       TEXT NOT NULL DEFAULT 'TODO'
                 CHECK (status IN ('TODO', 'IN_PROGRESS', 'BLOCKED', 'IN_REVIEW', 'DONE', 'CANCELLED')),
    assignee     TEXT, -- user id or agent principal
    due_at       TIMESTAMPTZ,
    completed_at TIMESTAMPTZ, -- set when verification passes (or is not required)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX tasks_project_idx  ON tasks (project_id);
CREATE INDEX tasks_status_idx   ON tasks (status);
CREATE INDEX tasks_priority_idx ON tasks (priority);
CREATE INDEX tasks_assignee_idx ON tasks (assignee);
CREATE INDEX tasks_due_idx      ON tasks (due_at);

-- Independent verification records. A task becomes DONE only when its
-- verification PASSES — the agent claiming completion is never sufficient.
CREATE TABLE verifications (
    id           TEXT PRIMARY KEY
                 CHECK (id ~ '^ver_[a-z0-9]+$'),
    subject_type TEXT NOT NULL CHECK (subject_type IN ('TASK', 'EXECUTION', 'MESSAGE')),
    subject_id   TEXT NOT NULL,
    required     BOOLEAN NOT NULL DEFAULT TRUE,
    status       TEXT NOT NULL DEFAULT 'PENDING'
                 CHECK (status IN ('PENDING', 'PASSED', 'FAILED', 'SKIPPED')),
    checks       JSONB NOT NULL DEFAULT '[]', -- [{check, status, detail}]
    verified_by  TEXT, -- user id / 'agent' / 'system'
    verified_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX verifications_subject_idx ON verifications (subject_type, subject_id);

-- ----------------------------------------------------------------------------
-- Agent: commands, plans, executions
-- ----------------------------------------------------------------------------

CREATE TABLE agent_commands (
    id              TEXT PRIMARY KEY
                    CHECK (id ~ '^cmd_[a-z0-9]+$'),
    conversation_id TEXT REFERENCES conversations (id) ON DELETE SET NULL,
    user_id         TEXT REFERENCES users (id) ON DELETE SET NULL,
    source          TEXT NOT NULL DEFAULT 'dashboard'
                    CHECK (source IN ('dashboard', 'whatsapp', 'email', 'api', 'automation', 'voice', 'cli')),
    command_text    TEXT NOT NULL,
    intent          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PLANNED'
                    CHECK (status IN ('PLANNED', 'WAITING_APPROVAL', 'RUNNING', 'VERIFYING',
                                      'COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED')),
    autonomous      BOOLEAN NOT NULL DEFAULT FALSE,
    plan            JSONB NOT NULL DEFAULT '[]', -- [{task, status, detail}]
    approval_id     TEXT, -- set when the plan is gated on an approval
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX agent_commands_conv_idx   ON agent_commands (conversation_id);
CREATE INDEX agent_commands_status_idx ON agent_commands (status);
CREATE INDEX agent_commands_user_idx   ON agent_commands (user_id);

CREATE TABLE executions (
    id              TEXT PRIMARY KEY
                    CHECK (id ~ '^exec_[a-z0-9]+$'),
    command_id      TEXT NOT NULL REFERENCES agent_commands (id) ON DELETE CASCADE,
    approval_id     TEXT,
    status          TEXT NOT NULL DEFAULT 'RUNNING'
                    CHECK (status IN ('PLANNED', 'WAITING_APPROVAL', 'RUNNING', 'VERIFYING',
                                      'COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED')),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    error_code      TEXT, -- one of the fixed API error codes
    error_message   TEXT,
    verification_id TEXT REFERENCES verifications (id) ON DELETE SET NULL,
    result          JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX executions_command_idx ON executions (command_id);
CREATE INDEX executions_status_idx  ON executions (status);

-- ----------------------------------------------------------------------------
-- Approvals (human-in-the-loop)
-- ----------------------------------------------------------------------------

CREATE TABLE approvals (
    id              TEXT PRIMARY KEY
                    CHECK (id ~ '^appr_[a-z0-9]+$'),
    type            TEXT NOT NULL
                    CHECK (type IN ('AGENT_EXECUTION', 'EMAIL_SEND', 'WHATSAPP_SEND', 'GENERIC')),
    status          TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'CANCELLED')),
    payload         JSONB NOT NULL DEFAULT '{}', -- the exact action requested
    request_reason  TEXT,
    requested_by    TEXT NOT NULL, -- requesting principal (user id / 'agent' / 'system')
    decided_by      TEXT, -- deciding user id
    decision_reason TEXT,
    expires_at      TIMESTAMPTZ,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at      TIMESTAMPTZ
);
CREATE INDEX approvals_status_idx ON approvals (status);
CREATE INDEX approvals_type_idx   ON approvals (type);

-- ----------------------------------------------------------------------------
-- Automations & Scheduler state
-- ----------------------------------------------------------------------------

CREATE TABLE automations (
    id              TEXT PRIMARY KEY
                    CHECK (id ~ '^auto_[a-z0-9]+$'),
    name            TEXT NOT NULL,
    description     TEXT,
    trigger         JSONB NOT NULL, -- {type: SCHEDULE|EVENT|MANUAL, cron|event|interval_seconds}
    action          JSONB NOT NULL, -- {type: PROJECT_MONITOR|RUN_CHECK|..., target, params}
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at     TIMESTAMPTZ,
    last_run_status TEXT CHECK (last_run_status IN ('SUCCESS', 'FAILURE', 'SKIPPED')),
    next_run_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX automations_enabled_idx ON automations (enabled, next_run_at);

-- ----------------------------------------------------------------------------
-- Notifications
-- ----------------------------------------------------------------------------

CREATE TABLE notifications (
    id                TEXT PRIMARY KEY
                      CHECK (id ~ '^notf_[a-z0-9]+$'),
    severity          TEXT NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    channel_requested TEXT CHECK (channel_requested IN ('PUSH', 'WHATSAPP', 'EMAIL', 'DASHBOARD')),
    channels          JSONB NOT NULL DEFAULT '[]', -- channels actually routed to
    title             TEXT NOT NULL,
    message           TEXT NOT NULL,
    project_id        TEXT REFERENCES projects (id) ON DELETE SET NULL,
    status            TEXT NOT NULL DEFAULT 'QUEUED'
                      CHECK (status IN ('QUEUED', 'SENT', 'FAILED', 'DISMISSED')),
    data              JSONB NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at           TIMESTAMPTZ
);
CREATE INDEX notifications_project_idx ON notifications (project_id);
CREATE INDEX notifications_created_idx ON notifications (created_at DESC);

-- ----------------------------------------------------------------------------
-- Audit log — APPEND-ONLY / IMMUTABLE
-- ----------------------------------------------------------------------------

CREATE TABLE audit_logs (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_type   TEXT NOT NULL CHECK (actor_type IN ('user', 'agent', 'system')),
    actor        TEXT NOT NULL, -- user id / 'agent' / 'system'
    action       TEXT NOT NULL, -- e.g. PROJECT_MONITOR, EMAIL_SEND, TASK_COMPLETE
    target_type  TEXT,
    target_id    TEXT,
    project_id   TEXT,
    result       TEXT NOT NULL CHECK (result IN ('SUCCESS', 'FAILURE', 'DENIED', 'TIMEOUT')),
    execution_id TEXT,
    command_id   TEXT,
    request_id   TEXT,
    details      JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_logs_occurred_idx  ON audit_logs (occurred_at DESC);
CREATE INDEX audit_logs_project_idx   ON audit_logs (project_id);
CREATE INDEX audit_logs_action_idx    ON audit_logs (action);
CREATE INDEX audit_logs_actor_idx     ON audit_logs (actor);
CREATE INDEX audit_logs_execution_idx ON audit_logs (execution_id);

-- ----------------------------------------------------------------------------
-- Incidents
-- ----------------------------------------------------------------------------

CREATE TABLE incidents (
    id          TEXT PRIMARY KEY
                CHECK (id ~ '^inc_[a-z0-9]+$'),
    severity    TEXT NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    system      TEXT NOT NULL,
    issue       TEXT NOT NULL,
    impact      TEXT,
    status      TEXT NOT NULL DEFAULT 'OPEN'
                CHECK (status IN ('OPEN', 'INVESTIGATING', 'MITIGATED', 'RESOLVED', 'CLOSED')),
    source      TEXT NOT NULL DEFAULT 'agent'
                CHECK (source IN ('agent', 'user', 'monitoring', 'integration')),
    resolved_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX incidents_status_idx ON incidents (status);

-- ----------------------------------------------------------------------------
-- Integrations (whatsapp / email / github)
-- ----------------------------------------------------------------------------

CREATE TABLE integrations (
    id              TEXT PRIMARY KEY, -- provider slug: 'whatsapp' | 'email' | 'github'
    provider        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'DISCONNECTED'
                    CHECK (status IN ('CONNECTED', 'DISCONNECTED', 'DEGRADED', 'EXPIRED')),
    permissions     JSONB NOT NULL DEFAULT '{}', -- {READ: true, SEND: false, ...}
    config_enc      BYTEA, -- encrypted provider credentials/config
    webhook_secret  TEXT, -- shared secret for inbound webhook signature
    last_checked_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO integrations (id, provider, permissions) VALUES
    ('whatsapp', 'whatsapp', '{"READ": false, "REPLY": false, "SEND": false, "MEDIA": false}'),
    ('email',    'email',    '{"READ": false, "DRAFT": false, "SEND": false, "DELETE": false}'),
    ('github',   'github',   '{"READ": false, "ISSUES": false, "PULL_REQUEST": false, "COMMIT": false, "MERGE": false}');

-- ----------------------------------------------------------------------------
-- WhatsApp
-- ----------------------------------------------------------------------------

CREATE TABLE whatsapp_messages (
    id              TEXT PRIMARY KEY
                    CHECK (id ~ '^wmsg_[a-z0-9]+$'),
    external_id     TEXT UNIQUE, -- provider message id (wamid...), inbound dedupe
    direction       TEXT NOT NULL CHECK (direction IN ('INBOUND', 'OUTBOUND')),
    sender          TEXT, -- E.164 (inbound)
    recipient       TEXT, -- E.164 (outbound)
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'QUEUED'
                    CHECK (status IN ('QUEUED', 'SENT', 'DELIVERED', 'READ', 'FAILED')),
    classification  TEXT
                    CHECK (classification IN ('PERSONAL', 'BUSINESS', 'CLIENT', 'PROJECT',
                                              'URGENT', 'SPAM', 'UNKNOWN')),
    idempotency_key TEXT,
    approval_id     TEXT REFERENCES approvals (id) ON DELETE SET NULL,
    sent_at         TIMESTAMPTZ,
    verified_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX wmsg_direction_idx  ON whatsapp_messages (direction, created_at DESC);
CREATE INDEX wmsg_idem_idx       ON whatsapp_messages (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX wmsg_classification_idx ON whatsapp_messages (classification);

-- ----------------------------------------------------------------------------
-- Email
-- ----------------------------------------------------------------------------

CREATE TABLE email_messages (
    id              TEXT PRIMARY KEY
                     CHECK (id ~ '^msg_[a-z0-9]+$'),
    external_id     TEXT UNIQUE, -- provider message id (GMessageId)
    folder          TEXT NOT NULL DEFAULT 'inbox'
                     CHECK (folder IN ('inbox', 'sent', 'drafts', 'spam', 'trash', 'archive')),
    sender          TEXT,
    subject         TEXT,
    body            TEXT,
    priority        TEXT NOT NULL DEFAULT 'NORMAL'
                     CHECK (priority IN ('LOW', 'NORMAL', 'HIGH', 'URGENT')),
    classification  TEXT
                     CHECK (classification IN ('PERSONAL', 'BUSINESS', 'CLIENT', 'PROJECT',
                                               'URGENT', 'SPAM', 'UNKNOWN')),
    summary         TEXT, -- agent-generated
    required_action TEXT,
    deadline        TIMESTAMPTZ,
    is_read         BOOLEAN NOT NULL DEFAULT FALSE,
    received_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX email_folder_idx   ON email_messages (folder, received_at DESC);
CREATE INDEX email_priority_idx ON email_messages (priority);

CREATE TABLE email_drafts (
    id              TEXT PRIMARY KEY
                     CHECK (id ~ '^draft_[a-z0-9]+$'),
    reply_to        TEXT REFERENCES email_messages (id) ON DELETE SET NULL,
    to_addr         TEXT NOT NULL,
    subject         TEXT,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'DRAFT'
                     CHECK (status IN ('DRAFT', 'APPROVAL_PENDING', 'SENT', 'REJECTED', 'DISCARDED')),
    approval_id     TEXT REFERENCES approvals (id) ON DELETE SET NULL,
    sent_message_id TEXT REFERENCES email_messages (id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX email_drafts_status_idx ON email_drafts (status);

-- ----------------------------------------------------------------------------
-- GitHub
-- ----------------------------------------------------------------------------

CREATE TABLE github_repositories (
    id              TEXT PRIMARY KEY
                     CHECK (id ~ '^repo_[a-z0-9]+$'),
    full_name       TEXT NOT NULL UNIQUE, -- owner/name
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    default_branch  TEXT NOT NULL DEFAULT 'main',
    is_private      BOOLEAN NOT NULL DEFAULT FALSE,
    project_id      TEXT REFERENCES projects (id) ON DELETE SET NULL,
    last_checked_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX github_repos_project_idx ON github_repositories (project_id);

-- Append-only history; the latest row is the current status.
CREATE TABLE repo_status_snapshots (
    repo_id         TEXT NOT NULL REFERENCES github_repositories (id) ON DELETE CASCADE,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    build           TEXT NOT NULL CHECK (build IN ('PASSING', 'FAILING', 'UNKNOWN')),
    tests           TEXT NOT NULL CHECK (tests IN ('PASSING', 'FAILING', 'RUNNING', 'UNKNOWN')),
    deployment      TEXT NOT NULL CHECK (deployment IN ('HEALTHY', 'DEGRADED', 'DOWN', 'UNKNOWN')),
    open_issues     INTEGER NOT NULL DEFAULT 0,
    security_alerts INTEGER NOT NULL DEFAULT 0,
    details         JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (repo_id, checked_at)
);

CREATE TABLE github_issues (
    id            TEXT PRIMARY KEY
                  CHECK (id ~ '^iss_[a-z0-9]+$'),
    repository_id TEXT NOT NULL REFERENCES github_repositories (id) ON DELETE CASCADE,
    number        INTEGER NOT NULL, -- GitHub issue number
    title         TEXT NOT NULL,
    body          TEXT,
    priority      TEXT CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    status        TEXT NOT NULL DEFAULT 'OPEN'
                  CHECK (status IN ('OPEN', 'CLOSED')),
    url           TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repository_id, number)
);

-- ----------------------------------------------------------------------------
-- Memory
-- ----------------------------------------------------------------------------

CREATE TABLE memory (
    id         TEXT PRIMARY KEY
               CHECK (id ~ '^mem_[a-z0-9]+$'),
    type       TEXT NOT NULL
               CHECK (type IN ('USER_PROFILE', 'PROJECT_MEMORY', 'CONTACT', 'TASK', 'DECISION',
                               'PREFERENCE', 'DEADLINE', 'CONVERSATION', 'AUTOMATION')),
    key        TEXT NOT NULL, -- e.g. sanaa.current_version
    value      JSONB NOT NULL, -- arbitrary JSON (scalar, object, or array)
    project_id TEXT REFERENCES projects (id) ON DELETE CASCADE,
    source     TEXT NOT NULL DEFAULT 'agent'
               CHECK (source IN ('agent', 'user', 'integration', 'verified', 'inference')),
    confidence REAL CHECK (confidence BETWEEN 0 AND 1), -- agent-derived facts only
    expires_at TIMESTAMPTZ, -- optional TTL; scheduler purges expired rows
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Upsert semantics: one value per (type, key, project)
CREATE UNIQUE INDEX memory_key_unique ON memory (type, key, COALESCE (project_id, ''));
CREATE INDEX memory_type_idx    ON memory (type);
CREATE INDEX memory_project_idx ON memory (project_id);

-- ----------------------------------------------------------------------------
-- Events (internal event bus — persisted log)
-- ----------------------------------------------------------------------------

CREATE TABLE events (
    id          TEXT PRIMARY KEY
                CHECK (id ~ '^evt_[a-z0-9]+$'),
    event       TEXT NOT NULL
                CHECK (event IN ('message.received', 'email.received', 'task.created',
                                 'task.completed', 'project.updated', 'deployment.failed',
                                 'security.alert', 'approval.required', 'approval.granted',
                                 'approval.rejected', 'automation.triggered', 'incident.created')),
    source      TEXT NOT NULL, -- whatsapp / email / github / agent / scheduler / monitoring
    project_id  TEXT,
    severity    TEXT CHECK (severity IN ('INFO', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    payload     JSONB NOT NULL DEFAULT '{}',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX events_event_idx   ON events (event, occurred_at DESC);
CREATE INDEX events_project_idx ON events (project_id, occurred_at DESC);

-- ----------------------------------------------------------------------------
-- Idempotency (retry safety for side-effect operations)
-- ----------------------------------------------------------------------------

CREATE TABLE idempotency_keys (
    scope         TEXT NOT NULL, -- 'whatsapp.send' | 'email.send' | 'agent.execute' | ...
    key           TEXT NOT NULL, -- client Idempotency-Key header
    request_hash  TEXT NOT NULL, -- SHA-256 of the normalized request body
    response_code INTEGER NOT NULL,
    response_body JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '24 hours'),
    PRIMARY KEY (scope, key)
);

-- ----------------------------------------------------------------------------
-- Triggers
-- ----------------------------------------------------------------------------

-- updated_at maintenance
CREATE OR REPLACE FUNCTION set_updated_at ()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_users_updated         BEFORE UPDATE ON users               FOR EACH ROW EXECUTE FUNCTION set_updated_at ();
CREATE TRIGGER trg_conversations_updated BEFORE UPDATE ON conversations       FOR EACH ROW EXECUTE FUNCTION set_updated_at ();
CREATE TRIGGER trg_projects_updated      BEFORE UPDATE ON projects            FOR EACH ROW EXECUTE FUNCTION set_updated_at ();
CREATE TRIGGER trg_tasks_updated         BEFORE UPDATE ON tasks               FOR EACH ROW EXECUTE FUNCTION set_updated_at ();
CREATE TRIGGER trg_agent_commands_updated BEFORE UPDATE ON agent_commands     FOR EACH ROW EXECUTE FUNCTION set_updated_at ();
CREATE TRIGGER trg_automations_updated   BEFORE UPDATE ON automations         FOR EACH ROW EXECUTE FUNCTION set_updated_at ();
CREATE TRIGGER trg_incidents_updated     BEFORE UPDATE ON incidents           FOR EACH ROW EXECUTE FUNCTION set_updated_at ();
CREATE TRIGGER trg_integrations_updated  BEFORE UPDATE ON integrations        FOR EACH ROW EXECUTE FUNCTION set_updated_at ();
CREATE TRIGGER trg_email_drafts_updated  BEFORE UPDATE ON email_drafts        FOR EACH ROW EXECUTE FUNCTION set_updated_at ();
CREATE TRIGGER trg_github_repos_updated  BEFORE UPDATE ON github_repositories FOR EACH ROW EXECUTE FUNCTION set_updated_at ();
CREATE TRIGGER trg_memory_updated        BEFORE UPDATE ON memory              FOR EACH ROW EXECUTE FUNCTION set_updated_at ();

-- Audit immutability: the agent (or any app code) can INSERT, never UPDATE/DELETE.
CREATE OR REPLACE FUNCTION prevent_audit_mutation ()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is immutable (table %, operation %)', TG_TABLE_NAME, TG_OP;
END;
$$;

CREATE TRIGGER trg_audit_immutable
    BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation ();

-- ----------------------------------------------------------------------------
-- Roles & privileges (executed by the deployment, kept here as reference)
--
--   ghayath_app      — API/agent role: full DML except audit mutation
--   ghayath_readonly — dashboard/viewer role: SELECT only
--
-- GRANT CONNECT ON DATABASE ghayath TO ghayath_app, ghayath_readonly;
-- GRANT USAGE ON SCHEMA public TO ghayath_app, ghayath_readonly;
--
-- GRANT SELECT, INSERT, UPDATE, DELETE ON
--     users, auth_tokens, conversations, conversation_messages,
--     projects, project_integrations, tasks, verifications,
--     agent_commands, executions, approvals, automations, notifications,
--     incidents, integrations, whatsapp_messages, email_messages,
--     email_drafts, github_repositories, repo_status_snapshots,
--     github_issues, memory, events, idempotency_keys
--     TO ghayath_app;
--
-- GRANT SELECT, INSERT ON audit_logs TO ghayath_app;  -- NOTE: no UPDATE/DELETE
-- REVOKE UPDATE, DELETE ON audit_logs FROM ghayath_app;
--
-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO ghayath_readonly;
-- ----------------------------------------------------------------------------

COMMIT;
