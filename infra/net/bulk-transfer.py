#!/usr/bin/env python3
"""bulk-transfer.py — resilient bulk transfer for the Termini fan network.

Why this exists
---------------
A single TCP stream collapses on the demo underlay under packet loss. Under
``tc netem loss 30%`` a 64 MiB transfer stalls after ~3 %
(2 003 672 / 67 108 864 bytes in the W7 red-team run, `fan-network-packet-loss.md`
G2) and never completes: every lost segment drives the sender's RTO into
exponential backoff and one flow cannot recover. Small request/response calls are
unaffected (15/15 ok) — the failure is specific to *bulk* transfers.

Two transports
--------------
``udp`` (default, recommended for lossy paths)
    Reliable selective-repeat over UDP. The file is cut into MTU-sized datagrams;
    the receiver keeps a bitmap and answers a query with the bitmap of what it
    has; the sender retransmits only the gaps, in rounds, until nothing is
    missing. Loss is orthogonal to throughput: because the return path is not
    the congested one, a 30 % drop rate costs ~1.4x on the wire and completes a
    64 MiB transfer in single-digit seconds (measured below). No congestion
    window to collapse, no RTO backoff.

``tcp`` (compatible fallback)
    The file is cut into fixed-size chunks, each sent over its **own short-lived
    TCP connection** with an application-level ACK and bounded per-chunk retries.
    Independent connections stop one stalled flow from stalling the whole
    transfer. Robust where UDP is filtered, but far slower under heavy loss
    (TCP still backs off per connection).

Both modes verify completion end to end (received byte count, optional SHA-256)
so the tool never reports success on a partial transfer, and both **resume**:
the receiver keeps a per-unit bitmap so a re-run only sends what is still
missing. stdlib only; runs the client and the server.

  # receiver (Node C)
  python3 bulk-transfer.py server --port 9200 --size 67108864 --out /root/artifact.bin

  # sender (Node A) — UDP, verified
  python3 bulk-transfer.py client --host <C-ip> --port 9200 --file ./artifact.bin \\
      --deadline 30 --verify sha256

  # TCP fallback
  python3 bulk-transfer.py client --host <C-ip> --port 9200 --file ./artifact.bin \\
      --transport tcp --chunk 65536 --streams 32 --deadline 30

See ``infra/net/README.md`` §Bulk transfer and
``w7/redteam/fan-network-packet-loss.md`` for the measured numbers.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time

# --------------------------------------------------------------- TCP protocol
MAGIC = b"TBT1"
# header: magic(4) type(1) reserved(3) offset(8) length(8) = 24 bytes
HDR = struct.Struct(">4sB3xQQ")
HDR_SIZE = HDR.size
T_DATA = 0
T_STATS = 1
T_FINALIZE = 2
T_HELLO = 3
ACK = b"\x01"
DEFAULT_CHUNK = 256 * 1024
SOCK_BUF = 4 * 1024 * 1024

# --------------------------------------------------------------- UDP protocol
# 8-byte sequence header; everything else is payload.
USEQ = struct.Struct(">Q")
USEQ_MAX = (1 << 64) - 1
U_QUERY = USEQ_MAX          # seq marker: "send me the received bitmap"
U_FINAL = USEQ_MAX - 1      # seq marker: "verify + finalise"
U_DATA_REPLY = b"\x02"      # server -> client: bitmap page
U_FINAL_REPLY = b"\x03"     # server -> client: final stats
# payload kept below the smallest path MTU the demo can present. Overlay path
# MTU is 1410 (GCP); 1410 - 20 (IPv4) - 8 (UDP) - 8 (seq) = 1374. Use 1360 for
# headroom; the direct path (1460) leaves slack.
DEFAULT_UDP_PAYLOAD = 1360
UDP_REPLY_PAGE = 60000      # bitmap is returned in pages of <= 60 000 bytes
DEFAULT_RATE_MBPS = 200.0


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _load_source(file: str | None, size: int, want_sha: bool) -> tuple[bytes, str]:
    if file:
        with open(file, "rb") as f:
            data = f.read()
        return data, (_sha256_file(file) if want_sha else "")
    data = b"\x00" * size
    return data, (hashlib.sha256(data).hexdigest() if want_sha else "")


# ================================================================= TCP server
def pack(ftype: int, offset: int = 0, length: int = 0) -> bytes:
    return HDR.pack(MAGIC, ftype, offset, length)


def read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        b = sock.recv(n - len(buf))
        if not b:
            raise ConnectionError("peer closed")
        buf += b
    return buf


def recv_frame(sock: socket.socket) -> tuple[int, int, int, bytes]:
    hdr = read_exact(sock, HDR_SIZE)
    magic, ftype, offset, length = HDR.unpack(hdr)
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r}")
    payload = read_exact(sock, length) if (ftype == T_DATA and length) else b""
    return ftype, offset, length, payload


def send_frame(sock: socket.socket, ftype: int, offset: int = 0, length: int = 0,
               payload: bytes = b"") -> None:
    sock.sendall(pack(ftype, offset, length) + payload)


class TcpReceiver:
    def __init__(self, port: int, size: int, out_path: str, seconds: float,
                 state_path: str | None):
        self.port, self.size, self.out_path = port, size, out_path
        self.state_path = state_path or (out_path + ".btstate")
        self.seconds = seconds
        self.fd = os.open(out_path, os.O_RDWR | os.O_CREAT, 0o644)
        os.ftruncate(self.fd, size)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.chunk = 0
        self.nchunks = 0
        self.bitmap = bytearray()
        self.unit = "chunk"
        self.conns = 0
        self.retransmits = 0
        self._load_state()

    # -- state (bitmap of completed chunks, persisted next to the output) ----
    def _load_state(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            st = json.load(open(self.state_path))
            bm = base64.b64decode(st["bitmap"])
            self.chunk = int(st["chunk"])
            self.nchunks = int(st["nchunks"])
            if len(bm) == self.nchunks and int(st.get("size", self.size)) == self.size:
                self.bitmap = bytearray(bm)
        except Exception:  # noqa: BLE001 - a corrupt sidecar must not be fatal
            self.chunk, self.nchunks, self.bitmap = 0, 0, bytearray()

    def _save_state(self) -> None:
        if not self.state_path:
            return
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"transport": "tcp", "unit": "chunk", "chunk": self.chunk,
                       "nchunks": self.nchunks, "size": self.size,
                       "bitmap": base64.b64encode(bytes(self.bitmap)).decode()}, f)
        os.replace(tmp, self.state_path)

    def _hello(self, chunk: int) -> None:
        if chunk <= 0:
            raise ValueError("chunk size must be > 0")
        if self.chunk != chunk:
            self.chunk = chunk
            self.nchunks = (self.size + chunk - 1) // chunk
            if len(self.bitmap) != self.nchunks:
                self.bitmap = bytearray(self.nchunks)

    def _stats(self, want_sha: bool) -> dict:
        with self.lock:
            done = sum(self.bitmap)
            rec = {"role": "server", "transport": "tcp", "size": self.size,
                   "chunk": self.chunk, "nchunks": self.nchunks,
                   "received_units": done,
                   "received_bytes": done * self.chunk if done < self.nchunks else self.size,
                   "complete": self.nchunks > 0 and done == self.nchunks,
                   "connections": self.conns, "retransmits": self.retransmits,
                   "bitmap_b64": base64.b64encode(bytes(self.bitmap)).decode()}
        if want_sha and rec["complete"]:
            rec["sha256"] = _sha256_file(self.out_path)
        return rec

    def _handle(self, c: socket.socket) -> None:
        try:
            for b in (socket.SO_SNDBUF, socket.SO_RCVBUF):
                try:
                    c.setsockopt(socket.SOL_SOCKET, b, SOCK_BUF)
                except OSError:
                    pass
            c.settimeout(15.0)
            while True:
                ftype, off, ln, payload = recv_frame(c)
                if ftype == T_HELLO:
                    with self.lock:
                        self._hello(int(ln))
                        self._save_state()
                    c.sendall(ACK)
                elif ftype == T_DATA:
                    if len(payload) != ln:
                        raise ValueError("short payload")
                    if self.chunk <= 0:
                        raise ValueError("data before hello")
                    with self.lock:
                        os.pwrite(self.fd, payload, off)
                        idx = off // self.chunk
                        if 0 <= idx < self.nchunks:
                            if self.bitmap[idx]:
                                self.retransmits += 1
                            self.bitmap[idx] = 1
                    c.sendall(ACK)
                elif ftype == T_STATS:
                    blob = json.dumps(self._stats(bool(ln))).encode()
                    c.sendall(struct.pack(">I", len(blob)) + blob)
                elif ftype == T_FINALIZE:
                    blob = json.dumps(self._stats(bool(ln))).encode()
                    with self.lock:
                        self._save_state()
                    c.sendall(struct.pack(">I", len(blob)) + blob)
                    self.stop.set()
                    return
                else:
                    raise ValueError(f"unknown frame type {ftype}")
        except (OSError, ValueError, ConnectionError):
            return
        finally:
            try:
                c.close()
            except OSError:
                pass

    def serve(self) -> int:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(256)
        srv.settimeout(0.5)
        t_end = time.time() + self.seconds
        try:
            while not self.stop.is_set() and time.time() < t_end:
                try:
                    c, _ = srv.accept()
                except socket.timeout:
                    continue
                self.conns += 1
                threading.Thread(target=self._handle, args=(c,), daemon=True).start()
        finally:
            srv.close()
            os.close(self.fd)
            self._save_state()
            _print_server_summary(self.size, sum(self.bitmap), self.nchunks,
                                  self.nchunks > 0 and sum(self.bitmap) == self.nchunks,
                                  self.conns, self.retransmits, "tcp", self.chunk)
        return 0


# ================================================================= TCP client
class TcpSender:
    def __init__(self, host: str, port: int, data: bytes, chunk: int, streams: int,
                 conn_timeout: float, retries: int, deadline: float,
                 want_sha: bool = False, sha: str = ""):
        self.host, self.port, self.data = host, port, data
        self.chunk, self.streams = chunk, max(1, streams)
        self.conn_timeout, self.retries, self.deadline = conn_timeout, retries, deadline
        self.want_sha, self.sha = want_sha, sha
        self.total = len(data)
        self.nchunks = (self.total + chunk - 1) // chunk
        self.lock = threading.Lock()
        self.t0 = time.time()
        self.done = self.attempts = self.chunk_retries = 0
        self.failed: list[int] = []

    def _connect(self) -> socket.socket:
        s = socket.create_connection((self.host, self.port), timeout=self.conn_timeout)
        for b in (socket.SO_SNDBUF, socket.SO_RCVBUF):
            try:
                s.setsockopt(socket.SOL_SOCKET, b, SOCK_BUF)
            except OSError:
                pass
        s.settimeout(self.conn_timeout)
        return s

    def _handshake(self) -> bool:
        """Tell the server the chunk size once (not per chunk): a HELLO round
        trip on every connection wastes a full timeout whenever the HELLO packet
        is dropped — the single largest cost under loss."""
        for _ in range(self.retries + 1):
            s = None
            try:
                s = self._connect()
                send_frame(s, T_HELLO, length=self.chunk)
                if s.recv(1) == ACK:
                    s.close()
                    return True
            except OSError:
                pass
            finally:
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
        return False

    def _stats(self) -> dict:
        s = socket.create_connection((self.host, self.port), timeout=self.conn_timeout)
        s.settimeout(max(self.conn_timeout, 60.0))
        send_frame(s, T_STATS)
        n = struct.unpack(">I", read_exact(s, 4))[0]
        blob = read_exact(s, n)
        s.close()
        return json.loads(blob)

    def _send_chunk(self, idx: int) -> None:
        off = idx * self.chunk
        payload = self.data[off:off + self.chunk]
        for attempt in range(self.retries + 1):
            if time.time() - self.t0 > self.deadline:
                break
            with self.lock:
                self.attempts += 1
                if attempt:
                    self.chunk_retries += 1
            s = None
            try:
                s = self._connect()
                send_frame(s, T_DATA, offset=off, length=len(payload), payload=payload)
                if s.recv(1) != ACK:
                    raise ConnectionError("data nack")
                s.close()
                with self.lock:
                    self.done += len(payload)
                return
            except (OSError, ConnectionError, ValueError):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
        self.failed.append(idx)

    def run(self) -> dict:
        try:
            st = self._stats()
            bm = base64.b64decode(st.get("bitmap_b64", ""))
        except (OSError, ValueError, ConnectionError):
            bm = b""
        self._handshake()
        todo = [i for i in range(self.nchunks) if not (i < len(bm) and bm[i])]
        resumed = self.nchunks - len(todo)
        if resumed:
            self.done = min(resumed * self.chunk, self.total)
        pos = 0

        def worker() -> None:
            nonlocal pos
            while True:
                with self.lock:
                    if pos >= len(todo) or time.time() - self.t0 > self.deadline:
                        return
                    idx = todo[pos]
                    pos += 1
                self._send_chunk(idx)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.streams)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=max(1.0, self.deadline + self.conn_timeout + 5))
        elapsed = time.time() - self.t0
        verify = {}
        try:
            if not self.failed and self.done >= self.total:
                s = socket.create_connection((self.host, self.port), timeout=self.conn_timeout)
                s.settimeout(120.0)
                send_frame(s, T_FINALIZE, length=self.want_sha)
                n = struct.unpack(">I", read_exact(s, 4))[0]
                verify = json.loads(read_exact(s, n))
                s.close()
        except (OSError, ValueError, ConnectionError) as e:  # noqa: BLE001
            verify = {"error": f"{type(e).__name__}: {e}"}
        return _client_record(
            "tcp", self.total, self.done, elapsed, self.failed, verify, self.want_sha,
            self.sha, {"chunk": self.chunk, "streams": self.streams,
                       "retries": self.retries, "attempts": self.attempts,
                       "chunk_retries": self.chunk_retries,
                       "resumed_units": resumed, "deadline_s": self.deadline})


# ================================================================= UDP server
class UdpReceiver:
    def __init__(self, port: int, size: int, out_path: str, seconds: float,
                 state_path: str | None, payload: int, rcvbuf: int):
        self.port, self.size, self.out_path = port, size, out_path
        self.state_path = state_path or (out_path + ".btstate")
        self.seconds, self.payload, self.rcvbuf = seconds, payload, rcvbuf
        self.fd = os.open(out_path, os.O_RDWR | os.O_CREAT, 0o644)
        os.ftruncate(self.fd, size)
        self.nseq = (size + payload - 1) // payload
        self.seen = bytearray(self.nseq)
        self.lock = threading.Lock()
        self.retransmits = 0
        self.queries = 0
        self.stop = threading.Event()
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            st = json.load(open(self.state_path))
            if (int(st.get("payload", 0)) == self.payload
                    and int(st.get("size", 0)) == self.size):
                bm = base64.b64decode(st["bitmap"])
                if len(bm) == self.nseq:
                    self.seen = bytearray(bm)
        except Exception:  # noqa: BLE001
            pass

    def _save_state(self) -> None:
        if not self.state_path:
            return
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"transport": "udp", "unit": "datagram", "payload": self.payload,
                       "size": self.size, "nseq": self.nseq,
                       "bitmap": base64.b64encode(bytes(self.seen)).decode()}, f)
        os.replace(tmp, self.state_path)

    def _bits(self) -> bytes:
        bits = bytearray((self.nseq + 7) // 8)
        for i, v in enumerate(self.seen):
            if v:
                bits[i >> 3] |= 1 << (i & 7)
        return bytes(bits)

    def _stats(self, want_sha: bool) -> dict:
        done = sum(self.seen)
        rec = {"role": "server", "transport": "udp", "size": self.size,
               "payload": self.payload, "nseq": self.nseq, "received_units": done,
               "received_bytes": done * self.payload if done < self.nseq else self.size,
               "complete": done == self.nseq, "queries": self.queries,
               "retransmits": self.retransmits}
        if want_sha and rec["complete"]:
            rec["sha256"] = _sha256_file(self.out_path)
        return rec

    def _reply_pages(self, s: socket.socket, addr, qid: int) -> None:
        bits = self._bits()
        total = len(bits)
        off = 0
        while off < total:
            chunk = bits[off:off + UDP_REPLY_PAGE]
            pkt = U_DATA_REPLY + struct.pack(">III", qid, off, total) + chunk
            for _ in range(2):  # duplicate: the reply path may itself be lossy
                try:
                    s.sendto(pkt, addr)
                except OSError:
                    pass
            off += len(chunk)

    def serve(self) -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self.rcvbuf:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf)
            except OSError:
                pass
        s.bind(("0.0.0.0", self.port))
        s.settimeout(1.0)
        t_end = time.time() + self.seconds
        while not self.stop.is_set() and time.time() < t_end:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < USEQ.size:
                continue
            seq = USEQ.unpack(data[:USEQ.size])[0]
            if seq == U_QUERY:
                qid = struct.unpack(">I", data[8:12])[0] if len(data) >= 12 else 0
                with self.lock:
                    self.queries += 1
                self._reply_pages(s, addr, qid)
                self._save_state()
            elif seq == U_FINAL:
                qid = struct.unpack(">I", data[8:12])[0] if len(data) >= 12 else 0
                want_sha = len(data) >= 13 and data[12] == 1
                blob = json.dumps(self._stats(want_sha)).encode()
                pkt = U_FINAL_REPLY + struct.pack(">II", qid, len(blob)) + blob
                for _ in range(3):
                    try:
                        s.sendto(pkt, addr)
                    except OSError:
                        pass
                self._save_state()
            elif seq < self.nseq:
                p = data[USEQ.size:]
                with self.lock:
                    if self.seen[seq]:
                        self.retransmits += 1
                    else:
                        self.seen[seq] = 1
                        os.pwrite(self.fd, p, seq * self.payload)
        s.close()
        os.close(self.fd)
        self._save_state()
        _print_server_summary(self.size, sum(self.seen), self.nseq,
                              sum(self.seen) == self.nseq, self.queries,
                              self.retransmits, "udp", self.payload)
        return 0


# ================================================================= UDP client
class UdpSender:
    def __init__(self, host: str, port: int, data: bytes, payload: int,
                 rate_mbps: float, deadline: float, retries: int,
                 want_sha: bool = False, sha: str = ""):
        self.host, self.port, self.data = host, port, data
        self.payload, self.rate_mbps = payload, rate_mbps
        self.deadline, self.retries = deadline, retries
        self.want_sha, self.sha = want_sha, sha
        self.total = len(data)
        self.nseq = (self.total + payload - 1) // payload
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUF)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUF)
        except OSError:
            pass
        self.t0 = time.time()
        self.wire_bytes = 0
        self.rounds = 0

    def _query(self) -> bytes | None:
        """Return the server's received-bitmap, or None if no reply."""
        for attempt in range(self.retries + 1):
            qid = (int(time.time() * 1000) ^ (attempt * 2654435761)) & 0xFFFFFFFF
            try:
                self.sock.sendto(USEQ.pack(U_QUERY) + struct.pack(">I", qid),
                                 (self.host, self.port))
            except OSError:
                pass
            pages: dict[int, bytes] = {}
            total = None
            self.sock.settimeout(1.5 if attempt == 0 else 1.0)
            deadline = time.time() + 4.0
            while time.time() < deadline:
                try:
                    data, _ = self.sock.recvfrom(65535)
                except socket.timeout:
                    break
                except OSError:
                    return None
                if not data or data[:1] != U_DATA_REPLY or len(data) < 13:
                    continue
                rqid, off, tlen = struct.unpack(">III", data[1:13])
                if rqid != qid:
                    continue
                total = tlen
                pages[off] = data[13:]
                if sum(len(v) for v in pages.values()) >= total:
                    break
            if total is not None and sum(len(v) for v in pages.values()) >= total:
                bits = bytearray(total)
                for off, chunk in pages.items():
                    bits[off:off + len(chunk)] = chunk
                return bytes(bits)
        return None

    def _missing(self, bits: bytes | None) -> set[int]:
        if bits is None:
            return set(range(self.nseq))
        return {i for i in range(self.nseq)
                if not (i < len(bits) * 8 and (bits[i >> 3] >> (i & 7)) & 1)}

    def run(self) -> dict:
        outstanding = self._missing(self._query())
        resumed = self.nseq - len(outstanding)
        burst = 256
        interval = (burst * (self.payload + 28) * 8) / (self.rate_mbps * 1e6)
        while outstanding and time.time() - self.t0 < self.deadline:
            self.rounds += 1
            n = 0
            for seq in outstanding:
                end = min(self.payload, self.total - seq * self.payload)
                pkt = USEQ.pack(seq) + self.data[seq * self.payload:seq * self.payload + end]
                try:
                    self.sock.sendto(pkt, (self.host, self.port))
                    self.wire_bytes += len(pkt)
                except OSError:
                    pass
                n += 1
                if n % burst == 0 and interval > 0:
                    time.sleep(interval)
            outstanding = self._missing(self._query())
        elapsed = time.time() - self.t0
        verify = {}
        done = self.total if not outstanding else (self.nseq - len(outstanding)) * self.payload
        if not outstanding:
            qid = int(time.time() * 1000) & 0xFFFFFFFF
            payload_tail = b"\x01" if self.want_sha else b"\x00"
            pkt = USEQ.pack(U_FINAL) + struct.pack(">I", qid) + payload_tail
            self.sock.settimeout(2.0)
            for _ in range(self.retries + 1):
                try:
                    self.sock.sendto(pkt, (self.host, self.port))
                    data, _ = self.sock.recvfrom(65535)
                except (OSError, socket.timeout):
                    continue
                if data[:1] == U_FINAL_REPLY and len(data) >= 9:
                    rqid, ln = struct.unpack(">II", data[1:9])
                    if rqid == qid:
                        verify = json.loads(data[9:9 + ln])
                        break
        failed = sorted(outstanding)
        rec = _client_record(
            "udp", self.total, min(done, self.total), elapsed, failed, verify,
            self.want_sha, self.sha,
            {"payload": self.payload, "rate_mbps": self.rate_mbps,
             "rounds": self.rounds, "wire_bytes": self.wire_bytes,
             "resumed_units": resumed, "deadline_s": self.deadline,
             "failed_units": len(failed)})
        try:
            self.sock.close()
        except OSError:
            pass
        return rec


