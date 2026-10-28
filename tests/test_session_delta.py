import dataclasses
import json
import os
import time
import unittest
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.errors import Denied
from mizu.engine_config import effective, session_record, save_session
from mizu.fs import now, read_json
from mizu.project import Project
from mizu.runtime import Engine, _progress_obligations_digest
from mizu.snapshot import PROGRESS_SIGNALS, validate_progress


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
        self.assertNotIn("goal", decoded)
        self.assertEqual(decoded["goal_digest"], self.project.snapshots.get()["goal_digest"])
        self.assertIn("recent_snapshots", decoded)
        record = projection(self, result["run"])
        self.assertEqual(record["prompt_mode"], "full")
        self.assertTrue(record["session_key"])

    def test_new_writer_next_admits_and_opens_as_first_task_without_rotating(self):
        from mizu.runtime import should_run
        step = 'Add the deterministic regression for the parser.'
        state = 'Now: implemented the parser.\nNext: ' + step
        for resumed in (False, True):
            with self.subTest(resumed=resumed):
                def work(ctx, *_):
                    (ctx.workspace / 'app.py').write_text('VALUE = ' + str(resumed) + '\n')
                    ctx.handle('finish', {'outcome': 'continue', 'summary': 'Parser implemented',
                                          'state': state})
                # Use distinct preceding declarations so both subtests model a new handoff.
                def clear(ctx, *_):
                    ctx.handle('finish', {'outcome': 'wait', 'summary': 'Start slice',
                                          'state': 'Now: starting.\nNext: Implement the parser.'})
                Engine(self.config, driver=ScriptDriver(clear)).run(self.project, 'worker')
                first = Engine(self.config, driver=ScriptDriver(work)).run(self.project, 'worker')
                snapshot = self.project.snapshots.get()
                self.assertEqual(snapshot['state'], state)
                for _ in range(2):
                    self.assertTrue(should_run(self.project, snapshot, role_name='worker'))
                if resumed:
                    path = save_worker_session(self, {'tokens': 0})
                def followup(ctx, prompt, _):
                    decoded = json.loads(prompt)
                    self.assertEqual(next(iter(decoded)), 'task')
                    self.assertEqual(decoded['task'], step)
                    self.assertEqual(decoded['published_snapshot']['id'], snapshot['id'])
                    # Even real work cannot redeclare the identical Next to loop.
                    (ctx.workspace / 'app.py').write_text('VALUE = completed\n')
                    ctx.handle('finish', {'outcome': 'continue', 'summary': 'Regression added',
                                          'state': state})
                result = Engine(self.config, driver=ScriptDriver(followup)).run(self.project, 'worker')
                self.assertEqual(read_json(self.project.root / 'runs' / result['run'] / 'started.json')['admission'],
                                 'next_task')
                self.assertEqual(projection(self, result['run'])['prompt_mode'], 'delta' if resumed else 'full')
                self.assertEqual(projection(self, result['run'])['session_key'],
                                 projection(self, first['run'])['session_key'])
                if resumed:
                    self.assertEqual(read_json(path)['id'], 'test-session')
                self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name='worker'))

    def test_delta_does_not_swallow_work_item_store_errors(self):
        from mizu.runtime import session_prompt
        ctx = self.context("worker")
        save_worker_session(self, {"tokens": 0})
        with patch.object(self.project.work_items, "page_open_for_order",
                          side_effect=Denied("work-item page failed closed")) as page_open:
            with self.assertRaisesRegex(Denied, "work-item page failed closed"):
                session_prompt(ctx, ctx.role, ctx.run_dir)
            # The initial opening capture and generic full fallback fail closed.
            self.assertEqual(page_open.call_count, 2)

    def test_resumed_session_receives_bounded_delta(self):
        first = Capture()
        first_result = Engine(self.config, driver=first).run(self.project, "worker")
        save_worker_session(self, {"tokens": 0})
        second = Capture()
        result = Engine(self.config, driver=second).run(self.project, "worker")
        decoded = json.loads(second.prompts[0])
        self.assertNotIn("goal", decoded)
        self.assertNotIn("recent_snapshots", decoded)
        self.assertEqual(decoded["goal_digest"], self.project.snapshots.get()["goal_digest"])
        self.assertIn("snapshot_delta", decoded)
        self.assertLess(len(second.prompts[0]), len(first.prompts[0]))
        record = projection(self, result["run"])
        self.assertEqual(record["prompt_mode"], "delta")
        self.assertEqual(record["session_key"], projection(self, first_result["run"])["session_key"])
        self.assertEqual(record["recent_snapshots"], 0)

    def test_new_unit_after_spend_wrap_up_opens_with_its_own_surface(self):
        from mizu.errors import LimitExceeded
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], capabilities=("files", "read", "finish"))
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project = Project(self.config, "sample")
        for resumed in (False, True):
            with self.subTest(resumed=resumed):
                # Begin without a remembered session, then exercise either a
                # fresh full opening or a resumed delta after a bounded seal.
                path = save_worker_session(self, {"tokens": 0})
                path.unlink()
                turns = []
                def bounded(ctx, prompt, profile):
                    turns.append((ctx, prompt))
                    if len(turns) == 1:
                        ctx.bound_hit = "requests"
                        raise LimitExceeded("per-run requests exhausted")
                    self.assertEqual(ctx.role.capabilities, ("finish",))
                    notice, original = prompt.split("\n\n", 1)
                    self.assertIn("For the rest of this work unit only:", notice)
                    self.assertIn("(mizu_finish)", notice)
                    self.assertNotIn("mizu_research", notice)
                    self.assertEqual(json.loads(original)["unit_tool_surface"]["tools"],
                                     ["mizu_files", "mizu_read", "mizu_finish"])
                    with self.assertRaisesRegex(Denied, "no capability"):
                        ctx.handle("files", {})
                    ctx.handle("finish", {"outcome": "wait", "summary": "bounded", "state": "resume"})
                engine = Engine(self.config, driver=ScriptDriver(bounded))
                first = engine.run(self.project, "worker")
                self.assertEqual(first["finish"]["outcome"], "bounded")
                path = save_worker_session(self, {"tokens": 0})
                if not resumed:
                    path.unlink()
                def new_unit(ctx, prompt, profile):
                    decoded = json.loads(prompt)
                    self.assertIsNone(ctx.bound_hit)
                    self.assertFalse(ctx.wrap_up_attempted)
                    self.assertEqual(decoded["unit_tool_surface"]["tools"],
                                     ["mizu_files", "mizu_read", "mizu_finish"])
                    self.assertIn("earlier work units, not later", decoded["unit_tool_surface"]["scope"])
                    ctx.handle("files", {})  # Restored grant works, not just text.
                    ctx.handle("finish", {"outcome": "wait", "summary": "new unit", "state": "ready"})
                second = Engine(self.config, driver=ScriptDriver(new_unit)).run(self.project, "worker")
                self.assertEqual(projection(self, second["run"])["prompt_mode"], "delta" if resumed else "full")
                if resumed:
                    self.assertEqual(projection(self, first["run"])["session_key"],
                                     projection(self, second["run"])["session_key"])

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
        self.assertNotIn("goal", decoded)
        self.assertEqual(decoded["goal_digest"], self.project.snapshots.get()["goal_digest"])
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

    def test_repeated_no_progress_rotates_on_next_admitted_dispatch(self):
        from mizu.runtime import session_prompt
        from mizu.engine_config import session_generation
        config = dataclasses.replace(
            self.config, limits=dataclasses.replace(
                self.config.limits, no_progress_recovery_after=2))
        ctx = self.context("worker")
        ctx.config = config
        path = save_worker_session(self, {"tokens": 0})
        signals = {name: False for name in PROGRESS_SIGNALS}
        progress = validate_progress({
            "schema": 1, "signals": signals,
            "selected": list(config.limits.progress_signals), "role": "worker",
            "has_progress": False, "no_progress_count": 1,
            "obligations_digest": _progress_obligations_digest(()), "recorded_at": now(),
        })
        ctx.snapshot = {**ctx.snapshot, "outcome": "continue", "progress": progress}
        _, mode, _, rotation, _ = session_prompt(ctx, ctx.role, ctx.run_dir)
        self.assertEqual(mode, "delta")
        self.assertIsNone(rotation)
        self.assertTrue(path.is_file())
        progress["no_progress_count"] = 2
        ctx.snapshot = {**ctx.snapshot, "progress": validate_progress(progress)}
        prompt, mode, _, rotation, _ = session_prompt(ctx, ctx.role, ctx.run_dir)
        self.assertEqual(mode, "full")
        self.assertNotIn("goal", json.loads(prompt))
        self.assertEqual(json.loads(prompt)["goal_digest"], ctx.goal_digest)
        self.assertIn("no-progress units 2 >=2", rotation["reason"])
        self.assertEqual(session_generation(path.parent), 1)
        self.assertFalse(path.exists())

    def test_no_progress_recovery_rotates_even_without_saved_session_id(self):
        from mizu.runtime import session_prompt
        from mizu.engine_config import session_generation
        ctx = self.context("worker")
        settings = effective(ctx.config, ctx.role, ctx.role.profile)
        path, saved = session_record(ctx, ctx.role.profile, settings)
        self.assertIsNone(saved)
        self.assertFalse(path.exists())
        signals = {name: False for name in PROGRESS_SIGNALS}
        progress = validate_progress({
            "schema": 1, "signals": signals,
            "selected": list(ctx.config.limits.progress_signals), "role": "worker",
            "has_progress": False, "no_progress_count": 2,
            "obligations_digest": _progress_obligations_digest(()), "recorded_at": now(),
        })
        ctx.snapshot = {**ctx.snapshot, "outcome": "continue", "progress": progress}
        _, mode, _, rotation, _ = session_prompt(ctx, ctx.role, ctx.run_dir)
        self.assertEqual(mode, "full")
        self.assertIn("no-progress units 2", rotation["reason"])
        self.assertEqual(session_generation(path.parent), 1)
        self.assertFalse(path.exists())

    def test_token_bound_rotates_to_a_fresh_full_prompt(self):
        config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, session_max_tokens=10))
        project = Project(config, "sample")
        save_worker_session(self, {"tokens": 11})
        capture = Capture()
        result = Engine(config, driver=capture).run(project, "worker")
        decoded = json.loads(capture.prompts[0])
        self.assertNotIn("goal", decoded)
        self.assertEqual(decoded["goal_digest"], self.project.snapshots.get()["goal_digest"])
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
        self.assertNotIn("goal", decoded)
        self.assertEqual(decoded["goal_digest"], self.project.snapshots.get()["goal_digest"])
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


