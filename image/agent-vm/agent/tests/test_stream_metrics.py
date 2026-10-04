"""Offline unit tests for streaming-chunk accounting (no ADK / no network).

Run at bake time by image/agent-vm/provision/35-agent-selftest.sh and by the
recipe check; stdlib `unittest` only.
"""

from __future__ import annotations

import time
import unittest

from termini_agent.stream_metrics import ChunkRecorder


class TestChunkRecorder(unittest.TestCase):
    def test_requires_start(self) -> None:
        with self.assertRaises(RuntimeError):
            ChunkRecorder().record("x")

    def test_rejects_empty_and_unknown_kind(self) -> None:
        rec = ChunkRecorder().start()
        with self.assertRaises(ValueError):
            rec.record("")
        with self.assertRaises(ValueError):
            rec.record("x", "thought")

    def test_single_blob_is_not_streaming(self) -> None:
        rec = ChunkRecorder().start()
        rec.record("whole answer")
        self.assertEqual(rec.count, 1)
        self.assertFalse(rec.streamed())

    def test_incremental_chunks_are_streaming(self) -> None:
        rec = ChunkRecorder().start()
        for tok in ("Host ", "healthy", " now"):
            rec.record(tok, "content")
            time.sleep(0.002)
        self.assertEqual(rec.count, 3)
        self.assertEqual(rec.content_count, 3)
        self.assertEqual(rec.content, "Host healthy now")
        self.assertTrue(rec.streamed())
        self.assertIsNotNone(rec.first_token_ms)
        self.assertLessEqual(rec.first_token_ms, rec.last_token_ms)
        self.assertEqual(len(rec.inter_token_ms()), 2)

    def test_reasoning_and_content_channels_tracked_separately(self) -> None:
        rec = ChunkRecorder().start()
        rec.record("think ", "reasoning")
        rec.record("more", "reasoning")
        rec.record("answer", "content")
        d = rec.as_dict()
        self.assertEqual(d["reasoning_chunks"], 2)
        self.assertEqual(d["content_chunks"], 1)
        self.assertEqual(d["reasoning"], "think more")
        self.assertEqual(d["content"], "answer")
        self.assertTrue(rec.streamed())

    def test_report_shape(self) -> None:
        rec = ChunkRecorder().start()
        rec.record("a")
        rec.record("b")
        d = rec.as_dict()
        for key in ("chunks", "content_chunks", "reasoning_chunks", "first_token_ms",
                    "last_token_ms", "total_ms", "inter_token_ms", "content", "reasoning"):
            self.assertIn(key, d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
