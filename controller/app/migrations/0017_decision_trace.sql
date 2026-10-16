-- 0017_decision_trace — the §4.3 decision trace on the decision row
-- (docs/contracts/operator-decision.md §4.3, §11). Adds the AI's reasoning
-- steps for a diagnosis — how the answer was reached, not just what it was —
-- so the panel can show a thinking log, not only a conclusion.
--
-- `decision_trace` is a JSON object (§4.3): `{trace_version, decided_by,
-- steps[]}` with each step `{step, ts, label, detail, state}`. `trace_version`
-- mirrors the top-level version on its own column so the API can return it
-- without parsing.
--
-- Written in the SAME statement/transaction as the decision (store.py), so a
-- trace can never exist without its decision. The `executed` / `audit_recorded`
-- steps are appended in the audit's transaction (store.append_decision_trace).
--
-- Both columns are NULL for a deterministic-mode row (§8.1), which is what
-- keeps that mode byte-identical to the pre-amendment loop (§12).
ALTER TABLE remediation_decisions ADD COLUMN decision_trace TEXT;   -- JSON object (§4.3)
ALTER TABLE remediation_decisions ADD COLUMN trace_version INTEGER; -- §4.3 top-level version
