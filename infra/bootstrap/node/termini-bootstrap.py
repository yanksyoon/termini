#!/usr/bin/env python3
"""termini-bootstrap — node-side bootstrap agent for Termini nodes.

Canonical node half of `docs/contracts/node-bootstrap.md` (registration §5, SSH
key custody §7, telemetry §8, idempotency §10). It brings a pre-baked node
ONLINE against the real controller core in `controller/`:

  1. read `/etc/termini/config.yaml` + `/var/lib/termini/enrollment.token`
  2. gather machine facts (CPU/RAM/disk/NIC/GPU) and a stable `machine_id`
  3. `POST /v1/nodes/register` (Bearer enrollment token) -> node_id + node_token
     + assigned role; persist identity so a reboot reconnects instead of
     re-enrolling (contract §10)
  4. signal systemd READY **only after the controller has acknowledged**
     (`termini-bootstrap.service` is `Type=notify`), then stream telemetry
  5. emit an objective `boot_to_registered_ms` / `register_ms` timing line so the
     "< 5 s" registration budget can be measured from the log (task t_066b6e3d)

Timing definitions (both logged on acknowledgement):

  * `register_ms`            — monotonic ms from this process starting to the
                               controller's 2xx registration response. This is the
                               contract §12.5 metric ("systemctl start
                               termini-bootstrap" -> ONLINE) and excludes OS boot.
  * `boot_to_registered_ms`  — `/proc/uptime` (container/VM boot clock, lxcfs-
                               virtualised here) read at the instant of the ack,
                               i.e. OS boot timestamp -> controller ack.

The controller mints mTLS certs and LXD join tokens (contract §5); the core in
this repo returns `identity: null` / `cluster: null`, which is why this agent
runs over plain HTTP on the demo underlay — see infra/bootstrap/README.md.

Runs as a systemd unit (`termini-bootstrap.service`). `termini-bootstrap status`
prints the persisted state as JSON. `termini-bootstrap mint-trust-token` mints a
one-time LXD certificate-add trust token (contract §5/§6.3) so the controller can
add its client certificate to this node's LXD trust store; see
`controller/app/lxdtrust.py` for the controller half.
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import platform
import random
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
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

# Monotonic reference for the service-start -> ack interval. Set at import time,
# before config parsing / fact gathering, so the measurement is honest.
PROC_START_MONO = time.monotonic()


# --------------------------------------------------------------------------- timing / systemd
def boot_uptime_s() -> float | None:
    """Seconds since boot as the node sees it (`/proc/uptime`).

    In an LXD container lxcfs virtualises this to the instance's own boot clock,
    so it is a genuine boot->now timestamp, not the host's. Returns None when the
    file is unreadable (then boot->ack is simply omitted from the log line).
    """
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def register_ms() -> int:
    return int((time.monotonic() - PROC_START_MONO) * 1000)


def boot_to_registered_ms() -> int | None:
    up = boot_uptime_s()
    return None if up is None else int(up * 1000)


def sd_notify(payload: str) -> None:
    """Best-effort systemd sd_notify (READY=1 / STATUS=...).

    No-op when the unit is not `Type=notify` ($NOTIFY_SOCKET unset), so the same
    binary works under a plain `Type=simple` unit too.
    """
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    if sock_path.startswith("@"):  # abstract namespace
        sock_path = "\0" + sock_path[1:]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.connect(sock_path)
            s.sendall(payload.encode())
        finally:
            s.close()
    except OSError:
        pass


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
    up = boot_uptime_s()
    return int(up) if up else 0


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


# Last successfully measured values, used only as a fallback when a probe
# cannot run (e.g. the controller is unreachable). They are never the value a
# healthy sample reports; see `measure_controller_rtt_ms` / `_probe_disk`.
_NET_LATENCY_LAST: dict[str, float] = {}
_DISK_WRITE_LATENCY_LAST: dict[str, float] = {}

# Paths whose I/O latency the disk facts probe (device label, filesystem path).
_DISK_MOUNTS = (("/dev/root", "/"), ("/dev/termini", "/var/lib/termini"))

# Socket timeout for the controller RTT probe. Matches the inter-node connect
# budget in docs/contracts/agent-runtime.md (2 s), so a path degraded past the
# latency-01 threshold (250 ms) is still measured rather than timing out.
RTT_PROBE_TIMEOUT_S = 2.0
# Bytes per write-latency probe; large enough to reach the device (not just the
# page cache) but small enough that one probe per sample is free.
DISK_PROBE_BYTES = 8192
_PING_RTT_RE = re.compile(r"time=([0-9.]+)\s*ms")


def _controller_endpoint(cfg: dict) -> tuple[str, int] | None:
    """(host, port) of the controller the node registers/streams to."""
    try:
        parts = urllib.parse.urlsplit(cfg.get("controller_url") or "")
    except ValueError:
        return None
    host = parts.hostname
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return host, port


def _icmp_rtt_ms(host: str, timeout: float = RTT_PROBE_TIMEOUT_S) -> float | None:
    """One ICMP echo round-trip to `host` via the system `ping`, in ms.

    ICMP measures the bare path RTT to the controller without dragging in the
    controller process's accept/handling time, which is what the contract's
    "round-trip to controller" (`net[].latency_ms`) means. Returns None when
    `ping` is unavailable or the host does not answer.
    """
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-n", "-W", str(max(1, int(timeout))), host],
            capture_output=True, text=True, timeout=timeout + 1.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = _PING_RTT_RE.search(proc.stdout)
    return round(float(m.group(1)), 3) if m else None


def _tcp_connect_rtt_ms(host: str, port: int, timeout: float = RTT_PROBE_TIMEOUT_S) -> float | None:
    """Fallback RTT: time a TCP handshake to the controller host:port."""
    t0 = time.monotonic()
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    conn.close()
    return round((time.monotonic() - t0) * 1000.0, 3)


def measure_controller_rtt_ms(cfg: dict, timeout: float = RTT_PROBE_TIMEOUT_S) -> float | None:
    """Measure one round-trip to the controller, in ms.

    Contract §4.4 defines `net[].latency_ms` as the "round-trip to controller".
    ICMP echo is used first (the same probe the fan-network red-team harness
    uses, `w7/redteam/net/net_chaos.py`); a TCP handshake to the controller's
    port is the fallback when `ping` is not installed. Injected path delay
    (tc netem) therefore shows up directly in the sample instead of a
    hardcoded constant. Returns None when the controller is unreachable.
    """
    ep = _controller_endpoint(cfg)
    if ep is None:
        return None
    host, port = ep
    rtt = _icmp_rtt_ms(host, timeout)
    if rtt is not None:
        return rtt
    return _tcp_connect_rtt_ms(host, port, timeout)


def _probe_disk(path: str) -> tuple[float | None, bool]:
    """Time an fsync'd write into `path`; return `(latency_ms, mount_ro)`.

    `latency_ms` is the wall time to write `DISK_PROBE_BYTES` and fsync it
    (contract §4.3 "write I/O latency"), or None when the probe could not run.
    `mount_ro` is True only when the write failed with EROFS — the definitive
    "filesystem flipped read-only" signal behind disk-fail-01 — rather than the
    previous hardcoded False. A fixed probe file is truncated in place, so the
    probe adds no free-space drift of its own.
    """
    probe = os.path.join(path, ".termini-io-probe")
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError as e:
        return None, getattr(e, "errno", None) == errno.EROFS
    latency: float | None = None
    mount_ro = False
    try:
        t0 = time.monotonic()
        os.write(fd, b"\0" * DISK_PROBE_BYTES)
        os.fsync(fd)
        latency = round((time.monotonic() - t0) * 1000.0, 3)
    except OSError as e:
        mount_ro = getattr(e, "errno", None) == errno.EROFS
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return latency, mount_ro


def disk_facts() -> list:
    out = []
    for dev, path in _DISK_MOUNTS:
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total <= 0:
            continue
        latency, mount_ro = _probe_disk(path)
        if latency is None:
            latency = _DISK_WRITE_LATENCY_LAST.get(path, 0.0)
        else:
            _DISK_WRITE_LATENCY_LAST[path] = latency
        out.append({
            "path": dev,
            "total_bytes": total,
            "free_bytes": free,
            "write_latency_ms": latency,
            "mount_ro": mount_ro,
        })
    if not out:
        out.append({"path": "/dev/root", "total_bytes": 1, "free_bytes": 0,
                    "write_latency_ms": _DISK_WRITE_LATENCY_LAST.get("/", 0.0),
                    "mount_ro": False})
    return out


def net_facts(cfg: dict, ifname: str) -> list:
    try:
        with open(f"/sys/class/net/{ifname}/statistics/rx_bytes", encoding="utf-8") as fh:
            rx = int(fh.read().strip())
        with open(f"/sys/class/net/{ifname}/statistics/tx_bytes", encoding="utf-8") as fh:
            tx = int(fh.read().strip())
    except OSError:
        rx = tx = 0
    rtt = measure_controller_rtt_ms(cfg)
    if rtt is None:
        # Controller unreachable: report the last measured RTT rather than a
        # fabricated constant (0.0 only until the first successful probe).
        rtt = _NET_LATENCY_LAST.get(ifname, 0.0)
    else:
        _NET_LATENCY_LAST[ifname] = rtt
    return [{"iface": ifname, "rx_bytes": rx, "tx_bytes": tx, "latency_ms": rtt}]


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
        "model": cfg.get("model_alias", "gemma"),
        "active_requests": 0,
        "queue_depth": 0,
        "tokens_per_s": 0.0,
        "vram_used_bytes": 0,
    }


# ------------------------------------------------------- per-instance inventory
# `lxd_instances[]` — node-telemetry.md §4.6, C0.
#
# This is the field the AI operator resolves a remediation target through: the
# guardrail's G5 requires the model's target to resolve against
# `target_resource.instances[]`, and the executor then turns the resolved target
# into `PUT /1.0/instances/<instance>/state` on THIS node's LXD. A node that
# reports no inventory therefore makes every model-chosen action unresolvable
# (`fallback_reason=invalid_target` — the model is consulted and always
# discarded), which is what every real node did before this block existed: the
# field was missing by construction (task t_8b66366d).
#
# `vm_id` is a C0 id and the ingest validator requires a 26-character Crockford
# ULID (`controller/app/fields.py`: `"ulid": True`), while an LXD instance name is
# lowercase/DNS-ish and may carry customer hostname data — so the name can never
# BE the id, and it is never sent. The id is DERIVED from the name instead:
#
#     vm_id = 26 Crockford base32 characters of SHA-256("termini-vm-id:" + name)
#
# deterministic and one-way. The controller derives the same id from the instance
# names it reads back over the node's own LXD REST API
# (`controller/app/operator/vmids.py`) and resolves a reported id back to the
# instance it belongs to — so an id that matches no instance on the node being
# dialled is, by construction, not a target (the executor's fail-closed path).
# The demo seeder carries the same rule. `tests/test_node_instance_inventory.py`
# pins this implementation's output so the three copies cannot drift silently.
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
VM_ID_PREFIX = "termini-vm-id:"
VM_ID_LEN = 26

# LXD's daemon socket. Snap LXD (the baked node image) keeps it under
# /var/snap; a distro-packaged LXD under /var/lib. Same order as `lxc` itself.
LXD_SOCKETS = ("/var/snap/lxd/common/lxd/unix.socket", "/var/lib/lxd/unix.socket")
# Wall-clock ceiling for one LXD API call. A sample is produced every second, so
# a hung LXD must not hold the telemetry loop: a call that does not answer in
# time is "no inventory this sample", never an empty node (see below).
LXD_PROBE_TIMEOUT_S = float(os.environ.get("TERMINI_LXD_PROBE_TIMEOUT_S", "5") or 5)
# Wall-clock ceiling for one systemd probe (services[]).
SERVICE_PROBE_TIMEOUT_S = float(os.environ.get("TERMINI_SERVICE_PROBE_TIMEOUT_S", "3") or 3)

# LXD status string -> the contract's `state` enum (§4.6).
_LXD_STATE = {
    "running": "running",
    "stopped": "stopped",
    "frozen": "frozen",
    "paused": "paused",
    "error": "error",
    "ready": "starting",
    "starting": "starting",
}

# Previous (monotonic seconds, cumulative CPU nanoseconds) per instance, for the
# per-instance CPU percentage — LXD reports cumulative usage, not a rate.
_CPU_PREV: dict[str, tuple[float, int]] = {}


def vm_id_for_instance(name: str) -> str:
    """The `vm_id` this node reports for LXD instance `name` (see above).

    Byte-for-byte the rule the controller resolves with
    (`controller/app/operator/vmids.py`). 130 bits of SHA-256 are emitted MSB-first
    as 26 Crockford characters, so the result is a valid controller ULID.
    """
    digest = hashlib.sha256((VM_ID_PREFIX + str(name)).encode("utf-8")).digest()
    value = int.from_bytes(digest[:17], "big") >> 6
    out = []
    for _ in range(VM_ID_LEN):
        out.append(CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


class _LxdUnixConnection(http.client.HTTPConnection):
    """HTTP over LXD's unix socket (the stdlib has no unix-socket transport)."""

    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def lxd_socket_path() -> str | None:
    """Path of the local LXD socket, or None when LXD is not installed."""
    for path in LXD_SOCKETS:
        if os.path.exists(path):
            return path
    return None


