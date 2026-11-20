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
from mizu.fs import canonical, digest, lock, mkdir, read_json
from support import history_write_json as write_json
from mizu.project import Project, initialize
from mizu.protocol import tool_definitions
from mizu.smoke_refs import shared_lock
from mizu.runtime import (CONSULT_FORBIDDEN, DELEGATE_FORBIDDEN, Engine, should_run,
                          check_consult_role, check_delegate_role)


class SnapshotTests(Fixture):
    def test_review_authority_is_not_a_tool_or_child_grant(self):
        from mizu.protocol import operation_allowed
        self.assertIn("review_verdict", CONSULT_FORBIDDEN)
        self.assertIn("review_verdict", DELEGATE_FORBIDDEN)
        role = dataclasses.replace(self.config.roles["consult"],
                                  capabilities=("finish", "review_verdict"))
        self.assertEqual([tool["name"] for tool in tool_definitions(role.capabilities)],
                         ["mizu_finish"])
        self.assertFalse(operation_allowed("review_verdict", role.capabilities))
        with self.assertRaises(ConfigError):
            check_consult_role(role.name, role)
        with self.assertRaises(ConfigError):
            check_delegate_role(role.name, role)

    def test_published_snapshot_survives_worktree_edit(self):
        snap = self.project.snapshots.get()
        (self.project.workspace / "app.py").write_text("VALUE = 3\n")
        self.assertEqual(self.project.snapshots.read(snap, "app.py"), b"VALUE = 2\n")

    def test_typed_next_step_snapshot_validation_and_legacy_reads(self):
        store = self.project.snapshots
        captured = store.capture_files(self.project.workspace)
        def create(**extra):
            return store.create(captured, goal=self.project.goal, state="Next: Legacy prose",
                                run=None, outcome="continue", summary="Captured", **extra)
        legacy = create()
        self.assertNotIn("next_step", store.get(legacy["id"]))
        typed = create(next_step="Inspect the result")
        self.assertEqual(store.get(typed["id"])["next_step"], "Inspect the result")
        def audit(snapshot):
            return store.audit_manifest(snapshot["id"], metadata_bytes=32768, maximum=1048576)
        self.assertNotIn("next_step", audit(legacy))
        self.assertEqual(audit(typed)["next_step"], "Inspect the result")
        with self.assertRaises(Denied):
            create(next_step="none")
        # A correct content hash does not excuse an invalid typed field, including
        # in the retention-only streaming reader.
        for step in (None, 42, "none", "two\nlines"):
            with self.subTest(step=step):
                forged = {k: v for k, v in typed.items() if k != "id"}
                forged["next_step"] = step
                forged["id"] = digest(canonical(forged))
                write_json(store.root / "snapshots" / (forged["id"] + ".json"), forged)
                with self.assertRaises(Denied):
                    store.get(forged["id"])
                with self.assertRaises(Denied):
                    audit(forged)

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

    def test_read_identity_capacity_preserves_existing_pins(self):
        ctx = self.context()
        (ctx.workspace/'one.txt').write_text('first text')
        (ctx.workspace/'two.txt').write_text('second text')
        with patch('mizu.runtime.MAX_READ_IDENTITIES', 1):
            first = ctx._op_read({'path':'one.txt', 'limit': 2})
            with self.assertRaises(LimitExceeded):
                ctx._op_read({'path':'two.txt'})
            self.assertEqual(ctx._op_read({'path':'one.txt','offset':2})['content_id'], first['content_id'])

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
        implicit = ctx._op_read({"path": "large.txt", "offset": READ_PAGE_CHARS})
        self.assertEqual(implicit["content_id"], first["content_id"])
        self.assertEqual(implicit["text"], raw.decode("utf-8", "replace")[READ_PAGE_CHARS:READ_PAGE_CHARS*2])
        changed = raw[:-1] + b"!"
        (ctx.workspace / "large.txt").write_bytes(changed)
        with self.assertRaisesRegex(Denied, "changed since the previous page"):
            ctx._op_read({"path": "large.txt", "content_id": first["content_id"],
                          "offset": READ_PAGE_CHARS})
        with self.assertRaisesRegex(Denied, "changed since the previous page"):
            ctx._op_read({"path": "large.txt", "offset": READ_PAGE_CHARS})
        independent = self.context()._op_read({"path": "large.txt", "offset": READ_PAGE_CHARS})
        self.assertEqual(independent['content_id'], digest(changed))
        with self.assertRaisesRegex(Denied, "changed since the previous page"):
            self.context()._op_read({"path": "large.txt", "offset": READ_PAGE_CHARS,
                                     "content_id": first['content_id']})
        (ctx.workspace / "other.txt").write_bytes(raw)
        other = ctx._op_read({"path": "other.txt", "offset": READ_PAGE_CHARS})
        self.assertEqual(other['content_id'], digest(raw))
        refreshed = ctx._op_read({"path": "large.txt"})
        self.assertNotEqual(refreshed['content_id'], first['content_id'])
        self.assertEqual(ctx._op_read({"path": "large.txt", "offset": READ_PAGE_CHARS})['content_id'], refreshed['content_id'])
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
    def _accepted_work_item(self, title="Runtime transition"):
        return self.project.work_items.create(idempotency_key=title, title=title,
                                              actor="worker")["record"]["work_item"]

    def test_retired_work_item_operations_have_no_runtime_surface(self):
        from mizu.config import CAPABILITIES
        from mizu.protocol import DEFINITIONS, operation_allowed
        from mizu.runtime import OBSERVATIONAL_OPS, WRAP_UP_OPS
        ctx = self.context("worker")
        for operation in ("work_items_legacy_read", "work_items_reconcile"):
            with self.subTest(operation=operation):
                for registry in (CAPABILITIES, DEFINITIONS, OBSERVATIONAL_OPS, WRAP_UP_OPS,
                                 CONSULT_FORBIDDEN, DELEGATE_FORBIDDEN):
                    self.assertNotIn(operation, registry)
                for role in self.config.roles.values():
                    self.assertNotIn(operation, role.capabilities)
                self.assertFalse(operation_allowed(operation, (operation,)))
                with self.assertRaisesRegex(Denied, "Unknown operation"):
                    ctx.dispatch(operation, {})

    def test_work_items_transition_uses_read_revision_and_binds_actor(self):
        item = self._accepted_work_item()
        ctx = self.context("worker")
        projection = ctx.handle("work_items_read", {})
        current = next(row for row in projection["items"] if row["id"] == item["id"])
        with self.assertRaises(Denied):
            ctx.handle("work_items_transition", {"id": item["id"], "expected_rev": current["rev"],
                                                  "status": "doing", "actor": "model-supplied"})
        result = ctx.handle("work_items_transition", {"id": item["id"],
                                                       "expected_rev": current["rev"],
                                                       "status": "doing"})
        self.assertEqual((result["rev"], result["status"], result["actor"]), (2, "doing", "worker"))
        self.assertEqual(self.project.work_items.get(item["id"]), result)

    def test_work_items_transition_refuses_blocked_without_wait(self):
        item = self._accepted_work_item()
        ctx = self.context("worker")
        with self.assertRaisesRegex(Denied, "supported observable wait"):
            ctx.handle("work_items_transition", {"id": item["id"],
                                                "expected_rev": item["rev"],
                                                "status": "blocked"})
        self.assertEqual(self.project.work_items.get(item["id"]), item)
        self.assertFalse((self.project.root / "waits").exists())

    def test_work_items_transition_stale_cas_refuses_without_write(self):
        item = self._accepted_work_item()
        self.project.work_items.transition(item["id"], expected_rev=1, status="doing", actor="other")
        ctx = self.context("worker")
        with self.assertRaisesRegex(Denied, "re-read"):
            ctx.handle("work_items_transition", {"id": item["id"], "expected_rev": 1,
                                                  "status": "blocked"})
        directory = self.project.root / "work-items" / item["id"]
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["2.json", "create.json"])
        self.assertEqual(self.project.work_items.get(item["id"])["rev"], 2)

    def test_work_items_transition_denial_and_consult_delegate_exclusion(self):
        import dataclasses
        from mizu.runtime import (CONSULT_FORBIDDEN, DELEGATE_FORBIDDEN,
                                  check_consult_role, check_delegate_role)
        item = self._accepted_work_item()
        ctx = self.context("worker")
        ctx.role = dataclasses.replace(
            ctx.role, capabilities=tuple(cap for cap in ctx.role.capabilities
                                         if cap != "work_items_transition"))
        with self.assertRaisesRegex(Denied, "no capability: work_items_transition"):
            ctx.handle("work_items_transition", {"id": item["id"], "expected_rev": 1,
                                                  "status": "doing"})
        self.assertFalse((self.project.root / "work-items" / item["id"] / "2.json").exists())
        self.assertIn("work_items_transition", CONSULT_FORBIDDEN)
        self.assertIn("work_items_transition", DELEGATE_FORBIDDEN)
        consult = self.config.roles["consult"]
        with self.assertRaises(ConfigError):
            check_consult_role("consult", dataclasses.replace(
                consult, capabilities=(*consult.capabilities, "work_items_transition")))
        delegate = self.config.roles["searcher"]
        with self.assertRaises(ConfigError):
            check_delegate_role("helper", dataclasses.replace(
                delegate, capabilities=(*delegate.capabilities, "work_items_transition")))
        for capability in ("queues",):
            self.assertIn(capability, CONSULT_FORBIDDEN)
            self.assertIn(capability, DELEGATE_FORBIDDEN)
            with self.assertRaises(ConfigError):
                check_consult_role("consult", dataclasses.replace(
                    consult, capabilities=(*consult.capabilities, capability)))
            with self.assertRaises(ConfigError):
                check_delegate_role("helper", dataclasses.replace(
                    delegate, capabilities=(*delegate.capabilities, capability)))

    def test_work_items_transition_preserves_store_wait_and_completion_checks(self):
        item = self._accepted_work_item()
        ctx = self.context("worker")
        blocked = ctx.handle("work_items_transition", {"id": item["id"], "expected_rev": 1,
                                                        "status": "blocked",
                                                        "wait": {"kind": "deadline",
                                                                 "at": "2030-01-01T00:00:00+00:00"}})
        self.assertEqual(blocked["wait"], {"kind": "deadline", "at": "2030-01-01T00:00:00+00:00"})
        self.assertFalse((self.project.root / "waits" / f"{item['id']}.json").exists())
        with self.assertRaisesRegex(Denied, "snapshot"):
            ctx.handle("work_items_transition", {"id": item["id"], "expected_rev": 2,
                                                  "status": "done"})
        self.assertEqual(self.project.work_items.get(item["id"])["rev"], 2)

        captured = self.project.snapshots.capture_files(self.project.workspace)
        verification = {"passed": True, "code_digest": captured["code_digest"], "run": "runtime-test"}
        snapshot = self.project.snapshots.create(
            captured, goal="Goal", state="Verified", run="runtime-test", outcome="done",
            summary="Verified completion", verification=verification)
        self.project.snapshots.publish(snapshot)
        ctx.handle("work_items_transition", {"id": item["id"], "expected_rev": 2, "status": "doing"})
        ctx.handle("work_items_transition", {"id": item["id"], "expected_rev": 3, "status": "doing",
                                              "snapshot_id": snapshot["id"]})
        completed = ctx.handle("work_items_transition", {"id": item["id"], "expected_rev": 4,
                                                          "status": "done",
                                                          "snapshot_id": snapshot["id"]})
        self.assertEqual(completed["snapshot_id"], snapshot["id"])
        self.assertEqual(completed["verification"], verification)
        # The delivery tree is taken from the exact bound immutable manifest.
        self.assertEqual(completed["delivered_tree"], captured["code_digest"])

    def test_work_items_read_by_id_open_and_terminal_is_observational(self):
        from mizu.fs import canonical
        from mizu.work_items import MAX_HISTORY_PAGE_BYTES

        ctx = self.context("worker")
        done_seed = self._accepted_work_item("Runtime done read")
        open_view = ctx.handle("work_items_read", {"id": done_seed["id"]})
        self.assertEqual(open_view["item"]["status"], "open")
        self.assertEqual(open_view["item"]["rev"], 1)
        self.assertEqual(open_view["history"], [done_seed])
        self.assertEqual(open_view["history_total"], 1)
        self.assertEqual(open_view["cursor"], 0)
        self.assertIsNone(open_view["next_cursor"])

        captured = self.project.snapshots.capture_files(self.project.workspace)
        verification = {"passed": True, "code_digest": captured["code_digest"], "run": "runtime-test"}
        snapshot = self.project.snapshots.create(
            captured, goal="Goal", state="Verified", run="runtime-test", outcome="done",
            summary="Verified completion", verification=verification)
        self.project.snapshots.publish(snapshot)
        ctx.handle("work_items_transition", {"id": done_seed["id"], "expected_rev": 1, "status": "doing"})
        ctx.handle("work_items_transition", {"id": done_seed["id"], "expected_rev": 2, "status": "doing",
                                              "snapshot_id": snapshot["id"]})
        ctx.handle("work_items_transition", {"id": done_seed["id"], "expected_rev": 3,
                                              "status": "done", "snapshot_id": snapshot["id"]})
        dropped_seed = self._accepted_work_item("Runtime dropped read")
        ctx.handle("work_items_transition", {"id": dropped_seed["id"], "expected_rev": 1,
                                              "status": "dropped", "reason": "No longer needed"})

        wake_generation = self.project.control().get("wake_generation")
        ctx.progress_tool = False
        done = ctx.handle("work_items_read", {"id": done_seed["id"], "after_rev": 3})
        dropped = ctx.handle("work_items_read", {"id": dropped_seed["id"]})
        self.assertEqual(done["item"]["status"], "done")
        self.assertEqual(done["item"]["rev"], 4)
        self.assertEqual(done["history"][0]["rev"], 4)
        self.assertEqual(done["history"][0]["snapshot_id"], snapshot["id"])
        self.assertEqual(done["history"][0]["verification"], verification)
        self.assertEqual(done["cursor"], 3)
        self.assertFalse(done["truncated"])
        self.assertEqual(dropped["item"]["status"], "dropped")
        self.assertEqual(dropped["history"][1]["reason"], "No longer needed")
        self.assertFalse(ctx.progress_tool)
        self.assertEqual(self.project.control().get("wake_generation"), wake_generation)
        self.assertLessEqual(len(canonical(done)), MAX_HISTORY_PAGE_BYTES)

    def test_work_items_read_by_id_rejects_invalid_combinations_and_cursors(self):
        from mizu.protocol import DEFINITIONS, validate
        ctx = self.context("worker")
        item = self._accepted_work_item("Runtime invalid read")
        validate({"id": item["id"], "after_rev": 0, "limit": 1}, DEFINITIONS["work_items_read"][1])
        for arguments in ({"id": "wi_wrong"}, {"id": item["id"], "after_rev": -1},
                          {"id": item["id"], "after_rev": 257}, {"id": item["id"], "after_rev": True}):
            with self.assertRaises(Denied):
                validate(arguments, DEFINITIONS["work_items_read"][1])
        with self.assertRaises(Denied):
            ctx.handle("work_items_read", {"id": item["id"], "cursor": "opaque"})
        with self.assertRaisesRegex(Denied, "ahead of current revision"):
            ctx.handle("work_items_read", {"id": item["id"], "after_rev": 2})
        with self.assertRaisesRegex(Denied, "after_rev requires id"):
            ctx.handle("work_items_read", {"after_rev": 0})

    def test_work_items_read_preserves_default_and_store_errors(self):
        ctx = self.context("worker")
        with patch.object(self.project.work_items, "page_open", return_value={"items": []}) as page_open:
            self.assertEqual(ctx.handle("work_items_read", {}), {"items": []})
            page_open.assert_called_once_with(cursor=None, limit=25)
        with patch.object(self.project.work_items, "page_open", side_effect=Denied("store rejected cursor")):
            with self.assertRaisesRegex(Denied, "store rejected cursor"):
                ctx.handle("work_items_read", {})

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

    def test_next_diagnostic_leaves_other_roles_and_outcomes_unchanged(self):
        for workspace in ("read", "none"):
            with self.subTest(workspace=workspace):
                ctx = self.context("searcher")
                ctx.role = dataclasses.replace(ctx.role, workspace=workspace)
                self.assertTrue(ctx.handle("finish", {"outcome": "continue", "summary": "Observed"})["sealed"])
        for outcome in ("wait", "blocked", "done"):
            with self.subTest(outcome=outcome):
                ctx = self.context()
                ctx.verification = {"passed": True}
                self.assertTrue(ctx.handle("finish", {"outcome": outcome, "summary": "Observed",
                                                       "state": "No immediate follow-up"})["sealed"])

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
        from mizu.config import system_context_text
        observer, writer = self.context("reviewer"), self.context("worker")
        self.assertEqual(observer.goal, before)
        self.assertEqual(writer.goal, "A new operator goal")
        (self.project.root / "PROJECT.md").write_text("Changed after context pinning")
        self.assertTrue(system_context_text(observer.role, observer.goal).endswith(before))
        self.assertTrue(system_context_text(writer.role, writer.goal).endswith("A new operator goal"))
        self.assertNotIn("Changed after context pinning", system_context_text(observer.role, observer.goal))

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
    def test_retired_work_item_grants_are_rejected_in_config_and_inaccessible_in_engine(self):
        retired = ("work_items_legacy_read", "work_items_reconcile")
        original = self.file.read_text()
        head, worker = original.split("[roles.worker]", 1)
        for operation in retired:
            with self.subTest(operation=operation):
                self.file.write_text(head + "[roles.worker]" + worker.replace(
                    'capabilities = [', f'capabilities = ["{operation}", ', 1))
                with self.assertRaisesRegex(ConfigError, "Invalid or duplicate capability in role worker"):
                    load(self.file)
        self.file.write_text(original)

        # Simulate stale in-memory grants, bypassing config validation to check both
        # Engine advertisement and the call boundary independently.
        role = self.config.roles["worker"]
        self.config.roles["worker"] = dataclasses.replace(role, capabilities=(*role.capabilities, *retired))

        def attempt(ctx, prompt, _profile):
            self.assertTrue(set(retired) <= set(ctx.role.capabilities))
            shown = json.loads(prompt)["unit_tool_surface"]["tools"]
            self.assertIn("mizu_work_items_read", shown)
            for operation in retired:
                self.assertNotIn(f"mizu_{operation}", shown)
                with self.assertRaisesRegex(Denied, f"no capability: {operation}"):
                    ctx.handle(operation, {})

        driver = ScriptDriver(attempt)
        self.assertEqual(Engine(self.config, driver=driver).run(self.project, "worker")["status"], "completed")
        self.assertEqual(len(driver.calls), 1)
        self.assertFalse((self.project.root / "work-items").exists())

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

    def test_unchanged_wait_streak_rotates_persistent_session_without_admitting(self):
        from mizu.engine_config import effective, session_generation, session_record, save_session
        from mizu.runtime import session_prompt
        from mizu.snapshot import no_progress_recovery_due
        config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, no_progress_recovery_after=2))
        self.config = config
        self.project.config = config
        base = self.project.snapshots.get()
        baseline = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace), goal=self.project.goal,
            state="Now: Unchanged.\nNext: Keep waiting", run=None, outcome="wait",
            summary="Input-quiet baseline", wake_at=time.time() + 3600,
            wake_generation=self.project.control()["wake_generation"], parent=base["id"])
        self.project.snapshots.publish(baseline)
        def finish_wait(ctx, prompt, profile):
            ctx.handle("finish", {"outcome": "wait", "summary": "Still waiting",
                                  "state": ctx.snapshot["state"]})
        engine = Engine(config, driver=ScriptDriver(finish_wait))
        engine.run(self.project, "worker")
        first = self.project.snapshots.get()
        self.assertEqual(first["progress"]["no_progress_count"], 1)
        ctx = self.context("worker")
        settings = effective(config, ctx.role, ctx.role.profile)
        path, _ = session_record(ctx, ctx.role.profile, settings)
        save_session(path, "test-session", {"tokens": 0})
        engine.run(self.project, "worker")
        second = self.project.snapshots.get()
        self.assertEqual(second["outcome"], "wait")
        self.assertEqual(second["progress"]["no_progress_count"], 2)
        self.assertTrue(no_progress_recovery_due(second["progress"], "wait", config, "worker"))
        # A due rotation is diagnostic on a parked snapshot, never an admission.
        self.assertFalse(should_run(self.project, second, role_name="worker", config=config))
        self.assertEqual(self.project.status()["progress"]["recovery"]["status"], "due")
        ctx = self.context("worker")
        _, mode, _, rotation, _ = session_prompt(ctx, ctx.role, ctx.run_dir)
        self.assertEqual(mode, "full")
        self.assertIn("no-progress units 2 >=2", rotation["reason"])
        self.assertEqual(session_generation(path.parent), 1)
        self.assertFalse(path.exists())

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
        result = self.admitted_call(engine, "consult", {"request": "What could fail?", "roles": ["advisor-a", "advisor-b"]})
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
            dispatch = read_json(child_dir / "dispatch-attempts.json")["attempts"][0]
            self.assertEqual(dispatch["class"], "consult")
            self.assertEqual(dispatch["parent_run"], read_json(child_dir / "started.json")["parent_run"])
            self.assertEqual(dispatch["profile"], "alternate" if "error" in answer else "primary")
            self.assertEqual(dispatch["status"], "raised" if "error" in answer else "returned")

    def test_agent_cannot_select_unapproved_consult_model(self):
        with self.assertRaises(ConfigError):
            Engine(self.config, driver=ScriptDriver()).consult(self.context(), {"request": "x", "roles": ["unapproved"]})
        with self.assertRaises(Denied):
            Engine(self.config, driver=ScriptDriver()).consult(self.context(), {"request": "x", "roles": []})
        with self.assertRaises(Denied):
            Engine(self.config, driver=ScriptDriver()).consult(self.context(), {"request": "x", "roles": ["consult", "consult"]})
        with self.assertRaises(Denied):
            Engine(self.config, driver=ScriptDriver()).consult(self.context(), {"request": "x", "roles": ["consult"], "role": "consult"})

    def test_consult_role_is_operator_chosen_not_hardcoded(self):
        import dataclasses
        from mizu.protocol import DEFINITIONS, validate
        validate({"request": "q?", "role": "advisor"}, DEFINITIONS["consult"][1])
        role = dataclasses.replace(self.config.roles["consult"], name="advisor")
        config = dataclasses.replace(self.config, roles={**self.config.roles, "advisor": role})
        engine = Engine(config, driver=ScriptDriver())
        result = self.admitted_call(engine, "consult", {"request": "q?", "role": "advisor"})
        self.assertEqual(result["roles"], ["advisor"])
        self.assertNotIn("aggregation", result)

    def test_consult_uses_answering_role_own_profile(self):
        seen = []
        def answer(ctx, prompt, profile):
            seen.append(profile)
            ctx.handle("finish", {"outcome": "wait", "summary": "Own model"})
        engine = Engine(self.config, driver=ScriptDriver(answer))
        result = self.admitted_call(engine, "consult", {"request": "Which model answers?"})
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
        result = self.admitted_call(engine, "consult", {"request": "q?"})
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
        result = self.admitted_call(engine, "consult", {"request": "q?", "roles": ["outsider", "sel-outsider"]})
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
            engine.consult(self.context(), {"request": "q?", "role": "worker"})
        with self.assertRaises(ConfigError):
            engine.consult(self.context(), {"request": "q?", "role": "ghost"})