# ================================================================= shared output
def _client_record(transport: str, total: int, sent: int, elapsed: float,
                   failed: list, verify: dict, want_sha: bool, sha: str,
                   extra: dict) -> dict:
    rec = {"role": "client", "transport": transport, "target_bytes": total,
           "sent_bytes": sent, "elapsed_s": round(elapsed, 3),
           "mbps": round((sent * 8 / 1e6) / elapsed, 2) if elapsed else None,
           "completed": sent >= total and not failed,
           "server": {k: verify.get(k) for k in
                      ("transport", "size", "chunk", "nchunks", "payload", "nseq",
                       "received_units", "received_bytes", "complete", "connections",
                       "retransmits", "queries", "sha256", "error") if k in verify}}
    rec.update(extra)
    if want_sha:
        rec["local_sha256"] = sha
        rec["sha256_match"] = verify.get("sha256") == sha
        rec["completed"] = rec["completed"] and bool(rec["sha256_match"])
    return rec


def _print_server_summary(size: int, done: int, units: int, complete: bool,
                          conns: int, retransmits: int, transport: str,
                          unit_size: int) -> None:
    try:
        print(json.dumps({"role": "server", "transport": transport, "size": size,
                          "unit_size": unit_size, "units": units,
                          "received_units": done, "complete": complete,
                          "connections": conns, "retransmits": retransmits}),
              flush=True)
    except Exception:  # noqa: BLE001
        pass


