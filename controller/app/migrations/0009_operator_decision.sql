-- 0009_operator_decision — model-driven decision provenance + fallback state
-- (docs/contracts/operator-decision.md §11). Adds provenance columns to the
-- decision log (decision_source/confidence/fallback_reason) and the
-- per-(node, rule) fallback-streak / forced-deterministic-cooldown table.

-- decision_source ∈ deterministic | model | fallback. `confidence` is NULL for
-- deterministic decisions; `fallback_reason` is one of the §10.1 codes, NULL
-- otherwise. Existing rows keep the deterministic default, so the migration is
-- backward-compatible.
ALTER TABLE remediation_decisions ADD COLUMN decision_source TEXT NOT NULL DEFAULT 'deterministic';
ALTER TABLE remediation_decisions ADD COLUMN confidence REAL;
ALTER TABLE remediation_decisions ADD COLUMN fallback_reason TEXT;

-- Repeated-failure loop (operator-decision.md §10.2): per (node, rule) the loop
-- tracks a fallback streak and a forced-deterministic cooldown window.
CREATE TABLE IF NOT EXISTS decision_fallback_state (
    node_id         TEXT NOT NULL,
    rule_id         TEXT NOT NULL,
    fallback_streak INTEGER NOT NULL DEFAULT 0,
    forced_until    TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (node_id, rule_id)
);
