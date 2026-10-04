"""M8: resume rebase plus infra-wait defer (budget/locks) without hiding faults.

Deferral is narrow: Busy locks/slots and InfraExceeded daily-budget /
disk-reserve guards defer; plain LimitExceeded bound faults (engine
deadlines, event-stream/RPC bounds, per-run tool/request bounds) still
count toward the brake. The Pi transport converts an in-run host-side
daily-budget refusal into ProtocolError/ModelFailure; Context.admission_wait
(set by type, never message text) maps that back to a deferral.
"""
import dataclasses
import sys
from types import SimpleNamespace
from support import Fixture, ScriptDriver, ROOT
from mizu.cli import _cmd_resume
from mizu.errors import Busy, InfraExceeded, LimitExceeded, ProtocolError
from mizu.fs import read_json, write_json
from mizu.project import Project
from mizu.runtime import Engine, is_deferred, is_infra_wait


class ResumeHealthTests(Fixture):
    def _failures(self, role="worker"):
        return read_json(self.project.root / "health" / f"{role}.json", {}).get("consecutive_failures", 0)

    def test_infra_waits_defer_without_counting(self):
        self.assertTrue(is_infra_wait(Busy("slots busy")))
        self.assertTrue(is_infra_wait(InfraExceeded("UTC daily model-request budget exhausted")))
        # Plain bound faults are not infra waits, even though they share the
        # LimitExceeded base type.
        self.assertFalse(is_infra_wait(LimitExceeded("Engine deadline exceeded")))
        self.assertFalse(is_infra_wait(LimitExceeded("Per-run tool limit exceeded")))
        self.assertFalse(is_infra_wait(ProtocolError("real fault")))
        self.assertTrue(is_deferred(InfraExceeded("x")))
        self.assertFalse(is_deferred(LimitExceeded("Engine deadline exceeded")))
        self.assertFalse(is_deferred(ProtocolError("real fault")))

    def test_budget_exhaustion_defers_and_stays_unpaused(self):
        def fails(ctx, *args):
            raise InfraExceeded("UTC daily model-request budget exhausted")
        with self.assertRaises(InfraExceeded):
            Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        with self.assertRaises(InfraExceeded):
            Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        with self.assertRaises(InfraExceeded):
            Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        # Never counted, never auto-paused; error evidence still recorded.
        self.assertEqual(self._failures(), 0)
        self.assertFalse(self.project.control()["paused"])
        self.assertTrue(list((self.project.root / "runs").glob("*/error.json")))

    def test_non_budget_limit_still_counts_and_pauses(self):
        def fails(ctx, *args):
            raise LimitExceeded("Engine deadline exceeded")
        for _ in range(3):
            with self.assertRaises(LimitExceeded):
                Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        self.assertTrue(self.project.control()["paused"])
        self.assertEqual(self._failures(), 3)

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

    def test_pi_channel_budget_exhaustion_defers(self):
        from mizu.pi import PiDriver
        cfg = dataclasses.replace(
            self.config,
            engines={**self.config.engines,
                      "pi": {**self.config.engines["pi"],
                             "command": (sys.executable, str(ROOT / "tests/fake_pi.py"))}},
            limits=dataclasses.replace(self.config.limits, daily_requests=1),
        )
        pi = PiDriver(cfg)

        class PiBudgetDriver:
            requires_sandbox = False

            def execute(self, context, prompt, *, profile=None):
                from mizu.budget import Budget
                try:
                    Budget(context.config.data / "budget",
                           context.config.limits.daily_requests,
                           context.config.limits.retention_days,
                           context.config.limits.shared_daily_requests).take(
                               "fill-to-exhaust", project=context.project.name)
                except Exception:
                    pass
                return pi.execute(context, prompt, profile=profile)

        for _ in range(3):
            for path in (cfg.data / "budget").glob("????-??-??.json"):
                try:
                    path.unlink()
                except OSError:
                    pass
            with self.assertRaises(ProtocolError):
                Engine(cfg, driver=PiBudgetDriver()).run(self.project, "reporter")
        # The Pi transport surfaces the host-side budget refusal as
        # ProtocolError, but admission_wait marks it as the same infra wait:
        # never counted, never auto-paused, evidence still recorded.
        self.assertEqual(self._failures("reporter"), 0)
        self.assertFalse(self.project.control()["paused"])
        self.assertTrue(list((self.project.root / "runs").glob("*/error.json")))

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
