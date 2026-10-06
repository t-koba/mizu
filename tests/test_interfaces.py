import dataclasses
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch
from support import Fixture, ScriptDriver, ROOT
from mizu.bridge import Bridge, connect as bridge_connect
from mizu.editor import Capsule, export, serve
from mizu.errors import Denied, LimitExceeded, ProtocolError, ModelFailure
from mizu.fs import canonical, read_json, write_json
from mizu.pi import PiDriver
from mizu.process import Result
from mizu.report import publish
from mizu.runtime import Engine, prompt_for
from mizu.sandbox import Sandbox
from mizu.services import quote, render as render_services
from mizu.storage import backup, prune
from mizu.web import Text, public_addresses, validate_url


class InsightTests(Fixture):
    def test_identity_and_duplicate_submission(self):
        args = dict(source="editor", title="Idea", body="Evidence", base_snapshot=self.project.snapshots.get()["id"], insight_id="same")
        one = self.project.insights.submit(**args)
        self.assertEqual(self.project.insights.submit(**args), one)
        with self.assertRaises(Denied):
            self.project.insights.submit(**{**args, "body": "different"})

    def test_decision_does_not_rewrite_original(self):
        item = self.project.insights.submit(source="searcher", title="Claim", body="Original", base_snapshot=None)
        self.project.insights.decide(item["id"], "reject", "Does not fit constraints", "", "test")
        self.assertEqual(self.project.insights.read(item["id"])["body"], "Original")
        self.assertEqual(self.project.insights.list(), [])
        self.assertEqual(len(list((self.project.root / "decision-history").glob("*.json"))), 1)

    def test_deferral_requires_revisit(self):
        item = self.project.insights.submit(source="editor", title="Idea", body="Original", base_snapshot=None)
        with self.assertRaises(Denied):
            self.project.insights.decide(item["id"], "defer", "Not now", "", "test")

    def test_spool_sender_cannot_be_forged(self):
        spool = self.project.root / "spool/editor"
        write_json(spool / "bad.json", {"source": "operator", "title": "x", "body": "x", "base_snapshot": None})
        self.assertEqual(self.project.insights.ingest_editor(),
                         {"accepted": 0, "rejected": 1, "reaped": 0})
        self.assertEqual(self.project.insights.list(), [])

    def test_malformed_spool_directory_is_quarantined(self):
        (self.project.root / "spool/editor/bad.json").mkdir()
        self.assertEqual(self.project.insights.ingest_editor()["rejected"], 1)

    def test_claim_recovery_is_idempotent(self):
        claimed = self.project.root / ".ingest"
        write_json(claimed / "recovered.json", {"title": "Idea", "body": "Recover after crash", "base_snapshot": None})
        self.assertEqual(self.project.insights.ingest_editor()["accepted"], 1)
        self.assertEqual(self.project.insights.ingest_editor()["accepted"], 0)

    def test_gc_removes_only_old_decided(self):
        import os
        import time
        old = self.project.insights.submit(source="searcher", title="Old", body="b", base_snapshot=None)
        self.project.insights.decide(old["id"], "reject", "no", "", "test")
        new = self.project.insights.submit(source="searcher", title="New", body="b", base_snapshot=None)
        self.project.insights.decide(new["id"], "reject", "no", "", "test")
        waiting = self.project.insights.submit(source="searcher", title="Wait", body="b", base_snapshot=None)
        self.project.insights.decide(waiting["id"], "defer", "later", "condition", "test")
        ancient = time.time() - 32 * 86400
        from mizu.fs import read_json, write_json
        import datetime
        decision_path = self.project.root / "decisions" / f"{old['id']}.json"
        decision = read_json(decision_path)
        decision["created_at"] = datetime.datetime.fromtimestamp(ancient, datetime.timezone.utc).isoformat()
        write_json(decision_path, decision)
        self.assertEqual(self.project.insights.gc_decided(), 1)
        self.assertFalse((self.project.root / "inbox" / f"{old['id']}.json").exists())
        self.assertTrue((self.project.root / "inbox" / f"{new['id']}.json").exists())
        self.assertTrue((self.project.root / "inbox" / f"{waiting['id']}.json").exists())
        self.assertTrue((self.project.root / "decisions" / f"{old['id']}.json").exists())


