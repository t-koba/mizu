"""M8: resume rebase plus infra-wait defer (budget/locks) without hiding faults."""
from types import SimpleNamespace
from support import Fixture, ScriptDriver
from mizu.cli import _cmd_resume
from mizu.errors import Busy, LimitExceeded, ProtocolError
from mizu.fs import read_json, write_json
from mizu.project import Project
from mizu.runtime import Engine, is_infra_wait


class ResumeHealthTests(Fixture):
    def _failures(self):
        return read_json(self.project.root / "health" / "worker.json", {}).get("consecutive_failures", 0)

    def test_infra_waits_defer_without_counting(self):
        self.assertTrue(is_infra_wait(Busy("slots busy")))
        self.assertTrue(is_infra_wait(LimitExceeded("UTC daily model-request budget exhausted")))
        self.assertFalse(is_infra_wait(ProtocolError("real fault")))

    def test_budget_exhaustion_defers_and_stays_unpaused(self):
        def fails(ctx, *args):
            raise LimitExceeded("UTC daily model-request budget exhausted")
        with self.assertRaises(LimitExceeded):
            Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        with self.assertRaises(LimitExceeded):
            Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        with self.assertRaises(LimitExceeded):
            Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        # Never counted, never auto-paused; error evidence still recorded.
        self.assertEqual(self._failures(), 0)
        self.assertFalse(self.project.control()["paused"])
        self.assertTrue(list((self.project.root / "runs").glob("*/error.json")))

    def test_busy_lock_defers_without_counting(self):
        def fails(ctx, *args):
            raise Busy("Already running: workspace")
        with self.assertRaises(Busy):
            Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        with self.assertRaises(Busy):
            Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        self.assertEqual(self._failures(), 0)
        self.assertFalse(self.project.control()["paused"])

    def test_real_faults_still_count_and_pause(self):
        def fails(*_):
            raise ProtocolError("failure")
        for _ in range(3):
            with self.assertRaises(ProtocolError):
                Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        self.assertTrue(self.project.control()["paused"])
        self.assertEqual(self._failures(), 3)

    def test_resume_resets_then_next_single_failure_does_not_repause(self):
        def fails(*_):
            raise ProtocolError("failure")
        for _ in range(3):
            with self.assertRaises(ProtocolError):
                Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        self.assertTrue(self.project.control()["paused"])
        _cmd_resume(self.config, self.project, SimpleNamespace())
        self.assertFalse(self.project.control()["paused"])
        self.assertEqual(self._failures(), 0)
        with self.assertRaises(ProtocolError):
            Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        self.assertFalse(self.project.control()["paused"])
        self.assertEqual(self._failures(), 1)

    def test_resume_keeps_last_run_evidence(self):
        write_json(self.project.root / "health" / "worker.json",
                   {"consecutive_failures": 2, "last_run": "abc", "updated_at": "t"})
        project = Project(self.config, "sample")
        reset = project.reset_health()
        self.assertEqual(reset, {"worker": 0})
        record = read_json(self.project.root / "health" / "worker.json")
        self.assertEqual(record["consecutive_failures"], 0)
        self.assertEqual(record["last_run"], "abc")
