-- 0021_telemetry_source_channel — one `seq` namespace per sample source.
--
-- Defect (t_c37aa0dc): `telemetry_samples` kept ONE `seq` counter per node, and
-- `store.insert_samples` dropped any sample whose `seq` was not greater than that
-- node's highest stored `seq`. The demo fault console writes its injected series
-- through the same path, so the injector's 1 Hz samples consumed `seq` values the
-- node's own agent (reporting at ~0.3 Hz with its own counter) had not reached
-- yet. After a 60 s injection the node was ~40 seqs behind, and every real
-- sample it posted was accepted (202) and dropped: measured on node-c, the
-- node's own series was missing for 2 m 19 s after a clear, while the dashboard
-- showed the injector's fabricated healthy baseline (62.0 %).
--
-- Fix: `source` names the sample's provenance channel (`node` for the node's own
-- telemetry, `demo_fault` for the operator's injected series). Dedup and head
-- lookups are per (node_id, source), so the injected series can never occupy a
-- `seq` the node has yet to send. Every existing row is the node's own series,
-- so the backfill default is `node`.
--
-- SQLite cannot drop the inline UNIQUE (node_id, seq) constraint, so the table
-- is rebuilt. Column order is preserved and `id` is carried over, keeping row
-- identity stable for any external reader.
CREATE TABLE telemetry_samples_v2 (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id      TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'node',      -- provenance channel: node | demo_fault
    seq          INTEGER NOT NULL,
    ts           TEXT NOT NULL,                      -- RFC3339 exactly as sent
    ts_epoch     REAL NOT NULL,                      -- seconds since epoch (UTC)
    payload_json TEXT NOT NULL,                      -- validated sample object
    UNIQUE (node_id, source, seq)
);

INSERT INTO telemetry_samples_v2 (id, call_id, node_id, source, seq, ts, ts_epoch, payload_json)
    SELECT id, call_id, node_id, 'node', seq, ts, ts_epoch, payload_json
      FROM telemetry_samples;

DROP TABLE telemetry_samples;

ALTER TABLE telemetry_samples_v2 RENAME TO telemetry_samples;

CREATE INDEX IF NOT EXISTS idx_samples_node_time
    ON telemetry_samples (node_id, ts_epoch);
