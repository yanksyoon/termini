"""Node C's first boot ships the inference front door (task t_359ed606).

The defect this pins: Node C's **first boot** used to start the host-exposed
llama.cpp server on `0.0.0.0:8080` and nothing else, so a freshly created node
came up on the pre-proxy `legacy` path until someone ran
`infra/inference/install-inference-proxy.sh` BY HAND. A node raised inside the
demo window must serve the forwarded decider with no manual step.

Three properties are tested, because any one alone can be satisfied while the
node still comes up wrong:

1. **The rendered cloud-init carries the front door.** `node-c-serve-block.sh`
   (the renderer BOTH gcloud paths use) must emit the first-boot script plus the
   front-door payload, base64-decoding to the EXACT committed bytes of
   `infra/inference/{install-inference-proxy.sh,proxy.py,
   termini-inference-proxy.service,termini-inference-switch.sh}` — so the payload
   cannot be a stale copy.
2. **The first boot CALLS the installer instead of copying its heredocs.**
   `llama-serve.sh` must reference `/usr/local/src/termini-inference/
   install-inference-proxy.sh`, and must no longer define
   `termini-llama-server.service` (whose loopback-worker form belongs to the
   installer, the single source of truth). It keeps exactly one unit of its own:
   the intent-extraction instance on :8090.
3. **The installer really installs what the card requires** — the loopback
   worker, the front door unit, the one-step switch in `/usr/local/sbin`, and a
   tmpfiles.d rule that recreates `/run/termini` after a reboot — while keeping
   the slot/context properties Defect A depended on (>=2 slots, 16k each).

The checker itself is shared with `infra/gcp/verify-cloud-init.sh`, and test 4
proves it can FAIL (a checker that cannot fail is not evidence).
"""
from __future__ import annotations

import base64
import importlib.util
import pathlib
import re
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
CLOUD_INIT = REPO / "infra/gcp/cloud-init"
RENDERER = CLOUD_INIT / "node-c-serve-block.sh"
SERVE_SCRIPT = CLOUD_INIT / "llama-serve.sh"
INSTALLER = REPO / "infra/inference/install-inference-proxy.sh"
SWITCH = REPO / "infra/inference/termini-inference-switch.sh"

# The shipped pins (image/models.lock.json + infra/gcp/variables.tf defaults).
RENDER_ENV = {
    "LLAMACPP_COMMIT": "7fe450e19305b828c199d602c23a8337aaa1f03b",
    "MODEL_URL": "https://storage.googleapis.com/termini-510609-models/gemma-3-4b-it-gguf/gemma-3-4b-it-Q4_K_M.gguf",
    "MODEL_FILE": "gemma-3-4b-it-Q4_K_M.gguf",
    "MODEL_SHA256": "04a43a22e8d2003deda5acc262f68ec1005fa76c735a9962a8c77042a74a7d19",
    "MODEL_ALIAS": "gemma",
    "PORT": "8080",
    "INFERENCE_BACKEND": "local",
}


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "node_c_front_door_check", CLOUD_INIT / "node_c_front_door_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHECK = _load_checker()


def render(mode: str = "--write-files", env: dict | None = None) -> str:
    """Run the shared renderer exactly as provision.sh / raise-standing-fleet.sh do."""
    import os

    full = dict(os.environ)
    full.update(RENDER_ENV)
    full.update(env or {})
    out = subprocess.run(
        [str(RENDERER), mode], env=full, check=True, capture_output=True, text=True)
    return out.stdout


