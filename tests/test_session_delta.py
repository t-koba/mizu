import dataclasses
import json
import os
import time
import unittest

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
        record = read_json(path)
        record["created_at"] = old
        from mizu.fs import write_json as _write_json
        _write_json(path, record)
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

    def test_frequent_same_id_saves_still_hit_lifetime_bound(self):
        # Lifetime, not inactivity: repeated saves inside the window must not
        # postpone rotation once total elapsed lifetime crosses the bound.
        from mizu.engine_config import rotation_due, save_session
        config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, session_max_age_seconds=60))
        project = Project(config, "sample")
        path = save_worker_session(self, {"tokens": 0})
        old = time.time() - 120
        record = read_json(path)
        record["created_at"] = old
        from mizu.fs import write_json as _write_json
        _write_json(path, record)
        # Simulate a frequent resume: same-id save refreshes mtime to now.
        save_session(path, "test-session", {"tokens": 0})
        kept = read_json(path)
        self.assertAlmostEqual(kept["created_at"], old, delta=1.0)
        due, reason = rotation_due(config.limits, path, kept)
        self.assertTrue(due)
        self.assertIn("age", reason)
        capture = Capture()
        result = Engine(config, driver=capture).run(project, "worker")
        record_proj = projection(self, result["run"])
        self.assertEqual(record_proj["prompt_mode"], "full")
        self.assertIn("age", record_proj["rotation"])
        self.assertFalse(path.exists())

    def test_new_id_resets_lifetime_clock(self):
        from mizu.engine_config import save_session, session_age_seconds
        path = save_worker_session(self, {"tokens": 0})
        old = time.time() - 120
        record = read_json(path)
        record["created_at"] = old
        from mizu.fs import write_json as _write_json
        _write_json(path, record)
        # A different conversation id mints a fresh creation time.
        save_session(path, "other-session", {"tokens": 0})
        fresh = read_json(path)["created_at"]
        self.assertGreater(fresh, old)
        self.assertAlmostEqual(fresh, time.time(), delta=10.0)
        self.assertIsNotNone(session_age_seconds(path, read_json(path)))

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


class RotationFailureTests(Fixture):
    def _due_session(self, age_seconds=1, backdate=10):
        import dataclasses, os, time
        config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, session_max_age_seconds=age_seconds))
        project = Project(config, "sample")
        path = save_worker_session(self, {"tokens": 0})
        old = time.time() - backdate
        from mizu.fs import read_json as _read_json, write_json as _write_json
        record = _read_json(path)
        record["created_at"] = old
        _write_json(path, record)
        os.utime(path, (old, old))
        return config, project, path

    def test_due_rotation_generation_failure_fails_without_dispatch(self):
        import unittest.mock
        from mizu import fs as fs_mod
        config, project, path = self._due_session()
        real = fs_mod.write_json
        def flaky(target, *args, **kwargs):
            from pathlib import Path as _Path
            if _Path(target).name == "generation.json":
                raise OSError("injected generation failure")
            return real(target, *args, **kwargs)
        capture = Capture()
        with unittest.mock.patch.object(fs_mod, "write_json", side_effect=flaky):
            with self.assertRaises(OSError):
                Engine(config, driver=capture).run(project, "worker")
        # Zero driver admission and no successful completion.
        self.assertEqual(capture.prompts, [])
        # Failed bump changes nothing: the old conversation stays resumable
        # for the next dispatch to retry the due rotation, never admitted now.
        self.assertTrue(path.is_file())

    def test_due_rotation_unlink_failure_fails_without_dispatch(self):
        import unittest.mock
        from pathlib import Path as _Path
        config, project, path = self._due_session()
        real_unlink = _Path.unlink
        def flaky_unlink(self, *args, **kwargs):
            if self.name == "session.json":
                raise OSError("injected unlink failure")
            return real_unlink(self, *args, **kwargs)
        capture = Capture()
        with unittest.mock.patch.object(_Path, "unlink", autospec=True,
                                        side_effect=lambda s, *a, **k: flaky_unlink(s, *a, **k)):
            with self.assertRaises(OSError):
                Engine(config, driver=capture).run(project, "worker")
        self.assertEqual(capture.prompts, [])


class SessionTotalsTests(unittest.TestCase):
    """Native per-engine shapes total exactly once (review 7837a1c0)."""

    def test_claude_cache_tokens_are_counted(self):
        from mizu.engine_config import session_token_total
        saved = {"usage": {"my-model": {"inputTokens": 10, "outputTokens": 5,
                                        "cacheReadInputTokens": 100,
                                        "cacheCreationInputTokens": 50}}}
        self.assertEqual(session_token_total(saved), 165)

    def test_codex_flat_total_is_used_verbatim(self):
        from mizu.engine_config import session_token_total
        saved = {"usage": {"inputTokens": 1, "outputTokens": 2,
                           "cachedInputTokens": 3, "reasoningOutputTokens": 4,
                           "totalTokens": 10}}
        self.assertEqual(session_token_total(saved), 10)

    def test_aggregates_win_without_double_count(self):
        from mizu.engine_config import session_token_total, session_cost_total
        saved = {"usage": {"m": {"totalTokens": 100, "inputTokens": 60, "outputTokens": 40}}}
        self.assertEqual(session_token_total(saved), 100)
        saved = {"usage": {"costUSD": 2.0, "m": {"costUSD": 2.0}}}
        self.assertEqual(session_cost_total(saved), 2.0)
        saved = {"usage": {"m": {"costUSD": 1.0, "cost_estimate_usd": 1.0}}}
        self.assertEqual(session_cost_total(saved), 1.0)


class SnapshotRevivalTests(Fixture):
    def test_omitted_snapshot_revives_by_exact_id(self):
        # The recent window omits old snapshots, but an exact pinned id
        # revives the full integrity-checked record: omitted blocks
        # return without re-deriving them.
        ids = []
        for i in range(8):
            (self.project.workspace / "app.py").write_bytes(
                f"VALUE = {i}\n".encode())
            record = self.project.snapshots.create(
                self.project.snapshots.capture_files(
                    self.project.workspace),
                goal=self.project.goal, state="next", run=None,
                outcome="wait", summary=f"snap {i}")
            self.project.snapshots.publish(record)
            ids.append(record["id"])
        current = self.project.snapshots.get()
        window = self.project.snapshots.history(
            current["id"], self.config.limits.prompt_snapshots)
        self.assertNotIn(ids[0], [entry["id"] for entry in window])
        revived = self.project.snapshots.get(ids[0])
        self.assertEqual(revived["id"], ids[0])
        self.assertEqual(revived["summary"], "snap 0")
