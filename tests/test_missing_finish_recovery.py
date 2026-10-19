"""Missing-finish session rotation: stuck persistent sessions restart fresh."""
import dataclasses
import json

from support import Fixture, ScriptDriver
from mizu.config import load
from mizu.engine_config import effective, session_generation, session_record, save_session
from mizu.errors import ConfigError, MissingFinish, ProtocolError
from mizu.fs import read_json, write_json
from mizu.runtime import Engine


FINISH = {"outcome": "wait", "summary": "Exact saved work", "state": "Next step is explicit.", "wait_seconds": 13}


class NoFinishDriver:
    requires_sandbox = False

    def __init__(self):
        self.context = None
        self.calls = 0

    def execute(self, context, prompt, *, profile=None):
        self.calls += 1
        self.context = context
        context.handle("_hello", {})
        context.handle("_budget", {"sequence": 1})
        context.handle("_request_done", {"sequence": 1})
        return {}


def seed_open_work(project):
    proposal = project.insights.submit(
        source="worker", title="Do the thing", body="Implement a core slice", base_snapshot=None)
    decision = project.insights.decide(
        proposal["id"], "accept", "reviewed", "", "run-1", expected_rev=1)
    return decision["work_item"]["id"]


def save_worker_session(fixture):
    ctx = fixture.context("worker")
    settings = effective(fixture.config, ctx.role, ctx.role.profile)
    path, _ = session_record(ctx, ctx.role.profile, settings)
    save_session(path, "test-session", {"tokens": 0})
    return path


def health(project, role="worker"):
    return read_json(project.root / "health" / f"{role}.json", {})


def streak(project, role="worker"):
    return health(project, role).get("consecutive_missing_finish", 0)


