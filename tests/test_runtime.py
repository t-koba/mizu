import dataclasses
import json
import os
import shutil
import time
from unittest.mock import patch
from support import Fixture, ScriptDriver
from mizu.budget import Budget
from mizu.config import load
from mizu.errors import Busy, Cancelled, Denied, LimitExceeded, ProtocolError
from mizu.fs import digest, lock, mkdir, read_json, write_json
from mizu.project import Project, initialize
from mizu.runtime import Engine, should_run


class SnapshotTests(Fixture):
    def test_published_snapshot_survives_worktree_edit(self):
        snap = self.project.snapshots.get()
        (self.project.workspace / "app.py").write_text("VALUE = 3\n")
        self.assertEqual(self.project.snapshots.read(snap, "app.py"), b"VALUE = 2\n")

    def test_excludes_secrets_and_symlinks(self):
        (self.project.workspace / ".env").write_text("SECRET=private")
        real = self.root / "real-outside.txt"
        real.write_text("outside")
        (self.project.workspace / "outside").symlink_to(real)
        captured = self.project.snapshots.capture_files(self.project.workspace)
        self.assertNotIn(".env", captured["files"])
        self.assertIn("outside", captured["skipped"])
        self.assertFalse(any(p.startswith(".git/") for p in captured["files"]))

    def test_snapshot_integrity(self):
        snap = self.project.snapshots.get()
        path = self.project.root / "snapshots" / (snap["id"] + ".json")
        data = read_json(path)
        data["summary"] = "tampered"
        write_json(path, data)
        with self.assertRaises(Denied):
            self.project.snapshots.get()

    def test_object_integrity(self):
        snap = self.project.snapshots.get()
        sha = snap["files"]["app.py"]["sha256"]
        (self.project.root / "objects" / sha).write_text("tampered")
        with self.assertRaises(Denied):
            self.project.snapshots.read(snap, "app.py")

    def test_initialization_cannot_overwrite(self):
        with self.assertRaises(Denied):
            initialize(self.config, "sample", self.root / "source", self.goal, ["worker"], [])

    def test_failed_initialization_is_not_published(self):
        config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, file_bytes=2))
        with self.assertRaises(LimitExceeded):
            initialize(config, "failed", self.root / "source", self.goal, ["worker"], [])
        self.assertFalse((self.config.data / "projects/failed").exists())

    def test_init_posture_defaults_unarmed(self):
        project = initialize(self.config, "calm", self.root / "source", self.goal, ["worker"], [])
        self.assertEqual(project.control()["armed"], False)
        self.assertEqual(project.control()["paused"], True)

    def test_init_armed_is_an_explicit_operator_choice(self):
        project = initialize(self.config, "eager", self.root / "source", self.goal, ["worker"], [], armed=True)
        self.assertEqual(project.control()["armed"], True)
        self.assertEqual(project.control()["paused"], False)

    def test_history_index_bounds_prompt_context(self):
        from mizu.runtime import Context, prompt_for
        import uuid
        for n in range(4):
            (self.project.workspace / "app.py").write_text(f"VALUE = {n}\n")
            captured = self.project.snapshots.capture_files(self.project.workspace)
            self.project.snapshots.publish(self.project.snapshots.create(
                captured, goal=self.project.goal, state=f"state {n}", run=None,
                outcome="continue", summary=f"work {n}"))
        config = dataclasses.replace(self.config,
                                     limits=dataclasses.replace(self.config.limits, prompt_snapshots=2))
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        ctx = Context(config, self.project, config.roles["worker"], run,
                      self.project.snapshots.get(), self.project.workspace)
        prompt = json.loads(prompt_for(ctx))
        self.assertEqual(len(prompt["recent_snapshots"]), 2)

    def test_history_index_truncation_is_configured(self):
        from mizu.snapshot import Snapshots
        store = Snapshots(self.root / "hstore", excludes=(), max_file=1024,
                          max_bytes=1048576, max_files=100, history_index=8)
        source = self.root / "hsrc"
        source.mkdir()
        (source / "app.py").write_text("v0\n")
        for n in range(12):
            (source / "app.py").write_text(f"v{n}\n")
            captured = store.capture_files(source)
            store.publish(store.create(captured, goal="g", state="s", run=None,
                                       outcome="continue", summary=f"w{n}"))
        self.assertEqual(len(store.history(store.get()["id"], 100)), 8)


