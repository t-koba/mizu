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