class PromptProjectionTests(Fixture):
    def test_prompts_open_with_the_fixed_new_unit_statement(self):
        import json
        from mizu.runtime import prompt_for, prompt_delta_for
        worker = self.context("worker")
        full = prompt_for(worker)
        opening = json.loads(full)["unit"]
        for prompt in (full, prompt_delta_for(worker)):
            decoded = json.loads(prompt)
            self.assertEqual(list(decoded)[0], "unit")
            self.assertEqual(decoded["unit"], opening)
            self.assertEqual(decoded["run"], {"id": worker.run_dir.name})
        worker_dir = worker.run_dir
        (worker_dir / "started.json").write_text(
            '{"run": %s, "started_at": "2026-10-18T00:00:00+00:00"}' % json.dumps(worker_dir.name))
        for prompt in (prompt_for(worker), prompt_delta_for(worker)):
            self.assertEqual(json.loads(prompt)["run"],
                             {"id": worker_dir.name, "started_at": "2026-10-18T00:00:00+00:00"})

    def test_unit_opening_surface_uses_dispatch_role_not_retained_authority(self):
        import json
        from mizu.protocol import tool_definitions
        from mizu.runtime import prompt_for, prompt_delta_for
        for name in self.config.roles:
            with self.subTest(role=name):
                ctx = self.context(name)
                # Neither retained authority, session identity nor spend owner
                # may widen what the model is told this dispatch exposes.
                ctx.turn_authority_role = self.config.roles["worker"]
                ctx.session_identity_capabilities = self.config.roles["worker"].capabilities
                ctx.spend_role = self.config.roles["worker"]
                full = json.loads(prompt_for(ctx))
                delta = json.loads(prompt_delta_for(ctx))
                surface = full["unit_tool_surface"]
                self.assertEqual(surface, delta["unit_tool_surface"])
                self.assertEqual(list(full)[:3], ["unit", "unit_tool_surface", "goal_digest"])
                self.assertEqual(list(delta)[:3], ["unit", "unit_tool_surface", "goal_digest"])
                self.assertEqual(surface["tools"], [t["name"] for t in tool_definitions(ctx.role.capabilities)])
                self.assertIn("UNIT-OPENING", surface["scope"])
                self.assertIn("earlier work units, not later", surface["scope"])
                self.assertIn("this same unit", surface["scope"])
                self.assertIn("this text grants nothing", surface["scope"])
                # Bounds and authority can change within this unit; even if a
                # prompt is explicitly rebuilt it reports only the reduced role.
                ctx.role = dataclasses.replace(ctx.role, capabilities=("finish",), engine_tools=())
                self.assertEqual(json.loads(prompt_for(ctx))["unit_tool_surface"]["tools"], ["mizu_finish"])
                with self.assertRaisesRegex(Denied, "no capability"):
                    ctx.handle("files", {})

    def test_bound_notices_list_only_granted_wrap_up_operations(self):
        from mizu.runtime import WRAP_UP_OPS
        ctx = self.context("worker")
        ctx.role = dataclasses.replace(ctx.role, capabilities=("files", "finish", "research"))
        ctx.turn_authority_role = self.config.roles["worker"]
        for bound in ("requests", "tools", "deadline", "drain"):
            with self.subTest(bound=bound):
                ctx.bound_hit = bound
                notice = ctx._bound_message()
                self.assertTrue(notice.startswith("For the rest of this work unit only:"))
                self.assertIn(f"per-run {bound} bound reached", notice)
                self.assertIn("(mizu_finish, mizu_research)", notice)
                for operation in WRAP_UP_OPS - {"finish", "research"}:
                    self.assertNotIn("mizu_" + operation, notice)
                self.assertNotIn("mizu_files", notice)

    def test_finish_refuses_unknown_next_profile(self):
        from mizu.errors import Denied
        finish = {"outcome": "continue", "summary": "s", "state": "Check pending",
                  "next_step": "Check the result",
                  "next_reason": "why", "next_profile": "no-such-profile"}
        with self.assertRaisesRegex(Denied, "no configured profile"):
            self.context("worker").handle("finish", dict(finish))
        worker = self.context("worker")
        self.assertEqual(worker.handle(
            "finish", {**finish, "next_profile": "alternate"})["sealed"], True)
        self.assertEqual(worker.finished["next_profile"], "alternate")
        self.assertEqual(worker.finished["next_reason"], "why")

    def test_open_work_items_projection_is_always_present(self):
        import json
        from mizu.runtime import prompt_for, prompt_delta_for
        self.assertEqual(self.config.limits.work_items_prompt, 20)
        for role in ("worker", "searcher", "reviewer", "reporter", "consult"):
            context = self.context(role)
            for prompt in (prompt_for(context), prompt_delta_for(context)):
                self.assertIn("open_work_items", json.loads(prompt))
                self.assertEqual(json.loads(prompt)["open_work_items"]["overflow_count"], 0)

    def test_active_record_reports_current_activity(self):
        import json
        from mizu.fs import read_json
        from support import history_write_json as write_json
        ctx = self.context("worker")
        active = ctx.project.root / "active" / "worker.json"
        write_json(active, {"run": ctx.run_dir.name, "role": "worker",
                            "started_at": "2026-10-18T00:00:00+00:00"})
        ctx.handle("read", {"path": "app.py"})
        record = read_json(active)
        self.assertEqual(record["run"], ctx.run_dir.name)
        self.assertEqual(record["role"], "worker")
        self.assertIn("tool:read", record["activity_seconds"])
        # The active file is a live gauge, not a stable contract: only the
        # current bucket is asserted for presence, not an exact value.
        self.assertIn(record["activity"], {"tool:read", "other"})
        self.assertIn("activity_since", record)

    def test_child_activity_is_published_without_overwriting_role_owner(self):
        from mizu.fs import read_json
        from support import history_write_json as write_json
        ctx = self.context("worker")
        ctx.parent_run = "parent-run"
        active = ctx.project.root / "active" / "worker.json"
        owner = {"run": "other-run", "role": "worker"}
        write_json(active, owner)
        ctx._activity_change(lambda: setattr(ctx, "_activity_operation", "read"))
        child = read_json(ctx.run_dir / "activity.json")
        self.assertEqual(child["schema"], 1)
        self.assertEqual(child["parent_run"], "parent-run")
        self.assertEqual(child["run"], ctx.run_dir.name)
        self.assertEqual(child["activity"], "tool:read")
        self.assertEqual(read_json(active), owner)

    def test_activity_gauge_matches_parent_record(self):
        from mizu.fs import read_json
        from support import history_write_json as write_json
        ctx = self.context("worker")
        active = ctx.project.root / "active" / "worker.json"
        write_json(active, {"run": ctx.run_dir.name, "role": "worker"})
        ctx._activity_change(lambda: setattr(ctx, "_activity_operation", "verify"))
        gauge = read_json(ctx.run_dir / "activity.json")
        for key in ("activity", "activity_since", "activity_seconds", "activity_total_seconds"):
            self.assertEqual(gauge[key], read_json(active)[key])

    def test_active_record_ignores_superseded_run_and_explain_tolerates_legacy(self):
        from mizu.fs import read_json
        from support import history_write_json as write_json
        from mizu.explain import explain
        ctx = self.context("worker")
        active = ctx.project.root / "active" / "worker.json"
        write_json(active, {"run": "not-this-run", "role": "worker",
                            "started_at": "2026-10-18T00:00:00+00:00"})
        ctx._activity_change(lambda: setattr(ctx, "_activity_operation", "read"))
        # A superseded run id is never overwritten by a stale context.
        self.assertEqual(read_json(active)["run"], "not-this-run")
        # A legacy record without activity keys still explains as current.
        write_json(active, {"run": ctx.run_dir.name, "role": "worker",
                            "started_at": "2026-10-18T00:00:00+00:00"})
        reported = explain(self.config, ctx.project, "worker")["role_facts"]["active"]
        self.assertEqual(reported["status"], "current")
        self.assertNotIn("activity", reported["record"])

    def test_explain_reports_live_activity(self):
        from mizu.fs import read_json
        from support import history_write_json as write_json
        from mizu.explain import explain
        ctx = self.context("worker")
        active = ctx.project.root / "active" / "worker.json"
        write_json(active, {"run": ctx.run_dir.name, "role": "worker",
                            "started_at": "2026-10-18T00:00:00+00:00"})
        ctx._activity_change(lambda: setattr(ctx, "_activity_operation", "read"))
        reported = explain(self.config, ctx.project, "worker")["role_facts"]["active"]
        self.assertEqual(reported["status"], "current")
        self.assertEqual(reported["record"]["activity"], "tool:read")
        self.assertIn("tool:read", reported["record"]["activity_seconds"])
        self.assertIn("activity_since", reported["record"])

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
        delta = json.loads(prompt_delta_for(self.context("worker")))
        self.assertEqual(delta["timezone"], self.config.timezone)
        self.assertIn("now", delta)
        self.assertEqual(list(delta.keys())[-2:], ["now", "timezone"])

