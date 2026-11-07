"""Bounded CI artifact reads and UTF-8 byte pages. Fake adapters, offline only."""
import dataclasses
import hashlib
import sys
import unittest
from unittest.mock import patch

from support import Fixture
from mizu import vcs
from mizu.config import DEFAULT_ARTIFACT_FULL_READ_CHARS, artifact_page_bytes, load
from mizu.errors import ConfigError, Denied

SHA_A = "a" * 40
NAME = "target/perf/audit.jsonl"


def sha256(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def artifact(content, *, whole=None, offset=0):
    whole = content if whole is None else whole
    size = len(whole.encode("utf-8"))
    end = offset + len(content.encode("utf-8"))
    return {"artifact": NAME, "digest": sha256(whole), "content": content,
            "size": size, "offset": offset, "next_offset": end if end < size else None,
            "page_digest": sha256(content)}


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


def serving_code(payload, expected=None):
    return ("import sys,json; req=json.load(sys.stdin); "
            + ("assert req == %r; " % expected if expected is not None else "")
            + "print(json.dumps(%r))" % payload)


# This synthetic adapter tests the read contract, not any operator/provider
# implementation. It refuses a start inside a codepoint or a limit too small
# for the next codepoint, rather than returning an empty no-progress page.
PAGING_ADAPTER = '''
import hashlib, json, sys
req = json.load(sys.stdin)
raw = ("aé😀z" * 2000).encode("utf-8")
offset, limit = req["offset"], req["limit"]
assert 0 <= offset <= len(raw)
raw[:offset].decode("utf-8")
end = min(offset + limit, len(raw))
while True:
    try:
        content = raw[offset:end].decode("utf-8")
        break
    except UnicodeDecodeError:
        end -= 1
assert content or offset == len(raw)
print(json.dumps({"artifact": req["artifact"], "content": content,
    "digest": hashlib.sha256(raw).hexdigest(), "size": len(raw),
    "offset": offset, "next_offset": end if end < len(raw) else None,
    "page_digest": hashlib.sha256(raw[offset:end]).hexdigest()}))
'''


class ArtifactValidationTests(unittest.TestCase):
    def parse(self, payload, **kwargs):
        return vcs.parse_artifact(payload, name=NAME, **kwargs)

    def test_whole_reads_preserve_character_cap_and_raw_utf8_hash(self):
        for content in ("", '"measured"\n', "é😀", "😀" * DEFAULT_ARTIFACT_FULL_READ_CHARS):
            with self.subTest(chars=len(content)):
                out = self.parse(artifact(content))
                self.assertEqual(out["content"], content)
                self.assertEqual(out["digest"], sha256(content))
                self.assertEqual(out["size"], len(content.encode("utf-8")))
                self.assertEqual(out["offset"], 0)
                self.assertIsNone(out["next_offset"])
        self.assertEqual(DEFAULT_ARTIFACT_FULL_READ_CHARS, 524288)
        self.assertEqual(vcs.MAX_ARTIFACT_BYTES, 2**63 - 1)
        with self.assertRaises(Denied):
            self.parse(artifact("x" * (DEFAULT_ARTIFACT_FULL_READ_CHARS + 1)))

    def test_legacy_whole_file_read_is_normalized_and_locally_verified(self):
        content = "é😀\n"
        payload = {"artifact": NAME, "digest": sha256(content), "content": content}
        out = self.parse(payload)
        self.assertEqual(out["artifact"], NAME)
        self.assertEqual(out["digest"], sha256(content))
        self.assertEqual(out["content"], content)
        self.assertEqual(out["size"], len(content.encode("utf-8")))
        self.assertEqual(out["offset"], 0)
        self.assertIsNone(out["next_offset"])
        self.assertEqual(out["page_digest"], sha256(content))
        with self.assertRaises(Denied):
            self.parse({**payload, "digest": "0" * 64})
        with self.assertRaises(Denied):
            self.parse({**payload, "artifact": "other.jsonl"})
        # Paging is a new contract; the legacy shape cannot satisfy a page.
        with self.assertRaises(Denied):
            self.parse(payload, offset=0, limit=8)

    def test_paged_complete_and_empty_eof_reads(self):
        for content in ("", "é😀"):
            with self.subTest(content=content):
                self.assertEqual(self.parse(artifact(content), offset=0, limit=8),
                                 artifact(content))
        eof = artifact("", whole="é😀", offset=6)
        self.assertEqual(self.parse(eof, offset=6, limit=1), eof)
        # A last page is not complete content unless it also starts at zero.
        tail = artifact("😀", whole="é😀", offset=2)
        self.assertEqual(self.parse(tail, offset=2, limit=4), tail)

    def test_partial_whole_digest_is_not_claimed_as_locally_recomputed(self):
        payload = artifact("é", whole="é😀")
        payload["digest"] = "f" * 64
        out = self.parse(payload, offset=0, limit=2)
        self.assertEqual(out["digest"], "f" * 64)
        self.assertNotEqual(out["digest"], out["page_digest"])

    def test_maximum_request_and_page_bounds(self):
        page = artifact("x" * artifact_page_bytes({}))
        self.parse(page, offset=0, limit=artifact_page_bytes({}))
        eof = {**artifact(""), "size": vcs.MAX_ARTIFACT_BYTES,
               "offset": vcs.MAX_ARTIFACT_BYTES}
        self.parse(eof, offset=vcs.MAX_ARTIFACT_BYTES, limit=1)
        with self.assertRaises(Denied):
            self.parse(artifact("é😀"), offset=0, limit=5)  # 2 chars, 6 bytes
        with self.assertRaises(Denied):
            self.parse(artifact("x" * (artifact_page_bytes({}) + 1)),
                       offset=0, limit=artifact_page_bytes({}))

    def test_shape_denial_names_only_bounded_fields(self):
        good = artifact("x")
        cases = [
            ({k: v for k, v in good.items() if k != "size"}, "missing size"),
            ({**good, "extra": "secret"}, "unexpected extra"),
            ({**good, "x" * 100: "secret"}, "unexpected field(s)"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(Denied) as caught:
                self.parse(payload)
            message = str(caught.exception)
            self.assertIn(expected, message)
            self.assertNotIn("secret", message)
        capped = {**good, **{f"x{index}": 0 for index in range(10)}}
        with self.assertRaises(Denied) as caught:
            self.parse(capped)
        self.assertIn("(+2 more)", str(caught.exception))
        with self.assertRaises(Denied) as caught:
            self.parse({**good, 1: "secret"})
        message = str(caught.exception)
        self.assertIn("unexpected field(s)", message)
        self.assertNotIn("secret", message)
        with self.assertRaises(Denied) as caught:
            self.parse({"artifact": NAME, "digest": 1, "content": 1})
        self.assertIn("content, digest", str(caught.exception))

    def test_exact_response_shape_name_text_and_digests(self):
        good = artifact("é😀")
        bad = [None, [], {k: v for k, v in good.items() if k != "size"},
               {**good, "extra": 0}]
        for key, values in {
                "artifact": ["other.jsonl", None], "content": [None, 1, "\ud800", "\udfff"],
                "digest": [None, 0, "F" * 64, "f" * 63, "0" * 64,
                           vcs.acquire_digest(good["content"])],
                "page_digest": [None, False, "F" * 64, "f" * 65, "0" * 64],
        }.items():
            bad.extend({**good, key: value} for value in values)
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(Denied):
                self.parse(payload)
        # Every partial response also needs a syntactically valid whole hash
        # and a matching, locally recomputed page hash.
        partial = artifact("é", whole="é😀")
        for change in ({"digest": "not a digest"}, {"page_digest": "0" * 64}):
            with self.subTest(change=change), self.assertRaises(Denied):
                self.parse({**partial, **change}, offset=0, limit=2)
        # Complete paged reads must verify the whole hash, too.
        with self.assertRaises(Denied):
            self.parse({**good, "digest": "0" * 64}, offset=0, limit=6)

    def test_range_echo_progress_size_and_eof_consistency(self):
        partial = artifact("é", whole="é😀")
        changes = [
            {"offset": 1}, {"offset": False}, {"offset": 0.0}, {"offset": -1},
            {"size": True}, {"size": 6.0}, {"size": "6"}, {"size": -1},
            {"size": vcs.MAX_ARTIFACT_BYTES + 1}, {"size": 1},
            {"next_offset": None}, {"next_offset": True}, {"next_offset": 2.0},
            {"next_offset": "2"}, {"next_offset": 0}, {"next_offset": 1},
            {"next_offset": 3}, {"next_offset": 7},
            {"content": "", "page_digest": sha256(""), "next_offset": 0},
            {"content": "", "page_digest": sha256(""), "next_offset": None},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(Denied):
                self.parse({**partial, **change}, offset=0, limit=2)
        # Null exactly at EOF, even for an empty file.
        for complete in (artifact("é"), artifact("")):
            for next_offset in (complete["size"], False, 1):
                with self.subTest(complete=complete, next_offset=next_offset):
                    with self.assertRaises(Denied):
                        self.parse({**complete, "next_offset": next_offset}, offset=0, limit=2)
        with self.assertRaises(Denied):
            self.parse(artifact("", offset=1), offset=1, limit=1)  # beyond EOF
        with self.assertRaises(Denied):
            self.parse(partial)  # full-read mode cannot quietly return a page
        tail = artifact("😀", whole="é😀", offset=2)
        for change in ({"offset": 0}, {"size": 5}, {"next_offset": 6}):
            with self.subTest(change=change), self.assertRaises(Denied):
                self.parse({**tail, **change}, offset=2, limit=4)

    def test_pair_and_request_types_refused_before_dispatch(self):
        bad = [{"offset": 0}, {"limit": 1}]
        for key, values in {
                "offset": [None, False, True, 0.0, "0", -1, vcs.MAX_ARTIFACT_BYTES + 1],
                "limit": [None, False, True, 1.0, "1", 0, -1, artifact_page_bytes({}) + 1],
        }.items():
            bad.extend({"offset": 0, "limit": 8, key: value} for value in values)
        with patch.object(vcs, "invoke") as invoke:
            for params in bad:
                with self.subTest(params=params), self.assertRaises(Denied):
                    vcs.read_via({}, "artifact", {"branch": "mizu", "artifact": NAME, **params})
            invoke.assert_not_called()


class ArtifactConfigTests(Fixture):
    def test_artifact_policy_defaults_and_ranges(self):
        self.assertEqual(self.config.vcs["max_bytes"], 524288)
        self.assertEqual(self.config.vcs["artifact_full_read_chars"], 524288)
        self.assertEqual(self.config.vcs["artifact_page_bytes"], 65536)
        original = "\n".join(line for line in self.file.read_text().splitlines()
                             if not line.startswith("artifact_full_read_chars ="))
        self.file.write_text(original)
        self.assertEqual(load(self.file).vcs["artifact_full_read_chars"], 524288)

        def configured(**values):
            before, section = original.split("\n[vcs]\n", 1)
            section, after = section.split("\n[", 1)
            lines = [line for line in section.splitlines()
                     if line.split("=", 1)[0].strip() not in values]
            lines.extend(f"{key} = {value}" for key, value in values.items())
            self.file.write_text(before + "\n[vcs]\n" + "\n".join(lines) + "\n[" + after)

        for maximum in (1, 7, 800000, 16777216):
            with self.subTest(maximum=maximum):
                configured(max_bytes=maximum)
                self.assertEqual(load(self.file).vcs["artifact_page_bytes"],
                                 min(524288, max(1, maximum // 8)))
                page_max = max(1, maximum // 8)
                for value in (1, page_max, page_max + 1, 0, -1, 'true', '"1"', '1.5'):
                    with self.subTest(value=value):
                        configured(max_bytes=maximum, artifact_page_bytes=value)
                        if type(value) is int and 1 <= value <= page_max:
                            self.assertEqual(load(self.file).vcs["artifact_page_bytes"], value)
                        else:
                            with self.assertRaisesRegex(ConfigError, "vcs.artifact_page_bytes"):
                                load(self.file)
        for value in (1, 524289, 2**63 - 1, 2**63, 0, -1, 'true', '"1"', '1.5'):
            with self.subTest(full_chars=value):
                configured(artifact_full_read_chars=value)
                if type(value) is int and 1 <= value <= 2**63 - 1:
                    self.assertEqual(load(self.file).vcs["artifact_full_read_chars"], value)
                else:
                    with self.assertRaisesRegex(ConfigError, "vcs.artifact_full_read_chars"):
                        load(self.file)


class ArtifactTests(Fixture):
    def test_large_file_pages_through_eof_and_runtime_schema(self):
        from mizu.fs import mkdir
        from mizu.runtime import Context
        # Beyond the former aggregate cap, including requests starting past it.
        code = PAGING_ADAPTER.replace('"aé😀z" * 2000', '"aé😀z" * 300000')
        settings = adapter_settings(code)
        config = dataclasses.replace(self.config, vcs=settings)
        role = dataclasses.replace(config.roles["worker"], capabilities=("vcs_read",))
        run = self.project.root / "runs" / "large-artifact-test"
        mkdir(run)
        ctx = Context(config, self.project, role, run,
                      self.project.snapshots.get(), self.project.workspace)
        raw = ("aé😀z" * 300000).encode("utf-8")
        offset = 2097160
        chunks = []
        while True:
            out = ctx.handle("vcs_read", {"op": "artifact", "branch": "mizu",
                             "artifact": NAME, "sha": SHA_A,
                             "offset": offset, "limit": 65536})["result"]
            self.assertEqual(out["size"], len(raw))
            self.assertEqual(out["digest"], hashlib.sha256(raw).hexdigest())
            chunks.append(out["content"].encode("utf-8"))
            if out["next_offset"] is None:
                break
            offset = out["next_offset"]
        self.assertEqual(b"".join(chunks), raw[2097160:])
        eof = vcs.read_via(settings, "artifact", {"branch": "mizu", "artifact": NAME,
                          "offset": len(raw), "limit": 1})
        self.assertIsNone(eof["next_offset"])
        self.assertEqual(eof["content"], "")

    def test_configured_page_full_read_and_adapter_response_bounds(self):
        params = {"branch": "mizu", "artifact": NAME}
        for content in ("é😀", "x" * 524289):
            payload = artifact(content)
            # Isolate the character policy from the invocation byte cap,
            # which is tested independently below.
            with patch.object(vcs, "invoke", return_value=payload):
                settings = {"artifact_full_read_chars": len(content)}
                self.assertEqual(vcs.read_via(settings, "artifact", params)["content"], content)
                with self.assertRaises(Denied):
                    vcs.read_via({"artifact_full_read_chars": len(content) - 1}, "artifact", params)
                # A whole-read cap never narrows an explicitly paged read.
                self.assertEqual(vcs.read_via({"artifact_full_read_chars": 1,
                                 "max_bytes": 8 * len(content.encode("utf-8")),
                                 "artifact_page_bytes": len(content.encode("utf-8"))},
                                 "artifact", {**params, "offset": 0,
                                 "limit": len(content.encode("utf-8"))})["content"], content)
        settings = adapter_settings(serving_code(artifact("é😀")))
        settings["artifact_page_bytes"] = 6
        self.assertEqual(vcs.read_via(settings, "artifact", {**params, "offset": 0,
                         "limit": 6})["content"], "é😀")
        with patch.object(vcs, "invoke") as invoke, self.assertRaises(Denied):
            vcs.read_via(settings, "artifact", {**params, "offset": 0, "limit": 7})
        invoke.assert_not_called()
        settings = adapter_settings(serving_code(artifact("x" * 1000)), maximum=512)
        settings["artifact_full_read_chars"] = 2000
        with self.assertRaises(Denied):
            vcs.read_via(settings, "artifact", params)

    def test_artifacts_exact_pages_and_eof(self):
        # Opaque cursor characters are not decoded or rewritten by Mizu.
        cursor = "é /+=?" + "x" * 250
        self.assertEqual(len(cursor), 256)
        params = {"branch": "mizu", "sha": SHA_A, "limit": 1}
        for rows, next_cursor, request_cursor in (
                ([], cursor, None), ([{"artifact": NAME, "size": 12}], None, cursor)):
            request = {"op": "artifacts", **params}
            if request_cursor is not None:
                request["cursor"] = request_cursor
            payload = {"branch": "mizu", "sha": SHA_A,
                       "artifacts": rows, "next_cursor": next_cursor}
            out = vcs.read_via(adapter_settings(serving_code(payload, request)),
                               "artifacts", {k: v for k, v in request.items() if k != "op"})
            self.assertEqual(out, {"op": "artifacts", "branch": "mizu", "sha": SHA_A,
                                   "artifacts": [{**r, "digest": None} for r in rows],
                                   "next_cursor": next_cursor, "trust": "external-untrusted"})
        # Omission sends the existing per-page cap explicitly, without sha/cursor.
        rows = [{"artifact": NAME}, {"artifact": "profiles/callgrind.out",
                                    "digest": "b" * 64}]
        payload = {"branch": "mizu", "artifacts": rows, "next_cursor": None}
        out = vcs.read_via(adapter_settings(serving_code(payload,
                           {"op": "artifacts", "branch": "mizu", "limit": vcs.MAX_ARTIFACTS})),
                           "artifacts", {"branch": "mizu"})
        self.assertEqual(out["artifacts"], [
            {"artifact": NAME, "size": None, "digest": None},
            {"artifact": "profiles/callgrind.out", "size": None, "digest": "b" * 64}])
        self.assertNotIn("sha", out)
        self.assertIsNone(out["next_cursor"])
        narrowed = {"branch": "mizu", "sha": SHA_A,
                    "artifacts": [], "next_cursor": None}
        with self.assertRaises(Denied):
            vcs.read_via(adapter_settings(serving_code(narrowed)), "artifacts",
                         {"branch": "mizu"})

    def test_artifacts_stale_cursor_refusal_propagates(self):
        params = {"branch": "mizu", "sha": SHA_A, "limit": 1, "cursor": "stale"}
        code = ("import sys,json; assert json.load(sys.stdin) == %r; "
                "sys.exit('stale or changed inventory cursor')" % {"op": "artifacts", **params})
        with self.assertRaises(Denied):
            vcs.read_via(adapter_settings(code), "artifacts", params)

    def test_artifacts_bad_requests_never_invoke_adapter(self):
        changes = ([{"limit": v} for v in (None, True, 0, -1, 257, 1.5, "1")]
                   + [{"cursor": v} for v in (None, True, 1, "", "x" * 257, "a\n", "a\x1b")]
                   + [{"branch": ""}, {"sha": "not-a-sha"}, {"offset": 0},
                      {"id": "not-an-artifact-filter"}, {"scope": "head"}])
        with patch.object(vcs, "invoke") as invoke:
            for change in changes:
                with self.subTest(change=change), self.assertRaises(Denied):
                    vcs.read_via({}, "artifacts", {"branch": "mizu", **change})
            invoke.assert_not_called()

    def test_artifacts_malformed_pages_fail_closed(self):
        page = {"branch": "mizu", "sha": SHA_A, "artifacts": [], "next_cursor": None}
        bad = [None, [], {"artifacts": []}]
        bad += [{k: v for k, v in page.items() if k != missing}
                for missing in ("branch", "sha", "artifacts", "next_cursor")]
        bad += [{**page, **change} for change in (
            {"branch": "other"}, {"branch": None}, {"sha": "b" * 40}, {"sha": None},
            {"artifacts": None}, {"artifacts": [{"artifact": NAME}] * 2},
            {"artifacts": [{"artifact": NAME}] * (vcs.MAX_ARTIFACTS + 1)},
            {"artifacts": [{"artifact": "../escape"}]},
            {"artifacts": [{"artifact": NAME, "size": True}]},
            {"artifacts": [{"artifact": NAME, "digest": "invalid"}]},
            {"artifacts": [{"artifact": NAME, "unknown": 1}]})]
        bad += [{**page, "next_cursor": v} for v in
                (True, 1, [], {}, "", "x" * 257, "a\n", "a\x1b")]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(Denied):
                vcs.read_via(adapter_settings(serving_code(payload)), "artifacts",
                             {"branch": "mizu", "sha": SHA_A, "limit": 1})

    def test_artifact_full_read_request_compatibility_and_metadata(self):
        payload = artifact('{"bench": "é😀"}\n')
        params = {"branch": "mizu", "artifact": NAME, "sha": SHA_A}
        settings = adapter_settings(serving_code(payload, {"op": "artifact", **params}))
        out = vcs.read_via(settings, "artifact", params)
        self.assertEqual(out, {"op": "artifact", "branch": "mizu", "sha": SHA_A,
                               **payload, "trust": "external-untrusted"})

    def test_artifact_legacy_full_read_adapter_compatibility(self):
        content = '{"bench": "é😀"}\n'
        payload = {"artifact": NAME, "digest": sha256(content), "content": content}
        params = {"branch": "mizu", "artifact": NAME}
        settings = adapter_settings(serving_code(payload, {"op": "artifact", **params}))
        out = vcs.read_via(settings, "artifact", params)
        self.assertEqual(out, {"op": "artifact", "branch": "mizu",
                               "artifact": NAME, "digest": sha256(content),
                               "content": content,
                               "size": len(content.encode("utf-8")),
                               "offset": 0, "next_offset": None,
                               "page_digest": sha256(content),
                               "trust": "external-untrusted"})

    def test_utf8_chunking_reassembly_eof_and_adapter_boundary_rejection(self):
        settings = adapter_settings(PAGING_ADAPTER)
        params = {"branch": "mizu", "artifact": NAME, "sha": SHA_A}
        offset, chunks, digests, sizes = 0, [], set(), set()
        while True:
            out = vcs.read_via(settings, "artifact", {**params, "offset": offset, "limit": 8192})
            page = out["content"].encode("utf-8")
            self.assertEqual(out["offset"], offset)
            self.assertLessEqual(len(page), 8192)
            self.assertEqual(out["page_digest"], hashlib.sha256(page).hexdigest())
            self.assertEqual(out["sha"], SHA_A)
            self.assertEqual(out["trust"], "external-untrusted")
            chunks.append(page)
            sizes.add(out["size"])
            digests.add(out["digest"])
            if out["next_offset"] is None:
                break
            self.assertEqual(out["next_offset"], offset + len(page))
            self.assertGreater(out["next_offset"], offset)
            offset = out["next_offset"]
        self.assertGreater(len(chunks), 1)
        raw = b"".join(chunks)
        self.assertEqual(raw.decode("utf-8"), "aé😀z" * 2000)
        self.assertEqual(sizes, {len(raw)})
        self.assertEqual(digests, {hashlib.sha256(raw).hexdigest()})
        eof = vcs.read_via(settings, "artifact", {**params, "offset": len(raw), "limit": 1})
        self.assertEqual(eof["content"], "")
        self.assertIsNone(eof["next_offset"])
        # Adapter-side boundary/insufficient-limit failures propagate as Denied.
        for start, limit in ((2, 8), (4, 8), (1, 1), (len(raw) + 1, 8)):
            with self.subTest(start=start, limit=limit), self.assertRaises(Denied):
                vcs.read_via(settings, "artifact", {**params, "offset": start, "limit": limit})

    def test_tool_schema_and_runtime_forward_pages_without_relaxing_other_ops(self):
        from mizu.fs import mkdir
        from mizu.protocol import DEFINITIONS
        from mizu.runtime import Context
        schema = DEFINITIONS["vcs_read"][1]["properties"]
        self.assertEqual(schema["cursor"]["maxLength"], vcs.MAX_ARTIFACT_CURSOR)
        self.assertEqual(schema["offset"]["maximum"], 2**63 - 1)
        self.assertEqual(schema["limit"]["maximum"], 16777216 // 8)
        settings = adapter_settings(PAGING_ADAPTER)
        config = dataclasses.replace(self.config, vcs=settings)
        role = dataclasses.replace(config.roles["worker"],
                                   capabilities=("vcs_read",))
        run = self.project.root / "runs" / "artifact-test"
        mkdir(run)
        ctx = Context(config, self.project, role, run,
                      self.project.snapshots.get(), self.project.workspace)
        # Both values exceed the old shared CI-cost schema's 999/8 caps.
        args = {"op": "artifact", "branch": "mizu", "artifact": NAME,
                "offset": 8000, "limit": 8192}
        out = ctx.handle("vcs_read", args)["result"]
        self.assertEqual(out["offset"], 8000)
        self.assertEqual(len(out["content"].encode("utf-8")), 8000)
        for change in ({"offset": None}, {"limit": artifact_page_bytes(settings) + 1}, {"offset": True}):
            with self.subTest(change=change), self.assertRaises(Denied):
                ctx.handle("vcs_read", {**args, **change})
        for key in ("offset", "limit"):
            with self.subTest(missing=key), self.assertRaises(Denied):
                ctx.handle("vcs_read", {k: v for k, v in args.items() if k != key})
        page = {"branch": "mizu", "sha": SHA_A, "artifacts": [], "next_cursor": "next"}
        listing = {"op": "artifacts", "branch": "mizu", "sha": SHA_A,
                   "limit": 2, "cursor": "opaque"}
        # Test runtime -> read_via -> controlled adapter, not a host integration.
        with patch.object(ctx.project, "vcs_settings", return_value=adapter_settings(
                serving_code(page, listing))):
            out = ctx.handle("vcs_read", listing)["result"]
        self.assertEqual(out, {"op": "artifacts", **page, "trust": "external-untrusted"})
        with patch.object(vcs, "invoke") as invoke:
            for change in ({"offset": 0}, {"limit": 257}, {"cursor": ""}):
                with self.subTest(change=change), self.assertRaises(Denied):
                    ctx.handle("vcs_read", {**listing, **change})
            invoke.assert_not_called()
        # Shared schema widening must not relax other operations' bounds.
        for other in ({"op": "status", "branch": "mizu", "offset": 0, "limit": 1},
                      {"op": "log", "branch": "mizu", "limit": 1},
                      {"op": "proposals", "limit": 1},
                      {"op": "ci_costs", "offset": 1000, "limit": 1},
                      {"op": "ci_costs", "offset": 0, "limit": 9}):
            with self.subTest(other=other), self.assertRaises(Denied):
                ctx.handle("vcs_read", other)

    def test_traversal_and_bad_names_refused_before_dispatch(self):
        with patch.object(vcs, "invoke") as invoke:
            for bad in ("", "../escape", "/abs", "a\\b", "a/./b", "a//b", "x\x1b[2K"):
                with self.subTest(bad=bad), self.assertRaises(Denied):
                    vcs.read_via({}, "artifact", {"branch": "mizu", "artifact": bad})
            with self.assertRaises(Denied):
                vcs.read_via({}, "artifacts", {"branch": ""})
            invoke.assert_not_called()

    def test_bad_adapter_hash_rejected_through_read_via(self):
        for change in ({"digest": "0" * 64}, {"page_digest": "0" * 64}):
            payload = {**artifact("measured\n"), **change}
            with self.subTest(change=change), self.assertRaises(Denied):
                vcs.read_via(adapter_settings(serving_code(payload)), "artifact",
                             {"branch": "mizu", "artifact": NAME})


if __name__ == "__main__":
    raise SystemExit(unittest.main())
