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
        "model": cfg.get("model_alias", "deepseek-flash"),
        "active_requests": 0,
        "queue_depth": 0,
        "tokens_per_s": 0.0,
        "vram_used_bytes": 0,
    }


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
