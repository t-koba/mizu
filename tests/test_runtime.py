import dataclasses
import json
import os
import shutil
import time
from unittest.mock import patch
from support import Fixture, ScriptDriver
from mizu.budget import Budget
from mizu.config import load
from mizu.errors import Busy, Cancelled, ConfigError, Denied, LimitExceeded, ProtocolError
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

    def test_read_pages_large_files_pins_content_and_bounds_responses(self):
        from mizu.fs import canonical, digest, MAX_FRAME, PREVIEW_BYTES, READ_PAGE_CHARS
        from mizu.protocol import DEFINITIONS, validate
        ctx = self.context()
        raw = b"a" * (PREVIEW_BYTES + 1000) + b"\xff\x00" + b"z" * 500
        (ctx.workspace / "large.txt").write_bytes(raw)
        first = ctx._op_read({"path": "large.txt"})
        self.assertEqual(first["content_id"], digest(raw))
        self.assertEqual(first["offset"], 0)
        self.assertEqual(len(first["text"]), READ_PAGE_CHARS)
        self.assertEqual(first["next_offset"], READ_PAGE_CHARS)
        pages = [first["text"]]
        offset = first["next_offset"]
        while offset is not None:
            result = ctx._op_read({"path": "large.txt", "content_id": first["content_id"],
                                   "offset": offset, "limit": READ_PAGE_CHARS})
            self.assertEqual(result["content_id"], first["content_id"])
            self.assertLessEqual(len(result["text"]), READ_PAGE_CHARS)
            pages.append(result["text"])
            offset = result["next_offset"]
        self.assertEqual("".join(pages), raw.decode("utf-8", "replace"))
        with self.assertRaises(Denied):
            ctx._op_read({"path": "large.txt", "offset": READ_PAGE_CHARS})
        changed = raw[:-1] + b"!"
        (ctx.workspace / "large.txt").write_bytes(changed)
        with self.assertRaisesRegex(Denied, "changed since the previous page"):
            ctx._op_read({"path": "large.txt", "content_id": first["content_id"],
                          "offset": READ_PAGE_CHARS})
        limited = self.context()
        limited.config = dataclasses.replace(
            limited.config,
            limits=dataclasses.replace(limited.config.limits, file_bytes=len(changed) - 1))
        with self.assertRaises(LimitExceeded):
            limited._op_read({"path": "large.txt"})
        validate({"path": "large.txt", "content_id": "a" * 64,
                  "offset": READ_PAGE_CHARS, "limit": READ_PAGE_CHARS}, DEFINITIONS["read"][1])
        with self.assertRaises(Denied):
            validate({"path": "large.txt", "limit": READ_PAGE_CHARS + 1}, DEFINITIONS["read"][1])
        controls = b"\x01" * READ_PAGE_CHARS
        (ctx.workspace / "controls.txt").write_bytes(controls)
        result = ctx._op_read({"path": "controls.txt"})
        frame = canonical({"content": [{"text": canonical(result).decode()}]})
        self.assertLess(len(frame), MAX_FRAME)

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
        started = read_json(self.project.root / "runs" / result["run"] / "started.json")
        self.assertIs(type(started["admission_elapsed_seconds"]), float)
        self.assertGreaterEqual(started["admission_elapsed_seconds"], 0.0)
        self.assertLess(started["admission_elapsed_seconds"], 5.0)
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
        import dataclasses
        def answer(ctx, prompt, profile):
            if profile == "alternate":
                raise ProtocolError("one provider unavailable")
            ctx.handle("finish", {"outcome": "wait", "summary": "Independent analysis"})
        roles = dict(self.config.roles)
        roles["advisor-a"] = dataclasses.replace(self.config.roles["consult"], name="advisor-a", profile="primary")
        roles["advisor-b"] = dataclasses.replace(self.config.roles["consult"], name="advisor-b", profile="alternate")
        config = dataclasses.replace(self.config, roles=roles)
        engine = Engine(config, driver=ScriptDriver(answer))
        result = self.admitted_call(engine, "consult", {"question": "What could fail?", "roles": ["advisor-a", "advisor-b"]})
        self.assertEqual({x["snapshot"] for x in result["answers"]}, {result["snapshot"]})
        self.assertEqual(len(result["answers"]), 2)
        self.assertEqual(result["roles"], ["advisor-a", "advisor-b"])
        self.assertEqual(result["answers"][0]["answer"], "Independent analysis")
        self.assertEqual(result["answers"][0]["role"], "advisor-a")
        self.assertIn("error", result["answers"][1])
        self.assertEqual(result["answers"][1]["role"], "advisor-b")
        for answer in result["answers"]:
            child_dir = self.project.root / "runs" / answer["run"]
            evidence_path = child_dir / ("error.json" if "error" in answer else "consultation.json")
            activity = read_json(evidence_path)["activity"]
            self.assertEqual(activity["schema"], 1)
            self.assertGreaterEqual(activity["elapsed_seconds"], 0.0)

    def test_agent_cannot_select_unapproved_consult_model(self):
        with self.assertRaises(ConfigError):
            Engine(self.config, driver=ScriptDriver()).consult(self.context(), {"question": "x", "roles": ["unapproved"]})
        with self.assertRaises(Denied):
            Engine(self.config, driver=ScriptDriver()).consult(self.context(), {"question": "x", "roles": []})
        with self.assertRaises(Denied):
            Engine(self.config, driver=ScriptDriver()).consult(self.context(), {"question": "x", "roles": ["consult", "consult"]})
        with self.assertRaises(Denied):
            Engine(self.config, driver=ScriptDriver()).consult(self.context(), {"question": "x", "roles": ["consult"], "role": "consult"})

    def test_consult_role_is_operator_chosen_not_hardcoded(self):
        import dataclasses
        from mizu.protocol import DEFINITIONS, validate
        validate({"question": "q?", "role": "advisor"}, DEFINITIONS["consult"][1])
        role = dataclasses.replace(self.config.roles["consult"], name="advisor")
        config = dataclasses.replace(self.config, roles={**self.config.roles, "advisor": role})
        engine = Engine(config, driver=ScriptDriver())
        result = self.admitted_call(engine, "consult", {"question": "q?", "role": "advisor"})
        self.assertEqual(result["roles"], ["advisor"])
        self.assertNotIn("aggregation", result)

    def test_consult_uses_answering_role_own_profile(self):
        seen = []
        def answer(ctx, prompt, profile):
            seen.append(profile)
            ctx.handle("finish", {"outcome": "wait", "summary": "Own model"})
        engine = Engine(self.config, driver=ScriptDriver(answer))
        result = self.admitted_call(engine, "consult", {"question": "Which model answers?"})
        self.assertEqual(result["roles"], [self.config.consult_role])
        self.assertEqual(len(result["answers"]), 1)
        own = self.config.roles[self.config.consult_role].profile
        self.assertEqual(result["answers"][0]["profile"], own)
        self.assertEqual(seen, [own])

    def test_consult_selector_role_resolves_via_own_selector(self):
        import dataclasses
        roles = dict(self.config.roles)
        roles["consult"] = dataclasses.replace(
            self.config.roles["consult"], profile="", selector="pick")
        selectors = {"pick": {"retry_seconds": 60, "recommendation_validity": "once",
                              "rules": [{"when": {}, "candidates": [{"profile": "primary"}]}]}}
        config = dataclasses.replace(self.config, roles=roles, selectors=selectors)
        engine = Engine(config, driver=ScriptDriver())
        result = self.admitted_call(engine, "consult", {"question": "q?"})
        self.assertEqual(result["roles"], [config.consult_role])
        self.assertEqual(result["answers"][0]["profile"], "primary")
        self.assertEqual(result["answers"][0]["role"], config.consult_role)

    def test_consult_config_only_gate_applies_to_selector_and_fixed(self):
        # Parity: a configured-but-not-project-enabled read-only role consults
        # the same whether fixed-profile or selector-resolved (config.roles
        # gate only; no project-membership check for ephemeral children).
        import dataclasses
        roles = dict(self.config.roles)
        roles["outsider"] = dataclasses.replace(self.config.roles["consult"], name="outsider")
        roles["sel-outsider"] = dataclasses.replace(
            self.config.roles["consult"], name="sel-outsider", profile="", selector="pick")
        selectors = {"pick": {"retry_seconds": 60, "recommendation_validity": "once",
                              "rules": [{"when": {}, "candidates": [{"profile": "primary"}]}]}}
        config = dataclasses.replace(self.config, roles=roles, selectors=selectors)
        engine = Engine(config, driver=ScriptDriver())
        result = self.admitted_call(engine, "consult", {"question": "q?", "roles": ["outsider", "sel-outsider"]})
        self.assertEqual(result["roles"], ["outsider", "sel-outsider"])
        by_role = {a["role"]: a for a in result["answers"]}
        self.assertIn("answer", by_role["outsider"])
        self.assertIn("answer", by_role["sel-outsider"])
        self.assertEqual(by_role["sel-outsider"]["profile"], "primary")

    def test_consult_profiles_key_is_refused_at_load(self):
        from mizu.config import load
        from mizu.errors import ConfigError
        text = self.file.read_text()
        self.file.write_text(text + 'consult_profiles = ["primary"]\n')
        with self.assertRaises(ConfigError):
            load(self.file)

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

    def test_small_pages_enumerate_each_pending_item_once(self):
        snap = self.project.snapshots.get()["id"]
        submitted = [self.project.insights.submit(source="searcher", title=f"Idea {n}",
                                                  body="Evidence", base_snapshot=snap)["id"]
                     for n in range(5)]
        ctx = self.context("worker")
        seen, offset, pages = [], 0, 0
        while True:
            response = ctx.handle("insights", {"offset": offset, "limit": 2})
            self.assertEqual(response["total"], 5)
            seen.extend(item["id"] for item in response["insights"])
            pages += 1
            if response["next_offset"] is None:
                break
            offset = response["next_offset"]
            self.assertLess(pages, 10)
        self.assertEqual(sorted(seen), sorted(submitted))
        self.assertEqual(len(set(seen)), 5)

    def test_small_pages_enumerate_each_decided_item_once(self):
        snap = self.project.snapshots.get()["id"]
        ids = [self.project.insights.submit(source="searcher", title=f"Idea {n}",
                                            body="Evidence", base_snapshot=snap)["id"]
               for n in range(4)]
        for insight_id in ids[:3]:
            self.project.insights.decide(insight_id, "accept", "sequenced", "", "test", expected_rev=1)
        ctx = self.context("worker")
        seen, offset, pages = [], 0, 0
        while True:
            response = ctx.handle("insights", {"action": "accept", "offset": offset, "limit": 1})
            self.assertEqual(response["total"], 3)
            seen.extend(item["id"] for item in response["insights"])
            pages += 1
            if response["next_offset"] is None:
                break
            offset = response["next_offset"]
            self.assertLess(pages, 10)
        self.assertEqual(sorted(seen), sorted(ids[:3]))
        self.assertEqual(len(set(seen)), 3)

    def test_accepted_work_lists_by_action_after_leaving_pending(self):
        snap = self.project.snapshots.get()["id"]
        ids = [self.project.insights.submit(source="searcher", title=f"Idea {n}",
                                            body="Evidence", base_snapshot=snap)["id"]
               for n in range(3)]
        self.project.insights.decide(ids[0], "accept", "sequenced", "", "test", expected_rev=1)
        self.project.insights.decide(ids[1], "modify", "scoped", "", "test", expected_rev=1)
        ctx = self.context("worker")
        pending = ctx.handle("insights", {"offset": 0, "limit": 1000})
        self.assertEqual(pending["total"], 1)
        self.assertEqual([item["id"] for item in pending["insights"]], [ids[2]])
        accepted = ctx.handle("insights", {"action": "accept", "offset": 0, "limit": 1000})
        self.assertEqual(accepted["total"], 1)
        self.assertEqual([item["id"] for item in accepted["insights"]], [ids[0]])
        modified = ctx.handle("insights", {"action": "modify", "offset": 0, "limit": 1000})
        self.assertEqual(modified["total"], 1)
        self.assertEqual([item["id"] for item in modified["insights"]], [ids[1]])
        rejected = ctx.handle("insights", {"action": "reject", "offset": 0, "limit": 1000})
        self.assertEqual(rejected["total"], 0)
        from mizu.errors import Denied
        with self.assertRaises(Denied):
            ctx.handle("insights", {"action": "withdraw"})
        with self.assertRaises(Denied):
            ctx.handle("insights", {"action": "bogus"})

