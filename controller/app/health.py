"""Node status / health derivation (dashboard.md §1.1–§1.3, frozen).

Pure functions: given a node row and its latest telemetry sample (or ``None``
when the node has never reported), derive the dashboard-facing ``status``,
``health`` object, ``uptime_sec``, and the ``degraded_reasons`` enum list. No
I/O, no DB access — trivially unit-testable.

The frozen rules (dashboard.md §1.3) are reproduced verbatim in the docstrings
below; this module is the single implementation.
"""
from __future__ import annotations

from typing import Any

# dashboard.md §1.3 — the only services the dashboard surfaces (allowlist).
ALLOWLISTED_SERVICES = ("termini-bootstrap", "lxd", "sshd", "llama-cpp")

# dashboard.md §1.1 — status derivation thresholds (seconds).
OFFLINE_AFTER_S = 45
DEGRADED_AFTER_S = 15

# dashboard.md §1.3 — GPU overtemp threshold (°C).
GPU_OVERTEMP_C = 85

# dashboard.md §1.3 — disk write latency flag threshold (ms).
DISK_DEGRADING_LATENCY_MS = 50

GiB = 1024 ** 3

# controller-api.md §5.1 — frozen per-node capacity-accounting parameters.
CPU_OVERCOMMIT_RATIO = 2.0
CPU_RESERVE_RATIO = 0.10
RAM_RESERVE_GB = 2
DISK_RESERVE_RATIO = 0.10

# controller-api.md §5.2 step 6 — agent_vm type floors below which a capacity
# clamp becomes a rejection (ram 1 GiB, vcpus 1, disk 4 GiB; FLOORS in
# termini/intent/types.py).
AGENT_VM_MIN_RAM_GB = 1
AGENT_VM_MIN_VCPUS = 1
AGENT_VM_MIN_DISK_GB = 4


def _first(arr: Any) -> dict:
    return arr[0] if isinstance(arr, list) and arr and isinstance(arr[0], dict) else {}


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def health_flags(sample: dict | None, role: str) -> dict:
    """Derive the health object from the latest telemetry sample (§1.3).

    A missing sample (never reported) leaves each flag at its default.
    """
    s = sample or {}
    disk = s.get("disk") if isinstance(s.get("disk"), list) else []
    gpu = s.get("gpu") if isinstance(s.get("gpu"), list) else []
    services = s.get("services") if isinstance(s.get("services"), list) else []
    inference = s.get("inference") if isinstance(s.get("inference"), dict) else {}

    cpu_util = _num(s.get("cpu_util_pct"))
    ram_total = _num(s.get("ram_total_bytes"))
    ram_used = _num(s.get("ram_used_bytes"))
    ram_used_pct = round(100.0 * ram_used / ram_total, 1) if (ram_total and ram_used is not None) else 0.0

    # disk_ok: false if any disk[].mount_ro == true, else true.
    disk_ok = not any(isinstance(d, dict) and d.get("mount_ro") is True for d in disk)

    if role == "inference":
        # gpu_ok: false if gpu[] empty or gpu[0].temp_celsius >= 85, else true.
        g0 = _first(gpu)
        gpu_ok = bool(gpu) and _num(g0.get("temp_celsius")) is not None and (
            (_num(g0.get("temp_celsius")) or 0) < GPU_OVERTEMP_C
        )
        # inference_ok: inference.server_up.
        inference_ok = inference.get("server_up") if "server_up" in inference else None
        if inference_ok is not None:
            inference_ok = bool(inference_ok)
    else:
        gpu_ok = None
        inference_ok = None

    # services[]: passthrough, allowlist only.
    svc = [
        {"name": d["name"], "status": d["status"]}
        for d in services
        if isinstance(d, dict) and d.get("name") in ALLOWLISTED_SERVICES
        and isinstance(d.get("status"), str)
    ]

    return {
        "cpu_util_pct": cpu_util,
        "ram_used_pct": ram_used_pct,
        "disk_ok": disk_ok,
        "gpu_ok": gpu_ok,
        "inference_ok": inference_ok,
        "services": svc,
        "degraded_reasons": [],
    }


