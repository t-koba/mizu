"""Revision-bound dependency requests: explicit recipient, exact-rev satisfaction."""
import unittest
from support import Fixture, ScriptDriver, finish_with_decision_acks
from mizu.errors import Denied, ConfigError
from mizu.fs import read_json, write_json
from mizu.runtime import Engine


def _submit(project, source, title="T"):
    return project.insights.submit(source=source, title=title, body="evidence",
                                   base_snapshot=project.snapshots.get()["id"])


def _wait(recipient="worker", target=None, rev=1, action="accept"):
    return {"kind": "dependency", "recipient": recipient,
            "requires": {"insight": target, "rev": rev, "action": action}}


def _defer(project, source, wait, title="Need"):
    item = _submit(project, source, title)
    project.insights.decide(item["id"], "defer", "parked", "wait for it", "test",
                             wait=wait, expected_rev=1)
    return item


class SatisfactionEvidenceTests(unittest.TestCase):
    def test_pure_predicate_preserves_revision_action_and_timestamp_semantics(self):
        from mizu.insights import satisfaction_evidence
        result = {"id": "target", "rev": "2", "title": "Result"}
        stored = _wait(target="target", rev=2, action="reject")
        record = {"rev": 2, "action": "reject", "created_at": "recorded timestamp"}
        expected = {"insight": "target", "rev": 2, "action": "reject",
                    "decided_at": "recorded timestamp", "title": "Result"}
        # The existing predicate coerces insight revisions, compares decision
        # revisions exactly, and requires a nonempty string (not a parsed date).
        self.assertEqual(satisfaction_evidence(stored, result, record), expected)
        for change in ({"rev": 1}, {"rev": "2"}, {"action": "accept"},
                       {"created_at": ""}, {"created_at": None}, {"created_at": 1}):
            with self.subTest(change=change):
                self.assertIsNone(satisfaction_evidence(stored, result, {**record, **change}))
        self.assertIsNone(satisfaction_evidence(stored, {**result, "rev": 3}, record))
        self.assertIsNone(satisfaction_evidence({}, result, record))
        self.assertIsNone(satisfaction_evidence(stored, result, None))


class DependencyRegistrationTests(Fixture):
    def test_valid_dependency_registers(self):
        result = _submit(self.project, "worker", "Result")
        item = _defer(self.project, "reviewer", _wait(target=result["id"]))
        stored = read_json(self.project.root / "waits" / f"{item['id']}.json")
        self.assertEqual((stored["recipient"], stored["requires"]),
                         ("worker", {"insight": result["id"], "rev": 1, "action": "accept"}))

    def test_malformed_dependency_refused(self):
        result = _submit(self.project, "worker", "Result")
        bad = [{"kind": "dependency"},
               {"kind": "dependency", "recipient": "", "requires": {"insight": result["id"], "rev": 1, "action": "accept"}},
               {"kind": "dependency", "recipient": "worker"},
               {"kind": "dependency", "recipient": "worker",
                "requires": {"insight": result["id"], "rev": 0, "action": "accept"}},
               {"kind": "dependency", "recipient": "worker",
                "requires": {"insight": result["id"], "rev": "1", "action": "accept"}},
               {"kind": "dependency", "recipient": "worker",
                "requires": {"insight": result["id"], "rev": 1, "action": "defer"}},
               {"kind": "dependency", "recipient": "worker",
                "requires": {"insight": result["id"], "rev": 1, "action": "accept"}, "extra": 1},
               {"kind": "dependency", "recipient": "worker",
                "requires": {"insight": result["id"], "rev": 1, "action": "accept", "at": "x"}},
               {"kind": "dependency", "recipient": "not a role!!",
                "requires": {"insight": result["id"], "rev": 1, "action": "accept"}}]
        for wait in bad:
            item = _submit(self.project, "reviewer", "Need")
            with self.assertRaises(Denied, msg=repr(wait)):
                self.project.insights.decide(item["id"], "defer", "r", "v", "test",
                                             wait=wait, expected_rev=1)

    def test_cli_and_schema_accept_dependency(self):
        from mizu.cli import _parse_wait
        from mizu.protocol import DEFINITIONS, validate
        self.assertEqual(_parse_wait("dependency:worker:abc:2:reject"),
                         {"kind": "dependency", "recipient": "worker",
                          "requires": {"insight": "abc", "rev": 2, "action": "reject"}})
        for spec in ("dependency", "dependency:worker", "dependency:worker:abc",
                     "dependency:worker:abc:0:accept", "dependency:worker:abc:1:defer",
                     "dependency:worker:abc:x:accept"):
            with self.assertRaises(ConfigError, msg=spec):
                _parse_wait(spec)
        validate({"id": "x", "action": "defer", "reason": "r", "revisit": "v", "rev": 1,
                  "wait": {"kind": "dependency", "recipient": "worker",
                           "requires": {"insight": "y", "rev": 1, "action": "accept"}}},
                 DEFINITIONS["decide"][1])
        # The protocol shape gate accepts the bare kind; semantic refusal
        # (missing recipient/requires) happens at registration, tested above.
        validate({"id": "x", "action": "defer", "reason": "r", "revisit": "v", "rev": 1,
                  "wait": {"kind": "dependency"}},
                 DEFINITIONS["decide"][1])


