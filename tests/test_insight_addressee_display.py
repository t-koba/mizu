"""Display-only resolution of the internal writer alias in model-facing rows."""
import json

from support import Fixture
from mizu.insights import insight_metadata
from mizu.unit_prompts import prompt_delta_for, prompt_for


class InsightAddresseeDisplayTests(Fixture):
    def _submit(self, source="operator", **metadata):
        return self.project.insights.submit(
            source=source, title="Editorial", body="Body",
            base_snapshot=self.project.snapshots.get()["id"], **metadata)

    def test_full_prompt_resolves_writer_alias_to_reader(self):
        item = self._submit()
        ctx = self.context("worker")
        decoded = json.loads(prompt_for(ctx))
        row = next(r for r in decoded["pending_insights"] if r["id"] == item["id"])
        self.assertEqual(row["addressee"], "worker")
        # Storage keeps the alias; only the displayed row is resolved.
        stored = self.project.insights.read(item["id"])
        self.assertEqual(insight_metadata(stored)["addressee"], "writer")

    def test_delta_prompt_resolves_writer_alias_to_reader(self):
        item = self._submit()
        ctx = self.context("worker")
        pending = self.project.insights.list(limit=30)
        decoded = json.loads(prompt_delta_for(ctx, pending))
        row = next(r for r in decoded["pending_insights"] if r["id"] == item["id"])
        self.assertEqual(row["addressee"], "worker")

    def test_tool_by_id_and_list_resolve_alias(self):
        item = self._submit()
        ctx = self.context("worker")
        self.assertEqual(ctx.handle("insights", {"id": item["id"]})["addressee"], "worker")
        listed = ctx.handle("insights", {"limit": 1000})["insights"]
        self.assertEqual(next(r["addressee"] for r in listed if r["id"] == item["id"]), "worker")
        self.assertEqual(insight_metadata(self.project.insights.read(item["id"]))["addressee"], "writer")

    def test_explicit_addressee_is_left_untouched(self):
        item = self._submit(addressee="operator")
        ctx = self.context("worker")
        self.assertEqual(ctx.handle("insights", {"id": item["id"]})["addressee"], "operator")
        row = next(r for r in json.loads(prompt_for(ctx))["pending_insights"] if r["id"] == item["id"])
        self.assertEqual(row["addressee"], "operator")

    def test_alias_resolves_for_an_insights_reader_that_is_not_the_worker(self):
        item = self._submit()
        ctx = self.context("reporter")
        self.assertEqual(ctx.handle("insights", {"id": item["id"]})["addressee"], "reporter")
        row = next(r for r in json.loads(prompt_for(ctx))["pending_insights"] if r["id"] == item["id"])
        self.assertEqual(row["addressee"], "reporter")


if __name__ == "__main__":
    import unittest
    unittest.main()
