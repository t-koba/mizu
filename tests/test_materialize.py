"""Offline-input materialization: generic host adapter, fail closed. Offline only."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import Fixture
from mizu import materialize
from mizu.config import load
from mizu.errors import Denied, ConfigError
from mizu.fs import canonical, digest


def settings(command, timeout=5, maximum=524288):
    return {"command": command, "timeout_seconds": timeout, "max_bytes": maximum}


def good_adapter_code(content="lockfile-content", cache="warm-cache-1", output="locks/app.lock"):
    # Adapter computes digest locally like a correct implementation would.
    # output=None omits the key to simulate a cache-only/older adapter.
    if output is None:
        return (
            "import sys,json,hashlib;"
            "json.load(sys.stdin);"
            f"content={content!r};"
            "from pathlib import Path;"
            "import sys; sys.path.insert(0, 'src');"
            "from mizu.fs import canonical, digest;"
            f"print(json.dumps({{'content': content, 'digest': digest(canonical(content)), 'cache': {cache!r}}}))"
        )
    return (
        "import sys,json,hashlib;"
        "json.load(sys.stdin);"
        f"content={content!r};"
        "from pathlib import Path;"
        "import sys; sys.path.insert(0, 'src');"
        "from mizu.fs import canonical, digest;"
        f"print(json.dumps({{'content': content, 'digest': digest(canonical(content)), 'cache': {cache!r}, 'output': {output!r}}}))"
    )


class MaterializeAdapterTests(Fixture):
    def test_success_returns_digest_bound_content(self):
        out = materialize.invoke(
            settings([sys.executable, "-c", good_adapter_code()]), "need cargo cache for manifests")
        self.assertEqual(out["content"], "lockfile-content")
        self.assertEqual(out["digest"], digest(canonical("lockfile-content")))
        self.assertEqual(out["cache"], "warm-cache-1")
        self.assertEqual(out["output"], "locks/app.lock")
        self.assertEqual(out["trust"], "external-untrusted")

    def test_invoke_preserves_adapter_output(self):
        out = materialize.invoke(
            settings([sys.executable, "-c", good_adapter_code(output="a/b.txt")]), "need deps")
        self.assertEqual(out["output"], "a/b.txt")

    def test_invoke_without_output_still_succeeds_but_apply_fails_closed(self):
        out = materialize.invoke(
            settings([sys.executable, "-c", good_adapter_code(output=None)]), "need cache only")
        self.assertEqual(out["output"], "")
        self.assertEqual(out["trust"], "external-untrusted")

    def test_invoke_invalid_output_refused(self):
        for bad in (123, ["a.txt"], {"p": 1}):
            stdout = json.dumps({"content": "c", "digest": digest(canonical("c")),
                                 "cache": "", "output": bad})
            result = SimpleNamespace(stdout=stdout, reason="exited", exit_code=0)
            with self.subTest(output=repr(bad)):
                with patch("mizu.materialize.run", return_value=result):
                    with self.assertRaises(Denied):
                        materialize.invoke(settings(["adapter"]), "deps")
        for bad in ("bad\x00path", "bad\npath", "x" * 4097):
            stdout = json.dumps({"content": "c", "digest": digest(canonical("c")),
                                 "cache": "", "output": bad})
            result = SimpleNamespace(stdout=stdout, reason="exited", exit_code=0)
            with self.subTest(output=repr(bad)):
                with patch("mizu.materialize.run", return_value=result):
                    with self.assertRaises(Denied):
                        materialize.invoke(settings(["adapter"]), "deps")

    def test_unconfigured_refused(self):
        with self.assertRaises(Denied):
            materialize.invoke(settings([]), "need something")

    def test_bad_spec_refused_without_spawn(self):
        with self.assertRaises(Denied):
            materialize.invoke(settings([sys.executable, "-c", "pass"]), "")
        with self.assertRaises(Denied):
            materialize.invoke(settings([sys.executable, "-c", "pass"]), "   ")
        with self.assertRaises(Denied):
            materialize.invoke(settings([sys.executable, "-c", "pass"]), "x" * 8193)

    def test_digest_mismatch_refused(self):
        code = "import sys,json; json.load(sys.stdin); print(json.dumps({'content': 'a', 'digest': 'b'*64}))"
        with self.assertRaises(Denied):
            materialize.invoke(settings([sys.executable, "-c", code]), "need deps")

    def test_nonzero_exit_refused(self):
        with self.assertRaises(Denied):
            materialize.invoke(settings([sys.executable, "-c", "import sys; sys.exit(3)"]), "need deps")

    def test_malformed_json_refused(self):
        code = "import sys; sys.stdout.write('not-json\\n')"
        with self.assertRaises(Denied):
            materialize.invoke(settings([sys.executable, "-c", code]), "need deps")

    def test_config_defaults_absent_section(self):
        # Existing configs without [materialize] keep loading, disabled.
        self.assertEqual(self.config.materialize.get("command"), [])
        self.assertEqual(self.config.materialize.get("timeout_seconds"), 20)
        self.assertEqual(self.config.materialize.get("diagnostic_bytes"), 2048)

    def test_config_unknown_key_refused(self):
        text = self.file.read_text()
        self.assertIn("[materialize]", text)
        self.file.write_text(text.replace("[materialize]", "[materialize]\nunknown_key = 1", 1))
        with self.assertRaisesRegex(ConfigError, "Unknown keys"):
            load(self.file)

    def test_runtime_requires_visible_workspace(self):
        ctx = self.context("worker")
        # Unconfigured adapter fails closed even with the grant path wired.
        from mizu.config import Role
        role = ctx.role
        # worker has write workspace; call without adapter configured
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"spec": "need test cache"})

    def test_consult_cannot_hold_materialize(self):
        from mizu.runtime import check_consult_role
        from mizu.config import Role
        role = Role("consult", "primary", (Path("x"),), "read", ("files", "read", "finish", "materialize"))
        with self.assertRaises(ConfigError):
            check_consult_role("consult", role)

    def test_config_output_allowed_is_not_a_mizu_key(self):
        # Output authorization lives in the host adapter (ops.toml), never here.
        self.assertNotIn("output_allowed", self.config.materialize)
        base = self.file.read_text()
        self.assertIn("[materialize]", base)
        self.file.write_text(base.replace("[materialize]", "[materialize]\noutput_allowed = []", 1))
        with self.assertRaisesRegex(ConfigError, "Unknown keys"):
            load(self.file)

    def test_invoke_invalid_unicode_and_byte_limit_fail_closed(self):
        adapter = settings(["adapter"], maximum=32)
        for content in ["\ud800", "é" * 17]:
            with self.subTest(content=repr(content)):
                dg = "a" * 64 if content == "\ud800" else digest(canonical(content))
                stdout = json.dumps({"content": content, "digest": dg})
                result = SimpleNamespace(stdout=stdout, reason="exited", exit_code=0)
                with patch("mizu.materialize.run", return_value=result):
                    with self.assertRaises(Denied):
                        materialize.invoke(adapter, "deps")
        with patch("mizu.materialize.run") as run:
            with self.assertRaises(Denied):
                materialize.invoke(adapter, "\ud800")
            run.assert_not_called()
        result = SimpleNamespace(stdout=b"\xff", reason="exited", exit_code=0)
        with patch("mizu.materialize.run", return_value=result):
            with self.assertRaises(Denied):
                materialize.invoke(adapter, "deps")

    def test_invoke_locator_fields_precede_large_result(self):
        import dataclasses
        from mizu.fs import read_json
        ctx = self.context("worker")
        ctx.role = dataclasses.replace(ctx.role, capabilities=(*ctx.role.capabilities, "materialize"))
        content = "x" * 10000
        data = {"content": content, "digest": digest(canonical(content)),
                "cache": "", "output": "prior.txt", "trust": "external-untrusted"}
        with patch("mizu.materialize.invoke", return_value=data):
            out = ctx.handle("materialize", {"spec": "deps"})
        keys = list(out)
        for key in ("run", "role", "kind", "evidence_id", "evidence"):
            self.assertLess(keys.index(key), keys.index("result"))
        self.assertEqual(out["run"], ctx.run_dir.name)
        self.assertEqual(out["role"], ctx.role.name)
        self.assertEqual(out["kind"], "materialize")
        self.assertEqual(read_json(ctx.run_dir / out["evidence"]), out)
        self.assertEqual(out["result"], data)


class MaterializeApplyTests(Fixture):
    def _grant(self, ctx):
        import dataclasses
        if "materialize" not in ctx.role.capabilities:
            ctx.role = dataclasses.replace(ctx.role, capabilities=tuple(ctx.role.capabilities) + ("materialize",))
        return ctx

    def _seed(self, ctx, spec_obj_or_str, content, *, old_style=False, evidence_id=None,
              run_id=None, role_name=None, cache="", kind_present=True,
              output="__mirror__", result_output_present=True):
        import uuid
        from mizu.fs import canonical, digest, write_json
        run = run_id or ctx.run_dir.name
        role = role_name or ctx.role.name
        eid = evidence_id or uuid.uuid4().hex
        spec = spec_obj_or_str if isinstance(spec_obj_or_str, str) else json.dumps(spec_obj_or_str)
        dg = digest(canonical(content))
        if output == "__mirror__":
            try:
                parsed = json.loads(spec) if isinstance(spec, str) else None
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("output"), str):
                output = parsed["output"]
            else:
                output = ""
        result = {"content": content, "digest": dg, "cache": cache,
                  "trust": "external-untrusted"}
        if result_output_present:
            result["output"] = output
        record = {"spec": spec, "digest": dg, "cache": cache,
                  "trust": "external-untrusted", "evidence_id": eid,
                  "evidence": f"materialize/{eid}.json",
                  "result": result}
        if not old_style:
            record["run"] = run
            record["role"] = role
            record["kind"] = "materialize"
        elif not kind_present:
            pass
        # Bind the source run to its own started metadata, including when it
        # predates the current writer invocation.
        target_run_dir = ctx.run_dir if run == ctx.run_dir.name else (ctx.project.root / "runs" / run)
        target_run_dir.mkdir(parents=True, exist_ok=True)
        write_json(target_run_dir / "started.json", {"run": run, "role": role})
        write_json(target_run_dir / "materialize" / f"{eid}.json", record)
        return run, eid, dg, spec

    def test_apply_success_exact_bytes_and_audit(self):
        ctx = self._grant(self.context("worker"))
        content = "line1\nline2 \u00e9\n"
        run, eid, dg, _ = self._seed(ctx, {"output": "locks/app.lock", "other": 1}, content)
        # Parent must already exist; no parent creation.
        (ctx.workspace / "locks").mkdir(parents=True, exist_ok=True)
        out = ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
        self.assertEqual(out["path"], "locks/app.lock")
        self.assertEqual(out["digest"], dg)
        self.assertEqual(out["trust"], "external-untrusted")
        self.assertEqual(out["source_run"], run)
        self.assertNotIn("content", out)
        raw = (ctx.workspace / "locks/app.lock").read_bytes()
        self.assertEqual(raw, content.encode("utf-8"))
        from mizu.fs import canonical, digest as _dg
        self.assertEqual(out["digest"], _dg(canonical(content)))
        self.assertEqual(out["bytes"], len(raw))
        # Audit receipt retained.
        from mizu.fs import read_json
        audit = read_json(ctx.run_dir / out["evidence"])
        self.assertEqual(audit["path"], "locks/app.lock")
        self.assertEqual(audit["digest"], dg)

    def test_apply_accepts_old_receipt_without_run_role(self):
        ctx = self._grant(self.context("worker"))
        run, eid, dg, _ = self._seed(ctx, {"output": "old.txt"}, "old-bytes", old_style=True)
        out = ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
        self.assertEqual(out["path"], "old.txt")
        self.assertEqual((ctx.workspace / "old.txt").read_bytes(), b"old-bytes")

    def test_apply_reads_retained_receipt_from_prior_same_role_run(self):
        import uuid
        ctx = self._grant(self.context("worker"))
        previous_run = uuid.uuid4().hex
        run, eid, _, _ = self._seed(
            ctx, {"output": "prior.txt"}, "from-prior-run", run_id=previous_run)
        self.assertNotEqual(run, ctx.run_dir.name)
        out = ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
        self.assertEqual(out["source_run"], previous_run)
        self.assertEqual((ctx.workspace / "prior.txt").read_bytes(), b"from-prior-run")

    def test_apply_missing_receipt_refused(self):
        import uuid
        ctx = self._grant(self.context("worker"))
        from mizu.fs import write_json
        write_json(ctx.run_dir / "started.json", {"run": ctx.run_dir.name, "role": ctx.role.name})
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": ctx.run_dir.name, "evidence_id": uuid.uuid4().hex}})

    def test_apply_wrong_run_or_role_refused(self):
        import uuid
        ctx = self._grant(self.context("worker"))
        run, eid, _, _ = self._seed(ctx, {"output": "a.txt"}, "x")
        other = uuid.uuid4().hex
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": other, "evidence_id": eid}})
        # Role mismatch: seed bound to another role.
        run2 = ctx.run_dir.name
        eid2 = uuid.uuid4().hex
        from mizu.fs import canonical, digest, write_json
        dg = digest(canonical("y"))
        rec = {"spec": json.dumps({"output": "a.txt"}), "digest": dg, "cache": "",
               "trust": "external-untrusted", "evidence_id": eid2,
               "evidence": f"materialize/{eid2}.json",
               "result": {"content": "y", "digest": dg, "output": "a.txt",
                          "trust": "external-untrusted"},
               "run": run2, "role": "searcher", "kind": "materialize"}
        write_json(ctx.run_dir / f"materialize/{eid2}.json", rec)
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": run2, "evidence_id": eid2}})

    def test_apply_corrupt_receipt_refused(self):
        import uuid
        ctx = self._grant(self.context("worker"))
        from mizu.fs import write_json
        run = ctx.run_dir.name
        write_json(ctx.run_dir / "started.json", {"run": run, "role": ctx.role.name})
        eid = uuid.uuid4().hex
        (ctx.run_dir / "materialize").mkdir(parents=True, exist_ok=True)
        (ctx.run_dir / "materialize" / f"{eid}.json").write_bytes(b"{not-json")
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})

    def test_apply_utf8_byte_limit_refused_and_leaves_file(self):
        ctx = self._grant(self.context("worker"))
        target = ctx.workspace / "keep.txt"
        target.write_bytes(b"original")
        ctx.config.materialize["max_bytes"] = 32
        run, eid, _, _ = self._seed(ctx, {"output": "keep.txt"}, "é" * 17)
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
        self.assertEqual(target.read_bytes(), b"original")
        self.assertFalse(list(ctx.workspace.glob(".new-*")))

    def test_apply_strict_json_and_unicode_refused_without_writes(self):
        ctx = self._grant(self.context("worker"))
        target = ctx.workspace / "keep.txt"
        target.write_bytes(b"original")
        # Valid bindings/content plus malformed extra fields must still fail.
        bad_fields = ['"extra":1,"extra":2', '"extra":NaN',
                      '"extra":Infinity', '"extra":-Infinity',
                      '"extra":1e9999', '"extra":-1e9999']
        for source in ("started", "receipt", "spec"):
            for fields in bad_fields:
                with self.subTest(source=source, fields=fields):
                    run, eid, _, _ = self._seed(ctx, {"output": "keep.txt"}, "new")
                    path = ctx.run_dir / ("started.json" if source == "started"
                                          else f"materialize/{eid}.json")
                    raw = path.read_text()
                    if source == "spec":
                        record = json.loads(raw)
                        record["spec"] = '{"output":"keep.txt",' + fields + '}'
                        raw = json.dumps(record)
                    else:
                        raw = raw.rstrip()[:-1] + ',' + fields + '}'
                    path.write_text(raw)
                    with self.assertRaises(Denied):
                        ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
                    self.assertEqual(target.read_bytes(), b"original")
        for source in ("started", "receipt", "content", "output", "spec"):
            with self.subTest(invalid_unicode=source):
                run, eid, _, _ = self._seed(ctx, {"output": "keep.txt"}, "new")
                path = ctx.run_dir / f"materialize/{eid}.json"
                if source == "started":
                    (ctx.run_dir / "started.json").write_bytes(b"\xff")
                elif source == "receipt":
                    path.write_bytes(b"\xff")
                else:
                    record = json.loads(path.read_text())
                    if source == "content":
                        record["result"]["content"] = "\ud800"
                    elif source == "output":
                        record["spec"] = json.dumps({"output": "\ud800"})
                    else:
                        record["spec"] = "\ud800"
                    path.write_text(json.dumps(record))
                with self.assertRaises(Denied):
                    ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
                self.assertEqual(target.read_bytes(), b"original")

    def test_apply_cache_controls_refused(self):
        ctx = self._grant(self.context("worker"))
        for cache in ("nul\x00", "new\nline"):
            with self.subTest(cache=cache):
                run, eid, _, _ = self._seed(ctx, {"output": "keep.txt"}, "new", cache=cache)
                with self.assertRaises(Denied):
                    ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
                self.assertFalse((ctx.workspace / "keep.txt").exists())

    def test_apply_file_bytes_requires_exact_positive_int(self):
        ctx = self._grant(self.context("worker"))
        run, eid, _, _ = self._seed(ctx, {"output": "keep.txt"}, "new")
        for bound in (True, False, 0, -1, 1024.0, "1024"):
            with self.subTest(bound=bound), patch("mizu.materialize.safe_read") as read:
                with self.assertRaises(Denied):
                    materialize.load_source(ctx.config.materialize, ctx.project.root,
                                            run, eid, ctx.role.name, bound)
                read.assert_not_called()

    def test_apply_digest_mismatch_refused_and_leaves_file(self):
        ctx = self._grant(self.context("worker"))
        (ctx.workspace / "keep.txt").write_bytes(b"original")
        run, eid, _, _ = self._seed(ctx, {"output": "keep.txt"}, "new-content")
        # Tamper retained content without updating digests.
        from mizu.fs import read_json, write_json
        rec = read_json(ctx.run_dir / f"materialize/{eid}.json")
        rec["result"]["content"] = "tampered"
        write_json(ctx.run_dir / f"materialize/{eid}.json", rec)
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
        self.assertEqual((ctx.workspace / "keep.txt").read_bytes(), b"original")

    def test_apply_malformed_and_invalid_output_refused(self):
        ctx = self._grant(self.context("worker"))
        (ctx.workspace / "allowed.txt").write_bytes(b"stay")
        cases = ["not-json", '[1,2]', json.dumps({"nope": 1}),
                 json.dumps({"output": 123}), json.dumps({"output": "/abs"}),
                 json.dumps({"output": "../escape"}), '{"a":1,"a":2}',
                 json.dumps({"output": "a\\b"})]
        for i, spec in enumerate(cases):
            run, eid, _, _ = self._seed(ctx, spec, "c%d" % i, output="allowed.txt")
            with self.assertRaises(Denied, msg=spec):
                ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
        self.assertEqual((ctx.workspace / "allowed.txt").read_bytes(), b"stay")
        self.assertFalse((ctx.workspace / "other.txt").exists())

    def test_apply_missing_adapter_output_refused_including_old_receipts(self):
        ctx = self._grant(self.context("worker"))
        # New receipt with no adapter output.
        run, eid, _, _ = self._seed(ctx, {"output": "a.txt"}, "x",
                                    result_output_present=False)
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
        self.assertFalse((ctx.workspace / "a.txt").exists())
        # Explicit empty adapter output.
        run, eid, _, _ = self._seed(ctx, {"output": "a.txt"}, "x", output="")
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
        # Legacy receipt predating result.output (no run/role/kind either).
        run, eid, _, _ = self._seed(ctx, {"output": "a.txt"}, "x", old_style=True,
                                    result_output_present=False)
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
        self.assertFalse((ctx.workspace / "a.txt").exists())

    def test_apply_output_mismatch_refused_and_leaves_file(self):
        ctx = self._grant(self.context("worker"))
        (ctx.workspace / "kept.txt").write_bytes(b"original")
        run, eid, _, _ = self._seed(ctx, {"output": "kept.txt"}, "new",
                                    output="other.txt")
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
        self.assertEqual((ctx.workspace / "kept.txt").read_bytes(), b"original")
        self.assertFalse((ctx.workspace / "other.txt").exists())

    def test_apply_invalid_adapter_output_refused(self):
        ctx = self._grant(self.context("worker"))
        for bad in ("/abs", "../escape", "a\\b", "x" * 4097):
            with self.subTest(output=repr(bad)):
                run, eid, _, _ = self._seed(ctx, {"output": bad}, "x", output=bad)
                with self.assertRaises(Denied):
                    ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})

    def test_apply_exclusive_shape(self):
        ctx = self._grant(self.context("worker"))
        import uuid
        run = ctx.run_dir.name
        eid = uuid.uuid4().hex
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"spec": "x", "apply": {"run": run, "evidence_id": eid}})
        with self.assertRaises(Denied):
            ctx.handle("materialize", {})

    def test_apply_read_only_role_refused(self):
        ctx = self.context("searcher")
        import dataclasses
        ctx.role = dataclasses.replace(ctx.role, capabilities=tuple(ctx.role.capabilities) + ("materialize",))
        import uuid
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": uuid.uuid4().hex, "evidence_id": uuid.uuid4().hex}})

    def test_apply_symlink_targets_and_parents_refused(self):
        import os
        ctx = self._grant(self.context("worker"))
        target = ctx.workspace / "real.txt"
        target.write_bytes(b"real")
        parent = ctx.workspace / "real-dir"
        parent.mkdir()
        (parent / "keep.txt").write_bytes(b"keep")
        try:
            os.symlink("real.txt", ctx.workspace / "link.txt")
            os.symlink("real-dir", ctx.workspace / "linked", target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("Platform cannot create symlinks")
        # Exercise native pinning where available and the portable fallback.
        from mizu import platform
        for openat in (platform.HAS_OPENAT, False):
            for output in ("link.txt", "linked/keep.txt"):
                with self.subTest(openat=openat, output=output):
                    run, eid, _, _ = self._seed(ctx, {"output": output}, "new")
                    with patch("mizu.fs._platform.HAS_OPENAT", openat):
                        with self.assertRaises(Denied):
                            ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
                    self.assertEqual(target.read_bytes(), b"real")
                    self.assertEqual((parent / "keep.txt").read_bytes(), b"keep")
                    self.assertFalse(list(ctx.workspace.rglob(".new-*")))

    def test_apply_hardlink_refused(self):
        import os
        ctx = self._grant(self.context("worker"))
        # Hardlink: existing destination with nlink > 1.
        hard = ctx.workspace / "hard.txt"
        hard.write_bytes(b"hard-original")
        if hasattr(os, "link"):
            sibling = ctx.workspace / "hard-sibling.txt"
            try:
                if sibling.exists():
                    sibling.unlink()
                os.link(hard, sibling)
            except OSError:
                pass
            if sibling.exists():
                run2, eid2, _, _ = self._seed(ctx, {"output": "hard.txt"}, "newer")
                with self.assertRaises(Denied):
                    ctx.handle("materialize", {"apply": {"run": run2, "evidence_id": eid2}})
                self.assertEqual(hard.read_bytes(), b"hard-original")

    def test_apply_no_parent_creation_and_excluded_refused(self):
        ctx = self._grant(self.context("worker"))
        run, eid, _, _ = self._seed(ctx, {"output": "missing/dir/file.txt"}, "x")
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": run, "evidence_id": eid}})
        self.assertFalse((ctx.workspace / "missing").exists())
        run2, eid2, _, _ = self._seed(ctx, {"output": ".git/kept.txt"}, "y")
        with self.assertRaises(Denied):
            ctx.handle("materialize", {"apply": {"run": run2, "evidence_id": eid2}})


if __name__ == "__main__":
    raise SystemExit(unittest.main())
