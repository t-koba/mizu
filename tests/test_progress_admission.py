"""Focused no-progress self-admission evidence and wake-policy tests."""
import dataclasses
import datetime as dt
import time

from support import Fixture, ScriptDriver, delivered_ack_tokens
from mizu.config import DEFAULT_PROGRESS_SIGNALS, load, role_no_progress_backoff_seconds, role_progress_signals
from mizu.errors import ConfigError, Denied
from mizu.explain import explain
from mizu.fs import canonical, digest, now, read_json, write_json
from mizu.runtime import (Engine, _is_observational_operation, _operator_woke,
                          _progress_record, _writer_next_task, should_run)
from mizu.snapshot import PROGRESS_SIGNALS, no_progress_recovery_due, progress_disposition, validate_next_step, validate_progress


class ProgressConfigTests(Fixture):
    def test_defaults_and_config_validation(self):
        limits = self.config.limits
        self.assertEqual(limits.progress_signals, DEFAULT_PROGRESS_SIGNALS)
        self.assertEqual(limits.no_progress_continuations, 0)
        self.assertEqual(limits.no_progress_recovery_after, 2)
        self.assertEqual(limits.no_progress_backoff_seconds, 0)
        self.assertEqual(limits.writer_no_progress_backoff_seconds, 300)

        raw = self.file.read_text()
        for setting, invalid in (
                ("progress_signals", '["tool", "unknown"]'),
                ("progress_signals", '["tool", "tool"]'),
                ("no_progress_continuations", "101"),
                ("no_progress_continuations", "-1"),
                ("no_progress_recovery_after", "0"),
                ("no_progress_recovery_after", "101"),
                ("no_progress_backoff_seconds", "86401"),
                ("no_progress_backoff_seconds", "-1"),
                ("writer_no_progress_backoff_seconds", "86401"),
                ("writer_no_progress_backoff_seconds", "-1")):
            candidate = raw
            if setting == "progress_signals":
                candidate = candidate.replace(
                    'progress_signals = ["tool", "code", "message", "input"] # Signals qualify immediate progress; writer handoff also needs a new next_step. Empty disables qualification.',
                    f"progress_signals = {invalid}")
            if setting == "no_progress_continuations":
                candidate = candidate.replace("no_progress_continuations = 0", f"no_progress_continuations = {invalid}")
            elif setting == "no_progress_recovery_after":
                candidate = candidate.replace("no_progress_recovery_after = 2", f"no_progress_recovery_after = {invalid}")
            elif setting == "no_progress_backoff_seconds":
                candidate = candidate.replace("no_progress_backoff_seconds = 0", f"no_progress_backoff_seconds = {invalid}")
            elif setting == "writer_no_progress_backoff_seconds":
                candidate = candidate.replace("writer_no_progress_backoff_seconds = 300",
                                              f"writer_no_progress_backoff_seconds = {invalid}")
            self.file.write_text(candidate)
            with self.assertRaises(ConfigError):
                load(self.file)
        self.file.write_text(raw)
        self.assertEqual(load(self.file).limits.progress_signals, DEFAULT_PROGRESS_SIGNALS)
        boundary = raw.replace("no_progress_continuations = 0", "no_progress_continuations = 100")
        boundary = boundary.replace("no_progress_backoff_seconds = 0", "no_progress_backoff_seconds = 86400")
        boundary = boundary.replace("writer_no_progress_backoff_seconds = 300",
                                    "writer_no_progress_backoff_seconds = 86400")
        boundary = boundary.replace("no_progress_recovery_after = 2", "no_progress_recovery_after = 100")
        self.file.write_text(boundary)
        limits = load(self.file).limits
        self.file.write_text(raw)
        self.assertEqual((limits.no_progress_continuations, limits.no_progress_backoff_seconds,
                          limits.writer_no_progress_backoff_seconds,
                          limits.no_progress_recovery_after), (100, 86400, 86400, 100))

        empty = raw.replace(
            'progress_signals = ["tool", "code", "message", "input"] # Signals qualify immediate progress; writer handoff also needs a new next_step. Empty disables qualification.',
            "progress_signals = []")
        self.file.write_text(empty)
        self.assertEqual(load(self.file).limits.progress_signals, ())
        state_opt_in = raw.replace(
            'progress_signals = ["tool", "code", "message", "input"] # Signals qualify immediate progress; writer handoff also needs a new next_step. Empty disables qualification.',
            'progress_signals = ["state"]')
        self.file.write_text(state_opt_in)
        self.assertEqual(load(self.file).limits.progress_signals, ("state",))