def lxd_api_get(path: str, timeout: float | None = None) -> object | None:
    """GET `path` from this node's own LXD daemon over its unix socket.

    Returns the decoded `metadata`, or None when the call cannot be made or does
    not answer 200. Never raises: a stopped LXD, a missing socket or a junk
    response must leave the telemetry loop running.
    """
    sock_path = lxd_socket_path()
    if sock_path is None:
        return None
    conn = None
    try:
        conn = _LxdUnixConnection(sock_path, LXD_PROBE_TIMEOUT_S if timeout is None else timeout)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            log(f"lxd api {path}: HTTP {resp.status}")
            return None
        payload = json.loads(body.decode("utf-8") or "null")
    except (OSError, ValueError, http.client.HTTPException) as e:
        log(f"lxd api {path}: {type(e).__name__}: {e}")
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - close() on a dead socket
                pass
    if isinstance(payload, dict) and "metadata" in payload:
        return payload["metadata"]
    return None


def _instance_cpu_pct(name: str, usage_ns: object, now: float) -> float | None:
    """Per-instance CPU utilisation, in percent of this node's total capacity.

    LXD's `state.cpu.usage` is cumulative nanoseconds, so a rate needs two
    readings: the first sample for an instance reports NOTHING rather than a
    fabricated 0 (the contract field is optional; the daemon does the same for
    `net[].latency_ms` before its first measurement).
    """
    if not isinstance(usage_ns, (int, float)) or isinstance(usage_ns, bool):
        return None
    prev = _CPU_PREV.get(name)
    _CPU_PREV[name] = (now, int(usage_ns))
    if prev is None:
        return None
    dt = now - prev[0]
    d_ns = int(usage_ns) - prev[1]
    if dt <= 0 or d_ns < 0:          # clock jumped or the counter reset
        return None
    cores = max(1, os.cpu_count() or 1)
    pct = 100.0 * (d_ns / 1e9) / (dt * cores)
    return round(min(100.0, max(0.0, pct)), 2)


