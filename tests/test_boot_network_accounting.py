"""Guard tests for boot-time network accounting.

The W4 acceptance used to assert "the image boots with zero network downloads"
against a *hardcoded* ``network_downloads_at_boot: 0`` that the verifier then
read back from the same file — a circular check. These tests pin the fix:

* ``image/agent-vm/tools/net-accounting.sh`` derives the number from the
  kernel's ``/proc/net/dev`` counters (fixtures below);
* a real fetch (non-loopback RX bytes) is reported as downloads and fails the
  ``--check`` path;
* neither preflight nor the in-VM acceptance still carries the literal ``0``,
  and the acceptance measures live instead of reading the field back.
"""

from __future__ import annotations

import pathlib
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
HELPER = REPO / "image/agent-vm/tools/net-accounting.sh"
OFFLINE_ACCEPT = REPO / "image/agent-vm/tools/offline-acceptance.sh"
AGENT_PREFLIGHT = REPO / "image/agent-vm/first-boot-preflight.sh"
PIPELINE_PREFLIGHT = REPO / "image/pipeline/first-boot-preflight.sh"

HEADER = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
)


def _proc_net_dev(*interfaces: tuple[str, int]) -> str:
    rows = ["    lo: 100 2 0 0 0 0 0 0 100 2 0 0 0 0 0 0"]
    for name, rx in interfaces:
        rows.append(f"  {name}: {rx} 10 0 0 0 0 0 0 2000 9 0 0 0 0 0 0")
    return HEADER + "\n".join(rows) + "\n"


class NetAccountingTest(unittest.TestCase):
    def _run(self, args, fixture=None):
        env = {}
        if fixture is not None:
            env["TERMINI_PROC_NET_DEV"] = fixture
        import os

        return subprocess.run(
            ["bash", str(HELPER), *args],
            capture_output=True, text=True,
            env={**os.environ, **env}, check=False,
        )

    def test_loopback_only_has_zero_downloads(self):
        import json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".dev", delete=False) as fh:
            fh.write(_proc_net_dev())
            fixture = fh.name
        doc = json.loads(self._run(["--json"], fixture).stdout)
        self.assertEqual(doc["downloaded_bytes"], 0)
        self.assertEqual(doc["rx_bytes"], 0)
        self.assertEqual(doc["nonloopback_interfaces"], [])
        self.assertTrue(doc["measured"])

    def test_link_local_dhcp_noise_is_within_allowance(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".dev", delete=False) as fh:
            fh.write(_proc_net_dev(("eth0", 3352)))  # observed DHCP/ND burst
            fixture = fh.name
        proc = self._run(["--check"], fixture)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("network_downloads_at_boot=0", proc.stdout)

    def test_real_fetch_is_reported_and_fails_check(self):
        import json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".dev", delete=False) as fh:
            fh.write(_proc_net_dev(("eth0", 524288)))
            fixture = fh.name
        doc = json.loads(self._run(["--json"], fixture).stdout)
        self.assertEqual(doc["downloaded_bytes"], 524288 - 8192)
        proc = self._run(["--check"], fixture)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("network_downloads_at_boot=", proc.stdout)


class NoHardcodedLiteralTest(unittest.TestCase):
    def test_preflights_do_not_write_the_literal(self):
        for path in (AGENT_PREFLIGHT, PIPELINE_PREFLIGHT, OFFLINE_ACCEPT):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(
                '"network_downloads_at_boot": 0', text,
                f"{path} still hardcodes network_downloads_at_boot: 0",
            )

    def test_acceptance_measures_live_not_from_the_boot_log(self):
        text = OFFLINE_ACCEPT.read_text(encoding="utf-8")
        # the old circular read: jq '.network_downloads_at_boot' "$BOOT_LOG"
        self.assertNotIn('.network_downloads_at_boot" "$BOOT_LOG', text.replace("'", '"'))
        self.assertIn("termini_net_counters", text)
        self.assertIn("termini_net_downloaded_bytes", text)


if __name__ == "__main__":
    unittest.main()
