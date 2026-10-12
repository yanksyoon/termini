"""Node registration request parsing + normalization.

Accepts the canonical `NodeRegistration` (node-bootstrap.md §5.2) and, for
compatibility with the earlier controller-api.md draft, the legacy
`{registration_id, hardware, os, lxd, installer_version}` shape. Both normalize
to one internal representation and one capabilities summary for role allocation.
"""
from __future__ import annotations

import re
from typing import Any

from . import errors

GiB = 1024 ** 3

# Node-reported LXD endpoint: `host:port` or `scheme://host:port`. IPv4/hostname
# or a bracketed IPv6 literal is accepted; a port is required because the address
# is what the apply-plan materialiser dials.
_ENDPOINT_RE = re.compile(
    r"^(?:(?P<scheme>[a-z][a-z0-9+.\-]*)://)?"
    r"(?P<host>\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.\-]+)"
    r":(?P<port>\d{1,5})$"
)


def _lxd_endpoint(raw: Any, ctx: str) -> str | None:
    """Validate an optional node LXD endpoint. Returns it or None.

    Absent (`None`/empty) is fine — that is exactly the older node the global
    fallback exists for. A *present* malformed value is a 422 rather than a silent
    drop: ignoring it would fall the node back to the global LXD URL and
    materialise VMs on the wrong host, which is worse than failing loudly.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise errors.invalid_argument(
            f"{ctx}: expected string, got {type(raw).__name__}",
            [{"field": ctx, "reason": "type"}],
        )
    value = raw.strip()
    if not value:
        return None
    m = _ENDPOINT_RE.match(value)
    if not m or not (1 <= int(m.group("port")) <= 65535):
        raise errors.invalid_argument(
            f"{ctx}: expected host:port or scheme://host:port, got {value!r}",
            [{"field": ctx, "reason": "format"}],
        )
    return value


def _endpoint_from(d: dict, *, nested: str, ctx: str) -> str | None:
    """Prefer `<nested>` (e.g. `machine.lxd.endpoint`), else the flat key."""
    obj = d.get(nested)
    flat = d.get("lxd_endpoint")
    if isinstance(obj, dict):
        return _lxd_endpoint(obj.get("endpoint"), f"{ctx}.{nested}.endpoint")
    return _lxd_endpoint(flat, f"{ctx}.lxd_endpoint")



def _require_str(d: dict, key: str, ctx: str, optional: bool = False) -> str | None:
    v = d.get(key)
    if v is None:
        if optional:
            return None
        raise errors.invalid_argument(f"{ctx}.{key}: required", [{"field": f"{ctx}.{key}", "reason": "missing"}])
    if not isinstance(v, str):
        raise errors.invalid_argument(
            f"{ctx}.{key}: expected string, got {type(v).__name__}",
            [{"field": f"{ctx}.{key}", "reason": "type"}],
        )
    return v


def _require_int(d: dict, key: str, ctx: str, minimum: int = 0) -> int:
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise errors.invalid_argument(
            f"{ctx}.{key}: expected integer, got {type(v).__name__}",
            [{"field": f"{ctx}.{key}", "reason": "type"}],
        )
    if v < minimum:
        raise errors.invalid_argument(
            f"{ctx}.{key}: {v} below minimum {minimum}",
            [{"field": f"{ctx}.{key}", "reason": "range"}],
        )
    return v


def _require_obj(d: dict, key: str, ctx: str) -> dict:
    v = d.get(key)
    if not isinstance(v, dict):
        raise errors.invalid_argument(
            f"{ctx}.{key}: expected object, got {type(v).__name__}",
            [{"field": f"{ctx}.{key}", "reason": "type"}],
        )
    return v


def _synthetic_flag(raw: dict) -> bool:
    """Optional registration provenance: was this row fabricated by the demo seeder?

    The seeder registers through this same public endpoint with the same
    single-use bootstrap tokens a node uses (node-bootstrap.md §5.2), so without an
    explicit marker a fabricated row cannot be told apart from real hardware. Real
    nodes never send this field; the seeder sets `"synthetic": true`.

    Accepts the boolean `synthetic` field, or the alias
    `"registration_source": "seeder"`. Anything else (absent, wrong type, a bogus
    value) reads as `False`, i.e. *"treat it as real hardware"* — the safe reading
    is never "assume synthetic", because that would let a real node's row be
    quietly deprioritised by a typo in an unrelated field.
    """
    v = raw.get("synthetic")
    if isinstance(v, bool):
        return v
    src = raw.get("registration_source")
    return isinstance(src, str) and src.strip().lower() == "seeder"


def parse_registration(raw: Any) -> dict:
    """Parse either registration shape into a normalized registration dict.

    Raises `errors.ApiError` (422) on structural violations.
    """
    if not isinstance(raw, dict):
        raise errors.invalid_argument("registration body: expected JSON object")

    if "machine" in raw:
        return _parse_canonical(raw)
    if "hardware" in raw:
        return _parse_legacy(raw)
    raise errors.invalid_argument(
        "registration body: missing 'machine' (canonical) or 'hardware' (legacy)",
        [{"field": "machine", "reason": "missing"}],
    )


def _parse_canonical(raw: dict) -> dict:
    machine = _require_obj(raw, "machine", "registration")
    machine_id = _require_str(machine, "machine_id", "machine", optional=True)
    hostname = _require_str(machine, "hostname", "machine", optional=True)

    cpu = _require_obj(machine, "cpu", "machine")
    cores = _require_int(cpu, "cores", "machine.cpu", minimum=1)
    threads = cpu.get("threads", cores)
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
        raise errors.invalid_argument("machine.cpu.threads: expected integer >= 1")

    memory = _require_obj(machine, "memory", "machine")
    ram_bytes = _require_int(memory, "total_bytes", "machine.memory", minimum=1)

    gpus = machine.get("gpus", [])
    if not isinstance(gpus, list):
        raise errors.invalid_argument("machine.gpus: expected array")
    gpu_vram = 0
    gpu_norm = []
    for i, g in enumerate(gpus):
        if not isinstance(g, dict):
            raise errors.invalid_argument(f"machine.gpus[{i}]: expected object")
        vram = g.get("vram_bytes")
        if isinstance(vram, bool) or not isinstance(vram, int) or vram < 0:
            raise errors.invalid_argument(f"machine.gpus[{i}].vram_bytes: expected non-negative integer")
        gpu_vram = max(gpu_vram, vram)
        gpu_norm.append({
            "model": g.get("model") if isinstance(g.get("model"), str) else None,
            "vram_bytes": vram,
        })

    disks = machine.get("disks", [])
    if not isinstance(disks, list):
        raise errors.invalid_argument("machine.disks: expected array")
    disk_bytes = 0
    for d in disks:
        if isinstance(d, dict) and isinstance(d.get("size_bytes"), int) and not isinstance(d.get("size_bytes"), bool):
            disk_bytes += d["size_bytes"]

    registration_id = machine_id or hostname or "node"
    return {
        "shape": "canonical",
        "synthetic": _synthetic_flag(raw),
        "registration_id": registration_id,
        "machine_id": machine_id,
        "hostname": hostname,
        "role_hint": machine.get("role_hint") if isinstance(machine.get("role_hint"), str) else None,
        "cpu_cores": threads,
        "ram_bytes": ram_bytes,
        "disk_bytes": disk_bytes,
        "gpu_vram_bytes": gpu_vram,
        "gpu_count": len(gpu_norm),
        "os": machine.get("os") if isinstance(machine.get("os"), dict) else None,
        "lxd": machine.get("lxd") if isinstance(machine.get("lxd"), dict) else None,
        "lxd_endpoint": _endpoint_from(machine, nested="lxd", ctx="machine"),
        "installer_version": machine.get("image_version")
        if isinstance(machine.get("image_version"), str)
        else None,
        "hardware_json": {
            "cpu": cpu, "memory": memory, "gpus": gpu_norm, "disks": disks,
        },
    }


def _parse_legacy(raw: dict) -> dict:
    registration_id = _require_str(raw, "registration_id", "registration")
    hardware = _require_obj(raw, "hardware", "registration")
    cpu = hardware.get("cpu")
    logical = 0
    if isinstance(cpu, dict):
        logical = cpu.get("logical_cores", 0)
        if isinstance(logical, bool) or not isinstance(logical, int):
            logical = 0
    ram_gb = hardware.get("ram_gb", 0)
    if isinstance(ram_gb, bool) or not isinstance(ram_gb, (int, float)):
        ram_gb = 0
    ram_bytes = int(ram_gb) * GiB

    gpus = hardware.get("gpu", [])
    if not isinstance(gpus, list):
        gpus = []
    gpu_vram = 0
    gpu_norm = []
    for i, g in enumerate(gpus):
        if not isinstance(g, dict):
            raise errors.invalid_argument(f"hardware.gpu[{i}]: expected object")
        vram_gb = g.get("vram_gb")
        if isinstance(vram_gb, bool) or not isinstance(vram_gb, (int, float)):
            raise errors.invalid_argument(f"hardware.gpu[{i}].vram_gb: expected number")
        vram_bytes = int(vram_gb) * GiB
        gpu_vram = max(gpu_vram, vram_bytes)
        gpu_norm.append({"model": g.get("model"), "vram_bytes": vram_bytes})

    disks = hardware.get("disk", [])
    if not isinstance(disks, list):
        disks = []
    disk_bytes = 0
    for d in disks:
        if isinstance(d, dict) and isinstance(d.get("size_gb"), (int, float)) and not isinstance(d.get("size_gb"), bool):
            disk_bytes += int(d["size_gb"]) * GiB

    return {
        "shape": "legacy",
        "synthetic": _synthetic_flag(raw),
        "registration_id": registration_id,
        "machine_id": None,
        "hostname": registration_id,
        "role_hint": None,
        "cpu_cores": logical,
        "ram_bytes": ram_bytes,
        "disk_bytes": disk_bytes,
        "gpu_vram_bytes": gpu_vram,
        "gpu_count": len(gpu_norm),
        "os": raw.get("os") if isinstance(raw.get("os"), dict) else None,
        "lxd": raw.get("lxd") if isinstance(raw.get("lxd"), dict) else None,
        "lxd_endpoint": _endpoint_from(raw, nested="lxd", ctx="registration"),
        "installer_version": raw.get("installer_version")
        if isinstance(raw.get("installer_version"), str)
        else None,
        "hardware_json": {"cpu": cpu, "ram_gb": ram_gb, "gpus": gpu_norm, "disk": disks},
    }
