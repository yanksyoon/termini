"""Unit tests for the INFERENCE SURFACE SPLIT (kanban `t_aa7a70f9`, D9).

The decision this locks down: the footprint has TWO inference surfaces with
different guarantees, and neither may stand in for the other.

  * **agent-facing** — Node C's `:8080`, `backend=local`, answered from that
    node's own weights. Client/agent traffic must not be forwarded, and the
    forwarded backend is no longer offered on that port;
  * **provider-only** — the CONTROLLER's `127.0.0.1:8085`, `backend=gemini-proxy`
    → `aiplatform.googleapis.com`. The operator's diagnosis runs there, it is
    reachable from the controller only, and it is the only listener in the
    footprint that is allowed to leave the premises.

What is asserted here, with no GCP and no live fleet:

  * the shipped Node C default is `local` (in the installer and in the rendered
    first-boot user-data), so a rebuilt node cannot hand agent traffic to a
    forwarded managed model;
  * the provider surface renders loopback-only, in `gemini-proxy` mode, with no
    key file anywhere (the metadata identity is the only credential);
  * the controller's drop-in sends the DIAGNOSIS to the loopback provider
    listener and still names Node C explicitly for agent placement — the split
    is what makes that explicit naming necessary;
  * the pre-split script REFUSES to run without `--allow-unsplit`;
  * `/health` reports the local path by MEASURING it, so the provider listener
    (which has no worker behind it) cannot claim a reachable local model;
  * flipping one surface leaves the other's `/health` byte-identical.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
INFERENCE_DIR = REPO_ROOT / "infra" / "inference"
PROXY_PATH = INFERENCE_DIR / "proxy.py"
SETUP_SURFACES = INFERENCE_DIR / "setup-inference-surfaces.sh"
PROVIDER_UNIT = INFERENCE_DIR / "termini-inference-provider.service"
NODE_C_INSTALLER = INFERENCE_DIR / "install-inference-proxy.sh"
LEGACY_SETUP = REPO_ROOT / "infra" / "cloudrun" / "setup-operator-node-c.sh"
NODE_C_RENDERER = REPO_ROOT / "infra" / "gcp" / "cloud-init" / "node-c-serve-block.sh"


def _load_proxy():
    spec = importlib.util.spec_from_file_location("termini_inference_proxy_surfaces", PROXY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


proxy = _load_proxy()

RENDER_ENV = {
    "LLAMACPP_COMMIT": "7fe450e19305b828c199d602c23a8337aaa1f03b",
    "MODEL_URL": "https://example.invalid/gemma.gguf",
    "MODEL_FILE": "gemma-3-4b-it-Q4_K_M.gguf",
    "MODEL_SHA256": "0" * 64,
    "MODEL_ALIAS": "gemma",
    "PORT": "8080",
}


# --------------------------------------------------------------------------- #
# A tiny fake worker (the "local" upstream) and the provider upstream
# --------------------------------------------------------------------------- #

class _Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        return

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._reply(200, {"status": "ok"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._reply(200, {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                         "finish_reason": "stop"}],
        })


class _Listener:
    """A proxy listener bound to an ephemeral loopback port."""

    def __init__(self, cfg) -> None:
        self.server = proxy.make_server(cfg, host="127.0.0.1", port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def health(self) -> dict:
        with urllib.request.urlopen(self.base + "/health", timeout=5) as resp:
            return json.loads(resp.read().decode())

    def chat(self) -> dict:
        payload = json.dumps({
            "model": "gemma",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
            "temperature": 0,
        }).encode()
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return {"status": resp.status, "body": json.loads(resp.read().decode()),
                        "headers": {k.lower(): v for k, v in resp.headers.items()}}
        except urllib.error.HTTPError as exc:  # the loud-failure path
            return {"status": exc.code, "body": json.loads(exc.read().decode()),
                    "headers": {k.lower(): v for k, v in exc.headers.items()}}

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _start_stub() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# --------------------------------------------------------------------------- #
# 1. The agent surface: Node C is pinned to LOCAL weights
# --------------------------------------------------------------------------- #

class NodeCAgentSurfaceIsLocalTest(unittest.TestCase):
    def test_the_installer_default_backend_is_local(self):
        text = NODE_C_INSTALLER.read_text(encoding="utf-8")
        self.assertIn('BACKEND="${BACKEND:-local}"', text)
        self.assertNotIn('BACKEND="${BACKEND:-gemini-proxy}"', text)

    def test_the_rendered_first_boot_pins_local(self):
        env = dict(RENDER_ENV)
        env["INFERENCE_BACKEND"] = "local"
        out = subprocess.run([str(NODE_C_RENDERER), "--write-files"], env=env,
                             cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn('BACKEND="local"', out.stdout)
        # The agent surface still fronts the loopback worker — the AR-4 shape.
        self.assertIn('PROXY_PORT="8080"', out.stdout)

    def test_the_renderer_default_is_local_when_unset(self):
        out = subprocess.run([str(NODE_C_RENDERER), "--write-files"], env=RENDER_ENV,
                             cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn('BACKEND="local"', out.stdout)


# --------------------------------------------------------------------------- #
# 2. The provider surface: rendered, loopback-only, key-free
# --------------------------------------------------------------------------- #

class ProviderSurfaceRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="termini-surfaces-")
        cls.out = pathlib.Path(cls.tmp.name) / "rendered"
        proc = subprocess.run(
            ["bash", str(SETUP_SURFACES), "--render-dir", str(cls.out)],
            cwd=REPO_ROOT, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        cls.env_file = (cls.out / "etc/termini/inference-proxy.env").read_text(encoding="utf-8")
        cls.dropin = (cls.out / "etc/systemd/system/termini-controller.service.d/"
                      "zz-inference-surfaces.conf").read_text(encoding="utf-8")
        cls.unit = (cls.out / "etc/systemd/system/"
                    "termini-inference-provider.service").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_the_provider_listener_is_gemini_on_loopback(self):
        self.assertIn("TERMINI_INFERENCE_BACKEND=gemini-proxy", self.env_file)
        self.assertIn("TERMINI_INFERENCE_HOST=127.0.0.1", self.env_file)
        self.assertIn("TERMINI_INFERENCE_PORT=8085", self.env_file)
        self.assertIn("TERMINI_GEMINI_HOST=aiplatform.googleapis.com", self.env_file)
        self.assertNotIn("TERMINI_INFERENCE_HOST=0.0.0.0", self.env_file)

    def test_the_provider_listener_carries_no_key_file(self):
        for text in (self.env_file, self.unit, SETUP_SURFACES.read_text(encoding="utf-8")):
            self.assertNotIn("TERMINI_GEMINI_ACCESS_TOKEN=", text)
            self.assertNotIn("activate-service-account", text)
            self.assertNotIn("--key-file", text)
            self.assertNotIn(".pem", text)
        self.assertIn("TERMINI_GEMINI_PROJECT=\n", self.env_file)

    def test_the_unit_is_the_provider_surface(self):
        self.assertIn("EnvironmentFile=-/etc/termini/inference-proxy.env", self.unit)
        self.assertIn("SyslogIdentifier=termini-inference-provider", self.unit)
        self.assertNotIn("0.0.0.0", self.unit)
        self.assertIn("NoNewPrivileges=true", self.unit)

    def test_the_dropin_sends_the_diagnosis_to_the_loopback_provider(self):
        self.assertIn("Environment=TERMINI_INFERENCE_BASE_URL=http://127.0.0.1:8085",
                      self.dropin)
        self.assertIn("Environment=TERMINI_INFERENCE_MODEL=gemma", self.dropin)
        # A provider round-trip is seconds, not the CPU-era 900 s budget.
        self.assertIn("Environment=TERMINI_OPERATOR_DECISION_TIMEOUT_S=120", self.dropin)

    def test_the_dropin_still_names_node_c(self):
        """The split is what makes the explicit placement exclusion necessary.

        `TERMINI_INFERENCE_BASE_URL` now names loopback, so it can no longer
        identify the model host; unless Node C is named here it would silently
        become eligible for auto agent placement (controller-api.md §5.2 step 5).
        """
        self.assertIn("Environment=TERMINI_AGENT_PLACEMENT_EXCLUDE_HOSTS=10.10.0.13",
                      self.dropin)
        self.assertIn("Environment=TERMINI_INTENT_INFERENCE_BASE_URL=http://10.10.0.13:8090",
                      self.dropin)

    def test_the_rendered_proxy_is_the_committed_proxy(self):
        rendered = (self.out / "opt/termini/inference/proxy.py").read_bytes()
        self.assertEqual(PROXY_PATH.read_bytes(), rendered)


# --------------------------------------------------------------------------- #
# 3. The pre-split script cannot undo the split by accident
# --------------------------------------------------------------------------- #

class PreSplitScriptRefusesTest(unittest.TestCase):
    def test_it_refuses_without_allow_unsplit(self):
        out = subprocess.run(["bash", str(LEGACY_SETUP)], cwd=REPO_ROOT,
                             capture_output=True, text=True)
        self.assertEqual(2, out.returncode, out.stdout + out.stderr)
        self.assertIn("--allow-unsplit", out.stderr)
        self.assertIn("setup-inference-surfaces.sh", out.stderr)

    def test_it_states_the_way_back(self):
        out = subprocess.run(["bash", str(LEGACY_SETUP), "--help"], cwd=REPO_ROOT,
                             capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stdout + out.stderr)
        self.assertIn("--allow-unsplit", out.stdout)


# --------------------------------------------------------------------------- #
# 4. /health tells the truth about the LOCAL path, per surface
# --------------------------------------------------------------------------- #

class LocalPathIsMeasuredTest(unittest.TestCase):
    def test_a_provider_listener_does_not_claim_a_local_model(self):
        """The controller's listener has NO worker behind it — say so."""
        listener = _Listener(proxy.Config(
            backend=proxy.BACKEND_GEMINI,
            host="127.0.0.1", port=0, alias="gemma",
            upstream="http://127.0.0.1:9",          # nothing listens there
            gemini_host="aiplatform.googleapis.com", gemini_project="p",
            access_token="test-token", state_file="/dev/null",
        ))
        try:
            health = listener.health()
        finally:
            listener.stop()
        self.assertEqual("gemini-proxy", health["backend"])
        self.assertEqual("external", health["egress"])
        self.assertEqual("aiplatform.googleapis.com", health["destination"])
        self.assertFalse(health["local_path"]["available"])

    def test_a_local_listener_with_a_worker_reports_it(self):
        stub, upstream = _start_stub()
        listener = _Listener(proxy.Config(
            backend=proxy.BACKEND_LOCAL,
            host="127.0.0.1", port=0, alias="gemma",
            upstream=upstream, state_file="/dev/null",
        ))
        try:
            health = listener.health()
        finally:
            listener.stop()
            stub.shutdown()
            stub.server_close()
        self.assertEqual("local", health["backend"])
        self.assertEqual("in-vpc", health["egress"])
        self.assertTrue(health["local_path"]["available"])