class PreviousReportTests(Fixture):
    def test_absent_artifact_reads_as_none(self):
        import json
        from mizu.runtime import prompt_for, prompt_delta_for
        self.assertIsNone(json.loads(prompt_for(self.context("reporter")))["previous_report"])
        self.assertIsNone(json.loads(prompt_delta_for(self.context("reporter")))["previous_report"])
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
        delta = json.loads(prompt_delta_for(self.context("reporter")))
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

    def test_script_driver_propagates_capture_refusal_after_request_bound(self):
        from mizu.errors import Denied, LimitExceeded
        from mizu.runtime import Engine
        from support import ScriptDriver

        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], requests_per_run=1)
        config = dataclasses.replace(self.config, roles=roles)
        reached = [False]
        original_check_bounds = self.project.snapshots.check_bounds

        def check_bounds(workspace):
            if reached[0]:
                raise LimitExceeded("synthetic workspace capture refusal")
            return original_check_bounds(workspace)

        def callback(ctx, *_):
            if ctx.bound_hit is None:
                # The second request reaches the per-run bound and yields to
                # the reduced-grant invocation, where finish is attempted.
                ctx.handle("_budget", {"sequence": 2})
            reached[0] = True

        with patch.object(self.project.snapshots, "check_bounds", side_effect=check_bounds):
            with self.assertRaisesRegex(Denied, "synthetic workspace capture refusal"):
                Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")

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
            import json
            self.assertTrue(prompt.startswith("For the rest of this work unit only:"))
            notice, original = prompt.split("\n\n", 1)
            self.assertIn("mizu_finish", notice)
            self.assertNotIn("mizu_files", notice)
            self.assertEqual(json.loads(original)["unit_tool_surface"]["tools"],
                             [tool["name"] for tool in tool_definitions(self.config.roles["worker"].capabilities)])
            with self.assertRaisesRegex(Denied, "no capability"):
                ctx.handle("files", {})
            seen.append("wrap-up")
            seen.append(tuple(sorted(ctx.role.capabilities)))
            seen.append(tuple(ctx.role.engine_tools))
            ctx.handle("finish", {"outcome": "continue", "summary": "resumed and sealed",
                                  "next_step": 'resume.', "state": "Now: wrapped.\nNext: resume.\nTasks: none\nBlocking: none"})
        result = Engine(self.config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finish"]["outcome"], "bounded")
        self.assertEqual(result["wrap_up"], {"bound": "requests"})
        grants = seen[2]
        self.assertTrue(set(grants) <= set(WRAP_UP_OPS))
        self.assertIn("finish", grants)
        self.assertEqual(seen[3], ())
        self.assertEqual(attempts, [(1, 1), (2, 2)])
        dispatches = read_json(self.project.root / "runs" / result["run"] / "dispatch-attempts.json")["attempts"]
        self.assertEqual([r["execution_attempt"] for r in dispatches], [1, 2])
        self.assertEqual([r["status"] for r in dispatches], ["raised", "returned"])

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
                                  "next_step": 'y.', "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
        Engine(self.config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(len(prompts), 2)
        self.assertIn("For the rest of this work unit only: per-run tools bound reached", prompts[1])
        self.assertIn("recorded as a bounded end", prompts[1])
        self.assertIn("Keep the published state", prompts[1])

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
        cap = self.project.snapshots.capture_files(self.project.workspace)
        parked = self.project.snapshots.create(
            cap, goal=self.project.goal, state="Wrapped.", run=None,
            outcome="bounded", summary="Hit the bound after sealing.", verification=None,
            wake_generation=self.project.control().get("wake_generation", ""))
        self.project.snapshots.publish(parked)
        snap = self.project.snapshots.get()
        self.assertEqual(snap["outcome"], "bounded")
        self.assertTrue(should_run(self.project, snap, current_time=time.time()))

    def test_daily_refusal_at_per_run_cap_burns_nothing(self):
        import dataclasses
        from mizu.drivers import admit_invocation
        from mizu.errors import InfraExceeded, DeadlineWrapUp
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
        with self.assertRaises(DeadlineWrapUp):
            ctx.handle("_budget", {"sequence": 99})
        self.assertEqual(ctx.wrap_up_requests, 0)
        ctx.wrap_up_attempted = True
        with self.assertRaises(InfraExceeded):
            ctx.handle("_budget", {"sequence": 99})
        self.assertEqual(ctx.bound_hit, "requests")
        self.assertEqual(ctx.wrap_up_requests, 0)
        self.assertTrue(ctx.admission_wait)
        with self.assertRaises(InfraExceeded):
            admit_invocation(ctx, unit="request")
        self.assertEqual(ctx.bound_hit, "requests")
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


