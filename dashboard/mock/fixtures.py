"""Deterministic, contract-faithful fixtures for the Termini dashboard mock.

Everything here mirrors the frozen contracts so the dashboard can be exercised
end-to-end without the real controller:

  * docs/contracts/dashboard.md   §1 telemetry, §2 log stream, §3 SSH string,
                                  §4 intervention
  * docs/contracts/controller-api.md §3 error envelope, §4.2 intents/apply
  * docs/telemetry-schema.md      §3 Sample object, §3.1-3.8 allowlists
  * controller/app/store.py       METRICS registry (19 names + units)

Stdlib only. No network.
"""
from __future__ import annotations

import hashlib
import math
import random
import time

# ---------------------------------------------------------------- identifiers
#
# NOTE: the example IDs printed in docs/contracts/dashboard.md are 30 chars long,
# which violates the frozen "26-char Crockford base32 ULID" invariant the same
# document (and governance.md §4) specifies. The fixtures below use canonical
# 26-char ULIDs so the mock satisfies the wire invariant.
PROJECT_ID = "01H4KZ9Q2R3T5V7W9X1Y3Z5A7B"
PROJECT_TOKEN = "demo-project-token-000000000000000000000000000000000000000000"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ulid_counter = 0


def ulid(now_ms: int | None = None) -> str:
    """A monotonic, valid-shaped 26-char Crockford base32 ULID.

    The per-millisecond counter increments from the *rightmost* position so the
    generated ULIDs are lexicographically increasing within a millisecond (the
    property the log-stream cursor relies on, dashboard.md §2.3).
    """
    global _ulid_counter
    _ulid_counter += 1
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    time_chars = []
    t = ts
    for _ in range(10):
        time_chars.append(_CROCKFORD[t % 32])
        t //= 32
    time_chars.reverse()
    rand_chars = ["0"] * 16
    n = _ulid_counter
    for i in range(15, -1, -1):
        rand_chars[i] = _CROCKFORD[n % 32]
        n //= 32
    return "".join(time_chars) + "".join(rand_chars)


