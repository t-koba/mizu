"""Selectable pi-durable execution: distinct engine, durable recovery, replay safety."""
import dataclasses
import unittest

from support import Fixture
from mizu import pi_durable
from mizu.drivers import driver_for
from mizu.engine_config import effective
from mizu.errors import AuthorityGrantMismatch, ConfigError, LimitExceeded, ModelFailure, ProtocolError
from mizu.engine_config import rotate_session, rotation_due, session_generation
from mizu.pi_durable_store import (begin_run, check_grant, complete_run, complete_turn,
                                   grant_digest, load_run, open_store, project_context,
                                   prune, prune_generations, record_turn, retention_candidates,
                                   store_path_for)


class SelectionTests(Fixture):
    def test_distinct_engine_preserves_pi(self):
        self.assertEqual(self.config.profiles["primary"]["engine"], "pi")
        settings = dict(self.config.profiles["primary"])
        settings["engine"] = "pi-durable"
        self.config.profiles["durable"] = settings
        self.assertEqual(effective(self.config, self.config.roles["worker"], "durable")["engine"], "pi-durable")
        self.assertEqual(effective(self.config, self.config.roles["worker"], "primary")["engine"], "pi")
        self.assertIsInstance(driver_for(self.config, "durable", {}), pi_durable.PiDurableDriver)

    def test_unsupported_backend_refused(self):
        with self.assertRaises(ConfigError):
            pi_durable.durable_options({"options": {"durable_backend": "redis"}})

    def test_unknown_resume_refused(self):
        with self.assertRaises(ConfigError):
            pi_durable.durable_options({"options": {"durable_resume": "always"}})


