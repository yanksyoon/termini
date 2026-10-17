"""F05 restart-refusal guard — a disk remounted read-only fences the workload.

Covers the two halves of the F05 scenario:

  * the pure guard (`decision.fenced_action_refusal` / `read_only_mounts` /
    `fenced_refusal_detail`) — refuses an `instance.restart`/`instance.start`
    while the node's latest telemetry still shows `mount_ro == true`;
  * the fallback incident (`decision.build_fallback_incident`) — names the mount
    and the read-only cause, and never claims recovery;
  * the loop wiring — a node that is OOM *and* read-only gets its `instance.stop`
    issued (the correct half) but its `instance.restart` refused with
    `reason == "disk_fenced"` on the audit row.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tests"))

from app import db, store, ulid  # noqa: E402
from app.operator import detect, playbook, Thresholds, OperatorLoop  # noqa: E402
from app.operator import decision  # noqa: E402
from app.operator import store as opstore  # noqa: E402
from app.operator.executor import LxdExecutor  # noqa: E402
from app.operator.models import HealthEvent, RemediationAction  # noqa: E402
from stub_lxd import StubLxd  # noqa: E402

RAM_TOTAL = 34_359_738_368
RAM_HIGH = 34_000_000_000


def ro_sample(seq: int, ts: str, *, ram_high: bool = False, vm_id: str = "01H4KZ9Q2R3T5V7W9X1Y3Z5E4H") -> dict:
    s = {
        "seq": seq,
        "ts": ts,
        "ram_total_bytes": RAM_TOTAL,
        "ram_used_bytes": RAM_HIGH if ram_high else RAM_TOTAL // 2,
        "disk": [{"path": "/dev/sda", "total_bytes": 274_877_906_944,
                  "free_bytes": 200_000_000_000, "write_latency_ms": 3.0,
                  "mount_ro": True}],
        "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": 0.8}],
        "lxd_instances": [
            {"vm_id": vm_id, "state": "running",
             "ram_used_bytes": 10_000_000_000, "ram_limit_bytes": 16_000_000_000,
             "disk_used_bytes": 1_000_000_000},
        ],
    }
    return s


class FencedGuardPureTest(unittest.TestCase):
    def test_restart_refused_on_read_only_disk(self):
        state = ro_sample(1, "2026-10-04T08:00:00Z")
        self.assertEqual(decision.fenced_action_refusal(state, "instance.restart"),
                         decision.DISK_FENCED)
        self.assertEqual(decision.fenced_action_refusal(state, "instance.start"),
                         decision.DISK_FENCED)

    def test_stop_delete_prune_allowed(self):
        state = ro_sample(1, "2026-10-04T08:00:00Z")
        for kind in ("instance.stop", "instance.delete", "image.prune", "instance.create"):
            self.assertIsNone(decision.fenced_action_refusal(state, kind), kind)

    def test_restart_allowed_when_healthy(self):
        healthy = ro_sample(1, "2026-10-04T08:00:00Z")
        healthy["disk"][0]["mount_ro"] = False
        self.assertIsNone(decision.fenced_action_refusal(healthy, "instance.restart"))
        self.assertIsNone(decision.fenced_action_refusal(healthy, "instance.start"))

    def test_no_disk_no_refusal(self):
        state = {"seq": 1, "ts": "2026-10-04T08:00:00Z"}
        self.assertIsNone(decision.fenced_action_refusal(state, "instance.restart"))
        self.assertIsNone(decision.fenced_action_refusal(None, "instance.restart"))
        self.assertIsNone(decision.fenced_action_refusal({}, "instance.start"))

    def test_read_only_mounts_names_the_path(self):
        state = {"disk": [{"path": "/dev/sda", "mount_ro": True},
                          {"path": "/dev/sdb", "mount_ro": False}]}
        self.assertEqual(decision.read_only_mounts(state), ["/dev/sda"])

    def test_refusal_detail_names_mount_and_read_only(self):
        state = ro_sample(1, "2026-10-04T08:00:00Z")
        detail = decision.fenced_refusal_detail(state, "instance.restart", "node-1")
        self.assertIn("disk_fenced", detail)
        self.assertIn("/dev/sda", detail)
        self.assertIn("mount_ro=true", detail)
        self.assertIn("instance.restart", detail)

    def test_fallback_incident_names_mount_and_does_not_claim_recovery(self):
        ev = HealthEvent(
            event_id="ev-1", node_id="node-1", rule_id="disk-fail-01",
            type="disk_fail", metric="disk_mount_ro", value=1.0,
            threshold="any disk[].mount_ro == true (EXT4 remount read-only)",
            fired_at="2026-10-04T08:00:00Z",
            evidence=[ro_sample(1, "2026-10-04T08:00:00Z")],
        )
        action = RemediationAction(
            kind="instance.stop", params={"instance_name": "agent-c1", "force": True},
            reason="disk remounted read-only", playbook="disk-fail-01",
            rule_id="disk-fail-01",
        )
        rec = decision.build_fallback_incident(ev, action, "inference_unavailable")
        self.assertEqual(rec["generated"], "fallback")
        self.assertIn("/dev/sda", rec["explanation"])
        self.assertIn("read-only", rec["explanation"])
        self.assertIn("fenced", rec["explanation"])
        self.assertIn("not recovered", rec["explanation"])
        # The fallback record never claims the workload recovered.
        self.assertNotIn("recovered.", rec["explanation"].replace("not recovered", ""))


class FencedGuardLoopTest(unittest.TestCase):
    """The loop issues the stop (correct half) but refuses the restart (guard)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-fenced-")
        self.db_path = os.path.join(self.tmp, "test.db")
        db.migrate(self.db_path)

        self.lxd = StubLxd()
        self.lxd.start()
        self.addCleanup(self.lxd.stop)
        self.lxd.set_state("agent-c1", "Running")

        self.cfg = SimpleNamespace(
            db_path=self.db_path,
            operator_poll_interval_s=1.0,
            operator_cooldown_s=0.0,
            operator_window=60,
            lxd_base_url=self.lxd.base_url,
            operator_dry_run=False,
            lxd_request_timeout_s=5.0,
            lxd_retry_max=2,
            lxd_retry_backoff_s=0.01,
            lxd_client_cert=None,
            lxd_client_key=None,
        )

        with db.session(self.db_path) as conn:
            self.project_id = ulid.new_ulid()
            conn.execute(
                "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (self.project_id, "test", "hash", 1, store.now_iso()),
            )
            self.project = {"id": self.project_id, "project_token_hash": "hash"}
            self.node_id = ulid.new_ulid()
            conn.execute(
                "INSERT INTO nodes (id, project_id, registration_id, machine_id,"
                " hostname_hash, role, node_token_hash, hardware_json, os_json,"
                " lxd_json, role_hint, installer_version, status, last_seen_at,"
                " registered_at, source_ip)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.node_id, self.project_id, "reg-1", None,
                 "5f2a9c1e7b4d4a1f", "compute", "tokhash", "{}", None, None,
                 "auto", "0.1.0", "online", store.now_iso(), store.now_iso(), None),
            )

        self.vm_id = ulid.new_ulid()
        self.playbooks = playbook.load_playbooks(str(REPO / "controller" / "playbooks"))

    def _make_loop(self):
        lxd = LxdExecutor(self.lxd.base_url, dry_run=False, retry_max=2, backoff_s=0.01)
        return OperatorLoop(self.cfg, self.project, Thresholds(), self.playbooks,
                            instance_names={self.vm_id: "agent-c1"},
                            default_instance_name="agent", lxd=lxd)

    def _insert(self, samples):
        with db.session(self.db_path) as conn:
            store.insert_samples(conn, self.node_id, ulid.new_ulid(), samples)

    def test_oom_restart_refused_while_disk_read_only(self):
        # A node that is OOM (would restart the workload) AND whose disk remounted
        # read-only (would stop it). The stop must be issued; the restart must be
        # refused by the guard because resuming writes onto the failed disk is the
        # one thing that must never happen.
        samples = []
        for i in range(3):
            samples.append(ro_sample(100 + i, f"2026-10-04T08:00:{i:02d}Z",
                                     ram_high=True, vm_id=self.vm_id))
        self._insert(samples)

        loop = self._make_loop()
        produced = loop.run_once()

        by_rule = {p["rule_id"]: p for p in produced}
        self.assertIn("disk-fail-01", by_rule)
        self.assertIn("oom-guard-01", by_rule)

        # The disk-fail half: a real stop reaches the stub.
        self.assertEqual(by_rule["disk-fail-01"]["action_kind"], "instance.stop")
        self.assertEqual(by_rule["disk-fail-01"]["result"], "ok")

        # The OOM half: the restart is refused by the guard, not executed.
        self.assertEqual(by_rule["oom-guard-01"]["action_kind"], "instance.restart")
        self.assertEqual(by_rule["oom-guard-01"]["result"], "error")

        # No restart reached the stub LXD.
        restarts = [(m, p, b) for (m, p, b) in self.lxd.requests
                    if m == "PUT" and isinstance(b, dict) and b.get("action") == "restart"]
        self.assertEqual(restarts, [])

        # The audit row records the machine-readable refusal reason.
        with db.session(self.db_path) as conn:
            audits = opstore.list_audits(conn, self.node_id, 20)
        oom_audit = [a for a in audits if a["rule_id"] == "oom-guard-01"][0]
        self.assertEqual(oom_audit["reason"], decision.DISK_FENCED)
        self.assertEqual(oom_audit["result"], "error")
        self.assertIn("disk_fenced", oom_audit["detail"])


if __name__ == "__main__":
    unittest.main()