def _int_or_zero(value: object) -> int:
    """LXD byte counters as a non-negative int (bool is not a byte count)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value > 0 else 0


def _dict_of(value: object) -> dict:
    """`value` when it is a mapping, else an empty mapping (never None)."""
    return value if isinstance(value, dict) else {}


def lxd_instance_inventory(cfg: dict) -> list | None:
    """`lxd_instances[]` for this node, from its own LXD (contract §4.6).

    One entry per RUNNING instance (§4.6: "one entry per running VM"), with the
    contract fields. LXD is asked over its unix socket (`GET /1.0/instances?
    recursion=2`), which is the same view `lxc list` shows — no network, no trust
    prompt, and no subprocess on the sample path.

    Returns None when the inventory cannot be read (no LXD socket, LXD not
    answering, junk response) so the caller OMITS the field: "this node did not
    report an inventory" is the truth, whereas an empty list would claim a node
    running nothing and would make every model-chosen target unresolvable.
    `lxd_inventory: 0` in config.yaml disables collection explicitly.
    """
    if str(cfg.get("lxd_inventory", "1")).strip().lower() in ("0", "false", "no", "off"):
        return None
    items = lxd_api_get("/1.0/instances?recursion=2")
    if not isinstance(items, list):
        return None

    now = time.monotonic()
    out: list = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name")
        if not isinstance(name, str) or not name:
            continue
        state = _LXD_STATE.get(str(it.get("status") or "").strip().lower())
        if state != "running":
            continue
        seen.add(name)
        lxd_state = _dict_of(it.get("state"))
        memory = _dict_of(lxd_state.get("memory"))
        disk = _dict_of(lxd_state.get("disk"))
        root = _dict_of(disk.get("root"))
        cpu = _dict_of(lxd_state.get("cpu"))
        entry = {
            "vm_id": vm_id_for_instance(name),
            "state": state,
            "ram_used_bytes": _int_or_zero(memory.get("usage")),
            # LXD's effective memory limit for the instance (the configured
            # limits.memory, or the node's total when the instance is unlimited):
            # exactly the number `lxc list` prints in its MEMORY column.
            "ram_limit_bytes": _int_or_zero(memory.get("total")),
            # What LXD reports for the root volume. 0 on a storage driver that
            # does not track per-instance usage (the `dir` driver, which is what
            # the baked node image initialises) — the same value `lxc list` shows.
            "disk_used_bytes": _int_or_zero(root.get("usage")),
        }
        pct = _instance_cpu_pct(name, cpu.get("usage"), now)
        if pct is not None:
            entry["cpu_util_pct"] = pct
        out.append(entry)
    # Drop readings for instances that are gone, so a long-lived node does not
    # accumulate state for every instance it has ever run.
    for gone in [n for n in _CPU_PREV if n not in seen]:
        _CPU_PREV.pop(gone, None)
    return out


# ------------------------------------------------------------- service status
# `services[]` — node-telemetry.md §4.8, C0. Emitted on EVERY sample: the node
# cards render an empty services list as "nothing to show", which is
# indistinguishable from "the node reports no services at all" (task t_8b66366d).
#
# `name` is a fixed allowlist (§4.8 / fields.SERVICE_FIELDS): termini-bootstrap,
# lxd, sshd, llama-cpp. Nothing else may appear here, so a host-specific unit is
# reported under the allowlisted name of the component it is.
_SERVICE_UNITS = {
    "lxd": ("snap.lxd.daemon.service", "lxd.service"),
    "sshd": ("ssh.service", "sshd.service"),
}
_LLAMA_UNITS = ("termini-inference-proxy.service", "termini-llama-server.service")


def _unit_status(unit: str) -> str | None:
    """Contract status for one systemd unit, or None when it does not exist.

    "Does not exist" and "is stopped" are different answers: LoadState
    distinguishes them, and reporting `stopped` for a unit that is not installed
    on this node would invent a service the node does not have.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "show", "-p", "LoadState", "-p", "ActiveState", unit],
            capture_output=True, text=True, timeout=SERVICE_PROBE_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields = {}
    for line in (proc.stdout or "").splitlines():
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    if fields.get("LoadState") in (None, "", "not-found"):
        return None
    active = fields.get("ActiveState")
    if active == "active":
        return "running"
    if active == "failed":
        return "failed"
    return "stopped"


