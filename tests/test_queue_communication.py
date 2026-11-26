"""Queue communication: the operator/host CLI, opening facts, readiness and imported work."""
import contextlib
import dataclasses
import datetime as dt
import io
import json
import unittest
from unittest.mock import patch

import support  # noqa: F401
from support import Fixture, ScriptDriver, history_write_json
from mizu import cli, dispatch_evidence, queue_facts, unit_finish
from mizu.errors import Denied, ProtocolError
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
    def test_operator_write_read_message_and_refs(self):
        code, first, _ = self.cli("queue", "sample", "write", "notes",
                                  "--body", json.dumps({"title": "Upstream release"}))
        self.assertEqual(code, 0)
        self.assertEqual((first["author"], first["seq"]), ("operator", 1))
        code, second, _ = self.cli("queue", "sample", "write", "notes", "--body", "-",
                                   "--ref", first["id"], stdin=json.dumps({"title": "Follow-up"}))
        self.assertEqual((code, second["refs"]), (0, [first["id"]]))
        # The operator's own writes are credited at append, so they have
        # already been delivered to it and no plain read returns them.
        _, own, _ = self.cli("queue", "sample", "read", "notes")
        self.assertEqual(own["messages"], [])
        _, observed, _ = self.cli("queue", "sample", "write", "notes", "--host",
                                  "--body", json.dumps({"title": "Observed"}))
        code, page, _ = self.cli("queue", "sample", "read", "notes")
        self.assertEqual([m["seq"] for m in page["messages"]], [3])
        self.assertEqual(page["head"], 3)
        # A read delivers the host's message to the operator and records cli.
        store = self.project.kernel_queues
        self.assertTrue(store.delivered("notes", 3, "operator"))
        fact = json.loads((self.project.root / "queues" / "notes" / "delivery"
                           / "00000000000000000003" / "operator.json").read_text())
        self.assertEqual(fact["unit"], "cli")
        # An explicit cursor pages the whole history; a plain read is drained.
        _, history, _ = self.cli("queue", "sample", "read", "notes", "--cursor", "0")
        self.assertEqual([m["seq"] for m in history["messages"]], [1, 2, 3])
        _, tail, _ = self.cli("queue", "sample", "read", "notes", "--cursor", "1")
        self.assertEqual([m["seq"] for m in tail["messages"]], [2, 3])
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
        _, first, _ = self.cli("queue", "sample", "write", "notes", "--body", body,
                               "--id", ident, "--host")
        _, again, _ = self.cli("queue", "sample", "write", "notes", "--body", body,
                               "--id", ident, "--host")
        self.assertEqual(first, again)
        _, later, _ = self.cli("queue", "sample", "write", "notes", "--body", json.dumps({"title": "Later"}),
                               "--deliver-at", "2099-01-01T00:00:00+00:00")
        self.assertEqual(later["deliver_at"], "2099-01-01T00:00:00+00:00")
        _, page, _ = self.cli("queue", "sample", "read", "notes")
        self.assertEqual([m["id"] for m in page["messages"]], [ident])
        self.assertTrue(page["delayed"])
        _, history, _ = self.cli("queue", "sample", "read", "notes", "--cursor", "0")
        self.assertEqual([m["id"] for m in history["messages"]], [ident])


