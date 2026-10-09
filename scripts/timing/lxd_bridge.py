#!/usr/bin/env python3
"""lxd_bridge.py — expose LXD's unix-socket REST API over TCP for the demo.

The Termini controller's operator executor (controller/app/operator/executor.py)
speaks the LXD REST API over plain HTTP(S) at TERMINI_LXD_BASE_URL. On this
single-host emulation LXD (snap) is only reachable through its unix socket
(/var/snap/lxd/common/lxd/unix.socket); there is no TCP listener. This tiny
stdlib bridge relays each HTTP request to the LXD unix socket and streams the
response back, so the *unmodified production executor* issues real LXD REST
calls that really mutate the demo cluster.

It is transport glue only: it does not inspect or rewrite method/path/body.
"""
import socket
import socketserver
import sys

SOCK = "/var/snap/lxd/common/lxd/unix.socket"


def _read_http_request(rfile):
    line = rfile.readline()
    if not line:
        return None
    request_line = line
    headers = []
    clen = 0
    while True:
        h = rfile.readline()
        if h in (b"\r\n", b"\n", b""):
            break
        headers.append(h)
        name = h.split(b":", 1)[0].strip().lower()
        if name == b"content-length":
            try:
                clen = int(h.split(b":", 1)[1].strip())
            except ValueError:
                clen = 0
    body = rfile.read(clen) if clen else b""
    return request_line, headers, body


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        parsed = _read_http_request(self.rfile)
        if parsed is None:
            return
        request_line, headers, body = parsed
        # Rebuild request for the unix socket: force Connection: close and a
        # correct Content-Length so the upstream read terminates cleanly.
        out = [request_line]
        for h in headers:
            name = h.split(b":", 1)[0].strip().lower()
            if name in (b"connection", b"content-length", b"host"):
                continue
            out.append(h)
        out.append(b"Host: localhost\r\n")
        out.append(b"Connection: close\r\n")
        out.append(b"Content-Length: %d\r\n" % len(body))
        out.append(b"\r\n")
        out.append(body)
        payload = b"".join(out)

        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            up.connect(SOCK)
            up.sendall(payload)
            chunks = []
            while True:
                c = up.recv(65536)
                if not c:
                    break
                chunks.append(c)
        finally:
            up.close()
        self.wfile.write(b"".join(chunks))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8443
    srv = Server((host, port), Handler)
    print(f"[lxd_bridge] LXD unix socket {SOCK} -> http://{host}:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
