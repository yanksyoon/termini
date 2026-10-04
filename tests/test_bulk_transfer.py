"""Unit tests for infra/net/bulk-transfer.py (loopback, stdlib only).

Covers both transports end to end on 127.0.0.1 — completion, byte count and
SHA-256 verification — plus the resume path (a second run over an existing
receiver bitmap). The lossy-path acceptance lives in
``infra/net/verify-bulk-transfer.sh`` (needs lxc + tc).
"""
from __future__ import annotations

import importlib.util
import os
import socket
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BT_PATH = os.path.join(ROOT, "infra", "net", "bulk-transfer.py")


def load_module():
    spec = importlib.util.spec_from_file_location("bulk_transfer", BT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def free_port(socktype):
    s = socket.socket(socket.AF_INET, socktype)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


mod = load_module()


class UdpTransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.port = free_port(socket.SOCK_DGRAM)
        self.size = 512 * 1024
        self.out = os.path.join(self.tmp, "recv.bin")
        self.recv = mod.UdpReceiver(self.port, self.size, self.out, 15.0, None,
                                    1360, 1 << 20)
        self.thread = threading.Thread(target=self.recv.serve, daemon=True)
        self.thread.start()
        time.sleep(0.4)  # let the receiver bind

    def tearDown(self):
        self.recv.stop.set()
        self.thread.join(timeout=5)

    def test_udp_transfer_completes_and_verifies(self):
        import hashlib
        data = b"z" * self.size
        sha = hashlib.sha256(data).hexdigest()
        snd = mod.UdpSender("127.0.0.1", self.port, data, 1360, 400.0, 15.0, 8, True, sha)
        rec = snd.run()
        self.assertTrue(rec["completed"], rec)
        self.assertTrue(rec["sha256_match"], rec)
        self.assertEqual(rec["server"]["received_bytes"], self.size)
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), data)

    def test_udp_resume_skips_delivered_units(self):
        import hashlib
        half = (self.size // 2 // 1360) * 1360  # align to the datagram boundary
        data = b"q" * self.size
        # first pass delivers the first half
        snd1 = mod.UdpSender("127.0.0.1", self.port, data[:half], 1360, 400.0, 15.0,
                             8, False, "")
        self.assertTrue(snd1.run()["completed"])
        # second pass sends the whole file; the first half is already there
        snd2 = mod.UdpSender("127.0.0.1", self.port, data, 1360, 400.0, 15.0, 8,
                             True, hashlib.sha256(data).hexdigest())
        rec = snd2.run()
        self.assertGreater(rec["resumed_units"], 0, rec)
        self.assertTrue(rec["completed"], rec)
        self.assertTrue(rec["sha256_match"], rec)
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), data)


class TcpTransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.port = free_port(socket.SOCK_STREAM)
        self.size = 512 * 1024
        self.out = os.path.join(self.tmp, "recv.bin")
        self.recv = mod.TcpReceiver(self.port, self.size, self.out, 15.0, None)
        self.thread = threading.Thread(target=self.recv.serve, daemon=True)
        self.thread.start()
        time.sleep(0.4)

    def tearDown(self):
        self.recv.stop.set()
        self.thread.join(timeout=5)

    def test_tcp_transfer_completes_and_verifies(self):
        import hashlib
        data = b"t" * self.size
        sha = hashlib.sha256(data).hexdigest()
        snd = mod.TcpSender("127.0.0.1", self.port, data, 64 * 1024, 4, 3.0, 6,
                            15.0, True, sha)
        rec = snd.run()
        self.assertTrue(rec["completed"], rec)
        self.assertTrue(rec["sha256_match"], rec)
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), data)


if __name__ == "__main__":
    unittest.main()
