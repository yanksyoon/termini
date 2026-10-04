"""W5 dashboard-surface tests (t_3468e5f0): node health, log stream, terminal,
SSH string, and manual intervention.

Discovered by CI via `python3 -m unittest discover -s tests`. The integration
test boots the real server (subprocess) and exercises the streaming/WebSocket
paths end-to-end with a minimal stdlib WS/SSE client. The terminal bridge runs
against the `local` backend (a real PTY over ``bash -i``) so the test is hermetic.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "controller"
sys.path.insert(0, str(CONTROLLER))

from app import commands, db as appdb, health, store, ulid  # noqa: E402

L4_24GB = 24 * 1024 ** 3
PORT = int(os.environ.get("TERMINI_DASH_TEST_PORT", "18081"))


# --------------------------------------------------------------------------- WS/SSE clients

class WsClient:
    """Tiny stdlib WebSocket client for the test.

    The controller runs in a subprocess alongside this test. Under a loaded CI
    runner its accept/response path can stall for a few seconds on one
    connection (observed on `test_terminal_ws_not_found_node`: the handshake
    read hit the socket timeout, then the very next request was served
    instantly). A transient stall must not red the suite, so the opening
    handshake is retried a bounded number of times on transient socket errors:

      * `TimeoutError` / `OSError` (stalled or reset connection) -> retry;
      * a non-101 status (real protocol failure) -> raise immediately;
      * retries exhausted -> re-raise the last transient error (test fails).

    Assertions are unchanged: every test still runs against a real, accepted
    101 handshake.
    """

    HANDSHAKE_ATTEMPTS = 3
    HANDSHAKE_TIMEOUT = 10.0

    def __init__(self, port: int, path: str) -> None:
        self.close_code = None
        self.close_reason = ""
        last_exc: Exception | None = None
        for attempt in range(self.HANDSHAKE_ATTEMPTS):
            try:
                self._connect_and_handshake(port, path)
                return
            except (TimeoutError, ConnectionError, OSError) as exc:
                # transient: stalled/reset connection. Non-101 (AssertionError)
                # is a real protocol failure and is not caught here.
                last_exc = exc
                self._close_sock()
                if attempt + 1 < self.HANDSHAKE_ATTEMPTS:
                    time.sleep(0.25 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    def _connect_and_handshake(self, port: int, path: str) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=self.HANDSHAKE_TIMEOUT)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = self._read_until(b"\r\n\r\n")
        status = resp.split(b"\r\n", 1)[0].decode()
        if "101" not in status:
            raise AssertionError(f"WS handshake failed: {status}")

    def _close_sock(self) -> None:
        try:
            self.sock.close()
        except (OSError, AttributeError):
            pass

    def _read_until(self, delim: bytes) -> bytes:
        buf = b""
        while delim not in buf:
            c = self.sock.recv(1)
            if not c:
                break
            buf += c
        return buf

    def _read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise EOFError("socket closed")
            buf += c
        return buf

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        b0 = 0x80 | opcode
        n = len(payload)
        mask = os.urandom(4)
        header = bytearray([b0])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode())

    def recv_message(self, timeout: float = 5.0) -> dict | None:
        self.sock.settimeout(timeout)
        bufs: list[bytes] = []
        while True:
            b0, b1 = self._read_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if masked:
                m = self._read_exact(4)
                payload = self._read_exact(length)
                payload = bytes(b ^ m[i % 4] for i, b in enumerate(payload))
            else:
                payload = self._read_exact(length)
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:  # close
                if len(payload) >= 2:
                    self.close_code = struct.unpack("!H", payload[:2])[0]
                    self.close_reason = payload[2:].decode(errors="replace")
                return None
            bufs.append(payload)
            if fin:
                break
        text = b"".join(bufs).decode()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class SseReader:
    """Raw SSE client: reads headers, then yields (event, data) dicts."""

    def __init__(self, port: int, path: str) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        req = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: text/event-stream\r\n\r\n"
        self.sock.sendall(req.encode())
        self._buf = b""
        self._read_until(b"\r\n\r\n")

    def _read_until(self, delim: bytes) -> bytes:
        while delim not in self._buf:
            c = self.sock.recv(1)
            if not c:
                raise EOFError("sse closed")
            self._buf += c
        head, self._buf = self._buf.split(delim, 1)
        return head

    def next_event(self, timeout: float = 5.0) -> dict | None:
        self.sock.settimeout(timeout)
        fields: dict[str, str] = {}
        while True:
            if b"\n\n" not in self._buf:
                chunk = self.sock.recv(4096)
                if not chunk:
                    return None
                self._buf += chunk
            if b"\n\n" not in self._buf:
                continue
            block, self._buf = self._buf.split(b"\n\n", 1)
            event = "message"
            data = ""
            for line in block.decode().split("\n"):
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data += line[5:].strip()
            if not data:
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                payload = {"_raw": data}
            return {"event": event, "data": payload}

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- helpers

def http(method: str, path: str, body=None, token: str | None = None, port: int = PORT):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def valid_sample(**overrides) -> dict:
    s = {
        "seq": 1000,
        "ts": "2026-10-04T07:49:00.000Z",
        "uptime_sec": 3600,
        "cpu_util_pct": 31.2,
        "cpu_core_count": 16,
        "ram_total_bytes": 34359738368,
        "ram_used_bytes": 19756849561,
        "swap_used_bytes": 0,
        "load_avg_1m": 1.42,
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "kernel_version": "6.8.0-142-generic",
        "lxd_cluster_role": "member",
        "hostname": "5f2a9c1e7b4d4a1f",
        "ip_local": "10.0.0.0/24",
        "mac_address": "9c1e7b4d4a1f5f2a",
        "project_id": ulid.new_ulid(),
        "disk": [{"path": "/dev/sda", "total_bytes": 274877906944, "free_bytes": 214748364800,
                  "write_latency_ms": 3.0, "mount_ro": False}],
        "net": [{"iface": "eth0", "rx_bytes": 123000, "tx_bytes": 11000, "latency_ms": 0.8}],
        "services": [
            {"name": "termini-bootstrap", "status": "running"},
            {"name": "lxd", "status": "running"},
            {"name": "sshd", "status": "running"},
        ],
    }
    s.update(overrides)
    return s


def envelope(node_id: str, sample: dict) -> dict:
    return {
        "_redaction": {"version": "redactor-config v3", "config_hash": "3f2a1b", "result": "ok"},
        "call_id": ulid.new_ulid(),
        "node_id": node_id,
        "schema_version": 1,
        "governance_chain_tip": ulid.new_ulid(),
        "samples": [sample],
    }


# --------------------------------------------------------------------------- unit tests

class HealthDerivationTest(unittest.TestCase):
    def test_online_healthy(self):
        node = {"role": "compute", "last_seen_at": "2026-10-04T07:49:00Z"}
        now = _epoch("2026-10-04T07:49:03Z")
        sample = valid_sample()
        status, h = health.node_status_health(node, sample, now)
        self.assertEqual(status, "online")
        self.assertTrue(h["disk_ok"])
        self.assertEqual(h["degraded_reasons"], [])
        self.assertAlmostEqual(h["ram_used_pct"], 57.5, places=1)

    def test_ram_used_pct_zero_when_no_total(self):
        sample = valid_sample()
        sample.pop("ram_total_bytes")
        h = health.health_flags(sample, "compute")
        self.assertEqual(h["ram_used_pct"], 0.0)

    def test_disk_read_only_degraded(self):
        node = {"role": "compute", "last_seen_at": "2026-10-04T07:49:00Z"}
        now = _epoch("2026-10-04T07:49:03Z")
        sample = valid_sample(disk=[{"path": "/dev/sda", "total_bytes": 1, "free_bytes": 0,
                                     "write_latency_ms": 1.0, "mount_ro": True}])
        status, h = health.node_status_health(node, sample, now)
        self.assertEqual(status, "degraded")
        self.assertFalse(h["disk_ok"])
        self.assertIn("disk_read_only", h["degraded_reasons"])

    def test_service_failed_degraded(self):
        node = {"role": "compute", "last_seen_at": "2026-10-04T07:49:00Z"}
        now = _epoch("2026-10-04T07:49:03Z")
        sample = valid_sample(services=[{"name": "sshd", "status": "failed"}])
        status, h = health.node_status_health(node, sample, now)
        self.assertEqual(status, "degraded")
        self.assertIn("service_failed", h["degraded_reasons"])

    def test_gpu_overtemp_inference(self):
        node = {"role": "inference", "last_seen_at": "2026-10-04T07:49:00Z"}
        now = _epoch("2026-10-04T07:49:03Z")
        sample = valid_sample(gpu=[{"model": "NVIDIA L4", "util_pct": 90.0,
                                    "vram_total_bytes": L4_24GB, "vram_used_bytes": 1,
                                    "temp_celsius": 92.0}],
                              inference={"server_up": True})
        status, h = health.node_status_health(node, sample, now)
        self.assertEqual(status, "degraded")
        self.assertFalse(h["gpu_ok"])
        self.assertIn("gpu_overtemp", h["degraded_reasons"])

    def test_heartbeat_late(self):
        node = {"role": "compute", "last_seen_at": "2026-10-04T07:49:00Z"}
        now = _epoch("2026-10-04T07:49:30Z")  # 30s stale -> degraded
        sample = valid_sample()
        status, h = health.node_status_health(node, sample, now)
        self.assertEqual(status, "degraded")
        self.assertIn("heartbeat_late", h["degraded_reasons"])

    def test_offline(self):
        node = {"role": "compute", "last_seen_at": "2026-10-04T07:49:00Z"}
        now = _epoch("2026-10-04T07:50:00Z")  # 60s stale -> offline
        status, _ = health.node_status_health(node, valid_sample(), now)
        self.assertEqual(status, "offline")


def _epoch(ts: str) -> float:
    from app.validate import parse_rfc3339_epoch
    return parse_rfc3339_epoch(ts)


class MetricAggregationTest(unittest.TestCase):
    def test_multi_disk_first_element(self):
        # Frozen mapping (dashboard.md §1.4): first element of each device array.
        sample = valid_sample(disk=[
            {"path": "/dev/sda", "total_bytes": 100, "free_bytes": 30, "write_latency_ms": 5.0, "mount_ro": False},
            {"path": "/dev/sdb", "total_bytes": 50, "free_bytes": 10, "write_latency_ms": 80.0, "mount_ro": False},
        ])
        self.assertEqual(store.METRICS["disk_total_bytes"][0](sample), 100)
        self.assertEqual(store.METRICS["disk_free_bytes"][0](sample), 30)
        self.assertEqual(store.METRICS["disk_write_latency_ms"][0](sample), 5.0)

    def test_multi_net_first_element(self):
        sample = valid_sample(net=[
            {"iface": "eth0", "rx_bytes": 100, "tx_bytes": 10, "latency_ms": 1.0},
            {"iface": "eth1", "rx_bytes": 200, "tx_bytes": 20, "latency_ms": 9.0},
        ])
        self.assertEqual(store.METRICS["net_rx_bytes"][0](sample), 100)
        self.assertEqual(store.METRICS["net_latency_ms"][0](sample), 1.0)


class CommandsStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termini-cmd-")
        self.db_path = os.path.join(self.tmp, "t.db")
        appdb.migrate(self.db_path)
        from app import events, logs
        self.broker = events.EventBroker()
        self.logs = logs.OperatorLogs(self.broker)
        with appdb.session(self.db_path) as conn:
            project, _ = store.ensure_project(conn, _FakeCfg(), store.now_iso())
            self.project_id = project["id"]

    def _conn(self):
        return appdb.connect(self.db_path)

    def test_human_command_goes_queued(self):
        with appdb.session(self.db_path) as conn:
            cmd = commands.issue_command(
                conn, self.logs, self.project_id, "node.reboot", {"delay_s": 0}, "human")
            self.assertEqual(cmd["state"], "queued")

    def test_ai_required_command_pending(self):
        with appdb.session(self.db_path) as conn:
            cmd = commands.issue_command(
                conn, self.logs, self.project_id, "node.drain", {}, "ai_operator")
            self.assertEqual(cmd["state"], "pending_approval")

    def test_ai_auto_command_queued(self):
        with appdb.session(self.db_path) as conn:
            cmd = commands.issue_command(
                conn, self.logs, self.project_id, "vm.resize", {}, "ai_operator")
            self.assertEqual(cmd["state"], "queued")

    def test_approve_flow(self):
        with appdb.session(self.db_path) as conn:
            cmd = commands.issue_command(
                conn, self.logs, self.project_id, "node.drain", {}, "ai_operator")
            cid = cmd["command_id"]
        with appdb.session(self.db_path) as conn:
            resp = commands.approve_command(conn, self.logs, self.project_id, cid)
            self.assertEqual(resp["state"], "approved")
            # double-approve -> 409
            from app import errors
            with self.assertRaises(errors.ApiError) as ctx:
                commands.approve_command(conn, self.logs, self.project_id, cid)
            self.assertEqual(ctx.exception.status, 409)

    def test_deny_flow(self):
        with appdb.session(self.db_path) as conn:
            cmd = commands.issue_command(
                conn, self.logs, self.project_id, "node.reboot", {}, "ai_operator")
            cid = cmd["command_id"]
        with appdb.session(self.db_path) as conn:
            resp = commands.deny_command(conn, self.logs, self.project_id, cid, "maintenance_window")
            self.assertEqual(resp["state"], "denied")
            from app import errors
            with self.assertRaises(errors.ApiError):
                commands.approve_command(conn, self.logs, self.project_id, cid)


class _FakeCfg:
    project_id = None
    project_tokens = ()
    max_inference_nodes = 1


# --------------------------------------------------------------------------- integration

class DashboardIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="termini-dash-")
        cls.db_path = os.path.join(cls.tmp, "test.db")
        cls.inventory_path = os.path.join(cls.tmp, "inventory.json")
        with open(cls.inventory_path, "w") as f:
            json.dump({
                "schema_version": "1.0", "generated_by": "provision.sh",
                "project": "termini-demo", "region": "us-central1", "zone": "us-central1-a",
                "network": {"vpc": "v", "subnetwork": "s", "cidr": "10.10.0.0/24", "gateway": "10.10.0.1"},
                "ssh": {"user": "ubuntu", "port": 22},
                "nodes": [
                    {"name": "node-a", "hostname": "termini-node-a", "role": "compute",
                     "machine_type": "n2", "private_ip": "10.10.0.11", "public_ip": "34.136.10.11",
                     "zone": "us-central1-a", "accelerator": None,
                     "tags": [], "labels": {"node": "node-a"}},
                    {"name": "node-c", "hostname": "termini-node-c", "role": "inference",
                     "machine_type": "g2", "private_ip": "10.10.0.13", "public_ip": "34.136.10.13",
                     "zone": "us-central1-a", "accelerator": {"type": "nvidia-l4", "count": 1},
                     "tags": [], "labels": {"node": "node-c"}},
                ],
            }, f)

        env = dict(os.environ)
        env.update({
            "TERMINI_HOST": "127.0.0.1",
            "TERMINI_PORT": str(PORT),
            "TERMINI_DB_PATH": cls.db_path,
            "TERMINI_BOOTSTRAP_TOKEN": "dash-boot-a,dash-boot-c",
            "TERMINI_PROJECT_TOKEN": "dash-project-token",
            "TERMINI_MAX_INFERENCE_NODES": "1",
            "TERMINI_MIN_REDACTOR_VERSION": "3",
            "TERMINI_INVENTORY_PATH": cls.inventory_path,
            "TERMINI_TERMINAL_BACKEND": "local",
            # Keep the integration server hermetic: the background operator loop
            # polls the telemetry store every second (and can shell out to LXD on
            # a breach). Nothing in these dashboard-surface tests depends on it,
            # and its 1 Hz DB churn/CI load is a source of nondeterminism.
            "TERMINI_OPERATOR_ENABLED": "0",
            "TERMINI_SSH_CA_PUBLIC_KEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJR2bq8RS3wcFV05oL8+m9nzkBr6kXzZVC13MkUiUE34 termini-operator-cert",
            "PYTHONUNBUFFERED": "1",
        })
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "app.main"], cwd=str(CONTROLLER), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        for _ in range(100):
            try:
                http("GET", "/v1/nodes/register", port=PORT)
                break
            except Exception:
                time.sleep(0.1)

        # register node A (compute) and node C (inference)
        cls.node_a, cls.node_a_token = cls._register("m-a", [], "dash-boot-a")
        cls.node_c, cls.node_c_token = cls._register("m-c", [{"vendor": "nvidia", "model": "NVIDIA L4",
                                                              "vram_bytes": L4_24GB}], "dash-boot-c")
        cls.project_id = cls.node_a["project_id"]

    @classmethod
    def _register(cls, machine_id, gpus, boot_token):
        body = {
            "schema_version": "1.0",
            "machine": {
                "machine_id": machine_id, "hostname": machine_id,
                "cpu": {"model": "x", "cores": 8, "threads": 16},
                "memory": {"total_bytes": 68719476736},
                "disks": [{"device": "/dev/sda", "size_bytes": 214748364800}],
                "gpus": gpus,
            },
        }
        st, r = http("POST", "/v1/nodes/register", body, token=boot_token, port=PORT)
        assert st in (200, 201), r
        return r, r["node_token"]

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def _telemetry(self, node_id, token, sample):
        return http("POST", f"/v1/nodes/{node_id}/telemetry",
                    envelope(node_id, sample), token=token, port=PORT)

    def test_node_list_has_health_and_status(self):
        st, r = http("GET", f"/v1/projects/{self.project_id}/nodes",
                     token="dash-project-token", port=PORT)
        self.assertEqual(st, 200)
        nodes = {n["node_id"]: n for n in r["nodes"]}
        self.assertIn(self.node_a["node_id"], nodes)
        a = nodes[self.node_a["node_id"]]
        self.assertEqual(a["role"], "compute")
        self.assertIn("health", a)
        self.assertIn("degraded_reasons", a["health"])
        self.assertIn("uptime_sec", a)
        self.assertIn("capacity", a)
        self.assertIn("status", a)

    def test_ssh_endpoint(self):
        # node-a should map to inventory public_ip via role fallback
        st, r = http("GET", f"/v1/projects/{self.project_id}/nodes/{self.node_a['node_id']}/ssh",
                     token="dash-project-token", port=PORT)
        self.assertEqual(st, 200)
        self.assertEqual(r["user"], "ubuntu")
        self.assertEqual(r["port"], 22)
        self.assertEqual(r["key_algorithm"], "ssh-ed25519")
        self.assertTrue(r["key_fingerprint"].startswith("SHA256:"))
        self.assertIn("ubuntu@", r["command"])

    def test_metric_series_point_timestamps_are_rfc3339(self):
        # Regression (W5 root integration): the dashboard charts call
        # Date.parse(point.t). The downsampled /metrics series must emit RFC3339
        # per contract §1.4, not epoch seconds — epoch silently dropped every
        # point, so the resource-utilization charts rendered "no data".
        import re
        st, _ = self._telemetry(self.node_a["node_id"], self.node_a_token,
                                valid_sample(seq=42, ts="2026-10-04T07:49:00.000Z"))
        self.assertEqual(st, 202)
        st, r = http(
            "GET",
            f"/v1/projects/{self.project_id}/nodes/{self.node_a['node_id']}"
            "/metrics?resolution=5s&metrics=cpu_util_pct",
            token="dash-project-token", port=PORT)
        self.assertEqual(st, 200)
        self.assertEqual(r["resolution"], "5s")
        points = [p for s in r["series"] for p in s["points"]]
        self.assertTrue(points, "expected at least one downsampled point")
        reg = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
        for p in points:
            self.assertRegex(p["t"], reg)
            self.assertNotRegex(p["t"], r"^\d+(\.\d+)?$")

    def test_reboot_action_and_offline_guard(self):
        st, r = self._telemetry(self.node_a["node_id"], self.node_a_token, valid_sample(seq=1))
        self.assertEqual(st, 202)
        st, r = http("POST", f"/v1/projects/{self.project_id}/nodes/{self.node_a['node_id']}/actions/reboot",
                     {"delay_s": 0}, token="dash-project-token", port=PORT)
        self.assertEqual(st, 202)
        self.assertEqual(r["type"], "node.reboot")
        self.assertEqual(r["state"], "queued")
        self.assertTrue(ulid.is_valid_ulid(r["command_id"]))

    def test_drain_action_validates_reason(self):
        st, r = http("POST", f"/v1/projects/{self.project_id}/nodes/{self.node_a['node_id']}/actions/drain",
                     {"reason": "not_a_reason"}, token="dash-project-token", port=PORT)
        self.assertEqual(st, 422)
        st, r = http("POST", f"/v1/projects/{self.project_id}/nodes/{self.node_a['node_id']}/actions/drain",
                     {"reason": "disk_degradation"}, token="dash-project-token", port=PORT)
        self.assertEqual(st, 202)
        self.assertEqual(r["type"], "node.drain")

    def test_approve_deny_endpoints(self):
        # Seed a pending_approval command directly, then approve/deny over HTTP.
        cid = ulid.new_ulid()
        with appdb.session(self.db_path) as conn:
            store.insert_command(conn, {
                "command_id": cid, "project_id": self.project_id,
                "node_id": self.node_a["node_id"], "type": "node.drain",
                "payload": {"reason": "disk_failure"}, "approval": "required",
                "state": "pending_approval", "issued_by": "ai_operator",
                "issued_at": store.now_iso(), "timeout_s": 60, "trigger": None,
            })
        st, r = http("POST", f"/v1/projects/{self.project_id}/remediations/{cid}/approve",
                     {}, token="dash-project-token", port=PORT)
        self.assertEqual(st, 200)
        self.assertEqual(r["state"], "approved")
        self.assertEqual(r["approved_by"], "human")
        # double-approve -> 409
        st, r = http("POST", f"/v1/projects/{self.project_id}/remediations/{cid}/approve",
                     {}, token="dash-project-token", port=PORT)
        self.assertEqual(st, 409)
        # deny a new pending command
        cid2 = ulid.new_ulid()
        with appdb.session(self.db_path) as conn:
            store.insert_command(conn, {
                "command_id": cid2, "project_id": self.project_id,
                "node_id": self.node_a["node_id"], "type": "node.reboot",
                "payload": {"delay_s": 0}, "approval": "required",
                "state": "pending_approval", "issued_by": "ai_operator",
                "issued_at": store.now_iso(), "timeout_s": 60, "trigger": None,
            })
        st, r = http("POST", f"/v1/projects/{self.project_id}/remediations/{cid2}/deny",
                     {"reason": "maintenance_window"}, token="dash-project-token", port=PORT)
        self.assertEqual(st, 200)
        self.assertEqual(r["state"], "denied")

    def test_intent_spec_accepted_and_rejected(self):
        st, r = http("POST", f"/v1/projects/{self.project_id}/intents",
                     {"spec": {"allocations": [
                         {"type": "agent_vm", "count": 3, "vcpus": 2, "ram_gb": 4, "disk_gb": 8},
                     ]}}, token="dash-project-token", port=PORT)
        self.assertEqual(st, 200, r)
        self.assertEqual(r["outcome"], "accepted")
        self.assertTrue(ulid.is_valid_ulid(r["plan_id"]))

        st, r = http("POST", f"/v1/projects/{self.project_id}/intents",
                     {"spec": {"allocations": [
                         {"type": "bogus_vm", "count": 1},
                     ]}}, token="dash-project-token", port=PORT)
        self.assertEqual(st, 422)
        self.assertEqual(r["error"]["code"], "GUARDRAIL_REJECTED")

    def test_apply_plan(self):
        st, plan = http("POST", f"/v1/projects/{self.project_id}/intents",
                        {"spec": {"allocations": [
                            {"type": "agent_vm", "count": 1, "vcpus": 2, "ram_gb": 4, "disk_gb": 8},
                        ]}}, token="dash-project-token", port=PORT)
        self.assertEqual(st, 200)
        st, r = http("POST", f"/v1/projects/{self.project_id}/vms/apply",
                     {"plan_id": plan["plan_id"]}, token="dash-project-token", port=PORT)
        self.assertEqual(st, 202)
        self.assertEqual(r["state"], "provisioning")

    def test_log_stream_delivers_fresh_event_over_sse(self):
        sse = SseReader(PORT, f"/v1/projects/{self.project_id}/logs?token=dash-project-token")
        # Trigger a log-emitting action.
        http("POST", f"/v1/projects/{self.project_id}/nodes/{self.node_a['node_id']}/actions/reboot",
             {"delay_s": 0}, token="dash-project-token", port=PORT)
        t0 = time.time()
        ev = sse.next_event(timeout=3.0)
        sse.close()
        self.assertIsNotNone(ev)
        self.assertLess(time.time() - t0, 3.0)
        self.assertEqual(ev["event"], "log")
        d = ev["data"]
        self.assertIn("log_id", d)
        self.assertIn("severity", d)
        self.assertIn("remediation", d)

    def test_ws_stream_mirrors_log_and_remediation(self):
        ws = WsClient(PORT, f"/stream/v1?token=dash-project-token")
        http("POST", f"/v1/projects/{self.project_id}/nodes/{self.node_a['node_id']}/actions/reboot",
             {"delay_s": 0}, token="dash-project-token", port=PORT)
        seen = []
        deadline = time.time() + 4
        while time.time() < deadline:
            msg = ws.recv_message(timeout=2.0)
            if msg is None:
                break
            seen.append(msg)
            if any(m.get("event") == "log.line" for m in seen) and \
               any(m.get("event") == "remediation.queued" for m in seen):
                break
        ws.close()
        events = {m.get("event") for m in seen}
        self.assertIn("log.line", events)
        self.assertIn("remediation.queued", events)

    def test_terminal_ws_interactive_shell(self):
        ws = WsClient(PORT, f"/terminal/v1/nodes/{self.node_a['node_id']}?token=dash-project-token")
        ready = ws.recv_message(timeout=5.0)
        self.assertIsNotNone(ready)
        self.assertEqual(ready.get("type"), "ready")

        ws.send_text(json.dumps({"type": "ping"}))
        pong = ws.recv_message(timeout=3.0)
        self.assertEqual(pong.get("type"), "pong")

        ws.send_text(json.dumps({"type": "resize", "cols": 100, "rows": 30}))

        marker = "HELLO_TERMINI_12345"
        ws.send_text(json.dumps({"type": "input",
                                 "data": base64.b64encode(f"echo {marker}\n".encode()).decode()}))
        out = ""
        deadline = time.time() + 6
        found = False
        while time.time() < deadline:
            msg = ws.recv_message(timeout=2.0)
            if msg is None:
                break
            if msg.get("type") == "output":
                out += base64.b64decode(msg.get("data", "")).decode(errors="replace")
            if marker in out:
                found = True
                break
        ws.close()
        self.assertTrue(found, f"marker not echoed; got output: {out!r}")

    def test_heartbeat_endpoint(self):
        st, r = http("POST", f"/v1/nodes/{self.node_a['node_id']}/heartbeat",
                     {}, token=self.node_a_token, port=PORT)
        self.assertIn(st, (200, 204))

    def test_terminal_ws_rejects_bad_token_with_4401(self):
        ws = WsClient(PORT, f"/terminal/v1/nodes/{self.node_a['node_id']}?token=wrong-token")
        msg = ws.recv_message(timeout=3.0)
        ws.close()
        self.assertIsNone(msg)
        self.assertEqual(ws.close_code, 4401)

    def test_terminal_ws_not_found_node(self):
        ws = WsClient(PORT, f"/terminal/v1/nodes/{ulid.new_ulid()}?token=dash-project-token")
        msg = ws.recv_message(timeout=3.0)
        ws.close()
        self.assertIsNotNone(msg)
        self.assertEqual(msg.get("type"), "error")
        self.assertEqual(msg.get("code"), "NOT_FOUND")


if __name__ == "__main__":
    unittest.main(verbosity=2)
