"""M2 step 2: normalized CI status plus best-effort daemon CI polling. Offline only."""
import dataclasses
import json
import sys
import unittest

from support import Fixture
from mizu import vcs
from mizu.errors import Denied
from mizu.runtime import poll_ci

SHA_A = "a" * 40
SHA_B = "b" * 40


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


def status_code(payload):
    return ("import sys,json; req=json.load(sys.stdin); "
            "print(json.dumps(%s))" % json.dumps(payload))


class CiStatusTests(Fixture):
    def test_status_normalized_and_failures_recorded(self):
        payload = {"checks": [
            {"check": "unit", "state": "failure", "sha": SHA_A, "url": "https://x/log"},
            {"check": "lint", "state": "success", "sha": SHA_A},
        ]}
        config = dataclasses.replace(self.config, vcs=adapter_settings(status_code(payload)))
        out = vcs.read_via(config.vcs, "status", {"branch": "main"})
        self.assertEqual(out["branch"], "main")
        self.assertEqual(len(out["checks"]), 2)
        self.assertEqual(out["checks"][1]["url"], "")
        self.assertEqual(out["trust"], "external-untrusted")

    def test_status_malformed_refused(self):
        config = dataclasses.replace(self.config, vcs=adapter_settings(status_code({"nope": 1})))
        with self.assertRaises(Denied):
            vcs.read_via(config.vcs, "status", {"branch": "main"})

    def test_status_bad_sha_refused(self):
        payload = {"checks": [{"check": "unit", "state": "failure", "sha": "zz"}]}
        config = dataclasses.replace(self.config, vcs=adapter_settings(status_code(payload)))
        with self.assertRaises(Denied):
            vcs.read_via(config.vcs, "status", {"branch": "main"})

    def test_poll_records_failure_dedupes_and_ignores_pass(self):
        payload = {"checks": [{"check": "unit", "state": "failure", "sha": SHA_A,
                               "url": "https://x/log"}]}
        config = dataclasses.replace(self.config, vcs=adapter_settings(status_code(payload)))
        before = self.project.snapshots.get()["id"]
        state = {}
        first = poll_ci(config, self.project, state, now=0.0, interval=15.0,
                        branches=["main"])
        self.assertTrue(first["ok"])
        self.assertEqual(first["event"], "ci_poll")
        self.assertEqual(first["failures"], 1)
        self.assertEqual(len(first["recorded"]), 1)
        # Not due: no second adapter call.
        calls = []
        def counting(settings, op, params):
            calls.append(params)
            return vcs.read_via(settings, op, params)
        skipped = poll_ci(config, self.project, state, now=14.0, interval=15.0,
                          branches=["main"], reader=counting)
        self.assertIsNone(skipped)
        self.assertEqual(calls, [])
        # Due again: same failure deduplicates to the same insight id.
        second = poll_ci(config, self.project, state, now=15.0, interval=15.0,
                         branches=["main"])
        self.assertTrue(second["ok"])
        self.assertEqual(second["recorded"][0]["id"], first["recorded"][0]["id"])
        pending = [i for i in self.project.insights.list(pending=True, limit=1000)
                   if i["id"] == first["recorded"][0]["id"]]
        self.assertEqual(len(pending), 1)
        # Success-only status records nothing but still polls ok.
        ok_payload = {"checks": [{"check": "unit", "state": "success", "sha": SHA_A}]}
        config2 = dataclasses.replace(self.config, vcs=adapter_settings(status_code(ok_payload)))
        third = poll_ci(config2, self.project, {}, now=30.0, interval=15.0,
                        branches=["main"])
        self.assertTrue(third["ok"])
        self.assertEqual(third["failures"], 0)
        self.assertEqual(third["recorded"], [])
        # Polling never publishes a snapshot.
        self.assertEqual(self.project.snapshots.get()["id"], before)

    def test_poll_disabled_adapter_never_polls(self):
        state = {}
        out = poll_ci(self.config, self.project, state, now=100.0, interval=15.0,
                      branches=["main"])
        self.assertIsNone(out)
        self.assertNotIn("last_poll", state)

    def test_poll_failure_is_best_effort_event(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings("import sys; sys.exit(3)"))
        before = self.project.snapshots.get()["id"]
        state = {}
        event = poll_ci(config, self.project, state, now=50.0, interval=15.0,
                        branches=["main"])
        self.assertEqual(event["event"], "ci_poll")
        self.assertFalse(event["ok"])
        self.assertIn("error", event)
        self.assertEqual(state["last_poll"], 50.0)
        again = poll_ci(config, self.project, state, now=51.0, interval=15.0,
                        branches=["main"])
        self.assertIsNone(again)
        self.assertEqual(self.project.snapshots.get()["id"], before)

    def test_poll_derives_branches_from_injected_refs(self):
        vcs.inject_refs(self.project.workspace, {"other": SHA_B, "main": SHA_A})
        seen = []
        def reader(settings, op, params):
            seen.append(params["branch"])
            return {"checks": [], "trust": "external-untrusted"}
        config = dataclasses.replace(self.config, vcs=adapter_settings(
            status_code({"checks": []})))
        out = poll_ci(config, self.project, {}, now=0.0, interval=15.0, reader=reader)
        self.assertTrue(out["ok"])
        self.assertEqual(out["branches"], ["main", "other"])
        self.assertEqual(sorted(seen), ["main", "other"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
