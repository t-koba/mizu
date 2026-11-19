"""Model-facing queue tools: grants, actor binding, validation and dispatch."""
import dataclasses
import unittest

import support  # noqa: F401
from support import Fixture
from mizu.errors import Denied
from mizu.kernel import parse_queues
from mizu.project import Project
from mizu.protocol import DEFINITIONS, operation_allowed, tool_definitions
from mizu.runtime import OBSERVATIONAL_OPS

QUEUES = {
    "notes": {
        "write": ["role:worker", "operator"],
        "read": ["role:worker", "role:searcher"],
        "wake": ["role:worker"],
        "schema": {"title": {"type": "string", "max": 80, "required": True}},
    },
    "sealed": {
        "write": ["operator"],
        "read": ["role:searcher"],
    },
}


class QueueTools(Fixture):
    def setUp(self):
        super().setUp()
        roles = {}
        for name, role in self.config.roles.items():
            capabilities = tuple(role.capabilities)
            if name in ("worker", "searcher"):
                capabilities += ("queues",)
            roles[name] = dataclasses.replace(role, capabilities=capabilities)
        self.config = dataclasses.replace(self.config, roles=roles,
                                          queues=parse_queues(QUEUES))
        self.project = Project(self.config, self.project.name)

    def granted(self, name="worker"):
        return self.context(name)

    def test_end_to_end_actor_binding_and_bounded_pages(self):
        ctx = self.granted()
        first = ctx.handle("queue_write", {"queue": "notes", "body": {"title": "one"}})
        self.assertEqual(first["author"], "role:worker")
        self.assertEqual(first["seq"], 1)
        retry = ctx.handle("queue_write", {"queue": "notes", "body": {"title": "one"},
                                           "id": first["id"]})
        self.assertEqual(retry, first)
        with self.assertRaisesRegex(Denied, "different content"):
            ctx.handle("queue_write", {"queue": "notes", "body": {"title": "two"},
                                       "id": first["id"]})
        second = ctx.handle("queue_write", {"queue": "notes", "body": {"title": "two"}})
        page = ctx.handle("queue_read", {"queue": "notes", "limit": 1})
        self.assertEqual([item["id"] for item in page["messages"]], [first["id"]])
        self.assertTrue(page["more"])
        rest = ctx.handle("queue_read", {"queue": "notes", "cursor": page["next"]})
        self.assertEqual([item["id"] for item in rest["messages"]], [second["id"]])
        self.assertEqual(ctx.handle("queue_ack", {"queue": "notes", "seq": second["seq"]}), {"seq": 2})
        self.assertEqual(ctx.handle("message_read", {"id": first["id"]}), first)
        with self.assertRaises(Denied):
            ctx.handle("message_read", {"id": "0" * 32})

    def test_refs_and_directions(self):
        ctx = self.granted()
        target = ctx.handle("queue_write", {"queue": "notes", "body": {"title": "target"}})
        reply = ctx.handle("queue_write", {"queue": "notes", "body": {"title": "reply"},
                                           "refs": [target["id"]]})
        self.assertEqual([m["id"] for m in ctx.handle(
            "message_refs", {"id": reply["id"], "direction": "from"})["messages"]],
            [target["id"]])
        self.assertEqual([m["id"] for m in ctx.handle(
            "message_refs", {"id": target["id"], "direction": "to"})["messages"]],
            [reply["id"]])

    def test_capability_and_queue_grants(self):
        denied = self.context("consult")
        with self.assertRaisesRegex(Denied, "no capability"):
            denied.handle("queue_write", {"queue": "notes", "body": {"title": "x"}})
        with self.assertRaisesRegex(Denied, "may not write"):
            self.granted("searcher").handle("queue_write",
                                          {"queue": "notes", "body": {"title": "x"}})
        with self.assertRaisesRegex(Denied, "may not read"):
            self.granted().handle("queue_read", {"queue": "sealed"})
        with self.assertRaisesRegex(Denied, "Undeclared queue"):
            self.granted().handle("queue_read", {"queue": "missing"})

    def test_no_actor_argument_and_bounds(self):
        ctx = self.granted()
        with self.assertRaisesRegex(Denied, "Invalid fields"):
            ctx.handle("queue_write", {"queue": "notes", "body": {"title": "x"},
                                       "actor": "role:reader"})
        with self.assertRaisesRegex(Denied, "Invalid fields"):
            ctx.handle("queue_read", {"queue": "notes", "role": "reader"})
        with self.assertRaisesRegex(Denied, "Out-of-range integer"):
            ctx.handle("queue_read", {"queue": "notes", "limit": 0})
        with self.assertRaisesRegex(Denied, "Unsupported value"):
            ctx.handle("message_refs", {"id": "0" * 32, "direction": "sideways"})

    def test_definitions_grants_and_observational_classification(self):
        for name in ("queue_write", "queue_read", "queue_ack", "message_read", "message_refs"):
            self.assertIn(name, DEFINITIONS)
            self.assertTrue(operation_allowed(name, {"queues"}))
            self.assertFalse(operation_allowed(name, set()))
        self.assertTrue({"queue_read", "message_read", "message_refs"} <= OBSERVATIONAL_OPS)
        self.assertFalse({"queue_write", "queue_ack"} & OBSERVATIONAL_OPS)
        definitions = {tool["operation"] for tool in tool_definitions(("queues",))}
        self.assertTrue({"queue_write", "queue_read", "queue_ack", "message_read",
                         "message_refs"} <= definitions)


if __name__ == "__main__":
    unittest.main()
