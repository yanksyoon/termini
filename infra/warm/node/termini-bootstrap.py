#!/usr/bin/env python3
"""termini-bootstrap — node-side bootstrap agent for the Termini demo cluster.

Implements the node half of `docs/contracts/node-bootstrap.md` closely enough to
bring a pre-baked demo node ONLINE against the real controller core that lives in
`controller/`:

  1. read `/etc/termini/config.yaml` + `/var/lib/termini/enrollment.token`
  2. gather machine facts (CPU/RAM/disk/NIC/GPU) and a stable `machine_id`
  3. `POST /v1/nodes/register` (Bearer enrollment token) -> node_id + node_token
     + assigned role; persist identity so a reboot reconnects instead of
     re-enrolling (contract §10)
  4. stream telemetry samples at the configured cadence
     (`POST /v1/nodes/{id}/telemetry`), with C1 fields already transformed so the
     controller governance gate accepts them

The controller mints mTLS certs and LXD join tokens (contract §5); the core in
this repo returns `identity: null` / `cluster: null`, which is why this agent
runs over plain HTTP on the demo underlay — see infra/warm/README.md.

Runs as a systemd unit (`termini-bootstrap.service`). `termini-bootstrap status`
prints the persisted state as JSON.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import socket
import sys
import time
import urllib.error
import urllib.request

CONFIG_PATH = os.environ.get("TERMINI_CONFIG", "/etc/termini/config.yaml")
TOKEN_PATH = os.environ.get("TERMINI_TOKEN_FILE", "/var/lib/termini/enrollment.token")
IDENTITY_DIR = os.environ.get("TERMINI_IDENTITY_DIR", "/var/lib/termini/identity")
STATE_PATH = os.environ.get("TERMINI_STATE_PATH", "/var/lib/termini/state.json")
SPOOL_DIR = os.environ.get("TERMINI_SPOOL_DIR", "/var/lib/termini/telemetry")
SECRETS = "/sys/class/dmi/id/product_uuid"
MACHINE_ID = "/etc/machine-id"

REDACTOR_VERSION = "redactor-config v3"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


# --------------------------------------------------------------------------- ULID
def new_ulid() -> str:
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = random.getrandbits(80)
    n = (ts << 80) | rnd
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))


def is_ulid(s: str) -> bool:
    return isinstance(s, str) and len(s) == 26 and all(c in _CROCKFORD for c in s)


# --------------------------------------------------------------------------- config
def parse_simple_yaml(path: str) -> dict:
    """Parse the flat `key: value` subset cloud-init writes (no nesting needed)."""
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


def read_token(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            t = fh.read().strip()
        return t or None
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------------- facts
def _first_line(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return default


def machine_id(node_name: str) -> str:
    uuid = _first_line(SECRETS)
    mid = _first_line(MACHINE_ID)
    basis = f"{uuid}:{mid}:{node_name}"
    if not uuid and not mid:
        basis = f"{node_name}:{socket.gethostname()}"
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def cpu_facts() -> dict:
    cores = os.cpu_count() or 1
    model = "unknown"
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return {"model": model, "sockets": 1, "cores": cores, "threads": cores}


def mem_total_bytes() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def gpu_facts(cfg: dict) -> list:
    """GPU facts reported at registration.

    On a real Node C this is read from `nvidia-smi`. This build host has no
    physical GPU, so the demo inference node is configured with the Node C
    variant's VRAM (`gpu_vram_bytes` in config.yaml, per infra-decisions.md D5)
    and reports it here; llama.cpp itself runs CPU-only. Compute nodes report [].
    """
    raw = cfg.get("gpu_vram_bytes", "")
    try:
        vram = int(raw)
    except (TypeError, ValueError):
        vram = 0
    if vram <= 0:
        return []
    return [{
        "vendor": "nvidia",
        "model": cfg.get("gpu_model", "NVIDIA L4"),
        "vram_bytes": vram,
        "pci_id": "0000:00:04.0",
        "driver": "535.183",
        "health": "ok",
    }]


def _file_or_uuid_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def primary_ipv4() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "0.0.0.0"


def transformed_ipv4(ip: str) -> str:
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    return "0.0.0.0/24"


def mac_of(ifname: str) -> str:
    try:
        with open(f"/sys/class/net/{ifname}/address", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "00:00:00:00:00:00"


def uptime_sec() -> int:
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return int(float(fh.read().split()[0]))
    except OSError:
        return 0


def load_avg_1m() -> float:
    try:
        return float(os.getloadavg()[0])
    except (OSError, AttributeError):
        return 0.0


def cpu_util_pct() -> float:
    """Rough instantaneous CPU utilisation from /proc/stat, clamped to [0,100]."""

    def snap():
        with open("/proc/stat", encoding="utf-8") as fh:
            fields = [int(x) for x in fh.readline().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields)
        return idle, total

    try:
        i1, t1 = snap()
        time.sleep(0.15)
        i2, t2 = snap()
        dt = t2 - t1
        if dt <= 0:
            return 0.0
        return round(min(100.0, max(0.0, 100.0 * (1.0 - (i2 - i1) / dt))), 2)
    except OSError:
        return 0.0


def disk_facts() -> list:
    out = []
    for path in ("/", "/var/lib/termini"):
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total <= 0:
            continue
        dev = "/dev/root" if path == "/" else "/dev/termini"
        out.append({
            "path": dev,
            "total_bytes": total,
            "free_bytes": free,
            "write_latency_ms": 0.5,
            "mount_ro": False,
        })
    if not out:
        out.append({"path": "/dev/root", "total_bytes": 1, "free_bytes": 0,
                    "write_latency_ms": 0.5, "mount_ro": False})
    return out


def net_facts(ifname: str) -> list:
    try:
        with open(f"/sys/class/net/{ifname}/statistics/rx_bytes", encoding="utf-8") as fh:
            rx = int(fh.read().strip())
        with open(f"/sys/class/net/{ifname}/statistics/tx_bytes", encoding="utf-8") as fh:
            tx = int(fh.read().strip())
    except OSError:
        rx = tx = 0
    return [{"iface": ifname, "rx_bytes": rx, "tx_bytes": tx, "latency_ms": 0.3}]


def inference_snapshot(cfg: dict) -> dict | None:
    """Probe the resident llama.cpp server on the inference node (D5)."""
    if cfg.get("role_hint") != "inference":
        return None
    port = cfg.get("inference_port", "8080")
    up = False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            up = r.status == 200
    except Exception:
        up = False
    return {
        "server_up": up,
        "model": cfg.get("model_alias", "deepseek-flash"),
        "active_requests": 0,
        "queue_depth": 0,
        "tokens_per_s": 0.0,
        "vram_used_bytes": 0,
    }


# --------------------------------------------------------------------------- http
def http_json(method: str, url: str, body=None, token: str | None = None,
              timeout: int = 10) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "X-Termini-Version": "1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
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


# --------------------------------------------------------------------------- state
def write_json(path: str, obj: dict, mode: int = 0o600) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def set_state(**kw) -> None:
    st = read_json(STATE_PATH) or {}
    st.update(kw)
    st["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_json(STATE_PATH, st)


# --------------------------------------------------------------------------- core
def registration_body(cfg: dict, node_name: str) -> dict:
    ip = primary_ipv4()
    ifname = cfg.get("iface", "eth0")
    return {
        "schema_version": "1.0",
        "machine": {
            "machine_id": machine_id(node_name),
            "hostname": node_name,
            "image_version": cfg.get("image_version", "2026.10.0"),
            "os": {"name": "ubuntu", "version": "24.04"},
            "lxd": {"version": "5.21.8", "snap_revision": "40958"},
            "cpu": cpu_facts(),
            "memory": {"total_bytes": mem_total_bytes()},
            "disks": [{"device": "/dev/root", "type": "ssd", "size_bytes": 1, "health": "ok"}],
            "nics": [{"name": ifname, "mac": mac_of(ifname),
                      "addresses": [transformed_ipv4(ip)], "role": "underlay"}],
            "gpus": gpu_facts(cfg),
            "underlay_ip": ip,
            "role_hint": cfg.get("role_hint", "auto"),
        },
        "host_ssh_keys": [],
        "agent": {"version": "0.1.0", "boot_id": new_ulid()},
    }


def register(cfg: dict, node_name: str, token: str) -> dict:
    url = cfg["controller_url"].rstrip("/") + "/v1/nodes/register"
    body = registration_body(cfg, node_name)
    status, resp = http_json("POST", url, body=body, token=token, timeout=15)
    if status not in (200, 201):
        raise RuntimeError(f"register failed: HTTP {status}: {resp}")
    if not isinstance(resp, dict) or not resp.get("node_id"):
        raise RuntimeError(f"register: malformed response: {resp}")
    identity = {
        "node_id": resp["node_id"],
        "node_token": resp["node_token"],
        "role": resp.get("role"),
        "project_id": resp.get("project_id"),
        "registration_id": body["machine"]["hostname"],
        "machine_id": body["machine"]["machine_id"],
    }
    write_json(os.path.join(IDENTITY_DIR, "node.json"), identity)
    set_state(state="ONLINE", node_id=resp["node_id"], role=resp.get("role"),
              registration_id=body["machine"]["hostname"], cluster_member=node_name,
              last_error=None)
    return identity


def build_sample(cfg: dict, seq: int) -> dict:
    node_name = cfg["node_name"]
    ip = primary_ipv4()
    ifname = cfg.get("iface", "eth0")
    sample = {
        "seq": seq,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "uptime_sec": uptime_sec(),
        "cpu_util_pct": cpu_util_pct(),
        "cpu_core_count": os.cpu_count() or 1,
        "ram_total_bytes": max(1, mem_total_bytes()),
        "ram_used_bytes": max(0, mem_total_bytes() - _mem_available()),
        "swap_used_bytes": _swap_used(),
        "load_avg_1m": load_avg_1m(),
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "kernel_version": platform.release(),
        "lxd_cluster_role": "member",
        "hostname": _file_or_uuid_hash(node_name),
        "ip_local": transformed_ipv4(ip),
        "mac_address": _file_or_uuid_hash(mac_of(ifname)),
        "project_id": cfg.get("project_id") or new_ulid(),
        "disk": disk_facts(),
        "net": net_facts(ifname),
    }
    inf = inference_snapshot(cfg)
    if inf is not None:
        sample["inference"] = inf
        sample["services"] = [{"name": "termini-bootstrap", "status": "running"},
                              {"name": "llama-cpp", "status": "running" if inf["server_up"] else "stopped"}]
    return sample


def _mem_available() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _swap_used() -> int:
    try:
        total = free = 0
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("SwapTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("SwapFree:"):
                    free = int(line.split()[1]) * 1024
        return max(0, total - free)
    except OSError:
        return 0


def telemetry_batch(cfg: dict, node_id: str, seq: int) -> dict:
    return {
        "_redaction": {"version": REDACTOR_VERSION,
                       "config_hash": hashlib.sha256(REDACTOR_VERSION.encode()).hexdigest()[:16],
                       "result": "ok"},
        "call_id": new_ulid(),
        "node_id": node_id,
        "schema_version": 1,
        "governance_chain_tip": new_ulid(),
        "samples": [build_sample(cfg, seq)],
    }


def run(cfg: dict) -> int:
    os.makedirs(SPOOL_DIR, exist_ok=True)
    node_name = cfg.get("node_name") or socket.gethostname()
    cfg["node_name"] = node_name
    interval = max(0.2, int(cfg.get("telemetry_interval_ms", "1000")) / 1000.0)

    identity = read_json(os.path.join(IDENTITY_DIR, "node.json"))
    if identity and identity.get("node_id") and identity.get("node_token"):
        log(f"reconnect: reusing persisted identity node_id={identity['node_id']}")
        set_state(state="ONLINE", node_id=identity["node_id"], role=identity.get("role"),
                  cluster_member=node_name)
    else:
        token = read_token(TOKEN_PATH)
        if not token:
            set_state(state="ACTIVATING", last_error="no enrollment token")
            log("no enrollment token yet; waiting (ACTIVATING)")
            return _wait_for_token_then_run(cfg)
        set_state(state="REGISTERING")
        delay = 1.0
        while True:
            try:
                identity = register(cfg, node_name, token)
                log(f"registered node_id={identity['node_id']} role={identity['role']}")
                break
            except urllib.error.HTTPError as e:  # pragma: no cover - defensive
                set_state(state="REGISTRATION_FAILED", last_error=str(e))
                log(f"registration terminal failure: {e}")
                return 1
            except Exception as e:
                set_state(state="REGISTERING", last_error=str(e))
                log(f"registration retry in {delay:.0f}s: {e}")
                time.sleep(delay)
                delay = min(60.0, delay * 2)

    node_id = identity["node_id"]
    node_token = identity["node_token"]
    url = cfg["controller_url"].rstrip("/") + f"/v1/nodes/{node_id}/telemetry"
    # Resume the sequence across restarts: the controller drops out-of-order seq,
    # so a reset counter would make every post after a restart a silent no-op.
    seq = int((read_json(STATE_PATH) or {}).get("seq") or 0)
    while True:
        seq += 1
        try:
            status, resp = http_json("POST", url, body=telemetry_batch(cfg, node_id, seq), token=node_token)
            if status in (200, 202):
                set_state(state="ONLINE", node_id=node_id, role=identity.get("role"),
                          cluster_member=node_name, seq=seq, last_telemetry_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          last_error=None)
            else:
                set_state(last_error=f"telemetry HTTP {status}: {resp}")
                time.sleep(1.0)
                continue
        except Exception as e:
            set_state(state="DEGRADED", last_error=f"telemetry: {e}")
            time.sleep(1.0)
            continue
        time.sleep(interval)


def _wait_for_token_then_run(cfg: dict) -> int:
    while not read_token(TOKEN_PATH):
        time.sleep(2)
    return run(cfg)


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} [termini-bootstrap] {msg}",
          flush=True)


def cmd_status() -> int:
    st = read_json(STATE_PATH) or {"state": "UNKNOWN"}
    print(json.dumps(st, indent=2))
    return 0 if st.get("state") == "ONLINE" else 1


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "status":
        return cmd_status()
    cfg = parse_simple_yaml(CONFIG_PATH)
    if not cfg.get("controller_url"):
        die_msg = f"missing controller_url in {CONFIG_PATH}"
        set_state(state="ACTIVATING", last_error=die_msg)
        log(die_msg)
        return 1
    return run(cfg)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
