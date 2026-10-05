"""Allowlisted HTTPS retrieval with DNS pinning, byte bounds and provenance.

Search defaults to a small feed index. An operator-owned JSON stdin/stdout
program can supply a general search service without coupling the core to it.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import time
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

from . import __version__
from .errors import Denied
from .fs import digest, now, read_json, write_json
from .process import run

#: Retained search candidates across adapter and feeds. Matches the tool
#: page limit so explicit paging can walk everything the sources returned.
#: Byte protection stays with per-field bounds and the adapter stdout cap
#: (`[web] max_bytes`); this count never enlarges a prompt by itself
#: because `_op_search` pages through it.
SEARCH_RESULT_LIMIT = 1000
#: Per-feed shaping bound: one huge feed must not crowd out other sources
#: before the global cut. Feed order decides which items are kept.
SEARCH_FEED_ITEM_LIMIT = 200


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag in ("p", "div", "br", "li", "h1", "h2", "h3", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def validate_url(url: str, hosts: list[str]) -> tuple[str, str]:
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise Denied("Invalid URL")
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise Denied("Invalid port") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or port not in (None, 443) or parsed.username or parsed.password:
        raise Denied("Only HTTPS without credentials, on port 443, is allowed")
    if not host or host not in {h.lower() for h in hosts}:
        raise Denied(f"Host is not allowlisted: {host}")
    # Fragments are client-side only: strip them instead of refusing, so feed
    # links like /doc#section-3 stay fetchable. They never reach the wire.
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return host, path


def public_addresses(host: str, allow_private: bool = False) -> list[tuple]:
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise Denied("DNS returned no addresses")
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        # Loopback, link-local, multicast, unspecified and transition addresses
        # are never dialable: they reach the host itself or nowhere meaningful.
        # Global unicast is always fine; RFC 1918/ULA private addresses need the
        # operator's explicit `intranet` choice.
        if ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_unspecified \
                or getattr(ip, "ipv4_mapped", None):
            raise Denied("DNS target is not a dialable address")
        if ip.version == 6 and (ip in ipaddress.ip_network("64:ff9b::/96") or
                               ip in ipaddress.ip_network("2002::/16")):
            raise Denied("Transition addresses are not supported")
        if not ip.is_global and not (allow_private and ip.is_private):
            raise Denied("DNS target is not a public unicast address")
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, addresses: list[tuple], timeout: int):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.addresses = addresses

    def connect(self):
        last_error: OSError | None = None
        for family, socktype, protocol, _, address in self.addresses:
            raw = socket.socket(family, socktype, protocol)
            raw.settimeout(self.timeout)
            try:
                raw.connect(address)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError as exc:
                raw.close()
                last_error = exc
        raise last_error or OSError("No address could be reached")


class Web:
    def __init__(self, settings: dict, cache: Path, receipts: Path):
        self.settings, self.cache, self.receipts = settings, cache, receipts

    def fetch(self, url: str) -> dict:
        original = url
        cache_file = self.cache / f"{digest(url.encode())}.json"
        cached = read_json(cache_file)
        if cached and time.time() - cached["retrieved_epoch"] < self.settings["cache_seconds"]:
            # A policy change must also revoke previously cached hosts.
            validate_url(cached["url"], self.settings["hosts"])
            validate_url(cached["final_url"], self.settings["hosts"])
            write_json(self.receipts / f"{cached['id']}.json", cached, exclusive=True)
            return {**cached, "cached": True}
        for _ in range(4):
            host, path = validate_url(url, self.settings["hosts"])
            intranet = bool(self.settings.get("intranet", False))
            connection = PinnedHTTPS(host, public_addresses(host, intranet), self.settings["timeout_seconds"])
            try:
                connection.request("GET", path, headers={"User-Agent": "Mizu/" + __version__,
                                   "Accept-Encoding": "identity", "Accept": "text/*,application/xml,application/json"})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise Denied("Redirect did not include a location")
                    url = urllib.parse.urljoin(url, location)
                    continue
                if response.status != 200:
                    raise Denied(f"HTTP status {response.status}")
                if response.getheader("Content-Encoding", "identity") != "identity":
                    raise Denied("Compressed web responses are not accepted")
                raw = response.read(self.settings["max_bytes"] + 1)
                if len(raw) > self.settings["max_bytes"]:
                    raise Denied("Web response exceeds byte limit")
                media = response.getheader("Content-Type", "").split(";")[0]
                if media and not (media.startswith("text/") or media in (
                        "application/xml", "application/json", "application/atom+xml", "application/rss+xml")):
                    raise Denied("Only text, JSON and XML documents are supported")
                text = raw.decode("utf-8", "replace")
                if media == "text/html":
                    parser = Text()
                    parser.feed(text)
                    text = "".join(parser.parts)
                receipt = {"id": digest((original + "\n" + digest(raw) + "\n" + str(time.time_ns())).encode()),
                           "url": original, "final_url": url, "retrieved_at": now(),
                           "retrieved_epoch": time.time(), "sha256": digest(raw),
                           "content_type": media, "text": text, "trust": "external-untrusted"}
                write_json(cache_file, receipt)
                write_json(self.receipts / f"{receipt['id']}.json", receipt, exclusive=True)
                return receipt
            finally:
                connection.close()
        raise Denied("Too many redirects")

    def _search_feeds(self, query: str) -> tuple[list, list, int, int]:
        """Keyword overlap over every configured feed; best-effort per feed.

        Schema: ``(results, errors, feeds_total, feeds_consulted)``. Each
        result carries ``"source"`` naming the feed it came from. Bounds:
        titles 300, summaries 1500, at most ``SEARCH_FEED_ITEM_LIMIT`` items
        per feed. Trust: external-untrusted. Failure: per-feed faults are
        listed, never raised.
        """
        results, errors = [], []
        feeds = list(self.settings["feeds"])
        for feed in feeds:
            kept = 0
            try:
                receipt = self.fetch(feed)
                source = receipt["text"]
                if "<!DOCTYPE" in source.upper() or "<!ENTITY" in source.upper():
                    raise Denied("XML document type and entity declarations are refused")
                root = ET.fromstring(source)
                for item in root.iter():
                    if kept >= SEARCH_FEED_ITEM_LIMIT:
                        break
                    if item.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
                        continue
                    fields = {child.tag.rsplit("}", 1)[-1]: child for child in item}
                    title_node, link_node = fields.get("title"), fields.get("link")
                    title = "" if title_node is None else "".join(title_node.itertext())
                    link = "" if link_node is None else link_node.get("href", link_node.text or "")
                    summary_node = fields.get("description")
                    if summary_node is None:
                        summary_node = fields.get("summary")
                    summary = "" if summary_node is None else "".join(summary_node.itertext())
                    if not link.startswith("https://"):
                        continue
                    # Fixed discovery shaping: keyword overlap only. Operator
                    # ranking lives in `search_command` when configured.
                    score = sum(word.lower() in (title + " " + summary).lower() for word in query.split())
                    results.append({"title": title[:300], "url": link, "summary": summary[:1500],
                                    "source": feed, "source_receipt": receipt["id"], "score": score})
                    kept += 1
            except (Denied, OSError, ET.ParseError, http.client.HTTPException) as exc:
                errors.append({"feed": feed, "error": str(exc)})
        ordered = sorted(results, key=lambda r: r["score"], reverse=True)
        return ordered, errors, len(feeds), len(feeds) - len(errors)

    def search(self, query: str) -> dict:
        """Search every configured source with explicit truncation flags.

        Schema: ``{"results": [{title,url,source,...}], "errors": [...],
        "scope", "trust", "truncated": bool, "feeds_total": int,
        "feeds_consulted": int}``. Bounds: titles 300, summaries 1500,
        at most ``SEARCH_RESULT_LIMIT`` retained candidates (matches the tool
        page limit, so callers page the full retained set). The adapter keeps
        ranking priority: its results come first, then feed matches fill the
        remaining slots. Trust: external-untrusted. Retry: per-feed
        best-effort, errors listed. Evidence: source_receipt per feed result.
        Failure: Denied for bad query/adapter shape.
        """
        if not isinstance(query, str) or not 1 <= len(query) <= 1000:
            raise Denied("Search query must be 1–1000 characters")
        command = self.settings["search_command"]
        if command:
            result = run(command, timeout=self.settings["timeout_seconds"],
                         maximum=self.settings["max_bytes"],
                         input_data=json.dumps({"query": query}).encode() + b"\n")
            if result.exit_code != 0 or result.reason != "exited":
                raise Denied("Configured search program failed")
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise Denied("Search adapter must return a JSON object with a results array")
            adapter_overflow = len(data["results"]) > SEARCH_RESULT_LIMIT
            results = data["results"]
            for item in results:
                if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("title", "url", "summary")):
                    raise Denied("Invalid search adapter result")
                # Search can discover new hosts. Fetching them still needs an explicit grant.
                if not item["url"].startswith("https://"):
                    raise Denied("Search result URL must use HTTPS")
                item["source"] = "search-adapter"
            results = results[:SEARCH_RESULT_LIMIT]
            feeds, errors, feeds_total, feeds_consulted = self._search_feeds(query)
            room = SEARCH_RESULT_LIMIT - len(results)
            results = results + feeds[:room]
            truncated = adapter_overflow or len(feeds) > room
            scope = "configured-search-adapter+feeds" if feeds_total else "configured-search-adapter"
            return {"results": results, "errors": errors, "scope": scope, "trust": "external-untrusted",
                    "truncated": truncated, "feeds_total": feeds_total, "feeds_consulted": feeds_consulted}
        ordered, errors, feeds_total, feeds_consulted = self._search_feeds(query)
        truncated = len(ordered) > SEARCH_RESULT_LIMIT
        return {"results": ordered[:SEARCH_RESULT_LIMIT],
                "errors": errors, "scope": "configured-feeds-only", "trust": "external-untrusted",
                "truncated": truncated, "feeds_total": feeds_total, "feeds_consulted": feeds_consulted}