class RenderedNodeCBlockTest(unittest.TestCase):
    """Requirement: a node from the image family serves the front door, unasked."""

    @classmethod
    def setUpClass(cls):
        cls.text = render("--write-files")

    def test_the_renderer_is_executable(self):
        self.assertTrue(RENDERER.is_file())
        self.assertTrue(RENDERER.stat().st_mode & 0o111, f"{RENDERER} is not executable")

    def test_rendered_block_passes_the_front_door_check(self):
        fail = CHECK.check_user_data(self.text, REPO)
        self.assertEqual([], fail, "\n".join(fail))

    def test_the_payload_is_the_committed_file_not_a_copy(self):
        """The four files are staged byte-for-byte (decoded, not merely present)."""
        found = CHECK.entries(self.text)
        for dest, rel, _mode in CHECK.FRONT_DOOR_PAYLOAD:
            self.assertIn(dest, found, f"missing {dest}")
            got = base64.b64decode(found[dest]["content"].strip().strip('"'), validate=True)
            self.assertEqual((REPO / rel).read_bytes(), got,
                             f"{dest} does not match {rel} byte-for-byte")

    def test_the_first_boot_default_backend_is_explicit(self):
        """A fresh node serves the SHIPPED default, and it is visible in the render.

        The shipped default is `local` (test t_aa7a70f9 / D9): :8080 is the AGENT
        surface and must answer from this node's own weights, never forward.
        """
        self.assertIn('BACKEND="local"', CHECK.block_scalar(self.text, CHECK.SERVE_SCRIPT))
        self.assertIn('PROXY_PORT="8080"', CHECK.block_scalar(self.text, CHECK.SERVE_SCRIPT))

    def test_the_backend_is_renderer_configurable(self):
        """`gemini-proxy` is one variable away at render time (and one command on the node)."""
        text = render("--write-files", {"INFERENCE_BACKEND": "gemini-proxy"})
        self.assertIn('BACKEND="gemini-proxy"', CHECK.block_scalar(text, CHECK.SERVE_SCRIPT))
        self.assertEqual([], CHECK.check_user_data(text, REPO, backend="gemini-proxy"))

    def test_an_invalid_backend_is_refused_at_render_time(self):
        import os

        env = dict(os.environ)
        env.update(RENDER_ENV)
        env["INFERENCE_BACKEND"] = "gpu"
        out = subprocess.run([str(RENDERER), "--write-files"], env=env,
                             capture_output=True, text=True)
        self.assertNotEqual(0, out.returncode)
        self.assertIn("must be local or gemini-proxy", out.stderr)

    def test_the_runcmd_runs_the_first_boot_script(self):
        self.assertEqual("  - [ /usr/local/sbin/termini-llama-serve.sh ]\n", render("--runcmd"))

    def test_the_checker_FAILS_on_a_tampered_payload(self):
        """The negative control: a single flipped byte must be caught."""
        found = CHECK.entries(self.text)
        dest = "/usr/local/src/termini-inference/proxy.py"
        raw = base64.b64decode(found[dest]["content"].strip().strip('"'), validate=True)
        tampered = base64.b64encode(raw.replace(b"import", b"imp0rt", 1)).decode()
        broken = self.text.replace(found[dest]["content"], f'"{tampered}"')
        fail = CHECK.check_user_data(broken, REPO)
        self.assertTrue(any("proxy.py" in f for f in fail), fail)

    def test_the_checker_FAILS_when_a_payload_entry_is_dropped(self):
        marker = "  - path: /usr/local/src/termini-inference/termini-inference-switch.sh\n"
        start = self.text.index(marker)
        end = self.text.index("  - path:", start + len(marker)) if "  - path:" in self.text[start + len(marker):] else len(self.text)
        fail = CHECK.check_user_data(self.text[:start] + self.text[end:], REPO)
        self.assertTrue(any("termini-inference-switch.sh" in f for f in fail), fail)


class FirstBootScriptTest(unittest.TestCase):
    """Requirement: call the installer; do not re-implement its units."""

    @classmethod
    def setUpClass(cls):
        cls.script = SERVE_SCRIPT.read_text()

    def test_it_calls_the_committed_installer(self):
        self.assertIn('"$FRONT_DOOR_SRC/install-inference-proxy.sh"', self.script)
        self.assertIn('--backend "$BACKEND"', self.script)
        self.assertIn('FRONT_DOOR_SRC:-/usr/local/src/termini-inference', self.script)

    def test_it_no_longer_writes_the_worker_unit(self):
        """The loopback worker unit is the installer's, so the two cannot drift."""
        self.assertNotIn("termini-llama-server.service <<UNIT", self.script)
        self.assertNotIn("termini-llama-server.service\n<<UNIT", self.script)

    def test_it_serves_only_the_intent_instance_itself(self):
        execs = [ln for ln in self.script.splitlines() if ln.startswith("ExecStart=")]
        self.assertEqual(1, len(execs), execs)
        self.assertIn("termini-llama-intent.service", self.script)
        port = re.search(r"--port\s+(\S+)", execs[0])
        self.assertIsNotNone(port, execs[0])
        self.assertEqual("$INTENT_PORT", port.group(1))
        self.assertRegex(self.script, r'INTENT_PORT="\$\{INTENT_PORT:-(\d+)\}"')

    def test_the_host_exposed_port_is_no_longer_hardcoded(self):
        self.assertNotIn("--host 0.0.0.0 --port $PORT", self.script)

    def test_it_fails_closed_when_the_payload_is_missing(self):
        self.assertIn("install-inference-proxy.sh missing", self.script)


