"""Destination decider: delegable egress policy with fail-closed mechanism."""
import json
import sys
import unittest
from pathlib import Path
from support import Fixture
from mizu.errors import Denied
from mizu.web import FETCH_CACHE_SCHEMA_VERSION, Web


ALLOW = [sys.executable, "-c", "import json,sys; json.load(sys.stdin); print(json.dumps({'allow': True}))"]
DENY = [sys.executable, "-c", "import json,sys; json.load(sys.stdin); print(json.dumps({'allow': False}))"]
MALFORMED = [sys.executable, "-c", "print('not json')"]
FAIL = [sys.executable, "-c", "import sys; sys.exit(3)"]


def make_web(tmp, settings, role="searcher"):
    cache = Path(tmp) / "cache"
    receipts = Path(tmp) / "receipts"
    cache.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(parents=True, exist_ok=True)
    base = {"hosts": [], "feeds": [], "cache_seconds": 0, "timeout_seconds": 5,
            "max_bytes": 65536, "search_command": [], "destination_command": [],
            "intranet": False, "probe_hosts": [], "probe_methods": ["GET"]}
    base.update(settings)
    return Web(base, cache, receipts, role)


class DeciderTests(Fixture):
    def test_unset_keeps_static_lists(self):
        web = make_web(self.temporary.name, {"hosts": ["example.com"]})
        self.assertEqual(web.check_url("https://example.com/x", "fetch")[0], "example.com")
        with self.assertRaises(Denied):
            web.check_url("https://other.example/x", "fetch")

    def test_decider_grants_beyond_static_lists_per_role(self):
        seen = {}
        prog = ("import json,sys; req=json.load(sys.stdin); "
                "open(%r,'w').write(json.dumps(req)); "
                "print(json.dumps({'allow': req['role']=='searcher'}))" % str(Path(self.temporary.name) / "req.json"))
        web = make_web(self.temporary.name,
                       {"hosts": [], "destination_command": [sys.executable, "-c", prog]},
                       role="searcher")
        self.assertEqual(web.check_url("https://open.example/x", "fetch")[0], "open.example")
        payload = json.loads((Path(self.temporary.name) / "req.json").read_text())
        self.assertEqual(payload["capability"], "fetch")
        self.assertEqual(payload["host"], "open.example")
        self.assertEqual(payload["hop"], 0)
        # Same decider denies the worker role.
        worker = make_web(self.temporary.name,
                          {"hosts": [], "destination_command": [sys.executable, "-c", prog]},
                          role="worker")
        with self.assertRaises(Denied):
            worker.check_url("https://open.example/x", "fetch")

    def test_decider_denies_list_allowed_host(self):
        web = make_web(self.temporary.name,
                       {"hosts": ["example.com"], "destination_command": DENY})
        with self.assertRaises(Denied):
            web.check_url("https://example.com/x", "fetch")

    def test_fail_closed_on_faults(self):
        for cmd in (MALFORMED, FAIL, ["/nonexistent-decider-argv"]):
            web = make_web(self.temporary.name,
                           {"hosts": ["*"], "destination_command": cmd})
            with self.assertRaises(Denied):
                web.check_url("https://example.com/x", "fetch")

    def test_transport_protections_stay(self):
        web = make_web(self.temporary.name, {"destination_command": ALLOW})
        for url in ("http://example.com/x", "https://example.com:8443/x",
                    "https://user@example.com/x"):
            with self.assertRaises(Denied):
                web.check_url(url, "fetch")

    def test_probe_method_still_gated(self):
        import socket
        from unittest.mock import patch
        web = make_web(self.temporary.name,
                       {"probe_hosts": [], "probe_methods": ["GET"],
                        "destination_command": ALLOW})
        with self.assertRaises(Denied):
            web.probe({"url": "https://probe.example/x", "method": "DELETE"})
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        from test_probe import FakeConnection
        with patch("mizu.web.public_addresses", return_value=answer), \
             patch("mizu.web.PinnedHTTPS", FakeConnection):
            record = web.probe({"url": "https://probe.example/x", "method": "GET"})
        self.assertEqual(record["url"], "https://probe.example/x")


if __name__ == "__main__":
    unittest.main()


