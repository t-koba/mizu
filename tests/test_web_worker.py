"""Offline worker IPC regressions: real child/process, cache-seeded fetches.

The separate fetch-media test mocks only HTTPS transport, not worker IPC.
"""
import dataclasses
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from support import Fixture
from mizu.errors import Denied
from mizu.config import MAX_INCLUDE_BYTES, load
from mizu.fs import PREVIEW_BYTES, canonical, digest, now, read_json, write_json
from mizu.process import environment
from mizu.protocol import DEFINITIONS
from mizu.web import (FETCH_CACHE_SCHEMA_VERSION, PROBE_RESPONSE_HEADERS_MAX,
                      PROBE_RESPONSE_NAME_MAX, PROBE_RESPONSE_VALUE_MAX, RESPONSE_MEDIA_MAX, Web)
from mizu.web_worker import MAX_WEB_WORKER_INPUT_BYTES, _output_limit, bounded


class WebWorkerTests(Fixture):
    def fetch_config(self, max_bytes=None):
        settings = {**self.config.web, "hosts": ["example.org"]}
        if max_bytes is not None:
            settings["max_bytes"] = max_bytes
        return dataclasses.replace(self.config, web=settings)

    def seed_cache(self, config, text, **extra):
        url = "https://example.org/receipt"
        raw = text.encode("utf-8")
        receipt = {"id": digest((url + "\n" + digest(raw) + "\n" + str(time.time_ns())).encode()),
                   "url": url, "final_url": url,
                   "retrieved_at": now(), "retrieved_epoch": time.time(),
                   "sha256": digest(raw), "content_type": "text/plain", "text": text,
                   "trust": "external-untrusted",
                   "fetch_policy_digest": digest(canonical({
                       "hosts": sorted(config.web["hosts"]),
                       "intranet": bool(config.web["intranet"]),
                       "max_bytes": config.web["max_bytes"],
                       "receipt_schema": FETCH_CACHE_SCHEMA_VERSION})), **extra}
        write_json(config.data / "web-cache" / f"{digest(url.encode())}.json", receipt)
        return receipt

    def assert_fetch_round_trip(self, character, max_bytes=None):
        config = self.fetch_config(max_bytes)
        text = character * config.web["max_bytes"]
        self.assertEqual(len(text.encode("utf-8")), config.web["max_bytes"])
        receipt = self.seed_cache(config, text)
        run_dir = self.root / "run"
        # No parent mocks: bounded starts mizu.web_worker through process.run.
        result = bounded(config, run_dir, "fetch", receipt["url"], lambda: False)
        self.assertEqual(result, {**receipt, "id": result["receipt_id"],
                                  "receipt_id": result["receipt_id"], "cached": True, "raw_available": False})
        web = Web(config.web, config.data / "web-cache", run_dir / "sources")
        self.assertEqual(web.replay(result["receipt_id"], "fetch"), result)

    def test_default_plain_fetch_round_trips(self):
        self.assertEqual(self.config.web["max_bytes"], 524288)
        self.assert_fetch_round_trip("a")

    def test_default_escaped_fetch_round_trips(self):
        self.assertEqual(self.config.web["max_bytes"], 524288)
        self.assert_fetch_round_trip("\x01")

    def test_configured_escaped_fetch_round_trips(self):
        self.assert_fetch_round_trip("\x01", 1024 * 1024)

    def test_probe_escape_heavy_body_reaches_host_policy_in_real_worker(self):
        config = self.fetch_config()
        config = dataclasses.replace(config, web={**config.web,
            "probe_hosts": ["allowed.example"], "probe_methods": ["POST"]})
        for body, exceeds_preview in (("a" * 32768, False), ("\x01" * 32768, True)):
            value = {"url": "https://denied.example/path", "method": "POST",
                     "headers": [], "body": body}
            frame = canonical({"settings": config.web, "cache": str(config.data / "web-cache"),
                               "receipts": str(self.root / "run" / "sources"), "operation": "probe",
                               "value": value, "role": ""})
            self.assertEqual(len(frame) > PREVIEW_BYTES, exceeds_preview)
            with self.subTest(escaped=exceeds_preview):
                with self.assertRaisesRegex(Denied, "Host is not allowlisted"):
                    bounded(config, self.root / "run", "probe", value, lambda: False)

    def test_large_valid_toml_settings_and_escaped_probe_reach_real_worker(self):
        text = self.file.read_text()
        command_line = next(line for line in text.splitlines()
                            if line.lstrip().startswith("search_command ="))
        tab_args = (",\n").join("'" * 3 + "\t" * 108000 + "'" * 3 for _ in range(9))
        large_command = "search_command = [\n" + tab_args + "\n]"
        text = text.replace(command_line, large_command)
        self.file.write_text(text)
        self.assertLessEqual(self.file.stat().st_size, MAX_INCLUDE_BYTES)
        config = load(self.file)
        config = dataclasses.replace(config, web={**config.web,
            "probe_hosts": ["allowed.example"], "probe_methods": ["POST"]})
        value = {"url": "https://denied.example/path", "method": "POST",
                 "headers": [], "body": "\x01" * 32768}
        frame = canonical({"settings": config.web, "cache": str(config.data / "web-cache"),
                           "receipts": str(self.root / "run" / "sources"), "operation": "probe",
                           "value": value, "role": ""})
        self.assertGreater(len(frame), 2 * MAX_INCLUDE_BYTES)
        self.assertLessEqual(len(frame), MAX_WEB_WORKER_INPUT_BYTES)
        with self.assertRaisesRegex(Denied, "Host is not allowlisted"):
            bounded(config, self.root / "run", "probe", value, lambda: False)

    def test_parent_rejects_oversized_worker_settings_before_spawn(self):
        config = self.fetch_config()
        config = dataclasses.replace(config, web={**config.web,
            "search_command": ["x" * MAX_WEB_WORKER_INPUT_BYTES]})
        with patch("mizu.web_worker.run") as spawn:
            with self.assertRaisesRegex(Denied, "Web worker request exceeds input byte limit"):
                bounded(config, self.root / "run", "search", {"query": "x"}, lambda: False)
        spawn.assert_not_called()

    def test_poisoned_cache_exceeding_ipc_limit_is_denied(self):
        config = self.fetch_config()
        maximum = _output_limit("fetch", config.web["max_bytes"])
        # Arbitrary cache fields are not shaped like generated receipts.
        receipt = self.seed_cache(config, "cached text",
                                  poison={"oversized": "\x01" * (maximum // 6 + 1)})
        self.assertGreater(len(canonical({**receipt, "cached": True})), maximum)
        with self.assertRaisesRegex(Denied, r"Web operation failed \(output_limit\)"):
            bounded(config, self.root / "run", "fetch", receipt["url"], lambda: False)


class WebWorkerLimitTests(unittest.TestCase):
    def test_worker_input_limit_covers_schema_maximum_escaped_probe(self):
        properties = DEFINITIONS["probe"][1]["properties"]
        headers = properties["headers"]
        header_properties = headers["items"]["properties"]
        escaped_chars = (properties["body"]["maxLength"] + properties["url"]["maxLength"]
                         + properties["method"]["maxLength"]
                         + headers["maxItems"] * (header_properties["name"]["maxLength"]
                                                    + header_properties["value"]["maxLength"]))
        required = 2 * MAX_INCLUDE_BYTES + 6 * escaped_chars + 2 * 64 * 1024
        self.assertEqual(MAX_WEB_WORKER_INPUT_BYTES, 3 * MAX_INCLUDE_BYTES)
        self.assertLessEqual(required, MAX_WEB_WORKER_INPUT_BYTES)

    def test_worker_rejects_oversized_stdin_before_json_parse(self):
        root = Path(__file__).resolve().parents[1] / "src"
        result = subprocess.run([sys.executable, "-m", "mizu.web_worker"],
                                input=b" " * (MAX_WEB_WORKER_INPUT_BYTES + 1),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=environment(extra={"PYTHONPATH": str(root)}),
                                timeout=10, check=False)
        stderr = result.stderr.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Web worker input exceeds byte limit", stderr)
        self.assertNotIn("JSON", stderr)

    def test_single_response_arithmetic_and_search_floor(self):
        header_allowance = 6 * PROBE_RESPONSE_HEADERS_MAX * (
            PROBE_RESPONSE_NAME_MAX + PROBE_RESPONSE_VALUE_MAX)
        self.assertEqual(header_allowance, 3244032)
        for max_bytes in (1, 524288, 1024 * 1024, 16777216):
            with self.subTest(max_bytes=max_bytes):
                self.assertEqual(_output_limit("fetch", max_bytes),
                                 max(2 * 1024 * 1024, 6 * max_bytes + 64 * 1024))
                self.assertEqual(_output_limit("probe", max_bytes),
                                 max(2 * 1024 * 1024, 6 * max_bytes + 64 * 1024 + header_allowance))
                self.assertEqual(_output_limit("search", max_bytes), 2 * 1024 * 1024)
                self.assertIs(type(_output_limit("probe", max_bytes)), int)
        self.assertEqual(_output_limit("probe", 524288) - _output_limit("fetch", 524288),
                         header_allowance)
        # Configured upper bound: arithmetic only, no 16 MiB body allocation.
        self.assertEqual(_output_limit("fetch", 16777216), 100728832)
        self.assertEqual(_output_limit("probe", 16777216), 103972864)


class FetchMediaTests(Fixture):
    def test_fetch_raw_response_limit_is_unchanged(self):
        config = dataclasses.replace(self.config, web={**self.config.web,
                                    "hosts": ["example.org"], "max_bytes": 3})
        web = Web(config.web, config.data / "web-cache", self.root / "sources")
        response = Mock(status=200)
        response.getheader.side_effect = lambda name, default=None: {
            "Content-Type": "text/plain"}.get(name, default)
        response.read.return_value = b"four"
        with patch("mizu.web.public_addresses", return_value=[]), \
             patch("mizu.web.PinnedHTTPS") as connection:
            connection.return_value.getresponse.return_value = response
            with self.assertRaisesRegex(Denied, "Web response exceeds byte limit"):
                web.fetch("https://example.org/over-limit")
        response.read.assert_called_once_with(4)
        connection.assert_called_once()
        self.assertFalse((web.cache / f"{digest(b'https://example.org/over-limit')}.json").exists())

    def test_long_content_type_is_bounded_without_changing_media_decision(self):
        config = dataclasses.replace(self.config, web={**self.config.web, "hosts": ["example.org"]})
        web = Web(config.web, config.data / "web-cache", self.root / "sources")
        # Not exactly text/html: preserve the original decision not to parse HTML.
        media = "text/html" + "é" * 512
        raw = b"<p>keep markup</p>"
        response = Mock(status=200)
        response.getheader.side_effect = lambda name, default=None: {
            "Content-Type": media + "; charset=utf-8"}.get(name, default)
        response.read.return_value = raw
        with patch("mizu.web.public_addresses", return_value=[]), \
             patch("mizu.web.PinnedHTTPS") as connection:
            connection.return_value.getresponse.return_value = response
            record = web.fetch("https://example.org/media")
        self.assertEqual(RESPONSE_MEDIA_MAX, 256)
        self.assertEqual(len(record["content_type"]), RESPONSE_MEDIA_MAX)
        self.assertEqual(record["content_type"], media[:RESPONSE_MEDIA_MAX])
        self.assertEqual(record["text"], raw.decode("utf-8"))
        response.read.assert_called_once_with(config.web["max_bytes"] + 1)
        self.assertEqual(web.replay(record["receipt_id"], "fetch"), record)
        cached = read_json(web.cache / f"{digest(record['url'].encode())}.json")
        self.assertEqual({k: v for k, v in cached.items() if k != "id"},
                         {k: v for k, v in record.items() if k not in ("id", "receipt_id", "raw_available")})


if __name__ == "__main__":
    unittest.main()