class PolicyTests(Fixture):
    def _durable_profile(self, **options):
        settings = dict(self.config.profiles["primary"])
        settings["engine"] = "pi-durable"
        merged = dict(settings.get("options", {}))
        merged.update(options)
        settings["options"] = merged
        self.config.profiles["durable"] = settings
        return "durable"

    def test_grant_binds_durable_policy(self):
        role = self.config.roles["worker"]
        model = {"provider": "p", "model": "m"}
        base = pi_durable.grant_for({"options": {}}, role, model)
        for key, value in (("durable_backend", "sqlite"), ("durable_resume", "fresh"),
                           ("durable_retention_days", 30),
                           ("durable_compaction_reserve_tokens", 5000),
                           ("durable_compaction_keep_recent_tokens", 8000),
                           ("durable_compaction_background_tokens", 1000)):
            changed = pi_durable.grant_for({"options": {key: value}}, role, model)
            self.assertNotEqual(base, changed, key)
        # Reasoning effort is a per-turn live key, not authority: an
        # effort-only retune keeps the grant so resume is not refused.
        for value in ("off", "low", "high"):
            same = pi_durable.grant_for({"options": {"thinkingLevel": value}}, role, model)
            self.assertEqual(base, same, value)

    def test_removed_turn_bound_is_refused(self):
        with self.assertRaises(ConfigError):
            pi_durable.durable_options({"options": {"durable_max_turns": 80}})

    def test_unset_compaction_keeps_engine_default(self):
        # Unset must mean engine/provider default, not a Mizu value: the
        # driver omits the field so the Harness resolves its own built-ins.
        policy = pi_durable.durable_options({"options": {}})
        self.assertIsNone(policy["compaction"])

    def test_partial_compaction_passes_through(self):
        policy = pi_durable.durable_options(
            {"options": {"durable_compaction_reserve_tokens": 500000}})
        self.assertEqual(policy["compaction"], {"reserveTokens": 500000})

    def test_compaction_thresholds_refused_outside_bounds(self):
        for key in ("durable_compaction_reserve_tokens",
                    "durable_compaction_keep_recent_tokens",
                    "durable_compaction_background_tokens"):
            with self.assertRaises(ConfigError, msg=key):
                pi_durable.durable_options({"options": {key: -1}})
            with self.assertRaises(ConfigError, msg=key):
                pi_durable.durable_options({"options": {key: 1073741825}})
            with self.assertRaises(ConfigError, msg=key):
                pi_durable.durable_options({"options": {key: "16384"}})

    def test_compaction_only_change_keeps_session_identity(self):
        from mizu.engine_config import conversation_settings, effective, session_record
        profile = self._durable_profile()
        role = self.config.roles["worker"]
        before = effective(self.config, role, profile)
        path_before, _ = session_record(self.context(), profile, before)
        key_before = path_before.parent.name
        # Retuning only compaction thresholds (and thinkingLevel) must not fork:
        # the content-bound key stays identical, so the same store continues.
        self.config.profiles[profile]["options"] = {
            **before["options"],
            "durable_compaction_reserve_tokens": 500000,
            "durable_compaction_background_tokens": 100000}
        after = effective(self.config, role, profile)
        path_after, _ = session_record(self.context(), profile, after)
        self.assertEqual(path_after.parent.name, key_before)
        # pi-durable keeps no live keys: every setting rides the live
        # conversation, so identity never consults the options at all.
        self.assertEqual(conversation_settings(after)["options"], after["options"])
        # Any other durable setting still rides the same conversation:
        # only the backend and the declared store format rotate it.
        self.config.profiles[profile]["options"] = {
            **before["options"], "durable_retention_days": 30}
        same = effective(self.config, role, profile)
        path_same, _ = session_record(self.context(), profile, same)
        self.assertEqual(path_same.parent.name, key_before)
        self.config.profiles[profile]["options"] = {
            **before["options"], "durable_backend": "other"}
        forked = effective(self.config, role, profile)
        path_forked, _ = session_record(self.context(), profile, forked)
        self.assertNotEqual(path_forked.parent.name, key_before)

    def test_pi_rejects_durable_options(self):
        settings = dict(self.config.profiles["primary"])
        merged = dict(settings.get("options", {}))
        merged["durable_resume"] = "fresh"
        settings["options"] = merged
        self.config.profiles["bad"] = settings
        with self.assertRaises(ConfigError):
            effective(self.config, self.config.roles["worker"], "bad")

    def test_pi_durable_rejects_pi_sdk_knobs(self):
        profile = self._durable_profile(codemode=True)
        with self.assertRaises(ConfigError):
            effective(self.config, self.config.roles["worker"], profile)

    def test_no_turn_precheck_dispatches_to_launcher(self):
        profile = self._durable_profile()
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        seen = {}
        def boom(*args, **kwargs):
            seen.update(kwargs)
            raise ModelFailure("pi-durable", kind="error", message="boom")
        driver._run_once = boom
        with self.assertRaises(ModelFailure):
            driver.execute(context, "hello", profile=profile)
        # Spend is bounded only by request admission: dispatch proceeds with
        # no turn cap passed down.
        self.assertNotIn("remaining", seen)
        self.assertTrue(seen.get("called", True))

    def _normal_launcher(self, context, dispatches, *, seal_on=2, tool_on_first=False):
        """Fake only the child process; exercise the real driver, bridge and SQLite."""
        import json
        from pathlib import Path
        from mizu.bridge import connect
        from mizu.errors import Denied
        from mizu.process import Result

        def launch(argv, **kwargs):
            cfg = json.loads(Path(argv[-1]).read_text())
            bridge = json.loads(Path(kwargs["env"]["MIZU_BRIDGE_CONFIG"]).read_text())
            dispatches.append(cfg)
            conn = open_store(Path(cfg["store"]))
            try:
                rows = conn.execute("SELECT run_key FROM durable_runs WHERE run_key LIKE ?",
                                    (context.run_dir.name + "%",)).fetchall()
                self.assertEqual(rows, [(context.run_dir.name + (
                    f"-a{context.attempt_ordinal}" if context.attempt_ordinal else ""),)])
                record = load_run(conn, rows[0][0])
                authority = context.turn_authority_role or context.role
                grant = pi_durable.grant_for(effective(context.config, authority, "primary"),
                                             authority, context.config.model("primary"))
                check_grant(conn, rows[0][0], grant)
                self.assertEqual(record["grant_digest"], grant)
                self.assertEqual(record["turns"][-1]["payload"]["request_id"], cfg["requestId"])
            finally:
                conn.close()

            def call(operation, arguments):
                with connect(bridge) as sock:
                    sock.sendall((json.dumps({"token": bridge["token"], "operation": operation,
                                               "arguments": arguments}) + "\n").encode())
                    with sock.makefile("rb") as stream:
                        return json.loads(stream.readline())

            self.assertTrue(call("_hello", {})["ok"])
            self.assertTrue(call("_budget", {"sequence": 1})["ok"])
            if tool_on_first and len(dispatches) == 1:
                self.assertTrue(call("files", {})["ok"])
            if len(dispatches) > 1:
                did_tool_work = bool(context.tool_count or context.engine_tool_invocations)
                if did_tool_work:
                    self.assertEqual([t["operation"] for t in cfg["tools"]], ["finish"])
                    self.assertEqual([t["operation"] for t in bridge["tools"]], ["finish"])
                    self.assertFalse(call("files", {})["ok"])
                    with self.assertRaisesRegex(Denied, "no capability"):
                        context.handle("files", {})
                else:
                    self.assertIn("files", [t["operation"] for t in cfg["tools"]])
                    self.assertIn("files", [t["operation"] for t in bridge["tools"]])
            sealed = len(dispatches) == seal_on
            if sealed:
                self.assertTrue(call("finish", {"outcome": "wait", "summary": "sealed",
                                                "state": "done"})["ok"])
            self.assertTrue(call("_request_done", {"sequence": 1})["ok"])
            return Result(0, json.dumps({"durable_result": True, "status": "done",
                                        "finish_called": sealed, "requests": 1}), "", "exited", 0)
        return launch

    def test_driver_applies_followup_in_same_work_unit_with_narrowed_tools(self):
        from pathlib import Path
        from unittest import mock
        from mizu.runtime import Engine
        for session in ("persistent", "ephemeral"):
            for resume in ("compatible", "fresh"):
                with self.subTest(session=session, resume=resume):
                    config = dataclasses.replace(self.config, profiles={
                        **self.config.profiles, "primary": {**self.config.profiles["primary"],
                            "engine": "pi-durable", "session": session,
                            "options": {"durable_resume": resume}}})
                    context = self.context("worker")
                    context.config = config
                    context.attempt_ordinal = 2  # Already-selected candidate; follow-up must not advance it.
                    driver = pi_durable.PiDurableDriver(config)
                    dispatches = []
                    with mock.patch("mizu.pi_durable.run", self._normal_launcher(
                            context, dispatches, tool_on_first=True)):
                        model = Engine(config, driver=driver)._execute(context, driver, "synthetic durable work")
                    self.assertEqual(context.finished["summary"], "sealed")
                    self.assertEqual(len(dispatches), 2)
                    self.assertEqual(context.attempt_ordinal, 2)
                    self.assertEqual(dispatches[0]["store"], dispatches[1]["store"])
                    key = context.run_dir.name + "-a2"
                    self.assertEqual([d["requestId"] for d in dispatches], [key, key + "-t2"])
                    conn = open_store(Path(dispatches[0]["store"]))
                    try:
                        record = load_run(conn, key)
                        self.assertEqual(record["status"], "completed")
                        self.assertTrue(record["result"]["sealed"])
                        self.assertEqual([t["state"] for t in record["turns"]], ["completed"] * 2)
                        with self.assertRaisesRegex(ConfigError, "authority differs"):
                            begin_run(conn, run_key=key, session_key=record["session_key"],
                                      project=record["project"], role=record["role"], grant="changed")
                    finally:
                        conn.close()
                    self.assertEqual(context.request_count, 2)
                    self.assertEqual(context.request_deadlines, {})
                    self.assertEqual(model["missing_finish"]["used_followups"], 1)

    def test_clean_unsealed_exhaustion_is_terminal(self):
        from pathlib import Path
        from unittest import mock
        from mizu.errors import MissingFinish
        from mizu.runtime import Engine
        for allowance in (0, 1, 3):
            config = dataclasses.replace(self.config,
                limits=dataclasses.replace(self.config.limits, missing_finish_followups=allowance),
                profiles={**self.config.profiles, "primary": {**self.config.profiles["primary"],
                    "engine": "pi-durable", "options": {}}})
            context = self.context()
            context.config = config
            driver = pi_durable.PiDurableDriver(config)
            dispatches = []
            with mock.patch("mizu.pi_durable.run", self._normal_launcher(context, dispatches, seal_on=99)):
                with self.assertRaises(MissingFinish):
                    Engine(config, driver=driver)._execute(context, driver, "no seal")
            self.assertEqual(len(dispatches), 1 + allowance)
            self.assertEqual(len({d["requestId"] for d in dispatches}), 1 + allowance)
            conn = open_store(Path(dispatches[0]["store"]))
            try:
                record = load_run(conn, context.run_dir.name)
                self.assertEqual(record["status"], "completed")
                self.assertFalse(record["result"]["sealed"])
                self.assertTrue(all(t["state"] == "completed" for t in record["turns"]))
            finally:
                conn.close()

    def test_reduced_grant_wrapups_keep_authority_and_work_unit(self):
        from mizu.errors import DeadlineWrapUp
        from mizu.runtime import Engine, WRAP_UP_OPS
        for bound in ("deadline", "requests"):
            config = dataclasses.replace(self.config, profiles={
                **self.config.profiles, "primary": {**self.config.profiles["primary"],
                    "engine": "pi-durable", "options": {"durable_resume": "fresh"}}})
            driver = pi_durable.PiDurableDriver(config)
            driver.requires_sandbox = False  # No process/command is executed by this test.
            dispatches = []
            def turn(ctx, settings, model, prompt, profile, run_key, store_path):
                dispatches.append((ctx, run_key, store_path, ctx._durable_request_id))
                authority = ctx.turn_authority_role or ctx.role
                conn = open_store(store_path)
                try:
                    check_grant(conn, run_key, pi_durable.grant_for(settings, authority, model))
                finally:
                    conn.close()
                if len(dispatches) == 1:
                    ctx.bound_hit = bound
                    if bound == "deadline":
                        raise DeadlineWrapUp("wrap up")
                    raise LimitExceeded("per-run requests exhausted")
                self.assertEqual(ctx.role.capabilities,
                                 tuple(c for c in authority.capabilities if c in WRAP_UP_OPS)
                                 if bound == "deadline" else
                                 tuple(c for c in WRAP_UP_OPS if c in authority.capabilities))
                self.assertEqual(ctx.role.engine_tools, ())
                ctx.handle("_hello", {})
                ctx.handle("_budget", {"sequence": 1})
                ctx.handle("finish", {"outcome": "wait", "summary": "bounded", "state": "done"})
                ctx.handle("_request_done", {"sequence": 1})
                return {"engine": "pi-durable", "requests": 1}
            driver._run_once = turn
            engine = Engine(config, driver=driver)
            if bound == "deadline":
                context = self.context()
                context.config = config
                engine._execute(context, driver, "work")
            else:
                engine.run(self.project, "worker")  # Exercises the outer spend-wrap-up transition.
            self.assertEqual(len(dispatches), 2)
            self.assertEqual(dispatches[0][1:3], dispatches[1][1:3])
            self.assertEqual(dispatches[1][3], dispatches[0][3] + "-t2")
            context = dispatches[0][0]
            self.assertEqual(context.attempt_ordinal, 0)
            self.assertIsNone(context.turn_authority_role)
            self.assertFalse(context.turn_continuation)
            conn = open_store(dispatches[0][2])
            try:
                record = load_run(conn, dispatches[0][1])
                self.assertTrue(record["result"]["sealed"])
                self.assertEqual(record["status"], "completed")
            finally:
                conn.close()

    def test_interrupted_recovery_matches_prompt_and_turn_grant_and_reuses_id(self):
        import copy
        profile = self._durable_profile()
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        dispatches = []
        def interrupted(ctx, *args):
            dispatches.append(ctx._durable_request_id)
            raise ModelFailure("pi-durable", kind="error", message="uncertain")
        driver._run_once = interrupted
        with self.assertRaises(ModelFailure):
            driver.execute(context, "original prompt", profile=profile)
        # New process/context has no host continuation permission. Neither a
        # changed prompt nor narrowed turn may recover the uncertain request.
        recovered = copy.copy(context)
        del recovered._durable_dispatch
        with self.assertRaisesRegex(ConfigError, "prompt or turn grant differs"):
            driver.execute(recovered, "modified prompt", profile=profile)
        recovered.turn_authority_role = context.role
        recovered.role = dataclasses.replace(context.role, capabilities=("finish",))
        with self.assertRaisesRegex(ConfigError, "prompt or turn grant differs"):
            driver.execute(recovered, "original prompt", profile=profile)
        self.assertEqual(len(dispatches), 1)
        recovered.role = context.role
        with self.assertRaises(ModelFailure):
            driver.execute(recovered, "original prompt", profile=profile)
        self.assertEqual(dispatches, [context.run_dir.name] * 2)

    def test_fresh_refuses_interrupted_recovery_even_with_continuation_flag(self):
        import copy
        profile = self._durable_profile(durable_resume="fresh")
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        calls = []
        def interrupted(*args):
            calls.append(1)
            raise ProtocolError("interrupted")
        driver._run_once = interrupted
        with self.assertRaises(ProtocolError):
            driver.execute(context, "work", profile=profile)
        recovered = copy.copy(context)
        del recovered._durable_dispatch
        recovered.turn_continuation = True  # A persisted/crash context alone cannot authorize it.
        with self.assertRaisesRegex(ConfigError, "resume mode is fresh"):
            driver.execute(recovered, "work", profile=profile)
        self.assertEqual(len(calls), 1)

    def test_deadline_wrap_up_failure_restores_temporary_authority(self):
        import dataclasses
        from mizu.errors import DeadlineWrapUp
        from mizu.runtime import Engine, WRAP_UP_OPS
        config = dataclasses.replace(self.config, profiles={
            **self.config.profiles, "primary": {**self.config.profiles["primary"],
                "engine": "pi-durable", "options": {}}})
        context = self.context()
        context.config = config
        original = context.role
        context.turn_authority_role = original
        context.turn_continuation = False
        context.role = dataclasses.replace(original, capabilities=("finish",), engine_tools=())
        driver = pi_durable.PiDurableDriver(config)
        driver.requires_sandbox = False
        calls = []

        def fail_after_wrap(ctx, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise DeadlineWrapUp("reserved wall boundary")
            self.assertIs(ctx.turn_authority_role, original)
            self.assertEqual(ctx.role.capabilities,
                             tuple(c for c in original.capabilities if c in WRAP_UP_OPS))
            raise ModelFailure("pi-durable", kind="error", message="wrap transport failed")

        driver.execute = fail_after_wrap
        with self.assertRaisesRegex(ModelFailure, "wrap transport failed"):
            Engine(config, driver=driver)._execute(context, driver, "work")
        self.assertEqual(len(calls), 2)
        self.assertIs(context.turn_authority_role, original)
        self.assertFalse(context.turn_continuation)
        # Preserve _execute's existing narrowed Context.role end state.
        self.assertEqual(context.role.capabilities,
                         tuple(c for c in original.capabilities if c in WRAP_UP_OPS))

    def test_driver_faults_do_not_trigger_missing_finish_followup(self):
        from mizu.errors import Cancelled
        from mizu.runtime import Engine
        for fault in (ModelFailure("pi-durable", kind="error", message="provider"),
                      ProtocolError("bad transport result"), OSError("storage"), Cancelled("stop")):
            config = dataclasses.replace(self.config, profiles={
                **self.config.profiles, "primary": {**self.config.profiles["primary"],
                    "engine": "pi-durable", "options": {}}})
            context = self.context()
            context.config = config
            driver = pi_durable.PiDurableDriver(config)
            calls = []
            def fail(*args):
                calls.append(1)
                raise fault
            driver._run_once = fail
            with self.assertRaises(type(fault)):
                Engine(config, driver=driver)._execute(context, driver, "work")
            self.assertEqual(len(calls), 1)
            self.assertIsNone(context.missing_finish_evidence)

    def test_session_finalization_error_after_unsealed_return_is_not_followed_up(self):
        from unittest import mock
        from mizu.runtime import Engine
        config = dataclasses.replace(self.config, profiles={
            **self.config.profiles, "primary": {**self.config.profiles["primary"],
                "engine": "pi-durable", "session": "persistent", "options": {}}})
        context = self.context()
        context.config = config
        driver = pi_durable.PiDurableDriver(config)
        dispatches = []
        with mock.patch("mizu.pi_durable.run", self._normal_launcher(context, dispatches)), \
                mock.patch("mizu.pi_durable.save_session", side_effect=OSError("session storage failed")):
            with self.assertRaisesRegex(OSError, "session storage failed"):
                Engine(config, driver=driver)._execute(context, driver, "work")
        self.assertEqual(len(dispatches), 1)
        self.assertIsNone(context.missing_finish_evidence)

    def test_normal_done_without_finish_is_distinct_from_unanswered(self):
        from mizu.usage import normalize
        context = self.context()
        context.handle("_hello", {})
        context.handle("_budget", {"sequence": 1})
        context.handle("_request_done", {"sequence": 1})
        context.model_evidence = {"requests": 0, "usage": [], "usage_known": False}
        model = {"provider": "p", "model": "m"}
        pi_durable.interpret_result({
            "durable_result": True, "status": "done", "finish_called": False,
            "requests": 1, "usage": {"input": 6, "output": 2}}, context, model, None)
        self.assertIsNone(context.finished)
        self.assertEqual(context.request_deadlines, {})
        self.assertEqual(context.model_evidence["requests"], 1)
        self.assertEqual(normalize(context.model_evidence["usage"][0], "pi-durable")["input_tokens"], 6)
        with self.assertRaisesRegex(ProtocolError, "invalid finish flag"):
            pi_durable.interpret_result({"durable_result": True, "status": "done",
                                         "finish_called": "false"}, context, model, None)

    def test_request_bound_result_maps_to_limit(self):
        from mizu.errors import ProtocolError
        from mizu.usage import normalize
        context = self.context()
        context.handle("_hello", {})
        context.handle("_budget", {"sequence": 1})
        context.handle("finish", {"outcome": "wait", "summary": "s", "state": "n"})
        context.model_evidence = {"requests": 0, "usage": [], "usage_known": False}
        model = {"provider": "p", "model": "m"}
        with self.assertRaises(LimitExceeded):
            pi_durable.interpret_result({"durable_result": True, "status": "request_bound",
                                         "reason": "spent", "requests": 5,
                                         "usage": {"input": 10, "output": 5}}, context, model, None)
        self.assertEqual(context.bound_hit, "requests")
        # The bound-exhausted path keeps launcher accounting, like the done path.
        # Fallback usage stays in raw provider shape so usage accounting counts it.
        self.assertEqual(context.model_evidence["requests"], 5)
        self.assertTrue(context.model_evidence["usage_known"])
        self.assertEqual(context.model_evidence["usage"][0]["input"], 10)
        self.assertEqual(context.model_evidence["usage"][0]["output"], 5)
        self.assertEqual(normalize(context.model_evidence["usage"][0], "pi-durable")["input_tokens"], 10)
        with self.assertRaises(ProtocolError):
            pi_durable.interpret_result({"durable_result": True, "status": "done",
                                         "finish_called": False}, context, model, None)
        with self.assertRaisesRegex(ProtocolError, "unknown settlement status"):
            pi_durable.interpret_result({"durable_result": True, "status": "future-status"},
                                         context, model, None)
        pi_durable.interpret_result({"durable_result": True, "status": "done",
                                     "finish_called": True, "requests": 2,
                                     "usage": {"input": 3, "output": 4}},
                                    context, model, None)
        self.assertTrue(context.model_evidence["usage_known"])
        self.assertEqual(normalize(context.model_evidence["usage"][0], "pi-durable")["input_tokens"], 3)

    def test_runtime_usage_preferred_over_launcher_delta(self):
        from mizu.usage import normalize, summarize
        from mizu.fs import mkdir, write_json
        context = self.context()
        context.handle("_hello", {})
        context.handle("_budget", {"sequence": 1})
        context.handle("_model_usage", {"sequence": 2, "usage": {"input": 7, "output": 3},
                                        "model": {"provider": "p", "model": "m"}})
        context.handle("_budget", {"sequence": 3})
        context.handle("_model_usage", {"sequence": 4, "usage": {"input": 11, "output": 5},
                                        "model": {"provider": "p", "model": "m"}})
        context.handle("finish", {"outcome": "wait", "summary": "s", "state": "n"})
        context.model_evidence = {"profile": "durable", "provider": "p", "model": "m",
                                  "engine": "pi-durable", "requests": 0,
                                  "request_unit": "model_request", "usage": [], "usage_known": False}
        model = {"provider": "p", "model": "m"}
        pi_durable.interpret_result({"durable_result": True, "status": "done",
                                     "finish_called": True, "requests": 2,
                                     "usage": {"input": 999, "output": 999}},
                                    context, model, None)
        # Per-request host evidence wins over the aggregated launcher document.
        self.assertEqual(context.model_evidence["requests"], 2)
        self.assertEqual(len(context.model_evidence["usage"]), 2)
        self.assertEqual(normalize(context.model_evidence["usage"][0], "pi-durable")["input_tokens"], 7)
        self.assertEqual(normalize(context.model_evidence["usage"][1], "pi-durable")["input_tokens"], 11)
        # The same evidence is countable through the shared usage summary.
        run_dir = context.run_dir
        write_json(run_dir / "started.json", {"run": run_dir.name, "started_at": "2026-10-07T17:38:00+00:00"})
        write_json(run_dir / "result.json", {"run": run_dir.name, "role": "worker", "status": "completed",
                                             "finished_at": "2026-10-07T17:38:00+00:00",
                                             "model": dict(context.model_evidence)})
        facts = summarize(self.project)
        self.assertEqual(facts["totals"]["input_tokens"], 18)
        self.assertEqual(facts["totals"]["output_tokens"], 8)
        self.assertEqual(facts["totals"]["requests"], 2)

    def test_engine_records_terminal_unanswered_usage_and_preserves_unknown(self):
        from unittest import mock
        from mizu.fs import read_json
        from mizu.runtime import Engine
        from mizu.usage import summarize

        settings = dict(self.config.profiles["primary"])
        settings["engine"] = "pi-durable"
        self.config.profiles["primary"] = settings
        driver = pi_durable.PiDurableDriver(self.config)
        engine = Engine(self.config, driver=driver)
        cases = (
            ({"input": 13, "output": 5},
             {"input": 901, "output": 902}),
            (None, None),
        )
        run_root = self.project.root / "runs"
        run_root.mkdir(parents=True, exist_ok=True)

        for runtime_usage, launcher_usage in cases:
            def settle(context, settings, selected_model, prompt, profile, run_key, store_path):
                context.model_evidence = {
                    "profile": profile, **selected_model, "engine": "pi-durable",
                    "requests": 0, "request_unit": "model_request",
                    "usage": [], "usage_known": False,
                }
                context.handle("_hello", {})
                context.handle("_budget", {"sequence": 1})
                if runtime_usage is not None:
                    context.handle("_model_usage", {
                        "sequence": 2, "usage": runtime_usage,
                        "model": selected_model,
                    })
                reported = {
                    "durable_result": True, "status": "unanswered",
                    "reason": "provider rejected", "detail": "terminal settlement",
                    "requests": 1,
                }
                if launcher_usage is not None:
                    reported["usage"] = launcher_usage
                pi_durable.interpret_result(
                    reported, context, selected_model,
                    settings.get("options", {}).get("thinkingLevel"))

            driver._run_once = settle
            before = set(run_root.iterdir())
            with mock.patch("mizu.runtime.container_runtime"), \
                    mock.patch("mizu.runtime.cleanup"):
                with self.assertRaisesRegex(ModelFailure, "provider rejected: terminal settlement"):
                    engine.run(self.project, "worker")
            created = set(run_root.iterdir()) - before
            self.assertEqual(len(created), 1)
            run_dir = created.pop()
            error = read_json(run_dir / "error.json")
            self.assertEqual(error["model_failure"]["kind"], "unanswered")
            self.assertEqual(error["model_failure"]["message"],
                             "provider rejected: terminal settlement")
            self.assertEqual(error["model"]["requests"], 1)
            if runtime_usage is not None:
                # Runtime per-request observations win over launcher aggregates.
                self.assertEqual(error["model"]["usage"], [runtime_usage])
                self.assertTrue(error["model"]["usage_known"])
            else:
                # With neither source reporting usage, evidence stays unknown.
                self.assertEqual(error["model"]["usage"], [])
                self.assertFalse(error["model"]["usage_known"])

        facts = summarize(self.project)
        self.assertEqual(facts["totals"]["input_tokens"], 13)
        self.assertEqual(facts["totals"]["output_tokens"], 5)
        self.assertEqual(facts["totals"]["requests"], 2)
        self.assertEqual(facts["totals"]["unknown_usage_runs"], 1)

    def test_same_context_followup_combines_host_and_launcher_delta_once(self):
        self._check_followup_usage_delta(host_report=True)

    def test_same_context_followup_saves_launcher_deltas_without_host_reports(self):
        self._check_followup_usage_delta(host_report=False)

    def _check_followup_usage_delta(self, *, host_report):
        import json
        profile = self._durable_profile()
        self.config.profiles[profile]["session"] = "persistent"
        role = self.config.roles["worker"]
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        host_usage = {"input": 10, "output": 2}
        dispatch_delta = {"input": 20, "output": 3}
        calls = []

        def dispatch(ctx, settings, selected_model, prompt, selected_profile, run_key, store_path):
            index = len(calls)
            calls.append(index)
            ctx.model_evidence = {
                "profile": selected_profile, **selected_model, "engine": "pi-durable",
                "requests": 0, "request_unit": "model_request",
                "usage": [], "usage_known": False,
            }
            ctx.handle("_hello", {})
            ctx.handle("_budget", {"sequence": 1})
            if index == 0:
                if host_report:
                    ctx.handle("_model_usage", {
                        "sequence": 1, "usage": host_usage, "model": selected_model})
                # Deliberately disagree when host evidence exists: it wins.
                reported_usage = {"input": 999, "output": 999} if host_report else host_usage
                finish_called = False
            else:
                # Earlier attempt's host report remains in Context, but this
                # dispatch has only its own launcher delta, excluding the first
                # turn's 10 input + 2 output cumulative session counters.
                reported_usage = dispatch_delta
                ctx.handle("finish", {"outcome": "wait", "summary": "sealed",
                                       "state": "done"})
                finish_called = True
            ctx.handle("_request_done", {"sequence": 1})
            reported = {"durable_result": True, "status": "done",
                        "finish_called": finish_called, "requests": 1,
                        "usage": dict(reported_usage)}
            pi_durable.interpret_result(
                reported, ctx, selected_model,
                settings.get("options", {}).get("thinkingLevel"))
            return dict(ctx.model_evidence)

        driver._run_once = dispatch
        first = driver.execute(context, "first", profile=profile)
        self.assertEqual(first["usage"], [host_usage])
        self.assertEqual(first["requests"], 1)
        session_file = next((self.project.root / "sessions" / role.name).rglob("session.json"))
        self.assertEqual(json.loads(session_file.read_text())["usage"], {"tokens": 12})

        context.execution_attempt += 1
        context.turn_continuation = True
        final = driver.execute(context, "follow-up", profile=profile)
        self.assertEqual(final["usage"], [host_usage, dispatch_delta])
        self.assertEqual(final["requests"], 2)
        self.assertEqual(context.model_evidence["usage"], [host_usage, dispatch_delta])
        self.assertEqual(context.model_evidence["requests"], 2)
        self.assertTrue(context.model_evidence["usage_known"])
        self.assertEqual(json.loads(session_file.read_text())["usage"], {"tokens": 35})
        self.assertEqual(len(calls), 2)

    def test_completed_sealed_duplicate_restores_host_finish(self):
        from pathlib import Path
        profile = self._durable_profile()
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        finish = {"outcome": "wait", "summary": "saved host seal", "state": "preserved",
                  "wait_seconds": 60, "recorded_at": "2026-10-15T00:00:00+00:00"}
        calls = []

        def sealed(ctx, *args):
            calls.append(1)
            ctx.finished = dict(finish)
            return {"engine": "pi-durable", "requests": 1}

        driver._run_once = sealed
        first = driver.execute(context, "work", profile=profile)
        conn = open_store(Path(first["durable"]["store"]))
        try:
            record = load_run(conn, context.run_dir.name)
            self.assertEqual(record["result"]["finish"], finish)
        finally:
            conn.close()
        context.finished = None
        duplicate = driver.execute(context, "work", profile=profile)
        self.assertEqual(context.finished, finish)
        self.assertTrue(duplicate["durable"]["duplicate"])
        self.assertEqual(len(calls), 1)

    def test_completed_unsealed_duplicate_fails_closed(self):
        profile = self._durable_profile()
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        calls = []

        def unsealed(*args):
            calls.append(1)
            return {"engine": "pi-durable", "requests": 1}

        driver._run_once = unsealed
        driver.execute(context, "work", profile=profile)
        with self.assertRaisesRegex(ConfigError, "unsealed run requires in-process continuation"):
            driver.execute(context, "work", profile=profile)
        self.assertEqual(len(calls), 1)

    def test_persistent_session_saves_cumulative_tokens(self):
        profile = self._durable_profile()
        role = self.config.roles["worker"]
        self.config.profiles[profile]["session"] = "persistent"
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        usage = {"input_tokens": 10, "output_tokens": 5,
                 "cache_read_tokens": 0, "cache_write_tokens": 0}
        driver._run_once = lambda *a, **k: {"engine": "pi-durable", "requests": 1,
                                            "usage": [dict(usage)]}
        out = driver.execute(context, "hello", profile=profile)
        self.assertEqual(out["requests"], 1)
        parallel = context.run_dir.parents[1] / "sessions" / role.name
        saved = [p for p in parallel.rglob("session.json")]
        self.assertEqual(len(saved), 1)
        import json
        record = json.loads(saved[0].read_text())
        self.assertEqual(record["usage"], {"tokens": 15})
        # A second run carries the saved total forward.
        context2 = self.context()
        driver.execute(context2, "again", profile=profile)
        record2 = json.loads(saved[0].read_text())
        self.assertEqual(record2["usage"], {"tokens": 30})




class SessionContinuityTests(Fixture):
    def _durable_settings(self, **options):
        settings = dict(self.config.profiles["primary"])
        settings["engine"] = "pi-durable"
        merged = dict(settings.get("options", {}))
        merged.update(options)
        settings["options"] = merged
        return settings

    def _key(self, role_name, settings):
        from mizu.engine_config import effective, session_record
        ctx = self.context(role_name)
        path, _ = session_record(ctx, "primary", settings)
        return path.parent.name

    def test_durable_identity_ignores_policy_caps_goal_and_settings(self):
        import dataclasses
        settings = self._durable_settings()
        before = self._key("worker", settings)
        role = dataclasses.replace(
            self.config.roles["worker"],
            capabilities=tuple([*self.config.roles["worker"].capabilities, "delegate"]))
        self.config = dataclasses.replace(
            self.config, roles={**self.config.roles, "worker": role})
        changed = self._durable_settings(thinkingLevel="high", durable_retention_days=30)
        self.assertEqual(self._key("worker", changed), before)

    def test_durable_identity_rotates_on_backend(self):
        settings = self._durable_settings()
        before = self._key("worker", settings)
        other = dict(settings)
        other["options"] = {**settings["options"], "durable_backend": "other"}
        self.assertNotEqual(self._key("worker", other), before)

    def test_other_engines_keep_rotation(self):
        import dataclasses
        from mizu.engine_config import effective
        role = self.config.roles["worker"]
        before_settings = dict(self.config.profiles["primary"])
        before = self._key("worker", before_settings)
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            role, capabilities=tuple([*role.capabilities, "delegate"]))
        self.config = dataclasses.replace(self.config, roles=roles)
        self.assertNotEqual(self._key("worker", before_settings), before)

    def test_reset_clears_sessions_and_stores(self):
        from mizu.engine_config import reset_role_sessions
        from mizu.pi_durable_store import store_path_for
        sessions = self.project.root / "sessions" / "worker"
        (sessions / "abc123").mkdir(parents=True)
        (sessions / "abc123" / "session.json").write_text("{}")
        store = store_path_for(self.config.data, "sample", "worker", "abc123")
        store.parent.mkdir(parents=True)
        store.write_text("x")
        receipt = reset_role_sessions(self.config, self.project, "worker")
        self.assertEqual(receipt["removed_sessions"], ["abc123"])
        self.assertEqual(receipt["removed_stores"], ["abc123"])
        self.assertFalse(sessions.exists())
        self.assertFalse(store.exists())

    def test_reset_refuses_unknown_role_and_active_run(self):
        from mizu.engine_config import reset_role_sessions
        from mizu.errors import ConfigError, Denied
        from mizu.fs import write_json
        with self.assertRaises(ConfigError):
            reset_role_sessions(self.config, self.project, "ghost")
        active = self.project.root / "active" / "worker.json"
        write_json(active, {"run": "live"})
        try:
            with self.assertRaises(Denied):
                reset_role_sessions(self.config, self.project, "worker")
        finally:
            active.unlink()

    def test_reset_cli_parses_and_executes(self):
        from mizu.cli import execute, parser
        sessions = self.project.root / "sessions" / "worker"
        (sessions / "abc123").mkdir(parents=True)
        args = parser().parse_args(["--config", str(self.file), "session", "reset",
                                    "sample", "--role", "worker"])
        self.assertEqual(args.session_command, "reset")
        receipt = execute(args)
        self.assertEqual(receipt["role"], "worker")
        self.assertEqual(receipt["removed_sessions"], ["abc123"])
        self.assertFalse(sessions.exists())



