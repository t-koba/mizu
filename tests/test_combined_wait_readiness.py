"""Shared CI/proposal readiness: one authoritative traversal, fail-closed routes."""
import dataclasses
from unittest.mock import Mock, patch

from support import Fixture
from mizu import ci_waits, proposal_waits, vcs
from mizu.distribution import public_source_identity
from mizu.errors import Denied
from mizu.fs import read_json, write_json
from mizu.project import Project
from mizu.publication import Publication
from mizu.runtime import _receipt_wait_due
from mizu.vcs_proposal_poll import poll_proposals

CI_SHA = "b" * 40
PROP_SHA = "b" * 40


class CombinedFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(
            self.config,
            vcs={**self.config.vcs, "poll_enabled": True,
                 "poll_proposals_enabled": True,
                 "command": ["configured-adapter"],
                 "poll_max_branches": 10})
        self.project = Project(self.config, "sample")
        self.store = self.project.work_items
        # One publication serves both routes so the shared scan sees both kinds.
        publication = Publication(
            self.project.root, "a" * 40, "c" * 64, "published", "push",
            projection=public_source_identity({}))
        intent, _ = publication.prepare("approval", "run")
        publication.mark_dispatch()
        self.receipt = publication.complete(intent, CI_SHA)
        self.ci_wait = {"kind": "ci", "publication_id": publication.id,
                        "checks": ["unit", "lint"]}
        self.prop_wait = {"kind": "proposal_merged",
                          "proposal_id": "forge:owner/repo#1",
                          "publication_id": publication.id}
        self.prop_row = {"id": self.prop_wait["proposal_id"], "state": "open",
                         "draft": False,
                         "head": {"repo": "owner/repo",
                                  "ref": "refs/heads/published", "sha": CI_SHA},
                         "base": {"repo": "owner/repo", "ref": "main",
                                  "sha": "d" * 40}}
        self.clock = 1000
        self.poll_state = {}
        self.ci_state = {}
        self.serial = 0

    def ci_poll(self, rows=None, reader=None):
        self.clock += 30
        if reader is None:
            rows = ([{"check": c, "sha": CI_SHA, "state": "success",
                      "run": "run-" + c} for c in ["lint", "unit"]]
                    if rows is None else rows)
            reader = Mock(return_value={"checks": rows})
        return ci_waits.poll(self.config, self.project, self.ci_state,
                             now=self.clock, interval=15, reader=reader)

    def prop_poll(self, rows=None, **metadata):
        self.clock += 30
        return poll_proposals(
            self.config, self.project, self.poll_state,
            now=self.clock, interval=15,
            reader=Mock(return_value={
                "proposals": [self.prop_row] if rows is None else rows,
                "complete": True, **metadata}))

    def seed(self, key=None, actor="worker"):
        self.serial += 1
        key = key or f"item-{self.serial}"
        return self.store.create(
            actor=actor, idempotency_key=key,
            title="Observe waits")["record"]["work_item"]

    def block_ci(self, key=None, actor="worker"):
        seed = self.seed(key=key, actor=actor)
        return self.store.transition(
            seed["id"], expected_rev=seed["rev"], status="blocked",
            actor=actor, wait=self.ci_wait)

    def block_prop(self, key=None, actor="worker"):
        seed = self.seed(key=key, actor=actor)
        return self.store.transition(
            seed["id"], expected_rev=seed["rev"], status="blocked",
            actor=actor, wait=self.prop_wait)


