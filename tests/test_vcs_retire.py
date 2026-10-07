"""Branch lifecycle classification plus owned-branch retirement. Offline only."""
import dataclasses
import sys
import unittest
import uuid

from support import Fixture
from mizu import vcs
from mizu.errors import ConfigError, Denied
from mizu.fs import mkdir
from mizu.runtime import Context, check_consult_role

SHA_A = "a" * 40
SHA_B = "b" * 40


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


RETIRE_OK = ("import sys,json; req=json.load(sys.stdin); "
             "print(json.dumps({'op': req['op'], 'branch': req['branch'], "
             "'sha': req['expected_sha'], 'deleted': True}))")
RETIRE_WRONG_SHA = ("import sys,json; req=json.load(sys.stdin); "
             "print(json.dumps({'op': req['op'], 'branch': req['branch'], "
             "'sha': '%s', 'deleted': True}))" % SHA_B)
RETIRE_SILENT = ("import sys,json; req=json.load(sys.stdin); "
             "print(json.dumps({'op': req['op'], 'branch': req['branch']}))")


def grant_settings(code):
    settings = adapter_settings(code)
    settings.update({"retire_grant": True, "owned_prefixes": ["mizu/"],
                     "protected_refs": ["main"]})
    return settings


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


class ClassifyTests(Fixture):
    def test_classes(self):
        policy = {"owned_prefixes": ["mizu/"], "protected_refs": ["main", "release/1"]}
        self.assertEqual(vcs.classify_branch("mizu/x-1", **policy), "owned")
        self.assertEqual(vcs.classify_branch("refs/heads/mizu/x-1", **policy), "owned")
        self.assertEqual(vcs.classify_branch("main", **policy), "protected")
        self.assertEqual(vcs.classify_branch("release/1", **policy), "protected")
        self.assertEqual(vcs.classify_branch("refs/remotes/upstream/main", **policy), "tracking")
        self.assertEqual(vcs.classify_branch("refs/pull/1/head", **policy), "external")
        self.assertEqual(vcs.classify_branch("feature", **policy), "other")

    def test_protected_wins_over_owned(self):
        self.assertEqual(vcs.classify_branch(
            "mizu/main", owned_prefixes=["mizu/"],
            protected_refs=["mizu/main"]), "protected")

    def test_prefix_matches_components_not_strings(self):
        self.assertEqual(vcs.classify_branch("mizu-x", owned_prefixes=["mizu"]), "other")
        self.assertEqual(vcs.classify_branch("mizu/x", owned_prefixes=["mizu"]), "owned")

    def test_bad_refs_and_policy_refused(self):
        for bad in ("", "../escape", "a:b", "x\\y", "refs/remotes/"):
            with self.assertRaises(Denied, msg=bad):
                vcs.classify_branch(bad, owned_prefixes=[], protected_refs=[])
        with self.assertRaises(Denied):
            vcs.classify_branch("mizu/x", owned_prefixes=[""], protected_refs=[])
        with self.assertRaises(Denied):
            vcs.classify_branch("mizu/x", owned_prefixes="../out", protected_refs=[])


class RetireGrantTests(Fixture):
    def test_grant_missing_refuses_without_spawn(self):
        settings = grant_settings(RETIRE_OK)
        settings["retire_grant"] = False
        with self.assertRaisesRegex(Denied, "not granted"):
            vcs.retire_via(settings, "mizu/x-1", SHA_A)

    def test_non_owned_preserved(self):
        for branch, want in (("main", "protected"), ("feature", "other"),
                             ("refs/remotes/upstream/main", "tracking"),
                             ("refs/pull/1/head", "external")):
            with self.assertRaisesRegex(Denied, want, msg=branch):
                vcs.retire_via(grant_settings(RETIRE_OK), branch, SHA_A)

    def test_bad_sha_refused(self):
        with self.assertRaises(Denied):
            vcs.retire_via(grant_settings(RETIRE_OK), "mizu/x-1", "not-a-sha")

    def test_echo_mismatch_refused(self):
        with self.assertRaisesRegex(Denied, "confirm"):
            vcs.retire_via(grant_settings(RETIRE_WRONG_SHA), "mizu/x-1", SHA_A)
        with self.assertRaisesRegex(Denied, "confirm"):
            vcs.retire_via(grant_settings(RETIRE_SILENT), "mizu/x-1", SHA_A)

    def test_owned_retire_receipt(self):
        code = ("import sys,json; req=json.load(sys.stdin); "
                "print(json.dumps({'op': req['op'], 'branch': req['branch'], "
                "'sha': req['expected_sha'], 'deleted': True}))")
        out = vcs.retire_via(grant_settings(code), "mizu/x-1", SHA_A)
        self.assertEqual(out, {"branch": "mizu/x-1", "sha": SHA_A, "deleted": True,
                               "classification": "owned", "trust": "external-untrusted"})


