"""Contract-shaped tests for the operator demo fault console (controller-api.md §4.7).

The endpoints are contract, so these tests exercise the *wire*: a real controller
process on an ephemeral port, a real node registration, real telemetry, and the
`POST/GET/DELETE /v1/projects/{pid}/demo/faults` routes with and without
credentials. What they pin:

  * auth — no token `401`, `node_token` `403` (the route is not reachable from a
    node context), project token `201`;
  * the injected series drives the frozen rule (`oom-guard-01` …) past its enter
    threshold and the rule's hysteresis returns below its clear threshold on clear;
  * while an injection is open, the node's own telemetry is superseded;
  * refusals — a second injection for the same (node, rule) `409 fault_already_active`,
    a re-injection while a remediation for that (node, rule) is in flight `409
    remediation_in_flight`, provenance mismatch `422`;
  * TTL — an injection ends by itself, loudly, at its TTL (never silently);
  * restart — an injection a previous process can no longer drive is swept to
    `expired` at boot;
  * the console page — `console/serve.py` serves `/console/` with the controller
    URL and project id but NEVER the project token.

Run: python3 -m unittest tests.test_demo_faults -v   (from the repo root)
"""
from __future__ import annotations

import json
import pathlib
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.request
from typing import Any

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))

from app import demo_faults  # noqa: E402
from app import db as appdb  # noqa: E402
from app.operator import detect  # noqa: E402

GiB = 1024 ** 3
PROJECT_ID = "01J0DEM0WARM000000000000AB"
PROJECT_TOKEN = "test-project-token"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(n: int = 0) -> str:
    val = ((int(time.time() * 1000) & ((1 << 48) - 1)) << 80) | ((n * 2654435761) & ((1 << 80) - 1))
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[val & 0x1F])
        val >>= 5
    return "".join(reversed(out))


def _actor_header(actor: str | None) -> dict:
    """`X-Termini-Actor` for a wire call; `None` means "send no header at all".

    Tests that do not care about attribution want the operator console's behaviour
    (`human`); the attribution tests pass an explicit card id or `None` to exercise
    the un-attributed path (controller-api.md §4.7, kanban t_9c1362f6).
    """
    return {"X-Termini-Actor": actor} if actor else {}


