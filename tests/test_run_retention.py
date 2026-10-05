"""M11: run-evidence retention keeps bulky logs bounded. Offline only."""
import gzip
import os
import time
import unittest
from pathlib import Path

from support import Fixture
from mizu.fs import write_json
from mizu.storage import prune
from mizu.usage import summarize


def _touch(path: Path, days_old: float):
    stamp = time.time() - days_old * 86400
    os.utime(path, (stamp, stamp))


class RunRetentionTests(Fixture):
    def _run_with_logs(self, name: str, days_old: float, body: bytes = b'{"type":"x"}\n'):
        run = self.project.root / "runs" / name
        run.mkdir(parents=True, exist_ok=True)
        write_json(run / "result.json", {
            "finished_at": "2026-10-03T00:00:00+00:00", "role": "worker",
            "model": {"engine": "pi", "provider": "p", "model": "m",
                      "requests": 1, "usage": [{"input_tokens": 1, "output_tokens": 2}],
                      "usage_known": True},
        })
        write_json(run / "started.json", {"started_at": "2026-10-03T00:00:00+00:00"})
        write_json(run / "selection.json", {"profile": "primary"})
        events = run / "pi-events.jsonl"
        events.write_bytes(body)
        diag = run / "diagnostics.txt"
        diag.write_bytes(b"diagnostics")
        _touch(events, days_old)
        _touch(diag, days_old)
        # Keep records fresh so only logs age out.
        now = time.time()
        for keep in ("result.json", "started.json", "selection.json"):
            os.utime(run / keep, (now, now))
        return run

    def test_truncation_marker_rides_with_its_stream(self):
        self.project.set_control(paused=True)
        mid = self._run_with_logs("mid", 10)
        old = self._run_with_logs("old", 40)
        for run in (mid, old):
            marker = run / "pi-events-truncated.json"
            marker.write_bytes(b'{"truncated":true}\n')
            _touch(marker, 10 if run == mid else 40)
        dry = prune(self.project, apply=False)
        self.assertIn("runs/mid/pi-events-truncated.json", dry["event_logs_compressed"])
        self.assertIn("runs/old/pi-events-truncated.json", dry["event_logs_removed"])
        applied = prune(self.project, apply=True)
        self.assertTrue((mid / "pi-events-truncated.json.gz").exists())
        self.assertFalse((old / "pi-events-truncated.json").exists())
        self.assertFalse((old / "pi-events-truncated.json.gz").exists())
        self.assertEqual(applied["applied"], True)

    def test_recent_logs_kept_old_compressed_older_dropped(self):
        self.project.set_control(paused=True)
        recent = self._run_with_logs("recent", 1)
        mid = self._run_with_logs("mid", 10)
        old = self._run_with_logs("old", 40)
        dry = prune(self.project, apply=False)
        self.assertEqual(dry["applied"], False)
        self.assertIn("runs/mid/pi-events.jsonl", dry["event_logs_compressed"])
        self.assertIn("runs/old/pi-events.jsonl", dry["event_logs_removed"])
        self.assertNotIn("runs/recent/pi-events.jsonl", dry["event_logs_compressed"] + dry["event_logs_removed"])
        # Dry run changes nothing.
        self.assertTrue((mid / "pi-events.jsonl").exists())
        applied = prune(self.project, apply=True)
        self.assertTrue((recent / "pi-events.jsonl").exists())
        self.assertFalse((mid / "pi-events.jsonl").exists())
        self.assertTrue((mid / "pi-events.jsonl.gz").exists())
        self.assertEqual(gzip.open(mid / "pi-events.jsonl.gz", "rb").read(), b'{"type":"x"}\n')
        self.assertFalse((old / "pi-events.jsonl").exists())
        self.assertFalse((old / "pi-events.jsonl.gz").exists())
        self.assertFalse((old / "diagnostics.txt").exists())
        # Records survive every stage.
        for run in (recent, mid, old):
            for keep in ("result.json", "started.json", "selection.json"):
                self.assertTrue((run / keep).is_file(), keep)
        self.assertIn("audit", applied)
        facts = summarize(self.project)
        self.assertEqual(facts["totals"]["runs"], 3)
        self.assertEqual(facts["totals"]["requests"], 3)

    def test_compressed_copy_keeps_mtime_so_drop_clock_does_not_restart(self):
        self.project.set_control(paused=True)
        run = self._run_with_logs("mid2", 10)
        raw_mtime = run.joinpath("pi-events.jsonl").stat().st_mtime
        prune(self.project, apply=True)
        gz = run / "pi-events.jsonl.gz"
        self.assertTrue(gz.exists())
        self.assertAlmostEqual(gz.stat().st_mtime, raw_mtime, delta=2)

    def test_zero_disables_each_stage(self):
        self.project.set_control(paused=True)
        run = self._run_with_logs("old2", 40)
        text = self.file.read_text()
        text = text.replace("event_log_compress_days = 7", "event_log_compress_days = 0")
        text = text.replace("event_log_retention_days = 31", "event_log_retention_days = 0")
        self.file.write_text(text)
        from mizu.config import load
        from mizu.project import Project
        project = Project(load(self.file), "sample")
        project.set_control(paused=True)
        dry = prune(project, apply=False)
        self.assertEqual(dry["event_logs_compressed"], [])
        self.assertEqual(dry["event_logs_removed"], [])
        self.assertTrue((run / "pi-events.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
