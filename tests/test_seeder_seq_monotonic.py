"""The seeder's sample sequence must survive a restart.

Found while bringing the LIVE demo back up: after a restart the dashboard showed both
nodes ONLINE with fresh telemetry arriving, yet NO threshold fired, NO decision was
recorded and NO remediation happened — and nothing was logged as an error.

Cause: `store.insert_samples` drops any sample whose `seq` is not greater than the
highest seq already stored for that node (out-of-order protection). The seeder seeded
its counter with `int(time.time()) % 100000`, which WRAPS. The previous run had left
`last_seq = 43102`; a restart put the counter at `18339` — below the high-water mark —
so every single sample was silently discarded. Telemetry looked healthy (HTTP 202) and
the operator loop polled happily over an empty stream.

Two properties are asserted: the sequence cannot wrap, and the store really does drop
a non-increasing seq (so the first property is load-bearing rather than decorative).
"""
from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO))

from app import db as appdb   # noqa: E402
from app import store         # noqa: E402


def _load_seeder():
    spec = importlib.util.spec_from_file_location("seed_demo", REPO / "console" / "seed_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SeqDoesNotWrap(unittest.TestCase):
    def test_initial_seq_is_not_modulo_wrapped(self):
        """A `% 100000` value is always < 100000; the epoch is not. This is the trap."""
        seeder = _load_seeder()
        seq = seeder.initial_seq()
        self.assertGreater(seq, 100_000,
                           "initial_seq must not be modulo-wrapped, or a restart can "
                           "produce a seq below the previous run's and every sample drops")

    def test_initial_seq_is_monotonic_in_time(self):
        seeder = _load_seeder()
        self.assertLess(seeder.initial_seq(1_791_700_000), seeder.initial_seq(1_791_818_339))

    def test_wrapped_formula_would_have_regressed(self):
        """Documents the actual numbers that broke the live demo."""
        last_seq_previous_run = 43102
        wrapped_now = 1_791_818_339 % 100_000      # 18339
        self.assertLess(wrapped_now, last_seq_previous_run,
                        "the wrapped value sits BELOW the previous high-water mark")
        self.assertGreater(1_791_818_339, last_seq_previous_run)


class StoreDropsNonIncreasingSeq(unittest.TestCase):
    """Why the above matters: the store really does discard these."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "controller.db"
        appdb.migrate(str(self.db))
        self.conn = sqlite3.connect(str(self.db))
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _sample(self, seq: int, ts: str) -> dict:
        return {"seq": seq, "ts": ts, "cpu_util_pct": 1.0}

    def test_lower_seq_is_dropped_and_higher_is_kept(self):
        node = "01M4NZK46WXG5ZEFAGAAV5JRXM"
        # previous run left a high-water mark
        ins, drop = store.insert_samples(self.conn, node, "c1",
                                         [self._sample(43102, "2026-10-11T18:00:00Z")])
        self.assertEqual((ins, drop), (1, 0))

        # a restart with a WRAPPED counter -> dropped
        ins, drop = store.insert_samples(self.conn, node, "c2",
                                         [self._sample(18339, "2026-10-12T15:00:00Z")])
        self.assertEqual((ins, drop), (0, 1), "a wrapped seq must be dropped (the bug)")

        # the unwrapped epoch -> kept
        ins, drop = store.insert_samples(self.conn, node, "c3",
                                         [self._sample(1791818339, "2026-10-12T15:00:01Z")])
        self.assertEqual((ins, drop), (1, 0), "an unwrapped seq must be accepted (the fix)")


if __name__ == "__main__":
    unittest.main()
