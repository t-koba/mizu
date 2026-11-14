"""Timer-only input admission uses strict current decisions, never delivery cursors."""
import dataclasses
import json
import re
from pathlib import Path
from unittest.mock import patch

from support import Fixture, ScriptDriver, forget_ledger
from mizu.cli import _cmd_run, parser
from mizu.config import load
from mizu.errors import ConfigError, Denied, LimitExceeded
from mizu.runtime import Engine
from mizu.services import render


class ScheduledInputConfigTests(Fixture):
    def test_default_and_supported_timed_roles(self):
        self.assertTrue(all(r.scheduled_inputs == () for r in self.config.roles.values()))
        original = self.file.read_text()
        for name in ("searcher", "reporter"):  # interval and calendar
            with self.subTest(role=name):
                text = original.replace(
                    f"[roles.{name}]", f'[roles.{name}]\nscheduled_inputs = ["deferred_insights"]')
                if name == "searcher":
                    text = text.replace('capabilities = ["files", "read", "search"',
                                        'capabilities = ["insights", "files", "read", "search"')
                self.file.write_text(text)
                self.assertEqual(load(self.file).roles[name].scheduled_inputs, ("deferred_insights",))

    def test_invalid_declarations_are_refused(self):
        original = self.file.read_text()
        for declaration in ('"deferred_insights"', '["unknown"]', '[1]',
                            '["deferred_insights", "deferred_insights"]'):
            with self.subTest(declaration=declaration):
                self.file.write_text(original.replace(
                    '[roles.reporter]', f'[roles.reporter]\nscheduled_inputs = {declaration}'))
                with self.assertRaises(ConfigError):
                    load(self.file)
        for name in ("worker", "consult", "reporter"):
            with self.subTest(role=name):
                text = original.replace(
                    f'[roles.{name}]', f'[roles.{name}]\nscheduled_inputs = ["deferred_insights"]')
                if name == "reporter":
                    start = text.index('[roles.reporter]')
                    text = text[:start] + text[start:].replace('"insights", ', '', 1)
                self.file.write_text(text)
                with self.assertRaises(ConfigError):
                    load(self.file)


