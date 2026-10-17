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

import calendar
import errno
import hashlib
import http.client
import json
import os
import platform
import random
import re
import socket
import ssl
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
    """`disk[]` (§4.3) plus the §4.13 extension (metric expansion, t_cae0dfa8).

    Rows are kept in `_DISK_MOUNTS` order and capped at `DISK_MAX` — the ceiling
    is asserted by test, so a growing probe list cannot silently grow the sample.
    The extension fields are added only where the source actually answered:
    `inode_used`/`inode_total` from the same `statvfs` that produced the byte
    figures, and `io_in_flight`/`io_ticks_ms` from the block device behind the
    mount (`_block_io_of`). A mount whose device cannot be resolved ships the
    §4.3 fields and omits the §4.13 ones — never a `0` that would read as "no
    I/O happening".
    """
    out = []
    majmin = _mount_majmin()   # one /proc/self/mountinfo read for every mount
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
        entry = {
            "path": dev,
            "total_bytes": total,
            "free_bytes": free,
            "write_latency_ms": latency,
            "mount_ro": mount_ro,
        }
        # §4.13 inode exhaustion: a full inode table fails writes while
        # `free_bytes` still looks healthy. Both fields or neither — a
        # filesystem that reports no inode table (f_files < 1) gets an honest
        # omission, not a `0` total that division by would turn into a lie.
        if st.f_files >= 1:
            entry["inode_total"] = st.f_files
            entry["inode_used"] = max(0, st.f_files - st.f_ffree)
        io = _block_io_of(path, majmin)
        if io is not None:
            entry["io_in_flight"], entry["io_ticks_ms"] = io
        out.append(entry)
    if not out:
        out.append({"path": "/dev/root", "total_bytes": 1, "free_bytes": 0,
                    "write_latency_ms": _DISK_WRITE_LATENCY_LAST.get("/", 0.0),
                    "mount_ro": False})
    return out[:DISK_MAX]


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
    entry = {"iface": ifname, "rx_bytes": rx, "tx_bytes": tx, "latency_ms": rtt}
    # §4.13 NIC error/drop counters (metric expansion, t_cae0dfa8): they are what
    # split "slow network" from "lossy network" for latency-01. Each is omitted
    # when its statistics file is unreadable — unlike `rx_bytes` above, which
    # has always fallen back to 0, a NEW field must never invent a clean
    # interface on a node that could not look.
    for counter in _NET_ERROR_COUNTERS:
        value = _iface_counter(ifname, counter)
        if value is not None:
            entry[counter] = value
    # One row per sample, the configured interface only (`NET_MAX`, §4.4/§4.13).
    return [entry][:NET_MAX]


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


# Wall-clock ceiling for one systemd probe (services[]).
SERVICE_PROBE_TIMEOUT_S = float(os.environ.get("TERMINI_SERVICE_PROBE_TIMEOUT_S", "3") or 3)


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
# This daemon's own unit. `termini-bootstrap` is reported on every sample, so its
# operational fields (§4.8) are read from systemd like any other unit rather than
# being asserted as a constant.
_BOOTSTRAP_UNITS = ("termini-bootstrap.service",)
_LLAMA_UNITS = ("termini-inference-proxy.service", "termini-llama-server.service")


# systemd properties read for one unit, in a single `systemctl show` call (§4.8).
# The operational fields (t_957956e5) ride the same call that produces `status`,
# so the extra evidence costs no additional subprocess.
_SERVICE_SHOW_PROPS = (
    "LoadState", "ActiveState", "NRestarts", "ExecMainStatus",
    "ActiveEnterTimestampMonotonic",
)

# Journal-count window and refresh (§4.11, recorded decision t_957956e5).
JOURNAL_WINDOW_S = int(os.environ.get("TERMINI_JOURNAL_WINDOW_S", "300") or 300)
JOURNAL_REFRESH_S = float(os.environ.get("TERMINI_JOURNAL_REFRESH_S", "60") or 60)


