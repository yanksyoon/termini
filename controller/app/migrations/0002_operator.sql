-- 0002_operator — AI-operator remediation loop (health events, decision log,
-- remediation audit trail).

-- A health event emitted by the detector: offending node + frozen rule + the
-- evidence window that justified it.
CREATE TABLE IF NOT EXISTS health_events (
    id                 TEXT PRIMARY KEY,          -- ULID
    node_id            TEXT NOT NULL REFERENCES nodes(id),
    rule_id            TEXT NOT NULL,             -- oom-guard-01 | disk-degrade-01 | latency-guard-01
    type               TEXT NOT NULL,             -- oom | disk_degrade | latency
    metric             TEXT NOT NULL,
    value              REAL,
    threshold          TEXT NOT NULL,             -- human-readable threshold
    evidence_seq_start INTEGER,
    evidence_seq_end   INTEGER,
    evidence_json      TEXT NOT NULL,             -- bounded evidence window (JSON)
    fired_at           TEXT NOT NULL              -- RFC3339 UTC
);

CREATE INDEX IF NOT EXISTS idx_health_events_node ON health_events (node_id, fired_at);

-- The decision log: one row per chosen remediation, traceable to its event.
-- event_id is UNIQUE so one health event can never produce two decisions
-- (the schema-level idempotency guard against double-fire).
CREATE TABLE IF NOT EXISTS remediation_decisions (
    id             TEXT PRIMARY KEY,              -- ULID
    event_id       TEXT NOT NULL UNIQUE REFERENCES health_events(id),
    node_id        TEXT NOT NULL,
    rule_id        TEXT NOT NULL,
    playbook       TEXT NOT NULL,
    action_kind    TEXT NOT NULL,                 -- instance.restart | image.prune | ...
    action_params  TEXT NOT NULL,                 -- JSON
    reason         TEXT NOT NULL,
    decided_at     TEXT NOT NULL                  -- RFC3339 UTC
);

-- The audit trail: node, event, action, result. Each row records exactly what
-- the executor did (or would do in dry-run). `idempotency_key` is used to
-- deduplicate a repeated (node, rule, action, target) remediation before any
-- LXD call is issued: a prior `ok`/`dry_run` audit with the same key yields
-- `already_applied` instead of a duplicate command. It is intentionally NOT
-- UNIQUE so a failed remediation (result=error) can be retried and re-audited.
CREATE TABLE IF NOT EXISTS remediation_audits (
    id              TEXT PRIMARY KEY,             -- ULID
    decision_id     TEXT NOT NULL REFERENCES remediation_decisions(id),
    event_id        TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    rule_id         TEXT NOT NULL,
    action_kind     TEXT NOT NULL,
    result          TEXT NOT NULL,                -- ok | error | dry_run | already_applied
    detail          TEXT,
    lxd_method      TEXT,                         -- HTTP method issued to LXD
    lxd_path        TEXT,                         -- LXD REST path
    lxd_body        TEXT,                         -- request body (JSON)
    idempotency_key TEXT NOT NULL,
    dry_run         INTEGER NOT NULL DEFAULT 0,
    executed_at     TEXT NOT NULL                 -- RFC3339 UTC
);

CREATE INDEX IF NOT EXISTS idx_remediation_audits_node
    ON remediation_audits (node_id, rule_id, executed_at);

CREATE INDEX IF NOT EXISTS idx_remediation_audits_idem
    ON remediation_audits (idempotency_key, executed_at);
