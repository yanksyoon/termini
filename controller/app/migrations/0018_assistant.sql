-- 0018_assistant — assistant-initiated remediation attribution (t_bfd2c2f7).
--
-- The assistant endpoint (POST /v1/projects/{pid}/assistant/ask) may execute any
-- action the guardrail admits, with an audit row that is attributable to the
-- conversation/message that asked for it. This migration adds the columns that
-- make that attribution durable on the two existing decision-surface tables:
--
--   * `initiated_by` — 'operator' (the operator loop) or 'assistant'. Existing
--     rows backfill to 'operator' so the operator path is unchanged.
--   * `conversation_id` / `message_id` — the assistant conversation and message
--     that requested the action (NULL for operator-initiated rows).
--
-- Both tables already carry the decision/audit identity, so an assistant action
-- appears on the decision surface (GET /v1/operator/{decisions,audits}) exactly
-- like an operator action, distinguishable by `initiated_by`.

ALTER TABLE remediation_audits ADD COLUMN initiated_by TEXT NOT NULL DEFAULT 'operator';
ALTER TABLE remediation_audits ADD COLUMN conversation_id TEXT;
ALTER TABLE remediation_audits ADD COLUMN message_id TEXT;

ALTER TABLE remediation_decisions ADD COLUMN initiated_by TEXT NOT NULL DEFAULT 'operator';
ALTER TABLE remediation_decisions ADD COLUMN conversation_id TEXT;
ALTER TABLE remediation_decisions ADD COLUMN message_id TEXT;
