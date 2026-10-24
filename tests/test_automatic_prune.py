"""Automatic per-project run retention at safe execution boundaries. Offline only."""
import dataclasses
import datetime as dt
import json
import time
import uuid
from unittest import mock

from support import Fixture
from mizu import storage
from mizu import storage_runs as runs
from mizu.config import load
from mizu.errors import ConfigError
from mizu.fs import lock, write_json

CLOCK = 2_000_000_000
DAY = 86400


def stamp(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


class AutomaticPruneTests(Fixture):
    def setUp(self):
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(runs.time, "time", return_value=CLOCK).start()
        self.state = self.project.root / "maintenance/automatic-prune.json"

    def run_record(self, age=40, *, parent=None, terminal="result.json", status=None):
        run = uuid.uuid4().hex
        directory = self.project.root / "runs" / run
        started = {"run": run, "role": "worker", "snapshot": "a" * 64,
                   "started_at": stamp(CLOCK - age * DAY)}
        result = {"run": run, "role": "worker", "snapshot": "a" * 64,
                  "finished_at": stamp(CLOCK - age * DAY + 1),
                  "status": status or "completed"}
        if parent:
            started["parent_run"] = parent
            result["parent_run"] = parent
        if terminal in ("consultation.json", "delegation.json"):
            result.update(answer="answer", snapshot="a" * 64, profile="primary")
        elif terminal == "error.json":
            result.update(status=status or "interrupted", error="failure")
        else:
            result["finish"] = {"outcome": "done", "summary": "done", "state": ""}
        write_json(directory / "started.json", started)
        if terminal:
            write_json(directory / terminal, result)
        return run

    def interval(self, seconds):
        self.project.config = dataclasses.replace(
            self.project.config,
            limits=dataclasses.replace(self.config.limits,
                                       automatic_prune_interval_seconds=seconds))

    def test_policy_default_and_bounds(self):
        self.assertEqual(load(self.file).limits.automatic_prune_interval_seconds, 86400)
        original = self.file.read_text()
        self.assertIn("automatic_prune_interval_seconds = 86400", original)
        for value in ("0", "2678400"):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    "automatic_prune_interval_seconds = 86400",
                    "automatic_prune_interval_seconds = " + value))
                self.assertEqual(load(self.file).limits.automatic_prune_interval_seconds, int(value))
        for value in ("-1", "2678401", "true", '"86400"', "86400.0"):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    "automatic_prune_interval_seconds = 86400",
                    "automatic_prune_interval_seconds = " + value))
                with self.assertRaises(ConfigError):
                    load(self.file)
        self.file.write_text(original)

    def test_zero_disables_but_manual_prune_stays(self):
        old = self.run_record()
        self.interval(0)
        self.assertEqual(storage.maintain_automatic_prune(self.project), {"status": "disabled"})
        self.assertFalse(self.state.exists())
        self.assertTrue((self.project.root / "runs" / old).is_dir())
        # Manual prune is unchanged and still expires the old run.
        applied = storage.prune(self.project, apply=True)
        self.assertTrue(applied["applied"])
        self.assertFalse((self.project.root / "runs" / old).exists())

    def test_due_prunes_then_not_due(self):
        old = self.run_record()
        result = storage.maintain_automatic_prune(self.project)
        self.assertEqual(result["status"], "pruned")
        self.assertFalse((self.project.root / "runs" / old).exists())
        self.assertTrue(self.state.is_file())
        stored = json.loads(self.state.read_bytes())
        self.assertEqual(stored, result)
        self.assertEqual(storage.maintain_automatic_prune(self.project), {"status": "not-due"})

    def test_not_due_when_recent(self):
        self.run_record()
        first = storage.maintain_automatic_prune(self.project)
        self.assertEqual(first["status"], "pruned")
        # A fresh check is not due; no prune locks are taken.
        with mock.patch.object(storage, "prune", side_effect=AssertionError):
            self.assertEqual(storage.maintain_automatic_prune(self.project), {"status": "not-due"})

    def test_busy_does_not_consume_cadence(self):
        old = self.run_record()
        with lock(self.project.root / "locks/automatic-prune.lock"):
            self.assertEqual(storage.maintain_automatic_prune(self.project), {"status": "busy"})
        self.assertFalse(self.state.exists())
        self.assertTrue((self.project.root / "runs" / old).is_dir())
        # Workspace contention is also busy and leaves the cadence due.
        with lock(self.project.root / "locks/workspace.lock"):
            self.assertEqual(storage.maintain_automatic_prune(self.project), {"status": "busy"})
        self.assertFalse(self.state.exists())
        self.assertTrue((self.project.root / "runs" / old).is_dir())
        # After release the due sweep still runs.
        self.assertEqual(storage.maintain_automatic_prune(self.project)["status"], "pruned")
        self.assertFalse((self.project.root / "runs" / old).exists())

    def test_malformed_state_is_unavailable_without_deletion(self):
        old = self.run_record()
        write_json(self.state, {"checked_at": "bad"})
        self.assertEqual(storage.maintain_automatic_prune(self.project), {"status": "unavailable"})
        self.assertTrue((self.project.root / "runs" / old).is_dir())
        # Future-dated state also skips without deletion.
        write_json(self.state, {"checked_at": int(time.time()) + 3600})
        self.assertEqual(storage.maintain_automatic_prune(self.project), {"status": "unavailable"})
        self.assertTrue((self.project.root / "runs" / old).is_dir())

    def test_incomplete_scan_is_refused_without_deletion_or_failure(self):
        old = self.run_record()
        bad = self.project.root / "inbox" / "bad.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"{")
        result = storage.maintain_automatic_prune(self.project)
        self.assertEqual(result["status"], "refused")
        self.assertIn("error", result)
        self.assertTrue((self.project.root / "runs" / old).is_dir())
        self.assertTrue(self.state.is_file())

    def test_old_terminal_expires_without_pause_live_refs_and_artifacts_kept(self):
        self.assertFalse(self.project.control().get("paused"))
        free = self.run_record()
        pinned = self.run_record()
        write_json(self.project.root / "inbox" / "sample.json", {"run": pinned})
        # Two dated artifacts: manual count-based deletion would drop one,
        # but automatic retention must never enable that clock.
        root = self.project.root / "artifacts"
        for name in ("old-one", "old-two"):
            child = root / name
            child.mkdir(parents=True, exist_ok=True)
            write_json(child / "evidence.json", {"published_at": "2020-01-01T00:00:00+00:00"})
        snapshots_before = sorted((self.project.root / "snapshots").glob("*.json"))
        self.assertTrue(snapshots_before)
        result = storage.maintain_automatic_prune(self.project)
        self.assertEqual(result["status"], "pruned", result)
        self.assertFalse(self.project.control().get("paused"))
        self.assertFalse((self.project.root / "runs" / free).exists())
        self.assertTrue((self.project.root / "runs" / pinned).is_dir())
        self.assertTrue((root / "old-one").is_dir())
        self.assertTrue((root / "old-two").is_dir())
        self.assertEqual(sorted((self.project.root / "snapshots").glob("*.json")), snapshots_before)

    def test_engine_run_attempts_automatic_prune_before_locks(self):
        from mizu.runtime import Engine
        from support import ScriptDriver
        calls = []

        def fake(project):
            calls.append(project.root)
            return {"status": "not-due"}

        def work(ctx, *_):
            ctx.handle("finish", {"outcome": "wait", "summary": "s", "state": "n"})

        with mock.patch.object(storage, "maintain_automatic_prune", side_effect=fake):
            Engine(self.config, driver=ScriptDriver(work), stop=None).run(self.project, "reporter")
        self.assertEqual(calls, [self.project.root])
