"""Interactive terminal bridge — xterm.js WebSocket to a node shell.

Implements the frozen framing and session lifecycle in dashboard.md §3.1:

  * node-level host shell (not a VM shell), selected by ``{node_id}``;
  * input/output/resize/ping/pong framing, terminal bytes base64-encoded;
  * one session per node (a second connection replaces the first);
  * idle > 15 min closes with ``exit`` code -1;
  * client close terminates the shell.

Reaching the node: the primary path is the node's outbound mTLS control channel
(D6); the demo bridge proxies via SSH using a short-lived CA-signed operator
certificate (dashboard.md §3.1, D4). The SSH backend shells out to the system
``ssh`` client over a local PTY (``pty`` + ``ssh -tt``), so terminal bytes are
binary-clean and resizes are forwarded via SIGWINCH. A ``local`` backend
(``bash -i``) exists for hermetic tests.
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from typing import Any

from . import ws as wsmod

BACKEND_LOCAL = "local"
BACKEND_SSH = "ssh"


def _set_winsize(master_fd: int, cols: int, rows: int) -> None:
    try:
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


class TerminalSession:
    """One interactive shell session brokered over a WebSocket."""

    def __init__(
        self,
        ws: wsmod.WebSocket,
        *,
        node_id: str,
        user: str,
        shell: str,
        master_fd: int,
        proc: subprocess.Popen,
        idle_timeout_s: int,
        on_close=None,
    ) -> None:
        self.ws = ws
        self.node_id = node_id
        self.user = user
        self.shell = shell
        self._master = master_fd
        self._proc = proc
        self._idle_timeout_s = idle_timeout_s
        self._on_close = on_close
        self._lock = threading.Lock()
        self._closed = False
        self._last_activity = time.time()
        self._started_output = False

    # ------------------------------------------------------------------ lifecycle
    def run(self) -> None:
        """Drive the client→server side until the client disconnects."""
        try:
            self._send({"type": "ready", "node_id": self.node_id,
                        "user": self.user, "shell": self.shell})
        except OSError:
            self.close()
            return
        reader = threading.Thread(target=self._reader_loop, daemon=True)
        reader.start()
        try:
            while not self._closed:
                msg = self.ws.recv_message()
                self._last_activity = time.time()
                self._handle_client(msg)
        except (wsmod.ConnectionClosed, wsmod.WebSocketError, OSError):
            pass  # client closed / protocol error -> terminate the shell
        finally:
            self.close()

    def _handle_client(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            self._send({"type": "error", "code": "INVALID_ARGUMENT",
                        "message": "frame is not valid JSON"})
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        if mtype == "input":
            data = msg.get("data")
            if isinstance(data, str):
                try:
                    payload = base64.b64decode(data, validate=True)
                except Exception:
                    return
                self._write_master(payload)
        elif mtype == "resize":
            cols = msg.get("cols")
            rows = msg.get("rows")
            if isinstance(cols, int) and isinstance(rows, int) and cols > 0 and rows > 0:
                _set_winsize(self._master, cols, rows)
                if self._proc and self._proc.poll() is None:
                    try:
                        os.kill(self._proc.pid, signal.SIGWINCH)
                    except OSError:
                        pass
        elif mtype == "ping":
            self._send({"type": "pong"})

    # -------------------------------------------------------------------- output
    def _reader_loop(self) -> None:
        buf = b""
        try:
            while not self._closed:
                ready, _, _ = select.select([self._master], [], [], 1.0)
                if ready:
                    chunk = os.read(self._master, 65536)
                    if not chunk:
                        break
                    self._started_output = True
                    self._last_activity = time.time()
                    self._send({"type": "output",
                                "data": base64.b64encode(chunk).decode("ascii")})
                elif time.time() - self._last_activity >= self._idle_timeout_s:
                    self._send({"type": "exit", "code": -1, "reason": "idle_timeout"})
                    self.close()
                    return
        except (OSError, ValueError):
            pass
        finally:
            if not self._closed:
                # Shell exited (or connect-time failure). Distinguish a clean exit
                # from an unreachable node: no output + instant non-zero exit.
                self._finish()

    def _finish(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        code = self._proc.poll() if self._proc else None
        if not self._started_output and (code not in (None, 0)):
            self._send({"type": "error", "code": "NODE_OFFLINE",
                        "message": "node not reachable"})
        else:
            self._send({"type": "exit", "code": code if code is not None else 0,
                        "reason": "normal"})
        self._teardown()

    # --------------------------------------------------------------------- I/O
    def _write_master(self, data: bytes) -> None:
        try:
            os.write(self._master, data)
        except OSError:
            self.close()

    def _send(self, obj: dict) -> None:
        try:
            self.ws.send_text(json.dumps(obj))
        except OSError:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._teardown()

    def _teardown(self) -> None:
        try:
            os.close(self._master)
        except OSError:
            pass
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                except OSError:
                    pass
        try:
            self.ws.send_close(1000, "session closed")
        except OSError:
            pass
        if self._on_close:
            try:
                self._on_close(self.node_id)
            except Exception:
                pass


def spawn_shell(backend: str, *, identity_key_path: str | None, cert_path: str | None,
                user: str, host: str | None, port: int, cols: int, rows: int) -> tuple[int, subprocess.Popen, str]:
    """Open a PTY and spawn the shell. Returns (master_fd, proc, shell_label).

    The SSH backend authenticates with a short-lived, CA-signed operator
    certificate (``identity_key_path`` + ``cert_path``), never a long-lived
    controller-held key (D4).
    """
    import pty

    if backend == BACKEND_LOCAL:
        cmd = ["/bin/bash", "-i"]
        shell = "/bin/bash"
    else:
        cmd = [
            "ssh", "-tt",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", "LogLevel=ERROR",
            "-i", identity_key_path or "",
        ]
        if cert_path:
            cmd += ["-o", f"CertificateFile={cert_path}"]
        cmd += [
            "-p", str(port),
            f"{user}@{host}",
        ]
        shell = "/bin/bash"

    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, cols, rows)
    proc = subprocess.Popen(
        cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        close_fds=True, start_new_session=True,
    )
    os.close(slave_fd)
    return master_fd, proc, shell


class TerminalManager:
    """One-session-per-node registry (dashboard.md §3.1)."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = threading.Lock()

    def acquire(self, node_id: str) -> TerminalSession | None:
        with self._lock:
            return self._sessions.get(node_id)

    def replace(self, node_id: str) -> TerminalSession | None:
        """Evict the existing session for node_id, returning it (or None)."""
        with self._lock:
            return self._sessions.pop(node_id, None)

    def register(self, session: TerminalSession) -> None:
        with self._lock:
            self._sessions[session.node_id] = session

    def _release(self, node_id: str) -> None:
        with self._lock:
            self._sessions.pop(node_id, None)

    def on_close(self, node_id: str) -> None:
        self._release(node_id)
