"""Every node a raise produces must be TRUSTED by the controller (t_d31b8e02).

The defect this pins: the controller's LXD client identity was installed into
node-a's trust store **by hand**, once, and no raise step ever did it. node-c
therefore answered `GET /1.0` with 200 (reachable, healthy, registering,
reporting telemetry) and `GET /1.0/instances` with **403** — the controller was
not trusted — so a plan with an explicit `target_node` naming node-c applied to
`{"state":"failed","vm_ids":[],"detail":"… HTTP 403 … Forbidden"}` while the same
plan on node-a created a real RUNNING instance. Agent placement hid the gap by
excluding node-c's host, which also hid its 8 vCPU / 29 GiB.

A recreated instance replaces its boot disk, so its trust store comes back empty.
That is why the fix is a raise-path step (`tools/install-lxd-trust.sh`, called by
`tools/raise-standing-fleet.sh --raise`) and not a one-off, and why the same code
must serve node-b when it is ever raised.

Three properties are tested, because satisfying any one alone can still ship the
defect:

1. **The raise path calls the install**, once per raised node, after creating it,
   and that call can be seen in the committed script (not just in a runbook).
2. **A node that is already trusted is left alone** — the helper probes first, so
   re-raising or retrying never mints a token or POSTs an identity needlessly.
3. **A node that is not trusted gets the controller identity, and the step fails
   loudly when it cannot prove the result.** A trust install whose failure path is
   "carry on" is how the original 403 reached a demo window.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
HELPER = REPO / "infra/gcp/tools/install-lxd-trust.sh"
RAISE = REPO / "infra/gcp/tools/raise-standing-fleet.sh"

# A token value that must never reach stdout/stderr of the helper.
FAKE_TOKEN = ("eyJjbGllbnRfbmFtZSI6" + "J0Zha2UtdG9rZW4tZG8tbm90LXByaW50J30=")

# Stands in for `gcloud`. Everything the helper does to the fleet arrives here, so
# the test can assert on the exact commands (and on the absence of some) without
# a cloud, a credential, or a live node.
FAKE_GCLOUD = r"""#!/usr/bin/env bash
# Fake `gcloud compute ssh|describe` for tests/test_raise_lxd_trust.py.
set -u
printf '%s\n' "$*" >> "$FAKEGCLOUD_LOG"
args=("$@")
verb="" instance="" cmd=""
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    ssh|describe) verb="${args[i]}"; instance="${args[i+1]:-}" ;;
    --command) cmd="${args[i+1]:-}" ;;
  esac
done

if [ "$verb" = "describe" ]; then
  case ",${FAKE_MISSING:-}," in *",$instance,"*) exit 1 ;; esac
  # A per-instance status override, e.g. FAKE_STATUS_termini_node_b=TERMINATED.
  varname="FAKE_STATUS_$(printf '%s' "$instance" | tr '-' '_')"
  status="${!varname:-${FAKE_STATUS:-RUNNING}}"
  printf '%s\n' "$status"
  exit 0
fi

case "$instance" in
  termini-controller)
    case "$cmd" in
      *lxdtrust\ verify*)
        n=0
        [ -f "$FAKEGCLOUD_STATE/verify_count" ] && n="$(cat "$FAKEGCLOUD_STATE/verify_count")"
        n=$((n + 1)); printf '%s' "$n" > "$FAKEGCLOUD_STATE/verify_count"
        case "${FAKE_MODE:-trusted}" in
          trusted)
            echo "https://node:8443: TRUSTED — authenticated GET /1.0/instances -> 200"; exit 0 ;;
          untrusted)
            if [ "$n" -eq 1 ]; then
              echo "https://node:8443: NOT TRUSTED — GET /1.0/instances -> HTTP 403 (controller cert not trusted)"
              exit 3
            fi
            echo "https://node:8443: TRUSTED — authenticated GET /1.0/instances -> 200"; exit 0 ;;
          still-untrusted)
            echo "https://node:8443: NOT TRUSTED — GET /1.0/instances -> HTTP 403 (controller cert not trusted)"
            exit 3 ;;
          unreachable)
            echo "https://node:8443: UNKNOWN — could not determine trust: error / timeout"; exit 2 ;;
        esac ;;
      *lxdtrust\ trust*)
        echo "https://node:8443 [token]: trusted — certificate added to trust store; authenticated GET /1.0/instances -> 200"
        exit 0 ;;
      *lxdtrust\ cert*)
        echo "cert=/data/lxd-client/controller-lxd-client.crt"
        echo "key=/data/lxd-client/controller-lxd-client.key"
        echo "fingerprint=a27fd4eb94cb070fe1792bed6d05dc31cf4162b8625ac4ec3bc5c9ac933670d5"
        exit 0 ;;
    esac ;;
  termini-node-*)
    case "$cmd" in
      *mint-trust-token*) printf '%s' "$FAKE_TOKEN"; exit 0 ;;
    esac ;;
