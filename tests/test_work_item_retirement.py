"""Bounded exhaustion recovery in disposable stores; no deployment or live ledger."""
import concurrent.futures
import copy
import dataclasses
import hashlib
import json
import os
from unittest.mock import patch

from support import Fixture
from mizu import work_item_events
from mizu.errors import Denied
from mizu.fs import canonical, digest, write_json
from mizu.storage import audit
from mizu.work_items import MAX_REVISIONS


class RetirementTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        proposal = self.project.insights.submit(source="reviewer", title="Exhaustion",
                                                body="Retain the scope", base_snapshot=None)
        self.seed = self.store.create(
            idempotency_key="exhaustion", title="Bounded lifecycle", actor="worker",
            origin={"insight_id": proposal["id"], "rev": 1}, scope="Keep accepted scope",
            description="Retain immutable description")["record"]["work_item"]
        self.item_id = self.seed["id"]
        self.directory = self.project.root / "work-items" / self.item_id
        self.path = self.directory / "retirement.json"

    def change(self, status="dropped", expected_rev=MAX_REVISIONS, **kwargs):
        return self.store.transition(self.item_id, expected_rev=expected_rev, status=status,
                                     actor=kwargs.pop("actor", "worker"),
                                     reason=kwargs.pop("reason", "Retire exhausted tracking; not completion"),
                                     **kwargs)

    def exhaust(self, *, blocked=False):
        # Bulk-write only valid ordinary fixture history, as the revision-bound
        # test does. Exercise the real last CAS from 255 to 256, not 256 CASes.
        self.change("doing", expected_rev=1)
        snapshot = self.project.snapshots.get()
        template = self.change("doing", expected_rev=2, snapshot_id=snapshot["id"])
        previous = template
        for rev in range(4, MAX_REVISIONS):
            status = "open" if rev % 2 == 0 else "doing"
            record = {**template, "rev": rev, "status": status,
                      "holder": "worker" if status == "doing" else None,
                      "transition": {"from": previous["status"], "to": status}}
            write_json(self.directory / f"{rev}.json", record, exclusive=True)
            previous = record
        if blocked:
            self.last = self.change("blocked", expected_rev=MAX_REVISIONS - 1,
                                    wait={"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"})
        else:
            self.last = self.change("doing", expected_rev=MAX_REVISIONS - 1,
                                    snapshot_id=snapshot["id"])
        self.assertEqual(self.last["rev"], 256)
        self.assertEqual(self.store.get(self.item_id), self.last)
        self.assertFalse(self.path.exists())
        return self.last

    def tree(self):
        return {p.name: p.read_bytes() for p in self.directory.iterdir()}

    def test_boundary_history_projection_slot_release_and_terminal_event(self):
        self.exhaust()
        other = self.store.create(idempotency_key="next", title="Next work", actor="worker")["record"]["work_item"]
        with self.assertRaisesRegex(Denied, "slots full"):
            self.store.transition(other["id"], expected_rev=1, status="doing", actor="worker")
        before = self.tree()
        with patch.object(self.store, "_wake_terminal", wraps=self.store._wake_terminal) as wake:
            retired = self.change(reason="  Retire exhausted tracking; not completion  ")
        wake.assert_called_once_with()
        self.assertEqual(retired["status"], "dropped")
        self.assertEqual(retired["rev"], MAX_REVISIONS)
        self.assertIsNone(retired["holder"])
        self.assertIsNone(retired["wait"])
        for field in ("origin", "scope", "delivery", "description", "title", "created_at"):
            self.assertEqual(retired[field], self.last[field])
        after = self.tree()
        self.assertEqual({k: v for k, v in after.items() if k != "retirement.json"}, before)
        self.assertNotIn("257.json", after)
        self.assertEqual(self.store.get(self.item_id), retired)
        terminal = json.loads(self.path.read_bytes())
        expected_hash = hashlib.sha256()
        for record in [self.seed] + [json.loads(before[f"{rev}.json"]) for rev in range(2, 257)]:
            expected_hash.update(canonical(record) + b"\n")
        self.assertEqual(terminal["chain_sha256"], expected_hash.hexdigest())
        self.assertEqual(terminal["request_sha256"], digest(canonical(terminal["request"])))
        history = []
        cursor = 0
        while True:
            page = self.store.explain(self.item_id, after_rev=cursor, limit=100)
            self.assertEqual(page["retirement"], terminal)
            self.assertEqual(page["history_total"], 256)
            self.assertEqual(page["item"], retired)
            self.assertIsNone(page["doing_since"])
            history.extend(page["history"])
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
        self.assertEqual([r["rev"] for r in history], list(range(1, 257)))
        self.assertEqual(history[-1], self.last)
        self.assertEqual(self.store.explain(self.item_id, after_rev=256)["history"], [])
        for page in (self.store.page_open(), self.store.page_open_readonly(), self.store.page_ready()):
            self.assertNotIn(self.item_id, [r["id"] for r in page["items"]])
        self.assertEqual(self.store.page_closed_readonly()["items"][0]["id"], self.item_id)
        self.assertEqual(work_item_events.pending(self.store, "reviewer", ["dropped"])["pending"], 1)
        claimed = self.store.transition(other["id"], expected_rev=1, status="doing", actor="worker")
        self.assertEqual(claimed["holder"], "worker")

    def test_exact_retry_and_concurrent_conflicts_are_immutable(self):
        self.exhaust()
        # A response may be lost after the exclusive publication has committed.
        from mizu.work_item_retirement import write_json as original_write
        def uncertain(*args, **kwargs):
            original_write(*args, **kwargs)
            raise OSError("response lost")
        with patch("mizu.work_item_retirement.write_json", side_effect=uncertain):
            with self.assertRaisesRegex(OSError, "response lost"):
                self.change()
        frozen = self.tree()
        expected = self.store.get(self.item_id)
        with patch("mizu.work_item_retirement.write_json", side_effect=AssertionError("retry wrote")), \
                patch.object(self.store, "_wake_terminal", side_effect=AssertionError("retry woke")):
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda _: self.change(), range(3)))
        self.assertTrue(all(r == expected for r in results))
        for changes in ({"reason": "Different"}, {"actor": "reviewer"}, {"expected_rev": 255},
                        {"status": "done"}, {"status": "open"}, {"status": "doing"},
                        {"scope": self.last["scope"]}, {"smoke_refs": []}, {"reason": None}):
            with self.subTest(changes=changes), self.assertRaises(Denied):
                self.change(**changes)
        self.assertEqual(self.tree(), frozen)

    def test_concurrent_first_writers_have_one_winner(self):
        self.exhaust()
        def attempt(reason):
            try:
                return self.change(reason=reason)
            except Denied:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["Retire A; not completion", "Retire B; not completion"]))
        self.assertEqual(sum(r is not None for r in results), 1)
        winner = next(r for r in results if r)
        self.assertEqual(self.change(reason=winner["reason"]), winner)
        self.assertFalse((self.directory / "257.json").exists())

    def test_blocked_retirement_clears_wait_and_hold(self):
        self.exhaust(blocked=True)
        self.assertIsNotNone(self.last["wait"])
        retired = self.change()
        self.assertIsNone(retired["wait"])
        self.assertIsNone(retired["holder"])
        self.assertFalse(self.store.page_open_readonly()["items"])

    def test_runtime_grant_and_actor_binding(self):
        self.exhaust()
        ctx = self.context("worker")
        args = {"id": self.item_id, "expected_rev": 256, "status": "dropped", "reason": "Not completion"}
        with self.assertRaises(Denied):
            ctx.handle("work_items_transition", {**args, "actor": "reviewer"})
        role = ctx.role
        ctx.role = dataclasses.replace(role, capabilities=tuple(c for c in role.capabilities
                                                              if c != "work_items_transition"))
        with self.assertRaisesRegex(Denied, "no capability"):
            ctx.handle("work_items_transition", args)
        self.assertFalse(self.path.exists())
        ctx.role = role
        self.assertEqual(ctx.handle("work_items_transition", args)["actor"], "worker")

    def assert_invalid_readonly(self):
        frozen = self.tree()
        with patch("mizu.work_items.os.unlink", side_effect=AssertionError("read-only recovery")), \
                patch("mizu.work_items.write_json", side_effect=AssertionError("read-only write")):
            for read in (lambda: list(self.store._iter_chain(self.item_id)),
                         self.store.page_open_readonly, self.store.page_closed_readonly):
                with self.assertRaises(Denied):
                    read()
            self.assertEqual(audit(self.project)["work_item_events_skipped"], 1)
        self.assertEqual(self.tree(), frozen)
        with self.assertRaises(Denied):
            self.change()
        self.assertEqual(self.tree(), frozen)

    def test_malformed_sidecars_and_wrong_or_gapped_chains_fail_closed(self):
        self.exhaust()
        self.change()
        original = json.loads(self.path.read_bytes())
        mutations = [{}, {**original, "schema": True}, {**original, "schema": 2},
                     {**original, "chain_sha256": "0" * 64},
                     {**original, "request_sha256": "0" * 64},
                     {**original, "created_at": "yesterday"},
                     {**original, "extra": "not allowed"}]
        for field, value in (("id", "wi_" + "0" * 32), ("expected_rev", 255),
                             ("status", "done"), ("actor", "Not an actor"), ("reason", ""),
                             ("reason", " Not normalized ")):
            record = copy.deepcopy(original)
            record["request"][field] = value
            record["request_sha256"] = digest(canonical(record["request"]))
            mutations.append(record)
        for record in mutations:
            with self.subTest(record=record):
                write_json(self.path, record)
                self.assert_invalid_readonly()
        self.path.write_text('{"schema":', encoding="utf-8")
        self.assert_invalid_readonly()
        write_json(self.path, original)
        changed_path = self.directory / "4.json"
        saved = changed_path.read_bytes()
        record = json.loads(saved)
        record["reason"] = "Otherwise valid, but not the chain retired"
        write_json(changed_path, record)
        self.assert_invalid_readonly()
        changed_path.write_bytes(saved)
        changed_path.unlink()
        self.assert_invalid_readonly()
        changed_path.write_bytes(saved)
        self.assertEqual(self.store.get(self.item_id)["status"], "dropped")
        self.assertEqual(audit(self.project)["work_item_events_skipped"], 0)

    def test_only_exhausted_nonterminal_chains_can_retire(self):
        # A fabricated early sidecar must never be ignored by a normal CAS.
        early = {"schema": 1,
                 "request": {"id": self.item_id, "expected_rev": 256, "status": "dropped",
                             "actor": "worker", "reason": "Not completion"},
                 "chain_sha256": digest(canonical(self.seed) + b"\n"),
                 "created_at": self.seed["updated_at"]}
        early["request_sha256"] = digest(canonical(early["request"]))
        write_json(self.path, early)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        with self.assertRaises(Denied):
            self.change("doing", expected_rev=1)
        self.path.unlink()
        with self.assertRaises(Denied):
            self.change()  # Actual current revision is still 1, not 256.
        self.assertFalse(self.path.exists())
        self.exhaust()
        # Neither arbitrary continuation nor completion is an exhaustion escape.
        for changes in ({"status": "done"}, {"status": "open"}, {"status": "blocked"},
                        {"reason": " "}, {"reason": "x" * 4001}, {"expected_rev": 257},
                        {"wait": {"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"}},
                        {"snapshot_id": self.project.snapshots.get()["id"]}):
            with self.subTest(changes=changes), self.assertRaises(Denied):
                self.change(**changes)
        self.assertFalse(self.path.exists())
        # A terminal ordinary revision at the boundary is already closed, and
        # cannot gain another terminal event or bypass completion verification.
        terminal = {**self.last, "status": "dropped", "holder": None,
                    "delivery": self.store._read(f"work-items/{self.item_id}/255.json")["delivery"],
                    "transition": {"from": "doing", "to": "dropped"}}
        write_json(self.directory / "256.json", terminal)
        self.assertEqual(self.store.get(self.item_id)["status"], "dropped")
        with self.assertRaises(Denied):
            self.change()
        self.assertFalse(self.path.exists())

    def test_unsafe_sidecars_and_recognized_temporary_recovery(self):
        self.exhaust()
        self.change()
        content = self.path.read_bytes()
        orphan = self.directory / ".new-abcdefgh"
        orphan.write_text("uncommitted", encoding="utf-8")
        with patch("mizu.work_items.os.unlink", side_effect=AssertionError("read-only recovery")):
            self.assertEqual(self.store.page_closed_readonly()["items"][0]["id"], self.item_id)
            self.assertEqual(audit(self.project)["work_item_events_skipped"], 1)
        self.assertTrue(orphan.exists())
        self.store.get(self.item_id)
        self.assertFalse(orphan.exists())
        try:
            os.link(self.path, orphan)
        except (OSError, NotImplementedError):
            self.skipTest("Hard links unavailable")
        # An old post-link atomic-write alias is a recognized committed target;
        # read-only views must not fix it. The locked read removes only alias.
        with self.assertRaises(Denied):
            self.store.page_closed_readonly()
        self.assertTrue(orphan.exists())
        self.assertEqual(self.store.get(self.item_id)["status"], "dropped")
        self.assertFalse(orphan.exists())
        self.assertEqual(self.path.read_bytes(), content)
        outside = self.project.root / "retirement-copy.json"
        os.link(self.path, outside)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        outside.unlink()
        self.path.unlink()
        try:
            os.symlink(outside, self.path)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(Denied):
            self.store.get(self.item_id)

    def test_retired_exhausted_item_releases_smoke_pins_like_dropped(self):
        from mizu import smoke_retention
        from mizu.fs import now
        smoke_id = "a" * 32
        data = self.project.config.data
        smoke_retention.publish(data, smoke_id, {"status": "pass", "time": now(),
                                                 "result": {"synthetic": True}}, None)
        # Exhaust with item-level smoke pins: the first CAS sets them, the rest inherit.
        first = self.store.transition(self.item_id, expected_rev=1, status="doing",
                                      actor="worker", smoke_refs=[smoke_id])
        self.assertEqual(first["smoke_refs"], [smoke_id])
        snapshot = self.project.snapshots.get()
        template = self.store.transition(self.item_id, expected_rev=2, status="doing",
                                         actor="worker", snapshot_id=snapshot["id"])
        self.assertEqual(template["smoke_refs"], [smoke_id])
        previous = template
        for rev in range(4, MAX_REVISIONS):
            status = "open" if rev % 2 == 0 else "doing"
            record = {**template, "rev": rev, "status": status,
                      "holder": "worker" if status == "doing" else None,
                      "transition": {"from": previous["status"], "to": status}}
            write_json(self.directory / f"{rev}.json", record, exclusive=True)
            previous = record
        self.last = self.store.transition(self.item_id, expected_rev=MAX_REVISIONS - 1,
                                          status="doing", actor="worker",
                                          snapshot_id=snapshot["id"])
        self.assertEqual(self.last["rev"], MAX_REVISIONS)
        self.assertEqual(self.last["smoke_refs"], [smoke_id])
        # The live exhausted item pins its refs through the read-only inventory.
        self.assertIn(smoke_id, smoke_retention.run(data)["pinned"])
        # The read-only inventory performs no recovery or writes.
        with patch("mizu.work_items.os.unlink", side_effect=AssertionError("read-only recovery")), \
                patch("mizu.work_items.write_json", side_effect=AssertionError("read-only write")):
            self.assertIn(smoke_id, smoke_retention.run(data)["pinned"])
        retired = self.change()
        self.assertEqual(retired["status"], "dropped")
        self.assertIsNone(retired["holder"])
        self.assertIsNone(retired["wait"])
        self.assertEqual(self.store.get(self.item_id)["status"], "dropped")
        # The committed sidecar itself stays within the shared safe-read bound,
        # so what the write committed remains readable; the projection does too.
        from mizu.fs import PREVIEW_BYTES
        sidecar = json.loads(self.path.read_bytes())
        self.assertLessEqual(len(canonical(sidecar)), PREVIEW_BYTES)
        self.assertLessEqual(len(canonical(retired)), PREVIEW_BYTES)
        # Historical delivery stays replayable via the storage audit.
        self.assertEqual(audit(self.project)["work_item_events_skipped"], 0)
        # After retirement the effective dropped state pins nothing, read-only.
        with patch("mizu.work_items.os.unlink", side_effect=AssertionError("read-only recovery")), \
                patch("mizu.work_items.write_json", side_effect=AssertionError("read-only write")):
            self.assertNotIn(smoke_id, smoke_retention.run(data)["pinned"])
        self.assertNotIn(smoke_id, smoke_retention.run(data)["pinned"])
        # An ordinary dropped item carrying the same refs releases identically.
        other = self.store.create(idempotency_key="ordinary-pin", title="Ordinary",
                                  actor="worker", smoke_refs=[smoke_id])["record"]["work_item"]
        self.assertIn(smoke_id, smoke_retention.run(data)["pinned"])
        self.store.transition(other["id"], expected_rev=1, status="dropped",
                              actor="worker", reason="Not needed")
        self.assertNotIn(smoke_id, smoke_retention.run(data)["pinned"])
