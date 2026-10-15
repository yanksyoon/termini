#!/usr/bin/env python3
"""Postcondition checks for Node C's first-boot inference front door (t_359ed606).

WHAT THIS PINS
--------------
Node C's first boot must install the inference front door with no manual step,
and it must do so by CALLING `infra/inference/install-inference-proxy.sh` — the
single source of truth for the proxy unit, the worker unit and their env files —
instead of carrying a copy of its heredocs. A rendered cloud-init user-data is
therefore expected to contain, ON NODE C ONLY:

  * `/usr/local/sbin/termini-llama-serve.sh` — the first-boot script, with every
    `__TOKEN__` resolved (backend / port / alias explicit) and exactly ONE
    `ExecStart=` (the intent-extraction instance: the decision instance is the
    installer's loopback worker now, not a heredoc here);
  * the front-door PAYLOAD at `/usr/local/src/termini-inference/…`, base64
    encoded, whose DECODED bytes must equal the committed repo files.

Used by `tests/test_node_c_first_boot.py` (every PR, via the shell renderer) and
by `infra/gcp/verify-cloud-init.sh` (the Terraform render). The rendering side is
`infra/gcp/cloud-init/node-c-serve-block.sh` for the gcloud paths and the
`infra/gcp/userdata` module for Terraform.

CLI:

    python3 infra/gcp/cloud-init/node_c_front_door_check.py <user-data.yaml> ...
        [--expect none]     # a node that must carry NO serve block (node-a/b)

Exit 0 = every check passed. Nothing is prettified: failures are printed raw.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import pathlib
import re
import sys

# Destination on the node -> (source file relative to the repo root, mode).
FRONT_DOOR_PAYLOAD = [
    ("/usr/local/src/termini-inference/install-inference-proxy.sh",
     "infra/inference/install-inference-proxy.sh", "0755"),
    ("/usr/local/src/termini-inference/proxy.py",
     "infra/inference/proxy.py", "0644"),
    ("/usr/local/src/termini-inference/termini-inference-proxy.service",
     "infra/inference/termini-inference-proxy.service", "0644"),
    ("/usr/local/src/termini-inference/termini-inference-switch.sh",
     "infra/inference/termini-inference-switch.sh", "0755"),
]
SERVE_SCRIPT = "/usr/local/sbin/termini-llama-serve.sh"

# `- path:` … up to the next entry, the next top-level key, or EOF. Matches the
# exact shape node-c-serve-block.sh and the userdata module emit; the regex is
# intentionally strict so a format change shows up as a failure instead of a
# silently skipped entry.
_ENTRY_RE = re.compile(
    r"^  - path: (?P<path>\S+)\n"
    r"(?P<attrs>(?:    \S.*\n)*)",
    re.MULTILINE,
)
_ATTR_RE = re.compile(r"^    (?P<key>[a-z_]+): (?P<value>.*)$", re.MULTILINE)


def entries(text: str) -> dict[str, dict[str, str]]:
    """Map destination path -> {raw attribute block}, for every write_files entry."""
    out: dict[str, dict[str, str]] = {}
    for m in _ENTRY_RE.finditer(text):
        attrs = {}
        for am in _ATTR_RE.finditer(m.group("attrs")):
            attrs[am.group("key")] = am.group("value")
        out[m.group("path")] = attrs
    return out


def block_scalar(text: str, path: str) -> str:
    """Return the literal (`content: |`) body of an entry, dedented."""
    m = re.search(rf"^  - path: {re.escape(path)}\n((?:    .*\n|\n)*)", text, re.MULTILINE)
    if not m:
        return ""
    body = []
    started = False
    for line in m.group(1).splitlines():
        if line.startswith("    content: |"):
            started = True
            continue
        if not started:
            continue
        if line.startswith("      "):
            body.append(line[6:])
        elif line.strip() == "":
            body.append("")
        else:
            break
    return "\n".join(body)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_user_data(text: str, repo_root: pathlib.Path, *, backend: str = "gemini-proxy",
                    alias: str = "gemma", port: int = 8080) -> list[str]:
    """Return a list of failure strings ([] == this user-data is correct)."""
    fail: list[str] = []
    found = entries(text)

    # --- the front-door payload: exact bytes, exact destination, exact mode --- #
    for dest, rel, mode in FRONT_DOOR_PAYLOAD:
        e = found.get(dest)
        if e is None:
            fail.append(f"missing write_files entry: {dest}")
            continue
        if e.get("permissions", "").strip("'\"") != mode:
            fail.append(f"{dest}: permissions {e.get('permissions')!r} != {mode!r}")
        if e.get("encoding", "").strip("'\"") not in ("b64", "base64"):
            fail.append(f"{dest}: encoding {e.get('encoding')!r} is not base64")
            continue
        raw = e.get("content", "").strip().strip('"')
        try:
            got = base64.b64decode(raw, validate=True)
        except Exception as exc:  # noqa: BLE001 - report the raw decode error
            fail.append(f"{dest}: base64 decode failed: {exc}")
            continue
        want = (repo_root / rel).read_bytes()
        if sha256(got) != sha256(want):
            fail.append(f"{dest}: decoded sha256 {sha256(got)[:16]} != repo {rel} "
                        f"{sha256(want)[:16]}")

    # --- the first-boot script: tokens resolved, and it CALLS the installer --- #
    script = block_scalar(text, SERVE_SCRIPT)
    if not script:
        fail.append(f"missing write_files entry: {SERVE_SCRIPT}")
        return fail
    for needle in (f'BACKEND="{backend}"', f'PROXY_PORT="{port}"', f'MODEL_ALIAS="{alias}"',
                   "FRONT_DOOR_SRC", "install-inference-proxy.sh", "--backend \"$BACKEND\""):
        if needle not in script:
            fail.append(f"{SERVE_SCRIPT}: missing {needle!r}")
    for bad in re.findall(r"__[A-Z][A-Z_]*__", script):
        fail.append(f"{SERVE_SCRIPT}: unresolved placeholder {bad}")
    execs = [ln for ln in script.splitlines() if ln.startswith("ExecStart=")]
    if len(execs) != 1:
        fail.append(f"{SERVE_SCRIPT}: expected exactly 1 ExecStart= (the intent instance, "
                    f"the decision instance belongs to install-inference-proxy.sh), got {len(execs)}")
    if "termini-llama-server.service <<UNIT" in script:
        fail.append(f"{SERVE_SCRIPT}: still carries a COPY of the worker unit heredoc — "
                    f"install-inference-proxy.sh must stay its single source of truth")
    if len(script.encode()) > 20_000:
        fail.append(f"{SERVE_SCRIPT}: body is {len(script.encode())} bytes — the front-door "
                    f"payload must live in its own base64 entries, not inlined here")
    return fail


def check_no_serve_block(text: str) -> list[str]:
    """Return failures for a node that must carry NO Node C serve block at all."""
    fail = []
    for needle in (SERVE_SCRIPT, *[dest for dest, _, _ in FRONT_DOOR_PAYLOAD]):
        if needle in text:
            fail.append(f"unexpected on a non-inference node: {needle}")
    return fail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("user_data", nargs="+", help="rendered cloud-init user-data YAML file(s)")
    ap.add_argument("--expect", choices=["front-door", "none"], default="front-door",
                    help="front-door (Node C) or none (node-a/b)")
    ap.add_argument("--repo-root", default=str(pathlib.Path(__file__).resolve().parents[3]))
    ap.add_argument("--backend", default="gemini-proxy")
    ap.add_argument("--alias", default="gemma")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args(argv)

    repo = pathlib.Path(args.repo_root)
    failures = 0
    for path in args.user_data:
        text = pathlib.Path(path).read_text()
        fail = (check_no_serve_block(text) if args.expect == "none"
                else check_user_data(text, repo, backend=args.backend, alias=args.alias,
                                     port=args.port))
        if fail:
            failures += len(fail)
            print(f"FAIL  {path}")
            for f in fail:
                print(f"        {f}")
        else:
            kind = "no serve block" if args.expect == "none" else "front door + payload"
            print(f"PASS  {path} ({kind}, {len(text)} bytes)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
