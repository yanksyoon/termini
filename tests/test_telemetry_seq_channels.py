"""One `seq` namespace per telemetry source — the demo fault console must not
starve the node's own stream (t_c37aa0dc).

What went wrong on the live fleet (2026-10-17): `store.insert_samples` deduped
`seq` against the highest seq stored for the NODE, and the demo fault injector
wrote its 1 Hz series into that same space. The node's own agent reports at
~0.3 Hz from its own counter, so after a 60 s injection the node was tens of
seqs behind and every sample it posted after the clear was accepted (202) and
dropped — 2 m 19 s of missing node telemetry while the dashboard kept showing
the injector's fabricated 62 % baseline.

The fix (migration 0021) gives each source its own channel and its own `seq`
head:

  * `store.NODE_SOURCE` ("node") — the node's own telemetry;
  * `store.DEMO_FAULT_SOURCE` ("demo_fault") — the operator's injected series.

Dedup and head lookups are per `(node_id, source)`, so the injector can never
occupy a `seq` the node has not sent yet, and every stored row says which
channel it came from. Both properties are pinned here.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "controller"))

from app import db as appdb    # noqa: E402
from app import store          # noqa: E402

NODE = "01M4ZQ7Q2J80ATY3K32PY3PPJH"


def _sample(seq: int, ts: str, ram_pct: float = 62.0) -> dict:
    return {
        "seq": seq,
        "ts": ts,
        "ram_total_bytes": 8 * store.GiB,
        "ram_used_bytes": int(8 * store.GiB * ram_pct / 100.0),
    }


def _iso(offset_s: int) -> str:
    """RFC3339 millis `offset_s` seconds after 2026-10-17T18:41:00Z."""
    import datetime

    base = datetime.datetime(2026, 10, 17, 18, 41, 0, tzinfo=datetime.timezone.utc)
    dt = base + datetime.timedelta(seconds=offset_s)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "controller.db"

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn


class SourceChannelStarvationTest(_DbCase):
    """The injected series must not consume the node's own seq values."""

    def setUp(self):
        super().setUp()
        appdb.migrate(str(self.db_path))
        self.conn = self.connect()

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_injected_seq_range_cannot_swallow_the_nodes_next_sample(self):
        # The node has reported seq 1..3 and is about to send 4.
        ins, drop = store.insert_samples(self.conn, NODE, "c-node", [
            _sample(1, "2026-10-17T18:40:00.000Z"),
            _sample(2, "2026-10-17T18:40:03.200Z"),
            _sample(3, "2026-10-17T18:40:06.400Z"),
        ])
        self.assertEqual((ins, drop), (3, 0))

        # A 60 s injection at 1 Hz: 60 samples, seq 1..60 in ITS OWN channel.
        injected = [
            _sample(i, _iso(i), ram_pct=97.0)
            for i in range(1, 61)
        ]
        ins, drop = store.insert_samples(
            self.conn, NODE, "c-fault", injected, source=store.DEMO_FAULT_SOURCE
        )
        self.assertEqual((ins, drop), (60, 0), "injected samples must all be stored")
        self.assertEqual(store.max_seq(self.conn, NODE, store.DEMO_FAULT_SOURCE), 60)
        self.assertEqual(store.max_seq(self.conn, NODE), 3)

        # The node's own next sample — the one the old shared head dropped — is
        # accepted immediately, and it is the newest row the read paths return.
        ins, drop = store.insert_samples(self.conn, NODE, "c-node", [
            _sample(4, "2026-10-17T18:42:01.000Z"),
        ])
        self.assertEqual((ins, drop), (1, 0),
                         "the injection's seq range starved the node's own stream")
        self.assertEqual(store.max_seq(self.conn, NODE), 4)

        latest = store.latest_sample(self.conn, NODE)
        self.assertEqual(latest["seq"], 4)
        self.assertAlmostEqual(
            latest["ram_used_bytes"] / latest["ram_total_bytes"] * 100, 62.0, places=3
        )

    def test_dedup_within_the_node_channel_is_unchanged(self):
        store.insert_samples(self.conn, NODE, "c1", [_sample(10, "2026-10-17T18:40:00.000Z")])
        ins, drop = store.insert_samples(self.conn, NODE, "c2", [
            _sample(10, "2026-10-17T18:40:01.000Z"),   # duplicate seq
            _sample(9, "2026-10-17T18:40:02.000Z"),    # out of order
        ])
        self.assertEqual((ins, drop), (0, 2), "node-channel dedup must still hold")

    def test_stored_rows_carry_their_source(self):
        store.insert_samples(self.conn, NODE, "c-node", [
            _sample(10, "2026-10-17T18:40:00.000Z"),
        ])
        store.insert_samples(self.conn, NODE, "c-fault", [
            _sample(10, "2026-10-17T18:40:01.000Z", ram_pct=97.0),
        ], source=store.DEMO_FAULT_SOURCE)
        rows = self.conn.execute(
            "SELECT source, seq, payload_json FROM telemetry_samples"
            " WHERE node_id = ? ORDER BY source", (NODE,)
        ).fetchall()
        self.assertEqual([r["source"] for r in rows], ["demo_fault", "node"])
        # …and the same seq exists in both channels: provenances are distinguishable.
        self.assertEqual([r["seq"] for r in rows], [10, 10])


class MigrationBackfillTest(_DbCase):
    """Migration 0021 keeps every pre-existing row as the node's own series."""

    def test_existing_samples_survive_and_default_to_the_node_channel(self):
        migrations = sorted((REPO / "controller" / "app" / "migrations").glob("*.sql"))
        pre = [p for p in migrations if int(p.stem.split("_", 1)[0]) <= 20]
        self.assertTrue(pre, "expected the pre-0021 migrations to exist")

        conn = self.connect()
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
            " name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for path in pre:
            version = int(path.stem.split("_", 1)[0])
            conn.executescript(path.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, path.stem, "2026-10-17T00:00:00Z"),
            )
        # Pre-fix shape: one row, no `source` column, shared seq space.
        conn.execute(
            "INSERT INTO telemetry_samples (call_id, node_id, seq, ts, ts_epoch, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("c-old", NODE, 29271, "2026-10-17T18:44:51.195Z", 1792253091.195,
             '{"seq": 29271, "ts": "2026-10-17T18:44:51.195Z"}'),
        )
        conn.commit()
        old_id = conn.execute("SELECT id FROM telemetry_samples").fetchone()["id"]
        conn.close()

        applied = appdb.migrate(str(self.db_path))
        self.assertIn("0021_telemetry_source_channel", applied)

        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM telemetry_samples").fetchone()
            self.assertEqual(row["id"], old_id, "row identity must be preserved")
            self.assertEqual(row["source"], "node", "existing rows are the node's own")
            self.assertEqual(row["seq"], 29271)
            self.assertEqual(row["ts"], "2026-10-17T18:44:51.195Z")
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            # The new unique key is per channel: the same seq may now exist for the
            # injected series, which is exactly what stops it starving the node.
            conn.execute(
                "INSERT INTO telemetry_samples (call_id, node_id, source, seq, ts, ts_epoch,"
                " payload_json) VALUES (?, ?, 'demo_fault', ?, ?, ?, ?)",
                ("c-fault", NODE, 29271, "2026-10-17T18:44:51.195Z", 1792253091.195, "{}"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_migration_is_idempotent(self):
        appdb.migrate(str(self.db_path))
        self.assertEqual(appdb.migrate(str(self.db_path)), [],
                         "a second boot must apply nothing")


if __name__ == "__main__":
    unittest.main()