def _int_or_none(value: object) -> int | None:
    """An int from a `systemctl show` value, or None when unset/unparseable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _active_age_sec(value: object) -> int | None:
    """Seconds since a unit entered its current active state, or None.

    `ActiveEnterTimestampMonotonic` is microseconds since boot, and 0 when the
    unit has never been active; /proc/uptime provides the matching reference.
    """
    us = _int_or_none(value)
    if us is None or us <= 0:
        return None
    up = boot_uptime_s()
    if not up:
        return None
    return max(0, int(up - us / 1e6))


def _unit_facts(unit: str) -> dict | None:
    """Contract service facts for one systemd unit, or None when it does not exist.

    "Does not exist" and "is stopped" are different answers: LoadState
    distinguishes them, and reporting `stopped` for a unit that is not installed
    on this node would invent a service the node does not have.

    Each operational field (§4.8) is omitted when systemd does not report it: a
    node that cannot read systemd must not render as "0 restarts, exit 0".
    """
    args = ["systemctl", "show"]
    for prop in _SERVICE_SHOW_PROPS:
        args += ["-p", prop]
    args.append(unit)
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True,
            timeout=SERVICE_PROBE_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields: dict = {}
    for line in (proc.stdout or "").splitlines():
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    if fields.get("LoadState") in (None, "", "not-found"):
        return None
    active = fields.get("ActiveState")
    if active == "active":
        facts: dict = {"status": "running"}
    elif active == "failed":
        facts = {"status": "failed"}
    else:
        facts = {"status": "stopped"}
    restarts = _int_or_none(fields.get("NRestarts"))
    if restarts is not None and restarts >= 0:
        facts["restart_count"] = restarts
    exit_code = _int_or_none(fields.get("ExecMainStatus"))
    if exit_code is not None and -1 <= exit_code <= 255:
        facts["last_exit_code"] = exit_code
    age = _active_age_sec(fields.get("ActiveEnterTimestampMonotonic"))
    if age is not None:
        facts["state_age_sec"] = age
    return facts


def _first_unit_facts(names: tuple) -> tuple[str | None, dict | None]:
    """`(unit, facts)` for the first name that exists on this node, else `(None, None)`."""
    for unit in names:
        facts = _unit_facts(unit)
        if facts is not None:
            return unit, facts
    return None, None


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when something accepts a TCP connection (fallback for `sshd`)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Journal counts are recomputed at most once per JOURNAL_REFRESH_S: the demo
# cadence is 1 s, and a journal sweep per second per unit would be pure cost.
_JOURNAL_CACHE: dict[str, tuple[float, dict]] = {}


def journal_counts(unit: str) -> dict:
    """Counts of `err` / `warning` journal entries for `unit` (§4.11).

    Counts, never lines: `--output-fields=PRIORITY` makes journalctl emit only
    the priority of each entry, so no message, identifier or payload is ever
    read into this process. An unreadable journal yields `{}` — the caller then
    omits the fields — because "0 errors" must never be invented from "could not
    ask"; a readable journal that genuinely has none returns real zeros.
    """
    now = time.monotonic()
    cached = _JOURNAL_CACHE.get(unit)
    if cached is not None and (now - cached[0]) < JOURNAL_REFRESH_S:
        return dict(cached[1])
    counts: dict = {}
    try:
        proc = subprocess.run(
            ["journalctl", "-u", unit, "--since", f"-{JOURNAL_WINDOW_S}s",
             "-p", "warning", "--no-pager", "-q", "--output=json",
             "--output-fields=PRIORITY"],
            capture_output=True, text=True,
            timeout=SERVICE_PROBE_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        errors = warnings = 0
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            prio = _int_or_none(entry.get("PRIORITY"))
            if prio is None:
                continue
            if prio <= 3:          # emerg, alert, crit, err
                errors += 1
            elif prio == 4:        # warning
                warnings += 1
        counts = {"journal_errors": errors, "journal_warnings": warnings}
    _JOURNAL_CACHE[unit] = (now, dict(counts))
    return counts


def llama_status(cfg: dict, inference: dict | None) -> str | None:
    """`llama-cpp` status where the node hosts the inference front door (D5).

    Present on the node that serves inference (Node C) and absent elsewhere, so a
    compute node does not advertise a service it does not run. The front door may
    be backed by local llama.cpp or forwarded to the managed model — either way
    the node-side component is the same one, and the alias/backend honesty label
    comes from `inference.model` and the API, not from here.
    """
    unit, _facts = _first_unit_facts(_LLAMA_UNITS)
    if str(cfg.get("role_hint") or "").strip().lower() != "inference" and unit is None:
        return None
    if inference is not None and inference.get("server_up"):
        return "running"
    if _facts is None:
        return "stopped"
    return _facts["status"]


def _service_entry(key: str, unit: str | None, facts: dict | None,
                   default_status: str) -> dict:
    """One `services[]` row: the allowlisted `name`, plus whatever was measured.

    The operational fields are copied only when the probe produced them, so a
    field the node cannot know is absent rather than zero (§4.8).
    """
    entry: dict = {"name": key, "status": default_status}
    if facts:
        entry["status"] = facts.get("status", default_status)
        for field in ("state_age_sec", "restart_count", "last_exit_code"):
            if field in facts:
                entry[field] = facts[field]
    if unit:
        entry.update(journal_counts(unit))
    return entry


def service_facts(cfg: dict, inference: dict | None = None) -> list:
    """`services[]` (contract §4.8) for this node — always populated.

    `termini-bootstrap` is running by construction: this sample is emitted by that
    unit, so a failed probe falls back to `running` rather than inventing a
    failure of the process that is talking. `lxd` falls back to the socket when
    systemd is not readable (so a node whose LXD answers is never reported as
    having no LXD), `sshd` falls back to the listener when no ssh unit is visible.
    """
    out: list = []

    boot_unit, boot_facts = _first_unit_facts(_BOOTSTRAP_UNITS)
    out.append(_service_entry("termini-bootstrap", boot_unit, boot_facts, "running"))

    lxd_unit, lxd_facts = _first_unit_facts(_SERVICE_UNITS["lxd"])
    lxd_status = (lxd_facts or {}).get("status")
    if lxd_status is None:
        lxd_status = "running" if lxd_socket_path() is not None else "stopped"
    out.append(_service_entry("lxd", lxd_unit, lxd_facts, lxd_status))

    ssh_unit, ssh_facts = _first_unit_facts(_SERVICE_UNITS["sshd"])
    ssh_status = (ssh_facts or {}).get("status")
    if ssh_status is None:
        ssh_status = "running" if _port_open("127.0.0.1", 22) else "stopped"
    out.append(_service_entry("sshd", ssh_unit, ssh_facts, ssh_status))

    llama = llama_status(cfg, inference)
    if llama is not None:
        llama_unit, llama_facts = _first_unit_facts(_LLAMA_UNITS)
        if llama_facts is not None and (inference is None or not inference.get("server_up")):
            llama = llama_facts["status"]
        elif llama_facts is None:
            llama = "running" if (inference is not None and inference.get("server_up")) else "stopped"
        out.append(_service_entry("llama-cpp", llama_unit, llama_facts, llama))
    return out


# ------------------------------------------------------------------- processes
# `processes[]` — node-telemetry.md §4.10, C0, METRICS ONLY (t_957956e5).
#
# Deny-by-default review admitted process METRICS and nothing else: the kernel
# task name (`comm`), the kernel state, and the counters. This daemon never reads
# `/proc/<pid>/cmdline`, `/proc/<pid>/environ`, the `exe` link, `cwd` or any open
# file path — so the C2 classes are not collected at all rather than redacted
# `tests/test_telemetry_processes.py` pins that an emitted entry has
# exactly the contract keys, and the controller rejects any of those C2 names at
# ingest even if a compromised node sent one.
PROCESSES_MAX = 10  # mirrors controller/app/fields.PROCESSES_MAX (§4.10)

# Cumulative CPU ticks per pid from the previous sample, for the CPU percentage.
_PROC_PREV: dict[int, tuple[float, int]] = {}

# /proc/<pid>/stat state letter -> the contract's `state` enum (§4.10). A letter
# outside this map (the kernel has states the contract does not enumerate) omits
# the field rather than being reported under a name that would be a lie.
_PROC_STATE = {
    "R": "running",
    "S": "sleeping",
    "D": "disk_sleep",
    "Z": "zombie",
    "T": "stopped",
    "t": "tracing",
    "X": "dead",
    "x": "dead",
}


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _comm_of(stat_text: str) -> str | None:
    """The kernel task name from a `/proc/<pid>/stat` line, contract-clean.

    `stat` is `pid (comm) state ...` and `comm` may itself contain spaces and
    parentheses, so it is taken between the FIRST `(` and the LAST `)`. It is
    then reduced to ≤15 printable ASCII characters — the kernel's TASK_COMM_LEN —
    which is both the contract's pattern (§4.10) and what makes the field
    structurally incapable of carrying prose, a path or a URL.
    """
    start = stat_text.find("(")
    end = stat_text.rfind(")")
    if start < 0 or end <= start:
        return None
    cleaned = "".join(
        ch if 0x20 <= ord(ch) <= 0x7E else "?" for ch in stat_text[start + 1:end]
    ).strip()[:15]
    return cleaned or None


def _rank_key(row: tuple) -> tuple:
    """`processes[]` total order (§4.10): RSS desc, then CPU desc, then `pid` asc.

    RSS is the **primary** key — the owner's note of 2026-10-16 set the window at
    "top-10 by RSS per sample". CPU and `pid` only break ties, so two readers of
    the same host agree on the set. A process with no CPU delta available yet
    carries the `-1.0` sentinel, which sorts below any real percentage rather
    than inventing a `0` (§4.10 omits `cpu_util_pct` on that sample).

    `row` is the collector's `(cpu_util_pct_or_minus_one, ram_used_bytes, pid,
    entry)` tuple.
    """
    cpu, ram, pid = row[0], row[1], row[2]
    return (-ram, -cpu, pid)


def process_facts() -> list | None:
    """`processes[]` (contract §4.10): the node's top-`PROCESSES_MAX` processes.

    Ranked by `ram_used_bytes` descending, then `cpu_util_pct` descending, then
    `pid` ascending (`_rank_key`) — the owner's top-10-by-RSS bound, and a total
    order, so two readers of the same host agree.

    Returns None when `/proc` cannot be listed, so the caller OMITS the field:
    "this node did not report a process table" is the truth, whereas an empty
    list would claim the node runs nothing (same rule as §4.6).
    """
    try:
        pids = sorted(int(name) for name in os.listdir("/proc") if name.isdigit())
    except OSError:
        return None

    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):
        page_size = 4096
    try:
        clk = int(os.sysconf("SC_CLK_TCK")) or 100
    except (ValueError, OSError, AttributeError):
        clk = 100
    cores = max(1, os.cpu_count() or 1)
    now = time.monotonic()
    up = boot_uptime_s()

    rows: list[tuple[float, int, int, dict]] = []
    live: set = set()
    for pid in pids:
        stat_text = _read_text(f"/proc/{pid}/stat")
        if not stat_text:
            continue
        rparen = stat_text.rfind(")")
        if rparen < 0:
            continue
        rest = stat_text[rparen + 2:].split()
        if len(rest) < 20:
            continue
        live.add(pid)

        entry: dict = {"pid": pid}
        comm = _comm_of(stat_text)
        if comm:
            entry["name"] = comm
        state = _PROC_STATE.get(rest[0])
        if state:
            entry["state"] = state

        utime = _int_or_none(rest[11])
        stime = _int_or_none(rest[12])
        ticks = None if utime is None or stime is None else utime + stime
        pct = None
        if ticks is not None:
            prev = _PROC_PREV.get(pid)
            _PROC_PREV[pid] = (now, ticks)
            if prev is not None and now > prev[0] and ticks >= prev[1]:
                pct = 100.0 * (ticks - prev[1]) / clk / (now - prev[0]) / cores
                pct = round(min(100.0, max(0.0, pct)), 2)
        if pct is not None:
            entry["cpu_util_pct"] = pct

        statm = _read_text(f"/proc/{pid}/statm")
        resident = None
        if statm:
            parts = statm.split()
            if len(parts) >= 2:
                resident = _int_or_none(parts[1])
        entry["ram_used_bytes"] = max(0, (resident or 0) * page_size)

        threads = _int_or_none(rest[17])
        if threads is not None and threads >= 1:
            entry["threads"] = threads

        starttime = _int_or_none(rest[19])
        if starttime is not None and up:
            entry["age_sec"] = max(0, int(up - starttime / clk))

        rows.append((pct if pct is not None else -1.0,
                     entry["ram_used_bytes"], pid, entry))

    # Forget pids that are gone, so a long-lived node does not accumulate state
    # for every process it has ever run.
    for gone in [p for p in _PROC_PREV if p not in live]:
        _PROC_PREV.pop(gone, None)

    rows.sort(key=_rank_key)
    return [entry for _pct, _ram, _pid, entry in rows[:PROCESSES_MAX]]


# --------------------------------------------- §4.12–§4.14 metric expansion
# The metric-expansion contract (node-telemetry.md §4.12–§4.14, research card
# t_cae0dfa8) adds three groups and extends three existing arrays. Every field is
# a plain C0 number sourced from /proc, /sys or an instance cgroup:
#
#   §4.12  `pressure{}` + `oom_kill_count` / `pgmajfault_count` /
#          `mem_available_bytes`;
#   §4.13  `disk[]` io_ticks_ms / io_in_flight / inode_used / inode_total,
#          `net[]` rx_errors / tx_errors / rx_dropped / tx_dropped, and the flat
#          `tcp_retransmit_segs` / `conntrack_count` / `conntrack_max`;
#   §4.14  `lxd_instances[]` oom_kill_count / pgmajfault_count (instance cgroup).
#
# Two rules hold for all of them, and they are the reason this card exists:
#
#   1. OMIT, NEVER FABRICATE. When the source is unreadable the field is absent —
#      never a `0`, and never a partially-filled row that reads as complete (the
#      §4.11 journal-count rule). "could not ask" and "asked, and it is zero" are
#      different facts, and only the second may ever be reported as `0`.
#   2. NOTHING C2 LEAVES. The conntrack *table* (`/proc/net/nf_conntrack`, a list
#      of per-flow peer addresses and ports) is C2 and is never opened: only the
#      two count files are read (governance.md §4.1.1's count-only rule). Nothing
#      here reads `cmdline`, `environ`, `exe`, `cwd` or a file path, and the LXD
#      `config`/`expanded_config` object (which carries an enrollment token, file
#      paths and a MAC — research card §3, headline finding) is never read
#      either; `process_facts()` above is the reference for that denial.
#
# The three roots are module constants so these collectors run against the
# committed real-shaped fixture tree (`tests/fixtures/telemetry/`) exactly as
# they run against a live host — the fixture is a copy of what this host's
# /proc, /sys and cgroup hierarchy actually printed.
PROC_ROOT = "/proc"
SYS_ROOT = "/sys"
CGROUP_ROOT = os.path.join(SYS_ROOT, "fs", "cgroup")


def _kv_int(text: str | None, key: str) -> int | None:
    """The integer on the `<key> <int>` line of a /proc- or cgroup-style text.

    /proc/vmstat, /proc/meminfo and cgroup `memory.events`/`memory.stat` are all
    `name value` tables, so they share one reader. A key that is absent (or a
    value that is not an integer) yields None: the caller then OMITS the field
    rather than reporting a `0` it never measured.
    """
    if not text:
        return None
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == key:
            return _int_or_none(parts[1]) if len(parts) > 1 else None
    return None


# `/proc/pressure/<resource>` line kinds. CPU and IO expose `some` (at least one
# task stalled); only memory exposes `full` (every task stalled). The contract's
# four scalars are exactly CPU/IO/MEM `some` + MEM `full`, 10 s averages (§4.12).
_PSI_SOURCES = (
    ("cpu_some_avg10", "cpu", "some"),
    ("io_some_avg10", "io", "some"),
    ("memory_some_avg10", "memory", "some"),
    ("memory_full_avg10", "memory", "full"),
)


def _psi_avg10(resource: str, kind: str) -> float | None:
    """The `avg10` percentage on the `<kind>` line of /proc/pressure/<resource>.

    Returns None when the file is unreadable, the line is absent or the value is
    outside the contract's 0–100 range — an out-of-range number is not a
    measurement of stall and must not be shipped as one.
    """
    text = _read_text(os.path.join(PROC_ROOT, "pressure", resource))
    if not text:
        return None
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != kind:
            continue
        for token in parts[1:]:
            if token.startswith("avg10="):
                try:
                    value = float(token.split("=", 1)[1])
                except ValueError:
                    return None
                return round(value, 2) if 0.0 <= value <= 100.0 else None
    return None


def pressure_facts() -> dict | None:
    """`pressure{}` (§4.12) — the four PSI scalars, or None when unavailable.

    All four keys or nothing: the object is a fixed four-key one, so a
    partially-filled object would read as "no stall" for whichever key the node
    could not measure. A measured `0.0` **is** reported (no stall observed is a
    real measurement); None means the source itself could not be read, and the
    caller then omits `pressure{}` entirely rather than shipping `{}` or zeros.
    """
    out: dict = {}
    for field, resource, kind in _PSI_SOURCES:
        value = _psi_avg10(resource, kind)
        if value is None:
            return None
        out[field] = value
    return out


def memory_pressure_scalars() -> dict:
    """§4.12's three flat scalars, each omitted (never `0`) when unreadable.

    `oom_kill_count` / `pgmajfault_count` are cumulative kernel counters from
    /proc/vmstat; `mem_available_bytes` is the kernel's own reclaimable-memory
    estimate from /proc/meminfo — the honest "free" that `MemFree` overcounts,
    which is why it is a *separate* field from `ram_used_bytes` rather than a
    replacement for the existing `_mem_available()` arithmetic.
    """
    out: dict = {}
    vmstat = _read_text(os.path.join(PROC_ROOT, "vmstat"))
    for field, key in (("oom_kill_count", "oom_kill"),
                       ("pgmajfault_count", "pgmajfault")):
        value = _kv_int(vmstat, key)
        if value is not None and value >= 0:
            out[field] = value
    available_kb = _kv_int(
        _read_text(os.path.join(PROC_ROOT, "meminfo")), "MemAvailable:")
    if available_kb is not None and available_kb >= 0:
        out["mem_available_bytes"] = available_kb * 1024
    return out


def tcp_retransmit_segs() -> int | None:
    """Cumulative TCP retransmits, from /proc/net/snmp's `Tcp:` row (§4.13).

    /proc/net/snmp is a labels line followed by its values line, so the value is
    read by the **index of the `RetransSegs` label** rather than by a fixed
    column. A kernel that inserts a counter therefore cannot silently shift this
    field onto a different number — the failure mode a positional read has.
    """
    text = _read_text(os.path.join(PROC_ROOT, "net", "snmp"))
    if not text:
        return None
    rows = [line.split() for line in text.splitlines() if line.startswith("Tcp:")]
    if len(rows) < 2:
        return None
    labels, values = rows[0][1:], rows[1][1:]
    if "RetransSegs" not in labels:
        return None
    index = labels.index("RetransSegs")
    if index >= len(values):
        return None
    value = _int_or_none(values[index])
    return value if value is not None and value >= 0 else None


def conntrack_facts() -> dict:
    """`conntrack_count` / `conntrack_max` (§4.13) — the COUNT files only.

    The conntrack *table* (`/proc/net/nf_conntrack`) is C2 — a list of per-flow
    5-tuples (peer IPs and ports) — and is never read for telemetry. This
    function must never grow a third read (governance.md §4.1.1 / §4.3; contract
    §4.13 pins the constraint so a future collector cannot drift onto it).

    `conntrack_max` is a table capacity and the contract's range is ≥ 1, so a
    `0` there is a file that says nothing usable: it is omitted, not emitted
    out of range.
    """
    out: dict = {}
    base = os.path.join(PROC_ROOT, "sys", "net", "netfilter")
    count = _int_or_none(_read_text(os.path.join(base, "nf_conntrack_count")))
    if count is not None and count >= 0:
        out["conntrack_count"] = count
    maximum = _int_or_none(_read_text(os.path.join(base, "nf_conntrack_max")))
    if maximum is not None and maximum >= 1:
        out["conntrack_max"] = maximum
    return out


def transport_scalars() -> dict:
    """§4.13's flat scalars: retransmits + the two conntrack counters."""
    out: dict = {}
    retransmit = tcp_retransmit_segs()
    if retransmit is not None:
        out["tcp_retransmit_segs"] = retransmit
    out.update(conntrack_facts())
    return out


# `disk[]` bound (§4.3/§4.13): one row per probed mount, kept in `_DISK_MOUNTS`
# order — the probe list is the rank, exactly as `process_facts()` ranks by RSS,
# and never the order a filesystem happens to enumerate in. `_DISK_MOUNTS` holds
# the two mounts the rules care about (`/` first, then the data mount); the cap
# below is the array's bound and the tail is dropped, so a node that grows that
# list cannot silently grow the sample.
DISK_MAX = 8

# `net[]` bound (§4.4/§4.13): one row per sample — the CONFIGURED interface
# (`cfg["iface"]`), never an enumeration of every interface on the node. A node
# hosting a hundred containers carries a hundred `veth*` peers, none of which is
# the path to the controller; the cap is the bound and the rule is "the
# configured interface only".
NET_MAX = 1

_MAJMIN_RE = re.compile(r"^\d+:\d+$")


def _mount_majmin() -> dict[str, str]:
    """Mount point → `major:minor` for every block-backed mount, in ONE read.

    /proc/self/mountinfo serves every path in `_DISK_MOUNTS`, so the device
    lookup costs one file read per sample rather than one subprocess per mount
    (`findmnt`/`df` would each be a subprocess — see the §4.8 `systemctl show`
    note for why that matters at a 1 s cadence). A mount whose line carries no
    `major:minor` (tmpfs, sysfs, overlay) simply does not appear, and its device
    counters are then omitted rather than read from another device's files.
    """
    text = _read_text(os.path.join(PROC_ROOT, "self", "mountinfo"))
    if not text:
        return {}
    out: dict = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5 or not _MAJMIN_RE.match(fields[2]):
            continue
        out[fields[4]] = fields[2]
    return out


def _block_io_of(mount_point: str, majmin: dict[str, str]) -> tuple[int, int] | None:
    """`(io_in_flight, io_ticks_ms)` for the device backing `mount_point`, or None.

    `/sys/dev/block/<major:minor>/stat` is the block device's own counter set and
    it also exists for a partition, so a mount on `/dev/sda1` is measured on
    sda1 rather than on the whole disk behind it. The kernel has frozen this
    table's column order at 11 fields (a 17-field variant appends discard and
    flush counters *after* them), so field 9 is I/Os in flight and field 10 is ms
    spent doing I/O — contract §4.13's sources. A shorter read is not a
    measurement and yields None, which omits both fields.

    The device is chosen by LONGEST matching mount point, so a nested mount
    (e.g. `/var/lib/termini` on its own volume) is measured on its own device
    and not on the root filesystem's.
    """
    key = None
    for mount in sorted(majmin, key=len, reverse=True):
        if mount_point == mount or mount_point.startswith(mount.rstrip("/") + "/"):
            key = majmin[mount]
            break
    if key is None:
        return None
    fields = (_read_text(os.path.join(SYS_ROOT, "dev", "block", key, "stat")) or "").split()
    if len(fields) < 10:
        return None
    in_flight = _int_or_none(fields[8])
    ticks = _int_or_none(fields[9])
    if in_flight is None or ticks is None or in_flight < 0 or ticks < 0:
        return None
    return in_flight, ticks


# The four NIC counters §4.13 adds, in the contract's order.
_NET_ERROR_COUNTERS = ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped")


def _iface_counter(ifname: str, counter: str) -> int | None:
    """One `/sys/class/net/<iface>/statistics/<counter>` value, or None."""
    value = _int_or_none(
        _read_text(os.path.join(SYS_ROOT, "class", "net", ifname, "statistics", counter)))
    return value if value is not None and value >= 0 else None


# §4.14: per-instance OOM / major-fault attribution, read from the instance's OWN
# CGROUP — not from the LXD API, whose `/1.0/instances/<name>` state object
# exposes neither counter. `lxc.payload.<name>` is the cgroup LXD itself creates
# per instance; the research card verified the path on this fleet's host
# (fixture `cgroup-lxd-probe.txt`, t_cae0dfa8).
#
# Two file reads per instance per sample — the same cost class as the existing
# `memory.current` read, so there is no sweep here to cache (the journal counts
# and the LXD inventory, which ARE expensive, keep their own refresh rules).
#
# The instance name is used ONLY to build this local path: it never reaches the
# wire (`vm_id` is derived, §4.6) and the C2 `config` object is never read.
_LXC_CGROUP_TEMPLATE = "lxc.payload.{name}"
# LXD instance names are DNS-ish; the name is untrusted input used to build a
# filesystem path below, so anything else is refused rather than joined in.
_LXC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


def instance_cgroup_facts(name: str) -> dict:
    """`lxd_instances[]`'s §4.14 counters for one instance, or `{}`.

    Both are cumulative cgroup counters: `oom_kill` (memory.events) and
    `pgmajfault` (memory.stat). Each is omitted — never `0` — when its file is
    unreadable, so an instance LXD runs under a different cgroup path (or a
    reader that cannot see the hierarchy) ships no counter instead of a zero
    that would claim nothing was ever killed.
    """
    if not isinstance(name, str) or not _LXC_NAME_RE.match(name):
        return {}
    base = os.path.join(CGROUP_ROOT, _LXC_CGROUP_TEMPLATE.format(name=name))
    out: dict = {}
    killed = _kv_int(_read_text(os.path.join(base, "memory.events")), "oom_kill")
    if killed is not None and killed >= 0:
        out["oom_kill_count"] = killed
    faults = _kv_int(_read_text(os.path.join(base, "memory.stat")), "pgmajfault")
    if faults is not None and faults >= 0:
        out["pgmajfault_count"] = faults
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


# --------------------------------------------------------------------------- own LXD API listener
# The controller executes an operator action by dialling THIS node's LXD REST API
# (`PUT /1.0/instances/<name>/state`, controller-api.md §4.2). A node whose
# listener is missing can only ever fail a remediation — measured on
# termini-node-a on 2026-10-17: `core.https_address` was EMPTY, nothing listened on
# :8443, and 183 of the last 200 operator executions read
# `URLError: <urlopen error [Errno 111] Connection refused>`. The listener is
# therefore a PRECONDITION of registration, not an assumption:
#
#   * `core.https_address` is baked into the node image
#     (image/pipeline/provision/10-node-os.sh) AND re-asserted here on every boot,
#     because a value that lives only in the daemon's own database is lost with it
#     (node-a's was empty after its 12:38Z crash);
#   * the endpoint is advertised ONLY once something answers on it, so the
#     controller never records a dead endpoint (machine.lxd.endpoint, §5.2);
#   * if it cannot be brought up, registration FAILS with the distinct kind
#     `lxd_api_unreachable` / state `LXD_API_UNAVAILABLE` and the unit never
#     reports READY — a node that cannot be remediated must not look healthy.
LXD_API_PATH = "/1.0"
LXD_API_PROBE_TIMEOUT_S = float(os.environ.get("TERMINI_LXD_API_PROBE_TIMEOUT_S", "3") or 3)
# Bounded wait for LXD to bind the listener after the config write / unit start.
LXD_API_READY_WAIT_S = float(os.environ.get("TERMINI_LXD_API_READY_WAIT_S", "45") or 45)
LXD_API_READY_POLL_S = float(os.environ.get("TERMINI_LXD_API_READY_POLL_S", "3") or 3)
# A HEARTBEAT may repair a lost listener at most once per window: a node whose LXD
# is down must not spend every beat on a bounded 45 s repair attempt.
LXD_API_REPAIR_INTERVAL_S = float(os.environ.get("TERMINI_LXD_API_REPAIR_INTERVAL_S", "300") or 300)
# monotonic of the last repair attempt (0.0 = none yet, so the first is immediate).
_LXD_API_LAST_REPAIR = 0.0

# The state a node parks in when its own LXD cannot be brought up. Distinct from
# REGISTRATION_FAILED: the enrollment credential is fine, the node is not usable.
LXD_API_FAILED_STATE = "LXD_API_UNAVAILABLE"


def lxd_port(cfg: dict) -> int:
    """The LXD REST port this node serves (`lxd_port`, §13; 8443 by default)."""
    raw = cfg.get("lxd_port", 8443)
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        return 8443
    return port if 1 <= port <= 65535 else 8443


def own_lxd_endpoint(cfg: dict) -> str:
    """This node's own LXD endpoint as `host:port` — the address the controller dials.

    The host is the node's primary IPv4 (the underlay address the controller sees
    the node at), never `127.0.0.1`: a loopback endpoint is unreachable from the
    controller and would silently re-create the global-fallback defect.
    """
    return f"{primary_ipv4()}:{lxd_port(cfg)}"


def split_endpoint(endpoint: str) -> tuple[str, int]:
    """`host:port` -> (host, port). Raises ValueError on anything else."""
    host, _, port = str(endpoint).rpartition(":")
    if not host or not port:
        raise ValueError(f"not a host:port endpoint: {endpoint!r}")
    return host, int(port)


def probe_lxd_api(host: str, port: int,
                  timeout: float = LXD_API_PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """True when something answers HTTP(S) on `host:port`; `(ok, detail)`.

    ANY HTTP status proves a listener — LXD answers `200` (untrusted view) or
    `403` (a listener that refuses this peer); a refused/timed-out connection
    proves nothing is there. TLS verification is deliberately OFF: LXD serves its
    own self-signed certificate and this probe asserts a *listener*, not an
    identity. Trust is the controller's mTLS client certificate in this node's
    trust store (controller/app/lxdtrust.py) — this probe never adds trust.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = None
    try:
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        conn.request("GET", LXD_API_PATH)
        resp = conn.getresponse()
        resp.read(2048)
        return True, f"HTTP {resp.status}"
    except (OSError, ssl.SSLError, http.client.HTTPException) as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def _lxd_socket_request(method: str, path: str, body: dict | None = None,
                        timeout: float | None = None) -> tuple[int, object, str]:
    """One LXD REST call over the daemon's unix socket -> (status, payload, where).

    `status == 0` means no socket answered; `where` then says why. This is the
    same transport `_lxd_instances_over_socket` uses, and it needs no privilege —
    which matters because this unit runs with `NoNewPrivileges=true` and the `lxc`
    CLI cannot exec there (measured on termini-node-a, 2026-10-15).
    """
    timeout = LXD_INVENTORY_TIMEOUT_S if timeout is None else timeout
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    last = "no LXD unix socket found"
    for sock_path in LXD_UNIX_SOCKETS:
        if not os.path.exists(sock_path):
            continue
        conn = None
        try:
            conn = _LxdUnixConnection(sock_path, timeout=timeout)
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        except (OSError, http.client.HTTPException) as e:
            last = f"{sock_path}: {type(e).__name__}: {e}"
            continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
        try:
            parsed = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            parsed = None
        return status, parsed, sock_path
    return 0, None, last


def start_lxd_daemon() -> None:
    """Ask systemd to start LXD (best effort, bounded).

    Only a *start*: a daemon that is crash-looping is not helped by a restart
    loop from this unit, and the caller's bounded wait reports the truth either
    way. Both unit names are tried because the snap and a distro package install
    under different names.
    """
    for unit in ("snap.lxd.daemon.service", "lxd.service"):
        try:
            subprocess.run(["systemctl", "start", unit], capture_output=True,
                           text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            continue


def ensure_lxd_api(cfg: dict) -> dict:
    """Bring this node's own LXD REST listener up and prove it answers.

    Returns `{"ok", "endpoint", "repaired", "detail"}`. `repaired` records
    whether a repair was attempted (so the log distinguishes "was already fine"
    from "we fixed it" from "we tried and it is still dead").

    Repair path, in order, each bounded:
      1. probe the node's own `host:port`;
      2. write `core.https_address` through the daemon's unix socket
         (`PUT /1.0` — the same write `lxc config set` performs, applied live);
      3. when no socket answers, ask systemd to start LXD;
      4. re-probe until LXD_API_READY_WAIT_S expires.
    """
    global _LXD_API_LAST_REPAIR
    endpoint = own_lxd_endpoint(cfg)
    try:
        host, port = split_endpoint(endpoint)
    except ValueError as e:
        return {"ok": False, "endpoint": endpoint, "repaired": False, "detail": str(e)}

    ok, detail = probe_lxd_api(host, port)
    if ok:
        return {"ok": True, "endpoint": endpoint, "repaired": False, "detail": detail}

    _LXD_API_LAST_REPAIR = time.monotonic()
    # `:port` (all interfaces) is the value the image bakes and the value the
    # node contract's underlay firewall expects; the reachable address is the
    # node's primary IPv4, which is what gets advertised as the endpoint.
    wanted = {"config": {"core.https_address": f":{port}"}}
    status, _payload, where = _lxd_socket_request("PUT", LXD_API_PATH, wanted)
    if status == 0:
        log(f"lxd api repair: daemon socket unreachable ({where}); starting the unit")
        start_lxd_daemon()
    else:
        log(f"lxd api repair: PUT {LXD_API_PATH} core.https_address=:{port} -> HTTP {status}")

    deadline = time.monotonic() + LXD_API_READY_WAIT_S
    retried_write = status != 0
    while time.monotonic() < deadline:
        ok, detail = probe_lxd_api(host, port)
        if ok:
            log(f"lxd api up on {endpoint} ({detail}) after repair")
            return {"ok": True, "endpoint": endpoint, "repaired": True, "detail": detail}
        if not retried_write:
            # The socket was dead on the first attempt (LXD not running yet): once
            # the unit has had a moment, re-issue the config write so the listener
            # comes up with the baked value rather than an empty one.
            status2, _p2, where2 = _lxd_socket_request("PUT", LXD_API_PATH, wanted)
            if status2:
                log(f"lxd api repair: retried PUT -> HTTP {status2} ({where2})")
                retried_write = True
        time.sleep(LXD_API_READY_POLL_S)
    return {"ok": False, "endpoint": endpoint, "repaired": True, "detail": detail}


def lxd_api_report(cfg: dict) -> dict:
    """Current listener state for the heartbeat, repairing at most once per window.

    Returns `{"listening", "endpoint", "detail"}`. A heartbeat must stay cheap
    while LXD is down, so the bounded repair runs only when the last attempt is
    older than LXD_API_REPAIR_INTERVAL_S.
    """
    endpoint = own_lxd_endpoint(cfg)
    try:
        host, port = split_endpoint(endpoint)
    except ValueError as e:
        return {"listening": False, "endpoint": endpoint, "detail": str(e)}
    ok, detail = probe_lxd_api(host, port)
    if ok:
        return {"listening": True, "endpoint": endpoint, "detail": detail}
    if (time.monotonic() - _LXD_API_LAST_REPAIR) >= LXD_API_REPAIR_INTERVAL_S:
        res = ensure_lxd_api(cfg)
        return {"listening": res["ok"], "endpoint": endpoint, "detail": res["detail"]}
    return {"listening": False, "endpoint": endpoint, "detail": detail}


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
    # The node's own LXD API is not listening and could not be repaired. Terminal
    # because retrying registration cannot create a listener — but it parks in the
    # DISTINCT state `LXD_API_UNAVAILABLE`, not REGISTRATION_FAILED: the enrollment
    # credential is fine, the machine is not remediable.
    "lxd_api_unreachable",
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


def registration_body(cfg: dict, node_name: str, lxd_endpoint: str | None = None) -> dict:
    """The §5.2 registration document.

    `lxd_endpoint` is included ONLY when the caller has *proved* this node's own
    LXD API answers on it (`ensure_lxd_api`). Advertising an unverified endpoint
    is worse than omitting it: the controller would store it and dial a dead
    address for every later remediation instead of using its documented fallback.
    """
    ip = primary_ipv4()
    ifname = cfg.get("iface", "eth0")
    lxd: dict = {"version": "5.21.8", "snap_revision": "40958"}
    if lxd_endpoint:
        lxd["endpoint"] = lxd_endpoint
    return {
        "schema_version": "1.0",
        "machine": {
            "machine_id": machine_id(node_name),
            "hostname": node_name,
            "image_version": cfg.get("image_version", "2026.10.0"),
            "os": {"name": "ubuntu", "version": "24.04"},
            "lxd": lxd,
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
    # Fail closed on a node nobody can remediate. The check is this node's own
    # listener — the same socket/port the controller will PUT to — and it runs
    # before the enrollment token is spent, so a node that cannot be remediated
    # never becomes a registered node the operator loop will try to act on.
    api = ensure_lxd_api(cfg)
    if not api["ok"]:
        raise RegistrationError(
            "lxd_api_unreachable", None,
            f"own LXD API is not listening on {api['endpoint']} "
            f"(repair_attempted={str(api['repaired']).lower()}): {api['detail']}",
        )
    url = cfg["controller_url"].rstrip("/") + "/v1/nodes/register"
    body = registration_body(cfg, node_name, lxd_endpoint=api["endpoint"])
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
    # Per-process metrics (§4.10). OMITTED (not emptied) when /proc cannot be
    # read, for exactly the reason `lxd_instances` is: "did not report" is the
    # truth and an empty list would claim the node runs nothing.
    procs = process_facts()
    if procs is not None:
        sample["processes"] = procs
    # §4.12–§4.14 (metric expansion, research card t_cae0dfa8). Each group is
    # OMITTED, never zero-filled, when its source cannot be read: a node that
    # cannot see /proc/pressure reports no pressure object rather than "no
    # stall", and a node that cannot see the conntrack counters reports neither
    # rather than `0/0`. The flat scalars are independent of each other, so a
    # node missing one source still ships the others.
    pressure = pressure_facts()
    if pressure is not None:
        sample["pressure"] = pressure
    sample.update(memory_pressure_scalars())
    sample.update(transport_scalars())
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


# --------------------------------------------------------------------------- per-instance inventory
# `lxd_instances[]` — contract field, node-telemetry.md §4.6, C0.
#
# This is the field the operator's guardrail resolves a remediation target
# through (G5: `params.instance_name` must resolve against
# `target_resource.instances[]`), and the field the executor then turns into
# `PUT /1.0/instances/<name>/state`. Reporting the node's REAL instances is what
# makes the loop solvable: with the array absent, every model-chosen action is
# unresolvable and falls back (`fallback_reason=invalid_target`), and with the
# array carrying invented ids the write 404s at the node.
#
# ONE reader (task t_dfe6acf9). #166 (main) and #167 (this branch) each wrote a
# copy of this field, and merging them left two definitions of it in this file —
# Python silently kept the later one. The socket transport below is the
# surviving one (it is the reason #167 exists: the `lxc` CLI is dead on a
# deployed node, see `_lxd_instances_over_socket`), and it carries §4.6's
# SEMANTICS as #166 implemented them: only instances in LXD's `Running` state are
# advertised, junk entries are skipped rather than fatal, and the CPU history of
# an instance that is gone is dropped.
#
# `vm_id` is a C0 id and the ingest validator requires a 26-char Crockford ULID
# (`controller/app/fields.py`), so an instance *name* can never be reported here
# (names are lowercase/DNS-ish and may carry customer data). The id is therefore
# DERIVED from the name:
#
#     vm_id = 26 Crockford base32 chars of SHA-256("termini-vm-id:" + <name>)
#
# deterministic and one-way, so the controller can derive the same id from the
# instance names it reads back over the LXD REST API and resolve a model-chosen
# vm_id to the instance it belongs to — with no config table to keep in sync and
# no name on the wire. `controller/app/operator/vmids.py` carries the
# byte-identical implementation; `tests/test_vm_id_derivation.py` fails the build
# if the two drift, and `tests/test_node_instance_inventory.py` pins the derived
# literals against the controller's and the seeder's copies.
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
VM_ID_LEN = 26
VM_ID_PREFIX = "termini-vm-id:"

# Wall-clock ceiling for the local LXD inventory probe. One sample per second means
# a hung LXD must not hold the telemetry loop; a probe that does not answer in
# time is reported as "no inventory this sample", never as an empty node.
LXD_INVENTORY_TIMEOUT_S = float(os.environ.get("TERMINI_LXD_INVENTORY_TIMEOUT_S", "5") or 5)

# LXD's own unix socket, in probe order — snap LXD (the baked node image) keeps it
# under /var/snap, a distro-packaged LXD under /var/lib, and some builds under
# /run. The REST API over this socket answers the same
# `GET /1.0/instances?recursion=2` view `lxc list` renders, and it needs no
# privilege escalation — which is the whole point (see `_lxd_instances_over_socket`).
LXD_UNIX_SOCKETS = (
    "/var/snap/lxd/common/lxd/unix.socket",
    "/var/lib/lxd/unix.socket",
    "/run/lxd/unix.socket",
)

# How long the `lxc` CLI is left alone after it has failed once. The agent samples
# every second, so an unguarded retry of a probe that cannot succeed (the CLI is
# blocked by the unit's own hardening — see below) costs a subprocess per sample
# AND writes a journal line per sample.
LXD_CLI_BACKOFF_S = 60.0

# monotonic of the last "inventory unavailable" log line (rate limited) and the
# monotonic until which the `lxc` CLI is not retried at all.
_INVENTORY_WARN_AT = 0.0
_INVENTORY_CLI_DEAD_UNTIL = 0.0


def _warn_inventory_unavailable(reason: str) -> None:
    """Say once a minute that this node is reporting no inventory — and why.

    Rate limited on purpose: at 1 Hz an unguarded line here floods the journal
    with thousands of identical entries while the operator's guardrail quietly
    has nothing to resolve a target against.
    """
    global _INVENTORY_WARN_AT
    now = time.monotonic()
    if now - _INVENTORY_WARN_AT >= LXD_CLI_BACKOFF_S:
        _INVENTORY_WARN_AT = now
        log(f"lxd inventory unavailable: {reason}")

# LXD's status strings -> the contract's enum (`node-telemetry.md` §4.6).
_LXD_STATE = {
    "running": "running",
    "stopped": "stopped",
    "frozen": "frozen",
    "paused": "paused",
    "error": "error",
    "ready": "starting",
    "starting": "starting",
}

# name -> (monotonic_s, cpu.usage_ns) from the previous sample, for the
# per-instance CPU percentage (LXD reports cumulative CPU nanoseconds).
_CPU_PREV: dict[str, tuple[float, int]] = {}


def vm_id_for_instance(name: str) -> str:
    """Controller-issued `vm_id` for LXD instance `name` (see the block above).

    Byte-for-byte compatible with `controller/app/operator/vmids.py`.
    """
    digest = hashlib.sha256((VM_ID_PREFIX + str(name)).encode("utf-8")).digest()
    value = int.from_bytes(digest[:17], "big") >> 6
    out = []
    for _ in range(VM_ID_LEN):
        out.append(CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def lxd_socket_path() -> str | None:
    """Path of the local LXD socket, or None when LXD is not installed.

    `service_facts` uses this to tell "LXD is not running" from "systemd is not
    readable"; the inventory probe below walks the same list for its transport.
    """
    for path in LXD_UNIX_SOCKETS:
        if os.path.exists(path):
            return path
    return None


def _int_or_zero(value: object) -> int:
    """An LXD byte counter as a non-negative int (bool is not a byte count)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value > 0 else 0


def _dict_of(value: object) -> dict:
    """`value` when it is a mapping, else an empty mapping (never None)."""
    return value if isinstance(value, dict) else {}


def _instance_cpu_pct(name: str, usage_ns: object, now: float) -> float | None:
    """Per-instance CPU utilisation (% of this node's total CPU capacity).

    LXD's `state.cpu.usage` is cumulative nanoseconds, so a percentage needs two
    samples: the first sample for an instance reports nothing rather than a
    fabricated 0. Returns None when the delta cannot be computed (first sample,
    clock going backwards, counter reset).
    """
    if not isinstance(usage_ns, (int, float)) or isinstance(usage_ns, bool):
        return None
    prev = _CPU_PREV.get(name)
    _CPU_PREV[name] = (now, int(usage_ns))
    if prev is None:
        return None
    dt = now - prev[0]
    d_ns = int(usage_ns) - prev[1]
    if dt <= 0 or d_ns < 0:
        return None
    cores = max(1, os.cpu_count() or 1)
    pct = 100.0 * (d_ns / 1e9) / (dt * cores)
    return round(min(100.0, max(0.0, pct)), 2)


class _LxdUnixConnection(http.client.HTTPConnection):
    """An HTTP connection to LXD over its own unix socket.

    `http.client` speaks to a TCP host; this subclass only replaces the socket it
    connects, which is the whole difference between `http://localhost:8443` and
    the daemon's local socket.
    """

    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:  # noqa: D102 (http.client API)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def _lxd_instances_over_socket() -> tuple[list | None, str]:
    """`GET /1.0/instances?recursion=2` over LXD's unix socket -> (items, why-not).

    This is the transport that actually works on a deployed node, and the reason
    is worth recording: the unit runs with `NoNewPrivileges=true`, so the `lxc`
    CLI — a snap wrapper that needs to change its AppArmor profile for the exec —
    dies with `cannot change profile for the next exec call: Operation not
    permitted` (measured on termini-node-a, 2026-10-15). The agent then reported
    NO `lxd_instances[]` at all, which is invisible in the dashboard and leaves
    the operator's guardrail with nothing to resolve a target against. Talking to
    the daemon's own socket needs no privilege at all.

    `recursion=2`, not `1`: LXD only attaches per-instance `state` (memory, CPU,
    disk) at depth 2. At depth 1 every instance comes back with no state at all,
    which would report a running workload as `ram_used_bytes: 0` — telemetry that
    reads as "idle" for the instance the fault is actually in, and that the
    operator's heaviest-instance rule would then rank below an empty one.
    Measured on node-a: depth 2 carries `state.memory.usage`; depth 1 does not.
    """
    last = "no LXD unix socket found"
    for path in LXD_UNIX_SOCKETS:
        if not os.path.exists(path):
            continue
        conn = None
        try:
            conn = _LxdUnixConnection(path, timeout=LXD_INVENTORY_TIMEOUT_S)
            conn.request("GET", "/1.0/instances?recursion=2")
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        except (OSError, http.client.HTTPException) as e:
            last = f"{path}: {type(e).__name__}: {e}"
            continue
        finally:
            if conn is not None:
                conn.close()
        if status != 200:
            last = f"{path}: HTTP {status}"
            continue
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            last = f"{path}: non-JSON answer: {e}"
            continue
        items = payload.get("metadata") if isinstance(payload, dict) else None
        if isinstance(items, list):
            return items, ""
        last = f"{path}: unexpected body shape"
    return None, last


def _lxd_instances_over_cli() -> tuple[list | None, str]:
    """`lxc list --format=json` -> (items, why-not). Fallback transport.

    Kept for a node whose agent can run the CLI (no `NoNewPrivileges`, or an
    `lxc` that is a plain binary rather than the snap wrapper). It renders the
    same REST view `_lxd_instances_over_socket` does.
    """
    try:
        proc = subprocess.run(
            ["lxc", "list", "--format=json"],
            capture_output=True, text=True, timeout=LXD_INVENTORY_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"lxc list: {type(e).__name__}: {e}"
    if proc.returncode != 0:
        return None, (f"lxc list exit {proc.returncode}: "
                      f"{(proc.stderr or '').strip()[:200]}")
    try:
        items = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        return None, f"lxc list returned non-JSON: {e}"
    if not isinstance(items, list):
        return None, "lxc list returned a non-list"
    return items, ""


# Restart observation per instance name: (monotonic seconds, `state.started_at`,
# observed restart count). LXD has no restart counter of its own, so the only
# honest number is the one this daemon can observe — a change in `started_at`
# between two samples is a restart. The first sighting establishes the baseline
# and reports nothing (a `0` there would claim "never restarted", which this
# daemon cannot know).
_INSTANCE_START_PREV: dict[str, tuple[float, str, int]] = {}


def _iso_epoch(value: object) -> float | None:
    """Seconds since the Unix epoch for an RFC3339 timestamp, or None.

    Parsed by hand rather than with ``datetime.fromisoformat`` because LXD emits
    nanosecond precision (``...T07:49:00.123456789Z``), which not every Python
    the node might run accepts, and a wrong age is worse than no age.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    m = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
        r"(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$",
        value.strip(),
    )
    if m is None:
        return None
    frac = float("0." + m.group(7)) if m.group(7) else 0.0
    try:
        epoch = calendar.timegm(
            (int(m.group(1)), int(m.group(2)), int(m.group(3)),
             int(m.group(4)), int(m.group(5)), int(m.group(6)), 0, 0, 0)
        )
    except (ValueError, OverflowError):
        return None
    tz = m.group(8) or "Z"
    if tz != "Z":
        sign = 1.0 if tz[0] == "+" else -1.0
        hh, mm = tz[1:3], tz[-2:]
        epoch -= sign * (int(hh) * 3600 + int(mm) * 60)
    return epoch + frac