def _service_status(names: tuple) -> str | None:
    """Status of the first `name` that exists on this node, else None."""
    for unit in names:
        status = _unit_status(unit)
        if status is not None:
            return status
    return None


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when something accepts a TCP connection (fallback for `sshd`)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def llama_status(cfg: dict, inference: dict | None) -> str | None:
    """`llama-cpp` status where the node hosts the inference front door (D5).

    Present on the node that serves inference (Node C) and absent elsewhere, so a
    compute node does not advertise a service it does not run. The front door may
    be backed by local llama.cpp or forwarded to the managed model — either way
    the node-side component is the same one, and the alias/backend honesty label
    comes from `inference.model` and the API, not from here.
    """
    unit = _service_status(_LLAMA_UNITS)
    if str(cfg.get("role_hint") or "").strip().lower() != "inference" and unit is None:
        return None
    if inference is not None and inference.get("server_up"):
        return "running"
    return unit or "stopped"


def service_facts(cfg: dict, inference: dict | None = None) -> list:
    """`services[]` (contract §4.8) for this node — always populated.

    `termini-bootstrap` is running by construction: this sample is emitted by that
    unit. `lxd` falls back to the socket when systemd is not readable (so a node
    whose LXD answers is never reported as having no LXD), `sshd` falls back to
    the listener when no ssh unit is visible.
    """
    out = [{"name": "termini-bootstrap", "status": "running"}]

    lxd_status = _service_status(_SERVICE_UNITS["lxd"])
    if lxd_status is None:
        lxd_status = "running" if lxd_socket_path() is not None else "stopped"
    out.append({"name": "lxd", "status": lxd_status})

    sshd_status = _service_status(_SERVICE_UNITS["sshd"])
    if sshd_status is None:
        sshd_status = "running" if _port_open("127.0.0.1", 22) else "stopped"
    out.append({"name": "sshd", "status": sshd_status})

    llama = llama_status(cfg, inference)
    if llama is not None:
        out.append({"name": "llama-cpp", "status": llama})
    return out


