#!/usr/bin/env python3
"""A telemetry timeout may never kill the seeder's ticker thread.

MEASURED DEFECT (live, 2026-10-12). The seeder registered both synthetic nodes,
streamed telemetry for ~20 s, then a telemetry POST raised

    TimeoutError: timed out

and its NodeTicker thread DIED. `main` kept sleeping in its idle loop, so
`systemctl is-active termini-seeder` still reported **ACTIVE** while no telemetry
flowed at all: the dashboard showed both nodes OFFLINE ~45 s later
(controller/app/health.py: DEGRADED_AFTER_S=15, OFFLINE_AFTER_S=45) and nothing
was ever logged as an error. A silent freeze that looks like a healthy service
from the outside — the worst possible failure for a demo.

Where it came from: the controller accepts a telemetry batch inside a SQLite write
transaction, and the operator used to hold the single writer for the whole model
call, so ingesting a 1 s sample waited on a decision (up to
TERMINI_OPERATOR_DECISION_TIMEOUT_S=120). The controller half is fixed
(PR #145: no transaction open across the model call), and the seeder half is fixed
here — a transport failure is now RETRYABLE, never fatal.

Properties asserted, in order of how much they matter:

1. `_request` turns "no answer" into a retryable `TransportError`, and an UNGUARDED
   call in a thread really does end that thread (the pre-fix shape, reproduced
   against a real hanging HTTP server — not a mock);
2. `NodeTicker` survives that same real timeout: still alive, still sending once
   the controller answers, with the failed attempts counted and logged;
3. even an unexpected exception cannot end the loop (defence in depth behind the
   guard that the whole process would otherwise hang on);
4. a ticker that dies anyway is respawned by the watchdog, and a ticker that is
   alive but silent is reported loudly — once per window, not per tick;
5. end to end, `main()` rides out a real controller stall and keeps both nodes
   flowing (`--duration` run against a stub controller).
"""
from __future__ import annotations

import contextlib
import http.server
import importlib.util
import io
import json
import pathlib
import re
import sys
import threading
import time
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

NODE_A = "01M4NZK46WXG5ZEFAGAAV5JRXM"
NODE_C = "01M4NZK46WXG5ZEFAGAAV5JRXN"
PROJECT = "01J0DEM0WARM000000000000AB"


