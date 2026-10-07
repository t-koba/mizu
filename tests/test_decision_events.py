"""Decision-event trigger: rejected findings reschedule only their origin role."""
import dataclasses
import json
import unittest
from support import Fixture, ScriptDriver
from mizu.errors import Busy, ConfigError, Denied
from mizu.fs import lock, read_json
from mizu.runtime import Engine, prompt_for


def _trigger_config(config, role="reviewer", events=("reject",)):
    roles = dict(config.roles)
    roles[role] = dataclasses.replace(roles[role], decision_events=events)
    return dataclasses.replace(config, roles=roles)


def _reject(project, source, title="Finding"):
    item = project.insights.submit(source=source, title=title, body="evidence",
                                   base_snapshot=project.snapshots.get()["id"])
    project.insights.decide(item["id"], "reject", "not material", "", "test", expected_rev=1)
    return item


class DecisionTriggerTests(Fixture):
    def test_unchanged_code_rejection_admits_origin_role_only(self):
        config = _trigger_config(self.config)
        engine = Engine(config, driver=ScriptDriver())
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
            Engine(config, driver=ScriptDriver()).run(self.project, "reviewer")
        self.project.set_control(armed=True, paused=False)
        with lock(self.project.root / "locks" / "run-reviewer.lock", blocking=False):
            with self.assertRaises(Busy):
                Engine(config, driver=ScriptDriver()).run(self.project, "reviewer")
        self.assertEqual(len(self.project.insights.decision_events("reviewer", ("reject",))), 1)
        admitted = Engine(config, driver=ScriptDriver()).run(self.project, "reviewer")
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

    def test_unconfigured_role_keeps_skipping(self):
        engine = Engine(self.config, driver=ScriptDriver())
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


if __name__ == "__main__":
    raise SystemExit(unittest.main())


class DeferRefinementTests(Fixture):
    def test_defer_routes_once_with_gap_and_never_repeats_while_open(self):
        config = _trigger_config(self.config, events=("reject", "defer"))
        engine = Engine(config, driver=ScriptDriver())
        engine.run(self.project, "reviewer")
        item = self.project.insights.submit(source="reviewer", title="Finding", body="v1",
                                            base_snapshot=self.project.snapshots.get()["id"])
        self.project.insights.decide(item["id"], "defer", "needs field data", "observe X in the wild", "test", expected_rev=1)
        events = self.project.insights.decision_events("reviewer", ("reject", "defer"))
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["action"], events[0]["reason"]), ("defer", "needs field data"))
        admitted = engine.run(self.project, "reviewer")
        started = read_json(self.project.root / "runs" / admitted["run"] / "started.json")
        self.assertEqual(started["admission"], "decision")
        # One focused reconsideration only: still deferred, still unchanged code, no new event.
        self.assertEqual(self.project.insights.decision_events("reviewer", ("reject", "defer")), [])
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")

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
        # The generation change wakes the writer; the old gap stays attached
        # with its rev so the writer reassesses a visibly stale decision.
        self.assertTrue(should_run(self.project, {**snap, "outcome": "wait"}))
        pending = [i for i in self.project.insights.list() if i["id"] == item["id"]][0]
        self.assertEqual(pending["rev"], 2)
        self.assertEqual((pending["decision"]["action"], pending["decision"]["rev"]), ("defer", 1))

    def test_deferred_gap_stays_visible_with_revisit(self):
        from mizu.dashboard import collect
        item = self.project.insights.submit(source="reviewer", title="Finding", body="v1",
                                            base_snapshot=self.project.snapshots.get()["id"])
        self.project.insights.decide(item["id"], "defer", "blocked on release", "wait for v2", "test", expected_rev=1)
        core = collect(self.project)
        entries = [e for e in core["pending_insights"] if e["id"] == item["id"]]
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["decision"]["action"], entries[0]["decision"]["revisit"]["text"]),
                         ("defer", "wait for v2"))
