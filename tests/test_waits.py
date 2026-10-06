"""Structured defer waits: explicit observable conditions wake one reconsideration."""
import datetime
import json
import unittest
from support import Fixture, ScriptDriver
from mizu.errors import Denied
from mizu.fs import read_json
from mizu.runtime import Engine, prompt_for


def _past():
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=5)).isoformat(timespec="seconds")


def _future():
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=1)).isoformat(timespec="seconds")


def _defer(project, source, wait, title="Finding"):
    item = project.insights.submit(source=source, title=title, body="evidence",
                                   base_snapshot=project.snapshots.get()["id"])
    project.insights.decide(item["id"], "defer", "parked", "wait for it", "test", wait=wait)
    return item


class WaitRegistrationTests(Fixture):
    def test_unsupported_kind_names_supported_set(self):
        item = self.project.insights.submit(source="reviewer", title="F", body="b",
                                            base_snapshot=self.project.snapshots.get()["id"])
        with self.assertRaisesRegex(Denied, "deadline.*code_change.*insight_decided"):
            self.project.insights.decide(item["id"], "defer", "r", "v", "test",
                                         wait={"kind": "ci_green"})
        with self.assertRaises(Denied):
            self.project.insights.decide(item["id"], "defer", "r", "v", "test",
                                         wait={"kind": "deadline", "at": "soon"})
        with self.assertRaises(Denied):
            self.project.insights.decide(item["id"], "defer", "r", "v", "test",
                                         wait={"kind": "deadline", "at": "2026-10-07T00:00:00"})
        with self.assertRaises(Denied):
            self.project.insights.decide(item["id"], "accept", "r", "", "test",
                                         wait={"kind": "code_change"})
        with self.assertRaises(Denied):
            self.project.insights.decide(item["id"], "defer", "r", "v", "test",
                                         wait={"kind": "code_change", "extra": 1})
        with self.assertRaises(Denied):
            self.project.insights.decide(item["id"], "defer", "r", "v", "test",
                                         wait={"kind": "insight_decided"})

    def test_cli_wait_spec_parsing(self):
        from mizu.cli import _parse_wait
        from mizu.errors import ConfigError
        self.assertIsNone(_parse_wait(None))
        self.assertEqual(_parse_wait("code_change"), {"kind": "code_change"})
        self.assertEqual(_parse_wait("deadline:2026-10-07T00:00:00+00:00"),
                         {"kind": "deadline", "at": "2026-10-07T00:00:00+00:00"})
        self.assertEqual(_parse_wait("insight_decided:abc"),
                         {"kind": "insight_decided", "insight": "abc"})
        for bad in ("carrier_pigeon", "code_change:x", "insight_decided:", "deadline"):
            with self.assertRaises(ConfigError):
                _parse_wait(bad)

    def test_tool_schema_accepts_wait(self):
        from mizu.protocol import DEFINITIONS, validate
        validate({"id": "x", "action": "defer", "reason": "r", "revisit": "v",
                  "wait": {"kind": "insight_decided", "insight": "y"}},
                 DEFINITIONS["decide"][1])


