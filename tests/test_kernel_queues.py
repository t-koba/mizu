"""Declared queues: credentials, schemas, delivery facts, refs, wakes and retention."""
import copy
import datetime as dt
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401  (puts src on the path)
from support import Fixture
from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.fs import atomic_write, canonical
from mizu.kernel import QueueStore, parse_queues
from mizu.kernel.queues import MAX_REFS, MAX_RETENTION_SCAN, PAGE_DEFAULT, PAGE_MAX, _instant
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
NOW = "2026-01-01T00:00:00+00:00"
SOONER = "2026-01-02T00:00:00+00:00"
LATER = "2026-01-03T00:00:00+00:00"
EVEN_LATER = "2026-01-04T00:00:00+00:00"
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

    @staticmethod
    def deliver(store, queue, message, readers=("role:alpha", "operator"), *, now=NOW):
        """Give each concrete reader a delivery fact for one due message."""
        for reader in readers:
            store.record_delivery(queue, message["seq"], reader, "test", now=now)

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

    def test_future_delivery_publishes_a_wake_hint(self):
        # A future delivery writes no role wake. The watched wake directory
        # must still change so a daemon caching its next deadline recomputes.
        before = (self.root / "wake").stat().st_mtime_ns if (self.root / "wake").exists() else None
        self.store.append("tasks", BODY, "role:alpha", deliver_at=instant(1))
        self.assertFalse((self.root / "wake" / "alpha").exists())
        self.assertTrue((self.root / "wake" / "delivery-pending").exists())
        after = (self.root / "wake").stat().st_mtime_ns
        self.assertNotEqual(after, before)

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

    def test_next_delivery_none_and_future_deadline(self):
        # Empty queue and a run of only undelayed messages have no deadline.
        self.assertIsNone(self.store.next_delivery("tasks", "role:alpha", now=NOW))
        self.store.append("tasks", BODY, "operator", now=NOW)
        self.store.append("tasks", BODY, "operator", now=NOW)
        self.assertIsNone(self.store.next_delivery("tasks", "role:alpha", now=NOW))
        # A future delayed message yields the same parsed aware datetime as _instant.
        self.store.append("tasks", BODY, "operator", now=NOW, deliver_at=SOONER)
        self.assertEqual(self.store.next_delivery("tasks", "role:alpha", now=NOW),
                         _instant(SOONER, "deliver_at"))

    def test_next_delivery_respects_head_of_line_and_delivery(self):
        store = QueueStore(self.root / "deadlines", parse_queues(DECLARATIONS))
        # The earlier delayed message (later deadline) masks the one behind it
        # with an earlier deadline: a due-scan cannot cross it.
        store.append("tasks", BODY, "operator", now=NOW, deliver_at=LATER)
        store.append("tasks", BODY, "operator", now=NOW, deliver_at=SOONER)
        self.assertEqual(store.next_delivery("tasks", "role:alpha", now=NOW),
                         _instant(LATER, "deliver_at"))
        # A delivered, due message is no longer awaiting delivery: the scan
        # reaches the next future wake-eligible deadline.
        advanced = QueueStore(self.root / "delivered-deadlines", parse_queues(DECLARATIONS))
        due = advanced.append("tasks", BODY, "operator", now=NOW, deliver_at=SOONER)
        advanced.record_delivery("tasks", due["seq"], "role:alpha", "read", now=LATER)
        advanced.append("tasks", BODY, "operator", now=LATER, deliver_at=EVEN_LATER)
        self.assertEqual(advanced.next_delivery("tasks", "role:alpha", now=LATER),
                         _instant(EVEN_LATER, "deliver_at"))
        # A future message that would not wake the actor yields no deadline.
        threads = QueueStore(self.root / "quiet-threads", parse_queues({"threads": {
            "write": ["role:*"], "read": ["role:*"]}}))
        threads.append("threads", {"q": "?"}, "role:alpha", now=NOW, deliver_at=LATER)
        self.assertIsNone(threads.next_delivery("threads", "role:alpha", now=NOW))
    def test_next_delivery_requires_read_grant(self):
        with self.assertRaises(Denied):
            self.store.next_delivery("notes", "role:beta", now=NOW)

    def test_own_immediate_message_does_not_wake_its_author(self):
        own = self.store.append("tasks", BODY, "role:alpha", now=NOW)
        self.assertFalse((self.root / "wake" / "alpha").exists())
        self.assertFalse(self.store.due("tasks", "role:alpha", now=NOW))
        # An immediate message credits its author with delivery: it is not
        # awaiting delivery but is still readable by paging explicit history.
        self.assertTrue(self.store.delivered("tasks", own["seq"], "role:alpha"))
        self.assertEqual(self.store.pending("tasks", "role:alpha", now=NOW)["count"], 0)
        self.assertEqual([m["id"] for m in self.store.read("tasks", "role:alpha",
                                                          cursor=0, now=NOW)["messages"]],
                         [own["id"]])
        self.store.append("tasks", BODY, "role:beta", now=NOW)
        self.assertTrue((self.root / "wake" / "alpha").exists())
        self.assertTrue(self.store.due("tasks", "role:alpha", now=NOW))
    def test_own_delayed_message_wakes_its_author_when_due(self):
        self.store.append("tasks", BODY, "role:alpha", deliver_at=instant(-1))
        self.assertTrue((self.root / "wake" / "alpha").exists())
        self.assertTrue(self.store.due("tasks", "role:alpha"))

    def test_a_message_wakes_a_reader_once(self):
        message = self.store.append("tasks", BODY, "operator", now=NOW)
        self.assertTrue(self.store.due("tasks", "role:alpha", now=NOW))
        through = self.store.pending("tasks", "role:alpha", now=NOW)["through"]
        self.assertEqual(through, message["seq"])
        self.store.record_delivery("tasks", through, "role:alpha", "read", now=NOW)
        self.assertFalse(self.store.due("tasks", "role:alpha", now=NOW))
        self.assertEqual(self.store.pending("tasks", "role:alpha", now=NOW)["count"], 0)
        # The first fact wins; a repeated call cannot change the wake state.
        self.assertEqual(self.store.record_delivery("tasks", through, "role:alpha",
                                                   "again", now=LATER)["unit"], "read")
        self.assertFalse(self.store.due("tasks", "role:alpha", now=NOW))
        self.store.append("tasks", BODY, "operator", now=NOW)
        self.assertTrue(self.store.due("tasks", "role:alpha", now=NOW))
        with self.assertRaises(Denied):
            self.store.delivered("notes", 1, "role:beta")  # no read grant
    def test_reply_wakes_the_author_it_refers_to(self):
        # A thread queue with no wake list: a reply reaches whoever wrote the message.
        store = QueueStore(self.root, parse_queues({"threads": {
            "write": ["role:*", "operator"], "read": ["role:*", "operator"]}}))
        question = store.append("threads", {"q": "?"}, "role:alpha", now=NOW)
        self.assertFalse((self.root / "wake" / "alpha").exists())
        self.assertFalse(store.due("threads", "role:alpha", now=NOW))
        store.append("threads", {"a": "self"}, "role:alpha", refs=[question["id"]], now=NOW)
        self.assertFalse(store.due("threads", "role:alpha", now=NOW))  # own follow-up is no wake
        reply = store.append("threads", {"a": "!"}, "operator", refs=[question["id"]], now=NOW)
        self.assertTrue((self.root / "wake" / "alpha").exists())
        self.assertFalse((self.root / "wake" / "beta").exists())  # not beta's thread
        self.assertTrue(store.due("threads", "role:alpha", now=NOW))
        self.assertFalse(store.due("threads", "role:beta", now=NOW))
        store.record_delivery("threads", reply["seq"], "role:alpha", "read", now=NOW)
        self.assertFalse(store.due("threads", "role:alpha", now=NOW))
    def test_read_and_delivery_never_cross_an_undelivered_delayed_message(self):
        now = "2026-01-01T00:00:00+00:00"
        later = "2026-01-02T00:00:00+00:00"
        first = self.store.append("notes", {}, "role:alpha", deliver_at=later, now=now)
        second = self.store.append("notes", {}, "host", now=now)
        blocked = self.store.read("notes", "role:alpha", now=now)
        self.assertEqual((blocked["messages"], blocked["next"], blocked["head"],
                          blocked["more"], blocked["delayed"]), ([], 0, 2, True, True))
        with self.assertRaisesRegex(Denied, "undelivered message"):
            self.store.record_delivery("notes", first["seq"], "role:alpha", "read", now=now)
        ready = self.store.read("notes", "role:alpha", now=later)
        self.assertEqual([m["id"] for m in ready["messages"]], [first["id"], second["id"]])
        self.assertEqual((ready["next"], ready["head"], ready["more"], ready["delayed"]),
                         (2, 2, False, False))
        self.deliver(self.store, "notes", first, now=later)
        self.deliver(self.store, "notes", second, now=later)
        self.assertEqual(self.store.read("notes", "role:alpha", now=later)["messages"], [])
    def test_delivery_due_run_does_not_consume_a_later_delayed_wake(self):
        now = "2026-01-01T00:00:00+00:00"
        later = "2026-01-02T00:00:00+00:00"
        due = self.store.append("tasks", BODY, "operator", now=now)
        delayed = self.store.append("tasks", BODY, "operator", now=now, deliver_at=later)
        facts = self.store.pending("tasks", "role:alpha", now=now)
        # A delayed message ends the run: it is neither counted nor delivered,
        # and `through` matches the position a read would reach.
        self.assertEqual((facts["count"], facts["through"]), (1, due["seq"]))
        self.assertEqual(self.store.read("tasks", "role:alpha", now=now)["next"],
                         facts["through"])
        self.store.record_delivery("tasks", due["seq"], "role:alpha", "read", now=now)
        self.assertFalse(self.store.due("tasks", "role:alpha", now=now))
        self.assertTrue(self.store.due("tasks", "role:alpha", now=later))
        # The delayed message can still be read once due, and delivers then.
        page = self.store.read("tasks", "role:alpha", unit="read", now=later)
        self.assertEqual([m["id"] for m in page["messages"]], [delayed["id"]])
        self.assertTrue(self.store.delivered("tasks", delayed["seq"], "role:alpha"))
        self.assertFalse(self.store.due("tasks", "role:alpha", now=later))

    def _indexed_lookups(self, store, queue, actor, now):
        """Compare indexed lookups with a no-index scan; count body loads."""
        real = store._message
        loads = []

        def counted(*args, **kwargs):
            loads.append(1)
            return real(*args, **kwargs)

        def scans():
            return (store.pending(queue, actor, newest=0, now=now),
                    store.due(queue, actor, now=now),
                    store.next_delivery(queue, actor, now=now))

        with patch.object(store, "_message", side_effect=counted):
            indexed_scans = scans()
        with patch.object(store, "_skip_index", return_value=((), ())):
            self.assertEqual(indexed_scans, scans())  # a full scan sees the same facts
            plain_page = store.read(queue, actor, now=now)
        self.assertEqual(store.read(queue, actor, now=now), plain_page)
        return len(loads)  # retention's own cursor check is not a lookup scan

    def test_expired_delayed_head_and_delivered_suffix_skip_independently_of_history(self):
        """An expired delivered deadline joins its delivered run once it is due."""
        loads = []
        for count in (16, 128):
            store = QueueStore(self.root / f"expired-{count}", self.queues)
            for index in range(count):
                options = {"deliver_at": LATER} if index == 0 else {}
                store.append("tasks", BODY, "operator", now=NOW, **options)
            for seq in range(1, count + 1):
                store.record_delivery("tasks", seq, "role:alpha", "read", now=LATER)
            # At an earlier observation time the delivered message is still a
            # boundary: the run may not be crossed even though it has a fact.
            self._indexed_lookups(store, "tasks", "role:alpha", NOW)
            loads.append(self._indexed_lookups(store, "tasks", "role:alpha", LATER))
            facts = store.pending("tasks", "role:alpha", newest=0, now=LATER)
            self.assertEqual((facts["count"], facts["through"], facts["more"]),
                             (0, count, False))
            self.assertFalse(store.due("tasks", "role:alpha", now=LATER))
            self.assertIsNone(store.next_delivery("tasks", "role:alpha", now=LATER))
        # Crossing the whole run costs no body load, at either retained size.
        self.assertEqual(loads, [0, 0])

    def test_undelivered_head_and_delivered_suffix_skip_independently_of_history(self):
        """A delivered run after one non-waking head is skipped without crossing it."""
        loads = []
        for count in (16, 128):
            store = QueueStore(self.root / f"head-{count}", self.queues)
            store.append("notes", {}, "host", now=NOW)  # no wake list for role:alpha
            for index in range(1, count):
                made = store.append("notes", {}, "host", now=NOW)
                self.deliver(store, "notes", made, readers=("role:alpha",), now=NOW)
            loads.append(self._indexed_lookups(store, "notes", "role:alpha", NOW))
            facts = store.pending("notes", "role:alpha", newest=1, now=NOW)
            self.assertEqual((facts["count"], facts["through"], facts["more"]),
                             (1, count, False))
            self.assertEqual([message["seq"] for message in facts["messages"]], [1])
            self.assertFalse(store.due("notes", "role:alpha", now=NOW))
            self.assertIsNone(store.next_delivery("notes", "role:alpha", now=NOW))
        # Each scan loads only the head, whatever the retained suffix length.
        self.assertEqual(loads, [3, 3])

    def test_delivered_future_message_inside_a_run_still_stops_the_scan(self):
        """A handoff fact cannot hide the deadline boundary at an earlier now."""
        first = self.store.append("tasks", BODY, "operator", now=NOW)
        future = self.store.append("tasks", BODY, "operator", now=NOW, deliver_at=LATER)
        tail = self.store.append("tasks", BODY, "operator", now=NOW)
        self.store.append("tasks", BODY, "operator", now=NOW)  # undelivered wake
        self.deliver(self.store, "tasks", first, readers=("role:alpha",), now=NOW)
        self.store.record_delivery("tasks", future["seq"], "role:alpha", "opening",
                                   now=NOW, handed=True)
        self.deliver(self.store, "tasks", tail, readers=("role:alpha",), now=NOW)
        for index, stamp in enumerate((NOW, LATER)):
            with self.subTest(now=stamp):
                self._indexed_lookups(self.store, "tasks", "role:alpha", stamp)
        before = self.store.pending("tasks", "role:alpha", newest=0, now=NOW)
        self.assertEqual((before["count"], before["through"], before["more"]), (0, 1, True))
        self.assertEqual(self.store.read("tasks", "role:alpha", now=NOW)["next"], 1)
        self.assertTrue(self.store.read("tasks", "role:alpha", now=NOW)["delayed"])
        self.assertFalse(self.store.due("tasks", "role:alpha", now=NOW))
        self.assertIsNone(self.store.next_delivery("tasks", "role:alpha", now=NOW))
        after = self.store.pending("tasks", "role:alpha", newest=0, now=LATER)
        self.assertEqual((after["count"], after["through"]), (1, 4))
        self.assertTrue(self.store.due("tasks", "role:alpha", now=LATER))

    def test_corrupt_skip_index_fails_closed_to_a_full_scan(self):
        """Malformed derived state never licenses skipping undelivered rows."""
        message = self.store.append("tasks", BODY, "operator", now=NOW)
        path = self.root / "queues" / "tasks" / "skip" / "role_alpha.json"
        for payload in (b"not json", canonical({"runs": [[1, 2]], "gates": []}),
                        canonical({"runs": [[1, 1]], "gates": [[2, LATER]]})):
            with self.subTest(payload=payload[:40]):
                atomic_write(path, payload)
                self._indexed_lookups(self.store, "tasks", "role:alpha", NOW)
                facts = self.store.pending("tasks", "role:alpha", newest=0, now=NOW)
                self.assertEqual((facts["count"], facts["through"]), (1, 1))
        self.deliver(self.store, "tasks", message, readers=("role:alpha",), now=NOW)
        self.assertEqual(self.store.pending("tasks", "role:alpha", now=NOW)["count"], 0)

    def test_removal_retires_the_messages_entry_from_delivered_runs(self):
        """Retention removes a message's derived skip entry with its facts."""
        raw = copy.deepcopy(DECLARATIONS)
        raw["events"] = {"write": ["host"], "read": ["operator", "role:alpha"],
                         "retain_last": 2}
        store = QueueStore(self.root / "retained-runs", parse_queues(raw))
        first = store.append("events", {}, "host", now=NOW)
        second = store.append("events", {}, "host", now=NOW)
        store.record_delivery("events", first["seq"], "role:alpha", "read", now=NOW)
        store.record_delivery("events", second["seq"], "role:alpha", "read", now=NOW)
        path = self.root / "retained-runs" / "queues" / "events" / "skip" / "role_alpha.json"
        self.assertEqual(json.loads(path.read_text())["runs"], [[1, 2]])
        store.append("events", {}, "host", now=NOW)
        self.assertEqual(json.loads(path.read_text())["runs"], [[2, 2]])
        self.assertEqual(store.pending("events", "role:alpha", newest=0, now=NOW)["count"], 1)

    def test_floor_never_crosses_a_delayed_message(self):
        """The derived floor preserves exact head-of-line for delayed messages."""
        due = self.store.append("tasks", BODY, "operator", now=NOW)
        delayed = self.store.append("tasks", BODY, "operator", now=NOW, deliver_at=LATER)
        tail = self.store.append("tasks", BODY, "operator", now=NOW)
        self.deliver(self.store, "tasks", due, readers=("role:alpha",), now=NOW)
        self.deliver(self.store, "tasks", tail, readers=("role:alpha",), now=NOW)  # delivered out of order
        # The delayed message in between is not crossed even though the tail
        # already has a delivery fact: at NOW it still bounds every due scan.
        self.assertFalse(self.store.due("tasks", "role:alpha", now=NOW))
        self.assertEqual(self.store.pending("tasks", "role:alpha", now=NOW)["count"], 0)
        self.assertEqual(self.store.next_delivery("tasks", "role:alpha", now=NOW),
                         _instant(LATER, "deliver_at"))
        # Once due and delivered, the tail's earlier fact becomes reachable and
        # the scan is clean at the later observation time too.
        self.deliver(self.store, "tasks", delayed, readers=("role:alpha",), now=LATER)
        self.assertFalse(self.store.due("tasks", "role:alpha", now=LATER))
        self.assertIsNone(self.store.next_delivery("tasks", "role:alpha", now=LATER))

    def test_delivery_facts_are_durable_and_idempotent(self):
        first = self.store.append("notes", {}, "host", now=NOW)
        page = self.store.read("notes", "role:alpha", now=NOW)
        self.assertEqual(([m["id"] for m in page["messages"]], page["next"], page["head"],
                          page["more"]), ([first["id"]], 1, 1, False))
        self.assertEqual(self.store.read("notes", "role:alpha", now=NOW)["messages"],
                         page["messages"])  # a plain read consumes no cursor
        self.assertEqual(self.store.record_delivery("notes", 1, "role:alpha", "read",
                                                   now=NOW)["unit"], "read")
        self.assertEqual(self.store.read("notes", "role:alpha", now=NOW)["messages"], [])
        second = self.store.append("notes", {}, "host", now=NOW)
        durable = QueueStore(self.root, self.queues)
        explicit = durable.read("notes", "role:alpha", cursor=0, now=NOW)
        self.assertEqual([m["id"] for m in explicit["messages"]], [first["id"], second["id"]])
        self.assertEqual([m["id"] for m in durable.read("notes", "role:alpha", now=NOW)["messages"]],
                         [second["id"]])
        self.assertEqual(durable.record_delivery("notes", 1, "role:alpha", "again",
                                                now=LATER)["unit"], "read")
        self.assertEqual(durable.record_delivery("notes", 2, "role:alpha", "read",
                                                now=NOW)["unit"], "read")
        for seq in (-1, 3, "1"):
            with self.subTest(seq=seq), self.assertRaises(Denied):
                durable.read("notes", "role:alpha", cursor=seq, now=NOW)
        with self.assertRaises(Denied):
            durable.read("notes", "role:alpha", limit=PAGE_MAX + 1, now=NOW)
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

    def test_by_id_handoff_honours_deadline_and_grant(self):
        """A by-id body is handed over only when due; refs remains a listing."""
        delayed = self.store.append("tasks", BODY, "operator", now=NOW, deliver_at=LATER)
        for options in ({}, {"unit": "probe"}):
            with self.subTest(options=options), self.assertRaises(Denied):
                self.store.message(delayed["id"], "role:alpha", now=NOW, **options)
            self.assertFalse(self.store.delivered("tasks", delayed["seq"], "role:alpha"))
        # An authorized reader can still follow a pending reference by id.
        reply = self.store.append("tasks", BODY, "role:alpha", now=NOW,
                                  refs=[delayed["id"]])
        self.assertEqual([m["id"] for m in
                          self.store.refs(delayed["id"], "to", "role:alpha")["messages"]],
                         [reply["id"]])
        self.assertEqual([m["id"] for m in
                          self.store.refs(reply["id"], "from", "role:alpha")["messages"]],
                         [delayed["id"]])
        # No grant bypass at the deadline, with or without a delivery unit.
        for options in ({}, {"unit": "probe"}):
            with self.subTest(options=options), self.assertRaises(Denied):
                self.store.message(delayed["id"], "role:gamma", now=LATER, **options)
        # Exact deadline is due: a pure read hands the body over but credits
        # nothing, and the delayed wake is still live until a unit reads it.
        self.assertEqual(self.store.message(delayed["id"], "role:alpha", now=LATER), delayed)
        self.assertFalse(self.store.delivered("tasks", delayed["seq"], "role:alpha"))
        self.assertTrue(self.store.due("tasks", "role:alpha", now=LATER))
        self.assertEqual(self.store.message(delayed["id"], "role:alpha", unit="probe",
                                            now=LATER), delayed)
        self.assertEqual(self.store._delivery("tasks", delayed["seq"], "role:alpha"),
                         {"unit": "probe", "at": LATER})
        self.assertEqual(self.store.message(delayed["id"], "role:alpha", unit="other",
                                            now=LATER), delayed)
        self.assertEqual(self.store._delivery("tasks", delayed["seq"], "role:alpha"),
                         {"unit": "probe", "at": LATER})
        self.assertFalse(self.store.due("tasks", "role:alpha", now=LATER))

    def test_pages_are_bounded_and_paged(self):
        for index in range(PAGE_MAX + 3):
            self.store.append("notes", {"n": index}, "host", now=NOW)
        first = self.store.read("notes", "role:alpha", limit=PAGE_MAX, now=LATER)
        self.assertEqual((len(first["messages"]), first["next"], first["head"], first["more"]),
                         (PAGE_MAX, PAGE_MAX, PAGE_MAX + 3, True))
        second = self.store.read("notes", "role:alpha", cursor=first["next"],
                                 limit=PAGE_MAX, now=LATER)
        self.assertEqual((len(second["messages"]), second["more"]), (3, False))
        self.assertEqual(second["messages"][0]["seq"], PAGE_MAX + 1)
        with self.assertRaises(Denied):
            self.store.read("notes", "role:alpha", limit=0, now=LATER)
    def test_retention_needs_all_readers_age_and_no_referrers(self):
        old = self.store.append("notes", {}, "host", now=OLD)
        self.store.read("notes", "role:alpha", now=NOW)  # no delivery fact yet
        self.assertEqual(self.store.message(old["id"], "role:alpha"), old)
        self.deliver(self.store, "notes", old, now=NOW)
        self.store.read("notes", "role:alpha", now=NOW)  # all readers covered
        with self.assertRaises(Denied):
            self.store.message(old["id"], "role:alpha")

        target = self.store.append("notes", {}, "role:alpha", now=OLD)
        referring = self.store.append("notes", {}, "role:alpha", now=OLD, refs=[target["id"]])
        self.deliver(self.store, "notes", target, now=NOW)
        self.deliver(self.store, "notes", referring, now=NOW)
        self.store.read("notes", "role:alpha", now=NOW)
        self.assertEqual(self.store.message(target["id"], "role:alpha"), target)
        with self.assertRaises(Denied):
            self.store.message(referring["id"], "role:alpha")
        self.deliver(self.store, "notes", target, now=NOW)
        self.store.read("notes", "role:alpha", now=NOW)
        with self.assertRaises(Denied):
            self.store.message(target["id"], "role:alpha")

    def test_count_retention_crosses_a_referred_message(self):
        """One live ref must not freeze the prefix, grow the queue or serve stale facts."""
        raw = copy.deepcopy(DECLARATIONS)
        raw["events"] = {"write": ["host"], "read": ["role:*", "operator"], "retain_last": 1}
        root = self.root / "counted"
        store = QueueStore(root, parse_queues(raw))
        pinned = store.append("events", {"n": 1}, "host")
        referrer = store.append("notes", {}, "role:alpha", now=OLD, refs=[pinned["id"]])
        total = MAX_RETENTION_SCAN + 5
        for number in range(2, total + 1):
            store.append("events", {"n": number}, "host")

        queue = root / "queues" / "events"
        retained = json.loads((queue / "retain.json").read_text())
        self.assertGreater(retained["seq"], MAX_RETENTION_SCAN)
        live = sorted(json.loads(p.read_text())["body"]["n"]
                      for p in (queue / "messages").glob("*.json"))
        self.assertEqual(live, [1, total])  # the pinned one and the newest one
        self.assertEqual(store.message(pinned["id"], "role:alpha")["seq"], 1)
        page = store.pending("events", "role:worker", newest=PAGE_MAX)
        self.assertEqual([m["body"]["n"] for m in page["messages"]], [total])
        self.assertEqual((page["count"], page["more"], page["through"]), (1, False, total))

        # The pinned file's end of life is the removal of its last referrer.
        self.deliver(store, "notes", referrer, now=NOW)
        store.read("notes", "role:alpha", now=NOW)
        self.assertFalse((queue / "messages" / f"{pinned['seq']:020d}.json").exists())
        with self.assertRaises(Denied):
            store.message(pinned["id"], "role:alpha")

    def test_delivery_crosses_retention_removed_prefix(self):
        old = self.store.append("notes", {}, "host", now=OLD)
        self.deliver(self.store, "notes", old, now=NOW)
        current = self.store.append("notes", {}, "host", now=NOW)
        self.assertFalse((self.root / "queues" / "notes" / "messages"
                          / f"{old['seq']:020d}.json").exists())
        declared = copy.deepcopy(DECLARATIONS)
        declared["notes"]["read"] = ["role:alpha", "operator", "role:beta"]
        added_reader = QueueStore(self.root, parse_queues(declared))
        page = added_reader.read("notes", "role:beta", now=LATER)
        self.assertEqual([row["seq"] for row in page["messages"]], [current["seq"]])
        self.assertEqual(added_reader.record_delivery("notes", current["seq"], "role:beta",
                                                     "read", now=LATER)["unit"], "read")
        self.assertEqual(added_reader.read("notes", "role:beta", now=LATER)["messages"], [])
    def test_wildcard_reader_never_loses_a_message_to_retention(self):
        message = self.store.append("checks", {}, "host", now=OLD)
        self.deliver(self.store, "checks", message, readers=("host", "role:alpha", "operator"))
        self.store.read("checks", "host", now=NOW)
        self.assertEqual(self.store.message(message["id"], "role:alpha"), message)

    def test_refused_append_does_not_poison_recovery(self):
        # checks grants host both writing and reading, so a host-authored
        # message is readable by role:alpha without an author credit.
        target = self.store.append("checks", {}, "host", now=NOW)
        for index in range(MAX_REFS):
            self.store.append("checks", {"n": index}, "host", refs=[target["id"]], now=NOW)
        with self.assertRaisesRegex(Denied, "Reference index is full"):
            self.store.append("checks", {"n": MAX_REFS}, "host", refs=[target["id"]], now=NOW)
        intent = self.root / "queues" / "intent.json"
        self.assertFalse(intent.exists())
        page = self.store.read("checks", "role:alpha", limit=1, now=LATER)
        self.assertEqual(len(page["messages"]), 1)
        self.assertEqual(self.store.record_delivery("checks", page["messages"][0]["seq"],
                                                   "role:alpha", "read", now=LATER)["unit"],
                         "read")
        self.assertEqual(self.store.append("checks", {}, "host", now=NOW)["seq"], MAX_REFS + 2)

    def test_poisoned_intent_is_discarded_without_wedging_the_store(self):
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
        atomic_write(atomic, canonical({"message": message}))
        resumed = QueueStore(self.root, self.queues)
        page = resumed.read("notes", "role:alpha", cursor=0)
        self.assertEqual(page["head"], 2)
        self.assertEqual([m["id"] for m in page["messages"]],
                         [first["id"], "e" * 32])
        self.assertFalse(atomic.exists())
        self.assertEqual(resumed.append("notes", {}, "role:alpha")["seq"], 3)

    def test_append_credits_due_author_only(self):
        own = self.store.append("tasks", BODY, "role:alpha", now=NOW)
        seq = own["seq"]
        self.assertTrue(self.store.delivered("tasks", seq, "role:alpha"))
        self.assertEqual(self.store._delivery("tasks", seq, "role:alpha"),
                         {"unit": "append", "at": NOW})
        self.assertFalse(self.store.delivered("tasks", seq, "role:beta"))
        delayed = self.store.append("tasks", BODY, "role:alpha", now=NOW, deliver_at=LATER)
        self.assertFalse(self.store.delivered("tasks", delayed["seq"], "role:alpha"))
        self.assertEqual(self.store.record_delivery("tasks", delayed["seq"], "role:alpha",
                                                   "read", now=LATER),
                         {"unit": "read", "at": LATER})
        self.assertTrue(self.store.delivered("tasks", delayed["seq"], "role:alpha"))

    def test_handed_records_future_delivery_but_normal_path_still_refuses(self):
        """The handoff opt-in credits a serialized body; eligibility keeps the deadline."""
        future = self.store.append("tasks", BODY, "operator", now=NOW, deliver_at=LATER)
        with self.assertRaises(Denied):
            self.store.record_delivery("tasks", future["seq"], "role:alpha", "read", now=NOW)
        self.assertFalse(self.store.delivered("tasks", future["seq"], "role:alpha"))
        # Only the handoff path may cross the deadline, and only for a body the reader got.
        self.store.record_delivery("tasks", future["seq"], "role:alpha", "opening",
                                   now=NOW, handed=True)
        self.assertTrue(self.store.delivered("tasks", future["seq"], "role:alpha"))
        # Exact deadline still governs wake: not due before LATER, no wake after delivery.
        self.assertFalse(self.store.due("tasks", "role:alpha", now=NOW))
        self.assertFalse(self.store.due("tasks", "role:alpha", now=LATER))

    def test_record_delivery_is_idempotent_and_validated(self):
        message = self.store.append("tasks", BODY, "operator", now=NOW)
        seq = message["seq"]
        first = self.store.record_delivery("tasks", seq, "role:alpha", "first", now=NOW)
        second = self.store.record_delivery("tasks", seq, "role:alpha", "second", now=LATER)
        self.assertEqual((first, second), ({"unit": "first", "at": NOW}, first))
        self.assertEqual(self.store._delivery("tasks", seq, "role:alpha"), first)
        self.assertTrue(self.store.delivered("tasks", seq, "role:alpha"))
        future = self.store.append("tasks", BODY, "operator", now=NOW, deliver_at=LATER)
        bad = [
            ("tasks", 0, "role:alpha", "read"),
            ("tasks", seq + 2, "role:alpha", "read"),  # beyond head
            ("tasks", future["seq"], "role:alpha", "read"),  # not due yet
            ("notes", 1, "role:beta", "read"),  # no read grant
            ("tasks", seq, "role:alpha", "x" * 129),
            ("tasks", seq, "role:alpha", "bad\x00"),
            ("tasks", seq, "role:alpha", 3),
        ]
        for queue, number, actor, unit in bad:
            with self.subTest(case=bad.index((queue, number, actor, unit))), \
                    self.assertRaises(Denied):
                self.store.record_delivery(queue, number, actor, unit, now=NOW)
        for queue, number, actor in (("tasks", 0, "role:alpha"),
                                     ("tasks", "1", "role:alpha"),
                                     ("notes", 1, "role:beta")):
            with self.subTest(seq=number, actor=actor), self.assertRaises(Denied):
                self.store.delivered(queue, number, actor)

    def test_malformed_delivery_record_fails_closed(self):
        message = self.store.append("notes", {}, "role:alpha", now=NOW)
        seq, actor = message["seq"], "operator"
        self.assertFalse(self.store.delivered("notes", seq, actor))
        path = self.root / "queues" / "notes" / "delivery" / f"{seq:020d}" / "operator.json"
        for payload in ({"unit": "read"},  # missing key
                        {"unit": "read", "at": "2026-01-01T00:00:00"},  # naive
                        {"unit": "read", "at": NOW, "extra": 1},
                        {"unit": "x" * 129, "at": NOW}):
            atomic_write(path, canonical(payload))
            with self.subTest(payload=str(payload)[:40]), self.assertRaises(Denied):
                self.store.delivered("notes", seq, actor)
            with self.assertRaises(Denied):
                self.store.record_delivery("notes", seq, actor, "other", now=NOW)
        path.unlink()
        self.assertFalse(self.store.delivered("notes", seq, actor))

    def test_removing_a_message_removes_its_delivery_facts(self):
        old = self.store.append("notes", {}, "role:alpha", now=OLD)
        directory = self.root / "queues" / "notes" / "delivery" / f"{old['seq']:020d}"
        self.assertTrue(directory.is_dir())
        self.deliver(self.store, "notes", old, now=NOW)
        self.store.read("notes", "role:alpha", now=NOW)
        self.assertFalse(directory.exists())  # age retention
        self.assertFalse(self.store.delivered("notes", old["seq"], "role:alpha"))
        self.store.read("notes", "role:alpha", now=NOW)
        self.store.append("notes", {}, "role:alpha", now=NOW)
        self.assertFalse(directory.exists())  # never recreated

        raw = copy.deepcopy(DECLARATIONS)
        raw["events"] = {"write": ["host"], "read": ["role:*", "operator"], "retain_last": 1}
        root = self.root / "counted-facts"
        store = QueueStore(root, parse_queues(raw))
        first = store.append("events", {"n": 1}, "host", now=NOW)
        store.record_delivery("events", first["seq"], "role:alpha", "read", now=NOW)
        first_dir = root / "queues" / "events" / "delivery" / f"{first['seq']:020d}"
        second = store.append("events", {"n": 2}, "host", now=NOW)
        self.assertFalse(first_dir.exists())  # count retention
        self.assertTrue((root / "queues" / "events" / "messages"
                         / f"{second['seq']:020d}.json").exists())
        store.append("events", {"n": 3}, "host", now=NOW)
        self.assertFalse(first_dir.exists())

        pinned = store.append("events", {"n": 4}, "host", now=NOW)
        store.record_delivery("events", pinned["seq"], "role:alpha", "read", now=NOW)
        pinned_dir = root / "queues" / "events" / "delivery" / f"{pinned['seq']:020d}"
        referrer = store.append("notes", {}, "role:alpha", now=OLD, refs=[pinned["id"]])
        store.append("events", {"n": 5}, "host", now=NOW)
        self.assertTrue(pinned_dir.exists())  # held by the live ref
        self.deliver(store, "notes", referrer, now=NOW)
        store.read("notes", "role:alpha", now=NOW)
        self.assertFalse((root / "queues" / "events" / "messages"
                          / f"{pinned['seq']:020d}.json").exists())
        self.assertFalse(pinned_dir.exists())  # last ref removed it
        store.append("events", {"n": 6}, "host", now=NOW)
        store.read("events", "role:alpha", now=NOW)
        self.assertFalse(pinned_dir.exists())

    def test_generated_id_pointers_retire_with_count_retained_messages(self):
        """Whole retained storage stays bounded, not just the message bodies."""
        raw = copy.deepcopy(DECLARATIONS)
        raw["events"] = {"write": ["host"], "read": ["operator"], "retain_last": 1}
        root = self.root / "generated-pointers"
        store = QueueStore(root, parse_queues(raw))
        bodies = lambda: len(list((root / "queues" / "events" / "messages").glob("*.json")))
        pointers = lambda: len(list((root / "queues" / "ids").glob("*.json")))
        appended = 0
        for total in (20, 200):
            while appended < total:
                store.append("events", {"n": 1}, "host", now=NOW)
                appended += 1
            # Both the bodies and the generated-id pointers are bounded at the
            # retention count, even though hundreds of messages were appended.
            self.assertEqual(bodies(), 1)
            self.assertEqual(pointers(), 1)

    def test_caller_id_tombstone_survives_removal_and_blocks_reuse(self):
        """Explicit ids keep their idempotency tombstone after pruning."""
        raw = copy.deepcopy(DECLARATIONS)
        raw["events"] = {"write": ["host"], "read": ["operator"], "retain_last": 1}
        root = self.root / "caller-tombstone"
        store = QueueStore(root, parse_queues(raw))
        explicit = "a" * 32
        made = store.append("events", {"n": 1}, "host", id=explicit, now=NOW)
        store.append("events", {"n": 2}, "host", now=NOW)  # prune the first
        self.assertFalse((root / "queues" / "events" / "messages"
                          / f"{made['seq']:020d}.json").exists())
        pointer = root / "queues" / "ids" / (explicit + ".json")
        self.assertTrue(pointer.exists())
        self.assertNotIn("id_generated", json.loads(pointer.read_text(encoding="utf-8")))
        with self.assertRaises(Denied):
            store.append("events", {"n": 1}, "host", id=explicit, now=NOW)

    def test_generated_target_pointer_is_kept_while_referrer_is_live(self):
        """A generated-id pointer of a still-referenced message is preserved."""
        raw = copy.deepcopy(DECLARATIONS)
        raw["events"] = {"write": ["host"], "read": ["role:alpha", "operator"],
                         "retain_last": 1}
        root = self.root / "live-ref-pointer"
        store = QueueStore(root, parse_queues(raw))
        target = store.append("events", {"n": 1}, "host", now=NOW)
        pointer = root / "queues" / "ids" / (target["id"] + ".json")
        self.assertTrue(json.loads(pointer.read_text(encoding="utf-8")).get("id_generated"))
        referrer = store.append("notes", {}, "role:alpha", now=OLD, refs=[target["id"]])
        store.append("events", {"n": 2}, "host", now=NOW)  # first held by live ref
        self.assertTrue(pointer.exists())  # live referenced message keeps its pointer
        self.deliver(store, "notes", referrer, now=NOW)
        store.read("notes", "role:alpha", now=NOW)  # last ref goes -> target removed
        self.assertFalse((root / "queues" / "events" / "messages"
                          / f"{target['seq']:020d}.json").exists())
        self.assertFalse(pointer.exists())  # now no live referrer: pointer retires

    def test_generated_id_marker_survives_crash_recovery(self):
        """A recovered generated-id message still retires its pointer."""
        raw = copy.deepcopy(DECLARATIONS)
        raw["events"] = {"write": ["host"], "read": ["operator"], "retain_last": 1}
        root = self.root / "recovery-pointer"
        store = QueueStore(root, parse_queues(raw))
        recovered = {"queue": "events", "seq": 1, "id": "b" * 32, "author": "host",
                     "at": NOW, "deliver_at": None, "refs": [], "body": {"n": 1}}
        atomic_write(root / "queues" / "intent.json",
                     canonical({"message": recovered, "id_generated": True}))
        resumed = QueueStore(root, parse_queues(raw))
        resumed.append("events", {"n": 2}, "host", now=NOW)  # triggers recovery + prune
        self.assertFalse((root / "queues" / "ids" / ("b" * 32 + ".json")).exists())

    def test_interrupted_generated_pointer_is_reconciled_after_reopen(self):
        """A fault after the body unlink leaves an intent that retention finishes."""
        root = self.root / "retire-recovery"
        store = QueueStore(root, self.queues)
        ids = root / "queues" / "ids"
        retire = root / "queues" / "retire"

        caller = store.append("notes", {}, "host", id="c" * 32, now=OLD)
        self.deliver(store, "notes", caller, now=NOW)
        store.read("notes", "role:alpha", now=NOW)  # age retention prunes the caller id
        caller_pointer = ids / (caller["id"] + ".json")
        self.assertTrue(caller_pointer.exists())
        self.assertNotIn("id_generated", json.loads(caller_pointer.read_text(encoding="utf-8")))

        victim = store.append("notes", {}, "host", now=OLD)
        live = store.append("notes", {}, "host", now=NOW)  # not due for removal
        self.deliver(store, "notes", victim, now=NOW)
        victim_pointer = ids / (victim["id"] + ".json")
        live_pointer = ids / (live["id"] + ".json")
        victim_body = root / "queues" / "notes" / "messages" / f"{victim['seq']:020d}.json"
        victim_facts = root / "queues" / "notes" / "delivery" / f"{victim['seq']:020d}"
        skip = root / "queues" / "notes" / "skip" / "role_alpha.json"

        real_remove = store._remove_delivery

        def fail_once(queue, seq):
            if seq == victim["seq"]:
                raise OSError("injected delivery failure")
            real_remove(queue, seq)

        with patch.object(store, "_remove_delivery", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "injected"):
                store.append("notes", {}, "host", now=NOW)

        intent = retire / (victim["id"] + ".json")
        self.assertFalse(victim_body.exists())
        self.assertTrue(victim_facts.is_dir())  # interrupted dependent cleanup
        self.assertEqual(json.loads(skip.read_text(encoding="utf-8"))["runs"], [[victim["seq"]] * 2])
        self.assertTrue(victim_pointer.exists())
        self.assertEqual(json.loads(intent.read_text(encoding="utf-8")),
                         {"id": victim["id"], "queue": "notes", "seq": victim["seq"],
                          "id_generated": True})

        resumed = QueueStore(root, self.queues)
        resumed.append("notes", {}, "host", now=NOW)  # reopen + append runs retention
        self.assertFalse(victim_pointer.exists())
        self.assertFalse(intent.exists())
        self.assertFalse(victim_facts.exists())
        self.assertFalse(skip.exists())  # derived runs finished too
        self.assertTrue(caller_pointer.exists())  # caller tombstone survives
        self.assertTrue(live_pointer.exists())  # pointer to a live message survives
        self.assertTrue((root / "queues" / "notes" / "messages"
                         / f"{live['seq']:020d}.json").exists())
        self.assertEqual(resumed.message(live["id"], "role:alpha"), live)

    def test_retirement_reconciliation_is_bounded_and_idempotent(self):
        root = self.root / "retire-bounded"
        store = QueueStore(root, self.queues)
        pointers, retire = root / "queues" / "ids", root / "queues" / "retire"
        ids = ("a" * 32, "b" * 32)
        for message_id, seq in zip(ids, (1, 2)):
            atomic_write(pointers / (message_id + ".json"),
                         canonical({"queue": "notes", "seq": seq, "id_generated": True}))
            atomic_write(retire / (message_id + ".json"),
                         canonical({"id": message_id, "queue": "notes", "seq": seq,
                                    "id_generated": True}))
        with patch("mizu.kernel.queues.MAX_RETIRE_RECONCILE", 1):
            store._reconcile_retirements()
            self.assertEqual(len(list(retire.glob("*.json"))), 1)
            self.assertEqual(sum((pointers / (item + ".json")).exists() for item in ids), 1)
            store._reconcile_retirements()  # the other intent is the next bounded pass
        self.assertEqual(list(retire.glob("*.json")), [])
        for message_id in ids:
            self.assertFalse((pointers / (message_id + ".json")).exists())
        store._reconcile_retirements()  # a completed reconciliation changes nothing
        self.assertEqual(list(retire.glob("*.json")), [])

        # A crash after the pointer unlink but before the intent unlink is a
        # no-op completion: no pointer to remove, but the intent still expires.
        orphan = "d" * 32
        atomic_write(retire / (orphan + ".json"),
                     canonical({"id": orphan, "queue": "notes", "seq": 3,
                                "id_generated": True}))
        store._reconcile_retirements()
        self.assertFalse((retire / (orphan + ".json")).exists())

    def test_malformed_retire_intent_licenses_no_deletion(self):
        root = self.root / "retire-unsafe"
        store = QueueStore(root, self.queues)
        pointers, retire = root / "queues" / "ids", root / "queues" / "retire"
        cases = [
            {"id": "1" * 32, "queue": "notes", "seq": 1},  # no generated marker
            {"id": "2" * 32, "queue": "notes", "seq": 1, "id_generated": False},
            {"id": "3" * 32, "queue": "notes", "seq": 2, "id_generated": True},  # wrong seq
            {"id": "4" * 32, "queue": "missing", "seq": 1, "id_generated": True},
            {"id": "5" * 32, "queue": "notes", "seq": 1, "id_generated": True, "extra": 1},
        ]
        for index, intent in enumerate(cases):
            message_id = intent["id"]
            with self.subTest(index=index):
                pointer = pointers / (message_id + ".json")
                atomic_write(pointer, canonical({"queue": "notes", "seq": 1,
                                                 "id_generated": True}))
                path = retire / (message_id + ".json")
                atomic_write(path, canonical(intent))
                store._reconcile_retirements()
                self.assertTrue(pointer.exists())
                self.assertTrue(path.exists())
        broken = "6" * 32
        atomic_write(pointers / (broken + ".json"),
                     canonical({"queue": "notes", "seq": 1, "id_generated": True}))
        atomic_write(retire / (broken + ".json"), b"not json")
        store._reconcile_retirements()
        self.assertTrue((pointers / (broken + ".json")).exists())

        # A well-formed intent over a caller-supplied tombstone is unsafe, and
        # so is one over a pointer whose body is still live.
        caller_id = "a" * 32
        atomic_write(pointers / (caller_id + ".json"), canonical({"queue": "notes", "seq": 1}))
        atomic_write(retire / (caller_id + ".json"),
                     canonical({"id": caller_id, "queue": "notes", "seq": 1,
                                "id_generated": True}))
        live = store.append("notes", {}, "host", now=NOW)
        live_intent = retire / (live["id"] + ".json")
        atomic_write(live_intent, canonical({"id": live["id"], "queue": "notes",
                                             "seq": live["seq"], "id_generated": True}))
        store._reconcile_retirements()
        self.assertTrue((pointers / (caller_id + ".json")).exists())
        self.assertTrue((pointers / (live["id"] + ".json")).exists())
        self.assertTrue(live_intent.exists())

    def test_migrate_cursors_converts_once_without_importing_woken(self):
        queue = self.root / "queues" / "tasks"
        for seq in range(1, 5):
            atomic_write(queue / "messages" / f"{seq:020d}.json", canonical({
                "queue": "tasks", "seq": seq, "id": f"{seq:032x}", "author": "operator",
                "at": NOW, "deliver_at": None, "refs": [], "body": BODY}))
        atomic_write(queue / "head.json", canonical({"seq": 4}))
        cursors, woken = queue / "cursors", queue / "woken"
        atomic_write(cursors / "role_alpha.json", canonical({"seq": 2}))
        atomic_write(cursors / "role_beta.json", canonical({"seq": 1}))
        atomic_write(cursors / "operator.json", canonical({"seq": 4}))
        atomic_write(woken / "role_alpha.json", canonical({"seq": 4}))
        atomic_write(woken / "role_beta.json", canonical({"seq": 4}))
        ghost = self.root / "queues" / "ghosts" / "cursors" / "host.json"
        atomic_write(ghost, canonical({"seq": 3}))

        self.assertEqual(self.store.migrate_cursors(), {"migrated": 3})
        self.assertEqual([path.name for path in cursors.glob("*.json")], [])
        self.assertEqual([path.name for path in woken.glob("*.json")], [])
        marker = self.root / "queues" / "delivery-migration.json"
        self.assertEqual(set(json.loads(marker.read_text(encoding="utf-8"))), {"at"})
        self.assertTrue(ghost.exists())  # only declared queues convert
        for seq in (1, 2):
            self.assertEqual(self.store._delivery("tasks", seq, "role:alpha")["unit"], "migration")
            self.assertTrue(self.store.delivered("tasks", seq, "role:alpha"))
        self.assertFalse(self.store.delivered("tasks", 3, "role:alpha"))  # woken 4 not imported
        self.assertTrue(self.store.delivered("tasks", 1, "role:beta"))
        self.assertFalse(self.store.delivered("tasks", 2, "role:beta"))
        for seq in (1, 2, 3, 4):  # operator cursor, not a woken seq
            self.assertEqual(self.store._delivery("tasks", seq, "operator")["unit"], "migration")
        self.assertEqual(self.store.migrate_cursors(), {"migrated": 0})

        # First queue access converts too, once, without importing woken seqs.
        root = self.root / "later"
        store = QueueStore(root, self.queues)
        atomic_write(root / "queues" / "notes" / "messages" / f"{1:020d}.json", canonical({
            "queue": "notes", "seq": 1, "id": "a" * 32, "author": "operator",
            "at": NOW, "deliver_at": None, "refs": [], "body": {}}))
        atomic_write(root / "queues" / "notes" / "head.json", canonical({"seq": 1}))
        cursor = root / "queues" / "notes" / "cursors" / "role_alpha.json"
        woken_file = root / "queues" / "notes" / "woken" / "operator.json"
        atomic_write(cursor, canonical({"seq": 1}))
        atomic_write(woken_file, canonical({"seq": 1}))
        store.read("notes", "role:alpha", now=NOW)
        self.assertTrue(store.delivered("notes", 1, "role:alpha"))
        self.assertFalse(cursor.exists())
        self.assertFalse(woken_file.exists())

    def test_pending_returns_oldest_page_and_drains_in_order(self):
        total = PAGE_DEFAULT + 7
        for number in range(total):
            self.store.append("notes", {"n": number}, "host", now=OLD)
        seen = []
        while True:
            page = self.store.pending("notes", "role:alpha", newest=PAGE_DEFAULT, now=NOW)
            batch = [m["body"]["n"] for m in page["messages"]]
            self.assertEqual(batch, list(range(len(seen), min(len(seen) + PAGE_DEFAULT, total))))
            self.assertEqual(page["count"], total - len(seen))
            seen.extend(batch)
            for message in page["messages"]:
                self.store.record_delivery("notes", message["seq"], "role:alpha", "read", now=NOW)
            if not page["more"]:
                break
        self.assertEqual((page["through"], len(seen)), (total, total))
        self.assertEqual(self.store.pending("notes", "role:alpha",
                                            newest=PAGE_DEFAULT, now=NOW)["count"], 0)

    def test_read_with_unit_records_delivery_and_skips_delivered(self):
        messages = [self.store.append("notes", {"n": number}, "host", now=OLD)
                    for number in range(3)]
        page = self.store.read("notes", "role:alpha", limit=2, unit="consume", now=NOW)
        self.assertEqual([m["id"] for m in page["messages"]],
                         [m["id"] for m in messages[:2]])
        for message in messages[:2]:
            self.assertEqual(self.store._delivery("notes", message["seq"], "role:alpha"),
                             {"unit": "consume", "at": NOW})
            self.assertTrue(self.store.delivered("notes", message["seq"], "role:alpha"))
        self.assertFalse(self.store.delivered("notes", messages[2]["seq"], "role:alpha"))
        facts = self.store.pending("notes", "role:alpha", now=NOW)
        self.assertEqual([m["id"] for m in facts["messages"]], [messages[2]["id"]])
        remaining = self.store.read("notes", "role:alpha", unit="consume", now=NOW)
        self.assertEqual([m["id"] for m in remaining["messages"]], [messages[2]["id"]])
        self.assertEqual(self.store.read("notes", "role:alpha", now=NOW)["messages"], [])
        # An explicit cursor still pages the delivered history, without writing.
        self.assertEqual([m["seq"] for m in self.store.read("notes", "role:alpha",
                                                            cursor=0, now=NOW)["messages"]],
                         [m["seq"] for m in messages])
        self.assertEqual(self.store.read("notes", "role:alpha", now=NOW,
                                         unit="again")["messages"], [])
        self.assertEqual(self.store._delivery("notes", messages[0]["seq"],
                                              "role:alpha")["unit"], "consume")
        for unit in ("x" * 129, "bad\x00", 3):
            with self.subTest(unit=str(unit)[:20]), self.assertRaises(Denied):
                self.store.read("notes", "role:alpha", unit=unit, now=NOW)

    def test_delivery_fact_stops_a_wake_without_a_reader_call(self):
        message = self.store.append("tasks", BODY, "operator", now=NOW)
        self.assertTrue(self.store.due("tasks", "role:alpha", now=NOW))
        self.assertEqual(self.store.pending("tasks", "role:alpha", now=NOW)["count"], 1)
        self.store.record_delivery("tasks", message["seq"], "role:alpha", "read", now=NOW)
        self.assertFalse(self.store.due("tasks", "role:alpha", now=NOW))
        self.assertEqual(self.store.pending("tasks", "role:alpha", now=NOW)["count"], 0)
        # Another reader's facts are independent: it still has the message pending.
        self.assertEqual(self.store.pending("tasks", "role:beta", now=NOW)["count"], 1)

    def test_delivered_prefix_beyond_page_max_cannot_hide_later_messages(self):
        # A delivered prefix longer than the page bound must not push a later
        # undelivered message out of reach of wake, opening, read or deadline.
        # role:beta has no fact, so retention keeps the prefix live.
        for _ in range(PAGE_MAX + 1):
            message = self.store.append("tasks", BODY, "operator", now=NOW)
            self.store.record_delivery("tasks", message["seq"], "role:alpha",
                                       "test", now=NOW)
        future = self.store.append("tasks", BODY, "operator", now=NOW, deliver_at=SOONER)
        self.assertEqual(self.store.next_delivery("tasks", "role:alpha", now=NOW),
                         _instant(SOONER, "deliver_at"))
        self.assertFalse(self.store.due("tasks", "role:alpha", now=NOW))
        self.assertTrue(self.store.due("tasks", "role:alpha", now=SOONER))
        facts = self.store.pending("tasks", "role:alpha", now=SOONER)
        self.assertEqual(facts["count"], 1)
        self.assertEqual([m["seq"] for m in facts["messages"]], [future["seq"]])
        page = self.store.read("tasks", "role:alpha", unit="read", now=SOONER)
        self.assertEqual([m["seq"] for m in page["messages"]], [future["seq"]])
        self.assertTrue(self.store.delivered("tasks", future["seq"], "role:alpha"))
        self.assertFalse(self.store.due("tasks", "role:alpha", now=SOONER))

    def test_explicit_cursor_read_records_delivery_for_returned_rows(self):
        # Handover is authoritative: a cursor sets only the page start, and
        # every row a read returns is recorded delivered (matching the docs).
        message = self.store.append("tasks", BODY, "operator", now=NOW)
        self.assertFalse(self.store.delivered("tasks", message["seq"], "role:alpha"))
        page = self.store.read("tasks", "role:alpha", cursor=0, unit="read", now=NOW)
        self.assertEqual([m["seq"] for m in page["messages"]], [message["seq"]])
        self.assertTrue(self.store.delivered("tasks", message["seq"], "role:alpha"))
        self.assertFalse(self.store.due("tasks", "role:alpha", now=NOW))
        self.assertEqual(self.store.read("tasks", "role:alpha", unit="read",
                                         now=NOW)["messages"], [])

    def test_due_and_next_delivery_skip_delivered_and_respect_head_of_line(self):
        first = self.store.append("tasks", BODY, "operator", now=NOW)
        future = self.store.append("tasks", BODY, "operator", now=NOW, deliver_at=SOONER)
        self.assertEqual(self.store.next_delivery("tasks", "role:alpha", now=NOW),
                         _instant(SOONER, "deliver_at"))
        self.store.record_delivery("tasks", first["seq"], "role:alpha", "read", now=NOW)
        self.assertFalse(self.store.due("tasks", "role:alpha", now=NOW))
        self.assertEqual(self.store.next_delivery("tasks", "role:alpha", now=NOW),
                         _instant(SOONER, "deliver_at"))
        self.assertTrue(self.store.due("tasks", "role:alpha", now=SOONER))
        self.store.record_delivery("tasks", future["seq"], "role:alpha", "read", now=SOONER)
        self.assertFalse(self.store.due("tasks", "role:alpha", now=SOONER))
        self.assertIsNone(self.store.next_delivery("tasks", "role:alpha", now=SOONER))
        # A later, undelivered due message wakes the reader again.
        latest = self.store.append("tasks", BODY, "operator", now=SOONER)
        self.assertTrue(self.store.due("tasks", "role:alpha", now=SOONER))
        self.store.record_delivery("tasks", latest["seq"], "role:alpha", "read", now=SOONER)
        self.assertFalse(self.store.due("tasks", "role:alpha", now=SOONER))

    def test_retention_requires_delivery_fact_for_each_concrete_reader(self):
        message = self.store.append("notes", {}, "host", now=OLD)
        self.store.record_delivery("notes", message["seq"], "role:alpha", "read", now=NOW)
        self.store.read("notes", "role:alpha", now=NOW)  # operator has no fact
        self.assertEqual(self.store.message(message["id"], "role:alpha"), message)
        directory = self.root / "queues" / "notes" / "delivery" / f"{message['seq']:020d}"
        self.store.record_delivery("notes", message["seq"], "operator", "read", now=NOW)
        self.store.read("notes", "role:alpha", now=NOW)
        with self.assertRaises(Denied):
            self.store.message(message["id"], "role:alpha")
        self.assertFalse(directory.exists())  # the removal took the facts with it
        self.assertFalse(self.store.delivered("notes", message["seq"], "operator"))

        # A wildcard reader cannot be proven covered, so retention fails closed.
        raw = copy.deepcopy(DECLARATIONS)
        raw["wild"] = {"write": ["host"], "read": ["role:*", "role:alpha", "operator"],
                       "retain_days": 1}
        store = QueueStore(self.root / "wild", parse_queues(raw))
        kept = store.append("wild", {}, "host", now=OLD)
        self.deliver(store, "wild", kept, now=NOW)
        store.read("wild", "role:alpha", now=NOW)
        self.assertEqual(store.message(kept["id"], "role:alpha"), kept)

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
