#!/usr/bin/env python3
"""gpu_check.py — prove the Node C GPU is visible AND usable from this process.

Acceptance probe for kanban task t_4ccb8ea7 ("GPU enablement on Node C"). It
answers two questions with machine-readable evidence, from the host process:

  1. does the host see the GPU?   nvidia-smi -L / --query-gpu
  2. is the device usable?        open /dev/nvidia* and make a real CUDA runtime
                                  call through libcudart (ctypes)

No third-party dependencies. Writes a JSON report and exits 0 (pass) / 1 (fail)
/ 2 (usage).

This is a *diagnostic*. Per docs/contracts/node-bootstrap.md §9 a GPU/driver
failure must NOT block registration, so nothing gates boot on this; the node
registers with gpus: [] and DEGRADED.

Testability: --smi, --dev-dir and --cudart let the offline rehearsal
(tools/rehearse_gpu.py) drive every code path without a GPU. A --dev-dir other
than /dev is treated as a fixture, so the character-device requirement is not
enforced against stub files.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import glob
import json
import os
import platform
import shutil
import stat
import subprocess
import sys

SCHEMA_VERSION = "1.0"
KIND = "termini.gpu_check"

# Device nodes the CUDA runtime needs. ctl+0 are the hard requirement to talk to
# the driver; nvidia-uvm is what CUDA (and therefore llama.cpp) needs for unified
# memory / stream work.
REQUIRED_NODES = ("nvidiactl", "nvidia0")
CUDA_NODES = ("nvidia-uvm",)
OPTIONAL_NODES = ("nvidia-modeset", "nvidia-uvm-tools")

CUDART_GLOBS = (
    "/usr/local/cuda*/targets/*/lib/libcudart.so*",
    "/usr/local/cuda*/lib64/libcudart.so*",
    "/opt/termini/cuda*/lib*/libcudart.so*",
    "/usr/lib/x86_64-linux-gnu/libcudart.so*",
    "/usr/lib/aarch64-linux-gnu/libcudart.so*",
)

CUDA_SUCCESS = 0


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd, timeout=15):
    """Return (returncode, stdout, stderr). Never raises for a missing binary."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def parse_smi_list(text):
    """Parse `nvidia-smi -L` lines: 'GPU 0: NVIDIA L4 (UUID: GPU-abc...)'."""
    gpus = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("GPU "):
            continue
        head, _, rest = line.partition(":")
        try:
            index = int(head.split()[1])
        except (IndexError, ValueError):
            index = len(gpus)
        name = rest.strip()
        uuid = None
        if "(" in name and name.endswith(")"):
            name, _, tail = name.partition("(")
            name = name.strip()
            uuid = tail[:-1].strip()
        gpus.append({"index": index, "name": name, "uuid": uuid})
    return gpus


