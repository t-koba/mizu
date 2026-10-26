"""Decision-event trigger: rejected findings reschedule only their origin role."""
import dataclasses
import json
import unittest
from unittest.mock import patch
from support import Fixture, ScriptDriver, finish_with_decision_acks
from mizu.errors import Busy, ConfigError, Denied
from mizu.fs import lock, read_json
from mizu.runtime import Engine, prompt_for


def _trigger_config(config, role="reviewer", events=("reject",)):
    roles = dict(config.roles)
    roles[role] = dataclasses.replace(roles[role], decision_events=events)
    return dataclasses.replace(config, roles=roles)


def _reject(project, source, title="Finding", insight_id=None):
    item = project.insights.submit(source=source, title=title, body="evidence",
                                   base_snapshot=project.snapshots.get()["id"],
                                   **({"insight_id": insight_id} if insight_id is not None else {}))
    project.insights.decide(item["id"], "reject", "not material", "", "test", expected_rev=1)
    return item


class DecisionTriggerTests(Fixture):
    def test_unchanged_code_rejection_admits_origin_role_only(self):
        config = _trigger_config(self.config)
        engine = Engine(config, driver=ScriptDriver(finish_with_decision_acks))
        self.assertEqual(engine.run(self.project, "reviewer")["status"], "completed")
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")
        _reject(self.project, "reviewer")
        admitted = engine.run(self.project, "reviewer")
        self.assertEqual(admitted["status"], "completed")
        started = read_json(self.project.root / "runs" / admitted["run"] / "started.json")
        self.assertEqual((started["admission"], started["decision_events"]), ("decision", 1))
        logged = read_json(self.project.root / "runs" / admitted["run"] / "decision-events.json")
        self.assertEqual(logged["events"][0]["reason"], "not material")
        # Acknowledged on success: the next unchanged tick skips again (no loop).
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")
        # The rejection never routes to the other role.
        self.assertEqual(self.project.insights.decision_events("searcher", ("reject",)), [])
        self.assertEqual(self.project.insights.decision_events("reviewer", ("reject",)), [])

    def test_prompt_carries_focused_events_not_history(self):
        _reject(self.project, "reviewer")
        events = self.project.insights.decision_events("reviewer", ("reject",))
        self.assertEqual(len(events), 1)
        ctx = self.context("reviewer")
        self.assertEqual(json.loads(prompt_for(ctx))["decision_events"], [])
        ctx.decision_events = events
        delivered = json.loads(prompt_for(ctx))["decision_events"]
        self.assertEqual(len(delivered), 1)
        self.assertEqual((delivered[0]["action"], delivered[0]["reason"], delivered[0]["body"]),
                         ("reject", "not material", "evidence"))
        self.assertNotIn("acknowledged", json.dumps(delivered))

    def test_failure_busy_and_pause_preserve_unacked_events(self):
        config = _trigger_config(self.config)
        _reject(self.project, "reviewer")
        def boom(context, prompt, profile=None):
            raise RuntimeError("mid-run restart")
        with self.assertRaises(RuntimeError):
            Engine(config, driver=ScriptDriver(boom)).run(self.project, "reviewer")
        self.assertEqual(len(self.project.insights.decision_events("reviewer", ("reject",))), 1)
        self.project.set_control(armed=True, paused=True)
        with self.assertRaises(Denied):
            Engine(config, driver=ScriptDriver(finish_with_decision_acks)).run(self.project, "reviewer")
        self.project.set_control(armed=True, paused=False)
        with lock(self.project.root / "locks" / "run-reviewer.lock", blocking=False):
            with self.assertRaises(Busy):
                Engine(config, driver=ScriptDriver(finish_with_decision_acks)).run(self.project, "reviewer")
        self.assertEqual(len(self.project.insights.decision_events("reviewer", ("reject",))), 1)
        admitted = Engine(config, driver=ScriptDriver(finish_with_decision_acks)).run(self.project, "reviewer")
        self.assertEqual(admitted["status"], "completed")
        self.assertEqual(self.project.insights.decision_events("reviewer", ("reject",)), [])

    def test_withdrawn_and_stale_rejections_never_emit(self):
        gone = _reject(self.project, "reviewer", title="Gone")
        self.project.insights.withdraw(gone["id"], source="operator", reason="author retracted")
        self.assertEqual(self.project.insights.decision_events("reviewer", ("reject",)), [])
        stale = _reject(self.project, "reviewer", title="Stale")
        self.project.insights.revise(stale["id"], source="reviewer", title="Stale",
                                     body="new evidence", base_snapshot=None)
        self.assertEqual(self.project.insights.decision_events("reviewer", ("reject",)), [])
        self.assertIn(stale["id"], [i["id"] for i in self.project.insights.list()])

    def test_decide_cannot_reanimate_withdrawal(self):
        item = _reject(self.project, "reviewer")
        self.project.insights.withdraw(item["id"], source="reviewer", reason="retracted")
        with self.assertRaises(Denied):
            self.project.insights.decide(item["id"], "accept", "second thoughts", "", "test", expected_rev=1)
        self.project.insights.revise(item["id"], source="reviewer", title="Finding",
                                     body="new evidence", base_snapshot=None)
        self.project.insights.decide(item["id"], "accept", "now material", "", "test", expected_rev=2)

    def test_event_trigger_skips_without_subscribed_event(self):
        self.project.set_control(wake_generation="")  # no pending operator wake
        # Schedule-only roles (no on_change) previously ran a full model unit
        # on every file-trigger wake, even with no subscribed event pending:
        # decision-history changes from unrelated roles re-triggered the
        # searcher, whose new insight re-triggered the writer, forming a loop.
        # Event-triggered dispatch must exit before any model use unless a
        # subscribed decision event or due wait is pending for that role,
        # while plain (timer) dispatch still runs its periodic unit.
        config = _trigger_config(self.config, role="searcher")
        driver = ScriptDriver(finish_with_decision_acks)
        engine = Engine(config, driver=driver)
        before = {d.name for d in (self.project.root / "runs").iterdir()} if (self.project.root / "runs").exists() else set()
        skipped = engine.run(self.project, "searcher", event_trigger=True)
        self.assertEqual(skipped.get("skipped"), "no-event")
        self.assertEqual(driver.calls, [])
        after = {d.name for d in (self.project.root / "runs").iterdir()} if (self.project.root / "runs").exists() else set()
        self.assertEqual(before, after)
        # An unrelated role's decision must not admit this role's event dispatch.
        _reject(self.project, "reviewer")
        self.assertEqual(engine.run(self.project, "searcher", event_trigger=True).get("skipped"), "no-event")
        self.assertEqual(driver.calls, [])
        # The periodic timer (plain dispatch) still runs without events.
        self.assertEqual(engine.run(self.project, "searcher")["status"], "completed")
        self.assertEqual(len(driver.calls), 1)
        # A subscribed event for this role admits exactly one focused unit.
        _reject(self.project, "searcher")
        admitted = engine.run(self.project, "searcher", event_trigger=True)
        self.assertEqual(admitted["status"], "completed")
        self.assertEqual(len(driver.calls), 2)
        self.assertEqual(engine.run(self.project, "searcher", event_trigger=True).get("skipped"), "no-event")

    def test_unconfigured_role_keeps_skipping(self):
        engine = Engine(self.config, driver=ScriptDriver(finish_with_decision_acks))
        engine.run(self.project, "reviewer")
        _reject(self.project, "reviewer")
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")


