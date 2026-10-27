"""M2 step 1: vcs_read/vcs_publish split with GO<branch> approval gate. Offline only."""
import dataclasses
import json
import os
import sys
import subprocess
from unittest.mock import patch
import unittest
import uuid

from support import Fixture, set_project_target
from mizu import vcs
from mizu.errors import ConfigError, Denied
from mizu.fs import canonical, digest, lock, mkdir, write_json
from mizu.project import git_env
from mizu.publication import Publication
from mizu.distribution import public_source_identity
from mizu.process import Result
from mizu.runtime import Context, check_consult_role


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


READ_OK = ("import sys,json; req=json.load(sys.stdin); "
           "print(json.dumps({'op': req['op'], 'branch': req['branch'], 'checks': []}))")
PUSH_OK = ("import sys,json; req=json.load(sys.stdin); "
           "print(json.dumps({'op': req['op'], 'branch': req['branch'], 'digest': req['digest'], 'projection_schema': req['projection_schema'], 'projection_algorithm': req['projection_algorithm'], 'projection_digest': req['projection_digest'], 'publication_id': req['publication_id'], 'published_sha': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'pushed': True}))")
PUSH_WRONG_DIGEST = ("import sys,json; req=json.load(sys.stdin); "
           "print(json.dumps({'op': req['op'], 'branch': req['branch'], 'digest': '" + "f" * 64 + "', 'projection_schema': req['projection_schema'], 'projection_algorithm': req['projection_algorithm'], 'projection_digest': req['projection_digest'], 'pushed': True}))")
PUSH_NO_DIGEST = ("import sys,json; req=json.load(sys.stdin); "
           "print(json.dumps({'op': req['op'], 'branch': req['branch'], 'pushed': True}))")
PUSH_WRONG_PROJECTION = ("import sys,json; req=json.load(sys.stdin); "
           "print(json.dumps({'op': req['op'], 'branch': req['branch'], 'digest': req['digest'], 'projection_schema': req['projection_schema'], 'projection_algorithm': req['projection_algorithm'], 'projection_digest': '" + "f" * 64 + "', 'publication_id': req['publication_id'], 'published_sha': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'pushed': True}))")


def init_publish_git(fixture):
    """Local commit only; never contact a remote or change shared fixtures."""
    ws = fixture.project.workspace
    def git(*args):
        return subprocess.run(["git", "-c", f"core.hooksPath={os.devnull}",
                               "-C", str(ws), *args], env=git_env(), check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
    git("init")
    git("add", "app.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Create fixture commit")
    return git("rev-parse", "HEAD")


class PublishFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.source_commit = init_publish_git(self)


def with_caps(fixture, name, extra):
    role = fixture.config.roles[name]
    new_role = dataclasses.replace(role, capabilities=tuple(list(role.capabilities) + extra))
    config = dataclasses.replace(fixture.config,
                                 roles={**fixture.config.roles, name: new_role})
    return config, new_role


def make_context(fixture, config, role, workspace=None):
    run = fixture.project.root / "runs" / uuid.uuid4().hex
    mkdir(run)
    snap = fixture.project.snapshots.get()
    ws = workspace or fixture.project.workspace
    return Context(config, fixture.project, role, run, snap, ws)


def current_digest(fixture):
    captured = fixture.project.snapshots.capture_files(fixture.project.workspace)
    assert not captured["skipped"]
    return captured["code_digest"]


def approve(fixture, branch, digest, action="accept", source="operator", run="operator"):
    record = fixture.project.insights.submit(
        source=source, title=f"GO {branch}",
        body=f"Ship it.\ndigest: {digest}\n",
        base_snapshot=None, run=None)
    fixture.project.insights.decide(record["id"], action, "reviewed", "", run,
                                      expected_rev=record["rev"])
    return record


class VcsPublishTests(PublishFixture):
    def test_unapproved_push_refused_without_spawn(self):
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        ctx = make_context(self, config, role)
        # No GO insight at all; adapter must never spawn (no evidence file).
        with self.assertRaisesRegex(Denied, "recorded human approval"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})
        self.assertEqual(list((ctx.run_dir / "vcs-publish").glob("*.json")), [])

    def test_stale_digest_refused(self):
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        approve(self, "main", "0" * 64)
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "stale"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_revised_go_requires_a_decision_for_the_current_revision(self):
        original_digest, revised_digest = "a" * 64, "b" * 64
        record = approve(self, "main", original_digest)
        revised = self.project.insights.revise(
            record["id"], source="operator", title=record["title"],
            body=f"Ship it.\ndigest: {revised_digest}\n",
            base_snapshot=record["base_snapshot"], run="operator",
            expected_rev=record["rev"])
        self.assertEqual(revised["rev"], 2)
        self.assertIsNone(self.project.insights.read(record["id"]).get("decision"))
        with self.assertRaisesRegex(Denied, "stale.*revision"):
            vcs.require_go_approval(self.project, "main", revised_digest)

        self.project.insights.decide(record["id"], "accept", "reviewed new digest", "",
                                     "operator", expected_rev=revised["rev"])
        approval = vcs.require_go_approval(self.project, "main", revised_digest)
        self.assertEqual(approval["insight"], record["id"])
        self.assertEqual(approval["code_digest"], revised_digest)

    def test_accepted_digest_publishes(self):
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        digest = current_digest(self)
        rec = approve(self, "main", digest)
        ctx = make_context(self, config, role)
        out = ctx.handle("vcs_publish", {"op": "push", "branch": "main"})
        self.assertTrue(out["published"])
        self.assertEqual(out["branch"], "main")
        self.assertEqual(out["code_digest"], digest)
        self.assertEqual(out["approval"], rec["id"])
        self.assertEqual(out["trust"], "external-untrusted")
        evidence = json.loads((ctx.run_dir / out["evidence"]).read_text())
        self.assertEqual(evidence["approval"], rec["id"])
        # Uniform binding: the file always equals the returned response.
        self.assertEqual(evidence, out)

    def test_branch_mismatch_refused(self):
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        approve(self, "main", current_digest(self))
        ctx = make_context(self, config, role)
        with self.assertRaises(Denied):
            ctx.handle("vcs_publish", {"op": "push", "branch": "other"})

    def test_vcs_read_cannot_publish(self):
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs=adapter_settings(READ_OK))
        ctx = make_context(self, config, role)
        out = ctx.handle("vcs_read", {"op": "status", "branch": "main"})
        self.assertEqual(out["trust"], "external-untrusted")
        with self.assertRaisesRegex(Denied, "cannot publish"):
            vcs.read_via(config.vcs, "push", {"branch": "main"})
        with self.assertRaisesRegex(Denied, "cannot publish"):
            vcs.publish_via(config.vcs, "status", {"branch": "main", "code_digest": "a" * 64})
        # vcs_read capability alone cannot reach publish (capability gate).
        with self.assertRaisesRegex(Denied, "no capability"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_publish_requires_grant_and_write(self):
        ctx = self.context("worker")
        self.assertNotIn("vcs_publish", ctx.role.capabilities)
        with self.assertRaises(Denied):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})
        # Read role holding publish is refused at call time (writable gate).
        base = self.config.roles["reviewer"]
        bad = dataclasses.replace(base, capabilities=tuple(list(base.capabilities) + ["vcs_publish"]))
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        snap = self.project.snapshots.get()
        # workspace for read role materialized input
        mkdir(run / "input")
        ctx2 = Context(dataclasses.replace(self.config, vcs=adapter_settings(PUSH_OK)),
                       self.project, bad, run, snap, run / "input")
        with self.assertRaisesRegex(Denied, "[Ww]ritable"):
            ctx2.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_publish_on_read_role_refused_at_load(self):
        text = self.file.read_text()
        needle = '[roles.reviewer]'
        self.assertIn(needle, text)
        segment = text.split(needle, 1)[1].split('[roles.', 1)[0]
        self.assertIn('"submit_insight"', segment)
        patched = text.replace(
            'capabilities = ["files", "read", "diff", "exec", "experiment", "submit_insight", "revise_insight", "withdraw_insight", "work_items_read", "finish"]',
            'capabilities = ["files", "read", "diff", "exec", "experiment", "submit_insight", "revise_insight", "withdraw_insight", "work_items_read", "finish", "vcs_publish"]',
            1)
        self.assertNotEqual(patched, text)
        path = self.root / "config/publish-read.toml"
        path.write_text(patched)
        from mizu.config import load
        with self.assertRaises(ConfigError):
            load(path)

    def test_consult_cannot_hold_publish(self):
        base = self.config.roles["consult"]
        bad = dataclasses.replace(base, capabilities=("files", "read", "finish", "vcs_publish"))
        with self.assertRaises(ConfigError):
            check_consult_role("consult", bad)
        # vcs_read stays allowed on consult (read-only).
        ok = dataclasses.replace(base, capabilities=("files", "read", "finish", "vcs_read"))
        check_consult_role("consult", ok)


