#!/usr/bin/env python3
"""termini-node-status — the customer's own view of a Termini node.

This is the confirmation surface of the client-facing install flow
(`infra/bootstrap/client/README.md`, task `t_70bd0f2b`). It is installed by
`termini-node-install.sh` onto the node and answers two questions **using only
the customer's own credential** — the node token the controller issued to this
node at registration, which lives 0600 on the node itself:

  1. "is my node registered?"            -> `termini-node-status`
  2. "what is my node's health right now?" -> `termini-node-status --watch`

The controller's project token is never involved: `GET
/v1/nodes/{node_id}/telemetry/recent` authenticates with the node's own token and
the controller refuses any other node's id
(`controller/app/server.py::_require_node`). That is the point of the card this
ships with — a client must never need the provider's credential.

Also used by the installer itself (`--wait`) so the "your node is registered"
confirmation and the three distinct failure messages have ONE implementation
instead of two that can drift.

Exit codes (consumed by `termini-node-install.sh` and by the test suite):

    0  registered and reachable (ONLINE)
    2  usage error
    3  enrollment credential rejected (unknown/expired)
    4  enrollment credential already used (single-use, burned by an earlier node)
    5  controller unreachable (DNS / refused / timeout)
    6  registration failed for another terminal reason (schema, version, ...)
    7  still connecting (waiting) — not a failure, but not done either
    8  no identity on this node yet (nothing has registered here)

Reads the SAME paths and environment overrides as the node daemon
(`infra/bootstrap/node/termini-bootstrap.py`) so it can never describe a
different node than the one that is running:
`TERMINI_CONFIG`, `TERMINI_IDENTITY_DIR`, `TERMINI_STATE_PATH`.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_PATH = os.environ.get("TERMINI_CONFIG", "/etc/termini/config.yaml")
IDENTITY_DIR = os.environ.get("TERMINI_IDENTITY_DIR", "/var/lib/termini/identity")
STATE_PATH = os.environ.get("TERMINI_STATE_PATH", "/var/lib/termini/state.json")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CREDENTIAL_REJECTED = 3
EXIT_CREDENTIAL_USED = 4
EXIT_UNREACHABLE = 5
EXIT_REGISTRATION_FAILED = 6
EXIT_STILL_CONNECTING = 7
EXIT_NO_IDENTITY = 8

USAGE = """\
termini-node-status — show this node's registration and health (your own credential)

Usage:
  termini-node-status [--controller URL] [--limit N] [--json]
  termini-node-status --watch [SECONDS] [--limit N]
  termini-node-status --wait [--timeout SECONDS]

Options:
  --controller URL   override the controller URL from the node config
  --limit N          how many recent telemetry samples to read (default 5, max 100)
  --json             print the raw JSON instead of the formatted view
  --watch [SECONDS]  stream health, one line per sample (default 5 s)
  --wait             wait for registration to finish, then print the confirmation
  --timeout SECONDS  bound for --wait (default 60)
  --explain-state F  classify a daemon state file (used by the installer/tests)
  -h, --help         this text
"""


# --------------------------------------------------------------------------- inputs
def parse_config(path: str) -> dict:
    """Parse the flat `key: value` subset the installer and cloud-init write."""
    cfg: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return cfg


def read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def identity_path() -> str:
    return os.path.join(IDENTITY_DIR, "node.json")


# --------------------------------------------------------------------------- controller
def http_json(url: str, token: str, timeout: float = 10.0) -> tuple[int, object]:
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token, "X-Termini-Version": "1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def fetch_samples(controller: str, node_id: str, node_token: str, limit: int) -> tuple[int, object]:
    url = (controller.rstrip("/") + f"/v1/nodes/{node_id}/telemetry/recent"
           + "?" + urllib.parse.urlencode({"limit": str(limit)}))
    return http_json(url, node_token)


# --------------------------------------------------------------------------- formatting
def health_line(sample: dict) -> str:
    """One human line per telemetry sample: the node's live health."""
    ram_total = sample.get("ram_total_bytes") or 0
    ram_used = sample.get("ram_used_bytes") or 0
    ram_pct = (100.0 * ram_used / ram_total) if ram_total else 0.0
    disk = ""
    disks = sample.get("disk") or []
    if isinstance(disks, list) and disks:
        d0 = disks[0] if isinstance(disks[0], dict) else {}
        total = d0.get("total_bytes") or 0
        free = d0.get("free_bytes") or 0
        if total:
            disk = f" disk_free={100.0 * free / total:.1f}%"
    net = ""
    nets = sample.get("net") or []
    if isinstance(nets, list) and nets and isinstance(nets[0], dict):
        lat = nets[0].get("latency_ms")
        if lat is not None:
            net = f" ctrl_rtt={lat}ms"
    return (
        f"seq={sample.get('seq')} ts={sample.get('ts')} "
        f"cpu={sample.get('cpu_util_pct')}% ram={ram_pct:.1f}% "
        f"load1={sample.get('load_avg_1m')}{disk}{net}"
    )


