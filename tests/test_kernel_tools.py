"""Generic kernel tools through grants, transport, dispatch and adapter protocols."""
import dataclasses
import io
import json
import unittest
from unittest.mock import patch

import support  # noqa: F401  (puts src on the path)
from support import Fixture, default_config_tree
from mizu.config import load
from mizu.errors import Denied
from mizu.kernel import parse_kinds
from mizu.fs import read_json
from mizu.mcp_proxy import serve as mcp_serve
from mizu.pi import PiDriver
from mizu.project import Project
from mizu.protocol import DEFINITIONS, tool_definitions, validate
from mizu.runtime import prompt_for

KINDS = {
    "note": {
        "states": ["open", "closed"], "initial": "open",
        "create": ["role:worker", "operator"],
        "fields": {"title": {"type": "string", "max": 80, "required": True},
                   "evidence": {"type": "ref", "kinds": ["check"]}},
        "transitions": [{"name": "close", "from": ["open"], "to": "closed",
                         "by": ["role:worker", "author"], "fields": ["evidence"],
                         "requires": [{"ref": "evidence", "state": ["passed"]}],
                         "emits": "note.closed"}],
    },
    "check": {
        "states": ["new", "passed"], "initial": "new", "create": ["role:worker"],
        "fields": {"snapshot": {"type": "string", "max": 64, "required": True}},
        "transitions": [{"name": "pass", "from": ["new"], "to": "passed",
                         "by": ["role:worker"]}],
    },
}
SUBSCRIBERS = {"worker": ("note.*", "check.*"), "reviewer": ("note.closed",),
               "searcher": ("service.*",)}
KERNEL_OPS = ("objects_create", "objects_transition", "objects_read", "objects_list",
              "events_read", "events_ack")
ARGS = {
    "objects_create": {"kind": "note", "fields": {"title": "t"}},
    "objects_transition": {"kind": "note", "id": "0" * 32, "transition": "close",
                           "expected_rev": 1, "fields": {}},
    "objects_read": {"kind": "note", "id": "0" * 32},
    "objects_list": {"kind": "note"},
    "events_read": {},
    "events_ack": {"seq": 0},
}