# --------------------------------------------------------------------------- http
def http_json(method: str, url: str, body=None, token: str | None = None,
              timeout: float = 15) -> tuple[int, object]:
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


# --------------------------------------------------------------------------- LXD trust token
# The controller authenticates to this node's LXD with a TLS client certificate
# and needs it in the node's trust store (docs/contracts/node-bootstrap.md §5/§6.3,
# controller/app/lxdtrust.py). The node half of that handshake is minting a
# one-time LXD *certificate-add trust token* (`lxc config trust add` with no
# certificate) which the controller consumes over the network. Minting runs over
# the local LXD unix socket (root / lxd group); it is a provisioning step, not an
# interactive shell on the node.
#
# `termini-bootstrap mint-trust-token` prints the token and writes it 0600 to
# /var/lib/termini/lxd-trust-token, from where it is handed to the controller
# (TERMINI_LXD_TRUST_TOKEN, or `python3 -m app.lxdtrust trust --token`).
_TRUST_TOKEN_PATH = os.environ.get(
    "TERMINI_LXD_TRUST_TOKEN_PATH", "/var/lib/termini/lxd-trust-token"
)
# A CertificateAddToken is base64 of a JSON blob — one long base64 run.
_TRUST_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=]{40,}")


def extract_trust_token(text: str) -> str | None:
    """Pull the base64 CertificateAddToken out of `lxc config trust add` output."""
    matches = _TRUST_TOKEN_RE.findall(text or "")
    return max(matches, key=len) if matches else None


