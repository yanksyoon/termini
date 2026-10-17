-- 0022_operator_log_actor_ids — let the operator log name WHO acted, not just which
-- kind of actor (kanban t_9c1362f6).
--
-- The live fleet is shared by several `infra` workers and every one of them presents
-- the same project token, so `actor IN ('ai_operator','human','assistant')` could not
-- say that a PEER cleared an injection: on 2026-10-17 a peer's clear-all was written
-- down as `human` ("the operator") and invalidated another worker's live acceptance.
-- The identity now comes from the caller's `X-Termini-Actor` header
-- (docs/contracts/controller-api.md §4.7) — a card id such as `t_c37aa0dc`, a tool
-- name such as `tool:demo-fault-clear-telemetry`, or `human` for the console. The
-- enum values above stay valid; the CHECK becomes a shape/length guard instead of a
-- closed set. `logs.py` still refuses to invent an actor: anything unrecognisable is
-- recorded as `unattributed`, never as `ai_operator`/`human`.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt in place (same
-- column order, data copied verbatim; operator_logs is not referenced by a foreign
-- key — the same procedure as 0019_assistant_command_actor.sql).

PRAGMA foreign_keys=OFF;

ALTER TABLE operator_logs RENAME TO operator_logs_old;
CREATE TABLE operator_logs (
    log_id           TEXT PRIMARY KEY,                 -- ULID
    project_id       TEXT NOT NULL,
    ts               TEXT NOT NULL,
    node_id          TEXT,                             -- null for project-level
    severity         TEXT NOT NULL CHECK (severity IN ('debug', 'info', 'warn', 'error')),
    -- 'ai_operator' | 'human' | 'assistant' | an explicit actor id (1..64 chars of
    -- [A-Za-z0-9_.:@-]): the id of the card/tool that acted, from X-Termini-Actor.
    actor            TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 64),
    message          TEXT NOT NULL,
    remediation_json TEXT,
    truncated        INTEGER NOT NULL DEFAULT 0
);
INSERT INTO operator_logs SELECT * FROM operator_logs_old;
DROP TABLE operator_logs_old;

CREATE INDEX IF NOT EXISTS idx_operator_logs_project ON operator_logs (project_id, log_id);

PRAGMA foreign_keys=ON;
