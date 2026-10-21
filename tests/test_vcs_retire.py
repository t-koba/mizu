"""Branch lifecycle classification plus owned-branch retirement. Offline only."""
import dataclasses
import json
import os
import sys
import unittest
import uuid

from support import Fixture, ROOT, host_child_cost
from mizu import vcs
from mizu.errors import ConfigError, Denied
from mizu.fs import mkdir
from mizu.runtime import Context, check_consult_role

SHA_A = "a" * 40
SHA_B = "b" * 40

#: The provider is a plain child of this interpreter, so this host's own
#: cost of starting one says how long the provider may take to answer. A
#: fixed number of seconds cannot say that: on a loaded host the provider
#: needs longer than the grant it was given, the product refuses with a
#: timeout, and the refusal no longer names the blockers the provider
#: reported.
PROVIDER_TIMEOUT_FLOOR_SECONDS = 30.0
PROVIDER_TIMEOUT_MULTIPLIER = 8.0
#: The ceiling keeps a provider that never answers from eating the check
#: phase's own budget, and stays far below it.
PROVIDER_TIMEOUT_CEILING_SECONDS = 120.0
#: How many times this host's child cost is sampled: the grant has to
#: cover the sample this host was slowest at.
PROVIDER_COST_SAMPLES = 3


def provider_timeout_seconds(cost: float) -> float:
    """Return the provider's grant for a host that costs ``cost`` seconds."""
    return min(PROVIDER_TIMEOUT_CEILING_SECONDS,
               max(PROVIDER_TIMEOUT_FLOOR_SECONDS, cost * PROVIDER_TIMEOUT_MULTIPLIER))


_provider_cost = None


def provider_child_cost() -> float:
    """Return this host's measured cost of the provider child.

    Measured once per run, at the slowest of several samples: the grant
    has to cover the sample this host was slowest at.
    """
    global _provider_cost
    if _provider_cost is None:
        _provider_cost = max(host_child_cost() for _ in range(PROVIDER_COST_SAMPLES))
    return _provider_cost


