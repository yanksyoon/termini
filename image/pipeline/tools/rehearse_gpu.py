#!/usr/bin/env python3
"""rehearse_gpu.py — exercise tools/gpu_check.py without a GPU.

Builds a stub fixture root (a fake `nvidia-smi`, fake /dev nodes and, when a C
compiler is available, a stub libcudart) and asserts the verifier's verdicts and
failure reasons across the interesting cases:

  ok            smi reports an L4 + device nodes open + CUDA call succeeds -> pass
  absent        no nvidia-smi on the host                    -> fail / nvidia_smi_absent
  nogpu         nvidia-smi works but reports no GPU          -> fail / no_gpu_reported
  nodevnodes    GPU present but /dev/nvidia* missing          -> fail / device_nodes_missing
  cuda-broken*  libcudart returns cudaErrorNoDevice          -> fail / cuda_runtime_unusable
  skip-cudart   device path only, CUDA probe skipped         -> pass
  (* only when a C compiler is available; otherwise skipped)

This validates the *checker*, not a real GPU — the live acceptance run is
`verify-gpu.sh` on Node C. With --write-examples it commits two reports to
../examples/ as evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
GPU_CHECK = os.path.join(SELF_DIR, "gpu_check.py")
EXAMPLES_DIR = os.path.normpath(os.path.join(SELF_DIR, "..", "..", "examples"))

SMI_OK = """#!/usr/bin/env bash
# stub nvidia-smi (GPU verifier rehearsal) — reports one NVIDIA L4
case "${1:-}" in
  -L) echo "GPU 0: NVIDIA L4 (UUID: GPU-9a1f2c3d-4e5f-6789-abcd-ef0123456789)" ;;
  --version) echo "NVIDIA-SMI 550.127.08   Driver Version: 550.127.08   CUDA Version: 12.4" ;;
  --query-gpu=*) echo "0, NVIDIA L4, 550.127.08, 23034 MiB, 8.9, GPU-9a1f2c3d" ;;
  *) echo "NVIDIA-SMI 550.127.08   Driver Version: 550.127.08   CUDA Version: 12.4" ;;
esac
"""

SMI_NOGPU = """#!/usr/bin/env bash
# stub nvidia-smi — driver present, no GPU attached
case "${1:-}" in
  -L) : ;;
  --query-gpu=*) : ;;
  *) echo "NVIDIA-SMI 550.127.08   Driver Version: 550.127.08   CUDA Version: 12.4" ;;
