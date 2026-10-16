#!/usr/bin/env python3
"""issue-enrollment.py — PROVIDER-side half of the client install flow.

Run this **on the controller host** (the mint route is loopback-only) to hand a
customer a node enrollment. It:

  1. reads the controller's own project token — from a file
     (`--project-token-file`) or from the running controller's systemd unit
     (`--from-unit`, the default) — and never prints it;
  2. mints N fresh **single-use** bootstrap tokens through
     `POST /v1/bootstrap-tokens` (docs/contracts/controller-api.md §4.6), which is
     restricted to a direct loopback client precisely so the project token cannot
     be turned into a minting capability by a remote caller;
  3. preflights the public install surface the customer will actually use
     (`<public-url>/install/termini-node.sh`) so a handover can never name an
     artifact the customer cannot fetch;
  4. prints, for each credential, the exact command the customer runs — and writes
     the same block to a 0600 handover file the provider can send over;
  5. re-reads its own output and REFUSES to finish if the project token appears in
     it (the card's guardrail: the project token must never reach a client
     artifact, doc or URL).

The customer's credential is dead after the first registration (single-use) —
which is the point: what the client holds is scoped to enrolling one node, and
the identity that node then uses to report its health is the node's own token,
issued at registration.

Usage:
    sudo python3 infra/bootstrap/client/issue-enrollment.py \
        --public-url https://<controller-host> [--count 2] [--name node-d]

Exit codes: 0 ok, 2 usage, 3 cannot read the project token, 4 the controller
refused the mint, 5 the controller/public URL is unreachable.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

EXIT_USAGE = 2
EXIT_NO_TOKEN = 3
EXIT_REFUSED = 4
EXIT_UNREACHABLE = 5

DEFAULT_CONTROLLER = "http://127.0.0.1:8080"
DEFAULT_UNIT = "termini-controller"
INSTALL_PATH = "/install/termini-node.sh"

USAGE = __doc__


def die(code: int, msg: str) -> "None":
    print(f"issue-enrollment: ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------------- inputs
def project_token_from_unit(unit: str) -> str | None:
    """Read TERMINI_PROJECT_TOKEN from a running systemd unit's environment.

    `systemctl show -p Environment` reports what the loaded unit declares — the
    token the running controller actually accepts, which is what we must mint
    with. systemd applies drop-ins after the unit (last one wins), so reading the
    environment rather than the unit file is deliberate.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "show", unit, "-p", "Environment"],
            capture_output=True, text=True, check=False,
        )
    except OSError as e:
        die(EXIT_NO_TOKEN, f"cannot run systemctl to read {unit}: {e}")
    for m in re.finditer(r"(?:^|\s)TERMINI_PROJECT_TOKEN=(\S+)", proc.stdout or ""):
        token = m.group(1).strip('"').strip("'")
        if token:
            return token
    return None


def read_project_token(path: str | None, unit: str) -> str:
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError as e:
            die(EXIT_NO_TOKEN,
                f"cannot read the controller's project token from --project-token-file {path}: {e}")
        if not token:
            die(EXIT_NO_TOKEN, f"--project-token-file {path} is empty (no project token)")
        return token
    token = project_token_from_unit(unit)
    if not token:
        die(
            EXIT_NO_TOKEN,
            f"cannot find TERMINI_PROJECT_TOKEN in unit {unit}.\n"
            f"  Run this on the controller host (it mints over loopback), or pass\n"
            f"  --project-token-file <FILE> / --from-unit <UNIT>.",
        )
    return token


# --------------------------------------------------------------------------- controller
def http(method: str, url: str, body: object = None, token: str | None = None,
         timeout: float = 15.0) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "X-Termini-Version": "1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, (json.loads(raw) if raw else None)
            except json.JSONDecodeError:
                # The install artifacts are plain files, not JSON.
                return r.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def mint(controller: str, project_token: str, count: int) -> list[str]:
    url = controller.rstrip("/") + "/v1/bootstrap-tokens"
    try:
        status, body = http("POST", url, body={"count": count}, token=project_token)
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        die(EXIT_UNREACHABLE,
            f"cannot reach the controller at {url}: {e}\n"
            f"  This helper must run ON the controller host: minting is restricted\n"
            f"  to a direct loopback client.")
    if status == 403:
        die(EXIT_REFUSED,
            f"the controller refused the mint (HTTP 403): {body}\n"
            f"  Minting is restricted to a DIRECT loopback connection carrying the\n"
            f"  project token. A proxied request (X-Forwarded-For and friends) is\n"
            f"  treated as remote even when its TCP peer is 127.0.0.1 — run this\n"
            f"  helper on the controller host, not through the public URL.")
    if status == 404:
        die(EXIT_REFUSED,
            f"the controller has no /v1/bootstrap-tokens route (HTTP 404): {body}\n"
            f"  The deployed controller predates the minting route; deploy a current\n"
            f"  controller (infra/gcp/tools/deploy-termini-tree.sh) and retry.")
    if status != 201 or not isinstance(body, dict):
        die(EXIT_REFUSED, f"the controller refused the mint (HTTP {status}): {body}")
    tokens = body.get("tokens")
    if not isinstance(tokens, list) or not all(isinstance(t, str) and t for t in tokens):
        die(EXIT_REFUSED, f"the controller returned no usable credentials: {body}")
    return tokens