def degraded_reasons(sample: dict | None, role: str, last_seen_age_s: float) -> list[str]:
    """Compute the frozen ``degraded_reasons`` enum list (§1.3)."""
    s = sample or {}
    reasons: list[str] = []
    disk = s.get("disk") if isinstance(s.get("disk"), list) else []
    gpu = s.get("gpu") if isinstance(s.get("gpu"), list) else []
    inference = s.get("inference") if isinstance(s.get("inference"), dict) else {}
    services = s.get("services") if isinstance(s.get("services"), list) else []

    # heartbeat_late: last_seen_at between 15s and 45s.
    if DEGRADED_AFTER_S < last_seen_age_s <= OFFLINE_AFTER_S:
        reasons.append("heartbeat_late")

    if any(isinstance(d, dict) and d.get("mount_ro") is True for d in disk):
        reasons.append("disk_read_only")
    if any(isinstance(d, dict) and _num(d.get("write_latency_ms")) is not None
           and _num(d.get("write_latency_ms")) >= DISK_DEGRADING_LATENCY_MS for d in disk):
        reasons.append("disk_degrading")

    if role == "inference":
        g0 = _first(gpu)
        if not gpu:
            reasons.append("gpu_unavailable")
        elif _num(g0.get("temp_celsius")) is not None and _num(g0.get("temp_celsius")) >= GPU_OVERTEMP_C:
            reasons.append("gpu_overtemp")
        if "server_up" in inference and inference.get("server_up") is False:
            reasons.append("inference_down")

    if any(isinstance(d, dict) and d.get("name") in ALLOWLISTED_SERVICES
           and d.get("status") == "failed" for d in services):
        reasons.append("service_failed")

    return reasons


def derive_status(last_seen_age_s: float, reasons: list[str], flags: dict) -> str:
    """status ∈ online | degraded | offline (dashboard.md §1.1, frozen)."""
    if last_seen_age_s > OFFLINE_AFTER_S:
        return "offline"
    if last_seen_age_s > DEGRADED_AFTER_S:
        return "degraded"
    # any health flag false (non-null) OR any allowlisted service not running.
    if flags.get("disk_ok") is False or flags.get("gpu_ok") is False or flags.get("inference_ok") is False:
        return "degraded"
    if any(s.get("status") != "running" for s in flags.get("services", [])):
        return "degraded"
    return "online"


def node_status_health(node: dict, sample: dict | None, now_epoch: float) -> tuple[str, dict]:
    """Return (status, health-object-with-reasons) for a node row + latest sample.

    ``now_epoch`` is the current time in seconds since epoch (UTC).
    """
    from .validate import parse_rfc3339_epoch

    role = node.get("role", "compute")
    last_seen = node.get("last_seen_at")
    try:
        age = now_epoch - parse_rfc3339_epoch(last_seen) if last_seen else OFFLINE_AFTER_S + 1
    except ValueError:
        age = OFFLINE_AFTER_S + 1

    flags = health_flags(sample, role)
    reasons = degraded_reasons(sample, role, age)
    status = derive_status(age, reasons, flags)
    flags["degraded_reasons"] = reasons
    return status, flags


def uptime_sec(sample: dict | None) -> int:
    if sample and isinstance(sample.get("uptime_sec"), int) and not isinstance(sample.get("uptime_sec"), bool):
        return sample["uptime_sec"]
    return 0


def capacity_of(hw: dict, cpu_allocated: int = 0, ram_allocated_gb: int = 0,
                disk_allocated_gb: int = 0) -> dict:
    """Allocatable bookkeeping: registered − reserve − allocated (dashboard.md §1.1).

    ``hw`` is the parsed ``hardware_json`` registration dict (canonical shape:
    ``{cpu, memory, gpus, disks}``; legacy: ``{cpu, ram_gb, gpu, disk}``).
    ``cpu_allocated`` (vCPUs), ``ram_allocated_gb`` and ``disk_allocated_gb``
    are the amounts already committed by prior accepted plans
    (controller-api.md §5.2 step 7). The returned ``*_free`` fields are the
    true allocatable headroom the per-node capacity clamp (step 6) consumes.
    """
    hw = hw or {}
    cpu = hw.get("cpu") if isinstance(hw.get("cpu"), dict) else {}
    mem = hw.get("memory") if isinstance(hw.get("memory"), dict) else {}
    disks = hw.get("disks") if isinstance(hw.get("disks"), list) else (
        hw.get("disk") if isinstance(hw.get("disk"), list) else []
    )
    gpus = hw.get("gpus") if isinstance(hw.get("gpus"), list) else (
        hw.get("gpu") if isinstance(hw.get("gpu"), list) else []
    )

    cpu_cores = cpu.get("threads") or cpu.get("logical_cores") or cpu.get("cores") or 0
    ram_total = mem.get("total_bytes")
    if ram_total is None and isinstance(mem.get("ram_gb"), (int, float)):
        ram_total = int(mem["ram_gb"]) * GiB
    disk_total = 0
    for d in disks:
        if isinstance(d, dict):
            b = d.get("size_bytes")
            if b is None and isinstance(d.get("size_gb"), (int, float)):
                b = int(d["size_gb"]) * GiB
            if isinstance(b, (int, float)) and not isinstance(b, bool):
                disk_total += int(b)

    ram_gb = int(ram_total) // GiB if isinstance(ram_total, (int, float)) and not isinstance(ram_total, bool) else 0
    disk_gb = disk_total // GiB

    cpu_allocated = max(0, int(cpu_allocated))
    ram_allocated_gb = max(0, int(ram_allocated_gb))
    disk_allocated_gb = max(0, int(disk_allocated_gb))

    # §5.1 reserves (OS/LXD) folded into the §5.2 step 6 allocatable ceiling.
    reserve_cores = int(cpu_cores * CPU_RESERVE_RATIO)
    allocatable_cores = max(0, cpu_cores - reserve_cores)
    overcommit_ceiling = int(allocatable_cores * CPU_OVERCOMMIT_RATIO)
    cpu_free = max(0, overcommit_ceiling - cpu_allocated)

    ram_free_gb = max(0, ram_gb - RAM_RESERVE_GB - ram_allocated_gb)

    reserved_disk_gb = int(disk_gb * DISK_RESERVE_RATIO)
    disk_free_gb = max(0, disk_gb - reserved_disk_gb - disk_allocated_gb)

    return {
        "cpu_cores": cpu_cores,
        "ram_gb": ram_gb,
        "disk_gb": disk_gb,
        "gpu_free": len(gpus),  # none allocated in the demo (inference_vm is a reference)
        "cpu_allocated": cpu_allocated,
        "ram_allocated_gb": ram_allocated_gb,
        "disk_allocated_gb": disk_allocated_gb,
        "cpu_free": cpu_free,
        "ram_free_gb": ram_free_gb,
        "disk_free_gb": disk_free_gb,
    }