class CombinedWaitReadinessTests(CombinedFixture):
    def test_ci_positive_plus_proposal_corruption_returns_true(self):
        ci_item = self.block_ci("ci-good")
        self.assertEqual(self.ci_poll()["observed"], 1)
        self.assertEqual(len(ci_waits.pending(self.store, "worker")), 1)
        # Proposal waiter with no record but a stray receipt is corrupt.
        self.prop_poll()
        prop_item = self.block_prop("prop-bad")
        shared = proposal_waits.shared_store(self.store)
        receipt_name = shared._receipt_name(
            "worker", proposal_waits._record_id(prop_item))
        receipt_path = self.project.root / receipt_name
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_bytes(b"stray")
        with self.assertRaises(Denied):
            proposal_waits.pending(self.store, "worker")
        # Shared readiness still wakes on the CI positive and discards error.
        self.assertTrue(_receipt_wait_due(self.store, "worker", limit=1))
        # Direct CI route still sees its event.
        self.assertEqual(len(ci_waits.pending(self.store, "worker")), 1)

    def test_ci_empty_plus_proposal_corruption_raises(self):
        self.prop_poll()
        prop_item = self.block_prop("prop-only")
        shared = proposal_waits.shared_store(self.store)
        receipt_name = shared._receipt_name(
            "worker", proposal_waits._record_id(prop_item))
        receipt_path = self.project.root / receipt_name
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_bytes(b"stray")
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        with self.assertRaises(Denied):
            _receipt_wait_due(self.store, "worker", limit=1)

    def test_unexpected_proposal_error_defers_only_for_ci_short_circuit(self):
        self.prop_poll()
        self.block_prop("proposal-error")
        with patch.object(proposal_waits, "evidence",
                          side_effect=TypeError("invalid proposal evidence")):
            with self.assertRaisesRegex(TypeError, "invalid proposal evidence"):
                _receipt_wait_due(self.store, "worker", limit=1)

        self.block_ci("ci-after-proposal-error")
        self.assertEqual(self.ci_poll()["observed"], 1)
        with patch.object(proposal_waits, "evidence",
                          side_effect=TypeError("invalid proposal evidence")):
            # The old CI-first `or` would not inspect proposal evidence when
            # this exact CI route produces a wake.
            self.assertTrue(_receipt_wait_due(self.store, "worker", limit=1))

    def test_per_item_ci_refusal_falls_back_to_good(self):
        bad = self.block_ci("bad")
        good = self.block_ci("good")
        self.assertEqual(self.ci_poll()["observed"], 2)
        self.assertEqual(len(ci_waits.pending(self.store, "worker")), 2)
        path = ci_waits._path(self.store, bad)
        record = read_json(path)
        record["conclusion"] = "tampered"
        write_json(path, record)
        # Direct CI route skips the broken waiter and still finds the good one.
        self.assertEqual(
            [e["work_item"] for e in ci_waits.pending(self.store, "worker")],
            [good["id"]])
        # Shared readiness does the same in one scan.
        self.assertTrue(_receipt_wait_due(self.store, "worker", limit=1))
        ci_state = ci_waits.readiness_init(self.store, "worker", limit=10)
        prop_state = proposal_waits.readiness_init(self.store, "worker", limit=10)
        from mizu.fs import lock as fs_lock
        with fs_lock(self.store.root / "locks" / "insights.lock"):
            for event_id, event in self.store._iter_item_events(recover=False):
                kind, seed = self.store._seed_for_event(
                    event_id, event, readonly=True)
                if kind != "seed":
                    continue
                item = self.store._get(
                    seed["id"], seed_event=event,
                    history_revisions=self.store._history_revisions(
                        seed["id"], recover=False))
                ci_waits.readiness_feed(ci_state, self.store, item)
                proposal_waits.readiness_feed(prop_state, self.store, item)
        events = ci_waits.readiness_events(ci_state)
        self.assertEqual([e["work_item"] for e in events], [good["id"]])

    def test_late_inventory_failure_after_candidate_still_raises(self):
        item = self.block_ci("late-good")
        self.assertEqual(self.ci_poll()["observed"], 1)
        original = self.store._iter_item_events

        def fail_late(*, recover):
            yielded = False
            for row in original(recover=recover):
                yield row
                if not yielded:
                    yielded = True
                    raise Denied("late inventory failure")

        with patch.object(self.store, "_iter_item_events",
                          side_effect=fail_late):
            with self.assertRaises(Denied):
                _receipt_wait_due(self.store, "worker", limit=1)

    def test_acknowledged_events_are_not_due(self):
        item = self.block_ci("ack-me")
        self.assertEqual(self.ci_poll()["observed"], 1)
        self.assertTrue(_receipt_wait_due(self.store, "worker", limit=1))
        events, manifest = ci_waits.delivery(self.store, "worker", limit=10)
        self.assertEqual(len(events), 1)
        shared = ci_waits.shared_store(self.store)
        from dataclasses import asdict
        shared.acknowledge("worker", [asdict(e) for e in manifest],
                           manifest=manifest)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.assertFalse(_receipt_wait_due(self.store, "worker", limit=1))

    def test_route_order_and_limits(self):
        items = [self.block_ci(f"ord-{i}") for i in range(3)]
        self.assertEqual(self.ci_poll()["observed"], 3)
        # Oldest-first order is preserved; limit truncates to the prefix.
        ci_state = ci_waits.readiness_init(self.store, "worker", limit=2)
        prop_state = proposal_waits.readiness_init(self.store, "worker", limit=2)
        from mizu.fs import lock as fs_lock
        with fs_lock(self.store.root / "locks" / "insights.lock"):
            for event_id, event in self.store._iter_item_events(recover=False):
                kind, seed = self.store._seed_for_event(
                    event_id, event, readonly=True)
                if kind != "seed":
                    continue
                got = self.store._get(
                    seed["id"], seed_event=event,
                    history_revisions=self.store._history_revisions(
                        seed["id"], recover=False))
                ci_waits.readiness_feed(ci_state, self.store, got)
                proposal_waits.readiness_feed(prop_state, self.store, got)
        events = ci_waits.readiness_events(ci_state)
        self.assertEqual(len(events), 2)
        self.assertEqual([e["work_item"] for e in events],
                         [items[0]["id"], items[1]["id"]])
        self.assertEqual(events, ci_waits.pending(self.store, "worker", limit=2))
        # Proposal ordering uses the same oldest-first timestamp key.
        self.prop_poll()
        p1 = self.block_prop("p-ord-1")
        # Second proposal needs its own registered row.
        row2 = {**self.prop_row, "id": "forge:owner/repo#2"}
        self.prop_poll(rows=[self.prop_row, row2])
        wait2 = {**self.prop_wait, "proposal_id": row2["id"]}
        seed2 = self.seed(key="p-ord-2")
        p2 = self.store.transition(
            seed2["id"], expected_rev=seed2["rev"], status="blocked",
            actor="worker", wait=wait2)
        row2_merged = {**row2, "state": "merged"}
        row1_merged = {**self.prop_row, "state": "merged"}
        self.prop_poll(rows=[row1_merged, row2_merged])
        prop_state2 = proposal_waits.readiness_init(
            self.store, "worker", limit=1)
        from mizu.fs import lock as fs_lock2
        with fs_lock2(self.store.root / "locks" / "insights.lock"):
            for event_id, event in self.store._iter_item_events(recover=False):
                kind, seed = self.store._seed_for_event(
                    event_id, event, readonly=True)
                if kind != "seed":
                    continue
                got = self.store._get(
                    seed["id"], seed_event=event,
                    history_revisions=self.store._history_revisions(
                        seed["id"], recover=False))
                proposal_waits.readiness_feed(prop_state2, self.store, got)
        prop_events = proposal_waits.readiness_events(prop_state2)
        self.assertEqual(len(prop_events), 1)
        self.assertEqual(prop_events[0]["work_item"], p1["id"])
        self.assertEqual(prop_events,
                         proposal_waits.pending(self.store, "worker", limit=1))

    def test_no_writes_during_readiness(self):
        self.block_ci("no-write")
        self.prop_poll()
        self.block_prop("no-write-prop")
        self.prop_poll(rows=[{**self.prop_row, "state": "merged"}])
        before = sorted(p.relative_to(self.project.root)
                        for p in self.project.root.rglob("*") if p.is_file())
        with (patch("mizu.ci_waits.sync_dir",
                    side_effect=AssertionError("no sync")),
              patch("mizu.ci_waits.write_json",
                    side_effect=AssertionError("no writes")),
              patch("mizu.shared_records.SharedRecords.put",
                    side_effect=AssertionError("no put")),
              patch("mizu.shared_records.SharedRecords.acknowledge",
                    side_effect=AssertionError("no ack"))):
            self.assertTrue(_receipt_wait_due(self.store, "worker", limit=1))
        after = sorted(p.relative_to(self.project.root)
                       for p in self.project.root.rglob("*") if p.is_file())
        self.assertEqual(before, after)

    def test_exactly_one_traversal_and_ready_work_unchanged(self):
        self.block_ci("count-me")
        self.assertEqual(self.ci_poll()["observed"], 1)
        ready_before = self.store.page_ready(limit=1)
        self.assertGreaterEqual(ready_before["total"], 0)
        original = self.store._iter_item_events
        calls = {"n": 0}

        def counting(*, recover):
            calls["n"] += 1
            yield from original(recover=recover)

        with patch.object(self.store, "_iter_item_events",
                          side_effect=counting):
            with patch.object(self.store, "page_ready",
                              side_effect=AssertionError("ready-work separate")):
                self.assertTrue(_receipt_wait_due(self.store, "worker", limit=1))
        self.assertEqual(calls["n"], 1)
        # Ready-work validation still runs on its own path with recovery.
        ready_after = self.store.page_ready(limit=1)
        self.assertEqual(ready_before["signature"], ready_after["signature"])