class ActivityEvidenceTests(Fixture):
    def test_activity_bucket_precedence_is_exclusive(self):
        ctx = self.context()
        ctx.request_sequences.add((ctx.execution_attempt, 1))
        clock = [100.0]
        with patch("mizu.runtime.time.monotonic", side_effect=lambda: clock[0]):
            ctx._activity_origin = ctx._activity_last = clock[0]
            ctx._activity_current = "other"
            ctx._activity_seconds.clear()
            ctx._activity_counts.clear()
            clock[0] = 101.0
            ctx.start_request(1)
            clock[0] = 103.0
            ctx._activity_change(lambda: setattr(ctx, "_activity_operation", "exec"),
                                 count="tool_call:exec")
            clock[0] = 107.0
            ctx._activity_change(lambda: setattr(ctx, "_activity_join", "consult"),
                                 count="join:consult")
            clock[0] = 112.0
            ctx._activity_change(lambda: setattr(ctx, "_activity_join", None))
            clock[0] = 114.0
            ctx._activity_change(lambda: setattr(ctx, "_activity_operation", None))
            clock[0] = 118.0
            ctx.end_request(1)
            clock[0] = 120.0
            ctx._activity_change(lambda: setattr(ctx, "_activity_operation", "experiment"),
                                 count="tool_call:experiment")
            clock[0] = 122.0
            ctx._activity_change(lambda: setattr(ctx, "_activity_operation", None))
            clock[0] = 123.0
            ctx._activity_change(lambda: setattr(ctx, "_activity_operation", "read"),
                                 count="tool_call:read")
            clock[0] = 126.0
            ctx._activity_change(lambda: setattr(ctx, "_activity_operation", None))
            clock[0] = 128.0
            activity = ctx.activity_summary()

        self.assertEqual(activity["schema"], 1)
        self.assertEqual(activity["elapsed_seconds"], 28.0)
        self.assertEqual(activity["exclusive_seconds"], {
            "join:consult": 5.0,
            "local_exec:exec": 6.0,
            "local_exec:experiment": 2.0,
            "model_request": 6.0,
            "other": 6.0,
            "tool:read": 3.0,
        })
        self.assertEqual(sum(activity["exclusive_seconds"].values()), activity["elapsed_seconds"])
        self.assertEqual(activity["counts"], {
            "join:consult": 1,
            "model_request": 1,
            "tool_call:exec": 1,
            "tool_call:experiment": 1,
            "tool_call:read": 1,
        })
        self.assertEqual(activity["max_concurrent_model_requests"], 1)
        self.assertEqual(activity["open_model_requests"], 0)

    def test_activity_accounting_stays_consistent_when_a_transition_raises(self):
        ctx = self.context()
        clock = [100.0]
        with patch("mizu.runtime.time.monotonic", side_effect=lambda: clock[0]):
            ctx._activity_origin = ctx._activity_last = clock[0]
            ctx._activity_current = "other"
            ctx._activity_seconds.clear()
            clock[0] = 102.0
            with self.assertRaisesRegex(RuntimeError, "transition failed"):
                ctx._activity_change(lambda: (_ for _ in ()).throw(RuntimeError("transition failed")))
            clock[0] = 105.0
            activity = ctx.activity_summary()
        self.assertEqual(activity["elapsed_seconds"], 5.0)
        self.assertEqual(activity["exclusive_seconds"], {"other": 5.0})

    def test_tool_and_join_buckets_close_after_exceptions(self):
        ctx = self.context()
        with patch.object(ctx, "dispatch", side_effect=RuntimeError("tool failed")):
            with self.assertRaisesRegex(RuntimeError, "tool failed"):
                ctx.handle("read", {"path": "app.py"})
        self.assertIsNone(ctx._activity_operation)
        self.assertIn("tool:read", ctx.activity_summary()["exclusive_seconds"])

        def fail_join(*_):
            raise RuntimeError("child failed")

        ctx.consult = fail_join
        with self.assertRaisesRegex(RuntimeError, "child failed"):
            ctx._op_consult({})
        self.assertIsNone(ctx._activity_join)
        self.assertTrue(ctx.joins_idle.is_set())
        activity = ctx.activity_summary()
        self.assertIn("join:consult", activity["exclusive_seconds"])
        self.assertEqual(activity["counts"]["join:consult"], 1)

    def test_request_concurrency_is_per_context_and_aggregate(self):
        ctx = self.context()
        ctx.request_sequences.update({(ctx.execution_attempt, 1), (ctx.execution_attempt, 2)})
        clock = [10.0]
        with patch("mizu.runtime.time.monotonic", side_effect=lambda: clock[0]):
            ctx._activity_origin = ctx._activity_last = clock[0]
            ctx._activity_seconds.clear()
            clock[0] = 11.0
            ctx.start_request(1)
            clock[0] = 12.0
            ctx.start_request(2)
            clock[0] = 13.0
            ctx.end_request(1)
            clock[0] = 14.0
            ctx.end_request(2)
            clock[0] = 15.0
            ctx.request_sequences.add((ctx.execution_attempt, 3))
            ctx.start_request(3)
            clock[0] = 16.0
            ctx.clear_request_deadlines()
            activity = ctx.activity_summary()
        self.assertEqual(activity["max_concurrent_model_requests"], 2)
        self.assertEqual(activity["counts"]["model_request"], 3)
        self.assertEqual(activity["open_model_requests"], 0)
        self.assertEqual(activity["reaped_model_requests"], 1)

    def test_normal_bounded_and_interrupted_runs_persist_activity(self):
        from mizu.fs import read_json

        def check(record, status):
            self.assertEqual(record["status"], status)
            activity = record["activity"]
            self.assertEqual(activity["schema"], 1)
            self.assertGreaterEqual(activity["elapsed_seconds"], 0.0)
            self.assertLessEqual(sum(activity["exclusive_seconds"].values()),
                                 activity["elapsed_seconds"] + 0.00001)
            self.assertGreaterEqual(activity["max_concurrent_model_requests"], 0)

        normal = Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        normal_record = read_json(self.project.root / "runs" / normal["run"] / "result.json")
        check(normal_record, "completed")

        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], requests_per_run=1)
        bounded_config = dataclasses.replace(self.config, roles=roles)

        def bounded(ctx, *_):
            ctx.handle("_budget", {"sequence": 2})
            ctx.handle("finish", {"outcome": "wait", "summary": "sealed under bound",
                                   "state": "Now: bounded.\nNext: resume.\nTasks: none\nBlocking: none"})

        bounded_result = Engine(bounded_config, driver=ScriptDriver(bounded)).run(self.project, "worker")
        bounded_record = read_json(self.project.root / "runs" / bounded_result["run"] / "result.json")
        check(bounded_record, "completed")
        self.assertEqual(bounded_record["finish"]["outcome"], "bounded")

        def interrupted(ctx, *_):
            ctx.handle("_budget", {"sequence": 1})
            raise ProtocolError("synthetic interruption")

        with self.assertRaises(ProtocolError):
            Engine(self.config, driver=ScriptDriver(interrupted)).run(self.project, "worker")
        errors = list((self.project.root / "runs").glob("*/error.json"))
        error = max(errors, key=lambda path: path.stat().st_mtime_ns)
        error_record = read_json(error)
        check(error_record, "interrupted")
        self.assertEqual(error_record["activity"]["open_model_requests"], 1)
        self.assertEqual(error_record["activity"]["reaped_model_requests"], 0)


