-- 0001_init — initial controller schema.

CREATE TABLE IF NOT EXISTS projects (
    id                    TEXT PRIMARY KEY,          -- ULID
    name                  TEXT NOT NULL,
    project_token_hash    TEXT NOT NULL,             -- sha256 of project token
    max_inference_nodes   INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id                 TEXT PRIMARY KEY,             -- ULID
    project_id         TEXT NOT NULL REFERENCES projects(id),
    registration_id    TEXT NOT NULL,                -- stable bootstrap label
    machine_id         TEXT,                         -- stable dedup/upsert key
    hostname_hash      TEXT,                         -- C1-transformed hostname
    role               TEXT NOT NULL CHECK (role IN ('compute', 'inference')),
    node_token_hash    TEXT NOT NULL,                -- sha256 of node token
    hardware_json      TEXT NOT NULL,                -- validated registration hardware
    os_json            TEXT,
    lxd_json           TEXT,
    role_hint          TEXT,
    installer_version  TEXT,
    status             TEXT NOT NULL DEFAULT 'online'
                       CHECK (status IN ('online', 'degraded', 'offline')),
    last_seen_at       TEXT NOT NULL,                -- RFC3339 UTC
    registered_at      TEXT NOT NULL,
    source_ip          TEXT,
    UNIQUE (project_id, registration_id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes (project_id);

-- Single-use bootstrap tokens; burned on successful registration.
CREATE TABLE IF NOT EXISTS bootstrap_tokens (
    token_hash       TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    used_at          TEXT,
    used_by_node_id  TEXT
);

-- Telemetry batch envelope (one row per accepted batch).
CREATE TABLE IF NOT EXISTS telemetry_batches (
    call_id                TEXT PRIMARY KEY,         -- ULID
    node_id                TEXT NOT NULL REFERENCES nodes(id),
    ts_received            TEXT NOT NULL,
    schema_version         INTEGER NOT NULL,
    governance_chain_tip   TEXT,
    redactor_version       TEXT,
    redactor_config_hash   TEXT,
    accepted_count         INTEGER NOT NULL,
    rejected_count         INTEGER NOT NULL,
    payload_json           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_node ON telemetry_batches (node_id, ts_received);

-- Telemetry samples (time-series). ts is preserved verbatim; ts_epoch for ordering.
CREATE TABLE IF NOT EXISTS telemetry_samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id      TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    ts           TEXT NOT NULL,                      -- RFC3339 exactly as sent
    ts_epoch     REAL NOT NULL,                      -- seconds since epoch (UTC)
    payload_json TEXT NOT NULL,                      -- validated sample object
    UNIQUE (node_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_samples_node_time
    ON telemetry_samples (node_id, ts_epoch);