def _load_seeder():
    spec = importlib.util.spec_from_file_location("seed_demo_ticker", REPO / "console" / "seed_demo.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


seed = _load_seeder()


# --- a stub controller -------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    """Registration + telemetry, with a configurable stall.

    `cfg` (the server's mutable dict) controls the telemetry behaviour:
      * `hang_first_s`  — the FIRST telemetry request sleeps this long before 202;
      * `hang_every_s`  — EVERY telemetry request sleeps this long (a full outage);
      * `always_status` — answer this status instead of 202.
    """

    server_version = "stub-controller/1.0"
    cfg: dict  # injected on the server instance by `stub_controller`

    def log_message(self, format, *args):  # keep the test output clean
        pass

    def _json(self, code: int, obj) -> None:
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # the seeder's readiness probe
        self._json(200, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        cfg = self.server.cfg
        if self.path == "/v1/nodes/register":
            machine = json.loads(raw or b"{}")["machine"]["machine_id"]
            with cfg["lock"]:
                cfg["registrations"].append(machine)
            node_id = NODE_A if machine == "demo-node-a" else NODE_C
            self._json(200, {"node_id": node_id, "node_token": "tok-" + node_id,
                             "project_id": PROJECT})
            return
        if self.path.endswith("/telemetry"):
            node_id = self.path.split("/")[3]
            with cfg["lock"]:
                cfg["telemetry_requests"] += 1
                first = cfg["telemetry_requests"] == 1
            if cfg["always_status"]:
                self._json(cfg["always_status"], {"error": "no"})
                return
            hang = cfg["hang_every_s"] or (cfg["hang_first_s"] if first else 0.0)
            if hang:
                time.sleep(hang)
            with cfg["lock"]:
                cfg["accepted"].append(node_id)
            self._json(202, {"accepted": 1})
            return
        self._json(404, {"error": "not found"})


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Expected: a client that timed out mid-response, or a handler still sleeping
        # while the test shuts the server down.
        pass


@contextlib.contextmanager
def stub_controller(**overrides):
    cfg = {"lock": threading.Lock(), "registrations": [], "accepted": [],
           "telemetry_requests": 0, "hang_first_s": 0.0, "hang_every_s": 0.0,
           "always_status": None}
    cfg.update(overrides)
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.cfg = cfg
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", cfg
    finally:
        srv.shutdown()
        srv.server_close()


def _ticker(base: str, node_id: str = NODE_A, **kw) -> "seed.NodeTicker":
    kw.setdefault("timeout_s", 0.3)
    kw.setdefault("attempts", 2)
    kw.setdefault("retry_base_s", 0.05)
    kw.setdefault("max_retry_s", 0.1)
    return seed.NodeTicker(base, {"node_id": node_id, "node_token": "tok"}, PROJECT,
                           period_s=6.0, phase=0.0, high_ram=False, **kw)


@contextlib.contextmanager
def quiet():
    """Swallow the seeder's expected log lines (the tests assert on their effect)."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


class TheLiveTimeoutIsReproduced(unittest.TestCase):
    """The exact mechanism, against a real hanging HTTP server."""

    def test_a_timeout_is_a_retryable_error_not_an_escape(self):
        with stub_controller(hang_first_s=5.0) as (base, _cfg):
            with self.assertRaises(seed.TransportError) as ctx:
                seed._request(base, "POST", f"/v1/nodes/{NODE_A}/telemetry",
                              {"samples": []}, "tok", timeout=0.3)
            self.assertEqual(ctx.exception.kind, "timeout",
                             "a timed-out POST must be classified as retryable")

    def test_unguarded_the_thread_dies_the_way_the_demo_froze(self):
        """Pre-fix shape: nothing caught it, so the thread ended mid-stream."""
        with stub_controller(hang_first_s=5.0) as (base, _cfg):
            seen: dict = {}

            def body():
                try:
                    seed._request(base, "POST", f"/v1/nodes/{NODE_A}/telemetry",
                                  {"samples": []}, "tok", timeout=0.3)
                except BaseException as e:  # noqa: BLE001 — this is the point
                    seen["exc"] = e
                    raise

            with mock.patch.object(threading, "excepthook"):  # silence the expected traceback
                t = threading.Thread(target=body, daemon=True)
                t.start()
                t.join(5)
            self.assertFalse(t.is_alive(), "without a guard the ticker thread ends here")
            self.assertIsInstance(seen.get("exc"), seed.TransportError)


class TheTickerSurvivesATelemetryTimeout(unittest.TestCase):
    def test_the_thread_stays_alive_and_telemetry_resumes(self):
        """Same real timeout, but through the ticker: no death, no lost telemetry."""
        with stub_controller(hang_first_s=2.0) as (base, cfg), quiet():
            t = _ticker(base)
            t.start()
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and t.sent < 2:
                    time.sleep(0.05)
                self.assertGreaterEqual(
                    t.failed, 1,
                    "the test must really hit the timeout, or it does not cover the bug")
                self.assertTrue(t.is_alive(),
                                "the ticker thread must survive the telemetry timeout")
                self.assertGreaterEqual(t.sent, 2,
                                        "telemetry must resume once the controller answers")
                self.assertGreater(cfg["telemetry_requests"], 2,
                                   "the failed attempt must have been retried")
            finally:
                t.stop.set()
                t.join(5)
            self.assertFalse(t.is_alive(), "stop() must end the ticker")

    def test_a_5xx_is_retried_and_a_4xx_is_not(self):
        """`database is locked` -> HTTP 500 (retryable); a bad token -> 403 (not)."""
        with stub_controller(always_status=500) as (base, _cfg), quiet():
            t = _ticker(base)
            self.assertFalse(t.send({"seq": 1}))
            self.assertEqual(t.failed, 2, "both attempts of a 5xx tick must be spent")
        with stub_controller(always_status=403) as (base, _cfg), quiet():
            t = _ticker(base)
            self.assertFalse(t.send({"seq": 1}))
            self.assertEqual(t.failed, 1,
                             "a 403 cannot heal by being sent again; retrying it is noise")


class TheLoopCannotBeEndedByAnUnexpectedError(unittest.TestCase):
    def test_a_bug_in_the_loop_does_not_end_the_thread(self):
        calls = {"n": 0}
        real = seed._request

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("boom")
            return 202, {}

        with mock.patch.object(seed, "_request", flaky), quiet():
            t = _ticker("http://127.0.0.1:1", attempts=1)
            try:
                t.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and t.sent < 1:
                    time.sleep(0.05)
                self.assertTrue(t.is_alive(), "an unexpected exception must not end the loop")
                self.assertGreaterEqual(t.failed, 2)
                self.assertGreaterEqual(t.sent, 1, "the loop must keep ticking after the bug")
            finally:
                t.stop.set()
                t.join(5)
        self.assertIs(seed._request, real)


class RespawnKeepsIdentityAndSequence(unittest.TestCase):
    def test_respawn_reuses_the_node_and_never_rewinds_the_sequence(self):
        t = _ticker("http://127.0.0.1:1")
        t.seq = seed.initial_seq() + 20
        t.sent = 7
        fresh = t.respawn()
        self.assertIsInstance(fresh, seed.NodeTicker)
        self.assertIsNot(fresh, t)
        self.assertFalse(fresh.is_alive(), "the replacement must be started by the watchdog")
        self.assertEqual(fresh.node_id, t.node_id)
        self.assertEqual(fresh.token, t.token, "no re-registration: the same node token")
        self.assertEqual(fresh.sent, 7)
        self.assertLessEqual(
            seed.initial_seq(), t.seq,
            "a respawn that restarted at the epoch value could re-emit a seq the "
            "controller already stored in the same second (it drops those silently)")
        self.assertEqual(fresh.seq, t.seq)


class _FakeTicker:
    """A ticker stand-in for watchdog tests (no threads, no HTTP)."""

    def __init__(self, node_id: str, *, alive: bool = True, stalled: float = 0.0,
                 last_error=None, failed: int = 0, seq: int = 0):
        self.node_id = node_id
        self.alive = alive
        self.stalled = stalled
        self.last_error = last_error
        self.failed = failed
        self.seq = seq
        self.respawned_into = None
        self.start_count = 0

    def is_alive(self):
        return self.alive

    def stalled_for(self):
        return self.stalled

    def respawn(self):
        self.respawned_into = _FakeTicker(self.node_id)
        return self.respawned_into

    def start(self):
        self.start_count += 1


class WatchdogHealsADeadTicker(unittest.TestCase):
    def test_a_dead_ticker_is_respawned_and_started(self):
        dead = _FakeTicker(NODE_A, alive=False, last_error="timeout: timed out", failed=9)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = seed.watchdog_pass([dead], stall_warn_s=20.0)
        self.assertEqual(len(out), 1)
        self.assertIs(out[0], dead.respawned_into)
        self.assertEqual(dead.respawned_into.start_count, 1, "the replacement must be started")
        self.assertIn("WATCHDOG", err.getvalue())
        self.assertIn(NODE_A, err.getvalue())
        self.assertIn("timeout", err.getvalue(), "the cause must be in the log line")

    def test_a_live_ticker_is_left_alone(self):
        live = _FakeTicker(NODE_A, alive=True)
        out = seed.watchdog_pass([live], stall_warn_s=20.0)
        self.assertIs(out[0], live, "a healthy ticker is not replaced")
        self.assertEqual(live.start_count, 0)
        self.assertIsNone(live.respawned_into)


class WatchdogReportsASilentTicker(unittest.TestCase):
    """The freeze that stays invisible: alive thread, no samples landing."""

    def test_warned_once_per_window_and_re_armed_after_recovery(self):
        t = _FakeTicker(NODE_A, alive=True, stalled=999.0,
                        last_error="timeout: POST /v1/nodes/.../telemetry: timed out",
                        failed=42)
        warned: dict = {}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            seed.watchdog_pass([t], stall_warn_s=20.0, warned_at=warned)
            first = err.getvalue()
            seed.watchdog_pass([t], stall_warn_s=20.0, warned_at=warned)
            repeated = err.getvalue()
        self.assertIn("WATCHDOG", first)
        self.assertIn(NODE_A, first)
        self.assertIn("42", first, "the failed-attempt count belongs in the line")
        self.assertEqual(repeated, first, "no re-logging inside the window (1 s cadence)")
        self.assertIn(NODE_A, warned)

        t.stalled = 0.0  # the node is healthy again
        seed.watchdog_pass([t], stall_warn_s=20.0, warned_at=warned)
        self.assertNotIn(NODE_A, warned, "recovery must re-arm the warning")

        t.stalled = 999.0
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            seed.watchdog_pass([t], stall_warn_s=20.0, warned_at=warned)
        self.assertIn("WATCHDOG", err.getvalue(), "a second episode must be reported")

    def test_a_healthy_ticker_never_warns(self):
        t = _FakeTicker(NODE_A, alive=True, stalled=1.0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            seed.watchdog_pass([t], stall_warn_s=20.0)
        self.assertEqual(err.getvalue(), "")


class TheDemoRidesOutAControllerStall(unittest.TestCase):
    """End to end: `main()` against a stub controller, no mocks in the path."""

    def _run(self, argv_extra, **stub_kw):
        with stub_controller(**stub_kw) as (base, cfg):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = seed.main(["--controller", base, "--token", "ptok",
                                "--bootstrap", "b1,b2",
                                "--telemetry-timeout", "0.3",
                                "--stall-warn-seconds", "2"] + argv_extra)
            return rc, cfg, out.getvalue()

    def test_one_timed_out_post_does_not_stop_the_demo(self):
        rc, cfg, log = self._run(["--duration", "3"], hang_first_s=0.8)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(cfg["registrations"]), ["demo-node-a", "demo-node-c"])
        self.assertEqual(set(cfg["accepted"]), {NODE_A, NODE_C},
                         f"both nodes must land samples through the stall; log:\n{log}")
        self.assertIn("telemetry FAILED", log, "the timeout must be logged, not swallowed")
        self.assertIn("telemetry recovered", log)
        self.assertNotIn("had DIED", log, "the ticker must never have needed a respawn")
        for node_id in (NODE_A, NODE_C):
            m = re.search(rf"{node_id}: (\d+) sample\(s\) accepted", log)
            self.assertIsNotNone(m, f"no per-node totals for {node_id}; log:\n{log}")
            self.assertGreater(int(m.group(1)), 0)

    def test_a_full_outage_is_loud_and_still_not_fatal(self):
        """No sample lands for the whole run: the journal must say so."""
        rc, _cfg, log = self._run(["--duration", "4"], hang_every_s=6.0)
        self.assertEqual(rc, 0)
        for node_id in (NODE_A, NODE_C):
            self.assertIn(f"WATCHDOG: no telemetry accepted for {node_id}", log)
        self.assertNotIn("had DIED", log, "a stall must not degenerate into a dead thread")
        self.assertIn("sample(s) accepted", log)


if __name__ == "__main__":
    unittest.main()