class ScheduledInputRuntimeTests(Fixture):
    def setUp(self):
        super().setUp()
        role = dataclasses.replace(self.config.roles["reporter"],
                                   scheduled_inputs=("deferred_insights",), on_change=False)
        self.config = dataclasses.replace(self.config, roles={**self.config.roles, "reporter": role})
        self.project.config = self.config
        self.project.set_control(wake_generation="")
        self.engine = Engine(self.config, driver=ScriptDriver())

    def submit(self, source="searcher", action=None):
        row = self.project.insights.submit(source=source, title="Input", body="Evidence", base_snapshot=None)
        if action:
            self.project.insights.decide(row["id"], action, "Reason", "Revisit", "test", expected_rev=1)
        return row["id"]

    def reaches_admission(self, role_name="reporter", **kwargs):
        # Stop at the actual slot/lock boundary: no model, run directory or OCI use.
        with patch.object(self.engine, "_admit", side_effect=RuntimeError("admission reached")) as admit:
            with self.assertRaisesRegex(RuntimeError, "admission reached"):
                self.engine.run(self.project, role_name, **kwargs)
        admit.assert_called_once()

    def test_empty_timer_skips_before_slot_or_run_and_binds_snapshot(self):
        runs = set((self.project.root / "runs").iterdir()) if (self.project.root / "runs").exists() else set()
        snapshot = self.project.snapshots.get()
        with patch.object(self.engine, "_admit") as admit:
            result = self.engine.run(self.project, "reporter", scheduled_invocation=True)
        self.assertEqual(result, {"skipped": "no-scheduled-input", "snapshot": snapshot["id"],
                                  "code_digest": snapshot["code_digest"]})
        admit.assert_not_called()
        self.assertFalse(self.engine.driver.calls)
        self.assertEqual(set((self.project.root / "runs").iterdir()) if (self.project.root / "runs").exists() else set(), runs)

    def test_direct_event_and_open_ended_runs_do_not_consult_source(self):
        with patch.object(self.project.insights, "has_current_defer", side_effect=AssertionError("not a timer gate")):
            self.reaches_admission()
            # Existing event gate decides the result; it must not read scheduled inputs.
            result = self.engine.run(self.project, "reporter", event_trigger=True, scheduled_invocation=True)
            self.assertEqual(result["skipped"], "no-event")
            role = dataclasses.replace(self.config.roles["reporter"], scheduled_inputs=())
            self.config.roles["reporter"] = role
            self.reaches_admission(scheduled_invocation=True)

    def test_operator_wake_and_continuation_bypass_empty_gate(self):
        self.project.set_control(wake_generation="new-wake")
        self.reaches_admission(scheduled_invocation=True)
        self.project.set_control(wake_generation="")
        self.config.roles["worker"] = dataclasses.replace(
            self.config.roles["worker"], daemon=False, interval_seconds=600,
            scheduled_inputs=("deferred_insights",))
        # Both saved reservations and pending safe-points keep their existing path.
        with patch("mizu.continuations.ready", return_value={"intent": "saved"}), patch(
                "mizu.continuations.reservation", side_effect=RuntimeError("continuation reached")), patch.object(
                self.project.insights, "has_current_defer", side_effect=AssertionError("continuation")):
            with self.assertRaisesRegex(RuntimeError, "continuation reached"):
                self.engine.run(self.project, "worker", scheduled_invocation=True)
        with patch("mizu.runtime.safe_point.pending_hint", return_value=True), patch.object(
                self.project.insights, "has_current_defer", side_effect=AssertionError("continuation")):
            self.reaches_admission("worker", scheduled_invocation=True)

    def test_current_defers_from_all_sources_survive_acknowledgement(self):
        self.submit("former-role", "defer")
        self.submit("searcher", "defer")
        rows = self.project.insights.decision_events("reporter", ["defer"], sources="any", limit=100)
        self.assertEqual(len(rows), 2)
        self.project.insights.acknowledge_decisions("reporter", rows, sources="any")
        self.assertEqual(self.project.insights.decision_events("reporter", ["defer"], sources="any", limit=100), [])
        self.assertTrue(self.project.insights.has_current_defer())
        self.engine.run(self.project, "reporter", scheduled_invocation=True)
        self.assertEqual(len(self.engine.driver.calls), 1)

    def test_undecided_resolved_and_stale_defer_are_not_current_deferrals(self):
        self.submit()
        self.submit("reviewer", "reject")
        key = self.submit("former-role", "defer")
        path = self.project.root / "inbox" / f"{key}.json"
        record = json.loads(path.read_text())
        record["rev"] = 2
        path.write_text(json.dumps(record))
        forget_ledger(self.project.root)
        self.assertFalse(self.project.insights.has_current_defer())
        self.assertEqual(self.engine.run(self.project, "reporter", scheduled_invocation=True)["skipped"],
                         "no-scheduled-input")
        # Existing producer accounting still includes undecided/stale-decision rows.
        self.assertEqual(self.project.insights.unresolved_counts(["searcher", "former-role"]),
                         {"searcher": 1, "former-role": 1})

    def test_unsafe_empty_decision_parent_does_not_prove_emptiness(self):
        self.submit()
        decisions = self.project.root / "decisions"
        if decisions.exists():
            decisions.rmdir()
        target = self.root / "unsafe-decisions"
        target.mkdir()
        try:
            decisions.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks unavailable")
        forget_ledger(self.project.root)  # Only a rebuild reads the unsafe directory.
        with self.assertRaises(Denied):
            self.project.insights.has_current_defer()
        self.reaches_admission(scheduled_invocation=True)

    def test_pending_insight_attention_blocks_empty_gate(self):
        self.project.insights.submit(
            source="searcher", title="Attention", body="Please review",
            base_snapshot=None, addressee="reporter")
        self.assertFalse(self.project.insights.has_current_defer())
        self.reaches_admission(scheduled_invocation=True)

    def test_non_defer_decision_blocks_empty_gate(self):
        # Deferred empty but a subscribed non-defer decision is pending: the
        # timer is the only dispatch on Windows, so the gate must fall through
        # to ordinary admission instead of returning no-scheduled-input.
        role = dataclasses.replace(self.config.roles["reporter"],
                                   decision_events=("accept",),
                                   decision_sources="any")
        self.config.roles["reporter"] = role
        self.project.config = self.config
        self.submit("searcher", "accept")
        self.assertFalse(self.project.insights.has_current_defer())
        self.reaches_admission(scheduled_invocation=True)

    def test_ready_check_job_blocks_empty_gate(self):
        with patch("mizu.runtime.check_jobs.wake_ready", return_value=True):
            self.reaches_admission(scheduled_invocation=True)

    def test_terminal_work_item_event_blocks_empty_gate(self):
        # Deferred empty but a subscribed terminal work-item event is pending.
        role = dataclasses.replace(self.config.roles["reporter"],
                                   work_item_events=("dropped",),
                                   work_item_sources="any")
        self.config.roles["reporter"] = role
        self.project.config = self.config
        proposal = self.project.insights.submit(
            source="searcher", title="Terminal", body="Evidence", base_snapshot=None)
        decision = self.project.insights.decide(
            proposal["id"], "accept", "reviewed", "", "run", expected_rev=1)
        seed = decision["work_item"]
        self.project.work_items.transition(
            seed["id"], expected_rev=1, status="dropped", actor="worker",
            reason="Terminal")
        self.assertFalse(self.project.insights.has_current_defer())
        self.reaches_admission(scheduled_invocation=True)

    def test_operator_record_blocks_empty_gate(self):
        # Deferred empty but an addressed operator record is pending.
        from mizu import operator_records as records
        records.write(self.project, event_id="notice1", kind="operator.notice",
                      body="Exact notice", addressee="reporter")
        self.assertFalse(self.project.insights.has_current_defer())
        self.reaches_admission(scheduled_invocation=True)

    def enable_ci_wake(self):
        role = dataclasses.replace(self.config.roles["reporter"], ci_wake=True)
        self.config = dataclasses.replace(
            self.config, roles={**self.config.roles, "reporter": role},
            vcs={**self.config.vcs, "ci_branch": "main"})
        self.project.config = self.config
        self.engine = Engine(self.config, driver=ScriptDriver())

    def test_ci_failure_blocks_empty_gate(self):
        from mizu.fs import write_json
        self.enable_ci_wake()
        write_json(self.project.root / "vcs-poll.json", {
            "failures": {"main": [{"sha": "a" * 40, "check": "checks"}]}})
        self.reaches_admission(scheduled_invocation=True)

    def test_malformed_ci_cache_does_not_prove_empty(self):
        from mizu.fs import write_json
        self.enable_ci_wake()
        write_json(self.project.root / "vcs-poll.json", {"failures": {"main": None}})
        self.reaches_admission(scheduled_invocation=True)

    def test_malformed_and_unreadable_evidence_falls_through(self):
        key = self.submit("searcher", "reject")
        for directory in ("inbox", "decisions"):
            path = self.project.root / directory / f"{key}.json"
            original = path.read_text()
            for malformed in ('{', '[]', json.dumps({"id": key, "source": "searcher", "rev": False})):
                with self.subTest(directory=directory, malformed=malformed):
                    path.write_text(malformed)
                    forget_ledger(self.project.root)
                    self.reaches_admission(scheduled_invocation=True)
            path.write_text(original)
            forget_ledger(self.project.root)
        for result in (None, 0):
            with self.subTest(result=result), patch.object(
                    self.project.insights, "has_current_defer", return_value=result):
                self.reaches_admission(scheduled_invocation=True)
        self.config.roles["reporter"] = dataclasses.replace(self.config.roles["reporter"], scheduled_inputs=("unknown",))
        self.reaches_admission(scheduled_invocation=True)


