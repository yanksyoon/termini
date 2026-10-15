-- 0015_agent_tasks — the agent task API (t_1a2c913c, agent-runtime.md §7.4).
--
-- One row per task a client gave an agent through the controller. This IS the
-- enumerable, human-audited record the card requires for evidence: which agent
-- (vm_id / node_id), who submitted it (actor), which audited dispatch command
-- carried it (command_id -> `commands`, the same envelope node actions use), and
-- how it ended (state / error_code / model / duration).
--
-- Content policy (governance §4, agent-runtime.md §7.1/§7.3 — decision AT-1):
--   * `prompt` and `answer` hold customer content (C2). They are stored ONLY
--     because the project token holder is the documented reader of its own task:
--     the client's dashboard reads the answer back through this API, which is the
--     only channel the client has (the instance's own result sink stays on the
--     premises too). The controller never logs either field, never folds them
--     into an operator-log line, and never sends them to an external model.
--   * every value that DOES cross into the audit stream is content-free: state,
--     model alias, error code, timings, byte counts.
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id          TEXT PRIMARY KEY,             -- controller ULID (the enumerable id)
    project_id       TEXT NOT NULL REFERENCES projects(id),
    vm_id            TEXT NOT NULL,                -- the agent's LXD instance name
    node_id          TEXT,                         -- owning node (nullable: unknown at insert)
    command_id       TEXT,                         -- audited `agent.dispatch_task` command
    agent_task_id    TEXT,                         -- the runtime's own task id
    state            TEXT NOT NULL CHECK (state IN ('queued', 'running', 'done', 'error')),
    actor            TEXT NOT NULL,                -- who submitted it (human)
    prompt           TEXT NOT NULL,                -- C2 (see header)
    model_preference TEXT,                         -- requested alias, null = endpoint default
    max_tokens       INTEGER NOT NULL DEFAULT 256,
    answer           TEXT,                         -- C2 (see header)
    model            TEXT,                         -- alias that actually answered
    provenance_json  TEXT,                         -- backend/answered_by/egress (honest label)
    usage_json       TEXT,
    error_code       TEXT,                         -- distinct, actionable failure code
    error_message    TEXT,
    error_json       TEXT,
    submitted_at     TEXT NOT NULL,                -- RFC3339 UTC
    started_at       TEXT,
    ended_at         TEXT,
    duration_ms      INTEGER,
    deadline_s       REAL NOT NULL DEFAULT 300,    -- controller-side task deadline
    polls            INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT                          -- transport error from the last poll
);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_project ON agent_tasks (project_id, task_id);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_vm ON agent_tasks (project_id, vm_id);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_state ON agent_tasks (project_id, state);
