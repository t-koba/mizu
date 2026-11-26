"""Operator `[vcs] default_branch` for CI polling, live refs and sync receipts. Offline only."""
import dataclasses
import json
import re
import sys
import types
import unittest
import uuid

from support import Fixture
from mizu import vcs
from mizu.ci_polling import _live_ci_branches, poll_ci
from mizu.config import load
from mizu.errors import ConfigError
from mizu.fs import mkdir
from mizu.runtime import Context
from mizu.vcs_poll_policy import vcs_default_branch

SHA_A = "a" * 40
SHA_B = "b" * 40
BRANCH = "release/1"


def adapter_settings(code, default_branch):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": 5, "max_bytes": 524288,
            "default_branch": default_branch}


def status_code():
    return ("import sys,json; json.load(sys.stdin); "
            "print(json.dumps({'checks': [], 'trust': 'external-untrusted'}))")


def fetch_code(refs):
    return ("import sys,json; json.load(sys.stdin); "
            "print(json.dumps({'refs': %s}))" % json.dumps(refs))


class DefaultBranchHelperTests(unittest.TestCase):
    def test_configured_branch_and_fail_closed_reads(self):
        self.assertEqual(vcs_default_branch(
            types.SimpleNamespace(vcs={"default_branch": BRANCH})), BRANCH)
        for settings in ({}, {"default_branch": ""}, {"default_branch": None},
                         {"default_branch": 12}, {"default_branch": [BRANCH]}):
            with self.subTest(settings=settings):
                self.assertEqual(vcs_default_branch(
                    types.SimpleNamespace(vcs=settings)), "")
        self.assertEqual(vcs_default_branch(types.SimpleNamespace()), "")
        self.assertEqual(vcs_default_branch(types.SimpleNamespace(vcs=[])), "")


class DefaultBranchConfigTests(Fixture):
    def test_shipped_example_supplies_the_fallback(self):
        self.assertEqual(self.config.vcs["default_branch"], "main")

    def test_load_accepts_empty_and_valid_names_and_rejects_invalid(self):
        pattern = re.compile(r'(?m)^default_branch = .*$')
        base = self.file.read_text()
        self.assertEqual(len(pattern.findall(base)), 1)
        for raw, expected in (('""', ""), (json.dumps(BRANCH), BRANCH),
                              (json.dumps("b" * 256), "b" * 256)):
            with self.subTest(value=raw[:24]):
                self.file.write_text(pattern.sub(f'default_branch = {raw}', base))
                self.assertEqual(load(self.file).vcs["default_branch"], expected)
        for raw in (json.dumps("bad branch"), json.dumps("bad\tbranch"),
                    json.dumps("c" * 257), "12", "[]"):
            with self.subTest(value=raw[:24]):
                self.file.write_text(pattern.sub(f'default_branch = {raw}', base))
                with self.assertRaisesRegex(ConfigError, r"vcs\.default_branch"):
                    load(self.file)


class DefaultBranchPollTests(Fixture):
    def _config(self, default_branch):
        return dataclasses.replace(
            self.config, vcs=adapter_settings(status_code(), default_branch))

    def test_live_inventory_uses_configured_default_only(self):
        config = self._config(BRANCH)
        self.assertEqual(_live_ci_branches(config, self.project), {BRANCH})
        vcs.inject_refs(self.project.root, {"develop": SHA_B})
        self.assertEqual(_live_ci_branches(config, self.project),
                         {"develop", BRANCH})
        empty = self._config("")
        self.assertEqual(_live_ci_branches(empty, self.project), {"develop"})
        vcs.inject_refs(self.project.root, {})
        self.assertEqual(_live_ci_branches(empty, self.project), set())

    def test_empty_workspace_poll_targets_configured_default(self):
        seen = []
        def reader(settings, op, params):
            seen.append(params["branch"])
            return {"checks": [], "trust": "external-untrusted"}
        event = poll_ci(self._config(BRANCH), self.project, {},
                        now=0.0, interval=15.0, reader=reader)
        self.assertTrue(event["ok"])
        self.assertEqual(event["branches"], [BRANCH])
        self.assertEqual(seen, [BRANCH])

    def test_empty_workspace_poll_has_no_target_without_default(self):
        seen = []
        def reader(settings, op, params):
            seen.append(params["branch"])
            return {"checks": [], "trust": "external-untrusted"}
        settings = adapter_settings(status_code(), "")
        unset = {key: value for key, value in settings.items()
                 if key != "default_branch"}
        for vcs_settings in (settings, unset):
            with self.subTest(settings=vcs_settings):
                config = dataclasses.replace(self.config, vcs=vcs_settings)
                event = poll_ci(config, self.project, {},
                                now=0.0, interval=15.0, reader=reader)
                self.assertEqual(event["branches"], [])
                self.assertEqual(seen, [])


class DefaultBranchSyncTests(Fixture):
    def _synced_context(self, refs, default_branch):
        worker = self.config.roles["worker"]
        synced = dataclasses.replace(worker, capabilities=(*worker.capabilities, "sync"))
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(fetch_code(refs), default_branch),
            roles={**self.config.roles, "worker": synced})
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        return Context(config, self.project, synced, run,
                       self.project.snapshots.get(), self.project.workspace)

    def test_receipt_upstream_uses_configured_default(self):
        refs = {"main": SHA_B, BRANCH: SHA_A}
        receipt = self._synced_context(refs, BRANCH).handle("sync", {})
        self.assertEqual(receipt["upstream"], SHA_A)
        self.assertEqual(receipt["refs"], ["main", BRANCH])
        self.assertEqual(set(receipt), {"synced", "injected", "prefix", "trust",
                                        "upstream", "evidence", "refs"})

    def test_receipt_upstream_is_null_without_default(self):
        receipt = self._synced_context({"main": SHA_A}, "").handle("sync", {})
        self.assertIsNone(receipt["upstream"])
        self.assertEqual(receipt["refs"], ["main"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