class DecisionTriggerConfigTests(Fixture):
    def _set_events(self, role, value_toml):
        lines = self.file.read_text().splitlines(keepends=True)
        start = next(i for i, line in enumerate(lines) if line.strip() == f"[roles.{role}]")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        lines.insert(end, f"decision_events = {value_toml}\n")
        self.file.write_text("".join(lines))

    def test_decision_events_load_and_withdraw_refused(self):
        from mizu.config import load
        self._set_events("reviewer", '["reject"]')
        self.assertEqual(load(self.file).roles["reviewer"].decision_events, ("reject",))
        self._set_events("searcher", '["withdraw"]')
        with self.assertRaises(ConfigError):
            load(self.file)




class AnySourceResolverTests(Fixture):
    def _any_config(self, role="searcher", events=("defer",)):
        import dataclasses
        roles = dict(self.config.roles)
        roles[role] = dataclasses.replace(roles[role], decision_events=events, decision_sources="any")
        return dataclasses.replace(self.config, roles=roles)

    def test_own_mode_keeps_cross_source_isolated(self):
        snap = self.project.snapshots.get()["id"]
        item = self.project.insights.submit(source="reviewer", title="Finding", body="v1",
                                            base_snapshot=snap)
        self.project.insights.decide(item["id"], "defer", "needs field data", "observe X", "test", expected_rev=1)
        self.assertEqual(self.project.insights.decision_events("searcher", ("defer",)), [])
        self.assertEqual(len(self.project.insights.decision_events("searcher", ("defer",), sources="any")), 1)

    def test_any_source_defer_admits_once_then_skips(self):
        self.project.set_control(wake_generation="")  # no pending operator wake
        import dataclasses
        from mizu.runtime import Engine
        from support import ScriptDriver
        config = self._any_config()
        engine = Engine(config, driver=ScriptDriver(finish_with_decision_acks))
        # No event yet: event trigger skips cheaply without model use.
        driver_calls = engine._driver.calls if hasattr(engine, "_driver") else None
        skipped = engine.run(self.project, "searcher", event_trigger=True)
        self.assertEqual(skipped.get("skipped"), "no-event")
        # A deferral from another source routes to the resolver.
        snap = self.project.snapshots.get()["id"]
        item = self.project.insights.submit(source="reviewer", title="Finding", body="v1",
                                            base_snapshot=snap)
        self.project.insights.decide(item["id"], "defer", "needs field data", "observe X", "test", expected_rev=1)
        events = self.project.insights.decision_events("searcher", ("defer",), sources="any")
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["action"], events[0]["source"]), ("defer", "reviewer"))
        admitted = engine.run(self.project, "searcher", event_trigger=True)
        self.assertEqual(admitted["status"], "completed")
        # One-shot: acknowledged, so the next event trigger skips again.
        self.assertEqual(self.project.insights.decision_events("searcher", ("defer",), sources="any"), [])
        self.assertEqual(engine.run(self.project, "searcher", event_trigger=True).get("skipped"), "no-event")
        # Own-source view never saw it.
        self.assertEqual(self.project.insights.decision_events("searcher", ("defer",)), [])

    def test_any_source_invalid_and_empty_refused_at_load(self):
        from mizu.config import load
        from mizu.errors import ConfigError
        # Unknown value refused.
        text = self.file.read_text()
        lines = text.splitlines(keepends=True)
        start = next(i for i, line in enumerate(lines) if line.strip() == "[roles.searcher]")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        lines.insert(end, 'decision_events = ["defer"]\ndecision_sources = "everyone"\n')
        self.file.write_text("".join(lines))
        with self.assertRaises(ConfigError):
            load(self.file)
        # any without events refused.
        self.file.write_text(text)
        lines = self.file.read_text().splitlines(keepends=True)
        start = next(i for i, line in enumerate(lines) if line.strip() == "[roles.searcher]")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        lines.insert(end, 'decision_sources = "any"\n')
        self.file.write_text("".join(lines))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_any_source_bad_kwarg_refused(self):
        from mizu.errors import Denied
        with self.assertRaises(Denied):
            self.project.insights.decision_events("searcher", ("defer",), sources="everyone")
        with self.assertRaises(Denied):
            self.project.insights.acknowledge_decisions("searcher", [], sources="everyone")

