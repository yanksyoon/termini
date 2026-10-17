-- 0014_demo_faults — operator-only demo fault injection (controller-api.md §4.7).
--
-- One row per injection an operator started from the fault console. The row is
-- the durable record of what was injected, on which node, for how long, and how
-- it ended — so an injection can never outlive its TTL silently (the injector
-- thread reads `state`/`expires_at` every tick and closes the episode), and a
-- controller restart can tell an injection that was running in a previous
-- process from a live one (the row is swept to `expired` at boot).
--
-- `baseline_json` is the node's healthy series the injection is layered on: the
-- fault samples are derived from it and the recovery burst re-emits it verbatim,
-- which is what makes "clear" return the rule below its clear threshold.
CREATE TABLE IF NOT EXISTS demo_faults (
    injection_id     TEXT PRIMARY KEY,               -- ULID
    project_id       TEXT NOT NULL REFERENCES projects(id),
    node_id          TEXT NOT NULL REFERENCES nodes(id),
    scenario         TEXT NOT NULL CHECK (scenario IN ('oom', 'disk-fail')),
    rule_id          TEXT NOT NULL,                  -- frozen rule the scenario drives
    target           TEXT NOT NULL CHECK (target IN ('real', 'synthetic')),  -- node provenance
    state            TEXT NOT NULL CHECK (state IN ('active', 'recovering', 'cleared', 'expired')),
    duration_s       REAL NOT NULL,                  -- TTL the operator asked for
    started_at       TEXT NOT NULL,                  -- RFC3339 UTC
    expires_at       TEXT NOT NULL,                  -- started_at + duration_s
    cleared_at       TEXT,
    cleared_by       TEXT,                           -- human actor / 'ttl'
    clear_reason     TEXT,
    next_seq         INTEGER NOT NULL DEFAULT 0,     -- monotonic telemetry seq for this node
    samples_written  INTEGER NOT NULL DEFAULT 0,
    recovery_samples INTEGER NOT NULL DEFAULT 0,
    baseline_json    TEXT NOT NULL,                  -- healthy series to restore on clear
    params_json      TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_demo_faults_project ON demo_faults (project_id, state);
CREATE INDEX IF NOT EXISTS idx_demo_faults_node ON demo_faults (node_id, state);