class ContextTests(Fixture):
    def test_capability_refusal(self):
        ctx = self.context("searcher")
        with self.assertRaises(Denied):
            ctx.handle("exec", {"script": "echo no"})
        with self.assertRaises(Denied):
            ctx.handle("decide", {"id": "x", "action": "accept", "reason": "no"})

    def test_extra_arguments_refused(self):
        with self.assertRaises(Denied):
            self.context().handle("read", {"path": "app.py", "source": "operator"})

    def test_finish_seals_tools_and_model_admission(self):
        ctx = self.context()
        ctx.handle("finish", {"outcome": "wait", "summary": "Observed", "state": "Waiting"})
        with self.assertRaises(Denied):
            ctx.handle("read", {"path": "app.py"})
        with self.assertRaises(Denied):
            ctx.handle("_budget", {"sequence": 1})

    def test_done_requires_verification(self):
        with self.assertRaises(Denied):
            self.context().handle("finish", {"outcome": "done", "summary": "Trust me", "state": "Done"})

    def test_pause_revokes_running_tools(self):
        ctx = self.context()
        self.project.set_control(paused=True)
        with self.assertRaises(Cancelled):
            ctx.handle("read", {"path": "app.py"})

    def test_request_budget_is_shared_and_idempotent(self):
        ctx = self.context()
        ctx.handle("_budget", {"sequence": 1})
        ctx.handle("_budget", {"sequence": 1})
        self.context("searcher").handle("_budget", {"sequence": 1})
        self.assertEqual(Budget(self.config.data / "budget", 100).usage()["used"], 2)

    def test_observer_goal_is_anchored_to_snapshot(self):
        before = self.project.snapshots.get()["goal"]
        (self.project.root / "PROJECT.md").write_text("A new operator goal")
        self.assertEqual(self.context("reviewer").goal, before)
        self.assertEqual(self.context("worker").goal, "A new operator goal")

    def test_verification_records_the_exact_code_digest(self):
        ctx = self.context()
        def successful(workspace, script, **kwargs):
            return {"id": "proof", "script": script, "exit_code": 0, "reason": "exited"}
        with patch.object(ctx.sandbox, "execute", side_effect=successful):
            result = ctx.handle("verify", {})
        self.assertTrue(result["passed"])
        ctx.handle("finish", {"outcome": "done", "summary": "Confirmed", "state": "Completed"})
        self.assertEqual(ctx.finished["outcome"], "done")

    def test_changing_code_during_verification_invalidates_it(self):
        ctx = self.context()
        def mutates(workspace, script, **kwargs):
            (workspace / "app.py").write_text("VALUE = 9\n")
            return {"id": "proof", "script": script, "exit_code": 0, "reason": "exited"}
        with patch.object(ctx.sandbox, "execute", side_effect=mutates):
            self.assertFalse(ctx.handle("verify", {})["passed"])


