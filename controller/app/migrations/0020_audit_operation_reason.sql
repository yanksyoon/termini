-- 0020_audit_operation_reason — record the LXD operation id and the
-- machine-readable refusal reason on the remediation audit row.
--
-- Two facts a judge (or the F05 demo) needs to read back from
-- GET /v1/operator/audits that the row could not carry before:
--
--   1. `lxd_operation` — the LXD operation id the write returned. A remediation
--      that "reached real LXD" is only provable when the row names the operation
--      LXD accepted (202 + `/1.0/operations/<id>`) and the executor polled to
--      Success. The executor already captures it on `ExecResult.operation`;
--      until now it was dropped before the audit write.
--
--   2. `reason` — the machine-readable refusal code when an action was NOT
--      issued (the same `ExecResult.reason` the operator-log line prints). The
--      F05 restart-refusal guard refuses an `instance.restart`/`instance.start`
--      on a node whose disk remounted read-only; the row must say `disk_fenced`
--      rather than a bare `error` buried in prose.
ALTER TABLE remediation_audits ADD COLUMN lxd_operation TEXT;
ALTER TABLE remediation_audits ADD COLUMN reason TEXT;