class ProgressAdmissionTests(Fixture):
    def _baseline(self):
        current = self.project.snapshots.get()
        base = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state="Now: Baseline inputs\nNext: Continue the progress experiment", run=None,
            outcome="wait", summary="Baseline inputs for a progress test",
            wake_at=time.time() + 3600,
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
            "obligations_digest": digest(canonical([])),
            "recorded_at": stamp or now(),
        })

    def _parked_snapshot(self, *, role="worker", selected=None, count=1,
                         outcome="continue", stamp=None, wake_at=None):
        current = self.project.snapshots.get()
        progress = self._record(role=role, selected=selected, count=count, stamp=stamp)
        snapshot = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state="Now: Baseline inputs\nNext: Continue the progress experiment", run=None, outcome=outcome,
            summary="Progress disposition test", wake_at=wake_at,
            wake_generation=self.project.control().get("wake_generation", ""),
            progress=progress, parent=current["id"])
        self.project.snapshots.publish(snapshot)
        return self.project.snapshots.get()

    def _recorded_run(self, callback=None, *, custom_config=None, base=None,
                      ack_decisions=False):
        base = base or self._baseline()
        config = custom_config or self.config
        if custom_config is not None:
            self.project.config = custom_config
        def run(ctx, prompt, profile):
            if callback:
                callback(ctx, base)
            if ctx.finished is None:
                args = {"outcome": "continue", "summary": "Finished this unit",
                        "next_step": 'Continue the progress experiment', "state": base["state"]}
                if ack_decisions:
                    args["ack_events"] = delivered_ack_tokens(prompt)
                ctx.handle("finish", args)
        Engine(config, driver=ScriptDriver(run)).run(self.project, "worker")
        return self.project.snapshots.get()

    def test_existing_state_reads_and_receipt_replays_are_observational(self):
        for operation in ("files", "read", "diff", "research_read", "vcs_read",
                          "command_evidence_read", "experiment_evidence_read",
                          "queue_read", "message_read", "message_refs"):
            self.assertTrue(_is_observational_operation(operation, {}), operation)
        self.assertFalse(_is_observational_operation("queue_write", {}), "queue_write")
        for action in ("list", "logs", "result"):
            self.assertTrue(_is_observational_operation("check_job", {"action": action}))
        cases = (("fetch", "receipt_id", {"url": "https://example.test/"}),
                 ("search", "result_id", {"query": "new work"}))
        for operation, argument, request in cases:
            self.assertTrue(_is_observational_operation(operation, {argument: "recorded-id"}), operation)
            self.assertFalse(_is_observational_operation(operation, request), operation)

    def test_next_declaration_is_bounded_unambiguous_and_requires_work(self):
        self.assertEqual(validate_next_step('Add the test.'), 'Add the test.')
        self.assertEqual(validate_next_step('x' * 4096), 'x' * 4096)
        for step in (None, 1, '', 'none', 'Nothing.', 'N/A...', ' padded', 'padded ',
                     'first\nsecond', 'tab\there', 'x' * 4097):
            with self.subTest(step=str(step)[:30]):
                with self.assertRaises(Denied):
                    validate_next_step(step)
        def work(ctx, _):
            (ctx.workspace / 'app.py').write_text('VALUE = 42\n')
            ctx.handle('finish', {'outcome': 'continue', 'summary': 'Worked',
                                  "next_step": 'Add the test.', 'state': 'Now: worked.\nNext: Add the test.'})
        snapshot = self._recorded_run(work)
        self.assertEqual(_writer_next_task(self.project, snapshot, 'worker'), 'Add the test.')
        self.assertIsNone(_writer_next_task(self.project, snapshot, 'reviewer'))
        for outcome in ('wait', 'blocked', 'done'):
            self.assertIsNone(_writer_next_task(self.project, {**snapshot, 'outcome': outcome}, 'worker'))
        missing_parent = {**snapshot, 'parent': '0' * 64}
        self.assertIsNone(_writer_next_task(self.project, missing_parent, 'worker'))

    def test_typed_step_is_not_inferred_or_compared_from_prose(self):
        def work(ctx, _):
            (ctx.workspace / 'app.py').write_text('VALUE = 42\n')
            ctx.handle('finish', {'outcome': 'continue', 'summary': 'Worked',
                                  'state': 'No prose declaration', 'next_step': 'Add the test.'})
        snapshot = self._recorded_run(work)
        legacy = {k: v for k, v in snapshot.items() if k != 'next_step'}
        legacy['state'] = 'Next: Add the test.'
        self.assertIsNone(_writer_next_task(self.project, legacy, 'worker'))
        candidate = {**snapshot, 'parent': snapshot['id'], 'state': 'Different prose'}
        self.assertIsNone(_writer_next_task(self.project, candidate, 'worker'))
        candidate.update(next_step='Run the test.', state=snapshot['state'])
        self.assertEqual(_writer_next_task(self.project, candidate, 'worker'), 'Run the test.')
        legacy_parent = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state='Next: Add the test.', run=None,
            outcome='continue', summary='Legacy snapshot')
        candidate.update(parent=legacy_parent['id'], next_step='Add the test.')
        self.assertEqual(_writer_next_task(self.project, candidate, 'worker'), 'Add the test.')

    def test_next_handoff_uses_effective_configured_progress_signals(self):
        base = self._baseline()
        state = "Now: worked.\nNext: Add the test."
        parent = base["id"]
        empty_signals = {name: False for name in PROGRESS_SIGNALS}

        def candidate(selected, signal):
            signals = {**empty_signals, signal: True}
            return {
                "outcome": "continue",
                "state": state, "next_step": "Add the test.",
                "parent": parent,
                "progress": self._record(
                    role="worker", selected=list(selected),
                    count=0 if signal in selected else 1, signals=signals),
            }

        def with_signals(signals):
            roles = dict(self.config.roles)
            roles["worker"] = dataclasses.replace(roles["worker"], progress_signals=signals)
            return dataclasses.replace(self.config, roles=roles)

        original = self.project.config
        try:
            self.project.config = self.config
            # Input and state remain progress facts, but the public Next contract
            # requires one of its selected work signals.
            input_only = candidate(self.config.limits.progress_signals, "input")
            self.assertIsNone(_writer_next_task(self.project, input_only, "worker"))

            self.project.config = with_signals(("state",))
            state_only = candidate(("state",), "state")
            self.assertIsNone(_writer_next_task(self.project, state_only, "worker"))

            self.project.config = with_signals(("tool",))
            tool_only = candidate(("tool",), "tool")
            self.assertEqual(_writer_next_task(self.project, tool_only, "worker"), "Add the test.")

            self.project.config = with_signals(())
            disabled = candidate((), "input")
            self.assertIsNone(_writer_next_task(self.project, disabled, "worker"))

            self.project.config = with_signals(("tool",))
            unselected = candidate(("tool",), "input")
            self.assertIsNone(_writer_next_task(self.project, unselected, "worker"))
        finally:
            self.project.config = original

    def test_state_only_next_does_not_create_handoff(self):
        base = self._baseline()
        state = 'Now: only declaring a follow-up.\nNext: Add the test.'
        self.assertNotEqual(state, base["state"])

        def declare_only(ctx, _):
            ctx.handle("finish", {"outcome": "continue", "summary": "Declared a step without progress",
                                   "next_step": "Add the test.", "state": state})

        snapshot = self._recorded_run(declare_only, base=base)
        self.assertTrue(snapshot["progress"]["signals"]["state"])
        self.assertNotIn("state", snapshot["progress"]["selected"])
        self.assertFalse(snapshot["progress"]["has_progress"])
        self.assertEqual(snapshot["progress"]["no_progress_count"], 1)
        self.assertIsNone(_writer_next_task(self.project, snapshot, "worker"))
        self.assertFalse(should_run(self.project, snapshot, role_name="worker"))

    def test_finish_only_unchanged_run_is_recorded_as_no_progress_and_parked(self):
        snapshot = self._recorded_run()
        self.assertEqual(snapshot["outcome"], "continue")
        self.assertEqual(snapshot["progress"]["selected"], list(DEFAULT_PROGRESS_SIGNALS))
        self.assertEqual(snapshot["progress"]["signals"], {name: False for name in PROGRESS_SIGNALS})
        self.assertFalse(snapshot["progress"]["has_progress"])
        self.assertEqual(snapshot["progress"]["no_progress_count"], 1)
        self.assertFalse(should_run(self.project, snapshot, role_name="worker"))

    def test_continue_without_qualifying_signal_parks_without_a_deadline(self):
        snapshot = self._recorded_run()
        self.assertEqual(progress_disposition(snapshot["progress"], "continue", self.config, "worker"),
                         "parked_no_progress")
        self.assertIsNone(snapshot["wake_at"])

    def test_operator_wake_offered_to_run_counts_as_input(self):
        base = self._baseline()
        self.project.set_control(wake_generation="operator-woke")
        def finish(ctx, snapshot):
            ctx.handle("finish", {"outcome": "continue", "summary": "Wake handled",
                                   "next_step": 'Continue the progress experiment', "state": snapshot["state"]})
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

    def _publish_progress(self, progress, outcome):
        current = self.project.snapshots.get()
        snapshot = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace), goal=self.project.goal,
            state="Now: unchanged input.\nNext: Inspect the result", run=None, outcome=outcome,
            summary="Counted no-progress finish", wake_at=4102444800,
            wake_generation=self.project.control().get("wake_generation", ""),
            progress=progress, parent=current["id"])
        self.project.snapshots.publish(snapshot)
        return self.project.snapshots.get()

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
        self.assertEqual(carried["no_progress_count"], 8)
        blocked = _progress_record({"progress": carried}, "worker", selected, no_progress, "blocked")
        self.assertEqual(blocked["no_progress_count"], 9)

    def test_allowance_backoff_policy_reassessment_and_role_scope(self):
        parked = self._parked_snapshot(count=1)
        self.assertFalse(should_run(self.project, parked, role_name="worker"))
        self.assertTrue(should_run(self.project, parked, role_name="searcher"))
        legacy = {key: value for key, value in parked.items() if key != "progress"}
        self.assertFalse(should_run(self.project, legacy, role_name="worker"))

        allowance_config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, no_progress_continuations=1))
        self.assertFalse(should_run(self.project, parked, role_name="worker", config=allowance_config))
        policy_config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, progress_signals=("code",)))
        self.assertFalse(should_run(self.project, parked, role_name="worker", config=policy_config))

        stamp = now()
        backoff_snapshot = self._parked_snapshot(count=1, stamp=stamp)
        backoff_config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, no_progress_backoff_seconds=60))
        epoch = dt.datetime.fromisoformat(stamp).timestamp()
        self.assertFalse(should_run(self.project, backoff_snapshot, current_time=epoch + 59,
                                    role_name="worker", config=backoff_config))
        self.assertFalse(should_run(self.project, backoff_snapshot, current_time=epoch + 61,
                                   role_name="worker", config=backoff_config))

    def test_configured_progress_outcomes_gate_daemon_readiness(self):
        parked = self._parked_snapshot(count=1, outcome="continue")
        self.assertTrue(should_run(self.project, parked))
        narrowed = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, progress_outcomes=("wait",)))
        self.assertFalse(should_run(self.project, parked, config=narrowed))

    def test_configured_backoff_does_not_dispatch_empty_writer(self):
        old_stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=120)).isoformat(timespec="microseconds")
        parked = self._parked_snapshot(count=1, stamp=old_stamp)
        limits = dataclasses.replace(self.config.limits, writer_no_progress_backoff_seconds=60)
        role = dataclasses.replace(self.config.roles["worker"], on_change=True)
        roles = dict(self.config.roles)
        roles["worker"] = role
        config = dataclasses.replace(self.config, limits=limits, roles=roles)
        self.project.config = config
        self.assertFalse(should_run(self.project, parked, current_time=dt.datetime.fromisoformat(old_stamp).timestamp() + 120,
                                    role_name="worker", config=config))
        observed = self.project.root / "observed" / "worker.json"
        write_json(observed, {"code_digest": parked["code_digest"]})
        calls = []
        def finish(ctx, prompt, profile):
            calls.append(True)
            ctx.handle("finish", {"outcome": "continue", "summary": "Backoff elapsed",
                                   "next_step": 'Continue the progress experiment', "state": ctx.snapshot["state"]})
        result = Engine(config, driver=ScriptDriver(finish)).run(self.project, "worker")
        self.assertEqual(calls, [])
        self.assertEqual(result["skipped"], "unchanged")

    def test_writer_progress_does_not_self_admit_read_only_on_change_role(self):
        engine = Engine(self.config, driver=ScriptDriver())
        self.assertEqual(engine.run(self.project, "reviewer")["status"], "completed")
        # A writer's record is not a read-only role's continuation request.
        for count in (0, 1):
            with self.subTest(count=count):
                self._parked_snapshot(count=count)
                self.assertEqual(engine.run(self.project, "reviewer")["skipped"], "unchanged")

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
        self.assertEqual(self.config.limits.writer_no_progress_backoff_seconds, 300)
        for name, expected in (("worker", 300), ("searcher", 0)):
            with self.subTest(role=name):
                self.assertIsNone(self.config.roles[name].no_progress_backoff_seconds)
                self.assertEqual(role_no_progress_backoff_seconds(self.config, self.config.roles[name]), expected)

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
        for value, expected in ((None, 300), (0, 0), (60, 60), (86400, 86400)):
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
        # Writer timers no longer admit no-progress continuations; the read-only
        # inheriting role uses its configured delay.
        self.assertEqual(progress_disposition(worker_record, "continue", config, "worker", epoch + 61),
                         "parked_no_progress")
        self.assertEqual(progress_disposition(searcher_record, "continue", config, "searcher", epoch + 61),
                         "parked_no_progress")
        self.assertEqual(progress_disposition(searcher_record, "continue", config, "searcher", epoch + 3601),
                         "self_admit_backoff")
        parked = self._parked_snapshot(count=1, stamp=stamp)
        self.assertFalse(should_run(self.project, parked, current_time=epoch + 61,
                                   role_name="worker", config=config))
        # Disabling the role timer keeps it parked after the project timer elapses.
        roles["worker"] = dataclasses.replace(roles["worker"], no_progress_backoff_seconds=0)
        disabled = dataclasses.replace(self.config, limits=limits, roles=roles)
        self.assertEqual(progress_disposition(worker_record, "continue", disabled, "worker", epoch + 7200),
                         "parked_no_progress")


