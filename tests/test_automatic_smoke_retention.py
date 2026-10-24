"""Offline automatic shared-smoke cadence and safe-boundary regressions."""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest import mock

from support import Fixture, ScriptDriver
from mizu import smoke_retention as retention
from mizu.config import load
from mizu.errors import ConfigError, Denied, LimitExceeded
from mizu.fs import held, write_json
from mizu.insights import Insights
from mizu.smoke_refs import shared_lock

CLOCK = 2_000_000_000
DAY = 86400
SETTING = "automatic_smoke_retention_interval_seconds"


class AutomaticSmokeRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.data.mkdir()
        self.project = SimpleNamespace(config=SimpleNamespace(
            data=self.data, limits=SimpleNamespace(**{SETTING: DAY})))
        self.state = self.data / "maintenance/automatic-smoke-retention.json"
        self.moment = dt.datetime.fromtimestamp(CLOCK, dt.timezone.utc)
        self.clock = mock.patch.object(retention.time, "time", return_value=CLOCK)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        # Replace only this module's clock, not the shared datetime module.
        clock = mock.Mock(wraps=dt.datetime)
        clock.now.return_value = self.moment
        self.date = mock.patch.object(retention, "dt", SimpleNamespace(
            datetime=clock, timezone=dt.timezone, timedelta=dt.timedelta))
        self.date.start()
        self.addCleanup(self.date.stop)

    def pair(self, key="a", age=32):
        key *= 32
        result = retention.publish(self.data, key, {
            "status": "pass", "time": (self.moment - dt.timedelta(days=age)).isoformat()}, None)
        return Path(result["pair"])

    def maintain(self):
        return retention.maintain_automatic_smoke_retention(self.project)

    def completed(self, checked_at=CLOCK):
        return {"checked_at": checked_at, "status": "completed", "days": 31,
                "retired_total": 0, "recovered_total": 0}

    def test_due_expires_only_old_unpinned_pairs_and_preserves_artifacts(self):
        free, pinned, recent = self.pair(), self.pair("b"), self.pair("c", age=31)
        root = self.data / "projects/owner"
        insights = Insights(root)
        item = insights.submit(source="worker", title="Live", body="Evidence", base_snapshot=None)
        insights.attach_evidence(item["id"], source="worker", text="Synthetic",
                                 expected_rev=1, smoke_refs=["b" * 32])
        for name in ("snapshots/keep.json", "objects/keep"):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"synthetic immutable evidence")
        result = self.maintain()
        self.assertEqual(result, dict(self.completed(), retired_total=1))
        self.assertFalse(free.exists())
        self.assertTrue(pinned.is_dir())
        self.assertTrue(recent.is_dir())
        self.assertEqual(json.loads(self.state.read_bytes()), result)
        self.assertLess(self.state.stat().st_size, 4096)
        for name in ("snapshots/keep.json", "objects/keep"):
            self.assertEqual((root / name).read_bytes(), b"synthetic immutable evidence")
        self.assertFalse((root / "control.json").exists())  # No pause/quiescence required.

    def test_shared_cadence_and_exact_due_boundary(self):
        self.assertEqual(self.maintain()["status"], "completed")
        another = SimpleNamespace(config=SimpleNamespace(data=self.data, limits=SimpleNamespace(
            automatic_smoke_retention_interval_seconds=DAY, automatic_prune_interval_seconds=0)))
        with mock.patch.object(retention, "_run_locked", side_effect=AssertionError("not due")):
            self.assertEqual(retention.maintain_automatic_smoke_retention(another), {"status": "not-due"})
            with mock.patch.object(retention.time, "time", return_value=CLOCK + DAY - 1):
                self.assertEqual(self.maintain(), {"status": "not-due"})
        with mock.patch.object(retention.time, "time", return_value=CLOCK + DAY):
            self.assertEqual(self.maintain()["status"], "completed")

    def test_disabled_touches_nothing_and_manual_apply_still_works(self):
        old = self.pair()
        setattr(self.project.config.limits, SETTING, 0)
        with mock.patch.object(retention, "shared_lock", side_effect=AssertionError), \
             mock.patch.object(retention, "_automatic_smoke_due", side_effect=AssertionError):
            self.assertEqual(self.maintain(), {"status": "disabled"})
        self.assertFalse(self.state.exists())
        self.assertTrue(old.is_dir())
        self.assertEqual(retention.run(self.data, moment=self.moment)["candidates_total"], 1)
        self.assertTrue(old.is_dir())
        self.assertEqual(retention.run(self.data, apply=True, moment=self.moment)["retired_total"], 1)
        self.assertFalse(old.exists())

    def test_invalid_runtime_intervals_are_unavailable_without_io(self):
        for value in (True, -1, 2678401, "86400", 1.0, None):
            with self.subTest(value=value), mock.patch.object(retention, "directory") as check:
                setattr(self.project.config.limits, SETTING, value)
                self.assertEqual(self.maintain(), {"status": "unavailable"})
                check.assert_not_called()

    def test_zero_day_policy_keeps_pairs(self):
        old = self.pair()
        retention.set_days(self.data, 0)
        result = self.maintain()
        self.assertEqual(result, dict(self.completed(), days=0))
        self.assertTrue(old.is_dir())
        self.assertEqual(retention.policy(self.data), 0)

    def test_busy_never_consumes_due_cadence(self):
        old = self.pair()
        with shared_lock(self.data):
            self.assertEqual(self.maintain(), {"status": "busy"})
        self.assertFalse(self.state.exists())
        self.assertTrue(old.is_dir())
        self.assertEqual(self.maintain()["retired_total"], 1)

    def test_two_projects_contend_for_one_shared_sweep(self):
        entered, release = threading.Event(), threading.Event()
        original = retention._run_locked
        def sweep(*args, **kwargs):
            entered.set()
            if not release.wait(30):
                raise AssertionError("test failed to release sweep")
            return original(*args, **kwargs)
        with mock.patch.object(retention, "_run_locked", side_effect=sweep) as run:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.maintain)
                try:
                    self.assertTrue(entered.wait(30))
                    self.assertEqual(self.maintain(), {"status": "busy"})
                    self.assertFalse(self.state.exists())
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=30)["status"], "completed")
            self.assertEqual(self.maintain(), {"status": "not-due"})
            self.assertEqual(run.call_count, 1)

    def test_due_is_rechecked_under_the_shared_lock(self):
        @contextlib.contextmanager
        def acquire(data):
            with shared_lock(data):
                # Another project finished between the fast check and acquire.
                write_json(self.state, self.completed())
                yield
        with mock.patch.object(retention, "shared_lock", side_effect=acquire), \
             mock.patch.object(retention, "_run_locked") as sweep:
            self.assertEqual(self.maintain(), {"status": "not-due"})
            sweep.assert_not_called()
        self.assertEqual(json.loads(self.state.read_bytes()), self.completed())

    def test_malformed_state_refuses_before_any_expiry(self):
        old = self.pair()
        self.state.parent.mkdir()
        invalid = [b"{", b"[]", b"x" * 4097,
                   b'{"checked_at":0,"checked_at":0,"status":"refused","error":"Denied"}']
        invalid.extend(json.dumps(value).encode() for value in (
            {}, {"checked_at": CLOCK}, dict(self.completed(), checked_at=True),
            dict(self.completed(), checked_at=CLOCK + 1), dict(self.completed(), checked_at=-1),
            dict(self.completed(), status="unknown"), dict(self.completed(), unexpected=1),
            dict(self.completed(), days=31.0), dict(self.completed(), retired_total=5001)))
        for raw in invalid:
            with self.subTest(raw=raw[:100]), \
                 mock.patch.object(retention, "_run_locked", side_effect=AssertionError):
                self.state.write_bytes(raw)
                self.assertEqual(self.maintain(), {"status": "unavailable"})
                self.assertEqual(self.state.read_bytes(), raw)
                self.assertTrue(old.is_dir())

    def test_incomplete_refs_and_scan_bounds_refuse_before_deletion(self):
        old = self.pair()
        bad = self.data / "projects/owner/inbox/bad.json"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"{")
        result = self.maintain()
        self.assertEqual(result, {"checked_at": CLOCK, "status": "refused", "error": "Denied"})
        self.assertTrue(old.is_dir())
        self.assertEqual(self.maintain(), {"status": "not-due"})
        bad.unlink()
        self.state.unlink()
        with mock.patch.object(retention, "MAX_PROJECTS", 0):
            self.assertEqual(self.maintain()["error"], "LimitExceeded")
        self.assertTrue(old.is_dir())

    def test_failures_are_bounded_best_effort_but_cancellation_propagates(self):
        old = self.pair()
        for exc in (Denied("private details"), LimitExceeded("bound"), OSError("I/O"), RuntimeError("bug")):
            with self.subTest(exc=type(exc).__name__), \
                 mock.patch.object(retention, "_run_locked", side_effect=exc):
                self.state.unlink(missing_ok=True)
                result = self.maintain()
                self.assertEqual(result, {"checked_at": CLOCK, "status": "refused", "error": type(exc).__name__})
                self.assertNotIn("private", self.state.read_text())
                self.assertTrue(old.is_dir())
        self.state.unlink()
        with mock.patch.object(retention, "safe_write", side_effect=OSError("unwritable")):
            self.assertEqual(self.maintain(), {"status": "unavailable"})
        self.assertFalse(self.state.exists())
        with mock.patch.object(retention, "_run_locked", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.maintain()

    def symlink(self, source, target, *, directory=False):
        try:
            source.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")

    def test_linked_data_and_directories_are_refused_before_outside_mutation(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        marker = outside / "marker"
        marker.write_bytes(b"untouched")
        alias = Path(self.temp.name) / "alias"
        self.symlink(alias, outside, directory=True)
        self.project.config.data = alias
        self.assertEqual(self.maintain(), {"status": "unavailable"})
        self.project.config.data = self.data
        for name in ("maintenance", "locks"):
            with self.subTest(name=name):
                link = self.data / name
                self.symlink(link, outside, directory=True)
                self.assertEqual(self.maintain(), {"status": "unavailable"})
                link.unlink()
        self.assertEqual({p.name for p in outside.iterdir()}, {"marker"})
        self.assertEqual(marker.read_bytes(), b"untouched")

    def test_linked_and_special_cadence_and_lock_files_are_refused(self):
        old = self.pair()
        target = Path(self.temp.name) / "target.json"
        write_json(target, self.completed(0))
        original = target.read_bytes()
        paths = [self.state, self.data / "locks/smoke-retention.lock"]
        for path in paths:
            path.parent.mkdir(exist_ok=True)
            path.unlink(missing_ok=True)
            for kind in ("symlink", "hardlink", "directory", "fifo"):
                if kind == "fifo" and not hasattr(os, "mkfifo"):
                    continue
                with self.subTest(path=path.name, kind=kind):
                    if kind == "symlink":
                        self.symlink(path, target)
                    elif kind == "hardlink":
                        os.link(target, path)
                    elif kind == "directory":
                        path.mkdir()
                    else:
                        os.mkfifo(path)
                    try:
                        self.assertEqual(self.maintain(), {"status": "unavailable"})
                        self.assertTrue(old.is_dir())
                        self.assertEqual(target.read_bytes(), original)
                    finally:
                        path.rmdir() if kind == "directory" else path.unlink()

    def test_state_swap_is_not_followed_by_the_safe_writer(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        target = outside / self.state.name
        write_json(target, self.completed(0))
        before = target.read_bytes()
        original = retention._run_locked
        def swap(*args, **kwargs):
            result = original(*args, **kwargs)
            self.state.parent.rmdir()
            self.symlink(self.state.parent, outside, directory=True)
            return result
        with mock.patch.object(retention, "_run_locked", side_effect=swap):
            self.assertEqual(self.maintain(), {"status": "unavailable"})
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual({p.name for p in outside.iterdir()}, {self.state.name})


class AutomaticSmokeConfigurationAndRuntimeTests(Fixture):
    def test_config_default_missing_and_bounds(self):
        original = self.file.read_text()
        setting = SETTING + " = 86400"
        self.assertIn(setting, original)
        self.assertEqual(getattr(load(self.file).limits, SETTING), DAY)
        self.file.write_text(original.replace(setting, "# omitted"))
        self.assertEqual(getattr(load(self.file).limits, SETTING), DAY)
        for value in ("0", "2678400", "-1", "2678401", "true", '"86400"', "86400.0"):
            with self.subTest(value=value):
                self.file.write_text(original.replace(setting, SETTING + " = " + value))
                if value in ("0", "2678400"):
                    self.assertEqual(getattr(load(self.file).limits, SETTING), int(value))
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)

    def test_runtime_hook_runs_once_before_project_locks_and_refusal_does_not_fail_work(self):
        from mizu.runtime import Engine
        calls = []
        original_admit = Engine._admit
        def admit(*args, **kwargs):
            self.assertEqual(calls, [self.project])
            return original_admit(*args, **kwargs)
        def maintain(project):
            calls.append(project)
            for path in (project.root / "locks").glob("*.lock"):
                self.assertFalse(held(path), path.name)
            return {"status": "unavailable"}
        def work(ctx, *_):
            ctx.handle("finish", {"outcome": "wait", "summary": "s", "state": "n"})
        with mock.patch.object(retention, "maintain_automatic_smoke_retention", side_effect=maintain), \
             mock.patch.object(Engine, "_admit", side_effect=admit, autospec=True) as admission:
            result = Engine(self.config, driver=ScriptDriver(work), stop=None).run(self.project, "reporter")
        self.assertEqual(calls, [self.project])
        admission.assert_called_once()
        self.assertEqual(result["finish"]["summary"], "s")

    def test_real_unavailable_maintenance_does_not_fail_runtime(self):
        from mizu.runtime import Engine
        state = self.config.data / "maintenance/automatic-smoke-retention.json"
        write_json(state, {"checked_at": "broken"})
        def work(ctx, *_):
            ctx.handle("finish", {"outcome": "wait", "summary": "worked", "state": "n"})
        result = Engine(self.config, driver=ScriptDriver(work), stop=None).run(self.project, "reporter")
        self.assertEqual(result["finish"]["summary"], "worked")
        self.assertEqual(json.loads(state.read_bytes()), {"checked_at": "broken"})
