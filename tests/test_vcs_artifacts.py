"""Bounded CI artifact reads and UTF-8 byte pages. Fake adapters, offline only."""
import dataclasses
import hashlib
import sys
import unittest
from unittest.mock import patch

from support import Fixture
from mizu import vcs
from mizu.errors import Denied

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
        for content in ("", '"measured"\n', "é😀", "😀" * vcs.MAX_ARTIFACT_CONTENT):
            with self.subTest(chars=len(content)):
                out = self.parse(artifact(content))
                self.assertEqual(out["content"], content)
                self.assertEqual(out["digest"], sha256(content))
                self.assertEqual(out["size"], len(content.encode("utf-8")))
                self.assertEqual(out["offset"], 0)
                self.assertIsNone(out["next_offset"])
        self.assertEqual(vcs.MAX_ARTIFACT_CONTENT, 524288)
        self.assertEqual(vcs.MAX_ARTIFACT_BYTES, 4 * vcs.MAX_ARTIFACT_CONTENT)
        with self.assertRaises(Denied):
            self.parse(artifact("x" * (vcs.MAX_ARTIFACT_CONTENT + 1)))

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
        page = artifact("x" * vcs.MAX_ARTIFACT_PAGE_BYTES)
        self.parse(page, offset=0, limit=vcs.MAX_ARTIFACT_PAGE_BYTES)
        eof = artifact("", whole="😀" * vcs.MAX_ARTIFACT_CONTENT,
                       offset=vcs.MAX_ARTIFACT_BYTES)
        self.parse(eof, offset=vcs.MAX_ARTIFACT_BYTES, limit=1)
        with self.assertRaises(Denied):
            self.parse(artifact("é😀"), offset=0, limit=5)  # 2 chars, 6 bytes
        with self.assertRaises(Denied):
            self.parse(artifact("x" * (vcs.MAX_ARTIFACT_PAGE_BYTES + 1)),
                       offset=0, limit=vcs.MAX_ARTIFACT_PAGE_BYTES)

    def test_exact_response_shape_name_text_and_digests(self):
        good = artifact("é😀")
        bad = [None, [], {k: v for k, v in good.items() if k != "size"},
               {**good, "extra": 0}, {k: good[k] for k in ("artifact", "digest", "content")}]
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
                "limit": [None, False, True, 1.0, "1", 0, -1, vcs.MAX_ARTIFACT_PAGE_BYTES + 1],
        }.items():
            bad.extend({"offset": 0, "limit": 8, key: value} for value in values)
        with patch.object(vcs, "invoke") as invoke:
            for params in bad:
                with self.subTest(params=params), self.assertRaises(Denied):
                    vcs.read_via({}, "artifact", {"branch": "mizu", "artifact": NAME, **params})
            invoke.assert_not_called()


class ArtifactTests(Fixture):
    def test_artifacts_list_normalized(self):
        payload = {"artifacts": [
            {"artifact": NAME, "size": 12}, {"artifact": "profiles/callgrind.out"},
        ]}
        out = vcs.read_via(adapter_settings(serving_code(payload)), "artifacts", {"branch": "mizu"})
        self.assertEqual(out["branch"], "mizu")
        self.assertEqual([r["artifact"] for r in out["artifacts"]],
                         [NAME, "profiles/callgrind.out"])
        self.assertEqual(out["trust"], "external-untrusted")

    def test_artifact_full_read_request_compatibility_and_metadata(self):
        payload = artifact('{"bench": "é😀"}\n')
        params = {"branch": "mizu", "artifact": NAME, "sha": SHA_A}
        settings = adapter_settings(serving_code(payload, {"op": "artifact", **params}))
        out = vcs.read_via(settings, "artifact", params)
        self.assertEqual(out, {"op": "artifact", "branch": "mizu", "sha": SHA_A,
                               **payload, "trust": "external-untrusted"})

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
        from mizu.runtime import Context
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
        for change in ({"offset": None}, {"limit": 8193}, {"offset": True}):
            with self.subTest(change=change), self.assertRaises(Denied):
                ctx.handle("vcs_read", {**args, **change})
        for key in ("offset", "limit"):
            with self.subTest(missing=key), self.assertRaises(Denied):
                ctx.handle("vcs_read", {k: v for k, v in args.items() if k != key})
        # Shared schema widening must not relax other operations' bounds.
        for other in ({"op": "status", "branch": "mizu", "offset": 0, "limit": 1},
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
