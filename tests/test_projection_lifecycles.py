"""Per-store lifecycles: disposable dashboard generations, expired web cache, retained sessions. Offline only."""
import json
import os
import time
import unittest
from pathlib import Path

from support import Fixture
from mizu.errors import Denied
from mizu.fs import write_json
from mizu.storage import audit, dashboard_candidates, prune


def _digest(name: str) -> str:
    import hashlib
    return hashlib.sha256(name.encode()).hexdigest()


def _touch(path: Path, age_seconds: float):
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


class ProjectionLifecycleTests(Fixture):
    def _dashboard(self, count: int, live_index: int | None = None):
        root = self.project.root / "dashboard"
        root.mkdir(parents=True, exist_ok=True)
        ids = []
        for i in range(count):
            did = _digest(f"dashboard-{i}")
            ids.append(did)
            (root / f"{did}.json").write_bytes(b'{"project":"sample"}\n')
            _touch(root / f"{did}.json", (count - i) * 10)
        if live_index is not None:
            write_json(root / "latest.json", {"dashboard": ids[live_index], "snapshot": _digest("snap")})
        return ids

    def test_dashboard_keep_protects_live_and_newest(self):
        self.project.set_control(paused=True)
        ids = self._dashboard(count=5, live_index=0)
        victims = dashboard_candidates(self.project, 3)
        names = sorted(p.name for p in victims)
        # Total 5, keep 3 incl. live (oldest). Oldest non-live beyond keep are victims.
        self.assertEqual(len(victims), 2)
        self.assertNotIn(f"{ids[0]}.json", names)
        # Oldest non-live go first.
        self.assertIn(f"{ids[1]}.json", names)

    def test_dashboard_zero_keeps_all(self):
        self.project.set_control(paused=True)
        self._dashboard(count=3, live_index=0)
        self.assertEqual(dashboard_candidates(self.project, 0), [])

    def test_dashboard_symlink_never_candidate(self):
        self.project.set_control(paused=True)
        ids = self._dashboard(count=2, live_index=1)
        root = self.project.root / "dashboard"
        link = root / f"{_digest('link')}.json"
        try:
            link.symlink_to(root / f"{ids[0]}.json")
        except OSError:
            self.skipTest("symlinks unavailable")
        victims = dashboard_candidates(self.project, 1)
        self.assertNotIn(link.name, [p.name for p in victims])

    def test_prune_reclaims_dashboard_and_expired_cache_but_retains_sessions(self):
        import dataclasses
        self.project.config = dataclasses.replace(
            self.project.config,
            limits=dataclasses.replace(self.project.config.limits, dashboard_keep=2))
        self.project.set_control(paused=True)
        ids = self._dashboard(count=4, live_index=3)
        # Sessions: durable evidence with one active record plus provider files.
        key = self.project.root / "sessions" / "worker" / "key-a"
        key.mkdir(parents=True, exist_ok=True)
        write_json(key / "session.json", {"id": "sess-1"})
        (key / "transcript-old.json").write_bytes(b'{"turn":1}\n')
        # Web cache: one expired, one fresh, one corrupt.
        cache = self.config.data / "web-cache"
        cache.mkdir(parents=True, exist_ok=True)
        expired_id = _digest("https://example.com/old")
        fresh_id = _digest("https://example.com/new")
        write_json(cache / f"{expired_id}.json", {"url": "https://example.com/old",
                   "retrieved_epoch": time.time() - 7200, "status": 200, "text": "old"})
        write_json(cache / f"{fresh_id}.json", {"url": "https://example.com/new",
                   "retrieved_epoch": time.time(), "status": 200, "text": "new"})
        corrupt_id = _digest("https://example.com/bad")
        (cache / f"{corrupt_id}.json").write_bytes(b"not json{")
        dry = prune(self.project, apply=False)
        self.assertIn(f"dashboard/{ids[0]}.json", dry["dashboard_generations"])
        self.assertNotIn(f"dashboard/{ids[3]}.json", dry["dashboard_generations"])
        self.assertIn(f"web-cache/{expired_id}.json", dry["web_cache"])
        self.assertNotIn(f"web-cache/{fresh_id}.json", dry["web_cache"])
        self.assertNotIn(f"web-cache/{corrupt_id}.json", dry["web_cache"])
        # Dry run removes nothing.
        self.assertTrue((self.project.root / "dashboard" / f"{ids[0]}.json").exists())
        self.assertTrue((cache / f"{expired_id}.json").exists())
        applied = prune(self.project, apply=True)
        self.assertFalse((self.project.root / "dashboard" / f"{ids[0]}.json").exists())
        self.assertTrue((self.project.root / "dashboard" / f"{ids[3]}.json").exists())
        self.assertFalse((cache / f"{expired_id}.json").exists())
        self.assertTrue((cache / f"{fresh_id}.json").exists())
        self.assertTrue((cache / f"{corrupt_id}.json").exists())
        # Sessions survive every stage.
        self.assertTrue((key / "session.json").is_file())
        self.assertTrue((key / "transcript-old.json").is_file())
        self.assertIn("audit", applied)
        record = json.loads((self.project.root / applied["audit"]).read_text())
        self.assertIn("dashboard_generations", record)
        self.assertIn("web_cache", record)

    def test_prune_requires_quiescence_for_projections(self):
        self._dashboard(count=2, live_index=1)
        with self.assertRaises(Denied):
            prune(self.project, apply=False)

    def test_audit_reports_capacity_for_all_three_stores(self):
        self.project.set_control(paused=True)
        self._dashboard(count=2, live_index=1)
        key = self.project.root / "sessions" / "worker" / "key-b"
        key.mkdir(parents=True, exist_ok=True)
        write_json(key / "session.json", {"id": "sess-2"})
        report = audit(self.project)
        self.assertEqual(report["preview_only"], True)
        self.assertEqual(report["dashboard"]["documents"], 2)
        self.assertEqual(report["dashboard"]["keep"], 30)
        self.assertIsNotNone(report["dashboard"]["live"])
        self.assertEqual(report["sessions"]["records"], 1)
        self.assertEqual(report["sessions"]["files"], 1)
        self.assertIn("cache_seconds", report["web_cache"])


if __name__ == "__main__":
    unittest.main()