if __name__ == "__main__":
    raise SystemExit(unittest.main())


class VcsApprovalChannelTests(Fixture):
    def test_model_submitted_go_refused(self):
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        approve(self, "main", current_digest(self), source="worker", run="operator")
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "operator channel"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_model_decide_refused(self):
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        approve(self, "main", current_digest(self), source="operator", run="run-abc123")
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "operator channel"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_publish_role_must_not_hold_decide(self):
        text = self.file.read_text()
        needle = '[roles.maintainer]'
        self.assertIn(needle, text)
        segment = text.split(needle, 1)[1].split('[roles.', 1)[0]
        self.assertIn('"decide"', segment)
        patched = text.replace(
            'capabilities = ["files", "read", "diff", "exec", "experiment", "verify", "insights", "decide", "consult", "work_items_read", "finish"]',
            'capabilities = ["files", "read", "diff", "exec", "experiment", "verify", "insights", "decide", "consult", "work_items_read", "finish", "vcs_publish"]',
            1)
        self.assertNotEqual(patched, text)
        path = self.root / "config/publish-decide.toml"
        path.write_text(patched)
        from mizu.config import load
        with self.assertRaisesRegex(ConfigError, "self-approval"):
            load(path)

    def test_publish_role_may_submit_insights(self):
        text = self.file.read_text()
        needle = '[roles.maintainer]'
        self.assertIn(needle, text)
        segment = text.split(needle, 1)[1].split('[roles.', 1)[0]
        patched = text.replace(
            'capabilities = ["files", "read", "diff", "exec", "experiment", "verify", "insights", "decide", "consult", "work_items_read", "finish"]',
            'capabilities = ["files", "read", "diff", "exec", "experiment", "verify", "insights", "submit_insight", "consult", "work_items_read", "finish", "vcs_publish"]',
            1)
        self.assertNotEqual(patched, text)
        path = self.root / "config/publish-submit.toml"
        path.write_text(patched)
        from mizu.config import load
        config = load(path)
        self.assertIn("vcs_publish", config.roles["maintainer"].capabilities)
        self.assertIn("submit_insight", config.roles["maintainer"].capabilities)


class VcsReservedRoleNameTests(Fixture):
    def test_operator_vcs_editor_names_refused_at_load(self):
        from mizu.config import load
        for name in ("operator", "vcs", "editor"):
            text = self.file.read_text() + f'\n[roles.{name}]\n'
            path = self.root / f"config/reserved-{name}.toml"
            path.write_text(text)
            with self.assertRaisesRegex(ConfigError, "reserved"):
                load(path)


class VcsDigestBindingTests(PublishFixture):
    def test_adapter_must_echo_approved_digest(self):
        import dataclasses as _dc
        config, role = with_caps(self, "worker", ["vcs_publish"])
        digest = current_digest(self)
        rec = approve(self, "main", digest)
        config = _dc.replace(config, vcs=adapter_settings(PUSH_OK))
        ctx = make_context(self, config, role)
        out = ctx.handle("vcs_publish", {"op": "push", "branch": "main"})
        self.assertEqual(out["code_digest"], digest)
        self.assertEqual(out["result"]["digest"], digest)

    def test_mismatched_digest_echo_refused(self):
        import dataclasses as _dc
        config, role = with_caps(self, "worker", ["vcs_publish"])
        digest = current_digest(self)
        approve(self, "main", digest)
        config = _dc.replace(config, vcs=adapter_settings(PUSH_WRONG_DIGEST))
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "acknowledge the whole-capture digest"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_missing_digest_echo_refused(self):
        import dataclasses as _dc
        config, role = with_caps(self, "worker", ["vcs_publish"])
        digest = current_digest(self)
        approve(self, "main", digest)
        config = _dc.replace(config, vcs=adapter_settings(PUSH_NO_DIGEST))
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "acknowledge the whole-capture digest"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_mismatched_projection_echo_refused(self):
        import dataclasses as _dc
        config, role = with_caps(self, "worker", ["vcs_publish"])
        digest = current_digest(self)
        approve(self, "main", digest)
        config = _dc.replace(config, vcs=adapter_settings(PUSH_WRONG_PROJECTION))
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "confirm the pushed projection identity"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_publish_via_requires_digest_param(self):
        with self.assertRaisesRegex(Denied, "code digest"):
            vcs.publish_via(adapter_settings(PUSH_OK), "push", {"branch": "main"})


