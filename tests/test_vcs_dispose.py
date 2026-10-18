"""Proposal disposition: close/merge behind grant plus GO approval. Offline only."""
import dataclasses
import json
import sys
import unittest
import uuid
from unittest.mock import ANY, patch

from support import Fixture
from mizu import vcs
from mizu.errors import ConfigError, Denied
from mizu.fs import mkdir
from mizu.runtime import Context, check_consult_role

SHA_A = "a" * 40
SHA_B = "b" * 40
BASE_A = "c" * 40
BASE_B = "d" * 40
TARGET = "main"
TARGET_OTHER = "release/2"
PROPOSAL_ID = "forge:owner/repo#1"


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


def dispose_code(payload):
    import tempfile
    from pathlib import Path
    path = Path(tempfile.mkdtemp()) / "disposed.json"
    path.write_text(json.dumps(payload))
    return ("import sys,json; req=json.load(sys.stdin); "
            "print(open(%r).read())" % str(path))


def granted(code, close=True, merge=True):
    settings = adapter_settings(code)
    settings["close_grant"] = close
    settings["merge_grant"] = merge
    return settings


def echo(identity=PROPOSAL_ID, sha=SHA_A, state="closed",
         base=BASE_A, target=TARGET):
    return {"id": identity, "sha": sha, "base": base,
            "target": target, "state": state}


def close_args(**over):
    args = {"op": "close", "id": PROPOSAL_ID, "sha": SHA_A,
            "base": BASE_A, "target": TARGET, "branch": "mizu/x-1"}
    args.update(over)
    return args


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


def approve(fixture, branch, digest, target=None, proposal_id=None,
              sha=None, base=None):
    body = f"Ship it.\ndigest: {digest}\n"
    if target is not None:
        body += f"target: {target}\n"
    if proposal_id is not None:
        body += f"proposal: {proposal_id}\n"
    if sha is not None:
        body += f"sha: {sha}\n"
    if base is not None:
        body += f"base: {base}\n"
    record = fixture.project.insights.submit(
        source="operator", title=f"GO {branch}", body=body,
        base_snapshot=None, run=None)
    fixture.project.insights.decide(record["id"], "accept", "reviewed", "", "operator",
                                      expected_rev=record["rev"])
    return record


