"""Destination decider: delegable egress policy with fail-closed mechanism."""
import json
import sys
import unittest
from pathlib import Path
from support import Fixture
from mizu.errors import Denied
from mizu.web import Web


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
        from mizu.fs import digest as fs_digest, read_json, write_json

        url = "https://example.com/live"
        tmp = Path(self.temporary.name)
        web = make_web(self.temporary.name,
                       {"hosts": [], "cache_seconds": 1800, "destination_command": ALLOW})
        cache_file = tmp / "cache" / f"{fs_digest(url.encode())}.json"
        write_json(cache_file, {"id": "a" * 64, "url": url, "final_url": url,
                                "retrieved_at": "2026-01-01T00:00:00Z",
                                "retrieved_epoch": time.time(), "sha256": "b" * 64,
                                "content_type": "text/plain", "text": "CACHED-POISON",
                                "trust": "external-untrusted"})

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
        self.assertTrue(calls, "decider fetch must reach transport live")
        # Bypass must not overwrite the seeded entry.
        self.assertIn("CACHED-POISON", read_json(cache_file)["text"])

    def test_fetch_uses_fresh_cache_without_decider(self):
        import time
        from unittest.mock import patch
        from mizu.fs import digest as fs_digest, write_json

        url = "https://example.com/live"
        tmp = Path(self.temporary.name)
        web = make_web(self.temporary.name,
                       {"hosts": ["example.com"], "cache_seconds": 1800,
                        "destination_command": []})
        write_json(tmp / "cache" / f"{fs_digest(url.encode())}.json",
                   {"id": "c" * 64, "url": url, "final_url": url,
                    "retrieved_at": "2026-01-01T00:00:00Z",
                    "retrieved_epoch": time.time(), "sha256": "d" * 64,
                    "content_type": "text/plain", "text": "CACHED-POISON",
                    "trust": "external-untrusted"})

        with patch("mizu.web.PinnedHTTPS") as conn:
            record = web.fetch(url)
        conn.assert_not_called()
        self.assertTrue(record.get("cached"))
        self.assertIn("CACHED-POISON", record["text"])