class MissingFinishRecoveryTests(Fixture):
    def test_consecutive_missing_finish_rotates_with_open_work(self):
        seed_open_work(self.project)
        path = save_worker_session(self)
        # Two consecutive missing-finish failures reach the default threshold (2).
        for _ in range(2):
            driver = NoFinishDriver()
            with self.assertRaises(MissingFinish):
                Engine(self.config, driver=driver).run(self.project, "worker")
        self.assertEqual(streak(self.project), 2)
        self.assertEqual(health(self.project)["consecutive_failures"], 2)
        self.assertFalse(self.project.control()["paused"])
        # Next admitted unit rotates to a fresh conversation before dispatch.
        capture = ScriptDriver()
        result = Engine(self.config, driver=capture).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        run_dir = self.project.root / "runs" / result["run"]
        rotation = read_json(run_dir / "rotation.json")
        self.assertTrue(rotation["rotated"])
        self.assertIn("missing-finish units 2 >=2", rotation["reason"])
        self.assertEqual(rotation["generation"], 1)
        self.assertEqual(session_generation(path.parent), 1)
        self.assertFalse(path.exists())
        projection = read_json(run_dir / "prompt_projection.json")
        self.assertEqual(projection["prompt_mode"], "full")
        self.assertIn("missing-finish units 2 >=2", projection["rotation"])
        # A sealed unit resets the streak.
        self.assertEqual(streak(self.project), 0)
        self.assertEqual(health(self.project)["consecutive_failures"], 0)

    def test_success_resets_streak(self):
        seed_open_work(self.project)
        with self.assertRaises(MissingFinish):
            Engine(self.config, driver=NoFinishDriver()).run(self.project, "worker")
        self.assertEqual(streak(self.project), 1)
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(streak(self.project), 0)
        self.assertEqual(health(self.project)["consecutive_failures"], 0)

    def test_non_missing_failure_resets_streak(self):
        seed_open_work(self.project)
        with self.assertRaises(MissingFinish):
            Engine(self.config, driver=NoFinishDriver()).run(self.project, "worker")
        self.assertEqual(streak(self.project), 1)

        class BoomDriver:
            requires_sandbox = False

            def execute(self, context, prompt, *, profile=None):
                context.handle("_hello", {})
                context.handle("_budget", {"sequence": 1})
                raise ProtocolError("synthetic transport fault")

        with self.assertRaises(ProtocolError):
            Engine(self.config, driver=BoomDriver()).run(self.project, "worker")
        self.assertEqual(streak(self.project), 0)
        self.assertEqual(health(self.project)["consecutive_failures"], 2)

    def test_no_open_work_never_rotates(self):
        # Empty queue: no open work at all.
        path = save_worker_session(self)
        for _ in range(2):
            with self.assertRaises(MissingFinish):
                Engine(self.config, driver=NoFinishDriver()).run(self.project, "worker")
        self.assertEqual(streak(self.project), 2)
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        run_dir = self.project.root / "runs" / result["run"]
        self.assertFalse((run_dir / "rotation.json").exists())
        self.assertEqual(session_generation(path.parent), 0)
        projection = read_json(run_dir / "prompt_projection.json")
        self.assertNotIn("missing-finish", projection.get("rotation", ""))

    def test_blocked_overflow_still_rotates_for_later_ready_work(self):
        # Fill the bounded general open page with older blocked rows so it
        # shows only blocked work, then seed one later actionable open item
        # beyond the page. The filtered ready-work scan must still rotate.
        limit = 2
        self.config = dataclasses.replace(
            self.config, limits=dataclasses.replace(
                self.config.limits, work_items_prompt=limit))
        for index in range(limit):
            proposal = self.project.insights.submit(
                source="worker", title=f"Blocked filler {index}",
                body="Fill the bounded general page", base_snapshot=None)
            decision = self.project.insights.decide(
                proposal["id"], "accept", "reviewed", "", "run-1", expected_rev=1)
            item_id = decision["work_item"]["id"]
            self.project.work_items.transition(
                item_id, expected_rev=1, status="blocked", actor="worker")
        ready_proposal = self.project.insights.submit(
            source="worker", title="Later actionable work",
            body="Ready work beyond the blocked page", base_snapshot=None)
        ready_decision = self.project.insights.decide(
            ready_proposal["id"], "accept", "reviewed", "", "run-1", expected_rev=1)
        ready_id = ready_decision["work_item"]["id"]
        general = self.project.work_items.page_open(limit=limit)
        self.assertEqual(len(general["items"]), limit)
        self.assertTrue(all(row["status"] == "blocked" for row in general["items"]))
        ready = self.project.work_items.page_ready(limit=1)
        self.assertTrue(ready["items"])
        self.assertEqual(ready["items"][0]["id"], ready_id)
        path = save_worker_session(self)
        for _ in range(2):
            with self.assertRaises(MissingFinish):
                Engine(self.config, driver=NoFinishDriver()).run(self.project, "worker")
        self.assertEqual(streak(self.project), 2)
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        run_dir = self.project.root / "runs" / result["run"]
        rotation = read_json(run_dir / "rotation.json")
        self.assertTrue(rotation["rotated"])
        self.assertIn("missing-finish units 2 >=2", rotation["reason"])
        self.assertEqual(session_generation(path.parent), 1)
        self.assertFalse(path.exists())

    def test_fully_blocked_queue_never_rotates(self):
        item_id = seed_open_work(self.project)
        self.project.work_items.transition(
            item_id, expected_rev=1, status="blocked", actor="worker")
        page = self.project.work_items.page_open(limit=100)
        self.assertTrue(page["total"] >= 1)
        self.assertTrue(all(row["status"] == "blocked" for row in page["items"]))
        path = save_worker_session(self)
        for _ in range(2):
            with self.assertRaises(MissingFinish):
                Engine(self.config, driver=NoFinishDriver()).run(self.project, "worker")
        self.assertEqual(streak(self.project), 2)
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        run_dir = self.project.root / "runs" / result["run"]
        self.assertFalse((run_dir / "rotation.json").exists())
        self.assertEqual(session_generation(path.parent), 0)

    def test_rotated_session_failure_still_hits_brake(self):
        seed_open_work(self.project)
        save_worker_session(self)
        for _ in range(2):
            with self.assertRaises(MissingFinish):
                Engine(self.config, driver=NoFinishDriver()).run(self.project, "worker")
        self.assertFalse(self.project.control()["paused"])
        # The rotated session also fails missing: third identical failure
        # reaches max_failures=3 and pauses the project.
        with self.assertRaises(MissingFinish):
            Engine(self.config, driver=NoFinishDriver()).run(self.project, "worker")
        final = health(self.project)
        self.assertEqual(final["consecutive_failures"], 3)
        self.assertEqual(final.get("consecutive_missing_finish"), 3)
        self.assertTrue(self.project.control()["paused"])

    def test_threshold_validation_rejects_out_of_range(self):
        raw = self.file.read_text()
        if "missing_finish_recovery_after" in raw:
            template = raw
        else:
            template = raw.replace(
                "missing_finish_counts_as_failure = true",
                "missing_finish_counts_as_failure = true\nmissing_finish_recovery_after = 2")
            self.file.write_text(template)
            raw = template
        for invalid in ("0", "101", "-1"):
            candidate = raw.replace(
                "missing_finish_recovery_after = 2",
                f"missing_finish_recovery_after = {invalid}")
            self.file.write_text(candidate)
            with self.assertRaises(ConfigError):
                load(self.file)
        boundary = raw.replace("missing_finish_recovery_after = 2", "missing_finish_recovery_after = 100")
        self.file.write_text(boundary)
        self.assertEqual(load(self.file).limits.missing_finish_recovery_after, 100)
        self.file.write_text(raw.replace("missing_finish_recovery_after = 2", "missing_finish_recovery_after = 2")
                              if "missing_finish_recovery_after = 2" in raw else raw)
        self.assertEqual(load(self.file).limits.missing_finish_recovery_after, 2)
