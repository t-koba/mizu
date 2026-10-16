"""Synthetic legacy history only: explicit reconciliation never migrates decisions."""
import concurrent.futures
import hashlib
import json
import os
from unittest.mock import patch

from support import Fixture
from mizu.errors import Denied, LimitExceeded
from mizu.fs import PREVIEW_BYTES, canonical, write_json


class LegacyWorkItemTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.serial = 0

    def hardlink(self, target, link):
        try:
            os.link(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("Hard links unavailable")

    def symlink(self, target, link, **kwargs):
        try:
            os.symlink(target, link, **kwargs)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")

    def legacy(self, **changes):
        self.serial += 1
        event_id = f"{self.serial:032x}"
        event = {"id": "synthetic-insight", "action": "accept", "rev": 1,
                 "reason": "Legacy assessment", "revisit": "", "run": "synthetic-run",
                 "created_at": "2024-01-01T00:00:00+00:00", **changes}
        path = self.project.root / "decision-history" / f"{event_id}.json"
        # Deliberately noncanonical original bytes prove no decision rewrite.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(event, indent=2) + "\n")
        return event_id, event, path

    def resolve(self, event_id, **changes):
        return self.store.reconcile(event_id, **{
            "expected_action": "accept", "outcome": "open", "reason": "Still actionable",
            "title": "Implement the retained slice", "actor": "worker", **changes})["record"]

    def sidecar(self, event_id):
        return self.project.root / "work-items" / f"wi_{event_id}" / "legacy.json"

    def test_open_drop_coverage_audit_and_history_immutability(self):
        opened, event, path = self.legacy(reason="r" * 1400, body="NEVER COPY BODY")
        dropped, _, drop_path = self.legacy(action="modify")
        original = {p: p.read_bytes() for p in (path, drop_path)}
        self.assertEqual(self.store.page_open()["unseeded_decisions"], 2)
        record = self.resolve(opened, expected_rev=1)
        self.assertEqual(record["event_sha256"], hashlib.sha256(canonical(event)).hexdigest())
        self.assertEqual(record["work_item"]["created_at"], event["created_at"])
        self.assertEqual(self.store.get(f"wi_{opened}"), record["work_item"])
        self.assertEqual(self.store.page_open()["unseeded_decisions"], 1)
        drop = self.resolve(dropped, expected_action="modify", outcome="drop", title=None,
                            reason="Superseded by a later assessed design")
        self.assertNotIn("work_item", drop)
        page = self.store.page_open()
        self.assertTrue(page["coverage_complete"])
        self.assertEqual(page["unseeded_decisions"], 0)
        self.assertEqual([row["id"] for row in page["items"]], [f"wi_{opened}"])
        audit = self.store.page_legacy()
        self.assertEqual(audit["total"], 2)
        self.assertTrue(audit["coverage_complete"])
        self.assertEqual([row["resolution"]["outcome"] for row in audit["items"]], ["open", "drop"])
        self.assertTrue(audit["items"][0]["reason_truncated"])
        self.assertNotIn("NEVER COPY BODY", json.dumps(audit))
        self.assertEqual(audit["items"][1]["resolution"]["reason_preview"], drop["reason"])
        for p, content in original.items():
            self.assertEqual(p.read_bytes(), content)
        with self.assertRaises(Denied):
            self.store.get(f"wi_{dropped}")

    def test_by_id_history_reads_open_reconciliation_sidecar_seed(self):
        event_id, _, _ = self.legacy()
        resolution = self.resolve(event_id)

        view = self.store.explain(f"wi_{event_id}")

        self.assertEqual(view["item"], resolution["work_item"])
        self.assertEqual(view["history"], [resolution["work_item"]])
        self.assertEqual(view["history_total"], 1)
        self.assertTrue(view["stale"])

    def test_current_decision_bytes_unchanged_and_seeded_events_refused(self):
        proposal = self.project.insights.submit(source="worker", title="Seeded", body="Synthetic", base_snapshot=None)
        decision = self.project.insights.decide(proposal["id"], "accept", "Assessed", "", "run", expected_rev=1)
        current_path = self.project.root / "decisions" / f"{proposal['id']}.json"
        event_id = decision["work_item"]["id"][3:]
        with self.assertRaises(Denied):
            self.resolve(event_id, outcome="drop", title=None)
        legacy_id, event, _ = self.legacy(id=proposal["id"])
        write_json(current_path, event)
        before = current_path.read_bytes()
        self.resolve(legacy_id)
        self.assertEqual(current_path.read_bytes(), before)
        # Even a syntactically valid sidecar cannot resolve a seeded source.
        record = self.resolve(legacy_id)
        record.update(event_id=event_id, event_sha256=hashlib.sha256(canonical(decision)).hexdigest())
        write_json(self.sidecar(event_id), record)
        for read in (self.store.page_open, self.store.page_legacy):
            with self.assertRaises(Denied):
                read()

    def test_closed_legacy_item_remains_covered_and_auditable(self):
        event_id, _, _ = self.legacy()
        record = self.resolve(event_id, title="t" * 200, reason="理由" * 2000)
        self.store.transition(f"wi_{event_id}", expected_rev=1, status="dropped", actor="worker", reason="No longer needed")
        page = self.store.page_open()
        self.assertEqual(page["items"], [])
        self.assertEqual(page["unseeded_decisions"], 0)
        self.assertTrue(page["coverage_complete"])
        audit = self.store.page_legacy()["items"][0]
        self.assertEqual(audit["resolution"]["outcome"], "open")
        self.assertTrue(audit["resolution"]["reason_truncated"])
        self.assertLessEqual(len(audit["resolution"]["reason_preview"].encode()), 1024)
        self.assertFalse(audit["openable"])
        self.assertEqual(self.resolve(event_id, title="t" * 200, reason="理由" * 2000), record)

    def test_incomplete_origin_can_drop_but_never_open(self):
        for changes in ({"rev": None}, {"rev": True}, {"rev": 0}, {"rev": "1"},
                        {"created_at": None}, {"created_at": "invalid"},
                        {"created_at": "2024-01-01"}, {"id": "../unsafe"}):
            with self.subTest(changes=changes):
                event_id, _, path = self.legacy(**changes)
                event = json.loads(path.read_bytes())
                event = {k: v for k, v in event.items() if v is not None}
                write_json(path, event)
                self.assertFalse(next(row for row in self.store.page_legacy()["items"]
                                      if row["event_id"] == event_id)["openable"])
                with self.assertRaises(Denied):
                    self.resolve(event_id)
                self.assertFalse(self.sidecar(event_id).exists())
                self.resolve(event_id, outcome="drop", title=None, reason="No valid legacy origin")
        self.assertTrue(self.store.page_open()["coverage_complete"])

    def test_exact_retry_is_immutable_even_after_transition_conflicts_refused(self):
        event_id, _, _ = self.legacy()
        first = self.resolve(event_id)
        before = self.sidecar(event_id).read_bytes()
        self.assertEqual(self.resolve(event_id, expected_rev=1), first)
        item_id = f"wi_{event_id}"
        self.store.transition(item_id, expected_rev=1, status="doing", actor="worker")
        self.assertEqual(self.resolve(event_id), first)
        for changes in ({"actor": "other"}, {"title": "Other title"}, {"reason": "Other reason"},
                        {"outcome": "drop", "title": None}, {"expected_action": "modify"},
                        {"expected_rev": 2}):
            with self.subTest(changes=changes), self.assertRaises(Denied):
                self.resolve(event_id, **changes)
        self.assertEqual(self.sidecar(event_id).read_bytes(), before)
        with self.assertRaisesRegex(Denied, "re-read"):
            self.store.transition(item_id, expected_rev=1, status="blocked", actor="worker")
        self.assertEqual(self.store.get(item_id)["rev"], 2)

    def test_drop_retry_and_concurrent_exclusive_resolution(self):
        event_id, _, _ = self.legacy()
        def resolve():
            return self.resolve(event_id, outcome="drop", title=None, reason="Duplicate")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: resolve(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(resolve(), results[0])
        with self.assertRaises(Denied):
            self.resolve(event_id)
        self.assertEqual([p.name for p in self.sidecar(event_id).parent.iterdir()], ["legacy.json"])

    def test_expectations_validation_and_seeded_nonaccept_refusal(self):
        event_id, _, _ = self.legacy()
        for changes in ({"expected_action": "modify"}, {"expected_rev": 2}, {"expected_rev": True},
                        {"reason": " "}, {"reason": "r" * 4001}, {"actor": "../role"},
                        {"title": None}, {"title": ""}, {"title": "t" * 201},
                        {"outcome": "drop"}, {"outcome": "bad"}):
            with self.subTest(changes=changes), self.assertRaises(Denied):
                self.resolve(event_id, **changes)
        self.assertFalse(self.sidecar(event_id).exists())
        for event_id in ("../event", "wi_" + "a" * 32, "a" * 33):
            with self.assertRaises(Denied):
                self.resolve(event_id)
        for changes in ({"action": "reject"}, {"work_item": None}):
            event_id, _, _ = self.legacy(**changes)
            with self.assertRaises(Denied):
                self.resolve(event_id, outcome="drop", title=None)

    def test_hash_binding_and_corruption_fail_closed(self):
        event_id, event, path = self.legacy()
        record = self.resolve(event_id)
        sidecar = self.sidecar(event_id)
        for changes in ({"event_id": "f" * 32}, {"event_sha256": "f" * 64},
                        {"revision": True}, {"revision": 2}, {"action": "modify"},
                        {"actor": "../actor"}, {"reason": " "}, {"outcome": "bad"},
                        {"created_at": "bad"}, {"extra": 1}, {"work_item": []},
                        {"work_item": {**record["work_item"], "rev": True}}):
            with self.subTest(changes=changes):
                write_json(sidecar, {**record, **changes})
                for read in (self.store.page_open, self.store.page_legacy,
                             lambda: self.store.get(f"wi_{event_id}"),
                             lambda: self.resolve(event_id)):
                    with self.assertRaises(Denied):
                        read()
        write_json(sidecar, record)
        write_json(path, {**event, "reason": "Changed original assessment"})
        for read in (self.store.page_open, self.store.page_legacy,
                     lambda: self.store.get(f"wi_{event_id}"), lambda: self.resolve(event_id)):
            with self.assertRaises(Denied):
                read()

    def test_malformed_committed_json_and_byte_bounds(self):
        event_id, _, _ = self.legacy()
        self.resolve(event_id)
        sidecar = self.sidecar(event_id)
        for content in (b"{", b"[]", b'{"value":NaN}'):
            sidecar.write_bytes(content)
            for read in (self.store.page_open, self.store.page_legacy):
                with self.assertRaises(Denied):
                    read()
        sidecar.write_bytes(b" " * (PREVIEW_BYTES + 1))
        with self.assertRaises(LimitExceeded):
            self.store.page_legacy()

    def test_post_link_sidecar_and_orphan_temporary_recovery(self):
        event_id, _, _ = self.legacy()
        record = self.resolve(event_id)
        sidecar = self.sidecar(event_id)
        alias = sidecar.parent / ".new-abcdefgh"
        self.hardlink(sidecar, alias)
        self.assertEqual(self.store.page_legacy()["items"][0]["resolution"]["outcome"], "open")
        self.assertFalse(alias.exists())
        alias.write_bytes(b"not committed")
        self.assertEqual(self.resolve(event_id), record)
        self.assertFalse(alias.exists())
        sidecar.unlink()
        alias.write_bytes(canonical(record))
        self.assertEqual(self.store.page_open()["unseeded_decisions"], 1)
        self.assertFalse(sidecar.exists())
        self.assertFalse(alias.exists())

    def test_temporary_recovery_is_bounded_and_unsafe_links_fail_closed(self):
        event_id, _, _ = self.legacy()
        self.resolve(event_id)
        sidecar = self.sidecar(event_id)
        outside = self.project.root / "unrecognized-link"
        self.hardlink(sidecar, outside)
        with self.assertRaises(Denied):
            self.store.page_legacy()
        outside.unlink()
        outside.write_text("uncommitted")
        self.hardlink(outside, sidecar.parent / ".new-abcdefgh")
        with self.assertRaises(Denied):
            self.resolve(event_id)
        (sidecar.parent / ".new-abcdefgh").unlink()
        unsafe = sidecar.parent / ".new-abcdefgh"
        self.symlink(outside, unsafe)
        with self.assertRaises(Denied):
            self.store.page_legacy()
        unsafe.unlink()
        for i in range(258):
            (sidecar.parent / f".new-{i:08d}").write_text("orphan")
        with self.assertRaisesRegex(Denied, "bound"):
            self.store.page_open()

    def test_symlink_sidecar_and_parent_refused_without_writes(self):
        event_id, _, _ = self.legacy()
        self.resolve(event_id)
        sidecar = self.sidecar(event_id)
        content = sidecar.read_bytes()
        sidecar.unlink()
        target = self.project.root / "sidecar-target"
        target.write_bytes(content)
        self.symlink(target, sidecar)
        with self.assertRaises(Denied):
            self.store.page_open()
        sidecar.unlink()
        sidecar.parent.rmdir()
        target_dir = self.project.root / "other-directory"
        target_dir.mkdir()
        self.symlink(target_dir, sidecar.parent, target_is_directory=True)
        with self.assertRaises(Denied):
            self.resolve(event_id)
        self.assertEqual(list(target_dir.iterdir()), [])

    def test_drop_cannot_carry_seed_or_transition_history(self):
        event_id, _, _ = self.legacy()
        record = self.resolve(event_id, outcome="drop", title=None)
        write_json(self.sidecar(event_id), {**record, "work_item": {}})
        with self.assertRaises(Denied):
            self.store.page_open()
        write_json(self.sidecar(event_id), record)
        write_json(self.sidecar(event_id).parent / "2.json", {})
        for read in (self.store.page_open, self.store.page_legacy):
            with self.assertRaises(Denied):
                read()

    def test_cursor_live_pages_and_selection_bounds(self):
        ids = [self.legacy()[0] for _ in range(13)]
        cursor, seen = None, []
        while True:
            page = self.store.page_legacy(cursor=cursor, limit=3)
            self.assertEqual(page["total"], 13)
            self.assertLessEqual(len(page["items"]), 3)
            self.assertEqual(page["cursor"], cursor)
            seen.extend(row["event_id"] for row in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
            self.assertLessEqual(len(cursor), 128)
            # Resolution does not offset-skip or remove audit membership.
            self.resolve(page["items"][0]["event_id"], outcome="drop", title=None)
        self.assertEqual(seen, sorted(ids))
        self.assertEqual(self.store.page_legacy(cursor="wl1|" + "f" * 32)["items"], [])
        import heapq
        push = heapq.heappush
        sizes = []
        def bounded_push(heap, row):
            sizes.append(len(heap) + 1)
            return push(heap, row)
        with patch("mizu.work_items.heapq.heappush", side_effect=bounded_push):
            self.store.page_legacy(limit=2)
        self.assertEqual(max(sizes), 2)
        for limit in (True, 0, 101, 1.5, "2", None):
            with self.assertRaises(Denied):
                self.store.page_legacy(limit=limit)
        for cursor in ("", 1, "wl1|" + "a" * 33, "wl1|" + "G" * 32,
                       "wi1|open|0|wi_" + "a" * 32, "x" * 129):
            with self.assertRaises(Denied):
                self.store.page_legacy(cursor=cursor)
        with patch("mizu.work_items.MAX_OPEN_ROW_BYTES", 10):
            with self.assertRaises(LimitExceeded):
                self.store.page_legacy()
        with patch("mizu.work_items.MAX_OPEN_PAGE_BYTES", 10):
            with self.assertRaises(LimitExceeded):
                self.store.page_legacy()

    def test_unsafe_committed_history_refused_even_beyond_cursor(self):
        event_id, _, path = self.legacy()
        for content in (b"[]", b"{", b'{"action":"unknown"}'):
            path.write_bytes(content)
            with self.assertRaises(Denied):
                self.store.page_legacy(cursor="wl1|" + "f" * 32, limit=1)
        path.unlink()
        target = self.project.root / "event-target"
        target.write_text('{"action":"accept"}')
        self.symlink(target, path)
        with self.assertRaises(Denied):
            self.store.page_legacy()

    def test_legacy_current_redecision_and_seeded_validation_remain_strict(self):
        proposal = self.project.insights.submit(source="worker", title="Origin", body="Synthetic", base_snapshot=None)
        event_id, event, _ = self.legacy(id=proposal["id"])
        self.resolve(event_id)
        revised = self.project.insights.revise(proposal["id"], source="worker", title="Revision",
                                               body="New synthetic revision", expected_rev=1, base_snapshot=None)
        current = {**event, "rev": revised["rev"], "action": "modify"}
        current_path = self.project.root / "decisions" / f"{proposal['id']}.json"
        write_json(current_path, current)
        before = current_path.read_bytes()
        with self.assertRaises(Denied):
            self.store.projection(f"wi_{event_id}")  # Well-shaped mutable pointers need history.
        current_event_id = "b" * 32
        write_json(self.project.root / "decision-history" / f"{current_event_id}.json", current)
        view = self.store.projection(f"wi_{event_id}")
        self.assertFalse(view["stale"])
        self.assertEqual(view["redecision"], {"rev": 2, "action": "modify"})
        self.assertEqual(current_path.read_bytes(), before)
        write_json(current_path, {**current, "work_item": None})
        with self.assertRaises(Denied):
            self.store.projection(f"wi_{event_id}")
