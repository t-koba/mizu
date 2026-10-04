import dataclasses
import json
import os
import time

from support import Fixture, ScriptDriver
from mizu.engine_config import effective, session_record, save_session
from mizu.fs import read_json
from mizu.project import Project
from mizu.runtime import Engine


class Capture(ScriptDriver):
    def __init__(self):
        super().__init__()
        self.prompts = []

    def execute(self, context, prompt, *, profile=None):
        self.prompts.append(prompt)
        return super().execute(context, prompt, profile=profile)


def save_worker_session(fixture, usage):
    ctx = fixture.context("worker")
    settings = effective(fixture.config, ctx.role, ctx.role.profile)
    path, _ = session_record(ctx, ctx.role.profile, settings)
    save_session(path, "test-session", usage)
    return path


def projection(fixture, run_id):
    return read_json(fixture.project.root / "runs" / run_id / "prompt_projection.json")


class SessionDeltaTests(Fixture):
    def test_new_persistent_session_receives_full_prompt(self):
        capture = Capture()
        result = Engine(self.config, driver=capture).run(self.project, "worker")
        decoded = json.loads(capture.prompts[0])
        self.assertIn("goal", decoded)
        self.assertIn("recent_snapshots", decoded)
        record = projection(self, result["run"])
        self.assertEqual(record["prompt_mode"], "full")
        self.assertTrue(record["session_key"])

    def test_resumed_session_receives_bounded_delta(self):
        first = Capture()
        first_result = Engine(self.config, driver=first).run(self.project, "worker")
        save_worker_session(self, {"tokens": 0})
        second = Capture()
        result = Engine(self.config, driver=second).run(self.project, "worker")
        decoded = json.loads(second.prompts[0])
        self.assertNotIn("goal", decoded)
        self.assertNotIn("recent_snapshots", decoded)
        self.assertIn("goal_digest", decoded)
        self.assertIn("snapshot_delta", decoded)
        self.assertLess(len(second.prompts[0]), len(first.prompts[0]))
        record = projection(self, result["run"])
        self.assertEqual(record["prompt_mode"], "delta")
        self.assertEqual(record["session_key"], projection(self, first_result["run"])["session_key"])
        self.assertEqual(record["recent_snapshots"], 0)

    def test_age_bound_rotates_to_a_fresh_full_prompt(self):
        config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, session_max_age_seconds=1))
        project = Project(config, "sample")
        path = save_worker_session(self, {"tokens": 0})
        old = time.time() - 10
        os.utime(path, (old, old))
        capture = Capture()
        result = Engine(config, driver=capture).run(project, "worker")
        decoded = json.loads(capture.prompts[0])
        self.assertIn("goal", decoded)
        record = projection(self, result["run"])
        self.assertEqual(record["prompt_mode"], "full")
        self.assertIn("age", record["rotation"])
        rotation = read_json(project.root / "runs" / result["run"] / "rotation.json")
        self.assertTrue(rotation["rotated"])
        self.assertIn("age", rotation["reason"])
        self.assertFalse(path.exists())

    def test_token_bound_rotates_to_a_fresh_full_prompt(self):
        config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, session_max_tokens=10))
        project = Project(config, "sample")
        save_worker_session(self, {"tokens": 11})
        capture = Capture()
        result = Engine(config, driver=capture).run(project, "worker")
        decoded = json.loads(capture.prompts[0])
        self.assertIn("goal", decoded)
        record = projection(self, result["run"])
        self.assertEqual(record["prompt_mode"], "full")
        self.assertIn("tokens", record["rotation"])

    def test_cost_bound_rotates_to_a_fresh_full_prompt(self):
        config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, session_max_cost_usd=1.5))
        project = Project(config, "sample")
        save_worker_session(self, {"model": {"costUSD": 2.0}})
        capture = Capture()
        result = Engine(config, driver=capture).run(project, "worker")
        record = projection(self, result["run"])
        self.assertEqual(record["prompt_mode"], "full")
        self.assertIn("cost", record["rotation"])

    def test_ephemeral_profile_always_receives_full_prompt(self):
        capture = Capture()
        result = Engine(self.config, driver=capture).run(self.project, "searcher")
        decoded = json.loads(capture.prompts[0])
        self.assertIn("goal", decoded)
        record = projection(self, result["run"])
        self.assertEqual(record["prompt_mode"], "full")
        self.assertEqual(record["session_key"], "")
