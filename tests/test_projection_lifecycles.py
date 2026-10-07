"""Expired web-cache vs retained sessions lifecycles. Offline only."""
import json
import os
import time
import unittest
from pathlib import Path

from support import Fixture
from mizu.errors import Denied
from mizu.fs import write_json
from mizu.storage import audit, prune


def _digest(name: str) -> str:
    import hashlib
    return hashlib.sha256(name.encode()).hexdigest()


def _touch(path: Path, age_seconds: float):
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


class ProjectionLifecycleTests(Fixture):
    def test_prune_reclaims_expired_cache_but_retains_sessions(self):
        self.project.set_control(paused=True)
        # Sessions: durable evidence with one active record plus provider files.
        key = self.project.root / "sessions" / "worker" / "key-a"
        key.mkdir(parents=True, exist_ok=True)
        write_json(key / "session.json", {"id": "sess-1"})
        (key / "transcript-old.json").write_bytes(b'{"turn":1}\n')
        # Web cache: one expired, one fresh, one aged poison entry, one fresh poison entry.
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
        _touch(cache / f"{corrupt_id}.json", 7200)
        fresh_corrupt_id = _digest("https://example.com/fresh-bad")
        (cache / f"{fresh_corrupt_id}.json").write_bytes(b"not json{")
        dry = prune(self.project, apply=False)
        self.assertIn(f"web-cache/{expired_id}.json", dry["web_cache"])
        self.assertNotIn(f"web-cache/{fresh_id}.json", dry["web_cache"])
        self.assertIn(f"web-cache/{corrupt_id}.json", dry["web_cache"])
        self.assertNotIn(f"web-cache/{fresh_corrupt_id}.json", dry["web_cache"])
        self.assertTrue((cache / f"{expired_id}.json").exists())
        applied = prune(self.project, apply=True)
        self.assertFalse((cache / f"{expired_id}.json").exists())
        self.assertTrue((cache / f"{fresh_id}.json").exists())
        self.assertFalse((cache / f"{corrupt_id}.json").exists())
        self.assertTrue((cache / f"{fresh_corrupt_id}.json").exists())
        # Sessions survive every stage.
        self.assertTrue((key / "session.json").is_file())
        self.assertTrue((key / "transcript-old.json").is_file())
        self.assertIn("audit", applied)
        record = json.loads((self.project.root / applied["audit"]).read_text())
        self.assertIn("web_cache", record)

    def test_fetch_treats_poison_cache_as_miss(self):
        import tempfile
        from mizu.fs import digest as fs_digest
        from mizu.web import Web
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        cache = tmp / "cache"
        receipts = tmp / "receipts"
        cache.mkdir(parents=True)
        receipts.mkdir(parents=True)
        web = Web({"hosts": [], "feeds": [], "cache_seconds": 1800, "timeout_seconds": 5,
                   "max_bytes": 1024, "search_command": [], "intranet": False}, cache, receipts)
        url = "https://example.com/poison"
        (cache / f"{fs_digest(url.encode())}.json").write_bytes(b"not json{")
        with self.assertRaises(Denied):
            web.fetch(url)
        write_json(cache / f"{fs_digest(url.encode())}.json", {"url": url})
        with self.assertRaises(Denied):
            web.fetch(url)

    def test_prune_requires_quiescence_for_projections(self):
        with self.assertRaises(Denied):
            prune(self.project, apply=False)

    def test_audit_reports_capacity_for_remaining_stores(self):
        self.project.set_control(paused=True)
        key = self.project.root / "sessions" / "worker" / "key-b"
        key.mkdir(parents=True, exist_ok=True)
        write_json(key / "session.json", {"id": "sess-2"})
        report = audit(self.project)
        self.assertEqual(report["preview_only"], True)
        self.assertNotIn("dashboard", report)
        self.assertEqual(report["sessions"]["records"], 1)
        self.assertEqual(report["sessions"]["files"], 1)
        self.assertIn("cache_seconds", report["web_cache"])


if __name__ == "__main__":
    unittest.main()
