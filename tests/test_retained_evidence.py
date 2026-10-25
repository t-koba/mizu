"""Offline exact-locator reads; no providers, containers or external adapters."""
import dataclasses
import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu.config import CAPABILITIES, load
from mizu.errors import Denied, LimitExceeded
from mizu.fs import READ_PAGE_CHARS, canonical, digest, safe_read, write_json
from mizu.protocol import DEFINITIONS, tool_definitions, validate


OPS = ("command_evidence_read", "experiment_evidence_read")


class RetainedEvidenceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.ctx = self.context()
        self.ctx.role = dataclasses.replace(self.ctx.role, capabilities=self.ctx.role.capabilities + OPS)
        self.run = uuid.uuid4().hex
        self.evidence_id = uuid.uuid4().hex
        self.directory = self.project.root / "runs" / self.run
        self.started = self.directory / "started.json"
        write_json(self.started, {"run": self.run, "role": "worker", "private_metadata": "never returned"})
        self.command = {"id": self.evidence_id, "kind": "command", "role": "worker",
                        "script": "printf example", "stdout": "界🌱é\n" * 6000,
                        "source": "forged", "trust": "trusted"}
        self.record = self.directory / "commands" / f"{self.evidence_id}.json"
        write_json(self.record, self.command)
        self.args = {"run": self.run, "evidence_id": self.evidence_id}

    def read(self, **kwargs):
        return self.ctx.handle(OPS[0], {**self.args, **kwargs})

    def test_no_implicit_grant_or_visibility_and_independent_grants(self):
        self.assertTrue(set(OPS) <= CAPABILITIES)
        for role in self.config.roles.values():
            self.assertFalse(set(OPS) & set(role.capabilities))
            self.assertFalse({"mizu_" + op for op in OPS} &
                             {tool["name"] for tool in tool_definitions(role.capabilities)})
        for granted, denied in (OPS, OPS[::-1]):
            self.ctx.role = dataclasses.replace(self.ctx.role, capabilities=(granted, "finish"))
            tools = tool_definitions(self.ctx.role.capabilities)
            self.assertEqual({tool["operation"] for tool in tools}, {granted, "finish"})
            self.assertEqual(next(tool["inputSchema"] for tool in tools if tool["operation"] == granted),
                             DEFINITIONS[granted][1])
            with self.assertRaisesRegex(Denied, "no capability"):
                self.ctx.handle(denied, self.args)
        # The strict config loader accepts the new grants only when explicitly named.
        self.file.write_text(self.file.read_text().replace('capabilities = [',
                             'capabilities = ["command_evidence_read", "experiment_evidence_read", ', 1))
        configured = load(self.file)
        self.assertTrue(any(set(OPS) <= set(role.capabilities) for role in configured.roles.values()))

    def test_own_role_two_run_pagination_reconstructs_multibyte_record(self):
        self.assertNotEqual(self.ctx.run_dir.name, self.run)
        raw = self.record.read_bytes()
        first = self.read()
        self.assertEqual(len(first["text"]), READ_PAGE_CHARS)
        pieces = [first["text"]]
        current = first
        while current["next_offset"] is not None:
            current = self.read(offset=current["next_offset"], content_id=first["content_id"])
            self.assertLessEqual(len(current["text"]), READ_PAGE_CHARS)
            pieces.append(current["text"])
        self.assertEqual("".join(pieces), raw.decode("utf-8"))
        self.assertEqual(first["content_id"], digest(raw))
        self.assertEqual(first["source"], "retained-command-evidence")
        self.assertEqual(first["trust"], "recorded-local-untrusted")
        self.assertEqual(first["run"], self.run)
        self.assertEqual(first["evidence_id"], self.evidence_id)
        self.assertEqual(first["evidence"], f"commands/{self.evidence_id}.json")
        self.assertNotIn("private_metadata", "".join(pieces))
        self.assertFalse(current["truncated"])
        self.assertIsNone(current["next_offset"])
        self.assertEqual(self.read(offset=len(raw), content_id=first["content_id"])["text"], "")

    def test_current_run_evidence_is_readable(self):
        run = self.ctx.run_dir.name
        write_json(self.ctx.run_dir / "started.json", {"run": run, "role": "worker"})
        target = self.ctx.run_dir / "commands" / f"{self.evidence_id}.json"
        record = {**self.command, "stdout": "current-run result"}
        write_json(target, record)
        page = self.ctx.handle(OPS[0], {"run": run, "evidence_id": self.evidence_id})
        self.assertEqual(page["run"], run)
        self.assertEqual(page["evidence_id"], self.evidence_id)
        self.assertEqual(json.loads(page["text"]), record)

    def test_exec_and_experiment_locators_and_wrapper(self):
        producer = self.context()
        write_json(producer.run_dir / "started.json", {"run": producer.run_dir.name, "role": "worker"})
        for op in ("exec", "experiment"):
            evidence_id = uuid.uuid4().hex
            command = {"id": evidence_id, "role": "worker", "kind": "command" if op == "exec" else op,
                       "script": "printf sample", "stdout": "sample", "stderr": "", "exit_code": 0,
                       "reason": "exited", "seconds": 0.1, "writable": op == "exec"}
            def execute(*args, **kwargs):
                write_json(producer.run_dir / "commands" / f"{evidence_id}.json", command)
                return command
            with patch.object(producer.sandbox, "execute", side_effect=execute):
                result = producer.handle(op, {"script": command["script"], **(
                    {"question": "Does it print?", "comparison": "baseline", "measure": "stdout"}
                    if op == "experiment" else {})})
            kind = "commands" if op == "exec" else "experiments"
            self.assertEqual(result["run"], producer.run_dir.name)
            self.assertEqual(result["id"], evidence_id)
            self.assertEqual(result["evidence"], f"{kind}/{evidence_id}.json")
            read_op = OPS[0] if op == "exec" else OPS[1]
            page = self.ctx.handle(read_op, {"run": result["run"], "evidence_id": result["id"]})
            self.assertEqual(page["evidence"], result["evidence"])
            self.assertEqual(page["trust"], "recorded-local-untrusted")
            self.assertEqual(page["source"], "retained-" + command["kind"] + "-evidence")
            record = json.loads(page["text"])
            if op == "experiment":
                self.assertEqual(record, {"question": "Does it print?", "comparison": "baseline",
                                          "measure": "stdout", "command": command})
                # The producer's command copy is not readable through the command grant.
                with self.assertRaisesRegex(Denied, "kind"):
                    self.ctx.handle(OPS[0], {"run": result["run"], "evidence_id": result["id"]})
            else:
                self.assertEqual(record, command)

    def test_kind_mismatch_and_invalid_experiment_wrappers(self):
        experiment = self.directory / "experiments" / self.record.name
        for record in (self.command, {"command": self.command}, [], {"command": []},
                       {"command": {**self.command, "kind": "experiment", "id": "a" * 32}},
                       {"command": {**self.command, "kind": "experiment", "role": "reviewer"}}):
            with self.subTest(record=record):
                write_json(experiment, record)
                with self.assertRaises(Denied):
                    self.ctx.handle(OPS[1], self.args)
        write_json(self.record, {**self.command, "kind": "experiment"})
        with self.assertRaisesRegex(Denied, "kind"):
            self.read()
        write_json(self.record, {"command": self.command})
        with self.assertRaises(Denied):
            self.read()

    def test_missing_corrupt_mismatched_started_metadata(self):
        invalid_nonfinite = (f'{{"run":"{self.run}","role":"worker","extra":NaN}}').encode()
        self.started.unlink()
        with self.assertRaises(Denied):
            self.read()
        for raw in (b"broken", b"\xff", b"[]", b"null", b"{}", invalid_nonfinite,
                    canonical({"run": "a" * 32, "role": "worker"}),
                    canonical({"run": self.run, "role": "reviewer"}), canonical({"run": self.run, "role": 3})):
            with self.subTest(raw=raw):
                self.started.write_bytes(raw)
                with self.assertRaises(Denied):
                    self.read()

    def test_malformed_traversal_ids_and_no_scope_or_path_parameters(self):
        bad_ids = ("", self.run[:12], self.run.upper(), "../" + self.run, "a" * 33, "g" * 32,
                   "a" * 31 + "/", "a" * 31 + "\\", self.run + "\n", "a" * 31 + "\x00")
        for key in ("run", "evidence_id"):
            for bad in bad_ids:
                with self.subTest(key=key, bad=bad), self.assertRaises(Denied):
                    self.read(**{key: bad})
        for key in ("role", "project", "path", "kind"):
            with self.subTest(key=key), self.assertRaises(Denied):
                self.read(**{key: "other"})
        for op in OPS:
            for args in ({}, {"run": self.run}, {**self.args, "limit": READ_PAGE_CHARS + 1},
                         {**self.args, "limit": 0}, {**self.args, "offset": -1}):
                with self.subTest(op=op, args=args), self.assertRaises(Denied):
                    validate(args, DEFINITIONS[op][1])

    def test_other_project_and_missing_or_expired_records_denied(self):
        # A complete valid locator outside this project's runs is not searched.
        foreign = self.root / "other-project" / "runs" / uuid.uuid4().hex
        write_json(foreign / "started.json", {"run": foreign.name, "role": "worker"})
        write_json(foreign / "commands" / self.record.name, self.command)
        with self.assertRaises(Denied):
            self.read(run=foreign.name)
        with self.assertRaises(Denied):
            self.read(evidence_id="f" * 32)
        first = self.read(limit=1)
        self.record.unlink()  # Missing/expired evidence cannot be refetched or recovered.
        for args in ({}, {"offset": 1, "content_id": first["content_id"]}):
            with self.assertRaises(Denied):
                self.read(**args)

    def test_corrupt_and_mismatched_records_denied(self):
        invalid_nonfinite = (f'{{"id":"{self.evidence_id}","kind":"command",'
                             f'"role":"worker","stdout":NaN}}').encode()
        for raw in (b"broken", b"\xff", b"[]", b"null", invalid_nonfinite,
                    canonical({**self.command, "id": "a" * 32}),
                    canonical({**self.command, "role": "reviewer"}),
                    b"[" * 2000 + b"]" * 2000):
            with self.subTest(raw=raw[:60]):
                self.record.write_bytes(raw)
                with self.assertRaises(Denied):
                    self.read()

    def test_continuation_requires_valid_digest_and_checks_entire_record(self):
        for expected in ("", "z" * 64, "A" * 64, "a" * 63):
            with self.subTest(expected=expected), self.assertRaisesRegex(Denied, "content_id"):
                self.read(content_id=expected)
        with self.assertRaisesRegex(Denied, "requires content_id"):
            self.read(offset=1)
        first = self.read(limit=20)
        with self.assertRaisesRegex(Denied, "changed"):
            self.read(content_id="0" * 64)
        # Last byte changes while both the first and continuation pages stay identical.
        raw = self.record.read_bytes()
        self.record.write_bytes(raw[:-1] + b" ")
        self.assertEqual(self.read(limit=20)["text"], first["text"])
        with self.assertRaisesRegex(Denied, "changed"):
            self.read(offset=20, limit=20, content_id=first["content_id"])
        write_json(self.started, {"run": self.run, "role": "reviewer"})
        with self.assertRaisesRegex(Denied, "role mismatch"):
            self.read(offset=20, content_id=first["content_id"])

    def test_complete_record_and_started_metadata_byte_bounds(self):
        self.ctx.config = dataclasses.replace(self.ctx.config, limits=dataclasses.replace(
            self.config.limits, file_bytes=500))
        with self.assertRaises(LimitExceeded):
            self.read(limit=1)
        write_json(self.record, {**self.command, "stdout": "界" * 130})
        self.assertLess(len(self.record.read_bytes().decode("utf-8")), 500)
        self.assertGreater(len(self.record.read_bytes()), 500)
        with self.assertRaises(LimitExceeded):
            self.read(limit=1)
        write_json(self.record, {**self.command, "stdout": ""})
        write_json(self.started, {"run": self.run, "role": "worker", "extra": "x" * 500})
        with self.assertRaises(LimitExceeded):
            self.read(limit=1)

    def test_started_metadata_has_a_small_fixed_bound(self):
        self.ctx.config = dataclasses.replace(self.ctx.config, limits=dataclasses.replace(
            self.config.limits, file_bytes=1024 * 1024))
        write_json(self.started, {"run": self.run, "role": "worker", "extra": "x" * 65536})
        with self.assertRaises(LimitExceeded):
            self.read(limit=1)

    def test_symlink_and_hardlink_files_are_refused(self):
        for target in (self.started, self.record):
            for kind in ("symlink", "hardlink"):
                with self.subTest(target=target.name, kind=kind):
                    saved = target.read_bytes()
                    outside = self.root / (target.name + ".outside")
                    outside.write_bytes(saved)
                    target.unlink()
                    try:
                        if kind == "symlink":
                            target.symlink_to(outside)
                        else:
                            os.link(outside, target)
                    except OSError as exc:
                        target.write_bytes(saved)
                        self.skipTest(f"Link creation unavailable: {type(exc).__name__}")
                    with self.assertRaises(Denied):
                        self.read()
                    target.unlink()
                    target.write_bytes(saved)

    def test_symlink_directory_component_is_refused(self):
        commands = self.record.parent
        moved = self.directory / "moved-commands"
        commands.rename(moved)
        try:
            commands.symlink_to(moved, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {type(exc).__name__}")
        with self.assertRaises(Denied):
            self.read()

    def test_only_exact_safe_reads_no_writes_or_history_scans(self):
        def inventory():
            return {str(p.relative_to(self.project.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.project.root.rglob("*") if p.is_file()}
        before = inventory()
        with patch("mizu.evidence_records.safe_read", wraps=safe_read) as reader, \
                patch("mizu.evidence_records.write_json", side_effect=AssertionError("write")), \
                patch("os.walk", side_effect=AssertionError("scan")), \
                patch("os.listdir", side_effect=AssertionError("scan")), \
                patch("os.scandir", side_effect=AssertionError("scan")), \
                patch.object(Path, "glob", side_effect=AssertionError("scan")), \
                patch.object(Path, "rglob", side_effect=AssertionError("scan")), \
                patch.object(Path, "iterdir", side_effect=AssertionError("scan")):
            first = self.read(limit=10)
            self.read(offset=10, limit=10, content_id=first["content_id"])
        self.assertEqual([(call.args[0], call.args[1], call.args[2]) for call in reader.call_args_list],
                         [(self.project.root, f"runs/{self.run}/{name}", maximum)
                          for name, maximum in (("started.json", 65536),
                                               (f"commands/{self.evidence_id}.json", self.config.limits.file_bytes)) * 2])
        self.assertEqual(inventory(), before)