class DependencySatisfactionTests(Fixture):
    def _setup(self):
        result = _submit(self.project, "worker", "Result")
        item = _defer(self.project, "reviewer", _wait(target=result["id"]))
        return result, item

    def test_exact_rev_decision_satisfies_with_evidence(self):
        result, item = self._setup()
        snap = self.project.snapshots.get()
        # Pending first: no premature wake for the requester, obligation for the recipient.
        self.assertEqual(self.project.insights.due_waits("reviewer", snap), [])
        obligations = self.project.insights.pending_obligations("worker")
        self.assertEqual(len(obligations), 1)
        self.assertEqual((obligations[0]["insight"], obligations[0]["requires"]["rev"]), (item["id"], 1))
        self.project.insights.decide(result["id"], "accept", "done", "", "worker", expected_rev=1)
        due = self.project.insights.due_waits("reviewer", snap)
        self.assertEqual(len(due), 1)
        event = due[0]
        self.assertEqual((event["kind"], event["recipient"]), ("dependency", "worker"))
        self.assertEqual(event["result"]["insight"], result["id"])
        self.assertEqual((event["result"]["rev"], event["result"]["action"]), (1, "accept"))
        self.assertTrue(event["result"]["decided_at"])
        # Satisfied obligations leave the recipient's pending list.
        self.assertEqual(self.project.insights.pending_obligations("worker"), [])

    def test_limit_selects_oldest_deliverable_dependency(self):
        from unittest.mock import patch
        first_result = _submit(self.project, "worker", "First result")
        second_result = _submit(self.project, "worker", "Second result")
        first = _defer(self.project, "reviewer", _wait(target=first_result["id"]), title="First request")
        second = _defer(self.project, "reviewer", _wait(target=second_result["id"]), title="Second request")
        self.project.insights.decide(first_result["id"], "accept", "done", "", "worker", expected_rev=1)
        self.project.insights.decide(second_result["id"], "accept", "done", "", "worker", expected_rev=1)
        paths = [self.project.root / "waits" / f"{item['id']}.json" for item in (first, second)]
        for path, rid in zip(paths, ("0" * 31 + "1", "0" * 31 + "2")):
            stored = read_json(path)
            stored["registered_at"] = "2026-01-01T00:00:00+00:00"
            stored["registration_id"] = rid
            write_json(path, stored)
        actual = self.project.insights._satisfaction_evidence
        def evidence(stored):
            return None if stored.get("owner") == first["id"] else actual(stored)
        with patch.object(self.project.insights, "_satisfaction_evidence", side_effect=evidence):
            due = self.project.insights.due_waits("reviewer", self.project.snapshots.get(), limit=1)
        self.assertEqual([row["insight"] for row in due], [second["id"]])

    def test_wrong_action_does_not_satisfy(self):
        result, _ = self._setup()
        self.project.insights.decide(result["id"], "reject", "no", "", "worker", expected_rev=1)
        self.assertEqual(self.project.insights.due_waits(
            "reviewer", self.project.snapshots.get()), [])

    def test_moved_result_is_stale_and_never_satisfies(self):
        result, _ = self._setup()
        self.project.insights.revise(result["id"], title="Result", body="new facts",
                                     expected_rev=1, source="worker",
                                     base_snapshot=self.project.snapshots.get()["id"])
        # Decide the NEW revision: the assessed rev 1 completion is stale.
        self.project.insights.decide(result["id"], "accept", "done", "", "worker", expected_rev=2)
        self.assertEqual(self.project.insights.due_waits(
            "reviewer", self.project.snapshots.get()), [])
        # The obligation stays pending: reassessment, not silent satisfaction.
        self.assertEqual(len(self.project.insights.pending_obligations("worker")), 1)

    def test_missing_or_withdrawn_result_never_satisfies(self):
        _, _ = self._setup()
        self.assertEqual(self.project.insights.due_waits(
            "reviewer", self.project.snapshots.get()), [])
        ghost = _defer(self.project, "reviewer", _wait(target="no-such-id"), title="Ghost")
        del ghost
        self.assertEqual(self.project.insights.due_waits(
            "reviewer", self.project.snapshots.get()), [])

    def test_request_revision_unbinds(self):
        result, item = self._setup()
        self.project.insights.decide(result["id"], "accept", "done", "", "worker", expected_rev=1)
        self.assertEqual(len(self.project.insights.due_waits("reviewer", self.project.snapshots.get())), 1)
        # New facts on the request invalidate the old assessment's binding.
        self.project.insights.revise(item["id"], title="Need", body="changed terms",
                                     expected_rev=1, source="reviewer",
                                     base_snapshot=self.project.snapshots.get()["id"])
        snap = self.project.snapshots.get()
        self.assertEqual(self.project.insights.due_waits("reviewer", snap), [])
        self.assertEqual(self.project.insights.pending_obligations("worker"), [])
        self.assertFalse((self.project.root / "waits" / f"{item['id']}.json").exists())

    def test_completion_consumed_idempotently(self):
        result, item = self._setup()
        self.project.insights.decide(result["id"], "accept", "done", "", "worker", expected_rev=1)
        due = self.project.insights.due_waits("reviewer", self.project.snapshots.get())
        self.assertEqual(self.project.insights.consume_waits("reviewer", due), 1)
        # Duplicate delivery after acknowledge consumes nothing further.
        self.assertEqual(self.project.insights.consume_waits("reviewer", due), 0)
        self.assertFalse((self.project.root / "waits" / f"{item['id']}.json").exists())