def print_registration(identity: dict, samples: list | None) -> None:
    print("YOUR NODE IS REGISTERED")
    print(f"  node_id        {identity.get('node_id')}")
    print(f"  role           {identity.get('role')}")
    print(f"  project        {identity.get('project_id')}")
    print(f"  registered_at  {identity.get('registered_at')}")
    print(f"  register_ms    {identity.get('register_ms')} "
          f"(controller acknowledgement)")
    if samples:
        print("HEALTH (your node, read with your node's own credential)")
        for s in samples:
            print("  " + health_line(s))
    else:
        print("HEALTH (no telemetry sample stored yet — the daemon posts one per "
              "telemetry interval)")


# --------------------------------------------------------------------------- state classification
def classify(state: dict) -> tuple[int, str, str]:
    """Map a daemon state file to `(exit_code, headline, advice)`.

    The daemon (`termini-bootstrap.py`, node-bootstrap.md §9) already classifies
    every registration failure into a `failure_kind`; this only translates that
    classification into something a customer can act on. Keeping the mapping here
    (and not in the shell installer) is what makes the three failure modes
    distinct and testable.
    """
    st = (state.get("state") or "UNKNOWN").upper()
    kind = state.get("failure_kind") or ""
    err = state.get("last_error") or ""
    attempts = state.get("registration_attempts")
    tail = (f"\n  registration_attempts={attempts}" if attempts else "") + \
           (f"\n  controller said: {err}" if err else "")

    if st == "ONLINE":
        return EXIT_OK, "registered and online", ""

    if st == "REGISTRATION_FAILED":
        if kind == "auth" and "already used" in err.lower():
            return (
                EXIT_CREDENTIAL_USED,
                "this enrollment credential has already been used",
                "Enrollment credentials are single-use: the first node that\n"
                "  registers with one consumes it.\n"
                "  * If THIS machine is already registered, re-run the installer\n"
                "    WITHOUT a credential — it reuses the node's persisted\n"
                "    identity and never needs one again.\n"
                "  * If you are installing a DIFFERENT node, ask your provider\n"
                "    for a new credential; this one cannot be reused." + tail,
            )
        if kind == "auth":
            return (
                EXIT_CREDENTIAL_REJECTED,
                "your enrollment credential was rejected by the controller",
                "The controller does not recognise the credential (single-use and\n"
                "  scoped to one registration).\n"
                "  * Check the whole token was copied — no missing characters and no\n"
                "    shell quoting damage;\n"
                "  * check it is the credential your provider issued for THIS node\n"
                "    (every node needs its own);\n"
                "  * then ask your provider for a fresh enrollment credential and\n"
                "    re-run the installer with it." + tail,
            )
        if kind.startswith("unreachable"):
            return (
                EXIT_UNREACHABLE,
                "the controller could not be reached",
                "* Check the controller URL is exact (scheme, host, port, no path);\n"
                "  * check this machine has outbound access to it (port 443/80) and\n"
                "    that no proxy or firewall strips the connection;\n"
                "  * check the controller is running.\n"
                "  The node daemon keeps retrying while its service runs; the\n"
                "  installer can simply be re-run once the controller answers." + tail,
            )
        return (
            EXIT_REGISTRATION_FAILED,
            f"registration failed ({kind or 'unknown reason'})",
            "The controller rejected this node's registration request. This is\n"
            "  usually a version mismatch between the node runtime and the\n"
            "  controller, or a malformed node identity on this machine. Send the\n"
            "  controller message below to your provider." + tail,
        )

    if st in ("ACTIVATING", "REGISTERING", "DEGRADED"):
        return (
            EXIT_STILL_CONNECTING,
            f"the node is not registered yet (state={st})",
            "The daemon is still working on it; it keeps retrying while the\n"
            "  service runs. A node that cannot reach its controller parks in\n"
            "  DEGRADED while it retries, so check the controller URL and this\n"
            "  machine's network path if this does not clear." + tail,
        )

    return (
        EXIT_STILL_CONNECTING,
        f"the node has not reported a state yet (state={st})",
        "Start the node service (`systemctl start termini-bootstrap`) or wait for\n"
        "  the first registration attempt to finish." + tail,
    )


# --------------------------------------------------------------------------- commands
def cmd_explain(path: str) -> int:
    state = read_json(path)
    if state is None:
        print(f"cannot read a daemon state file at {path}", file=sys.stderr)
        return EXIT_STILL_CONNECTING
    code, headline, advice = classify(state)
    print(f"[{code}] {headline}")
    if advice:
        print("  " + advice)
    return code


def wait_for_registration(controller: str, timeout: float) -> int:
    """Wait for the daemon to reach a decision, then print what the client sees."""
    deadline = time.monotonic() + max(1.0, timeout)
    state: dict = read_json(STATE_PATH) or {}
    while True:
        state = read_json(STATE_PATH) or state
        code, headline, advice = classify(state)
        if code != EXIT_STILL_CONNECTING:
            if code == EXIT_OK:
                identity = read_json(identity_path()) or {}
                samples = first_samples(controller, identity, deadline)
                print_registration(identity, samples)
                return EXIT_OK
            print(f"REGISTRATION FAILED: {headline}", file=sys.stderr)
            if advice:
                print("  " + advice, file=sys.stderr)
            return code
        if time.monotonic() >= deadline:
            print(f"NOT REGISTERED YET: {headline}", file=sys.stderr)
            if advice:
                print("  " + advice, file=sys.stderr)
            return EXIT_STILL_CONNECTING
        time.sleep(0.5)