class PublicationReceiptTests(PublishFixture):
    def publication(self, branch="main", op="push"):
        code_digest = current_digest(self)
        approval = approve(self, branch, code_digest)
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        ctx = make_context(self, config, role)
        from mizu.distribution import public_source_identity
        captured = self.project.snapshots.capture_files(self.project.workspace)
        projection = public_source_identity(captured.get("files", {}))
        store = Publication(self.project.root, self.source_commit, code_digest, branch, op,
                            projection=projection)
        return ctx, store, approval, {"op": op, "branch": branch}

    @staticmethod
    def reply(_argv, **kwargs):
        req = json.loads(kwargs["input_data"])
        return Result(0, json.dumps({"digest": req["digest"],
                      "projection_schema": req["projection_schema"],
                      "projection_algorithm": req["projection_algorithm"],
                      "projection_digest": req["projection_digest"],
                      "publication_id": req["publication_id"],
                      "published_sha": "b" * 40}), "", "exited", 0)

    def test_publication_id_status_query_uses_receipt_sha_only(self):
        config, role = with_caps(self, "worker", ["vcs_publish", "vcs_read"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        code_digest = current_digest(self)
        approval = approve(self, "main", code_digest)
        source = self.source_commit
        publisher = make_context(self, config, role)
        from mizu.distribution import public_source_identity
        _captured = self.project.snapshots.capture_files(self.project.workspace)
        receipt_store = Publication(self.project.root, source, code_digest, "main", "push",
                                    projection=public_source_identity(_captured.get("files", {})))
        publish_args = {"op": "push", "branch": "main"}
        with patch.object(vcs, "run", side_effect=self.reply):
            published = publisher.handle("vcs_publish", publish_args)
        self.assertEqual(published["publication_id"], receipt_store.id)
        self.assertEqual(published["approval"], approval["id"])

        def status_adapter(_argv, **kwargs):
            request = json.loads(kwargs["input_data"])
            self.assertEqual(request, {"op": "status", "branch": "main", "sha": "b" * 40})
            return Result(0, json.dumps({"checks": [
                {"check": "required", "state": "success", "sha": "b" * 40},
                {"check": "unrelated", "state": "failure", "sha": "c" * 40},
            ]}), "", "exited", 0)

        reader_config = dataclasses.replace(config, vcs=adapter_settings(READ_OK))
        reader = make_context(self, reader_config, role)
        with patch.object(vcs, "run", side_effect=status_adapter) as adapter:
            observed = reader.handle("vcs_read", {"op": "status",
                                                   "publication_id": receipt_store.id})
        adapter.assert_called_once()
        self.assertEqual(observed["branch"], "main")
        self.assertEqual(observed["result"]["publication"], {
            "publication_id": receipt_store.id, "source_commit": source,
            "code_digest": code_digest, "branch": "main",
            "published_sha": "b" * 40, "approval": approval["id"],
            "run": publisher.run_dir.name,
            "projection_schema": receipt_store.identity["projection_schema"],
            "projection_algorithm": receipt_store.identity["projection_algorithm"],
            "projection_digest": receipt_store.identity["projection_digest"]})
        self.assertEqual(observed["result"]["lineage"]["status"], "unknown")
        checks = observed["result"]["checks"]
        self.assertEqual([(row["check"], row["sha"]) for row in checks],
                         [("required", "b" * 40)])
        retained = json.loads((reader.run_dir / observed["evidence"]).read_text())
        self.assertEqual(retained, observed)

    def test_publication_id_status_rejects_overrides_and_invalid_receipt(self):
        config, role = with_caps(self, "worker", ["vcs_publish", "vcs_read"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        code_digest = current_digest(self)
        approve(self, "main", code_digest)
        publisher = make_context(self, config, role)
        from mizu.distribution import public_source_identity as _identity
        _cap = self.project.snapshots.capture_files(self.project.workspace)
        store = Publication(self.project.root, self.source_commit, code_digest, "main", "push",
                            projection=_identity(_cap.get("files", {})))
        with patch.object(vcs, "run", side_effect=self.reply):
            publisher.handle("vcs_publish", {"op": "push", "branch": "main"})
        reader = make_context(self, dataclasses.replace(config, vcs=adapter_settings(READ_OK)), role)
        with patch.object(vcs, "run") as adapter:
            with self.assertRaisesRegex(Denied, "no branch, sha"):
                reader.handle("vcs_read", {"op": "status", "publication_id": store.id,
                                             "branch": "main"})
            adapter.assert_not_called()
        receipt = json.loads(store.path("receipt").read_text())
        receipt["published_sha"] = "invalid"
        store.path("receipt").write_text(json.dumps(receipt))
        with patch.object(vcs, "run") as adapter:
            with self.assertRaisesRegex(Denied, "Invalid publication receipt"):
                reader.handle("vcs_read", {"op": "status", "publication_id": store.id})
            adapter.assert_not_called()

    def test_receipt_fields_stable_identity_and_cross_run_replay(self):
        ctx, store, approval, args = self.publication()
        def dispatch(*argv, **kwargs):
            request = json.loads(kwargs["input_data"])
            self.assertEqual(request["publication_id"], store.id)
            self.assertEqual(request["source_commit"], self.source_commit)
            self.assertTrue(store.path("intent").is_file())
            self.assertTrue(store.path("dispatch").is_file())
            return self.reply(*argv, **kwargs)
        with patch.object(vcs, "run", side_effect=dispatch) as adapter:
            out = ctx.handle("vcs_publish", args)
        adapter.assert_called_once()
        receipt_bytes = store.path("receipt").read_bytes()
        receipt = json.loads(receipt_bytes)
        self.assertEqual(receipt, {**store.identity, "schema": store.schema,
                         "publication_id": digest(canonical(store.identity)),
                         "approval": approval["id"], "run": ctx.run_dir.name,
                         "published_sha": "b" * 40})
        self.assertEqual(out["publication_id"], store.id)
        self.assertEqual(out["published_sha"], "b" * 40)
        self.assertEqual(self.project.root / out["receipt"], store.path("receipt"))
        retry = make_context(self, ctx.config, ctx.role)
        with patch.object(vcs, "run") as adapter:
            replay = retry.handle("vcs_publish", args)
        adapter.assert_not_called()
        self.assertEqual(replay["publication_id"], out["publication_id"])
        self.assertEqual(replay["published_sha"], out["published_sha"])
        self.assertEqual(replay["run"], ctx.run_dir.name)
        self.assertEqual(store.path("receipt").read_bytes(), receipt_bytes)
        self.assertEqual(json.loads((retry.run_dir / replay["evidence"]).read_bytes()), replay)
        # Replay still needs a current digest-bound human approval.
        (self.project.workspace / "app.py").write_bytes(b"changed\n")
        with patch.object(vcs, "run") as adapter:
            with self.assertRaises(Denied):
                retry.handle("vcs_publish", args)
        adapter.assert_not_called()

    def test_identity_changes_only_with_stable_request_pins(self):
        _, store, _, _ = self.publication()
        pins = dict(source=self.source_commit, code_digest=store.identity["code_digest"],
                    branch="main", op="push",
                    projection={"schema": store.identity["projection_schema"],
                                "algorithm": store.identity["projection_algorithm"],
                                "digest": store.identity["projection_digest"]})
        self.assertEqual(Publication(self.project.root, **pins).id, store.id)
        for key, value in (("source", "a" * 64), ("code_digest", "0" * 64),
                           ("branch", "other"), ("op", "pr")):
            with self.subTest(key=key):
                self.assertNotEqual(Publication(self.project.root, **{**pins, key: value}).id, store.id)
        with self.subTest(key="projection_digest"):
            from mizu.distribution import public_source_identity as _identity2
            altered = dict(pins)
            altered["projection"] = {"schema": pins["projection"]["schema"],
                                     "algorithm": pins["projection"]["algorithm"],
                                     "digest": "0" * 64}
            self.assertNotEqual(Publication(self.project.root, **altered).id, store.id)

    def test_undispatched_intent_reuses_identity(self):
        ctx, store, approval, args = self.publication()
        with lock(store.lock):
            intent, receipt = store.prepare(approval["id"], ctx.run_dir.name)
        self.assertIsNone(receipt)
        before = store.path("intent").read_bytes()
        retry = make_context(self, ctx.config, ctx.role)
        with patch.object(vcs, "run", side_effect=self.reply) as adapter:
            out = retry.handle("vcs_publish", args)
        adapter.assert_called_once()
        self.assertEqual(out["publication_id"], intent["publication_id"])
        self.assertEqual(out["run"], ctx.run_dir.name)
        self.assertNotEqual(retry.run_dir.name, ctx.run_dir.name)
        self.assertEqual(store.path("intent").read_bytes(), before)

    def test_undispatched_intent_refuses_different_approval(self):
        ctx, store, approval, args = self.publication()
        with lock(store.lock):
            store.prepare(approval["id"], ctx.run_dir.name)
        before = store.path("intent").read_bytes()
        # The current gate supplies a different valid GO for the same pins.
        with patch.object(vcs, "require_go_approval", return_value={"insight": "other-go"}), patch.object(vcs, "run") as adapter:
            with self.assertRaisesRegex(Denied, "approval mismatch"):
                ctx.handle("vcs_publish", args)
        adapter.assert_not_called()
        self.assertEqual(store.path("intent").read_bytes(), before)
        self.assertFalse(store.path("dispatch").exists())
        self.assertFalse(store.path("receipt").exists())
        # Completed replay is evidence-only, even with a later approval.
        with patch.object(vcs, "run", side_effect=self.reply):
            original = ctx.handle("vcs_publish", args)
        with patch.object(vcs, "require_go_approval", return_value={"insight": "other-go"}), patch.object(vcs, "run") as adapter:
            replay = make_context(self, ctx.config, ctx.role).handle("vcs_publish", args)
        adapter.assert_not_called()
        self.assertEqual(replay["approval"], original["approval"])
        self.assertEqual(replay["run"], original["run"])

    def test_unsafe_evidence_directories_have_no_outside_side_effects(self):
        for component in ("project", "evidence", "publications"):
            for kind in ("symlink", "file"):
                with self.subTest(component=component, kind=kind):
                    base = self.root / f"unsafe-{component}-{kind}"
                    base.mkdir()
                    project = base / "project"
                    store = Publication(project, self.source_commit, "0" * 64, "main", "push",
                                        projection=public_source_identity({}))
                    paths = {"project": project, "evidence": project / "evidence",
                             "publications": store.root}
                    target = paths[component]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    outside = base / "outside"
                    outside.mkdir()
                    if kind == "symlink":
                        try:
                            target.symlink_to(outside, target_is_directory=True)
                        except (OSError, NotImplementedError):
                            # Windows without the symlink privilege still runs
                            # every non-directory case below.
                            continue
                    else:
                        target.write_bytes(b"not a directory")
                    with self.assertRaisesRegex(Denied, "Unsafe publication"):
                        with lock(store.lock):
                            store.prepare("go", "run")
                    # Direct preparation must not bypass the path screening.
                    with self.assertRaisesRegex(Denied, "Unsafe publication"):
                        store.prepare("go", "run")
                    self.assertEqual(list(outside.iterdir()), [])
                    self.assertEqual(list(base.rglob("*.lock")), [])
                    self.assertEqual(list(base.rglob("*.json")), [])

    def test_durable_write_failures_prevent_dispatch(self):
        for kind in ("intent", "dispatch"):
            for late in (False, True):
                with self.subTest(kind=kind, late=late):
                    ctx, store, _, args = self.publication(f"failure/{kind}-{late}")
                    def fail(path, record, **kwargs):
                        if path == store.path(kind):
                            if late:
                                write_json(path, record, **kwargs)
                            raise OSError("durable write failed")
                        write_json(path, record, **kwargs)
                    with patch("mizu.publication.write_json", side_effect=fail), patch.object(vcs, "run") as adapter:
                        with self.assertRaisesRegex(Denied, "durable write failed"):
                            ctx.handle("vcs_publish", args)
                    adapter.assert_not_called()
                    self.assertFalse(store.path("receipt").exists())
                    if kind == "dispatch" and late:
                        with patch.object(vcs, "run") as adapter:
                            with self.assertRaisesRegex(Denied, store.id):
                                ctx.handle("vcs_publish", args)
                        adapter.assert_not_called()

    def test_invalid_responses_remain_unresolved_and_never_redispatch(self):
        invalid = [{}, {"digest": "0" * 64}, {"publication_id": None},
                   {"publication_id": "0" * 64}, {"publication_id": "A" * 64}]
        invalid += [{"published_sha": sha} for sha in (None, "a" * 12, "a" * 39,
                    "a" * 41, "a" * 63, "a" * 65, "A" * 40, "g" * 40, 123)]
        for index, bad in enumerate(invalid):
            with self.subTest(bad=bad):
                ctx, store, _, args = self.publication(f"invalid/{index}")
                data = {"digest": store.identity["code_digest"],
                        "publication_id": store.id, "published_sha": "a" * 40}
                data = {} if not bad else {**data, **bad}
                with patch.object(vcs, "run", return_value=Result(0, json.dumps(data), "", "exited", 0)) as adapter:
                    with self.assertRaises(Denied):
                        ctx.handle("vcs_publish", args)
                adapter.assert_called_once()
                self.assertTrue(store.path("dispatch").is_file())
                self.assertFalse(store.path("receipt").exists())
                pending, = (ctx.run_dir / "vcs-publish").glob("*.json")
                self.assertEqual(json.loads(pending.read_bytes())["outcome"], "unknown")
                with patch.object(vcs, "run") as adapter:
                    with self.assertRaisesRegex(Denied, "unresolved.*" + store.id):
                        make_context(self, ctx.config, ctx.role).handle("vcs_publish", args)
                adapter.assert_not_called()

    def test_receipt_failure_is_unresolved_and_receipt_precedes_observation(self):
        ctx, store, _, args = self.publication()
        def fail_receipt(path, record, **kwargs):
            if path == store.path("receipt"):
                raise OSError("receipt failed")
            write_json(path, record, **kwargs)
        with patch.object(vcs, "run", side_effect=self.reply), patch(
                "mizu.publication.write_json", side_effect=fail_receipt):
            with self.assertRaisesRegex(Denied, "receipt failed"):
                ctx.handle("vcs_publish", args)
        with patch.object(vcs, "run") as adapter:
            with self.assertRaisesRegex(Denied, store.id):
                ctx.handle("vcs_publish", args)
        adapter.assert_not_called()

        ctx, store, _, args = self.publication("observation")
        def fail_observation(path, record, **kwargs):
            if record.get("outcome") == "observed":
                self.assertTrue(store.path("receipt").is_file())
                raise OSError("observation failed")
            write_json(path, record, **kwargs)
        with patch.object(vcs, "run", side_effect=self.reply), patch(
                "mizu.evidence_records.write_json", side_effect=fail_observation):
            with self.assertRaisesRegex(OSError, "observation failed"):
                ctx.handle("vcs_publish", args)
        with patch.object(vcs, "run") as adapter:
            out = ctx.handle("vcs_publish", args)
        adapter.assert_not_called()
        self.assertEqual(out["publication_id"], store.id)

    def test_bad_request_identity_refused_before_adapter(self):
        from mizu.distribution import PUBLIC_SOURCE_ALGORITHM, PUBLIC_SOURCE_SCHEMA
        params = {"branch": "main", "code_digest": "a" * 64,
                  "projection_schema": PUBLIC_SOURCE_SCHEMA,
                  "projection_algorithm": PUBLIC_SOURCE_ALGORITHM,
                  "projection_digest": "a" * 64,
                  "publication_id": "b" * 64, "source_commit": "c" * 40}
        for key, values in (("publication_id", (None, "", "A" * 64, "a" * 63, 1)),
                            ("source_commit", (None, "abc", "A" * 40, "a" * 41))):
            for value in values:
                with self.subTest(key=key, value=value), patch.object(vcs, "run") as adapter:
                    with self.assertRaises(Denied):
                        vcs.publish_via(adapter_settings(PUSH_OK), "push", {**params, key: value})
                    adapter.assert_not_called()
        for sha in ("a" * 40, "b" * 64):
            with patch.object(vcs, "run", return_value=Result(0, json.dumps(
                    {"digest": params["code_digest"],
                     "projection_schema": params["projection_schema"],
                     "projection_algorithm": params["projection_algorithm"],
                     "projection_digest": params["projection_digest"],
                     "publication_id": params["publication_id"],
                     "published_sha": sha}), "", "exited", 0)):
                self.assertEqual(vcs.publish_via(adapter_settings(PUSH_OK), "push", params)["published_sha"], sha)

    def test_unavailable_or_invalid_source_git_refused(self):
        ctx, store, _, args = self.publication()
        (ctx.workspace / ".git").rename(self.root / "saved-git")
        with patch.object(vcs, "run") as adapter:
            with self.assertRaisesRegex(Denied, "local workspace Git HEAD"):
                ctx.handle("vcs_publish", args)
        adapter.assert_not_called()
        self.assertFalse(store.path("intent").exists())
        for sha in ("abc", "A" * 40, "a" * 41, "a" * 40 + "\n" + "b" * 40):
            with self.subTest(sha=sha), patch("mizu.publication.process.run",
                    return_value=Result(0, sha, "", "exited", 0)), patch.object(vcs, "run") as adapter:
                with self.assertRaisesRegex(Denied, "local workspace Git HEAD"):
                    ctx.handle("vcs_publish", args)
                adapter.assert_not_called()

    def test_receipt_is_create_only_and_reads_are_bounded(self):
        from mizu.errors import LimitExceeded
        ctx, store, _, args = self.publication()
        with patch.object(vcs, "run", side_effect=self.reply):
            out = ctx.handle("vcs_publish", args)
        intent = json.loads(store.path("intent").read_bytes())
        with self.assertRaisesRegex(Denied, "Immutable object conflict"):
            store.complete(intent, "c" * 40)
        self.assertEqual(json.loads(store.path("receipt").read_bytes())["published_sha"], out["published_sha"])
        store.path("receipt").write_bytes(b" " * 8193)
        with patch.object(vcs, "run") as adapter:
            with self.assertRaises(LimitExceeded):
                ctx.handle("vcs_publish", args)
        adapter.assert_not_called()

class PublicationLineageTests(PublishFixture):
    """A single bounded adapter observation, never inferred from content."""

    def fact(self, **changes):
        import datetime as dt
        return {**{"schema": 1, "repository": "1" * 64, "remote": "origin",
                   "target_ref": "refs/heads/main", "target_oid": "a" * 40,
                   "published_oid": "b" * 40, "merge_bases": ["a" * 40],
                   "target_is_ancestor": True,
                   "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}, **changes}

    def target(self, **changes):
        base = {"repository": "1" * 64, "remote": "origin",
                "target_ref": "refs/heads/main", "version": 1}
        base.update(changes)
        return base

    def publish(self, make_fact, target=None):
        config, role = with_caps(self, "worker", ["vcs_publish", "vcs_read"])
        expected = self.target() if target is None else target
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        set_project_target(self, expected)
        approve(self, "topic", current_digest(self))
        publisher = make_context(self, config, role)

        def adapter(_argv, **kwargs):
            req = json.loads(kwargs["input_data"])
            self.assertEqual(req.get("lineage_target"), expected)
            return Result(0, json.dumps({**{k: req[k] for k in (
                "digest", "projection_schema", "projection_algorithm", "projection_digest",
                "publication_id")}, "published_sha": "b" * 40,
                "lineage": make_fact()}), "", "exited", 0)
        with patch.object(vcs, "run", side_effect=adapter):
            published = publisher.handle("vcs_publish", {"op": "push", "branch": "topic"})
        return config, role, publisher, published

    def read(self, config, role, publication_id, make_fact):
        reader = make_context(self, config, role)
        expected = self.project.lineage_target
        def adapter(_argv, **kwargs):
            req = json.loads(kwargs["input_data"])
            self.assertEqual(req, {"op": "status", "branch": "topic", "sha": "b" * 40,
                                   "lineage_target": expected})
            return Result(0, json.dumps({"checks": [{"check": "ci", "state": "success",
                                                      "sha": "b" * 40}],
                                         "lineage": make_fact()}), "", "exited", 0)
        with patch.object(vcs, "run", side_effect=adapter):
            result = reader.handle("vcs_read", {"op": "status", "publication_id": publication_id})
        self.assertEqual(result["result"]["checks"][0]["check"], "ci")
        self.assertEqual(json.loads((reader.run_dir / result["evidence"]).read_text()), result)
        return result["result"]["lineage"]

    def test_publication_and_fresh_status(self):
        config, role, publisher, published = self.publish(self.fact)
        receipt = Publication.read_receipt(self.project.root, published["publication_id"])
        self.assertEqual(receipt["lineage"]["target_oid"], "a" * 40)
        # Idempotent replay never replaces the first observation.
        with patch.object(vcs, "run", side_effect=AssertionError("unexpected dispatch")):
            replay = publisher.handle("vcs_publish", {"op": "push", "branch": "topic"})
        self.assertEqual(replay["lineage"], receipt["lineage"])
        cases = [
            (self.fact, "ready", "target_ancestor"),
            (lambda: self.fact(target_oid="c" * 40, merge_bases=["c" * 40]), "ready", "target_ancestor"),
            (lambda: self.fact(target_oid="c" * 40, merge_bases=["a" * 40], target_is_ancestor=False), "unknown", "not_ancestor"),
            (lambda: self.fact(repository="2" * 64), "unknown", "observation_invalid"),
            (lambda: None, "unknown", "observation_missing"),
            (lambda: self.fact(merge_bases=["a" * 40, "c" * 40]), "unknown", "observation_invalid"),
            (lambda: self.fact(published_oid="c" * 40), "unknown", "observation_invalid"),
            (lambda: self.fact(remote="upstream"), "unknown", "observation_invalid"),
            (lambda: self.fact(target_ref="refs/heads/other"), "unknown", "observation_invalid"),
            (lambda: self.fact(observed_at="2025-01-01T00:00:00"), "unknown", "observation_invalid"),
            (lambda: self.fact(observed_at="2020-01-01T00:00:00+00:00"), "unknown", "observation_invalid"),
            (lambda: self.fact(merge_bases=["c" * 40], target_is_ancestor=False), "unknown", "ancestry_conflict"),
        ]
        for fact, expected, reason in cases:
            with self.subTest(reason=reason):
                observed = []
                def capture():
                    observation = fact()
                    observed.append(observation)
                    return observation
                outcome = self.read(config, role, published["publication_id"], capture)
                observation = observed[0]
                self.assertEqual((outcome["status"], outcome["reason"]), (expected, reason))
                self.assertEqual(outcome["receipt"], receipt["lineage"])
                moved = observation is not None and observation["target_oid"] != "a" * 40
                historical = ("stale", "target_moved") if moved else (expected, reason)
                self.assertEqual((outcome["receipt_status"], outcome["receipt_reason"]), historical)
                if reason not in ("observation_missing", "observation_invalid"):
                    bound = outcome["observation"]
                    self.assertIsNotNone(bound)
                    self.assertEqual(bound["schema"], 2)
                    for key in ("repository", "remote", "target_ref", "target_oid",
                                "published_oid", "merge_bases", "target_is_ancestor",
                                "observed_at"):
                        self.assertEqual(bound[key], observation[key])
                    expected_target = self.project.lineage_target
                    self.assertEqual(bound["expected_repository"], expected_target["repository"])
                    self.assertEqual(bound["expected_remote"], expected_target["remote"])
                    self.assertEqual(bound["expected_target_ref"], expected_target["target_ref"])
                    self.assertEqual(bound["expected_version"], expected_target["version"])
                else:
                    self.assertIsNone(outcome["observation"])

    def test_missing_publication_observation_is_unknown(self):
        config, role, _, published = self.publish(lambda: None)
        receipt = Publication.read_receipt(self.project.root, published["publication_id"])
        self.assertNotIn("lineage", receipt)
        result = self.read(config, role, published["publication_id"], self.fact)
        self.assertEqual((result["status"], result["reason"], result["receipt_status"],
                          result["receipt_reason"]),
                         ("unknown", "receipt_missing_or_invalid", "unknown", "receipt_missing_or_invalid"))
        self.assertIsNone(result["receipt"])
        self.assertIsNotNone(result["observation"])

    def test_non_ancestor_receipt_never_becomes_ready(self):
        config, role, _, published = self.publish(
            lambda: self.fact(merge_bases=["c" * 40], target_is_ancestor=False))
        result = self.read(config, role, published["publication_id"], self.fact)
        self.assertEqual((result["status"], result["reason"]), ("unknown", "ancestry_conflict"))
        unchanged = self.read(config, role, published["publication_id"],
                              lambda: self.fact(merge_bases=["c" * 40], target_is_ancestor=False))
        self.assertEqual((unchanged["status"], unchanged["reason"], unchanged["receipt_status"]),
                         ("unknown", "not_ancestor", "unknown"))
        moved = self.read(config, role, published["publication_id"],
                          lambda: self.fact(target_oid="c" * 40, merge_bases=["c" * 40]))
        self.assertEqual((moved["status"], moved["reason"]), ("ready", "target_ancestor"))
        self.assertEqual((moved["receipt_status"], moved["receipt_reason"]), ("stale", "target_moved"))

    def test_malformed_publication_does_not_fail_push(self):
        config, role, _, published = self.publish(lambda: self.fact(merge_bases=[]))
        self.assertNotIn("lineage", Publication.read_receipt(self.project.root, published["publication_id"]))
        self.assertEqual(self.read(config, role, published["publication_id"], self.fact)["status"], "unknown")

    def test_malformed_retained_lineage_does_not_break_ci(self):
        config, role, _, published = self.publish(self.fact)
        publication_id = published["publication_id"]
        store = Publication(self.project.root, self.source_commit, current_digest(self), "topic", "push",
                            projection=public_source_identity(
                                self.project.snapshots.capture_files(self.project.workspace)["files"]))
        self.assertEqual(store.id, publication_id)
        receipt = json.loads(store.path("receipt").read_text())
        receipt["lineage"]["merge_bases"] = ["a" * 40, "c" * 40]
        store.path("receipt").write_text(json.dumps(receipt))
        outcome = self.read(config, role, publication_id, self.fact)
        self.assertEqual((outcome["status"], outcome["reason"]),
                         ("unknown", "receipt_missing_or_invalid"))

    def test_offline_git_ancestry_and_target_movement(self):
        import datetime as dt
        workspace = self.project.workspace
        def git(*args):
            return subprocess.run(["git", "-C", str(workspace), *args], env=git_env(),
                                  check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True).stdout.strip()
        target = self.source_commit
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Publish")
        published_sha = git("rev-parse", "HEAD")
        config, role = with_caps(self, "worker", ["vcs_publish", "vcs_read"])
        expected = {"repository": "1" * 64, "remote": "origin",
                    "target_ref": "refs/heads/main", "version": 1}
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        set_project_target(self, expected)
        approve(self, "topic", current_digest(self))
        publisher = make_context(self, config, role)
        reader = make_context(self, config, role)

        def adapter(_argv, **kwargs):
            req = json.loads(kwargs["input_data"])
            assert req.get("lineage_target") == expected, req
            bases = git("merge-base", "--all", target, published_sha).splitlines()
            fact = {"schema": 1, "repository": "1" * 64, "remote": "origin", "target_ref": "refs/heads/main",
                    "target_oid": target, "published_oid": published_sha, "merge_bases": bases,
                    "target_is_ancestor": bases == [target],
                    "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            if req["op"] == "status":
                response = {"checks": [], "lineage": fact}
            else:
                response = {**{k: req[k] for k in ("digest", "projection_schema",
                            "projection_algorithm", "projection_digest", "publication_id")},
                            "published_sha": published_sha, "lineage": fact}
            return Result(0, json.dumps(response), "", "exited", 0)

        with patch.object(vcs, "run", side_effect=adapter):
            publication = publisher.handle("vcs_publish", {"op": "push", "branch": "topic"})
            args = {"op": "status", "publication_id": publication["publication_id"]}
            self.assertEqual(reader.handle("vcs_read", args)["result"]["lineage"]["status"], "ready")
            target = published_sha
            moved = reader.handle("vcs_read", args)["result"]["lineage"]
            self.assertEqual((moved["status"], moved["receipt_status"]), ("ready", "stale"))
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Move target")
            target = git("rev-parse", "HEAD")
            moved = reader.handle("vcs_read", args)["result"]["lineage"]
            self.assertEqual((moved["status"], moved["reason"], moved["receipt_status"]),
                             ("unknown", "not_ancestor", "stale"))

class LineageTargetConfiguredTests(PublishFixture):
    """Configured promotion target: binding, requests, staleness and fallbacks."""

    def fact(self, **changes):
        import datetime as dt
        return {**{"schema": 1, "repository": "1" * 64, "remote": "origin",
                   "target_ref": "refs/heads/main", "target_oid": "a" * 40,
                   "published_oid": "b" * 40, "merge_bases": ["a" * 40],
                   "target_is_ancestor": True,
                   "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}, **changes}

    def target(self, **changes):
        base = {"repository": "1" * 64, "remote": "origin",
                "target_ref": "refs/heads/main", "version": 1}
        base.update(changes)
        return base

    def publish_with(self, make_fact, target):
        config, role = with_caps(self, "worker", ["vcs_publish", "vcs_read"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        set_project_target(self, target)
        approve(self, "topic", current_digest(self))
        publisher = make_context(self, config, role)

        def adapter(_argv, **kwargs):
            req = json.loads(kwargs["input_data"])
            if target is None:
                self.assertNotIn("lineage_target", req)
            else:
                self.assertEqual(req.get("lineage_target"), target)
            return Result(0, json.dumps({**{k: req[k] for k in (
                "digest", "projection_schema", "projection_algorithm", "projection_digest",
                "publication_id")}, "published_sha": "b" * 40,
                "lineage": make_fact()}), "", "exited", 0)
        with patch.object(vcs, "run", side_effect=adapter):
            published = publisher.handle("vcs_publish", {"op": "push", "branch": "topic"})
        return config, role, publisher, published

    def status_with(self, config, role, publication_id, make_fact, expect_request_target=True):
        reader = make_context(self, config, role)
        expected = self.project.lineage_target

        def adapter(_argv, **kwargs):
            req = json.loads(kwargs["input_data"])
            if expect_request_target and expected is not None:
                self.assertEqual(req.get("lineage_target"), expected)
            else:
                # Missing descriptor carries no target in the request.
                self.assertNotIn("lineage_target", req)
            return Result(0, json.dumps({"checks": [{"check": "ci", "state": "success",
                                                      "sha": "b" * 40}],
                                         "lineage": make_fact()}), "", "exited", 0)
        with patch.object(vcs, "run", side_effect=adapter):
            result = reader.handle("vcs_read", {"op": "status", "publication_id": publication_id})
        return result["result"]["lineage"]

    def test_configured_main_binds_descriptor_and_readies(self):
        expected = self.target()
        config, role, _, published = self.publish_with(self.fact, expected)
        receipt = Publication.read_receipt(self.project.root, published["publication_id"])
        self.assertEqual(receipt["lineage"]["schema"], 2)
        self.assertEqual(receipt["lineage"]["expected_repository"], expected["repository"])
        self.assertEqual(receipt["lineage"]["expected_target_ref"], expected["target_ref"])
        self.assertEqual(receipt["lineage"]["expected_version"], 1)
        lineage = self.status_with(config, role, published["publication_id"], self.fact)
        self.assertEqual((lineage["status"], lineage["reason"]), ("ready", "target_ancestor"))
        self.assertEqual(lineage["observation"]["schema"], 2)

    def test_configured_release_ref_readies(self):
        expected = self.target(target_ref="refs/heads/release-1")
        fact = lambda **c: self.fact(target_ref="refs/heads/release-1", **c)
        config, role, _, published = self.publish_with(fact, expected)
        receipt = Publication.read_receipt(self.project.root, published["publication_id"])
        self.assertEqual(receipt["lineage"]["target_ref"], "refs/heads/release-1")
        lineage = self.status_with(config, role, published["publication_id"], fact)
        self.assertEqual((lineage["status"], lineage["reason"]), ("ready", "target_ancestor"))

    def test_missing_descriptor_leaves_unknown_but_publish_and_ci_succeed(self):
        config, role, _, published = self.publish_with(self.fact, None)
        # Ordinary publish still succeeds without a descriptor.
        self.assertTrue(published["published"])
        receipt = Publication.read_receipt(self.project.root, published["publication_id"])
        # Without a descriptor the new fact stays unbound legacy (or absent if mismatched).
        # Here the adapter returned origin/main so it is stored unbound.
        self.assertIn("lineage", receipt)
        self.assertEqual(receipt["lineage"]["schema"], 1)
        lineage = self.status_with(config, role, published["publication_id"], self.fact,
                                   expect_request_target=False)
        self.assertEqual((lineage["status"], lineage["reason"]), ("unknown", "unconfigured"))
        # CI checks are still present alongside the unknown lineage.
        reader = make_context(self, config, role)
        with patch.object(vcs, "run", side_effect=lambda _a, **k: Result(0, json.dumps({"checks": [{"check": "ci", "state": "success", "sha": "b" * 40}]}), "", "exited", 0)):
            out = reader.handle("vcs_read", {"op": "status", "publication_id": published["publication_id"]})
        self.assertEqual(out["result"]["checks"][0]["check"], "ci")
        self.assertEqual(out["result"]["lineage"]["status"], "unknown")

    def test_unconfigured_adapter_cannot_self_bind_a_future_target(self):
        from mizu import lineage as lineage_module
        target = self.target()
        self_bound = lineage_module.bind(self.fact(), "b" * 40, target)
        self.assertIsNotNone(self_bound)
        config, role, _, published = self.publish_with(lambda: self_bound, None)
        receipt = Publication.read_receipt(self.project.root, published["publication_id"])
        self.assertNotIn("lineage", receipt)
        set_project_target(self, target)
        configured = config
        current = self.status_with(configured, role, published["publication_id"], self.fact)
        self.assertEqual((current["status"], current["reason"]),
                         ("unknown", "receipt_missing_or_invalid"))

    def test_wrong_repository_is_invalid_not_ready(self):
        expected = self.target()
        config, role, _, published = self.publish_with(self.fact, expected)
        lineage = self.status_with(config, role, published["publication_id"],
                                   lambda: self.fact(repository="2" * 64))
        self.assertEqual((lineage["status"], lineage["reason"]), ("unknown", "observation_invalid"))
        self.assertIsNone(lineage["observation"])

    def test_wrong_ref_is_invalid_not_ready(self):
        expected = self.target()
        config, role, _, published = self.publish_with(self.fact, expected)
        lineage = self.status_with(config, role, published["publication_id"],
                                   lambda: self.fact(target_ref="refs/heads/other"))
        self.assertEqual((lineage["status"], lineage["reason"]), ("unknown", "observation_invalid"))

    def test_target_movement_marks_stale_but_stays_ready(self):
        expected = self.target()
        config, role, _, published = self.publish_with(self.fact, expected)
        lineage = self.status_with(config, role, published["publication_id"],
                                   lambda: self.fact(target_oid="c" * 40, merge_bases=["c" * 40]))
        self.assertEqual((lineage["status"], lineage["reason"]), ("ready", "target_ancestor"))
        self.assertEqual((lineage["receipt_status"], lineage["receipt_reason"]), ("stale", "target_moved"))

    def test_descriptor_ref_change_makes_prior_unusable(self):
        old = self.target()
        config, role, _, published = self.publish_with(self.fact, old)
        # Operator reconfigures to a release ref; prior main-bound receipt cannot ready.
        new = self.target(target_ref="refs/heads/release-1")
        set_project_target(self, new)
        config2 = config
        lineage = self.status_with(config2, role, published["publication_id"], self.fact)
        self.assertEqual((lineage["status"], lineage["reason"]), ("unknown", "descriptor_mismatch"))
        self.assertEqual((lineage["receipt_status"], lineage["receipt_reason"]), ("stale", "descriptor_mismatch"))

    def test_descriptor_version_bump_makes_prior_stale(self):
        old = self.target(version=1)
        config, role, _, published = self.publish_with(self.fact, old)
        new = self.target(version=2)
        set_project_target(self, new)
        config2 = config
        lineage = self.status_with(config2, role, published["publication_id"], self.fact)
        self.assertEqual((lineage["status"], lineage["reason"]), ("unknown", "descriptor_mismatch"))
        self.assertEqual((lineage["receipt_status"], lineage["receipt_reason"]), ("stale", "descriptor_mismatch"))

    def test_unbound_legacy_never_establishes_readiness(self):
        # Publish without a descriptor to mint a legacy unbound receipt.
        config0, _, _, published = self.publish_with(self.fact, None)
        receipt = Publication.read_receipt(self.project.root, published["publication_id"])
        self.assertEqual(receipt["lineage"]["schema"], 1)
        # Later configuration cannot retroactively ready it.
        expected = self.target()
        set_project_target(self, expected)
        config = config0
        role = config0.roles if hasattr(config0, "roles") else None
        # Reuse a worker role with read grant.
        _, read_role = with_caps(self, "worker", ["vcs_read"])
        lineage = self.status_with(config, read_role, published["publication_id"], self.fact)
        self.assertEqual((lineage["status"], lineage["reason"]), ("unknown", "unbound"))

    def test_unrelated_publication_without_lineage_succeeds(self):
        expected = self.target()
        config, role, _, published = self.publish_with(lambda: None, expected)
        self.assertTrue(published["published"])
        receipt = Publication.read_receipt(self.project.root, published["publication_id"])
        self.assertNotIn("lineage", receipt)
        lineage = self.status_with(config, role, published["publication_id"], self.fact)
        self.assertEqual((lineage["status"], lineage["reason"]), ("unknown", "receipt_missing_or_invalid"))

    def test_two_projects_share_adapter_but_bind_independent_targets(self):
        from mizu.project import initialize
        from concurrent.futures import ThreadPoolExecutor

        second = initialize(self.config, "other", self.root / "source", self.goal,
                            ["worker"], ["python3 -m unittest"])
        second.set_control(armed=True, paused=False, wake_generation="test-wake")
        first = self.project
        config, role = with_caps(self, "worker", ["vcs_publish", "vcs_read"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        targets = [self.target(), self.target(repository="2" * 64,
                                               remote="upstream", target_ref="refs/heads/release-1")]
        contexts = []
        for project, target in zip((first, second), targets):
            self.project = project
            set_project_target(self, target)
            if project.name == "other":
                init_publish_git(self)
            approve(self, "topic", current_digest(self))
            contexts.append(make_context(self, config, role))

        seen = []
        def adapter(_argv, **kwargs):
            req = json.loads(kwargs["input_data"])
            target = req["lineage_target"]
            seen.append((req["op"], target))
            fact = self.fact(repository=target["repository"], remote=target["remote"],
                             target_ref=target["target_ref"])
            if req["op"] == "status":
                data = {"checks": [{"check": "ci", "state": "success", "sha": "b" * 40}],
                        "lineage": fact}
            else:
                data = {**{k: req[k] for k in ("digest", "projection_schema", "projection_algorithm",
                                                "projection_digest", "publication_id")},
                        "published_sha": "b" * 40, "lineage": fact}
            return Result(0, json.dumps(data), "", "exited", 0)

        with patch.object(vcs, "run", side_effect=adapter):
            publications = [ctx.handle("vcs_publish", {"op": op, "branch": "topic"})
                            for ctx, op in zip(contexts, ("push", "pr"))]
            # Both projects remain live on the same configuration while reads overlap.
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda pair: pair[0].handle(
                    "vcs_read", {"op": "status", "publication_id": pair[1]["publication_id"]}),
                    zip(contexts, publications)))
        for ctx, target, publication, result in zip(contexts, targets, publications, results):
            receipt = Publication.read_receipt(ctx.project.root, publication["publication_id"])
            self.assertEqual(receipt["lineage"]["expected_repository"], target["repository"])
            self.assertEqual(receipt["lineage"]["expected_target_ref"], target["target_ref"])
            self.assertEqual(result["result"]["lineage"]["status"], "ready")
            self.assertEqual(result["result"]["lineage"]["observation"]["expected_remote"], target["remote"])
        self.assertCountEqual(seen, [("push", targets[0]), ("pr", targets[1]),
                                    ("status", targets[0]), ("status", targets[1])])

    def test_lineage_target_config_validation(self):
        from mizu.config import load
        from mizu.errors import ConfigError
        from mizu.project import Project
        path = self.project.root / "project.toml"
        base = path.read_text()
        self.assertIsNone(self.project.lineage_target)
        # Valid main and release targets load.
        for ref in ("refs/heads/main", "refs/heads/release-1"):
            text = base + f'\n[vcs.lineage_target]\nrepository = "{"1" * 64}"\nremote = "origin"\ntarget_ref = "{ref}"\nversion = 1\n'
            path.write_text(text)
            loaded = Project(self.config, self.project.name)
            self.assertEqual(loaded.lineage_target["target_ref"], ref)
        # Missing keys, unknown keys, bad values and implicit defaults are refused.
        bad_fragments = [
            'repository = "1" * 64',  # invalid TOML type context, still fails load
            '[vcs.lineage_target]\nrepository = "1"\nremote = "origin"\ntarget_ref = "refs/heads/main"\nversion = 1\n',
            '[vcs.lineage_target]\nrepository = "' + "1" * 64 + '"\nremote = ""\ntarget_ref = "refs/heads/main"\nversion = 1\n',
            '[vcs.lineage_target]\nrepository = "' + "1" * 64 + '"\nremote = "origin"\ntarget_ref = "main"\nversion = 1\n',
            '[vcs.lineage_target]\nrepository = "' + "1" * 64 + '"\nremote = "--upload-pack"\ntarget_ref = "refs/heads/main"\nversion = 1\n',
            '[vcs.lineage_target]\nrepository = "' + "1" * 64 + '"\nremote = "origin"\ntarget_ref = "refs/heads/a..b"\nversion = 1\n',
            '[vcs.lineage_target]\nrepository = "' + "1" * 64 + '"\nremote = "origin"\ntarget_ref = "refs/heads/.hidden"\nversion = 1\n',
            '[vcs.lineage_target]\nrepository = "' + "1" * 64 + '"\nremote = "origin"\ntarget_ref = "refs/heads/main"\nversion = true\n',
            '[vcs.lineage_target]\nrepository = "' + "1" * 64 + '"\nremote = "origin"\ntarget_ref = "refs/heads/main"\nversion = 0\n',
            '[vcs.lineage_target]\nrepository = "' + "1" * 64 + '"\nremote = "origin"\ntarget_ref = "refs/heads/main"\nversion = 1\nextra = 1\n',
            '[vcs.lineage_target]\nrepository = "' + "1" * 64 + '"\nremote = "origin"\nversion = 1\n',
        ]
        for frag in bad_fragments:
            with self.subTest(frag=frag[:40]):
                path.write_text(base + "\n" + frag + "\n")
                with self.assertRaises(ConfigError):
                    Project(self.config, self.project.name)
        # No implicit origin/main: a table missing remote/ref is refused (covered above),
        # and an empty table is refused.
        path.write_text(base + "\n[vcs.lineage_target]\n")
        with self.assertRaises(ConfigError):
            Project(self.config, self.project.name)
        for fragment in ("\n[vcs]\nunknown = 1\n", "\nvcs = 1\n",
                         "\n[vcs]\nlineage_target = 1\n"):
            path.write_text(base + fragment)
            for read_only in (False, True):
                with self.subTest(fragment=fragment, read_only=read_only):
                    with self.assertRaises(ConfigError):
                        Project(self.config, self.project.name, read_only=read_only)
        self.file.write_text(self.file.read_text() + "\n[vcs.lineage_target]\n")
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_project_target_survives_backup_restore(self):
        from mizu.project import Project
        from mizu.storage import backup, restore

        target = self.target(target_ref="refs/heads/release-1")
        set_project_target(self, target)
        self.project.set_control(paused=True)
        archive = self.root / "lineage-backup.tar.gz"
        backup(self.project, archive)
        restore(self.config, "lineage-restored", archive)
        restored = Project(self.config, "lineage-restored")
        self.assertEqual(restored.lineage_target, target)
