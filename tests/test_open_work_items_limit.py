"""Open-work seed transactions use the ledger and its shared record lock."""
import multiprocessing

from support import Fixture, bind_delivery
from mizu.config import load
from mizu.errors import ConfigError, Denied, OpenWorkItemsFull
from mizu.fs import write_json
from mizu.insights import Insights, _decision_key
from mizu.project import Project


# Spawn/report waits are host costs, not product deadlines. The child's
# release backstop covers all parent readiness waits before the gate opens.
CHILD_WAIT_SECONDS = 60
CHILD_RELEASE_SECONDS = 600


def compete(root, kind, insight_id, legacy_id, ready, release, results):
    """Independent writers share only durable state and the project lock."""
    from pathlib import Path
    insights = Insights(Path(root), open_work_items=2)
    store = insights._work_item_store()
    ready.put(kind)
    if not release.wait(CHILD_RELEASE_SECONDS):
        results.put((kind, "synchronization timeout"))
        return
    try:
        if kind == "direct":
            store.create(idempotency_key="race", title="Direct", actor="worker")
        elif kind == "decision":
            insights.decide(insight_id, "accept", "Take work", "", "run", expected_rev=1)
        else:
            store.reconcile(legacy_id, expected_action="accept", outcome="open",
                            reason="Take work", actor="worker", title="Legacy")
        results.put((kind, "created"))
    except OpenWorkItemsFull as exc:
        results.put((kind, exc.evidence))
    except Exception as exc:
        results.put((kind, type(exc).__name__ + ": " + str(exc)))