class AuthPathTests(Fixture):
    def _durable_profile(self, **options):
        settings = dict(self.config.profiles["primary"])
        settings["engine"] = "pi-durable"
        merged = dict(settings.get("options", {}))
        merged.update(options)
        settings["options"] = merged
        self.config.profiles["durable"] = settings
        return "durable"

    def test_auth_path_defaults_to_durable_engine_directory(self):
        from pathlib import Path
        expected = self.config.agent_dir("pi-durable") / "auth.json"
        self.assertEqual(pi_durable.durable_auth_path(self.config), expected)
        self.assertTrue(Path(expected).parent.is_dir())

    def test_run_once_writes_engine_paths_into_effective_config(self):
        import json
        import types
        from pathlib import Path
        from unittest import mock
        from mizu.engine_config import effective
        profile = self._durable_profile()
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        context.handle("_hello", {})
        context.handle("_budget", {"sequence": 1})
        context.handle("finish", {"outcome": "wait", "summary": "s", "state": "n"})
        reported = {"durable_result": True, "status": "done", "finish_called": True,
                    "requests": 1, "usage": {"input": 1, "output": 1}}
        fake = types.SimpleNamespace(reason="exited", exit_code=0,
                                     stdout=json.dumps(reported), stderr="")
        settings = effective(self.config, context.role, profile)
        model = self.config.model(profile)
        with mock.patch.object(pi_durable, "Bridge") as bridge_cls, \
                mock.patch.object(pi_durable, "run", return_value=fake):
            bridge_cls.return_value.__enter__.return_value.config_file = str(
                context.run_dir / "bridge.json")
            driver._run_once(context, settings, model, "hello", profile,
                             "run-key", Path("/tmp/store.sqlite"))
        written = json.loads((context.run_dir / "pi-durable-effective.json").read_text())
        self.assertEqual(written["authPath"],
                         str(self.config.agent_dir("pi-durable") / "auth.json"))
        # The operator-defined model catalog is read from the same engine
        # directory, so one models.json defines a model for both engines.
        self.assertEqual(written["modelsPath"],
                         str(self.config.agent_dir("pi-durable") / "models.json"))
        self.assertEqual(written["requestId"], "run-key")
        # Unset keeps the engine default: the effective file omits the field
        # so no Mizu value is injected.
        self.assertNotIn("compaction", written)