if __name__ == "__main__":
    raise SystemExit(unittest.main())


class DeferRefinementTests(Fixture):
    def test_defer_rides_along_without_admitting_until_topic_moves(self):
        config = _trigger_config(self.config, events=("reject", "defer"))
        engine = Engine(config, driver=ScriptDriver(finish_with_decision_acks))
        engine.run(self.project, "reviewer")
        item = self.project.insights.submit(source="reviewer", title="Finding", body="v1",
                                            base_snapshot=self.project.snapshots.get()["id"])
        self.project.insights.decide(item["id"], "defer", "needs field data", "observe X in the wild", "test", expected_rev=1)
        events = self.project.insights.decision_events("reviewer", ("reject", "defer"))
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["action"], events[0]["reason"], events[0]["revisit"]),
                         ("defer", "needs field data", "observe X in the wild"))
        # A deferral alone never admits a run: unchanged and event-trigger
        # dispatch skip cheaply instead of starting model runs.
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")
        self.assertEqual(engine.run(self.project, "reviewer", event_trigger=True).get("skipped"), "no-event")
        # A failure leaves the same window in place (no rotation advance).
        self.assertEqual(self.project.insights.decision_events("reviewer", ("reject", "defer"))[0]["insight"], item["id"])
        # The topic moving (meaningful revision reopening it) ends the ride-along.
        self.project.insights.revise(item["id"], source="reviewer", title="Finding",
                                     body="v2 with field data", base_snapshot=None)
        self.assertEqual(self.project.insights.decision_events("reviewer", ("reject", "defer")), [])

    def test_configured_ridealong_limit_controls_dispatch_window(self):
        import dataclasses
        snap = self.project.snapshots.get()["id"]
        for i in range(4):
            item = self.project.insights.submit(source="reviewer", title="L%02d" % i, body="v1",
                                                base_snapshot=snap)
            self.project.insights.decide(item["id"], "defer", "parked", "wait", "test", expected_rev=1)
        # Project default stays 10; an explicit 2 keeps the 2-window.
        self.assertEqual(self.config.limits.decision_events_limit, 10)
        limited = dataclasses.replace(self.config.limits, decision_events_limit=2)
        config = dataclasses.replace(self.config, limits=limited)
        window = self.project.insights.decision_events(
            "reviewer", ("reject", "defer"), limit=config.limits.decision_events_limit)
        self.assertEqual(len(window), 2)
        # Bad values refuse at load, fail closed.
        from mizu.config import load
        from mizu.errors import ConfigError
        text = self.file.read_text()
        self.file.write_text(text.replace("pending_insights = 30", "pending_insights = 30\ndecision_events_limit = 0"))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_defer_window_advances_only_with_exact_receipts(self):
        snap = self.project.snapshots.get()["id"]
        for i in range(4):
            item = self.project.insights.submit(source="reviewer", title="D%02d" % i, body="v1",
                                                base_snapshot=snap)
            self.project.insights.decide(item["id"], "defer", "parked", "wait", "test", expected_rev=1)
        first = self.project.insights.decision_events("reviewer", ("reject", "defer"), limit=2)
        self.assertEqual(len(first), 2)
        self.project.insights.acknowledge_decisions("reviewer", first)
        second = self.project.insights.decision_events("reviewer", ("reject", "defer"), limit=2)
        self.assertEqual(len(second), 2)
        # Explicit receipts advance past the first window; no silent rotation.
        self.assertEqual(set(e["insight"] for e in first) & set(e["insight"] for e in second), set())
        self.project.insights.acknowledge_decisions("reviewer", second)
        third = self.project.insights.decision_events("reviewer", ("reject", "defer"), limit=2)
        self.assertEqual(third, [])

    def test_meaningful_revision_wakes_writer_with_stale_gap_visible(self):
        from mizu.runtime import should_run
        self.project.set_control(armed=True, paused=False, wake_generation="")
        item = self.project.insights.submit(source="reviewer", title="Finding", body="v1",
                                            base_snapshot=self.project.snapshots.get()["id"])
        self.project.insights.decide(item["id"], "defer", "needs field data", "observe X", "test", expected_rev=1)
        captured = self.project.snapshots.capture_files(self.project.workspace)
        republished = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Writer parked.", run=None,
            outcome="wait", summary="Parked with deferred finding.",
            inbox_seen=self.project.insights.generation())
        self.project.snapshots.publish(republished)
        snap = self.project.snapshots.get()
        self.assertFalse(should_run(self.project, {**snap, "outcome": "wait"}))
        self.project.insights.revise(item["id"], source="reviewer", title="Finding",
                                     body="v2 with field data", base_snapshot=None)
        # The generation change wakes the writer; the revision reopens the
        # topic with no current decision so the new body must be re-decided.
        # The superseded defer stays in decisions/history audit, never as
        # the current disposition.
        self.assertTrue(should_run(self.project, {**snap, "outcome": "wait"}))
        pending = [i for i in self.project.insights.list() if i["id"] == item["id"]][0]
        self.assertEqual(pending["rev"], 2)
        self.assertIsNone(pending["decision"])
        from mizu.fs import read_json
        raw = read_json(self.project.root / "decisions" / f"{item['id']}.json")
        self.assertEqual((raw["action"], raw["rev"]), ("defer", 1))

    def test_revised_defer_must_be_decided_again_at_new_rev(self):
        item = self.project.insights.submit(source="reviewer", title="Finding", body="v1",
                                            base_snapshot=self.project.snapshots.get()["id"])
        self.project.insights.decide(item["id"], "defer", "needs field data", "observe X", "test", expected_rev=1)
        self.project.insights.revise(item["id"], source="reviewer", title="Finding",
                                     body="v2 with field data", base_snapshot=None)
        pending = [i for i in self.project.insights.list() if i["id"] == item["id"]][0]
        self.assertIsNone(pending["decision"])
        # A decision at the old rev is refused; the new rev is actionable.
        from mizu.errors import Denied
        with self.assertRaises(Denied):
            self.project.insights.decide(item["id"], "accept", "stale read", "", "test", expected_rev=1)
        record = self.project.insights.decide(item["id"], "accept", "read v2", "", "test", expected_rev=2)
        self.assertEqual(record["rev"], 2)

    def test_globally_oldest_defers_precede_newer_substantive_under_limit(self):
        # Prompt bounds are not acknowledgement or priority rules.
        snap = self.project.snapshots.get()["id"]
        for i in range(20):
            item = self.project.insights.submit(source="reviewer", title="D%02d" % i, body="v1",
                                                base_snapshot=snap)
            with patch("mizu.insights.now", return_value=f"2026-01-01T00:00:{i:02d}+00:00"):
                self.project.insights.decide(item["id"], "defer", "parked", "wait", "test", expected_rev=1)
        rejected = self.project.insights.submit(source="reviewer", title="R", body="v1",
                                                base_snapshot=snap)
        self.project.insights.decide(rejected["id"], "reject", "not material", "", "test", expected_rev=1)
        events = self.project.insights.decision_events("reviewer", ("reject", "defer"), limit=10)
        self.assertEqual(len(events), 10)
        self.assertTrue(all(e["action"] == "defer" for e in events))
        self.assertEqual(self.project.insights.decision_events(
            "reviewer", ("reject", "defer"), limit=10), events)
        self.project.insights.acknowledge_decisions("reviewer", events)
        again = self.project.insights.decision_events("reviewer", ("reject", "defer"), limit=10)
        self.assertEqual(len(again), 10)
        self.assertTrue(all(e["action"] == "defer" for e in again))
        self.project.insights.acknowledge_decisions("reviewer", again)
        last = self.project.insights.decision_events("reviewer", ("reject", "defer"), limit=10)
        self.assertEqual([e["insight"] for e in last], [rejected["id"]])

    def test_deferred_gap_stays_visible_with_revisit(self):
        item = self.project.insights.submit(source="reviewer", title="Finding", body="v1",
                                            base_snapshot=self.project.snapshots.get()["id"])
        self.project.insights.decide(item["id"], "defer", "blocked on release", "wait for v2", "test", expected_rev=1)
        entries = [e for e in self.project.insights.list() if e["id"] == item["id"]]
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["decision"]["action"], entries[0]["decision"]["revisit"]),
                         ("defer", "wait for v2"))
