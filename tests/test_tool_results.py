"""Oversized tool results page at the tool with a machine-usable continuation."""
import dataclasses
import json

from support import Fixture
from mizu import tool_results
from mizu.errors import Denied
from mizu.protocol import DEFINITIONS, tool_definitions
from mizu.fs import canonical


class ToolResultTests(Fixture):
    def ctx(self, bound=4096):
        ctx = self.context("worker")
        ctx.config = dataclasses.replace(ctx.config, limits=dataclasses.replace(
            ctx.config.limits, tool_response_bytes=bound))
        return ctx

    def pages(self, ctx, operation, arguments, result):
        first = tool_results.bounded(ctx, operation, arguments, result)
        out = [first]
        while out[-1]["next_offset"] is not None:
            out.append(tool_results.read(ctx, {"cursor": first["cursor"], "offset": out[-1]["next_offset"]}))
        return out

    def test_small_results_are_unchanged(self):
        ctx = self.ctx()
        result = {"items": ["x"]}
        self.assertIs(tool_results.bounded(ctx, "files", {}, result), result)

    def test_pages_concatenate_and_every_response_honours_the_bound(self):
        ctx = self.ctx(4096)
        result = {"rows": [{"id": i, "t": "é\"\\\n雪" * 9, "c": "\x01"} for i in range(700)]}
        pages = self.pages(ctx, "work_items_read", {"limit": 5}, result)
        self.assertGreater(len(pages), 3)
        for page in pages:
            self.assertLessEqual(len(canonical(page)), 4096)
            self.assertEqual(page["total_bytes"], len(canonical(result)))
        joined = "".join(p["page"] for p in pages)
        self.assertEqual(joined.encode(), canonical(result))
        self.assertEqual(json.loads(joined), result)
        self.assertTrue(pages[0]["truncated"] and not pages[-1]["truncated"])
        self.assertIsNone(pages[-1]["next_offset"])
        self.assertEqual([p["offset"] for p in pages[1:]], [p["next_offset"] for p in pages[:-1]])

    def test_invalid_continuations_are_refused(self):
        ctx = self.ctx()
        first = tool_results.bounded(ctx, "search", {}, {"r": "x" * 20000})
        cursor = first["cursor"]
        self.assertEqual(first["continue"], {"tool": "mizu_result_read", "cursor": cursor,
                                             "offset": first["next_offset"]})
        good = {"cursor": cursor, "offset": first["next_offset"]}
        self.assertEqual(tool_results.read(ctx, good)["offset"], first["next_offset"])
        for bad in ({**good, "cursor": "0" * 32}, {**good, "cursor": "zz"}, {**good, "offset": 10 ** 7}):
            with self.assertRaises(Denied):
                tool_results.read(ctx, bad)
        wide = tool_results.bounded(ctx, "search", {}, {"r": "雪" * 5000})
        with self.assertRaises(Denied):
            tool_results.read(ctx, {"cursor": wide["cursor"], "offset": 7})
        # A cursor is valid only for the role that received it, and only in its run.
        other = self.context("reviewer")
        with self.assertRaises(Denied):
            tool_results.read(other, good)
        ctx.role = dataclasses.replace(ctx.role, name="reviewer")
        with self.assertRaises(Denied):
            tool_results.read(ctx, good)

    def test_handle_pages_through_the_broker_without_repeating_the_tool(self):
        ctx = self.ctx(4096)
        calls = []
        big = {"files": [f"path/{i:05d}.txt" for i in range(2000)]}

        def op(self_, args):
            calls.append(args)
            return big
        from mizu.runtime import Context
        original = Context._DISPATCH["files"]
        Context._DISPATCH["files"] = op
        try:
            ctx.handle("_hello", {})
            first = ctx.handle("files", {})
            self.assertTrue(first["truncated"])
            text, page = first["page"], first
            while page["next_offset"] is not None:
                self.assertEqual(page["continue"]["tool"], "mizu_result_read")
                page = ctx.handle("result_read", {"cursor": page["continue"]["cursor"],
                                                  "offset": page["continue"]["offset"]})
                self.assertLessEqual(len(canonical(page)), 4096)
                text += page["page"]
        finally:
            Context._DISPATCH["files"] = original
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(text), big)

    def test_only_the_one_continuation_tool_knows_cursors_and_it_is_tiny(self):
        for name, (_, schema) in DEFINITIONS.items():
            with self.subTest(tool=name):
                self.assertNotIn("result_cursor", json.dumps(schema))
                self.assertNotIn("result_offset", json.dumps(schema))
        size = len(json.dumps(tool_definitions(("result_read",))[0]))
        self.assertLess(size, 450)
        for role in self.config.roles.values():
            self.assertIn("result_read", role.capabilities)
        self.assertEqual(self.config.roles["worker"].capabilities.count("finish"), 1)

    def test_paged_artifact_reads_keep_their_own_bound(self):
        ctx = self.ctx()
        big = {"result": "x" * 20000}
        args = {"op": "artifact", "branch": "b", "artifact": "a", "offset": 0, "limit": 65536}
        self.assertIs(tool_results.bounded(ctx, "vcs_read", args, big), big)
        whole = {k: v for k, v in args.items() if k not in ("offset", "limit")}
        self.assertTrue(tool_results.bounded(ctx, "vcs_read", whole, big)["truncated"])
        self.assertIs(tool_results.bounded(ctx, "result_read", {}, big), big)

    def test_limit_is_configuration_validated_at_load(self):
        from mizu.config import load
        from mizu.errors import ConfigError
        path = self.root / "config" / "limits.toml"
        text = self.file.read_text()
        self.assertIn("tool_response_bytes = 32768", text)
        for value, ok in ((4096, True), (4095, False), (1048577, False)):
            path.write_text(text.replace("tool_response_bytes = 32768", f"tool_response_bytes = {value}", 1))
            if ok:
                self.assertEqual(load(path).limits.tool_response_bytes, value)
            else:
                with self.assertRaises(ConfigError):
                    load(path)
