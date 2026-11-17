"""Both Pi bridges must accept the current Python-owned tool schemas offline."""
import json
import subprocess
import tempfile
import unittest

from support import ROOT
from mizu.protocol import DEFINITIONS


class ToolSchemaConformanceTests(unittest.TestCase):
    def test_catalog_and_nested_diagnostic(self):
        manifest = {"mizu_" + name: schema for name, (_, schema) in DEFINITIONS.items()}
        manifest["object_openness_fixture"] = {
            "type": "object", "properties": {
                "default_closed": {"type": "object", "properties": {}},
                "explicit_closed": {"type": "object", "properties": {}, "additionalProperties": False},
                "open_items": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            },
        }
        fixture = "unsupported_nested_fixture"
        manifest[fixture] = {
            "type": "object", "properties": {
                "payload": {"type": "array", "items": {
                    "type": "object", "properties": {"bad_field": {"type": "unsupported"}},
                }},
            },
        }
        # One process for the whole catalog and diagnostic fixture. File-backed
        # capture bounds memory even on failure; the helper also caps its output.
        maximum = 2 * 1024 * 1024
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            result = subprocess.run(
                ["node", str(ROOT / "tests/tool-schema-conformance.mjs")],
                input=json.dumps(manifest).encode(), stdout=stdout, stderr=stderr,
                cwd=ROOT, timeout=30, check=False,
            )  # Missing Node is an error, never a skip.
            stdout.seek(0)
            stderr.seek(0)
            output, errors = stdout.read(maximum + 1), stderr.read(maximum + 1)
        self.assertLessEqual(len(output), maximum)
        self.assertLessEqual(len(errors), maximum)
        self.assertEqual(result.returncode, 0, errors.decode(errors="replace"))
        rows = json.loads(output)
        expected = {(adapter, tool) for adapter in ("pi", "pi-durable") for tool in manifest}
        self.assertEqual(len(rows), len(expected))
        self.assertEqual({(row["adapter"], row["tool"]) for row in rows}, expected)
        for row in rows:
            with self.subTest(adapter=row["adapter"], tool=row["tool"]):
                if row["tool"] == fixture:
                    self.assertEqual(row["error"]["path"],
                                     "#/properties/payload/items/properties/bad_field")
                    self.assertIn("unsupported", row["error"]["message"])
                else:
                    self.assertNotIn("error", row, row)
                    self.assert_object_openness(manifest[row["tool"]], row["schema"])

    def assert_object_openness(self, source, converted, path="#"):
        if source.get("type") == "object" or "properties" in source:
            self.assertIs(converted["additionalProperties"],
                          source.get("additionalProperties") is True, path)
            for name, child in source.get("properties", {}).items():
                self.assert_object_openness(child, converted["properties"][name],
                                            f"{path}/properties/{name}")
        if source.get("type") == "array":
            self.assert_object_openness(source["items"], converted["items"], f"{path}/items")
