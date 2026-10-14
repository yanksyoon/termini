"""Defect A regression: the seeder-driven operator must not starve `POST /intents`.

The failure this pins (measured live on the standing fleet, 2026-10-14):
`console/seed_demo.py` drives faults, the AI operator therefore issues a model
decision every time one latches, and on node-c that decision is a ~9,000-token
prompt that takes ~400 s to prefill on CPU. node-c's llama.cpp was started
`--parallel 1`, i.e. a ONE-slot queue, so a user's intent extraction that arrived
while a decision was in flight was held open behind it and never answered — the
client saw `HTTP 000` after 200 s and no access-log line was ever written (the
handler was still blocked inside the model call). Telemetry kept returning 202
throughout, so this was never SQLite contention or a saturated thread pool.

Two properties are tested, because either alone is not a fix:

1. **The shipped model server must serve more than one slot** with the full
   16,384-token context per slot. This is the condition that removes the mutual
   exclusion, and it is what the live fix changed
   (`infra/gcp/cloud-init/llama-serve.sh`). The test fails on the old
   `--parallel 1` flags — it is the regression that would have caught Defect A.
2. **The controller must always answer with a status code**, even when the model
   is saturated: the extraction bound is the intent path's own
   (`TERMINI_INTENT_MODEL_TIMEOUT_S`), not the operator's minutes-long
   `TERMINI_INFERENCE_TIMEOUT_S`, so a queued extraction degrades to an explicit
   retryable `503` instead of hanging past any client's patience.
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(REPO))

from app import config, db, events, inventory, logs, server as server_mod, sshca, store, terminal  # noqa: E402

PROJECT_ID = "01J0DEM0WARM000000000000AB"
PROJECT_TOKEN = "test-project-token"

# `termini-node-a`'s real registered hardware facts (read from the deployed
# controller: `cpu.cores/threads 2`, `memory.total_bytes 8319729664`,
# 61,285,326,848-byte boot disk ⇒ 2 vCPU / 7 GiB / 57 GiB, i.e. 4 vCPU of
# overcommit and 5 GiB allocatable RAM after the 2 GiB OS reserve).
NODE_A_HW = {
    "cpu": {"model": "Intel(R) Xeon(R) CPU @ 2.80GHz", "cores": 2, "threads": 2},
    "memory": {"total_bytes": 8319729664},
    "gpus": [],
    "disks": [{"size_bytes": 61285326848}],
}

# What the fixed extraction prompt's few-shot shows the model, i.e. what a plain
# "deploy 2 worker agents" resolves to.
DEFAULT_SPEC = {
    "type": "agent_vm", "count": 2, "vcpus": 1, "ram_gb": 1, "disk_gb": 8,
    "passthrough_gpu": False, "target_node": "auto",
}


def _load_cfg(db_path: str, intent_timeout_s: float, extra_env: dict | None = None):
    env = {
        "TERMINI_BOOTSTRAP_TOKEN": "boot-a,boot-c",
        "TERMINI_PROJECT_TOKEN": PROJECT_TOKEN,
        "TERMINI_PROJECT_ID": PROJECT_ID,
        "TERMINI_DB_PATH": db_path,
        "TERMINI_MAX_INFERENCE_NODES": "1",
        "TERMINI_LXD_BASE_URL": "http://127.0.0.1:1",
        "TERMINI_HOST": "127.0.0.1",
        "TERMINI_PORT": "0",
        "TERMINI_INTENT_MODEL_TIMEOUT_S": str(intent_timeout_s),
    }
    env.update(extra_env or {})
    with mock.patch.dict(os.environ, env, clear=False):
        return config.Config.load()


# --------------------------------------------------------------------------- stub model


class _StubModelState:
    def __init__(self, slots: int, hold_s: float, content: str):
        self.slots = threading.BoundedSemaphore(slots)
        self.hold_s = hold_s
        self.content = content
        self.inflight = 0
        self.max_concurrent = 0
        self.lock = threading.Lock()
        self.busy = threading.Event()


class _StubModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "stub-model/1"

    def log_message(self, *args):  # noqa: A003 - silence
        pass

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        st: _StubModelState = self.server.state  # type: ignore[attr-defined]
        # Model llama.cpp's slot semantics: a request with no free slot is HELD
        # OPEN with no response until one frees (it is not refused, which is why
        # the client saw a hang and not an error).
        st.slots.acquire()
        try:
            with st.lock:
                st.inflight += 1
                st.max_concurrent = max(st.max_concurrent, st.inflight)
            st.busy.set()
            if st.hold_s:
                time.sleep(st.hold_s)
            body = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": st.content}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            with st.lock:
                st.inflight -= 1
            st.slots.release()


class _StubModel:
    def __init__(self, slots=1, hold_s=0.0, content=None):
        self.state = _StubModelState(
            slots, hold_s,
            content or json.dumps({"allocations": [DEFAULT_SPEC]}),
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubModelHandler)
        self.httpd.daemon_threads = True
        self.httpd.state = self.state
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def occupy_a_slot(self, hold_s: float):
        """Send one request that holds a slot, and return once it is in flight.

        Stands in for the operator's ~400 s decision: with one slot it blocks
        every other caller; with two it leaves one free.
        """
        body = json.dumps({"model": "claude", "messages": [{"role": "user", "content": "decide"}]}).encode()
        req = urllib.request.Request(
            self.base_url + "/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        t = threading.Thread(target=self._post_ignore, args=(req,), daemon=True)
        t.start()
        assert self.state.busy.wait(timeout=5), "stub slot never became busy"
        return t

    @staticmethod
    def _post_ignore(req):
        try:
            with urllib.request.urlopen(req, timeout=30):
                pass
        except Exception:  # noqa: BLE001 - background filler
            pass


# --------------------------------------------------------------------------- serve-flag pin


class ShippedModelServerSlotsTest(unittest.TestCase):
    """Node-c must not serve the operator and the user from one model instance."""

    def setUp(self):
        self.script = (REPO / "infra/gcp/cloud-init/llama-serve.sh").read_text()
        # Parse the ACTUAL service invocations, not the prose around them (the
        # comments deliberately discuss the old `--parallel 1` flags).
        self.exec_lines = [ln for ln in self.script.splitlines() if ln.startswith("ExecStart=")]
        self.assertEqual(len(self.exec_lines), 2,
                         "expected the decision instance and the intent instance")
        self.decision_line = self.exec_lines[0]
        self.intent_line = self.exec_lines[1]

    def test_the_decision_instance_has_more_than_one_slot(self):
        m = re.search(r"--parallel\s+(\d+)", self.decision_line)
        self.assertIsNotNone(m, "llama-serve.sh no longer sets --parallel")
        self.assertGreaterEqual(
            int(m.group(1)), 2,
            "node-c must serve >=2 slots: with --parallel 1 an operator decision "
            "monopolises the only slot and a POST /intents extraction is never "
            "answered (Defect A)",
        )

    def test_every_slot_keeps_the_full_16k_context(self):
        parallel = int(re.search(r"--parallel\s+(\d+)", self.decision_line).group(1))
        ctx = int(re.search(r"--ctx-size\s+(\d+)", self.decision_line).group(1))
        self.assertGreaterEqual(
            ctx // parallel, 16384,
            "llama.cpp divides --ctx-size across --parallel slots; the 20-sample "
            "operator prompt is ~9k tokens and must fit one slot",
        )

    def test_a_dedicated_extraction_instance_is_served_on_its_own_port(self):
        """A second slot is NOT enough — the extraction needs its own PROCESS.

        llama.cpp fills its batch from the operator's ~12k-token prompt first, so
        a small concurrent request on another slot of the SAME server gets no
        compute until that prefill ends (measured: the extraction hit its 120 s
        bound while the decision prefilled). The shipped cloud-init therefore
        serves a separate small-context instance for `POST /intents`.
        """
        self.assertIn("termini-llama-intent.service", self.script)
        m_port = re.search(r"--port\s+(\S+)", self.intent_line)
        self.assertIsNotNone(m_port, "the intent instance must pin its own port")
        self.assertNotEqual(m_port.group(1),
                            re.search(r"--port\s+(\S+)", self.decision_line).group(1))
        # The dedicated port is a concrete default (not the templated decision
        # port) and is not the frozen inference port.
        default_port = re.search(r'INTENT_PORT="\$\{INTENT_PORT:-(\d+)\}"', self.script)
        self.assertIsNotNone(default_port, "INTENT_PORT must have a concrete default")
        self.assertNotEqual(default_port.group(1), "8080")
        self.assertRegex(self.script, r"systemctl enable --now termini-llama-intent\.service")
        # Small context: it only ever holds the extraction prompt.
        ctx = int(re.search(r"--ctx-size\s+(\d+)", self.intent_line).group(1))
        self.assertLessEqual(ctx, 8192)

    def test_the_two_instances_do_not_oversubscribe_the_node(self):
        threads = [int(re.search(r"--threads\s+(\d+)", ln).group(1))
                   for ln in self.exec_lines]
        self.assertLessEqual(sum(threads), 10,
                             "the node has 8 vCPUs; the intent instance must keep "
                             "a real thread budget without starving the decision")


class IntentModelConfigTest(unittest.TestCase):
    """The intent path's endpoint/timeout are its own, not the operator's."""

    def test_dedicated_endpoint_is_used_when_set(self):
        cfg = _load_cfg(tempfile.mktemp(suffix=".db"), 1.0,
                        extra_env={"TERMINI_INTENT_INFERENCE_BASE_URL": "http://10.0.0.9:8090"})
        self.assertEqual(cfg.intent_inference_base_url, "http://10.0.0.9:8090")
        built = server_mod._load_intent_model_config(120.0, cfg.intent_inference_base_url)
        self.assertIsNotNone(built)
        self.assertEqual(built.base_url, "http://10.0.0.9:8090")
        self.assertEqual(built.timeout_s, 120.0)

    def test_endpoint_defaults_to_unset_and_falls_back(self):
        cfg = _load_cfg(tempfile.mktemp(suffix=".db"), 1.0)
        self.assertEqual(cfg.intent_inference_base_url, "")
        with mock.patch.dict(os.environ, {
            "TERMINI_INFERENCE_BASE_URL": "http://10.0.0.7:8080",
            "TERMINI_INFERENCE_TIMEOUT_S": "900",
        }):
            built = server_mod._load_intent_model_config(120.0, cfg.intent_inference_base_url or None)
        self.assertIsNotNone(built)
        # Falls back to the shared endpoint, but NEVER to the operator's timeout.
        self.assertEqual(built.base_url, "http://10.0.0.7:8080")
        self.assertEqual(built.timeout_s, 120.0)


# --------------------------------------------------------------------------- http harness


class _IntentHttpBase(unittest.TestCase):
    """Boots the real ControllerServer + a stub model, and drives it over HTTP."""

    INTENT_TIMEOUT_S = 1.0

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-intent-load-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "controller.db")
        db.migrate(self.db_path)
        self.cfg = _load_cfg(self.db_path, self.INTENT_TIMEOUT_S)
        with db.session(self.db_path) as conn:
            store.seed_bootstrap_tokens(conn, list(self.cfg.bootstrap_tokens), store.now_iso())
            self.project, _ = store.ensure_project(conn, self.cfg, store.now_iso())
        self.node_id = self._add_node()

        self.broker = events.EventBroker()
        self.logs = logs.OperatorLogs(self.broker, ring_size=100)
        self.srv = server_mod.ControllerServer(
            ("127.0.0.1", 0), self.cfg, self.project, self.broker, self.logs,
            inventory.Inventory.load(None, "ubuntu", 22), terminal.TerminalManager(),
            sshca.SSHCA(None),
        )
        self.port = self.srv.server_address[1]
        self._thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _add_node(self):
        with db.session(self.db_path) as conn:
            reg = {
                "registration_id": "node-a", "machine_id": "node-a", "hostname": "node-a",
                "role_hint": None, "installer_version": None,
                "hardware_json": NODE_A_HW, "os": None, "lxd": None,
                "lxd_endpoint": None,
            }
            node_id, _ = store.upsert_node(
                conn, PROJECT_ID, reg, "compute", store.sha256("node-a"), None, store.now_iso())
        return node_id

    def _post_intent(self, prompt: str, timeout_s: float = 60.0):
        body = json.dumps({"prompt": prompt}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/projects/{PROJECT_ID}/intents",
            data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + PROJECT_TOKEN},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
                return e.code, json.loads(raw or b"{}")
            finally:
                e.close()


class IntentUnderModelLoadTest(_IntentHttpBase):
    INTENT_TIMEOUT_S = 1.0

    def setUp(self):
        super().setUp()
        # `TERMINI_INFERENCE_TIMEOUT_S` stays the operator's minutes-long value;
        # the intent path must ignore it (that is the whole point).
        self._env = mock.patch.dict(os.environ, {
            "TERMINI_INFERENCE_TIMEOUT_S": "900",
            "TERMINI_INFERENCE_BASE_URL": "http://127.0.0.1:1",  # replaced per test
        })
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_a_saturated_model_cannot_hang_the_intent_endpoint(self):
        """One slot, held by a decision: the intent still gets a real status.

        On the pre-fix code the extraction inherited the 900 s inference timeout
        and this request produced `HTTP 000 after 200 s`. Now it is bounded by
        `TERMINI_INTENT_MODEL_TIMEOUT_S` and answers `503` with an
        `extraction_failed` detail — the caller can retry.
        """
        stub = _StubModel(slots=1, hold_s=8.0)
        self.addCleanup(stub.stop)
        os.environ["TERMINI_INFERENCE_BASE_URL"] = stub.base_url
        stub.occupy_a_slot(8.0)

        t0 = time.monotonic()
        status, body = self._post_intent("deploy 2 worker agents", timeout_s=20.0)
        elapsed = time.monotonic() - t0

        self.assertEqual(status, 503, body)
        self.assertLess(elapsed, 6.0, "the request must be bounded, not hang")
        self.assertEqual(body["error"]["code"], "INTERNAL")
        self.assertTrue(
            any("extraction_failed" in json.dumps(d) for d in body["error"]["details"]),
            body,
        )
        # It is the INTENT bound that fired, not the 900 s inference timeout.
        self.assertLess(elapsed, 900)

    def test_two_slots_serve_the_intent_while_a_decision_is_in_flight(self):
        """The live fix, end to end: the NL deploy is SERVED, not queued.

        A two-slot model (what `llama-serve.sh` now ships) with one slot occupied
        by an operator-style decision, plus a warm slot for the user. The plain
        natural-language deploy must come back with a real `200` accepted plan
        resolving to the node-a-shaped node — no hang, no 422.
        """
        stub = _StubModel(slots=2, hold_s=0.5)
        self.addCleanup(stub.stop)
        os.environ["TERMINI_INFERENCE_BASE_URL"] = stub.base_url
        stub.occupy_a_slot(3.0)  # an operator decision holding ONE slot

        t0 = time.monotonic()
        status, body = self._post_intent("deploy 2 worker agents", timeout_s=30.0)
        elapsed = time.monotonic() - t0

        self.assertEqual(status, 200, body)
        self.assertLess(elapsed, 20.0)
        self.assertEqual(body["outcome"], "accepted")
        alloc = body["allocations"][0]
        self.assertEqual(alloc["node_id"], self.node_id)
        self.assertEqual(alloc["status"], "accepted")
        # The default sizing fits node-a: 2 agents x (1 vCPU / 1 GiB).
        self.assertGreaterEqual(alloc["resolved"]["ram_gb"], 1)
        self.assertGreaterEqual(alloc["resolved"]["vcpus"], 1)

    def test_concurrent_intents_are_both_served_on_a_two_slot_model(self):
        stub = _StubModel(slots=2, hold_s=0.4)
        self.addCleanup(stub.stop)
        os.environ["TERMINI_INFERENCE_BASE_URL"] = stub.base_url

        results: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def fire(prompt):
            r = self._post_intent(prompt, timeout_s=30.0)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=fire, args=(f"deploy {n} worker agents",))
                   for n in (2, 3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=40)

        self.assertEqual(len(results), 2, results)
        for status, body in results:
            self.assertEqual(status, 200, body)
        # Both really executed at the model at the same time (2 slots in use).
        self.assertEqual(stub.state.max_concurrent, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
