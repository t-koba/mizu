"""Display-only routing attention and capability-based decision responsibility."""
from copy import deepcopy
import json

from support import Fixture, ScriptDriver
from mizu.runtime import Engine
from mizu.insights import insight_metadata
from mizu.unit_prompts import prompt_delta_for, prompt_for


def _pending_event(source="operator", addressee="writer"):
    """The flattened row shape delivered by decision_delivery for an insight."""
    return {
        "event": "abc123", "insight": "i1", "rev": 1,
        "kind": "insight.pending", "insight_kind": "proposal", "urgency": "normal",
        "title": "Editorial", "body": "Body", "source": source,
        "created_at": "2026-11-17T00:00:00+00:00",
        "target": source if addressee == "writer" else addressee,
        "addressee": addressee,
        "ack_token": "0" * 64,
    }


class InsightAddresseeDisplayTests(Fixture):
    def _submit(self, source="operator", *, title="Editorial", **metadata):
        return self.project.insights.submit(
            source=source, title=title, body="Body",
            base_snapshot=self.project.snapshots.get()["id"], **metadata)

    def _reader_rows(self, ctx, items):
        pending = self.project.insights.list(limit=30)
        return {
            "full": json.loads(prompt_for(ctx))["pending_insights"],
            "delta": json.loads(prompt_delta_for(ctx, pending))["pending_insights"],
            "list": ctx.handle("insights", {"limit": 1000})["insights"],
            "by-id": [ctx.handle("insights", {"id": item["id"]}) for item in items],
        }

    def _assert_display(self, row, attention, *, can_decide):
        self.assertNotIn("addressee", row)
        self.assertEqual(row["attention"], attention)
        if can_decide:
            self.assertEqual(row["decide"], "you")
        else:
            self.assertNotIn("decide", row)

    def _assert_reader(self, role, *, can_decide):
        items = [self._submit(source) for source in ("operator", "searcher", "worker")]
        stored = [self.project.insights.read(item["id"]) for item in items]
        ctx = self.context(role)
        self.assertEqual("decide" in ctx.role.capabilities, can_decide)
        for surface, rows in self._reader_rows(ctx, items).items():
            with self.subTest(role=role, surface=surface):
                self.assertEqual({row["id"] for row in rows}, {item["id"] for item in items})
                for row in rows:
                    self._assert_display(row, row["source"], can_decide=can_decide)
        for item, original in zip(items, stored):
            record = self.project.insights.read(item["id"])
            self.assertEqual(record, original)
            self.assertEqual(insight_metadata(record)["addressee"], "writer")
            self.assertEqual(record["source"], item["source"])
            self.assertNotIn("attention", record)
            self.assertNotIn("decide", record)

    def test_worker_can_decide_all_submitters_on_every_surface(self):
        self._assert_reader("worker", can_decide=True)

    def test_reporter_sees_attention_without_decision_mark_on_every_surface(self):
        self._assert_reader("reporter", can_decide=False)

    def test_explicit_addressee_value_is_preserved_as_attention(self):
        item = self._submit(addressee="operator")
        for role in ("worker", "reporter"):
            for surface, rows in self._reader_rows(self.context(role), [item]).items():
                with self.subTest(role=role, surface=surface):
                    self.assertEqual(len(rows), 1)
                    self._assert_display(rows[0], "operator", can_decide=role == "worker")
        self.assertEqual(self.project.insights.read(item["id"])["addressee"], "operator")

    def test_decided_rows_have_no_decision_mark(self):
        ctx = self.context("worker")
        for action in ("accept", "modify", "defer", "reject"):
            with self.subTest(action=action):
                item = self._submit(title=action)
                self.project.insights.decide(
                    item["id"], action, "Reviewed", "New evidence", ctx.run_dir.name,
                    expected_rev=item["rev"])
                rows = ctx.handle("insights", {"action": action})["insights"]
                self.assertEqual(len(rows), 1)
                by_id = ctx.handle("insights", {"id": item["id"]})
                for row in (rows[0], by_id):
                    self._assert_display(row, "operator", can_decide=False)
                    self.assertEqual(row["decision"]["action"], action)
        # Deferred insights remain in the pending inventory but are decided.
        for surface, rows in self._reader_rows(ctx, []).items():
            if surface == "by-id":
                continue
            with self.subTest(surface=surface):
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["decision"]["action"], "defer")
                self._assert_display(rows[0], "operator", can_decide=False)

    def test_revision_reopens_decision_mark_on_every_surface(self):
        item = self._submit()
        self.project.insights.decide(
            item["id"], "reject", "Reviewed", "", "test", expected_rev=item["rev"])
        revised = self.project.insights.revise(
            item["id"], source="operator", title="New evidence", body="Revised body",
            base_snapshot=item["base_snapshot"], run="test", expected_rev=item["rev"])
        for surface, rows in self._reader_rows(self.context("worker"), [revised]).items():
            with self.subTest(surface=surface):
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["rev"], 2)
                self.assertIsNone(rows[0]["decision"])
                self._assert_display(rows[0], "operator", can_decide=True)

    def _assert_event_projection(self, event):
        original = deepcopy(event)
        for role in ("worker", "reporter"):
            ctx = self.context(role)
            ctx.decision_events = [event]
            for prompt in (prompt_for(ctx), prompt_delta_for(ctx, [])):
                row = json.loads(prompt)["decision_events"][0]
                target = event["target"]
                self._assert_display(row, target, can_decide=role == "worker")
                # Only the displayed routing/decision fields change; all other
                # row content and the ack binding stay exact.
                expected = {k: v for k, v in event.items() if k != "addressee"}
                expected["attention"] = target
                if role == "worker":
                    expected["decide"] = "you"
                self.assertEqual(row, expected)
                self.assertEqual(row["event"], event["event"])
                self.assertEqual(row["ack_token"], event["ack_token"])
            self.assertEqual(ctx.decision_events[0], original)
        self.assertEqual(event, original)

    def test_real_delivered_event_is_projected_in_the_prompt(self):
        item = self._submit(source="worker", addressee="writer")
        captured = {}

        def driver(ctx, prompt, *_):
            captured["rows"] = json.loads(prompt)["decision_events"]
            ctx.handle("finish", {"outcome": "wait", "summary": "S", "state": "N"})

        Engine(self.config, driver=ScriptDriver(driver)).run(self.project, "worker")
        rows = [row for row in captured["rows"] if row.get("kind") == "insight.pending"]
        self.assertEqual(len(rows), 1)
        self._assert_display(rows[0], "worker", can_decide=True)
        self.assertEqual(rows[0]["insight"], item["id"])
        self.assertTrue(rows[0]["ack_token"])

    def test_decision_event_alias_resolves_in_full_and_delta_prompts(self):
        for source in ("operator", "searcher", "worker"):
            with self.subTest(source=source):
                self._assert_event_projection(_pending_event(source=source))

    def test_decision_event_explicit_addressee_becomes_attention(self):
        self._assert_event_projection(_pending_event(addressee="reviewer"))

    def test_non_insight_decision_event_passes_through(self):
        ctx = self.context("worker")
        event = {"kind": "decision", "action": "defer", "ack_token": "1" * 64}
        ctx.decision_events = [event]
        for prompt in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            self.assertEqual(json.loads(prompt)["decision_events"][0], event)


if __name__ == "__main__":
    import unittest
    unittest.main()