class EngineTests(Fixture):
    def test_complete_work_unit_publishes(self):
        old = self.project.snapshots.get()["id"]
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertNotEqual(self.project.snapshots.get()["id"], old)
        self.assertFalse(list((self.project.root / "active").glob("*.json")))

    def test_failure_preserves_uncommitted_changes_not_snapshot(self):
        old = self.project.snapshots.get()["id"]
        def fails(ctx, *_):
            (ctx.workspace / "app.py").write_text("VALUE = 9\n")
            raise ProtocolError("transport interrupted")
        with self.assertRaises(ProtocolError):
            Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        self.assertEqual(self.project.snapshots.get()["id"], old)
        self.assertEqual((self.project.workspace / "app.py").read_text(), "VALUE = 9\n")
        self.assertEqual(len(list((self.project.root / "runs").glob("*/error.json"))), 1)

    def test_second_writer_cannot_run(self):
        with lock(self.project.root / "locks/workspace.lock"):
            with self.assertRaises(Busy):
                Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")

    def test_unchanged_review_avoids_model_call(self):
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        engine.run(self.project, "reviewer")
        self.assertEqual(engine.run(self.project, "reviewer")["skipped"], "unchanged")
        self.assertEqual(len(driver.calls), 1)

    def test_repeated_failure_pauses_project(self):
        def fails(*_):
            raise ProtocolError("failure")
        for _ in range(3):
            with self.assertRaises(ProtocolError):
                Engine(self.config, driver=ScriptDriver(fails)).run(self.project, "worker")
        self.assertTrue(self.project.control()["paused"])

    def test_zero_max_failures_never_auto_pauses(self):
        self.file.write_text(self.file.read_text().replace("max_failures = 3", "max_failures = 0"))
        config = load(self.file)
        project = Project(config, "sample")
        def fails(*_):
            raise ProtocolError("failure")
        for _ in range(4):
            with self.assertRaises(ProtocolError):
                Engine(config, driver=ScriptDriver(fails)).run(project, "worker")
        self.assertFalse(project.control()["paused"])
        self.assertEqual(read_json(project.root / "health" / "worker.json")["consecutive_failures"], 4)

    def test_arrival_during_work_is_not_marked_seen(self):
        def incoming(ctx, *_):
            ctx.project.insights.submit(source="editor", title="new", body="arrived mid-run", base_snapshot=ctx.snapshot["id"])
        Engine(self.config, driver=ScriptDriver(incoming)).run(self.project, "worker")
        self.assertTrue(should_run(self.project, self.project.snapshots.get()))

    def test_explicit_wake_during_work_survives(self):
        def wake(ctx, *_):
            ctx.project.set_control(wake_generation="new-wake")
        Engine(self.config, driver=ScriptDriver(wake)).run(self.project, "worker")
        self.assertTrue(should_run(self.project, self.project.snapshots.get()))

    def test_wait_does_not_spin(self):
        Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        snap = self.project.snapshots.get()
        self.assertFalse(should_run(self.project, snap, current_time=snap["wake_at"] - 1))
        self.assertTrue(should_run(self.project, snap, current_time=snap["wake_at"] + 1))

    def test_code_change_wait_wakes_writer(self):
        snap = self.project.snapshots.get()
        proposal = self.project.insights.submit(source="worker", title="Risk", body="evidence",
                                                base_snapshot=snap["id"])
        self.project.insights.decide(proposal["id"], "defer", "recheck after edits", "when code changes",
                                     "worker", wait={"kind": "code_change"}, expected_rev=1)
        (self.project.workspace / "app.py").write_bytes(b"VALUE = 3\n")
        Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        # The wait came due after the run published: no inbox, goal, or wake
        # change marks it, so only the event check admits the next dispatch.
        self.assertTrue(should_run(self.project, self.project.snapshots.get()))
        self.assertTrue(should_run(self.project, self.project.snapshots.get(), role_name="worker"))
        # An unnamed check covers the same write set.
        self.assertTrue(should_run(self.project, self.project.snapshots.get(), role_name="ghost"))

    def test_decision_event_wakes_writer(self):
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], decision_events=("reject",))
        config = dataclasses.replace(self.config, roles=roles)
        project = Project(config, "sample")
        proposal = project.insights.submit(source="worker", title="Idea", body="evidence",
                                           base_snapshot=project.snapshots.get()["id"])
        Engine(config, driver=ScriptDriver()).run(project, "worker")
        self.assertFalse(should_run(project, project.snapshots.get(), role_name="worker"))
        # A rejection leaves no inbox-generation footprint, so only the
        # event check admits the next dispatch.
        project.insights.decide(proposal["id"], "reject", "not now", "", "worker", expected_rev=1)
        self.assertTrue(should_run(project, project.snapshots.get(), role_name="worker"))
        self.assertTrue(should_run(project, project.snapshots.get()))

    def test_done_does_not_reopen_for_news(self):
        snap = {**self.project.snapshots.get(), "outcome": "done", "wake_generation": "test-wake"}
        self.project.insights.submit(source="searcher", title="News", body="interesting", base_snapshot=snap["id"])
        self.assertFalse(should_run(self.project, snap))
        self.project.set_control(wake_generation="operator-new")
        self.assertTrue(should_run(self.project, snap))

    def test_consult_same_snapshot_and_isolated_failure(self):
        def answer(ctx, prompt, profile):
            if profile == "alternate":
                raise ProtocolError("one provider unavailable")
            ctx.handle("finish", {"outcome": "wait", "summary": "Independent analysis"})
        engine = Engine(self.config, driver=ScriptDriver(answer))
        parent = self.context()
        result = engine.consult(parent, {"question": "What could fail?"})
        self.assertEqual({x["snapshot"] for x in result["answers"]}, {result["snapshot"]})
        self.assertEqual(len(result["answers"]), 2)
        self.assertEqual(result["answers"][0]["answer"], "Independent analysis")
        self.assertIn("error", result["answers"][1])

    def test_agent_cannot_select_unapproved_consult_model(self):
        with self.assertRaises(Denied):
            Engine(self.config, driver=ScriptDriver()).consult(self.context(), {"question": "x", "profiles": ["unapproved"]})

    def test_consult_role_is_operator_chosen_not_hardcoded(self):
        import dataclasses
        from mizu.protocol import DEFINITIONS, validate
        validate({"question": "q?", "role": "advisor"}, DEFINITIONS["consult"][1])
        role = dataclasses.replace(self.config.roles["consult"], name="advisor")
        config = dataclasses.replace(self.config, roles={**self.config.roles, "advisor": role})
        engine = Engine(config, driver=ScriptDriver())
        result = engine.consult(self.context(), {"question": "q?", "role": "advisor"})
        self.assertEqual(result["role"], "advisor")
        self.assertNotIn("aggregation", result)

    def test_consult_refuses_non_readonly_role(self):
        from mizu.errors import ConfigError
        engine = Engine(self.config, driver=ScriptDriver())
        with self.assertRaises(ConfigError):
            engine.consult(self.context(), {"question": "q?", "role": "worker"})
        with self.assertRaises(ConfigError):
            engine.consult(self.context(), {"question": "q?", "role": "ghost"})


