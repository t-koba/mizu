"""Offline feed observations, role cursor transactions and real worker IPC."""
import dataclasses
import json
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import Fixture
from mizu import feed_updates
from mizu.config import CAPABILITIES
from mizu.errors import Denied
from mizu.fs import canonical, digest, now, read_json, write_json
from mizu.protocol import DEFINITIONS, validate
from mizu.web import FETCH_CACHE_SCHEMA_VERSION, Web


FEED = "https://example.org/feed"
OTHER = "https://example.org/other"


def rss(*items):
    return "<rss><channel>" + "".join(items) + "</channel></rss>"


def item(key, title="title", summary="summary"):
    return (f"<item><guid>{key}</guid><title>{title}</title>"
            f"<description>{summary}</description><link>https://example.org/{key}</link></item>")


class FeedUpdatesTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, web={**self.config.web, "hosts": ["example.org"],
                                                          "feeds": [FEED], "search_feed_item_limit": 3})
        self.xml = {FEED: rss(item("a")), OTHER: rss(item("a"))}

    def observe(self, config, run_dir, operation, value, cancel, role):
        self.assertEqual(operation, "feed_updates")
        web = Web(config.web, config.data / "web-cache", run_dir / "sources", role)
        web.fetch = Mock(return_value={"text": self.xml[value], "id": "a" * 64})
        return web.feed_entries(value)

    def read(self, role="searcher"):
        ctx = self.context(role)
        with patch.object(ctx, "_bounded_web_operation", side_effect=self.observe):
            return ctx._op_feed_updates({})

    def state_path(self, role="searcher", feed=FEED):
        return self.project.root / "feed-updates" / digest(role.encode()) / (digest(feed.encode()) + ".json")

    def test_explicit_grant_and_empty_argument_schema(self):
        self.assertIn("feed_updates", CAPABILITIES)
        validate({}, DEFINITIONS["feed_updates"][1])
        with self.assertRaises(Denied):
            validate({"role": "worker"}, DEFINITIONS["feed_updates"][1])
        ctx = self.context("searcher")
        with self.assertRaisesRegex(Denied, "no capability"):
            ctx.handle("feed_updates", {})
        ctx.role = dataclasses.replace(ctx.role, capabilities=ctx.role.capabilities + ("feed_updates",))
        with patch.object(ctx, "_bounded_web_operation", side_effect=self.observe):
            result = ctx.handle("feed_updates", {})
        self.assertTrue(result["complete"])
        self.assertEqual(result["trust"], "external-untrusted")

    def test_stable_ids_namespaced_by_feed_and_role_latest_set(self):
        first = self.read()
        identity = first["feeds"][0]["entries"][0]["identity"]
        self.xml[FEED] = rss(item("a", "changed title").replace("https://example.org/a", "https://example.org/new"))
        self.assertEqual(self.read()["feeds"][0]["entries"], [])
        self.assertEqual(self.read("reviewer")["feeds"][0]["entries"][0]["identity"], identity)
        self.config = dataclasses.replace(self.config, web={**self.config.web, "feeds": [FEED, OTHER]})
        statuses = self.read()["feeds"]
        self.assertNotEqual(statuses[1]["entries"][0]["identity"], identity)
        self.xml[FEED] = rss(item("b"))
        self.assertEqual(len(self.read()["feeds"][0]["entries"]), 1)
        state = read_json(self.state_path())
        self.assertEqual(len(state["identities"]), 1)
        self.xml[FEED] = rss(item("a"))  # not an all-time seen ledger
        self.assertEqual(len(self.read()["feeds"][0]["entries"]), 1)

    def test_atom_id_alternate_link_and_https_fallback(self):
        self.xml[FEED] = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>urn:entry:1</id>'
                          '<link rel="self" href="https://example.org/self"/>'
                          '<link href="https://example.org/article"/><title>Atom</title></entry></feed>')
        first = self.read()["feeds"][0]["entries"][0]
        self.assertEqual(first["url"], "https://example.org/article")
        self.xml[FEED] = self.xml[FEED].replace("/article", "/changed")
        self.assertEqual(self.read()["feeds"][0]["entries"], [])
        self.xml[FEED] = rss('<item><link>https://example.org/fallback</link></item>')
        fallback = self.read()["feeds"][0]["entries"][0]
        self.assertNotEqual(first["identity"], fallback["identity"])
        self.assertEqual(self.read()["feeds"][0]["entries"], [])

    def test_invalid_identities_and_overflow_never_replace_cursor(self):
        self.read()
        before = self.state_path().read_bytes()
        cases = [(rss("<item><title>No identity</title></item>"), "missing_identity"),
                 (rss(item("b"), item("b")), "duplicate_identity"),
                 (rss(item("b").replace("</guid>", "</guid><guid>other</guid>")), "ambiguous_identity"),
                 (rss('<item><link>https://example.org/a</link><link>https://example.org/b</link></item>'),
                  "ambiguous_identity"),
                 (rss(item("b"), item("c"), item("d"), item("e")), None)]
        for xml, error in cases:
            with self.subTest(error=error):
                self.xml[FEED] = xml
                result = self.read()
                status = result["feeds"][0]
                self.assertFalse(result["complete"])
                self.assertFalse(status["cursor_advanced"])
                self.assertEqual(status["overflow"], error is None)
                if error:
                    self.assertIn(error, status["errors"])
                self.assertEqual(self.state_path().read_bytes(), before)
        self.xml[FEED] = rss(item("b"), item("c"), item("d"))
        self.assertTrue(self.read()["complete"])  # exactly at bound

    def test_corrupt_or_unsafe_cursor_fails_closed(self):
        self.read()
        path = self.state_path()
        valid = path.read_bytes()
        for content in (b"{", b"x" * (feed_updates._STATE_BYTES + 1),
                        canonical({**read_json(path), "identities": ["bad"]}),
                        canonical({**read_json(path), "identities": ["f" * 64]}),
                        valid.replace(b'"version":1', b'"version":0,"version":1')):
            path.write_bytes(content)
            result = self.read()["feeds"][0]
            self.assertEqual(result["errors"], ["corrupt_state"])
            self.assertFalse(result["complete"])
            self.assertEqual(path.read_bytes(), content)
        path.write_bytes(valid)
        target = self.root / "linked-cursor"
        target.write_bytes(valid)
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("Symlink creation unavailable")
        self.assertEqual(self.read()["feeds"][0]["errors"], ["corrupt_state"])
        self.assertEqual(target.read_bytes(), valid)

    def test_independent_feed_failure_and_state_write_failure(self):
        self.config = dataclasses.replace(self.config, web={**self.config.web, "feeds": [FEED, OTHER]})
        ctx = self.context("searcher")
        def operation(*args):
            if args[3] == FEED:
                raise Denied("fetch failed")
            return self.observe(*args)
        with patch.object(ctx, "_bounded_web_operation", side_effect=operation):
            statuses = ctx._op_feed_updates({})["feeds"]
        self.assertFalse(statuses[0]["complete"])
        self.assertTrue(statuses[1]["complete"])
        self.assertFalse(self.state_path().exists())
        self.assertTrue(self.state_path(feed=OTHER).exists())
        with patch("mizu.feed_updates.safe_write", side_effect=OSError("disk full")):
            status = self.read()["feeds"][0]
        self.assertEqual(status["errors"], ["state_write_failed"])
        self.assertFalse(status["cursor_advanced"])
        self.assertEqual(len(self.read()["feeds"][0]["entries"]), 1)

    def test_response_truncation_and_cancellation_do_not_consume_entries(self):
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits,
                                                                                tool_response_bytes=4096))
        self.xml[FEED] = rss(*(item(str(n), "t" * 300, "s" * 1500) for n in range(3)))
        for _ in range(2):
            result = self.read()
            self.assertLessEqual(len(canonical(result)), 4096)
            self.assertTrue(result["truncated"])
            self.assertFalse(result["complete"])
            self.assertFalse(self.state_path().exists())
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits,
                                                                                tool_response_bytes=32768))
        ctx = self.context("searcher")
        with patch.object(ctx, "_bounded_web_operation", side_effect=self.observe), \
                patch.object(ctx, "tools_cancelled", return_value=True):
            status = ctx._op_feed_updates({})["feeds"][0]
        self.assertEqual(status["errors"], ["cancelled"])
        self.assertFalse(self.state_path().exists())
        self.assertEqual(len(self.read()["feeds"][0]["entries"]), 3)
        self.xml[FEED] = rss(item("long", "t" * 500, "s" * 3000))
        result = self.read()
        self.assertTrue(result["complete"])
        self.assertEqual(result["feeds"][0]["entries"][0]["title"], "t" * 500)
        self.assertEqual(result["feeds"][0]["entries"][0]["summary"], "s" * 3000)

    def test_release_unconfigured_feed_state_and_atomic_leftovers(self):
        self.read()
        path = self.state_path()
        leftover = path.parent / ".new-abcd1234"
        leftover.write_bytes(b"unfinished")
        self.config = dataclasses.replace(self.config, web={**self.config.web, "feeds": []})
        self.assertTrue(self.read()["complete"])
        self.assertFalse(path.exists())
        self.assertFalse(leftover.exists())
        self.assertTrue((path.parents[2] / "feed-updates.lock").exists())  # stable project gate
        self.config = dataclasses.replace(self.config, web={**self.config.web, "feeds": [FEED]})
        self.assertEqual(len(self.read()["feeds"][0]["entries"]), 1)

    def test_concurrent_reads_serialize_cursor_consumption(self):
        first, second, release = Event(), Event(), Event()
        def operation(entered, *args):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.observe(*args)
        contexts = [self.context("searcher"), self.context("searcher")]
        contexts[0]._bounded_web_operation = lambda *args: operation(first, *args)
        contexts[1]._bounded_web_operation = lambda *args: operation(second, *args)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(contexts[0]._op_feed_updates, {})
            self.assertTrue(first.wait(5))
            b = pool.submit(contexts[1]._op_feed_updates, {})
            try:
                self.assertTrue(second.wait(5))  # neither network call holds the state gate
            finally:
                release.set()
            results = [a.result(10), b.result(10)]
        self.assertEqual(sorted(len(r["feeds"][0]["entries"]) for r in results), [0, 1])
        self.assertTrue(all(r["complete"] for r in results))

    def test_release_removed_role_state_preserves_current_role_and_project(self):
        self.read()
        self.read("reviewer")
        current, removed = self.state_path(), self.state_path("reviewer")
        before = current.read_bytes()
        gate = self.project.root / "feed-updates.lock"
        inode = gate.stat().st_ino
        obsolete_lock = removed.parent.with_suffix(".lock")
        obsolete_lock.touch()
        (removed.parent / ".new-abcd1234").write_bytes(b"unfinished")
        unrelated = self.root / "other-project" / "feed-updates" / removed.parent.name / removed.name
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(removed.read_bytes())
        self.config = dataclasses.replace(self.config, roles={k: v for k, v in self.config.roles.items()
                                                             if k != "reviewer"})
        self.assertEqual(self.read()["feeds"][0]["entries"], [])
        self.assertFalse(removed.parent.exists())
        self.assertFalse(obsolete_lock.exists())
        self.assertEqual(current.read_bytes(), before)
        self.assertEqual(gate.stat().st_ino, inode)
        self.assertTrue(unrelated.exists())

    def test_role_cleanup_refuses_unsafe_trees_without_partial_deletion(self):
        self.read()
        self.read("reviewer")
        removed, current = self.state_path("reviewer"), self.state_path()
        before = current.read_bytes()
        self.config = dataclasses.replace(self.config, roles={k: v for k, v in self.config.roles.items()
                                                             if k != "reviewer"})
        target = self.root / "outside-feed-state"
        target.write_bytes(b"untouched")
        unsafe = removed.parent / ("f" * 64 + ".json")
        for kind in ("symlink", "hardlink", "directory", "unexpected"):
            with self.subTest(kind=kind):
                path = unsafe if kind != "unexpected" else removed.parent / "unknown"
                if kind in ("symlink", "hardlink"):
                    try:
                        path.symlink_to(target) if kind == "symlink" else path.hardlink_to(target)
                    except (OSError, NotImplementedError):
                        self.skipTest("Link creation unavailable")
                elif kind == "directory":
                    path.mkdir()
                else:
                    path.write_bytes(b"unknown")
                try:
                    with self.assertRaises(Denied):
                        self.read()
                    self.assertTrue(removed.exists())
                    self.assertEqual(current.read_bytes(), before)
                    self.assertEqual(target.read_bytes(), b"untouched")
                finally:
                    path.rmdir() if kind == "directory" else path.unlink()
        removed.unlink()
        removed.parent.rmdir()
        try:
            removed.parent.symlink_to(target.parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")
        with self.assertRaises(Denied):
            self.read()
        self.assertEqual(target.read_bytes(), b"untouched")

    def test_role_cleanup_waits_for_live_cursor_transaction(self):
        self.read("reviewer")
        removed = self.state_path("reviewer")
        writing, requested, release = Event(), Event(), Event()
        original_write, original_lock = feed_updates.safe_write, feed_updates.lock
        ctx = self.context("reviewer")
        ctx._bounded_web_operation = self.observe
        self.config = dataclasses.replace(self.config, roles={k: v for k, v in self.config.roles.items()
                                                             if k != "reviewer"})
        def write(root, name, data):
            if root == removed.parent:
                writing.set()
                self.assertTrue(release.wait(5))
            return original_write(root, name, data)
        def gate(path):
            if writing.is_set():
                requested.set()
            return original_lock(path)
        with patch("mizu.feed_updates.safe_write", side_effect=write), \
                patch("mizu.feed_updates.lock", side_effect=gate), ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(ctx._op_feed_updates, {})
            try:
                self.assertTrue(writing.wait(5))
                b = pool.submit(self.read)
                self.assertTrue(requested.wait(5))
                self.assertTrue(removed.exists())
                self.assertFalse(b.done())
            finally:
                release.set()
            self.assertTrue(a.result(10)["complete"])
            self.assertTrue(b.result(10)["complete"])
        self.assertFalse(removed.parent.exists())
        self.assertTrue(self.state_path().exists())

    def test_out_of_order_observations_compare_latest_cursor(self):
        entered, release = Event(), Event()
        ctx = self.context("searcher")
        def delayed(*args):
            observation = self.observe(*args)
            entered.set()
            self.assertTrue(release.wait(5))
            return observation
        ctx._bounded_web_operation = delayed
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(ctx._op_feed_updates, {})
            try:
                self.assertTrue(entered.wait(5))
                self.xml[FEED] = rss(item("a"), item("b"))
                newer = self.read()["feeds"][0]
                self.assertEqual(len(newer["entries"]), 2)
            finally:
                release.set()
            older = pending.result(10)["feeds"][0]
        self.assertEqual(older["entries"], [])  # compared after the newer commit
        self.assertTrue(older["cursor_advanced"])
        repeated = self.read()["feeds"][0]["entries"]
        self.assertEqual([row["identity"] for row in repeated], [newer["entries"][1]["identity"]])

    def test_project_scope_and_metadata_only_truncation(self):
        self.read()
        ctx = SimpleNamespace(config=self.config, role=self.config.roles["searcher"],
                              project=SimpleNamespace(root=self.root / "other-project"),
                              run_dir=self.root / "other-run", tools_cancelled=lambda: False,
                              _bounded_web_operation=self.observe)
        self.assertEqual(len(feed_updates.read(ctx)["feeds"][0]["entries"]), 1)
        feeds = [f"https://example.org/feed/{n}" for n in range(35)]
        self.xml.update({feed: rss(item("a")) for feed in feeds})
        self.config = dataclasses.replace(self.config, web={**self.config.web, "feeds": feeds},
                                         limits=dataclasses.replace(self.config.limits, tool_response_bytes=4096))
        ctx = self.context("searcher")
        ctx.role = dataclasses.replace(ctx.role, capabilities=ctx.role.capabilities + ("feed_updates",))
        with (patch.object(ctx, "_bounded_web_operation") as observe,
              patch.object(feed_updates, "safe_write") as write_cursor):
            for _ in range(2):
                page = ctx.handle("feed_updates", {})
                self.assertTrue(page["truncated"])
                self.assertIn("continue", page)
                text = ""
                while True:
                    self.assertLessEqual(len(canonical(page)), 4096)
                    text += page["page"]
                    if page["next_offset"] is None:
                        break
                    continuation = page["continue"]
                    self.assertEqual(continuation["tool"], "mizu_result_read")
                    page = ctx.handle("result_read", {"cursor": continuation["cursor"],
                                                      "offset": continuation["offset"]})
                result = json.loads(text)
                self.assertTrue(result["truncated"])
                self.assertFalse(result["complete"])
                self.assertEqual([row["feed"] for row in result["feeds"]], feeds)
                for status in result["feeds"]:
                    self.assertFalse(status["complete"])
                    self.assertFalse(status["cursor_advanced"])
                    self.assertEqual(status["entries"], [])
                    self.assertFalse(self.state_path(feed=status["feed"]).exists())
            observe.assert_not_called()
            write_cursor.assert_not_called()

    def test_truncated_feed_does_not_block_small_healthy_feed(self):
        self.config = dataclasses.replace(self.config, web={**self.config.web, "feeds": [FEED, OTHER]},
                                         limits=dataclasses.replace(self.config.limits, tool_response_bytes=4096))
        self.xml[OTHER] = rss(*(item(str(n), "t" * 300, "s" * 1500) for n in range(3)))
        result = self.read()
        self.assertTrue(result["feeds"][0]["complete"])
        self.assertFalse(result["feeds"][1]["complete"])
        self.assertTrue(result["feeds"][1]["truncated"])
        self.assertTrue(self.state_path().exists())
        self.assertFalse(self.state_path(feed=OTHER).exists())
        self.assertLessEqual(len(canonical(result)), 4096)

    def test_many_large_workers_are_released_before_next_observation(self):
        # Weak references guard lifecycle, not a platform-dependent RSS estimate:
        # neither the worker envelope nor an omitted 1.9 MB row may survive into
        # the next network call. Only bounded returned rows/statuses can survive.
        class Tracked(dict):
            pass
        feeds = [f"https://example.org/feed/{n}" for n in range(64)]
        self.config = dataclasses.replace(self.config, web={**self.config.web, "feeds": feeds})
        ctx = self.context("searcher")
        previous, observed = [], []
        def operation(config, run_dir, operation, feed, cancel, role):
            self.assertTrue(all(ref() is None for ref in previous))
            if observed:
                self.assertTrue(self.state_path(feed=feeds[0]).exists())  # committed before next fetch
            small = feed in (feeds[0], feeds[-1])
            row = Tracked(identity=digest(feed.encode()), title="small" if small else "x" * 1900000,
                          summary="", url="", source_receipt="a" * 64)
            result = Tracked(entries=[row], errors=[], overflow=False, complete=True)
            previous[:] = [weakref.ref(result)] + ([] if small else [weakref.ref(row)])
            observed.append(feed)
            return result
        with patch.object(ctx, "_bounded_web_operation", side_effect=operation):
            result = ctx._op_feed_updates({})
        self.assertEqual(observed, feeds)
        self.assertTrue(all(ref() is None for ref in previous))
        self.assertLessEqual(len(canonical(result)), self.config.limits.tool_response_bytes)
        self.assertTrue(result["truncated"])
        for status in result["feeds"]:
            small = status["feed"] in (feeds[0], feeds[-1])
            self.assertEqual(status["cursor_advanced"], small)
            self.assertEqual(status["complete"], small)
            self.assertEqual(len(status["entries"]), int(small))
            self.assertEqual(self.state_path(feed=status["feed"]).exists(), small)

    def test_reserved_metadata_handles_late_failures_at_response_bounds(self):
        feeds = [f"https://example.org/feed/{n}" for n in range(12)]
        for bound in (4096, 4097, 8192):
            with self.subTest(bound=bound):
                self.state_path(feed=feeds[0]).unlink(missing_ok=True)
                self.config = dataclasses.replace(self.config, web={**self.config.web, "feeds": feeds},
                                                 limits=dataclasses.replace(self.config.limits,
                                                                            tool_response_bytes=bound))
                ctx = self.context("searcher")
                def operation(config, run_dir, operation, feed, cancel, role):
                    n = feeds.index(feed)
                    if n == 1:
                        raise Denied('\x00😀"\\' * 512)  # Escaped diagnostics consume bytes, not characters.
                    if n == 2:
                        return {"entries": [], "errors": ["ambiguous_identity", "duplicate_identity",
                                                          "missing_identity"], "overflow": False, "complete": False}
                    return {"entries": [] if n > 2 else [{"identity": digest(feed.encode()), "title": "t" * 200,
                                         "summary": "", "url": "", "source_receipt": "a" * 64}],
                            "errors": [], "overflow": False, "complete": True}
                original = feed_updates.safe_write
                def write(root, name, data):
                    if name != digest(feeds[0].encode()) + ".json":
                        raise OSError("disk full")
                    return original(root, name, data)
                with patch.object(ctx, "_bounded_web_operation", side_effect=operation), \
                        patch("mizu.feed_updates.safe_write", side_effect=write):
                    result = ctx._op_feed_updates({})
                self.assertLessEqual(len(canonical(result)), bound)
                self.assertFalse(result["complete"])
                self.assertEqual(len(result["feeds"]), len(feeds))
                self.assertTrue(result["feeds"][0]["cursor_advanced"])
                self.assertEqual(result["feeds"][1]["errors"], ["observation_failed"])
                self.assertEqual(len(result["feeds"][2]["errors"]), 3)
                self.assertTrue(any(row["errors"] == ["state_write_failed"] for row in result["feeds"]))
                self.assertTrue(all(not row["cursor_advanced"] for row in result["feeds"][1:]))

    def test_maximum_identity_set_fits_state_read_bound(self):
        self.config = dataclasses.replace(self.config, web={**self.config.web, "search_feed_item_limit": 1000},
                                         limits=dataclasses.replace(self.config.limits, tool_response_bytes=1048576))
        self.xml[FEED] = rss(*(item(str(n)) for n in range(1000)))
        self.assertTrue(self.read()["complete"])
        self.assertLessEqual(self.state_path().stat().st_size, feed_updates._STATE_BYTES)
        self.assertEqual(self.read()["feeds"][0]["entries"], [])

    def test_real_worker_output_overflow_does_not_consume_cursor(self):
        self.config = dataclasses.replace(self.config, web={**self.config.web, "search_feed_item_limit": 1000,
                                                          "max_bytes": 4 * 1024 * 1024})
        xml = rss(*(item(str(n), summary="s" * 1500).replace(
            f"https://example.org/{n}", "https://example.org/" + "x" * 3900) for n in range(400)))
        self.seed_cache(xml)
        ctx = self.context("searcher")
        status = ctx.dispatch("feed_updates", {})["feeds"][0]
        self.assertEqual(status["errors"], ["observation_failed"])
        self.assertFalse(status["complete"])
        self.assertFalse(self.state_path().exists())

    def seed_cache(self, xml):
        raw = xml.encode()
        receipt = {"id": digest(raw), "url": FEED, "final_url": FEED, "retrieved_at": now(), "retrieved_epoch": time.time(),
                   "sha256": digest(raw), "content_type": "application/rss+xml", "text": xml,
                   "trust": "external-untrusted", "fetch_policy_digest": digest(canonical({
                       "hosts": sorted(self.config.web["hosts"]), "intranet": self.config.web["intranet"],
                       "max_bytes": self.config.web["max_bytes"], "receipt_schema": FETCH_CACHE_SCHEMA_VERSION}))}
        write_json(self.config.data / "web-cache" / (digest(FEED.encode()) + ".json"), receipt)

    def test_real_worker_cached_feed_then_parse_failure_and_entity_refusal(self):
        self.seed_cache(self.xml[FEED])
        ctx = self.context("searcher")
        result = ctx.dispatch("feed_updates", {})
        self.assertTrue(result["complete"])
        receipt = result["feeds"][0]["entries"][0]["source_receipt"]
        self.assertTrue((ctx.run_dir / "sources" / receipt / "receipt.json").exists())
        self.assertEqual(ctx.dispatch("feed_updates", {})["feeds"][0]["entries"], [])
        before = self.state_path().read_bytes()
        for xml in ("<rss><channel>", "<not-a-feed/>", "<rss/>",
                    '<!DOCTYPE rss [<!ENTITY e "x">]><rss><channel/></rss>'):
            self.seed_cache(xml)
            status = ctx.dispatch("feed_updates", {})["feeds"][0]
            self.assertEqual(status["errors"], ["observation_failed"])
            self.assertFalse(status["complete"])
            self.assertEqual(self.state_path().read_bytes(), before)
