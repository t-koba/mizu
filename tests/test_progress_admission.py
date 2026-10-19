"""Focused no-progress self-admission evidence and wake-policy tests."""
import dataclasses
import datetime as dt
import time
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.config import DEFAULT_PROGRESS_SIGNALS, load, role_no_progress_backoff_seconds
from mizu.errors import ConfigError, Denied
from mizu.explain import explain
from mizu.fs import canonical, digest, now, write_json
from mizu.runtime import (Engine, _is_observational_operation, _progress_obligations_digest,
                          _progress_record, should_run)
from mizu.snapshot import PROGRESS_SIGNALS, progress_disposition, validate_progress


class ProgressConfigTests(Fixture):
    def test_defaults_and_config_validation(self):
        limits = self.config.limits
        self.assertEqual(limits.progress_signals, DEFAULT_PROGRESS_SIGNALS)
        self.assertEqual(limits.no_progress_continuations, 0)
        self.assertEqual(limits.no_progress_recovery_after, 2)
        self.assertEqual(limits.no_progress_backoff_seconds, 0)

        raw = self.file.read_text()
        for setting, invalid in (
                ("progress_signals", '["tool", "unknown"]'),
                ("progress_signals", '["tool", "tool"]'),
                ("no_progress_continuations", "101"),
                ("no_progress_continuations", "-1"),
                ("no_progress_recovery_after", "0"),
                ("no_progress_recovery_after", "101"),
                ("no_progress_backoff_seconds", "86401"),
                ("no_progress_backoff_seconds", "-1")):
            candidate = raw.replace(
                'progress_signals = ["tool", "code", "decision", "insight", "input"] # Any selected true signal admits another continue/bounded unit. Empty disables progress qualification.',
                f"progress_signals = {invalid}" if setting == "progress_signals" else
                'progress_signals = ["tool", "code", "decision", "insight", "input"]')
            if setting == "no_progress_continuations":
                candidate = candidate.replace("no_progress_continuations = 0", f"no_progress_continuations = {invalid}")
            elif setting == "no_progress_recovery_after":
                candidate = candidate.replace("no_progress_recovery_after = 2", f"no_progress_recovery_after = {invalid}")
            elif setting == "no_progress_backoff_seconds":
                candidate = candidate.replace("no_progress_backoff_seconds = 0", f"no_progress_backoff_seconds = {invalid}")
            self.file.write_text(candidate)
            with self.assertRaises(ConfigError):
                load(self.file)
        self.file.write_text(raw)
        self.assertEqual(load(self.file).limits.progress_signals, DEFAULT_PROGRESS_SIGNALS)
        boundary = raw.replace("no_progress_continuations = 0", "no_progress_continuations = 100")
        boundary = boundary.replace("no_progress_backoff_seconds = 0", "no_progress_backoff_seconds = 86400")
        boundary = boundary.replace("no_progress_recovery_after = 2", "no_progress_recovery_after = 100")
        self.file.write_text(boundary)
        limits = load(self.file).limits
        self.file.write_text(raw)
        self.assertEqual((limits.no_progress_continuations, limits.no_progress_backoff_seconds,
                          limits.no_progress_recovery_after), (100, 86400, 100))

        empty = raw.replace(
            'progress_signals = ["tool", "code", "decision", "insight", "input"] # Any selected true signal admits another continue/bounded unit. Empty disables progress qualification.',
            "progress_signals = []")
        self.file.write_text(empty)
        self.assertEqual(load(self.file).limits.progress_signals, ())
        state_opt_in = raw.replace(
            'progress_signals = ["tool", "code", "decision", "insight", "input"] # Any selected true signal admits another continue/bounded unit. Empty disables progress qualification.',
            'progress_signals = ["state"]')
        self.file.write_text(state_opt_in)
        self.assertEqual(load(self.file).limits.progress_signals, ("state",))


