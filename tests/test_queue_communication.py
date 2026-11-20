"""Queue communication: the operator/host CLI, opening facts, readiness and imported work."""
import contextlib
import dataclasses
import io
import json
import unittest
from unittest.mock import patch

import support  # noqa: F401
from support import Fixture, ScriptDriver
from mizu import cli, queue_facts
from mizu.errors import Denied
from mizu.fs import write_json
from mizu.kernel import parse_queues
from mizu.project import Project
from mizu.role_admission import should_run
from mizu.runtime import Engine
from mizu.work_item_events import pending
from mizu.work_item_seed import make_seed
from mizu.work_items import WorkItems

QUEUES = {
    "notes": {
        "write": ["role:*", "operator", "host"],
        "read": ["role:worker", "operator"],
        "wake": ["role:worker"],
        "schema": {"title": {"type": "string", "max": 80, "required": True},
                   "body": {"type": "string", "max": 1000}},
    },
    "answers": {"write": ["operator"], "read": ["role:*", "operator"]},
}


class QueueFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, queues=parse_queues(QUEUES))
        self.project = Project(self.config, self.project.name)

    def cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, "load", return_value=self.config), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is not None:
                with patch("sys.stdin", io.StringIO(stdin)):
                    code = cli.main(["--config", str(self.file), *argv])
            else:
                code = cli.main(["--config", str(self.file), *argv])
        return code, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()


class QueueCliTests(QueueFixture):
    def test_operator_write_read_ack_message_and_refs(self):
        code, first, _ = self.cli("queue", "sample", "write", "notes",
                                  "--body", json.dumps({"title": "Upstream release"}))
        self.assertEqual(code, 0)
        self.assertEqual((first["author"], first["seq"]), ("operator", 1))
        code, second, _ = self.cli("queue", "sample", "write", "notes", "--body", "-",
                                   "--ref", first["id"], stdin=json.dumps({"title": "Follow-up"}))
        self.assertEqual((code, second["refs"]), (0, [first["id"]]))
        code, page, _ = self.cli("queue", "sample", "read", "notes")
        self.assertEqual([m["seq"] for m in page["messages"]], [1, 2])
        self.assertEqual(page["head"], 2)
        # Reading after an explicit cursor pages from the end and moves no cursor.
        _, tail, _ = self.cli("queue", "sample", "read", "notes", "--cursor", "1")
        self.assertEqual([m["seq"] for m in tail["messages"]], [2])
        _, again, _ = self.cli("queue", "sample", "read", "notes")
        self.assertEqual(len(again["messages"]), 2)
        self.assertEqual(self.cli("queue", "sample", "ack", "notes", "2")[1], {"seq": 2})
        _, empty, _ = self.cli("queue", "sample", "read", "notes")
        self.assertEqual(empty["messages"], [])
        _, message, _ = self.cli("queue", "sample", "message", second["id"])
        self.assertEqual(message["body"], {"title": "Follow-up"})
        _, incoming, _ = self.cli("queue", "sample", "refs", first["id"], "--direction", "in")
        self.assertEqual([m["id"] for m in incoming["messages"]], [second["id"]])
        _, outgoing, _ = self.cli("queue", "sample", "refs", second["id"], "--direction", "out")
        self.assertEqual([m["id"] for m in outgoing["messages"]], [first["id"]])

    def test_host_principal_and_grants_fail_closed_as_json(self):
        code, written, _ = self.cli("queue", "sample", "write", "notes", "--host",
                                    "--body", json.dumps({"title": "Observed"}))
        self.assertEqual((code, written["author"]), (0, "host"))
        code, out, err = self.cli("queue", "sample", "write", "answers", "--host",
                                  "--body", json.dumps({"body": "x"}))
        self.assertNotEqual(code, 0)
        self.assertIsNone(out)
        self.assertEqual(json.loads(err)["type"], "Denied")
        code, _, err = self.cli("queue", "sample", "write", "notes", "--body", "[1]")
        self.assertNotEqual(code, 0)
        self.assertIn("error", json.loads(err))

    def test_idempotent_id_and_delayed_delivery(self):
        body = json.dumps({"title": "Retry"})
        ident = "a" * 32
        _, first, _ = self.cli("queue", "sample", "write", "notes", "--body", body, "--id", ident)
        _, again, _ = self.cli("queue", "sample", "write", "notes", "--body", body, "--id", ident)
        self.assertEqual(first, again)
        _, later, _ = self.cli("queue", "sample", "write", "notes", "--body", json.dumps({"title": "Later"}),
                               "--deliver-at", "2099-01-01T00:00:00+00:00")
        self.assertEqual(later["deliver_at"], "2099-01-01T00:00:00+00:00")
        _, page, _ = self.cli("queue", "sample", "read", "notes")
        self.assertEqual([m["id"] for m in page["messages"]], [ident])
        self.assertTrue(page["delayed"])