esac
exit 1
"""


class RaiseInstallsControllerTrust(unittest.TestCase):
    """The committed raise path installs the identity on every node it raises."""

    @classmethod
    def setUpClass(cls):
        cls.src = RAISE.read_text()

    def test_raise_path_calls_the_install_for_each_raised_node(self):
        self.assertIn("install-lxd-trust.sh", self.src,
                      "raise-standing-fleet.sh must install the controller LXD trust")
        self.assertIn("RAISED+=(\"$n\")", self.src,
                      "the script must remember which nodes it raised")
        self.assertRegex(
            self.src,
            r'(?s)for n in "\$\{RAISED\[@\]\}"; do.*?install-lxd-trust\.sh" --node "\$\{n#node-\}"',
            "the install must run per raised node, after the create loop",
        )

    def test_opt_out_exists_and_is_loud(self):
        self.assertIn("--no-trust-install", self.src)
        # Opting out must say what is now missing, not pass silently.
        self.assertRegex(self.src, r'--no-trust-install:.*NOT trusted')

    def test_helper_is_executable_and_fails_fast(self):
        self.assertTrue(os.access(HELPER, os.X_OK), "install-lxd-trust.sh must be executable")
        text = HELPER.read_text()
        self.assertIn("set -euo pipefail", text)

    def test_bootstrap_contract_documents_the_step(self):
        contract = (REPO / "docs/contracts/node-bootstrap.md").read_text()
        self.assertIn("6.4 Controller trust on every node", contract)
        self.assertIn("install-lxd-trust.sh", contract)
        # node-b is the node that has never had the step: the contract must say so.
        self.assertRegex(contract, r'Node B carries the same omission')


class HelperBehaviour(unittest.TestCase):
    """Behaviour of install-lxd-trust.sh against a scripted gcloud."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "gcloud"
        fake.write_text(FAKE_GCLOUD)
        fake.chmod(0o755)
        self.state = self.root / "state"
        self.state.mkdir()
        self.log = self.root / "calls.log"
        self.log.write_text("")
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()

    def run_helper(self, *args, mode="trusted", missing="", env_extra=None):
        env = {
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.root),
            "TMPDIR": str(self.tmpdir),
            "FAKEGCLOUD_LOG": str(self.log),
            "FAKEGCLOUD_STATE": str(self.state),
            "FAKE_MODE": mode,
            "FAKE_MISSING": missing,
            "FAKE_TOKEN": FAKE_TOKEN,
        }
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", str(HELPER), *args],
            capture_output=True, text=True, env=env, timeout=120,
        )

    def calls(self) -> str:
        return self.log.read_text()

    # --- the idempotent no-op ------------------------------------------------
    def test_already_trusted_node_is_a_no_op(self):
        r = self.run_helper("--node", "a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already trusted", r.stderr)
        self.assertNotIn("mint-trust-token", self.calls(),
                         "a trusted node must not have a token minted")
        self.assertNotIn("lxdtrust trust", self.calls(),
                         "a trusted node must not have the identity re-installed")
        # exactly one probe, no mutation
        self.assertEqual(self.calls().count("lxdtrust verify"), 1, self.calls())

    # --- the defect being fixed ---------------------------------------------
    def test_untrusted_node_gets_the_controller_identity(self):
        r = self.run_helper("--node", "c", mode="untrusted")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        # token minted ON the node, consumed ON the controller
        self.assertIn("termini-node-c", calls)
        self.assertIn("mint-trust-token", calls)
        self.assertIn("lxdtrust trust", calls)
        self.assertIn("termini-controller", calls)
        self.assertIn("--token", calls)
        # and proven afterwards: a probe after the install, not just trust's own
        self.assertEqual(calls.count("lxdtrust verify"), 2, calls)
        self.assertIn("TRUSTED", r.stderr + r.stdout)

    def test_token_never_reaches_the_output(self):
        r = self.run_helper("--node", "c", mode="untrusted")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(FAKE_TOKEN, r.stdout + r.stderr,
                         "the one-time trust token must never be printed")
        # the 0600 file it was transited through is removed on exit
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    # --- fail-loud ----------------------------------------------------------
    def test_install_that_cannot_prove_itself_fails(self):
        r = self.run_helper("--node", "c", mode="still-untrusted")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("still not 200", r.stderr)

    def test_node_that_never_answers_is_not_reported_as_trusted(self):
        r = self.run_helper("--node", "c", "--timeout", "0", mode="unreachable")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("never answered", r.stderr)
        self.assertNotIn("mint-trust-token", self.calls())

    # --- selection ----------------------------------------------------------
    def test_dry_run_mutates_nothing(self):
        r = self.run_helper("--node", "a", "--timeout", "0", "--dry-run", mode="untrusted")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would probe", r.stderr)
        self.assertNotIn("mint-trust-token", self.calls())
        self.assertNotIn("lxdtrust trust", self.calls())

    def test_all_skips_a_node_that_does_not_exist(self):
        r = self.run_helper("--all", mode="trusted", missing="termini-node-b")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("termini-node-b does not exist", r.stderr)
        # a and c were still probed
        self.assertIn("termini-node-a", self.calls())
        self.assertIn("termini-node-c", self.calls())

    def test_a_requested_node_that_does_not_exist_is_an_error(self):
        r = self.run_helper("--node", "b", mode="trusted", missing="termini-node-b")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not exist", r.stderr)

    # --- the node that is not up (node-b today) -----------------------------
    def test_all_skips_a_terminated_node(self):
        r = self.run_helper("--all", mode="trusted",
                            env_extra={"FAKE_STATUS_termini_node_b": "TERMINATED"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TERMINATED", r.stderr)
        self.assertNotIn("ssh termini-node-b", self.calls(),
                         "a TERMINATED node must not be SSHed")

    def test_explicitly_naming_a_terminated_node_is_an_error(self):
        r = self.run_helper("--node", "b", mode="trusted",
                            env_extra={"FAKE_STATUS_termini_node_b": "TERMINATED"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("TERMINATED", r.stderr)
        self.assertNotIn("lxdtrust verify", self.calls())


if __name__ == "__main__":
    unittest.main()