class VcsDisposeTests(Fixture):
    def test_dispose_refused_without_grant(self):
        with self.assertRaisesRegex(Denied, "not granted"):
            vcs.dispose_via(adapter_settings(dispose_code(echo())),
                            "close", PROPOSAL_ID, SHA_A, BASE_A, TARGET)

    def test_unknown_op_refused(self):
        with self.assertRaises(Denied):
            vcs.dispose_via(granted(dispose_code(echo())),
                            "reopen", PROPOSAL_ID, SHA_A, BASE_A, TARGET)

    def test_bad_params_refused_before_dispatch(self):
        settings = {"command": ["/nonexistent-adapter"],
                    "timeout_seconds": 5, "max_bytes": 524288,
                    "close_grant": True, "merge_grant": True}
        for op, identity, sha, base, target in (
                ("close", "", SHA_A, BASE_A, TARGET),
                ("close", PROPOSAL_ID, "xyz", BASE_A, TARGET),
                ("close", PROPOSAL_ID, SHA_A, "xyz", TARGET),
                ("close", PROPOSAL_ID, SHA_A, BASE_A, "../escape"),
                ("close", PROPOSAL_ID, SHA_A, BASE_A, None),
                ("close", None, SHA_A, BASE_A, TARGET)):
            with self.assertRaises(Denied):
                vcs.dispose_via(settings, op, identity, sha, base, target)

    def test_close_receipt(self):
        out = vcs.dispose_via(granted(dispose_code(echo())),
                              "close", PROPOSAL_ID, SHA_A, BASE_A, TARGET)
        self.assertEqual(out, {"op": "close", "id": PROPOSAL_ID,
                               "sha": SHA_A, "base": BASE_A,
                               "target": TARGET, "state": "closed",
                               "trust": "external-untrusted"})

    def test_merge_receipt(self):
        out = vcs.dispose_via(
            granted(dispose_code(echo(state="merged"))),
            "merge", PROPOSAL_ID, SHA_A, BASE_A, TARGET)
        self.assertEqual(out["state"], "merged")

    def test_grants_are_per_action(self):
        # Close and merge policies are distinct: each grant enables only
        # its own action, never the other.
        with self.assertRaisesRegex(Denied, "not granted"):
            vcs.dispose_via(granted(dispose_code(echo()), close=False),
                            "close", PROPOSAL_ID, SHA_A, BASE_A, TARGET)
        with self.assertRaisesRegex(Denied, "not granted"):
            vcs.dispose_via(granted(dispose_code(echo(state="merged")), merge=False),
                            "merge", PROPOSAL_ID, SHA_A, BASE_A, TARGET)
        out = vcs.dispose_via(granted(dispose_code(echo()), merge=False),
                              "close", PROPOSAL_ID, SHA_A, BASE_A, TARGET)
        self.assertEqual(out["state"], "closed")

    def test_moved_head_fails_closed(self):
        settings = granted(dispose_code(echo(sha=SHA_B)))
        with self.assertRaisesRegex(Denied, "expected sha"):
            vcs.dispose_via(settings, "close", PROPOSAL_ID, SHA_A, BASE_A, TARGET)

    def test_moved_base_or_target_fails_closed(self):
        settings = granted(dispose_code(echo(base=BASE_B)))
        with self.assertRaisesRegex(Denied, "expected base and target"):
            vcs.dispose_via(settings, "close", PROPOSAL_ID, SHA_A, BASE_A, TARGET)
        settings = granted(dispose_code(echo(target=TARGET_OTHER)))
        with self.assertRaisesRegex(Denied, "expected base and target"):
            vcs.dispose_via(settings, "close", PROPOSAL_ID, SHA_A, BASE_A, TARGET)

    def test_externally_superseded_fails_closed(self):
        # The forge resolved the proposal another way: a close must not
        # claim a merge, and a merge must not claim a close.
        with self.assertRaisesRegex(Denied, "terminal"):
            vcs.dispose_via(granted(dispose_code(echo(state="merged"))),
                            "close", PROPOSAL_ID, SHA_A, BASE_A, TARGET)
        with self.assertRaisesRegex(Denied, "terminal"):
            vcs.dispose_via(granted(dispose_code(echo(state="closed"))),
                            "merge", PROPOSAL_ID, SHA_A, BASE_A, TARGET)
        # Still open means the action did not take.
        with self.assertRaisesRegex(Denied, "terminal"):
            vcs.dispose_via(granted(dispose_code(echo(state="open"))),
                            "merge", PROPOSAL_ID, SHA_A, BASE_A, TARGET)

    def test_malformed_echo_refused(self):
        full = {"id": PROPOSAL_ID, "sha": SHA_A, "base": BASE_A,
                "target": TARGET, "state": "closed"}
        partial = {"id": PROPOSAL_ID, "sha": SHA_A, "state": "closed"}
        for payload in (partial,
                        {**full, "extra": 1},
                        {**full, "state": "wip"},
                        {**full, "state": None},
                        ["closed"]):
            with self.assertRaises(Denied):
                vcs.dispose_via(granted(dispose_code(payload)),
                                "close", PROPOSAL_ID, SHA_A, BASE_A, TARGET)

    def test_routine_close_resolves_without_approval(self):
        # Close is routine terminal reconciliation behind close_grant
        # alone: no GO approval is required and none is recorded.
        config, role = with_caps(self, "worker", ["vcs_dispose"])
        config = dataclasses.replace(config, vcs=granted(dispose_code(echo())))
        ctx = make_context(self, config, role)
        out = ctx.handle("vcs_dispose", close_args())
        self.assertTrue(out["disposed"])
        self.assertEqual(out["state"], "closed")
        self.assertEqual(out["base"], BASE_A)
        self.assertEqual(out["target"], TARGET)
        self.assertNotIn("approval", out)
        evidence = json.loads((ctx.run_dir / "vcs-dispose.json").read_text())
        self.assertEqual(evidence["state"], "closed")
        self.assertNotIn("approval", evidence)

    def test_unapproved_merge_refused_without_spawn(self):
        # Merge promotes the integrated tree: without a matching GO
        # approval the adapter must never spawn (no evidence file).
        config, role = with_caps(self, "worker", ["vcs_dispose"])
        settings = granted(dispose_code(echo(state="merged")))
        config = dataclasses.replace(config, vcs=settings)
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "recorded human approval"):
            ctx.handle("vcs_dispose", close_args(op="merge"))
        self.assertFalse((ctx.run_dir / "vcs-dispose.json").exists())

    def test_stale_digest_refused(self):
        config, role = with_caps(self, "worker", ["vcs_dispose"])
        config = dataclasses.replace(
            config, vcs=granted(dispose_code(echo(state="merged"))))
        approve(self, "main", "0" * 64)
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "stale"):
            ctx.handle("vcs_dispose", close_args(op="merge", branch="main",
                                                 target="main"))

    def test_approved_merge_resolves(self):
        config, role = with_caps(self, "worker", ["vcs_dispose"])
        config = dataclasses.replace(
            config, vcs=granted(dispose_code(echo(state="merged"))))
        digest = current_digest(self)
        rec = approve(self, "main", digest, target="main",
                      proposal_id=PROPOSAL_ID, sha=SHA_A, base=BASE_A)
        ctx = make_context(self, config, role)
        out = ctx.handle("vcs_dispose", close_args(op="merge", branch="main",
                                                   target="main"))
        self.assertTrue(out["disposed"])
        self.assertEqual(out["state"], "merged")
        self.assertEqual(out["approval"], rec["id"])
        self.assertEqual(out["code_digest"], digest)
        evidence = json.loads((ctx.run_dir / "vcs-dispose.json").read_text())
        self.assertEqual(evidence["approval"], rec["id"])
        self.assertEqual(evidence["state"], "merged")