class DependencyDispatchTests(Fixture):
    def _engine(self):
        return Engine(self.config, driver=ScriptDriver(finish_with_decision_acks))

    def test_obligation_rides_along_but_never_admits(self):
        import dataclasses
        roles = dict(self.config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], decision_events=("reject",))
        config = dataclasses.replace(self.config, roles=roles)
        engine = Engine(config, driver=ScriptDriver())
        engine.run(self.project, "reviewer")
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")
        result = _submit(self.project, "worker", "Result")
        _defer(self.project, "worker", _wait(recipient="reviewer", target=result["id"]))
        # Obligations alone admit nothing.
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")
        # When the recipient runs for other reasons, the obligation is visible with evidence.
        rejected = _submit(self.project, "reviewer", "Other")
        self.project.insights.decide(rejected["id"], "reject", "no", "", "reviewer", expected_rev=1)
        admitted = engine.run(self.project, "reviewer")
        started = read_json(self.project.root / "runs" / admitted["run"] / "started.json")
        self.assertEqual(started["obligation_events"], 1)
        logged = read_json(self.project.root / "runs" / admitted["run"] / "obligation-events.json")
        self.assertEqual(logged["events"][0]["recipient"], "reviewer")
        self.assertEqual(logged["events"][0]["requires"]["insight"], result["id"])

    def test_satisfied_completion_admits_requester_once(self):
        engine = self._engine()
        engine.run(self.project, "reviewer")
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")
        result = _submit(self.project, "worker", "Result")
        item = _defer(self.project, "reviewer", _wait(target=result["id"]))
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")
        self.project.insights.decide(result["id"], "accept", "done", "", "worker", expected_rev=1)
        admitted = engine.run(self.project, "reviewer")
        started = read_json(self.project.root / "runs" / admitted["run"] / "started.json")
        self.assertEqual((started["admission"], started["wait_events"]), ("wait", 1))
        logged = read_json(self.project.root / "runs" / admitted["run"] / "wait-events.json")
        self.assertEqual(logged["events"][0]["result"]["action"], "accept")
        self.assertFalse((self.project.root / "waits" / f"{item['id']}.json").exists())
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")

    def test_dependency_wait_is_readable_from_store(self):
        result = _submit(self.project, "worker", "Result")
        item = _defer(self.project, "reviewer", _wait(target=result["id"]))
        wait = self.project.insights.wait_for(item["id"])
        self.assertEqual((wait["kind"], wait["recipient"]), ("dependency", "worker"))
        self.assertEqual(wait["requires"]["insight"], result["id"])



