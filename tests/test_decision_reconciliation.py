"""Host-only exact receipt mapping under a pinned quiescent boundary."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mizu.admission import role_exclusion
from mizu.decision_reconciliation import preflight_decision_receipts, apply_decision_receipts
from mizu.errors import Busy, Denied, LimitExceeded
from mizu.fs import MAX_FRAME, canonical, digest, held, lock, read_json, write_json
from mizu.insights import Insights, INSIGHT_DECISION_POLICY, CorruptDecisionReceipt
from mizu.shared_records import SharedRecords


class DecisionReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.insights = Insights(self.root)
        self.routes = {"reviewer": {"actions": ["reject", "defer"], "sources": "own"},
                       "worker": {"actions": ["reject", "defer"], "sources": "any"},
                       "editor": {"actions": [], "sources": "any"}}
        self.store = SharedRecords(self.root, {"insight.decision": INSIGHT_DECISION_POLICY})

    def decide(self, index=0, *, action="reject", source="reviewer"):
        item = self.insights.submit(source=source, title=f"Finding {index}", body=f"Body {index}",
                                    base_snapshot=None, origin="test")
        self.insights.decide(item["id"], action, "Exact reason", "Exact revisit", "test",
                             expected_rev=1)
        return item

    def rows(self, role="reviewer"):
        return self.insights.decision_events(role, ["reject", "defer"], sources="any", limit=100)

    def ack(self, role, rows):
        self.insights.acknowledge_decisions(role, rows, sources="any")

    def preflight(self, routes=None):
        routes = self.routes if routes is None else routes
        return preflight_decision_receipts(self.insights, routes, enabled_roles=list(routes))

    def apply(self, plan, routes=None):
        routes = self.routes if routes is None else routes
        return apply_decision_receipts(self.insights, plan, routes, enabled_roles=list(routes))

    def rid(self, row):
        return f"insight.decision/{row['event']}"

    def legacy_path(self, role, row):
        return self.root / "decision-receipts" / role / f"{row['ack_token']}.json"

    def shared_receipts(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in (self.root / "shared-record-receipts").glob("*/*.json")}

    def legacy_bytes(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in (self.root / "decision-receipts").glob("*/*.json")}

    def test_empty_and_partial_exact_mapping_restart_and_role_isolation(self):
        self.decide(0)
        self.decide(1, source="searcher")
        reviewer, worker = self.rows(), self.rows("worker")
        empty = self.preflight()
        self.assertEqual(empty, self.preflight())
        self.assertFalse((self.root / "shared-records").exists())
        result = self.apply(empty)
        self.assertEqual(self.shared_receipts(), {})
        self.assertEqual(result["roles"]["worker"]["shared_pending_ids"], sorted(map(self.rid, worker)))
        own = next(row for row in reviewer if row["source"] == "reviewer")
        self.ack("reviewer", [own])
        before = self.legacy_bytes()
        plan = self.preflight()
        self.assertEqual(plan["roles"]["reviewer"]["legacy_received_ids"], [self.rid(own)])
        self.assertEqual(plan["roles"]["worker"]["legacy_received_ids"], [])
        plan = json.loads(canonical(plan))  # Retained plan, no live Python identity.
        self.insights = Insights(self.root)
        result = self.apply(plan)
        self.assertEqual(self.legacy_bytes(), before)
        self.assertEqual(len(self.shared_receipts()), 1)
        self.assertEqual(result["roles"]["reviewer"]["shared_pending_ids"], [])
        self.assertEqual(result["roles"]["worker"]["shared_pending_ids"], sorted(map(self.rid, worker)))
        receipt_before = self.shared_receipts()
        self.assertEqual(self.apply(plan), result)
        self.assertEqual(self.shared_receipts(), receipt_before)
        # Legacy remains active: worker still sees both, reviewer sees only the other source via any.
        self.assertEqual(self.rows("worker"), worker)
        self.assertEqual(self.rows(), [row for row in reviewer if row != own])
        for role, route in self.routes.items():
            page, manifest = self.insights.shared_decision_page(
                role, route["actions"], sources=route["sources"], max_bytes=MAX_FRAME, limit=100)
            legacy = self.insights.decision_events(role, route["actions"], sources=route["sources"], limit=100)
            self.assertEqual({entry.record_id for entry in manifest}, set(map(self.rid, legacy)))
            self.assertEqual(page["pending_count"], len(legacy))

    def test_same_event_maps_distinct_roles_and_empty_subscription_writes_nothing(self):
        self.decide()
        for role in ("reviewer", "worker"):
            self.ack(role, self.rows(role))
        plan = self.preflight()
        self.apply(plan)
        self.assertEqual(len(self.shared_receipts()), 2)
        self.assertEqual(plan["roles"]["editor"]["current_ids"], [])
        empty = {role: {"actions": [], "sources": "any"} for role in self.routes}
        before = self.shared_receipts()
        self.apply(self.preflight(empty), empty)
        self.assertEqual(self.shared_receipts(), before)
        self.assertEqual(self.apply(self.preflight({}), {})["roles"], {})

    def test_routes_are_required_complete_closed_unique_and_canonical(self):
        for routes, enabled in [({}, ["worker"]), (self.routes, ["worker"]),
                                ({"worker": {}}, ["worker"]),
                                ({"worker": {"actions": ["reject", "reject"], "sources": "any"}}, ["worker"]),
                                ({"worker": {"actions": ["withdraw"], "sources": "any"}}, ["worker"]),
                                ({"worker": {"actions": [], "sources": "unknown"}}, ["worker"]),
                                ({}, ["worker", "worker"])]:
            with self.subTest(routes=routes), self.assertRaises(Denied):
                preflight_decision_receipts(self.insights, routes, enabled_roles=enabled)
        a = self.preflight()
        routes = dict(reversed(list(self.routes.items())))
        reordered = copy.deepcopy(routes)
        reordered["reviewer"]["actions"].reverse()
        self.assertEqual(a, self.preflight(reordered))

    def test_cursors_wrong_payload_token_and_stale_corrupt_receipts_do_not_map(self):
        self.decide()
        row = self.rows()[0]
        wrong = dict(row, title="Old payload")
        wrong["ack_token"] = self.insights._decision_token("reviewer", wrong)
        self.ack("reviewer", [wrong])
        # Corruption at an unrelated exact-token path is irrelevant.
        self.legacy_path("reviewer", wrong).write_bytes(b"not json")
        write_json(self.root / "decision-cursors" / "reviewer.json", {"at": "2999-01-01T00:00:00+00:00"})
        plan = self.preflight()
        self.assertEqual(plan["roles"]["reviewer"]["legacy_received_ids"], [])
        before = self.legacy_bytes()
        self.apply(plan)
        self.assertEqual(self.shared_receipts(), {})
        self.assertEqual(self.legacy_bytes(), before)

    def test_withdrawn_and_revision_stale_receipts_are_irrelevant(self):
        item = self.decide(action="defer")
        row = self.rows()[0]
        self.ack("reviewer", [row])
        self.legacy_path("reviewer", row).write_bytes(b"corrupt stale receipt")
        self.insights.withdraw(item["id"], source="reviewer", reason="Withdraw", run="test", expected_rev=1)
        self.assertEqual(self.preflight()["boundary"]["sources"], {})
        item = self.decide(1, action="defer")
        row = self.rows()[0]
        self.ack("reviewer", [row])
        self.legacy_path("reviewer", row).write_bytes(b"corrupt stale receipt")
        path = self.root / "inbox" / f"{item['id']}.json"
        write_json(path, dict(read_json(path), rev=2))
        self.apply(self.preflight())
        self.assertEqual(self.shared_receipts(), {})

    def test_source_and_receipt_movement_refuses_before_any_writes(self):
        item = self.decide()
        row = self.rows()[0]
        for field, value in [("rev", 2), ("body", "Changed body"), ("source", "searcher"),
                             ("origin", "Changed non-payload source")]:
            plan = self.preflight()
            path = self.root / "inbox" / f"{item['id']}.json"
            original = read_json(path)
            write_json(path, dict(original, **{field: value}))
            with self.subTest(field=field), self.assertRaises(Denied):
                self.apply(plan)
            self.assertEqual(self.shared_receipts(), {})
            write_json(path, original)
        plan = self.preflight()
        self.ack("reviewer", [row])  # Absent -> present is part of the boundary.
        with self.assertRaises(Denied):
            self.apply(plan)
        plan = self.preflight()
        path = self.legacy_path("reviewer", row)
        original = read_json(path)
        write_json(path, dict(original, acknowledged_at="2026-01-01T00:00:00+00:00"))
        with self.assertRaises(Denied):
            self.apply(plan)
        self.assertEqual(self.shared_receipts(), {})

    def test_action_route_source_route_role_set_and_plan_digest_movement_refuses(self):
        self.decide()
        self.ack("reviewer", self.rows())
        plan = self.preflight()
        changed = []
        for field, value in [("actions", []), ("sources", "any")]:
            routes = copy.deepcopy(self.routes)
            routes["reviewer"][field] = value
            changed.append(routes)
        changed.append({key: value for key, value in self.routes.items() if key != "editor"})
        changed.append(dict(self.routes, extra={"actions": [], "sources": "any"}))
        for routes in changed:
            with self.subTest(routes=routes), self.assertRaises(Denied):
                self.apply(plan, routes)
        for field in ("plan_digest", "route_digest"):
            bad = copy.deepcopy(plan)
            if field == "plan_digest":
                bad[field] = "0" * 64
            else:
                bad["boundary"][field] = "0" * 64
                bad["plan_digest"] = digest(canonical({"schema": 1, "boundary": bad["boundary"]}))
            with self.assertRaises(Denied):
                self.apply(bad)
        self.assertEqual(self.shared_receipts(), {})
        self.assertFalse((self.root / "shared-records").exists())

    def test_current_action_change_invalidates_plan(self):
        item = self.decide(action="defer")
        plan = self.preflight()
        self.insights.decide(item["id"], "reject", "New reason", "", "test", expected_rev=1)
        with self.assertRaises(Denied):
            self.apply(plan)
        self.assertEqual(self.shared_receipts(), {})

    def test_superseded_receipt_does_not_map_to_current_action(self):
        item = self.decide(action="defer")
        old = self.rows()[0]
        self.ack("reviewer", [old])
        self.legacy_path("reviewer", old).write_bytes(b"irrelevant corrupt old receipt")
        self.insights.decide(item["id"], "reject", "Current reason", "", "test", expected_rev=1)
        current = self.rows()[0]
        self.assertNotEqual(current["event"], old["event"])
        plan = self.preflight()
        self.assertEqual(plan["roles"]["reviewer"]["current_ids"], [self.rid(current)])
        self.apply(plan)
        self.assertEqual(self.shared_receipts(), {})

    def test_corrupt_relevant_receipts_refuse_and_excluded_receipts_are_ignored(self):
        self.decide()
        row = self.rows()[0]
        self.ack("reviewer", [row])
        path = self.legacy_path("reviewer", row)
        original = path.read_bytes()
        plan = self.preflight()
        invalid = [b"garbage", b"[]"]
        for field, value in [("role", "worker"), ("event", "0" * 32),
                             ("ack_token", "0" * 64), ("acknowledged_at", "2026-01-01T00:00:00")]:
            invalid.append(canonical(dict(json.loads(original), **{field: value})))
        for raw in invalid:
            path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(CorruptDecisionReceipt):
                self.preflight()
            with self.assertRaises(CorruptDecisionReceipt):
                self.apply(plan)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(self.shared_receipts(), {})
        excluded = copy.deepcopy(self.routes)
        excluded["reviewer"]["actions"] = []
        self.apply(self.preflight(excluded), excluded)
        self.assertEqual(self.shared_receipts(), {})

    def test_unsafe_relevant_receipt_refuses(self):
        self.decide()
        row = self.rows()[0]
        self.ack("reviewer", [row])
        path = self.legacy_path("reviewer", row)
        target = self.root / "target.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises((Denied, OSError)):
            self.preflight()
        path.unlink()
        os.link(target, path)
        with self.assertRaises(Denied):
            self.preflight()
        self.assertEqual(self.shared_receipts(), {})

    def test_extra_shared_receipt_mismatch_refuses_preflight_and_apply(self):
        self.decide()
        plan = self.preflight()
        ids = list(self.insights.project_decision_records())
        manifest = self.store.freeze("reviewer", ids)
        from dataclasses import asdict
        self.store.acknowledge("reviewer", [asdict(entry) for entry in manifest], manifest=manifest)
        before = self.shared_receipts()
        with self.assertRaises(Denied):
            self.preflight()
        with self.assertRaises(Denied):
            self.apply(plan)
        self.assertEqual(self.shared_receipts(), before)

    def test_partial_interruption_exact_retry_after_restart(self):
        for i in range(3):
            self.decide(i)
        self.ack("reviewer", self.rows())
        before = self.legacy_bytes()
        plan = self.preflight()
        original = SharedRecords.acknowledge

        def interrupted(store, role, selections, *, manifest):
            original(store, role, selections[:1], manifest=manifest)
            raise OSError("injected interruption after one exact receipt")

        with patch.object(SharedRecords, "acknowledge", interrupted), self.assertRaises(OSError):
            self.apply(plan)
        prefix = self.shared_receipts()
        self.assertEqual(len(prefix), 1)
        self.insights = Insights(self.root)
        retry = self.preflight()
        self.assertEqual(retry["boundary"], plan["boundary"])
        self.assertEqual(retry["plan_digest"], plan["plan_digest"])
        result = self.apply(json.loads(canonical(plan)))
        self.assertEqual(len(self.shared_receipts()), 3)
        self.assertEqual(result["roles"]["reviewer"]["shared_pending_ids"], [])
        self.assertEqual(self.legacy_bytes(), before)
        for name, raw in prefix.items():
            self.assertEqual(self.shared_receipts()[name], raw)

    def test_preexisting_shared_receipts_cannot_disappear_or_change(self):
        self.decide()
        self.ack("reviewer", self.rows())
        self.apply(self.preflight())
        plan = self.preflight()
        path = next((self.root / "shared-record-receipts").glob("*/*.json"))
        original = read_json(path)
        write_json(path, dict(original, acknowledged_at="2026-01-01T00:00:00+00:00"))
        with self.assertRaises(Denied):
            self.apply(plan)
        path.unlink()
        with self.assertRaises(Denied):
            self.apply(plan)

    def test_boundary_locks_held_across_scan_projection_receipts_and_verification(self):
        self.decide()
        self.ack("reviewer", self.rows())
        checks = []

        def check():
            paths = [role_exclusion(self.root, role) for role in sorted(self.routes)]
            paths += [self.root / "locks" / "insights.lock"]
            paths += [self.root / "locks" / f"decision-receipts-{digest(role.encode())}.lock"
                      for role in sorted(self.routes)]
            for path in paths:
                self.assertTrue(held(path), path.name)
                with self.assertRaises(Busy), lock(path, blocking=False):
                    pass
            checks.append(True)

        scan = self.insights._decision_projection_envelopes_locked
        put = SharedRecords.put
        acknowledge = SharedRecords.acknowledge

        def scanning(*, limit):
            check()
            return scan(limit=limit)

        def putting(store, *args, **kwargs):
            check()
            return put(store, *args, **kwargs)

        def acking(store, *args, **kwargs):
            check()
            return acknowledge(store, *args, **kwargs)

        with patch.object(self.insights, "_decision_projection_envelopes_locked", scanning), \
                patch.object(SharedRecords, "put", putting), patch.object(SharedRecords, "acknowledge", acking):
            self.apply(self.preflight())
        self.assertGreaterEqual(len(checks), 5)
        for role in self.routes:
            self.assertFalse(held(self.root / "locks" / f"decision-receipts-{digest(role.encode())}.lock"))

    def test_direct_ack_uses_receipt_lock(self):
        self.decide()
        rows = self.rows()
        original = self.insights._decision_received

        def received(*args):
            self.assertTrue(held(self.root / "locks" / f"decision-receipts-{digest('reviewer'.encode())}.lock"))
            return original(*args)

        with patch.object(self.insights, "_decision_received", received):
            self.ack("reviewer", rows)

    def test_legacy_receipt_locks_are_role_scoped(self):
        self.decide()
        reviewer = self.rows("reviewer")
        worker = self.insights.decision_events("worker", ["reject"], sources="any", limit=100)
        with self.insights._decision_receipt_lock("reviewer"):
            self.insights.acknowledge_decisions("worker", worker, sources="any")
        self.assertEqual(len(self.legacy_bytes()), len(worker))
        with self.insights._decision_receipt_lock("worker"):
            self.insights.acknowledge_decisions("reviewer", reviewer, sources="any")
        self.assertEqual(len(self.legacy_bytes()), len(worker) + len(reviewer))

    def test_active_role_including_empty_subscription_is_busy_before_writes(self):
        self.decide()
        self.ack("reviewer", self.rows())
        plan = self.preflight()
        for role in self.routes:
            with lock(role_exclusion(self.root, role), blocking=False):
                with self.assertRaises(Busy):
                    self.preflight()
                with self.assertRaises(Busy):
                    self.apply(plan)
            self.assertEqual(self.shared_receipts(), {})
            self.assertFalse((self.root / "shared-records").exists())

    def test_post_map_report_overflow_is_checked_before_any_writes(self):
        self.decide()
        self.ack("reviewer", self.rows())
        plan = self.preflight()
        post = copy.deepcopy(plan)
        for report in post["roles"].values():
            report["shared_received_ids"] = report["legacy_received_ids"]
            report["shared_pending_ids"] = report["legacy_pending_ids"]
            report["shared_receipt_digests"] = {rid: "0" * 64 for rid in report["legacy_received_ids"]}
        bound = len(canonical(plan))
        self.assertGreater(len(canonical(post)), bound)
        with patch("mizu.decision_reconciliation.MAX_FRAME", bound):
            with self.assertRaises(LimitExceeded):
                self.preflight()
            with self.assertRaises(LimitExceeded):
                self.apply(plan)
        self.assertEqual(self.shared_receipts(), {})
        self.assertFalse((self.root / "shared-records").exists())

    def test_count_and_serialized_report_overflow_refuse_before_writes(self):
        self.decide()
        self.ack("reviewer", self.rows())
        plan = self.preflight()
        with patch("mizu.decision_reconciliation.MAX_FRAME", 512):
            with self.assertRaises(LimitExceeded):
                self.preflight()
            with self.assertRaises(LimitExceeded):
                self.apply(plan)
        envelopes = self.insights._decision_projection_envelopes_locked(limit=512)
        # Exercise the real 512-row selector with a cheap controlled source.
        history = self.root / "decision-history"
        sample = next(iter(envelopes.values()))["payload"]
        for i in range(513):
            (history / f"{i:032x}.json").touch()

        def selected(path, wanted, **kwargs):
            payload = dict(sample, event=path.stem, insight=path.stem)
            return None, payload

        with patch.object(self.insights, "_current_decision_event", selected), self.assertRaises(LimitExceeded):
            self.preflight()
        self.assertEqual(self.shared_receipts(), {})
        self.assertFalse((self.root / "shared-records").exists())
