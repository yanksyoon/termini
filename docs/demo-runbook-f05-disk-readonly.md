# F05 runbook — "a disk remounted read-only" (fence-fast-and-stop)

The counterweight to F01. There, the right move is a recoverable restart. Here the
right move is destructive and justified: **stop writes before data corruption**,
then **refuse** to put the workload back on the failed disk. The follow-through is
the point — it must NOT restart, NOT delete, NOT re-run, and NOT claim recovery.

- Guard code: `controller/app/operator/decision.py`
  (`fenced_action_refusal` / `fenced_refusal_detail` / `read_only_mounts`,
  `DISK_FENCED = "disk_fenced"`).
- Playbook: `controller/playbooks/disk-fail.yaml` (`instance.stop`, `target: heaviest`).
- Detection: `disk-fail-01` — any `disk[].mount_ro == true` (single sample fires).
- Dashboard state: `fenced` — "needs a human: disk failed, workload fenced".

## Precondition (do not demo past this if it fails)

From the controller, the demo node's LXD API must answer 200 (trusted):

```bash
cd /opt/termini/controller && PYTHONPATH=/opt/termini/controller:/opt/termini \
  TERMINI_DB_PATH=/data/controller.db python3 -m app.lxdtrust verify \
  --node https://termini-node-c.asia-south1-a.c.termini-510609.internal:8443
# expect: TRUSTED — authenticated GET /1.0/instances -> 200
```

If it does not answer, **block with evidence** — a decision that never executed is
not a demo.

## The run

1. **Inject** `disk-fail` on the real node, targeting a real instance:

   ```bash
   POST /v1/projects/<pid>/demo/faults
   {"node_id": "<real node>", "scenario": "disk-fail", "target": "real", "duration_s": 120}
   ```

   The driver writes a faulted sample every second into the same store path the
   node's own telemetry uses; `disk[].mount_ro` flips `true`.

2. **Watch the card.** `disk-fail-01` fires (single sample), the operator decides
   `instance.stop` on the instance holding the failed mount (the `heaviest` target,
   resolved against the node's live LXD inventory — the F01 target rule). The
   incident card's evidence names the metric `disk_mount_ro`; the explanation names
   the mount and the read-only cause.

3. **The stop, with its operation id.** Open the audit row
   (`GET /v1/operator/audits`): `action_kind: instance.stop`, `result: ok`,
   `lxd_path: /1.0/instances/<name>/state`, and `lxd_operation: /1.0/operations/<id>`
   — the LXD operation the executor waited on to Success.

4. **The node is fenced.** The dashboard shows the node `fenced` with
   "needs a human: disk failed, workload fenced". There is **no auto-restart** —
   the disk-fail playbook is `stop`, and the guard would refuse a restart anyway.

5. **Restart refused.** While the node's latest telemetry still shows
   `mount_ro: true`, any `instance.restart` / `instance.start` on that node is
   refused by the guard **before it reaches LXD**. The audit row records
   `result: error`, `reason: disk_fenced`, and the operator-log line says why
   (`disk_fenced: refused instance.restart … mount_ro=true on /dev/sda …`). A
   restart that would resume writes onto the failed disk never executes.

6. **What a human does next.** The guard does not auto-recover; a human must:
   (a) replace the failed disk (in the demo: clear the fault, which restores the
   healthy series and re-arms the rule), then (b) raise the instance elsewhere
   (`/vms/apply` to materialise it on a healthy node, or restart it once the node
   reports `mount_ro: false` again).

## Graded "must NOT" (asserted, not hoped)

- No `instance.restart` / `instance.start` is issued while `mount_ro: true` —
  the guard refuses it in code, regardless of what the model/playbook decided.
- No `instance.delete` — the disk-fail playbook is `stop`, never `delete`.
- No second action in the same episode — one health event → one decision
  (schema-enforced `event_id` UNIQUE), one audit.
- No claim of recovery — the incident record names the mount and the read-only
  cause and says the workload is fenced, never recovered.