def _cap_decision(index: int, field: str, requested: int, applied: int, reason: str) -> dict:
    return {
        "index": index,
        "field": field,
        "decision": "clamped",
        "from": requested,
        "to": applied,
        "reason": reason,
    }


def clamp_to_node_capacity(
    count: int, vcpus: int, ram_gb: int, disk_gb: int,
    capacity: dict, index: int = 0,
) -> tuple[dict, list[dict], str | None]:
    """Apply controller-api.md §5.2 step 6 to one ``agent_vm`` class.

    Clamps each per-VM field against its share of the node's allocatable
    capacity (aggregate = count × per-VM), in the frozen order RAM → vCPU →
    disk, rejecting with the matching ``exceeds_node_*_ceiling`` reason when
    the clamped value drops below the type floor. ``capacity`` is the node's
    :func:`capacity_of` result (must include ``ram_free_gb``, ``cpu_free``,
    ``disk_free_gb``).

    Returns ``(resolved, decisions, reject_reason)`` where ``resolved`` is the
    final ``{count, vcpus, ram_gb, disk_gb}`` and ``reject_reason`` is ``None``
    when the class fits.
    """
    resolved = {"count": count, "vcpus": vcpus, "ram_gb": ram_gb, "disk_gb": disk_gb}
    decisions: list[dict] = []

    free_ram = capacity.get("ram_free_gb", 0)
    cpu_free = capacity.get("cpu_free", 0)
    free_disk = capacity.get("disk_free_gb", 0)

    # RAM → min(requested, free RAM per VM). Reject if < type min (1 GiB).
    per_vm_ram = free_ram // count if count > 0 else 0
    if ram_gb > per_vm_ram:
        decisions.append(_cap_decision(index, "ram_gb", ram_gb, per_vm_ram, "exceeds_node_ram_ceiling"))
        ram_gb = per_vm_ram
    if ram_gb < AGENT_VM_MIN_RAM_GB:
        resolved["ram_gb"] = ram_gb
        return resolved, decisions, "exceeds_node_ram_ceiling"

    # vCPU → min(requested, overcommit ceiling per VM). Reject if < 1.
    per_vm_vcpus = cpu_free // count if count > 0 else 0
    if vcpus > per_vm_vcpus:
        decisions.append(_cap_decision(index, "vcpus", vcpus, per_vm_vcpus, "exceeds_node_cpu_ceiling"))
        vcpus = per_vm_vcpus
    if vcpus < AGENT_VM_MIN_VCPUS:
        resolved["vcpus"] = vcpus
        return resolved, decisions, "exceeds_node_cpu_ceiling"

    # disk → min(requested, free disk per VM). Reject if < type min (4 GiB).
    # Skipped when the node registered no disk (capacity unknown), so a
    # disk-less registration cannot spuriously reject a disk request.
    per_vm_disk = free_disk // count if count > 0 else 0
    if capacity.get("disk_gb", 0) > 0 and disk_gb > per_vm_disk:
        decisions.append(_cap_decision(index, "disk_gb", disk_gb, per_vm_disk, "exceeds_node_disk_ceiling"))
        disk_gb = per_vm_disk
    if capacity.get("disk_gb", 0) > 0 and disk_gb < AGENT_VM_MIN_DISK_GB:
        resolved["disk_gb"] = disk_gb
        return resolved, decisions, "exceeds_node_disk_ceiling"

    resolved["vcpus"] = vcpus
    resolved["ram_gb"] = ram_gb
    resolved["disk_gb"] = disk_gb
    return resolved, decisions, None