class PerRoleProgressSignalsTests(Fixture):
    def _baseline(self):
        current = self.project.snapshots.get()
        base = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state="Now: Baseline inputs\nNext: Continue the progress experiment", run=None,
            outcome="wait", summary="Baseline inputs for a progress test",
            wake_at=time.time() + 3600,
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
            "obligations_digest": digest(canonical([])),
            "recorded_at": stamp or now(),
        })

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
                                      "next_step": 'Continue the progress experiment', "state": base["state"]})
        Engine(config, driver=ScriptDriver(run)).run(self.project, "worker")
        return self.project.snapshots.get()

    def _with_role_signals(self, role_name, signals):
        roles = dict(self.config.roles)
        roles[role_name] = dataclasses.replace(roles[role_name], progress_signals=signals)
        return dataclasses.replace(self.config, roles=roles)

    def test_inherits_project_list_by_default(self):
        for name in ("worker", "searcher"):
            with self.subTest(role=name):
                self.assertIsNone(self.config.roles[name].progress_signals)
                self.assertEqual(role_progress_signals(self.config, self.config.roles[name]),
                                 tuple(self.config.limits.progress_signals))
                self.assertEqual(role_progress_signals(self.config, self.config.roles[name]),
                                 DEFAULT_PROGRESS_SIGNALS)

    def test_nonempty_override_including_state(self):
        config = self._with_role_signals("worker", ("state",))
        self.assertEqual(role_progress_signals(config, config.roles["worker"]), ("state",))
        # Unset roles keep the project default.
        self.assertEqual(role_progress_signals(config, config.roles["searcher"]),
                         tuple(config.limits.progress_signals))
        state_only = self._record(role="worker", selected=("state",), count=0,
                                  signals={**{n: False for n in PROGRESS_SIGNALS}, "state": True})
        self.assertTrue(state_only["has_progress"])
        self.assertEqual(state_only["no_progress_count"], 0)
        tool_only = self._record(role="worker", selected=("state",),
                                 signals={**{n: False for n in PROGRESS_SIGNALS}, "tool": True})
        self.assertFalse(tool_only["has_progress"])
        self.assertEqual(progress_disposition(state_only, "continue", config, "worker"),
                         "writer_requires_work_signal")
        parked = self._record(role="worker", selected=("state",), count=1,
                              signals={**{n: False for n in PROGRESS_SIGNALS}, "tool": True})
        self.assertFalse(parked["has_progress"])
        self.assertEqual(progress_disposition(parked, "continue", config, "worker"),
                         "parked_no_progress")

    def test_empty_override_disables_qualification(self):
        config = self._with_role_signals("worker", ())
        self.assertEqual(role_progress_signals(config, config.roles["worker"]), ())
        crowded = self._record(role="worker", selected=(), count=1,
                               signals={n: True for n in PROGRESS_SIGNALS})
        self.assertFalse(crowded["has_progress"])
        self.assertEqual(progress_disposition(crowded, "continue", config, "worker"),
                         "parked_no_progress")
        mismatched_empty = self._record(role="worker", selected=(), count=2)
        self.assertFalse(no_progress_recovery_due(mismatched_empty, "continue", self.config))
        due = validate_progress({**self._record(role="worker", selected=(), count=2),
                                 "no_progress_count": 2})
        self.assertTrue(no_progress_recovery_due(due, "continue", config))
        snapshot = self._recorded_run(custom_config=config)
        self.assertEqual(snapshot["progress"]["selected"], [])
        self.assertFalse(snapshot["progress"]["has_progress"])

    def test_role_values_reject_unknown_and_duplicates(self):
        original = self.file.read_text()
        for value in ('["tool", "unknown"]', '["tool", "tool"]', '"tool"', '[123]'):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    "[roles.worker]", f"[roles.worker]\nprogress_signals = {value}"))
                with self.assertRaises(ConfigError):
                    load(self.file)
        self.file.write_text(original)
        empty = original.replace("[roles.worker]", "[roles.worker]\nprogress_signals = []")
        self.file.write_text(empty)
        self.assertEqual(load(self.file).roles["worker"].progress_signals, ())
        self.file.write_text(original.replace(
            "[roles.worker]", '[roles.worker]\nprogress_signals = ["state"]'))
        self.assertEqual(load(self.file).roles["worker"].progress_signals, ("state",))
        self.file.write_text(original)

    def test_disposition_and_recovery_follow_effective_list(self):
        config = self._with_role_signals("worker", ("state",))
        # Same record content parks under the override but progresses by default.
        tool_only_override = self._record(role="worker", selected=("state",),
                                          signals={**{n: False for n in PROGRESS_SIGNALS}, "tool": True})
        self.assertEqual(progress_disposition(tool_only_override, "continue", config, "worker"),
                         "parked_no_progress")
        tool_only_default = self._record(role="worker", selected=list(DEFAULT_PROGRESS_SIGNALS), count=0,
                                         signals={**{n: False for n in PROGRESS_SIGNALS}, "tool": True})
        self.assertEqual(progress_disposition(tool_only_default, "continue", self.config, "worker"),
                         "progress_requires_next")
        # Recovery requires the stored selection to match the effective list.
        mismatched = self._record(role="worker", selected=list(DEFAULT_PROGRESS_SIGNALS), count=2)
        self.assertFalse(no_progress_recovery_due(mismatched, "continue", config))
        matched = self._record(role="worker", selected=("state",), count=2)
        self.assertTrue(no_progress_recovery_due(matched, "continue", config))
        legacy = self._record(role="worker", selected=list(DEFAULT_PROGRESS_SIGNALS), count=2)
        self.assertTrue(no_progress_recovery_due(legacy, "continue", self.config))


