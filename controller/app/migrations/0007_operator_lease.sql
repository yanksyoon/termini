-- 0007_operator_lease — single-owner lease for the AI-operator remediation loop.
--
-- One row per project. Only the operator that holds a non-expired lease may act;
-- any other operator instance (the in-process `OperatorLoop` and the standalone
-- `app.operator.cli` both acquire this same lease) is a standby and issues no
-- remediation until the current owner's lease lapses (crash/failover). This is
-- the single-owner guarantee that makes remediation idempotent across operator
-- instances: two processes can never both issue an LXD command for the same
-- fault episode, because at most one holds the lease at a time.
--
-- owner_id is a stable identity: the in-process loop uses "loop:<project_id>"
-- (so a controller restart re-acquires its own lease instead of waiting it out),
-- the standalone CLI uses "cli:<ulid>" (one identity per process).
CREATE TABLE IF NOT EXISTS operator_lease (
    project_id  TEXT PRIMARY KEY,   -- the demo project (single-project controller)
    owner_id    TEXT NOT NULL,      -- stable identity of the current owner
    acquired_at TEXT NOT NULL,      -- RFC3339 UTC (ms) of last acquisition/takeover
    expires_at  TEXT NOT NULL       -- RFC3339 UTC (ms); owner must renew before this
);