def _instance_restart_count(name: str, started_at: object, now: float) -> int | None:
    """Restarts of `name` observed by THIS daemon since it started (§4.6)."""
    if not isinstance(started_at, str) or not started_at:
        return None
    prev = _INSTANCE_START_PREV.get(name)
    count = 0 if prev is None else prev[2]
    if prev is not None and prev[1] != started_at:
        count += 1
    _INSTANCE_START_PREV[name] = (now, started_at, count)
    if prev is None:
        return None  # baseline only: this daemon has observed no restart yet
    return count


def lxd_instance_inventory(cfg: dict) -> list | None:
    """`lxd_instances[]` for this node, from its OWN LXD (contract §4.6).

    Reads the local LXD through its unix socket, falling back to the `lxc` CLI —
    no network, no trust prompt, and the same view LXD itself serves over the REST
    API (the node's own daemon, not the controller's copy of it).

    One entry per RUNNING instance. §4.6 is explicit: "only instances in LXD's
    `Running` state are reported; a stopped instance is not a remediation target
    and is not advertised" — and this array is exactly what the guardrail
    resolves a model-chosen target against.

    Returns None when the inventory cannot be read (no socket, no `lxc` binary,
    LXD not initialised, a probe that times out or answers junk) so the caller
    OMITS the field: "this node did not report an inventory" is the truth, whereas
    an empty list would claim a node with no instances and would make every
    model-chosen target unresolvable. `lxd_inventory: 0` in config.yaml disables
    it explicitly.
    """
    if str(cfg.get("lxd_inventory", "1")).strip().lower() in ("0", "false", "no", "off"):
        return None

    global _INVENTORY_CLI_DEAD_UNTIL
    items, reason = _lxd_instances_over_socket()
    if items is None:
        if time.monotonic() >= _INVENTORY_CLI_DEAD_UNTIL:
            cli_items, cli_reason = _lxd_instances_over_cli()
            if cli_items is None:
                # Do not pay for a subprocess (or a journal line) every single
                # sample while the CLI is known to be blocked.
                _INVENTORY_CLI_DEAD_UNTIL = time.monotonic() + LXD_CLI_BACKOFF_S
                reason = f"{reason}; {cli_reason}"
            else:
                items = cli_items
        else:
            reason = f"{reason}; lxc CLI not retried yet (last attempt failed)"
    if items is None:
        _warn_inventory_unavailable(reason)
        return None
    # A transport that answered a non-list is an unreadable LXD, not an empty
    # node: an empty list is a legitimate answer and is reported as one.
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
        # §4.14 per-instance OOM / major-fault attribution, from this instance's
        # own cgroup (not the LXD API, which exposes neither). Both are omitted
        # when the cgroup files are unreadable (metric expansion, t_cae0dfa8).
        entry.update(instance_cgroup_facts(name))
        # Operational fields (§4.6, t_957956e5): how long the instance has been in
        # this state, and how many restarts this daemon has observed. Both are
        # omitted when the underlying fact is unavailable rather than guessed.
        started_at = lxd_state.get("started_at")
        started_epoch = _iso_epoch(started_at)
        if started_epoch is not None:
            entry["state_age_sec"] = max(0, int(time.time() - started_epoch))
        restarts = _instance_restart_count(name, started_at, now)
        if restarts is not None:
            entry["restart_count"] = restarts
        out.append(entry)
    # Drop readings for instances that are gone (or no longer running), so a
    # long-lived node does not accumulate state for every instance it has ever run.
    for gone in [n for n in _CPU_PREV if n not in seen]:
        _CPU_PREV.pop(gone, None)
    for gone in [n for n in _INSTANCE_START_PREV if n not in seen]:
        _INSTANCE_START_PREV.pop(gone, None)
    return out


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
    """Enter the terminal failure state and hold it (§9 row 2).

    A node whose own LXD API cannot be brought up parks in the DISTINCT state
    `LXD_API_UNAVAILABLE`: the enrollment credential is fine, the machine simply
    cannot be remediated, and an operator has to look at LXD rather than at the
    token. Every other terminal kind keeps the documented `REGISTRATION_FAILED`.
    """
    state = LXD_API_FAILED_STATE if err.kind == "lxd_api_unreachable" else "REGISTRATION_FAILED"
    set_state(state=state, last_error=str(err),
              failure_kind=err.kind, registration_attempts=attempts, terminal=True)
    log(f"{state} (terminal, kind={err.kind}, attempts={attempts}): {err}")
    log("no auto-retry: operator must re-provision (fresh token / fix config) "
        "and restart termini-bootstrap")
    sd_notify(f"STATUS={state} kind={err.kind}")  # never READY
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