def preflight_install_surface(public_url: str, count: int) -> None:
    """The customer command must point at an install artifact that really exists."""
    url = public_url.rstrip("/") + INSTALL_PATH
    try:
        status, _ = http("GET", url)
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        die(EXIT_UNREACHABLE,
            f"the public install surface is not reachable: {url}: {e}\n"
            f"  The customer cannot fetch the installer from a URL that does not\n"
            f"  answer. Check --public-url (it must be reachable from YOUR customer's\n"
            f"  machine, not just from here) and that the controller is running.")
    if status != 200:
        die(EXIT_UNREACHABLE,
            f"the public install surface answered HTTP {status}: {url}\n"
            f"  Deploy a controller that serves /install/ (infra/gcp/tools/deploy-termini-tree.sh)\n"
            f"  before handing out enrollment credentials.")
    print(f"[issue-enrollment] install surface OK: {url} -> HTTP {status} "
          f"({count} credential(s) to hand over)")


# --------------------------------------------------------------------------- handover
def handover_block(token: str, public_url: str, name: str, index: int, count: int) -> str:
    node = name if count == 1 else f"{name}-{index}"
    return (
        f"--- Termini node enrollment {index}/{count} ---\n"
        f"credential (single-use, enrols ONE node; NOT the controller's project token):\n"
        f"  {token}\n"
        f"\n"
        f"Command for the customer — run it ON the machine that becomes the node,\n"
        f"as root (it installs the node and registers it):\n"
        f"\n"
        f"  curl -fsSL {public_url}{INSTALL_PATH} | sudo sh -s -- \\\n"
        f"      --controller {public_url} --token {token} --name {node}\n"
        f"\n"
        f"Re-running that command on the SAME machine reuses its node identity and\n"
        f"does not consume another credential. This credential is spent by the first\n"
        f"node that registers with it.\n"
    )


def main(argv: list[str]) -> int:
    public_url = os.environ.get("TERMINI_PUBLIC_URL", "")
    controller = os.environ.get("TERMINI_CONTROLLER_INTERNAL", DEFAULT_CONTROLLER)
    token_file = os.environ.get("TERMINI_PROJECT_TOKEN_FILE", "")
    unit = DEFAULT_UNIT
    count = 1
    name = os.environ.get("TERMINI_NODE_NAME", "node")
    out = ""
    skip_preflight = False

    args = argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(USAGE, end="")
            return 0
        if a == "--public-url" and i + 1 < len(args):
            public_url, i = args[i + 1], i + 2
        elif a == "--controller" and i + 1 < len(args):
            controller, i = args[i + 1], i + 2
        elif a == "--count" and i + 1 < len(args):
            try:
                count = int(args[i + 1])
            except ValueError:
                die(EXIT_USAGE, "--count must be an integer")
            i += 2
        elif a == "--name" and i + 1 < len(args):
            name, i = args[i + 1], i + 2
        elif a == "--project-token-file" and i + 1 < len(args):
            token_file, i = args[i + 1], i + 2
        elif a == "--from-unit":
            unit, i = DEFAULT_UNIT, i + 1
            if i < len(args) and not args[i].startswith("-"):
                unit, i = args[i], i + 1
        elif a == "--out" and i + 1 < len(args):
            out, i = args[i + 1], i + 2
        elif a == "--no-out":
            out, i = "-", i + 1
        elif a == "--skip-preflight":
            skip_preflight, i = True, i + 1
        else:
            print(f"issue-enrollment: unknown argument: {a}", file=sys.stderr)
            print(USAGE, end="", file=sys.stderr)
            return EXIT_USAGE

    if not public_url:
        die(EXIT_USAGE, "--public-url is required: the URL your CUSTOMER will reach "
                        "the controller on (e.g. https://34-93-190-64.sslip.io).")
    public_url = public_url.rstrip("/")
    if not (1 <= count <= 32):
        die(EXIT_USAGE, "--count must be in [1, 32]")

    project_token = read_project_token(token_file or None, unit)

    if not skip_preflight:
        preflight_install_surface(public_url, count)

    tokens = mint(controller, project_token, count)
    blocks = [handover_block(t, public_url, name, n + 1, len(tokens))
              for n, t in enumerate(tokens)]
    text = "\n".join(blocks)

    # Guardrail (card t_70bd0f2b): the project token must never reach a client
    # artifact. Re-read our OWN output rather than trusting the call sites.
    if project_token in text or any(t == project_token for t in tokens):
        die(EXIT_REFUSED,
            "REFUSING to hand over: the project token leaked into the client "
            "artifact. The minted credential is identical to the project token — "
            "do not send this output to anyone.")

    if out != "-":
        if not out:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            out = os.path.join(os.path.expanduser("~"), f"termini-enrollment-{stamp}.txt")
        try:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(out, 0o600)
        except OSError as e:
            die(EXIT_REFUSED, f"cannot write the handover file {out}: {e}")
        with open(out, encoding="utf-8") as fh:
            written = fh.read()
        if project_token in written:
            os.remove(out)
            die(EXIT_REFUSED, f"REFUSING to hand over: project token found in {out}")

    print(text)
    if out != "-":
        print(f"[issue-enrollment] handover written 0600 to {out}")
    print("[issue-enrollment] verified: the controller's project token appears nowhere "
          "in this handover.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
