"""Direct work creation: real stores, runtime grants and offline persistence."""
import concurrent.futures
import copy
import dataclasses
import json
import os
from unittest.mock import patch

from support import Fixture
from mizu import work_item_events as events
from mizu.errors import ConfigError, Denied, LimitExceeded
from mizu.fs import PREVIEW_BYTES, write_json
from mizu.project import Project
from mizu.protocol import DEFINITIONS, validate
from mizu.runtime import (CONSULT_FORBIDDEN, DELEGATE_FORBIDDEN, WRAP_UP_OPS,
                          check_consult_role, check_delegate_role, prompt_for, prompt_delta_for)
from mizu.storage import audit, backup, restore


class DirectWorkItemTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items

    def create(self, key="stable-key", title="Self-directed work", origin=None):
        return self.store.create(idempotency_key=key, title=title, origin=origin, actor="worker")

    def insight(self):
        return self.project.insights.submit(source="reviewer", title="Proposal", body="Evidence",
                                            base_snapshot=None)

    def test_exact_replay_after_transition_and_conflict_are_immutable(self):
        first = self.create()
        item = first["record"]["work_item"]
        self.assertTrue(first["created"])
        self.assertIsNone(item["origin"])
        self.store.transition(item["id"], expected_rev=1, status="doing", actor="worker")
        self.assertEqual(self.create(), {"record": first["record"], "created": False})
        with self.assertRaisesRegex(Denied, "Conflicting"):
            self.create(title="Different intent")
        self.assertEqual(self.store.get(item["id"])["rev"], 2)
        self.assertEqual(self.store.explain(item["id"])["history_total"], 2)
        self.assertFalse((self.project.root / "decision-history").exists())

    def test_concurrent_retry_and_actor_scoped_key(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.create(), range(4)))
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertTrue(all(r["record"] == results[0]["record"] for r in results))
        other = self.store.create(idempotency_key="stable-key", title="Self-directed work", actor="other")
        self.assertNotEqual(other["record"]["work_item"]["id"], results[0]["record"]["work_item"]["id"])

    def test_insight_and_parent_references_check_exact_current_revision(self):
        proposal = self.insight()
        origin = {"insight_id": proposal["id"], "rev": 1}
        first = self.create(origin=origin)
        self.project.insights.revise(proposal["id"], source="reviewer", title="Revised", body="New",
                                     base_snapshot=None, expected_rev=1)
        self.assertFalse(self.create(origin=origin)["created"])
        with self.assertRaises(Denied):
            self.create("stale", origin=origin)
        parent = first["record"]["work_item"]
        split_origin = {"work_item_id": parent["id"], "rev": 1}
        split = self.create("split", origin=split_origin)["record"]["work_item"]
        self.assertEqual(split["origin"], split_origin)
        self.store.transition(parent["id"], expected_rev=1, status="doing", actor="worker")
        self.assertFalse(self.create("split", origin=split_origin)["created"])
        with self.assertRaises(Denied):
            self.create("split-stale", origin=split_origin)
        view = self.store.projection(split["id"])
        self.assertEqual((view["current_origin_rev"], view["stale"], view["redecision"]), (None, False, None))
        self.assertTrue(self.store.projection(parent["id"])["stale"])

    def test_invalid_requests_and_missing_origins_fail_closed(self):
        for origin in ({}, {"rev": 1}, {"insight_id": "missing", "rev": 1},
                       {"work_item_id": "wi_" + "f" * 32, "rev": 1},
                       {"insight_id": "../bad", "rev": 1},
                       {"insight_id": "missing", "rev": True},
                       {"insight_id": "missing", "work_item_id": "wi_" + "a" * 32, "rev": 1}):
            with self.subTest(origin=origin), self.assertRaises(Denied):
                self.create(origin=origin)
        for key, title in (("", "Title"), ("k" * 129, "Title"), ("key", ""), ("key", "t" * 201)):
            with self.assertRaises(Denied):
                self.create(key, title)
        item_id = self.store._create_id("worker", "stable-key")
        with self.assertRaises(Denied):
            self.create(origin={"work_item_id": item_id, "rev": 1})
        self.assertEqual(self.store.page_open()["total"], 0)

    def test_mixed_inventory_keeps_coverage_and_shared_history_semantics(self):
        proposal = self.insight()
        seeded = self.project.insights.decide(proposal["id"], "accept", "Implement", "", "run", expected_rev=1)
        direct = self.create()["record"]["work_item"]
        page = self.store.page_open(limit=1)
        self.assertEqual((page["total"], page["overflow_count"], page["coverage_complete"]), (2, 1, True))
        next_page = self.store.page_open(cursor=page["next_cursor"], limit=1)
        self.assertEqual({page["items"][0]["id"], next_page["items"][0]["id"]},
                         {seeded["work_item"]["id"], direct["id"]})
        ctx = self.context("worker")
        for text in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            prompt = json.loads(text)
            self.assertIn(direct["id"], [i["id"] for i in prompt["open_work_items"]["items"]])
        self.assertEqual(self.store.page_open(), self.store.page_open_readonly())
        self.store.transition(direct["id"], expected_rev=1, status="doing", actor="worker")
        with self.assertRaises(Denied):
            self.store.transition(direct["id"], expected_rev=1, status="blocked", actor="worker")
        with self.assertRaises(Denied):
            self.store.transition(direct["id"], expected_rev=2, status="done", actor="worker")
        self.store.transition(direct["id"], expected_rev=2, status="dropped", reason="Superseded", actor="worker")
        closed = self.store.page_closed_readonly()
        self.assertEqual(closed["items"][0]["id"], direct["id"])
        self.assertEqual(self.store.page_open()["total"], 1)
        self.assertEqual(self.store.explain(direct["id"])["history_total"], 3)

    def test_legacy_unseeded_coverage_is_not_satisfied_by_direct_work(self):
        write_json(self.project.root / "decision-history" / ("a" * 32 + ".json"),
                   {"id": "legacy", "rev": 1, "action": "accept"})
        self.create()
        page = self.store.page_open()
        self.assertEqual((page["total"], page["unseeded_decisions"], page["coverage_complete"]), (1, 1, False))

    def test_terminal_delivery_never_falls_back_to_creator_or_parent_actor(self):
        proposal = self.insight()
        insight_item = self.create("insight", origin={"insight_id": proposal["id"], "rev": 1})["record"]["work_item"]
        parent = self.create("parent")["record"]["work_item"]
        split = self.create("split", origin={"work_item_id": parent["id"], "rev": 1})["record"]["work_item"]
        for item in (insight_item, parent, split):
            self.store.transition(item["id"], expected_rev=1, status="dropped", reason="Closed", actor="worker")
        own = events.pending(self.store, "reviewer", ("dropped",))
        self.assertEqual([r["id"] for r in own["events"]], [insight_item["id"]])
        any_source = events.pending(self.store, "reviewer", ("dropped",), sources="any")
        self.assertEqual((any_source["pending"], any_source["unknown_routing"]), (3, 2))
        self.assertEqual(events.pending(self.store, "worker", ("dropped",))["pending"], 0)
        events.acknowledge(self.store, "reviewer", any_source["events"])
        self.assertEqual(events.pending(self.store, "reviewer", ("dropped",), sources="any")["acknowledged"], 3)

    def test_backup_restore_preserves_seeds_history_and_replay_keys(self):
        first = self.create()
        item_id = first["record"]["work_item"]["id"]
        self.store.transition(item_id, expected_rev=1, status="dropped", reason="Closed", actor="worker")
        events.acknowledge(self.store, "reviewer", events.pending(
            self.store, "reviewer", ("dropped",), sources="any")["events"])
        self.project.set_control(paused=True)
        archive = self.root / "backup.tar.gz"
        backup(self.project, archive)
        restore(self.config, "restored", archive)
        restored = Project(self.config, "restored")
        replay = restored.work_items.create(idempotency_key="stable-key", title="Self-directed work", actor="worker")
        self.assertEqual(replay, {"record": first["record"], "created": False})
        self.assertEqual(restored.work_items.get(item_id)["status"], "dropped")
        self.assertEqual(events.pending(restored.work_items, "reviewer", ("dropped",), sources="any")["acknowledged"], 1)

    def test_direct_done_evidence_protects_snapshot_in_storage_audit(self):
        item = self.create()["record"]["work_item"]
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snapshot = self.project.snapshots.create(captured, goal="Goal", state="State", run="run",
                    outcome="done", summary="Verified", verification={"passed": True, "run": "run",
                    "code_digest": captured["code_digest"]})
        self.project.snapshots.publish(snapshot)
        done = self.store.transition(item["id"], expected_rev=1, status="done", actor="worker", snapshot_id=snapshot["id"])
        self.assertEqual(self.store.get(item["id"]), done)
        report = audit(self.project)
        self.assertEqual(report["work_item_events_skipped"], 0)
        self.assertGreaterEqual(report["references"]["work_item_snapshots"], 1)

    def test_uncertain_post_commit_response_returns_same_seed(self):
        from mizu import work_items
        real_write = work_items.write_json
        def commit_then_fail(path, value, **kwargs):
            real_write(path, value, **kwargs)
            raise OSError("Simulated lost response")
        with patch.object(work_items, "write_json", side_effect=commit_then_fail):
            with self.assertRaises(OSError):
                self.create()
        replay = self.create()
        self.assertFalse(replay["created"])
        self.assertEqual(self.store.page_open()["total"], 1)

    def test_orphan_temporary_is_recovered_and_committed_alias_recovers(self):
        item_id = self.store._create_id("worker", "stable-key")
        directory = self.project.root / "work-items" / item_id
        directory.mkdir(parents=True)
        temp = directory / ".new-abcdefgh"
        temp.write_text("Uncommitted")
        # A crash before commit leaves a recognized orphan; recovery removes it
        # without promotion so a fresh direct create commits.
        record = self.create()["record"]
        self.assertTrue(record)
        self.assertFalse(temp.exists())
        self.assertTrue((directory / "create.json").exists())
        if os.name != "nt":
            os.link(directory / "create.json", temp)
            self.assertFalse(self.create()["created"])
            self.assertFalse(temp.exists())
        self.assertEqual(self.store.get(item_id), record["work_item"])

    def test_orphan_precommit_directory_validates_temporaries_without_inventing_work(self):
        item_id = self.store._create_id("worker", "stable-key")
        directory = self.project.root / "work-items" / item_id
        directory.mkdir(parents=True)
        temp = directory / ".new-abcdefgh"
        temp.write_text("Uncommitted")
        self.assertEqual(self.store.page_open_readonly()["total"], 0)
        self.assertTrue(temp.exists())
        self.assertEqual(self.store.page_open()["total"], 0)
        self.assertFalse(temp.exists())
        (directory / "unknown.json").write_text("{}")
        with self.assertRaises(Denied):
            self.store.page_open_readonly()

    def test_malformed_creation_and_history_records_fail_closed(self):
        record = self.create()["record"]
        item_id = record["work_item"]["id"]
        path = self.project.root / "work-items" / item_id / "create.json"
        mutations = []
        for key, value in (("actor", "other"), ("idempotency_key", "conflict"), ("kind", "unknown")):
            changed = copy.deepcopy(record)
            changed[key] = value
            mutations.append(changed)
        changed = copy.deepcopy(record)
        changed["work_item"]["origin"] = {"insight_id": "origin", "rev": True}
        mutations.append(changed)
        changed = copy.deepcopy(record)
        changed["extra"] = 1
        mutations.append(changed)
        for changed in mutations:
            write_json(path, changed)
            for read in (lambda: self.store.get(item_id), self.store.page_open,
                         self.store.page_closed_readonly, lambda: self.create()):
                with self.subTest(changed=changed), self.assertRaises(Denied):
                    read()
        path.write_bytes(b"x" * (PREVIEW_BYTES + 1))
        with self.assertRaises(LimitExceeded):
            self.store.get(item_id)
        write_json(path, record)
        doing = self.store.transition(item_id, expected_rev=1, status="doing", actor="worker")
        doing["origin"] = {"work_item_id": "wi_" + "f" * 32, "rev": 1}
        write_json(path.parent / "2.json", doing)
        with self.assertRaises(Denied):
            self.store.get(item_id)

    def test_unsafe_direct_seed_paths_and_records_fail_closed(self):
        item_id = self.create()["record"]["work_item"]["id"]
        path = self.project.root / "work-items" / item_id / "create.json"
        target = self.project.root / "copy.json"
        path.rename(target)
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")
        for call in (lambda: self.store.get(item_id), self.store.page_open, self.create):
            with self.assertRaises(Denied):
                call()

    def test_runtime_schema_grant_actor_and_child_exclusions(self):
        from mizu.config import CAPABILITIES
        operation = "work_items_create"
        args = {"idempotency_key": "runtime-key", "title": "Runtime work"}
        schema = DEFINITIONS[operation][1]
        self.assertIn(operation, CAPABILITIES)
        self.assertNotIn(operation, WRAP_UP_OPS)
        for name, role in self.config.roles.items():
            self.assertEqual(operation in role.capabilities, name == "worker")
        for changes in ({"actor": "model"}, {"idempotency_key": ""}, {"idempotency_key": "k" * 129},
                        {"title": "t" * 201}, {"origin": {"insight_id": "x", "rev": True}}):
            with self.assertRaises(Denied):
                validate({**args, **changes}, schema)
        ctx = self.context("worker")
        with self.assertRaises(Denied):
            ctx.handle(operation, {**args, "actor": "model"})
        result = ctx.handle(operation, args)
        self.assertEqual(result["record"]["actor"], "worker")
        self.assertTrue(ctx.progress_tool)
        ctx.progress_tool = False
        self.assertFalse(ctx.handle(operation, args)["created"])
        self.assertFalse(ctx.progress_tool)
        ctx.role = dataclasses.replace(ctx.role, capabilities=tuple(c for c in ctx.role.capabilities if c != operation))
        with self.assertRaisesRegex(Denied, "no capability"):
            ctx.handle(operation, args)
        self.assertIn(operation, CONSULT_FORBIDDEN)
        self.assertIn(operation, DELEGATE_FORBIDDEN)
        for check, name in ((check_consult_role, "consult"), (check_delegate_role, "searcher")):
            role = dataclasses.replace(self.config.roles[name], capabilities=("files", "read", "finish", operation))
            with self.assertRaises(ConfigError):
                check(name, role)