class PromptProjectionTests(Fixture):
    def test_pending_and_acceptance_are_capability_gated(self):
        import json
        from mizu.runtime import prompt_for
        self.project.insights.submit(source="searcher", title="Idea", body="Evidence",
                                     base_snapshot=self.project.snapshots.get()["id"])
        worker = json.loads(prompt_for(self.context("worker")))
        self.assertEqual(len(worker["pending_insights"]), 1)
        self.assertEqual(worker["acceptance_commands"], ["python3 -m unittest"])
        searcher = json.loads(prompt_for(self.context("searcher")))
        self.assertEqual(searcher["pending_insights"], [])
        self.assertEqual(searcher["acceptance_commands"], [])
        consult = json.loads(prompt_for(self.context("consult")))
        self.assertEqual(consult["pending_insights"], [])
        self.assertEqual(consult["acceptance_commands"], [])
        reviewer = json.loads(prompt_for(self.context("reviewer")))
        self.assertEqual(reviewer["pending_insights"], [])
        self.assertEqual(reviewer["acceptance_commands"], [])
        reporter = json.loads(prompt_for(self.context("reporter")))
        self.assertEqual(len(reporter["pending_insights"]), 1)
        self.assertEqual(reporter["acceptance_commands"], [])

    def test_exact_snapshot_references_are_pinned(self):
        import json
        from mizu.runtime import prompt_for
        prompt = json.loads(prompt_for(self.context("worker")))
        snap = self.project.snapshots.get()
        self.assertEqual(prompt["published_snapshot"]["id"], snap["id"])
        self.assertEqual(prompt["published_snapshot"]["code_digest"], snap["code_digest"])
        self.assertEqual(prompt["published_snapshot"]["state"], snap["state"])
        for entry in prompt["recent_snapshots"]:
            self.assertIn("id", entry)
            self.assertIn("code_digest", entry)

    def test_run_records_prompt_projection_evidence(self):
        import json
        from mizu.fs import read_json
        before = self.project.snapshots.get()["id"]
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        projection = read_json(self.project.root / "runs" / result["run"] / "prompt_projection.json")
        self.assertEqual(projection["run"], result["run"])
        self.assertEqual(projection["snapshot"], before)
        self.assertGreaterEqual(projection["prompt_bytes"], 100)
        self.assertGreaterEqual(projection["recent_snapshots"], 1)

class PromptClockTests(Fixture):
    def test_prompt_carries_current_time_and_zone_last(self):
        import json
        from mizu.runtime import prompt_for, prompt_delta_for
        prompt = json.loads(prompt_for(self.context("worker")))
        self.assertEqual(prompt["timezone"], self.config.timezone)
        self.assertIn("now", prompt)
        self.assertIn("+", prompt["now"].replace("Z", "+") if prompt["now"] else "+")
        keys = list(json.loads(prompt_for(self.context("worker"))).keys())
        self.assertEqual(keys[-2:], ["now", "timezone"])
        delta = json.loads(prompt_delta_for(self.context("worker"), []))
        self.assertEqual(delta["timezone"], self.config.timezone)
        self.assertIn("now", delta)
        self.assertEqual(list(delta.keys())[-2:], ["now", "timezone"])

