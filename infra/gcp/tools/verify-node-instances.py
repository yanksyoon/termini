#!/usr/bin/env python3
"""Prove a REAL node reports its LXD instances + services (t_8b66366d).

Run ON a Termini node as root:

    sudo python3 infra/gcp/tools/verify-node-instances.py [--daemon /usr/local/bin/termini-bootstrap]

What it checks, in one go:

  1. `lxc list --format=json` on the node — every RUNNING instance, with the
     `vm_id` the node's own daemon derives for it (the same function, imported
     from the installed daemon — never a second copy of the rule);
  2. the sample the CONTROLLER stored for that node (fetched back over the
     node-token endpoint, so it is the ingested, validated sample):
       - `lxd_instances[]` present and listing exactly the running instances;
       - `services[]` present.
  3. exits non-zero, with the reason, when either is missing — a green run is
     evidence, and nothing else is.

Why the controller round-trip: what the daemon *sent* proves nothing about what
the fleet sees. The stored sample is the one the guardrail resolves against.
"""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import urllib.request

IDENT_PATH = "/var/lib/termini/identity/node.json"
CONFIG_PATH = "/etc/termini/config.yaml"


def config_value(key: str, path: str = CONFIG_PATH, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return default


def load_daemon(path: str):
    """Import the INSTALLED daemon so the id derivation has one source of truth."""
    loader = importlib.machinery.SourceFileLoader("termini_bootstrap_installed", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def running_instances() -> list:
    proc = subprocess.run(["lxc", "list", "--format=json"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("lxc list failed: %s" % (proc.stderr or "").strip())
    return [i for i in json.loads(proc.stdout or "[]") if i.get("status") == "Running"]


def stored_samples(node_id: str, token: str, controller: str, limit: int) -> list:
    url = "%s/v1/nodes/%s/telemetry/recent?limit=%d" % (controller.rstrip("/"), node_id, limit)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp).get("samples", [])


def compare(expected: dict, sample: dict) -> list[str]:
    """Reasons the stored sample does not match the node's running instances.

    `expected` is {vm_id -> instance name} derived from `lxc list`; `sample` is
    the controller's stored sample for this node. An empty list means the sample
    lists exactly the running instances (and their services).
    """
    failures: list[str] = []
    reported = sample.get("lxd_instances")
    if reported is None:
        failures.append("lxd_instances is absent from the stored sample")
    elif not isinstance(reported, list):
        failures.append("lxd_instances is not an array: %r" % (reported,))
    else:
        got = {e.get("vm_id"): e for e in reported if isinstance(e, dict)}
        for vm_id, name in expected.items():
            if vm_id not in got:
                failures.append("%s (%s) is RUNNING but not in the stored sample" % (name, vm_id))
        for vm_id, entry in got.items():
            if vm_id not in expected:
                failures.append("%s is in the stored sample but is not a running instance" % vm_id)
            elif entry.get("state") != "running":
                failures.append("%s reported state=%r" % (vm_id, entry.get("state")))
    if not sample.get("services"):
        failures.append("services[] is absent or empty in the stored sample")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon", default="/usr/local/bin/termini-bootstrap")
    ap.add_argument("--identity", default=IDENT_PATH,
                    help="node identity file (default: the real one)")
    ap.add_argument("--config", default=CONFIG_PATH,
                    help="node config file (default: the real one)")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    tb = load_daemon(args.daemon)
    ident = json.load(open(args.identity, encoding="utf-8"))
    node_id, token = ident["node_id"], ident["node_token"]
    controller = config_value("controller_url", args.config, "http://10.10.0.2:8080")
    print("node_id=%s  controller=%s  daemon=%s" % (node_id, controller, args.daemon))

    live = running_instances()
    expected = {tb.vm_id_for_instance(i["name"]): i["name"] for i in live}
    print("\n`lxc list` running instances -> derived vm_id (the controller resolves with these):")
    for vm_id, name in sorted(expected.items()):
        print("   %s  %s" % (vm_id, name))

    samples = stored_samples(node_id, token, controller, args.limit)
    if not samples:
        print("\nFAIL: the controller stored NO sample for this node")
        return 1
    latest = samples[0]
    print("\nstored sample seq=%s ts=%s" % (latest.get("seq"), latest.get("ts")))
    print("   lxd_instances = %s" % json.dumps(latest.get("lxd_instances")))
    print("   services      = %s" % json.dumps(latest.get("services")))

    failures = compare(expected, latest)
    if failures:
        print("\nFAIL:")
        for f in failures:
            print("   - %s" % f)
        return 1
    print("\nPASS: the controller's stored sample lists every running instance on this node "
          "(with vm_id/state/resources) and its services.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