class SessionIsolationTests(Fixture):
    def _durable_profile(self, **options):
        settings = dict(self.config.profiles["primary"])
        settings["engine"] = "pi-durable"
        merged = dict(settings.get("options", {}))
        merged.update(options)
        settings["options"] = merged
        self.config.profiles["durable"] = settings
        return "durable"

    def _succeed(self, driver, tokens=15):
        usage = {"input_tokens": tokens, "output_tokens": 0,
                 "cache_read_tokens": 0, "cache_write_tokens": 0}
        driver._run_once = lambda *a, **k: {"engine": "pi-durable", "requests": 1,
                                            "usage": [dict(usage)]}

    def _ephemeral(self):
        context = self.context()
        context.ephemeral = True
        return context

    def test_two_ephemeral_runs_use_distinct_stores(self):
        profile = self._durable_profile()
        driver = pi_durable.PiDurableDriver(self.config)
        self._succeed(driver)
        first_ctx, second_ctx = self._ephemeral(), self._ephemeral()
        first = driver.execute(first_ctx, "one", profile=profile)
        second = driver.execute(second_ctx, "two", profile=profile)
        self.assertNotEqual(first["durable"]["store"], second["durable"]["store"])
        # Each actual store holds only its own conversation: the other run
        # key resolves in neither store.
        from pathlib import Path
        for own, own_ctx, other_ctx in ((first, first_ctx, second_ctx),
                                        (second, second_ctx, first_ctx)):
            conn = open_store(Path(own["durable"]["store"]))
            try:
                self.assertIsNotNone(load_run(conn, own_ctx.run_dir.name))
                self.assertIsNone(load_run(conn, other_ctx.run_dir.name))
            finally:
                conn.close()

    def test_ephemeral_retry_resumes_same_store(self):
        profile = self._durable_profile()
        driver = pi_durable.PiDurableDriver(self.config)
        context = self._ephemeral()
        run_keys = []
        def boom(*args, **kwargs):
            run_keys.append(args[5])
            raise ModelFailure("pi-durable", kind="error", message="boom")
        driver._run_once = boom
        with self.assertRaises(ModelFailure):
            driver.execute(context, "hello", profile=profile)
        from pathlib import Path
        def succeed(*args, **kwargs):
            run_keys.append(args[5])
            return {"engine": "pi-durable", "requests": 1, "usage": []}
        driver._run_once = succeed
        out = driver.execute(context, "hello", profile=profile)
        self.assertEqual(run_keys[0], run_keys[1])
        self.assertTrue(out["durable"]["resumed"])
        conn = open_store(Path(out["durable"]["store"]))
        try:
            record = load_run(conn, context.run_dir.name)
        finally:
            conn.close()
        # Crash recovery kept the same conversation: one run row, now
        # completed, holding the same persisted prompt turn and submission id.
        self.assertEqual(record["status"], "completed")
        self.assertEqual(len(record["turns"]), 1)

    def test_terminal_unanswered_fallback_uses_new_row_and_preserves_unknown(self):
        from pathlib import Path
        first_profile = self._durable_profile()
        self.config.profiles[first_profile]["session"] = "persistent"
        fallback_profile = "durable-fallback"
        fallback_settings = dict(self.config.profiles[first_profile])
        fallback_settings["provider"] = "fallback-provider"
        fallback_settings["model"] = "fallback-model"
        fallback_settings["options"] = {**fallback_settings.get("options", {}),
                                         "thinkingLevel": "high"}
        self.config.profiles[fallback_profile] = fallback_settings
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        captured = []

        def unanswered(ctx, settings, model, prompt, selected_profile, run_key, store_path):
            captured.append((run_key, store_path,
                             pi_durable.grant_for(settings, ctx.role, model)))
            raise ModelFailure("pi-durable", kind="unanswered", message="model_error")

        driver._run_once = unanswered
        with self.assertRaises(ModelFailure):
            driver.execute(context, "first candidate", profile=first_profile)
        context.attempt_ordinal = 1

        def complete(ctx, settings, model, prompt, selected_profile, run_key, store_path):
            captured.append((run_key, store_path,
                             pi_durable.grant_for(settings, ctx.role, model)))
            return {"engine": "pi-durable", "requests": 1, "usage": []}

        driver._run_once = complete
        out = driver.execute(context, "fallback candidate", profile=fallback_profile)
        self.assertEqual(captured[0][1], captured[1][1])
        self.assertNotEqual(captured[0][0], captured[1][0])
        self.assertNotEqual(captured[0][2], captured[1][2])
        # Different durable profiles with different grants still address one
        # persistent SQLite session; the ordinal creates the distinct row.
        self.assertEqual(out["durable"]["store"], str(captured[0][1]))
        self.assertEqual(out["durable"]["grant"], captured[1][2])
        conn = open_store(Path(out["durable"]["store"]))
        try:
            rows = conn.execute("SELECT run_key FROM durable_runs ORDER BY run_key").fetchall()
            old = load_run(conn, captured[0][0])
            new = load_run(conn, captured[1][0])
        finally:
            conn.close()
        self.assertEqual({row[0] for row in rows}, {captured[0][0], captured[1][0]})
        self.assertEqual(old["status"], "interrupted")
        self.assertEqual(old["turns"][0]["state"], "unknown")
        self.assertEqual(old["turns"][0]["result"], {"uncertain": True})
        self.assertEqual(new["status"], "completed")

    def test_attempt_ordinal_distinguishes_equal_grant_candidates(self):
        first_profile = self._durable_profile()
        self.config.profiles[first_profile]["session"] = "persistent"
        fallback_profile = "durable-equal"
        self.config.profiles[fallback_profile] = dict(self.config.profiles[first_profile])
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        captured = []

        def unanswered(ctx, settings, model, prompt, selected_profile, run_key, store_path):
            captured.append((run_key, store_path,
                             pi_durable.grant_for(settings, ctx.role, model)))
            raise ModelFailure("pi-durable", kind="unanswered", message="model_error")

        driver._run_once = unanswered
        with self.assertRaises(ModelFailure):
            driver.execute(context, "first candidate", profile=first_profile)
        context.attempt_ordinal = 1

        def complete(ctx, settings, model, prompt, selected_profile, run_key, store_path):
            captured.append((run_key, store_path,
                             pi_durable.grant_for(settings, ctx.role, model)))
            return {"engine": "pi-durable", "requests": 1, "usage": []}

        driver._run_once = complete
        driver.execute(context, "equal-authority fallback", profile=fallback_profile)
        self.assertNotEqual(captured[0][0], captured[1][0])
        self.assertEqual(captured[0][1], captured[1][1])
        self.assertEqual(captured[0][2], captured[1][2])

    def test_changed_grant_without_advanced_attempt_still_refuses_resume(self):
        import dataclasses
        profile = self._durable_profile()
        self.config.profiles[profile]["session"] = "persistent"
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        seen = []

        def interrupted(*args, **kwargs):
            seen.append((args[5], args[6]))
            raise ModelFailure("pi-durable", kind="error", message="uncertain")

        driver._run_once = interrupted
        with self.assertRaises(ModelFailure):
            driver.execute(context, "retry same candidate", profile=profile)
        context.role = dataclasses.replace(context.role,
            capabilities=tuple([*context.role.capabilities, "delegate"]))
        with self.assertRaises(AuthorityGrantMismatch) as refused:
            driver.execute(context, "changed grant", profile=profile)
        self.assertIs(type(refused.exception), AuthorityGrantMismatch)
        self.assertIsInstance(refused.exception, ConfigError)
        self.assertIn("authority differs", str(refused.exception))
        self.assertEqual([item[0] for item in seen], [context.run_dir.name])
        # The persistent store's identity does not change with the grant; the
        # original run row remains the only candidate for this request id.
        conn = open_store(seen[0][1])
        try:
            self.assertIsNotNone(load_run(conn, context.run_dir.name))
            self.assertIsNone(load_run(conn, context.run_dir.name + "-a1"))
        finally:
            conn.close()

    def test_effort_only_change_resumes_without_grant_refusal(self):
        profile = self._durable_profile()
        self.config.profiles[profile]["session"] = "persistent"
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        seen = []

        def interrupted(*args, **kwargs):
            seen.append((args[5], args[6]))
            raise ModelFailure("pi-durable", kind="error", message="uncertain")

        driver._run_once = interrupted
        with self.assertRaises(ModelFailure):
            driver.execute(context, "retry same candidate", profile=profile)
        # Effort-only retune keeps the conversation and its authority.
        self.config.profiles[profile]["options"] = {
            **self.config.profiles[profile]["options"], "thinkingLevel": "high"}
        self._succeed(driver)
        out = driver.execute(context, "retry same candidate", profile=profile)
        self.assertTrue(out["durable"]["resumed"])
        self.assertEqual([item[0] for item in seen], [context.run_dir.name])
        # The interrupted run row resumes in place; no second run is born.
        conn = open_store(seen[0][1])
        try:
            rows = conn.execute("SELECT run_key, status FROM durable_runs").fetchall()
            self.assertEqual([(row[0], row[1]) for row in rows],
                             [(context.run_dir.name, "completed")])
        finally:
            conn.close()

    def test_token_rotation_starts_a_fresh_conversation(self):
        import json
        import types
        profile = self._durable_profile()
        self.config.profiles[profile]["session"] = "persistent"
        role = self.config.roles["worker"]
        driver = pi_durable.PiDurableDriver(self.config)
        self._succeed(driver, tokens=15)
        first = driver.execute(self.context(), "one", profile=profile)
        session_file = next((self.project.root / "sessions" / role.name).rglob("session.json"))
        saved = json.loads(session_file.read_text())
        limits = types.SimpleNamespace(session_max_tokens=1, session_max_cost_usd=0,
                                       session_max_age_seconds=0)
        due, reason = rotation_due(limits, session_file, saved, "pi-durable")
        self.assertTrue(due, reason)
        rotate_session(session_file, self.project.root / "runs" / "rot", reason)
        self.assertEqual(session_generation(session_file.parent), 1)
        out = driver.execute(self.context(), "two", profile=profile)
        self.assertNotEqual(out["durable"]["store"], first["durable"]["store"])
        from pathlib import Path as _Path
        _store = _Path(out["durable"]["store"])
        self.assertEqual(_store.name, "store.sqlite")
        self.assertTrue(_store.parent.name.endswith("-g1"))
        # The abandoned store is retired, and the fresh store holds no
        # trace of the previous conversation.
        from pathlib import Path
        self.assertFalse(Path(first["durable"]["store"]).exists())
        self.assertEqual(out["durable"]["pruned_generations"],
                         [Path(first["durable"]["store"]).parent.name])
        conn = open_store(Path(out["durable"]["store"]))
        try:
            rows = conn.execute("SELECT run_key FROM durable_runs").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        fresh = json.loads(session_file.read_text())
        self.assertEqual(fresh["usage"], {"tokens": 15})

    def test_persistent_retry_keeps_crash_recovery_without_rotation(self):
        profile = self._durable_profile()
        self.config.profiles[profile]["session"] = "persistent"
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        def boom(*args, **kwargs):
            raise ModelFailure("pi-durable", kind="error", message="boom")
        driver._run_once = boom
        with self.assertRaises(ModelFailure):
            driver.execute(context, "hello", profile=profile)
        self._succeed(driver)
        out = driver.execute(context, "hello", profile=profile)
        self.assertTrue(out["durable"]["resumed"])
        self.assertEqual(out["durable"]["pruned_generations"], [])
        from pathlib import Path
        conn = open_store(Path(out["durable"]["store"]))
        try:
            record = load_run(conn, context.run_dir.name)
        finally:
            conn.close()
        self.assertEqual(record["status"], "completed")
        self.assertEqual(len(record["turns"]), 1)

    def test_corrupt_generation_refuses_instead_of_resuming(self):
        profile = self._durable_profile()
        self.config.profiles[profile]["session"] = "persistent"
        role = self.config.roles["worker"]
        driver = pi_durable.PiDurableDriver(self.config)
        self._succeed(driver)
        driver.execute(self.context(), "one", profile=profile)
        session_dir = next((self.project.root / "sessions" / role.name).rglob("session.json")).parent
        (session_dir / "generation.json").write_text("{corrupt")
        with self.assertRaises(ConfigError):
            driver.execute(self.context(), "two", profile=profile)