class InstallerIsTheSingleSourceOfTruthTest(unittest.TestCase):
    """The units/env the first boot ends up with, as the card lists them."""

    @classmethod
    def setUpClass(cls):
        cls.installer = INSTALLER.read_text()

    def test_the_worker_unit_is_loopback_only(self):
        self.assertIn('WORKER_PORT="${WORKER_PORT:-8081}"', self.installer)
        self.assertIn("LLAMA_HOST=127.0.0.1", self.installer)
        self.assertIn("LLAMA_PORT=$WORKER_PORT", self.installer)
        # the unit binds from the env file, i.e. loopback, not 0.0.0.0:8080
        self.assertIn("--host \\$LLAMA_HOST --port \\$LLAMA_PORT", self.installer)
        self.assertNotIn("--host 0.0.0.0", self.installer)

    def test_the_worker_keeps_two_full_context_slots(self):
        """Defect A's fix must survive the move out of llama-serve.sh."""
        execs = [ln for ln in self.installer.splitlines() if ln.startswith("ExecStart=")]
        self.assertEqual(1, len(execs), execs)
        parallel = int(re.search(r"--parallel\s+(\d+)", execs[0]).group(1))
        ctx = int(re.search(r"--ctx-size\s+(\d+)", execs[0]).group(1))
        self.assertGreaterEqual(parallel, 2)
        self.assertGreaterEqual(ctx // parallel, 16384)

    def test_it_installs_the_switch_and_the_boot_safe_state_dir(self):
        self.assertIn('"$SBIN_DIR/termini-inference-switch"', self.installer)
        self.assertIn('SBIN_DIR:-/usr/local/sbin', self.installer)
        self.assertIn("d /run/termini 0755 termini-infer termini-infer -", self.installer)
        self.assertIn("systemd-tmpfiles --create", self.installer)

    def test_the_proxy_unit_and_backend_env_are_the_installers_own(self):
        self.assertIn('"$UNIT_DIR/termini-inference-proxy.service"', self.installer)
        self.assertIn("TERMINI_INFERENCE_BACKEND=$BACKEND", self.installer)
        self.assertIn('TERMINI_INFERENCE_HOST=0.0.0.0', self.installer)

    def test_it_installs_script_files_that_exist(self):
        """The payload the renderer stages is exactly what the installer consumes."""
        for path in (INSTALLER, SWITCH, REPO / "infra/inference/proxy.py",
                     REPO / "infra/inference/termini-inference-proxy.service"):
            self.assertTrue(path.is_file() and path.stat().st_size > 0, path)


class GcloudRenderersShareOneBlockTest(unittest.TestCase):
    """Both gcloud raise paths must emit the SAME block (no third copy)."""

    def test_both_renderers_call_the_shared_script(self):
        for rel in ("infra/gcp/provision.sh", "infra/gcp/tools/raise-standing-fleet.sh"):
            text = (REPO / rel).read_text()
            with self.subTest(renderer=rel):
                self.assertIn("node-c-serve-block.sh", text)
                self.assertIn('"$NODE_C_SERVE_BLOCK" --write-files', text)
                self.assertIn('"$NODE_C_SERVE_BLOCK" --runcmd', text)
                # no local copy of the unit heredocs / the old inline substitution
                self.assertNotIn("termini-llama-server.service <<UNIT", text)
                self.assertNotIn("__LLAMACPP_COMMIT__", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
