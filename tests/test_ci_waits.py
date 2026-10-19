"""Offline receipt-pinned CI waits: no publication or real network adapter."""
import dataclasses
import json
from unittest.mock import Mock, patch

from support import Fixture, ScriptDriver
from mizu import ci_waits, vcs
from mizu.errors import Denied
from mizu.fs import PREVIEW_BYTES, canonical, read_json, write_json
from mizu.project import Project
from mizu.protocol import DEFINITIONS, validate
from mizu.publication import Publication
from mizu.runtime import Engine, _role_events_due, poll_project_vcs, refresh_poll_state, should_run

SHA = "b" * 40


class CiWaitTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "poll_enabled": True, "command": ["configured-adapter"], "poll_max_branches": 2})
        self.project = Project(self.config, "sample")
        self.store = self.project.work_items
        self.publication = Publication(self.project.root, "a" * 40, "c" * 64, "published", "push")
        intent, _ = self.publication.prepare("approval", "run")
        self.publication.mark_dispatch()
        self.receipt = self.publication.complete(intent, SHA)
        self.wait = {"kind": "ci", "publication_id": self.publication.id, "checks": ["unit", "lint"]}
        self.clock = 1000
        self.state = {}

    def block(self, key="item", actor="worker", wait=None):
        seed = self.store.create(actor=actor, idempotency_key=key, title="Observe CI")["record"]["work_item"]
        return self.store.transition(seed["id"], expected_rev=1, status="blocked", actor=actor,
                                     wait=self.wait if wait is None else wait)

    def rows(self, state="success", sha=SHA):
        return [{"check": c, "sha": sha, "state": state, "run": "run-" + c}
                for c in ["lint", "unit"]]

    def poll(self, rows=None, reader=None):
        self.clock += 30
        if reader is None:
            reader = Mock(return_value={"checks": self.rows() if rows is None else rows})
        result = ci_waits.poll(self.config, self.project, self.state,
                              now=self.clock, interval=15, reader=reader)
        return result, reader

    def test_registration_is_work_item_only_and_pins_valid_receipt(self):
        item = self.block()
        self.assertEqual(item["wait"], {**self.wait, "checks": ["lint", "unit"],
                                        "branch": "published", "sha": SHA})
        self.assertEqual(self.store.get(item["id"])["wait"], item["wait"])
        self.assertEqual(self.store.page_open()["items"][0]["wait"], item["wait"])
        args = {"id": item["id"], "expected_rev": 2, "status": "blocked", "wait": self.wait}
        validate(args, DEFINITIONS["work_items_transition"][1])
        with self.assertRaises(Denied):
            validate({"id": "proposal", "action": "defer", "reason": "wait", "rev": 1,
                      "wait": self.wait}, DEFINITIONS["decide"][1])
        with self.assertRaises(Denied):
            self.project.insights._normalize_wait(self.wait, "defer")
        # Stored wait reads revalidate the completed receipt, not just ID syntax.
        self.publication.path("receipt").unlink()
        with self.assertRaises(Denied):
            self.store.get(item["id"])

    def test_registration_refuses_invalid_bounds_uncertain_receipts_and_disabled_polling(self):
        bad = [[], ["unit"] * 2, [""], ["x\x85"], ["x\n"], ["x" * 257], [1], ["x\ud800"],
               [str(i) for i in range(65)], ["x" * 256 for _ in range(64)]]
        for i, checks in enumerate(bad):
            with self.subTest(checks=repr(checks)[:80]), self.assertRaises(Denied):
                self.block(str(i), wait={**self.wait, "checks": checks})
        for extra in ({"branch": "other"}, {"sha": "d" * 40}, {"publication_id": "0" * 64}):
            with self.subTest(extra=extra), self.assertRaises(Denied):
                self.block(str(extra), wait={**self.wait, **extra})
        for changes in ({"poll_enabled": False}, {"command": []}):
            with patch.dict(self.store.vcs, changes), self.assertRaises(Denied):
                self.block(str(changes))
        self.publication.path("receipt").unlink()  # dispatched, uncertain, not completed
        with self.assertRaises(Denied):
            self.block("uncertain")

    def test_moved_branch_is_never_used_instead_of_receipt_sha(self):
        item = self.block()
        # Adapter serves only the newer head: even all-green is unresolved.
        result, reader = self.poll(self.rows(sha="d" * 40))
        self.assertEqual(result["observed"], 0)
        reader.assert_called_once_with(self.config.vcs, "status", {"branch": "published", "sha": SHA})
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        result, _ = self.poll()
        self.assertEqual(result["observed"], 1)
        self.assertEqual(ci_waits.pending(self.store, "worker")[0]["sha"], SHA)
        self.assertEqual(self.store.get(item["id"]), item)

    def test_only_full_unique_declared_terminal_set_resolves(self):
        item = self.block()
        good = self.rows()
        cases = [[], good[:1], [good[0], {**good[1], "state": "pending"}],
                 [good[0], {**good[1], "state": "unknown"}], good + [good[0]],
                 good + [{**good[0], "sha": "d" * 40}],
                 [good[0], {**good[1], "sha": "d" * 40}]]
        for rows in cases:
            with self.subTest(rows=rows):
                result, _ = self.poll(rows)
                self.assertEqual(result["observed"], 0)
                self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        # An undeclared row is not a requirement and cannot decide the conclusion.
        result, _ = self.poll(good + [{"check": "other", "state": "pending", "sha": SHA}])
        self.assertEqual(result["observed"], 1)
        self.assertEqual(ci_waits.pending(self.store, "worker")[0]["conclusion"], "success")
        self.assertEqual(self.store.get(item["id"])["status"], "blocked")

    def test_terminal_failure_also_wakes_without_transition_and_ack_deduplicates(self):
        item = self.block()
        result, _ = self.poll([self.rows()[0], {**self.rows()[1], "state": "failure"}])
        self.assertEqual(result["observed"], 1)
        events = ci_waits.pending(self.store, "worker")
        self.assertEqual((len(events), events[0]["conclusion"], events[0]["rev"]), (1, "failure", 2))
        self.assertEqual(events[0]["publication_id"], self.publication.id)
        self.assertEqual(events[0]["trust"], "external-untrusted")
        self.assertEqual(events[0]["checks"][1]["run"], "run-unit")
        self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), "worker"))
        self.assertEqual(ci_waits.pending(self.store, "reviewer"), [])
        result, reader = self.poll()
        self.assertEqual(result["polled"], 0)
        reader.assert_not_called()
        ci_waits.acknowledge(self.store, "worker", events)
        ci_waits.acknowledge(self.store, "worker", events)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), "worker"))
        self.assertEqual(self.store.get(item["id"]), item)

    def test_runtime_delivers_evidence_in_prompt_and_run_then_stops_waking(self):
        engine = Engine(self.config, driver=ScriptDriver())
        for conclusion in ("success", "failure"):
            with self.subTest(conclusion=conclusion):
                item = self.block(conclusion, actor="reviewer")
                engine.run(self.project, "reviewer")
                self.poll(self.rows(state=conclusion))
                delivered = engine.run(self.project, "reviewer")
                self.assertEqual(delivered["status"], "completed")
                run = self.project.root / "runs" / delivered["run"]
                event = read_json(run / "wait-events.json")["events"][0]
                prompt = json.loads(engine.driver.calls[-1][1])
                self.assertEqual(prompt["wait_events"], [event])
                self.assertEqual(event["work_item"], item["id"])
                self.assertEqual(event["conclusion"], conclusion)
                self.assertEqual(read_json(run / "started.json")["admission"], "wait")
                self.assertEqual(self.store.get(item["id"]), item)
                self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")

    def test_stale_observation_and_old_delivery_cannot_reach_reopened_waiter(self):
        item = self.block()
        def changed_reader(*args):
            self.store.transition(item["id"], expected_rev=2, status="open", actor="worker")
            self.store.transition(item["id"], expected_rev=3, status="blocked", actor="worker", wait=self.wait)
            return {"checks": self.rows()}
        result, _ = self.poll(reader=changed_reader)
        self.assertEqual(result["observed"], 0)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.poll()
        old = ci_waits.pending(self.store, "worker")
        self.store.transition(item["id"], expected_rev=4, status="doing", actor="reviewer")
        current = self.store.transition(item["id"], expected_rev=5, status="blocked", actor="worker", wait=self.wait)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.assertEqual(ci_waits.pending(self.store, "reviewer"), [])
        ci_waits.acknowledge(self.store, "worker", old)
        self.poll()
        fresh = ci_waits.pending(self.store, "reviewer")
        self.assertEqual(fresh[0]["rev"], current["rev"])
        self.assertNotEqual(fresh[0]["evidence_digest"], old[0]["evidence_digest"])
        ci_waits.acknowledge(self.store, "worker", old)
        self.assertEqual(ci_waits.pending(self.store, "reviewer"), fresh)

    def test_bounded_rotation_throttle_errors_and_shared_state(self):
        for i in range(5):
            self.block(str(i), wait={**self.wait, "checks": [str(i)]})
        seen = []
        def reader(settings, op, params):
            # Capture item selection through its cursor, all responses unresolved.
            seen.append(self.state["ci_wait_cursor"])
            if len(seen) == 1:
                raise Denied("unavailable")
            return {"checks": []}
        for _ in range(3):
            result, _ = self.poll(reader=reader)
            self.assertEqual(result["polled"], 2)
        self.assertEqual(len(set(seen)), 5)
        self.assertIsNone(ci_waits.poll(self.config, self.project, self.state,
                                       now=self.clock, interval=15, reader=reader))
        shared = {k: self.state[k] for k in ("last_ci_wait_poll", "ci_wait_cursor")}
        resumed = {}
        refresh_poll_state(resumed, shared)
        self.assertEqual({k: resumed[k] for k in shared}, shared)
        self.assertIsNone(ci_waits.poll(self.config, self.project, resumed,
                                       now=self.clock, interval=15, reader=reader))

    def test_writer_wakes_after_done_but_control_gates_and_ack_still_hold(self):
        self.block()
        snap = {**self.project.snapshots.get(), "outcome": "done",
                "wake_generation": self.project.control()["wake_generation"]}
        self.assertFalse(should_run(self.project, snap, role_name="worker"))
        self.poll()
        self.assertTrue(should_run(self.project, snap, role_name="worker"))
        for control in ({"paused": True}, {"draining": True}, {"armed": False}):
            self.project.set_control(armed=True, paused=False, draining=False)
            self.project.set_control(**control)
            self.assertFalse(should_run(self.project, snap, role_name="worker"))
        self.project.set_control(armed=True, paused=False, draining=False)
        ci_waits.acknowledge(self.store, "worker", ci_waits.pending(self.store, "worker"))
        self.assertFalse(should_run(self.project, snap, role_name="worker"))

    def test_poll_project_persists_ci_wait_state(self):
        self.block()
        with patch("mizu.runtime.poll_upstream", return_value=None), \
                patch("mizu.runtime.poll_ci", return_value=None), \
                patch("mizu.runtime.poll_proposals", return_value=None), \
                patch("mizu.vcs.read_via", return_value={"checks": self.rows()}) as reader:
            events = poll_project_vcs(self.config, self.project, {}, now=1000, interval=15)
        self.assertEqual(events[0]["observed"], 1)
        reader.assert_called_once()
        shared = read_json(self.project.root / "vcs-poll.json")
        self.assertEqual(shared["last_ci_wait_poll"], 1000)
        self.assertTrue(shared["ci_wait_cursor"].startswith("wi_"))

    def test_default_reader_preserves_mismatched_duplicate_rows(self):
        self.block()
        rows = self.rows() + [{**self.rows()[0], "sha": "d" * 40}]
        with patch("mizu.vcs.invoke", return_value={"checks": rows}) as adapter:
            result = ci_waits.poll(self.config, self.project, {}, now=1000, interval=15)
        self.assertEqual(result["observed"], 0)
        self.assertEqual(adapter.call_args.args[1], {"op": "status", "branch": "published", "sha": SHA})
        # Existing exact-SHA tool behavior remains filtered.
        with patch("mizu.vcs.invoke", return_value={"checks": rows}):
            data = vcs.read_via(self.config.vcs, "status", {"branch": "published", "sha": SHA})
        self.assertEqual(len(data["checks"]), 2)

    def test_large_valid_set_and_evidence_bounds(self):
        names = [str(i).zfill(2) + "😀" * 4 for i in range(64)]
        item = self.block(wait={**self.wait, "checks": names})
        rows = [{"check": c, "state": "success", "sha": SHA, "run": "😀" * 128} for c in names]
        result, _ = self.poll(rows)
        self.assertEqual(result["observed"], 1)
        event = ci_waits.pending(self.store, "worker")[0]
        self.assertLess(len(canonical(event)), PREVIEW_BYTES)
        self.assertLessEqual(len(canonical(item["wait"]["checks"])), ci_waits.MAX_CHECK_SET_BYTES)
        self.assertLessEqual(len(canonical(self.store.page_open()["items"][0])), 8192)
        self.assertEqual(self.store.page_open()["items"][0]["id"], item["id"])

    def test_bounded_delivery_and_partial_ack_replay(self):
        items = [self.block(str(i)) for i in range(3)]
        self.poll()
        self.poll()
        page = ci_waits.pending(self.store, "worker", limit=2)
        self.assertEqual(len(page), 2)
        with patch("mizu.ci_waits.write_json", wraps=write_json) as writer:
            def fail_second(path, record, **kwargs):
                if writer.call_count == 2:
                    raise OSError("interrupted ack")
                return write_json(path, record, **kwargs)
            writer.side_effect = fail_second
            with self.assertRaises(OSError):
                ci_waits.acknowledge(self.store, "worker", page)
        remaining = ci_waits.pending(self.store, "worker")
        self.assertEqual(len(remaining), 2)
        self.assertNotIn(page[0], remaining)
        self.assertIn(page[1], remaining)
        self.assertTrue(all(self.store.get(i["id"])["status"] == "blocked" for i in items))

    def test_oversized_evidence_is_not_delivery_or_a_poll_crash(self):
        from mizu.errors import LimitExceeded
        item = self.block()
        write_json(ci_waits._path(self.store, item), {"padding": "x" * PREVIEW_BYTES})
        with self.assertRaises(LimitExceeded):
            ci_waits.pending(self.store, "worker")
        result, reader = self.poll()
        self.assertFalse(result["ok"])
        reader.assert_not_called()
        snap = {**self.project.snapshots.get(), "outcome": "done",
                "wake_generation": self.project.control()["wake_generation"]}
        self.assertFalse(should_run(self.project, snap, role_name="worker"))

    def test_failed_evidence_write_never_wakes_and_tampered_evidence_fails_closed(self):
        item = self.block()
        with patch("mizu.ci_waits.write_json", side_effect=OSError("unwritable")):
            result, _ = self.poll()
        self.assertFalse(result["ok"])
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.poll()
        event = ci_waits.pending(self.store, "worker")[0]
        path = ci_waits._path(self.store, item)
        record = read_json(path)
        record["conclusion"] = "failure"
        write_json(path, record)
        with self.assertRaises(Denied):
            ci_waits.pending(self.store, "worker")
        self.assertEqual(event["conclusion"], "success")