def now_iso(ms: int | None = None) -> str:
    ms = ms if ms is not None else int(time.time() * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + f".{ms % 1000:03d}Z"


# ------------------------------------------------------------------- registry

# name -> unit, exactly the 19 metrics in dashboard.md §1.4 / store.py METRICS.
METRIC_UNITS = {
    "cpu_util_pct": "percent",
    "cpu_core_count": "count",
    "ram_total_bytes": "bytes",
    "ram_used_bytes": "bytes",
    "swap_used_bytes": "bytes",
    "load_avg_1m": "loadavg",
    "cpu_temp_celsius": "celsius",
    "gpu_util_pct": "percent",
    "gpu_vram_used_bytes": "bytes",
    "gpu_vram_total_bytes": "bytes",
    "net_rx_bytes": "bytes",
    "net_tx_bytes": "bytes",
    "net_latency_ms": "ms",
    "disk_total_bytes": "bytes",
    "disk_free_bytes": "bytes",
    "disk_write_latency_ms": "ms",
    "inference_tokens_per_s": "tok/s",
    "inference_active_requests": "count",
    "inference_vram_used_bytes": "bytes",
}


def _arr(sample: dict, key: str) -> list:
    v = sample.get(key)
    return v if isinstance(v, list) else []


def _sum_of(sample: dict, container: str, key: str):
    vals = [d.get(key) for d in _arr(sample, container) if isinstance(d, dict)]
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return sum(nums) if nums else None


def _max_of(sample: dict, container: str, key: str):
    vals = [d.get(key) for d in _arr(sample, container) if isinstance(d, dict)]
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return max(nums) if nums else None


def metric_value(name: str, sample: dict):
    """Flatten a nested Sample to a single value (store.py convention)."""
    inf_raw = sample.get("inference")
    inf: dict = inf_raw if isinstance(inf_raw, dict) else {}
    gpu = _arr(sample, "gpu")
    if name == "cpu_util_pct":
        return sample.get("cpu_util_pct")
    if name == "cpu_core_count":
        return sample.get("cpu_core_count")
    if name == "ram_total_bytes":
        return sample.get("ram_total_bytes")
    if name == "ram_used_bytes":
        return sample.get("ram_used_bytes")
    if name == "swap_used_bytes":
        return sample.get("swap_used_bytes")
    if name == "load_avg_1m":
        return sample.get("load_avg_1m")
    if name == "cpu_temp_celsius":
        return sample.get("cpu_temp_celsius")
    if name == "gpu_util_pct":
        return _max_of(sample, "gpu", "util_pct")
    if name == "gpu_vram_used_bytes":
        return _sum_of(sample, "gpu", "vram_used_bytes")
    if name == "gpu_vram_total_bytes":
        return _sum_of(sample, "gpu", "vram_total_bytes")
    if name == "net_rx_bytes":
        return _sum_of(sample, "net", "rx_bytes")
    if name == "net_tx_bytes":
        return _sum_of(sample, "net", "tx_bytes")
    if name == "net_latency_ms":
        return _max_of(sample, "net", "latency_ms")
    if name == "disk_total_bytes":
        return _sum_of(sample, "disk", "total_bytes")
    if name == "disk_free_bytes":
        return _sum_of(sample, "disk", "free_bytes")
    if name == "disk_write_latency_ms":
        return _max_of(sample, "disk", "write_latency_ms")
    if name == "inference_tokens_per_s":
        return inf.get("tokens_per_s")
    if name == "inference_active_requests":
        return inf.get("active_requests")
    if name == "inference_vram_used_bytes":
        return inf.get("vram_used_bytes")
    return None


# ---------------------------------------------------------------------- nodes

NODES = [
    {
        "node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5B1E",
        "registration_id": "termini-node-a",
        "role": "compute",
        "public_ip": "34.136.10.11",
        "seed": 0.4,
        "flappy": True,  # demonstrates a live status transition
    },
    {
        "node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5C2F",
        "registration_id": "termini-node-b",
        "role": "compute",
        "public_ip": "34.136.10.12",
        "seed": 1.1,
        "flappy": True,
    },
    {
        "node_id": "01H4KZ9Q2R3T5V7W9X1Y3Z5D3G",
        "registration_id": "termini-node-c",
        "role": "inference",
        "public_ip": "34.136.10.13",
        "seed": 1.9,
        "flappy": False,
    },
]

HARDWARE = {
    "compute": {
        "cpu": {"model": "Intel Xeon Gold 6338", "threads": 16, "cores": 8},
        "memory": {"total_bytes": 34359738368},
        "disks": [{"device": "/dev/sda", "type": "ssd", "size_bytes": 274877906944}],
        "gpus": [],
    },
    "inference": {
        "cpu": {"model": "AMD EPYC 7B13", "threads": 16, "cores": 8},
        "memory": {"total_bytes": 34359738368},
        "disks": [{"device": "/dev/sda", "type": "ssd", "size_bytes": 274877906944}],
        "gpus": [{"vendor": "nvidia", "model": "NVIDIA L4", "vram_bytes": 25769803776}],
    },
}

SERVICE_ALLOWLIST = ("termini-bootstrap", "lxd", "sshd", "llama-cpp")

# Agent inventory for the demo (§4.6 `lxd_instances[]`). The real rule is
#   vm_id = 26 Crockford chars of sha256("termini-vm-id:" + name)
# (app/operator/vmids.py); the ids below are computed by the same rule so the
# mock cannot disagree with the controller about what an agent id looks like.
# `AGENTS` selects the edge state a screenshot run wants:
#   "some"   two agents on node-a, node-b omits the field (cannot read LXD),
#            node-c reports an empty array (zero running, definitely)
#   "none"   every node reports an empty array -> the panel's empty state
#   "absent" every node omits the field -> "inventory not reported" everywhere
AGENTS = "some"

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def agent_id(name: str) -> str:
    digest = hashlib.sha256(("termini-vm-id:" + name).encode("utf-8")).digest()
    value = int.from_bytes(digest[:17], "big") >> 6
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def _instances(node: dict):
    """The §4.6 inventory a node reports for this demo mode."""
    if AGENTS == "absent":
        return None
    if AGENTS == "none":
        return []
    if node["registration_id"] == "termini-node-b":
        return None                      # node cannot read its own LXD: OMITTED
    if node["registration_id"] == "termini-node-c":
        return []                        # inference host runs no agents
    names = ["termini-01m4wjhwqr3b8pcav2tvqbgg-0-0",
             "termini-01m4wjhwqr3b8pcav2tvqbgg-0-1"]
    return [
        {
            "vm_id": agent_id(name),
            "state": "running",
            "cpu_util_pct": round(4.2 + i * 3.1, 1),
            "ram_used_bytes": int((0.42 + i * 0.11) * 1024 ** 3),
            "ram_limit_bytes": 1073741824,
            "disk_used_bytes": 0,
            "restart_count": i,
        }
        for i, name in enumerate(names)
    ]



def _wave(seed: float, t: float, lo: float, hi: float) -> float:
    v = math.sin(t / 9.0 + seed) * 0.5 + math.sin(t / 2.3 + seed * 2.3) * 0.5
    return lo + ((v + 1.0) / 2.0) * (hi - lo)


def sample(node: dict, t_sec: float, seq: int, uptime_sec: int | None = None) -> dict:
    """One telemetry Sample (§3) at time t_sec (seconds, float)."""
    s = node["seed"]
    gpu_node = node["role"] == "inference"
    hot = 35 if node["registration_id"] == "termini-node-a" else 0
    ram_total = 34359738368
    ram_pct = _wave(s + 13, t_sec, 0.38 + hot / 200, 0.72 + hot / 400)
    disks = []
    for i, d in enumerate(HARDWARE[node["role"]]["disks"]):
        disks.append(
            {
                "path": d["device"],
                "total_bytes": d["size_bytes"],
                "free_bytes": int(d["size_bytes"] * _wave(s + 3 + i, t_sec, 0.28, 0.55)),
                "write_latency_ms": round(_wave(s + 4, t_sec, 6.0, 62.0) if hot else _wave(s + 4, t_sec, 1.2, 12.0), 1),
                "mount_ro": False,
            }
        )
    out = {
        "seq": seq,
        "ts": now_iso(int(t_sec * 1000)),
        "uptime_sec": int(uptime_sec) if uptime_sec is not None else int(t_sec),
        "cpu_util_pct": round(_wave(s, t_sec, 10 + hot, 55 + hot), 1),
        "cpu_core_count": 16,
        "ram_total_bytes": ram_total,
        "ram_used_bytes": int(ram_total * ram_pct),
        "swap_used_bytes": 0,
        "load_avg_1m": round(_wave(s + 1, t_sec, 0.4, 6.5), 2),
        "cpu_temp_celsius": round(_wave(s + 2, t_sec, 40, 62 + hot / 3), 1),
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "kernel_version": "6.8.0-142-generic",
        "lxd_cluster_role": "member",
        "hostname": "5f2a9c1e7b4d4a1f",
        "ip_local": "10.20.1.0/24",
        "mac_address": "9c1e7b4d4a1f5f2a",
        "project_id": PROJECT_ID,
        "disk": disks,
        "net": [
            {
                "iface": "eth0",
                "rx_bytes": int(_wave(s + 5, t_sec, 2e5, 3.2e7)),
                "tx_bytes": int(_wave(s + 6, t_sec, 1e5, 1.4e7)),
                "latency_ms": round(_wave(s + 7, t_sec, 0.3, 4.5), 1),
            }
        ],
        "gpu": (
            [
                {
                    "model": "NVIDIA L4",
                    "util_pct": round(_wave(s + 8, t_sec, 35, 96), 1),
                    "vram_total_bytes": 25769803776,
                    "vram_used_bytes": int(_wave(s + 9, t_sec, 0.4, 0.85) * 25769803776),
                    "temp_celsius": round(_wave(s + 10, t_sec, 52, 78), 1),
                }
            ]
            if gpu_node
            else []
        ),
        "services": [
            {"name": "termini-bootstrap", "status": "running"},
            {"name": "lxd", "status": "running"},
            {"name": "sshd", "status": "running"},
        ]
        + ([{"name": "llama-cpp", "status": "running"}] if gpu_node else []),
    }
    instances = _instances(node)
    if instances is None:
        # §4.6: the field is OMITTED, not emptied, when the node cannot read its
        # own LXD — "did not report" and "reports nothing" are different facts.
        out.pop("lxd_instances", None)
    else:
        out["lxd_instances"] = instances
    if gpu_node:
        out["inference"] = {
            "server_up": True,
            "model": "deepseek-flash",
            "active_requests": int(_wave(s + 11, t_sec, 0, 6)),
            "queue_depth": 0,
            "tokens_per_s": round(_wave(s + 12, t_sec, 20, 70), 1),
            "vram_used_bytes": 12884901888,
        }
    return out


# -------------------------------------------------------------------- health

def derive_health(node: dict, latest: dict | None) -> dict:
    """dashboard.md §1.3."""
    s = latest or {}
    disks = _arr(s, "disk")
    gpus = _arr(s, "gpu")
    cpu = s.get("cpu_util_pct")
    ram_pct = 0.0
    if isinstance(s.get("ram_total_bytes"), (int, float)) and s.get("ram_total_bytes"):
        if isinstance(s.get("ram_used_bytes"), (int, float)):
            ram_pct = 100.0 * s["ram_used_bytes"] / s["ram_total_bytes"]

    disk_ro = any(d.get("mount_ro") is True for d in disks)
    disk_slow = any(
        isinstance(d.get("write_latency_ms"), (int, float)) and d["write_latency_ms"] >= 50 for d in disks
    )

    gpu_ok = None
    inference_ok = None
    reasons = []
    if node["role"] == "inference":
        if not gpus:
            gpu_ok = False
            reasons.append("gpu_unavailable")
        elif isinstance(gpus[0].get("temp_celsius"), (int, float)) and gpus[0]["temp_celsius"] >= 85:
            gpu_ok = False
            reasons.append("gpu_overtemp")
        else:
            gpu_ok = True
        inf_raw = s.get("inference")
        inf: dict = inf_raw if isinstance(inf_raw, dict) else {}
        if isinstance(inf.get("server_up"), bool):
            inference_ok = inf["server_up"]
            if inference_ok is False:
                reasons.append("inference_down")

    services = [x for x in _arr(s, "services") if x.get("name") in SERVICE_ALLOWLIST]
    if disk_ro:
        reasons.append("disk_read_only")
    if disk_slow:
        reasons.append("disk_degrading")
    if any(x.get("status") == "failed" for x in services):
        reasons.append("service_failed")

    return {
        "cpu_util_pct": cpu,
        "ram_used_pct": round(ram_pct, 1),
        "disk_ok": not disk_ro,
        "gpu_ok": gpu_ok,
        "inference_ok": inference_ok,
        "services": services,
        "degraded_reasons": reasons,
    }


def derive_status(node: dict, latest: dict | None, now_ms: int, last_seen_ms: int) -> tuple[str, list]:
    """dashboard.md §1.1 status derivation. Returns (status, reasons)."""
    age_ms = now_ms - last_seen_ms
    health = derive_health(node, latest)
    reasons = list(health["degraded_reasons"])
    if age_ms > 45000:
        return "offline", reasons
    if age_ms > 15000:
        if "heartbeat_late" not in reasons:
            reasons.insert(0, "heartbeat_late")
        return "degraded", reasons
    flags_false = any(v is False for v in (health["disk_ok"], health["gpu_ok"], health["inference_ok"]))
    svc_bad = any(x.get("status") != "running" for x in health["services"])
    if flags_false or svc_bad:
        return "degraded", reasons
    return "online", reasons


def node_view(node: dict, latest: dict | None, now_ms: int, last_seen_ms: int) -> dict:
    status, _ = derive_status(node, latest, now_ms, last_seen_ms)
    hw = HARDWARE[node["role"]]
    cpu = hw.get("cpu", {})
    mem = hw.get("memory", {})
    disk_bytes = sum(d.get("size_bytes", 0) for d in hw.get("disks", []))
    return {
        "node_id": node["node_id"],
        "registration_id": node["registration_id"],
        "role": node["role"],
        "status": status,
        "last_seen_at": now_iso(last_seen_ms),
        "uptime_sec": (latest or {}).get("uptime_sec", 0),
        "hardware": hw,
        "capacity": {
            "cpu_cores": cpu.get("threads", 0),
            "ram_gb": mem.get("total_bytes", 0) // (1024 ** 3),
            "disk_gb": disk_bytes // (1024 ** 3),
            "gpu_free": 1 if node["role"] == "inference" else 0,
            "cpu_allocated": 4,
            "ram_allocated_gb": 8,
        },
        "health": derive_health(node, latest),
    }


# -------------------------------------------------------------------- series

def series(node: dict, start: float, end: float, resolution: str, names: list[str]) -> dict:
    bucket = {"raw": 5.0, "5s": 5.0, "1m": 60.0, "5m": 300.0}.get(resolution, 5.0)
    seq = int(start)
    out = {n: {"name": n, "unit": METRIC_UNITS.get(n, ""), "points": []} for n in names if n in METRIC_UNITS}
    t = start
    while t <= end:
        s = sample(node, t, seq)
        ts = now_iso(int(t * 1000))
        for n in out:
            v = metric_value(n, s)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[n]["points"].append({"t": ts, "v": round(float(v), 4)})
        t += bucket
        seq += 1
    return {"node_id": node["node_id"], "resolution": resolution, "series": [out[n] for n in names if n in out]}


# ---------------------------------------------------------------------- logs

def log_line(node_id, severity, actor, message, remediation=None, log_id=None) -> dict:
    """dashboard.md §2.1 envelope."""
    return {
        "log_id": log_id or ulid(),
        "ts": now_iso(),
        "node_id": node_id,
        "severity": severity,
        "actor": actor,
        "message": message,
        "remediation": remediation,
    }


def ssh_view(node: dict) -> dict:
    """dashboard.md §3.2."""
    return {
        "node_id": node["node_id"],
        "user": "ubuntu",
        "host": node["public_ip"],
        "port": 22,
        "key_algorithm": "ed25519",
        "key_fingerprint": "SHA256:AbC123dEf456GhI789jKl012MnO345pQr678sTu90=",
        "key_comment": "termini-breakglass",
        "command": f"ssh -i ~/.ssh/termini_breakglass ubuntu@{node['public_ip']}",
    }


def err(code: str, message: str, status: int, details=None) -> tuple[int, dict]:
    return status, {
        "error": {
            "code": code,
            "message": message,
            "details": details or [],
            "request_id": ulid(),
        }
    }


# ------------------------------------------------- operator decisions (mock)

def synthetic_node(project_id: str = PROJECT_ID) -> dict:
    """A seeder-fabricated row, labelled `synthetic: true` (dashboard.md §1.1).

    Returned only for `?include=synthetic`; the default listing is the real
    fleet, exactly as the contract's standing-fleet amendment requires.
    """
    return {
        "node_id": ulid(),
        "registration_id": "demo-node-a",
        "role": "compute",
        "synthetic": True,
        "status": "online",
        "last_seen_at": now_iso(),
        "uptime_sec": 4200,
        "hardware": {
            "cpu": {"model": "AMD EPYC 7B13", "threads": 16, "cores": 8},
            "memory": {"total_bytes": 68719476736},
            "disks": [{"device": "/dev/sda", "type": "ssd", "size_bytes": 274877906944}],
            "gpus": [],
        },
        "capacity": {"cpu_cores": 16, "ram_gb": 64, "disk_gb": 256, "gpu_free": 0, "cpu_allocated": 4, "ram_allocated_gb": 8},
        "health": {
            "cpu_util_pct": 31.2,
            "ram_used_pct": 96.0,
            "disk_ok": True,
            "gpu_ok": None,
            "inference_ok": None,
            "services": [],
            "degraded_reasons": [],
        },
    }


def _decision_row(node_id: str, *, rule_id: str, type_: str, metric: str, value: float, threshold: str,
                  action_kind: str, instance: str, reason: str, source: str, confidence=None,
                  fallback_reason=None, result: str = "ok", decided_ms: int | None = None,
                  incident_generated: str | None = None, incident_title: str | None = None,
                  incident_explanation: str | None = None, incident_evidence: list | None = None,
                  incident_actions: list | None = None) -> dict:
    decided_ms = decided_ms if decided_ms is not None else int(time.time() * 1000)
    event_id = ulid(decided_ms - 4000)
    decision_id = ulid(decided_ms)
    event = {
        "id": event_id,
        "node_id": node_id,
        "rule_id": rule_id,
        "type": type_,
        "metric": metric,
        "value": value,
        "threshold": threshold,
        "evidence_seq_start": 1000,
        "evidence_seq_end": 1002,
        "fired_at": now_iso(decided_ms - 4000),
        "breach_started_at": now_iso(decided_ms - 12000),
        "evidence": [],
    }
    decision = {
        "id": decision_id,
        "event_id": event_id,
        "node_id": node_id,
        "rule_id": rule_id,
        "playbook": rule_id,
        "action_kind": action_kind,
        "action_params": {"instance_name": instance, "force": True},
        "reason": reason,
        "decided_at": now_iso(decided_ms),
        "decision_source": source,
        "confidence": confidence,
        "fallback_reason": fallback_reason,
        # §4.2 incident record (migration 0016). All five stay `None` when the
        # caller passes no record — the deterministic-mode row (§8.1) writes no
        # incident columns at all, and the dashboard renders that as
        # "no incident record (deterministic mode)" rather than an empty card.
        "incident_title": incident_title,
        "incident_explanation": incident_explanation,
        "incident_evidence": incident_evidence,
        "incident_actions": incident_actions,
        "incident_generated": incident_generated,
    }
    audit = {
        "id": ulid(decided_ms + 2),
        "decision_id": decision_id,
        "event_id": event_id,
        "node_id": node_id,
        "rule_id": rule_id,
        "action_kind": action_kind,
        "result": result,
        "detail": "state transitioned" if result == "ok" else "HTTP 404: Instance not found",
        "lxd_method": "PUT",
        "lxd_path": f"/1.0/instances/{instance}/state",
        "lxd_body": '{"action": "stop", "force": true}',
        "idempotency_key": f"{node_id}:{rule_id}:{action_kind}:{instance}:1",
        "dry_run": 0,
        "executed_at": now_iso(decided_ms + 2),
        "lxd_endpoint": "https://10.10.0.11:8443",
    }
    return {"decision": decision, "event": event, "audit": audit}


def operator_decisions(nodes: list[dict], now_ms: int | None = None) -> dict:
    """Decisions + the events they answered + their audit rows (newest first).

    Contains a MODEL decision (with confidence + rationale) and a deterministic
    FALLBACK, because the dashboard must show both and never conflate them — plus
    a deterministic-mode row that carries NO `incident_generated`/incident
    columns at all (§8.1), which is what the dashboard renders as "no incident
    record (deterministic mode)". Each row that has one carries the §4.2 incident
    record the controller persists (migration 0016).
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    a = nodes[0]["node_id"] if nodes else ulid()
    b = nodes[-1]["node_id"] if nodes else ulid()
    instance = "01H4KZ9Q2R3T5V7W9X1Y3Z5E4H"
    rows = [
        _decision_row(
            b, rule_id="disk-degrade-01", type_="disk_degrade", metric="disk_write_latency_ms",
            value=120.0, threshold="free <= 5% OR write_latency_ms >= 50 OR free slope <= -5000000.0 bytes/s",
            action_kind="instance.stop", instance=instance,
            reason="disk write latency on /dev/sda is 120ms, exceeding the 50ms threshold; stopping the instance with the highest disk usage will reduce disk I/O.",
            source="model", confidence=0.85, decided_ms=now_ms - 60_000,
            incident_generated="model",
            incident_title="Disk write latency on /dev/sda",
            incident_explanation=(
                "write_latency_ms on /dev/sda reached 120 ms against a 50 ms threshold while free space "
                "stayed above the 5% floor, so this is I/O contention rather than a full disk. The only "
                "instance on the node holds the write path, so stopping it removes the load without "
                "draining the node."
            ),
            incident_evidence=[
                {"field": "disk_write_latency_ms", "value": 120.0, "seq": 1120, "ts": now_iso(now_ms - 64_000)},
                {"field": "disk_free_bytes", "value": 74000000000, "seq": 1120},
            ],
            incident_actions=[
                {"action_kind": "instance.stop", "target": instance,
                 "note": "stop the instance holding the write path"},
            ],
        ),
        _decision_row(
            a, rule_id="oom-guard-01", type_="oom", metric="ram_used_bytes",
            value=33114165248.0, threshold=">= 95% of ram_total_bytes for 2 consecutive samples",
            action_kind="instance.restart", instance=instance,
            reason="host RAM is at 96.04% of total, and this instance is the heaviest consumer; restarting it should free memory.",
            source="model", confidence=0.9, decided_ms=now_ms - 180_000,
            incident_generated="model",
            incident_title="Memory exhaustion on the node",
            incident_explanation=(
                "ram_used_bytes reached 96.4% of ram_total_bytes and stayed there for two consecutive "
                "samples, and one instance holds 29% of the node's RAM by itself. That is a single-instance "
                "leak rather than fleet-wide pressure."
            ),
            incident_evidence=[
                {"field": "ram_used_bytes", "value": 33114165248, "seq": 1110, "ts": now_iso(now_ms - 184_000)},
                {"field": "ram_total_bytes", "value": 34359738368, "seq": 1110, "ts": now_iso(now_ms - 184_000)},
                {"field": "lxd_instances.ram_used_bytes", "value": 9960000000, "seq": 1110},
            ],
            incident_actions=[
                {"action_kind": "instance.restart", "target": instance,
                 "note": "restart the heaviest instance to reclaim its resident memory"},
            ],
        ),
        _decision_row(
            a, rule_id="oom-guard-01", type_="oom", metric="ram_used_bytes",
            value=33000000000.0, threshold=">= 95% of ram_total_bytes for 2 consecutive samples",
            action_kind="instance.restart", instance=instance,
            reason="playbook: restart the heaviest instance (RAM 96%)",
            source="fallback", fallback_reason="inference_unavailable",
            result="error", decided_ms=now_ms - 300_000,
            # §8.1: the fallback writes a record too — event-derived C0 fields
            # only, and labelled so it is never read as model output.
            incident_generated="fallback",
            incident_title="oom on the node",
            incident_explanation=(
                "model unavailable (inference_unavailable); the deterministic playbook chose "
                f"instance.restart on {instance} for rule oom-guard-01"
            ),
            incident_evidence=[
                {"field": "ram_used_bytes", "value": 33000000000, "seq": 1122, "ts": now_iso(now_ms - 304_000)},
            ],
            incident_actions=[{"action_kind": "instance.restart", "target": instance}],
        ),
        _decision_row(
            b, rule_id="latency-01", type_="latency", metric="net_latency_ms",
            value=240.0, threshold="net_latency_ms >= 200 for 3 consecutive samples",
            action_kind="instance.restart", instance=instance,
            reason="playbook: restart the instance reporting 240ms of sustained network latency",
            source="deterministic", decided_ms=now_ms - 420_000,
            # deterministic mode writes NO incident columns (§8.1) — the state the
            # dashboard must render as "no incident record (deterministic mode)".
            incident_generated=None,
        ),
    ]
    decisions = [r["decision"] for r in rows][::-1]
    events = [r["event"] for r in rows][::-1]
    audits = [r["audit"] for r in rows][::-1]
    return {"decisions": decisions, "events": events, "audits": audits}


def intent_plan(prompt: str) -> dict:
    """A deterministic guardrail plan (accepted, or rejected for a magic word)."""
    if random is not None and any(w in (prompt or "").lower() for w in ("reject", "impossible", "too big")):
        return {
            "plan_id": ulid(),
            "request_id": ulid(),
            "project_id": PROJECT_ID,
            "outcome": "rejected",
            "created_at": now_iso(),
            "allocations": [
                {
                    "index": 0,
                    "status": "rejected",
                    "requested": {"type": "agent_vm", "count": 4, "vcpus": 2, "ram_gb": 4},
                    "resolved": None,
                    "guardrail": {"clamped_fields": [], "reject_reasons": ["exceeds_node_ram_ceiling"]},
                    "node_id": None,
                    "vm_ids": [],
                }
            ],
        }
    return {
        "plan_id": ulid(),
        "request_id": ulid(),
        "project_id": PROJECT_ID,
        "outcome": "accepted",
        "created_at": now_iso(),
        "allocations": [
            {
                "index": 0,
                "status": "accepted",
                "requested": {"type": "agent_vm", "count": 3, "vcpus": 2, "ram_gb": 4, "target_node": "auto"},
                "resolved": {"type": "agent_vm", "count": 3, "vcpus": 2, "ram_gb": 4, "target_node": NODES[0]["node_id"]},
                "guardrail": {"clamped_fields": [], "reject_reasons": []},
                "node_id": NODES[0]["node_id"],
                "vm_ids": [],
            },
            {
                "index": 1,
                "status": "accepted",
                "requested": {"type": "agent_vm", "count": 1, "vcpus": 12, "ram_gb": 4, "target_node": "auto"},
                "resolved": {"type": "agent_vm", "count": 1, "vcpus": 8, "ram_gb": 4, "target_node": NODES[1]["node_id"]},
                "guardrail": {
                    "clamped_fields": [
                        {"field": "vcpus", "decision": "clamped", "from": 12, "to": 8, "reason": "preferred_range"}
                    ],
                    "reject_reasons": [],
                },
                "node_id": NODES[1]["node_id"],
                "vm_ids": [],
            },
        ],
    }
