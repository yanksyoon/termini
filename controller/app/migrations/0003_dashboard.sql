-- 0003_dashboard — W5 dashboard surfaces (t_3468e5f0).
-- Adds: per-node SSH/inventory mapping, the remediation command store, and the
-- persistent operator-log tail (SSE/WS backfill source).

-- Per-node public IP from the infra inventory (feeds GET /nodes/{id}/ssh and
-- the terminal bridge). NULL until the controller is seeded with inventory.json.
-- public_ip is intentionally NOT accepted from node telemetry (governance.md
-- §4.2 denies ip_public); it comes only from the controller's own inventory.
ALTER TABLE nodes ADD COLUMN public_ip TEXT;

-- Remediation / operation command store (controller-api.md §6 + dashboard.md
-- §2.1/§4.4). One row per issued command; the state machine is frozen in
-- dashboard.md §4.4:
--   pending_approval -> approved|denied -> queued -> acked -> running ->
--   succeeded|failed   (timed_out is a distinct terminal state)
CREATE TABLE IF NOT EXISTS commands (
    command_id       TEXT PRIMARY KEY,                 -- ULID
    project_id       TEXT NOT NULL REFERENCES projects(id),
    node_id          TEXT,                             -- null for project-level
    type             TEXT NOT NULL,                    -- commandAllowlist() type
    payload_json     TEXT NOT NULL DEFAULT '{}',
    approval         TEXT NOT NULL CHECK (approval IN ('auto', 'required')),
    state            TEXT NOT NULL CHECK (state IN (
                         'pending_approval', 'approved', 'denied', 'queued',
                         'acked', 'running', 'succeeded', 'failed', 'timed_out')),
    issued_by        TEXT NOT NULL CHECK (issued_by IN ('ai_operator', 'human', 'system')),
    issued_at        TEXT NOT NULL,
    approved_at      TEXT,
    approved_by      TEXT,
    denied_at        TEXT,
    denied_by        TEXT,
    deny_reason      TEXT,
    acked_at         TEXT,
    completed_at     TEXT,
    result_json      TEXT,
    trigger_json     TEXT,                             -- node-telemetry.md §5 linkage
    timeout_s        INTEGER NOT NULL DEFAULT 60
);

CREATE INDEX IF NOT EXISTS idx_commands_project ON commands (project_id);
CREATE INDEX IF NOT EXISTS idx_commands_node ON commands (node_id);
CREATE INDEX IF NOT EXISTS idx_commands_state ON commands (state);

-- Persistent AI-operator / human-intervention log lines. The live ring buffer
-- is in memory (10k); this table is the durable tail used for Last-Event-ID
-- backfill. ULIDs are time-ordered, so `log_id > ?` is a monotonic cursor.
CREATE TABLE IF NOT EXISTS operator_logs (
    log_id           TEXT PRIMARY KEY,                 -- ULID
    project_id       TEXT NOT NULL,
    ts               TEXT NOT NULL,
    node_id          TEXT,                             -- null for project-level
    severity         TEXT NOT NULL CHECK (severity IN ('debug', 'info', 'warn', 'error')),
    actor            TEXT NOT NULL CHECK (actor IN ('ai_operator', 'human')),
    message          TEXT NOT NULL,
    remediation_json TEXT,                             -- null when none attached
    truncated        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_operator_logs_project ON operator_logs (project_id, log_id);
