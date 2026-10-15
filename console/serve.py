#!/usr/bin/env python3
"""Termini W5 — customer operations console (dashboard + web terminal).

One origin, one command. Serves the operations dashboard at ``/`` and the
xterm.js web terminal at ``/terminal/``, both pointed at the same controller,
with runtime configuration injected server-side so the URL stays clean and the
project token is not pasted into the address bar.

    python3 console/serve.py \\
        --controller http://127.0.0.1:8085 \\
        --project 01J0DEM0WARM000000000000AB \\
        --token <project_token> \\
        --key '~/.ssh/termini_operator'

Then open ``http://127.0.0.1:8090/`` (dashboard) — the header's *Web terminal*
control opens ``/terminal/`` on the same controller.

This is a *static compose + config injector*: it does not proxy the controller.
The dashboard and terminal talk to ``--controller`` directly (CORS is enabled on
the controller for REST + SSE; the terminal WebSocket carries the token in the
query string, contract ``docs/contracts/dashboard.md`` §3.1). It is stdlib-only
and safe to run next to the controller.

Env fallbacks: ``TERMINI_CONTROLLER_URL``, ``TERMINI_PROJECT_ID``,
``TERMINI_PROJECT_TOKEN``, ``TERMINI_SSH_CLIENT_KEY_PATH``.
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, ".."))
DASH_DIR = os.path.join(REPO_ROOT, "dashboard")
TERM_DIR = os.path.join(REPO_ROOT, "frontend", "terminal")
# Operator-only fault console (controller-api.md §4.7). Deliberately NOT linked
# from the client dashboard's nav and NOT given the project token by this server:
# the operator pastes the token themselves, so an unauthenticated visitor can load
# the page and do nothing with it (the controller answers 401/403).
FAULT_DIR = os.path.join(HERE, "fault_console")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".map": "application/json",
}

# Injected before the terminal module loads; app.js reads window.__TERMINI_TERMINAL_DEFAULTS__.
TERM_ANCHOR = '<script type="module" src="js/app.js"></script>'

# Injected into the operator fault console's index.html; app.js reads
# window.__TERMINI_FAULT_DEFAULTS__. Controller URL + project id ONLY — the console
# is useless (and must stay useless) without the operator typing the project token.
FAULT_ANCHOR = '<script src="js/app.js"></script>'


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _safe_join(root: str, rel: str) -> str | None:
    """Join ``root`` + ``rel`` and confine the result to ``root``."""
    rel = posixpath.normpath("/" + rel).lstrip("/")
    target = os.path.normpath(os.path.join(root, rel))
    if target == root or target.startswith(root + os.sep):
        return target
    return None


class ConsoleConfig:
    controller: str  # e.g. http://127.0.0.1:8085  (no /v1)
    project: str
    token: str
    key: str

    def dashboard_config_js(self, base_js: bytes) -> bytes:
        """Return dashboard/config.js with the runtime values overlaid.

        Appending an override keeps the original file's documented precedence
        intact (query params > localStorage > window.TERMINI_CONFIG > defaults):
        the injected object is still just window.TERMINI_CONFIG, only filled in.
        """
        override = {
            "apiBase": f"{self.controller}/v1",
            "projectId": self.project,
            "token": self.token,
            "terminalUrl": "/terminal/",
        }
        tail = (
            "\n// injected by console/serve.py\n"
            "window.TERMINI_CONFIG = Object.assign({}, window.TERMINI_CONFIG, "
            + json.dumps(override)
            + ");\n"
        )
        return base_js + tail.encode("utf-8")

    def fault_index_html(self, base_html: bytes) -> bytes:
        """Return the fault console's index.html with the non-secret defaults.

        Only `apiBase` and `projectId` are injected. The project token is NOT:
        the console requires the operator to paste it, and it lives in the tab's
        sessionStorage — that is what keeps the surface operator-only.
        """
        defaults = {"apiBase": self.controller, "projectId": self.project}
        script = ("<script>window.__TERMINI_FAULT_DEFAULTS__ = "
                  + json.dumps(defaults) + ";</script>\n    ")
        text = base_html.decode("utf-8")
        if FAULT_ANCHOR in text:
            text = text.replace(FAULT_ANCHOR, script + FAULT_ANCHOR, 1)
        else:  # anchor moved/renamed -> fail loud rather than serve a console that cannot talk
            raise RuntimeError("fault console index.html is missing the app.js anchor")
        return text.encode("utf-8")

    def terminal_index_html(self, base_html: bytes) -> bytes:
        defaults = {"api": self.controller, "project": self.project,
                    "token": self.token, "key": self.key}
        script = (
            "<script>window.__TERMINI_TERMINAL_DEFAULTS__ = "
            + json.dumps(defaults)
            + ";</script>\n    "
        )
        text = base_html.decode("utf-8")
        if TERM_ANCHOR in text:
            text = text.replace(TERM_ANCHOR, script + TERM_ANCHOR, 1)
        else:  # anchor moved/renamed -> fail loud rather than serve stale UI
            raise RuntimeError("terminal index.html is missing the app.js anchor")
        return text.encode("utf-8")


class ConsoleHandler(BaseHTTPRequestHandler):
    server: "ConsoleServer"  # type: ignore[assignment]  # set by socketserver
    server_version = "termini-console/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - http.server API name
        sys.stderr.write("[console] %s - %s\n" % (self.address_string(), format % args))

    # ------------------------------------------------------------------ helpers
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _not_found(self) -> None:
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def _ctype(self, path: str) -> str:
        return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")

    def _static(self, root: str, rel: str) -> None:
        target = _safe_join(root, rel)
        if target is None or not os.path.isfile(target):
            return self._not_found()
        self._send(200, _read(target), self._ctype(target))

    # ------------------------------------------------------------------ routes
    def do_GET(self) -> None:  # noqa: N802
        self.do_HEAD()

    def do_HEAD(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        norm = posixpath.normpath(path)

        if norm in ("/healthz", "/health"):
            return self._send(200, b"ok\n", "text/plain; charset=utf-8")

        # console home + dashboard assets
        if norm in ("/", "/index.html"):
            base = _read(os.path.join(DASH_DIR, "index.html"))
            return self._send(200, base, "text/html; charset=utf-8")

        if norm == "/config.js":
            base = _read(os.path.join(DASH_DIR, "config.js"))
            return self._send(200, self.server.cfg.dashboard_config_js(base),
                              "text/javascript; charset=utf-8")

        # terminal bundle
        if norm in ("/terminal", "/terminal/", "/terminal/index.html"):
            base = _read(os.path.join(TERM_DIR, "index.html"))
            return self._send(200, self.server.cfg.terminal_index_html(base),
                              "text/html; charset=utf-8")

        if norm.startswith("/terminal/"):
            return self._static(TERM_DIR, norm[len("/terminal/"):])

        # operator-only fault console (controller-api.md §4.7)
        if norm in ("/console", "/console/", "/console/index.html"):
            base = _read(os.path.join(FAULT_DIR, "index.html"))
            return self._send(200, self.server.cfg.fault_index_html(base),
                              "text/html; charset=utf-8")

        if norm.startswith("/console/"):
            return self._static(FAULT_DIR, norm[len("/console/"):])

        # everything else is a dashboard asset (assets/, js/, favicon…)
        return self._static(DASH_DIR, norm.lstrip("/"))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    cfg: ConsoleConfig


def build_server(host: str, port: int, cfg: ConsoleConfig) -> ConsoleServer:
    srv = ConsoleServer((host, port), ConsoleHandler)
    srv.cfg = cfg
    return srv


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Termini W5 customer operations console")
    ap.add_argument("--host", default=os.environ.get("TERMINI_CONSOLE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("TERMINI_CONSOLE_PORT", "8090")))
    ap.add_argument("--controller", default=os.environ.get("TERMINI_CONTROLLER_URL", "http://127.0.0.1:8085"))
    ap.add_argument("--project", default=os.environ.get("TERMINI_PROJECT_ID", ""))
    ap.add_argument("--token", default=os.environ.get("TERMINI_PROJECT_TOKEN", ""))
    ap.add_argument("--key", default=os.environ.get("TERMINI_SSH_CLIENT_KEY_PATH", "~/.ssh/termini_operator"))
    args = ap.parse_args(argv)

    cfg = ConsoleConfig()
    cfg.controller = args.controller.rstrip("/")
    cfg.project = args.project
    cfg.token = args.token
    cfg.key = args.key

    srv = build_server(args.host, args.port, cfg)
    host, port = srv.server_address[0], srv.server_address[1]
    print(f"[console] controller : {cfg.controller}")
    print(f"[console] project    : {cfg.project or '(unset — set in the dashboard Settings dialog)'}")
    print(f"[console] dashboard  : http://{host}:{port}/")
    print(f"[console] terminal   : http://{host}:{port}/terminal/")
    print(f"[console] fault console (operator-only, project token required): "
          f"http://{host}:{port}/console/")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