class DependencyRegistrationIdTests(Fixture):
    def test_obligations_carry_unique_registration_id(self):
        from mizu.fs import read_json
        result = _submit(self.project, "worker", "Result")
        first = _defer(self.project, "reviewer", _wait(target=result["id"]), title="First")
        second = _defer(self.project, "reviewer", _wait(target=result["id"]), title="Second")
        stored_first = read_json(self.project.root / "waits" / f"{first['id']}.json")
        stored_second = read_json(self.project.root / "waits" / f"{second['id']}.json")
        for stored in (stored_first, stored_second):
            rid = stored.get("registration_id")
            self.assertIsInstance(rid, str)
            self.assertTrue(rid)
            self.assertNotIn(":", rid)
        self.assertNotEqual(stored_first["registration_id"], stored_second["registration_id"])
        obligations = {row["insight"]: row for row in
                       self.project.insights.pending_obligations("worker")}
        self.assertEqual(obligations[first["id"]]["registration_id"],
                         stored_first["registration_id"])
        self.assertEqual(obligations[second["id"]]["registration_id"],
                         stored_second["registration_id"])

    def test_legacy_obligation_reads_via_stable_fallback(self):
        from mizu.fs import read_json, write_json
        result = _submit(self.project, "worker", "Result")
        item = _defer(self.project, "reviewer", _wait(target=result["id"]))
        path = self.project.root / "waits" / f"{item['id']}.json"
        stored = read_json(path)
        stored.pop("registration_id", None)
        write_json(path, stored)
        first = self.project.insights.pending_obligations("worker")
        self.assertEqual(len(first), 1)
        fallback = first[0]["registration_id"]
        self.assertTrue(fallback.startswith("legacy:"))
        self.assertIn(item["id"], fallback)
        self.assertNotIn("registration_id", read_json(path))
        second = self.project.insights.pending_obligations("worker")
        self.assertEqual(second[0]["registration_id"], fallback)
        other = _defer(self.project, "reviewer", _wait(target=result["id"]), title="Other")
        other_stored = read_json(self.project.root / "waits" / f"{other['id']}.json")
        self.assertNotIn(":", other_stored["registration_id"])
        self.assertNotEqual(other_stored["registration_id"], fallback)

    def test_obligation_limit_returns_globally_earliest_not_path_order(self):
        from mizu.fs import read_json, write_json
        result = _submit(self.project, "worker", "Result")
        [_defer(self.project, "reviewer", _wait(target=result["id"]), title=f"Need{i}")
         for i in range(3)]
        paths = sorted((self.project.root / "waits").glob("*.json"))
        self.assertEqual(len(paths), 3)
        stamps = "2026-01-01T00:00:00+00:00"
        rids = ["00000000000000000000000000000003",
                "00000000000000000000000000000002",
                "00000000000000000000000000000001"]
        for path, rid in zip(paths, rids):
            stored = read_json(path)
            stored["registered_at"] = stamps
            stored["registration_id"] = rid
            write_json(path, stored)
        by_path = {path.stem: rid for path, rid in zip(paths, rids)}
        smallest = min(by_path, key=lambda k: by_path[k])
        first = self.project.insights.pending_obligations("worker", limit=1)
        self.assertEqual([row["insight"] for row in first], [smallest])
        self.assertEqual(first[0]["registration_id"], by_path[smallest])
        two = self.project.insights.pending_obligations("worker", limit=2)
        self.assertEqual([row["insight"] for row in two],
                         sorted(by_path, key=lambda k: by_path[k])[:2])

    def test_dependency_reregistration_gets_new_identity(self):
        from mizu.fs import read_json
        result = _submit(self.project, "worker", "Result")
        item = _defer(self.project, "reviewer", _wait(target=result["id"]))
        first = read_json(self.project.root / "waits" / f"{item['id']}.json")["registration_id"]
        self.project.insights.revise(item["id"], source="reviewer", title="Need",
                                     body="changed terms", expected_rev=1,
                                     base_snapshot=self.project.snapshots.get()["id"])
        self.project.insights.decide(item["id"], "defer", "parked again", "wait again", "test",
                                     wait=_wait(target=result["id"]), expected_rev=2)
        second = read_json(self.project.root / "waits" / f"{item['id']}.json")["registration_id"]
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertNotEqual(first, second)

if __name__ == "__main__":
    unittest.main()
