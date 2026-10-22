"""Regressions for role-cursor diff basing: diff starts where the role left off.

The invoking role's ``observed/<role>.json`` cursor names the base when it is
still retained and ancestral; otherwise diff keeps the previous-distinct-code
fallback. A cursor naming the anchor itself renders an empty diff so
state-only ranges are preserved, never skipped for an older digest.
"""
from support import Fixture, ScriptDriver
from mizu.errors import Denied
from mizu.fs import write_json
from mizu.runtime import Engine


class DiffBaseTests(Fixture):
    def _publish(self, body: bytes):
        (self.project.workspace / "app.py").write_bytes(body)
        Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        return self.project.snapshots.get()

    def _three(self):
        first = self.project.snapshots.get()
        second = self._publish(b"VALUE = 5\n")
        third = self._publish(b"VALUE = 7\n")
        return first, second, third

    def _observe(self, role, snapshot):
        write_json(self.project.root / "observed" / f"{role}.json",
                   {"snapshot": snapshot["id"], "code_digest": snapshot["code_digest"]})

    def test_explicit_base_spans_intermediate_snapshot(self):
        first, second, third = self._three()
        result = self.project.snapshots.changes(third["id"], base=first["id"])
        self.assertEqual(result["base_snapshot"], first["id"])
        self.assertEqual(result["target_snapshot"], third["id"])
        self.assertIn("-VALUE = 2", result["diff"])
        self.assertIn("+VALUE = 7", result["diff"])
        fallback = self.project.snapshots.changes(third["id"])
        self.assertEqual(fallback["base_snapshot"], second["id"])

    def test_dispatch_uses_role_observed_cursor(self):
        first, _second, third = self._three()
        self._observe("reviewer", first)
        result = self.context("reviewer").handle("diff", {})
        self.assertEqual(result["base_snapshot"], first["id"])
        self.assertEqual(result["target_snapshot"], third["id"])
        self.assertIn("-VALUE = 2", result["diff"])
        self.assertIn("+VALUE = 7", result["diff"])
        # A cursor naming the anchor renders an empty diff, not an older base.
        self._observe("reviewer", third)
        current = self.context("reviewer").handle("diff", {})
        self.assertEqual(current["base_snapshot"], third["id"])
        self.assertEqual(current["target_snapshot"], third["id"])
        self.assertEqual(current["diff"], "")

    def test_cursor_newer_than_context_snapshot_falls_back(self):
        first = self.project.snapshots.get()
        second = self._publish(b"VALUE = 5\n")
        stale = self.context("reviewer")
        third = self._publish(b"VALUE = 7\n")
        self._observe("reviewer", third)
        result = stale.handle("diff", {})
        self.assertEqual(result["target_snapshot"], second["id"])
        self.assertEqual(result["base_snapshot"], first["id"])

    def test_missing_or_invalid_cursor_keeps_fallback(self):
        first, second, third = self._three()
        observed = self.project.root / "observed" / "reviewer.json"
        # Absent cursor keeps the previous-distinct fallback.
        self.assertEqual(self.context("reviewer").handle("diff", {})["base_snapshot"],
                         second["id"])
        # Malformed, digest-mismatched, unknown and oversized cursors agree.
        observed.write_text("{not json")
        self.assertEqual(self.context("reviewer").handle("diff", {})["base_snapshot"],
                         second["id"])
        write_json(observed, {"snapshot": first["id"], "code_digest": second["code_digest"]})
        self.assertEqual(self.context("reviewer").handle("diff", {})["base_snapshot"],
                         second["id"])
        write_json(observed, {"snapshot": "f" * 64, "code_digest": "f" * 64})
        self.assertEqual(self.context("reviewer").handle("diff", {})["base_snapshot"],
                         second["id"])
        observed.write_bytes(b"x" * 5000)
        self.assertEqual(self.context("reviewer").handle("diff", {})["base_snapshot"],
                         second["id"])
        # The store itself refuses an unretained explicit base.
        with self.assertRaises(Denied):
            self.project.snapshots.changes(third["id"], base="f" * 64)
