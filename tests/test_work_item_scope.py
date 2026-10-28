"""Accepted scope: recorded at decision time, inherited, and bound by closure."""
import json

from support import Fixture, bind_delivery
from mizu.errors import Denied
from mizu.protocol import DEFINITIONS, validate


class WorkItemScopeTests(Fixture):
    """The scope a decision accepts is the scope every later revision holds.

    A scope change is its own recorded event; a closure may not change the
    scope it inherits.
    """

    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.serial = 0

    def proposal(self, title="Scoped work"):
        self.serial += 1
        return self.project.insights.submit(source="reviewer", title=title,
                                            body="Evidence", base_snapshot=None)

    def decide(self, scope=None, action="accept", reason="reviewed"):
        proposal = self.proposal()
        options = {} if scope is None else {"scope": scope}
        return self.project.insights.decide(proposal["id"], action, reason, "", "run-1",
                                            expected_rev=1, **options)

    def snapshot(self):
        """Publish one verified snapshot of the presented workspace."""
        store = self.project.snapshots
        captured = store.capture_files(self.project.workspace)
        verification = {"passed": True, "code_digest": captured["code_digest"], "run": "run-1"}
        snapshot = store.create(captured, goal="Goal", state="State", run="run-1",
                                outcome="done", summary="Verified", verification=verification)
        store.publish(snapshot)
        return snapshot

    def change(self, item_id, rev, status="doing", **kwargs):
        return self.store.transition(item_id, expected_rev=rev, status=status,
                                     actor=kwargs.pop("actor", "worker"), **kwargs)

    def test_decision_records_the_accepted_scope_in_its_seed(self):
        scope = "Keep the CI cell timeout at 30 minutes."
        proposal = self.proposal()
        decision = self.project.insights.decide(proposal["id"], "accept", "reviewed", "",
                                                "run-1", expected_rev=1, scope=scope)
        self.assertEqual(decision["scope"], scope)
        seed = decision["work_item"]
        self.assertEqual(seed["scope"], scope)
        self.assertEqual(self.store.get(seed["id"])["scope"], scope)
        # The immutable decision event carries the same scope.
        path = self.project.root / "decision-history" / f"{seed['id'][3:]}.json"
        self.assertEqual(json.loads(path.read_text())["scope"], scope)
        # The current decision pointer carries it too and stays readable.
        status = self.store.submitter_status(proposal["id"])
        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "open")

    def test_decision_without_a_scope_keeps_the_legacy_shape(self):
        decision = self.decide()
        self.assertNotIn("scope", decision)
        self.assertNotIn("scope", decision["work_item"])
        self.assertIsNone(self.store.get(decision["work_item"]["id"]).get("scope"))

    def test_an_invalid_accepted_scope_is_refused(self):
        for value in ("", " \n\t", "x" * 4001, 1, [], {"scope": "text"}):
            with self.subTest(value_type=type(value)), self.assertRaises(Denied):
                self.decide(scope=value)
        with self.assertRaises(Denied):
            self.project.insights.decide(self.proposal()["id"], "accept", "reviewed", "",
                                         "run-1", expected_rev=1, scope="   ")

    def test_a_stated_scope_is_validated_by_the_tool_schema(self):
        schema = DEFINITIONS["decide"][1]
        base = {"id": "x" * 32, "action": "accept", "reason": "ok", "rev": 1}
        validate({**base, "scope": "Deliver the receipt"}, schema)
        for value in ("", "  ", "x" * 4001, 5, None):
            with self.subTest(value_type=type(value)), self.assertRaises(Denied):
                validate({**base, "scope": value}, schema)

    def test_closure_inherits_the_accepted_scope(self):
        scope = "Keep the CI cell timeout at 30 minutes."
        decision = self.decide(scope=scope)
        item_id = decision["work_item"]["id"]
        doing = self.change(item_id, 1, reason="Started")
        self.assertEqual(doing["scope"], scope)
        snapshot = self.snapshot()
        bind_delivery(self.project, item_id, snapshot["id"], expected_rev=2)
        closed = self.change(item_id, 3, status="done", reason="Verified",
                             snapshot_id=snapshot["id"])
        self.assertEqual(closed["scope"], scope)
        self.assertEqual(self.store.get(item_id)["scope"], scope)

    def test_a_scope_change_is_its_own_recorded_event(self):
        first = "Keep the CI cell timeout at 30 minutes."
        second = "Raise the CI cell timeout to 60 minutes."
        decision = self.decide(scope=first)
        item_id = decision["work_item"]["id"]
        changed = self.change(item_id, 1, scope=second, reason="Superseded the first scope")
        self.assertEqual(changed["scope"], second)
        history = self.store.explain(item_id)["history"]
        # The seed states the accepted scope and the revision states the change.
        self.assertEqual([record["scope"] for record in history], [first, second])
        item = self.store.explain(item_id)["item"]
        self.assertEqual(item["scope"], second)
        self.assertEqual(item["rev"], 2)
        # The change is visible in the item's history, not only in prose.
        self.assertEqual(self.store.get(item_id)["scope"], second)

    def test_a_closure_cannot_change_the_accepted_scope(self):
        scope = "Keep the CI cell timeout at 30 minutes."
        decision = self.decide(scope=scope)
        item_id = decision["work_item"]["id"]
        self.change(item_id, 1)
        snapshot = self.snapshot()
        bind_delivery(self.project, item_id, snapshot["id"], expected_rev=2)
        with self.assertRaisesRegex(Denied, "Completion cannot change the accepted scope"):
            self.change(item_id, 3, status="done", reason="Changed the scope",
                        scope="Raise the CI cell timeout to 60 minutes.",
                        snapshot_id=snapshot["id"])
        # The refused closure wrote nothing; the scope in force is unchanged.
        self.assertEqual(self.store.get(item_id)["rev"], 3)
        self.assertEqual(self.store.get(item_id)["scope"], scope)
        # A closure that states the same scope is accepted.
        closed = self.change(item_id, 3, status="done", reason="Verified", scope=scope,
                             snapshot_id=snapshot["id"])
        self.assertEqual(closed["scope"], scope)

    def test_a_closure_of_an_item_without_a_scope_may_state_one(self):
        decision = self.decide()
        item_id = decision["work_item"]["id"]
        snapshot = self.snapshot()
        with self.assertRaises(Denied):
            self.change(item_id, 1, status="done", reason="Verified",
                        scope="Deliver the bounded receipt.", snapshot_id=snapshot["id"])
        self.change(item_id, 1, scope="Deliver the bounded receipt.", reason="State accepted scope")
        bind_delivery(self.project, item_id, snapshot["id"], expected_rev=2)
        closed = self.change(item_id, 3, status="done", reason="Verified", snapshot_id=snapshot["id"])
        self.assertEqual(closed["scope"], "Deliver the bounded receipt.")
        self.assertEqual(self.store.get(item_id)["scope"], "Deliver the bounded receipt.")

    def test_a_transition_without_a_scope_never_clears_it(self):
        scope = "Keep the CI cell timeout at 30 minutes."
        decision = self.decide(scope=scope)
        item_id = decision["work_item"]["id"]
        first = self.change(item_id, 1)
        self.assertEqual(first["scope"], scope)
        second = self.change(item_id, 2, status="blocked", reason="Waiting",
                             wait={"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"})
        self.assertEqual(second["scope"], scope)
        third = self.change(item_id, 3, status="doing", reason="Resumed")
        self.assertEqual(third["scope"], scope)
        self.assertEqual(self.store.get(item_id)["scope"], scope)

    def test_a_record_that_clears_the_scope_is_refused(self):
        from mizu.fs import write_json
        scope = "Keep the CI cell timeout at 30 minutes."
        decision = self.decide(scope=scope)
        item_id = decision["work_item"]["id"]
        doing = self.change(item_id, 1)
        path = self.project.root / "work-items" / item_id / "2.json"
        write_json(path, {**doing, "scope": None})
        for read in (lambda: self.store.get(item_id), self.store.page_open_readonly,
                     lambda: self.store.explain(item_id)):
            with self.assertRaises(Denied):
                read()

    def test_created_work_carries_its_scope(self):
        created = self.store.create(idempotency_key="scoped", title="Self-directed work",
                                    actor="worker", scope="Deliver the bounded receipt.")
        seed = created["record"]["work_item"]
        self.assertEqual(seed["scope"], "Deliver the bounded receipt.")
        self.assertEqual(self.store.get(seed["id"])["scope"], "Deliver the bounded receipt.")
        # The scope is part of the immutable creation request.
        with self.assertRaisesRegex(Denied, "Conflicting"):
            self.store.create(idempotency_key="scoped", title="Self-directed work",
                              actor="worker")

    def test_created_work_without_a_scope_keeps_the_legacy_shape(self):
        created = self.store.create(idempotency_key="plain", title="Self-directed work",
                                    actor="worker")
        seed = created["record"]["work_item"]
        self.assertNotIn("scope", seed)
        doing = self.change(seed["id"], 1)
        self.assertIsNone(doing["scope"])

    def test_the_operator_cli_carries_the_accepted_scope(self):
        from mizu.cli import _cmd_insight, parser
        proposal = self.proposal()
        args = parser().parse_args(["insight", "decide", self.project.name, "--id", proposal["id"],
                                    "--decision", "accept", "--reason", "Work",
                                    "--expected-rev", "1", "--work-item-intent", "create",
                                    "--accepted-scope", "Deliver the bounded receipt."])
        result = _cmd_insight(self.config, self.project, args)
        self.assertEqual(result["scope"], "Deliver the bounded receipt.")
        self.assertEqual(result["work_item"]["scope"], "Deliver the bounded receipt.")

    def test_the_tool_carries_the_scope_through(self):
        ctx = self.context("worker")
        proposal = self.proposal()
        record = ctx.handle("decide", {"id": proposal["id"], "action": "accept",
                                       "reason": "reviewed", "rev": 1,
                                       "scope": "Deliver the bounded receipt."})
        self.assertEqual(record["scope"], "Deliver the bounded receipt.")
        item_id = record["work_item"]["id"]
        changed = ctx.handle("work_items_transition", {"id": item_id, "expected_rev": 1,
                                                       "status": "doing",
                                                       "reason": "Superseded the first scope",
                                                       "scope": "Deliver the receipt and its summary."})
        self.assertEqual(changed["scope"], "Deliver the receipt and its summary.")

    def test_the_page_rows_show_the_scope_in_force(self):
        scope = "Keep the CI cell timeout at 30 minutes."
        decision = self.decide(scope=scope)
        item_id = decision["work_item"]["id"]
        self.change(item_id, 1, reason="Started")
        page = self.store.page_open_readonly(limit=10)
        row = next(r for r in page["items"] if r["id"] == item_id)
        self.assertEqual(row["scope_preview"], scope)
        self.assertFalse(row["scope_truncated"])
        snapshot = self.snapshot()
        bind_delivery(self.project, item_id, snapshot["id"], expected_rev=2)
        self.change(item_id, 3, status="done", reason="Verified", snapshot_id=snapshot["id"])
        closed = self.store.page_closed_readonly(limit=10)
        crow = next(r for r in closed["items"] if r["id"] == item_id)
        self.assertEqual(crow["scope_preview"], scope)
        self.assertFalse(crow["scope_truncated"])

    def test_a_scope_change_is_its_own_event_even_at_the_same_status(self):
        first = "Keep the CI cell timeout at 30 minutes."
        second = "Raise the CI cell timeout to 60 minutes."
        decision = self.decide(scope=first)
        item_id = decision["work_item"]["id"]
        self.change(item_id, 1)
        # A scope change is its own recorded event, so it may stand at the
        # same status and it carries the reason that names what it supersedes.
        changed = self.change(item_id, 2, status="doing", scope=second,
                              reason="Superseded the first scope")
        self.assertEqual(changed["scope"], second)
        history = self.store.explain(item_id)["history"]
        self.assertEqual([record["scope"] for record in history],
                         [first, first, second])
        with self.assertRaisesRegex(Denied, "A scope change requires a reason"):
            self.change(item_id, 3, status="doing", scope="Raise it to 90 minutes.")
