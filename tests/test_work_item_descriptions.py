"""Immutable optional descriptions across both seed shapes and delivery paths."""
import copy
import json

from support import Fixture
from mizu import work_item_events as events
from mizu.errors import Denied, LimitExceeded
from mizu.fs import PREVIEW_BYTES, canonical, write_json
from mizu.project import Project
from mizu.protocol import DEFINITIONS, validate
from mizu.runtime import prompt_for, prompt_delta_for
from mizu.storage import backup, restore
from mizu.work_item_seed import SEED_FIELDS, make_seed, validate_seed
from mizu.work_items import EVENT_FIELDS, MAX_OPEN_ROW_BYTES


class WorkItemDescriptionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items

    def create(self, **kwargs):
        return self.store.create(idempotency_key="key", title="Work", actor="worker", **kwargs)

    def decision(self, reason="Implement the reviewed scope", action="accept"):
        proposal = self.project.insights.submit(source="reviewer", title="Proposal", body="Evidence",
                                                base_snapshot=None)
        event = self.project.insights.decide(proposal["id"], action, reason, "", "run", expected_rev=1)
        return proposal, event

    def test_direct_persistence_exact_retry_and_description_conflicts(self):
        description = "  Scope\nwith exact whitespace preserved.  "
        first = self.create(description=description)
        seed = first["record"]["work_item"]
        self.assertEqual(seed["description"], description)
        self.assertEqual(set(seed), SEED_FIELDS | {"description"})
        path = self.project.root / "work-items" / seed["id"] / "create.json"
        original = path.read_bytes()
        self.assertEqual(json.loads(original), first["record"])
        self.store.transition(seed["id"], expected_rev=1, status="doing", actor="worker")
        self.assertEqual(self.create(description=description), {"record": first["record"], "created": False})
        for args in ({}, {"description": description.strip()}, {"description": "Another scope"}):
            with self.subTest(args=args), self.assertRaisesRegex(Denied, "Conflicting"):
                self.create(**args)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.store.get(seed["id"])["description"], description)

    def test_omission_preserves_v1_seed_retry_and_transition_shape(self):
        first = self.create()
        seed = first["record"]["work_item"]
        self.assertEqual(set(seed), SEED_FIELDS)
        self.assertEqual(make_seed(seed["id"], "Work", None, seed["created_at"]), seed)
        path = self.project.root / "work-items" / seed["id"] / "create.json"
        original = path.read_bytes()
        doing = self.store.transition(seed["id"], expected_rev=1, status="doing", actor="worker")
        history_bytes = (path.parent / "2.json").read_bytes()
        self.assertEqual(set(doing), EVENT_FIELDS)
        blocked = self.store.transition(seed["id"], expected_rev=2, status="blocked", actor="worker",
                wait={"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"})
        self.assertEqual(set(blocked), EVENT_FIELDS)
        self.assertEqual(self.store.get(seed["id"]), blocked)
        self.assertEqual(self.create(), {"record": first["record"], "created": False})
        with self.assertRaisesRegex(Denied, "Conflicting"):
            self.create(description="Cannot upgrade an immutable v1 seed")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((path.parent / "2.json").read_bytes(), history_bytes)
        self.assertNotIn("description_preview", self.store.page_open()["items"][0])

    def test_description_limits_in_protocol_store_and_seed_validator(self):
        schema = DEFINITIONS["work_items_create"][1]
        args = {"idempotency_key": "key", "title": "Work"}
        for value in ("", " \n\t\u3000", "x" * 4001, None, 1, [], "\ud800"):
            with self.subTest(value_type=type(value), length=len(value) if isinstance(value, str) else None):
                with self.assertRaises(Denied):
                    validate({**args, "description": value}, schema)
                with self.assertRaises(Denied):
                    self.create(description=value)
        self.assertEqual(self.store.page_open()["total"], 0)
        for value in ("x", "\n x\n", "😀" * 4000):
            validate({**args, "description": value}, schema)
            result = self.store.create(**args, actor="worker", description=value)
            seed = result["record"]["work_item"]
            self.assertEqual(validate_seed(seed), seed)
            self.assertLess(len(canonical(result["record"])), PREVIEW_BYTES)
            args["idempotency_key"] += "x"
        malformed = dict(seed, unexpected=True)
        with self.assertRaises(Denied):
            validate_seed(malformed)

    def test_v2_chain_retains_description_and_rejects_change_removal_or_addition(self):
        for version in (1, 2):
            with self.subTest(version=version):
                seed = self.store.create(idempotency_key=str(version), title="Work", actor="worker",
                                         **({"description": "Scope"} if version == 2 else {}))["record"]["work_item"]
                item_id = seed["id"]
                doing = self.store.transition(item_id, expected_rev=1, status="doing", actor="worker")
                blocked = self.store.transition(item_id, expected_rev=2, status="blocked", actor="worker",
                wait={"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"})
                self.assertEqual(set(blocked), EVENT_FIELDS | ({"description"} if version == 2 else set()))
                if version == 2:
                    self.assertEqual(blocked["description"], "Scope")
                path = self.project.root / "work-items" / item_id / "2.json"
                mutations = [dict(doing, description="Changed"), dict(doing, description=None)]
                if version == 2:
                    removed = copy.deepcopy(doing)
                    del removed["description"]
                    mutations.append(removed)
                for tampered in mutations:
                    write_json(path, tampered)
                    for read in (lambda: self.store.get(item_id), self.store.page_open_readonly,
                                 lambda: self.store.explain(item_id)):
                        with self.assertRaises(Denied):
                            read()
                write_json(path, doing)
                self.assertEqual(self.store.get(item_id), blocked)

    def test_decision_mapping_replay_and_tamper_rejection(self):
        for action in ("accept", "modify"):
            proposal, event = self.decision(action=action)
            seed = event["work_item"]
            self.assertEqual(seed["description"], event["reason"])
            path = self.project.root / "decision-history" / f"{seed['id'][3:]}.json"
            original = path.read_bytes()
            retry = lambda: self.project.insights.decide(proposal["id"], action, event["reason"], "", "retry",
                                                         expected_rev=1)
            self.assertEqual(retry(), event)
            self.assertEqual(path.read_bytes(), original)
            for changed_field in ("description", "reason"):
                tampered = copy.deepcopy(event)
                if changed_field == "description":
                    tampered["work_item"]["description"] = "Another scope"
                else:
                    tampered["reason"] = "Another scope"
                write_json(path, tampered)
                for read in (retry, lambda: self.store.get(seed["id"]), self.store.page_open_readonly):
                    with self.subTest(action=action, changed_field=changed_field), self.assertRaises(Denied):
                        read()
            write_json(path, event)
            self.assertEqual(retry(), event)

    def test_legacy_decision_without_description_retries_and_extends_v1_chain(self):
        proposal, event = self.decision()
        # Emulate a published pre-description keyed decision, including a reason
        # longer than new descriptions permit: do not revalidate it as v2.
        del event["work_item"]["description"]
        event["reason"] = "r" * 5000
        seed = event["work_item"]
        path = self.project.root / "decision-history" / f"{seed['id'][3:]}.json"
        current = self.project.root / "decisions" / f"{proposal['id']}.json"
        write_json(path, event)
        write_json(current, event)
        original = path.read_bytes()
        doing = self.store.transition(seed["id"], expected_rev=1, status="doing", actor="worker")
        self.assertEqual(set(doing), EVENT_FIELDS)
        self.assertEqual(self.store.get(seed["id"]), doing)
        self.assertEqual(self.project.insights.decide(proposal["id"], "accept", event["reason"], "", "retry",
                                                     expected_rev=1), event)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(current.read_bytes(), original)

    def test_new_decision_description_limit_is_atomic(self):
        proposal = self.project.insights.submit(source="reviewer", title="Proposal", body="Evidence",
                                                base_snapshot=None)
        with self.assertRaises(Denied):
            self.project.insights.decide(proposal["id"], "accept", "x" * 4001, "", "run", expected_rev=1)
        self.assertFalse((self.project.root / "decision-history").exists())
        self.assertFalse((self.project.root / "decisions" / f"{proposal['id']}.json").exists())
        self.assertEqual(self.store.page_open()["total"], 0)
        event = self.project.insights.decide(proposal["id"], "accept", "😀" * 4000, "", "run", expected_rev=1)
        self.assertLess(len(canonical(event)), PREVIEW_BYTES)
        self.assertEqual(event["work_item"]["description"], event["reason"])

    def test_runtime_omitted_description_keeps_v1_seed_shape(self):
        ctx = self.context("worker")
        result = ctx.handle("work_items_create", {"idempotency_key": "no-description", "title": "Work"})
        seed = result["record"]["work_item"]
        self.assertEqual(set(seed), SEED_FIELDS)
        self.assertNotIn("description", seed)

    def test_runtime_reads_prompts_compact_pages_and_terminal_delivery(self):
        ctx = self.context("worker")
        description = "😀" * 4000
        args = {"idempotency_key": "key", "title": "Work", "description": description}
        result = ctx.handle("work_items_create", args)
        seed = result["record"]["work_item"]
        item_id = seed["id"]
        self.assertEqual(seed["description"], description)
        self.assertFalse(ctx.handle("work_items_create", args)["created"])
        read = ctx.handle("work_items_read", {"id": item_id})
        self.assertEqual(read["item"]["description"], description)
        self.assertEqual(read["history"][0]["description"], description)
        row = self.store.page_open()["items"][0]
        self.assertEqual(row, self.store.page_open_readonly()["items"][0])
        for text in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            self.assertEqual(json.loads(text)["open_work_items"]["items"][0], row)
        self.store.transition(item_id, expected_rev=1, status="dropped", actor="worker", reason="Closed")
        closed = self.store.page_closed_readonly()["items"][0]
        payload = events.pending(self.store, "reviewer", ("dropped",), sources="any")["events"][0]
        for compact in (row, closed, payload):
            self.assertNotIn("description", compact)
            self.assertEqual(compact["description_preview"], "😀" * 256)
            self.assertTrue(compact["description_truncated"])
            self.assertLessEqual(len(compact["description_preview"].encode()), 1024)
            self.assertLessEqual(len(canonical(compact)), MAX_OPEN_ROW_BYTES)
        self.assertEqual(payload["routing"], "unknown")
        events.acknowledge(self.store, "reviewer", [payload])
        self.assertEqual(events.pending(self.store, "reviewer", ("dropped",), sources="any")["pending"], 0)

    def test_compact_short_preview_and_escaped_byte_bounds(self):
        seed = self.create(description="Brief scope")["record"]["work_item"]
        row = self.store.page_open()["items"][0]
        self.assertEqual((row["description_preview"], row["description_truncated"]), ("Brief scope", False))
        seed = self.store.create(idempotency_key="escaped", title="Work", actor="worker",
                                 description="x" + "\x01" * 3999)["record"]["work_item"]
        self.store.transition(seed["id"], expected_rev=1, status="dropped", actor="worker", reason="\x00" * 2000)
        # Preview bounds do not replace canonical row/envelope byte bounds.
        with self.assertRaises(LimitExceeded):
            self.store.page_closed_readonly()
        with self.assertRaises(LimitExceeded):
            events.pending(self.store, "reviewer", ("dropped",), sources="any")

    def test_backup_restore_keeps_v1_v2_description_and_exact_receipts(self):
        legacy = self.create()
        described = self.store.create(idempotency_key="v2", title="Work", actor="worker", description="Scope")
        proposal, decision = self.decision()
        for result in (legacy, described):
            self.store.transition(result["record"]["work_item"]["id"], expected_rev=1, status="dropped",
                                  actor="worker", reason="Closed")
        pending = events.pending(self.store, "reviewer", ("dropped",), sources="any")
        events.acknowledge(self.store, "reviewer", pending["events"])
        self.project.set_control(paused=True)
        archive = self.root / "backup.tar.gz"
        backup(self.project, archive)
        restore(self.config, "restored", archive)
        restored = Project(self.config, "restored")
        for key, result, kwargs in (("key", legacy, {}), ("v2", described, {"description": "Scope"})):
            self.assertEqual(restored.work_items.create(idempotency_key=key, title="Work", actor="worker", **kwargs),
                             {"record": result["record"], "created": False})
        self.assertEqual(restored.work_items.get(decision["work_item"]["id"])["description"], decision["reason"])
        self.assertEqual(restored.insights.decide(proposal["id"], "accept", decision["reason"], "", "retry",
                                                  expected_rev=1), decision)
        self.assertEqual(events.pending(restored.work_items, "reviewer", ("dropped",), sources="any")["acknowledged"], 2)
