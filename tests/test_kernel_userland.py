"""Stage-2 example userland loaded through config include and kernel tools."""
import dataclasses
import datetime as dt
import tempfile
import tomllib
import unittest
import uuid
from pathlib import Path

import support  # noqa: F401  (puts src on the path)
from mizu.config import load
from mizu.errors import Denied
from mizu.fs import mkdir
from mizu.kernel import parse_kinds
from mizu.project import initialize
from mizu.protocol import tool_definitions
from mizu.runtime import Context

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tests" / "kernel_userland.toml"
EXAMPLE = ROOT / "examples" / "development" / "kinds.toml"
OPS = ("objects_create", "objects_transition", "objects_read", "objects_list",
       "events_read", "events_ack")
EVENT_NAMES = ("insight.created", "insight.defer", "insight.revise",
               "insight.accept", "decision.recorded")


class KernelUserland(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mizu-kernel-userland-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Only the state location is test-owned; kinds, roles and grants all
        # come from the checked-in deployment config and its include.
        self.config = dataclasses.replace(load(CONFIG), data=self.root / "data")
        source = self.root / "source"
        source.mkdir()
        (source / "app.py").write_bytes(b"VALUE = 2\n")
        goal = self.root / "GOAL.md"
        goal.write_text("Preserve correctness.\n")
        self.project = initialize(self.config, "userland", source, goal,
                                  ["writer", "reviewer", "observer", "idle"],
                                  ["python3 -m unittest"])
        self.project.set_control(armed=True, paused=False, wake_generation="test-wake")
        self._contexts = {}
        self._lifecycle = None

    def context(self, name):
        if name not in self._contexts:
            run = self.project.root / "runs" / uuid.uuid4().hex
            mkdir(run)
            snapshot = self.project.snapshots.get()
            workspace = run / "input"
            self.project.snapshots.materialize(snapshot, workspace)
            self._contexts[name] = Context(self.config, self.project,
                                           self.config.roles[name], run, snapshot, workspace)
        return self._contexts[name]

    def insight_fields(self, **extra):
        return {"title": "Kernel migration", "body": "Include the example userland",
                "holder": "role:reviewer", "urgency": "high", **extra}

    def lifecycle(self):
        """Create, defer, revise and accept an insight; host records its decision."""
        if self._lifecycle is not None:
            return self._lifecycle
        author, holder = self.context("writer"), self.context("reviewer")
        insight = author.handle("objects_create", {"kind": "insight",
                                                   "fields": self.insight_fields()})
        revisit = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
        deferred = holder.handle("objects_transition", {
            "kind": "insight", "id": insight["id"], "transition": "defer",
            "expected_rev": 1, "fields": {"revisit_at": revisit}})
        revised = author.handle("objects_transition", {
            "kind": "insight", "id": insight["id"], "transition": "revise",
            "expected_rev": 2,
            "fields": {"title": "Revised", "body": "With the addressed holder",
                       "urgency": "low"}})
        accepted = holder.handle("objects_transition", {
            "kind": "insight", "id": insight["id"], "transition": "accept",
            "expected_rev": 3, "fields": {}})
        decision_fields = {"insight": insight["id"], "revision": 4,
                           "action": "accept", "reason": "Addressed holder accepted"}
        with self.assertRaisesRegex(Denied, "may not create decision"):
            author.handle("objects_create", {"kind": "decision", "fields": decision_fields})
        # The example declares decisions host/operator-only: a model role cannot
        # forge one, so the host writes it through the same generic kernel store.
        decision = self.project.kernel_objects.create("decision", decision_fields, "host")
        self._lifecycle = (insight, deferred, revised, accepted, decision)
        return self._lifecycle

    def test_example_kinds_load_from_deployment_config_include(self):
        example = parse_kinds(tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["kinds"])
        self.assertEqual(self.config.kinds, example)
        self.assertEqual(self.config.file, CONFIG.resolve())
        self.assertEqual(self.config.kinds["decision"].create, ("host", "operator"))
        self.assertEqual(self.config.kinds["insight"].transitions["revise"].by, ("author",))
        for name in ("writer", "reviewer"):
            self.assertTrue({"objects", "events"} <= set(self.config.roles[name].capabilities))
        self.assertEqual(self.config.roles["observer"].subscribe, ("insight.**", "decision.**"))
        self.assertEqual(self.config.roles["idle"].subscribe, ("insight.never",))
        self.assertEqual(self.project.kernel_events.subscriptions,
                         {name: self.config.roles[name].subscribe for name in self.project.roles})
        tools = {t["operation"] for t in tool_definitions(self.config.roles["writer"].capabilities)}
        self.assertTrue(set(OPS) <= tools)
        self.assertFalse(set(OPS) & {t["operation"] for t in
                                     tool_definitions(self.config.roles["idle"].capabilities)})

    def test_insight_and_decision_lifecycle_through_tools(self):
        author = self.context("writer")
        insight, deferred, revised, accepted, decision = self.lifecycle()
        self.assertEqual((insight["state"], insight["rev"], insight["author"], insight["actor"]),
                         ("pending", 1, "role:writer", "role:writer"))
        self.assertEqual((deferred["state"], deferred["rev"], deferred["actor"]),
                         ("deferred", 2, "role:reviewer"))
        self.assertEqual((revised["state"], revised["rev"], revised["fields"]["title"]),
                         ("pending", 3, "Revised"))
        self.assertEqual((accepted["state"], accepted["rev"]), ("accepted", 4))
        self.assertEqual((decision["state"], decision["actor"], decision["author"]),
                         ("recorded", "host", "host"))
        self.assertEqual((decision["fields"]["insight"], decision["fields"]["revision"],
                          decision["fields"]["action"]), (insight["id"], 4, "accept"))

        self.assertEqual(author.handle("objects_read", {"kind": "insight",
                                                        "id": insight["id"]}), accepted)
        self.assertEqual(author.handle("objects_read", {"kind": "insight",
                                                        "id": insight["id"], "rev": 1}),
                         insight)
        self.assertEqual(author.handle("objects_read", {"kind": "decision",
                                                        "id": decision["id"]}), decision)
        listed = author.handle("objects_list", {"kind": "insight", "state": "accepted",
                                                 "where": {"title": "Revised"}, "limit": 1})
        self.assertEqual([obj["id"] for obj in listed["objects"]], [insight["id"]])
        self.assertIsNone(listed["next"])
        decisions = author.handle("objects_list", {"kind": "decision", "state": "recorded"})
        self.assertEqual([obj["id"] for obj in decisions["objects"]], [decision["id"]])

        # Compare-and-swap and per-kind permissions remain config driven.
        with self.assertRaisesRegex(Denied, "changed"):
            self.context("reviewer").handle("objects_transition", {
                "kind": "insight", "id": insight["id"], "transition": "accept",
                "expected_rev": 3, "fields": {}})
        with self.assertRaisesRegex(Denied, "no transition"):
            author.handle("objects_transition", {"kind": "insight", "id": insight["id"],
                                                 "transition": "unknown", "expected_rev": 4})

    def test_declared_subscribers_pages_and_cursors_through_tools(self):
        self.lifecycle()
        writer, reviewer, observer = (self.context(name)
                                      for name in ("writer", "reviewer", "observer"))
        self.assertEqual([e["name"] for e in observer.handle("events_read", {})["events"]],
                         list(EVENT_NAMES))
        self.assertEqual([e["name"] for e in writer.handle("events_read", {})["events"]],
                         list(EVENT_NAMES))
        self.assertEqual([e["name"] for e in reviewer.handle("events_read", {})["events"]],
                         ["insight.created", "insight.accept"])
        self.assertEqual(self.project.kernel_events.pending("idle")["events"], [])
        self.assertTrue((self.project.root / "wake" / "writer").exists())
        self.assertTrue((self.project.root / "wake" / "observer").exists())
        self.assertFalse((self.project.root / "wake" / "idle").exists())

        head = self.project.kernel_events.head()
        first = observer.handle("events_read", {"limit": 2})
        self.assertEqual([e["name"] for e in first["events"]], list(EVENT_NAMES[:2]))
        self.assertEqual((first["head"], first["more"], first["next"]), (head, True, 2))
        rest = observer.handle("events_read", {"limit": 2, "cursor": first["next"]})
        self.assertEqual([e["name"] for e in rest["events"]], list(EVENT_NAMES[2:4]))
        last = observer.handle("events_read", {"limit": 2, "cursor": rest["next"]})
        self.assertEqual([e["name"] for e in last["events"]], [EVENT_NAMES[4]])
        self.assertFalse(last["more"])

        acked = writer.handle("events_ack", {"seq": 3})
        self.assertEqual(acked["seq"], 3)
        self.assertEqual([e["seq"] for e in writer.handle("events_read", {})["events"]], [4, 5])
        self.assertEqual(self.project.kernel_events.cursor("reviewer"), 0)
        self.assertEqual([e["name"] for e in reviewer.handle("events_read", {})["events"]],
                         ["insight.created", "insight.accept"])
        self.assertEqual(writer.handle("events_ack", {"seq": head})["seq"], head)
        self.assertEqual(writer.handle("events_read", {})["events"], [])
        with self.assertRaises(Denied):
            writer.handle("events_ack", {"seq": head + 1})

    def test_validation_bounds_and_trusted_actor_binding(self):
        author, reviewer = self.context("writer"), self.context("reviewer")
        insight = author.handle("objects_create", {"kind": "insight",
                                                   "fields": self.insight_fields()})
        head = self.project.kernel_events.head()
        for fields, expected in (
            ({"title": "t", "holder": "role:reviewer"}, "Missing required fields"),
            (self.insight_fields(urgency="x" * 17), "field urgency"),
            (self.insight_fields(surprise=1), "Fields not settable here"),
        ):
            with self.subTest(fields=fields), self.assertRaisesRegex(Denied, expected):
                author.handle("objects_create", {"kind": "insight", "fields": fields})
        for args, expected in (
            ({"kind": "insight", "state": "unknown"}, "Undeclared state"),
            ({"kind": "insight", "where": {"surprise": 1}}, "Undeclared fields"),
            ({"kind": "insight", "limit": 513}, "limit"),
        ):
            with self.subTest(args=args), self.assertRaisesRegex(Denied, expected):
                author.handle("objects_list", args)
        with self.assertRaises(Denied):
            author.handle("events_read", {"limit": 0})
        with self.assertRaisesRegex(Denied, "may not make transition"):
            reviewer.handle("objects_transition", {
                "kind": "insight", "id": insight["id"], "transition": "revise",
                "expected_rev": 1, "fields": {"title": "not the author"}})
        for revision, action, target, expected in (
            (1, "bogus", insight["id"], "field action must be one of"),
            (0, "accept", insight["id"], "field revision"),
            (1, "accept", "0" * 32, "must reference an existing object"),
        ):
            with self.subTest(revision=revision, target=target):
                with self.assertRaisesRegex(Denied, expected):
                    self.project.kernel_objects.create("decision", {
                        "insight": target, "revision": revision,
                        "action": action, "reason": "r"}, "host")
        for extra in ({"actor": "operator"}, {"role": "reviewer"}):
            with self.subTest(extra=extra), self.assertRaises(Denied):
                author.handle("objects_create", {"kind": "insight",
                                                 "fields": self.insight_fields(), **extra})
        with self.assertRaises(Denied):
            author.handle("events_read", {"role": "reviewer"})
        with self.assertRaises(Denied):
            author.handle("events_ack", {"role": "reviewer", "seq": 0})
        self.assertEqual(self.project.kernel_events.head(), head)
        self.assertEqual(self.project.kernel_events.cursor("reviewer"), 0)
        self.assertEqual(author.handle("objects_read", {"kind": "insight",
                                                        "id": insight["id"]}), insight)

    def test_missing_kernel_grant_is_denied_before_io(self):
        idle = self.context("idle")
        args = {
            "objects_create": {"kind": "insight", "fields": self.insight_fields()},
            "objects_transition": {"kind": "insight", "id": "0" * 32,
                                   "transition": "accept", "expected_rev": 1},
            "objects_read": {"kind": "insight", "id": "0" * 32},
            "objects_list": {"kind": "insight"},
            "events_read": {},
            "events_ack": {"seq": 0},
        }
        for op in OPS:
            with self.subTest(op=op):
                for call in (idle.handle, idle.dispatch):
                    with self.assertRaisesRegex(Denied, "no capability"):
                        call(op, args[op])
        self.assertFalse((self.project.root / "objects" / "insight").exists())
        self.assertFalse((self.project.root / "events" / "head.json").exists())
        self.assertFalse((self.project.root / "wake" / "idle").exists())
        observer = self.context("observer")
        with self.assertRaisesRegex(Denied, "no capability"):
            observer.handle("objects_create", args["objects_create"])
        self.assertEqual(observer.handle("events_read", {})["events"], [])
        self.assertEqual(observer.handle("events_ack", {"seq": 0})["seq"], 0)


if __name__ == "__main__":
    unittest.main()