esac
"""

CUDART_STUB = r"""
#include <stddef.h>
typedef int cudaError_t;
int cudaGetDeviceCount(int *c) {
#ifdef STUB_BROKEN
  *c = 0; return 100; /* cudaErrorNoDevice */
#else
  *c = 1; return 0;
#endif
}
int cudaSetDevice(int d) { (void)d; return 0; }
int cudaMemGetInfo(size_t *freeBytes, size_t *total) {
#ifdef STUB_BROKEN
  (void)freeBytes; (void)total; return 100;
#else
  *freeBytes = (size_t)20 * 1024 * 1024 * 1024;
  *total = (size_t)22 * 1024 * 1024 * 1024;
  return 0;
#endif
}
int cudaStreamCreate(void **s) { *s = (void *)1; return 0; }
int cudaStreamDestroy(void *s) { (void)s; return 0; }
const char *cudaGetErrorString(int e) { (void)e; return "stub"; }
"""


def write_exec(path, body):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def build_fixture(root):
    """Return a dict of fixture paths."""
    bin_dir = os.path.join(root, "bin")
    dev_dir = os.path.join(root, "dev")
    dev_empty = os.path.join(root, "dev-empty")
    os.makedirs(bin_dir, exist_ok=True)
    os.makedirs(dev_dir, exist_ok=True)
    os.makedirs(dev_empty, exist_ok=True)

    smi_ok = os.path.join(bin_dir, "nvidia-smi")
    smi_nogpu = os.path.join(bin_dir, "nvidia-smi-nogpu")
    write_exec(smi_ok, SMI_OK)
    write_exec(smi_nogpu, SMI_NOGPU)

    for name in ("nvidiactl", "nvidia0", "nvidia-uvm", "nvidia-modeset"):
        with open(os.path.join(dev_dir, name), "wb"):
            pass

    fixture = {
        "smi_ok": smi_ok,
        "smi_nogpu": smi_nogpu,
        "smi_missing": os.path.join(bin_dir, "does-not-exist"),
        "dev": dev_dir,
        "dev_empty": dev_empty,
        "cudart_ok": None,
        "cudart_bad": None,
    }

    cc = shutil.which("cc") or shutil.which("gcc")
    if cc:
        src = os.path.join(root, "cudart_stub.c")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(CUDART_STUB)
        ok_so = os.path.join(bin_dir, "libcudart_ok.so")
        bad_so = os.path.join(bin_dir, "libcudart_bad.so")
        for out, extra in ((ok_so, []), (bad_so, ["-DSTUB_BROKEN"])):
            proc = subprocess.run(
                [cc, "-shared", "-fPIC", "-o", out, src, *extra],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                fixture["cudart_ok" if out == ok_so else "cudart_bad"] = out
            else:
                print(f"[rehearse] warn: could not compile {out}: {proc.stderr.strip()}")
    else:
        print("[rehearse] note: no C compiler; CUDA-runtime cases will be skipped")

    return fixture


def run_case(work, name, extra, expected_verdict, expected_reason):
    report_path = os.path.join(work, f"{name}.json")
    proc = subprocess.run(
        [sys.executable, GPU_CHECK, *extra, "--json", report_path, "--quiet"],
        capture_output=True,
        text=True,
        check=False,
    )
    ok = True
    detail = ""
    try:
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"no report: {exc}", None

    if report.get("verdict") != expected_verdict or report.get("reason") != expected_reason:
        ok = False
        detail = (
            f"got verdict={report.get('verdict')} reason={report.get('reason')}, "
            f"want verdict={expected_verdict} reason={expected_reason}"
        )
    expected_rc = 0 if expected_verdict == "pass" else 1
    if proc.returncode != expected_rc:
        ok = False
        detail = (detail + " " if detail else "") + f"exit={proc.returncode} want={expected_rc}"
    return ok, detail, report


def main(argv=None):
    ap = argparse.ArgumentParser(description="rehearse the Node C GPU verifier")
    ap.add_argument("--write-examples", action="store_true")
    args = ap.parse_args(argv)

    work = tempfile.mkdtemp(prefix="termini-gpu-rehearsal-")
    failures = 0
    reports = {}
    try:
        fx = build_fixture(work)

        cases = [
            # name, extra argv, want verdict, want reason
            ("ok", ["--smi", fx["smi_ok"], "--dev-dir", fx["dev"]], "pass", None),
            ("absent", ["--smi", fx["smi_missing"], "--dev-dir", fx["dev"]], "fail", "nvidia_smi_absent"),
            ("nogpu", ["--smi", fx["smi_nogpu"], "--dev-dir", fx["dev"]], "fail", "no_gpu_reported"),
            ("nodevnodes", ["--smi", fx["smi_ok"], "--dev-dir", fx["dev_empty"]], "fail", "device_nodes_missing"),
            ("skip-cudart", ["--smi", fx["smi_ok"], "--dev-dir", fx["dev"], "--skip-cudart"], "pass", None),
            ("absent-skip-cudart", ["--smi", fx["smi_missing"], "--dev-dir", fx["dev"], "--skip-cudart"], "fail", "nvidia_smi_absent"),
        ]

        # The positive case must exercise the real CUDA-runtime probe when we can
        # build the stub; otherwise fall back to the --skip-cudart variant.
        positive = cases[0]
        if fx["cudart_ok"]:
            positive[1].extend(["--cudart", fx["cudart_ok"]])
            if fx["cudart_bad"]:
                cases.append(
                    ("cuda-broken", ["--smi", fx["smi_ok"], "--dev-dir", fx["dev"], "--cudart", fx["cudart_bad"]],
                     "fail", "cuda_runtime_unusable")
                )
        else:
            positive[1].append("--skip-cudart")

        print("== GPU verifier rehearsal")
        for name, extra, verdict, reason in cases:
            ok, detail, report = run_case(work, name, extra, verdict, reason)
            status = "PASS" if ok else "FAIL"
            print(f"  {status}  {name}: verdict={verdict} reason={reason or '-'} {detail}")
            if not ok:
                failures += 1
            reports[name] = report

        if args.write_examples:
            os.makedirs(EXAMPLES_DIR, exist_ok=True)
            for name, filename in (("ok", "gpu-check.rehearsal.json"), ("absent", "gpu-check.absent.json")):
                if reports.get(name):
                    dest = os.path.join(EXAMPLES_DIR, filename)
                    with open(dest, "w", encoding="utf-8") as fh:
                        json.dump(reports[name], fh, indent=2, sort_keys=False)
                        fh.write("\n")
                    print(f"[rehearse] wrote {dest}")

        if failures == 0:
            print("\nGPU VERIFIER REHEARSAL: PASS")
        else:
            print(f"\nGPU VERIFIER REHEARSAL: FAIL ({failures} case(s))")
        return 0 if failures == 0 else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
