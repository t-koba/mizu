"""Aggregate event retention is policy; transport parsing stays fixed mechanism. Offline only."""
import dataclasses
import json
import os
import sys
import unittest

from support import Fixture
from mizu.drivers import EVENT_RECORD_BYTES, EVENT_STREAM_BYTES
from mizu.engine_channel import Channel, stream_budget
from mizu.errors import ProtocolError
from mizu.fs import read_json


FLOOD_LINES = 20000


def _flood_argv():
    # Byte-exact on every host: binary writes never translate LF to CRLF the
    # way Windows text-mode stdout does, so retained+dropped accounting holds.
    return [sys.executable, "-c",
            "import json,sys\n"
            f"for i in range({FLOOD_LINES}):\n"
            " sys.stdout.buffer.write((json.dumps({'type':'tick','n':i,'pad':'x'*200})+'\\n').encode())"]


class EventEvidenceTests(Fixture):
    def _channel(self, argv, label, budget=None):
        ctx = self.context()
        if budget is not None:
            ctx.config = dataclasses.replace(
                ctx.config, limits=dataclasses.replace(ctx.config.limits, event_stream_bytes=budget))
        return Channel(ctx, argv, dict(os.environ), str(ctx.run_dir), label)

    def _drain(self, channel, *, swallow=True):
        received = 0
        try:
            while True:
                channel.receive()
                received += 1
        except ProtocolError:
            if not swallow:
                raise
        return received

    def test_policy_default_matches_mechanism_default(self):
        from mizu.config import Limits
        self.assertEqual(Limits().event_stream_bytes, EVENT_STREAM_BYTES)
        self.assertEqual(stream_budget(object()), EVENT_STREAM_BYTES)

    def test_overflow_keeps_parsing_and_marks_truncation(self):
        budget = 65536
        channel = self._channel(_flood_argv(), "flood", budget)
        try:
            received = self._drain(channel)
        finally:
            channel.close()
        self.assertEqual(received, FLOOD_LINES)
        self.assertTrue(channel.truncated)
        events = channel.context.run_dir / "flood-events.jsonl"
        self.assertEqual(events.stat().st_size, budget)
        total = sum(len(json.dumps({"type": "tick", "n": i, "pad": "x" * 200}).encode()) + 1
                    for i in range(FLOOD_LINES))
        self.assertEqual(channel.retained_bytes, budget)
        self.assertEqual(channel.retained_bytes + channel.dropped_bytes, total)
        marker = read_json(channel.context.run_dir / "flood-events-truncated.json")
        self.assertTrue(marker["truncated"])
        self.assertEqual(marker["stream_budget"], budget)
        self.assertEqual(marker["retained_bytes"], budget)
        self.assertEqual(marker["dropped_bytes"], total - budget)

    def test_no_marker_without_overflow(self):
        channel = self._channel(
            [sys.executable, "-c", "print('{\"type\":\"done\"}')"], "small")
        try:
            self._drain(channel)
        finally:
            channel.close()
        self.assertFalse(channel.truncated)
        self.assertFalse((channel.context.run_dir / "small-events-truncated.json").exists())

    def test_single_record_past_transport_bound_fails_closed(self):
        channel = self._channel(
            [sys.executable, "-c",
             "import sys; sys.stdout.write('x' * (2 * 1024 * 1024))"], "huge")
        try:
            with self.assertRaises(ProtocolError):
                self._drain(channel, swallow=False)
        finally:
            channel.close()
        self.assertGreater(len(channel.raw), EVENT_RECORD_BYTES)


if __name__ == "__main__":
    unittest.main()
