"""Kernel objects, requirements, events, subscriptions, instants and recovery."""
import datetime as dt
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401  (puts src on the path)
from mizu.errors import ConfigError, Denied
from mizu.fs import now
from mizu.kernel import EventLog, Objects, matches, parse_kinds, parse_subscriptions

KINDS = tomllib.loads('''
[kinds.check]
states = ["running", "passed", "failed"]
initial = "running"
create = ["host"]
fields.snapshot = {type = "string", max = 64, required = true}
[[kinds.check.transitions]]
name = "pass"
from = ["running"]
to = "passed"
by = ["host"]

[kinds.task]
states = ["open", "closed"]
initial = "open"
create = ["role:alpha", "operator"]
fields.title = {type = "string", max = 80, required = true}
fields.holder = {type = "principal"}
fields.evidence = {type = "ref", kinds = ["check"]}
fields.due = {type = "instant", event = "task.due"}
fields.tags = {type = "list", item = {type = "string", max = 16}, max_items = 3}
[[kinds.task.transitions]]
name = "claim"
from = ["open"]
to = "open"
by = ["role:alpha", "role:beta"]
fields = ["holder"]
[[kinds.task.transitions]]
name = "close"
from = ["open"]
to = "closed"
by = ["holder"]
fields = ["evidence"]
requires = [{ref = "evidence", state = ["passed"], where = {snapshot = "$snapshot"}}]
emits = "task.finished"
''')["kinds"]