def mint_lxd_trust_token(name: str = "termini-controller",
                         *, timeout: float = 30.0) -> str | None:
    """Mint a one-time LXD trust token on this node; return it (None on failure).

    Requires LXD to be initialised on the node and the caller to have local
    admin access (root or the `lxd` group). Any failure is logged and returned
    as None — a node that cannot mint a token still registers and reports.
    """
    try:
        proc = subprocess.run(
            ["lxc", "config", "trust", "add", "--force-local", "--name", name],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log(f"cannot mint LXD trust token: {e}")
        return None
    if proc.returncode != 0:
        log("cannot mint LXD trust token: "
            + (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"))
        return None
    token = extract_trust_token(proc.stdout)
    if not token:
        log("cannot mint LXD trust token: no token in output: "
            + proc.stdout.strip()[:200])
    return token


def cmd_mint_trust_token(argv: list[str]) -> int:
    name = "termini-controller"
    out = _TRUST_TOKEN_PATH
    args = argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--name" and i + 1 < len(args):
            name, i = args[i + 1], i + 2
        elif args[i] == "--out" and i + 1 < len(args):
            out, i = args[i + 1], i + 2
        else:
            i += 1
    token = mint_lxd_trust_token(name)
    if not token:
        return 1
    try:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
    except OSError as e:
        log(f"minted token but could not write {out}: {e}")
    print(token)
    log(f"minted LXD trust token name={name} -> {out}")
    return 0


# --------------------------------------------------------------------------- registration failure policy
# Bounded, classified registration retry policy (node-bootstrap.md §9).
#
# The old loop collapsed every failure into one `except Exception` that backed
# off forever, so a consumed 401 token, a bad DNS name or a refused connection
# all sat in REGISTERING indefinitely (W7 findings F1/F2). `register()` now
# raises a *classified* `RegistrationError`; the retry loop uses `kind` to decide
# terminal vs. transient:
#
#   auth       401/403 — bad/consumed/expired token -> <=3 backoff retries then REGISTRATION_FAILED
#   schema     400     — malformed body             -> <=3 backoff retries then REGISTRATION_FAILED
#   unsupported 426    — image/controller skew      -> terminal immediately (operator alert)
#   conflict   409     — machine_id already present -> reconnect path, else bounded->terminal
#   malformed  2xx without node_id                  -> bounded -> terminal
#   unreachable_dns / unreachable_refused / unreachable_timeout / unreachable
#              controller not reachable -> fail fast: <=3 backoff retries then
#              terminal REGISTRATION_FAILED (state DEGRADED *during* the retries,
#              so the node is never silently stuck in the transient REGISTERING)
#   rate_limited 429 / server 5xx -> backoff 1 s -> 60 s cap, indefinitely (§9 rows 1/3)
DEFAULT_REGISTRATION_MAX_RETRIES = 3
DEFAULT_REGISTER_TIMEOUT_S = 5.0
TERMINAL_KINDS = frozenset({
    "auth", "schema", "unsupported", "conflict", "malformed",
    "unreachable", "unreachable_dns", "unreachable_refused", "unreachable_timeout",
})

# 0 = hold the terminal state forever (production); tests set a bound.
TERMINAL_HOLD_S = float(os.environ.get("TERMINI_TERMINAL_HOLD_S", "0") or 0)


class RegistrationError(Exception):
    """Classified registration failure carrying what the retry policy needs."""

    def __init__(self, kind: str, status: int | None, message: str):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.terminal = kind in TERMINAL_KINDS


def classify_http_error(status: int, resp: object) -> "RegistrationError":
    """Map a non-2xx registration response to a classified failure."""
    code = ""
    if isinstance(resp, dict) and isinstance(resp.get("error"), dict):
        code = str(resp["error"].get("code") or "")
    detail = f"register failed: HTTP {status}" + (f" {code}" if code else "") + f": {resp}"
    if status in (401, 403):
        return RegistrationError("auth", status, detail)
    if status == 426 or code == "UNSUPPORTED_VERSION":
        return RegistrationError("unsupported", status, detail)
    if status == 400:
        return RegistrationError("schema", status, detail)
    if status == 409:
        return RegistrationError("conflict", status, detail)
    if status == 429 or code == "RATE_LIMITED":
        return RegistrationError("rate_limited", status, detail)
    return RegistrationError("server", status, detail)  # 5xx: transient, retry forever


def classify_network_error(exc: BaseException) -> "RegistrationError":
    """Map a socket/URL error to a classified failure.

    DNS failure and connection-refused are separated from a generic timeout so
    the operator sees *why* the node failed fast, not just "cannot connect".
    """
    reason = getattr(exc, "reason", None) or exc
    if isinstance(reason, socket.gaierror):
        kind, hint = "unreachable_dns", "DNS name could not be resolved"
    elif isinstance(reason, ConnectionRefusedError):
        kind, hint = "unreachable_refused", "connection refused"
    elif isinstance(reason, ConnectionResetError):
        kind, hint = "unreachable_refused", "connection reset"
    elif isinstance(reason, (socket.timeout, TimeoutError)):
        kind, hint = "unreachable_timeout", "timed out"
    else:
        kind, hint = "unreachable", "network error"
    return RegistrationError(kind, None, f"register unreachable ({hint}): {reason}")


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
def root_disk_bytes() -> int:
    """Root filesystem size in bytes from ``statvfs``; 0 when it cannot be measured."""
    try:
        st = os.statvfs("/")
    except OSError:
        return 0
    return st.f_blocks * st.f_frsize


def registration_disk_facts() -> list:
    """Registered disk facts (`machine.disks[]`, contract §5.2), from the real device.

    This used to be a hardcoded ``size_bytes: 1`` placeholder. Every real node
    therefore registered a 1-byte root disk, which the controller reads as 0 GiB of
    disk (``1 // GiB == 0``): the per-node disk ceiling was silently never enforced
    and the node looked disk-less. The size is measured here instead; when it cannot
    be measured the key is OMITTED, which the controller reads as "disk not
    reported" (``health.capacity_of`` -> ``disk_known = False``) rather than as a
    fabricated number.
    """
    total = root_disk_bytes()
    disk: dict = {"device": "/dev/root", "type": "ssd", "health": "ok"}
    if total > 0:
        disk["size_bytes"] = total
    return [disk]


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
            "lxd": {"version": "5.21.8", "snap_revision": "40958",
                    # The node's own LXD REST address. The controller cannot place a
                    # VM without it (a single global URL cannot name one node among a
                    # fleet); an older node omits it and the controller falls back.
                    "endpoint": f"{ip}:{cfg.get('lxd_port', 8443)}"},
            "cpu": cpu_facts(),
            "memory": {"total_bytes": mem_total_bytes()},
            "disks": registration_disk_facts(),
            "nics": [{"name": ifname, "mac": mac_of(ifname),
                      "addresses": [transformed_ipv4(ip)], "role": "underlay"}],
            "gpus": gpu_facts(cfg),
            "underlay_ip": ip,
            "role_hint": cfg.get("role_hint", "auto"),
        },
        "host_ssh_keys": host_ssh_keys(cfg),
        "agent": {"version": "0.1.0", "boot_id": new_ulid()},
    }


def host_ssh_keys(cfg: dict) -> list:
    """Host SSH public keys reported at registration (contract §5.2/§7).

    cloud-init drops the node's host keys' public halves under
    `/etc/termini/ssh/host_keys.pub` (one per line) when present; absent, this is
    an empty list and registration still succeeds.
    """
    raw = cfg.get("host_ssh_keys_file", "/etc/termini/ssh/host_keys.pub")
    try:
        with open(raw, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    except OSError:
        return []


def register(cfg: dict, node_name: str, token: str) -> dict:
    url = cfg["controller_url"].rstrip("/") + "/v1/nodes/register"
    body = registration_body(cfg, node_name)
    timeout = float(cfg.get("register_timeout_s", DEFAULT_REGISTER_TIMEOUT_S))
    try:
        status, resp = http_json("POST", url, body=body, token=token, timeout=timeout)
    except urllib.error.HTTPError as e:  # pragma: no cover - http_json returns codes
        raise RegistrationError("server", e.code, f"register failed: HTTP {e.code}: {e}") from e
    except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError) as e:
        # DNS failure, connection refused/reset, connect timeout: classify so the
        # retry loop can fail fast instead of retrying silently forever (F2).
        raise classify_network_error(e) from e
    acked_mono_ms = register_ms()
    acked_boot_ms = boot_to_registered_ms()
    if status not in (200, 201):
        raise classify_http_error(status, resp)
    if not isinstance(resp, dict) or not resp.get("node_id"):
        raise RegistrationError("malformed", status, f"register: malformed response: {resp}")
    identity = {
        "node_id": resp["node_id"],
        "node_token": resp["node_token"],
        "role": resp.get("role"),
        "project_id": resp.get("project_id"),
        "registration_id": body["machine"]["hostname"],
        "machine_id": body["machine"]["machine_id"],
        "register_ms": acked_mono_ms,
        "boot_to_registered_ms": acked_boot_ms,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_json(os.path.join(IDENTITY_DIR, "node.json"), identity)
    set_state(state="ONLINE", node_id=resp["node_id"], role=resp.get("role"),
              registration_id=body["machine"]["hostname"], cluster_member=node_name,
              register_ms=acked_mono_ms, boot_to_registered_ms=acked_boot_ms,
              last_error=None, failure_kind=None, terminal=False, registration_attempts=0)
    log(f"REGISTERED node_id={resp['node_id']} role={resp.get('role')} "
        f"register_ms={acked_mono_ms} boot_to_registered_ms={acked_boot_ms}")
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
        "net": net_facts(cfg, ifname),
    }
    # Per-instance inventory (§4.6). OMITTED when the node cannot read its own
    # LXD: "did not report" is the truth, an empty list would claim the node runs
    # nothing (and would make every model-chosen remediation target unresolvable).
    inventory = lxd_instance_inventory(cfg)
    if inventory is not None:
        sample["lxd_instances"] = inventory
    inf = inference_snapshot(cfg)
    if inf is not None:
        sample["inference"] = inf
    # Emitted on EVERY sample (§4.8): a missing list renders as "no services".
    sample["services"] = service_facts(cfg, inf)
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


def notify_ready(identity: dict) -> None:
    """Tell systemd the unit is up — ONLY once the controller has acknowledged."""
    sd_notify(f"READY=1\nSTATUS=registered node_id={identity.get('node_id')} "
              f"role={identity.get('role')}")


# --------------------------------------------------------------------------- registration retry loop
def terminal_hold() -> None:
    """Park the process in its terminal state (no auto-retry).

    `termini-bootstrap.service` is `Restart=always`; simply exiting would make
    systemd respawn the daemon straight back into the retry loop, so a genuinely
    terminal REGISTRATION_FAILED must hold the process. READY is never sent
    (the unit never goes active), and `state.json` stays observable for the
    operator. `TERMINI_TERMINAL_HOLD_S > 0` bounds the hold (tests); SIGTERM
    still stops the unit normally.
    """
    if TERMINAL_HOLD_S > 0:
        time.sleep(TERMINAL_HOLD_S)
        return
    while True:
        time.sleep(3600)


def _park_registration_failed(err: RegistrationError, attempts: int) -> None:
    """Enter the terminal REGISTRATION_FAILED state and hold it (§9 row 2)."""
    set_state(state="REGISTRATION_FAILED", last_error=str(err),
              failure_kind=err.kind, registration_attempts=attempts, terminal=True)
    log(f"REGISTRATION_FAILED (terminal, kind={err.kind}, attempts={attempts}): {err}")
    log("no auto-retry: operator must re-provision (fresh token / fix config) "
        "and restart termini-bootstrap")
    sd_notify(f"STATUS=REGISTRATION_FAILED kind={err.kind}")  # never READY
    terminal_hold()


def register_with_retries(cfg: dict, node_name: str, token: str) -> dict | None:
    """Register, applying the classified §9 retry policy.

    Returns the persisted identity on a 2xx acknowledgement. On a terminal
    failure it parks the daemon in REGISTRATION_FAILED (holding, no auto-retry)
    and returns None. Transient controller errors (5xx / 429) back off 1 s → 60 s
    indefinitely so the node keeps trying without ever lying about its state.
    """
    max_retries = int(cfg.get("registration_max_retries", DEFAULT_REGISTRATION_MAX_RETRIES))
    base = max(0.1, float(cfg.get("registration_backoff_base_s", 1.0)))
    cap = max(base, float(cfg.get("registration_backoff_cap_s", 60.0)))
    delay = base
    retries: dict[str, int] = {}
    while True:
        try:
            identity = register(cfg, node_name, token)
            notify_ready(identity)  # READY only after the 2xx acknowledgement
            return identity
        except RegistrationError as e:
            n = retries.get(e.kind, 0) + 1
            retries[e.kind] = n
            if e.terminal and n > max_retries:
                _park_registration_failed(e, n)
                return None
            # A controller we cannot reach is DEGRADED while we back off (§9 row
            # 1); every other transient failure keeps the REGISTERING state.
            during = "DEGRADED" if e.kind.startswith("unreachable") else "REGISTERING"
            set_state(state=during, last_error=str(e), failure_kind=e.kind,
                      registration_attempts=n)
            budget = max_retries if e.terminal else "inf"
            log(f"registration retry {n}/{budget} in {delay:.0f}s: {e}")
            time.sleep(delay)
            delay = min(cap, delay * 2)
        except Exception as e:  # noqa: BLE001 - never retry an unknown failure silently forever
            n = retries.get("unexpected", 0) + 1
            retries["unexpected"] = n
            if n > max_retries:
                _park_registration_failed(
                    RegistrationError("unexpected", None, f"unexpected error: {e!r}"), n)
                return None
            set_state(state="REGISTERING", last_error=f"unexpected: {e}",
                      registration_attempts=n)
            log(f"registration retry {n}/{max_retries} in {delay:.0f}s: unexpected error: {e}")
            time.sleep(delay)
            delay = min(cap, delay * 2)


def run(cfg: dict) -> int:
    os.makedirs(SPOOL_DIR, exist_ok=True)
    node_name = cfg.get("node_name") or socket.gethostname()
    cfg["node_name"] = node_name
    interval = max(0.2, int(cfg.get("telemetry_interval_ms", "1000")) / 1000.0)

    identity = read_json(os.path.join(IDENTITY_DIR, "node.json"))
    if identity and identity.get("node_id") and identity.get("node_token"):
        log(f"reconnect: reusing persisted identity node_id={identity['node_id']}")
        set_state(state="ONLINE", node_id=identity["node_id"], role=identity.get("role"),
                  cluster_member=node_name, last_error=None, failure_kind=None, terminal=False)
        notify_ready(identity)          # idempotent reboot path: already enrolled
    else:
        token = read_token(TOKEN_PATH)
        if not token:
            set_state(state="ACTIVATING", last_error="no enrollment token")
            log("no enrollment token yet; waiting (ACTIVATING)")
            return _wait_for_token_then_run(cfg)
        set_state(state="REGISTERING")
        identity = register_with_retries(cfg, node_name, token)
        if identity is None:
            return 1  # terminal REGISTRATION_FAILED: parked, no auto-retry

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
    if len(argv) > 1 and argv[1] == "mint-trust-token":
        return cmd_mint_trust_token(argv)
    cfg = parse_simple_yaml(CONFIG_PATH)
    if not cfg.get("controller_url"):
        die_msg = f"missing controller_url in {CONFIG_PATH}"
        set_state(state="ACTIVATING", last_error=die_msg)
        log(die_msg)
        return 1
    return run(cfg)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