if __name__ == "__main__":
    import unittest
    unittest.main()


class ReadOnlyProgressTests(Fixture):
    def cursor(self):
        return read_json(self.project.root / "observed" / "reviewer.json")

    def run_observer(self, callback=None, outcome="continue", *, ack_decisions=False, **kwargs):
        def finish(ctx, *_):
            if callback:
                callback(ctx)
            if outcome == "bounded":
                ctx.bound_hit = "requests"
            args = {"outcome": "continue" if outcome == "bounded" else outcome,
                    "summary": "Observed this unit", "state": "Role-local conclusion"}
            if ack_decisions:
                args["ack_events"] = [event["ack_token"] for event in ctx.decision_events]
            ctx.handle("finish", args)
        return Engine(self.config, driver=ScriptDriver(finish)).run(
            self.project, "reviewer", **kwargs)

    def test_bounded_continuations_use_role_policy_and_saturating_counter(self):
        self.config.roles["reviewer"] = dataclasses.replace(
            self.config.roles["reviewer"], progress_signals=("state",))
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, no_progress_continuations=1))
        self.project.config = self.config
        self.run_observer(outcome="bounded")
        self.assertTrue(self.cursor()["progress"]["signals"]["state"])
        self.assertEqual(self.run_observer(outcome="bounded")["status"], "completed")
        self.assertEqual(self.cursor()["progress"]["no_progress_count"], 1)
        self.assertEqual(self.run_observer(outcome="bounded")["status"], "completed")
        self.assertEqual(self.cursor()["progress"]["no_progress_count"], 2)
        self.assertEqual(self.run_observer()["skipped"], "unchanged")
        cursor = self.cursor()
        cursor["progress"]["no_progress_count"] = 101
        # The admitting wake is already the record's input signal, so this
        # unit observes unchanged input and exercises saturation, not a reset.
        cursor["progress"]["signals"]["input"] = True
        write_json(self.project.root / "observed" / "reviewer.json", cursor)
        self.project.set_control(wake_generation="next-empty-unit")
        self.run_observer(outcome="bounded")
        self.assertEqual(self.cursor()["progress"]["no_progress_count"], 101)

    def test_malformed_control_wake_fails_closed_on_observer_completion(self):
        self.run_observer(outcome="wait")
        role = self.config.roles["reviewer"]
        self.config.roles["reviewer"] = dataclasses.replace(
            role, capabilities=(*role.capabilities, "work_items_transition"))
        self.project.config = self.config
        item = self.project.work_items.create(
            idempotency_key="malformed-wake-ready", title="Observer-ready work",
            actor="worker")["record"]["work_item"]
        self.project.set_control(wake_generation=123)
        self.assertFalse(_operator_woke(self.project.control(), self.cursor(), observer=True))
        self.assertEqual(self.run_observer()["status"], "completed")
        self.assertEqual(self.cursor()["wake_generation_digest"], digest(b""))
        self.project.work_items.transition(item["id"], expected_rev=1, actor="reviewer",
                                           status="dropped", reason="Test task settled")
        # Removal is a ready-set event, not a malformed control wake.
        self.assertEqual(self.run_observer(outcome="wait", event_trigger=True)["status"], "completed")
        self.assertFalse(self.cursor()["progress"]["signals"]["input"])
        self.assertEqual(self.run_observer(event_trigger=True)["skipped"], "no-event")

    def test_invalid_observer_progress_cannot_self_admit(self):
        self.run_observer(outcome="wait")
        cursor = {**self.cursor(), "outcome": "continue", "progress": {}}
        write_json(self.project.root / "observed" / "reviewer.json", cursor)
        self.assertEqual(self.run_observer()["skipped"], "unchanged")
        self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name="reviewer"))

    def test_wake_during_run_remains_pending_and_failures_do_not_acknowledge(self):
        self.run_observer(outcome="wait")
        self.project.set_control(wake_generation="offered-wake")
        self.run_observer(lambda ctx: self.project.set_control(wake_generation="later-wake"),
                          outcome="wait", event_trigger=True)
        self.assertEqual(self.cursor()["wake_generation_digest"], digest(b"offered-wake"))
        def fail(ctx):
            raise Denied("Injected observer failure")
        with self.assertRaises(Denied):
            self.run_observer(fail, outcome="wait", event_trigger=True)
        self.assertEqual(self.cursor()["wake_generation_digest"], digest(b"offered-wake"))
        self.assertEqual(self.run_observer(outcome="wait", event_trigger=True)["status"], "completed")
        self.assertEqual(self.cursor()["wake_generation_digest"], digest(b"later-wake"))
        self.assertEqual(self.run_observer(event_trigger=True)["skipped"], "no-event")