def first_samples(controller: str, identity: dict, deadline: float,
                  limit: int = 5) -> list:
    """Read this node's own telemetry until the first sample exists."""
    node_id, token = identity.get("node_id"), identity.get("node_token")
    if not node_id or not token or not controller:
        return []
    while True:
        try:
            status, body = fetch_samples(controller, node_id, token, limit)
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            return []
        if status == 200 and isinstance(body, dict) and body.get("samples"):
            return body["samples"]
        if time.monotonic() >= deadline:
            return []
        time.sleep(1.0)


def cmd_show(controller: str, limit: int, as_json: bool) -> int:
    identity = read_json(identity_path())
    if not identity or not identity.get("node_id") or not identity.get("node_token"):
        print(f"this machine has no Termini node identity yet ({identity_path()}).",
              file=sys.stderr)
        print("  Register it first: re-run the installer with the enrollment "
              "credential your provider gave you.", file=sys.stderr)
        return EXIT_NO_IDENTITY
    try:
        status, body = fetch_samples(controller, identity["node_id"],
                                     identity["node_token"], limit)
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        print(f"cannot reach the controller at {controller}: {e}", file=sys.stderr)
        return EXIT_UNREACHABLE
    if status != 200:
        print(f"the controller refused this node's own credential (HTTP {status}): {body}",
              file=sys.stderr)
        return EXIT_CREDENTIAL_REJECTED
    samples = body.get("samples") if isinstance(body, dict) else None
    if as_json:
        print(json.dumps({"identity": identity, "samples": samples}, indent=2))
        return EXIT_OK
    print_registration(identity, samples or [])
    return EXIT_OK


def cmd_watch(controller: str, interval: float, limit: int) -> int:
    identity = read_json(identity_path())
    if not identity or not identity.get("node_id") or not identity.get("node_token"):
        print(f"this machine has no Termini node identity yet ({identity_path()}).",
              file=sys.stderr)
        return EXIT_NO_IDENTITY
    seen: set = set()
    print_registration(identity, [])
    print(f"STREAMING health every {interval:g}s (Ctrl-C to stop)")
    while True:
        try:
            status, body = fetch_samples(controller, identity["node_id"],
                                         identity["node_token"], limit)
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            print(f"  [unreachable] {e}", flush=True)
            time.sleep(interval)
            continue
        if status != 200:
            print(f"  [http {status}] {body}", flush=True)
            time.sleep(interval)
            continue
        for s in (body.get("samples") or []):
            key = (s.get("seq"), s.get("ts"))
            if key in seen:
                continue
            seen.add(key)
            print("  " + health_line(s), flush=True)
        time.sleep(interval)


def main(argv: list[str]) -> int:
    controller_override = None
    limit = 5
    as_json = False
    wait = False
    timeout = 60.0
    watch: float | None = None
    explain: str | None = None

    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(USAGE, end="")
            return EXIT_OK
        if a == "--controller" and i + 1 < len(argv):
            controller_override, i = argv[i + 1], i + 2
        elif a == "--limit" and i + 1 < len(argv):
            try:
                limit = max(1, min(100, int(argv[i + 1])))
            except ValueError:
                print("--limit must be an integer", file=sys.stderr)
                return EXIT_USAGE
            i += 2
        elif a == "--json":
            as_json, i = True, i + 1
        elif a == "--wait":
            wait, i = True, i + 1
        elif a == "--timeout" and i + 1 < len(argv):
            try:
                timeout = float(argv[i + 1])
            except ValueError:
                print("--timeout must be a number", file=sys.stderr)
                return EXIT_USAGE
            i += 2
        elif a == "--watch":
            watch = 5.0
            i += 1
            if i < len(argv) and not argv[i].startswith("-"):
                try:
                    watch = float(argv[i])
                except ValueError:
                    print("--watch takes an optional number of seconds",
                          file=sys.stderr)
                    return EXIT_USAGE
                i += 1
        elif a == "--explain-state" and i + 1 < len(argv):
            explain, i = argv[i + 1], i + 2
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            print(USAGE, end="", file=sys.stderr)
            return EXIT_USAGE

    if explain:
        return cmd_explain(explain)

    cfg = parse_config(CONFIG_PATH)
    controller = (controller_override or cfg.get("controller_url") or "").rstrip("/")
    if not controller:
        print(f"no controller URL: pass --controller or set controller_url in {CONFIG_PATH}",
              file=sys.stderr)
        return EXIT_USAGE
    if wait:
        return wait_for_registration(controller, timeout)
    if watch is not None:
        return cmd_watch(controller, max(0.5, watch), limit)
    return cmd_show(controller, limit, as_json)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except KeyboardInterrupt:
        raise SystemExit(EXIT_OK) from None
