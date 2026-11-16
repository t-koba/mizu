"""Optional adapter-wide change pages; fake adapters only, offline."""
import sys
import unittest
from unittest.mock import patch

from support import Fixture
from mizu import vcs
from mizu.config import load
from mizu.errors import ConfigError, Denied


ENABLED = {"change_stream_enabled": True}


def page(cursor=None, subjects=None, next_cursor="next"):
    return {"op": "changes", "cursor": cursor,
            "subjects": [] if subjects is None else subjects, "next_cursor": next_cursor}


class ChangesTests(unittest.TestCase):
    def test_stateless_forwarding_and_opaque_results(self):
        cursor = "opaque / é :?= " + "x" * 200
        subjects = ["namespace:any / é", "namespace:any / é", "", "x" * 1024]
        with patch.object(vcs, "invoke", side_effect=[page(subjects=subjects, next_cursor=cursor),
                                                    page(cursor, next_cursor=cursor)]) as invoke:
            cancel = lambda: False
            result = vcs.read_changes_or_none(ENABLED, {}, cancel=cancel)
            self.assertEqual(result, {"op": "changes", "subjects": subjects,
                                      "next_cursor": cursor, "trust": "external-untrusted"})
            invoke.assert_called_once_with(ENABLED, {"op": "changes", "limit": 256}, cancel=cancel)
            result = vcs.read_via(ENABLED, "changes", {"cursor": cursor, "limit": 1}, cancel=cancel)
            self.assertEqual(result["subjects"], [])
            self.assertEqual(result["next_cursor"], cursor)
            invoke.assert_called_with(ENABLED, {"op": "changes", "cursor": cursor, "limit": 1},
                                      cancel=cancel)
        subjects = ["opaque"] * vcs.MAX_CHANGED_SUBJECTS
        self.assertEqual(vcs.parse_changes(page(subjects=subjects))["subjects"], subjects)

    def test_disabled_never_invokes_and_supported_is_opt_in(self):
        self.assertIn("changes", vcs.READ_OPS)
        with patch.object(vcs, "invoke") as invoke:
            for settings in ({}, {"change_stream_enabled": False}, {"change_stream_enabled": 1}):
                self.assertIsNone(vcs.read_changes_or_none(settings, {}))
                with self.assertRaisesRegex(Denied, "disabled"):
                    vcs.read_via(settings, "changes", {})
            invoke.assert_not_called()

    def test_only_explicit_scoped_unsupported_response_falls_back(self):
        for cursor, params in ((None, {}), ("opaque", {"cursor": "opaque"})):
            with self.subTest(cursor=cursor), patch.object(vcs, "invoke", return_value={
                    "op": "changes", "cursor": cursor, "supported": False}):
                self.assertIsNone(vcs.read_changes_or_none(ENABLED, params))
                with self.assertRaisesRegex(Denied, "unsupported"):
                    vcs.read_via(ENABLED, "changes", params)
        for response in ({"op": "changes", "supported": False},
                         {"op": "other", "cursor": None, "supported": False},
                         {"op": "changes", "cursor": "wrong", "supported": False},
                         {"op": "changes", "cursor": None, "supported": 0},
                         {"op": "changes", "cursor": None, "supported": False, "subjects": []}):
            with self.subTest(response=response), patch.object(vcs, "invoke", return_value=response):
                with self.assertRaises(Denied):
                    vcs.read_changes_or_none(ENABLED, {})

    def test_bad_requests_are_denied_before_invocation(self):
        cases = [None, {"branch": "main"}, {"sha": "a" * 40}, {"offset": 0}, {"check": "test"}]
        cases += [{"cursor": value} for value in (None, 1, "", "x" * 257, "bad\n", "bad\x1b")]
        cases += [{"limit": value} for value in (None, True, 0, -1, 257, 1.5, "1")]
        with patch.object(vcs, "invoke") as invoke:
            for params in cases:
                with self.subTest(params=params), self.assertRaises(Denied):
                    vcs.read_via(ENABLED, "changes", params)
            invoke.assert_not_called()

    def test_malformed_pages_fail_closed_without_fallback(self):
        cases = [None, [], {}, {**page(), "op": "status"}, {**page(), "cursor": "wrong"}]
        cases += [{key: value for key, value in page().items() if key != missing}
                  for missing in page()]
        cases += [{**page(), "next_cursor": value} for value in (None, 1, "", "x" * 257)]
        cases += [{**page(), "subjects": value}
                  for value in (None, {}, "subject", [1], [None], [True], ["x"] * 257)]
        cases += [{**page(), "unexpected": "value"}]
        for control in ("\x00", "\n", "\x1b", "\x7f", "\x85", "\u202e"):
            cases += [page(next_cursor="bad" + control), page(subjects=["bad" + control])]
        for response in cases:
            with self.subTest(response=response), patch.object(vcs, "invoke", return_value=response):
                with self.assertRaises(Denied):
                    vcs.read_changes_or_none(ENABLED, {})
        with patch.object(vcs, "invoke", return_value=page(subjects=["a", "b"])):
            with self.assertRaises(Denied):
                vcs.read_changes_or_none(ENABLED, {"limit": 1})

    def test_real_invocation_uses_existing_transport_bounds(self):
        code = ("import sys,json; req=json.load(sys.stdin); "
                "assert req == {'op':'changes','limit':256}; "
                "print(json.dumps(%r))" % page(subjects=["opaque"]))
        settings = {**ENABLED, "command": [sys.executable, "-c", code],
                    "timeout_seconds": 1, "max_bytes": 1024}
        self.assertEqual(vcs.read_changes_or_none(settings, {})["subjects"], ["opaque"])
        for code, options in (("print('x' * 2048)", {}),
                              ("print('not-json')", {}),
                              ("import sys; sys.exit(2)", {}),
                              ("import time; time.sleep(5)", {}),
                              ("pass", {"cancel": lambda: True})):
            with self.subTest(code=code), self.assertRaises(Denied):
                vcs.read_changes_or_none({**settings, "command": [sys.executable, "-c", code]},
                                         {}, **options)


class ChangesConfigTests(Fixture):
    def test_gate_defaults_off_and_requires_a_boolean(self):
        original = "\n".join(line for line in self.file.read_text().splitlines()
                             if not line.startswith("change_stream_enabled ="))
        self.file.write_text(original)
        self.assertIs(load(self.file).vcs["change_stream_enabled"], False)
        for value in ("true", "false", "1", '"true"', "[]"):
            self.file.write_text(original.replace("\n[vcs]\n", "\n[vcs]\nchange_stream_enabled = " + value + "\n"))
            with self.subTest(value=value):
                if value in ("true", "false"):
                    self.assertIs(load(self.file).vcs["change_stream_enabled"], value == "true")
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)


if __name__ == "__main__":
    unittest.main()
