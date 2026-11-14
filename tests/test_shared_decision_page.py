"""Shadow pages have no runtime delivery or acknowledgement authority."""
import contextlib
from dataclasses import asdict, replace
import datetime as dt
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from mizu.errors import Denied, LimitExceeded
from mizu.fs import (MAX_FRAME, READ_PAGE_CHARS, canonical, digest, read_json, write_json)
from mizu.insights import Insights, INSIGHT_DECISION_POLICY, INSIGHT_PENDING_POLICY
import mizu.insights as insights_module
from mizu.shared_records import SharedRecords
from support import write_fixture_json
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

    def seed_decision_batch(self, count, stamps):
        """Seed one real example per action/source/time pattern, then clone valid rows."""
        inbox = self.root / "inbox"
        decisions = self.root / "decisions"
        history = self.root / "decision-history"
        templates = {}
        for index in range(4):
            insight_id = f"{index:032x}"
            action = ACTIONS[index]
            source = "reviewer" if index % 2 else "searcher"
            with patch("mizu.insights.now", return_value=stamps[index]):
                self.decide(index, insight_id=insight_id, action=action, source=source)
            key = insights_module._decision_key(insight_id, 1)
            if action == "defer":
                event_path = next(path for path in history.glob("*.json")
                                  if read_json(path)["id"] == insight_id)
            else:
                event_path = history / f"{key}.json"
            templates[index] = (read_json(inbox / f"{insight_id}.json"),
                                read_json(decisions / f"{insight_id}.json"),
                                read_json(event_path), action)
        for index in range(4, count):
            template_index = index % 4
            item, decision, event, action = templates[template_index]
            insight_id = f"{index:032x}"
            key = insights_module._decision_key(insight_id, 1)
            event_id = key if action != "defer" else uuid.uuid4().hex
            write_fixture_json(inbox / f"{insight_id}.json",
                       {**item, "id": insight_id, "title": f"Finding {index}",
                        "body": f"Exact body {index}"})
            write_fixture_json(decisions / f"{insight_id}.json",
                       {**decision, "id": insight_id, "decision_key": key})
            write_fixture_json(history / f"{event_id}.json",
                       {**event, "id": insight_id, "decision_key": key})

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

    def test_101_events_utc_then_stable_id_order_and_receipt_replay(self):
        stamps = ("2026-01-01T01:00:00+01:00", "2025-12-31T19:00:00-05:00",
                  "2026-01-01T00:00:01+00:00", "2026-01-01T00:00:00+00:00")
        # Cross the 100-row page boundary; the separate cap-boundary test below
        # covers history larger than the configured projection window.
        self.seed_decision_batch(101, stamps)
        projected = self.insights.project_decision_records()
        expected = sorted(projected, key=lambda key: (
            dt.datetime.fromisoformat(self.store.get(key)[0]["created_at"]).astimezone(dt.timezone.utc), key))
        first, manifest = self.page()
        self.assertEqual([entry.record_id for entry in manifest], expected[:100])
        self.assertEqual(first["pending_count"], 101)
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
        self.assertEqual(next_page["pending_count"], 51)
        self.assertEqual([entry.record_id for entry in next_manifest], remaining[:100])
        self.assertEqual(self.page("searcher")[0]["pending_count"], 101)
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
        # Keep several 137-character reads across multibyte and escaped text
        # without excessive repetition of the same payload.
        for i in range(3):
            self.decide(i, body=('日本語 😀 "quote" \\ slash\n\t' * 50))
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

    def test_post_put_corruption_denied_for_eligible_and_filtered_rows(self):
        self.decide(action="reject", source="reviewer")
        put = SharedRecords.put
        # Eligible page projection is validated after put.
        for malformed in (True, False):
            with self.subTest(role="reviewer", actions=("reject",), sources="own",
                              malformed=malformed):
                shutil.rmtree(self.root / "shared-records", ignore_errors=True)
                corrupted = []

                def corrupt(store, envelope, *, writer):
                    computed = put(store, envelope, writer=writer)
                    path = self.root / store._record_name(envelope["id"])
                    original = path.read_bytes()
                    changed = json.loads(original)
                    changed["payload"]["title"] = "Changed after put"
                    raw = b"not json" if malformed else canonical(changed)
                    path.write_bytes(raw)
                    corrupted.append((path, original, raw))
                    return computed

                try:
                    with patch.object(SharedRecords, "put", corrupt), \
                            self.assertRaises(Denied):
                        self.page()
                    self.assertEqual(len(corrupted), 1)
                    path, _, raw = corrupted[0]
                    self.assertEqual(path.read_bytes(), raw)
                    self.assertFalse((self.root / "shared-record-receipts").exists())
                finally:
                    for path, original, _ in corrupted:
                        path.write_bytes(original)
        # A filtered existing projection is still validated, even though this
        # page does not select or rewrite it.
        for role, actions, sources in (("reviewer", ("accept",), "any"),
                                       ("worker", ("reject",), "own")):
            for malformed in (True, False):
                with self.subTest(role=role, actions=actions, sources=sources,
                                  malformed=malformed):
                    shutil.rmtree(self.root / "shared-records", ignore_errors=True)
                    projected = self.insights.project_decision_records()
                    self.assertEqual(len(projected), 1)
                    record_id = next(iter(projected))
                    path = self.root / self.store._record_name(record_id)
                    original = path.read_bytes()
                    changed = json.loads(original)
                    changed["payload"]["title"] = "Changed after put"
                    raw = b"not json" if malformed else canonical(changed)
                    path.write_bytes(raw)
                    try:
                        with self.assertRaises(Denied):
                            self.page(role, actions, sources=sources)
                        self.assertEqual(path.read_bytes(), raw)
                    finally:
                        path.write_bytes(original)

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
                patch.object(Insights, "_current_decision_event", selected), \
                patch.object(Insights, "_delivery_candidate_current", return_value=True):
            page, manifest = self.page()
        self.assertEqual(len(page["events"]), 100)
        self.assertEqual(page["pending_count"], 513)
        self.assertTrue(page["truncated"])
        self.assertTrue(page["count_truncated"])
        self.assertEqual(len(manifest), 100)
        self.assertEqual(self.page()[0]["pending_count"], 1)

    def test_configured_projection_records_own_the_page_scan(self):
        self.decide(0)
        self.decide(1)
        page, manifest = Insights(
            self.root, decision_projection_records=1).shared_decision_page(
                "reviewer", ACTIONS, max_bytes=MAX_FRAME)
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["pending_count"], 2)
        self.assertTrue(page["truncated"])
        self.assertEqual(len(manifest), 1)
        page, manifest = Insights(
            self.root, decision_projection_records=2).shared_decision_page(
                "reviewer", ACTIONS, max_bytes=MAX_FRAME)
        self.assertEqual(len(page["events"]), 2)
        self.assertEqual(len(manifest), 2)

    def test_projection_bound_plus_one_decisions_remain_pageable_after_acknowledgement(self):
        # The configured default's numeric value is covered separately; use a
        # small explicit bound to exercise the same cap-relative behavior.
        bound = 3
        self.insights.decision_projection_records = bound
        self.decide()
        legacy = self.insights.decision_events("reviewer", ACTIONS)[0]
        payload = {k: v for k, v in legacy.items() if k != "ack_token"}
        paths = [Path(f"{i:032x}.json") for i in range(bound + 1)]

        def selected(insights, path, *args, **kwargs):
            return dt.datetime.now(dt.timezone.utc), dict(
                payload, event=path.stem, insight=path.stem)

        delivered = 0
        with patch.object(Path, "glob", return_value=paths), \
                patch.object(Insights, "_current_decision_event", selected), \
                patch.object(Insights, "_delivery_candidate_current", return_value=True):
            while True:
                page, manifest = self.page()
                delivered += len(page["events"])
                if not page["events"]:
                    self.assertEqual(page["pending_count"], 0)
                    break
                self.store.acknowledge(
                    "reviewer", [asdict(entry) for entry in manifest], manifest=manifest)
        self.assertEqual(delivered, bound + 1)

    def test_moved_retained_attention_keeps_complete_pending_total(self):
        for index in range(3):
            self.insights.submit(source="worker", title=f"Attention {index}",
                                 body=f"Attention body {index}", base_snapshot=None,
                                 addressee="worker")
        original = Insights._pending_insight_envelopes_locked
        moved = []

        def stale(insights, configured_roles, role_name, *, paths=None):
            candidates = list(original(insights, configured_roles, role_name, paths=paths))
            if paths is None:
                oldest = min(candidates, key=lambda pair: Insights._decision_key(*pair))
                insight_id = oldest[1]["payload"]["insight"]
                self.insights.revise(insight_id, source="worker", title="Moved attention",
                                     body="Revised during the page scan", base_snapshot=None,
                                     expected_rev=1, addressee="worker")
                moved.append(oldest[0])
            return iter(candidates)

        with patch.object(Insights, "_pending_insight_envelopes_locked", stale):
            page, manifest = Insights(
                self.root, decision_projection_records=1).shared_decision_page(
                    "worker", ACTIONS, max_bytes=MAX_FRAME, configured_roles={"worker"})
        self.assertEqual(len(moved), 1)
        self.assertEqual(page["events"], [])
        self.assertEqual(page["pending_count"], 2)
        self.assertTrue(page["truncated"])
        self.assertTrue(page["byte_truncated"])
        self.assertEqual(manifest, ())
        page, manifest = Insights(
            self.root, decision_projection_records=1).shared_decision_page(
                "worker", ACTIONS, max_bytes=MAX_FRAME, configured_roles={"worker"})
        self.assertEqual(page["pending_count"], 3)
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(len(manifest), 1)

    def test_one_combined_window_across_decisions_and_pending_attention(self):
        self.decide(0, source="worker")
        self.decide(1, source="worker")
        for index in range(2):
            self.insights.submit(source="worker", title=f"Attention {index}",
                                 body=f"Attention body {index}", base_snapshot=None,
                                 addressee="worker")
        page, manifest = Insights(
            self.root, decision_projection_records=2).shared_decision_page(
                "worker", ACTIONS, max_bytes=MAX_FRAME, configured_roles={"worker"})
        self.assertEqual(page["pending_count"], 4)
        self.assertEqual(len(page["events"]), 2)
        self.assertTrue(page["truncated"])
        store = SharedRecords(self.root, {"insight.decision": INSIGHT_DECISION_POLICY,
                                          "insight.pending": INSIGHT_PENDING_POLICY})
        store.acknowledge("worker", [asdict(entry) for entry in manifest], manifest=manifest)
        page, manifest = Insights(
            self.root, decision_projection_records=2).shared_decision_page(
                "worker", ACTIONS, max_bytes=MAX_FRAME, configured_roles={"worker"})
        self.assertEqual(page["pending_count"], 2)
        self.assertEqual(len(page["events"]), 2)
        store.acknowledge("worker", [asdict(entry) for entry in manifest], manifest=manifest)
        page, manifest = Insights(
            self.root, decision_projection_records=2).shared_decision_page(
                "worker", ACTIONS, max_bytes=MAX_FRAME, configured_roles={"worker"})
        self.assertEqual(page["events"], [])
        self.assertEqual(page["pending_count"], 0)
        self.assertEqual(manifest, ())

    def test_pending_attention_past_configured_bound_delivers_after_ack(self):
        for index in range(2):
            self.insights.submit(source="reviewer", title=f"Pending {index}",
                                 body=f"Body {index}", base_snapshot=None,
                                 addressee="reviewer")
        limited = Insights(self.root, decision_projection_records=1)
        pending_store = SharedRecords(self.root, {
            "insight.decision": INSIGHT_DECISION_POLICY,
            "insight.pending": INSIGHT_PENDING_POLICY})
        page, manifest = limited.shared_decision_page(
            "reviewer", (), max_bytes=MAX_FRAME, configured_roles={"reviewer"})
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["pending_count"], 2)
        self.assertTrue(page["truncated"])
        pending_store.acknowledge(
            "reviewer", [asdict(entry) for entry in manifest], manifest=manifest)
        page, manifest = limited.shared_decision_page(
            "reviewer", (), max_bytes=MAX_FRAME, configured_roles={"reviewer"})
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["pending_count"], 1)
        self.assertFalse(page["truncated"])
        pending_store.acknowledge(
            "reviewer", [asdict(entry) for entry in manifest], manifest=manifest)
        page, manifest = limited.shared_decision_page(
            "reviewer", (), max_bytes=MAX_FRAME, configured_roles={"reviewer"})
        self.assertEqual(page["events"], [])
        self.assertEqual(page["pending_count"], 0)
        self.assertEqual(manifest, ())

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

        scanned = []
        original_current = Insights._current_decision_event

        def observe_current(*args, **kwargs):
            # The full-history scan is deliberately unlocked; selected rows are
            # revalidated against their exact source while the lock is held.
            scanned.append(held)
            return original_current(*args, **kwargs)

        with patch("mizu.insights.record_lock", observed_lock), \
                patch.object(Insights, "_current_decision_event", observe_current), \
                patch.object(SharedRecords, "put", observe(SharedRecords.put)), \
                patch.object(SharedRecords, "received", observe(SharedRecords.received)), \
                patch.object(SharedRecords, "freeze", observe(SharedRecords.freeze)):
            self.page()
        self.assertIn(False, scanned)
        self.assertIn(True, scanned)
        self.assertFalse(held)

    def test_archived_inbox_row_is_skipped_during_unlocked_scan(self):
        self.insights.submit(source="reviewer", title="Pending", body="Retained",
                             base_snapshot=None, addressee="worker")
        real = insights_module.safe_read
        removed = []

        def vanish(root, name, maximum):
            if name.startswith("inbox/") and not removed:
                (root / name).unlink()
                removed.append(name)
            return real(root, name, maximum)

        with patch("mizu.insights.safe_read_managed", vanish):
            page, manifest = self.insights.shared_decision_page(
                "worker", ACTIONS, max_bytes=MAX_FRAME, configured_roles={"worker"})
        self.assertEqual(len(removed), 1)
        self.assertEqual(page["events"], [])
        self.assertEqual(page["pending_count"], 0)
        self.assertEqual(manifest, ())

    def test_unlocked_scan_does_not_exclude_a_concurrent_source_write(self):
        self.decide()
        original = Insights._decision_page_envelopes_locked
        started = threading.Event()
        release = threading.Event()
        failures = []

        def blocking(insights, *, validate, keep):
            started.set()
            self.assertTrue(release.wait(5))
            return original(insights, validate=validate, keep=keep)

        def page():
            try:
                with patch.object(Insights, "_decision_page_envelopes_locked", blocking):
                    self.page()
            except BaseException as exc:  # surfaced in the main thread
                failures.append(exc)

        reader = threading.Thread(target=page)
        reader.start()
        try:
            self.assertTrue(started.wait(2))
            committed = threading.Event()

            def write():
                item = self.insights.submit(source="reviewer", title="Concurrent",
                                            body="Committed during the scan", base_snapshot=None)
                self.insights.decide(item["id"], "reject", "Exact reason", "Exact revisit",
                                     "test", expected_rev=1)
                committed.set()

            writer = threading.Thread(target=write)
            writer.start()
            self.assertTrue(committed.wait(2))
            writer.join(5)
            self.assertFalse(writer.is_alive())
        finally:
            release.set()
            reader.join(5)
        self.assertFalse(reader.is_alive())
        self.assertEqual(failures, [])

    def test_unlocked_scan_row_must_still_be_current_at_selection(self):
        self.decide()
        original = Insights._decision_page_envelopes_locked

        def stale(insights, *, validate, keep):
            candidates, total = original(insights, validate=validate, keep=keep)
            for key in candidates:
                event = key.rsplit("/", 1)[-1]
                (self.root / "decision-history" / f"{event}.json").unlink()
            return candidates, total

        with patch.object(Insights, "_decision_page_envelopes_locked", stale):
            page, manifest = self.page()
        self.assertEqual(page["events"], [])
        self.assertEqual(page["pending_count"], 0)
        self.assertEqual(manifest, ())
        # A source that moved before validation leaves no immutable orphan.
        self.assertFalse((self.root / "shared-records").exists())

    def test_new_candidate_moves_before_projection_write(self):
        self.decide()
        real = record_lock

        @contextlib.contextmanager
        def remove_on_lock(root):
            for path in (self.root / "decision-history").glob("*.json"):
                path.unlink()
            with real(root):
                yield

        with patch("mizu.insights.record_lock", remove_on_lock):
            page, manifest = self.page()
        self.assertEqual(page["events"], [])
        self.assertEqual(page["pending_count"], 0)
        self.assertEqual(manifest, ())
        # The source was current during the unlocked scan; the locked
        # revalidation must prevent the now-stale projection from being written.
        self.assertFalse((self.root / "shared-records").exists())

    def test_new_candidate_malformed_before_projection_write_fails_closed(self):
        self.decide()
        real = record_lock

        @contextlib.contextmanager
        def corrupt_on_lock(root):
            for path in (self.root / "decision-history").glob("*.json"):
                path.write_bytes(b"not json")
            with real(root):
                yield

        with patch("mizu.insights.record_lock", corrupt_on_lock), \
                self.assertRaises(Denied):
            self.page()
        self.assertFalse((self.root / "shared-records").exists())

    def test_unlocked_scan_source_becoming_malformed_fails_closed(self):
        self.decide()
        original = Insights._decision_page_envelopes_locked

        def broken(insights, *, validate, keep):
            candidates, total = original(insights, validate=validate, keep=keep)
            for key in candidates:
                event = key.rsplit("/", 1)[-1]
                (self.root / "decision-history" / f"{event}.json").write_bytes(b"not json")
            return candidates, total

        with patch.object(Insights, "_decision_page_envelopes_locked", broken), \
                self.assertRaises(Denied):
            self.page()
        # Malformed source fails closed before any new projection is written.
        self.assertFalse((self.root / "shared-records").exists())

    def test_unlocked_scan_pending_revision_is_revalidated(self):
        item = self.insights.submit(source="reviewer", title="Pending", body="v1",
                                    base_snapshot=None, addressee="worker")
        original = Insights._pending_insight_envelopes_locked

        def stale(insights, configured_roles, role_name, *, paths=None):
            candidates = list(original(insights, configured_roles, role_name, paths=paths))
            if paths is None:
                self.insights.revise(item["id"], source="reviewer", title="Pending",
                                     body="v2", base_snapshot=None, expected_rev=1,
                                     addressee="worker")
                return candidates
            return iter(candidates)

        with patch.object(Insights, "_pending_insight_envelopes_locked", stale):
            page, manifest = self.insights.shared_decision_page(
                "worker", ACTIONS, max_bytes=MAX_FRAME, configured_roles={"worker"})
        self.assertEqual(page["events"], [])
        self.assertEqual(page["pending_count"], 0)
        self.assertEqual(manifest, ())
        self.assertFalse((self.root / "shared-records").exists())

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
