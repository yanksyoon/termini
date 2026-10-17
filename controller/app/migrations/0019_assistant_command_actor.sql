-- 0019_assistant_command_actor — extend the command/log enums to include
-- 'assistant' (t_bfd2c2f7). The assistant may execute guardrail-admitted
-- command-channel verbs (node.drain / node.reboot) with `issued_by: assistant`
-- and a log `actor: assistant`, so the record distinguishes assistant-initiated
-- actions from operator- and human-initiated ones. SQLite cannot alter a CHECK
-- constraint, so the two tables are rebuilt in place (same column order, data
-- copied verbatim; neither table is referenced by a foreign key).

PRAGMA foreign_keys=OFF;

ALTER TABLE commands RENAME TO commands_old;
CREATE TABLE commands (
    command_id       TEXT PRIMARY KEY,                 -- ULID
    project_id       TEXT NOT NULL REFERENCES projects(id),
    node_id          TEXT,                             -- null for project-level
    type             TEXT NOT NULL,                    -- commandAllowlist() type
    payload_json     TEXT NOT NULL DEFAULT '{}',
    approval         TEXT NOT NULL CHECK (approval IN ('auto', 'required')),
    state            TEXT NOT NULL CHECK (state IN (
                         'pending_approval', 'approved', 'denied', 'queued',
                         'acked', 'running', 'succeeded', 'failed', 'timed_out')),
    issued_by        TEXT NOT NULL CHECK (issued_by IN ('ai_operator', 'human', 'system', 'assistant')),
    issued_at        TEXT NOT NULL,
    approved_at      TEXT,
    approved_by      TEXT,
    denied_at        TEXT,
    denied_by        TEXT,
    deny_reason      TEXT,
    acked_at         TEXT,
    completed_at     TEXT,
    result_json      TEXT,
    trigger_json     TEXT,
    timeout_s        INTEGER NOT NULL DEFAULT 60
);
INSERT INTO commands SELECT * FROM commands_old;
DROP TABLE commands_old;

ALTER TABLE operator_logs RENAME TO operator_logs_old;
CREATE TABLE operator_logs (
    log_id           TEXT PRIMARY KEY,                 -- ULID
    project_id       TEXT NOT NULL,
    ts               TEXT NOT NULL,
    node_id          TEXT,                             -- null for project-level
    severity         TEXT NOT NULL CHECK (severity IN ('debug', 'info', 'warn', 'error')),
    actor            TEXT NOT NULL CHECK (actor IN ('ai_operator', 'human', 'assistant')),
    message          TEXT NOT NULL,
    remediation_json TEXT,
    truncated        INTEGER NOT NULL DEFAULT 0
);
INSERT INTO operator_logs SELECT * FROM operator_logs_old;
DROP TABLE operator_logs_old;

CREATE INDEX IF NOT EXISTS idx_commands_project ON commands (project_id);
CREATE INDEX IF NOT EXISTS idx_commands_node ON commands (node_id);
CREATE INDEX IF NOT EXISTS idx_commands_state ON commands (state);
CREATE INDEX IF NOT EXISTS idx_operator_logs_project ON operator_logs (project_id, log_id);

PRAGMA foreign_keys=ON;