class Kernel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.kinds = parse_kinds(KINDS)
        self.log = EventLog(self.root, {"beta": ("task.*",), "gamma": ("**",), "delta": ("task.due",)})
        self.objects = Objects(self.root, self.kinds, self.log)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lifecycle_requirements_and_events(self):
        task = self.objects.create("task", {"title": "t"}, "role:alpha")
        self.assertEqual((task["state"], task["rev"]), ("open", 1))
        with self.assertRaises(Denied):
            self.objects.create("task", {"title": "t"}, "role:beta")
        with self.assertRaises(Denied):
            self.objects.create("task", {}, "role:alpha")
        claimed = self.objects.transition("task", task["id"], "claim", expected_rev=1,
                                          fields={"holder": "role:beta"}, actor="role:beta")
        with self.assertRaises(Denied):  # stale revision
            self.objects.transition("task", task["id"], "claim", expected_rev=1, fields={}, actor="role:beta")
        check = self.objects.create("check", {"snapshot": "s1"}, "host")
        with self.assertRaisesRegex(Denied, "Requirement not met"):  # check still running
            self.objects.transition("task", task["id"], "close", expected_rev=2,
                                    fields={"evidence": check["id"]}, actor="role:beta", snapshot="s1")
        self.objects.transition("check", check["id"], "pass", expected_rev=1, fields={}, actor="host")
        with self.assertRaises(Denied):  # only the holder closes
            self.objects.transition("task", task["id"], "close", expected_rev=2,
                                    fields={"evidence": check["id"]}, actor="role:alpha", snapshot="s1")
        with self.assertRaisesRegex(Denied, "Requirement not met"):  # evidence bound to another snapshot
            self.objects.transition("task", task["id"], "close", expected_rev=2,
                                    fields={"evidence": check["id"]}, actor="role:beta", snapshot="s2")
        closed = self.objects.transition("task", task["id"], "close", expected_rev=claimed["rev"],
                                         fields={"evidence": check["id"]}, actor="role:beta", snapshot="s1")
        self.assertEqual((closed["state"], closed["rev"]), ("closed", 3))
        self.assertEqual([r["rev"] for r in (self.objects.read("task", task["id"], 1),)], [1])
        names = [self.log.read(seq)["name"] for seq in range(1, self.log.head() + 1)]
        self.assertEqual(names, ["task.created", "task.claim", "check.created", "check.pass", "task.finished"])
        self.assertEqual([e["name"] for e in self.log.pending("beta")["events"]],
                         ["task.created", "task.claim", "task.finished"])
        self.assertTrue((self.root / "wake" / "beta").exists())

    def test_cursor_ack_and_paging(self):
        for index in range(5):
            self.objects.create("task", {"title": str(index)}, "operator")
        page = self.log.pending("gamma", limit=2)
        self.assertEqual(len(page["events"]), 2)
        self.assertTrue(page["more"])
        self.log.ack("gamma", page["events"][-1]["seq"])
        self.assertEqual(self.log.pending("gamma", limit=10)["events"][0]["seq"], 3)
        self.log.ack("gamma", 1)  # never moves backward
        self.assertEqual(self.log.cursor("gamma"), 2)
        with self.assertRaises(Denied):
            self.log.ack("gamma", 99)

    def test_instants_fire_once(self):
        soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
        task = self.objects.create("task", {"title": "t", "due": soon}, "operator")
        self.assertEqual(self.objects.next_instant(), soon)
        self.assertEqual(self.objects.fire_due(), [])
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=6)
        fired = self.objects.fire_due(later)
        self.assertEqual([(e["name"], e["id"]) for e in fired], [("task.due", task["id"])])
        self.assertEqual(self.objects.fire_due(later), [])
        self.assertIsNone(self.objects.next_instant())
        self.assertEqual(list((self.root / "timers").glob("*.json")), [])

    def test_reserved_event_write_is_idempotent_and_fails_closed(self):
        event = self.log.append("task.created", {"title": "t"}, "operator")
        (self.root / "events" / "head.json").unlink()  # crash after the file, before the head
        self.assertEqual(self.log.append_at(event), event)  # an identical retry repairs it
        self.assertEqual(self.log.head(), 1)
        with self.assertRaises(Denied):
            self.log.append_at({**event, "actor": "host"})  # different content at the same seq
        self.assertEqual(self.log.read(1), event)
        self.assertEqual(self.log.append("task.created", {"title": "u"}, "operator")["seq"], 2)

    def test_recovery_publishes_a_firing_whose_event_was_not_appended(self):
        soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
        task = self.objects.create("task", {"title": "t", "due": soon}, "operator")
        event = {"seq": self.log.head() + 1, "name": "task.due", "kind": "task", "id": task["id"],
                 "field": "due", "due": soon, "actor": "host", "at": now()}
        (self.root / "timers" / ".firing.json").write_text(
            json.dumps({"seq": event["seq"], "event": event}), encoding="utf-8")
        fresh = Objects(self.root, self.kinds, self.log)
        self.assertEqual(fresh.fire_due(dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=6)), [])
        self.assertEqual(self.log.read(event["seq"]), event)
        self.assertEqual([row["name"] for row in self.log.pending("delta")["events"]], ["task.due"])
        self.assertEqual(fresh.fire_due(dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=6)), [])
        self.assertEqual(self.log.head(), 2)

    def test_instant_firing_survives_an_interrupted_unlink(self):
        soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
        task = self.objects.create("task", {"title": "t", "due": soon}, "operator")
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=6)
        timer = self.root / "timers" / f"task.{task['id']}.due.json"
        intent = self.root / "timers" / ".firing.json"
        self.assertFalse((self.root / "wake" / "delta").exists())
        real_unlink = os.unlink
        failures = []

        def interrupted_unlink(path):
            if Path(path) == timer and not failures:
                failures.append(path)
                raise OSError("interrupted unlink")
            return real_unlink(path)

        with patch("mizu.kernel.objects.os.unlink", side_effect=interrupted_unlink):
            with self.assertRaisesRegex(OSError, "interrupted unlink"):
                self.objects.fire_due(later)
        self.assertEqual(len(failures), 1)
        self.assertTrue(timer.exists())
        reserved = json.loads(intent.read_text())["event"]

        fresh = Objects(self.root, self.kinds, self.log)
        self.assertEqual(fresh.fire_due(later), [])  # recovery emits before normal processing
        self.assertEqual([self.log.read(seq)["name"] for seq in range(1, self.log.head() + 1)],
                         ["task.created", "task.due"])
        self.assertEqual(self.log.head(), 2)
        self.assertEqual(self.log.read(reserved["seq"]), reserved)
        self.assertEqual(fresh.fire_due(later), [])
        self.assertFalse(timer.exists())
        self.assertFalse(intent.exists())
        self.assertTrue((self.root / "wake" / "delta").exists())

    def test_recovery_completes_an_interrupted_write(self):
        task = self.objects.create("task", {"title": "t"}, "operator")
        # Simulate a crash after the revision landed but before its event was appended.
        record = {**self.objects.read("task", task["id"]), "rev": 2, "transition": "claim",
                  "fields": {"title": "t", "holder": "role:beta"}}
        (self.root / "objects" / "task" / task["id"] / "00000002.json").write_text(json.dumps(record))
        (self.root / "objects" / "intent.json").write_text(json.dumps({
            "kind": "task", "id": task["id"], "rev": 2, "event": "task.claim",
            "from": "open", "to": "open", "actor": "role:beta"}))
        self.objects.create("task", {"title": "u"}, "operator")  # next locked write recovers first
        names = [self.log.read(seq)["name"] for seq in range(1, self.log.head() + 1)]
        self.assertEqual(names, ["task.created", "task.claim", "task.created"])
        self.assertFalse((self.root / "objects" / "intent.json").exists())

    def test_listing_is_filtered_and_paged(self):
        made = [self.objects.create("task", {"title": str(i), "tags": ["a"]}, "operator") for i in range(3)]
        page = self.objects.list("task", state="open", limit=2)
        self.assertEqual(len(page["objects"]), 2)
        rest = self.objects.list("task", state="open", limit=2, after=page["next"])
        self.assertEqual(len(page["objects"]) + len(rest["objects"]), len(made))
        self.assertIsNone(rest["next"])

    def test_declarations_fail_closed(self):
        bad = [
            {"x": {"states": ["a"], "initial": "b", "create": ["host"]}},
            {"x": {"states": ["a"], "initial": "a", "create": ["nobody"]}},
            {"x": {"states": ["a"], "initial": "a", "create": ["host"], "fields": {"f": {"type": "blob"}}}},
            {"x": {"states": ["a"], "initial": "a", "create": ["host"],
                   "transitions": [{"name": "t", "from": ["z"], "to": "a", "by": ["host"]}]}},
            {"x": {"states": ["a"], "initial": "a", "create": ["host"],
                   "transitions": [{"name": "t", "from": ["a"], "to": "a", "by": ["holder"]}]}},
            {"x": {"states": ["a"], "initial": "a", "create": ["host"], "extra": 1}},
            {"x": {"states": ["a"], "initial": "a", "create": ["host"],
                   "fields": {"r": {"type": "ref", "kinds": ["missing"]}}}},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                parse_kinds(raw)
        with self.assertRaises(ConfigError):
            parse_subscriptions(["Bad.Name"], "roles.x.subscribe")

    def test_patterns_are_terminal_only(self):
        with self.assertRaises(ConfigError):
            parse_subscriptions(["check.**.failed"], "roles.x.subscribe")
        for pattern in ("check.**.failed", "**.**", "a.**.b", "a.b.**.c"):
            with self.subTest(pattern=pattern), self.assertRaises(ConfigError):
                parse_subscriptions([pattern], "roles.x.subscribe")
            for name in ("check.pass", "check.run.failed", "check.pass.deep"):
                self.assertFalse(matches(pattern, name))
        self.assertEqual(parse_subscriptions(["**", "check.**", "check.*.failed"], "roles.x.subscribe"),
                         ("**", "check.**", "check.*.failed"))
        self.assertTrue(matches("check.**", "check.pass"))
        self.assertTrue(matches("check.**", "check.pass.deep"))
        self.assertFalse(matches("check.**", "check"))
        self.assertTrue(matches("check.*.failed", "check.run.failed"))
        self.assertFalse(matches("check.*.failed", "check.run.deep.failed"))
        exact = "a.b.c.d.e.f.g.h"
        self.assertEqual(parse_subscriptions([exact], "roles.x.subscribe"), (exact,))
        self.assertTrue(matches(exact, exact))
        self.assertFalse(matches(exact, "a.b.c.d.e.f.g"))
        for pattern in ("a.b.c.d.e.f.g.h.i", "a.b.c.d.e.f.g.h.**"):
            with self.subTest(pattern=pattern), self.assertRaises(ConfigError):
                parse_subscriptions([pattern], "roles.x.subscribe")

    def test_pattern_matching(self):
        self.assertTrue(matches("task.*", "task.created"))
        self.assertFalse(matches("task.*", "task.created.again"))
        self.assertTrue(matches("task.**", "task.created.again"))
        self.assertFalse(matches("task.**", "task"))
        self.assertTrue(matches("**", "anything.at.all"))


if __name__ == "__main__":
    unittest.main()


from support import Fixture  # noqa: E402
from mizu.config import load  # noqa: E402


class KernelConfiguration(Fixture):
    def test_kinds_and_subscriptions_load_from_configuration(self):
        with self.file.open('a', encoding='utf-8') as stream:
            stream.write('\n[kinds.note]\nstates = ["open"]\ninitial = "open"\ncreate = ["operator"]\n'
                         'fields.title = {type = "string", max = 10}\n')
        role = next(iter(self.config.roles))
        text = self.file.read_text()
        self.file.write_text(text.replace(f'[roles.{role}]\n', f'[roles.{role}]\nsubscribe = ["note.*"]\n', 1))
        config = load(self.file)
        self.assertEqual(config.kinds["note"].states, ("open",))
        self.assertEqual(config.roles[role].subscribe, ("note.*",))
        self.assertEqual(self.config.kinds, {})


class ExampleUserland(unittest.TestCase):
    def test_example_kinds_parse(self):
        root = Path(__file__).resolve().parents[1] / "examples/development/kinds.toml"
        kinds = parse_kinds(tomllib.loads(root.read_text())["kinds"])
        self.assertIn("proposal", kinds)