def _submit(fixture, source, title):
    return fixture.project.insights.submit(
        source=source, title=title, body="body",
        base_snapshot=fixture.project.snapshots.get()["id"])


class SessionBacklogTests(Fixture):
    def test_full_prompt_states_open_backlog(self):
        ids = {_submit(self, "worker", f"Finding {i}")["id"] for i in range(2)}
        capture = Capture()
        result = Engine(self.config, driver=capture).run(self.project, "worker")
        decoded = json.loads(capture.prompts[0])
        self.assertNotIn("goal", decoded)
        self.assertEqual(decoded["goal_digest"], self.project.snapshots.get()["goal_digest"])
        stated = decoded["open_insights"]
        self.assertEqual(set(stated["ids"]), ids)
        self.assertEqual(stated["total"], 2)
        self.assertFalse(stated["truncated"])
        self.assertEqual(projection(self, result["run"])["open_insights"], 2)

    def test_delta_with_no_new_offers_still_states_backlog(self):
        from mizu.engine_config import write_prompt_state
        from mizu.runtime import session_prompt
        ids = {_submit(self, "worker", f"Finding {i}")["id"] for i in range(2)}
        ctx = self.context("worker")
        path = save_worker_session(self, {"tokens": 0})
        # Mimic the post-run state: same snapshot, same inbox generation, so
        # the delta offers nothing new -- the reported empty-backlog case.
        write_prompt_state(path.parent, snapshot=ctx.snapshot["id"],
                           inbox_generation=self.project.insights.generation())
        prompt, mode, _, _, _ = session_prompt(ctx, ctx.role, ctx.run_dir)
        decoded = json.loads(prompt)
        self.assertEqual(mode, "delta")
        self.assertEqual(decoded["pending_insights"], [])
        stated = decoded["open_insights"]
        self.assertEqual(set(stated["ids"]), ids)
        self.assertEqual(stated["total"], 2)
        self.assertFalse(stated["truncated"])

    def test_changed_delta_offers_the_same_once_captured_inventory(self):
        from mizu.runtime import session_prompt, prompt_for
        item = _submit(self, "worker", "Finding")
        ctx = self.context("worker")
        save_worker_session(self, {"tokens": 0})
        with patch.object(self.project.insights, "projection", wraps=self.project.insights.projection) as captured:
            prompt, mode, _, _, _ = session_prompt(ctx, ctx.role, ctx.run_dir)
            full = json.loads(prompt_for(ctx))
            self.assertEqual(captured.call_count, 1)
        delta = json.loads(prompt)
        self.assertEqual(mode, "delta")
        self.assertEqual([row["id"] for row in delta["pending_insights"]], [item["id"]])
        self.assertEqual(delta["pending_insights"], full["pending_insights"])
        self.assertEqual(delta["work_order"]["opening_generation"], full["work_order"]["opening_generation"])

    def test_insight_arriving_after_delta_capture_stays_pending_for_next_unit(self):
        from mizu.engine_config import write_prompt_state
        from mizu.runtime import session_prompt
        work = self.project.work_items.create(
            idempotency_key="delta-capture-race", title="Work", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        path = save_worker_session(self, {"tokens": 0})
        store = self.project.insights
        original_generation = store.generation
        baseline = original_generation()
        write_prompt_state(path.parent, snapshot=ctx.snapshot["id"],
                           inbox_generation=baseline)
        inserted = []

        def inject_after_generation_read():
            value = original_generation()
            if not inserted:
                inserted.append(_submit(self, "worker", "Arrived after capture"))
            return value

        with patch.object(store, "generation", side_effect=inject_after_generation_read):
            prompt, mode, _, _, _ = session_prompt(ctx, ctx.role, ctx.run_dir)
        delta = json.loads(prompt)
        insight_id = inserted[0]["id"]
        opening_generation = delta["work_order"]["opening_generation"]
        self.assertEqual(mode, "delta")
        self.assertEqual(delta["pending_insights"], [])
        self.assertNotIn(insight_id, delta["open_insights"]["ids"])
        ctx.handle("work_order", {
            "items": [work["id"]], "rationale": "reviewed frozen opening",
            "expected_rev": delta["work_order"]["rev"],
            "expected_generation": opening_generation,
        })
        self.assertEqual(self.project.work_order_status()["status"], "review_due")

        next_ctx = self.context("worker")
        next_prompt, next_mode, _, _, _ = session_prompt(
            next_ctx, next_ctx.role, next_ctx.run_dir)
        next_delta = json.loads(next_prompt)
        self.assertEqual(next_mode, "delta")
        self.assertIn(insight_id, {row["id"] for row in next_delta["pending_insights"]})
        self.assertEqual(next_delta["work_order"]["status"], "review_due")

    def test_resume_generation_read_failure_reoffers_captured_inventory(self):
        from mizu.runtime import session_prompt
        item = _submit(self, "worker", "Offer when cursor unavailable")
        ctx = self.context("worker")
        save_worker_session(self, {"tokens": 0})
        with patch.object(self.project.insights, "generation",
                          side_effect=OSError("inbox cursor unavailable")):
            prompt, mode, _, _, _ = session_prompt(ctx, ctx.role, ctx.run_dir)
        decoded = json.loads(prompt)
        self.assertEqual(mode, "delta")
        self.assertIn(item["id"], {row["id"] for row in decoded["pending_insights"]})

    def test_open_backlog_truncation_is_explicit(self):
        import dataclasses
        from mizu.runtime import prompt_delta_for
        self.config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, pending_insights=2))
        for i in range(3):
            _submit(self, "worker", f"Finding {i}")
        stated = json.loads(prompt_delta_for(self.context("worker"), []))["open_insights"]
        self.assertEqual(len(stated["ids"]), 2)
        self.assertEqual(stated["total"], 3)
        self.assertTrue(stated["truncated"])

    def test_open_backlog_gated_by_capability(self):
        from mizu.runtime import prompt_delta_for, prompt_for
        _submit(self, "worker", "Finding")
        stated = json.loads(prompt_delta_for(self.context("consult"), []))["open_insights"]
        self.assertEqual(stated, {"ids": [], "total": 0, "truncated": False})
        full = json.loads(prompt_for(self.context("consult")))
        self.assertEqual(full["pending_insights"], [])
        self.assertEqual(full["open_insights"], {"ids": [], "total": 0, "truncated": False})


if __name__ == "__main__":
    unittest.main()