class EditorTests(Fixture):
    def setUp(self):
        super().setUp()
        self.bundle = self.root / "bundle"
        export(self.project, self.bundle)
        self.outbox = self.project.root / "spool/editor"
        self.capsule = Capsule(self.bundle, self.outbox)

    def test_readonly_contract_has_no_edit_tool(self):
        with self.assertRaises(Denied):
            self.capsule.call("write_file", {"path": "app.py", "text": "bad"})
        self.assertEqual(self.capsule.call("read_file", {"path": "app.py"})["text"], "VALUE = 2\n")

    def test_path_traversal_and_nonexported_paths(self):
        for path in ("../snapshot.json", ".git/config", "/etc/passwd"):
            with self.assertRaises(Denied):
                self.capsule.call("read_file", {"path": path})

    def test_capsule_stays_on_one_snapshot(self):
        (self.project.workspace / "app.py").write_text("VALUE = 88\n")
        Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        self.assertEqual(self.capsule.read("app.py"), "VALUE = 2\n")
        self.assertNotEqual(self.capsule.snapshot["id"], self.project.snapshots.get()["id"])

    def test_proposal_flows_to_worker_without_write(self):
        before = (self.project.workspace / "app.py").read_bytes()
        result = self.capsule.call("submit_insight", {"title": "Consider", "body": "Check boundary conditions"})
        self.assertEqual(self.project.insights.ingest_editor()["accepted"], 1)
        self.assertEqual(self.project.insights.read(result["id"])["source"], "editor")
        self.assertEqual((self.project.workspace / "app.py").read_bytes(), before)

    def test_tampered_bundle_file_is_refused(self):
        (self.bundle / "code/app.py").write_text("tamper")
        with self.assertRaises(Denied):
            self.capsule.read("app.py")

    def test_outbox_cannot_contain_bundle(self):
        with self.assertRaises(Denied):
            Capsule(self.bundle, self.bundle.parent)

    def test_mcp_lifecycle_and_jsonl(self):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "app.py"}}}]
        sink = io.BytesIO()
        serve(self.bundle, self.outbox, input_stream=io.BytesIO(b"".join(map(canonical, messages))), output_stream=sink)
        replies = [json.loads(x) for x in sink.getvalue().split(b"\n") if x]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertFalse(replies[2]["result"]["isError"])
        self.assertEqual(len(replies[1]["result"]["tools"]), 6)

    def test_mcp_parse_error_and_uninitialized(self):
        sink = io.BytesIO()
        raw = b"{broken}\n" + canonical({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        serve(self.bundle, self.outbox, input_stream=io.BytesIO(raw), output_stream=sink)
        replies = [json.loads(x) for x in sink.getvalue().split(b"\n") if x]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertIn("error", replies[1])


class PiProtocolTests(Fixture):
    def driver(self, scenario="normal"):
        cfg = dataclasses.replace(self.config, engines={**self.config.engines, "pi": {**self.config.engines["pi"], "command": (sys.executable, str(ROOT / "tests/fake_pi.py"), "--fake-scenario", scenario)}})
        return PiDriver(cfg)

    def test_rpc_waits_for_settlement_and_preserves_unicode(self):
        ctx = self.context("consult")
        result = self.driver().execute(ctx, prompt_for(ctx))
        self.assertEqual(result["requests"], 1)
        self.assertIn("After agent_end", ctx.finished["summary"])
        self.assertIn("\u2028", ctx.finished["summary"])

    def test_pi_failure_does_not_invent_status_or_reset_from_text(self):
        ctx = self.context("consult")
        with self.assertRaises(ModelFailure) as caught:
            self.driver("provider-error").execute(ctx, "x")
        evidence = caught.exception.evidence
        self.assertEqual(evidence["source"], "pi")
        self.assertIn("429", evidence["message"])
        self.assertIsNone(evidence["code"])
        self.assertIsNone(evidence["retry_at"])

    def test_wrong_exact_model_is_refused_before_payment(self):
        ctx = self.context("consult")
        with self.assertRaises(ProtocolError):
            self.driver("wrong-model").execute(ctx, prompt_for(ctx))
        self.assertEqual(ctx.request_count, 0)

    def test_early_exit_is_not_success(self):
        ctx = self.context("consult")
        with self.assertRaises(ProtocolError):
            self.driver("exit").execute(ctx, "x")

    def test_invalid_json_is_not_success(self):
        ctx = self.context("consult")
        with self.assertRaises(ProtocolError):
            self.driver("bad-json").execute(ctx, "x")

    def test_settled_without_finish_refused(self):
        ctx = self.context("consult")
        with self.assertRaises(ProtocolError):
            self.driver("missing-finish").execute(ctx, "x")

    def test_handled_prompt_not_confused_with_completed_run(self):
        ctx = self.context("consult")
        with self.assertRaises(ProtocolError):
            self.driver("handled").execute(ctx, "x")

    def test_managed_launcher_uses_effective_configuration_file(self):
        ctx = self.context()
        argv = self.driver().argv(ctx.role, ctx.run_dir, "primary", ctx.goal_digest)
        from pathlib import Path as _Path
        self.assertTrue(_Path(argv[-2]).as_posix().endswith("adapters/pi/launcher.mjs"))
        self.assertEqual(argv[-1],str(ctx.run_dir / "pi-effective.json"))
        self.assertNotIn("--mode",argv)

    def test_bridge_requires_per_run_secret(self):
        with Bridge(lambda op, args: {"accepted": True}, [], timeout=2) as bridge:
            config = read_json(bridge.config_file)
            with bridge_connect(config) as sock:
                sock.sendall(canonical({"token": "wrong", "operation": "finish", "arguments": {}}))
                result = json.loads(sock.makefile("rb").readline())
                self.assertFalse(result["ok"])

    def test_bridge_tcp_loopback_round_trip_and_auth(self):
        with Bridge(lambda op, args: {"accepted": True}, [], timeout=2, transport="tcp") as bridge:
            config = read_json(bridge.config_file)
            self.assertEqual(config["transport"], "tcp")
            self.assertEqual(config["host"], "127.0.0.1")
            self.assertTrue(isinstance(config["port"], int) and 1 <= config["port"] <= 65535)
            with bridge_connect(config) as sock:
                sock.sendall(canonical({"token": config["token"], "operation": "finish", "arguments": {}}))
                self.assertTrue(json.loads(sock.makefile("rb").readline())["ok"])
            with bridge_connect(config) as sock:
                sock.sendall(canonical({"token": "wrong", "operation": "finish", "arguments": {}}))
                self.assertFalse(json.loads(sock.makefile("rb").readline())["ok"])
            with self.assertRaises(Denied):
                bridge_connect({**config, "host": "203.0.113.7"})


class IsolationAndWebTests(Fixture):
    def test_sandbox_has_no_host_fallback_or_network(self):
        ctx = self.context()
        args = ctx.sandbox.argv("test", ctx.workspace, "echo hi", writable=True)
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pull=never", "--pids-limit", "--memory", "--cpus"):
            self.assertIn(flag, args)
        self.assertFalse(any("docker.sock" in s or "credentials.env" in s for s in args))
        self.assertEqual(args[-3:], [self.config.sandbox.image, "-c", "echo hi"])

    def test_experiment_source_is_readonly(self):
        ctx = self.context()
        args = ctx.sandbox.argv("test", ctx.workspace, "true", writable=True, experiment=True)
        # resolve() because TMPDIR-style prefixes routinely contain symlinks.
        sources = {str(ctx.workspace), str(ctx.workspace.resolve())}
        mounts = [a for a in args if any(s in a for s in sources)]
        self.assertTrue(mounts, "workspace must be mounted")
        for mount in mounts:
            self.assertTrue(mount.endswith(":ro") or mount.endswith(":ro,z") or "readonly" in mount,
                            f"experiment source must be readonly: {mount}")
        self.assertIn("/work", args)

    def test_failed_cleanup_is_fail_closed(self):
        ctx = self.context()
        with patch("mizu.sandbox.run", side_effect=[Result(0, "ok", "", "exited", .1), Result(1, "", "cleanup failed", "exited", .1)]):
            with self.assertRaises(Denied):
                ctx.sandbox.execute(ctx.workspace, "true", writable=True)
        self.assertEqual(len(list((ctx.run_dir / "commands").glob("*.json"))), 2)

    def test_url_allowlist_is_exact(self):
        self.assertEqual(validate_url("https://example.com/a?q=1", ["example.com"]), ("example.com", "/a?q=1"))
        # Fragments are stripped client-side, never sent.
        self.assertEqual(validate_url("https://example.com/a#section-3", ["example.com"]), ("example.com", "/a"))
        for url in ("http://example.com/", "https://example.com.evil.test/", "https://user@example.com/", "https://example.com:1234/"):
            with self.assertRaises(Denied):
                validate_url(url, ["example.com"])

    def test_private_dns_refused(self):
        for address in ("127.0.0.1", "169.254.169.254", "10.1.2.3", "192.168.1.1"):
            with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]):
                with self.assertRaises(Denied):
                    public_addresses("example.com")

    def test_private_dns_allowed_by_policy(self):
        from mizu.web import public_addresses as addresses
        good_v4 = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in ("10.1.2.3", "192.168.1.1")]
        with patch("socket.getaddrinfo", return_value=good_v4):
            self.assertEqual(addresses("example.com", True), good_v4)
        good_v6 = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::1", 443, 0, 0))]
        with patch("socket.getaddrinfo", return_value=good_v6):
            self.assertEqual(addresses("example.com", True), good_v6)
        for address in ("127.0.0.1", "169.254.169.254", "224.0.0.1"):
            with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]):
                with self.assertRaises(Denied):
                    addresses("example.com", True)

    def test_public_dns_is_pinned(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch("socket.getaddrinfo", return_value=answer):
            self.assertEqual(public_addresses("example.com"), answer)

    def test_html_text_drops_scripts(self):
        parser = Text()
        parser.feed("<p>Readable</p><script>danger()</script><style>bad</style>")
        self.assertIn("Readable", "".join(parser.parts))
        self.assertNotIn("danger", "".join(parser.parts))


class OperationsTests(Fixture):
    def test_report_stores_markdown_and_evidence_without_html(self):
        snapshot = self.project.snapshots.get()
        result = publish(self.project, snapshot, {"title": "T", "body": "## Notes\n\nplain text, no markup emitted"})
        artifact = Path(result["markdown"])
        self.assertTrue(artifact.is_file())
        self.assertEqual(artifact.parent.name, result["artifact"])
        self.assertFalse(list(self.project.root.rglob("*.html")))
        evidence = json.loads((artifact.parent / "evidence.json").read_text())
        self.assertEqual(evidence["snapshot"], snapshot["id"])
        latest = json.loads((self.project.root / "artifacts/latest.json").read_text())
        self.assertEqual(latest["artifact"], result["artifact"])

    def test_facts_only_report_does_not_need_model(self):
        snapshot = self.project.snapshots.get()
        result = publish(self.project, snapshot)
        self.assertTrue(Path(result["markdown"]).is_file())
        self.assertIn(snapshot["id"][:12], Path(result["markdown"]).read_text())

    def test_old_headline_shape_is_rejected(self):
        from mizu.protocol import DEFINITIONS, validate
        from mizu.errors import Denied
        with self.assertRaises(Denied):
            validate({"headline": "H", "sections": []}, DEFINITIONS["report"][1])

    def test_reporter_run_publishes_artifact(self):
        def stages(ctx, *_):
            ctx.handle("report", {"title": "Shift notes", "body": "Nothing invented."})
        result = Engine(self.config, driver=ScriptDriver(stages)).run(self.project, "reporter")
        self.assertIn("artifact", result)
        self.assertTrue((self.project.root / "artifacts/latest.json").is_file())

    def test_snapshot_diff_and_history_are_anchored(self):
        old = self.project.snapshots.get()
        (self.project.workspace / "app.py").write_text("VALUE = 5\n")
        Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        new = self.project.snapshots.get()
        changes = self.project.snapshots.changes(new["id"])
        self.assertIn("-VALUE = 2", changes["diff"])
        self.assertIn("+VALUE = 5", changes["diff"])
        self.assertEqual(changes["base_snapshot"], old["id"])
        self.assertEqual(len(self.project.snapshots.history(old["id"])), 1)

    def test_service_units_do_not_embed_credentials(self):
        cases = (("linux", "mizu-6-sample-worker.service", "mizu-6-sample-reporter.timer"),
                 ("macos", "mizu-6-sample-worker.plist", "mizu-6-sample-reporter.plist"),
                 ("windows", "mizu-6-sample-worker.xml", "mizu-6-sample-reporter.xml"))
        for system, worker_unit, reporter_unit in cases:
            units = render_services(dataclasses.replace(self.config, timezone="local"), self.project, ROOT / "bin/mizu", system=system)
            self.assertIn(worker_unit, units)
            self.assertIn(reporter_unit, units)
            self.assertNotIn("credentials.env", "".join(units.values()))
        units = render_services(self.config, self.project, ROOT / "bin/mizu", system="linux")
        self.assertIn("07:00:00 UTC", units["mizu-6-sample-reporter.timer"])
        self.assertIn("Delegate=yes", units["mizu-6-sample-worker.service"])

    def test_systemd_argument_escaping(self):
        self.assertEqual(quote('/a b/$c%q'), '"/a b/$$c%%q"')
        with self.assertRaises(Denied):
            quote("bad\npath")

    def test_backup_requires_quiescence(self):
        with self.assertRaises(Denied):
            backup(self.project, self.root / "backup.tar.gz")

    def test_backup_excludes_credentials_and_retains_checkpoint(self):
        self.project.set_control(paused=True)
        result = backup(self.project, self.root / "backup.tar.gz")
        with tarfile.open(result["archive"]) as archive:
            names = archive.getnames()
            metadata = json.load(archive.extractfile("backup.json"))
        self.assertIn("backup.json", names)
        self.assertIn("snapshots/" + result["checkpoint"] + ".json", names)
        self.assertFalse(any("credentials.env" in s or s.startswith("workspace/") for s in names))
        self.assertFalse(metadata["credentials_included"])

    def test_prune_never_removes_evidence(self):
        Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        self.project.set_control(paused=True)
        before = sorted(str(x) for x in (self.project.root / "snapshots").glob("*"))
        result = prune(self.project, apply=True)
        self.assertEqual(before, sorted(str(x) for x in (self.project.root / "snapshots").glob("*")))
        audit = self.project.root / result["audit"]
        self.assertTrue(audit.is_file())
        self.assertIn("applied_at", json.loads(audit.read_text()))

    def test_prune_artifact_retention_is_explicit(self):
        snap = self.project.snapshots.get()
        ids = [publish(self.project, snap, {"title": f"E{n}", "body": "x"})["artifact"] for n in range(3)]
        for rank, aid in enumerate(ids):
            path = self.project.root / "artifacts" / aid / "evidence.json"
            record = json.loads(path.read_text())
            record["published_at"] = f"2026-01-0{rank + 1}T00:00:00+00:00"
            path.write_text(json.dumps(record))
        self.project.set_control(paused=True)
        dry = prune(self.project, keep_artifacts=1)
        self.assertFalse(dry["applied"])
        self.assertEqual(len(dry["artifacts"]), 2)
        self.assertTrue(all((self.project.root / p).is_dir() for p in dry["artifacts"]))
        prune(self.project, apply=True, keep_artifacts=1)
        remaining = sorted(p.name for p in (self.project.root / "artifacts").glob("*") if p.is_dir())
        self.assertEqual(remaining, [ids[2]])
        again = prune(self.project, apply=True, keep_artifacts=1)
        self.assertEqual(again["reproducible_inputs"], [])
        self.assertEqual(again["artifacts"], [])

    def test_prune_without_pointer_keeps_full_count(self):
        from mizu.report import publish
        snap = self.project.snapshots.get()
        for n in range(3):
            record = publish(self.project, snap, {"title": f"P{n}", "body": "x"})
            path = self.project.root / "artifacts" / record["artifact"] / "evidence.json"
            data = json.loads(path.read_text())
            data["published_at"] = f"2026-02-0{n + 1}T00:00:00+00:00"
            path.write_text(json.dumps(data))
        (self.project.root / "artifacts" / "latest.json").unlink()
        self.project.set_control(paused=True)
        dry = prune(self.project, keep_artifacts=1)
        self.assertEqual(len(dry["artifacts"]), 2)
        prune(self.project, apply=True, keep_artifacts=1)
        remaining = sorted(p.name for p in (self.project.root / "artifacts").glob("*") if p.is_dir())
        self.assertEqual(len(remaining), 1)

    def test_status_carries_budget_and_cli_budget_matches(self):
        from mizu.cli import execute, parser
        status = self.project.status()
        self.assertIn("used_requests", status["budget"])
        args = parser().parse_args(["--config", str(self.file), "budget", "sample"])
        self.assertEqual(execute(args)["limit"], status["budget"]["limit_requests"])

    def test_service_install_rejects_unscheduled_and_cleans_stale(self):
        import tempfile
        from mizu.services import install
        from pathlib import Path
        cases = (("linux", (".service", ".timer")), ("macos", (".plist",)), ("windows", (".xml",)))
        for system, extensions in cases:
            with tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                for extension in extensions:
                    (directory / f"mizu-sample-ghost{extension}").write_text("stale")
                result = install(dataclasses.replace(self.config, timezone="local"), self.project, ROOT / "bin/mizu", directory, system=system)
                self.assertNotIn("ghost", "".join(result["written"]))
                for extension in extensions:
                    self.assertTrue((directory / f"mizu-sample-ghost{extension}").exists())
                self.assertIn(result["verification"], ("pass", "not_run: native validator unavailable",
                                                       "syntax-pass; native registration not_run"))

    def test_service_install_verification_branches(self):
        import subprocess
        import tempfile
        import unittest.mock
        from pathlib import Path
        from mizu.errors import Denied
        from mizu.services import install
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            run = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with unittest.mock.patch("mizu.services.shutil.which", return_value="/usr/bin/systemd-analyze"), \
                 unittest.mock.patch("mizu.services.subprocess.run", return_value=run) as runner:
                result = install(self.config, self.project, ROOT / "bin/mizu", directory, system="linux")
                self.assertEqual(result["verification"], "pass")
                self.assertTrue(runner.called)
            failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="bad unit")
            with unittest.mock.patch("mizu.services.shutil.which", return_value="/usr/bin/systemd-analyze"), \
                 unittest.mock.patch("mizu.services.subprocess.run", return_value=failed):
                with self.assertRaises(Denied):
                    install(self.config, self.project, ROOT / "bin/mizu", directory, system="linux")

    def test_promote_mode_change_needs_explicit_path(self):
        import sys
        import types
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import install
        finally:
            sys.path.remove(str(ROOT / "scripts"))
        from mizu.errors import Denied
        from mizu.fs import canonical, digest, write_json
        releases = self.root / "prefix" / "releases"
        args = types.SimpleNamespace(prefix=self.root / "prefix", bin_dir=self.root / "bin",
                                     config=self.root / "missing.toml")
        def stage_release(name, mode):
            release = releases / name
            (release / "bin").mkdir(parents=True)
            launcher = release / "bin" / "mizu"
            launcher.write_text('print("mizu 0.1.0")\n')
            launcher.chmod(0o755)
            write_json(release / "source-manifest.json", {})
            write_json(release / "installation.json", {"version": "0.1.0",
                         "source_sha256": digest(canonical({})), "mode": mode,
                         "validation": "local-install-checks-passed"})
            return release
        first = stage_release("r1", "pi")
        self.assertEqual(install.promote(args, first)["status"], "promoted")
        second = stage_release("r2", "core-only")
        with self.assertRaises(Denied):
            install.promote(args, second)
        args.allow_mode_change = True
        result = install.promote(args, second)
        self.assertEqual(result["mode_change"], {"from": "pi", "to": "core-only"})

    def test_link_replace_falls_back_for_windows_dir_symlinks(self):
        import sys
        import tempfile
        import unittest.mock
        from pathlib import Path
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import install
        finally:
            sys.path.remove(str(ROOT / "scripts"))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "a").mkdir()
            (directory / "b").mkdir()
            link = directory / "current"
            link.symlink_to(directory / "a")
            # Windows refuses rename-over-directory-symlink; the fallback
            # unlinks first and retries instead of failing the promotion.
            with unittest.mock.patch.object(install._platform, "IS_WINDOWS", True), \
                 unittest.mock.patch.object(install.os, "replace",
                                            side_effect=[OSError("denied"), None]) as replacer:
                install.link_atomically(link, directory / "b")
            self.assertEqual(replacer.call_count, 2)

    def test_backup_verify_counts_members(self):
        from mizu.storage import backup
        self.project.set_control(paused=True)
        result = backup(self.project, self.root / "verified.tar.gz", verify=True)
        self.assertGreater(result["members"], 0)

    def test_smoke_never_raises_operator_budget(self):
        import dataclasses
        from mizu import smoke
        from mizu.runtime import Engine
        low = dataclasses.replace(self.config.limits, requests_per_run=1, tools_per_run=1)
        config = dataclasses.replace(self.config, limits=low)
        seen = {}
        real_run = Engine.run
        def fake_run(self, project, role_name):
            seen.update(project.config.limits.__dict__ if hasattr(project, "config") else {})
            seen.update(self.config.limits.__dict__)
            raise RuntimeError("stop after capture")
        with unittest.mock.patch.object(Engine, "run", fake_run), \
             unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False):
            try:
                smoke.live(config, role_name="consult")
            except Exception:
                pass
        self.assertLessEqual(seen.get("requests_per_run", 2), 1)
        self.assertLessEqual(seen.get("tools_per_run", 5), 1)

    def test_sandbox_timeout_is_clamped(self):
        from mizu.sandbox import Sandbox
        engine = Sandbox(self.config, self.project.root, "worker", self.project.root / "runs" / "probe")
        argv = engine.argv("n", self.project.workspace, "true", writable=False)
        self.assertIn("--user", argv)