class OpenWorkItemsLimitTests(Fixture):
    def setUp(self):
        super().setUp()
        self.insights = self.project.insights
        self.store = self.project.work_items
        self.insights.open_work_items = 2
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

    def assert_full(self, call, current, delta=1):
        with self.assertRaises(OpenWorkItemsFull) as caught:
            call()
        exc = caught.exception
        self.assertIsInstance(exc, Denied)
        self.assertEqual(exc.evidence, {"type": "OpenWorkItemsFull", "current_count": current,
                                      "limit": self.insights.open_work_items,
                                      "requested_delta": delta})
        for key, value in exc.evidence.items():
            self.assertIn(str(value) if key == "type" else f"{key}={value}", str(exc))

    def assert_undecided(self, insight_id):
        self.assertFalse((self.project.root / "decision-history" /
                          f"{_decision_key(insight_id, 1)}.json").exists())
        self.assertFalse((self.project.root / "decisions" / f"{insight_id}.json").exists())
        self.assertIsNone(self.insights.read(insight_id).get("decision"))

    def test_config_default_disabled_bounds_and_project_wiring(self):
        text = self.file.read_text()
        self.assertEqual(load(self.file).limits.open_work_items, 100)
        self.file.write_text(text.replace("open_work_items = 100\n", ""))
        self.assertEqual(load(self.file).limits.open_work_items, 100)
        for value in (0, 1, 1073741824):
            self.file.write_text(text.replace("open_work_items = 100", f"open_work_items = {value}"))
            config = load(self.file)
            self.assertEqual(config.limits.open_work_items, value)
            self.assertEqual(Project(config, "sample").insights.open_work_items, value)
        for value in ("-1", "1073741825", "true", "0.5", '"2"'):
            self.file.write_text(text.replace("open_work_items = 100", f"open_work_items = {value}"))
            with self.assertRaises(ConfigError):
                load(self.file)

    def test_direct_boundary_and_replay_when_full(self):
        first = self.create()
        self.create("two")
        self.assertEqual(self.store.page_open()["total"], 2)
        self.assert_full(lambda: self.create("three"), 2)
        self.assertEqual(self.create(), {"record": first["record"], "created": False})
        with self.assertRaisesRegex(Denied, "Conflicting"):
            self.store.create(idempotency_key="one", title="Changed", actor="worker")
        self.assertEqual(self.store.page_open()["total"], 2)

    def test_count_current_statuses_and_release_on_terminal_transition(self):
        first = self.create()["record"]["work_item"]
        second = self.create("two")["record"]["work_item"]
        self.store.transition(first["id"], expected_rev=1, status="doing", actor="worker")
        self.store.transition(second["id"], expected_rev=1, status="blocked", actor="worker",
                              reason="Waiting",
                              wait={"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"})
        self.assert_full(lambda: self.create("three"), 2)
        self.store.transition(first["id"], expected_rev=2, status="dropped", actor="worker",
                              reason="Merged")
        self.create("three")
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snapshot = self.project.snapshots.create(
            captured, goal="Goal", state="State", run="run", outcome="done", summary="Verified",
            verification={"passed": True, "code_digest": captured["code_digest"], "run": "run"})
        self.project.snapshots.publish(snapshot)
        self.store.transition(second["id"], expected_rev=2, status="doing", actor="worker")
        bind_delivery(self.project, second["id"], snapshot["id"], expected_rev=3)
        self.store.transition(second["id"], expected_rev=4, status="done", actor="worker",
                              snapshot_id=snapshot["id"])
        self.create("four")
        self.assertEqual(self.store.page_open()["total"], 2)

    def test_disabled_then_lowered_cap_preserves_over_limit_work(self):
        self.insights.open_work_items = 0
        first = self.create()
        event = self.decide(self.proposal(), work_items=self.tasks)
        self.insights.open_work_items = 1
        self.assertEqual(self.store.page_open()["total"], 3)
        self.assert_full(lambda: self.create("extra"), 3)
        self.assertFalse(self.create()["created"])
        self.store.transition(first["record"]["work_item"]["id"], expected_rev=1,
                              status="doing", actor="worker")
        for seed in event["work_items"]:
            self.assertEqual(self.store.get(seed["id"]), seed)

    def test_single_decisions_are_atomic_and_input_unchanged(self):
        self.create()
        accepted = self.proposal()
        event = self.decide(accepted)
        pending = self.proposal()
        before = self.insights.read(pending)
        self.assert_full(lambda: self.decide(pending, action="modify"), 2)
        self.assert_undecided(pending)
        self.assertEqual(self.insights.read(pending), before)
        self.assertEqual(self.decide(accepted), event)
        self.assertEqual(self.store.page_open()["total"], 2)
        self.assert_full(lambda: self.reconcile(self.legacy(1)), 2)

    def test_multi_task_delta_all_or_nothing_and_replay(self):
        self.create()
        proposal = self.proposal()
        self.assert_full(lambda: self.decide(proposal, work_items=self.tasks), 1, delta=2)
        self.assert_undecided(proposal)
        self.assertEqual(self.store.page_open()["total"], 1)
        self.insights.open_work_items = 3
        event = self.decide(proposal, work_items=self.tasks)
        self.assertEqual(len(event["work_items"]), 2)
        self.assertEqual(self.decide(proposal, work_items=self.tasks), event)
        self.assertEqual(self.store.page_open()["total"], 3)
        self.assert_full(lambda: self.decide(self.proposal(), work_items=self.tasks), 3, delta=2)

    def test_no_work_defer_reject_allowed_when_full(self):
        self.create()
        self.create("two")
        for action in ("accept", "modify", "defer", "reject"):
            with self.subTest(action=action):
                args = {"work_item_intent": "none"} if action in ("accept", "modify") else {}
                event = self.decide(self.proposal(), action=action, **args)
                self.assertNotIn("work_item", event)
        self.assertEqual(self.store.page_open()["total"], 2)

    def test_legacy_open_counts_replay_and_drop_works_at_capacity(self):
        opened, pending = self.legacy(1), self.legacy(2)
        first = self.reconcile(opened)
        self.create()
        self.assert_full(lambda: self.reconcile(pending), 2)
        self.assertFalse((self.project.root / "work-items" / f"wi_{pending}" / "legacy.json").exists())
        self.assertEqual(self.reconcile(opened), {"record": first["record"], "created": False})
        self.assertEqual(self.reconcile(pending, "drop")["record"]["outcome"], "drop")
        self.assert_full(lambda: self.create("extra"), 2)

    def test_tool_refusal_is_typed_and_does_not_commit(self):
        self.create()
        self.create("two")
        ctx = self.context()
        self.assert_full(lambda: ctx.handle("work_items_create", {
            "idempotency_key": "extra", "title": "extra"}), 2)
        self.assertFalse(ctx.progress_tool)
        self.assertEqual(self.store.page_open()["total"], 2)

    def test_corrupt_chain_cannot_supply_a_false_capacity_count(self):
        item = self.create()["record"]["work_item"]
        directory = self.project.root / "work-items" / item["id"]
        (directory / "3.json").write_text("{}")
        with self.assertRaisesRegex(Denied, "Gapped"):
            self.create("extra")
        self.assertFalse((self.project.root / "work-items" /
                          self.store._create_id("worker", "extra") / "create.json").exists())

    def test_process_writers_compete_for_one_slot_across_all_seed_paths(self):
        self.create()
        proposal, legacy_id = self.proposal(), self.legacy(1)
        mp = multiprocessing.get_context("spawn")
        ready, results, release = mp.Queue(), mp.Queue(), mp.Event()
        workers = [mp.Process(target=compete, args=(str(self.project.root), kind, proposal,
                                                   legacy_id, ready, release, results))
                   for kind in ("direct", "decision", "legacy")]
        try:
            for process in workers:
                process.start()
            self.assertEqual({ready.get(timeout=CHILD_WAIT_SECONDS) for _ in workers}, {"direct", "decision", "legacy"})
            release.set()
            outcomes = [results.get(timeout=CHILD_WAIT_SECONDS)[1] for _ in workers]
            self.assertEqual(outcomes.count("created"), 1, outcomes)
            self.assertEqual([row for row in outcomes if isinstance(row, dict)],
                             [{"type": "OpenWorkItemsFull", "current_count": 2,
                               "limit": 2, "requested_delta": 1}] * 2)
            for process in workers:
                process.join(CHILD_WAIT_SECONDS)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(self.store.page_open()["total"], 2)
        finally:
            release.set()
            for process in workers:
                if process.is_alive():
                    process.terminate()
                if process.pid is not None:
                    process.join(CHILD_WAIT_SECONDS)
            ready.close()
            results.close()
