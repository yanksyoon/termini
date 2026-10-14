#!/usr/bin/env python3
"""Agents must land on the COMPUTE node, and remediation must follow them.

MEASURED DEFECT (live, standing fleet). Two nodes registered as role `compute`:

    termini-node-a  2 vCPU /  8 GiB   LXD https://10.10.0.11:8443   ← the compute node
    termini-node-c  8 vCPU / 32 GiB   LXD https://10.10.0.13:8443   ← runs llama.cpp

Both are `compute` because the role rule is GPU-gated (`node-bootstrap.md` §5.3 /
`controller-api.md` §4.1): node C has no GPU and serves the model on CPU, so nothing
in the role vocabulary says "this machine is the model host". Consequences:

  * `auto` placement picks the node with the most free allocatable RAM, so every
    accepted plan materialised its instances on **node-c** — the machine that is
    busy serving the decision model for the whole fleet — while the user wanted
    their agents on node-a ("i can't seem to deploy agents on node a");
  * the operator loop remediated with ONE global executor bound to
    `TERMINI_LXD_BASE_URL` (node-a), so a fault reported by node-c was remediated
    against **node-a**: materialisation and remediation touched different machines.

The fix has two halves, both pinned here:

  1. PLACEMENT — `auto` for `agent_vm` never resolves to the node that serves the
     model. That node is identified from the model address the controller already
     knows (`TERMINI_INFERENCE_BASE_URL`) plus any operator-named hosts
     (`TERMINI_AGENT_PLACEMENT_EXCLUDE_HOSTS`). The exclusion is dropped when it
     would leave no candidate, so a single-node demo stays placeable. An EXPLICIT
     `target_node` is still honoured (naming a node is not `auto`).
  2. REMEDIATION — the operator loop resolves the executor PER NODE
     (`app/operator/target.executor_for_node`), exactly as materialisation does, so
     a remediation reaches the node that actually holds the instance.

The frozen ROLE rule is deliberately NOT changed: making node C register
`inference` is a contract decision the user has not made, so it is reported as a
recommendation instead (see the PR body). Everything here works with node C still
registering `compute`.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import types
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import db, server as server_mod, store  # noqa: E402
from app.operator import target as target_mod  # noqa: E402
from app.operator.loop import OperatorLoop  # noqa: E402
from termini.intent import DEFAULT_HOST_CAPS, clamp_validate  # noqa: E402

GiB = 1024 ** 3
PROJECT_ID = "01J0DEM0WARM000000000000AC"

# The live fleet's addresses (underlay IPs of node-a / node-c).
COMPUTE_EP = "10.10.0.11:8443"
MODEL_EP = "10.10.0.13:8443"
MODEL_URL = "http://10.10.0.13:8080"

NODE_A = "01M4TGD31WXH97JAA5S6A4R10D"
NODE_C = "01M4TGEEZEHJEQB01T5PBBE97M"


def hw(*, cores: int, ram_gb: int, disk_gb: int = 256) -> dict:
    return {
        "cpu": {"cores": cores, "threads": cores},
        "memory": {"total_bytes": ram_gb * GiB},
        "gpus": [],
        "disks": [{"size_bytes": disk_gb * GiB}],
    }


def node(nid: str, *, cores: int, ram_gb: int, endpoint: str | None,
         role: str = "compute", synthetic: bool = False) -> dict:
    return {
        "id": nid, "role": role, "synthetic": synthetic,
        "lxd_endpoint": endpoint, "source_ip": (endpoint or "").split(":")[0] or None,
        "hardware_json": json.dumps(hw(cores=cores, ram_gb=ram_gb)),
    }


def spec(*, target_node: str = "auto", type_: str = "agent_vm"):
    return types.SimpleNamespace(
        type=type_, target_node=target_node, passthrough_gpu=False)


def cfg(**over):
    base = dict(inference_base_url=MODEL_URL, agent_placement_exclude_hosts=())
    base.update(over)
    return types.SimpleNamespace(**base)


class _Resolver:
    """Stand-in `self` carrying a controller config, like the real handler."""

    def __init__(self, configuration=None):
        self.server = types.SimpleNamespace(cfg=configuration or cfg())

    def resolve(self, s, nodes):
        return server_mod.ControllerHandler._resolve_target(self, s, nodes)


# ------------------------------------------------------------------ 1. host extraction

class HostExtractionTest(unittest.TestCase):
    def test_bare_host_and_scheme_forms(self):
        from app.server import _endpoint_host
        self.assertEqual(_endpoint_host("10.10.0.13:8443"), "10.10.0.13")
        self.assertEqual(_endpoint_host("https://10.10.0.13:8443"), "10.10.0.13")
        self.assertEqual(_endpoint_host("http://10.10.0.13:8080"), "10.10.0.13")
        self.assertEqual(_endpoint_host("10.10.0.13"), "10.10.0.13")
        self.assertEqual(_endpoint_host("HTTPS://10.10.0.13:8443"), "10.10.0.13")

    def test_nonsense_reads_as_no_host(self):
        from app.server import _endpoint_host
        for junk in (None, "", "   ", 5, {}, []):
            self.assertEqual(_endpoint_host(junk), "")
        self.assertEqual(_endpoint_host("[fd42::1]:8443"), "fd42::1")

    def test_node_address_prefers_the_endpoint_then_the_source_ip(self):
        from app.server import node_address_host
        self.assertEqual(node_address_host(node(NODE_A, cores=2, ram_gb=8,
                                                endpoint=COMPUTE_EP)), "10.10.0.11")
        self.assertEqual(node_address_host(
            {"id": NODE_A, "lxd_endpoint": None, "source_ip": "10.10.0.13"}), "10.10.0.13")
        self.assertEqual(node_address_host({"id": NODE_A}), "")


# ------------------------------------------------------------ 2. the exclusion set

class ExcludedHostsTest(unittest.TestCase):
    def test_the_model_base_url_host_is_excluded(self):
        from app.server import agent_placement_excluded_hosts
        self.assertEqual(agent_placement_excluded_hosts(cfg()), {"10.10.0.13"})

    def test_operator_named_hosts_are_added(self):
        from app.server import agent_placement_excluded_hosts
        got = agent_placement_excluded_hosts(
            cfg(agent_placement_exclude_hosts=("10.10.0.99:8443", "https://10.10.0.98")))
        self.assertIn("10.10.0.99", got)
        self.assertIn("10.10.0.98", got)
        self.assertIn("10.10.0.13", got)

    def test_no_config_means_no_exclusion(self):
        from app.server import agent_placement_excluded_hosts, _placement_excluded_hosts
        self.assertEqual(agent_placement_excluded_hosts(None), set())
        # A bare test `self` (as the older pure-resolver tests use) must not blow up.
        self.assertEqual(_placement_excluded_hosts(None), set())
        self.assertEqual(_placement_excluded_hosts(types.SimpleNamespace()), set())


# ------------------------------------------------------------- 3. the placement rule

class AutoPlacementAvoidsTheModelHostTest(unittest.TestCase):
    def setUp(self):
        self.r = _Resolver()
        # The live shape: the model host has 4x the compute node's RAM, so a pure
        # "most free RAM" rule picks it every time.
        self.model = node(NODE_C, cores=8, ram_gb=32, endpoint=MODEL_EP)
        self.compute = node(NODE_A, cores=2, ram_gb=8, endpoint=COMPUTE_EP)

    def test_auto_lands_on_the_compute_node_despite_less_ram(self):
        for order in ([self.model, self.compute], [self.compute, self.model]):
            self.assertEqual(self.r.resolve(spec(), order), self.compute)

    def test_auto_still_prefers_more_ram_AMONG_compute_nodes(self):
        big = node("01M4AAAAAAAAAAAAAAAAAAAAAA", cores=8, ram_gb=32, endpoint="10.10.0.21:8443")
        small = node("01M4BBBBBBBBBBBBBBBBBBBBBB", cores=2, ram_gb=8, endpoint="10.10.0.22:8443")
        self.assertEqual(self.r.resolve(spec(), [small, big]), big)

    def test_an_explicit_target_to_the_model_host_is_still_honoured(self):
        # `auto` is what avoids the model host; naming it is a deliberate choice.
        self.assertEqual(self.r.resolve(spec(target_node=NODE_C),
                                        [self.model, self.compute]), self.model)

    def test_exclusion_is_dropped_when_it_would_leave_no_candidate(self):
        # Single-node demo: the only node IS the model host -> still placeable.
        self.assertEqual(self.r.resolve(spec(), [self.model]), self.model)

    def test_a_node_without_an_address_is_not_excluded(self):
        anon = node(NODE_A, cores=2, ram_gb=8, endpoint=None)
        self.assertEqual(self.r.resolve(spec(), [self.model, anon]), anon)

    def test_no_inference_url_configured_means_no_exclusion(self):
        r = _Resolver(cfg(inference_base_url=""))
        self.assertEqual(r.resolve(spec(), [self.compute, self.model]), self.model)

    def test_inference_vm_resolution_is_untouched(self):
        inf = node(NODE_C, cores=8, ram_gb=32, endpoint=MODEL_EP, role="inference")
        self.assertEqual(self.r.resolve(spec(type_="inference_vm"), [self.compute, inf]), inf)


# ---------------------------------------------------------------- 4. plan-level resolution

class _PlanHandler:
    """Drives `_build_plan` with a config-bearing handler."""

    def __init__(self, configuration):
        self.server = types.SimpleNamespace(cfg=configuration)

    def _resolve_target(self, s, nodes):
        return server_mod.ControllerHandler._resolve_target(self, s, nodes)


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        db.migrate(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO projects (id, name, project_token_hash, max_inference_nodes, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (PROJECT_ID, "test", "a" * 64, 1, store.now_iso()),
        )

    def _node(self, nid: str, *, cores: int, ram_gb: int, endpoint: str | None,
              role: str = "compute") -> str:
        reg = {
            "registration_id": nid, "machine_id": nid, "hostname": nid,
            "role_hint": None, "installer_version": None,
            "hardware_json": hw(cores=cores, ram_gb=ram_gb), "os": None, "lxd": None,
            "lxd_endpoint": endpoint, "synthetic": False,
        }
        node_id, _ = store.upsert_node(
            self.conn, PROJECT_ID, reg, role, store.sha256(nid), None, store.now_iso())
        self.conn.commit()
        return node_id

    def _build(self, components, configuration=None):
        result = clamp_validate(components, DEFAULT_HOST_CAPS)
        return server_mod.ControllerHandler._build_plan(
            _PlanHandler(configuration or cfg()), self.conn, PROJECT_ID, result, None)


class PlanLandsOnTheComputeNodeTest(_DbCase):
    def setUp(self):
        super().setUp()
        self.model_ep_id = self._node("model-host", cores=8, ram_gb=32, endpoint=MODEL_EP)
        self.compute_id = self._node("compute-node", cores=2, ram_gb=8, endpoint=COMPUTE_EP)

    def test_auto_allocation_commits_on_the_compute_node(self):
        plan = self._build([{"type": "agent_vm", "count": 2, "vcpus": 1,
                             "ram_gb": 1, "disk_gb": 4}])
        self.assertEqual(plan["outcome"], "accepted", plan)
        self.assertEqual(plan["allocations"][0]["node_id"], self.compute_id)
        self.assertEqual(store.allocated_capacity(self.conn, self.compute_id)["cpu_allocated"], 2)
        self.assertEqual(store.allocated_capacity(self.conn, self.model_ep_id)["cpu_allocated"], 0)

    def test_capacity_is_clamped_against_the_compute_node_not_the_model_host(self):
        # 16 GiB would fit the 30 GiB model host but not the 6 GiB compute node.
        plan = self._build([{"type": "agent_vm", "count": 1, "vcpus": 2,
                             "ram_gb": 16, "disk_gb": 8}])
        alloc = plan["allocations"][0]
        self.assertEqual(plan["outcome"], "accepted", plan)
        self.assertEqual(alloc["node_id"], self.compute_id)
        self.assertEqual(alloc["status"], "clamped")
        self.assertEqual(alloc["resolved"]["ram_gb"], 6)

    def test_explicit_target_to_the_model_host_is_still_honoured(self):
        plan = self._build([{"type": "agent_vm", "count": 1, "vcpus": 2, "ram_gb": 4,
                             "disk_gb": 8, "target_node": self.model_ep_id}])
        self.assertEqual(plan["allocations"][0]["node_id"], self.model_ep_id)


# -------------------------------------------------------- 5. remediation follows the node

class RemediationTargetsTheOwningNodeTest(_DbCase):
    """The operator loop must dial the node that reported the event."""

    def _loop_with_nodes(self, nodes):
        """A loop built the way PRODUCTION builds it: a credentialed executor is
        handed in, bound to the global `TERMINI_LXD_BASE_URL` (`app/main`)."""
        from app.operator.executor import LxdExecutor

        cfg_ns = types.SimpleNamespace(
            db_path=self.path,
            lxd_base_url="https://" + COMPUTE_EP,   # the OLD single global target
            lxd_node_scheme="https",
            lxd_request_timeout_s=7.0, lxd_retry_max=2, lxd_retry_backoff_s=0.25,
            lxd_client_cert=None, lxd_client_key=None, operator_dry_run=False,
        )
        loop = OperatorLoop.__new__(OperatorLoop)
        loop.cfg = cfg_ns
        loop._executors = {}
        loop.lxd = LxdExecutor(cfg_ns.lxd_base_url, timeout_s=7.0, retry_max=2,
                               backoff_s=0.25)
        ids = {}
        for nid, endpoint in nodes:
            ids[nid] = self._node(nid, cores=4, ram_gb=16, endpoint=endpoint)
        return loop, ids

    def test_a_node_c_event_dials_node_c_not_the_global_default(self):
        loop, ids = self._loop_with_nodes([("node-a", COMPUTE_EP), ("node-c", MODEL_EP)])
        ex_a = loop._executor_for(ids["node-a"])
        ex_c = loop._executor_for(ids["node-c"])
        self.assertEqual(ex_a.base_url, "https://10.10.0.11:8443")
        self.assertEqual(ex_c.base_url, "https://10.10.0.13:8443")
        self.assertNotEqual(ex_c.base_url, loop.lxd.base_url)

    def test_the_handed_in_executor_is_a_FALLBACK_not_an_override(self):
        """The live regression this pins.

        `app/main` hands the loop a credentialed executor bound to the global
        `TERMINI_LXD_BASE_URL`. Treating that as "the" executor for every node made
        the node-c fix a no-op: the live node-c `disk-degrade-01` audit recorded
        `lxd_endpoint=https://10.10.0.11:8443` (node-a) instead of node-c.
        """
        loop, ids = self._loop_with_nodes([("node-c", MODEL_EP)])
        self.assertEqual(loop._executor_for(ids["node-c"]).base_url,
                         "https://10.10.0.13:8443")
        # ... and the global executor is NOT mutated in the process.
        self.assertEqual(loop.lxd.base_url, "https://10.10.0.11:8443")

    def test_a_retargeted_executor_keeps_the_controllers_identity_and_settings(self):
        loop, ids = self._loop_with_nodes([("node-c", MODEL_EP)])
        ex = loop._executor_for(ids["node-c"])
        # Same TLS context object: the client identity/TLS policy cannot drift.
        self.assertIs(ex._ssl_ctx, loop.lxd._ssl_ctx)
        self.assertEqual(ex.timeout_s, loop.lxd.timeout_s)
        self.assertEqual(ex.retry_max, loop.lxd.retry_max)
        self.assertEqual(ex.backoff_s, loop.lxd.backoff_s)
        self.assertEqual(ex.dry_run, loop.lxd.dry_run)
        self.assertEqual(ex.client_cert, loop.lxd.client_cert)
        self.assertEqual(ex.client_key, loop.lxd.client_key)
        self.assertEqual(ex.server_ca, loop.lxd.server_ca)

    def test_a_bare_host_port_endpoint_gets_the_https_scheme(self):
        # node endpoints are stored as `host:port` on the live fleet.
        loop, ids = self._loop_with_nodes([("node-c", "10.10.0.13:8443")])
        self.assertEqual(loop._executor_for(ids["node-c"]).base_url,
                         "https://10.10.0.13:8443")

    def test_a_node_without_an_endpoint_falls_back_to_the_global_target(self):
        loop, ids = self._loop_with_nodes([("legacy", None)])
        self.assertIs(loop._executor_for(ids["legacy"]), loop.lxd)

    def test_an_unknown_node_falls_back_to_the_global_target(self):
        loop, _ids = self._loop_with_nodes([])
        self.assertIs(loop._executor_for("01M4TGD31WXH97JAA5S6A4R10D"), loop.lxd)

    def test_resolution_is_cached_per_node(self):
        loop, ids = self._loop_with_nodes([("node-c", MODEL_EP)])
        first = loop._executor_for(ids["node-c"])
        self.assertIs(loop._executor_for(ids["node-c"]), first)

    def test_target_module_builds_the_nodes_own_endpoint(self):
        ex = target_mod.executor_for_node(
            types.SimpleNamespace(lxd_base_url="https://10.10.0.11:8443",
                                  lxd_node_scheme="https", lxd_request_timeout_s=10.0,
                                  lxd_retry_max=1, lxd_retry_backoff_s=0.0,
                                  lxd_client_cert=None, lxd_client_key=None),
            {"id": NODE_C, "lxd_endpoint": MODEL_EP})
        self.assertEqual(ex.base_url, "https://10.10.0.13:8443")


if __name__ == "__main__":
    unittest.main(verbosity=2)