class ScheduledInputPlatformTests(Fixture):
    def test_only_timer_commands_are_marked_on_all_platforms(self):
        for system in ("linux", "macos", "windows"):
            with self.subTest(system=system):
                roles = dict(self.config.roles)
                roles["reporter"] = dataclasses.replace(
                    roles["reporter"], scheduled_inputs=("deferred_insights",))
                config = dataclasses.replace(self.config, timezone="local", roles=roles)
                definitions = render(config, self.project, self.root / "mizu", system=system)
                marked = [text for text in definitions.values() if "--scheduled" in text]
                self.assertEqual(len(marked), 1)  # only the opted-in calendar role
                for text in marked:
                    self.assertNotIn("--event", text)
                for text in definitions.values():
                    if "--event" in text or "daemon" in text:
                        self.assertNotIn("--scheduled", text)

    def test_cli_threads_hidden_timer_marker_only_for_run(self):
        for flags, expected in (([], False), (["--event"], False), (["--scheduled"], True)):
            with self.subTest(flags=flags):
                args = parser().parse_args(["run", "sample", "--role", "reporter", *flags])
                with patch("mizu.cli._platform.is_root", return_value=False), patch("mizu.runtime.Engine.run") as run:
                    _cmd_run(self.config, self.project, args)
                self.assertEqual(run.call_args.kwargs["scheduled_invocation"], expected)
        self.assertNotIn("--scheduled", parser()._subparsers._group_actions[0].choices["run"].format_help())
