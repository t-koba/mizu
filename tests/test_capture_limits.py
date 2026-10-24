"""Capture limits name their cause and reach the writer that owns the workspace.

Offline only: no adapter, no network, no publication.
"""
import dataclasses
import json
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu import snapshot as snapshot_module
from mizu.errors import CaptureLimitExceeded, Denied, LimitExceeded
from mizu.fs import read_json, write_json
from mizu.project import Project
from mizu.runtime import Engine, _record_health_failure, prompt_for


class CaptureLimitTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, file_bytes=64))
        self.project = Project(self.config, "sample")

    def test_capture_limit_names_the_path_and_the_size(self):
        ctx = self.context()
        (ctx.workspace / "big.profraw").write_bytes(b"x" * 200)
        with self.assertRaises(LimitExceeded) as refused:
            self.project.snapshots.capture_files(ctx.workspace)
        message = str(refused.exception)
        self.assertIn("big.profraw", message)
        self.assertIn("200", message)
        self.assertIn("64", message)
        # The cheap precondition names the same cause.
        with self.assertRaises(LimitExceeded) as refused:
            self.project.snapshots.check_bounds(ctx.workspace)
        self.assertIn("big.profraw", str(refused.exception))

    def test_total_and_count_bounds_name_their_numbers(self):
        limits = dataclasses.replace(self.config.limits, snapshot_bytes=1)
        project = Project(dataclasses.replace(self.config, limits=limits), "sample")
        with self.assertRaises(LimitExceeded) as refused:
            project.snapshots.capture_files(self.project.workspace)
        self.assertIn("Snapshot byte limit exceeded", str(refused.exception))
        (self.project.workspace / "second.txt").write_bytes(b"two")
        limits = dataclasses.replace(self.config.limits, snapshot_files=1)
        project = Project(dataclasses.replace(self.config, limits=limits), "sample")
        with self.assertRaises(LimitExceeded) as refused:
            project.snapshots.capture_files(self.project.workspace)
        message = str(refused.exception)
        self.assertIn("Snapshot file count exceeded", message)
        self.assertIn("2 files", message)

    def test_finish_is_refused_with_the_offending_path_while_the_writer_can_act(self):
        ctx = self.context()
        (ctx.workspace / "big.profraw").write_bytes(b"x" * 200)
        with self.assertRaises(Denied) as refused:
            ctx._op_finish({"summary": "Remove the stray profile", "state": "cleaning",
                            "outcome": "continue"})
        self.assertIn("big.profraw", str(refused.exception))
        self.assertIsNone(ctx.finished)
        # The writer removes the file and finishes again within the same run.
        (ctx.workspace / "big.profraw").unlink()
        self.assertEqual(ctx._op_finish({"summary": "Removed the stray profile",
                                         "state": "clean", "outcome": "continue"}),
                         {"sealed": True})

    def test_non_object_health_json_does_not_interrupt_terminal_failure_recording(self):
        for malformed_health in ([], None):
            with self.subTest(health=malformed_health):
                write_json(self.project.root / "health" / "worker.json", malformed_health)
                failure = Denied("terminal failure")
                # Exercise the runtime's terminal-failure helper directly: a
                # valid non-object JSON record must not escape as AttributeError.
                _record_health_failure(self.project, "worker", "run-malformed",
                                       failure, 3)
                health = read_json(self.project.root / "health" / "worker.json")
                self.assertEqual(health["consecutive_failures"], 1)
                self.assertEqual(health["consecutive_missing_finish"], 0)
                self.assertEqual(health["last_run"], "run-malformed")
                self.assertEqual(health["error"], str(failure))
                self.assertFalse(self.project.control()["paused"])

    def test_cause_reaches_health_the_pause_reason_and_the_next_unit(self):
        ctx = self.context()
        (ctx.workspace / "big.profraw").write_bytes(b"x" * 200)
        with self.assertRaises(Denied):
            ctx._op_finish({"summary": "work", "state": "cleaning", "outcome": "continue"})
        cause = ctx.capture_limit
        self.assertIn("big.profraw", cause)
        _record_health_failure(self.project, "worker", "run-1", Denied("boom"), 1, cause=cause)
        health = read_json(self.project.root / "health" / "worker.json")
        self.assertEqual(health["capture_limit"], cause)
        self.assertIn("big.profraw", self.project.control()["reason"])
        # A later unit reads the cause from its context instead of repeating it.
        later = self.context()
        prompt = json.loads(prompt_for(later))
        self.assertEqual(prompt["capture_limit"], cause)

    def test_publication_capture_failure_reaches_health_and_the_next_unit(self):
        # The authoritative capture at publication fails, not the finish
        # precondition: the cause must still reach health and the next unit.
        cause = "Snapshot file exceeds the byte limit: big.profraw (200 > 64 bytes)"

        def work(ctx, *_):
            ctx.handle("finish", {"outcome": "continue", "summary": "work", "state": "done"})

        with patch.object(snapshot_module.Snapshots, "capture_files",
                          side_effect=CaptureLimitExceeded(cause)):
            with self.assertRaises(CaptureLimitExceeded):
                Engine(self.config, driver=ScriptDriver(work)).run(self.project, "worker")
        error = read_json(next((self.project.root / "runs").glob("*/error.json")))
        self.assertEqual(error["capture_limit"], cause)
        health = read_json(self.project.root / "health" / "worker.json")
        self.assertEqual(health["capture_limit"], cause)
        later = self.context()
        self.assertEqual(json.loads(prompt_for(later))["capture_limit"], cause)

    def test_the_health_writer_clears_a_resolved_cause_and_keeps_a_live_one(self):
        ctx = self.context()
        (ctx.workspace / "big.profraw").write_bytes(b"x" * 200)
        with self.assertRaises(Denied):
            ctx._op_finish({"summary": "work", "state": "cleaning", "outcome": "continue"})
        cause = ctx.capture_limit
        _record_health_failure(self.project, "worker", "run-1", Denied("boom"), 3, cause=cause)
        # The writer removes the file: the workspace is representable again.
        (ctx.workspace / "big.profraw").unlink()
        ctx._op_finish({"summary": "work", "state": "clean", "outcome": "continue"})
        self.assertIsNone(ctx.capture_limit)
        self.assertTrue(ctx.capture_resolved)
        _record_health_failure(self.project, "worker", "run-2", Denied("boom"), 3,
                               resolved=ctx.capture_resolved)
        health = read_json(self.project.root / "health" / "worker.json")
        self.assertNotIn("capture_limit", health)
        later = self.context()
        self.assertIsNone(json.loads(prompt_for(later))["capture_limit"])

    def test_the_engine_clears_a_resolved_cause_and_keeps_a_live_one(self):
        # The engine wiring, not a hand-fed helper call, decides the cause.

        def unrepresentable(ctx, *_):
            (ctx.workspace / "big.profraw").write_bytes(b"x" * 200)
            ctx.handle("finish", {"outcome": "continue", "summary": "work",
                                  "state": "cleaning"})

        with self.assertRaises(Denied):
            Engine(self.config, driver=ScriptDriver(unrepresentable)).run(self.project, "worker")
        health = read_json(self.project.root / "health" / "worker.json")
        self.assertIn("big.profraw", health["capture_limit"])

        # A unit that fails before any capture keeps the cause that still applies.
        def unrelated(ctx, *_):
            raise Denied("provider down")

        with self.assertRaises(Denied):
            Engine(self.config, driver=ScriptDriver(unrelated)).run(self.project, "worker")
        health = read_json(self.project.root / "health" / "worker.json")
        self.assertIn("big.profraw", health["capture_limit"])

        # A successful unit clears it: the workspace is representable again.
        (self.project.workspace / "big.profraw").unlink()

        def succeeding(ctx, *_):
            ctx.handle("finish", {"outcome": "continue", "summary": "work", "state": "clean"})

        Engine(self.config, driver=ScriptDriver(succeeding)).run(self.project, "worker")
        health = read_json(self.project.root / "health" / "worker.json")
        self.assertNotIn("capture_limit", health)
        self.assertIsNone(json.loads(prompt_for(self.context()))["capture_limit"])

    def test_a_live_cause_survives_an_unrelated_failure(self):
        ctx = self.context()
        (ctx.workspace / "big.profraw").write_bytes(b"x" * 200)
        with self.assertRaises(Denied):
            ctx._op_finish({"summary": "work", "state": "cleaning", "outcome": "continue"})
        cause = ctx.capture_limit
        _record_health_failure(self.project, "worker", "run-1", Denied("boom"), 3, cause=cause)
        # A later unrelated failure keeps the cause that still applies.
        _record_health_failure(self.project, "worker", "run-2", Denied("provider down"), 3)
        health = read_json(self.project.root / "health" / "worker.json")
        self.assertEqual(health["capture_limit"], cause)