def heartbeat_interval_s(cfg: dict) -> float:
    """Seconds between heartbeats (`heartbeat_interval_s`, §13; 15 s by default).

    The controller's registration response advertises its own value; this node
    runs the standard REST fallback, so the config value is what it uses. 5 s is
    the floor — a beat every second would be telemetry by another name.
    """
    try:
        value = float(str(cfg.get("heartbeat_interval_s", 15)).strip())
    except (TypeError, ValueError):
        value = 15.0
    return max(5.0, value)


def heartbeat_body(cfg: dict) -> dict:
    """Heartbeat payload: this node's own LXD endpoint, when it is listening.

    `lxd_endpoint` is emitted ONLY for a listener that answered just now, so the
    controller re-learns a node's address when it changes (a rebuilt node, a new
    DHCP lease) and never records an endpoint nothing listens on. `lxd_api` is
    always emitted: "listening: false" is a fact the controller can log, whereas
    omitting the object would look like an old node with nothing to say.
    """
    report = lxd_api_report(cfg)
    body: dict = {"lxd_api": {"listening": bool(report["listening"]),
                              "endpoint": report["endpoint"]}}
    if report["listening"]:
        body["lxd_endpoint"] = report["endpoint"]
    else:
        body["lxd_api"]["detail"] = report.get("detail") or ""
    return body


