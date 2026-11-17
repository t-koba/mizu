"""Display-only resolution of the internal writer alias in model-facing rows."""
import json

from support import Fixture
from mizu.insights import insight_metadata
from mizu.unit_prompts import prompt_delta_for, prompt_for


def _pending_event(source="operator", addressee="writer"):
    """The payload shape delivered by decision_delivery for an insight."""
    return {
        "schema": 1,
        "id": "insight.pending/abc123",
        "kind": "insight.pending",
        "created_at": "2026-11-17T00:00:00+00:00",
        "writer": "host.insights",
        "subject": {"event": "abc123", "insight": "i1", "rev": 1},
        "payload": {
            "title": "Editorial", "body": "Body", "source": source,
            "created_at": "2026-11-17T00:00:00+00:00",
            "insight": "i1", "rev": 1, "kind": "insight.pending",
            "target": source if addressee == "writer" else addressee,
            "addressee": addressee,
        },
        "ack_token": "0" * 64,
    }


class InsightAddresseeDisplayTests(Fixture):
    def _submit(self, source="operator", **metadata):
        return self.project.insights.submit(
            source=source, title="Editorial", body="Body",
            base_snapshot=self.project.snapshots.get()["id"], **metadata)

    def test_full_prompt_resolves_writer_alias_to_submitter(self):
        item = self._submit()
        ctx = self.context("worker")
        decoded = json.loads(prompt_for(ctx))
        row = next(r for r in decoded["pending_insights"] if r["id"] == item["id"])
        self.assertEqual(row["addressee"], "operator")
        # Storage keeps the alias; only the displayed row is resolved.
        stored = self.project.insights.read(item["id"])
        self.assertEqual(insight_metadata(stored)["addressee"], "writer")
        self.assertEqual(stored["source"], "operator")

    def test_delta_prompt_resolves_writer_alias_to_submitter(self):
        item = self._submit()
        ctx = self.context("worker")
        pending = self.project.insights.list(limit=30)
        decoded = json.loads(prompt_delta_for(ctx, pending))
        row = next(r for r in decoded["pending_insights"] if r["id"] == item["id"])
        self.assertEqual(row["addressee"], "operator")

    def test_tool_by_id_and_list_resolve_alias(self):
        item = self._submit()
        ctx = self.context("worker")
        self.assertEqual(ctx.handle("insights", {"id": item["id"]})["addressee"], "operator")
        listed = ctx.handle("insights", {"limit": 1000})["insights"]
        self.assertEqual(next(r["addressee"] for r in listed if r["id"] == item["id"]), "operator")
        self.assertEqual(insight_metadata(self.project.insights.read(item["id"]))["addressee"], "writer")

    def test_explicit_addressee_is_left_untouched(self):
        item = self._submit(addressee="operator")
        ctx = self.context("worker")
        self.assertEqual(ctx.handle("insights", {"id": item["id"]})["addressee"], "operator")
        row = next(r for r in json.loads(prompt_for(ctx))["pending_insights"] if r["id"] == item["id"])
        self.assertEqual(row["addressee"], "operator")

    def test_alias_is_the_submitter_for_every_reader(self):
        item = self._submit()
        for role in ("worker", "reporter"):
            ctx = self.context(role)
            by_id = ctx.handle("insights", {"id": item["id"]})
            self.assertEqual(by_id["addressee"], "operator")
            row = next(r for r in json.loads(prompt_for(ctx))["pending_insights"]
                       if r["id"] == item["id"])
            self.assertEqual(row["addressee"], "operator")

    def test_decision_event_alias_resolves_in_full_and_delta_prompts(self):
        ctx = self.context("worker")
        event = _pending_event()
        ctx.decision_events = [event]
        for prompt in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            row = json.loads(prompt)["decision_events"][0]
            self.assertEqual(row["payload"]["addressee"], "operator")
            self.assertEqual(row["payload"]["source"], "operator")
            # Identity and acknowledgement binding are byte-identical.
            self.assertEqual(row["id"], event["id"])
            self.assertEqual(row["subject"], event["subject"])
            self.assertEqual(row["ack_token"], event["ack_token"])
        self.assertEqual(ctx.decision_events[0]["payload"]["addressee"], "writer")

    def test_decision_event_with_explicit_addressee_is_untouched(self):
        ctx = self.context("worker")
        event = _pending_event(addressee="reviewer")
        ctx.decision_events = [event]
        for prompt in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            self.assertEqual(json.loads(prompt)["decision_events"][0], event)

    def test_non_insight_decision_event_passes_through(self):
        ctx = self.context("worker")
        event = {"kind": "decision", "action": "defer", "ack_token": "1" * 64}
        ctx.decision_events = [event]
        for prompt in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            self.assertEqual(json.loads(prompt)["decision_events"][0], event)


if __name__ == "__main__":
    import unittest
    unittest.main()