class QueueFactsTests(QueueFixture):
    def write(self, title, actor="operator", **options):
        return self.project.kernel_queues.append("notes", {"title": title}, actor, **options)

    def test_pending_counts_due_run_and_newest(self):
        store = self.project.kernel_queues
        for index in range(5):
            self.write(f"m{index}")
        facts = store.pending("notes", "role:worker", newest=2)
        self.assertEqual((facts["count"], facts["more"]), (5, False))
        self.assertEqual([m["body"]["title"] for m in facts["messages"]], ["m3", "m4"])
        self.assertTrue(store.due("notes", "role:worker"))
        store.ack("notes", "role:worker", 5)
        self.assertFalse(store.due("notes", "role:worker"))
        self.write("held", deliver_at="2099-01-01T00:00:00+00:00")
        self.write("after")
        self.assertEqual(store.pending("notes", "role:worker")["count"], 0)
        self.assertFalse(store.due("notes", "role:worker"))
        with self.assertRaises(Denied):
            store.pending("notes", "role:searcher")
        with self.assertRaises(Denied):
            store.pending("notes", "role:worker", newest=513)

    def test_opening_states_facts_for_readable_queues_only(self):
        self.write("first")
        self.write("second")
        self.project.kernel_queues.append("answers", {"body": "yes"}, "operator")
        rows = queue_facts.opening(self.project, "worker")
        self.assertEqual([row["queue"] for row in rows], ["notes", "answers"])
        notes = rows[0]
        self.assertEqual(notes["count"], 2)
        self.assertEqual(len(notes["messages"]), self.config.limits.queue_opening_messages - 1)
        self.assertEqual(queue_facts.readable(self.config, "searcher"), ["answers"])
        self.assertEqual(queue_facts.waking(self.config, "worker"), ["notes"])
        self.assertEqual(queue_facts.waking(self.config, "searcher"), [])

    def test_opening_is_bounded_by_configured_limit(self):
        for index in range(6):
            self.write(f"m{index}")
        limits = dataclasses.replace(self.config.limits, queue_opening_messages=1)
        self.project.config = dataclasses.replace(self.config, limits=limits)
        rows = queue_facts.opening(self.project, "worker")
        self.assertEqual((rows[0]["count"], [m["body"]["title"] for m in rows[0]["messages"]]),
                         (6, ["m5"]))

    def test_unit_opening_carries_queue_facts_without_instructions(self):
        self.write("Observed upstream release")
        prompts = []

        def driver(ctx, prompt, profile=None):
            prompts.append(json.loads(prompt))
            ctx.handle("finish", {"outcome": "wait", "summary": "Read the queue", "state": "Next."})

        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"],
                                              capabilities=(*roles["worker"].capabilities, "queues"))
        config = dataclasses.replace(self.config, roles=roles)
        self.project = Project(config, self.project.name)
        Engine(config, driver=ScriptDriver(driver)).run(self.project, "worker")
        queues = prompts[0]["queues"]
        self.assertEqual(queues[0]["queue"], "notes")
        self.assertEqual(queues[0]["count"], 1)
        self.assertEqual(queues[0]["messages"][0]["body"]["title"], "Observed upstream release")
        self.assertEqual(set(queues[0]), {"queue", "count", "more", "messages"})

    def test_readiness_admits_on_due_message_in_a_waking_queue(self):
        snapshot = self.project.snapshots.get()
        self.project.set_control(armed=True, paused=False)
        self.assertFalse(queue_facts.due(self.project, "worker"))
        self.write("Wake")
        self.assertTrue(queue_facts.due(self.project, "worker"))
        self.assertTrue((self.project.root / "wake" / "worker").exists())
        with patch("mizu.role_admission._ready_work_observation", return_value=(False, False, False)):
            self.assertTrue(should_run(self.project, snapshot, role_name="worker"))
        self.project.kernel_queues.ack("notes", "role:worker", 1)
        self.assertFalse(queue_facts.due(self.project, "worker"))
        # A message in a queue that does not wake the role is no reason to admit it.
        self.project.kernel_queues.append("answers", {"body": "later"}, "operator")
        self.assertFalse(queue_facts.due(self.project, "worker"))


class ImportedWorkTests(Fixture):
    def test_imported_seed_with_legacy_origin_and_inert_wait_opens_by_operator(self):
        item_id = "wi_" + "c" * 32
        seed = make_seed(item_id, "Converted work", {"insight_id": "abc", "rev": 2},
                         "2026-01-01T00:00:00+00:00")
        seed.update(status="blocked", wait={"kind": "retired_kind", "target": "abc"})
        directory = self.project.root / "work-items" / item_id
        directory.mkdir(parents=True)
        write_json(directory / "create.json", {"kind": "imported", "work_item": seed}, exclusive=True)
        store = WorkItems(self.project.root, self.project.snapshots, self.project.ledger)
        store._ledger.rebuild("items")
        item = store.current(item_id)
        self.assertEqual((item["status"], item["origin"]), ("blocked", {"insight_id": "abc", "rev": 2}))
        from mizu.work_item_waits import liveness
        self.assertEqual(liveness(store, item["wait"], None)["reason"], "unsupported_wait")
        args = cli.parser().parse_args(["--config", str(self.file), "work-items", "open", "sample",
                                        item_id, "--expected-rev", "1", "--reason", "Wait kind retired"])
        opened = cli._execute(self.config, args)
        self.assertEqual((opened["status"], opened["actor"], opened["holder"]), ("open", "operator", None))
        self.assertEqual(store.audit()["consistent"], True)

    def test_new_origins_are_opaque_strings_and_terminal_events_route_to_creator(self):
        store = self.project.work_items
        item = store.create(idempotency_key="k", title="From a message", actor="reviewer",
                            origin="f" * 32)["record"]["work_item"]
        self.assertEqual(item["origin"], "f" * 32)
        with self.assertRaises(Denied):
            store.create(idempotency_key="bad", title="Bad", actor="reviewer", origin={"insight_id": "x"})
        store.transition(item["id"], expected_rev=1, status="dropped", actor="worker", reason="Not needed")
        own = pending(store, "reviewer", ("dropped",))
        self.assertEqual([row["id"] for row in own["events"]], [item["id"]])
        self.assertEqual(own["events"][0]["creator"], "reviewer")
        self.assertEqual(pending(store, "worker", ("dropped",))["events"], [])
        closed = store.page_closed(origin="f" * 32)
        self.assertEqual([row["id"] for row in closed["items"]], [item["id"]])


if __name__ == "__main__":
    unittest.main()
