-- 0016_incident_record — the §4.2 incident record on the decision row
-- (docs/contracts/operator-decision.md §11, §8.1). Adds the structured answer
-- to "what was the incident, why does the AI think so, on what evidence, what
-- did it do" alongside the decision log, so an incident response is not reduced
-- to a one-line `rationale`.
--
-- The record is written in the SAME statement/transaction as the decision, so a
-- record can never exist without its decision. `incident_evidence` and
-- `incident_actions` are JSON arrays (§4.2); `incident_title`/`incident_explanation`
-- are the bounded strings; `incident_generated` ∈ 'model' | 'fallback' (§8.1).
--
-- All five columns are NULL for a deterministic-mode row (§8.1), which is what
-- keeps that mode byte-identical to the pre-amendment loop (§12).
ALTER TABLE remediation_decisions ADD COLUMN incident_title TEXT;
ALTER TABLE remediation_decisions ADD COLUMN incident_explanation TEXT;
ALTER TABLE remediation_decisions ADD COLUMN incident_evidence TEXT;   -- JSON array (§4.2)
ALTER TABLE remediation_decisions ADD COLUMN incident_actions TEXT;    -- JSON array (§4.2)
ALTER TABLE remediation_decisions ADD COLUMN incident_generated TEXT;  -- model | fallback