def check_phase_budget_seconds() -> float:
    """Return the check phase's own budget: the bound every grant stays under."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check
        return float(check.DEFAULT_CHECK_TIMEOUT_SECONDS)
    finally:
        sys.path.remove(str(ROOT / "scripts"))


def adapter_settings(code, timeout=None, maximum=524288):
    """Grant the provider child the time this host needs to answer it."""
    if timeout is None:
        timeout = provider_timeout_seconds(provider_child_cost())
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


RETIRE_OK = ("import sys,json; req=json.load(sys.stdin); "
             "print(json.dumps({'op': req['op'], 'branch': req['branch'], "
             "'sha': req['expected_sha'], 'deleted': True, "
             "'live_refs': []}))")
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

    def test_full_form_protected_matches_short_branch(self):
        # Either spelling protects: a full refs/heads entry must protect the
        # short branch from retirement, and vice versa.
        for ref, entry in (("mizu/main", "refs/heads/mizu/main"),
                           ("refs/heads/mizu/main", "mizu/main"),
                           ("refs/heads/mizu/main", "refs/heads/mizu/main")):
            self.assertEqual(vcs.classify_branch(
                ref, owned_prefixes=["mizu/"],
                protected_refs=[entry]), "protected", f"{ref} vs {entry}")
        settings = grant_settings(RETIRE_OK)
        settings["protected_refs"] = ["refs/heads/mizu/main"]
        settings["owned_prefixes"] = ["mizu/"]
        with self.assertRaisesRegex(Denied, "protected"):
            vcs.retire_via(settings, "mizu/main", SHA_A)

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
                "'sha': req['expected_sha'], 'deleted': True, "
                "'live_refs': []}))")
        out = vcs.retire_via(grant_settings(code), "mizu/x-1", SHA_A)
        self.assertEqual(out, {"branch": "mizu/x-1", "sha": SHA_A, "deleted": True,
                               "live_refs": [], "classification": "owned",
                               "trust": "external-untrusted"})

    def test_provider_blockers_refused(self):
        # The fresh provider check is authoritative: reported live refs
        # block even with no recorded observation, and a contradictory
        # deleted:true alongside blockers is still refused.
        # Consume the request before replying: otherwise early pipe closure
        # races the writer and tests input_error instead of response validation.
        template = ("import sys,json; json.load(sys.stdin); "
                    "print(json.dumps({'branch': 'mizu/x-1', "
                    "'sha': %r, 'deleted': %s, "
                    "'live_refs': ['forge:owner/repo#7']}))")
        with self.assertRaisesRegex(Denied, "confirm"):
            vcs.retire_via(grant_settings(template % (SHA_A, False)),
                           "mizu/x-1", SHA_A)
        with self.assertRaisesRegex(Denied, "forge:owner/repo#7"):
            vcs.retire_via(grant_settings(template % (SHA_A, True)),
                           "mizu/x-1", SHA_A)

    def test_provider_timeout_refused(self):
        # The product's own timeout refusal keeps its coverage here: a
        # provider that never answers is refused as a timeout, and the
        # refusal says so instead of naming blockers it never reported.
        # The bound is deliberately short: this test guards the refusal,
        # not the provider's speed.
        settings = grant_settings("import time; time.sleep(30)")
        settings["timeout_seconds"] = 1
        with self.assertRaisesRegex(Denied, "Configured VCS program timed out"):
            vcs.retire_via(settings, "mizu/x-1", SHA_A)

    def test_missing_or_malformed_affirmation_refused(self):
        # A provider that cannot affirm the atomic live-ref check fails
        # closed: the old echo shape without live_refs no longer retires.
        no_key = ("import sys,json; json.load(sys.stdin); "
                  "print(json.dumps({'branch': 'mizu/x-1', 'sha': %r, "
                  "'deleted': True}))" % SHA_A)
        with self.assertRaisesRegex(Denied, "live proposal references"):
            vcs.retire_via(grant_settings(no_key), "mizu/x-1", SHA_A)
        for refs in ("forge:owner/repo#7", [None], [""], ["x" * 129],
                     ["ok", 7]):
            code = ("import sys,json; json.load(sys.stdin); "
                    "print(json.dumps({'branch': 'mizu/x-1', 'sha': %r, "
                    "'deleted': True, 'live_refs': %r}))" % (SHA_A, refs))
            with self.assertRaisesRegex(Denied, "live proposal references",
                                        msg=repr(refs)):
                vcs.retire_via(grant_settings(code), "mizu/x-1", SHA_A)


class ProviderGrantBoundTests(Fixture):
    """The provider's grant follows this host, not a fixed number of seconds."""

    def test_grant_uses_this_host_measured_cost(self):
        # The grant the provider runs under is this host's own measured
        # cost of that child, scaled. A fixed number of seconds is not:
        # on a loaded host the provider needs longer than the grant it
        # was given, and the refusal stops naming the blockers.
        settings = grant_settings(RETIRE_OK)
        cost = provider_child_cost()
        self.assertEqual(settings["timeout_seconds"], provider_timeout_seconds(cost))
        self.assertGreater(settings["timeout_seconds"], cost)
        self.assertGreaterEqual(settings["timeout_seconds"], PROVIDER_TIMEOUT_FLOOR_SECONDS)

    def test_grant_scales_with_the_measured_cost(self):
        # The shape of the derivation, independent of this host: a host
        # that costs more gets a wider grant, an unknown cost falls back
        # to the floor, and no grant reaches the check phase's own budget.
        self.assertEqual(provider_timeout_seconds(0.0), PROVIDER_TIMEOUT_FLOOR_SECONDS)
        self.assertEqual(provider_timeout_seconds(10.0), 10.0 * PROVIDER_TIMEOUT_MULTIPLIER)
        self.assertEqual(provider_timeout_seconds(1000.0), PROVIDER_TIMEOUT_CEILING_SECONDS)
        self.assertLess(provider_timeout_seconds(1.0), provider_timeout_seconds(10.0))
        self.assertLess(PROVIDER_TIMEOUT_CEILING_SECONDS, check_phase_budget_seconds() / 2)

    @unittest.skipUnless(os.environ.get("MIZU_SLOW_PROVIDER"),
                         "opt-in: a provider that answers after the legacy fixed grant")
    def test_refusal_names_the_blockers_of_a_slow_provider(self):
        # The property is the content of the refusal, not the provider's
        # speed: a provider that answers after the legacy fixed grant
        # still names its blockers. Kept out of the default suite path:
        # it waits for the slow provider on every run it is enabled for.
        delay = 6.0
        code = ("import time,sys,json; time.sleep(%r); "
                "print(json.dumps({'branch': 'mizu/x-1', 'sha': %r, "
                "'deleted': True, 'live_refs': ['forge:owner/repo#7']}))"
                % (delay, SHA_A))
        with self.assertRaisesRegex(Denied, "forge:owner/repo#7"):
            vcs.retire_via(grant_settings(code), "mizu/x-1", SHA_A)