class WaitDispatchTests(Fixture):
    def _engine(self):
        return Engine(self.config, driver=ScriptDriver())

    def test_deadline_due_admits_and_consumes_once(self):
        engine = self._engine()
        engine.run(self.project, "reviewer")
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")
        item = _defer(self.project, "reviewer", {"kind": "deadline", "at": _past()})
        admitted = engine.run(self.project, "reviewer")
        self.assertEqual(admitted["status"], "completed")
        started = read_json(self.project.root / "runs" / admitted["run"] / "started.json")
        self.assertEqual((started["admission"], started["wait_events"]), ("wait", 1))
        logged = read_json(self.project.root / "runs" / admitted["run"] / "wait-events.json")
        self.assertEqual((logged["events"][0]["kind"], logged["events"][0]["reason"]),
                         ("deadline", "parked"))
        self.assertFalse((self.project.root / "waits" / f"{item['id']}.json").exists())
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")

    def test_future_deadline_stays_pending(self):
        engine = self._engine()
        engine.run(self.project, "reviewer")
        _defer(self.project, "reviewer", {"kind": "deadline", "at": _future()})
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")
        self.assertEqual(self.project.insights.due_waits("reviewer", self.project.snapshots.get()), [])

    def test_code_change_fires_only_on_new_digest(self):
        engine = self._engine()
        engine.run(self.project, "reviewer")
        _defer(self.project, "reviewer", {"kind": "code_change"})
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")
        (self.project.workspace / "signal.txt").write_text("changed", encoding="utf-8")
        store = self.project.snapshots
        store.publish(store.create(store.capture_files(self.project.workspace), goal=self.project.goal,
                                   state="S", run=None, outcome="wait", summary="code moved"))
        admitted = engine.run(self.project, "reviewer")
        started = read_json(self.project.root / "runs" / admitted["run"] / "started.json")
        # Changed code admits as change, with the satisfied wait delivered alongside.
        self.assertEqual((started["admission"], started["wait_events"]), ("change", 1))
        logged = read_json(self.project.root / "runs" / admitted["run"] / "wait-events.json")
        self.assertEqual(logged["events"][0]["kind"], "code_change")

    def test_insight_decided_fires_on_substantive_decision(self):
        engine = self._engine()
        engine.run(self.project, "reviewer")
        target = self.project.insights.submit(source="searcher", title="T", body="b",
                                              base_snapshot=self.project.snapshots.get()["id"])
        item = _defer(self.project, "reviewer", {"kind": "insight_decided", "insight": target["id"]})
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")
        # Any substantive decision resolves the dependency, including rejection:
        # the waiter reassesses instead of waiting forever.
        self.project.insights.decide(target["id"], "reject", "no", "", "test")
        due = self.project.insights.due_waits("reviewer", self.project.snapshots.get())
        self.assertEqual([(d["insight"], d["target"]) for d in due], [(item["id"], target["id"])])

    def test_target_withdrawal_never_emits(self):
        engine = self._engine()
        engine.run(self.project, "reviewer")
        target = self.project.insights.submit(source="searcher", title="T", body="b",
                                              base_snapshot=self.project.snapshots.get()["id"])
        _defer(self.project, "reviewer", {"kind": "insight_decided", "insight": target["id"]})
        self.project.insights.withdraw(target["id"], source="searcher", reason="moot")
        self.assertEqual(self.project.insights.due_waits("reviewer", self.project.snapshots.get()), [])
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")

    def test_revision_resolve_and_withdraw_invalidate(self):
        engine = self._engine()
        engine.run(self.project, "reviewer")
        item = _defer(self.project, "reviewer", {"kind": "deadline", "at": _past()})
        self.project.insights.revise(item["id"], source="reviewer", title="F", body="v2",
                                     base_snapshot=None)
        self.assertIsNone(self.project.insights.wait_for(item["id"]))
        item2 = _defer(self.project, "reviewer", {"kind": "deadline", "at": _past()}, title="G")
        self.project.insights.decide(item2["id"], "accept", "done", "", "test")
        self.assertEqual(self.project.insights.due_waits("reviewer", self.project.snapshots.get()), [])
        item3 = _defer(self.project, "reviewer", {"kind": "deadline", "at": _past()}, title="H")
        self.project.insights.withdraw(item3["id"], source="reviewer", reason="moot")
        self.assertEqual(self.project.insights.due_waits("reviewer", self.project.snapshots.get()), [])
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")

    def test_wait_routes_to_source_only_and_carries_prompt(self):
        engine = self._engine()
        engine.run(self.project, "reviewer")
        _defer(self.project, "reviewer", {"kind": "deadline", "at": _past()})
        self.assertEqual(self.project.insights.due_waits("searcher", self.project.snapshots.get()), [])
        due = self.project.insights.due_waits("reviewer", self.project.snapshots.get())
        ctx = self.context("reviewer")
        ctx.wait_events = due
        delivered = json.loads(prompt_for(ctx))["wait_events"]
        self.assertEqual((delivered[0]["kind"], delivered[0]["body"]), ("deadline", "evidence"))
        engine.run(self.project, "reviewer")

    def test_paused_role_keeps_wait_pending(self):
        engine = self._engine()
        engine.run(self.project, "reviewer")
        item = _defer(self.project, "reviewer", {"kind": "deadline", "at": _past()})
        self.project.set_control(armed=True, paused=True, wake_generation="paused-wait")
        with self.assertRaises(Denied):
            engine.run(self.project, "reviewer")
        self.assertIsNotNone(self.project.insights.wait_for(item["id"]))

    def test_dashboard_exposes_registered_wait(self):
        from mizu.dashboard import collect
        _defer(self.project, "reviewer", {"kind": "deadline", "at": _future()})
        core = collect(self.project)
        entries = [e for e in core["pending_insights"] if e["decision"] is not None]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["wait"]["kind"], "deadline")


if __name__ == "__main__":
    unittest.main()
