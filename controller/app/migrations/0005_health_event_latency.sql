-- 0004_health_event_latency — record the fault-onset timestamp on each health
-- event so detection latency (breach_started_at -> fired_at) is auditable.

ALTER TABLE health_events ADD COLUMN breach_started_at TEXT;
