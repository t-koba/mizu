"""Attached evidence: annotate another insight without moving ownership."""
import dataclasses
import unittest
from support import Fixture, ScriptDriver
from mizu.errors import ConfigError, Denied


def _deferred(project, source="searcher", title="Finding", body="v1"):
    item = project.insights.submit(source=source, title=title, body=body,
                                   base_snapshot=project.snapshots.get()["id"])
    project.insights.decide(item["id"], "defer", "needs field data", "observe X",
                             "test", expected_rev=1)
    return item


class AttachEvidenceTests(Fixture):
    def test_attach_advances_generation_and_lists_count(self):
        item = _deferred(self.project)
        before = self.project.insights.generation()
        record = self.project.insights.attach_evidence(
            item["id"], source="resolver", text="CI green at abc123",
            run="run-1", expected_rev=1)
        self.assertEqual(len(record["evidence"]), 1)
        self.assertEqual(record["evidence"][0]["author"], "resolver")
        self.assertEqual(record["evidence"][0]["text"], "CI green at abc123")
        self.assertNotEqual(self.project.insights.generation(), before)
        pending = [i for i in self.project.insights.list() if i["id"] == item["id"]]
        self.assertEqual(pending[0]["evidence_count"], 1)
        self.assertEqual(self.project.insights.read(item["id"])["evidence"][0]["run"], "run-1")

    def test_attach_binds_rev_and_validates(self):
        item = _deferred(self.project)
        with self.assertRaises(Denied):
            self.project.insights.attach_evidence(
                item["id"], source="resolver", text="stale", expected_rev=2)
        with self.assertRaises(Denied):
            self.project.insights.attach_evidence(
                "missing", source="resolver", text="x", expected_rev=1)
        with self.assertRaises(Denied):
            self.project.insights.attach_evidence(
                item["id"], source="resolver", text="   ", expected_rev=1)
        with self.assertRaises(Denied):
            self.project.insights.attach_evidence(
                item["id"], source="resolver", text="x" * 8193, expected_rev=1)
        with self.assertRaises(Denied):
            self.project.insights.attach_evidence(
                item["id"], source="resolver", text="x")

    def test_attach_refused_on_decided_and_withdrawn(self):
        item = _deferred(self.project)
        self.project.insights.decide(item["id"], "accept", "read v1", "", "test", expected_rev=1)
        with self.assertRaises(Denied):
            self.project.insights.attach_evidence(
                item["id"], source="resolver", text="late", expected_rev=1)
        gone = _deferred(self.project, title="Gone")
        self.project.insights.withdraw(gone["id"], source="searcher", reason="retracted")
        with self.assertRaises(Denied):
            self.project.insights.attach_evidence(
                gone["id"], source="resolver", text="late", expected_rev=1)

    def test_evidence_list_is_bounded(self):
        from mizu.insights import MAX_EVIDENCE_ENTRIES
        item = _deferred(self.project)
        for n in range(MAX_EVIDENCE_ENTRIES):
            self.project.insights.attach_evidence(
                item["id"], source="resolver", text=f"note {n}", expected_rev=1)
        with self.assertRaises(Denied):
            self.project.insights.attach_evidence(
                item["id"], source="resolver", text="one too many", expected_rev=1)

    def test_revise_archives_evidence_and_resets(self):
        item = _deferred(self.project)
        self.project.insights.attach_evidence(
            item["id"], source="resolver", text="for v1", expected_rev=1)
        self.project.insights.revise(item["id"], source="searcher", title="Finding",
                                     body="v2 with field data", base_snapshot=None)
        self.assertEqual(self.project.insights.read(item["id"])["evidence"], [])
        archived = self.project.insights.history(item["id"])
        self.assertEqual(len(archived[0]["evidence"]), 1)

    def test_decision_event_carries_evidence(self):
        item = _deferred(self.project)
        self.project.insights.attach_evidence(
            item["id"], source="resolver", text="CI green at abc123", expected_rev=1)
        self.project.insights.decide(item["id"], "accept", "evidence shows green", "",
                                     "test", expected_rev=1)
        events = self.project.insights.decision_events("searcher", ("accept",))
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["evidence"]), 1)
        self.assertEqual(events[0]["evidence"][0]["text"], "CI green at abc123")

    def test_runtime_capability_gate(self):
        item = _deferred(self.project)
        ctx = self.context("reviewer")
        with self.assertRaises(Denied):
            ctx.handle("attach_evidence", {"id": item["id"], "rev": 1, "text": "no grant"})
        roles = dict(self.config.roles)
        caps = tuple(self.config.roles["searcher"].capabilities) + ("attach_evidence",)
        roles["searcher"] = dataclasses.replace(self.config.roles["searcher"], capabilities=caps)
        self.config = dataclasses.replace(self.config, roles=roles)
        record = self.context("searcher").handle(
            "attach_evidence", {"id": item["id"], "rev": 1, "text": "granted"})
        self.assertEqual(record["evidence"][-1]["author"], "searcher")

    def test_consultation_role_cannot_hold_attach(self):
        from mizu.runtime import check_consult_role
        base = self.config.roles["consult"]
        role = dataclasses.replace(base, capabilities=tuple(base.capabilities) + ("attach_evidence",))
        with self.assertRaises(ConfigError):
            check_consult_role("consult", role)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