# --------------------------------------------------------------------------- #
# 5. The two surfaces are independent
# --------------------------------------------------------------------------- #

class SurfacesAreIndependentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stub, self.worker_url = _start_stub()
        # Node C's AGENT surface: local weights.
        self.agent = _Listener(proxy.Config(
            backend=proxy.BACKEND_LOCAL, host="127.0.0.1", port=0,
            alias="gemma", upstream=self.worker_url, state_file="/dev/null",
        ))
        # The CONTROLLER's PROVIDER surface: Gemini, loopback, different backend.
        self.provider = _Listener(proxy.Config(
            backend=proxy.BACKEND_GEMINI, host="127.0.0.1", port=0,
            alias="gemma", upstream=self.worker_url,
            gemini_host="http://127.0.0.1:9",   # a dead Vertex: no network in tests
            gemini_project="termini-510609", access_token="test-token",
            state_file="/dev/null",
        ))

    def tearDown(self) -> None:
        self.agent.stop()
        self.provider.stop()
        self.stub.shutdown()
        self.stub.server_close()

    def test_each_surface_reports_its_own_backend(self):
        self.assertEqual("local", self.agent.health()["backend"])
        self.assertEqual("gemini-proxy", self.provider.health()["backend"])

    def test_serving_on_one_surface_does_not_change_the_other(self):
        before = self.provider.health()
        served = self.agent.chat()
        after = self.provider.health()
        self.assertEqual(200, served["status"], served)
        # `local` mode relays llama.cpp's own bytes, so the surface labels itself
        # in the response HEADERS (the gemini path also labels the body).
        self.assertEqual("local", served["headers"]["x-termini-backend"])
        self.assertEqual("in-vpc", served["headers"]["x-termini-egress"])
        self.assertEqual("local-weights", served["headers"]["x-termini-answered-by"])
        # The provider listener neither saw the request nor changed its answer:
        # no shared state, no shared unit, no shared env file.
        self.assertEqual(before, after)

    def test_breaking_one_surface_leaves_the_other_serving(self):
        # The agent surface loses its worker (a stopped llama.cpp) — the provider
        # surface must be unaffected, and vice versa. This is the property that
        # makes them two surfaces rather than one with a flag.
        provider_before = self.provider.health()
        dead = _Listener(proxy.Config(
            backend=proxy.BACKEND_LOCAL, host="127.0.0.1", port=0,
            alias="gemma", upstream="http://127.0.0.1:9", state_file="/dev/null",
        ))
        try:
            failed = dead.chat()
            self.assertNotEqual(200, failed["status"], failed)
            self.assertEqual(503, failed["status"])
            self.assertEqual("MODEL_UNAVAILABLE", failed["body"]["error"]["code"])
            self.assertEqual(provider_before, self.provider.health())
        finally:
            dead.stop()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