class RetireOpTests(Fixture):
    def test_runtime_op_retires_owned_with_evidence(self):
        config, role = with_caps(self, "worker", ["vcs_retire"])
        config = dataclasses.replace(config, vcs=grant_settings(RETIRE_OK))
        ctx = make_context(self, config, role)
        out = ctx.handle("vcs_retire", {"branch": "mizu/x-1", "expected_sha": SHA_A})
        self.assertTrue(out["retired"])
        self.assertEqual(out["classification"], "owned")
        evidence = json.loads((ctx.run_dir / out["evidence"]).read_text())
        self.assertEqual(evidence["sha"], out["sha"])
        # Uniform binding: the file always equals the returned response.
        self.assertEqual(evidence, out)

    def test_runtime_op_preserves_protected_without_spawn(self):
        config, role = with_caps(self, "worker", ["vcs_retire"])
        config = dataclasses.replace(config, vcs=grant_settings(RETIRE_OK))
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "protected"):
            ctx.handle("vcs_retire", {"branch": "main", "expected_sha": SHA_A})
        self.assertEqual(list((ctx.run_dir / "vcs-retire").glob("*.json")), [])

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


def observed_proposal(ref="mizu/x-1", state="open", base_ref="main"):
    return {"id": "forge:owner/repo#1", "state": state,
            "head": {"repo": "owner/repo", "ref": ref, "sha": SHA_A},
            "base": {"repo": "owner/repo", "ref": base_ref, "sha": SHA_B},
            "draft": False, "mergeable": True,
            "checks": [{"check": "unit", "state": "success", "sha": SHA_A}],
            "url": "https://forge/x/pull/1"}


class RetireLiveRefTests(Fixture):
    def retire(self, code=RETIRE_OK):
        config, role = with_caps(self, "worker", ["vcs_retire"])
        config = dataclasses.replace(config, vcs=grant_settings(code))
        return make_context(self, config, role)

    def test_open_head_reference_blocks_without_spawn(self):
        vcs.record_proposal_state(self.project, observed_proposal())
        ctx = self.retire("import sys; sys.exit(3)")
        with self.assertRaisesRegex(Denied, "forge:owner/repo#1"):
            ctx.handle("vcs_retire", {"branch": "mizu/x-1",
                                      "expected_sha": SHA_A})
        self.assertEqual(list((ctx.run_dir / "vcs-retire").glob("*.json")), [])

    def test_full_ref_spelling_blocks(self):
        # Either spelling deletes the same branch, so either spelling
        # must see the live reference: refs/heads/mizu/x-1 blocks
        # exactly like mizu/x-1.
        vcs.record_proposal_state(self.project, observed_proposal())
        self.assertEqual(vcs.live_proposal_refs(self.project, "mizu/x-1"),
                         ["forge:owner/repo#1"])
        self.assertEqual(
            vcs.live_proposal_refs(self.project, "refs/heads/mizu/x-1"),
            ["forge:owner/repo#1"])
        ctx = self.retire()
        with self.assertRaisesRegex(Denied, "forge:owner/repo#1"):
            ctx.handle("vcs_retire", {"branch": "refs/heads/mizu/x-1",
                                      "expected_sha": SHA_A})
        self.assertEqual(list((ctx.run_dir / "vcs-retire").glob("*.json")), [])

    def test_open_base_reference_blocks(self):
        vcs.record_proposal_state(self.project,
                                  observed_proposal(ref="feature",
                                                    base_ref="mizu/x-1"))
        ctx = self.retire()
        with self.assertRaisesRegex(Denied, "open proposal"):
            ctx.handle("vcs_retire", {"branch": "mizu/x-1",
                                      "expected_sha": SHA_A})

    def test_terminal_disposition_unblocks(self):
        vcs.record_proposal_state(self.project, observed_proposal())
        vcs.record_proposal_state(self.project,
                                  observed_proposal(state="merged"))
        ctx = self.retire()
        out = ctx.handle("vcs_retire", {"branch": "mizu/x-1",
                                        "expected_sha": SHA_A})
        self.assertTrue(out["retired"])

    def test_tombstoned_head_does_not_block(self):
        row = observed_proposal()
        row["head"] = {"deleted": True, "repo": "owner/repo", "ref": "mizu/x-1"}
        vcs.record_proposal_state(self.project, row)
        ctx = self.retire()
        out = ctx.handle("vcs_retire", {"branch": "mizu/x-1",
                                        "expected_sha": SHA_A})
        self.assertTrue(out["retired"])

    def test_unrelated_reference_does_not_block(self):
        vcs.record_proposal_state(self.project,
                                  observed_proposal(ref="mizu/other"))
        ctx = self.retire()
        out = ctx.handle("vcs_retire", {"branch": "mizu/x-1",
                                        "expected_sha": SHA_A})
        self.assertTrue(out["retired"])

    def test_ci_records_never_block(self):
        self.project.insights.submit(source="vcs", title="Historical CI failure",
                                     body="An old per-check observation", base_snapshot=None,
                                     insight_id="ci-" + "a" * 32)
        self.assertEqual(vcs.live_proposal_refs(self.project, "mizu/x-1"), [])