class KernelTools(Fixture):
    def setUp(self):
        super().setUp()
        roles = {}
        for name, role in self.config.roles.items():
            caps = tuple(role.capabilities)
            if name == "worker":
                caps += ("objects", "events")
            roles[name] = dataclasses.replace(role, capabilities=caps,
                                              subscribe=SUBSCRIBERS.get(name, ()))
        kinds = parse_kinds(KINDS)
        self.config = dataclasses.replace(self.config, roles=roles, kinds=kinds)
        self.project = Project(self.config, self.project.name)
        self.assertEqual(set(self.project.kernel_events.subscriptions),
                         set(self.project.roles))

    def granted(self, name="worker", *capabilities):
        context = self.context(name)
        context.role = dataclasses.replace(context.role,
                                           capabilities=(*context.role.capabilities, *capabilities))
        return context

    def note(self, context, title="t"):
        return context.handle("objects_create", {"kind": "note", "fields": {"title": title}})

    def evidence(self, context):
        check = context.handle("objects_create",
                               {"kind": "check", "fields": {"snapshot": "s"}})
        context.handle("objects_transition", {"kind": "check", "id": check["id"],
                                              "transition": "pass", "expected_rev": 1})
        return check["id"]

    def close(self, context, note_id, evidence_id, expected_rev=1):
        return context.handle("objects_transition", {
            "kind": "note", "id": note_id, "transition": "close",
            "expected_rev": expected_rev, "fields": {"evidence": evidence_id}})

    def test_all_six_tools_end_to_end(self):
        ctx = self.context("worker")
        note = self.note(ctx)
        self.assertEqual((note["state"], note["rev"], note["author"], note["actor"]),
                         ("open", 1, "role:worker", "role:worker"))
        self.assertEqual(ctx.handle("objects_read", {"kind": "note", "id": note["id"]}), note)
        self.assertEqual(ctx.handle("objects_read", {"kind": "note", "id": note["id"],
                                                     "rev": 1}), note)
        pending = ctx.handle("objects_create",
                             {"kind": "check", "fields": {"snapshot": "s"}})
        with self.assertRaisesRegex(Denied, "Requirement not met"):
            self.close(ctx, note["id"], pending["id"])
        ctx.handle("objects_transition", {"kind": "check", "id": pending["id"],
                                          "transition": "pass", "expected_rev": 1})
        evidence = pending["id"]
        check = ctx.handle("objects_read", {"kind": "check", "id": evidence})
        self.assertEqual(check["state"], "passed")
        closed = self.close(ctx, note["id"], evidence)
        self.assertEqual((closed["state"], closed["rev"]), ("closed", 2))
        with self.assertRaisesRegex(Denied, "changed"):
            self.close(ctx, note["id"], evidence)
        page = ctx.handle("objects_list", {"kind": "note", "state": "closed",
                                           "where": {"title": "t"}, "limit": 1})
        self.assertEqual([obj["id"] for obj in page["objects"]], [note["id"]])
        self.assertIsNone(page["next"])
        second = self.note(ctx, "second")
        first = ctx.handle("objects_list", {"kind": "note", "limit": 1})
        rest = ctx.handle("objects_list", {"kind": "note", "limit": 1,
                                           "cursor": first["next"]})
        self.assertIsNotNone(first["next"])
        self.assertEqual(len(rest["objects"]), 1)
        self.assertNotEqual(first["objects"][0]["id"], rest["objects"][0]["id"])
        self.assertEqual({first["objects"][0]["id"], rest["objects"][0]["id"]},
                         {note["id"], second["id"]})
        self.assertIsNone(rest["next"])

        head = self.project.kernel_events.head()
        first_page = ctx.handle("events_read", {"limit": 2})
        self.assertEqual(len(first_page["events"]), 2)
        self.assertEqual(first_page["head"], head)
        self.assertTrue(first_page["more"])
        second_page = ctx.handle("events_read", {"cursor": first_page["next"], "limit": 2})
        self.assertEqual([e["seq"] for e in second_page["events"]],
                         [e["seq"] for e in self.project.kernel_events.pending(
                             "worker", after=first_page["next"], limit=2)["events"]])
        self.assertTrue(all(e["seq"] > first_page["events"][-1]["seq"]
                            for e in second_page["events"]))
        acked = ctx.handle("events_ack", {"seq": second_page["events"][-1]["seq"]})
        self.assertEqual(acked["seq"], second_page["events"][-1]["seq"])
        after = ctx.handle("events_read", {})
        self.assertTrue(all(e["seq"] > acked["seq"] for e in after["events"]))
        self.assertEqual(self.project.kernel_events.cursor("worker"), acked["seq"])

    def test_ungranted_capability_is_denied_before_io(self):
        reviewer = self.context("reviewer")
        for op, args in ARGS.items():
            with self.subTest(op=op):
                for call in (reviewer.handle, reviewer.dispatch):
                    with self.assertRaisesRegex(Denied, "no capability"):
                        call(op, args)
        self.assertFalse((self.project.root / "objects" / "note").exists())
        self.assertFalse((self.project.root / "events" / "head.json").exists())
        self.assertFalse(any((self.project.root / "wake").glob("*")))
        # No deployment role is implicitly granted either kernel capability.
        shipped = load(default_config_tree() / "config.toml")
        for name, role in {**shipped.roles, **self.config.roles}.items():
            if name != "worker":
                with self.subTest(role=name):
                    self.assertNotIn("objects", role.capabilities)
                    self.assertNotIn("events", role.capabilities)

    def test_unknown_kinds_transitions_and_invalid_arguments(self):
        ctx = self.context("worker")
        for op in ("objects_create", "objects_read", "objects_list"):
            with self.subTest(op=op):
                with self.assertRaisesRegex(Denied, "Undeclared kind"):
                    ctx.handle(op, {"kind": "absent", **{k: v for k, v in ARGS[op].items()
                                                         if k != "kind"}})
        note = self.note(ctx)
        with self.assertRaisesRegex(Denied, "Undeclared kind"):
            ctx.handle("objects_transition", {"kind": "absent", "id": note["id"],
                                              "transition": "close", "expected_rev": 1})
        with self.assertRaisesRegex(Denied, "no transition"):
            ctx.handle("objects_transition", {"kind": "note", "id": note["id"],
                                              "transition": "missing", "expected_rev": 1})
        with self.assertRaisesRegex(Denied, "Requirement not met"):
            self.close(ctx, note["id"], "0" * 32)
        for extra in ({"actor": "operator"}, {"role": "reviewer"}, {"id": "bad"}):
            with self.subTest(extra=extra):
                with self.assertRaises(Denied):
                    ctx.handle("objects_create", {**ARGS["objects_create"], **extra})
        with self.assertRaises(Denied):
            ctx.handle("objects_create", {"kind": "note", "fields": {}})
        with self.assertRaises(Denied):
            ctx.handle("objects_read", {"kind": "note", "id": note["id"], "rev": 0})
        with self.assertRaises(Denied):
            ctx.handle("objects_list", {"kind": "note", "limit": 513})
        for invalid in ({"state": "unknown"}, {"where": {"unknown": "value"}},
                        {"where": {"title": 1}}, {"cursor": "invalid"}):
            with self.subTest(invalid=invalid), self.assertRaises(Denied):
                ctx.handle("objects_list", {"kind": "note", **invalid})
        with self.assertRaises(Denied):
            ctx.handle("events_read", {"limit": 0})
        with self.assertRaises(Denied):
            ctx.handle("events_ack", {"seq": self.project.kernel_events.head() + 1})
        with self.assertRaisesRegex(Denied, "may not create"):
            self.granted("reviewer", "objects").handle(
                "objects_create", {"kind": "note", "fields": {"title": "t"}})
        self.assertEqual(self.project.kernel_events.head(), 1)

    def test_objects_create_accepts_an_unused_caller_supplied_id(self):
        ctx = self.context("worker")
        object_id = "ef" * 16
        note = ctx.handle("objects_create", {"kind": "note", "fields": {"title": "t"},
                                             "id": object_id})
        self.assertEqual(note["id"], object_id)
        self.assertEqual(ctx.handle("objects_read", {"kind": "note", "id": object_id}), note)
        self.assertEqual(self.project.kernel_objects.revisions("note", object_id), [1])
        head = self.project.kernel_events.head()
        for bad in ("bad", "EF" * 16, object_id + "0", None):
            with self.subTest(bad=bad):
                for call in (ctx.handle, ctx.dispatch):
                    with self.assertRaises(Denied):
                        call("objects_create", {"kind": "note", "fields": {"title": "t"}, "id": bad})
        for call in (ctx.handle, ctx.dispatch):
            with self.assertRaisesRegex(Denied, "already exists"):
                call("objects_create", {"kind": "note", "fields": {"title": "other"}, "id": object_id})
        self.assertEqual(self.project.kernel_events.head(), head)
        self.assertEqual(self.project.kernel_objects.revisions("note", object_id), [1])
        self.assertEqual(ctx.handle("objects_read", {"kind": "note", "id": object_id}), note)
        with self.assertRaises(Denied):  # grants and the bound actor are unchanged
            ctx.handle("objects_create", {"kind": "note", "fields": {"title": "t"},
                                          "id": "12" * 16, "actor": "operator"})

    def test_event_append_wakes_every_matching_subscriber(self):
        ctx = self.context("worker")
        wake = self.project.root / "wake"
        self.note(ctx)
        self.assertTrue((wake / "worker").exists())
        self.assertFalse((wake / "reviewer").exists())
        self.assertFalse((wake / "searcher").exists())
        self.close(ctx, self.note(ctx, "other")["id"], self.evidence(ctx))
        self.assertTrue((wake / "reviewer").exists())
        self.assertFalse((wake / "searcher").exists())
        self.assertEqual([e["name"] for e in self.project.kernel_events.pending("searcher")["events"]], [])
        self.assertEqual([e["name"] for e in self.project.kernel_events.pending("reviewer")["events"]],
                         ["note.closed"])

    def test_actor_and_event_cursor_cannot_be_chosen_by_the_model(self):
        ctx = self.context("worker")
        for op, extra in (("objects_create", {"actor": "operator"}),
                          ("objects_transition", {"actor": "role:reviewer"}),
                          ("events_read", {"role": "reviewer"}),
                          ("events_ack", {"role": "reviewer", "seq": 0})):
            with self.subTest(op=op):
                with self.assertRaises(Denied):
                    ctx.handle(op, {**ARGS[op], **extra})
        note = self.note(ctx)
        self.assertEqual((note["author"], note["actor"]), ("role:worker", "role:worker"))
        self.close(ctx, note["id"], self.evidence(ctx))
        ctx.handle("events_ack", {"seq": self.project.kernel_events.head()})
        self.assertEqual(self.project.kernel_events.cursor("reviewer"), 0)
        reviewer = self.granted("reviewer", "events")
        self.assertEqual(reviewer.handle("events_read", {})["events"][0]["name"], "note.closed")
        self.project.kernel_events.ack("reviewer", self.project.kernel_events.head())
        self.assertEqual(ctx.handle("events_read", {})["events"], [])

    def test_protocol_schemas_grants_broker_and_pi(self):
        granted = self.context("worker")
        ungranted = self.context("reviewer")
        self.assertEqual({t["operation"] for t in tool_definitions(("objects", "events"))},
                         set(KERNEL_OPS))
        granted_tools = {t["operation"]: t for t in tool_definitions(granted.role.capabilities)}
        self.assertTrue(set(KERNEL_OPS) <= set(granted_tools))
        self.assertFalse(set(KERNEL_OPS) & {t["operation"] for t in tool_definitions(ungranted.role.capabilities)})
        for op in KERNEL_OPS:
            schema = granted_tools[op]["inputSchema"]
            self.assertEqual(schema, DEFINITIONS[op][1])
            self.assertFalse(schema["additionalProperties"])
            with self.assertRaises(Denied):
                validate({**ARGS[op], "surprise": 1}, schema)
        self.assertTrue(set("mizu_" + op for op in KERNEL_OPS) <=
                        set(json.loads(prompt_for(granted))["unit_tool_surface"]["tools"]))
        self.assertFalse(set("mizu_" + op for op in KERNEL_OPS) &
                         set(json.loads(prompt_for(ungranted))["unit_tool_surface"]["tools"]))

        # The broker advertises the shared schemas and forwards through Context.
        def transact(tools, handle, messages):
            from mizu.bridge import Bridge
            with Bridge(handle, tools, timeout=60) as bridge:
                sink = io.BytesIO()
                source = io.BytesIO(b"".join(messages))
                mcp_serve(input_stream=source, output_stream=sink,
                          bridge=read_json(bridge.config_file))
                return [json.loads(line) for line in sink.getvalue().splitlines() if line]

        call = (b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":'
                b'{"name":"mizu_objects_create","arguments":{"kind":"note",'
                b'"fields":{"title":"broker"}}}}\n')
        init = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        listing = b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        replies = transact(tool_definitions(granted.role.capabilities), granted.handle,
                           (init, listing, call))
        tools = {t["name"]: t for t in replies[1]["result"]["tools"]}
        self.assertEqual(tools["mizu_objects_create"]["inputSchema"],
                         DEFINITIONS["objects_create"][1])
        self.assertEqual(set("mizu_" + op for op in KERNEL_OPS) & set(tools),
                         set("mizu_" + op for op in KERNEL_OPS))
        created = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual((created["author"], created["fields"]["title"]),
                         ("role:worker", "broker"))
        denied = transact(tool_definitions(ungranted.role.capabilities), ungranted.handle,
                          (init, listing, call))
        self.assertFalse(set("mizu_" + op for op in KERNEL_OPS) &
                         {t["name"] for t in denied[1]["result"]["tools"]})
        self.assertTrue(denied[2]["result"]["isError"])
        self.assertIn("Unknown tool", denied[2]["result"]["content"][0]["text"])

        # Pi sends the same grant-filtered definitions to its bridge.
        seen = {}

        class Stop(Exception):
            pass

        def capture(handle, tools, **kwargs):
            seen["tools"] = tools
            raise Stop

        with patch("mizu.pi.Bridge", side_effect=capture):
            with self.assertRaises(Stop):
                PiDriver(self.config).execute(granted, "prompt")
        self.assertEqual({t["operation"] for t in seen["tools"]},
                         {t["operation"] for t in granted_tools.values()})


if __name__ == "__main__":
    unittest.main()