class RetireOpTests(Fixture):
    def test_runtime_op_retires_owned_with_evidence(self):
        config, role = with_caps(self, "worker", ["vcs_retire"])
        config = dataclasses.replace(config, vcs=grant_settings(RETIRE_OK))
        ctx = make_context(self, config, role)
        out = ctx.handle("vcs_retire", {"branch": "mizu/x-1", "expected_sha": SHA_A})
        self.assertTrue(out["retired"])
        self.assertEqual(out["classification"], "owned")
        self.assertTrue((ctx.run_dir / "vcs-retire.json").exists())

    def test_runtime_op_preserves_protected_without_spawn(self):
        config, role = with_caps(self, "worker", ["vcs_retire"])
        config = dataclasses.replace(config, vcs=grant_settings(RETIRE_OK))
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "protected"):
            ctx.handle("vcs_retire", {"branch": "main", "expected_sha": SHA_A})
        self.assertFalse((ctx.run_dir / "vcs-retire.json").exists())

    def test_consult_roles_cannot_retire(self):
        with self.assertRaises(ConfigError):
            check_consult_role("reviewer", type("R", (), {
                "workspace": "read",
                "capabilities": ("files", "read", "vcs_retire", "finish")})())


class RetireConfigTests(Fixture):
    def test_defaults_fail_closed(self):
        self.assertIs(self.config.vcs["retire_grant"], False)
        self.assertEqual(list(self.config.vcs["owned_prefixes"]), [])
        self.assertEqual(list(self.config.vcs["protected_refs"]), [])

    def _with_vcs_lines(self, *lines):
        text = self.file.read_text()
        kept = [ln for ln in text.splitlines(keepends=True) if "[vcs]" not in ln]
        kept += ["[vcs]\n"] + [ln + "\n" for ln in lines]
        self.file.write_text("".join(kept))

    def test_load_accepts_and_refuses_retire_policy(self):
        from mizu.config import load
        self._with_vcs_lines('retire_grant = true', 'owned_prefixes = ["mizu/"]',
                             'protected_refs = ["main"]')
        cfg = load(self.file)
        self.assertTrue(cfg.vcs["retire_grant"])
        self.assertEqual(list(cfg.vcs["owned_prefixes"]), ["mizu/"])
        self.assertEqual(list(cfg.vcs["protected_refs"]), ["main"])
        bad_sets = (('retire_grant = "yes"',),
                    ('owned_prefixes = [""]',),
                    ('owned_prefixes = ["../out"]',),
                    ('owned_prefixes = "mizu/"',),
                    ('protected_refs = ["has space"]',),
                    ('retire_unknown = true',))
        for lns in bad_sets:
            self._with_vcs_lines(*lns)
            with self.assertRaises(ConfigError, msg=lns[0]):
                load(self.file)

    def test_load_refuses_retire_with_self_approval(self):
        from mizu.config import load
        text = self.file.read_text()
        body = text.split("[roles.worker]")[1].split("[roles.")[0]
        # The worker already holds decide, so granting retire must fail load.
        self.assertIn('"decide"', body)
        body = body.replace('capabilities = [',
                            'capabilities = ["vcs_retire", ', 1)
        self.file.write_text(text + "\n[roles.retirer]" + body)
        with self.assertRaisesRegex(ConfigError, "vcs_retire"):
            load(self.file)