# ================================================================= entrypoints
def run_client(host: str, port: int, file: str | None, size: int, transport: str,
               chunk: int, streams: int, conn_timeout: float, retries: int,
               deadline: float, payload: int, rate_mbps: float, verify: str,
               out: str) -> int:
    want_sha = verify == "sha256"
    data, sha = _load_source(file, size, want_sha)
    if transport == "udp":
        rec = UdpSender(host, port, data, payload, rate_mbps, deadline, retries,
                        want_sha, sha).run()
    else:
        rec = TcpSender(host, port, data, chunk, streams, conn_timeout, retries,
                        deadline, want_sha, sha).run()
    json.dump(rec, open(out, "w"), indent=2)
    print(json.dumps(rec))
    return 0 if rec["completed"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("server")
    sp.add_argument("--port", type=int, default=9200)
    sp.add_argument("--size", type=int, required=True, help="expected total bytes")
    sp.add_argument("--out", required=True)
    sp.add_argument("--state", default=None, help="progress sidecar path")
    sp.add_argument("--seconds", type=float, default=600.0, help="max lifetime")
    sp.add_argument("--transport", choices=["udp", "tcp"], default="udp")
    sp.add_argument("--payload", type=int, default=DEFAULT_UDP_PAYLOAD,
                    help="udp payload bytes (<= path MTU - 36)")
    sp.add_argument("--rcvbuf", type=int, default=SOCK_BUF)
    cp = sub.add_parser("client")
    cp.add_argument("--host", required=True)
    cp.add_argument("--port", type=int, default=9200)
    cp.add_argument("--file", default=None)
    cp.add_argument("--size", type=int, default=64 * 1024 * 1024,
                    help="bytes of zeros when --file is omitted")
    cp.add_argument("--transport", choices=["udp", "tcp"], default="udp")
    cp.add_argument("--payload", type=int, default=DEFAULT_UDP_PAYLOAD)
    cp.add_argument("--rate-mbps", type=float, default=DEFAULT_RATE_MBPS,
                    help="udp send pacing (0 = unthrottled)")
    cp.add_argument("--chunk", type=int, default=DEFAULT_CHUNK)
    cp.add_argument("--streams", type=int, default=8)
    cp.add_argument("--conn-timeout", type=float, default=3.0)
    cp.add_argument("--retries", type=int, default=8)
    cp.add_argument("--deadline", type=float, default=30.0)
    cp.add_argument("--verify", choices=["none", "bytes", "sha256"], default="bytes")
    cp.add_argument("--out", default="/tmp/bulk-client.json")
    args = ap.parse_args()
    if args.cmd == "server":
        if args.transport == "udp":
            return UdpReceiver(args.port, args.size, args.out, args.seconds,
                               args.state, args.payload, args.rcvbuf).serve()
        return TcpReceiver(args.port, args.size, args.out, args.seconds,
                           args.state).serve()
    return run_client(args.host, args.port, args.file, args.size, args.transport,
                      args.chunk, args.streams, args.conn_timeout, args.retries,
                      args.deadline, args.payload, args.rate_mbps, args.verify,
                      args.out)


if __name__ == "__main__":
    raise SystemExit(main())
