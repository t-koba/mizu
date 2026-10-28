"""Shadow pages have no runtime delivery or acknowledgement authority."""
import contextlib
from dataclasses import asdict, replace
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mizu.errors import Denied, LimitExceeded
from mizu.fs import MAX_FRAME, READ_PAGE_CHARS, canonical, digest
from mizu.insights import Insights, INSIGHT_DECISION_POLICY
from mizu.shared_records import SharedRecords
from mizu.smoke_refs import record_lock

ACTIONS = ("accept", "modify", "reject", "defer")


class SharedDecisionPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.insights = Insights(self.root)
        self.store = SharedRecords(self.root, {"insight.decision": INSIGHT_DECISION_POLICY})

    def decide(self, index=0, *, action="reject", source="reviewer", body=None, insight_id=None):
        item = self.insights.submit(source=source, title=f"Finding {index}",
                                    body=body or f"Exact body {index}", base_snapshot=None,
                                    **({"insight_id": insight_id} if insight_id is not None else {}))
        self.insights.decide(item["id"], action, "Exact reason", "Exact revisit", "test",
                             expected_rev=1, **({"work_item_intent": "none"}
                                               if action in ("accept", "modify") else {}))
        return item

    def page(self, role="reviewer", actions=ACTIONS, *, max_bytes=MAX_FRAME,
             limit=100, sources="any"):
        page, manifest = self.insights.shared_decision_page(
            role, actions, max_bytes=max_bytes, limit=limit, sources=sources)
        self.assertLessEqual(len(canonical(page)), max_bytes)
        self.assertEqual(len(manifest), len(page["events"]))
        for row, entry in zip(page["events"], manifest):
            self.assertEqual(row["ack_token"], entry.ack_token)
            self.assertEqual(f"insight.decision/{row['event']}", entry.record_id)
        return page, manifest

    def read(self, manifest, **kwargs):
        return self.store.read_page("reviewer", manifest[0].ack_token, manifest=manifest, **kwargs)

    def test_legacy_payload_parity_for_all_action_and_source_routes(self):
        for index, action in enumerate(ACTIONS):
            for source in ("reviewer", "searcher"):
                self.decide(index, action=action, source=source)
        for sources in ("own", "any"):
            for role in ("reviewer", "searcher", "worker"):
                for actions in (ACTIONS, *((a,) for a in ACTIONS), ()):
                    with self.subTest(sources=sources, role=role, actions=actions):
                        page, _ = self.page(role, actions, sources=sources)
                        legacy = self.insights.decision_events(role, actions, sources=sources, limit=100)
                        def strip(rows):
                            return [{k: v for k, v in row.items() if k != "ack_token"} for row in rows]
                        self.assertEqual(strip(page["events"]), strip(legacy))
                        self.assertEqual(page["pending_count"], len(legacy))
                        self.assertFalse(page["truncated"])
        self.assertFalse((self.root / "shared-record-receipts").exists())
        self.assertFalse((self.root / "decision-receipts").exists())

    def test_205_events_utc_then_stable_id_order_and_receipt_replay(self):
        stamps = ("2026-01-01T01:00:00+01:00", "2025-12-31T19:00:00-05:00",
                  "2026-01-01T00:00:01+00:00", "2026-01-01T00:00:00+00:00")
        # These fixtures target page behavior; auto-ID dedup is covered separately in test_insight_archive.
        for i in range(205):
            with patch("mizu.insights.now", return_value=stamps[i % len(stamps)]):
                self.decide(i, insight_id=f"{i:032x}", action=ACTIONS[i % 4],
                            source="reviewer" if i % 2 else "searcher")
        projected = self.insights.project_decision_records()
        expected = sorted(projected, key=lambda key: (
            dt.datetime.fromisoformat(self.store.get(key)[0]["created_at"]).astimezone(dt.timezone.utc), key))
        first, manifest = self.page()
        self.assertEqual([entry.record_id for entry in manifest], expected[:100])
        self.assertEqual(first["pending_count"], 205)
        self.assertTrue(first["count_truncated"])
        self.assertFalse(first["byte_truncated"])
        self.store.acknowledge("reviewer", manifest=manifest)
        self.store.acknowledge("reviewer", [], manifest=manifest)
        self.assertEqual(self.page(), (first, manifest))
        selections = [asdict(entry) for entry in manifest[::2]]
        self.store.acknowledge("reviewer", selections, manifest=manifest)
        self.store.acknowledge("reviewer", selections, manifest=manifest)
        self.insights = Insights(self.root)
        next_page, next_manifest = self.page()
        remaining = [key for key in expected if key not in {entry.record_id for entry in manifest[::2]}]
        self.assertEqual(next_page["pending_count"], 155)
        self.assertEqual([entry.record_id for entry in next_manifest], remaining[:100])
        self.assertEqual(self.page("searcher")[0]["pending_count"], 205)
        self.assertEqual(len(self.insights.decision_events("reviewer", ACTIONS, sources="any", limit=100)), 100)
        seen = [entry.record_id for entry in manifest[::2]]
        while next_manifest:
            seen.extend(entry.record_id for entry in next_manifest)
            self.store.acknowledge("reviewer", [asdict(e) for e in next_manifest], manifest=next_manifest)
            next_page, next_manifest = self.page()
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), set(expected))
        self.assertEqual(next_page["pending_count"], 0)
        self.assertFalse(next_page["truncated"])

    def test_source_revision_and_route_movement_keep_old_manifest_retrievable(self):
        item = self.decide(source="searcher")
        page, manifest = self.page()
        original = self.read(manifest)
        self.assertEqual(self.page(sources="own")[0]["events"], [])
        self.assertEqual(self.page(actions=())[0]["events"], [])
        self.insights.revise(item["id"], source="searcher", title="Revised", body="New body",
                             expected_rev=1, run="test", base_snapshot=None)
        self.assertEqual(self.page()[0]["events"], [])
        self.assertEqual(self.read(manifest), original)
        self.insights.decide(item["id"], "reject", "New reason", "", "test", expected_rev=2)
        new, new_manifest = self.page()
        self.assertEqual(new["pending_count"], 1)
        self.assertNotEqual(new_manifest[0].record_id, manifest[0].record_id)
        self.assertEqual(self.read(manifest), original)
        self.assertEqual(page["events"][0]["rev"], 1)
        self.assertEqual(new["events"][0]["rev"], 2)

    def test_multibyte_escaped_aggregate_bounds_reference_and_reconstruction(self):
        for i in range(3):
            self.decide(i, body=('日本語 😀 "quote" \\ slash\n\t' * 500))
        full, _ = self.page()
        exact_size = len(canonical(full))
        self.assertEqual(self.page(max_bytes=exact_size)[0], full)
        smaller, _ = self.page(max_bytes=exact_size - 1)
        self.assertTrue(any("ref" in row for row in smaller["events"]))
        small, manifest = self.page(max_bytes=650)
        self.assertEqual(len(manifest), 1)
        self.assertTrue(small["byte_truncated"])
        self.assertFalse(small["count_truncated"])
        row = small["events"][0]
        entry = manifest[0]
        self.assertEqual(row["ref"]["record_id"], entry.record_id)
        self.assertEqual(row["ref"]["record_digest"], entry.record_digest)
        chunks = []
        offset = 0
        while True:
            part = self.read(manifest, offset=offset, limit=137)
            self.assertLessEqual(len(part["text"]), 137)
            self.assertEqual(part["offset"], offset)
            chunks.append(part["text"])
            if part["next_offset"] is None:
                break
            offset = part["next_offset"]
        raw = "".join(chunks).encode("utf-8")
        self.assertEqual(len(raw), row["ref"]["bytes"])
        self.assertEqual(digest(raw), entry.record_digest)
        self.assertEqual(len(raw.decode("utf-8")), part["total_chars"])
        self.assertEqual(raw, canonical(self.store.get(entry.record_id)[0]))
        self.assertEqual(self.read(manifest, offset=part["total_chars"])["text"], "")
        for bound in (1, 150, 400):
            with self.subTest(bound=bound), self.assertRaises(LimitExceeded):
                self.page(max_bytes=bound)
        self.assertEqual(self.page(max_bytes=650), (small, manifest))
        self.assertFalse((self.root / "shared-record-receipts").exists())

    def test_read_authorization_bounds_and_exact_digest_every_page(self):
        self.decide(body="abc😀" * 3000)
        _, manifest = self.page(max_bytes=650)
        entry = manifest[0]
        self.assertEqual(len(self.read(manifest)["text"]), READ_PAGE_CHARS)
        for offset in (-1, True, 1.5, "0", None, 10**8):
            with self.subTest(offset=offset), self.assertRaises(Denied):
                self.read(manifest, offset=offset)
        for limit in (0, -1, True, 1.5, READ_PAGE_CHARS + 1):
            with self.subTest(limit=limit), self.assertRaises(Denied):
                self.read(manifest, limit=limit)
        for role, token, frozen in (
            ("worker", entry.ack_token, manifest),
            ("reviewer", "0" * 64, manifest),
            ("reviewer", entry.record_id, manifest),
            ("reviewer", entry.ack_token, ()),
            ("reviewer", entry.ack_token, list(manifest)),
            ("reviewer", entry.ack_token, (replace(entry, record_digest="0" * 64),)),
            ("reviewer", entry.ack_token, (entry, entry)),
        ):
            with self.subTest(role=role, token=token), self.assertRaises(Denied):
                self.store.read_page(role, token, manifest=frozen)
        path = self.root / self.store._record_name(entry.record_id)
        original = path.read_bytes()
        changed = json.loads(original)
        changed["payload"]["title"] = "Changed after first read"
        for raw in (b"not json", canonical(changed)):
            path.write_bytes(raw)
            with self.assertRaises(Denied):
                self.read(manifest, offset=1)
            with self.assertRaises(Denied):
                self.page()
            self.assertEqual(path.read_bytes(), raw)
        path.write_bytes(original)
        path.unlink()
        with self.assertRaises(Denied):
            self.read(manifest)

    def test_corrupt_shared_receipt_fails_closed_but_legacy_receipt_is_ignored(self):
        self.decide()
        legacy = self.insights.decision_events("reviewer", ACTIONS, sources="any")
        self.insights.acknowledge_decisions("reviewer", legacy, sources="any")
        page, manifest = self.page()
        self.assertEqual(page["pending_count"], 1)
        entry = manifest[0]
        self.store.acknowledge("reviewer", [asdict(entry)], manifest=manifest)
        receipt = (self.root / "shared-record-receipts" / digest(b"reviewer") /
                   f"{digest(entry.record_id.encode())}.json")
        receipt.write_bytes(b"corrupt")
        with self.assertRaises(Denied):
            self.page()
        self.assertEqual(receipt.read_bytes(), b"corrupt")
        self.assertIn("text", self.read(manifest))

    def test_unsafe_store_and_record_denied_for_page_and_retrieval(self):
        self.decide()
        _, manifest = self.page()
        directory = self.root / "shared-records"
        moved = self.root / "retained"
        directory.rename(moved)
        try:
            directory.symlink_to(moved, target_is_directory=True)
        except (OSError, NotImplementedError):
            moved.rename(directory)
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(Denied):
            self.page()
        with self.assertRaises(Denied):
            self.read(manifest)
        directory.unlink()
        moved.rename(directory)
        path = next(directory.rglob("*.json"))
        retained = path.with_suffix(".retained")
        path.rename(retained)
        path.symlink_to(retained)
        with self.assertRaises(Denied):
            self.page()
        with self.assertRaises(Denied):
            self.read(manifest)

    def test_validation_empty_page_and_count_overflow_before_writes(self):
        for kwargs in ({"limit": True}, {"limit": 0}, {"limit": 101},
                       {"max_bytes": True}, {"max_bytes": 0}, {"max_bytes": MAX_FRAME + 1},
                       {"sources": "other"}, {"actions": ("withdraw",)}, {"actions": "reject"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Denied):
                self.page(**kwargs)
        with self.assertRaises(TypeError):
            self.insights.shared_decision_page("reviewer", ACTIONS)
        empty, manifest = self.page()
        self.assertEqual(manifest, ())
        self.assertEqual(self.page(max_bytes=len(canonical(empty)))[0], empty)
        self.decide()
        self.assertEqual(self.page(actions=())[0]["pending_count"], 0)
        self.assertFalse((self.root / "shared-records").exists())
        legacy = self.insights.decision_events("reviewer", ACTIONS)[0]
        payload = {k: v for k, v in legacy.items() if k != "ack_token"}
        paths = [Path(f"{i:032x}.json") for i in range(513)]

        def selected(insights, path, *args, **kwargs):
            return dt.datetime.now(dt.timezone.utc), dict(payload, event=path.stem, insight=path.stem)

        with patch.object(Path, "glob", return_value=paths), \
                patch.object(Insights, "_current_decision_event", selected), self.assertRaises(LimitExceeded):
            self.page()
        self.assertFalse((self.root / "shared-records").exists())
        self.assertEqual(self.page()[0]["pending_count"], 1)

    def test_lock_covers_selection_projection_receipt_reads_and_freeze(self):
        self.decide()
        held = False

        @contextlib.contextmanager
        def observed_lock(root):
            nonlocal held
            with record_lock(root):
                held = True
                try:
                    yield
                finally:
                    held = False

        def observe(method):
            def wrapped(*args, **kwargs):
                self.assertTrue(held)
                return method(*args, **kwargs)
            return wrapped

        with patch("mizu.insights.record_lock", observed_lock), \
                patch.object(Insights, "_current_decision_event", observe(Insights._current_decision_event)), \
                patch.object(SharedRecords, "put", observe(SharedRecords.put)), \
                patch.object(SharedRecords, "received", observe(SharedRecords.received)), \
                patch.object(SharedRecords, "freeze", observe(SharedRecords.freeze)):
            self.page()
        self.assertFalse(held)

    def test_partial_projection_failure_retry_and_only_selected_rows_frozen(self):
        for i in range(3):
            self.decide(i)
        put = SharedRecords.put
        count = 0

        def interrupted(store, envelope, *, writer):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("Interrupted shadow projection")
            return put(store, envelope, writer=writer)

        with patch.object(SharedRecords, "put", interrupted), self.assertRaises(OSError):
            self.page(limit=1)
        paths = list((self.root / "shared-records").rglob("*.json"))
        self.assertEqual(len(paths), 1)
        original = paths[0].read_bytes()
        with patch.object(SharedRecords, "freeze", wraps=self.store.freeze) as freeze:
            page, manifest = self.page(limit=1)
        freeze.assert_called_once_with("reviewer", [entry.record_id for entry in manifest])
        self.assertEqual(page["pending_count"], 3)
        self.assertEqual(len(manifest), 1)
        self.assertEqual(paths[0].read_bytes(), original)
        self.assertEqual(self.page(limit=1), (page, manifest))


if __name__ == "__main__":
    unittest.main()
