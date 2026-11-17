"""Policy-bounded protocol observation. Offline only (mocked transport)."""
import http.client
import io
import socket
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
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
        self.length = 0  # Complete body supplied by this transport fixture.

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
    def test_probe_requires_complete_bounded_http_body(self):
        cases = (
            ("short", "GET", 200, b"Content-Length: 10\r\n", b"abc", "incomplete"),
            ("fixed", "GET", 200, b"Content-Length: 3\r\n", b"abc", None),
            ("chunked", "GET", 200, b"Transfer-Encoding: chunked\r\nContent-Length: 10\r\n",
             b"3\r\nabc\r\n0\r\n\r\n", None),
            ("close", "GET", 200, b"", b"abc", None),
            ("head", "HEAD", 200, b"Content-Length: 10\r\n", b"", None),
            ("informational", "GET", 103, b"Content-Length: 10\r\n", b"", None),
            ("no-content", "GET", 204, b"Content-Length: 10\r\n", b"", None),
            ("not-modified", "GET", 304, b"Content-Length: 10\r\n", b"", None),
            ("missing-last-chunk", "GET", 200, b"Transfer-Encoding: chunked\r\n",
             b"3\r\nabc\r\n", "incomplete"),
            ("short-chunk", "GET", 200, b"Transfer-Encoding: chunked\r\n", b"5\r\nabc", "incomplete"),
            ("oversize", "GET", 200, b"Content-Length: 4\r\n", b"abcd", "exceeds byte limit"),
        )
        for name, method, status, headers, body, error in cases:
            with self.subTest(name=name):
                wire = (f"HTTP/1.1 {status} Test\r\n".encode()
                        + headers + b"\r\n" + body)
                response = http.client.HTTPResponse(Mock(makefile=lambda *args: io.BytesIO(wire)), method=method)
                response.begin()
                web = make_web(self.root / name)
                web.settings.update(max_bytes=3, probe_methods=["GET", "HEAD"])
                with response, patch("mizu.web.public_addresses", return_value=[]), \
                     patch("mizu.web.PinnedHTTPS") as connection:
                    connection.return_value.getresponse.return_value = response
                    request = {"url": "https://probe.example/body", "method": method}
                    if error:
                        with self.assertRaisesRegex(Denied, error):
                            web.probe(request)
                    else:
                        record = web.probe(request)
                        self.assertEqual(record["text"], "abc" if status == 200 and method == "GET" else "")
                        self.assertEqual(record["status"], status)
                        self.assertEqual(web.replay(record["receipt_id"], "probe"), record)
                self.assertEqual(list(web.cache.glob("*.json")), [])
                self.assertEqual(len(list(web.receipts.glob("*/receipt.json"))), 0 if error else 1)
                connection.return_value.close.assert_called_once()

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

    def test_host_domain_suffix_uses_shared_check(self):
        from mizu.web import validate_url
        web = make_web(Path(self.temporary.name))
        web.settings = {**SETTINGS, "probe_hosts": [".probe.example"], "probe_methods": ["GET"]}
        # Subdomain passes the shared destination check (method still gated).
        self.assertEqual(validate_url("https://api.probe.example/x", [".probe.example"]),
                         ("api.probe.example", "/x"))
        with self.assertRaises(Denied):
            web.probe({"url": "https://other.example/x", "method": "GET"})
        with self.assertRaises(Denied):
            web.probe({"url": "https://api.probe.example/x", "method": "DELETE"})

    def test_host_open_entry_uses_shared_check(self):
        from mizu.web import validate_url
        # "*" opens any host through the same check; methods still gated.
        self.assertEqual(validate_url("https://anything.example/x", ["*"]),
                         ("anything.example", "/x"))
        web = make_web(Path(self.temporary.name))
        web.settings = {**SETTINGS, "probe_hosts": ["*"], "probe_methods": ["GET"]}
        self.assertEqual(validate_url("https://anything.example/x", web.settings["probe_hosts"]),
                         ("anything.example", "/x"))
        with self.assertRaises(Denied):
            web.probe({"url": "https://anything.example/x", "method": "DELETE"})

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
        receipts = list((Path(self.temporary.name) / "receipts").glob("*/receipt.json"))
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
        validate({"receipt_id": "a" * 64}, DEFINITIONS["probe"][1])
        # Conditional request requirements are enforced before dispatch.
        with patch("mizu.runtime.bounded") as bounded:
            with self.assertRaises(Denied):
                self.context()._op_probe({"url": "https://probe.example/x"})
            bounded.assert_not_called()

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