class DeciderCacheBypassTests(Fixture):
    """Fetch replays redirect hops live under a decider; receipts keep no hop count."""

    def test_fetch_bypasses_fresh_cache_under_decider(self):
        import time
        from unittest.mock import patch
        from mizu.fs import canonical, digest as fs_digest, read_json, write_json

        url = "https://example.com/live"
        tmp = Path(self.temporary.name)
        web = make_web(self.temporary.name,
                       {"hosts": [], "cache_seconds": 1800, "destination_command": ALLOW})
        cache_file = tmp / "cache" / f"{fs_digest(url.encode())}.json"
        write_json(cache_file, {"id": "a" * 64, "url": url, "final_url": url,
                                "retrieved_at": "2026-01-01T00:00:00Z",
                                "retrieved_epoch": time.time(), "sha256": "b" * 64,
                                "content_type": "text/plain", "text": "CACHED-POISON",
                                "trust": "external-untrusted",
                                "fetch_policy_digest": fs_digest(canonical({
                                    "hosts": [], "intranet": False, "max_bytes": 65536,
                                    "receipt_schema": FETCH_CACHE_SCHEMA_VERSION}))})

        class LiveResponse:
            status = 200

            def getheader(self, name, default=None):
                if name.lower() == "content-type":
                    return "text/plain"
                if name.lower() == "content-encoding":
                    return "identity"
                return default

            def getheaders(self):
                return [("Content-Type", "text/plain")]

            def read(self, limit=None):
                return b"LIVE-CONTENT" if limit is None else b"LIVE-CONTENT"[:limit]

        calls = []

        class LiveConnection:
            def __init__(self, host, addresses, timeout):
                calls.append(host)

            def request(self, method, path, body=None, headers=None):
                pass

            def getresponse(self):
                return LiveResponse()

            def close(self):
                pass

        import socket
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("mizu.web.public_addresses", return_value=answer), \
             patch("mizu.web.PinnedHTTPS", LiveConnection):
            record = web.fetch(url)
        self.assertIn("LIVE-CONTENT", record["text"])
        self.assertNotIn("cached", record)
        self.assertNotIn("fetch_policy_digest", record)
        self.assertTrue(calls, "decider fetch must reach transport live")
        # Bypass must not overwrite the seeded entry.
        self.assertIn("CACHED-POISON", read_json(cache_file)["text"])

    def test_fetch_uses_fresh_cache_without_decider(self):
        import time
        from unittest.mock import patch
        from mizu.fs import canonical, digest as fs_digest, write_json

        url = "https://example.com/live"
        tmp = Path(self.temporary.name)
        web = make_web(self.temporary.name,
                       {"hosts": ["example.com", "other.example"], "cache_seconds": 1800,
                        "destination_command": []})
        write_json(tmp / "cache" / f"{fs_digest(url.encode())}.json",
                   {"id": "c" * 64, "url": url, "final_url": url,
                    "retrieved_at": "2026-01-01T00:00:00Z",
                    "retrieved_epoch": time.time(), "sha256": "d" * 64,
                    "content_type": "text/plain", "text": "CACHED-POISON",
                    "trust": "external-untrusted",
                    "fetch_policy_digest": fs_digest(canonical({
                        "hosts": sorted(web.settings["hosts"]),
                        "intranet": False, "max_bytes": 65536,
                        "receipt_schema": FETCH_CACHE_SCHEMA_VERSION}))})

        # Host order is not a policy change. Original/final gates still run.
        web.settings["hosts"].reverse()
        with patch("mizu.web.PinnedHTTPS") as conn, \
             patch.object(web, "check_url", wraps=web.check_url) as check_url:
            record = web.fetch(url)
        conn.assert_not_called()
        self.assertEqual(check_url.call_count, 2)
        check_url.assert_any_call(url, "fetch")
        self.assertTrue(record.get("cached"))
        self.assertIn("CACHED-POISON", record["text"])


    def test_static_cache_misses_after_intermediate_host_revocation(self):
        from unittest.mock import Mock, patch
        from mizu.fs import canonical, digest as fs_digest, read_json

        url = "https://start.example/doc"
        middle = "https://middle.example/doc"
        end = "https://end.example/doc"
        web = make_web(self.temporary.name,
                       {"hosts": ["start.example", "middle.example", "end.example"],
                        "cache_seconds": 1800})
        start_response = Mock(status=302)
        start_response.getheader.return_value = middle
        middle_response = Mock(status=302)
        middle_response.getheader.return_value = end
        end_response = Mock(status=200)
        end_response.getheader.side_effect = lambda name, default=None: {
            "Content-Type": "text/plain"}.get(name, default)
        end_response.read.return_value = b"LIVE-CONTENT"
        connections = {
            host: Mock(getresponse=Mock(return_value=response))
            for host, response in (("start.example", start_response),
                                   ("middle.example", middle_response),
                                   ("end.example", end_response))}

        with patch("mizu.web.public_addresses", return_value=[]) as addresses, \
             patch("mizu.web.PinnedHTTPS", side_effect=lambda host, *_: connections[host]) as conn:
            first = web.fetch(url)
            self.assertNotIn("cached", first)
            self.assertEqual(first["final_url"], end)
            self.assertEqual([call.args[0] for call in conn.call_args_list],
                             ["start.example", "middle.example", "end.example"])
            cached = read_json(web.cache / f"{fs_digest(url.encode())}.json")
            self.assertEqual(cached["fetch_policy_digest"], fs_digest(canonical({
                "hosts": sorted(web.settings["hosts"]), "intranet": False, "max_bytes": 65536,
                "receipt_schema": FETCH_CACHE_SCHEMA_VERSION})))
            conn.reset_mock()
            addresses.reset_mock()
            web.settings["hosts"].remove("middle.example")

            with self.assertRaisesRegex(Denied, "Host is not allowlisted: middle.example"):
                web.fetch(url)
            # The live start redirect is re-read; the revoked middle is not resolved or dialed.
            self.assertEqual([call.args[0] for call in conn.call_args_list], ["start.example"])
            addresses.assert_called_once_with("start.example", False)

    def test_static_cache_without_policy_digest_is_a_miss(self):
        import time
        from unittest.mock import Mock, patch
        from mizu.fs import digest as fs_digest, read_json, write_json

        url = "https://example.com/live"
        web = make_web(self.temporary.name,
                       {"hosts": ["example.com"], "cache_seconds": 1800})
        cache_file = web.cache / f"{fs_digest(url.encode())}.json"
        write_json(cache_file, {"id": "e" * 64, "url": url, "final_url": url,
                                "retrieved_at": "2026-01-01T00:00:00Z",
                                "retrieved_epoch": time.time(), "sha256": "f" * 64,
                                "content_type": "text/plain", "text": "LEGACY-CONTENT",
                                "trust": "external-untrusted"})
        response = Mock(status=200)
        response.getheader.side_effect = lambda name, default=None: {
            "Content-Type": "text/plain"}.get(name, default)
        response.read.return_value = b"LIVE-CONTENT"
        with patch("mizu.web.public_addresses", return_value=[]), \
             patch("mizu.web.PinnedHTTPS") as conn:
            conn.return_value.getresponse.return_value = response
            record = web.fetch(url)
        conn.assert_called_once()
        self.assertNotIn("cached", record)
        self.assertEqual(record["text"], "LIVE-CONTENT")
        self.assertIn("fetch_policy_digest", record)
        self.assertEqual(read_json(cache_file), record)

    def test_previous_receipt_schema_cache_misses(self):
        import time
        from unittest.mock import Mock, patch
        from mizu.fs import canonical, digest as fs_digest, write_json

        url = "https://example.com/live"
        web = make_web(self.temporary.name,
                       {"hosts": ["example.com"], "cache_seconds": 1800})
        cache_file = web.cache / f"{fs_digest(url.encode())}.json"
        # Receipt digest used before content-type metadata became bounded.
        old_digest = fs_digest(canonical({"hosts": ["example.com"],
                                          "intranet": False, "max_bytes": 65536}))
        write_json(cache_file, {"id": "g" * 64, "url": url, "final_url": url,
                                "retrieved_at": "2026-01-01T00:00:00Z",
                                "retrieved_epoch": time.time(), "sha256": "h" * 64,
                                "content_type": "text/" + "x" * 4096,
                                "text": "LEGACY-CONTENT", "trust": "external-untrusted",
                                "fetch_policy_digest": old_digest})
        response = Mock(status=200)
        response.getheader.side_effect = lambda name, default=None: {
            "Content-Type": "text/plain"}.get(name, default)
        response.read.return_value = b"LIVE-CONTENT"
        with patch("mizu.web.public_addresses", return_value=[]), \
             patch("mizu.web.PinnedHTTPS") as conn:
            conn.return_value.getresponse.return_value = response
            record = web.fetch(url)
        conn.assert_called_once()
        self.assertNotIn("cached", record)
        self.assertEqual(record["text"], "LIVE-CONTENT")
        self.assertEqual(record["content_type"], "text/plain")
        self.assertNotEqual(record["fetch_policy_digest"], old_digest)