class SharedSeedCreationTests(Fixture):
    def proposal(self):
        return self.project.insights.submit(source="worker", title="Proposal", body="Evidence",
                                            base_snapshot=None)

    def decide(self, proposal, action="accept", **kwargs):
        return self.project.insights.decide(
            proposal["id"], action, "Implement", "later" if action == "defer" else "",
            "run", expected_rev=1, **kwargs)

    def assert_no_decision(self, proposal):
        from mizu.insights import _decision_key
        self.assertFalse((self.project.root / "decisions" / f"{proposal['id']}.json").exists())
        self.assertFalse((self.project.root / "decision-history" /
                          f"{_decision_key(proposal['id'], 1)}.json").exists())

    def test_decision_history_symlink_refuses_every_action_without_outside_write(self):
        outside = self.root / "outside"
        outside.mkdir()
        history = self.project.root / "decision-history"
        try:
            history.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks unavailable")
        for action, intent in (("accept", "create"), ("modify", "create"), ("accept", "none"),
                               ("reject", None), ("defer", None), ("withdraw", None)):
            with self.subTest(action=action, intent=intent):
                proposal = self.proposal()
                with self.assertRaisesRegex(Denied, "Unsafe work-item directory"):
                    if action == "withdraw":
                        self.project.insights.withdraw(proposal["id"], source="worker",
                                                       reason="Obsolete", run="run", expected_rev=1)
                    else:
                        self.decide(proposal, action, work_item_intent=intent)
                self.assert_no_decision(proposal)
                self.assertEqual(list(outside.iterdir()), [])

    def test_decisions_symlink_refuses_every_action_without_outside_write(self):
        import shutil
        outside = self.root / "outside-decisions"
        outside.mkdir()
        decisions = self.project.root / "decisions"
        saved = self.root / "saved-decisions"
        if saved.exists():
            shutil.rmtree(saved)
        try:
            if decisions.is_symlink() or decisions.is_file():
                decisions.unlink()
            elif decisions.is_dir():
                decisions.rename(saved)
            try:
                decisions.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Directory symlinks unavailable")
            for action, intent in (("accept", "create"), ("modify", "create"), ("accept", "none"),
                                   ("reject", None), ("defer", None), ("withdraw", None)):
                with self.subTest(action=action, intent=intent):
                    proposal = self.proposal()
                    with self.assertRaises(Denied):
                        if action == "withdraw":
                            self.project.insights.withdraw(proposal["id"], source="worker",
                                                           reason="Obsolete", run="run", expected_rev=1)
                        else:
                            self.decide(proposal, action, work_item_intent=intent)
                    self.assert_no_decision(proposal)
                    self.assertEqual(list(outside.iterdir()), [])
        finally:
            if decisions.is_symlink():
                decisions.unlink()
            if saved.exists():
                if decisions.exists():
                    shutil.rmtree(decisions)
                saved.rename(decisions)

    def test_decisions_symlink_refuses_pointer_repair_without_outside_write(self):
        import shutil
        proposal = self.proposal()
        original = self.decide(proposal, work_item_intent="create")
        from mizu.insights import _decision_key
        event_path = self.project.root / "decision-history" / f"{_decision_key(proposal['id'], 1)}.json"
        before = event_path.read_bytes()
        pointer = self.project.root / "decisions" / f"{proposal['id']}.json"
        pointer.unlink()
        decisions = self.project.root / "decisions"
        saved = self.root / "saved-decisions-repair"
        outside = self.root / "outside-decisions-repair"
        outside.mkdir()
        if saved.exists():
            shutil.rmtree(saved)
        try:
            decisions.rename(saved)
            try:
                decisions.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                saved.rename(decisions)
                self.skipTest("Directory symlinks unavailable")
            with self.assertRaises(Denied):
                self.decide(proposal, work_item_intent="create")
            self.assertEqual(event_path.read_bytes(), before)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            if decisions.is_symlink():
                decisions.unlink()
            if saved.exists():
                saved.rename(decisions)
        replay = self.decide(proposal, work_item_intent="create")
        self.assertEqual(replay, original)

    def test_waits_symlink_refuses_defer_with_wait_without_outside_write(self):
        import datetime
        outside = self.root / "outside-waits"
        outside.mkdir()
        waits = self.project.root / "waits"
        try:
            waits.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks unavailable")
        try:
            # A decision without a registered wait writes no wait file.
            plain = self.proposal()
            self.decide(plain, "defer")
            self.assertEqual(list(outside.iterdir()), [])
            future = (datetime.datetime.now(datetime.timezone.utc)
                      + datetime.timedelta(hours=1)).isoformat(timespec="seconds")
            parked = self.proposal()
            with self.assertRaises(Denied):
                self.decide(parked, "defer", wait={"kind": "deadline", "at": future})
            self.assert_no_decision(parked)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            if waits.is_symlink():
                waits.unlink()

    def test_orphan_temporary_is_recovered_for_fresh_decision_seed(self):
        from mizu.insights import _decision_key
        proposal = self.proposal()
        item_id = "wi_" + _decision_key(proposal["id"], 1)
        directory = self.project.root / "work-items" / item_id
        directory.mkdir(parents=True)
        temp = directory / ".new-abcdefgh"
        temp.write_text("Uncommitted")
        record = self.decide(proposal, work_item_intent="create")
        self.assertIn("work_item", record)
        self.assertFalse(temp.exists())
        self.assertEqual(record["work_item"]["id"], item_id)

    def test_unsafe_history_directory_preserves_existing_pointer(self):
        proposal = self.proposal()
        self.decide(proposal, "defer")
        pointer = self.project.root / "decisions" / f"{proposal['id']}.json"
        before = pointer.read_bytes()
        history = self.project.root / "decision-history"
        history.rename(self.project.root / "saved-history")
        history.write_text("not a directory")
        with self.assertRaises(Denied):
            self.decide(proposal, work_item_intent="create")
        self.assertEqual(pointer.read_bytes(), before)
        self.assertEqual(history.read_text(), "not a directory")

    def test_decision_seed_refuses_every_preexisting_item_record_before_commit(self):
        from mizu.insights import _decision_key
        for filename in ("2.json", "3.json", "legacy.json", "create.json"):
            with self.subTest(filename=filename):
                proposal = self.proposal()
                item_id = "wi_" + _decision_key(proposal["id"], 1)
                directory = self.project.root / "work-items" / item_id
                directory.mkdir(parents=True)
                record = directory / filename
                record.write_text("{}")
                with self.assertRaisesRegex(Denied, "pre-existing history"):
                    self.decide(proposal, work_item_intent="create")
                self.assert_no_decision(proposal)
                self.assertEqual(record.read_text(), "{}")
                self.assertEqual(list(directory.iterdir()), [record])

    def test_direct_seed_refuses_sidecar_numeric_and_unsafe_item_slots(self):
        store = self.project.work_items
        for filename in ("2.json", "legacy.json"):
            with self.subTest(filename=filename):
                item_id = store._create_id("worker", filename)
                directory = self.project.root / "work-items" / item_id
                directory.mkdir(parents=True)
                (directory / filename).write_text("{}")
                with self.assertRaisesRegex(Denied, "pre-existing history"):
                    store.create(idempotency_key=filename, title="Work", actor="worker")
                self.assertFalse((directory / "create.json").exists())
                self.assertEqual((directory / filename).read_text(), "{}")
        outside = self.root / "outside"
        outside.mkdir()
        proposal = self.proposal()
        from mizu.insights import _decision_key
        item_id = "wi_" + _decision_key(proposal["id"], 1)
        directory = self.project.root / "work-items" / item_id
        try:
            directory.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks unavailable")
        with self.assertRaises(Denied):
            self.decide(proposal, work_item_intent="create")
        self.assert_no_decision(proposal)
        self.assertEqual(list(outside.iterdir()), [])

    def test_both_paths_use_preflight_and_decision_checks_live_origin(self):
        store = self.project.work_items
        self.assertIs(self.project.insights.work_items, store)
        with patch.object(store, "create_seed", wraps=store.create_seed) as preflight:
            store.create(idempotency_key="direct", title="Work", actor="worker")
            self.decide(self.proposal(), work_item_intent="create")
            self.assertEqual(preflight.call_count, 2)
        proposal = self.proposal()
        with patch.object(store, "_origin_revision", return_value=2):
            with self.assertRaisesRegex(Denied, "origin revision"):
                self.decide(proposal, work_item_intent="create")
        self.assert_no_decision(proposal)

    def test_preflight_validates_title_origin_and_source_id_binding_without_writes(self):
        from mizu.fs import lock, now
        from mizu.insights import _decision_key
        store = self.project.work_items
        proposal = self.proposal()
        origin = {"insight_id": proposal["id"], "rev": 1}
        key = _decision_key(proposal["id"], 1)
        with lock(self.project.root / "locks" / "insights.lock"):
            for item_id, title, ref, source in (
                    ("wi_" + key, "", origin, {"decision_key": key}),
                    ("wi_" + key, "Work", {"rev": 1}, {"decision_key": key}),
                    ("wi_" + "f" * 32, "Work", origin, {"decision_key": key}),
                    ("wi_" + key, "Work", origin, {"actor": "worker", "idempotency_key": "key"})):
                with self.subTest(source=source, title=title), self.assertRaises(Denied):
                    store.create_seed(item_id, title, ref, now(), **source)
        self.assert_no_decision(proposal)
        self.assertFalse((self.project.root / "work-items").exists())

    def test_decision_retry_validates_chain_before_pointer_repair(self):
        proposal = self.proposal()
        original = self.decide(proposal, work_item_intent="create")
        item_id = original["work_item"]["id"]
        store = self.project.work_items
        store.transition(item_id, expected_rev=1, status="doing", actor="worker")
        pointer = self.project.root / "decisions" / f"{proposal['id']}.json"
        pointer.unlink()
        # A valid retry returns the original creation event, not revision 2,
        # and must not consult the live origin or create a second seed record.
        with patch.object(store, "_origin_revision", side_effect=AssertionError("Live origin replay")):
            replay = self.decide(proposal, work_item_intent="create")
        self.assertEqual(replay, original)
        self.assertEqual(json.loads(pointer.read_text()), original)
        directory = self.project.root / "work-items" / item_id
        self.assertFalse((directory / "create.json").exists())
        self.assertEqual(store.get(item_id)["rev"], 2)
        event = self.project.root / "decision-history" / f"{item_id[3:]}.json"
        before = event.read_bytes()
        pointer.unlink()
        revision = directory / "2.json"
        valid = revision.read_bytes()
        revision.write_text("{}")
        with self.assertRaises(Denied):
            self.decide(proposal, work_item_intent="create")
        self.assertFalse(pointer.exists())
        self.assertEqual(event.read_bytes(), before)
        revision.write_bytes(valid)
        self.assertEqual(self.decide(proposal, work_item_intent="create"), original)

    def test_decision_retry_rejects_conflicting_storage_even_without_transitions(self):
        for filename in ("3.json", "legacy.json", "create.json", "unknown.json"):
            with self.subTest(filename=filename):
                proposal = self.proposal()
                event = self.decide(proposal, work_item_intent="create")
                directory = self.project.root / "work-items" / event["work_item"]["id"]
                directory.mkdir(parents=True)
                (directory / filename).write_text("{}")
                pointer = self.project.root / "decisions" / f"{proposal['id']}.json"
                before = pointer.read_bytes()
                with self.assertRaises(Denied):
                    self.decide(proposal, work_item_intent="create")
                self.assertEqual(pointer.read_bytes(), before)