class ProgressAdmissionTests(Fixture):
    def _baseline(self):
        current = self.project.snapshots.get()
        generation = self.project.insights.generation()
        base = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state=current["state"], run=None,
            outcome="wait", summary="Baseline inputs for a progress test",
            wake_at=time.time() + 3600, inbox_seen=generation,
            wake_generation=self.project.control().get("wake_generation", ""),
            parent=current["id"])
        self.project.snapshots.publish(base)
        return base

    def _record(self, *, role="worker", selected=None, count=1, signals=None, stamp=None):
        selected = list(self.config.limits.progress_signals if selected is None else selected)
        signals = {name: False for name in PROGRESS_SIGNALS} if signals is None else dict(signals)
        has_progress = any(signals[name] for name in selected)
        return validate_progress({
            "schema": 1, "signals": signals, "selected": selected, "role": role,
            "has_progress": has_progress, "no_progress_count": count,
            "obligations_digest": _progress_obligations_digest(
                self.project.insights.pending_obligations(role)),
            "recorded_at": stamp or now(),
        })

    def _parked_snapshot(self, *, role="worker", selected=None, count=1,
                         outcome="continue", stamp=None, wake_at=None):
        current = self.project.snapshots.get()
        progress = self._record(role=role, selected=selected, count=count, stamp=stamp)
        snapshot = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state=current["state"], run=None, outcome=outcome,
            summary="Progress disposition test", wake_at=wake_at,
            inbox_seen=self.project.insights.generation(),
            wake_generation=self.project.control().get("wake_generation", ""),
            progress=progress, parent=current["id"])
        self.project.snapshots.publish(snapshot)
        return self.project.snapshots.get()

    def _recorded_run(self, callback=None, *, custom_config=None, base=None):
        base = base or self._baseline()
        config = custom_config or self.config
        if custom_config is not None:
            self.project.config = custom_config
        def run(ctx, prompt, profile):
            if callback:
                callback(ctx, base)
            if ctx.finished is None:
                ctx.handle("finish", {"outcome": "continue", "summary": "Finished this unit",
                                      "state": base["state"]})
        Engine(config, driver=ScriptDriver(run)).run(self.project, "worker")
        return self.project.snapshots.get()

    def test_existing_state_reads_and_receipt_replays_are_observational(self):
        for operation in ("files", "read", "diff", "insights", "research_read", "vcs_read",
                          "command_evidence_read", "experiment_evidence_read"):
            self.assertTrue(_is_observational_operation(operation, {}), operation)
        cases = (("fetch", "receipt_id", {"url": "https://example.test/"}),
                 ("probe", "receipt_id", {"url": "https://example.test/", "method": "GET"}),
                 ("search", "result_id", {"query": "new work"}))
        for operation, argument, request in cases:
            self.assertTrue(_is_observational_operation(operation, {argument: "recorded-id"}), operation)
            self.assertFalse(_is_observational_operation(operation, request), operation)

    def test_finish_only_unchanged_run_is_recorded_as_no_progress_and_parked(self):
        snapshot = self._recorded_run()
        self.assertEqual(snapshot["outcome"], "continue")
        self.assertEqual(snapshot["progress"]["selected"], list(DEFAULT_PROGRESS_SIGNALS))
        self.assertEqual(snapshot["progress"]["signals"], {name: False for name in PROGRESS_SIGNALS})
        self.assertFalse(snapshot["progress"]["has_progress"])
        self.assertEqual(snapshot["progress"]["no_progress_count"], 1)
        self.assertFalse(should_run(self.project, snapshot, role_name="worker"))

    def test_default_progress_ignores_observations_and_free_text_state(self):
        def repeated_read(ctx, base):
            for _ in range(3):
                ctx.handle("files", {})
        observation = self._recorded_run(repeated_read)
        self.assertFalse(observation["progress"]["signals"]["tool"])
        self.assertFalse(observation["progress"]["has_progress"])
        self.assertEqual(observation["progress"]["no_progress_count"], 1)
        self.assertFalse(should_run(self.project, observation, role_name="worker"))

        def failed_tool(ctx, base):
            with self.assertRaises(Denied):
                ctx.handle("files", {"unexpected": True})
            ctx.handle("finish", {"outcome": "continue", "summary": "Tool refused",
                                   "state": base["state"]})
        failed = self._recorded_run(failed_tool)
        self.assertFalse(failed["progress"]["signals"]["tool"])

        def change_code(ctx, base):
            (ctx.workspace / "app.py").write_text("VALUE = 17\n")
            ctx.handle("finish", {"outcome": "continue", "summary": "Code changed",
                                   "state": base["state"]})
        code = self._recorded_run(change_code)
        self.assertTrue(code["progress"]["signals"]["code"])

        def finish_with_state(ctx, base, value):
            ctx.handle("finish", {"outcome": "continue", "summary": "State was reworded",
                                   "state": value})
        state = self._recorded_run(
            lambda ctx, base: finish_with_state(ctx, base, "Now: a distinct recorded state."))
        self.assertTrue(state["progress"]["signals"]["state"])
        self.assertNotIn("state", state["progress"]["selected"])
        self.assertFalse(state["progress"]["has_progress"])
        self.assertEqual(state["progress"]["no_progress_count"], 1)
        self.assertFalse(should_run(self.project, state, role_name="worker"))

        opted_in_limits = dataclasses.replace(
            self.config.limits, progress_signals=DEFAULT_PROGRESS_SIGNALS + ("state",))
        opted_in_config = dataclasses.replace(self.config, limits=opted_in_limits)
        opted_in = self._recorded_run(
            lambda ctx, base: finish_with_state(ctx, base, "Now: another paraphrase."),
            custom_config=opted_in_config)
        self.assertIn("state", opted_in["progress"]["selected"])
        self.assertTrue(opted_in["progress"]["has_progress"])

        enabled = dataclasses.replace(self.config.roles["worker"], capabilities=tuple(
            dict.fromkeys((*self.config.roles["worker"].capabilities, "submit_insight"))))
        roles = dict(self.config.roles)
        roles["worker"] = enabled
        config = dataclasses.replace(self.config, roles=roles)
        def actions(ctx, base):
            record = ctx.handle("submit_insight", {"title": "A tested action", "body": "Observed detail."})
            ctx.handle("decide", {"id": record["id"], "action": "accept", "reason": "Evidence supports it.", "rev": 1})
        action = self._recorded_run(actions, custom_config=config)
        self.assertTrue(action["progress"]["signals"]["tool"])
        self.assertTrue(action["progress"]["signals"]["decision"])
        self.assertTrue(action["progress"]["signals"]["insight"])
        self.assertEqual(action["progress"]["no_progress_count"], 0)

    def test_new_inbox_generation_offered_to_run_counts_as_input(self):
        base = self._baseline()
        self.project.insights.submit(source="editor", title="New input", body="Fresh evidence.",
                                     base_snapshot=base["id"])
        result = self._recorded_run(base=base)
        self.assertTrue(result["progress"]["signals"]["input"])

    def test_operator_wake_offered_to_run_counts_as_input(self):
        base = self._baseline()
        self.project.set_control(wake_generation="operator-woke")
        def finish(ctx, snapshot):
            ctx.handle("finish", {"outcome": "continue", "summary": "Wake handled",
                                   "state": snapshot["state"]})
        result = self._recorded_run(finish, base=base)
        self.assertTrue(result["progress"]["signals"]["input"])
        self.assertEqual(result["progress"]["no_progress_count"], 0)

    def test_native_engine_tool_progress_excludes_refusals_and_idempotent_replays(self):
        ctx = self.context()
        ctx.role = dataclasses.replace(ctx.role, engine_tools=("native",))
        args = {"name": "native", "invocation_id": "native-first"}
        with self.assertRaises(Denied):
            ctx.handle("_engine_tool", {**args, "name": "ungranted"})
        self.assertFalse(ctx.progress_tool)
        self.assertEqual(ctx.handle("_engine_tool", args), {"allowed": True})
        self.assertTrue(ctx.progress_tool)
        # Clear only the observation flag to detect any extra replay evidence.
        ctx.progress_tool = False
        self.assertEqual(ctx.handle("_engine_tool", args), {"allowed": True})
        self.assertFalse(ctx.progress_tool)
        self.assertEqual(ctx.tool_count, 1)
        ctx.handle("_engine_tool", {**args, "invocation_id": "native-next"})
        self.assertTrue(ctx.progress_tool)
        self.assertEqual(ctx.tool_count, 2)

        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], engine_tools=("native",))
        config = dataclasses.replace(self.config, roles=roles)
        snapshot = self._recorded_run(
            lambda ctx, base: ctx.handle("_engine_tool", args), custom_config=config)
        self.assertTrue(snapshot["progress"]["signals"]["tool"])
        self.assertEqual(snapshot["progress"]["no_progress_count"], 0)

    def test_persistent_obligations_and_non_due_ci_do_not_reset_streak(self):
        target = self.project.insights.submit(
            source="worker", title="Result needed", body="Evidence.",
            base_snapshot=self.project.snapshots.get()["id"])
        item = self.project.insights.submit(
            source="reviewer", title="Dependent finding", body="Evidence.",
            base_snapshot=self.project.snapshots.get()["id"])
        self.project.insights.decide(
            item["id"], "defer", "Needs result", "After decision", "test", expected_rev=1,
            wait={"kind": "dependency", "recipient": "worker", "requires": {
                "insight": target["id"], "rev": 1, "action": "accept"}})
        parked = self._parked_snapshot(count=4)
        present_ci = [{"sha": "red", "check": "offline", "run": "test"}]
        def ci_due(project, config, role, *due_args):
            return [] if due_args else present_ci
        with patch("mizu.runtime._ci_due_for", side_effect=ci_due):
            for expected in (5, 6):
                self.assertTrue(self.project.insights.pending_obligations("worker"))
                snapshot = self._recorded_run(base=parked)
                self.assertFalse(snapshot["progress"]["signals"]["input"])
                self.assertEqual(snapshot["progress"]["no_progress_count"], expected)
                self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
                parked = snapshot

    def test_new_obligation_is_input_once_even_without_inbox_generation_change(self):
        target = self.project.insights.submit(
            source="worker", title="Result needed", body="Evidence.",
            base_snapshot=self.project.snapshots.get()["id"])
        item = self.project.insights.submit(
            source="reviewer", title="Dependent finding", body="Evidence.",
            base_snapshot=self.project.snapshots.get()["id"])
        parked = self._parked_snapshot(count=4)
        generation = self.project.insights.generation()
        self.project.insights.decide(
            item["id"], "defer", "Needs result", "After decision", "test", expected_rev=1,
            wait={"kind": "dependency", "recipient": "worker", "requires": {
                "insight": target["id"], "rev": 1, "action": "accept"}})
        self.assertEqual(self.project.insights.generation(), generation)
        self.assertTrue(self.project.insights.pending_obligations("worker"))

        first = self._recorded_run(base=parked)
        self.assertTrue(first["progress"]["signals"]["input"])
        self.assertEqual(first["progress"]["no_progress_count"], 0)
        second = self._recorded_run(base=first)
        self.assertFalse(second["progress"]["signals"]["input"])
        self.assertEqual(second["progress"]["no_progress_count"], 1)
        self.assertFalse(should_run(self.project, second, role_name="worker"))

    def test_due_ci_and_consumed_wait_and_decision_events_reset_streak_once(self):
        # These are one-shot inputs, unlike pending obligation/CI visibility.
        for kind in ("ci", "wait", "decision"):
            with self.subTest(kind=kind):
                config = self.config
                if kind in ("wait", "decision"):
                    item = self.project.insights.submit(
                        source="worker", title="One-shot input", body="Evidence.",
                        base_snapshot=self.project.snapshots.get()["id"])
                    if kind == "wait":
                        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)).isoformat()
                        self.project.insights.decide(
                            item["id"], "defer", "Until deadline", "After deadline", "test",
                            expected_rev=1, wait={"kind": "deadline", "at": past})
                    else:
                        self.project.insights.decide(
                            item["id"], "reject", "Reconsider", "", "test", expected_rev=1)
                        roles = dict(self.config.roles)
                        roles["worker"] = dataclasses.replace(roles["worker"], decision_events=("reject",))
                        config = dataclasses.replace(self.config, roles=roles)
                parked = self._parked_snapshot(count=4)
                due = [{"sha": "red", "check": "offline", "run": "test"}] if kind == "ci" else []
                with patch("mizu.runtime._ci_due_for", return_value=due):
                    result = self._recorded_run(base=parked, custom_config=config)
                self.assertTrue(result["progress"]["signals"]["input"])
                self.assertEqual(result["progress"]["no_progress_count"], 0)
                self.assertEqual(self.project.insights.due_waits("worker", result), [])
                self.assertEqual(self.project.insights.decision_events("worker", ("reject",)), [])
                with patch("mizu.runtime._ci_due_for", return_value=[]):
                    repeated = self._recorded_run(base=result, custom_config=config)
                self.assertFalse(repeated["progress"]["signals"]["input"])
                self.assertEqual(repeated["progress"]["no_progress_count"], 1)

    def test_explicit_wakes_dispatch_on_change_writer_with_unchanged_code(self):
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], on_change=True)
        config = dataclasses.replace(self.config, roles=roles)
        self.project.config = config
        for kind in ("operator", "inbox", "goal", "elapsed_wait"):
            with self.subTest(kind=kind):
                parked = self._parked_snapshot(
                    count=4, outcome="wait" if kind == "elapsed_wait" else "continue",
                    wake_at=time.time() - 10 if kind == "elapsed_wait" else None)
                write_json(self.project.root / "observed" / "worker.json",
                           {"code_digest": parked["code_digest"]})
                if kind == "operator":
                    self.project.set_control(wake_generation="new-operator-wake")
                elif kind == "inbox":
                    self.project.insights.submit(
                        source="editor", title="Fresh inbox", body="Evidence.",
                        base_snapshot=parked["id"])
                elif kind == "goal":
                    (self.project.root / "PROJECT.md").write_text("A changed objective.\n")
                self.assertTrue(should_run(self.project, parked, role_name="worker"))
                calls = []
                def finish(ctx, prompt, profile):
                    calls.append(True)
                    ctx.handle("finish", {"outcome": "continue", "summary": "Wake consumed",
                                          "state": ctx.snapshot["state"]})
                engine = Engine(config, driver=ScriptDriver(finish))
                # Explicit file-trigger dispatch still requires a due event.
                self.assertEqual(engine.run(self.project, "worker", event_trigger=True)["skipped"], "no-event")
                result = engine.run(self.project, "worker")
                self.assertEqual(result["status"], "completed")
                self.assertEqual(calls, [True])
                snapshot = self.project.snapshots.get()
                self.assertEqual(snapshot["code_digest"], parked["code_digest"])
                if kind != "goal":
                    self.assertTrue(snapshot["progress"]["signals"]["input"])
                    self.assertEqual(snapshot["progress"]["no_progress_count"], 0)
                    # The fresh input permits one follow-up, but cannot loop.
                    self.assertEqual(engine.run(self.project, "worker")["status"], "completed")
                    snapshot = self.project.snapshots.get()
                    self.assertFalse(snapshot["progress"]["signals"]["input"])
                    self.assertEqual(snapshot["progress"]["no_progress_count"], 1)
                self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
                self.assertEqual(engine.run(self.project, "worker")["skipped"], "unchanged")

    def test_streak_increments_and_resets_on_selected_progress_role_or_policy_change(self):
        selected = ("code",)
        no_progress = {name: False for name in PROGRESS_SIGNALS}
        previous = {"progress": self._record(role="worker", selected=selected, count=5)}
        first = _progress_record(previous, "worker", selected, no_progress, "continue")
        second = _progress_record({"progress": first}, "worker", selected, no_progress, "bounded")
        self.assertEqual((first["no_progress_count"], second["no_progress_count"]), (6, 7))
        with_code = {**no_progress, "code": True}
        reset = _progress_record({"progress": second}, "worker", selected, with_code, "continue")
        self.assertEqual(reset["no_progress_count"], 0)
        role_reset = _progress_record({"progress": second}, "searcher", selected, no_progress, "continue")
        policy_reset = _progress_record({"progress": second}, "worker", ("state",), no_progress, "continue")
        self.assertEqual(role_reset["no_progress_count"], 1)
        self.assertEqual(policy_reset["no_progress_count"], 1)
        empty_policy = _progress_record({"progress": second}, "worker", (),
                                        {**no_progress, "tool": True}, "continue")
        self.assertFalse(empty_policy["has_progress"])
        self.assertEqual(empty_policy["no_progress_count"], 1)
        carried = _progress_record({"progress": second}, "worker", selected, no_progress, "wait")
        self.assertEqual(carried["no_progress_count"], 7)

    def test_allowance_backoff_policy_reassessment_and_role_scope(self):
        parked = self._parked_snapshot(count=1)
        self.assertFalse(should_run(self.project, parked, role_name="worker"))
        self.assertTrue(should_run(self.project, parked, role_name="searcher"))
        legacy = {key: value for key, value in parked.items() if key != "progress"}
        self.assertTrue(should_run(self.project, legacy, role_name="worker"))

        allowance_config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, no_progress_continuations=1))
        self.assertTrue(should_run(self.project, parked, role_name="worker", config=allowance_config))
        policy_config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, progress_signals=("code",)))
        self.assertTrue(should_run(self.project, parked, role_name="worker", config=policy_config))

        stamp = now()
        backoff_snapshot = self._parked_snapshot(count=1, stamp=stamp)
        backoff_config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, no_progress_backoff_seconds=60))
        epoch = dt.datetime.fromisoformat(stamp).timestamp()
        self.assertFalse(should_run(self.project, backoff_snapshot, current_time=epoch + 59,
                                    role_name="worker", config=backoff_config))
        self.assertTrue(should_run(self.project, backoff_snapshot, current_time=epoch + 61,
                                   role_name="worker", config=backoff_config))

    def test_configured_backoff_dispatches_on_change_role(self):
        old_stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=120)).isoformat(timespec="microseconds")
        parked = self._parked_snapshot(count=1, stamp=old_stamp)
        limits = dataclasses.replace(self.config.limits, no_progress_backoff_seconds=60)
        role = dataclasses.replace(self.config.roles["worker"], on_change=True)
        roles = dict(self.config.roles)
        roles["worker"] = role
        config = dataclasses.replace(self.config, limits=limits, roles=roles)
        self.project.config = config
        observed = self.project.root / "observed" / "worker.json"
        write_json(observed, {"code_digest": parked["code_digest"]})
        calls = []
        def finish(ctx, prompt, profile):
            calls.append(True)
            ctx.handle("finish", {"outcome": "continue", "summary": "Backoff elapsed",
                                   "state": ctx.snapshot["state"]})
        result = Engine(config, driver=ScriptDriver(finish)).run(self.project, "worker")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "completed")

    def test_writer_progress_does_not_self_admit_read_only_on_change_role(self):
        engine = Engine(self.config, driver=ScriptDriver())
        self.assertEqual(engine.run(self.project, "reviewer")["status"], "completed")
        # A writer's record is not a read-only role's continuation request.
        for count in (0, 1):
            with self.subTest(count=count):
                self._parked_snapshot(count=count)
                self.assertEqual(engine.run(self.project, "reviewer")["skipped"], "unchanged")

    def test_external_wakes_and_wait_expiry_survive_parking(self):
        parked = self._parked_snapshot(count=1)
        self.project.set_control(wake_generation="a-new-operator-generation")
        self.assertTrue(should_run(self.project, parked, role_name="worker"))
        self.project.set_control(wake_generation=parked["wake_generation"])

        with patch.object(self.project.insights, "generation", return_value="new-inbox-generation"):
            self.assertTrue(should_run(self.project, parked, role_name="worker"))
        with patch("mizu.runtime._role_events_due", return_value=True):
            self.assertTrue(should_run(self.project, parked, role_name="worker"))
        with patch("mizu.runtime._ci_due_for", return_value=[{"sha": "red"}]):
            self.assertTrue(should_run(self.project, parked, role_name="worker"))

        wait = self._parked_snapshot(count=1, outcome="wait", wake_at=100)
        self.assertTrue(should_run(self.project, wait, current_time=101, role_name="worker"))

    def test_no_progress_recovery_warning_is_visible_without_self_admitting(self):
        parked = self._parked_snapshot(count=2)
        self.assertFalse(should_run(self.project, parked, role_name="worker"))
        status = self.project.status()
        self.assertEqual(status["progress"]["recovery"], {
            "status": "due", "threshold_due": True, "pending_work": True,
            "session_scope": "persistent", "no_progress_count": 2, "after": 2})

    def test_ephemeral_recovery_status_separates_threshold_from_rotation(self):
        from mizu.project import Project
        self._parked_snapshot(count=2)
        role = self.config.roles["worker"]
        profiles = dict(self.config.profiles)
        profiles[role.profile] = {**profiles[role.profile], "session": "ephemeral"}
        config = dataclasses.replace(self.config, profiles=profiles)
        status = Project(config, self.project.name).status()
        self.assertEqual(status["progress"]["recovery"], {
            "status": "not_applicable", "threshold_due": True, "pending_work": True,
            "session_scope": "ephemeral", "no_progress_count": 2, "after": 2})

    def test_selector_recovery_scope_is_conditional_when_profiles_mixed(self):
        from mizu.project import Project, session_persistence_scope
        role = self.config.roles["worker"]
        profiles = dict(self.config.profiles)
        alternate = next(name for name in profiles if name != role.profile)
        profiles[role.profile] = {**profiles[role.profile], "session": "persistent"}
        profiles[alternate] = {**profiles[alternate], "session": "ephemeral"}
        selector = {"rules": [{"candidates": [{"profile": role.profile},
                                                {"profile": alternate}]}]}
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(role, profile="", selector="mixed")
        config = dataclasses.replace(self.config, profiles=profiles, roles=roles,
                                     selectors={**self.config.selectors, "mixed": selector})
        self.assertEqual(session_persistence_scope(config, "worker"), "conditional")
        self.assertEqual(session_persistence_scope(config, "missing"), "unknown")
        self._parked_snapshot(count=2)
        status = Project(config, self.project.name).status()
        self.assertEqual(status["progress"]["recovery"]["status"], "conditional")
        self.assertTrue(status["progress"]["recovery"]["threshold_due"])

    def test_malformed_progress_fails_closed_and_visibility_is_bounded(self):
        parked = self._parked_snapshot(count=1)
        invalid = dict(parked)
        invalid["progress"] = {**parked["progress"], "has_progress": True}
        body = {key: value for key, value in invalid.items() if key != "id"}
        invalid_id = digest(canonical(body))
        invalid["id"] = invalid_id
        write_json(self.project.root / "snapshots" / f"{invalid_id}.json", invalid, exclusive=True)
        with self.assertRaises(Denied):
            self.project.snapshots.get(invalid_id)
        self.assertEqual(progress_disposition(parked["progress"], "continue", self.config, "worker"),
                         "parked_no_progress")
        malformed = {**parked, "progress": {**parked["progress"], "has_progress": True}}
        self.assertFalse(should_run(self.project, malformed, role_name="worker"))

        status = self.project.status()
        self.assertEqual(status["progress"]["disposition"], "parked_no_progress")
        self.assertEqual(status["progress"]["record"]["no_progress_count"], 1)
        report = explain(self.config, self.project, "worker")
        projected = report["project_facts"]["snapshot"]["record"]
        self.assertEqual(projected["progress_disposition"], "parked_no_progress")
        self.assertEqual(projected["progress"]["role"], "worker")
        self.assertEqual(set(projected["progress"]["signals"]), set(PROGRESS_SIGNALS))


    def test_per_role_backoff_default_inherits_project_limit(self):
        self.assertEqual(self.config.limits.no_progress_backoff_seconds, 0)
        for name in ("worker", "searcher"):
            with self.subTest(role=name):
                self.assertIsNone(self.config.roles[name].no_progress_backoff_seconds)
                self.assertEqual(role_no_progress_backoff_seconds(self.config, self.config.roles[name]), 0)

    def test_per_role_backoff_rejects_out_of_range(self):
        original = self.file.read_text()
        for value in ("-1", "86401", '"60"', "1.5", "true"):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    "[roles.worker]", f"[roles.worker]\nno_progress_backoff_seconds = {value}"))
                with self.assertRaises(ConfigError):
                    load(self.file)
        self.file.write_text(original)

    def test_per_role_backoff_bounds_inherit_or_override(self):
        original = self.file.read_text()
        for value, expected in ((None, 0), (0, 0), (60, 60), (86400, 86400)):
            with self.subTest(value=value):
                text = original
                if value is not None:
                    text = text.replace(
                        "[roles.worker]", f"[roles.worker]\nno_progress_backoff_seconds = {value}")
                self.file.write_text(text)
                config = load(self.file)
                self.assertEqual(role_no_progress_backoff_seconds(config, config.roles["worker"]), expected)
                # Unset roles keep the project default.
                self.assertEqual(role_no_progress_backoff_seconds(config, config.roles["searcher"]),
                                 config.limits.no_progress_backoff_seconds)
                self.file.write_text(original)

    def test_per_role_timer_expires_independently(self):
        stamp = now()
        epoch = dt.datetime.fromisoformat(stamp).timestamp()
        worker_record = self._record(role="worker", count=1, stamp=stamp)
        searcher_record = self._record(role="searcher", count=1, stamp=stamp)
        limits = dataclasses.replace(self.config.limits, no_progress_backoff_seconds=3600)
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], no_progress_backoff_seconds=60)
        config = dataclasses.replace(self.config, limits=limits, roles=roles)
        # One role's short timer expires while the inheriting role stays parked.
        self.assertEqual(progress_disposition(worker_record, "continue", config, "worker", epoch + 61),
                         "self_admit_backoff")
        self.assertEqual(progress_disposition(searcher_record, "continue", config, "searcher", epoch + 61),
                         "parked_no_progress")
        self.assertEqual(progress_disposition(searcher_record, "continue", config, "searcher", epoch + 3601),
                         "self_admit_backoff")
        parked = self._parked_snapshot(count=1, stamp=stamp)
        self.assertTrue(should_run(self.project, parked, current_time=epoch + 61,
                                   role_name="worker", config=config))
        # Disabling the role timer keeps it parked after the project timer elapses.
        roles["worker"] = dataclasses.replace(roles["worker"], no_progress_backoff_seconds=0)
        disabled = dataclasses.replace(self.config, limits=limits, roles=roles)
        self.assertEqual(progress_disposition(worker_record, "continue", disabled, "worker", epoch + 7200),
                         "parked_no_progress")


if __name__ == "__main__":
    import unittest
    unittest.main()
