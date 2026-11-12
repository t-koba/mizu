"""Resident daemon reuses a negative readiness result while the state tree is unchanged."""
import dataclasses
import os
from contextlib import nullcontext
from unittest.mock import Mock, patch

from support import Fixture
from mizu import readiness_witness
from mizu.config import ConfigError, load
from mizu.fs import write_json
from mizu.project import Project
from mizu.runtime import daemon


class ReadinessRecheckTests(Fixture):
    def setUp(self):
        super().setUp()
        self.clock = [1000.0]

    def with_interval(self, seconds):
        return dataclasses.replace(
            self.config, limits=dataclasses.replace(
                self.config.limits, readiness_recheck_seconds=seconds))

    def polls(self, configs, *, between=(), ready=None, settled=True):
        """Run one daemon iteration per config; ``between[i]`` runs after iteration i."""
        stop = Mock()
        stop.is_set.side_effect = [False] * len(configs) + [True]
        calls = []

        def waited(_delay):
            index = len(calls_waited)
            calls_waited.append(index)
            if index < len(between) and between[index] is not None:
                between[index]()

        calls_waited = []
        stop.wait.side_effect = waited

        def construct(config, name):
            project = Project(config, name)
            project.insights.ingest_editor = Mock()
            return project

        def evaluate(*_args, **_kwargs):
            calls.append(len(calls_waited))
            if ready is not None:
                return ready(len(calls))
            return False

        with (patch("mizu.runtime.threading.Event", return_value=stop),
              patch("mizu.runtime.signal.signal"),
              patch("mizu.runtime.lock", return_value=nullcontext()),
              patch("mizu.runtime.load", side_effect=configs),
              patch("mizu.runtime.Project", side_effect=construct),
              patch("mizu.runtime.should_run", side_effect=evaluate),
              patch("mizu.runtime.vcs_poll_enabled", return_value=False),
              patch("mizu.runtime.witness_settled", return_value=settled),
              patch("mizu.runtime.time.monotonic", side_effect=lambda: self.clock[0]),
              patch("mizu.probes.service_due", return_value=None),
              patch("mizu.probes.next_deadline", return_value=None),
              patch("mizu.runtime.Engine") as engine,
              patch("builtins.print")):
            engine.return_value.run.return_value = {"status": "done"}
            daemon(self.config, "sample", "worker")
        return calls, engine

    def advance(self, seconds):
        return lambda: self.clock.__setitem__(0, self.clock[0] + seconds)

    def test_default_evaluates_on_every_tick(self):
        calls, _ = self.polls([self.config] * 4)
        self.assertEqual(len(calls), 4)

    def test_unchanged_tree_is_evaluated_once_per_interval(self):
        config = self.with_interval(60)
        calls, _ = self.polls([config] * 5)
        self.assertEqual(len(calls), 1)
        calls, _ = self.polls(
            [config] * 5, between=[self.advance(30), self.advance(31), None, None])
        self.assertEqual(calls, [0, 2])

    def test_state_writes_trigger_the_next_evaluation(self):
        root = self.project.root
        writes = {
            "inbox": lambda: write_json(root / "inbox" / "x.json", {"a": 1}),
            "decisions": lambda: write_json(root / "decisions" / "x.json", {"a": 1}),
            "work item": lambda: (root / "work-items" / "wi_x").mkdir(parents=True),
            "check job": lambda: (root / "check-jobs" / "job").mkdir(parents=True),
            "control": lambda: write_json(root / "control.json", {
                "armed": True, "paused": False, "wake_generation": "again", "draining": False}),
            "nested": lambda: write_json(root / "evidence" / "ci-waits" / "x.json", {"a": 1}),
        }
        for label, write in writes.items():
            with self.subTest(label):
                (root / "evidence" / "ci-waits").mkdir(parents=True, exist_ok=True)
                calls, _ = self.polls([self.with_interval(3600)] * 3, between=[None, write, None])
                self.assertEqual(calls, [0, 2])

    def test_skipped_trees_do_not_trigger_evaluation(self):
        runs = self.project.root / "runs"
        runs.mkdir(exist_ok=True)
        calls, _ = self.polls([self.with_interval(3600)] * 3,
                              between=[None, lambda: write_json(runs / "r.json", {"a": 1})])
        self.assertEqual(calls, [0])

    def test_positive_result_and_errors_are_never_reused(self):
        config = self.with_interval(3600)
        calls, engine = self.polls([config] * 3, ready=lambda n: True)
        self.assertEqual(len(calls), 3)

        def failing(_n):
            raise ValueError("unreadable")
        calls, _ = self.polls([config] * 3, ready=failing)
        self.assertEqual(len(calls), 3)

    def test_negative_result_after_positive_is_cached_again(self):
        calls, _ = self.polls([self.with_interval(3600)] * 4, ready=lambda n: n == 1)
        # The positive first result clears the state; the second result is
        # negative and is then reused.
        self.assertEqual(calls, [0, 1])

    def test_a_write_made_by_the_evaluation_costs_exactly_one_more_evaluation(self):
        root = self.project.root

        def ready(n):
            if n == 1:
                write_json(root / "inbox" / "own.json", {"a": 1})
            return False
        calls, _ = self.polls([self.with_interval(3600)] * 4, ready=ready)
        self.assertEqual(calls, [0, 1])

    def test_config_change_clears_the_result(self):
        config = self.with_interval(3600)
        changed = dataclasses.replace(
            config, limits=dataclasses.replace(config.limits, retention_days=config.limits.retention_days + 1))
        calls, _ = self.polls([config, config, changed, changed])
        self.assertEqual(calls, [0, 2])

    def test_unsettled_tree_is_not_reused(self):
        calls, _ = self.polls([self.with_interval(3600)] * 3, settled=False)
        self.assertEqual(len(calls), 3)

    def test_pause_clears_the_result(self):
        def toggle(paused):
            return lambda: self.project.set_control(paused=paused, reason="test")
        calls, _ = self.polls([self.with_interval(3600)] * 4,
                              between=[None, toggle(True), toggle(False)])
        self.assertEqual(calls, [0, 3])

    def test_witness_settledness_follows_timestamp_granularity(self):
        taken = 10 ** 18
        self.assertTrue(readiness_witness.settled(taken - 10 ** 9, taken))
        self.assertFalse(readiness_witness.settled(taken - 1000, taken))

    def test_witness_sees_replaced_files_and_new_directories(self):
        root = self.project.root
        before = readiness_witness.witness(root)[0]
        self.assertEqual(before, readiness_witness.witness(root)[0])
        write_json(root / "inbox" / "w.json", {"a": 1})
        after = readiness_witness.witness(root)[0]
        self.assertNotEqual(before, after)
        (root / "runs" / "r").mkdir(parents=True)
        after = readiness_witness.witness(root)[0]
        (root / "runs" / "s").mkdir()
        write_json(root / "runs" / "s" / "f.json", {})
        self.assertEqual(after, readiness_witness.witness(root)[0])

    def test_config_bounds_fail_closed(self):
        self.assertEqual(self.config.limits.readiness_recheck_seconds, 0)
        text = self.file.read_text()
        self.assertIn("readiness_recheck_seconds = 0", text)
        for value, valid in ((0, True), (60, True), (86400, True), (-1, False),
                             (86401, False), ("true", False), ("1.5", False)):
            with self.subTest(value=value):
                self.file.write_text(text.replace(
                    "readiness_recheck_seconds = 0", f"readiness_recheck_seconds = {value}", 1))
                if valid:
                    self.assertEqual(load(self.file).limits.readiness_recheck_seconds, value)
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)
