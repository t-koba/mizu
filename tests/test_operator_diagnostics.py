"""Bounded, model-visible stderr from configured operator adapters. Offline only."""
import json
import sys
import unittest

from support import Fixture
from mizu import materialize, vcs
from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.process import Result, adapter_failure_diagnostic
from mizu.web import Web


def failing_command(stderr, code=3):
    program = ("import sys; sys.stdin.read(); "
               f"sys.stderr.write({stderr!r}); sys.exit({code})")
    return [sys.executable, "-c", program]


def config_setting(text, section, value):
    lines = text.splitlines()
    inside = False
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inside = stripped == f"[{section}]"
        elif inside and stripped.startswith("diagnostic_bytes ="):
            lines[index] = f"diagnostic_bytes = {value}"
            replaced = True
            break
    if not replaced:
        raise AssertionError(f"missing [{section}].diagnostic_bytes")
    return "\n".join(lines) + "\n"


class AdapterDiagnosticConfigTests(Fixture):
    def test_per_adapter_defaults_and_zero_disable(self):
        for section in ("web", "vcs", "materialize"):
            with self.subTest(section=section):
                self.assertEqual(getattr(self.config, section)["diagnostic_bytes"], 2048)
                path = self.root / "config" / f"{section}-diagnostics-off.toml"
                path.write_text(config_setting(self.file.read_text(), section, 0))
                config = load(path)
                self.assertEqual(getattr(config, section)["diagnostic_bytes"], 0)

    def test_omitted_diagnostic_keys_keep_the_documented_default(self):
        lines = [line for line in self.file.read_text().splitlines()
                 if not line.strip().startswith("diagnostic_bytes =")]
        path = self.root / "config" / "diagnostics-omitted.toml"
        path.write_text("\n".join(lines) + "\n")
        config = load(path)
        for section in ("web", "vcs", "materialize"):
            with self.subTest(section=section):
                self.assertEqual(getattr(config, section)["diagnostic_bytes"], 2048)

    def test_each_adapter_rejects_values_outside_hard_bound(self):
        for section in ("web", "vcs", "materialize"):
            for value in (-1, 8193):
                with self.subTest(section=section, value=value):
                    path = self.root / "config" / f"{section}-{value}.toml"
                    path.write_text(config_setting(self.file.read_text(), section, value))
                    with self.assertRaisesRegex(ConfigError, "diagnostic_bytes"):
                        load(path)


class AdapterDiagnosticFormatterTests(unittest.TestCase):
    def test_tail_is_bounded_and_explicitly_untrusted(self):
        result = Result(3, "", "prefixTAIL", "exited", 0.1)
        text = adapter_failure_diagnostic(result, source="test adapter", maximum_bytes=4)
        self.assertIn("external-untrusted", text)
        self.assertIn("tail capped at 4 bytes", text)
        self.assertIn("TAIL", text)
        self.assertNotIn("prefix", text)

    def test_disabled_or_non_exit_failure_has_no_excerpt(self):
        for maximum, result in (
                (0, Result(3, "", "private marker", "exited", 0.1)),
                (2048, Result(0, "", "private marker", "exited", 0.1)),
                (2048, Result(3, "", "private marker", "timeout", 0.1)),
                (2048, Result(3, "", "private marker", "output_limit", 0.1)),
                (2048, Result(3, "", "private marker", "input_error", 0.1))):
            with self.subTest(maximum=maximum, reason=result.reason, exit_code=result.exit_code):
                self.assertEqual(adapter_failure_diagnostic(
                    result, source="test adapter", maximum_bytes=maximum), "")


class AdapterFailureDiagnosticTests(Fixture):
    def make_web(self, **settings):
        cache = self.root / "web-cache"
        receipts = self.root / "receipts"
        cache.mkdir(exist_ok=True)
        receipts.mkdir(exist_ok=True)
        base = {**self.config.web, "timeout_seconds": 5, "max_bytes": 65536,
                "diagnostic_bytes": 2048, "hosts": [], "feeds": [],
                "search_command": [], "destination_command": [], **settings}
        return Web(base, cache, receipts, "searcher")

    def test_materialize_failure_surfaces_only_configured_stderr_tail(self):
        settings = {"command": failing_command("prefixTAIL"), "timeout_seconds": 5,
                    "max_bytes": 65536, "diagnostic_bytes": 4}
        with self.assertRaises(Denied) as caught:
            materialize.invoke(settings, "fixture request")
        message = str(caught.exception)
        self.assertIn("external-untrusted", message)
        self.assertIn("TAIL", message)
        self.assertNotIn("prefix", message)

    def test_zero_suppresses_materialize_stderr(self):
        marker = "adapter_fixture_private_marker"
        settings = {"command": failing_command(marker), "timeout_seconds": 5,
                    "max_bytes": 65536, "diagnostic_bytes": 0}
        with self.assertRaises(Denied) as caught:
            materialize.invoke(settings, "fixture request")
        self.assertNotIn(marker, str(caught.exception))

    def test_vcs_failure_surfaces_labelled_stderr(self):
        marker = "vcs_fixture_diagnostic"
        settings = {"command": failing_command(marker), "timeout_seconds": 5,
                    "max_bytes": 65536, "diagnostic_bytes": 2048}
        with self.assertRaises(Denied) as caught:
            vcs.invoke(settings, {"op": "fetch"})
        self.assertIn("external-untrusted", str(caught.exception))
        self.assertIn(marker, str(caught.exception))

    def test_web_search_failure_surfaces_labelled_stderr(self):
        marker = "search_fixture_diagnostic"
        web = self.make_web(search_command=failing_command(marker))
        with self.assertRaises(Denied) as caught:
            web.search("fixture")
        self.assertIn("external-untrusted", str(caught.exception))
        self.assertIn(marker, str(caught.exception))

    def test_web_destination_failure_still_denies_and_surfaces_stderr(self):
        marker = "destination_fixture_diagnostic"
        web = self.make_web(destination_command=failing_command(marker))
        with self.assertRaises(Denied) as caught:
            web.check_url("https://example.com/", "fetch")
        self.assertIn("external-untrusted", str(caught.exception))
        self.assertIn(marker, str(caught.exception))

    def test_valid_destination_denial_does_not_surface_stderr(self):
        marker = "not-a-failure-diagnostic"
        program = ("import sys,json; sys.stdin.read(); "
                   f"sys.stderr.write({marker!r}); "
                   "print(json.dumps({'allow': False}))")
        web = self.make_web(destination_command=[sys.executable, "-c", program])
        with self.assertRaises(Denied) as caught:
            web.check_url("https://example.com/", "fetch")
        self.assertIn("Destination decider refused", str(caught.exception))
        self.assertNotIn(marker, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
