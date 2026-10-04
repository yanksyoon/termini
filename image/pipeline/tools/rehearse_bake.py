#!/usr/bin/env python3
"""rehearse_bake.py — exercise the image pipeline end-to-end on THIS host.

The real bake (Packer/QEMU + multi-GB GGUF) needs a build host with KVM and
network. This harness validates the *logic* that the bake and the first boot
depend on, with no network and no giant downloads:

  * builds a stub image root ({opt/termini, etc/termini, var/lib/termini/models})
  * runs pipeline/first-boot-preflight.sh against it and asserts it PASSES,
    emitting a boot log that shows the artifacts resolved from local disk
  * removes one model file and asserts the preflight now FAILS (the checks
    are real, not decorative)
  * emits a rehearsal image-manifest.json and validates it against
    manifests/image-manifest.schema.json with the same validator the build uses

Exit 0 only if every assertion holds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
IMAGE_DIR = HERE.parents[2]
PIPELINE_DIR = IMAGE_DIR / "pipeline"
LOCK = IMAGE_DIR / "models.lock.json"
SCHEMA = IMAGE_DIR / "manifests" / "image-manifest.schema.json"
PREFLIGHT = PIPELINE_DIR / "first-boot-preflight.sh"
VERIFY = PIPELINE_DIR / "tools" / "verify_manifest.py"
EXAMPLES = IMAGE_DIR / "examples"

STUB_PAD = 4096


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_stub_model(root: Path, alias: str, models_dir: Path) -> Path:
    path = models_dir / f"stub-{alias}.gguf"
    blob = b"GGUF" + alias.encode() + b"\0" * max(0, STUB_PAD - 4 - len(alias))
    path.write_bytes(blob)
    return path


def build_stub_root(tmp: Path, real_lock: dict) -> tuple[Path, dict]:
    root = tmp / "root"
    models_dir = root / "var/lib/termini/models"
    (root / "opt/termini").mkdir(parents=True)
    (root / "etc/termini").mkdir(parents=True)
    (root / "var/lib/termini").mkdir(parents=True)
    models_dir.mkdir(parents=True)

    stub_lock = json.loads(json.dumps(real_lock))  # deep copy
    for alias, spec in stub_lock["aliases"].items():
        stub = write_stub_model(root, alias, models_dir)
        spec["file"] = stub.name
        spec["size_bytes"] = stub.stat().st_size
        spec["sha256"] = sha256_file(stub)
        spec["url"] = f"stub://rehearsal/{alias}.gguf"
        dest = models_dir / stub.name
        (models_dir / f"{stub.name}.sha256").write_text(f"{spec['sha256']}  {dest}\n", encoding="utf-8")

    (root / "opt/termini/models.lock.json").write_text(json.dumps(stub_lock, indent=2), encoding="utf-8")

    server = root / "opt/termini/llama.cpp/bin/llama-server"
    server.parent.mkdir(parents=True)
    server.write_text("#!/bin/sh\necho 'stub llama-server'\n", encoding="utf-8")
    server.chmod(0o755)
    (root / "opt/termini/llama.cpp/bin/llama-cli").write_text("#!/bin/sh\n", encoding="utf-8")

    (root / "etc/termini/llama.cpp-build.txt").write_text(
        f"commit={real_lock['llamacpp']['commit']}\nvariant=compute\ncuda=OFF\n", encoding="utf-8")
    (root / "etc/termini/image-version").write_text("termini-node-rehearsal\n", encoding="utf-8")
    (root / "var/lib/termini/.baked-offline").write_text("rehearsal sentinel\n", encoding="utf-8")
    return root, stub_lock


def run_preflight(root: Path, log_out: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PREFLIGHT_TS"] = "rehearsal"
    return subprocess.run(
        ["bash", str(PREFLIGHT), "--root", str(root), "--log-out", str(log_out),
         "--verify-sha", "--allow-stub"],
        capture_output=True, text=True, env=env, check=False,
    )


def build_manifest(tmp: Path, real_lock: dict, alias: str, version: str, variant: str) -> Path:
    payload = tmp / "root"
    tarball = tmp / f"rehearsal-{variant}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(payload, arcname=".")
    size = tarball.stat().st_size
    sha = sha256_file(tarball)
    doc = {
        "schema_version": "1.0",
        "alias": alias,
        "version": version,
        "immutable_tag": f"agentic-os-w3-{version}+gdeadbeefcafe",
        "variant": variant,
        "gitsha": "deadbeefcafe",
        "built_at": "2026-10-04T00:00:00Z",
        "base_image": {
            "name": "ubuntu-24.04-server-cloudimg-amd64", "release": "20260911",
            "arch": "amd64",
            "url": "https://cloud-images.ubuntu.com/noble/20260911/noble-server-cloudimg-amd64.img",
            "sha256": "612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354",
        },
        "artifacts": [{
            "name": tarball.name,
            "format": "lxd-image",
            "path": str(tarball),
            "size_bytes": size,
            "sha256": sha,
        }],
        "pins": {
            "llamacpp_commit": real_lock["llamacpp"]["commit"],
            "lxd_snap_channel": "5.21/stable",
            "lxd_snap_revision": None,
            "models": {a: real_lock["aliases"][a]["sha256"] for a in real_lock["aliases"]},
        },
        "contents": {
            "llama_server_path": "/opt/termini/llama.cpp/bin/llama-server",
            "model_root": real_lock["model_root_in_image"],
            "agent_vm_alias": "agent-vm/2026.10.0",
            "bootstrap_unit": "termini-bootstrap.service",
        },
        "first_boot": {
            "preflight_unit": "termini-firstboot-preflight.service",
            "boot_log_path": "/var/log/termini/first-boot.json",
            "network_required": False,
        },
    }
    out = tmp / "image-manifest.json"
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rehearse the Termini image pipeline locally")
    ap.add_argument("--write-examples", action="store_true",
                    help="also write image/examples/boot-log.rehearsal.json + image-manifest.rehearsal.json")
    ap.add_argument("--keep", action="store_true", help="keep the temp workspace")
    args = ap.parse_args(argv)

    real_lock = json.loads(LOCK.read_text(encoding="utf-8"))
    failures: list[str] = []

    tmp = Path(tempfile.mkdtemp(prefix="termini-rehearsal-"))
    try:
        root, stub_lock = build_stub_root(tmp, real_lock)
        log = tmp / "first-boot.json"

        # 1. positive case
        pos = run_preflight(root, log)
        print(pos.stdout.strip())
        if pos.returncode != 0:
            failures.append(f"preflight failed on a complete stub root (rc={pos.returncode})")
        else:
            doc = json.loads(log.read_text(encoding="utf-8"))
            if doc.get("verdict") != "pass":
                failures.append("boot log verdict != pass on complete root")
            if doc.get("network_downloads_at_boot") != 0:
                failures.append("boot log reported network downloads at boot")
            if not any(m["alias"] == "deepseek-flash" for m in doc.get("models", [])):
                failures.append("boot log did not resolve deepseek-flash from local disk")
            print(f"[rehearse] positive: verdict={doc.get('verdict')} "
                  f"models={[m['alias'] for m in doc.get('models', [])]}")
            if args.write_examples:
                EXAMPLES.mkdir(parents=True, exist_ok=True)
                (EXAMPLES / "boot-log.rehearsal.json").write_text(
                    json.dumps(doc, indent=2) + "\n", encoding="utf-8")

        # 2. negative case: the checks must actually catch a missing artifact
        missing = root / "var/lib/termini/models" / stub_lock["aliases"]["deepseek-flash"]["file"]
        missing.unlink()
        neg = run_preflight(root, tmp / "first-boot.fail.json")
        if neg.returncode == 0:
            failures.append("preflight PASSED with a model file deleted (checks are not real)")
        else:
            fdoc = json.loads((tmp / "first-boot.fail.json").read_text(encoding="utf-8"))
            failed_checks = [c["name"] for c in fdoc["checks"] if not c["ok"]]
            print(f"[rehearse] negative (as expected): verdict={fdoc['verdict']} failed_checks={failed_checks}")
            if "model_files_present" not in failed_checks:
                failures.append("negative case did not flag model_files_present")

        # 3. manifest schema validation
        manifest = build_manifest(tmp, real_lock, "agentic-os/w3", "2026.10.0", "compute")
        val = subprocess.run(
            [sys.executable, str(VERIFY), "--lock", str(LOCK), "--schema", str(SCHEMA),
             "--manifest", str(manifest)],
            capture_output=True, text=True, check=False,
        )
        print(val.stdout.strip())
        if val.returncode != 0:
            failures.append("rehearsal manifest failed schema validation:\n" + val.stderr.strip())
        elif args.write_examples:
            EXAMPLES.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(manifest, EXAMPLES / "image-manifest.rehearsal.json")
    finally:
        if args.keep:
            print(f"[rehearse] workspace kept at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"REHEARSE FAIL: {f}", file=sys.stderr)
        return 1
    print("REHEARSE OK: preflight passes on a complete root, fails on a missing model, manifest validates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
