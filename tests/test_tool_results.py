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
            out.append(tool_results.continuation(ctx, operation, {
                **arguments, "result_cursor": first["result_cursor"],
                "result_offset": out[-1]["next_offset"]}))
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
        args = {"limit": 5}
        first = tool_results.bounded(ctx, "search", args, {"r": "x" * 20000})
        cursor = first["result_cursor"]
        good = {**args, "result_cursor": cursor, "result_offset": first["next_offset"]}
        self.assertEqual(tool_results.continuation(ctx, "search", good)["offset"], first["next_offset"])
        for bad in ({**good, "result_cursor": "0" * 32}, {**good, "result_cursor": "zz"},
                    {**good, "result_offset": 10 ** 7}, {**good, "limit": 6},
                    {"result_offset": 1}):
            with self.assertRaises(Denied):
                tool_results.continuation(ctx, "search", bad)
        with self.assertRaises(Denied):
            tool_results.continuation(ctx, "vcs_read", good)
        # Offset inside a multi-byte character is refused.
        wide = tool_results.bounded(ctx, "search", {}, {"r": "雪" * 5000})
        with self.assertRaises(Denied):
            tool_results.continuation(ctx, "search", {"result_cursor": wide["result_cursor"],
                                                      "result_offset": 7})
        self.assertIsNone(tool_results.continuation(ctx, "search", args))

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
                page = ctx.handle("files", {"result_cursor": first["result_cursor"],
                                            "result_offset": page["next_offset"]})
                text += page["page"]
        finally:
            Context._DISPATCH["files"] = original
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(text), big)

    def test_schema_accepts_the_continuation_everywhere_but_finish(self):
        for name, (_, schema) in DEFINITIONS.items():
            with self.subTest(tool=name):
                has = "result_cursor" in schema["properties"]
                self.assertEqual(has, name != "finish")
        self.assertEqual(self.config.limits.tool_response_bytes, 32768)

    def test_paged_artifact_reads_keep_their_own_bound(self):
        ctx = self.ctx()
        big = {"result": "x" * 20000}
        args = {"op": "artifact", "branch": "b", "artifact": "a", "offset": 0, "limit": 65536}
        self.assertIs(tool_results.bounded(ctx, "vcs_read", args, big), big)
        whole = {k: v for k, v in args.items() if k not in ("offset", "limit")}
        self.assertTrue(tool_results.bounded(ctx, "vcs_read", whole, big)["truncated"])

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