def http(base: str, method: str, path: str, body=None, token: str | None = None,
         timeout: float = 15.0, headers: dict | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(base + path, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        finally:
            e.close()
        try:
            return e.code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------- #
# pure-function tests (no server)
# --------------------------------------------------------------------------- #
class SampleTests(unittest.TestCase):
    """The injected series must move exactly the metric its scenario names."""

    BASE = {
        "ram_total_bytes": 8 * GiB, "ram_used_bytes": 1 * GiB, "cpu_util_pct": 1.0,
        "disk": [{"path": "/dev/sda", "total_bytes": 100 * GiB, "free_bytes": 1,
                  "write_latency_ms": 99.0, "mount_ro": True}],
        "net": [{"iface": "eth0", "latency_ms": 42.0}],
        "lxd_instances": [{"vm_id": "01M4AAAAAAAAAAAAAAAAAAAAAA", "ram_limit_bytes": 4 * GiB,
                           "ram_used_bytes": 1 * GiB}],
    }

    def test_healthy_baseline_is_below_every_clear_threshold(self):
        h = demo_faults.healthy_sample(self.BASE, seq=1, ts="2026-10-15T00:00:00.000Z")
        rec = detect.recovered([h] * 5, detect.Thresholds())
        self.assertEqual(rec, {r: True for r in detect.RULE_IDS}, rec)
        self.assertFalse(h["disk"][0]["mount_ro"])

    def test_each_scenario_breaches_its_own_rule(self):
        h = demo_faults.healthy_sample(self.BASE, seq=1, ts="2026-10-15T00:00:00.000Z")
        th = detect.Thresholds()
        for scenario, rule in demo_faults.RULE_BY_SCENARIO.items():
            with self.subTest(scenario=scenario):
                s = demo_faults.fault_sample(h, scenario, None, seq=2,
                                             ts="2026-10-15T00:00:01.000Z")
                streak = detect.breach_streak([s], th)[rule]
                self.assertTrue(streak, f"{scenario} did not breach {rule}")
                # …and only its own rule: the other scenarios stay healthy.
                others = [r for r in detect.RULE_IDS if r != rule]
                for other in others:
                    self.assertFalse(detect.breach_streak([s], th)[other],
                                     f"{scenario} also breached {other}")

    def test_clear_sample_count_matches_the_rules_hysteresis(self):
        th = detect.Thresholds()
        self.assertEqual(demo_faults.clear_samples_for("oom-guard-01", th),
                         th.oom_clear_consecutive)
        self.assertEqual(demo_faults.clear_samples_for("disk-fail-01", th),
                         th.disk_fail_clear_consecutive)
        self.assertGreaterEqual(demo_faults.clear_samples_for("nonsense", th), 1)

    def test_recovery_burst_provably_rearms_the_rule(self):
        """The exact series a clear emits must satisfy `recovered` for the rule."""
        h = demo_faults.healthy_sample(self.BASE, seq=1, ts="2026-10-15T00:00:00.000Z")
        th = detect.Thresholds()
        for scenario, rule in demo_faults.RULE_BY_SCENARIO.items():
            faulted = demo_faults.fault_sample(h, scenario, None, seq=2,
                                               ts="2026-10-15T00:00:01.000Z")
            n = demo_faults.clear_samples_for(rule, th)
            recovery = [demo_faults.healthy_sample(h, seq=3 + i,
                                                   ts=f"2026-10-15T00:00:0{3 + i}.000Z")
                        for i in range(n)]
            self.assertTrue(detect.recovered([faulted] + recovery, th)[rule],
                            f"{scenario}: {n} recovery sample(s) did not re-arm {rule}")


# --------------------------------------------------------------------------- #
# wire tests against a real controller process
# --------------------------------------------------------------------------- #
class FaultEndpointsTest(unittest.TestCase):
    """Boots the controller once; every wire test drives the §4.7 routes."""

    @classmethod
    def setUpClass(cls):
        cls.state = pathlib.Path(tempfile.mkdtemp(prefix="demo-faults-test-"))
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.db_path = str(cls.state / "controller.db")
        env = dict(
            __import__("os").environ,
            TERMINI_HOST="127.0.0.1", TERMINI_PORT=str(cls.port),
            TERMINI_DB_PATH=cls.db_path,
            TERMINI_PROJECT_ID=PROJECT_ID, TERMINI_PROJECT_TOKEN=PROJECT_TOKEN,
            TERMINI_BOOTSTRAP_TOKEN="t-boot-1,t-boot-2",
            TERMINI_OPERATOR_ENABLED="true", TERMINI_OPERATOR_DRY_RUN="true",
            TERMINI_OPERATOR_POLL_INTERVAL_S="1", TERMINI_OPERATOR_COOLDOWN_S="3",
            TERMINI_OOM_CONSECUTIVE="2", TERMINI_OOM_CLEAR_CONSECUTIVE="2",
            TERMINI_DEMO_FAULT_GUARD_S="120",
        )
        cls.log = open(cls.state / "controller.log", "w")
        cls.proc = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=str(CONTROLLER),
                                    env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                http(cls.base, "GET", "/v1/telemetry/recent", timeout=2)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise AssertionError("controller did not start; see " + str(cls.state / "controller.log"))
        cls._seq = 0

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.log.close()

    def setUp(self):
        """A fresh node per test.

        The `remediation_in_flight` guard is per `(node, rule)`, so tests that
        inject the same scenario must not share a node the operator has already
        remediated — otherwise a test would be refused because of an earlier test's
        episode rather than because of its own subject.
        """
        self.node = self._new_node()

    # ------------------------------------------------------------- fixtures
    @classmethod
    def _mint_bootstrap(cls) -> str:
        st, body = http(cls.base, "POST", "/v1/bootstrap-tokens", {"count": 1},
                        token=PROJECT_TOKEN)
        assert st in (200, 201) and body.get("tokens"), (st, body)
        return body["tokens"][0]

    @classmethod
    def _new_node(cls, hostname: str | None = None, *, synthetic: bool = False,
                  seed_seq: int = 100) -> dict:
        cls._seq += 1
        host = hostname or f"test-node-{cls._seq:02d}"
        body = {
            "schema_version": "1.0", "synthetic": synthetic,
            "machine": {"machine_id": host, "hostname": host,
                        "cpu": {"model": "test", "cores": 4, "threads": 8},
                        "memory": {"total_bytes": 8 * GiB},
                        "disks": [{"device": "/dev/sda", "size_bytes": 100 * GiB}],
                        "gpus": []},
        }
        st, reg = http(cls.base, "POST", "/v1/nodes/register", body,
                       token=cls._mint_bootstrap())
        assert st in (200, 201), (st, reg)
        reg["synthetic"] = synthetic
        cls._telemetry(reg, seed_seq)
        return reg

    @classmethod
    def _telemetry(cls, node: dict, seq: int):
        sample = {
            "seq": seq, "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "uptime_sec": 3600 + seq, "cpu_util_pct": 31.2, "cpu_core_count": 8,
            "ram_total_bytes": 8 * GiB, "ram_used_bytes": int(8 * GiB * 0.62),
            "hostname": "5f2a9c1e7b4d4a1f", "ip_local": "10.0.0.0/24",
            "mac_address": "9c1e7b4d4a1f5f2a", "project_id": PROJECT_ID,
            "disk": [{"path": "/dev/sda", "total_bytes": 100 * GiB,
                      "free_bytes": int(100 * GiB * 0.78), "write_latency_ms": 3.0,
                      "mount_ro": False}],
            "net": [{"iface": "eth0", "rx_bytes": 1, "tx_bytes": 1, "latency_ms": 0.8}],
            "services": [{"name": "lxd", "status": "running"}],
            "lxd_instances": [{"vm_id": "01M4AAAAAAAAAAAAAAAAAAAAAA", "state": "running",
                               "ram_limit_bytes": 4 * GiB, "ram_used_bytes": 1 * GiB}],
        }
        env = {"_redaction": {"version": "redactor-config v3", "config_hash": "3f2a1b",
                              "result": "ok"},
               "call_id": ulid(seq), "node_id": node["node_id"], "schema_version": 1,
               "governance_chain_tip": ulid(seq), "samples": [sample]}
        return http(cls.base, "POST", f"/v1/nodes/{node['node_id']}/telemetry", env,
                    token=node["node_token"])

    def _inject(self, actor: str | None = "human", **body):
        body.setdefault("node_id", self.node["node_id"])
        body.setdefault("target", "real")
        body.setdefault("duration_s", 30)
        return http(self.base, "POST", f"/v1/projects/{PROJECT_ID}/demo/faults", body,
                    token=PROJECT_TOKEN, headers=_actor_header(actor))

    def _clear(self, injection_id: str, actor: str | None = "human"):
        return http(self.base, "DELETE",
                    f"/v1/projects/{PROJECT_ID}/demo/faults/{injection_id}",
                    token=PROJECT_TOKEN, headers=_actor_header(actor))

    def _metrics(self, node_id: str, metric: str = "ram_used_bytes"):
        st, body = http(self.base, "GET",
                        f"/v1/projects/{PROJECT_ID}/nodes/{node_id}/metrics"
                        f"?metrics={metric}&resolution=raw", token=PROJECT_TOKEN)
        self.assertEqual(st, 200, body)
        series = body["series"]
        return series[0]["points"] if series else []

    def _rows(self, sql: str, args=()) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    # ------------------------------------------------------------ auth tests
    def test_start_requires_a_token(self):
        st, body = http(self.base, "POST", f"/v1/projects/{PROJECT_ID}/demo/faults",
                        {"node_id": self.node["node_id"], "scenario": "oom", "target": "real"})
        self.assertEqual(st, 401, body)
        self.assertEqual(body["error"]["code"], "UNAUTHENTICATED")

    def test_node_token_cannot_inject(self):
        st, body = http(self.base, "POST", f"/v1/projects/{PROJECT_ID}/demo/faults",
                        {"node_id": self.node["node_id"], "scenario": "oom", "target": "real"},
                        token=self.node["node_token"])
        self.assertEqual(st, 403, body)
        self.assertEqual(body["error"]["code"], "PERMISSION_DENIED")

    def test_list_requires_a_token(self):
        st, body = http(self.base, "GET", f"/v1/projects/{PROJECT_ID}/demo/faults")
        self.assertEqual(st, 401, body)

    def test_clear_requires_a_token(self):
        st, body = http(self.base, "DELETE",
                        f"/v1/projects/{PROJECT_ID}/demo/faults/{ulid(9)}")
        self.assertEqual(st, 401, body)

    # ------------------------------------------------------- validation tests
    def test_unknown_scenario_is_rejected(self):
        st, body = self._inject(scenario="meltdown")
        self.assertEqual(st, 422, body)
        self.assertEqual(body["error"]["code"], "INVALID_ARGUMENT")

    def test_removed_scenarios_are_rejected_with_the_allowed_set(self):
        """disk-degrade and latency are no longer injectable (owner decision).

        The 422 must list the allowed set — not 500, not a silent accept.
        """
        for scenario in ("disk-degrade", "latency"):
            with self.subTest(scenario=scenario):
                st, body = self._inject(scenario=scenario)
                self.assertEqual(st, 422, body)
                self.assertEqual(body["error"]["code"], "INVALID_ARGUMENT")
                self.assertIn("oom", body["error"]["message"])
                self.assertIn("disk-fail", body["error"]["message"])

    def test_duration_out_of_range_is_rejected(self):
        st, body = self._inject(scenario="oom", duration_s=1)
        self.assertEqual(st, 422, body)

    def test_target_must_match_the_nodes_provenance(self):
        synth = self._new_node(synthetic=True)
        st, body = http(self.base, "POST", f"/v1/projects/{PROJECT_ID}/demo/faults",
                        {"node_id": synth["node_id"], "scenario": "oom",
                         "target": "real", "duration_s": 30}, token=PROJECT_TOKEN)
        self.assertEqual(st, 422, body)
        self.assertEqual(body["error"]["details"][0]["field"], "target")

    def test_unknown_node_is_not_found(self):
        st, body = self._inject(node_id=ulid(77), scenario="oom")
        self.assertEqual(st, 404, body)

    # ------------------------------------------------------- behaviour tests
    def test_inject_clear_cycle_drives_and_rearms_the_rule(self):
        """The headline acceptance: inject → the rule fires → clear → it re-arms."""
        st, inj = self._inject(scenario="oom", duration_s=60)
        self.assertEqual(st, 201, inj)
        self.assertEqual(inj["rule_id"], "oom-guard-01")
        self.assertTrue(inj["open"])
        injection_id = inj["injection_id"]

        # The injected series is the node's series while it runs, and it breaches.
        peak = 0.0
        fired = None
        for _ in range(12):
            time.sleep(1.0)
            points = self._metrics(self.node["node_id"])
            peak = max([p["v"] / (8 * GiB) * 100 for p in points] or [peak, 0.0])
            st, events = http(self.base, "GET",
                              f"/v1/operator/events?node={self.node['node_id']}&limit=5",
                              token=PROJECT_TOKEN)
            if events and events.get("events"):
                fired = events["events"][0]
            if fired and peak >= 95.0:
                break
        self.assertGreaterEqual(peak, 95.0, "injected RAM never crossed the OOM threshold")
        self.assertIsNotNone(fired, "oom-guard-01 never fired")
        self.assertEqual(fired["rule_id"], "oom-guard-01")

        # The node's own report is superseded while the injection holds the series.
        st, resp = self._telemetry(self.node, 200)
        self.assertEqual(st, 202, resp)
        self.assertEqual(resp["accepted"], 0)
        self.assertEqual(resp["rejected"], 1)

        # Operator log: the client dashboard shows who injected it. Scoped to THIS
        # injection — the class shares one controller DB across tests.
        logs = self._rows("SELECT actor, message FROM operator_logs ORDER BY log_id")
        injected = [l for l in logs
                    if "fault injected by operator" in l["message"] and injection_id in l["message"]]
        self.assertTrue(injected, logs[-3:])
        self.assertEqual(injected[0]["actor"], "human")

        # Clear: a recovery burst, then the rule is provably below its threshold.
        st, cleared = self._clear(injection_id)
        self.assertEqual(st, 200, cleared)
        self.assertEqual(cleared["state"], "recovering")
        need = cleared["clear_samples_needed"]
        # Wait for the FACT, not a fixed duration. The recovery burst writes ONE
        # healthy sample per driver tick, and `time.sleep(need + 2)` was tighter than
        # a loaded CI runner's tick budget: main went red on it (2026-10-16) and the
        # ci-watchdog reverted a good commit. The assertion itself is unchanged.
        row = None
        for _ in range(int(need) * 4 + 10):
            time.sleep(1.0)
            st, listing = http(self.base, "GET",
                               f"/v1/projects/{PROJECT_ID}/demo/faults", token=PROJECT_TOKEN)
            for f in listing["faults"]:
                if f["injection_id"] == injection_id:
                    row = f
            if row is not None and row["state"] == "cleared":
                break
        self.assertIsNotNone(row, "the injection disappeared from the console listing")
        self.assertEqual(row["state"], "cleared",
                         "the recovery burst never finished (rule still in an OPEN state)")
        self.assertEqual(row["recovery_samples"], need)
        tail = self._metrics(self.node["node_id"])[-need:]
        self.assertTrue(tail, "no samples after the clear")
        for p in tail:
            self.assertLessEqual(p["v"] / (8 * GiB) * 100, 90.0,
                                 "the restored series is not below the OOM clear band")
        cleared_logs = [l for l in self._rows("SELECT message FROM operator_logs")
                        if "back below threshold" in l["message"] and injection_id in l["message"]]
        self.assertTrue(cleared_logs, "the clear was not recorded in the operator log")

        # Idempotent, and a fault is always clearable.
        st, again = self._clear(injection_id)
        self.assertEqual(st, 200)
        self.assertEqual(again["status"], "already_cleared")

    def test_a_clear_resumes_the_nodes_own_series_on_its_next_sample(self):
        """t_c37aa0dc: an injection's 1 Hz `seq` range must not starve the node.

        Measured on the live fleet before this fix: after a 60 s injection the
        node's own ~0.3 Hz counter was tens of seqs behind the injector's head, so
        its next samples were accepted (`202`) and dropped — 2 m 19 s of missing
        node telemetry. The injected series now lives in its own provenance channel
        (`source='demo_fault'`), so the node's very next sample is stored even
        though its `seq` is far below the injected head.
        """
        node = self._new_node(seed_seq=1)      # the node's own head is seq 1
        st, inj = self._inject(node_id=node["node_id"], scenario="oom", duration_s=60)
        self.assertEqual(st, 201, inj)

        # Wait until the injector has written a head well above the node's counter.
        injected_n = 0
        deadline = time.time() + 20
        while time.time() < deadline:
            rows = self._rows(
                "SELECT COUNT(*) AS n FROM telemetry_samples"
                " WHERE node_id = ? AND source = 'demo_fault'", (node["node_id"],))
            injected_n = int(rows[0]["n"])
            if injected_n >= 6:
                break
            time.sleep(0.5)
        self.assertGreaterEqual(injected_n, 6, "the injector never wrote its series")

        # While the injection holds the node's series the node's own samples are
        # superseded; on the node its counter still advances (it is only the storage
        # that skips them).
        for seq in (2, 3, 4):
            st, resp = self._telemetry(node, seq)
            self.assertEqual(st, 202, resp)
            self.assertEqual(resp["accepted"], 0, resp)

        st, cleared = self._clear(inj["injection_id"])
        self.assertEqual(st, 200, cleared)
        # The node's samples are superseded until the recovery burst closes the
        # injection; wait for the FACT (several driver ticks at 1 Hz).
        row = {}
        for _ in range(30):
            rows = self._rows("SELECT state FROM demo_faults WHERE injection_id = ?",
                              (inj["injection_id"],))
            row = rows[0] if rows else {}
            if row.get("state") == "cleared":
                break
            time.sleep(0.5)
        self.assertEqual(row.get("state"), "cleared", row)

        # THE ASSERTION: the node's next sample — seq 5, below the injected head —
        # is stored, and it is the node's real number, not the injector's baseline.
        st, resp = self._telemetry(node, 5)
        self.assertEqual(st, 202, resp)
        self.assertEqual(resp["accepted"], 1,
                         "the node's own sample was dropped after the clear")
        stored = self._rows(
            "SELECT source, seq, ts, payload_json FROM telemetry_samples"
            " WHERE node_id = ? ORDER BY id DESC LIMIT 1", (node["node_id"],))
        self.assertEqual(stored[0]["source"], "node")
        self.assertEqual(stored[0]["seq"], 5)
        self.assertAlmostEqual(
            json.loads(stored[0]["payload_json"])["ram_used_bytes"] / (8 * GiB) * 100,
            62.0, places=3)

        # Both provenances are in the stored series and remain distinguishable.
        per_source = self._rows(
            "SELECT source, seq FROM telemetry_samples WHERE node_id = ?"
            " ORDER BY source, seq", (node["node_id"],))
        node_seqs = [r["seq"] for r in per_source if r["source"] == "node"]
        fault_seqs = [r["seq"] for r in per_source if r["source"] == "demo_fault"]
        self.assertEqual(node_seqs, [1, 5], per_source)   # 2,3,4 were superseded
        self.assertGreaterEqual(len(fault_seqs), 6, per_source)
        self.assertEqual(fault_seqs, sorted(fault_seqs), per_source)
        # …and the injected head really did reach the seq the node then sent (5):
        # with one shared namespace that is exactly the sample the old store dropped.
        self.assertGreaterEqual(fault_seqs[-1], 5, per_source)

        # The dashboard/rules read the node's own latest number again.
        points = self._metrics(node["node_id"])
        self.assertAlmostEqual(points[-1]["v"] / (8 * GiB) * 100, 62.0, places=3)

    def test_second_injection_for_the_same_rule_is_refused(self):
        st, inj = self._inject(scenario="oom", duration_s=120)
        self.assertEqual(st, 201, inj)
        try:
            st, dup = self._inject(scenario="oom", duration_s=30)
            self.assertEqual(st, 409, dup)
            self.assertEqual(dup["error"]["code"], "CONFLICT")
            reasons = [d["reason"] for d in dup["error"]["details"]]
            self.assertIn("fault_already_active", reasons)
            self.assertIn(inj["injection_id"], json.dumps(dup))
            # …but a different rule on the same node is still injectable.
            st, other = self._inject(scenario="disk-fail", duration_s=120)
            self.assertEqual(st, 201, other)
            self._clear(other["injection_id"])
        finally:
            self._clear(inj["injection_id"])

    def test_reinjection_while_a_remediation_is_in_flight_is_refused(self):
        """Safety rule 3: do not stack a fault on a rule the operator is working."""
        st, inj = self._inject(scenario="disk-fail", duration_s=60)
        self.assertEqual(st, 201, inj)
        # disk-fail-01 fires on its FIRST breaching sample, so the loop remediates
        # within a couple of seconds; the guard window (600 s here) then holds.
        fired = False
        for _ in range(15):
            time.sleep(1.0)
            rows = self._rows("SELECT id FROM health_events WHERE rule_id = 'disk-fail-01'")
            if rows:
                fired = True
                break
        self.assertTrue(fired, "disk-fail-01 never fired")
        self._clear(inj["injection_id"])
        # The re-injection is only refused `remediation_in_flight` once the cleared
        # injection has left the OPEN states, and the driver needs
        # `clear_samples_needed` ticks (1 Hz: work + cadence) to get it there. The
        # fixed 3 s sleep this used to do RACES that tick — measured on this tree
        # the row reaches `cleared` at 2.86-2.99 s, so a driver slip of ~10 ms
        # leaves it `recovering`, guard 1a answers `fault_already_active` and this
        # assertion fails with the WRONG reason (reproduced 1 run in 5 on a loaded
        # box; that is the CI failure of 2026-10-16, run 38944644550). Wait for the
        # fact, bounded; the assertion below is unchanged.
        deadline = time.time() + 15
        row = {"state": "recovering"}
        while time.time() < deadline and row["state"] in demo_faults.OPEN_STATES:
            time.sleep(0.2)
            st, listing = http(self.base, "GET",
                               f"/v1/projects/{PROJECT_ID}/demo/faults",
                               token=PROJECT_TOKEN)
            self.assertEqual(st, 200, listing)
            row = next(f for f in listing["faults"]
                       if f["injection_id"] == inj["injection_id"])
        self.assertNotIn(row["state"], demo_faults.OPEN_STATES,
                         "the cleared injection never left the open states")
        st, again = self._inject(scenario="disk-fail", duration_s=30)
        self.assertEqual(st, 409, again)
        reasons = [d["reason"] for d in again["error"]["details"]]
        self.assertIn("remediation_in_flight", reasons)

    def test_a_stale_tick_cannot_resurrect_a_cleared_injection(self):
        """A tick that read its row BEFORE a clear must not write that row back.

        Deterministic reproduction of the interleaving that made main red on
        2026-10-16 (`row["state"] == "cleared"` observed as `"active"` — an EARLIER
        state, which is what proves a stale write) and cost a good commit a watchdog
        revert. The driver reads its open rows at the start of `tick()` and writes at
        the end, so a clear landing mid-tick was overwritten and the series the clear
        had just restored was re-breached. No sleeps and no load here: the
        interleaving is driven by hand.
        """
        st, inj = self._inject(scenario="oom", duration_s=60)
        self.assertEqual(st, 201, inj)
        injection_id = inj["injection_id"]
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            stale = demo_faults.get_fault(conn, injection_id)        # the tick's read
            self.assertEqual(stale["state"], "active")
            node = demo_faults.store.get_node(conn, stale["node_id"])
            injector = demo_faults.DemoFaultInjector(
                types.SimpleNamespace(db_path=self.db_path), PROJECT_ID)
            st, _cleared = self._clear(injection_id)                 # the operator's clear
            self.assertEqual(st, 200)
            # …and now the in-flight tick finishes, with the row it read BEFORE it
            sample = demo_faults.fault_sample(
                json.loads(stale["baseline_json"]), stale["scenario"],
                json.loads(stale["params_json"] or "{}"), seq=0,
                ts=demo_faults._now_iso())
            injector._write_sample(conn, node, stale, sample, state_after="active")
            fresh = demo_faults.get_fault(conn, injection_id)
            self.assertEqual(fresh["state"], "recovering",
                             "a stale tick resurrected a cleared injection "
                             f"(state={fresh['state']!r})")
        finally:
            conn.close()

    def test_injection_ends_loudly_at_its_ttl(self):
        synth = self._new_node(synthetic=True)
        st, inj = self._inject(node_id=synth["node_id"], target="synthetic",
                               scenario="oom", duration_s=5)
        self.assertEqual(st, 201, inj)
        time.sleep(5 + inj["clear_samples_needed"] + 4)
        st, listing = http(self.base, "GET", f"/v1/projects/{PROJECT_ID}/demo/faults",
                           token=PROJECT_TOKEN)
        row = next(f for f in listing["faults"] if f["injection_id"] == inj["injection_id"])
        self.assertEqual(row["state"], "cleared")
        self.assertEqual(row["clear_reason"], "ttl_expired")
        self.assertEqual(row["cleared_by"], "ttl")
        self.assertTrue([l for l in self._rows("SELECT message FROM operator_logs")
                         if "TTL expired" in l["message"]])

    def test_clear_all_closes_every_open_injection(self):
        a = self._inject(scenario="oom", duration_s=120)[1]
        b = self._inject(scenario="disk-fail", duration_s=120)[1]
        self.assertEqual(a["state"], "active")
        self.assertEqual(b["state"], "active")
        st, body = http(self.base, "POST", f"/v1/projects/{PROJECT_ID}/demo/faults/clear",
                        {"reason": "test_clear_all"}, token=PROJECT_TOKEN,
                        headers=_actor_header("human"))
        self.assertEqual(st, 200, body)
        self.assertGreaterEqual(body["count"], 2)
        self.assertTrue(all(f["state"] == "recovering" for f in body["cleared"]))
        self.assertEqual(body["cleared"][0]["clear_reason"], "test_clear_all")
        # A clear runs a recovery burst: the injections stay `recovering` (and are
        # therefore still "open") until each has emitted its re-arm samples.
        need = max(f["clear_samples_needed"] for f in body["cleared"])
        time.sleep(need + 2)
        st, listing = http(self.base, "GET",
                           f"/v1/projects/{PROJECT_ID}/demo/faults?state=open",
                           token=PROJECT_TOKEN)
        self.assertEqual([f["injection_id"] for f in listing["faults"]], [])
        st, listing = http(self.base, "GET",
                           f"/v1/projects/{PROJECT_ID}/demo/faults", token=PROJECT_TOKEN)
        for finished in body["cleared"]:
            row = next(f for f in listing["faults"]
                       if f["injection_id"] == finished["injection_id"])
            self.assertEqual(row["state"], "cleared")
            self.assertEqual(row["cleared_by"], "human")

    def test_list_reports_every_injection_and_its_scenarios(self):
        st, listing = http(self.base, "GET", f"/v1/projects/{PROJECT_ID}/demo/faults",
                           token=PROJECT_TOKEN)
        self.assertEqual(st, 200)
        self.assertEqual(listing["scenarios"], list(demo_faults.SCENARIO_NAMES))
        self.assertIn("open_count", listing)
        self.assertTrue(all("injection_id" in f for f in listing["faults"]))


    # ------------------------------------------------------------------ #
    # Acceptance (b): a clear names WHO did it — never a silent "the operator".
    #
    # Every worker and the console present the same project token, so on 2026-10-17 a
    # peer's clear-all was recorded as `cleared_by=human` and a live acceptance was
    # invalidated by what looked like an operator action (kanban t_9c1362f6). The
    # caller now declares itself in `X-Termini-Actor`; a caller that does not is
    # recorded as `unattributed_project_token` — the truth, not a convenient lie.
    # ------------------------------------------------------------------ #
    def test_clear_names_the_worker_that_did_it(self):
        st, inj = self._inject(scenario="oom", duration_s=120)
        self.assertEqual(st, 201, inj)
        st, body = self._clear(inj["injection_id"], actor="t_c37aa0dc")
        self.assertEqual(st, 200, body)
        self.assertEqual(body["cleared_by"], "t_c37aa0dc")
        logs = self._rows("SELECT message, actor FROM operator_logs "
                          "WHERE message LIKE '%fault cleared by%'")
        self.assertTrue(
            [l for l in logs if l["actor"] == "t_c37aa0dc" and "t_c37aa0dc" in l["message"]],
            f"the audit line must name the caller: {logs}")

    def test_clear_without_an_actor_header_is_recorded_as_unattributed(self):
        st, inj = self._inject(scenario="disk-fail", duration_s=120)
        self.assertEqual(st, 201, inj)
        st, body = self._clear(inj["injection_id"], actor=None)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["cleared_by"], demo_faults.UNATTRIBUTED_ACTOR)
        self.assertNotEqual(body["cleared_by"], "human",
                            "an un-attributed caller must never read as the operator")
        logs = self._rows("SELECT message FROM operator_logs "
                          "WHERE message LIKE '%fault cleared by%'")
        self.assertTrue([l for l in logs if "un-attributed project-token caller" in l["message"]],
                        f"no audit line explains the missing actor: {logs}")

    def test_clear_all_attributes_every_injection_it_closed(self):
        a = self._inject(scenario="oom", duration_s=120)[1]
        b = self._inject(scenario="disk-fail", duration_s=120)[1]
        st, body = http(self.base, "POST", f"/v1/projects/{PROJECT_ID}/demo/faults/clear",
                        {"reason": "peer_collision_test"}, token=PROJECT_TOKEN,
                        headers=_actor_header("t_77f26781"))
        self.assertEqual(st, 200, body)
        by_id = {f["injection_id"]: f for f in body["cleared"]}
        for injection in (a, b):
            self.assertIn(injection["injection_id"], by_id)
            self.assertEqual(by_id[injection["injection_id"]]["cleared_by"], "t_77f26781")
        rows = self._rows("SELECT injection_id, cleared_by, clear_reason FROM demo_faults "
                          "WHERE injection_id IN (?, ?)",
                          (a["injection_id"], b["injection_id"]))
        self.assertEqual({r["cleared_by"] for r in rows}, {"t_77f26781"})
        self.assertEqual({r["clear_reason"] for r in rows}, {"peer_collision_test"})

    def test_the_drivers_own_lines_name_the_worker_not_the_operator(self):
        """Supersede + recovery-complete belong to the worker, not to `human`.

        These two lines are emitted by the driver, not by a caller, and they used
        to default to `human`: a fault a worker injected and the controller merely
        advanced left "the operator" in the audit trail — the same false attribution
        the clear itself was fixed for (kanban t_9c1362f6).
        """
        st, inj = self._inject(actor="t_9c1362f6", scenario="oom", duration_s=120)
        self.assertEqual(st, 201, inj)
        # The supersede line is emitted on the node's own next report, not on a tick.
        self._telemetry(self.node, 9000)
        st, body = self._clear(inj["injection_id"], actor="t_9c1362f6")
        self.assertEqual(st, 200, body)
        time.sleep(body["clear_samples_needed"] + 2)
        blamed = self._rows(
            "SELECT message, actor FROM operator_logs WHERE actor = 'human' "
            "AND message LIKE ? "
            "AND (message LIKE '%superseded by active fault injection%' "
            "     OR message LIKE '%back below threshold%')",
            (f"%{inj['injection_id']}%",))
        self.assertEqual(blamed, [], f"driver lines blamed the operator: {blamed}")
        named = self._rows(
            "SELECT message, actor FROM operator_logs WHERE actor = 't_9c1362f6' "
            "AND message LIKE '%back below threshold%' AND message LIKE ?",
            (f"%{inj['injection_id']}%",))
        self.assertTrue(named, "the recovery-complete line must name the worker")
        superseded = self._rows(
            "SELECT message, actor FROM operator_logs WHERE actor = 't_9c1362f6' "
            "AND message LIKE '%superseded by active fault injection%' AND message LIKE ?",
            (f"%{inj['injection_id']}%",))
        self.assertTrue(superseded, "the supersede line must name the worker")

    def test_injection_is_attributed_to_its_caller(self):
        st, inj = self._inject(actor="t_9c1362f6", scenario="oom", duration_s=120)
        self.assertEqual(st, 201, inj)
        logs = self._rows("SELECT message, actor FROM operator_logs "
                          "WHERE message LIKE '%fault injected by%'")
        self.assertTrue([l for l in logs
                         if l["actor"] == "t_9c1362f6" and "fault injected by t_9c1362f6" in l["message"]],
                        f"the injection line must name the caller: {logs}")

    def test_a_malformed_actor_header_is_refused_not_defaulted(self):
        st, inj = self._inject(scenario="oom", duration_s=120)
        self.assertEqual(st, 201, inj)
        st, body = http(self.base, "DELETE",
                        f"/v1/projects/{PROJECT_ID}/demo/faults/{inj['injection_id']}",
                        token=PROJECT_TOKEN,
                        headers={"X-Termini-Actor": "not a valid actor!"})
        self.assertEqual(st, 422, body)
        self.assertEqual(body["error"]["code"], "INVALID_ARGUMENT")
        row = self._rows("SELECT state, cleared_by FROM demo_faults WHERE injection_id = ?",
                         (inj["injection_id"],))[0]
        self.assertEqual(row["state"], "active", "a refused clear must not clear anything")
        self.assertIsNone(row["cleared_by"])


class OperatorLogActorMigrationTest(unittest.TestCase):
    """Migration 0022: an operator-log line can name the card that acted.

    The actor enum ('ai_operator'/'human'/'assistant') is what made a peer worker's
    clear-all indistinguishable from the operator's; the CHECK is now a shape guard,
    so the explicit id from `X-Termini-Actor` can be stored — while every existing
    row keeps its original actor (kanban t_9c1362f6).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="operator-log-actor-")
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(pathlib.Path(self.tmp.name) / "controller.db")

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def test_existing_rows_survive_and_actor_ids_are_accepted(self):
        migrations = sorted((CONTROLLER / "app" / "migrations").glob("*.sql"))
        pre = [p for p in migrations if int(p.stem.split("_", 1)[0]) <= 21]
        self.assertTrue(pre, "expected the pre-0022 migrations to exist")

        conn = self.connect()
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
                     " name TEXT NOT NULL, applied_at TEXT NOT NULL)")
        for path in pre:
            conn.executescript(path.read_text())
            conn.execute("INSERT INTO schema_migrations (version, name, applied_at)"
                         " VALUES (?, ?, ?)",
                         (int(path.stem.split("_", 1)[0]), path.stem, "2026-10-17T00:00:00Z"))
        # A pre-fix line, written when the only actors were the enum values.
        conn.execute(
            "INSERT INTO operator_logs (log_id, project_id, ts, node_id, severity, actor,"
            " message) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ulid(1), PROJECT_ID, "2026-10-17T19:49:32.607Z", None, "info", "human",
             "fault cleared by operator: pre-migration line"),
        )
        conn.execute(
            "INSERT INTO operator_logs (log_id, project_id, ts, node_id, severity, actor,"
            " message) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ulid(2), PROJECT_ID, "2026-10-17T19:49:33.000Z", None, "warn", "ai_operator",
             "oom-guard-01 fired"),
        )
        conn.commit()
        # Before the migration the new id is refused by the old CHECK — this is the
        # failure that made the audit line vanish (caught and logged, not stored).
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO operator_logs (log_id, project_id, ts, node_id, severity,"
                " actor, message) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ulid(3), PROJECT_ID, "2026-10-17T19:49:34.000Z", None, "info",
                 "t_c37aa0dc", "fault cleared by t_c37aa0dc"))
            conn.commit()
        conn.close()

        applied = appdb.migrate(self.db_path)
        self.assertIn("0022_operator_log_actor_ids", applied)

        conn = self.connect()
        rows = conn.execute("SELECT actor, message FROM operator_logs ORDER BY log_id"
                            ).fetchall()
        self.assertEqual([r["actor"] for r in rows], ["human", "ai_operator"],
                         "existing attribution must be preserved verbatim")
        conn.execute(
            "INSERT INTO operator_logs (log_id, project_id, ts, node_id, severity, actor,"
            " message) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ulid(4), PROJECT_ID, "2026-10-17T19:49:34.000Z", None, "info",
             "t_c37aa0dc", "fault cleared by t_c37aa0dc"))
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM operator_logs").fetchone()[0], 3)
        # …and the enum values the AI-operator loop uses still work.
        conn.execute(
            "INSERT INTO operator_logs (log_id, project_id, ts, node_id, severity, actor,"
            " message) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ulid(5), PROJECT_ID, "2026-10-17T19:49:35.000Z", None, "info",
             "assistant", "assistant line"))
        conn.commit()
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class RestartSweepTest(unittest.TestCase):
    """An injection no process can drive must not be reported as live (§4.7)."""

    def test_injection_left_open_by_a_dead_process_is_swept_at_boot(self):
        import os
        state = pathlib.Path(tempfile.mkdtemp(prefix="demo-faults-sweep-"))
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        db_path = str(state / "controller.db")
        # Who the deploy tool would have recorded for this tree (kanban t_9c1362f6):
        # the sweep must name them, not "the operator".
        deploy_actor = state / "DEPLOY_ACTOR"
        deploy_actor.write_text(json.dumps({
            "schema_version": "1.0", "actor": "t_77f26781", "ref": "wt/peer-branch",
            "commit": "56843b6deadbeef0123456789abcdef01234567",
            "deployed_at": "2026-10-17T19:30:45Z", "window": "checked"}))
        env = dict(os.environ, TERMINI_HOST="127.0.0.1", TERMINI_PORT=str(port),
                   TERMINI_DB_PATH=db_path, TERMINI_PROJECT_ID=PROJECT_ID,
                   TERMINI_PROJECT_TOKEN=PROJECT_TOKEN,
                   TERMINI_BOOTSTRAP_TOKEN="t-boot-1",
                   TERMINI_DEPLOY_ACTOR_PATH=str(deploy_actor),
                   TERMINI_OPERATOR_ENABLED="false")
        log = open(state / "controller.log", "w")

        def start():
            proc = subprocess.Popen([sys.executable, "-m", "app.main"],
                                    cwd=str(CONTROLLER), env=env,
                                    stdout=log, stderr=subprocess.STDOUT)
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    http(base, "GET", "/v1/telemetry/recent", timeout=2)
                    return proc
                except Exception:  # noqa: BLE001
                    time.sleep(0.2)
            raise AssertionError("controller did not start")

        proc = start()
        try:
            st, reg = http(base, "POST", "/v1/nodes/register", {
                "schema_version": "1.0",
                "machine": {"machine_id": "sweep-node", "hostname": "sweep-node",
                            "cpu": {"cores": 4, "threads": 8},
                            "memory": {"total_bytes": 8 * GiB},
                            "disks": [{"device": "/dev/sda", "size_bytes": 100 * GiB}]},
            }, token="t-boot-1")
            self.assertIn(st, (200, 201), reg)
            st, inj = http(base, "POST", f"/v1/projects/{PROJECT_ID}/demo/faults",
                           {"node_id": reg["node_id"], "scenario": "oom", "duration_s": 120,
                            "target": "real"}, token=PROJECT_TOKEN)
            self.assertEqual(st, 201, inj)
        finally:
            proc.terminate()
            proc.wait(timeout=10)

        proc2 = start()
        try:
            st, listing = http(base, "GET", f"/v1/projects/{PROJECT_ID}/demo/faults",
                               token=PROJECT_TOKEN)
            row = next(f for f in listing["faults"]
                       if f["injection_id"] == inj["injection_id"])
            self.assertEqual(row["state"], "expired", row)
            self.assertEqual(row["cleared_by"], "controller_restart")
            self.assertFalse(row["open"])
            # …and the audit trail names the deploy that caused the restart, with its
            # commit — not "the operator" (acceptance (b), kanban t_9c1362f6).
            self.assertIn("t_77f26781", row["clear_reason"], row["clear_reason"])
            self.assertIn("56843b6deadb", row["clear_reason"], row["clear_reason"])
            conn = sqlite3.connect(db_path)
            try:
                logs = [r[0] for r in conn.execute(
                    "SELECT message FROM operator_logs WHERE message LIKE '%expired%'")]
                actors = [r[0] for r in conn.execute(
                    "SELECT actor FROM operator_logs WHERE message LIKE '%expired%'")]
            finally:
                conn.close()
            self.assertTrue([m for m in logs
                             if "controller restarted by t_77f26781" in m
                             and "2026-10-17T19:30:45Z" in m], logs)
            self.assertEqual(actors, ["t_77f26781"])
        finally:
            proc2.terminate()
            proc2.wait(timeout=10)
            log.close()


# --------------------------------------------------------------------------- #
# the console page itself
# --------------------------------------------------------------------------- #
class FaultConsolePageTest(unittest.TestCase):
    """`console/serve.py` serves the console without ever handing out the token."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(REPO / "console"))
        import serve  # noqa: E402  (console/serve.py)

        cls.serve = serve
        cls.cfg = serve.ConsoleConfig()
        cls.cfg.controller = "http://127.0.0.1:8085"
        cls.cfg.project = PROJECT_ID
        cls.cfg.token = "SUPER-SECRET-PROJECT-TOKEN"
        cls.cfg.key = "~/.ssh/termini_operator"

    def test_index_gets_the_url_and_project_but_never_the_token(self):
        base = (REPO / "console" / "fault_console" / "index.html").read_bytes()
        out = self.cfg.fault_index_html(base).decode()
        self.assertIn("http://127.0.0.1:8085", out)
        self.assertIn(PROJECT_ID, out)
        self.assertNotIn("SUPER-SECRET-PROJECT-TOKEN", out)
        self.assertIn("__TERMINI_FAULT_DEFAULTS__", out)

    def test_missing_anchor_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            self.cfg.fault_index_html(b"<html><body>no script here</body></html>")

    def test_console_js_calls_the_contracted_routes(self):
        js = (REPO / "console" / "fault_console" / "js" / "app.js").read_text()
        for route in ('"/demo/faults"', "`/demo/faults/${id}`", '"/demo/faults/clear"'):
            self.assertIn(route, js)

    def test_dashboard_nav_does_not_link_the_console(self):
        """The guardrail: the client-facing surface must not advertise it."""
        for path in (REPO / "dashboard" / "index.html").rglob("*"):
            if path.is_file() and path.suffix in (".html", ".js", ".mjs"):
                self.assertNotIn("/console/", path.read_text(errors="replace"),
                                 f"{path} links the operator console")


if __name__ == "__main__":
    unittest.main()