class WrapUpTests(Fixture):
    def test_requests_bound_ends_bounded_without_brake(self):
        import dataclasses
        from mizu.fs import read_json
        from mizu.runtime import Engine
        from support import ScriptDriver
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], requests_per_run=1)
        config = dataclasses.replace(
            self.config, roles=roles,
            limits=dataclasses.replace(self.config.limits, max_failures=1))
        def callback(ctx, prompt, profile):
            # The first admitted sequence is spent by the driver; the next
            # request hits the bound mid-turn with the model still alive.
            ctx.handle("_budget", {"sequence": 2})
            ctx.handle("finish", {"outcome": "wait", "summary": "saved under bound",
                                  "state": "Now: wrapped.\nNext: resume.\nTasks: none\nBlocking: none"})
        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finish"]["outcome"], "bounded")
        self.assertEqual(result["finish"]["bound"], "requests")
        self.assertEqual(self.project.snapshots.get()["outcome"], "bounded")
        health = read_json(self.project.root / "health" / "worker.json")
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertFalse(self.project.control()["paused"])

    def test_escaped_bound_gets_one_reduced_wrap_up_attempt(self):
        from mizu.errors import LimitExceeded
        from mizu.runtime import Engine, WRAP_UP_OPS
        from support import ScriptDriver
        seen = []
        attempts = []
        def callback(ctx, prompt, profile):
            attempts.append((ctx.execution_attempt, ctx.request_count))
            if not seen:
                seen.append("first")
                ctx.bound_hit = "requests"
                raise LimitExceeded("Per-run provider request budget exhausted")
            seen.append("wrap-up")
            seen.append(tuple(sorted(ctx.role.capabilities)))
            seen.append(tuple(ctx.role.engine_tools))
            ctx.handle("finish", {"outcome": "continue", "summary": "resumed and sealed",
                                  "state": "Now: wrapped.\nNext: resume.\nTasks: none\nBlocking: none"})
        result = Engine(self.config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finish"]["outcome"], "bounded")
        self.assertEqual(result["wrap_up"], {"bound": "requests"})
        grants = seen[2]
        self.assertTrue(set(grants) <= set(WRAP_UP_OPS))
        self.assertIn("finish", grants)
        self.assertEqual(seen[3], ())
        self.assertEqual(attempts, [(1, 1), (2, 2)])

    def test_wrap_up_prompt_states_bound(self):
        from mizu.errors import LimitExceeded
        from mizu.runtime import Engine
        from support import ScriptDriver
        prompts = []
        def callback(ctx, prompt, profile):
            prompts.append(prompt)
            if len(prompts) == 1:
                ctx.bound_hit = "tools"
                raise LimitExceeded("Per-run tool limit exceeded")
            ctx.handle("finish", {"outcome": "continue", "summary": "s",
                                  "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
        Engine(self.config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(len(prompts), 2)
        self.assertIn("Per-run tools budget exhausted", prompts[1])

    def test_no_finish_inside_wrap_up_fails_as_before(self):
        import time
        from mizu.errors import LimitExceeded
        from mizu.fs import read_json
        from mizu.runtime import Engine, should_run
        from support import ScriptDriver
        def callback(ctx, prompt, profile):
            ctx.bound_hit = ctx.bound_hit or "requests"
            raise LimitExceeded("Per-run provider request budget exhausted")
        with self.assertRaises(LimitExceeded):
            Engine(self.config, driver=ScriptDriver(callback)).run(self.project, "worker")
        health = read_json(self.project.root / "health" / "worker.json")
        self.assertEqual(health["consecutive_failures"], 1)

    def test_bounded_snapshot_runs_again(self):
        import time
        from mizu.runtime import should_run
        gen = self.project.insights.generation()
        cap = self.project.snapshots.capture_files(self.project.workspace)
        parked = self.project.snapshots.create(
            cap, goal=self.project.goal, state="Wrapped.", run=None,
            outcome="bounded", summary="Hit the bound after sealing.", verification=None,
            inbox_seen=gen,
            wake_generation=self.project.control().get("wake_generation", ""))
        self.project.snapshots.publish(parked)
        snap = self.project.snapshots.get()
        self.assertEqual(snap["outcome"], "bounded")
        self.assertTrue(should_run(self.project, snap, current_time=time.time()))

    def test_daily_refusal_at_per_run_cap_burns_nothing(self):
        import dataclasses
        from mizu.drivers import admit_invocation
        from mizu.errors import InfraExceeded
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], requests_per_run=1)
        config = dataclasses.replace(
            self.config, roles=roles,
            limits=dataclasses.replace(self.config.limits, daily_requests=0))
        self.project.config = config
        ctx = self.context("worker")
        ctx.config = config
        ctx.role = config.roles["worker"]
        ctx.request_count = 1
        with self.assertRaises(InfraExceeded):
            ctx.handle("_budget", {"sequence": 99})
        self.assertIsNone(ctx.bound_hit)
        self.assertEqual(ctx.wrap_up_requests, 0)
        self.assertTrue(ctx.admission_wait)
        with self.assertRaises(InfraExceeded):
            admit_invocation(ctx, unit="request")
        self.assertIsNone(ctx.bound_hit)
        self.assertEqual(ctx.wrap_up_requests, 0)

    def test_daily_budget_refusal_is_not_wrap_up(self):
        import dataclasses
        from mizu.errors import InfraExceeded
        config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, daily_requests=0))
        self.project.config = config
        ctx = self.context("worker")
        ctx.config = config
        with self.assertRaises(InfraExceeded):
            ctx.handle("_budget", {"sequence": 1})
        self.assertIsNone(ctx.bound_hit)

    def test_read_resolves_unique_prefix(self):
        snap = self.project.snapshots.get()["id"]
        full = self.project.insights.submit(source="searcher", title="Idea",
                                            body="Evidence", base_snapshot=snap)["id"]
        record = self.project.insights.read(full[:8])
        self.assertEqual(record["id"], full)
        self.assertEqual(record["title"], "Idea")
        # The full id still reads exactly.
        self.assertEqual(self.project.insights.read(full)["id"], full)

    def test_ambiguous_prefix_and_garbage_are_refused(self):
        from mizu.errors import Denied
        snap = self.project.snapshots.get()["id"]
        first = "deadbeef" + "1" + "0" * 23
        second = "deadbeef" + "2" + "0" * 23
        self.project.insights.submit(source="searcher", title="One", body="Evidence",
                                     base_snapshot=snap, insight_id=first)
        self.project.insights.submit(source="searcher", title="Two", body="Evidence",
                                     base_snapshot=snap, insight_id=second)
        with self.assertRaises(Denied):
            self.project.insights.read("deadbeef")
        self.assertEqual(self.project.insights.read("deadbeef1")["id"], first)
        with self.assertRaises(Denied):
            self.project.insights.read("f" * 8)
        with self.assertRaises(Denied):
            self.project.insights.read("not an id!")
        with self.assertRaises(Denied):
            self.project.insights.read("a" * 32)


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
                          "open_insights",
                          "decision_events", "decision_task", "wait_events", "obligations",
                          "acceptance_commands", "research_state",
                          "ci_branch", "ci_failures", "previous_report", "now",
                          "timezone"])
        self.assertEqual(
            list(json.loads(prompt_delta_for(ctx, [])).keys()),
            ["goal_digest", "published_snapshot", "snapshot_delta",
             "pending_insights", "open_insights", "decision_events", "decision_task", "wait_events", "obligations",
             "research_state", "workspace", "workspace_mode",
             "acceptance_commands", "ci_branch", "ci_failures", "previous_report",
             "now", "timezone"])