class StoreTests(unittest.TestCase):
    def test_isolation_by_project_role_session(self):
        a = store_path_for("/data", "proj", "worker", "aaa")
        b = store_path_for("/data", "proj", "worker", "bbb")
        c = store_path_for("/data", "proj", "other", "aaa")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a.parent.parts[-4:], ("durable", "proj", "worker", "aaa"))
        self.assertEqual(a.name, "store.sqlite")

    def test_real_persistence_recovery_and_replay_safety(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "store.sqlite"
            conn = open_store(path)
            grant = grant_digest(policy_text="p", capabilities=("a",), model={"provider": "x", "model": "y"},
                                 adapter_digest_value="d", options_digest="o")
            first = begin_run(conn, run_key="r1", session_key="s", project="p", role="r", grant=grant)
            self.assertFalse(first["resumed"])
            record_turn(conn, "r1", 1, "prompt", {"text": "hello"})
            dup = record_turn(conn, "r1", 1, "prompt", {"text": "hello"})
            self.assertTrue(dup["duplicate"])
            complete_turn(conn, "r1", 1, {"ok": True})
            complete_run(conn, "r1", {"engine": "pi-durable"})
            conn.close()
            # Crash recovery: reopen, reload, resume with same grant.
            conn = open_store(path)
            record = load_run(conn, "r1")
            self.assertEqual(record["status"], "completed")
            self.assertEqual(len(record["turns"]), 1)
            resumed = begin_run(conn, run_key="r1", session_key="s", project="p", role="r", grant=grant)
            self.assertTrue(resumed["resumed"])
            self.assertIsNotNone(resumed["result"])
            projection = project_context(conn, "r1")
            self.assertEqual(projection["turns"], 1)
            conn.close()

    def test_path_alone_never_authorizes_resume(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            conn = open_store(Path(td) / "s.sqlite")
            grant = grant_digest(policy_text="p", capabilities=("a",), model={"provider": "x", "model": "y"},
                                 adapter_digest_value="d", options_digest="o")
            begin_run(conn, run_key="r", session_key="s", project="p", role="r", grant=grant)
            with self.assertRaises(AuthorityGrantMismatch) as refused:
                check_grant(conn, "r", "different-grant")
            self.assertIs(type(refused.exception), AuthorityGrantMismatch)
            self.assertIsInstance(refused.exception, ConfigError)
            with self.assertRaises(AuthorityGrantMismatch) as resumed:
                begin_run(conn, run_key="r", session_key="s", project="p", role="r", grant="different-grant")
            self.assertIs(type(resumed.exception), AuthorityGrantMismatch)
            self.assertIsInstance(resumed.exception, ConfigError)
            conn.close()

    def test_retention_keeps_unfinished_and_unverifiable(self):
        import tempfile, time
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            conn = open_store(Path(td) / "s.sqlite")
            old = time.time() - 90 * 86400
            rows = (
                ("done", "completed", '{"x":1}'),
                ("active", "active", None),
                ("unknown-turn", "completed", '{"x":1}'),
                ("future-turn", "completed", '{"x":1}'),
                ("missing-result", "completed", None),
                ("malformed-result", "completed", "not-json"),
            )
            for key, status, result in rows:
                conn.execute("INSERT INTO durable_runs(run_key, session_key, project, role, grant_digest, status, created_at, updated_at, result_json) VALUES(?,?,?,?,?,?,?,?,?)",
                             (key, "s", "p", "r", "g", status, old, old, result))
            for key, state in (("unknown-turn", "unknown"), ("future-turn", "future-pending")):
                conn.execute("INSERT INTO durable_turns(run_key, seq, kind, payload_json, state) VALUES(?,?,?,?,?)",
                             (key, 1, "prompt", '{"a":1}', state))
            conn.commit()
            candidates = retention_candidates(conn, retention_days=31, now=time.time())
            self.assertEqual(candidates, ["done"])
            preview = prune(conn, candidates, dry_run=True)
            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["candidates"], ["done"])
            applied = prune(conn, candidates, dry_run=False)
            self.assertEqual(applied["removed"], ["done"])
            self.assertIsNone(load_run(conn, "done"))
            for key in ("active", "unknown-turn", "future-turn", "missing-result", "malformed-result"):
                self.assertIsNotNone(conn.execute("SELECT 1 FROM durable_runs WHERE run_key=?", (key,)).fetchone())
            conn.close()


class PruneGenerationsTests(unittest.TestCase):
    def _role_dir(self, root, project="proj", role="worker"):
        from pathlib import Path
        role_dir = Path(root) / "durable" / project / role
        role_dir.mkdir(parents=True)
        return role_dir

    def _grant(self):
        return grant_digest(policy_text="p", capabilities=("a",),
                            model={"provider": "x", "model": "y"},
                            adapter_digest_value="d", options_digest="o")

    def _terminal_store(self, directory):
        from pathlib import Path
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        conn = open_store(directory / "store.sqlite")
        try:
            begin_run(conn, run_key="r1", session_key="s", project="p",
                      role="r", grant=self._grant())
            record_turn(conn, "r1", 1, "prompt", {"text": "hi"})
            complete_turn(conn, "r1", 1, {"ok": True})
            complete_run(conn, "r1", {"engine": "pi-durable"})
        finally:
            conn.close()

    def test_prune_removes_only_terminal_older_generations(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            role_dir = self._role_dir(td)
            for name in ("aaa", "aaa-g1"):
                self._terminal_store(role_dir / name)
            for name in ("aaa-g2", "bbb", "aaa-gX", "aa", "aaa-g2-extra"):
                (role_dir / name).mkdir()
            removed = prune_generations(td, project="proj", role="worker",
                                        base_key="aaa", generation=2)
            self.assertEqual(removed, ["aaa", "aaa-g1"])
            remaining = sorted(p.name for p in role_dir.iterdir())
            self.assertEqual(remaining, ["aa", "aaa-g2", "aaa-g2-extra", "aaa-gX", "bbb"])

    def test_prune_retains_unresolved_recovery_evidence(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            role_dir = self._role_dir(td)
            # Active run: recovery evidence still needed.
            active = role_dir / "aaa"
            active.mkdir()
            conn = open_store(active / "store.sqlite")
            begin_run(conn, run_key="r1", session_key="s", project="p",
                      role="r", grant=self._grant())
            conn.close()
            # Unknown-completion turn: side effects unaccounted.
            unknown = role_dir / "aaa-g1"
            unknown.mkdir()
            conn = open_store(unknown / "store.sqlite")
            begin_run(conn, run_key="r2", session_key="s", project="p",
                      role="r", grant=self._grant())
            record_turn(conn, "r2", 1, "prompt", {"text": "hi"})
            complete_run(conn, "r2", {"engine": "pi-durable"})
            conn.execute("UPDATE durable_turns SET state='unknown' WHERE run_key='r2'")
            conn.commit()
            conn.close()
            # Corrupt and missing stores cannot be verified: retained.
            corrupt = role_dir / "aaa-g2"
            corrupt.mkdir()
            (corrupt / "store.sqlite").write_bytes(b"not a database")
            missing = role_dir / "aaa-g3"
            missing.mkdir()
            removed = prune_generations(td, project="proj", role="worker",
                                        base_key="aaa", generation=9)
            self.assertEqual(removed, [])
            remaining = sorted(p.name for p in role_dir.iterdir())
            self.assertEqual(remaining, ["aaa", "aaa-g1", "aaa-g2", "aaa-g3"])

    def test_prune_retains_unexpected_run_and_turn_states(self):
        import tempfile
        from pathlib import Path
        from mizu.pi_durable_store import generation_terminal
        with tempfile.TemporaryDirectory() as td:
            role_dir = self._role_dir(td)
            # A corrupt edit or future run status is not verifiable.
            bogus_run = role_dir / "aaa"
            bogus_run.mkdir()
            conn = open_store(bogus_run / "store.sqlite")
            conn.execute("INSERT INTO durable_runs(run_key, session_key, project, role, grant_digest, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                         ("r1", "s", "p", "r", "g", "bogus", 0, 0))
            conn.commit()
            conn.close()
            self.assertFalse(generation_terminal(bogus_run))
            # A non-enumerated turn state counts as unresolved evidence.
            bogus_turn = role_dir / "aaa-g1"
            bogus_turn.mkdir()
            conn = open_store(bogus_turn / "store.sqlite")
            begin_run(conn, run_key="r2", session_key="s", project="p",
                      role="r", grant=self._grant())
            record_turn(conn, "r2", 1, "prompt", {"text": "hi"})
            complete_turn(conn, "r2", 1, {"ok": True})
            complete_run(conn, "r2", {"engine": "pi-durable"})
            conn.execute("UPDATE durable_turns SET state='bogus' WHERE run_key='r2'")
            conn.commit()
            conn.close()
            self.assertFalse(generation_terminal(bogus_turn))
            removed = prune_generations(td, project="proj", role="worker",
                                        base_key="aaa", generation=9)
            self.assertEqual(removed, [])
            self.assertTrue(bogus_run.is_dir())
            self.assertTrue(bogus_turn.is_dir())

    def test_prune_never_touches_files_or_symlinks(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            role_dir = self._role_dir(td)
            (role_dir / "aaa-g1").write_text("stray")
            real = role_dir / "real"
            self._terminal_store(real)
            link = role_dir / "aaa-g2"
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            removed = prune_generations(td, project="proj", role="worker",
                                        base_key="aaa", generation=9)
            self.assertEqual(removed, [])
            self.assertTrue((role_dir / "aaa-g1").is_file())
            self.assertTrue(link.is_symlink())

    def test_prune_missing_role_dir_is_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(prune_generations(td, project="proj", role="worker",
                                               base_key="aaa", generation=1), [])

    def test_prune_rejects_bad_generation(self):
        from mizu.errors import ConfigError
        for bad in (0, -1, "2", None):
            with self.assertRaises(ConfigError):
                prune_generations("/data", project="p", role="r",
                                  base_key="aaa", generation=bad)


class RotationTransitionTests(unittest.TestCase):
    def _session_dir(self, root):
        import json
        from pathlib import Path
        directory = Path(root) / "sess"
        directory.mkdir(parents=True)
        (directory / "session.json").write_text(json.dumps(
            {"engine": "pi", "session_id": "s", "totals": {}}))
        run = Path(root) / "run"
        run.mkdir()
        return directory, run

    def _fail_on(self, name):
        from pathlib import Path
        from mizu import fs as fs_mod
        real = fs_mod.write_json

        def guarded(path, *args, **kwargs):
            if Path(path).name == name:
                raise OSError(f"injected {name} failure")
            return real(path, *args, **kwargs)
        return guarded

    def test_failed_generation_bump_changes_nothing(self):
        import tempfile
        from unittest import mock
        from mizu import fs as fs_mod
        with tempfile.TemporaryDirectory() as td:
            directory, run = self._session_dir(td)
            with mock.patch.object(fs_mod, "write_json",
                                   side_effect=self._fail_on("generation.json")):
                with self.assertRaises(OSError):
                    rotate_session(directory / "session.json", run, "test")
            # Nothing moved: the next dispatch retries the due rotation
            # instead of silently resuming or half-rotating.
            self.assertTrue((directory / "session.json").is_file())
            self.assertFalse((directory / "generation.json").exists())
            self.assertFalse((run / "rotation.json").exists())
            self.assertEqual(session_generation(directory), 0)

    def test_post_bump_failure_still_points_at_fresh_generation(self):
        import tempfile
        from unittest import mock
        from mizu import fs as fs_mod
        with tempfile.TemporaryDirectory() as td:
            directory, run = self._session_dir(td)
            with mock.patch.object(fs_mod, "write_json",
                                   side_effect=self._fail_on("rotation.json")):
                with self.assertRaises(OSError):
                    rotate_session(directory / "session.json", run, "test")
            # The bump already landed: the next dispatch mints its fresh
            # conversation under the new generation, never the prior one.
            self.assertEqual(session_generation(directory), 1)
            self.assertFalse((directory / "session.json").exists())


if __name__ == "__main__":
    unittest.main()
