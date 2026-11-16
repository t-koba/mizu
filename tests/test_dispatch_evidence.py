"""Local fake-driver measurements; not provider cache-residency tests."""
import dataclasses
import os
import subprocess
import sys
from unittest.mock import patch

from support import ROOT, Fixture, ScriptDriver
from mizu import dispatch_evidence as evidence
from mizu.config import DispatchEvidence, MIN_DISPATCH_EVIDENCE_BYTES, Usage
from mizu.engine_config import effective, reset_role_sessions, rotate_session, session_record, session_generation
from mizu.errors import DeadlineWrapUp, ModelFailure
from mizu.fs import read_json, write_json
from mizu.runtime import Engine
from mizu.scheduler_evidence import summarize
from mizu.usage import MAX_ATTEMPT_USAGE_KEY_CHARS, attempt_usage, normalize


class DispatchEvidenceTests(Fixture):
    def records(self, context):
        return read_json(context.run_dir / "dispatch-attempts.json")["attempts"]

    def test_each_physical_dispatch_snapshots_valid_admission_marker_or_unknown(self):
        ctx = self.context()
        driver = type('Driver', (), {'execute': lambda *args, **kwargs: {}})()
        for number, identity in enumerate((None, "bad", "A" * 32, "1" * 32, "2" * 32), 1):
            ctx.execution_attempt = number
            if identity is not None:
                ctx._admission_id = identity
            evidence.execute(ctx, driver, "work")
        self.assertEqual([r["admission_id"] for r in self.records(ctx)],
                         [None, None, None, "1" * 32, "2" * 32])

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

    def test_ordinals_follow_selected_session_across_runs_and_failed_calls(self):
        contexts = [self.context() for _ in range(3)]
        settings = effective(self.config, contexts[0].role, contexts[0].role.profile)
        error = ModelFailure("test", code="synthetic")
        class Driver:
            def execute(self, context, prompt):
                session_record(context, context.role.profile, settings)
                # Repeated observation of the same selection is idempotent.
                session_record(context, context.role.profile, settings)
                context.handle("_model_usage", {"sequence": 1,
                    "usage": {"input": 100, "cacheRead": 25, "cacheWrite": 0},
                    "model": {"provider": "observed", "model": "m"}})
                if context is contexts[1]:
                    raise error
                return "original return"
        for index, ctx in enumerate(contexts):
            path, _ = session_record(ctx, ctx.role.profile, settings)  # peek
            if index == 0:
                self.assertFalse((path.parent / "dispatch-order.json").exists())
            evidence.capture_projection(ctx, ctx.role.profile, "full", path.parent.name, None, path.parent)
            with patch.object(evidence, "now", side_effect=[
                    f"2026-09-01T00:00:0{index * 2}+00:00",
                    f"2026-09-01T00:00:0{index * 2 + 1}+00:00"]):
                if index == 1:
                    with self.assertRaises(ModelFailure) as caught:
                        evidence.execute(ctx, Driver(), "work")
                    self.assertIs(caught.exception, error)
                else:
                    self.assertEqual(evidence.execute(ctx, Driver(), "work"), "original return")
        self.assertEqual([self.records(c)[0]["dispatch_ordinal"] for c in contexts], [1, 2, 3])
        sequence_ids = [self.records(c)[0]["dispatch_sequence_id"] for c in contexts]
        self.assertEqual(len(set(sequence_ids)), 1)
        self.assertTrue(evidence._sequence_id(sequence_ids[0]))
        self.assertEqual(summarize(self.project)["pairs"]["valid"], 1)
        # Lost/evicted evidence leaves the consumed ordinal visible as a gap.
        (contexts[1].run_dir / "dispatch-attempts.json").unlink()
        view = summarize(self.project)
        self.assertEqual(view["pairs"]["valid"], 0)
        self.assertEqual(view["pairs"]["excluded"]["ordinal_gap"], 1)
        # A reservation with no run record (crash after selection) also consumes
        # its ordinal rather than reclaiming it on the next call.
        self.assertEqual(evidence._dispatch_ordinal(path.parent, 0), 4)
        self.assertEqual(evidence._dispatch_ordinal(path.parent, 0), 5)

    def test_counter_generation_rotation_reuses_only_current_state(self):
        ctx = self.context()
        settings = effective(self.config, ctx.role, ctx.role.profile)
        path, _ = session_record(ctx, ctx.role.profile, settings)
        self.assertEqual(evidence._dispatch_ordinal(path.parent, 0), 1)
        first_state = read_json(path.parent / "dispatch-order.json")
        sequence_id = first_state["sequence_id"]
        self.assertTrue(evidence._sequence_id(sequence_id))
        self.assertEqual(evidence._dispatch_ordinal(path.parent, 0), 2)
        for generation in range(1, 4):
            rotate_session(path, ctx.run_dir, "test")
            self.assertEqual(evidence._dispatch_ordinal(path.parent, generation), 1)
            self.assertEqual(read_json(path.parent / "dispatch-order.json"),
                             {"generation": generation, "ordinal": 1,
                              "sequence_id": sequence_id})
        self.assertEqual(sorted(p.name for p in path.parent.glob("dispatch-order*")),
                         ["dispatch-order.json", "dispatch-order.lock"])
        reset_role_sessions(self.config, self.project, ctx.role.name)
        self.assertFalse(path.parent.exists())
        path, _ = session_record(ctx, ctx.role.profile, settings)
        self.assertEqual(evidence._dispatch_ordinal(path.parent, 0), 1)
        reset_state = read_json(path.parent / "dispatch-order.json")
        self.assertNotEqual(reset_state["sequence_id"], sequence_id)

    def test_legacy_counter_migrates_with_a_new_sequence_identity(self):
        directory = self.context().run_dir / "legacy-counter"
        directory.mkdir()
        self.assertEqual(evidence._dispatch_ordinal(directory, 0), 1)
        target = directory / "dispatch-order.json"
        state = read_json(target)
        sequence_id = state.pop("sequence_id")
        write_json(target, state)
        ordinal, migrated_id = evidence._dispatch_reservation(directory, 0)
        self.assertEqual(ordinal, 2)
        self.assertNotEqual(migrated_id, sequence_id)
        self.assertTrue(evidence._sequence_id(migrated_id))
        self.assertEqual(read_json(target), {"generation": 0, "ordinal": 2,
                                             "sequence_id": migrated_id})

    def test_counter_contention_is_nonblocking_and_cannot_bridge_lost_evidence(self):
        ctx = self.context()
        directory = ctx.run_dir
        self.assertEqual(evidence._dispatch_ordinal(directory, 0), 1)
        prefix = "import os, sys; sys.path.insert(0, sys.argv[2]); "
        child = subprocess.Popen([sys.executable, "-c", prefix +
            "from mizu import platform; "
            "fd=os.open(sys.argv[1], os.O_RDWR); platform.lock_fd(fd, blocking=False); "
            "print('locked', flush=True); sys.stdin.readline(); platform.unlock_fd(fd)",
            str(directory / "dispatch-order.lock"), str(ROOT / "src")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            import threading
            ready = []
            reader = threading.Thread(target=lambda: ready.append(child.stdout.readline()), daemon=True)
            reader.start()
            reader.join(timeout=5)
            self.assertEqual(ready, ["locked\n"])
            contender = subprocess.run([sys.executable, "-c", prefix +
                "from pathlib import Path; from mizu.dispatch_evidence import _dispatch_ordinal; "
                "print(_dispatch_ordinal(Path(sys.argv[1]), 0))",
                str(directory), str(ROOT / "src")], capture_output=True, text=True, timeout=5)
            self.assertEqual((contender.returncode, contender.stdout), (0, "None\n"))
        finally:
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0)
        self.assertIsNone(evidence._dispatch_ordinal(directory, 0))
        self.assertEqual((directory / "dispatch-order.lock").stat().st_size, 2)
        self.assertEqual((directory / "dispatch-order.lock").read_bytes(), b"0!")

    def test_counter_failures_leave_dispatch_unchanged_and_order_unknown(self):
        for failure in ("unsupported", "corrupt", "missing", "write"):
            with self.subTest(failure=failure):
                ctx = self.context()
                settings = effective(self.config, ctx.role, ctx.role.profile)
                path, _ = session_record(ctx, ctx.role.profile, settings)
                # Separate session directories keep each failure independent.
                directory = ctx.run_dir / "counter"
                directory.mkdir()
                self.assertEqual(evidence._dispatch_ordinal(directory, 0), 1)
                target = directory / "dispatch-order.json"
                if failure == "corrupt":
                    target.write_text('{"generation":0,"ordinal":true}')
                elif failure == "missing":
                    target.unlink()
                if failure == "unsupported":
                    guard = patch.object(evidence.platform, "lock_fd", side_effect=OSError("unsupported"))
                elif failure == "write":
                    guard = patch.object(evidence, "write_json", side_effect=OSError("write"))
                else:
                    guard = patch.object(evidence.platform, "lock_fd", wraps=evidence.platform.lock_fd)
                with guard:
                    self.assertIsNone(evidence._dispatch_ordinal(directory, 0))
                self.assertIsNone(evidence._dispatch_ordinal(directory, 0))
                # The physical call still returns and records a null ordinal
                # when the portable lock is unavailable.
                evidence.capture_projection(ctx, ctx.role.profile, "full", path.parent.name, None, path.parent)
                class Driver:
                    def execute(self, context, prompt):
                        session_record(context, context.role.profile, settings)
                        return "unchanged"
                with patch.object(evidence.platform, "lock_fd", side_effect=OSError("unsupported")):
                    self.assertEqual(evidence.execute(ctx, Driver(), "work"), "unchanged")
                self.assertIsNone(self.records(ctx)[0]["dispatch_ordinal"])
                self.assertIsNone(self.records(ctx)[0]["dispatch_sequence_id"])

    def test_invalid_generation_state_poisoned_and_not_repaired(self):
        ctx = self.context()
        settings = effective(self.config, ctx.role, ctx.role.profile)
        path, _ = session_record(ctx, ctx.role.profile, settings)
        self.assertEqual(evidence._dispatch_ordinal(path.parent, 0), 1)
        (path.parent / "generation.json").write_text(
            '{"generation":0,"generation":1}')
        self.assertIsNone(evidence._dispatch_ordinal(path.parent, 0))
        self.assertEqual((path.parent / "dispatch-order.lock").read_bytes(), b"0!")
        self.assertIsNone(evidence._dispatch_ordinal(path.parent, 0))

    def test_unsafe_counter_files_are_never_followed_or_repaired(self):
        for kind in ("symlink", "hardlink", "fifo", "oversized", "duplicate", "exhausted"):
            with self.subTest(kind=kind):
                directory = self.context().run_dir
                self.assertEqual(evidence._dispatch_ordinal(directory, 0), 1)
                target = directory / "dispatch-order.json"
                target.unlink()
                outside = directory / "untouched"
                outside.write_bytes(b"private")
                if kind == "symlink":
                    try:
                        target.symlink_to(outside)
                    except (OSError, NotImplementedError):
                        continue
                elif kind == "hardlink":
                    os.link(outside, target)
                elif kind == "fifo":
                    if not hasattr(os, "mkfifo"):
                        continue
                    os.mkfifo(target)
                elif kind == "oversized":
                    target.write_bytes(b" " * 257)
                elif kind == "duplicate":
                    target.write_text('{"generation":0,"ordinal":1,"ordinal":2}')
                else:
                    write_json(target, {"generation": 0, "ordinal": evidence.MAX_ORDINAL})
                self.assertIsNone(evidence._dispatch_ordinal(directory, 0))
                self.assertEqual(outside.read_bytes(), b"private")
                self.assertIsNone(evidence._dispatch_ordinal(directory, 0))

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

    def test_configured_record_and_byte_bounds(self):
        class Driver:
            def execute(self, *_):
                return {}
        for count in (64, 2, 66):
            ctx = self.context()
            ctx.config = dataclasses.replace(ctx.config, dispatch_evidence=DispatchEvidence(max_attempts=count))
            for attempt in range(count + 2):
                ctx.execution_attempt = attempt
                evidence.execute(ctx, Driver(), "work")
            target = ctx.run_dir / "dispatch-attempts.json"
            state = read_json(target)
            self.assertEqual(len(state["attempts"]), count)
            self.assertEqual(state["dropped_attempts"], 2)
            self.assertEqual(state["attempts"][0]["execution_attempt"], 2)
            ctx.config = dataclasses.replace(ctx.config, dispatch_evidence=DispatchEvidence(
                max_attempts=count, max_bytes=4096))
            ctx.execution_attempt += 1
            evidence.execute(ctx, Driver(), "work")
            state = read_json(target)
            self.assertLessEqual(target.stat().st_size, 4096)
            if count > 2:
                self.assertLess(len(state["attempts"]), count)
            self.assertEqual(state["attempts"][-1]["execution_attempt"], ctx.execution_attempt)
            self.assertEqual(len(state["attempts"]) + state["dropped_attempts"], count + 3)
            # A budget too small for even one attempt still writes a valid envelope.
            ctx.config = dataclasses.replace(ctx.config, dispatch_evidence=DispatchEvidence(
                max_bytes=MIN_DISPATCH_EVIDENCE_BYTES))
            evidence.execute(ctx, Driver(), "work")
            state = read_json(target)
            self.assertEqual(state["attempts"], [])
            self.assertLessEqual(target.stat().st_size, MIN_DISPATCH_EVIDENCE_BYTES)
            self.assertEqual(state["dropped_attempts"], count + 4)

    def test_model_and_unknown_key_samples_are_dispatch_owned(self):
        class Driver:
            def execute(self, *_):
                return {}
        for models, keys in ((0, 0), (1, 2), (16, 20), (18, 22)):
            ctx = self.context()
            ctx.config = dataclasses.replace(ctx.config, usage=Usage(unknown_keys=1),
                dispatch_evidence=DispatchEvidence(max_models=models, unknown_keys=keys))
            ctx.runtime_usage[(0, 0)] = {"input": 5, **{f"key{i:02}": i for i in range(24)}}
            for i in range(20):
                ctx.runtime_models[(0, i)] = {"provider": "acme", "model": f"m{i:02}"}
            evidence.execute(ctx, Driver(), "work")
            record = self.records(ctx)[0]
            self.assertEqual(len(record["models"]), models)
            self.assertEqual(record["models_truncated"], models < 20)
            self.assertEqual(record["usage"]["unrecognized_keys"], [f"key{i:02}" for i in range(keys)])
            self.assertEqual(record["usage"]["input_tokens"], 5)
            self.assertEqual(normalize(ctx.runtime_usage[(0, 0)], "pi",
                                      unknown_keys=ctx.config.usage.unknown_keys)["unrecognized_keys"], ["key00"])
        self.assertEqual(evidence.MAX_TEXT, 256)
        self.assertEqual(evidence._text("m" * 256), "m" * 256)
        self.assertIsNone(evidence._text("m" * 257))

    def test_unknown_usage_key_text_has_a_fixed_safety_ceiling(self):
        ctx = self.context()
        ctx.runtime_usage[(0, 0)] = {"input": 1, "k" * 300: 2}

        class Driver:
            def execute(self, *_):
                return {}

        evidence.execute(ctx, Driver(), "work")
        keys = self.records(ctx)[0]["usage"]["unrecognized_keys"]
        self.assertEqual(MAX_ATTEMPT_USAGE_KEY_CHARS, 256)
        self.assertEqual(keys, ["k" * MAX_ATTEMPT_USAGE_KEY_CHARS])

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
