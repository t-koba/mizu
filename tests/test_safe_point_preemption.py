"""Configured safe-point handoff over synthetic driver protocols only."""
import dataclasses
import sys
from unittest.mock import patch

from support import Fixture, ROOT
from mizu.continuations import ready
from mizu.errors import Denied
from mizu.drivers import DriverCheckpoint
from mizu.project import Project
from mizu.runtime import Engine


class SafePointPreemptionTests(Fixture):
    def configured(self, scenario, *, quantum=1, outcome_engine="claude"):
        profile = self.config.roles["worker"].profile
        profiles = dict(self.config.profiles)
        profiles[profile] = {**profiles[profile], "engine": outcome_engine,
                             "provider": "synthetic", "model": "offline",
                             "session": "persistent", "options": {}}
        engines = dict(self.config.engines)
        engines[outcome_engine] = {**engines[outcome_engine],
            "command": (sys.executable, str(ROOT / "tests/fake_engines.py"),
                        outcome_engine, scenario)}
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"],
            capabilities=tuple(c for c in roles["worker"].capabilities
                               if c not in ("exec", "experiment", "verify")))
        limits = dataclasses.replace(self.config.limits,
            safe_point_yield_quantum=quantum, missing_finish_followups=0)
        config = dataclasses.replace(self.config, profiles=profiles, roles=roles,
                                     engines=engines, limits=limits)
        return config, Project(config, "sample")

    def test_continue_yield_prepares_before_publication_and_commits_once(self):
        config, project = self.configured("seal-continue")
        with patch("mizu.runtime.has_live_demand", return_value=True) as demand:
            result = Engine(config).run(project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finish"]["outcome"], "continue")
        self.assertEqual(demand.call_count, 1)
        snapshot = project.snapshots.get()
        self.assertEqual(snapshot["id"], result["snapshot"])
        self.assertEqual(snapshot["outcome"], "continue")
        intent = ready(project.snapshots, "worker")
        self.assertIsNotNone(intent)
        self.assertEqual(intent["state"], "committed")
        self.assertEqual(intent["source_run"], result["run"])
        self.assertEqual(intent["snapshot"], snapshot["id"])
        self.assertNotEqual(intent["successor"], result["run"])

    def test_unpublished_prepared_intent_is_not_runnable(self):
        config, project = self.configured("seal-continue")
        before = project.snapshots.get()["id"]
        with patch("mizu.runtime.has_live_demand", return_value=True), \
             patch.object(project.snapshots, "publish", side_effect=OSError("synthetic publish failure")):
            with self.assertRaises(OSError):
                Engine(config).run(project, "worker")
        self.assertEqual(project.snapshots.get()["id"], before)
        intent = ready(project.snapshots, "worker")
        self.assertIsNone(intent)
        from mizu.continuations import pending
        prepared = pending(project.snapshots, "worker")
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["state"], "prepared")

    def test_failed_finish_only_seal_does_not_prepare_or_publish_handoff(self):
        config, project = self.configured("seal-failed")
        before = project.snapshots.get()["id"]
        with patch("mizu.runtime.has_live_demand", return_value=True) as demand:
            with self.assertRaises(Denied):
                Engine(config).run(project, "worker")
        self.assertEqual(demand.call_count, 1)
        self.assertEqual(project.snapshots.get()["id"], before)
        self.assertIsNone(ready(project.snapshots, "worker"))

    def test_wait_outcome_is_not_forced_into_continuation(self):
        config, project = self.configured("seal-wait")
        with patch("mizu.runtime.has_live_demand", return_value=True) as demand:
            result = Engine(config).run(project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finish"]["outcome"], "wait")
        self.assertEqual(demand.call_count, 1)
        self.assertEqual(project.snapshots.get()["outcome"], "wait")
        self.assertIsNone(ready(project.snapshots, "worker"))

    def test_quantum_requires_matching_completed_unit_boundary(self):
        config, project = self.configured("seal-wait", quantum=2)
        context = self.context("worker")
        context.config = config
        context.project = project
        context.completed_driver_units = 1
        context.driver_checkpoint = DriverCheckpoint("claude", "query", 0, 1)
        engine = Engine(config)
        with patch("mizu.runtime.has_live_demand", return_value=True) as demand, \
             patch.object(engine, "_seal_driver_checkpoint") as seal:
            self.assertIsNone(engine._maybe_safe_point_yield(context))
            demand.assert_not_called()
            seal.assert_not_called()
            context.completed_driver_units = 2
            seal.return_value = {"outcome": "wait"}
            self.assertEqual(engine._maybe_safe_point_yield(context), {"outcome": "wait"})
        demand.assert_called_once()
        seal.assert_called_once_with(context, context.driver_checkpoint, profile=context.role.profile)

    def test_default_disabled_and_drain_skip_demand_probe(self):
        config, project = self.configured("seal-wait", quantum=0)
        context = self.context("worker")
        context.config = config
        with patch("mizu.runtime.has_live_demand", return_value=True) as demand:
            self.assertIsNone(Engine(config)._maybe_safe_point_yield(context))
            demand.assert_not_called()

        config, project = self.configured("seal-wait", quantum=1)
        context = self.context("worker")
        context.config = config
        context.driver_checkpoint = object()
        context.completed_driver_units = 1
        project.set_control(armed=True, paused=False, draining=True)
        context.project = project
        with patch("mizu.runtime.has_live_demand", return_value=True) as demand:
            self.assertIsNone(Engine(config)._maybe_safe_point_yield(context))
            demand.assert_not_called()