def parse_query_csv(text):
    """Parse `--query-gpu=index,name,driver_version,memory.total,compute_cap,uuid`."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("index"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        rows.append(
            {
                "index": parts[0],
                "name": parts[1],
                "driver_version": parts[2],
                "memory_total": parts[3],
                "compute_cap": parts[4] if len(parts) > 4 else None,
                "uuid": parts[5] if len(parts) > 5 else None,
            }
        )
    return rows


def parse_smi_version(text):
    """Pull the driver + CUDA version out of `nvidia-smi --version` / header."""
    driver = cuda = None
    for token_line in text.splitlines():
        if "Driver Version:" in token_line:
            driver = token_line.split("Driver Version:", 1)[1].strip().split()[0]
        if "CUDA Version:" in token_line:
            cuda = token_line.split("CUDA Version:", 1)[1].strip()
    return driver, cuda


def inspect_device_nodes(dev_dir):
    """Return (nodes, missing_required, missing_cuda). Each node is a dict."""
    nodes = []
    missing_required = []
    missing_cuda = []
    for name in REQUIRED_NODES + CUDA_NODES + OPTIONAL_NODES:
        path = os.path.join(dev_dir, name)
        entry = {"path": path, "name": name, "exists": os.path.exists(path)}
        if entry["exists"]:
            st = os.stat(path)
            entry["is_char"] = stat.S_ISCHR(st.st_mode)
            entry["mode"] = oct(stat.S_IMODE(st.st_mode))
            try:
                fd = os.open(path, os.O_RDWR)
                os.close(fd)
                entry["openable"] = True
                entry["open_error"] = None
            except OSError as exc:
                entry["openable"] = False
                entry["open_error"] = f"{exc.__class__.__name__}: {exc}"
        else:
            entry["is_char"] = False
            entry["openable"] = False
            entry["open_error"] = None
            if name in REQUIRED_NODES:
                missing_required.append(name)
            if name in CUDA_NODES:
                missing_cuda.append(name)
        nodes.append(entry)
    return nodes, missing_required, missing_cuda


def find_cudart(explicit=None):
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for pattern in CUDART_GLOBS:
        for path in sorted(glob.glob(pattern)):
            if os.path.exists(path):
                return path
    return None


def probe_cuda_runtime(lib_path, min_gpus):
    """Call libcudart from THIS process. Returns a dict; never raises."""
    result = {
        "path": lib_path,
        "usable": False,
        "device_count": None,
        "mem_free_bytes": None,
        "mem_total_bytes": None,
        "error": None,
    }
    if not lib_path:
        result["error"] = "libcudart not found"
        return result
    try:
        lib = ctypes.CDLL(lib_path)
    except OSError as exc:
        result["error"] = f"dlopen failed: {exc}"
        return result
    try:
        count = ctypes.c_int(-1)
        lib.cudaGetDeviceCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
        lib.cudaGetDeviceCount.restype = ctypes.c_int
        rc = lib.cudaGetDeviceCount(ctypes.byref(count))
        if rc != CUDA_SUCCESS:
            result["error"] = f"cudaGetDeviceCount -> cudaError {rc}"
            return result
        result["device_count"] = count.value
        if count.value < min_gpus:
            result["error"] = f"device_count {count.value} < required {min_gpus}"
            return result

        lib.cudaSetDevice.argtypes = [ctypes.c_int]
        lib.cudaSetDevice.restype = ctypes.c_int
        rc = lib.cudaSetDevice(0)
        if rc != CUDA_SUCCESS:
            result["error"] = f"cudaSetDevice(0) -> cudaError {rc}"
            return result

        free_b = ctypes.c_size_t(0)
        total_b = ctypes.c_size_t(0)
        lib.cudaMemGetInfo.argtypes = [
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t),
        ]
        lib.cudaMemGetInfo.restype = ctypes.c_int
        rc = lib.cudaMemGetInfo(ctypes.byref(free_b), ctypes.byref(total_b))
        if rc != CUDA_SUCCESS:
            result["error"] = f"cudaMemGetInfo -> cudaError {rc}"
            return result
        result["mem_free_bytes"] = free_b.value
        result["mem_total_bytes"] = total_b.value

        stream = ctypes.c_void_p()
        lib.cudaStreamCreate.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.cudaStreamCreate.restype = ctypes.c_int
        rc = lib.cudaStreamCreate(ctypes.byref(stream))
        if rc != CUDA_SUCCESS:
            result["error"] = f"cudaStreamCreate -> cudaError {rc}"
            return result
        lib.cudaStreamDestroy.argtypes = [ctypes.c_void_p]
        lib.cudaStreamDestroy.restype = ctypes.c_int
        lib.cudaStreamDestroy(stream)

        result["usable"] = True
        return result
    except AttributeError as exc:
        result["error"] = f"libcudart missing expected symbol: {exc}"
        return result
    except Exception as exc:  # noqa: BLE001 - diagnostic, report don't crash
        result["error"] = f"{exc.__class__.__name__}: {exc}"
        return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Node C GPU presence + CUDA usability check")
    ap.add_argument("--smi", default="nvidia-smi", help="path to nvidia-smi")
    ap.add_argument("--dev-dir", default="/dev", help="device root (/dev on a real node)")
    ap.add_argument("--cudart", default=None, help="explicit libcudart path")
    ap.add_argument("--skip-cudart", action="store_true", help="do not require a CUDA runtime probe")
    ap.add_argument("--min-gpus", type=int, default=1)
    ap.add_argument("--json", default=None, help="write the JSON report here")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    fixture = os.path.realpath(args.dev_dir) != "/dev"
    checks = []
    required = set()

    def check(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    # 1. nvidia-smi present ---------------------------------------------------
    smi_path = args.smi if os.path.isabs(args.smi) else (shutil.which(args.smi) or "")
    smi_present = bool(smi_path) and os.path.exists(smi_path) and os.access(smi_path, os.X_OK)
    check("nvidia_smi_present", smi_present, f"path={smi_path or args.smi}")
    required.add("nvidia_smi_present")

    smi_version = {"driver_version": None, "cuda_version": None}
    gpus = []
    query = []
    if smi_present:
        _, ver_out, _ = run([smi_path, "--version"])
        if not ver_out:
            _, ver_out, _ = run([smi_path])
        dv, cv = parse_smi_version(ver_out)
        smi_version = {"driver_version": dv, "cuda_version": cv}
        _, list_out, list_err = run([smi_path, "-L"])
        gpus = parse_smi_list(list_out)
        _, query_out, _ = run(
            [
                smi_path,
                "--query-gpu=index,name,driver_version,memory.total,compute_cap,uuid",
                "--format=csv,noheader",
            ]
        )
        query = parse_query_csv(query_out)
    else:
        list_err = "nvidia-smi not found"

    # 2. GPU reported ---------------------------------------------------------
    check(
        "gpu_reported",
        len(gpus) >= args.min_gpus,
        f"count={len(gpus)} required={args.min_gpus}"
        + ("" if gpus else f" (smi stderr: {list_err or 'none'})"),
    )
    required.add("gpu_reported")

    # 3. device nodes ---------------------------------------------------------
    nodes, missing_required, missing_cuda = inspect_device_nodes(args.dev_dir)
    device_nodes_ok = not missing_required
    check(
        "device_nodes",
        device_nodes_ok,
        f"dev_dir={args.dev_dir}"
        + (f" missing={missing_required}" if missing_required else " ctl+0 present"),
    )
    required.add("device_nodes")

    # 4. device nodes openable from this process ------------------------------
    required_nodes = [n for n in nodes if n["name"] in REQUIRED_NODES]
    openable = all(n.get("openable") for n in required_nodes) and bool(required_nodes)
    check(
        "device_openable",
        openable,
        "; ".join(
            f"{n['path']}={'ok' if n.get('openable') else (n.get('open_error') or 'missing')}"
            for n in required_nodes
        ),
    )
    required.add("device_openable")

    if not fixture and device_nodes_ok:
        non_char = [n["name"] for n in required_nodes if not n.get("is_char")]
        check(
            "device_nodes_char",
            not non_char,
            "all required nodes are character devices" if not non_char else f"not char: {non_char}",
        )
        required.add("device_nodes_char")

    # 5. CUDA runtime usable from this process --------------------------------
    cudart = find_cudart(args.cudart)
    if args.skip_cudart:
        cuda = {"path": cudart, "usable": None, "error": "skipped (--skip-cudart)"}
    else:
        cuda = probe_cuda_runtime(cudart, args.min_gpus)
        if not cuda["usable"] and missing_cuda and not cuda.get("error"):
            cuda["error"] = f"missing cuda device node(s): {missing_cuda}"
        check(
            "cuda_runtime",
            cuda["usable"],
            f"lib={cudart or 'not-found'}"
            + (f" device_count={cuda.get('device_count')}" if cuda.get("device_count") is not None else "")
            + (f" error={cuda['error']}" if cuda.get("error") else " ok"),
        )
        required.add("cuda_runtime")

    def _driver_loaded():
        if os.path.isdir("/sys/module/nvidia"):
            return True
        try:
            with open("/proc/modules", encoding="utf-8", errors="ignore") as fh:
                return any(line.startswith("nvidia ") for line in fh)
        except OSError:
            return False

    driver_loaded = _driver_loaded()

    failures = [c for c in checks if c["name"] in required and not c["ok"]]

    reason_map = {
        "nvidia_smi_present": "nvidia_smi_absent",
        "gpu_reported": "no_gpu_reported",
        "device_nodes": "device_nodes_missing",
        "device_openable": "device_not_openable",
        "cuda_runtime": "cuda_runtime_unusable",
        "device_nodes_char": "device_nodes_not_char",
    }
    verdict = "fail" if failures else "pass"
    reason = reason_map.get(failures[0]["name"], failures[0]["name"]) if failures else None

    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "ts": now(),
        "host": platform.node(),
        "kernel": platform.release(),
        "fixture": fixture,
        "smi": {"path": smi_path or args.smi, "present": smi_present, **smi_version},
        "driver": {"loaded": bool(driver_loaded), "branch_recorded": None},
        "gpus": query or gpus,
        "gpu_count": len(gpus),
        "device_nodes": nodes,
        "cudart": cuda,
        "checks": checks,
        "verdict": verdict,
        "reason": reason,
    }

    # Driver branch recorded at bake time, when present (informational).
    record = "/etc/termini/nvidia-driver.txt"
    if os.path.exists(record):
        try:
            with open(record, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("branch="):
                        report["driver"]["branch_recorded"] = line.strip().split("=", 1)[1]
        except OSError:
            pass

    if args.json:
        out_dir = os.path.dirname(os.path.abspath(args.json))
        os.makedirs(out_dir, exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=False)
            fh.write("\n")

    if not args.quiet:
        print(f"[gpu-check] verdict={verdict} reason={reason or '-'}")
        for c in checks:
            print(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['name']}: {c['detail']}")
        for g in (query or gpus):
            print(f"  gpu  {g.get('index')}: {g.get('name')} driver={g.get('driver_version')} mem={g.get('memory_total')}")
        if args.json:
            print(f"[gpu-check] report -> {args.json}")

    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