class InsightRevisionTests(Fixture):
    def test_revise_replaces_content_and_archives_audit(self):
        item = self.project.insights.submit(source="operator", title="T", body="v1", base_snapshot=None)
        self.assertEqual(item["rev"], 1)
        gen1 = self.project.insights.generation()
        revised = self.project.insights.revise(item["id"], source="operator", title="T", body="v2", base_snapshot=None, expected_rev=1)
        self.assertEqual(revised["rev"], 2)
        self.assertEqual(self.project.insights.read(item["id"])["body"], "v2")
        hist = self.project.insights.history(item["id"])
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["body"], "v1")
        listed = [i for i in self.project.insights.list(pending=False) if i["id"] == item["id"]][0]
        self.assertEqual(listed["rev"], 2)
        self.assertNotEqual(self.project.insights.generation(), gen1)

    def test_noop_revise_does_not_wake(self):
        item = self.project.insights.submit(source="operator", title="T", body="v1", base_snapshot=None)
        gen1 = self.project.insights.generation()
        same = self.project.insights.revise(item["id"], source="operator", title="T", body="v1", base_snapshot=None)
        self.assertEqual(same["rev"], 1)
        self.assertEqual(self.project.insights.history(item["id"]), [])
        self.assertEqual(self.project.insights.generation(), gen1)

    def test_revise_requires_submitter_and_cas(self):
        item = self.project.insights.submit(source="operator", title="T", body="v1", base_snapshot=None)
        with self.assertRaises(Denied):
            self.project.insights.revise(item["id"], source="editor", title="T", body="v2", base_snapshot=None)
        with self.assertRaises(Denied):
            self.project.insights.revise(item["id"], source="operator", title="T", body="v2", base_snapshot=None, expected_rev=99)
        with self.assertRaises(Denied):
            self.project.insights.submit(source="operator", title="T", body="other", base_snapshot=None, insight_id=item["id"])

    def test_stale_approval_becomes_pending_and_gc_keeps_it(self):
        import datetime
        from mizu.fs import read_json, write_json
        item = self.project.insights.submit(source="operator", title="T", body="v1", base_snapshot=None)
        self.project.insights.decide(item["id"], "accept", "good", "", "test")
        self.assertEqual(self.project.insights.list(), [])
        self.project.insights.revise(item["id"], source="operator", title="T", body="v2", base_snapshot=None)
        self.assertIn(item["id"], [i["id"] for i in self.project.insights.list()])
        decision_path = self.project.root / "decisions" / f"{item['id']}.json"
        decision = read_json(decision_path)
        ancient = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=32)
        decision["created_at"] = ancient.isoformat()
        write_json(decision_path, decision)
        self.assertEqual(self.project.insights.gc_decided(), 0)
        self.assertEqual(self.project.insights.read(item["id"])["rev"], 2)