class VcsDisposeRoleTests(Fixture):
    def test_dispose_role_must_not_hold_decide(self):
        text = self.file.read_text()
        needle = '[roles.maintainer]'
        segment = text.split(needle, 1)[1].split('[roles.', 1)[0]
        self.assertIn('"decide"', segment)
        patched = text.replace(
            'capabilities = ["files", "read", "diff", "exec", "experiment", "verify", "insights", "decide", "consult", "finish"]',
            'capabilities = ["files", "read", "diff", "exec", "experiment", "verify", "insights", "decide", "consult", "finish", "vcs_dispose"]',
            1)
        if patched == text:
            self.skipTest("maintainer capability line shape changed")
        path = self.root / "config/dispose-decide.toml"
        path.write_text(patched)
        from mizu.config import load
        with self.assertRaisesRegex(ConfigError, "self-approval"):
            load(path)

    def test_dispose_role_may_submit_insights(self):
        text = self.file.read_text()
        needle = '[roles.maintainer]'
        segment = text.split(needle, 1)[1].split('[roles.', 1)[0]
        patched = text.replace(
            'capabilities = ["files", "read", "diff", "exec", "experiment", "verify", "insights", "decide", "consult", "finish"]',
            'capabilities = ["files", "read", "diff", "exec", "experiment", "verify", "insights", "submit_insight", "consult", "finish", "vcs_dispose"]',
            1)
        if patched == text:
            self.skipTest("maintainer capability line shape changed")
        path = self.root / "config/dispose-submit.toml"
        path.write_text(patched)
        from mizu.config import load
        config = load(path)
        self.assertIn("vcs_dispose", config.roles["maintainer"].capabilities)
        self.assertIn("submit_insight", config.roles["maintainer"].capabilities)

    def test_dispose_grants_load(self):
        text = self.file.read_text()
        self.assertIn("[vcs]", text)
        path = self.root / "config/dispose-grant.toml"
        path.write_text(text.replace("close_grant = false",
                                     "close_grant = true", 1))
        from mizu.config import load
        config = load(path)
        self.assertTrue(config.vcs["close_grant"])
        self.assertFalse(config.vcs["merge_grant"])
        bad = self.root / "config/dispose-grant-bad.toml"
        bad.write_text(text.replace("close_grant = false",
                                    "close_grant = \"yes\"", 1))
        with self.assertRaises(ConfigError):
            load(bad)
        stale = self.root / "config/dispose-grant-stale.toml"
        stale.write_text(text.replace("retire_grant = false",
                                      "retire_grant = false\ndispose_grant = true", 1))
        with self.assertRaisesRegex(ConfigError, "Unknown keys"):
            load(stale)

    def test_consult_cannot_hold_dispose(self):
        base = self.config.roles["consult"]
        bad = dataclasses.replace(base, capabilities=("files", "read", "finish", "vcs_dispose"))
        with self.assertRaises(ConfigError):
            check_consult_role("consult", bad)


