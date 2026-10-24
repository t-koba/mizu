"""Local fake-driver measurements; not provider cache-residency tests."""
import dataclasses
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu import dispatch_evidence as evidence
from mizu.engine_config import effective, session_record, session_generation
from mizu.errors import DeadlineWrapUp, ModelFailure
from mizu.fs import read_json, write_json
from mizu.runtime import Engine
from mizu.usage import attempt_usage


class DispatchEvidenceTests(Fixture):
    def records(self, context):
        return read_json(context.run_dir / "dispatch-attempts.json")["attempts"]

    def test_start_is_crash_visible_and_missing_usage_is_unknown(self):
        seen = []
        def callback(ctx, *_):
            start = self.records(ctx)[0]
            self.assertEqual(start["status"], "started")
            self.assertIsNone(start["ended_at"])
            seen.append(ctx)
        Engine(self.config, driver=ScriptDriver(callback)).run(self.project, "reporter")
        record = self.records(seen[0])[0]
        self.assertEqual(record["status"], "returned")
        self.assertEqual(record["class"], "normal")
        self.assertIsNotNone(record["ended_at"])
        self.assertGreaterEqual(record["elapsed_seconds"], 0)
        self.assertEqual(record["usage"]["status"], "unknown")
        self.assertEqual(record["usage"]["unknown_reason"], "no_runtime_usage")
        self.assertIsNone(record["usage"]["cache_read_tokens"])
        self.assertTrue(record["models_unknown"])
        self.assertEqual(record["session"]["unknown_reason"], "driver_session_unobserved")

    def test_usage_and_models_are_attempt_local_on_missing_finish_followup(self):
        ctx = self.context()
        ctx.execution_attempt = 1
        class Driver:
            def execute(self, context, prompt):
                attempt = context.execution_attempt
                context.handle("_model_usage", {"sequence": 1,
                    "usage": {"input": attempt * 3, "cacheRead": attempt * 7},
                    "model": {"provider": "observed", "model": f"m{attempt}"}})
                context.handle("_model_usage", {"sequence": 2,
                    "usage": {"input": attempt, "cacheRead": 0}})
                if attempt == 2:
                    context.handle("finish", {"outcome": "wait", "summary": "done", "state": "Next step explicit."})
                return {"usage": [{"input": 99999}]}  # Deliberately ignored.
        Engine(self.config)._execute(ctx, Driver(), "work")
        records = self.records(ctx)
        self.assertEqual([r["execution_attempt"] for r in records], [1, 2])
        self.assertEqual([r["usage"]["input_tokens"] for r in records], [4, 8])
        self.assertEqual([r["usage"]["cache_read_tokens"] for r in records], [7, 14])
        self.assertEqual([r["models"][0]["model"] for r in records], ["m1", "m2"])
        self.assertTrue(all(r["models_unknown"] for r in records))
        self.assertIsNone(records[0]["usage"]["output_tokens"])
        self.assertEqual(records[1]["phase"], "continuation")

    def test_deadline_wrapup_records_each_call_without_changing_exception_path(self):
        ctx = self.context()
        ctx.execution_attempt = 1
        class Driver:
            def execute(self, context, prompt):
                if context.execution_attempt == 1:
                    context.bound_hit = "requests"
                    raise DeadlineWrapUp("synthetic")
                context.handle("finish", {"outcome": "wait", "summary": "sealed", "state": "Next step explicit."})
                return {}
        Engine(self.config)._execute(ctx, Driver(), "work")
        records = self.records(ctx)
        self.assertEqual([r["execution_attempt"] for r in records], [1, 2])
        self.assertEqual([r["status"] for r in records], ["raised", "returned"])
        self.assertEqual(records[1]["phase"], "continuation")

    def test_write_failure_preserves_return_and_exception(self):
        ctx = self.context()
        def select(context, *_):
            session_record(context, context.role.profile,
                           effective(context.config, context.role, context.role.profile))
        with patch.object(evidence, "write_json", side_effect=OSError("synthetic")):
            result = Engine(self.config)._execute(ctx, ScriptDriver(select), "work")
        self.assertIn("requests", result)
        ctx = self.context()
        error = ModelFailure("test", code="synthetic")
        class Driver:
            def execute(self, *_):
                raise error
        with patch.object(evidence, "write_json", side_effect=OSError("synthetic")):
            with self.assertRaises(ModelFailure) as caught:
                Engine(self.config)._execute(ctx, Driver(), "work")
        self.assertIs(caught.exception, error)

    def test_local_session_identity_requires_matching_driver_selection(self):
        ctx = self.context()
        ctx.execution_attempt = 1
        settings = effective(self.config, ctx.role, ctx.role.profile)
        path, _ = session_record(ctx, ctx.role.profile, settings)
        evidence.capture_projection(ctx, ctx.role.profile, "full", path.parent.name, None, path.parent)
        class Driver:
            def execute(self, context, prompt):
                session_record(context, context.role.profile, settings)
                if context.execution_attempt == 2:
                    context.handle("finish", {"outcome": "wait", "summary": "sealed", "state": "Next step explicit."})
                return {}
        Engine(self.config)._execute(ctx, Driver(), "work")
        session = self.records(ctx)[0]["session"]
        self.assertEqual(session["key"], path.parent.name)
        self.assertEqual(session["generation"], session_generation(path.parent))
        self.assertEqual(session["prompt_mode"], "full")
        self.assertIsNone(session["unknown_reason"])
        continuation = self.records(ctx)[1]["session"]
        self.assertEqual(continuation["key"], session["key"])
        self.assertEqual(continuation["prompt_mode"], "continuation")
        self.assertIsNone(continuation["rotation"])

    def test_generation_and_profile_mismatches_do_not_borrow_projection(self):
        for mismatch in ("profile", "generation"):
            with self.subTest(mismatch=mismatch):
                ctx = self.context()
                settings = effective(self.config, ctx.role, ctx.role.profile)
                path, _ = session_record(ctx, ctx.role.profile, settings)
                evidence.capture_projection(ctx, ctx.role.profile, "delta", path.parent.name, None, path.parent)
                if mismatch == "profile":
                    ctx._dispatch_projection["profile"] = "other"
                else:
                    write_json(path.parent / "generation.json", {"generation": 3})
                class Driver:
                    def execute(self, context, prompt):
                        session_record(context, context.role.profile, settings)
                        context.handle("finish", {"outcome": "wait", "summary": "sealed", "state": "Next step explicit."})
                        return {}
                Engine(self.config)._execute(ctx, Driver(), "work")
                session = self.records(ctx)[0]["session"]
                self.assertIsNone(session["key"])
                self.assertIsNone(session["prompt_mode"])
                self.assertEqual(session["unknown_reason"], "prompt_profile_mismatch" if mismatch == "profile"
                                 else "prompt_session_mismatch")

    def test_fixed_record_and_byte_bounds(self):
        ctx = self.context()
        class Driver:
            def execute(self, *_):
                return {}
        for attempt in range(evidence.MAX_ATTEMPTS + 2):
            ctx.execution_attempt = attempt
            evidence.execute(ctx, Driver(), "work")
        state = read_json(ctx.run_dir / "dispatch-attempts.json")
        self.assertEqual(len(state["attempts"]), evidence.MAX_ATTEMPTS)
        self.assertEqual(state["dropped_attempts"], 2)
        self.assertEqual(state["attempts"][0]["execution_attempt"], 2)
        with patch.object(evidence, "MAX_BYTES", 4096):
            ctx.execution_attempt += 1
            evidence.execute(ctx, Driver(), "work")
            self.assertLessEqual((ctx.run_dir / "dispatch-attempts.json").stat().st_size, 4096)
        self.assertEqual(self.records(ctx)[-1]["execution_attempt"], ctx.execution_attempt)

    def test_unknown_profile_and_nonfinite_usage_are_safe(self):
        ctx = self.context()
        ctx.role = dataclasses.replace(ctx.role, profile="")
        ctx.runtime_usage[(0, 1)] = {"input": float("nan"), "cacheRead": True, "odd": object()}
        class Driver:
            def execute(self, *_):
                return {}
        evidence.execute(ctx, Driver(), "work")
        record = self.records(ctx)[0]
        self.assertIsNone(record["profile"])
        self.assertEqual(record["profile_unknown_reason"], "selected_profile_unavailable")
        self.assertIsNone(record["engine"])
        self.assertEqual(record["usage"]["status"], "unknown")
        self.assertIsNone(record["usage"]["cache_read_tokens"])

    def test_ephemeral_run_class_and_session_are_not_persistent(self):
        seen = []
        def callback(ctx, *_):
            settings = effective(ctx.config, ctx.role, ctx.role.profile)
            session_record(ctx, ctx.role.profile, settings)
            seen.append(ctx)
        Engine(self.config, driver=ScriptDriver(callback), ephemeral=True).run(self.project, "reporter")
        record = self.records(seen[0])[0]
        self.assertEqual(record["class"], "ephemeral")
        self.assertEqual(record["session"]["unknown_reason"], "nonpersistent_session")
        self.assertIsNone(record["session"]["key"])

    def test_attempt_usage_omitted_invalid_and_unrecognized_are_not_zero(self):
        for observations in ([], [{}], [{"newProviderField": 3}], [{"input": -1}],
                             [{"input": 3}, {}], [{"input": True}]):
            with self.subTest(observations=observations):
                usage = attempt_usage(observations, "pi")
                self.assertEqual(usage["status"], "unknown")
                self.assertIsNone(usage["input_tokens"])
                self.assertIsNone(usage["cache_read_tokens"])
        self.assertEqual(attempt_usage([{"cacheRead": 0}], "pi")["cache_read_tokens"], 0)
        self.assertEqual(attempt_usage([{"input": 75, "cacheRead": 25, "cacheWrite": 10}],
                                       "pi")["cache_write_tokens"], 10)
