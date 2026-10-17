"""The live-fleet window lease (kanban t_9c1362f6).

These pin the property the card asks for — **(a) two concurrent workers cannot both
hold a live window, and the loser is refused loudly** — plus the surrounding rules
that make the refusal usable: the message names the holder, purpose and expiry; an
expired window is reclaimable; only the owner can release; `check` gives a script a
distinguishable exit code; an unreadable lease file is never trampled.

The concurrency test runs REAL separate processes against one state directory with
the critical section deliberately widened (TERMINI_LIVE_WINDOW_TEST_DELAY_S). That
is what makes it a proof rather than a race that usually passes: with the read and
the write unserialised, every taker would see "free" and every taker would win.

Run: python3 -m unittest tests.test_live_window -v   (from the repo root)
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
TOOL = REPO / "infra" / "gcp" / "tools" / "live-window.py"


def _load():
    spec = importlib.util.spec_from_file_location("termini_live_window", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lw = _load()


def run_cli(args: list[str], state_dir: str, *, env_extra: dict | None = None):
    env = dict(os.environ)
    env.pop("TERMINI_LIVE_WINDOW_TEST_DELAY_S", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(TOOL), "--state-dir", state_dir] + args,
        capture_output=True, text=True, env=env, timeout=60,
    )


class LeaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="live-window-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def state(self) -> dict:
        return json.loads(pathlib.Path(self.dir, lw.LEASE_NAME).read_text())

    def write_state(self, **overrides) -> dict:
        lease = {
            "schema_version": "1.0",
            "owner": "t_c37aa0dc",
            "purpose": "live acceptance: demo fault clear telemetry",
            "taken_at": "2026-10-17T19:30:00.000Z",
            "taken_at_epoch": time.time() - 60,
            "expires_at": "2026-10-17T20:15:00.000Z",
            "expires_at_epoch": time.time() + 3600,
            "ttl_s": 2700.0,
            "host": "termini-controller",
            "pid": 1234,
        }
        lease.update(overrides)
        pathlib.Path(self.dir, lw.LEASE_NAME).write_text(json.dumps(lease))
        return lease


class TakeTest(LeaseTestCase):
    def test_take_creates_a_readable_lease(self):
        res = run_cli(["take", "--owner", "t_9c1362f6", "--ttl", "45m",
                       "--purpose", "live acceptance: t_9c1362f6"], self.dir)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("taken: t_9c1362f6 holds the live fleet", res.stdout)
        lease = self.state()
        self.assertEqual(lease["owner"], "t_9c1362f6")
        self.assertEqual(lease["purpose"], "live acceptance: t_9c1362f6")
        self.assertAlmostEqual(lease["ttl_s"], 2700.0)
        self.assertGreater(lease["expires_at_epoch"], time.time() + 2600)

    def test_second_owner_is_refused_loudly_and_nothing_changes(self):
        first = run_cli(["take", "--owner", "t_c37aa0dc", "--ttl", "45m",
                         "--purpose", "peer acceptance"], self.dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.state()

        second = run_cli(["take", "--owner", "t_9c1362f6", "--ttl", "45m",
                          "--purpose", "my acceptance"], self.dir)
        self.assertEqual(second.returncode, lw.EXIT_HELD, second.stdout)
        # Loud = the refusal says who holds it, why, and until when.
        self.assertIn("REFUSED: the live fleet is already claimed", second.stderr)
        self.assertIn("owner=t_c37aa0dc", second.stderr)
        self.assertIn("peer acceptance", second.stderr)
        self.assertIn("expires in", second.stderr)
        self.assertIn("You are t_9c1362f6", second.stderr)
        self.assertEqual(self.state(), before, "the refusal must not touch the lease")

    def test_same_owner_renews_rather_than_refusing(self):
        run_cli(["take", "--owner", "t_9c1362f6", "--ttl", "10m", "--purpose", "p1"],
                self.dir)
        first = self.state()
        out = run_cli(["take", "--owner", "t_9c1362f6", "--ttl", "45m", "--purpose", "p2"],
                      self.dir)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("renewed", out.stdout)
        renewed = self.state()
        self.assertEqual(renewed["owner"], "t_9c1362f6")
        self.assertEqual(renewed["purpose"], "p2")
        self.assertGreater(renewed["expires_at_epoch"], first["expires_at_epoch"])
        self.assertIn("renewed_at", renewed)

    def test_expired_lease_is_reclaimable_and_says_so(self):
        self.write_state(owner="t_c37aa0dc", expires_at_epoch=time.time() - 30)
        out = run_cli(["take", "--owner", "t_9c1362f6", "--ttl", "30m",
                       "--purpose", "after the peer's window lapsed"], self.dir)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("reclaimed an EXPIRED window from t_c37aa0dc", out.stdout)
        self.assertEqual(self.state()["owner"], "t_9c1362f6")

    def test_purpose_is_required(self):
        out = run_cli(["take", "--owner", "t_9c1362f6"], self.dir)
        self.assertEqual(out.returncode, lw.EXIT_USAGE)
        self.assertIn("--purpose", out.stderr)
        self.assertFalse(pathlib.Path(self.dir, lw.LEASE_NAME).exists())
        # …and the API-level guard has the same rule, with a reason a human reads.
        with self.assertRaises(lw.Refused) as ctx:
            lw.LiveWindow(self.dir).take("t_9c1362f6", "   ", 600)
        self.assertIn("--purpose is required", str(ctx.exception))

    def test_bad_owner_and_bad_ttl_are_usage_errors(self):
        out = run_cli(["take", "--owner", "no spaces allowed", "--purpose", "x"], self.dir)
        self.assertEqual(out.returncode, lw.EXIT_USAGE)
        self.assertIn("--owner must be", out.stderr)
        out = run_cli(["take", "--owner", "t_9c1362f6", "--ttl", "7h", "--purpose", "x"],
                      self.dir)
        self.assertEqual(out.returncode, lw.EXIT_USAGE)
        self.assertIn("between 60s and 6h", out.stderr)


class ConcurrencyTest(LeaseTestCase):
    """Acceptance (a): two concurrent takers cannot both hold the window."""

    def test_concurrent_takers_exactly_one_wins(self):
        takers = [f"t_0000000{i}" for i in range(6)]
        env = dict(os.environ,
                   TERMINI_LIVE_WINDOW_TEST_DELAY_S="0.25")
        procs = []
        for owner in takers:
            procs.append(subprocess.Popen(
                [sys.executable, str(TOOL), "--state-dir", self.dir, "take",
                 "--owner", owner, "--ttl", "30m", "--purpose", f"concurrent taker {owner}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env))
        results = []
        for owner, proc in zip(takers, procs):
            out, err = proc.communicate()
            results.append((owner, out, err, proc.returncode))

        winners = [r[0] for r in results if r[3] == 0]
        losers = [r for r in results if r[3] == lw.EXIT_HELD]
        self.assertEqual(len(winners), 1,
                         f"exactly one taker must win, got {winners}: {results}")
        self.assertEqual(len(losers), len(takers) - 1, results)
        # The lease on disk is the winner's, and every loser was told who it is.
        self.assertEqual(self.state()["owner"], winners[0])
        for owner, out, err, _rc in losers:
            self.assertIn("REFUSED: the live fleet is already claimed", err,
                          f"{owner} lost without a refusal message: {out}{err}")
            self.assertIn(f"owner={winners[0]}", err)

    def test_a_release_frees_the_window_for_the_next_owner(self):
        self.assertEqual(run_cli(["take", "--owner", "t_9c1362f6", "--purpose", "a"],
                                 self.dir).returncode, 0)
        self.assertEqual(run_cli(["release", "--owner", "t_9c1362f6"], self.dir).returncode, 0)
        out = run_cli(["take", "--owner", "t_c37aa0dc", "--purpose", "b"], self.dir)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.state()["owner"], "t_c37aa0dc")


class ReleaseAndCheckTest(LeaseTestCase):
    def test_non_owner_cannot_release(self):
        run_cli(["take", "--owner", "t_c37aa0dc", "--purpose", "peer"], self.dir)
        before = self.state()
        out = run_cli(["release", "--owner", "t_9c1362f6"], self.dir)
        self.assertEqual(out.returncode, lw.EXIT_NOT_OWNER, out.stdout)
        self.assertIn("belongs to someone else", out.stderr)
        self.assertEqual(self.state(), before)

    def test_release_with_nothing_held_is_not_an_error(self):
        out = run_cli(["release", "--owner", "t_9c1362f6"], self.dir)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("nothing to release", out.stdout)

    def test_check_exit_codes_are_scriptable(self):
        # 4: nothing claimed
        self.assertEqual(run_cli(["check", "--owner", "t_9c1362f6"], self.dir).returncode,
                         lw.EXIT_NO_LEASE)
        run_cli(["take", "--owner", "t_9c1362f6", "--purpose", "mine"], self.dir)
        # 0: owned by the caller
        self.assertEqual(run_cli(["check", "--owner", "t_9c1362f6"], self.dir).returncode,
                         lw.EXIT_OK)
        # 3: held by somebody else
        other = run_cli(["check", "--owner", "t_c37aa0dc"], self.dir)
        self.assertEqual(other.returncode, lw.EXIT_HELD)
        self.assertIn("another owner holds the live window", other.stdout)

    def test_an_expired_owner_window_does_not_pass_check(self):
        self.write_state(owner="t_9c1362f6", expires_at_epoch=time.time() - 5)
        out = run_cli(["check", "--owner", "t_9c1362f6"], self.dir)
        self.assertEqual(out.returncode, lw.EXIT_NO_LEASE)
        self.assertIn("expired", out.stdout)

    def test_status_reports_holder_and_remaining_time(self):
        run_cli(["take", "--owner", "t_c37aa0dc", "--ttl", "45m", "--purpose", "peer"], self.dir)
        out = run_cli(["status"], self.dir)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("owner=t_c37aa0dc", out.stdout)
        self.assertIn("HELD by t_c37aa0dc", out.stdout)
        empty = tempfile.TemporaryDirectory(prefix="live-window-empty-")
        self.addCleanup(empty.cleanup)
        free = run_cli(["status"], empty.name)
        self.assertIn("free — no live window is claimed", free.stdout)

    def test_an_unreadable_lease_file_is_not_trampled(self):
        pathlib.Path(self.dir, lw.LEASE_NAME).write_text("{not json")
        out = run_cli(["take", "--owner", "t_9c1362f6", "--purpose", "x"], self.dir)
        self.assertEqual(out.returncode, lw.EXIT_UNREADABLE, out.stdout)
        self.assertIn("Refusing to trample it", out.stderr)
        self.assertEqual(pathlib.Path(self.dir, lw.LEASE_NAME).read_text(), "{not json")


class TtlParseTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(lw.parse_ttl("90s"), 90)
        self.assertEqual(lw.parse_ttl("45m"), 2700)
        self.assertEqual(lw.parse_ttl("2h"), 7200)
        self.assertEqual(lw.parse_ttl("600"), 600)

    def test_out_of_range(self):
        for bad in ("30s", "7h", "abc", ""):
            with self.assertRaises(lw.Refused):
                lw.parse_ttl(bad)


if __name__ == "__main__":
    unittest.main()