if __name__ == "__main__":
    raise SystemExit(unittest.main())


class VcsDisposeRecoveryTests(Fixture):
    def test_changed_head_retry_recovers(self):
        # The assessed head moved between observation and disposition:
        # the first attempt fails closed with no evidence, and a retry
        # at the re-observed head succeeds. The adapter answers a stale
        # sha with the moved head and confirms the fresh one.
        code = ("import sys,json; req=json.load(sys.stdin); "
                "echo = %r if req['sha'] == %r else req['sha']; "
                "print(json.dumps({'id': req['id'], 'sha': echo, "
                "'base': req['base'], 'target': req['target'], "
                "'state': 'closed'}))" % (SHA_B, SHA_A))
        config, role = with_caps(self, "worker", ["vcs_dispose"])
        config = dataclasses.replace(config, vcs=granted(code))
        ctx = make_context(self, config, role)
        args = close_args()
        with self.assertRaisesRegex(Denied, "expected sha"):
            ctx.handle("vcs_dispose", dict(args))
        self.assertFalse((ctx.run_dir / "vcs-dispose.json").exists())
        out = ctx.handle("vcs_dispose", {**args, "sha": SHA_B})
        self.assertTrue(out["disposed"])
        self.assertEqual(out["sha"], SHA_B)
        self.assertEqual(out["state"], "closed")

CLOSE_IF_PINS = {
    "id": PROPOSAL_ID,
    "head_sha": SHA_A,
    "base_sha": BASE_A,
    "target_repo": "forge:owner/repo",
    "target_ref": TARGET,
    "target_sha": SHA_B,
}


def close_if_args(**over):
    return {"op": "close_if_adopted", **CLOSE_IF_PINS, **over}


def close_if_code(payload, marker=None):
    expected = {"op": "close_if_adopted", **CLOSE_IF_PINS}
    code = ("import sys,json; request=json.load(sys.stdin); "
            "assert request == %r; " % expected)
    if marker is not None:
        code += ("from pathlib import Path; marker=Path(%r); "
                 "count=int(marker.read_text()) if marker.exists() else 0; "
                 "marker.write_text(str(count+1)); " % str(marker))
    return code + "print(%r)" % json.dumps(payload)


def close_if_receipt(*, closed, **over):
    return {**CLOSE_IF_PINS,
            "predicate": "changed-path-tree-entries/v1",
            "closed": closed, "reason": "adopted" if closed else "not_adopted",
            **over}