def send_heartbeat(cfg: dict, url: str, node_token: str) -> None:
    """POST one heartbeat. Failure is logged, never fatal: telemetry is the
    primary signal and a missed beat is recorded by the controller as staleness."""
    try:
        body = heartbeat_body(cfg)
        status, resp = http_json("POST", url, body=body, token=node_token,
                                 timeout=float(cfg.get("register_timeout_s",
                                                       DEFAULT_REGISTER_TIMEOUT_S)))
    except Exception as e:  # noqa: BLE001 — a heartbeat must never stop the daemon
        log(f"heartbeat failed: {e}")
        return
    if status not in (200, 204):
        log(f"heartbeat HTTP {status}: {resp}")
    elif not body["lxd_api"]["listening"]:
        log(f"heartbeat: own LXD API NOT listening on {body['lxd_api']['endpoint']} "
            f"({body['lxd_api'].get('detail', '')}); endpoint not advertised")


def run(cfg: dict) -> int:
    os.makedirs(SPOOL_DIR, exist_ok=True)
    node_name = cfg.get("node_name") or socket.gethostname()
    cfg["node_name"] = node_name
    interval = max(0.2, int(cfg.get("telemetry_interval_ms", "1000")) / 1000.0)

    identity = read_json(os.path.join(IDENTITY_DIR, "node.json"))
    if identity and identity.get("node_id") and identity.get("node_token"):
        log(f"reconnect: reusing persisted identity node_id={identity['node_id']}")
        # A reboot is exactly when core.https_address can have been lost with the
        # daemon's database, so the listener is re-established (and re-proved)
        # before this unit reports READY. No READY while the node cannot be
        # remediated: retry here instead of registering (the identity is valid).
        while True:
            api = ensure_lxd_api(cfg)
            if api["ok"]:
                break
            set_state(state=LXD_API_FAILED_STATE, node_id=identity["node_id"],
                      failure_kind="lxd_api_unreachable",
                      last_error=f"own LXD API not listening on {api['endpoint']}: {api['detail']}")
            log(f"{LXD_API_FAILED_STATE}: own LXD API not listening on {api['endpoint']} "
                f"({api['detail']}); not reporting READY")
            sd_notify(f"STATUS={LXD_API_FAILED_STATE} endpoint={api['endpoint']}")
            time.sleep(max(5.0, LXD_API_READY_POLL_S * 2))
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
            return 1  # terminal failure: parked, no auto-retry

    node_id = identity["node_id"]
    node_token = identity["node_token"]
    url = cfg["controller_url"].rstrip("/") + f"/v1/nodes/{node_id}/telemetry"
    hb_url = cfg["controller_url"].rstrip("/") + f"/v1/nodes/{node_id}/heartbeat"
    hb_interval = heartbeat_interval_s(cfg)
    next_beat = 0.0                     # first beat immediately: report the endpoint
    # Resume the sequence across restarts: the controller drops out-of-order seq,
    # so a reset counter would make every post after a restart a silent no-op.
    seq = int((read_json(STATE_PATH) or {}).get("seq") or 0)
    while True:
        if time.monotonic() >= next_beat:
            send_heartbeat(cfg, hb_url, node_token)
            next_beat = time.monotonic() + hb_interval
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