class QueueFactsTests(QueueFixture):
    def write(self, title, actor="operator", **options):
        return self.project.kernel_queues.append("notes", {"title": title}, actor, **options)

    def test_pending_counts_due_run_and_newest(self):
        store = self.project.kernel_queues
        for index in range(5):
            self.write(f"m{index}")
        facts = store.pending("notes", "role:worker", newest=2)
        self.assertEqual((facts["count"], facts["more"]), (5, True))
        self.assertEqual([m["body"]["title"] for m in facts["messages"]], ["m0", "m1"])
        self.assertTrue(store.due("notes", "role:worker"))
        for seq in range(1, 6):
            store.record_delivery("notes", seq, "role:worker", "test")
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
                         (6, ["m0"]))

    def test_successive_openings_deliver_every_message_in_seq_order(self):
        for index in range(3):
            self.write(f"m{index}")
        limits = dataclasses.replace(self.config.limits, queue_opening_messages=1)
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], capabilities=(*roles["worker"].capabilities, "queues"))
        config = dataclasses.replace(self.config, roles=roles, limits=limits)
        self.project = Project(config, self.project.name)

        seen = []

        def observer(ctx, prompt, profile=None):
            shown = json.loads(prompt).get("queues", [])
            if shown:
                seen.append(shown[0]["messages"][0]["body"]["title"])
            ctx.handle("finish", {"outcome": "wait", "summary": "Seen", "state": "Next."})

        for _ in range(3):
            Engine(config, driver=ScriptDriver(observer)).run(self.project, "worker")
        self.assertEqual(seen, ["m0", "m1", "m2"])
        # No reader call: the openings themselves delivered every message in
        # seq order, so each is delivered and the wake has ended.
        store = self.project.kernel_queues
        for seq in (1, 2, 3):
            self.assertTrue(store.delivered("notes", seq, "role:worker"))
        self.assertFalse(queue_facts.due(self.project, "worker"))
        self.assertEqual(queue_facts.opening(self.project, "worker"), [])

    def test_pre_handoff_failure_keeps_the_only_wake(self):
        """Delivery is credited at the driver handoff, not at prompt setup."""
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], capabilities=(*roles["worker"].capabilities, "queues"))
        config = dataclasses.replace(self.config, roles=roles)
        self.project = Project(config, self.project.name)
        self.write("unseen")
        driver = ScriptDriver(lambda ctx, prompt, profile=None: ctx.handle(
            "finish", {"outcome": "wait", "summary": "Seen", "state": "Next."}))

        def fail_setup(*args, **kwargs):
            raise OSError("synthetic pre-handoff setup failure")

        with patch.object(dispatch_evidence, "capture_projection", side_effect=fail_setup):
            with self.assertRaises(OSError):
                Engine(config, driver=driver).run(self.project, "worker")
        # No prompt reached the driver, so nothing was delivered: the wake and
        # the pending message survive for a later unit.
        self.assertEqual(driver.calls, [])
        self.assertTrue(queue_facts.due(self.project, "worker"))
        self.assertFalse(self.project.kernel_queues.delivered("notes", 1, "role:worker"))
        self.assertEqual(len(queue_facts.opening(self.project, "worker")[0]["messages"]), 1)

        # A subsequent unit that does reach the driver hands over and credits it.
        Engine(config, driver=driver).run(self.project, "worker")
        self.assertFalse(queue_facts.due(self.project, "worker"))
        self.assertTrue(self.project.kernel_queues.delivered("notes", 1, "role:worker"))
        self.assertEqual(queue_facts.opening(self.project, "worker"), [])

    def test_referred_body_serialized_in_opening_is_credited(self):
        """A nested referred body handed to the driver is credited by its own queue/seq."""
        limits = dataclasses.replace(self.config.limits, queue_opening_messages=1)
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], capabilities=(*roles["worker"].capabilities, "queues"))
        config = dataclasses.replace(self.config, roles=roles, limits=limits)
        self.project = Project(config, self.project.name)
        store = self.project.kernel_queues
        store.append("answers", {"title": "filler"}, "operator")
        target = store.append("answers", {"title": "TARGET"}, "operator")
        store.append("notes", {"title": "reply"}, "operator", refs=[target["id"]])

        handed = []
        def reader(ctx, prompt, profile=None):
            for row in json.loads(prompt).get("queues", []):
                for message in row.get("messages", []):
                    for ref in message.get("refers", []):
                        handed.append(ref["body"]["title"])
            ctx.handle("finish", {"outcome": "wait", "summary": "Read", "state": "Next."})

        Engine(config, driver=ScriptDriver(reader)).run(self.project, "worker")
        # The target was beyond the one-message opening limit of its own queue,
        # but its body was serialized as a referred body and so was delivered.
        self.assertEqual(handed, ["TARGET"])
        self.assertTrue(store.delivered("answers", target["seq"], "role:worker"))
        # No reader call drains it: it will not be shown again.
        self.assertNotIn(target["seq"], [m["seq"] for m in
                         store.pending("answers", "role:worker")["messages"]])

    def test_future_dated_referred_body_is_credited_and_not_shown_again(self):
        """A referred body with a future deadline is handed over and credited once."""
        limits = dataclasses.replace(self.config.limits, queue_opening_messages=1)
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], capabilities=(*roles["worker"].capabilities, "queues"))
        config = dataclasses.replace(self.config, roles=roles, limits=limits)
        self.project = Project(config, self.project.name)
        store = self.project.kernel_queues
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        target = store.append("answers", {"title": "DELAYED-TARGET"}, "operator",
                              deliver_at=future)
        store.append("notes", {"title": "reply"}, "operator", refs=[target["id"]])

        handed = []
        def reader(ctx, prompt, profile=None):
            for row in json.loads(prompt).get("queues", []):
                for message in row.get("messages", []):
                    for ref in message.get("refers", []):
                        handed.append(ref["body"]["title"])
            ctx.handle("finish", {"outcome": "wait", "summary": "Read", "state": "Next."})

        Engine(config, driver=ScriptDriver(reader)).run(self.project, "worker")
        # The body was serialized into the dispatched opening even though its
        # deadline is in the future, so the handoff must record the fact.
        self.assertEqual(handed, ["DELAYED-TARGET"])
        self.assertTrue(store.delivered("answers", target["seq"], "role:worker"))
        # Past the deadline it is not offered again: the reader already got it.
        past = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
        self.assertNotIn(target["seq"], [m["seq"] for m in
                         store.pending("answers", "role:worker", now=past)["messages"]])

    def test_unreadable_ref_is_not_serialized_nor_credited(self):
        """A body the reader never received stays undelivered."""
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], capabilities=(*roles["worker"].capabilities, "queues"))
        queues = dict(QUEUES)
        queues["secret"] = {"write": ["operator"], "read": ["operator"],
                            "schema": {"title": {"type": "string", "max": 80, "required": True}}}
        config = dataclasses.replace(self.config, roles=roles, queues=parse_queues(queues))
        self.project = Project(config, self.project.name)
        store = self.project.kernel_queues
        hidden = store.append("secret", {"title": "HIDDEN"}, "operator")
        store.append("notes", {"title": "reply"}, "operator", refs=[hidden["id"]])

        handed = []
        def reader(ctx, prompt, profile=None):
            for row in json.loads(prompt).get("queues", []):
                for message in row.get("messages", []):
                    handed.extend(message.get("refers", []))
            ctx.handle("finish", {"outcome": "wait", "summary": "Read", "state": "Next."})

        Engine(config, driver=ScriptDriver(reader)).run(self.project, "worker")
        self.assertEqual(handed, [])
        fact = (self.project.root / "queues" / "secret" / "delivery"
                / f"{hidden['seq']:020d}" / "role:worker.json")
        self.assertFalse(fact.exists())

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
        self.assertEqual(set(queues[0]), {"queue", "count", "more", "messages", "through"})

    def test_waking_message_keeps_no_finish_intent_when_interrupted(self):
        """An interrupted finish leaves no intent; opening delivery still consumes the wake."""
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], capabilities=(*roles["worker"].capabilities, "queues"))
        config = dataclasses.replace(self.config, roles=roles)
        self.project = Project(config, self.project.name)
        self.write("Observed upstream release")
        self.assertTrue(queue_facts.due(self.project, "worker"))

        first_prompts = []
        real_write = unit_finish.write_json

        def interrupt_at_finish(path, value, *args, **kwargs):
            if str(path).endswith("finish-intent.json"):
                raise ProtocolError("synthetic interruption")
            return real_write(path, value, *args, **kwargs)

        def interrupted(ctx, prompt, profile=None):
            first_prompts.append(json.loads(prompt))
            # Finish validation and close_exec have already run here; only
            # the durable finish intent remains. Interrupt exactly that write,
            # so the unit ends without a completed result.
            with patch("mizu.unit_finish.write_json", side_effect=interrupt_at_finish):
                ctx.handle("finish", {"outcome": "wait", "summary": "Interrupted",
                                      "state": "Next."})

        with self.assertRaisesRegex(ProtocolError, "synthetic interruption"):
            Engine(config, driver=ScriptDriver(interrupted)).run(self.project, "worker")

        # The first observer already saw the queue in its own opening facts.
        self.assertEqual(first_prompts[0]["queues"][0]["count"], 1)
        self.assertEqual(first_prompts[0]["queues"][0]["messages"][0]["body"]["title"],
                         "Observed upstream release")
        self.assertFalse(list((self.project.root / "runs").rglob("finish-intent.json")))
        # Delivery was recorded at dispatch, before the finish was interrupted:
        # the message is no longer due or in a later opening, and no wake loops.
        self.assertFalse(queue_facts.due(self.project, "worker"))
        self.assertEqual(queue_facts.opening(self.project, "worker"), [])
        self.assertTrue(self.project.kernel_queues.delivered("notes", 1, "role:worker"))

        prompts = []

        def observer(ctx, prompt, profile=None):
            prompts.append(json.loads(prompt))
            ctx.handle("finish", {"outcome": "wait", "summary": "Read queue", "state": "Next."})

        Engine(config, driver=ScriptDriver(observer)).run(self.project, "worker")
        self.assertEqual(prompts[0]["queues"], [])
        self.assertFalse(queue_facts.due(self.project, "worker"))
        self.assertEqual(queue_facts.opening(self.project, "worker"), [])

    def test_finished_unit_is_not_rewoken_by_messages_its_opening_delivered(self):
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], capabilities=(*roles["worker"].capabilities, "queues"))
        config = dataclasses.replace(self.config, roles=roles)
        self.project = Project(config, self.project.name)
        self.write("Observed upstream release")
        self.assertTrue(queue_facts.due(self.project, "worker"))
        prompts = []

        def reader(ctx, prompt, profile=None):
            prompts.append(json.loads(prompt))
            ctx.handle("finish", {"outcome": "wait", "summary": "Read, not acknowledged",
                                  "state": "Next."})

        Engine(config, driver=ScriptDriver(reader)).run(self.project, "worker")
        self.assertEqual(prompts[0]["queues"][0]["count"], 1)
        self.assertFalse(queue_facts.due(self.project, "worker"))
        # The opening's delivery drains it from both wakes and later openings.
        self.assertEqual(queue_facts.opening(self.project, "worker"), [])
        self.write("Another release")
        self.assertTrue(queue_facts.due(self.project, "worker"))

    def test_hidden_queues_section_credits_nothing_and_stays_pending(self):
        """A prompt without the queues section neither wakes nor credits queue content."""
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], capabilities=(*roles["worker"].capabilities, "queues"),
            context_exclude=("queues",))
        config = dataclasses.replace(self.config, roles=roles)
        self.project = Project(config, self.project.name)
        message = self.write("Observed upstream release")
        prompts = []

        def reader(ctx, prompt, profile=None):
            prompts.append(json.loads(prompt))
            ctx.handle("finish", {"outcome": "wait", "summary": "Read without the section",
                                  "state": "Next."})

        # Omitting the section also removes queue wake eligibility: an unshown
        # message must not wake every unit without ever being drained.
        self.assertNotIn("notes", queue_facts.waking(config, "worker"))
        self.assertFalse(queue_facts.due(self.project, "worker"))
        Engine(config, driver=ScriptDriver(reader)).run(self.project, "worker")
        self.assertNotIn("queues", prompts[0])
        # No false delivery fact: the content was never handed over.
        self.assertFalse(self.project.kernel_queues.delivered("notes", message["seq"], "role:worker"))
        # The message stays pending, so nothing is lost.
        rows = queue_facts.opening(self.project, "worker")
        self.assertEqual([m["seq"] for m in rows[0]["messages"]], [message["seq"]])

    def test_restored_queues_section_delivers_pending_message(self):
        """Returning the section delivers the still-pending message on the next opening."""
        message = self.write("Observed upstream release")
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], capabilities=(*roles["worker"].capabilities, "queues"))
        config = dataclasses.replace(self.config, roles=roles)
        self.project = Project(config, self.project.name)
        seen = []

        def reader(ctx, prompt, profile=None):
            seen.append(json.loads(prompt).get("queues", []))
            ctx.handle("finish", {"outcome": "wait", "summary": "Seen", "state": "Next."})

        Engine(config, driver=ScriptDriver(reader)).run(self.project, "worker")
        self.assertEqual(seen[0][0]["messages"][0]["seq"], message["seq"])
        self.assertTrue(self.project.kernel_queues.delivered("notes", message["seq"], "role:worker"))
        self.assertFalse(queue_facts.due(self.project, "worker"))

    def test_answer_wakes_the_asker_and_arrives_with_its_question(self):
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], capabilities=(*roles["worker"].capabilities, "queues"))
        queues = dict(self.config.queues)
        queues["answers"] = dataclasses.replace(queues["answers"], write=("role:*", "operator"), wake=())
        config = dataclasses.replace(self.config, roles=roles, queues=queues)
        self.project = Project(config, self.project.name)
        store = self.project.kernel_queues
        asked = store.append("answers", {"body": "May I?"}, "role:worker")
        self.assertFalse(queue_facts.due(self.project, "worker"))
        store.append("answers", {"body": "Yes"}, "operator", refs=[asked["id"]])
        self.assertTrue(queue_facts.due(self.project, "worker"))
        rows = queue_facts.opening(self.project, "worker")
        answer = next(m for m in rows[0]["messages"] if m["author"] == "operator")
        self.assertEqual([m["body"]["body"] for m in answer["refers"]], ["May I?"])

    def test_readiness_admits_on_due_message_in_a_waking_queue(self):
        snapshot = self.project.snapshots.get()
        self.project.set_control(armed=True, paused=False)
        self.assertFalse(queue_facts.due(self.project, "worker"))
        self.write("Wake")
        self.assertTrue(queue_facts.due(self.project, "worker"))
        self.assertTrue((self.project.root / "wake" / "worker").exists())
        with patch("mizu.role_admission._ready_work_observation", return_value=(False, False, False)):
            self.assertTrue(should_run(self.project, snapshot, role_name="worker"))
        self.project.kernel_queues.record_delivery("notes", 1, "role:worker", "test")
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

    def test_documented_imported_create_json_is_accepted_and_reopened(self):
        # This is the exact create.json documented in docs/operations.md for a
        # converted decision-seeded item whose wait kind was removed.
        item_id = "wi_0123456789abcdef0123456789abcdef"
        document = {
            "kind": "imported",
            "work_item": {
                "id": item_id,
                "title": "Finish the retained work",
                "origin": {"insight_id": "7f3a91c2d4e6", "rev": 2},
                "status": "blocked",
                "wait": {"kind": "insight_decided", "target": "7f3a91c2d4e6"},
                "rev": 1,
                "holder": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
        }
        directory = self.project.root / "work-items" / item_id
        directory.mkdir(parents=True)
        write_json(directory / "create.json", document, exclusive=True)
        store = WorkItems(self.project.root, self.project.snapshots, self.project.ledger)
        store._ledger.rebuild("items")
        item = store.current(item_id)
        self.assertEqual(item["status"], "blocked")
        self.assertEqual(item["origin"], {"insight_id": "7f3a91c2d4e6", "rev": 2})
        from mizu.work_item_waits import liveness
        self.assertEqual(liveness(store, item["wait"], None)["reason"], "unsupported_wait")
        # The removed wait is inert; the operator conversion reopens the item.
        args = cli.parser().parse_args([
            "--config", str(self.file), "work-items", "open", "sample", item_id,
            "--expected-rev", "1", "--reason", "Wait kind retired by stage 2; reopened for triage",
        ])
        opened = cli._execute(self.config, args)
        self.assertEqual((opened["status"], opened["actor"], opened["holder"]),
                         ("open", "operator", None))
        self.assertIsNone(opened.get("wait"))
        self.assertEqual(store.audit()["consistent"], True)

        # The same runbook promises reopening object_resolved on an insight or
        # record. Those subjects were retired, so the stored wait must stay
        # bounded inert history that audits and reopens by operator CAS, while
        # live work-item object waits remain strictly validated.
        from mizu.fs import read_json
        for subject_type in ("insight", "record"):
            item_id = "wi_" + ("a" if subject_type == "insight" else "b") * 32
            seed = make_seed(item_id, "Retired object wait", {"insight_id": "7f3a91c2d4e6", "rev": 2},
                             "2026-01-01T00:00:00+00:00")
            seed.update(status="blocked",
                        wait={"kind": "object_resolved", "subject": {"type": subject_type, "id": "legacy"}})
            directory = self.project.root / "work-items" / item_id
            directory.mkdir(parents=True)
            write_json(directory / "create.json", {"kind": "imported", "work_item": seed}, exclusive=True)
            store._ledger.rebuild("items")
            current = store.current(item_id)
            self.assertEqual(liveness(store, current["wait"], None)["reason"], "unsupported_wait")
            self.assertEqual(store.audit()["consistent"], True)
            args = cli.parser().parse_args([
                "--config", str(self.file), "work-items", "open", "sample", item_id,
                "--expected-rev", "1", "--reason", "Retired object subject; reopened for triage",
            ])
            reopened = cli._execute(self.config, args)
            self.assertEqual((reopened["status"], reopened.get("wait")), ("open", None))
            self.assertEqual(store.audit()["consistent"], True)

        # A later blocked revision carrying a retired object wait stays replayable
        # and reopenable, not reported as a corrupt chain.
        item_id = "wi_" + "c" * 32
        target = store.create(idempotency_key="target", title="Target", actor="worker")["record"]["work_item"]
        waiter = store.create(idempotency_key="waiter", title="Waiter", actor="worker")["record"]["work_item"]
        store.transition(waiter["id"], expected_rev=1, status="blocked", actor="worker",
                         wait={"kind": "object_resolved", "subject": {"type": "work_item", "id": target["id"]}})
        revision_path = self.project.root / "work-items" / waiter["id"] / "2.json"
        revision = read_json(revision_path)
        revision["wait"] = {"kind": "object_resolved", "subject": {"type": "record", "id": target["id"]}}
        history_write_json(revision_path, revision)
        store._ledger.rebuild("items")
        current = store.current(waiter["id"])
        self.assertEqual((current["rev"], current["status"]), (2, "blocked"))
        self.assertEqual(liveness(store, current["wait"], None)["reason"], "unsupported_wait")
        self.assertEqual(store.audit()["consistent"], True)
        args = cli.parser().parse_args([
            "--config", str(self.file), "work-items", "open", "sample", waiter["id"],
            "--expected-rev", "2", "--reason", "Retired object subject; reopened for triage",
        ])
        reopened = cli._execute(self.config, args)
        self.assertEqual((reopened["status"], reopened.get("wait")), ("open", None))
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
