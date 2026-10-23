"""M2 step 1: vcs_read/vcs_publish split with GO<branch> approval gate. Offline only."""
import dataclasses
import json
import os
import sys
import subprocess
from unittest.mock import patch
import unittest
import uuid

from support import Fixture
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
                "mizu.runtime.write_json", side_effect=fail_observation):
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
