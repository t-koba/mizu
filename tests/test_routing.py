"""Worker-directed next-unit routing: recommendation channel, freshness, fallback."""
import copy
import dataclasses
import json
import types
import unittest

from support import Fixture, ScriptDriver
from mizu.classification import prepare
from mizu.errors import Denied
from mizu.fs import digest
from mizu.routing import current
from mizu.selection import validate_selectors


def cond(path, op, value):
    return {"path": path, "op": op, "value": value}


def specification():
    return {"retry_seconds": 30,
            "rules": [{"when": cond("recommendation.profile", "eq", "deep"),
                       "candidates": [{"profile": "alternate"}, {"profile": "primary"}]},
                      {"candidates": [{"profile": "primary"}]}]}


class RoutingFixture(Fixture):
    def setup_selection(self, spec=None, *, role_name="worker"):
        spec = copy.deepcopy(spec or specification())
        validate_selectors({"dynamic": spec}, self.config.profiles, self.file.parent)
        role = dataclasses.replace(self.config.roles[role_name], profile="", selector="dynamic", on_change=False)
        self.config = dataclasses.replace(self.config, selectors={"dynamic": spec},
                                          roles={**self.config.roles, role_name: role})
        self.project.config = self.config
        return role

    def engine(self):
        return types.SimpleNamespace(config=self.config)

    def prepare(self, role):
        return prepare(self.engine(), self.project, role, self.project.snapshots.get())


class RoutingValidationTests(RoutingFixture):
    def test_finish_rejects_malformed_recommendation(self):
        ctx = self.context("worker")
        base = {"outcome": "continue", "summary": "work", "state": "next"}
        with self.assertRaises(Denied):
            ctx.handle("finish", {**base, "next_profile": "Deep!!"})
        with self.assertRaises(Denied):
            ctx.handle("finish", {**base, "next_reason": "no profile named"})
        # A well-formed recommendation seals normally.
        ctx.handle("finish", {**base, "next_profile": "deep", "next_reason": "  needs depth  "})
        self.assertEqual(ctx.finished["next_profile"], "deep")
        self.assertEqual(ctx.finished["next_reason"], "needs depth")

    def test_absent_recommendation_leaves_ordinary_selection(self):
        role = self.setup_selection()
        decision = self.prepare(role)
        self.assertEqual(decision["profile"], "primary")
        self.assertEqual(decision["recommendation"], {"status": "absent"})


class RoutingLifecycleTests(RoutingFixture):
    def finish_next(self, profile, reason=None):
        from mizu.runtime import Engine
        args = {"outcome": "continue", "summary": "unit done", "state": "next",
                "next_profile": profile}
        if reason is not None:
            args["next_reason"] = reason
        def callback(context, prompt, _profile):
            context.handle("finish", args)
        result = Engine(self.config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        return result

    def test_finished_unit_records_task_bound_recommendation(self):
        result = self.finish_next("deep", "needs depth")
        entry = current(self.project, "worker", digest(self.project.goal.encode()),
                        self.project.snapshots.get()["id"])
        self.assertEqual(entry["status"], "fresh")
        self.assertEqual(entry["recommendation"]["profile"], "deep")
        self.assertEqual(entry["recommendation"]["reason"], "needs depth")
        self.assertEqual(entry["recommendation"]["run"], result["run"])
        raw = self.project.root / "routing" / "worker.json"
        self.assertLessEqual(raw.stat().st_size, 1024)
        stored = json.loads(raw.read_text())
        self.assertEqual(stored["snapshot"], result["snapshot"])
        self.assertEqual(stored["goal_digest"], digest(self.project.goal.encode()))

    def test_fresh_recommendation_routes_through_operator_rules(self):
        role = self.setup_selection()
        self.finish_next("deep")
        decision = self.prepare(role)
        self.assertEqual(decision["recommendation"]["status"], "fresh")
        self.assertEqual(decision["profile"], "alternate")
        self.assertEqual(decision["rule"], 0)

    def test_unmapped_recommendation_falls_back_to_default(self):
        role = self.setup_selection()
        self.finish_next("mystery")
        decision = self.prepare(role)
        self.assertEqual(decision["recommendation"]["status"], "fresh")
        self.assertEqual(decision["profile"], "primary")
        self.assertEqual(decision["rule"], 1)

    def test_new_publication_rejects_stale_recommendation(self):
        role = self.setup_selection()
        self.finish_next("deep")
        (self.project.workspace / "app.py").write_bytes(b"VALUE = 9\n")
        captured = self.project.snapshots.capture_files(self.project.workspace)
        self.project.snapshots.publish(self.project.snapshots.create(
            captured, goal=self.project.goal, state="next", run=None,
            outcome="wait", summary="changed"))
        decision = self.prepare(role)
        self.assertEqual(decision["recommendation"], {"status": "stale"})
        self.assertEqual(decision["profile"], "primary")

    def test_corrupt_record_is_invalid_never_fatal(self):
        role = self.setup_selection()
        path = self.project.root / "routing" / "worker.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{not json")
        decision = self.prepare(role)
        self.assertEqual(decision["recommendation"], {"status": "invalid"})
        self.assertEqual(decision["profile"], "primary")

    def test_latest_finish_wins(self):
        role = self.setup_selection()
        self.finish_next("deep")
        self.finish_next("primary")
        decision = self.prepare(role)
        self.assertEqual(decision["recommendation"]["status"], "fresh")
        self.assertEqual(decision["recommendation"]["recommendation"]["profile"], "primary")
        self.assertEqual(decision["profile"], "primary")

    def test_recommendation_adds_no_inference(self):
        role = self.setup_selection()
        self.finish_next("deep")
        before = set((self.project.root / "runs").iterdir())
        decision = self.prepare(role)
        self.assertIsNone(decision["classification"])
        self.assertEqual(set((self.project.root / "runs").iterdir()), before)
        self.assertEqual(decision["profile"], "alternate")


class RoutingSessionTests(RoutingFixture):
    def test_profile_switch_gets_fresh_session_and_returns(self):
        from mizu.engine_config import effective, save_session, session_record
        role = self.config.roles["worker"]
        main = effective(self.config, role, "primary")
        deep = effective(self.config, role, "alternate")
        ctx = self.context("worker")
        main_path, main_record = session_record(ctx, "primary", main)
        deep_path, deep_record = session_record(ctx, "alternate", deep)
        self.assertIsNone(main_record)
        self.assertIsNone(deep_record)
        self.assertNotEqual(main_path.parent, deep_path.parent)
        save_session(main_path, "main-conversation", {"tokens": 3})
        save_session(deep_path, "deep-conversation", {"tokens": 5})
        # Returning to main resumes its conversation; deep stays separate.
        back_path, back_record = session_record(ctx, "primary", main)
        self.assertEqual(back_path, main_path)
        self.assertEqual(back_record["id"], "main-conversation")
        _, deep_again = session_record(ctx, "alternate", deep)
        self.assertEqual(deep_again["id"], "deep-conversation")


if __name__ == "__main__":
    unittest.main()
