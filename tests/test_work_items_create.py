"""Direct work creation: real stores, runtime grants and offline persistence."""
import concurrent.futures
import copy
import dataclasses
import json
import os
from unittest.mock import patch

from support import Fixture, bind_delivery, forget_ledger
from mizu import work_item_events as events
from mizu.errors import ConfigError, Denied, LimitExceeded
from mizu.fs import PREVIEW_BYTES
from support import history_write_json as write_json
from mizu.project import Project
from mizu.protocol import DEFINITIONS, validate
from mizu.work_item_seed import MAX_ORIGIN_CHARS
from mizu.runtime import (CONSULT_FORBIDDEN, DELEGATE_FORBIDDEN, WRAP_UP_OPS,
                          check_consult_role, check_delegate_role, prompt_for, prompt_delta_for)
from mizu.storage import audit, backup, restore


class DirectWorkItemTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items

    def create(self, key="stable-key", title="Self-directed work", origin=None):
        return self.store.create(idempotency_key=key, title=title, origin=origin, actor="worker")

    def test_concurrent_retry_and_actor_scoped_key(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.create(), range(4)))
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertTrue(all(r["record"] == results[0]["record"] for r in results))
        other = self.store.create(idempotency_key="stable-key", title="Self-directed work", actor="other")
        self.assertNotEqual(other["record"]["work_item"]["id"], results[0]["record"]["work_item"]["id"])

    def test_invalid_requests_and_missing_origins_fail_closed(self):
        for origin in ("", "   ", "bad\x00origin", "x" * (MAX_ORIGIN_CHARS + 1),
                       {}, {"rev": 1}, {"insight_id": "missing", "rev": 1},
                       {"work_item_id": "wi_" + "f" * 32, "rev": 1}):
            with self.subTest(origin=origin), self.assertRaises(Denied):
                self.create(origin=origin)
        for key, title in (("", "Title"), ("k" * 129, "Title"), ("key", ""), ("key", "t" * 201)):
            with self.assertRaises(Denied):
                self.create(key, title)
        self.assertEqual(self.store.page_open()["total"], 0)
        # A parent id is now just an opaque string reference, not a lookup.
        parent = self.store._create_id("worker", "parent-key")
        item = self.create("child-key", origin=parent)["record"]["work_item"]
        self.assertEqual(item["origin"], parent)

    def test_title_length_edges_fit_the_seed_format(self):
        for key, title in (("short-title", "x"), ("maximum-title", "t" * 200)):
            with self.subTest(length=len(title)):
                item = self.create(key, title)["record"]["work_item"]
                self.assertEqual(item["title"], title)

    def test_terminal_delivery_routes_by_creator_and_treats_origin_as_opaque(self):
        message = self.create("message", origin="message:proposal")["record"]["work_item"]
        parent = self.create("parent")["record"]["work_item"]
        split = self.create("split", origin=parent["id"])["record"]["work_item"]
        ids = {item["id"] for item in (message, parent, split)}
        for item in (message, parent, split):
            self.store.transition(item["id"], expected_rev=1, status="dropped", reason="Closed", actor="worker")
        # Delivery follows the host-supplied creator. An origin string, even
        # one naming another item, never routes the event to another role.
        own = events.pending(self.store, "worker", ("dropped",))
        self.assertEqual({r["id"] for r in own["events"]}, ids)
        self.assertEqual({r["creator"] for r in own["events"]}, {"worker"})
        self.assertEqual((own["unknown_routing"], own["routing_complete"]), (0, True))
        any_source = events.pending(self.store, "reviewer", ("dropped",), sources="any")
        self.assertEqual((any_source["pending"], any_source["unknown_routing"]), (3, 0))
        self.assertEqual(events.pending(self.store, "reviewer", ("dropped",))["pending"], 0)
        events.acknowledge(self.store, "worker", own["events"])
        self.assertEqual(events.pending(self.store, "worker", ("dropped",))["acknowledged"], 3)

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
        bind_delivery(self.project, item["id"], snapshot["id"])
        done = self.store.transition(item["id"], expected_rev=3, status="done", actor="worker", snapshot_id=snapshot["id"])
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
        forget_ledger(self.project.root)
        self.assertEqual(self.store.page_open_readonly()["total"], 0)
        self.assertTrue(temp.exists())
        self.assertEqual(self.store.page_open()["total"], 0)
        self.assertFalse(temp.exists())
        (directory / "unknown.json").write_text("{}")
        forget_ledger(self.project.root)
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
        changed["work_item"]["origin"] = "bad\x00origin"
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
        forget_ledger(self.project.root)
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
                        {"title": "t" * 201}, {"origin": "   "}):
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

    def test_origin_schema_rejects_malformed_at_protocol(self):
        schema = DEFINITIONS["work_items_create"][1]
        base = {"idempotency_key": "origin-key", "title": "Origin work"}
        parent = "wi_" + "a" * 32
        validate(dict(base), schema)
        # An origin is opaque text at the protocol: a message id and a parent
        # work-item id are both valid, with no reference shape to interpret.
        for origin in ("message:proposal", parent, "x" * MAX_ORIGIN_CHARS):
            with self.subTest(origin=origin):
                validate({**base, "origin": origin}, schema)
        for origin in ({"rev": 1}, {}, {"insight_id": "proposal", "rev": 1},
                       {"work_item_id": parent, "rev": 1}, "", "   ",
                       "bad\x00origin", "x" * (MAX_ORIGIN_CHARS + 1), 1):
            with self.subTest(origin=origin), self.assertRaises(Denied):
                validate({**base, "origin": origin}, schema)
