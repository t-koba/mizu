"""Declared queues: credentials, schemas, cursors, refs, wakes and retention."""
import copy
import datetime as dt
import tempfile
import tomllib
import unittest
from pathlib import Path

import support  # noqa: F401  (puts src on the path)
from support import Fixture
from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.kernel import QueueStore, parse_queues
from mizu.kernel.queues import MAX_REFS, PAGE_MAX
from mizu.project import Project

DECLARATIONS = tomllib.loads('''
[queues.tasks]
write = ["role:*", "host", "operator"]
read = ["role:alpha", "role:beta"]
wake = ["role:alpha"]
retain_days = 1
[queues.tasks.schema]
title = {type = "string", max = 40, required = true}
count = {type = "integer", min = 0, max = 10}
flag = {type = "boolean"}
when = {type = "instant"}
digest = {type = "digest"}
target = {type = "id"}
principal = {type = "principal"}
tags = {type = "list", item = {type = "string", max = 8}, max_items = 3}
choice = {type = "string", values = ["one", "two"]}

[queues.notes]
write = ["role:alpha", "host"]
read = ["role:alpha", "operator"]
retain_days = 1

[queues.checks]
write = ["host"]
read = ["role:*", "operator", "host"]
wake = ["role:alpha"]

[queues.audit]
write = ["role:alpha"]
read = ["role:alpha"]
''')["queues"]

BODY = {"title": "hello"}
OLD = "2020-01-01T00:00:00+00:00"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "development" / "queues.toml"


def instant(days):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat()


class QueueDeclarations(unittest.TestCase):
    def test_example_and_parsed_shape(self):
        queues = parse_queues(DECLARATIONS)
        self.assertEqual(queues["tasks"].write, ("role:*", "host", "operator"))
        self.assertEqual(queues["tasks"].read, ("role:alpha", "role:beta"))
        self.assertEqual(queues["tasks"].wake, ("role:alpha",))
        self.assertEqual((queues["tasks"].retain_days, queues["checks"].retain_days), (1, 30))
        self.assertEqual(queues["tasks"].fields["choice"].values, ("one", "two"))
        self.assertEqual(queues["tasks"].fields["tags"].item.type, "string")
        self.assertEqual(queues["tasks"].fields["tags"].max_items, 3)
        example = parse_queues(tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["queues"])
        self.assertIn("proposals", example)
        self.assertTrue(example["proposals"].fields["title"].required)
        self.assertIn("work", example)
        # Questions are threads: answers refer to them and wake the asker, not a wake list.
        self.assertEqual(example["questions"].wake, ())
        self.assertNotIn("answers", example)
        self.assertIn("answer", example["questions"].fields)
        work = example["work"].fields
        self.assertTrue(work["kind"].required)
        self.assertEqual(work["kind"].values, ("item", "transition", "wait", "completion"))
        self.assertEqual(work["status"].values,
                         ("open", "doing", "blocked", "done", "dropped"))
        self.assertEqual(work["holder"].type, "principal")
        self.assertEqual(work["checks"].max_items, 64)
        policy = (EXAMPLE.parent / "policies" / "messages.md").read_text(encoding="utf-8")
        for kind in work["kind"].values:
            self.assertIn(kind, policy)
        self.assertNotIn("revision", policy)
        readme = (EXAMPLE.parent / "README.md").read_text(encoding="utf-8")
        self.assertIn("transitions, waits and completions", readme)

    def test_unknown_and_invalid_declarations_refuse(self):
        base = '[queues.q]\nwrite = ["role:alpha"]\nread = ["role:alpha"]\n'
        invalid = [
            'queues = 3',
            '[queues.Upper]\nwrite = ["host"]\nread = ["host"]\n',
            base + 'extra = 1\n',
            '[queues.q]\nwrite = ["alpha"]\nread = ["role:alpha"]\n',
            '[queues.q]\nwrite = ["role:Alpha"]\nread = ["role:alpha"]\n',
            '[queues.q]\nwrite = []\nread = ["role:alpha"]\n',
            base + 'wake = ["role:beta"]\n',
            base + 'wake = ["host"]\n',
            base + 'wake = ["role:*"]\n',
            '[queues.q]\nwrite = ["host"]\nread = ["role:*"]\nwake = ["role:*"]\n',
            base + 'retain_days = 0\n',
            base + 'retain_days = -1\n',
            base + 'retain_days = "3"\n',
            base + 'retain_days = true\n',
            base + 'schema = 1\n',
            base + 'schema.title = {type = "string", bogus = 1}\n',
            base + 'schema.title = {type = "money"}\n',
            base + 'schema.title = {type = "string", min = 1}\n',
            base + 'schema.count = {type = "integer", values = ["1"]}\n',
            base + 'schema.title = {type = "string", max_items = 2}\n',
            base + 'schema.tags = {type = "list", item = {type = "string"}}\n',
            base + 'schema.tags = {type = "list", item = {type = "list", item = {type = "string"}, max_items = 2}, max_items = 2}\n',
            base + 'schema.tags = {type = "list", item = {type = "string"}, max_items = 0}\n',
            base + '"Bad field" = {type = "string"}\n',
        ]
        for text in invalid:
            with self.subTest(text=text[-60:]), self.assertRaises(ConfigError):
                parse_queues(tomllib.loads(text)["queues"])


class QueueStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.queues = parse_queues(DECLARATIONS)
        self.store = QueueStore(self.root, self.queues)

    def test_grants_and_bound_actor(self):
        made = self.store.append("tasks", BODY, "role:alpha")
        self.assertEqual(made["author"], "role:alpha")
        self.assertEqual(made["seq"], 1)
        self.assertEqual(set(made), {"queue", "seq", "id", "author", "at", "deliver_at",
                                     "refs", "body"})
        alien = copy.deepcopy(DECLARATIONS)
        alien["audit"]["schema"] = {"actor": {"type": "string"}}
        store = QueueStore(self.root / "bound", parse_queues(alien))
        bound = store.append("audit", {"actor": "role:beta"}, "role:alpha")
        self.assertEqual((bound["author"], bound["body"]["actor"]), ("role:alpha", "role:beta"))
        self.store.append("tasks", BODY, "role:gamma")  # role:* grants writing
        self.store.append("tasks", BODY, "operator")
        for queue, actor in (("notes", "role:beta"), ("checks", "role:beta"),
                             ("tasks", "role:*"), ("missing", "role:alpha")):
            with self.subTest(queue=queue, actor=actor), self.assertRaises(Denied):
                self.store.append(queue, BODY, actor)
        for queue, actor in (("tasks", "role:gamma"), ("notes", "role:beta"),
                             ("missing", "role:alpha")):
            with self.subTest(queue=queue, actor=actor), self.assertRaises(Denied):
                self.store.read(queue, actor)
        with self.assertRaises(TypeError):  # a role argument never exists on the API
            self.store.append("tasks", BODY, "role:alpha", role="role:beta")

    def test_supplied_ids_are_idempotent_and_conflicts_refuse(self):
        message_id = "ab" * 16
        first = self.store.append("tasks", BODY, "role:alpha", id=message_id)
        again = self.store.append("tasks", BODY, "role:alpha", id=message_id)
        self.assertEqual(first, again)
        self.assertEqual(self.store.read("tasks", "role:alpha")["head"], 1)
        for kwargs in ({"body": {"title": "other"}}, {"actor": "role:beta"},
                       {"queue": "notes"}, {"refs": [first["id"]]},
                       {"deliver_at": OLD}):
            args = {"queue": kwargs.pop("queue", "tasks"), "body": BODY,
                    "actor": "role:alpha", "id": message_id}
            args.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(Denied):
                self.store.append(**args)
        self.assertEqual(self.store.append("tasks", BODY, "role:alpha")["seq"], 2)
        with self.assertRaises(Denied):
            self.store.append("tasks", BODY, "role:alpha", id="not-an-id")
        with self.assertRaises(Denied):
            self.store.append("notes", {}, "role:alpha", id=message_id)

    def test_schema_validation_is_fail_closed(self):
        good = {"title": "ok", "count": 3, "flag": True, "when": instant(0),
                "digest": "a" * 64, "target": "b" * 32, "principal": "role:beta",
                "tags": ["a", "bb"], "choice": "one"}
        self.assertEqual(self.store.append("tasks", good, "role:alpha")["body"], good)
        bad = [
            {},
            {**good, "title": "x" * 41},
            {**good, "title": 3},
            {**good, "count": 11},
            {**good, "count": -1},
            {**good, "count": 1.5},
            {**good, "count": True},
            {**good, "flag": "true"},
            {**good, "when": "2020-01-01T00:00:00"},
            {**good, "digest": "A" * 64},
            {**good, "target": "b" * 64},
            {**good, "principal": "Alpha"},
            {**good, "tags": ["a", "b", "c", "d"]},
            {**good, "tags": [3]},
            {**good, "choice": "three"},
            {**good, "extra": 1},
        ]
        for body in bad:
            with self.subTest(body=str(body)[:60]), self.assertRaises(Denied):
                self.store.append("tasks", body, "role:alpha")
        for body in (None, [], "text", {1: "x"}):
            with self.subTest(body=body), self.assertRaises(Denied):
                self.store.append("notes", body, "role:alpha")
        with self.assertRaises(Denied):
            self.store.append("notes", {"large": "x" * (1024 * 1024 + 1)}, "role:alpha")

    def test_delayed_delivery_and_wake_files(self):
        self.store.append("tasks", BODY, "role:alpha", deliver_at=instant(1))
        self.assertFalse((self.root / "wake" / "alpha").exists())
        blocked = self.store.read("tasks", "role:alpha")
        self.assertEqual(blocked["messages"], [])
        self.assertTrue(blocked["delayed"])
        self.store.append("tasks", BODY, "role:alpha", deliver_at=instant(-1))
        self.assertIn('"at"', (self.root / "wake" / "alpha").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "wake" / "beta").exists())
        page = self.store.read("tasks", "role:alpha")
        self.assertEqual(page["messages"], [])  # head-of-line: first message is not due
        self.assertTrue(page["delayed"])
        self.store.append("checks", {"snapshot": "s"}, "host", deliver_at=instant(-1))
        self.assertTrue((self.root / "wake" / "alpha").exists())
        with self.assertRaises(Denied):
            self.store.append("tasks", BODY, "role:alpha", deliver_at="2020-01-01T00:00:00")

    def test_own_immediate_message_does_not_wake_its_author(self):
        self.store.append("tasks", BODY, "role:alpha")
        self.assertFalse((self.root / "wake" / "alpha").exists())
        self.assertFalse(self.store.due("tasks", "role:alpha"))
        self.assertEqual(self.store.pending("tasks", "role:alpha")["count"], 1)  # still readable
        self.store.append("tasks", BODY, "role:beta")
        self.assertTrue((self.root / "wake" / "alpha").exists())
        self.assertTrue(self.store.due("tasks", "role:alpha"))

    def test_own_delayed_message_wakes_its_author_when_due(self):
        self.store.append("tasks", BODY, "role:alpha", deliver_at=instant(-1))
        self.assertTrue((self.root / "wake" / "alpha").exists())
        self.assertTrue(self.store.due("tasks", "role:alpha"))

    def test_a_message_wakes_a_reader_once(self):
        self.store.append("tasks", BODY, "operator")
        self.assertTrue(self.store.due("tasks", "role:alpha"))
        through = self.store.pending("tasks", "role:alpha")["through"]
        self.assertEqual(self.store.mark_woken("tasks", "role:alpha", through), 1)
        self.assertFalse(self.store.due("tasks", "role:alpha"))  # unacknowledged, not a standing wake
        self.assertEqual(self.store.pending("tasks", "role:alpha")["count"], 1)
        self.assertEqual(self.store.mark_woken("tasks", "role:alpha", 0), 1)  # monotonic
        self.assertEqual(self.store.mark_woken("tasks", "role:alpha", 99), 1)  # bounded by head
        self.store.append("tasks", BODY, "operator")
        self.assertTrue(self.store.due("tasks", "role:alpha"))
        with self.assertRaises(Denied):
            self.store.mark_woken("notes", "role:beta", 1)

    def test_reply_wakes_the_author_it_refers_to(self):
        # A thread queue with no wake list: a reply reaches whoever wrote the message.
        store = QueueStore(self.root, parse_queues({"threads": {
            "write": ["role:*", "operator"], "read": ["role:*", "operator"]}}))
        question = store.append("threads", {"q": "?"}, "role:alpha")
        self.assertFalse((self.root / "wake" / "alpha").exists())
        self.assertFalse(store.due("threads", "role:alpha"))
        store.append("threads", {"a": "self"}, "role:alpha", refs=[question["id"]])
        self.assertFalse(store.due("threads", "role:alpha"))  # own follow-up is no wake
        store.append("threads", {"a": "!"}, "operator", refs=[question["id"]])
        self.assertTrue((self.root / "wake" / "alpha").exists())
        self.assertFalse((self.root / "wake" / "beta").exists())  # not beta's thread
        self.assertTrue(store.due("threads", "role:alpha"))
        self.assertFalse(store.due("threads", "role:beta"))
        store.mark_woken("threads", "role:alpha", store.pending("threads", "role:alpha")["through"])
        self.assertFalse(store.due("threads", "role:alpha"))

    def test_read_and_ack_never_cross_an_undelivered_delayed_message(self):
        now = "2026-01-01T00:00:00+00:00"
        later = "2026-01-02T00:00:00+00:00"
        first = self.store.append("notes", {}, "role:alpha", deliver_at=later, now=now)
        second = self.store.append("notes", {}, "role:alpha", now=now)
        blocked = self.store.read("notes", "role:alpha", now=now)
        self.assertEqual((blocked["messages"], blocked["next"], blocked["head"],
                          blocked["more"], blocked["delayed"]), ([], 0, 2, True, True))
        with self.assertRaisesRegex(Denied, "undelivered message"):
            self.store.ack("notes", "role:alpha", second["seq"], now=now)
        ready = self.store.read("notes", "role:alpha", now=later)
        self.assertEqual([m["id"] for m in ready["messages"]], [first["id"], second["id"]])
        self.assertEqual((ready["next"], ready["head"], ready["more"], ready["delayed"]),
                         (2, 2, False, False))
        self.assertEqual(self.store.ack("notes", "role:alpha", second["seq"], now=later), 2)
        self.assertEqual(self.store.read("notes", "role:alpha", now=later)["messages"], [])

    def test_woken_due_run_does_not_consume_a_later_delayed_wake(self):
        now = "2026-01-01T00:00:00+00:00"
        later = "2026-01-02T00:00:00+00:00"
        due = self.store.append("tasks", BODY, "operator", now=now)
        delayed = self.store.append("tasks", BODY, "operator", now=now, deliver_at=later)
        facts = self.store.pending("tasks", "role:alpha", now=now)
        # A delayed message ends the run: it is neither counted nor marked woken,
        # and `through` matches the position a read would reach.
        self.assertEqual((facts["count"], facts["through"]), (1, due["seq"]))
        self.assertEqual(self.store.read("tasks", "role:alpha", now=now)["next"],
                         facts["through"])
        self.store.mark_woken("tasks", "role:alpha", facts["through"])
        self.store.ack("tasks", "role:alpha", due["seq"], now=now)
        self.assertFalse(self.store.due("tasks", "role:alpha", now=now))
        self.assertTrue(self.store.due("tasks", "role:alpha", now=later))
        # The delayed message itself can still be read and acked once due.
        page = self.store.read("tasks", "role:alpha", now=later)
        self.assertEqual([m["id"] for m in page["messages"]], [delayed["id"]])

    def test_cursors_and_acks_are_durable_and_monotonic(self):
        first = self.store.append("notes", {}, "role:alpha")
        page = self.store.read("notes", "role:alpha")
        self.assertEqual(([m["id"] for m in page["messages"]], page["next"], page["head"],
                          page["more"]), ([first["id"]], 1, 1, False))
        self.assertEqual(self.store.read("notes", "role:alpha")["messages"], page["messages"])
        self.assertEqual(self.store.ack("notes", "role:alpha", 1), 1)
        self.assertEqual(self.store.read("notes", "role:alpha")["messages"], [])
        second = self.store.append("notes", {}, "role:alpha")
        durable = QueueStore(self.root, self.queues)
        explicit = durable.read("notes", "role:alpha", cursor=0)
        self.assertEqual([m["id"] for m in explicit["messages"]], [first["id"], second["id"]])
        self.assertEqual([m["id"] for m in durable.read("notes", "role:alpha")["messages"]],
                         [second["id"]])
        self.assertEqual(durable.ack("notes", "role:alpha", 1), 1)
        self.assertEqual(durable.ack("notes", "role:alpha", 2), 2)
        for seq in (-1, 3, "1"):
            with self.subTest(seq=seq), self.assertRaises(Denied):
                durable.ack("notes", "role:alpha", seq)
        with self.assertRaises(Denied):
            durable.read("notes", "role:alpha", cursor=3)
        with self.assertRaises(Denied):
            durable.read("notes", "role:alpha", limit=PAGE_MAX + 1)

    def test_references_in_both_directions(self):
        target = self.store.append("checks", {}, "host")
        referring = self.store.append("tasks", BODY, "role:alpha", refs=[target["id"]])
        other = self.store.append("checks", {}, "host", refs=[target["id"]])
        self.assertEqual(self.store.message(target["id"], "role:alpha"), target)
        to = self.store.refs(target["id"], "to", "role:alpha")
        self.assertEqual([m["id"] for m in to["messages"]],
                         sorted([referring["id"], other["id"]]))
        self.assertEqual([m["id"] for m in self.store.refs(referring["id"], "from", "role:alpha")["messages"]],
                         [target["id"]])
        self.assertEqual([m["id"] for m in self.store.refs(target["id"], "to", "role:gamma")["messages"]],
                         [other["id"]])
        page = self.store.refs(target["id"], "to", "role:alpha", limit=1)
        self.assertEqual(len(page["messages"]), 1)
        self.assertTrue(page["more"])
        rest = self.store.refs(target["id"], "to", "role:alpha", limit=1, cursor=page["next"])
        self.assertEqual(len(rest["messages"]), 1)
        self.assertFalse(rest["more"])
        for call in (lambda: self.store.refs(target["id"], "sideways", "role:alpha"),
                     lambda: self.store.refs(referring["id"], "from", "role:gamma"),
                     lambda: self.store.refs("c" * 32, "to", "role:alpha"),
                     lambda: self.store.append("checks", {}, "host", refs=["d" * 32]),
                     lambda: self.store.append("tasks", BODY, "role:gamma",
                                               refs=[self.store.append("notes", {}, "role:alpha")["id"]])):
            with self.assertRaises(Denied):
                call()

    def test_pages_are_bounded_and_paged(self):
        for index in range(PAGE_MAX + 3):
            self.store.append("notes", {"n": index}, "role:alpha")
        first = self.store.read("notes", "role:alpha", limit=PAGE_MAX)
        self.assertEqual((len(first["messages"]), first["next"], first["head"], first["more"]),
                         (PAGE_MAX, PAGE_MAX, PAGE_MAX + 3, True))
        second = self.store.read("notes", "role:alpha", cursor=first["next"], limit=PAGE_MAX)
        self.assertEqual((len(second["messages"]), second["more"]), (3, False))
        self.assertEqual(second["messages"][0]["seq"], PAGE_MAX + 1)
        with self.assertRaises(Denied):
            self.store.read("notes", "role:alpha", limit=0)

    def test_retention_needs_all_readers_age_and_no_referrers(self):
        old = self.store.append("notes", {}, "role:alpha", now=OLD)
        self.store.read("notes", "role:alpha")  # reader cursor still zero
        self.assertEqual(self.store.message(old["id"], "role:alpha"), old)
        self.store.ack("notes", "role:alpha", old["seq"])
        self.store.read("notes", "role:alpha")  # operator has not acknowledged
        self.assertEqual(self.store.message(old["id"], "role:alpha"), old)
        self.store.ack("notes", "operator", old["seq"])
        self.store.read("notes", "role:alpha")
        with self.assertRaises(Denied):
            self.store.message(old["id"], "role:alpha")

        target = self.store.append("notes", {}, "role:alpha", now=OLD)
        referring = self.store.append("notes", {}, "role:alpha", now=OLD, refs=[target["id"]])
        for reader in ("role:alpha", "operator"):
            self.store.ack("notes", reader, referring["seq"])
        self.store.read("notes", "role:alpha")
        self.assertEqual(self.store.message(target["id"], "role:alpha"), target)
        with self.assertRaises(Denied):
            self.store.message(referring["id"], "role:alpha")
        self.store.read("notes", "role:alpha")
        with self.assertRaises(Denied):
            self.store.message(target["id"], "role:alpha")

    def test_ack_crosses_retention_removed_prefix(self):
        old = self.store.append("notes", {}, "role:alpha", now=OLD)
        for reader in ("role:alpha", "operator"):
            self.store.ack("notes", reader, old["seq"])
        current = self.store.append("notes", {}, "role:alpha")
        self.assertFalse((self.root / "queues" / "notes" / "messages"
                          / f"{old['seq']:020d}.json").exists())
        declared = copy.deepcopy(DECLARATIONS)
        declared["notes"]["read"] = ["role:alpha", "operator", "role:beta"]
        added_reader = QueueStore(self.root, parse_queues(declared))
        page = added_reader.read("notes", "role:beta")
        self.assertEqual([row["seq"] for row in page["messages"]], [current["seq"]])
        self.assertEqual(added_reader.ack("notes", "role:beta", current["seq"]), current["seq"])
        self.assertEqual(added_reader.read("notes", "role:beta")["messages"], [])

    def test_wildcard_reader_never_loses_a_message_to_retention(self):
        message = self.store.append("checks", {}, "host", now=OLD)
        for reader in ("host", "role:alpha", "operator"):
            self.store.ack("checks", reader, message["seq"])
        self.store.read("checks", "host")
        self.assertEqual(self.store.message(message["id"], "role:alpha"), message)

    def test_refused_append_does_not_poison_recovery(self):
        from mizu.fs import atomic_write, canonical
        target = self.store.append("notes", {}, "role:alpha")
        for index in range(MAX_REFS):
            self.store.append("notes", {"n": index}, "role:alpha", refs=[target["id"]])
        with self.assertRaisesRegex(Denied, "Reference index is full"):
            self.store.append("notes", {"n": MAX_REFS}, "role:alpha", refs=[target["id"]])
        intent = self.root / "queues" / "intent.json"
        self.assertFalse(intent.exists())
        page = self.store.read("notes", "role:alpha", limit=1)
        self.assertEqual(len(page["messages"]), 1)
        self.assertEqual(self.store.ack("notes", "role:alpha", page["messages"][0]["seq"]), 1)
        self.assertEqual(self.store.append("notes", {}, "role:alpha")["seq"], MAX_REFS + 2)

    def test_poisoned_intent_is_discarded_without_wedging_the_store(self):
        from mizu.fs import atomic_write, canonical
        target = self.store.append("notes", {}, "role:alpha")
        for index in range(MAX_REFS):
            self.store.append("notes", {"n": index}, "role:alpha", refs=[target["id"]])
        intent = self.root / "queues" / "intent.json"
        poison = {"queue": "notes", "seq": MAX_REFS + 2, "id": "f" * 32,
                  "author": "role:alpha", "at": "2026-01-01T00:00:00+00:00",
                  "deliver_at": None, "refs": [target["id"]], "body": {}}
        atomic_write(intent, canonical({"message": poison}))
        with self.assertRaisesRegex(Denied, "Reference index is full"):
            self.store.read("notes", "role:alpha", limit=1)
        self.assertFalse(intent.exists())
        self.assertEqual(self.store.append("notes", {}, "role:alpha")["seq"], MAX_REFS + 2)

    def test_delayed_intent_for_removed_queue_recovers_without_wedging(self):
        from mizu.fs import atomic_write, canonical
        # Both sides of the delivery instant: a due replay must also finish
        # without the declaration it would otherwise use to emit wakes.
        for deliver_at in (instant(1), instant(-1)):
            with self.subTest(deliver_at=deliver_at), tempfile.TemporaryDirectory() as work:
                root = Path(work)
                atomic = root / "queues" / "intent.json"
                message = {"queue": "checks", "seq": 1, "id": "b" * 32, "author": "host",
                           "at": "2026-01-01T00:00:00+00:00", "deliver_at": deliver_at,
                           "refs": [], "body": {}}
                atomic_write(atomic, canonical({"message": message}))
                reduced = QueueStore(root, parse_queues({"notes": DECLARATIONS["notes"]}))
                self.assertEqual(reduced.read("notes", "role:alpha")["messages"], [])
                self.assertFalse(atomic.exists())
                self.assertTrue((root / "queues" / "checks" / "messages"
                                 / f"{message['seq']:020d}.json").exists())
                self.assertFalse((root / "wake" / "alpha").exists())
                self.assertEqual(reduced.append("notes", {}, "role:alpha")["seq"], 1)

    def test_interrupted_append_is_completed_on_recovery(self):
        first = self.store.append("notes", {}, "role:alpha")
        message = dict(first)
        message["seq"], message["id"] = 2, "e" * 32
        atomic = self.root / "queues" / "intent.json"
        from mizu.fs import atomic_write, canonical
        atomic_write(atomic, canonical({"message": message}))
        resumed = QueueStore(self.root, self.queues)
        page = resumed.read("notes", "role:alpha")
        self.assertEqual(page["head"], 2)
        self.assertEqual([m["id"] for m in page["messages"]],
                         [first["id"], "e" * 32])
        self.assertFalse(atomic.exists())
        self.assertEqual(resumed.append("notes", {}, "role:alpha")["seq"], 3)


class QueueConfiguration(Fixture):
    def test_top_level_section_and_lazy_project_accessor(self):
        original = self.file.read_text()
        section = '\n[queues.tasks]\nwrite = ["role:*"]\nread = ["role:worker"]\n'
        self.file.write_text(original + section)
        config = load(self.file)
        self.assertEqual(config.queues["tasks"].read, ("role:worker",))
        project = Project(config, self.project.name)
        store = project.kernel_queues
        self.assertIsInstance(store, QueueStore)
        self.assertIs(project.kernel_queues, store)
        self.file.write_text(original + section + 'wake = ["role:reporter"]\n')
        with self.assertRaises(ConfigError):
            load(self.file)


if __name__ == "__main__":
    unittest.main()