class PreviousReportTests(Fixture):
    def test_absent_artifact_reads_as_none(self):
        import json
        from mizu.runtime import prompt_for, prompt_delta_for
        self.assertIsNone(json.loads(prompt_for(self.context("reporter")))["previous_report"])
        self.assertIsNone(json.loads(prompt_delta_for(self.context("reporter"), []))["previous_report"])
        self.assertIsNone(json.loads(prompt_for(self.context("worker")))["previous_report"])

    def test_report_role_sees_previous_edition(self):
        import json
        from mizu.report import publish
        from mizu.runtime import prompt_for, prompt_delta_for
        snap = self.project.snapshots.get()
        record = publish(self.project, snap, {"title": "Edition one", "body": "shipped green"})
        prompt = json.loads(prompt_for(self.context("reporter")))
        previous = prompt["previous_report"]
        self.assertEqual(previous["artifact"], record["artifact"])
        self.assertEqual(previous["snapshot"], snap["id"])
        self.assertIn("shipped green", previous["body"])
        self.assertFalse(previous["body_truncated"])
        # Capability-gated: worker sees none even with an edition recorded.
        self.assertIsNone(json.loads(prompt_for(self.context("worker")))["previous_report"])
        # Delta prompts carry it too so resumed editions stay current.
        delta = json.loads(prompt_delta_for(self.context("reporter"), []))
        self.assertEqual(delta["previous_report"]["artifact"], record["artifact"])
        # Clock stays last in both shapes.
        self.assertEqual(list(prompt.keys())[-2:], ["now", "timezone"])
        self.assertEqual(list(delta.keys())[-2:], ["now", "timezone"])

    def test_previous_edition_body_is_bounded(self):
        import json
        from mizu.report import publish, PREVIOUS_REPORT_BYTES
        from mizu.runtime import prompt_for
        snap = self.project.snapshots.get()
        publish(self.project, snap, {"title": "Long", "body": "x" * 20000})
        previous = json.loads(prompt_for(self.context("reporter")))["previous_report"]
        self.assertTrue(previous["body_truncated"])
        self.assertLessEqual(len(previous["body"].encode("utf-8")), PREVIOUS_REPORT_BYTES)

    def test_corrupt_pointer_reads_as_none(self):
        import json
        from mizu.fs import mkdir
        from mizu.runtime import prompt_for
        root = self.project.root / "artifacts"
        mkdir(root)
        (root / "latest.json").write_text("{broken")
        self.assertIsNone(json.loads(prompt_for(self.context("reporter")))["previous_report"])

    def test_large_multibyte_edition_still_previews(self):
        import json
        from mizu.report import publish, PREVIOUS_REPORT_BYTES
        from mizu.runtime import prompt_for
        snap = self.project.snapshots.get()
        publish(self.project, snap, {"title": "Emoji", "body": "\U0001F600" * 48000})
        previous = json.loads(prompt_for(self.context("reporter")))["previous_report"]
        self.assertIsNotNone(previous)
        self.assertTrue(previous["body_truncated"])
        self.assertLessEqual(len(previous["body"].encode("utf-8")), PREVIOUS_REPORT_BYTES)

class InsightRetrievalTests(Fixture):
    def test_explicit_paging_reaches_beyond_prompt_selection(self):
        from mizu.runtime import Context
        snap = self.project.snapshots.get()["id"]
        for n in range(35):
            self.project.insights.submit(source="searcher", title=f"Idea {n:02d}",
                                         body="Evidence", base_snapshot=snap)
        ctx = self.context("worker")
        first = ctx.handle("insights", {"offset": 0, "limit": 1000})
        self.assertEqual(first["total"], 35)
        self.assertEqual(len(first["insights"]), 35)
        # Prompt selection stays bounded.
        import json
        from mizu.runtime import prompt_for
        prompt = json.loads(prompt_for(ctx))
        self.assertEqual(len(prompt["pending_insights"]), 30)
        # Overflow page is discoverable without knowing IDs.
        overflow = ctx.handle("insights", {"offset": 30, "limit": 1000})
        self.assertEqual(overflow["total"], 35)
        self.assertEqual(len(overflow["insights"]), 5)
        self.assertFalse(overflow["selection_truncated"])
        seen = [item["id"] for page in (first, overflow) for item in page["insights"]]
        self.assertEqual(len(set(seen)), 35)


class PromptOrderTests(Fixture):
    def test_prompt_key_order_is_fixed(self):
        # Stable key order keeps provider working caches warm: pins
        # first, capability-gated sections in one place, clock last.
        # Any reordering must update this test deliberately.
        import json
        from mizu.runtime import prompt_for, prompt_delta_for
        ctx = self.context("worker")
        self.assertEqual(list(json.loads(prompt_for(ctx)).keys()),
                         ["goal", "published_snapshot", "recent_snapshots",
                          "workspace", "workspace_mode", "pending_insights",
                          "decision_events", "wait_events",
                          "acceptance_commands", "research_state",
                          "ci_branch", "previous_report", "now",
                          "timezone"])
        self.assertEqual(
            list(json.loads(prompt_delta_for(ctx, [])).keys()),
            ["goal_digest", "published_snapshot", "snapshot_delta",
             "pending_insights", "decision_events", "wait_events",
             "research_state", "workspace", "workspace_mode",
             "acceptance_commands", "ci_branch", "previous_report",
             "now", "timezone"])