class VcsCloseIfAdoptedTests(Fixture):
    def context(self, code, *, close_grant=True, capabilities=True):
        extra = ["vcs_dispose"] if capabilities else []
        config, role = with_caps(self, "worker", extra)
        if not capabilities:
            role = dataclasses.replace(role, capabilities=tuple(
                cap for cap in role.capabilities if cap != "vcs_dispose"))
            config = dataclasses.replace(config,
                                         roles={**config.roles, role.name: role})
        settings = granted(code, close=close_grant)
        config = dataclasses.replace(config, vcs=settings)
        return make_context(self, config, role)

    def test_conditional_close_is_one_shot_and_preserves_first_success_receipt(self):
        success = close_if_receipt(closed=True)
        refusal = close_if_receipt(closed=False, reason="already-terminal")
        ctx = self.context(close_if_code(success))
        with patch.object(vcs, "invoke", side_effect=[success, refusal]) as invoke:
            first = ctx.handle("vcs_dispose", close_if_args())
            with self.assertRaisesRegex(Denied, "already attempted"):
                ctx.handle("vcs_dispose", close_if_args())
        invoke.assert_called_once()
        self.assertEqual(invoke.call_args.args[0], ctx.config.vcs)
        self.assertEqual(invoke.call_args.args[1], close_if_args())
        self.assertTrue(first["closed"])
        evidence = json.loads((ctx.run_dir / "vcs-dispose.json").read_text())
        self.assertEqual(evidence, first)
        self.assertTrue(evidence["closed"])

    def test_invalid_conditional_close_pins_do_not_consume_one_shot_dispatch(self):
        success = close_if_receipt(closed=True)
        ctx = self.context(close_if_code(success))
        with self.assertRaises(Denied):
            ctx.handle("vcs_dispose", close_if_args(head_sha="invalid"))
        with patch.object(vcs, "invoke", return_value=success) as invoke:
            result = ctx.handle("vcs_dispose", close_if_args())
        invoke.assert_called_once()
        self.assertEqual(invoke.call_args.args[0], ctx.config.vcs)
        self.assertEqual(invoke.call_args.args[1], close_if_args())
        self.assertTrue(result["closed"])

    def test_conditional_close_refusal_is_returned_and_retained(self):
        payload = close_if_receipt(
            closed=False, reason="close_adopted_disabled_or_not_adopted")
        calls = self.root / "adapter-calls.txt"
        ctx = self.context(close_if_code(payload, calls))
        out = ctx.handle("vcs_dispose", close_if_args())
        self.assertFalse(out["closed"])
        self.assertFalse(out["disposed"])
        self.assertEqual(out["predicate"], "changed-path-tree-entries/v1")
        self.assertEqual(out["adapter_receipt"], payload)
        evidence = json.loads((ctx.run_dir / "vcs-dispose.json").read_text())
        self.assertEqual(evidence["adapter_receipt"], payload)
        self.assertEqual(evidence["id"], CLOSE_IF_PINS["id"])
        self.assertFalse(evidence["closed"])
        self.assertEqual(calls.read_text(), "1")

    def test_conditional_close_retains_truncated_mismatch_receipt(self):
        payload = close_if_receipt(
            closed=False, reason="tree_mismatch",
            mismatched_paths=[f"src/example{i}.py" for i in range(50)],
            mismatched_count=51)
        calls = self.root / "adapter-calls.txt"
        ctx = self.context(close_if_code(payload, calls))
        out = ctx.handle("vcs_dispose", close_if_args())
        self.assertFalse(out["closed"])
        self.assertFalse(out["disposed"])
        self.assertEqual(out["adapter_receipt"], payload)
        self.assertEqual(json.loads((ctx.run_dir / "vcs-dispose.json").read_text()), out)
        self.assertEqual(calls.read_text(), "1")

    def test_conditional_close_success_retains_adapter_receipt(self):
        payload = close_if_receipt(closed=True)
        ctx = self.context(close_if_code(payload))
        out = ctx.handle("vcs_dispose", close_if_args())
        self.assertTrue(out["closed"])
        self.assertTrue(out["disposed"])
        self.assertEqual(out["adapter_receipt"], payload)
        self.assertEqual(json.loads((ctx.run_dir / "vcs-dispose.json").read_text()), out)

    def test_conditional_close_requires_mizu_close_grant(self):
        self.assertFalse(self.config.vcs["close_grant"])
        with self.assertRaisesRegex(Denied, "not granted"):
            vcs.close_if_adopted_via(adapter_settings("raise SystemExit(77)"),
                                     dict(CLOSE_IF_PINS))
        ctx = self.context(close_if_code(close_if_receipt(closed=True)),
                           close_grant=False)
        with self.assertRaisesRegex(Denied, "not granted"):
            ctx.handle("vcs_dispose", close_if_args())
        self.assertFalse((ctx.run_dir / "vcs-dispose.json").exists())

    def test_conditional_close_requires_role_capability(self):
        ctx = self.context(close_if_code(close_if_receipt(closed=True)),
                           capabilities=False)
        with self.assertRaisesRegex(Denied, "no capability: vcs_dispose"):
            ctx.handle("vcs_dispose", close_if_args())
        self.assertFalse((ctx.run_dir / "vcs-dispose.json").exists())

    def assert_receipt_denied(self, payload):
        # Parser matrix only: no host adapter, and no subprocess per case.
        with patch.object(vcs, "invoke", return_value=payload) as invoke:
            with self.assertRaises(Denied):
                vcs.close_if_adopted_via({"close_grant": True}, dict(CLOSE_IF_PINS))
            invoke.assert_called_once_with({"close_grant": True}, close_if_args(),
                                                cancel=ANY)

    def test_conditional_close_requires_every_receipt_field(self):
        valid = close_if_receipt(closed=False)
        for key in valid:
            with self.subTest(missing=key):
                self.assert_receipt_denied({k: v for k, v in valid.items() if k != key})

    def test_conditional_close_requires_exact_pin_echoes(self):
        valid = close_if_receipt(closed=False)
        for key, value in CLOSE_IF_PINS.items():
            for bad in (None, True, 1, [], {}, "", value + "stale", "x" * 4097):
                with self.subTest(pin=key, value=bad):
                    self.assert_receipt_denied({**valid, key: bad})

    def test_conditional_close_rejects_malformed_receipt(self):
        valid = close_if_receipt(closed=False)
        cases = [None, [], [valid], "receipt", 0, False]
        cases += [{**valid, "predicate": value} for value in
                  (None, True, 1, [], {}, "", "changed-path-tree-entries/v2",
                   "changed-path-tree-entries/v1 ")]
        cases += [{**valid, "closed": value} for value in
                  (None, 0, 1, "true", "false", [], {})]
        cases += [{**valid, "reason": value} for value in
                  (None, True, 1, [], {}, "", "   ", "x" * 4097)]
        # Do not admit extra fields, even plausible adapter metadata.
        cases += [{**valid, key: value} for key, value in
                  (("unexpected", True), ("op", "close_if_adopted"),
                   ("trust", "trusted"), ("mismatched_count", 1),
                   ("mismatched_paths", []))]
        for index, payload in enumerate(cases):
            with self.subTest(case=index):
                self.assert_receipt_denied(payload)

    def test_conditional_close_rejects_bad_mismatch_evidence(self):
        valid = close_if_receipt(closed=False)
        cases = [(value, 0) for value in (None, True, 0, "path.py", {})]
        cases += [([], value) for value in
                  (None, True, False, "0", 0.0, [], {}, -1, 2001)]
        # More listed paths than the mismatch count, more than 50 paths,
        # and an out-of-range count are invalid. Partial lists are allowed.
        cases += [(["path.py"], 0), (["path0.py", "path1.py"], 1),
                  ([f"path{i}.py" for i in range(51)], 2000),
                  ([f"path{i}.py" for i in range(50)], 2001),
                  (["path.py"], True), (["path.py"], 1.0),
                  (["same.py", "same.py"], 2)]
        cases += [([value], 1) for value in
                  (None, True, 1, [], {}, "", "p" * 4097)]
        for index, (paths, count) in enumerate(cases):
            with self.subTest(case=index):
                self.assert_receipt_denied(
                    {**valid, "mismatched_paths": paths, "mismatched_count": count})

    def test_conditional_close_rejects_terminal_controls(self):
        for char in ("\x00", "\n", "\r", "\t", "\x1b", "\x7f", "\x85", "\u202e"):
            with self.subTest(control=repr(char)):
                self.assert_receipt_denied(close_if_receipt(
                    closed=False, reason="reason" + char))
                self.assert_receipt_denied(close_if_receipt(
                    closed=False, mismatched_paths=["path" + char], mismatched_count=1))

    def test_conditional_close_rejects_closed_with_mismatches(self):
        for count, listed in ((1, 0), (1, 1), (50, 1), (51, 0), (51, 50), (2000, 0)):
            with self.subTest(count=count, listed=listed):
                self.assert_receipt_denied(close_if_receipt(
                    closed=True, mismatched_count=count,
                    mismatched_paths=[f"path{i}.py" for i in range(listed)]))

    def test_conditional_close_accepts_partial_mismatch_lists_without_altering_receipt(self):
        for count, listed in ((0, 0), (1, 1), (49, 1), (50, 1),
                              (51, 1), (51, 0), (2000, 50)):
            payload = close_if_receipt(
                closed=False, mismatched_count=count,
                mismatched_paths=[f"src/example{i}.py" for i in range(listed)])
            with self.subTest(count=count, listed=listed), patch.object(
                    vcs, "invoke", return_value=payload):
                out = vcs.close_if_adopted_via({"close_grant": True}, dict(CLOSE_IF_PINS))
                self.assertFalse(out["closed"])
                self.assertEqual(out["adapter_receipt"], payload)
                self.assertEqual(out["trust"], "external-untrusted")
        payload = close_if_receipt(closed=True, mismatched_paths=[], mismatched_count=0)
        with patch.object(vcs, "invoke", return_value=payload):
            out = vcs.close_if_adopted_via({"close_grant": True}, dict(CLOSE_IF_PINS))
            self.assertTrue(out["closed"])
            self.assertEqual(out["adapter_receipt"], payload)

    def test_conditional_close_accepts_reason_and_path_size_limits(self):
        payload = close_if_receipt(
            closed=False, reason="理由" * 2048,
            mismatched_paths=["路" * 4096], mismatched_count=1)
        with patch.object(vcs, "invoke", return_value=payload):
            out = vcs.close_if_adopted_via({"close_grant": True}, dict(CLOSE_IF_PINS))
            self.assertEqual(out["adapter_receipt"], payload)

    def test_conditional_close_malformed_refusal_is_not_recorded(self):
        calls = self.root / "adapter-calls.txt"
        ctx = self.context(close_if_code(close_if_receipt(closed=False, reason=""), calls))
        with self.assertRaises(Denied):
            ctx.handle("vcs_dispose", close_if_args())
        self.assertFalse((ctx.run_dir / "vcs-dispose.json").exists())
        self.assertEqual(calls.read_text(), "1")

    def test_conditional_close_tool_schema_accepts_only_action_fields(self):
        from mizu.protocol import DEFINITIONS, validate
        validate(close_if_args(), DEFINITIONS["vcs_dispose"][1])
        with self.assertRaises(Denied):
            validate({**close_if_args(), "unexpected": True},
                     DEFINITIONS["vcs_dispose"][1])

    def test_conditional_close_rejects_invalid_pins_before_adapter(self):
        settings = {"command": ["/nonexistent-adapter"],
                    "timeout_seconds": 5, "max_bytes": 524288,
                    "close_grant": True}
        invalid = (
            {**CLOSE_IF_PINS, "head_sha": "stale"},
            {**CLOSE_IF_PINS, "target_ref": "../main"},
            {**CLOSE_IF_PINS, "target_repo": ""},
            {**CLOSE_IF_PINS, "unexpected": True},
        )
        for pins in invalid:
            with self.subTest(pins=pins), self.assertRaises(Denied):
                vcs.close_if_adopted_via(settings, pins)
