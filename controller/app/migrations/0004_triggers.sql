-- 0003_triggers — persisted metric-threshold triggers for the AI operator.

-- Trigger configuration: the threshold values are stored durably (not held only
-- in memory). The loop seeds this table from environment variables on first
-- run and reads it on every poll, so a value written here survives a controller
-- restart and is picked up by the next poll without a re-deploy.
CREATE TABLE IF NOT EXISTS trigger_config (
    rule_id     TEXT PRIMARY KEY,          -- oom-guard-01 | disk-fail-01 | disk-degrade-01 | latency-01
    config_json TEXT NOT NULL,             -- JSON: {enter thresholds, clear thresholds, debounce counts}
    source      TEXT NOT NULL,             -- default | env | runtime
    updated_at  TEXT NOT NULL              -- RFC3339 UTC
);

-- Trigger state: the hysteresis latch, one row per (node, rule). Persisting
-- `state` (armed | fired) across restarts is what stops a controller restart
-- from re-firing an already-remediated fault, and lets a recovering metric
-- re-arm without flapping.
CREATE TABLE IF NOT EXISTS trigger_state (
    node_id         TEXT NOT NULL,
    rule_id         TEXT NOT NULL,
    state           TEXT NOT NULL,         -- armed | fired
    last_fired_at   TEXT,                  -- RFC3339 UTC (ms) of the last fire
    last_cleared_at TEXT,                  -- RFC3339 UTC (ms) of the last re-arm
    fired_count     INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,         -- RFC3339 UTC
    PRIMARY KEY (node_id, rule_id)
);

CREATE INDEX IF NOT EXISTS idx_trigger_state_node ON trigger_state (node_id);
