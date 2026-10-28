"""Blocked-work diagnostics are bounded observations, never ledger mutations."""
import json
import os
from unittest.mock import patch

from support import Fixture
from mizu.errors import Denied
from mizu.fs import PREVIEW_BYTES, canonical, read_json, write_json
from mizu.runtime import prompt_for

FUTURE = {"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"}
PAST = {"kind": "deadline", "at": "2000-01-01T00:00:00+00:00"}


class BlockedWorkStatusTests(Fixture):
    def block(self, key, wait=FUTURE, actor="worker"):
        store = self.project.work_items
        item = store.create(idempotency_key=key, title=key, actor=actor)["record"]["work_item"]
        return store.transition(item["id"], expected_rev=1, status="blocked", actor=actor, wait=wait)

    def target(self):
        return self.project.insights.submit(source="worker", title="Review target",
                                            body="Observable decision target", base_snapshot=None)

    def view(self):
        return self.project.work_items.blocked_liveness_readonly()

    def test_legacy_absence_is_actionable_but_new_writes_require_wait(self):
        store = self.project.work_items
        blocked = self.block("legacy")
        blocked["wait"] = None
        path = self.project.root / "work-items" / blocked["id"] / "2.json"
        write_json(path, blocked)  # Historical record, not a new transition.
        before = path.read_bytes()
        row = self.view()["items"][0]
        self.assertEqual((row["state"], row["reason"]), ("absent", "no_wait"))
        self.assertEqual(store.page_ready(role_name="worker")["total"], 1)
        self.assertEqual(store.page_ready(role_name="reviewer")["total"], 0)
        ready = json.loads(prompt_for(self.context()))["ready_work"]["items"][0]
        self.assertFalse(ready["wait_satisfied"])
        self.assertEqual(ready["wait_liveness"]["state"], "absent")
        with self.assertRaisesRegex(Denied, "observable wait"):
            store.transition(blocked["id"], expected_rev=2, status="blocked", actor="worker",
                             scope="Clarification", reason="Keep legacy block")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(store.get(blocked["id"]), blocked)
        store.transition(blocked["id"], expected_rev=2, status="open", actor="worker")
        self.assertEqual(store.get(blocked["id"])["rev"], 3)  # Chain replay survives.

    def test_current_insight_state_pending_due_superseded_and_withdrawn(self):
        target = self.target()
        blocked = self.block("decision", {"kind": "insight_decided", "insight": target["id"]})
        def check(state, ready, reason=None):
            row = self.view()["items"][0]
            self.assertEqual(row["state"], state)
            if reason:
                self.assertEqual(row["reason"], reason)
            self.assertEqual(self.project.work_items.page_ready()["total"], ready)
            self.assertEqual(self.project.work_items.get(blocked["id"]), blocked)
        check("pending", 0)
        self.project.insights.decide(target["id"], "defer", "Need review", "Review later", "run", expected_rev=1)
        check("pending", 0)
        self.project.insights.decide(target["id"], "reject", "Not suitable", "", "run", expected_rev=1)
        check("due", 1)
        self.project.insights.revise(target["id"], source="worker", expected_rev=1,
                                     title="Revised target", body="New evidence", base_snapshot=None)
        check("pending", 0, "superseded_decision")
        self.project.insights.withdraw(target["id"], source="worker", expected_rev=2, reason="Not pursuing")
        check("impossible", 1, "target_withdrawn")
        ready = json.loads(prompt_for(self.context()))["ready_work"]["items"][0]
        self.assertFalse(ready["wait_satisfied"])

    def test_missing_target_deadline_and_code_change_are_readonly(self):
        self.block("missing", {"kind": "insight_decided", "insight": "f" * 32})
        self.block("future")
        self.block("past", PAST)
        self.block("code", {"kind": "code_change"})
        before = {p.relative_to(self.project.root): p.read_bytes()
                  for p in self.project.root.rglob("*") if p.is_file()}
        with patch("mizu.work_items.lock", side_effect=AssertionError("read-only")):
            view = self.view()
        self.assertEqual(view["counts"], {"absent": 0, "pending": 2, "due": 1,
                                           "impossible": 1, "receipt_bound": 0, "unknown": 0})
        self.assertTrue(view["coverage_complete"])
        after = {p.relative_to(self.project.root): p.read_bytes()
                 for p in self.project.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.project.work_items.page_ready()["total"], 2)

    def test_projection_rows_bytes_and_unseeded_coverage(self):
        for key in range(3):
            self.block(str(key))
        store = self.project.work_items
        view = store.blocked_liveness_readonly(limit=2)
        self.assertEqual((len(view["items"]), view["total"], view["overflow_count"]), (2, 3, 1))
        self.assertTrue(view["truncated"])
        self.assertTrue(view["coverage_complete"])  # Counts cover omitted rows.
        self.assertLess(len(canonical(view)), 8192)
        self.assertEqual(self.project.status()["blocked_work"]["total"], 3)
        with patch("mizu.work_items.MAX_OPEN_PAGE_BYTES", 1):
            self.assertIsNone(self.view()["total"])
            self.assertFalse(self.view()["coverage_complete"])
        write_json(self.project.root / "decision-history" / ("b" * 32 + ".json"), {"action": "accept"})
        view = self.view()
        self.assertFalse(view["coverage_complete"])
        self.assertEqual(view["unseeded_decisions"], 1)
        self.assertIsNone(view["total"])
        self.assertIsNone(view["counts"])
        for limit in (0, 101, True):
            with self.assertRaises(Denied):
                store.blocked_liveness_readonly(limit=limit)

    def test_malformed_oversized_or_unavailable_target_never_reports_false_zero(self):
        target = self.target()
        self.block("unsafe-target", {"kind": "insight_decided", "insight": target["id"]})
        path = self.project.root / "inbox" / f"{target['id']}.json"
        original = path.read_bytes()
        for bad in (b"[]", b"{bad", b" " * (PREVIEW_BYTES + 1)):
            path.write_bytes(bad)
            view = self.view()
            self.assertFalse(view["coverage_complete"])
            self.assertIsNone(view["counts"])
            self.assertEqual(view["items"][0]["state"], "unknown")
            page = self.project.work_items.page_ready()
            self.assertEqual(page["items"], [])
            self.assertEqual(page["total"], 0)
            self.assertFalse(page["coverage_complete"])
            self.assertEqual(page["unknown_blocked"], 1)
            ready = json.loads(prompt_for(self.context()))["ready_work"]
            self.assertEqual(ready["total"], 0)
            self.assertFalse(ready["coverage_complete"])
            self.assertEqual(ready["unknown_blocked"], 1)
        path.write_bytes(original)
        path.unlink()
        with patch("mizu.insight_archive.read_immutable", side_effect=Denied("incomplete archive")):
            self.assertEqual(self.view()["items"][0]["state"], "unknown")
        write_json(self.project.root / "insight-ids" / path.name, {"id": target["id"]})
        self.assertEqual(self.view()["items"][0]["state"], "unknown")

    def test_unsafe_links_are_unknown_not_missing(self):
        target = self.target()
        self.block("linked", {"kind": "insight_decided", "insight": target["id"]})
        path = self.project.root / "inbox" / f"{target['id']}.json"
        other = path.with_suffix(".saved")
        path.rename(other)
        try:
            os.link(other, path)
        except OSError:
            self.skipTest("hardlinks unavailable")
        self.assertEqual(self.view()["items"][0]["state"], "unknown")
        path.unlink()
        try:
            path.symlink_to(other.name)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.assertEqual(self.view()["items"][0]["state"], "unknown")
        self.assertIsNone(self.view()["counts"])

    def test_archived_target_and_dependency_keep_current_revision_authority(self):
        with patch("mizu.insights.now", return_value="2000-01-01T00:00:00+00:00"):
            target = self.target()
            self.project.insights.decide(target["id"], "reject", "Not suitable", "", "run", expected_rev=1)
        self.assertEqual(self.project.insights.gc_decided(keep_days=1), 1)
        self.assertFalse((self.project.root / "inbox" / f"{target['id']}.json").exists())
        self.block("archived", {"kind": "insight_decided", "insight": target["id"]})
        self.block("dependency", {"kind": "dependency", "recipient": "reviewer",
                                   "requires": {"insight": target["id"], "rev": 1, "action": "reject"}})
        with patch("mizu.insight_archive.read", side_effect=AssertionError("mutable read")), \
                patch("mizu.insight_archive._connect", side_effect=AssertionError("mutable connection")):
            self.assertEqual(self.view()["counts"]["due"], 2)
            self.assertEqual(self.project.work_items.page_ready()["total"], 2)
            # Any archive sidecar fails closed without recovery or deletion.
            for suffix in ("-journal", "-wal", "-shm"):
                with self.subTest(sidecar=suffix):
                    sidecar = self.project.root / ("insight-archive.sqlite3" + suffix)
                    sidecar.write_bytes(b"pending")
                    view = self.view()
                    self.assertFalse(view["coverage_complete"])
                    self.assertTrue(all(row["state"] == "unknown" for row in view["items"]))
                    self.assertEqual(sidecar.read_bytes(), b"pending")
                    sidecar.unlink()

    def dependency(self, target, rev=1, action="accept"):
        return self.block("dependency", {"kind": "dependency", "recipient": "reviewer",
                                        "requires": {"insight": target, "rev": rev, "action": action}})

    def check_dependency(self, blocked, state, reason):
        row = self.view()["items"][0]
        self.assertEqual((row["state"], row["reason"]), (state, reason))
        ready = self.project.work_items.page_ready()
        actionable = state in ("due", "impossible")
        self.assertEqual(ready["total"], int(actionable))
        if actionable:
            self.assertEqual(ready["items"][0]["wait_liveness"]["state"], state)
        self.assertEqual(self.project.work_items.get(blocked["id"]), blocked)

    def test_dependency_missing_target_is_impossible(self):
        blocked = self.dependency("f" * 32)
        self.check_dependency(blocked, "impossible", "target_missing")

    def test_dependency_withdrawn_target_is_impossible(self):
        target = self.target()
        blocked = self.dependency(target["id"])
        self.project.insights.withdraw(target["id"], source="worker", expected_rev=1, reason="Withdrawn")
        self.check_dependency(blocked, "impossible", "target_withdrawn")

    def test_dependency_superseded_revision_is_impossible_even_without_current_decision(self):
        target = self.target()
        blocked = self.dependency(target["id"])
        self.project.insights.revise(target["id"], source="worker", expected_rev=1,
                                     title="Revised", body="New evidence", base_snapshot=None)
        self.check_dependency(blocked, "impossible", "target_superseded")
        self.project.insights.decide(target["id"], "accept", "Suitable", "", "run", expected_rev=2,
                                     work_item_intent="none")
        self.check_dependency(blocked, "impossible", "target_superseded")

    def test_dependency_superseded_matching_decision_is_not_due(self):
        target = self.target()
        blocked = self.dependency(target["id"], action="reject")
        self.project.insights.decide(target["id"], "reject", "Not suitable", "", "run", expected_rev=1)
        self.check_dependency(blocked, "due", "condition_met")
        self.project.insights.revise(target["id"], source="worker", expected_rev=1,
                                     title="Revised", body="New evidence", base_snapshot=None)
        self.check_dependency(blocked, "impossible", "target_superseded")

    def test_dependency_conflicting_substantive_decisions_are_impossible(self):
        for action in ("accept", "modify", "reject"):
            with self.subTest(action=action):
                target = self.target()
                blocked = self.block(action, {"kind": "dependency", "recipient": "reviewer",
                    "requires": {"insight": target["id"], "rev": 1,
                                 "action": "reject" if action != "reject" else "accept"}})
                self.project.insights.decide(target["id"], action, "Assessed", "", "run", expected_rev=1,
                                             work_item_intent="none" if action != "reject" else None)
                self.check_dependency(blocked, "impossible", "decision_conflict")
                self.project.work_items.transition(blocked["id"], expected_rev=2, status="dropped",
                                                   actor="worker", reason="Case complete")

    def test_dependency_undecided_and_deferred_are_pending_then_authoritatively_due(self):
        target = self.target()
        blocked = self.dependency(target["id"], action="reject")
        self.check_dependency(blocked, "pending", "undecided")
        self.project.insights.decide(target["id"], "defer", "Need review", "Later", "run", expected_rev=1)
        self.check_dependency(blocked, "pending", "condition_pending")
        self.project.insights.decide(target["id"], "reject", "Not suitable", "", "run", expected_rev=1)
        with patch("mizu.insights.satisfaction_evidence", return_value=None) as evidence:
            self.check_dependency(blocked, "pending", "condition_pending")
            evidence.assert_called_with(blocked["wait"], target,
                read_json(self.project.root / "decisions" / f"{target['id']}.json"))
        self.check_dependency(blocked, "due", "condition_met")

    def test_dependency_future_revision_pending_despite_older_decisions(self):
        target = self.target()
        blocked = self.dependency(target["id"], rev=3, action="reject")
        self.check_dependency(blocked, "pending", "future_revision")
        self.project.insights.decide(target["id"], "accept", "Suitable", "", "run", expected_rev=1,
                                     work_item_intent="none")
        self.check_dependency(blocked, "pending", "future_revision")
        for rev in (2, 3):
            self.project.insights.revise(target["id"], source="worker", expected_rev=rev - 1,
                                         title=f"Revised {rev}", body=f"New evidence {rev}", base_snapshot=None)
            self.check_dependency(blocked, "pending", "future_revision" if rev == 2 else "superseded_decision")
        self.project.insights.decide(target["id"], "reject", "Not suitable", "", "run", expected_rev=3)
        self.check_dependency(blocked, "due", "condition_met")

    def test_unknown_blocked_does_not_suppress_known_ready_work(self):
        from mizu.runtime import _has_ready_work, should_run
        target = self.target()
        blocked = self.block("unknown-blocked", {"kind": "insight_decided", "insight": target["id"]})
        opened = self.project.work_items.create(
            idempotency_key="known-open", title="Known open", actor="worker")["record"]["work_item"]
        path = self.project.root / "inbox" / f"{target['id']}.json"
        path.write_bytes(b"[]")
        self.assertEqual(self.view()["items"][0]["state"], "unknown")
        page = self.project.work_items.page_ready()
        self.assertIn(opened["id"], [row["id"] for row in page["items"]])
        self.assertNotIn(blocked["id"], [row["id"] for row in page["items"]])
        self.assertEqual(page["total"], 1)
        self.assertFalse(page["coverage_complete"])
        self.assertEqual(page["unknown_blocked"], 1)
        ready = json.loads(prompt_for(self.context()))["ready_work"]
        self.assertIn(opened["id"], [row["id"] for row in ready["items"]])
        self.assertNotIn(blocked["id"], [row["id"] for row in ready["items"]])
        self.assertEqual(ready["total"], 1)
        self.assertFalse(ready["coverage_complete"])
        self.assertEqual(ready["unknown_blocked"], 1)
        self.assertTrue(_has_ready_work(self.project))
        parked = self.project.snapshots.get()
        self.assertTrue(should_run(self.project, parked, role_name="worker"))

    def test_only_unknown_reports_zero_without_claiming_coverage(self):
        target = self.target()
        self.block("solo-unknown", {"kind": "insight_decided", "insight": target["id"]})
        path = self.project.root / "inbox" / f"{target['id']}.json"
        path.write_bytes(b"[]")
        page = self.project.work_items.page_ready()
        self.assertEqual(page["items"], [])
        self.assertEqual(page["total"], 0)
        self.assertFalse(page["coverage_complete"])
        self.assertEqual(page["unknown_blocked"], 1)
        ready = json.loads(prompt_for(self.context()))["ready_work"]
        self.assertEqual(ready["items"], [])
        self.assertEqual(ready["total"], 0)
        self.assertFalse(ready["coverage_complete"])
        self.assertEqual(ready["unknown_blocked"], 1)

    def test_incomplete_ledger_invalidates_whole_projection_without_repair(self):
        blocked = self.block("broken-chain")
        path = self.project.root / "work-items" / blocked["id"] / "2.json"
        record = read_json(path)
        record["rev"] = 4
        write_json(path, record)
        view = self.view()
        self.assertEqual(view["diagnostic"], "unreadable")
        self.assertIsNone(view["total"])
        self.assertIsNone(view["truncated"])
        self.assertFalse(view["coverage_complete"])
        self.assertEqual(read_json(path), record)
