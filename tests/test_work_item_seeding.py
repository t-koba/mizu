"""Work-item seeding is independent of project backlog, not transaction bounds."""
from support import Fixture, forget_ledger
from support import history_write_json as write_json
from mizu.config import load
from mizu.errors import Denied
from mizu.project import Project
from mizu.runtime import producer_hold


class WorkItemSeedingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.insights = self.project.insights
        self.store = self.project.work_items
        self.tasks = [{"title": "First", "description": "First slice"},
                      {"title": "Second", "description": "Second slice"}]

    def create(self, key="one"):
        return self.store.create(idempotency_key=key, title=key, actor="worker")

    def proposal(self):
        return self.insights.submit(source="worker", title="Proposal", body="Input",
                                    base_snapshot=None)["id"]

    def decide(self, insight_id, action="accept", **kwargs):
        return self.insights.decide(insight_id, action, "Take work", "Later", "run",
                                    expected_rev=1, **kwargs)

    def legacy(self, serial):
        event_id = f"{serial:032x}"
        write_json(self.project.root / "decision-history" / f"{event_id}.json",
                   {"id": "legacy-input", "action": "accept", "rev": 1,
                    "reason": "Retained", "revisit": "", "run": "run",
                    "created_at": "2024-01-01T00:00:00+00:00"})
        return event_id

    def reconcile(self, event_id, outcome="open"):
        return self.store.reconcile(event_id, expected_action="accept", outcome=outcome,
                                    reason="Retained", actor="worker",
                                    title="Legacy" if outcome == "open" else None)

    def test_all_seed_paths_and_replays_above_former_backlog_cap(self):
        for index in range(101):
            self.create(str(index))
        direct = self.create()
        with self.assertRaisesRegex(Denied, "Conflicting"):
            self.store.create(idempotency_key="one", title="Changed", actor="worker")
        self.assertEqual(self.create(), {"record": direct["record"], "created": False})
        for action in ("accept", "modify"):
            for tasks in (None, self.tasks):
                insight = self.proposal()
                kwargs = {} if tasks is None else {"work_items": tasks}
                event = self.decide(insight, action=action, **kwargs)
                self.assertEqual(self.decide(insight, action=action, **kwargs), event)
                for seed in event.get("work_items", [event.get("work_item")]):
                    self.assertEqual(self.store.get(seed["id"]), seed)
        legacy = self.legacy(1)
        first = self.reconcile(legacy)
        self.assertEqual(self.reconcile(legacy), {"record": first["record"], "created": False})
        ctx = self.context()
        ctx.handle("work_items_create", {"idempotency_key": "tool", "title": "Tool"})
        page = self.store.page_open(limit=2)
        self.assertEqual(page["total"], 110)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["truncated"])
        self.assertEqual(page["overflow_count"], 108)
        self.assertIsNone(producer_hold(self.project, "searcher"))

    def test_accepted_seeds_succeed_above_configured_producer_threshold(self):
        self.file.write_text(self.file.read_text().replace("open_work_items = 0", "open_work_items = 1")
                             .replace("held_at_capacity = false", "held_at_capacity = true"))
        self.project = Project(load(self.file), "sample")
        self.insights, self.store = self.project.insights, self.project.work_items
        self.create("existing")
        self.create("above-threshold")
        self.assertEqual(producer_hold(self.project, "searcher")["reason"], "open_work_full")
        for action in ("accept", "modify"):
            for tasks in (None, self.tasks):
                proposal = self.proposal()
                kwargs = {} if tasks is None else {"work_items": tasks}
                event = self.decide(proposal, action=action, **kwargs)
                self.assertEqual(self.decide(proposal, action=action, **kwargs), event)
                for seed in event.get("work_items", [event.get("work_item")]):
                    self.assertEqual(self.store.get(seed["id"]), seed)
        self.assertEqual(self.store.count_open_readonly(), 8)

    def test_corrupt_chain_refuses_every_seed_path_before_commit(self):
        item = self.create()["record"]["work_item"]
        proposal, legacy = self.proposal(), self.legacy(1)
        directory = self.project.root / "work-items" / item["id"]
        (directory / "3.json").write_text("{}")
        for kind in ("direct", "single", "multi", "legacy"):
            with self.subTest(kind=kind):
                forget_ledger(self.project.root)
                with self.assertRaisesRegex(Denied, "Gapped"):
                    if kind == "direct":
                        self.create("extra")
                    elif kind in ("single", "multi"):
                        kwargs = {} if kind == "single" else {"work_items": self.tasks}
                        self.decide(proposal, **kwargs)
                    else:
                        self.reconcile(legacy)
        self.assertIsNone(self.insights.read(proposal).get("decision"))
        self.assertFalse((self.project.root / "work-items" /
                          self.store._create_id("worker", "extra") / "create.json").exists())
        self.assertFalse((self.project.root / "work-items" / f"wi_{legacy}" / "legacy.json").exists())
