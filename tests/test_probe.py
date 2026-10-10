"""Policy-bounded protocol observation. Offline only (mocked transport)."""
import socket
import unittest
from pathlib import Path
from unittest.mock import patch
from support import Fixture
from mizu.errors import Denied
from mizu.web import Web


SETTINGS = {"hosts": [], "feeds": [], "cache_seconds": 1800, "timeout_seconds": 5,
            "max_bytes": 65536, "search_command": [], "intranet": False,
            "probe_hosts": ["probe.example"], "probe_methods": ["GET", "POST"]}


class FakeResponse:
    def __init__(self, status=404, headers=(("Content-Type", "application/json"), ("Cache-Control", "no-store")), body=b'{"error":"nope"}'):
        self.status = status
        self._headers = list(headers)
        self._body = body

    def getheaders(self):
        return list(self._headers)

    def getheader(self, name, default=None):
        lowered = name.lower()
        for key, value in self._headers:
            if key.lower() == lowered:
                return value
        return default

    def read(self, limit=None):
        return self._body if limit is None else self._body[:limit]


class FakeConnection:
    last = None

    def __init__(self, host, addresses, timeout):
        self.host = host
        self.sent = None
        FakeConnection.last = self

    def request(self, method, path, body=None, headers=None):
        self.sent = (method, path, body, dict(headers or {}))

    def getresponse(self):
        return FakeResponse()

    def close(self):
        pass


def make_web(fixture_tmp: Path):
    cache = fixture_tmp / "cache"
    receipts = fixture_tmp / "receipts"
    cache.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(parents=True, exist_ok=True)
    return Web(dict(SETTINGS), cache, receipts)


class ProbeTests(Fixture):
    def test_disabled_without_hosts(self):
        web = make_web(Path(self.temporary.name))
        web.settings = {**SETTINGS, "probe_hosts": []}
        with self.assertRaises(Denied):
            web.probe({"url": "https://probe.example/x", "method": "GET"})

    def test_method_allowlist(self):
        web = make_web(Path(self.temporary.name))
        with self.assertRaises(Denied):
            web.probe({"url": "https://probe.example/x", "method": "DELETE"})

    def test_host_allowlist(self):
        web = make_web(Path(self.temporary.name))
        with self.assertRaises(Denied):
            web.probe({"url": "https://other.example/x", "method": "GET"})

    def test_credential_headers_refused(self):
        web = make_web(Path(self.temporary.name))
        for name in ("Authorization", "Cookie", "Proxy-Auth", "Host", "Content-Length", "Sec-Fetch-Site"):
            with self.assertRaises(Denied):
                web.probe({"url": "https://probe.example/x", "method": "GET",
                           "headers": [{"name": name, "value": "secret"}]})

    def test_cr_headers_refused_before_transport(self):
        web = make_web(Path(self.temporary.name))
        for headers in ([{"name": "X-A", "value": "a\rb"}],
                        [{"name": "X-A\rb", "value": "1"}]):
            with self.assertRaises(Denied):
                web.probe({"url": "https://probe.example/x", "method": "GET",
                           "headers": headers})

    def test_non_2xx_returns_as_observation(self):
        web = make_web(Path(self.temporary.name))
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("mizu.web.public_addresses", return_value=answer), \
             patch("mizu.web.PinnedHTTPS", FakeConnection):
            record = web.probe({"url": "https://probe.example/rpc", "method": "POST",
                                "headers": [{"name": "X-Test", "value": "1"}],
                                "body": '{"jsonrpc":"2.0"}'})
        self.assertEqual(record["status"], 404)
        self.assertEqual(record["method"], "POST")
        self.assertEqual(record["trust"], "external-untrusted")
        self.assertIn("nope", record["text"])
        self.assertTrue(any(h["name"].lower() == "cache-control" for h in record["headers"]))
        method, path, body, headers = FakeConnection.last.sent
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/rpc")
        self.assertEqual(headers.get("X-Test"), "1")
        self.assertNotIn("Authorization", headers)
        # Receipt persisted
        receipts = list((Path(self.temporary.name) / "receipts").glob("*.json"))
        self.assertEqual(len(receipts), 1)

    def test_no_redirect_follow_and_no_cache(self):
        class Redirect(FakeResponse):
            def __init__(self):
                super().__init__(status=302, headers=(("Location", "https://probe.example/other"),), body=b"")

        class RedirectConn(FakeConnection):
            def getresponse(self):
                return Redirect()

        web = make_web(Path(self.temporary.name))
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("mizu.web.public_addresses", return_value=answer), \
             patch("mizu.web.PinnedHTTPS", RedirectConn):
            record = web.probe({"url": "https://probe.example/a", "method": "GET"})
        self.assertEqual(record["status"], 302)
        self.assertEqual(len(list((Path(self.temporary.name) / "cache").glob("*.json"))), 0)

    def test_protocol_schema_and_capability(self):
        from mizu.protocol import DEFINITIONS, validate
        from mizu.config import CAPABILITIES
        self.assertIn("probe", CAPABILITIES)
        self.assertIn("probe", DEFINITIONS)
        validate({"url": "https://probe.example/x", "method": "GET"}, DEFINITIONS["probe"][1])
        with self.assertRaises(Denied):
            validate({"url": "https://probe.example/x"}, DEFINITIONS["probe"][1])

    def test_runtime_dispatch_pages_body(self):
        import dataclasses
        from mizu.errors import Denied as _Denied
        ctx = self.context("worker")
        # Fixture worker has no probe grant: fail closed before any network.
        with self.assertRaises(_Denied):
            ctx.handle("probe", {"url": "https://probe.example/x", "method": "GET"})
        role = dataclasses.replace(ctx.role, capabilities=tuple([*ctx.role.capabilities, "probe"]))
        ctx.role = role
        record = {"id": "a" * 64, "url": "https://probe.example/x", "method": "GET",
                  "status": 200, "headers": [], "text": "0123456789", "trust": "external-untrusted"}
        import mizu.runtime as rt
        with patch.object(rt, "bounded", return_value=dict(record)):
            out = ctx.handle("probe", {"url": "https://probe.example/x", "method": "GET",
                                       "offset": 2, "limit": 3})
        self.assertEqual(out["text"], "234")
        self.assertEqual(out["status"], 200)
        self.assertTrue(out["truncated"])

    def test_probe_config_defaults_fail_closed(self):
        # Fixture config has no probe_hosts/probe_methods: probe refuses.
        self.assertEqual(list(self.config.web.get("probe_hosts", [])), [])
        self.assertEqual(list(self.config.web.get("probe_methods", [])), [])


if __name__ == "__main__":
    unittest.main()
