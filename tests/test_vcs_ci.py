"""M2 step 2: normalized CI status plus best-effort daemon CI polling. Offline only."""
import dataclasses
import json
import sys
import unittest
from unittest.mock import patch

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


class CiLogSelectorTests(Fixture):
    def setUp(self):
        super().setUp()
        # Synthetic request capture, not a provider/CI integration test.
        code = ("import sys,json; req=json.load(sys.stdin); "
                "print(json.dumps({'request': req, 'log': 'bounded log', "
                "'trust': 'adapter-supplied', "
                "**({k: req[k] for k in ('check', 'sha')} if 'check' in req else {})}))")
        role = self.config.roles["worker"]
        role = dataclasses.replace(role, capabilities=(*role.capabilities, "vcs_read"))
        self.config = dataclasses.replace(
            self.config, vcs=adapter_settings(code),
            roles={**self.config.roles, "worker": role})
        self.ctx = self.context()

    def test_exact_selector_and_omission_preserve_request_and_response(self):
        for sha in (SHA_A, "b" * 64):
            for selector in ({}, {"check": "x"}, {"check": "é" * 256},
                             {"check": "Offline checks / platform-compatibility (macos-latest, 3.11)"}):
                args = {"op": "log", "branch": "main", "sha": sha, **selector}
                with self.subTest(args=args):
                    out = self.ctx.handle("vcs_read", args)
                    self.assertEqual(out["result"], {
                        "request": args, "log": "bounded log",
                        "trust": "external-untrusted"})

    def test_selected_log_denies_missing_or_mismatched_response_bindings(self):
        args = {"branch": "main", "sha": SHA_A, "check": "unit"}
        payloads = (
            {"log": "ALL JOBS"},  # Adapter ignores the selector.
            {"log": "selected log", "check": "unit"},
            {"log": "selected log", "sha": SHA_A},
            {"log": "selected log", "check": "other", "sha": SHA_A},
            {"log": "selected log", "check": "unit", "sha": SHA_B},
        )
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                    Denied, "Log response must match the requested check and sha"):
                vcs.read_via(adapter_settings(status_code(payload)), "log", args)

    def test_log_response_bindings_are_consumed_only_for_selected_requests(self):
        payload = {"log": "selected log", "check": "unit", "sha": SHA_A,
                   "trust": "adapter-supplied"}
        settings = adapter_settings(status_code(payload))
        for params in ({"check": "unit", "sha": SHA_A}, {"sha": SHA_A}, {}):
            with self.subTest(params=params):
                out = vcs.read_via(settings, "log", {"branch": "main", **params})
                expected = ({"log": "selected log"} if "check" in params else payload)
                self.assertEqual(out, {**expected, "trust": "external-untrusted"})

    def test_invalid_selector_matches_status_name_validation_without_invocation(self):
        invalid = (None, False, 1, [], {}, "", "x" * 257, "ci\r", "ci\n",
                   "ci\x00", "ci\t", "ci\x1b[2K", "ci\x7f", "ci\x85",
                   "ci\u202e", "ci\u200b")
        with patch("mizu.vcs.invoke") as invoke:
            for check in invalid:
                with self.subTest(check=check):
                    args = {"op": "log", "branch": "main", "sha": SHA_A, "check": check}
                    with self.assertRaises(Denied):
                        self.ctx.handle("vcs_read", args)
                    with self.assertRaises(Denied):
                        vcs.read_via(self.config.vcs, "log", args)
                    with self.assertRaises(Denied):
                        vcs.parse_status_checks({"checks": [
                            {"check": check, "state": "success", "sha": SHA_A}]})
            invoke.assert_not_called()

    def test_selector_rejected_on_every_other_read_op(self):
        from mizu.protocol import DEFINITIONS
        ops = DEFINITIONS["vcs_read"][1]["properties"]["op"]["enum"]
        with patch("mizu.vcs.invoke") as invoke:
            for op in ops:
                if op == "log":
                    continue
                with self.subTest(op=op):
                    args = {"op": op, "branch": "main", "sha": SHA_A, "check": "unit"}
                    with self.assertRaisesRegex(Denied, "check is only valid for log"):
                        self.ctx.handle("vcs_read", args)
                    with self.assertRaises(Denied):
                        vcs.read_via(self.config.vcs, op, args)
            invoke.assert_not_called()

    def test_log_preserves_optional_sha_but_selector_requires_exact_sha(self):
        invalid_branches = [{"sha": SHA_A}, {"branch": "../main", "sha": SHA_A},
                            {"branch": "", "sha": SHA_A}]
        invalid_shas = ("", "HEAD", "a" * 39, "a" * 41, "z" * 40, 1)
        with patch("mizu.vcs.invoke") as invoke:
            for params in invalid_branches:
                for selector in ({}, {"check": "unit"}):
                    args = {"op": "log", **params, **selector}
                    with self.subTest(args=args), self.assertRaises(Denied):
                        self.ctx.handle("vcs_read", args)
                    with self.assertRaises(Denied):
                        vcs.read_via(self.config.vcs, "log", args)
            for sha in invalid_shas:
                args = {"op": "log", "branch": "main", "sha": sha}
                with self.subTest(args=args), self.assertRaises(Denied):
                    self.ctx.handle("vcs_read", args)
                with self.assertRaises(Denied):
                    vcs.read_via(self.config.vcs, "log", args)
            with self.assertRaises(Denied):
                self.ctx.handle("vcs_read", {"op": "log", "branch": "main", "check": "unit"})
            with self.assertRaises(Denied):
                vcs.read_via(self.config.vcs, "log", {"branch": "main", "check": "unit"})
            invoke.assert_not_called()
        args = {"op": "log", "branch": "main"}
        self.assertEqual(self.ctx.handle("vcs_read", args)["result"], {
            "request": args, "log": "bounded log", "trust": "external-untrusted"})


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

    def test_terminal_and_bidi_controls_refused(self):
        for check in ("ci\x1b[2K", "ci\r", "ci\u202e", "ci\x7f", "ci\u200b"):
            with self.assertRaises(Denied, msg=repr(check)):
                vcs.parse_status_checks(
                    {"checks": [{"check": check, "state": "success", "sha": SHA_A}]})
        for state in ("suc\x1bcess", "ok\u202e"):
            with self.assertRaises(Denied, msg=repr(state)):
                vcs.parse_status_checks(
                    {"checks": [{"check": "unit", "state": state, "sha": SHA_A}]})
        with self.assertRaises(Denied):
            vcs.parse_status_checks(
                {"checks": [{"check": "unit", "state": "success", "sha": SHA_A,
                             "url": "https://ci/run/1\r\x1b[8m"}]})

    def test_poll_caches_failures_without_creating_insights(self):
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
        self.assertFalse(first["ci_failures_may_be_truncated"])
        self.assertNotIn("recorded", first)
        self.assertEqual(self.project.insights.list(pending=False), [])
        # Not due: no second adapter call.
        calls = []
        def counting(settings, op, params):
            calls.append(params)
            return vcs.read_via(settings, op, params)
        skipped = poll_ci(config, self.project, state, now=14.0, interval=15.0,
                          branches=["main"], reader=counting)
        self.assertIsNone(skipped)
        self.assertEqual(calls, [])
        # A repeat leaves inbox generations unchanged; the cache stays red.
        generation = self.project.insights.generation()
        second = poll_ci(config, self.project, state, now=15.0, interval=15.0,
                         branches=["main"])
        self.assertTrue(second["ok"])
        self.assertEqual(state["failures"], {"main": [{"sha": SHA_A, "check": "unit"}]})
        self.assertEqual(self.project.insights.generation(), generation)
        # Success-only status records nothing but still polls ok.
        ok_payload = {"checks": [{"check": "unit", "state": "success", "sha": SHA_A}]}
        config2 = dataclasses.replace(self.config, vcs=adapter_settings(status_code(ok_payload)))
        third = poll_ci(config2, self.project, {}, now=30.0, interval=15.0,
                        branches=["main"])
        self.assertTrue(third["ok"])
        self.assertEqual(third["failures"], 0)
        self.assertEqual(self.project.insights.list(pending=False), [])
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
        vcs.inject_refs(self.project.root, {"other": SHA_B, "main": SHA_A})
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

    def test_poll_rotates_over_more_refs_than_budget(self):
        refs = {"a": "a" * 40, "b": "b" * 40, "c": "c" * 40, "d": "d" * 40, "main": SHA_A}
        vcs.inject_refs(self.project.root, refs)
        seen = []
        def reader(settings, op, params):
            seen.append(params["branch"])
            return {"checks": [], "trust": "external-untrusted"}
        config = dataclasses.replace(self.config, vcs={**adapter_settings(
            status_code({"checks": []})), "poll_max_branches": 4})
        state: dict = {}
        first = poll_ci(config, self.project, state, now=0.0, interval=15.0, reader=reader)
        self.assertTrue(first["ok"])
        self.assertEqual(first["branches"], ["a", "b", "c", "d"])
        self.assertEqual(first["branches_total"], 5)
        # A second, older fixed slice would repeat a,b,c,d and starve main.
        second = poll_ci(config, self.project, state, now=15.0, interval=15.0, reader=reader)
        self.assertTrue(second["ok"])
        self.assertEqual(second["branches"], ["main", "a", "b", "c"])
        self.assertEqual(second["branches_total"], 5)
        third = poll_ci(config, self.project, state, now=30.0, interval=15.0, reader=reader)
        self.assertEqual(third["branches"], ["d", "main", "a", "b"])
        self.assertEqual(set(seen), {"a", "b", "c", "d", "main"})


if __name__ == "__main__":
    raise SystemExit(unittest.main())

class CiStickyRedTests(Fixture):
    def _wait_snapshot(self):
        import time
        gen = self.project.insights.generation()
        cap = self.project.snapshots.capture_files(self.project.workspace)
        wait = self.project.snapshots.create(
            cap, goal=self.project.goal,
            state="Now: parked\nNext: none\nTasks: none\nBlocking: none",
            run=None, outcome="wait", summary="parked", verification=None,
            wake_at=time.time() + 86400, inbox_seen=gen,
            wake_generation=self.project.control().get("wake_generation", ""))
        self.project.snapshots.publish(wait)
        return self.project.snapshots.get()

    def test_deferred_failure_stays_current_until_green(self):
        import dataclasses
        import time
        from mizu.fs import write_json
        from mizu.runtime import current_ci_failures, poll_ci, should_run, wake_ci_failures
        failing = {"checks": [{"check": "perf audit", "state": "failure",
                               "sha": SHA_A, "url": "https://x/log"}]}
        config = dataclasses.replace(
            self.config, vcs={**adapter_settings(status_code(failing)), "ci_branch": "mizu"})
        self.project.config = config
        snap = self._wait_snapshot()
        self.assertFalse(should_run(self.project, snap, current_time=time.time()))
        state: dict = {}
        event = poll_ci(config, self.project, state, now=0.0, interval=15.0,
                        branches=["mizu"])
        self.assertTrue(event["ok"])
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 0.0, "failures": state.get("failures")})
        self.assertEqual(current_ci_failures(self.project),
                         [{"branch": "mizu", "sha": SHA_A, "check": "perf audit"}])
        # A red head wakes the writer with no insight or decision at all.
        self.assertEqual(self.project.insights.list(pending=False), [])
        parked = self.project.snapshots.get()
        self.assertTrue(current_ci_failures(self.project))
        self.assertTrue(should_run(self.project, parked, current_time=time.time()))
        # Green on the same branch/check clears the fact without any decision.
        passing = {"checks": [{"check": "perf audit", "state": "success", "sha": SHA_A}]}
        config2 = dataclasses.replace(
            self.config, vcs=adapter_settings(status_code(passing)))
        state2: dict = {"failures": state.get("failures")}
        event2 = poll_ci(config2, self.project, state2, now=15.0, interval=15.0,
                         branches=["mizu"])
        self.assertTrue(event2["ok"])
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 15.0, "failures": state2.get("failures")})
        self.assertEqual(current_ci_failures(self.project), [])

    def test_only_selected_branch_wakes_writer(self):
        import dataclasses
        import time
        from mizu.fs import write_json
        from mizu.runtime import current_ci_failures, should_run, wake_ci_failures
        config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "mizu"})
        self.project.config = config
        snap = self._wait_snapshot()
        # Red on an unrelated branch stays visible but never wakes.
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 0.0, "failures": {"alpha": [{"sha": SHA_A, "check": "unit"}],
                                                      "codex/old": [{"sha": SHA_A, "check": "perf"}]}})
        self.assertEqual(len(current_ci_failures(self.project)), 2)
        self.assertEqual(wake_ci_failures(self.project, config), [])
        self.assertFalse(should_run(self.project, snap, current_time=time.time()))
        # Red on the selected branch wakes.
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 1.0, "failures": {"alpha": [{"sha": SHA_A, "check": "unit"}],
                                                      "mizu": [{"sha": SHA_A, "check": "unit"}]}})
        self.assertEqual(wake_ci_failures(self.project, config),
                         [{"branch": "mizu", "sha": SHA_A, "check": "unit"}])
        self.assertTrue(should_run(self.project, snap, current_time=time.time()))

    def test_empty_branch_disables_wake_fail_closed(self):
        import time
        from mizu.fs import write_json
        from mizu.runtime import current_ci_failures, should_run, wake_ci_failures
        # Default fixture has empty ci_branch: any red stays visible, none wakes.
        self.assertEqual(self.project.config.vcs.get("ci_branch"), "")
        snap = self._wait_snapshot()
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 0.0, "failures": {"mizu": [{"sha": SHA_A, "check": "unit"}]}})
        self.assertTrue(current_ci_failures(self.project))
        self.assertEqual(wake_ci_failures(self.project, self.project.config), [])
        self.assertFalse(should_run(self.project, snap, current_time=time.time()))

    def test_superseding_sha_replaces_cached_failure(self):
        import dataclasses
        from mizu.runtime import current_ci_failures, poll_ci
        from mizu.fs import write_json
        red_a = {"checks": [{"check": "unit", "state": "failure", "sha": SHA_A}]}
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(status_code(red_a)))
        state: dict = {}
        poll_ci(config, self.project, state, now=0.0, interval=15.0,
                branches=["main"])
        red_b = {"checks": [{"check": "unit", "state": "failure", "sha": SHA_B}]}
        config2 = dataclasses.replace(
            self.config, vcs=adapter_settings(status_code(red_b)))
        poll_ci(config2, self.project, state, now=15.0, interval=15.0,
                branches=["main"])
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 15.0, "failures": state.get("failures")})
        self.assertEqual(current_ci_failures(self.project),
                         [{"branch": "main", "sha": SHA_B, "check": "unit"}])

    def test_malformed_cache_reads_as_no_failures(self):
        from mizu.fs import write_json
        from mizu.runtime import current_ci_failures
        write_json(self.project.root / "vcs-poll.json",
                   {"failures": {"main": [{"sha": "zz", "check": "unit"}]}})
        self.assertEqual(current_ci_failures(self.project), [])
        write_json(self.project.root / "vcs-poll.json", {"failures": "nope"})
        self.assertEqual(current_ci_failures(self.project), [])


class CiRewakeTests(Fixture):
    """Change-based CI wake: an unchanged red parks until sha/check/head/interval changes."""

    def _wait_snapshot(self):
        import time
        gen = self.project.insights.generation()
        cap = self.project.snapshots.capture_files(self.project.workspace)
        wait = self.project.snapshots.create(
            cap, goal=self.project.goal,
            state="Now: parked\nNext: none\nTasks: none\nBlocking: none",
            run=None, outcome="wait", summary="parked", verification=None,
            wake_at=time.time() + 86400, inbox_seen=gen,
            wake_generation=self.project.control().get("wake_generation", ""))
        self.project.snapshots.publish(wait)
        return self.project.snapshots.get()

    def _red(self, branch="mizu", sha=None, check="unit"):
        import dataclasses
        from mizu.fs import write_json
        sha = SHA_A if sha is None else sha
        config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": branch})
        self.project.config = config
        snap = self._wait_snapshot()
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 1000.0,
                    "failures": {branch: [{"sha": sha, "check": check}]}})
        return config, snap

    def test_identical_red_parks_after_record(self):
        from mizu.runtime import _ci_due_for, should_run, wake_ci_failures, write_ci_wake
        config, snap = self._red()
        base = 2000.0
        self.assertTrue(should_run(self.project, snap, current_time=base,
                                   role_name="worker", config=config))
        present = wake_ci_failures(self.project, config)
        self.assertEqual(len(present), 1)
        write_ci_wake(self.project, "worker", ("mizu",), present, snap["id"], base)
        self.assertFalse(should_run(self.project, snap, current_time=base + 10,
                                    role_name="worker", config=config))
        self.assertEqual(
            _ci_due_for(self.project, config, "worker", snap["id"], base + 10), [])
        # Project-level check (no role) stays conservative: presence still wakes.
        self.assertTrue(should_run(self.project, snap, current_time=base + 10))

    def test_new_sha_wakes(self):
        from mizu.fs import write_json
        from mizu.runtime import should_run, wake_ci_failures, write_ci_wake
        config, snap = self._red(sha=SHA_A)
        base = 2000.0
        write_ci_wake(self.project, "worker", ("mizu",),
                      wake_ci_failures(self.project, config), snap["id"], base)
        self.assertFalse(should_run(self.project, snap, current_time=base + 10,
                                    role_name="worker", config=config))
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 2001.0,
                    "failures": {"mizu": [{"sha": SHA_B, "check": "unit"}]}})
        self.assertTrue(should_run(self.project, snap, current_time=base + 11,
                                   role_name="worker", config=config))

    def test_new_check_wakes(self):
        from mizu.fs import write_json
        from mizu.runtime import should_run, wake_ci_failures, write_ci_wake
        config, snap = self._red(check="unit")
        base = 2000.0
        write_ci_wake(self.project, "worker", ("mizu",),
                      wake_ci_failures(self.project, config), snap["id"], base)
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 2001.0,
                    "failures": {"mizu": [{"sha": SHA_A, "check": "lint"}]}})
        self.assertTrue(should_run(self.project, snap, current_time=base + 11,
                                   role_name="worker", config=config))

    def test_interval_elapsed_wakes(self):
        from mizu.runtime import should_run, wake_ci_failures, write_ci_wake
        config, snap = self._red()
        base = 2000.0
        write_ci_wake(self.project, "worker", ("mizu",),
                      wake_ci_failures(self.project, config), snap["id"], base)
        self.assertFalse(should_run(self.project, snap, current_time=base + 1799,
                                    role_name="worker", config=config))
        self.assertTrue(should_run(self.project, snap, current_time=base + 1800,
                                   role_name="worker", config=config))

    def test_moved_snapshot_wakes(self):
        from mizu.runtime import should_run, wake_ci_failures, write_ci_wake
        config, snap = self._red()
        base = 2000.0
        write_ci_wake(self.project, "worker", ("mizu",),
                      wake_ci_failures(self.project, config), snap["id"], base)
        self.assertFalse(should_run(self.project, snap, current_time=base + 10,
                                    role_name="worker", config=config))
        moved = self._wait_snapshot()
        self.assertNotEqual(moved["id"], snap["id"])
        self.assertTrue(should_run(self.project, moved, current_time=base + 11,
                                   role_name="worker", config=config))

    def test_malformed_record_wakes(self):
        from mizu.fs import write_json
        from mizu.runtime import should_run, wake_ci_failures, write_ci_wake
        config, snap = self._red()
        base = 2000.0
        write_ci_wake(self.project, "worker", ("mizu",),
                      wake_ci_failures(self.project, config), snap["id"], base)
        self.assertFalse(should_run(self.project, snap, current_time=base + 10,
                                    role_name="worker", config=config))
        wake_path = self.project.root / "vcs-ci-wake-worker.json"
        for bad in ({"version": 1, "branch": "mizu", "fact": "nope",
                     "snapshot": snap["id"], "at": base},
                    {"version": 999, "branch": "mizu",
                     "fact": [{"sha": SHA_A, "check": "unit"}],
                     "snapshot": snap["id"], "at": base},
                    {"version": 1, "branch": "mizu",
                     "fact": [{"sha": SHA_A, "check": "unit"}],
                     "snapshot": snap["id"], "at": "bad"},
                    "garbage-string"):
            write_json(wake_path, bad)
            self.assertTrue(should_run(self.project, snap, current_time=base + 10,
                                       role_name="worker", config=config),
                            msg=repr(bad))

    def test_ci_rewake_seconds_load(self):
        from mizu.config import load
        from mizu.errors import ConfigError
        self.assertEqual(self.config.vcs.get("ci_rewake_seconds"), 1800)

        def _with_line(line):
            text = self.file.read_text()
            lines = [ln for ln in text.splitlines(keepends=True)
                     if "ci_rewake_seconds" not in ln]
            if "[vcs]\n" in lines:
                at = lines.index("[vcs]\n") + 1
                lines.insert(at, line + "\n")
            else:
                lines += ["[vcs]\n", line + "\n"]
            self.file.write_text("".join(lines))

        for good in (60, 1800, 86400):
            _with_line(f"ci_rewake_seconds = {good}")
            self.assertEqual(load(self.file).vcs["ci_rewake_seconds"], good)
        for bad in ("ci_rewake_seconds = 0", "ci_rewake_seconds = 59",
                    "ci_rewake_seconds = 86401",
                    'ci_rewake_seconds = "3600"',
                    "ci_rewake_seconds = true",
                    "ci_rewake_seconds = 3.5"):
            _with_line(bad)
            with self.assertRaises(ConfigError, msg=bad):
                load(self.file)

    def test_rewake_seconds_defensive(self):
        import dataclasses
        from mizu.runtime import _ci_rewake_seconds
        self.assertEqual(_ci_rewake_seconds(self.config), 1800)
        for value, expected in ((60, 60.0), (86400, 86400.0),
                                (59, 1800.0), (86401, 1800.0)):
            with self.subTest(value=value):
                configured = dataclasses.replace(
                    self.config, vcs={**self.config.vcs, "ci_rewake_seconds": value})
                self.assertEqual(_ci_rewake_seconds(configured), expected)
        garbage = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_rewake_seconds": "bad"})
        self.assertEqual(_ci_rewake_seconds(garbage), 1800)
        missing = dataclasses.replace(
            self.config,
            vcs={key: value for key, value in self.config.vcs.items()
                 if key != "ci_rewake_seconds"})
        self.assertEqual(_ci_rewake_seconds(missing), 1800)

    def test_dispatch_rewake_suppresses_identical_red(self):
        import dataclasses
        from mizu.fs import read_json, write_json
        from mizu.runtime import Engine
        from support import ScriptDriver
        config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "mizu"})
        self.project.config = config
        self._wait_snapshot()
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 1000.0,
                    "failures": {"mizu": [{"sha": SHA_A, "check": "unit"}]}})
        driver = ScriptDriver()
        first = Engine(config, driver=driver).run(
            self.project, "worker", event_trigger=True)
        self.assertEqual(first.get("status"), "completed")
        started = read_json(
            self.project.root / "runs" / first["run"] / "started.json")
        self.assertEqual(started["admission"], "ci")
        self.assertEqual(started["ci_failures"], 1)
        calls_after_first = len(driver.calls)
        self.assertGreater(calls_after_first, 0)
        second = Engine(config, driver=driver).run(
            self.project, "worker", event_trigger=True)
        self.assertEqual(second.get("skipped"), "no-event")
        self.assertEqual(len(driver.calls), calls_after_first)


class CiCheckTimeTests(Fixture):
    """A provider's check times make the job time of a run derivable."""

    def test_status_carries_bounded_check_times(self):
        rows = vcs.parse_status_checks(
            {"checks": [{"check": "unit", "state": "success", "sha": SHA_A,
                         "started_at": "2026-07-20T05:00:00+00:00",
                         "completed_at": "2026-07-20T05:03:20+00:00"}]})
        self.assertEqual(rows[0]["started_at"], "2026-07-20T05:00:00+00:00")
        self.assertEqual(rows[0]["completed_at"], "2026-07-20T05:03:20+00:00")
        # The job time is derived here, so a consumer need not re-parse the text.
        self.assertEqual(rows[0]["job_seconds"], 200)
        # A check that has not finished may report only its start.
        started = vcs.parse_status_checks(
            {"checks": [{"check": "unit", "state": "pending", "sha": SHA_A,
                         "started_at": "2026-07-20T05:00:00+00:00"}]})
        self.assertNotIn("completed_at", started[0])
        self.assertNotIn("job_seconds", started[0])
        # Absent stays absent (exactly check/state/sha/url).
        plain = vcs.parse_status_checks(
            {"checks": [{"check": "unit", "state": "success", "sha": SHA_A}]})
        self.assertEqual(set(plain[0]), {"check", "state", "sha", "url"})

    def test_status_refuses_a_completion_before_its_start(self):
        for started, completed in (
                ("2026-07-20T05:00:00+00:00", "2026-07-20T04:59:59+00:00"),
                ("2026-07-20T05:00:00+02:00", "2026-07-20T04:00:00+02:00")):
            with self.assertRaises(Denied):
                vcs.parse_status_checks(
                    {"checks": [{"check": "unit", "state": "success", "sha": SHA_A,
                                 "started_at": started, "completed_at": completed}]})
        # The same instants in another offset are the same order, so they pass.
        rows = vcs.parse_status_checks(
            {"checks": [{"check": "unit", "state": "success", "sha": SHA_A,
                         "started_at": "2026-07-20T05:00:00+00:00",
                         "completed_at": "2026-07-20T06:00:00+01:00"}]})
        self.assertEqual(rows[0]["completed_at"], "2026-07-20T06:00:00+01:00")
        # The same instant in another offset: the job time is zero, not an hour.
        self.assertEqual(rows[0]["job_seconds"], 0)
        # A supplied job time is only accepted when it matches the derived one.
        accepted = vcs.parse_status_checks(
            {"checks": [{"check": "unit", "state": "success", "sha": SHA_A,
                         "started_at": "2026-07-20T05:00:00+00:00",
                         "completed_at": "2026-07-20T05:03:20+00:00", "job_seconds": 200}]})
        self.assertEqual(accepted[0]["job_seconds"], 200)
        with self.assertRaises(Denied):
            vcs.parse_status_checks(
                {"checks": [{"check": "unit", "state": "success", "sha": SHA_A,
                             "started_at": "2026-07-20T05:00:00+00:00",
                             "completed_at": "2026-07-20T05:03:20+00:00", "job_seconds": 5}]})
        with self.assertRaises(Denied):
            vcs.parse_status_checks(
                {"checks": [{"check": "unit", "state": "success", "sha": SHA_A,
                             "started_at": "2026-07-20T05:00:00+00:00", "job_seconds": 200}]})
        # A reported job longer than a week is refused rather than believed.
        with self.assertRaises(Denied):
            vcs.parse_status_checks(
                {"checks": [{"check": "unit", "state": "success", "sha": SHA_A,
                             "started_at": "2026-01-01T00:00:00+00:00",
                             "completed_at": "2026-07-20T00:00:00+00:00"}]})

    def test_status_refuses_malformed_check_times(self):
        for bad in ("", "x" * 65, "yesterday", "2026-07-20T05:00:00", 3, None,
                    "2026-07-20T05:00:00+00:00\n"):
            with self.assertRaises(Denied, msg=repr(bad)):
                vcs.parse_status_checks(
                    {"checks": [{"check": "unit", "state": "success", "sha": SHA_A,
                                 "started_at": bad}]})
            with self.assertRaises(Denied, msg=repr(bad)):
                vcs.parse_status_checks(
                    {"checks": [{"check": "unit", "state": "success", "sha": SHA_A,
                                 "completed_at": bad}]})


class CiRunIdentityTests(Fixture):
    """Adapter run identity keys the CI wake fact; absent stays sha-keyed."""

    def test_cached_run_and_check_name_boundaries(self):
        from mizu.ci_observation import _clean_ci_failures, _valid_ci_run

        self.assertEqual(_valid_ci_run('r' * 128), 'r' * 128)
        self.assertIsNone(_valid_ci_run('r' * 129))
        cleaned = _clean_ci_failures({"main": [
            {"sha": SHA_A, "check": "c" * 256, "run": "r" * 128},
            {"sha": SHA_A, "check": "c" * 257, "run": "r" * 128},
        ]})
        self.assertEqual(cleaned, {
            "main": [{"sha": SHA_A, "check": "c" * 256, "run": "r" * 128}],
        })

    def test_status_accepts_valid_run(self):
        rows = vcs.parse_status_checks(
            {"checks": [{"check": "unit", "state": "failure", "sha": SHA_A,
                         "run": "run-123"}]})
        self.assertEqual(rows[0]["run"], "run-123")
        # Absent stays absent (exactly check/state/sha/url).
        plain = vcs.parse_status_checks(
            {"checks": [{"check": "unit", "state": "failure", "sha": SHA_A}]})
        self.assertEqual(set(plain[0]), {"check", "state", "sha", "url"})
        self.assertNotIn("run", plain[0])

    def test_status_run_does_not_establish_a_unique_job_execution(self):
        # The contract allows repeated check names and opaque run identities.
        # Neither a poll nor two same-named jobs gains a job/attempt identity
        # from timestamp parsing; consumers cannot use this tuple as a key
        # for daily cost deduplication.
        row = {"check": "unit", "state": "success", "sha": SHA_A,
               "run": "opaque-workflow-or-job",
               "started_at": "2026-07-20T00:00:00+00:00",
               "completed_at": "2026-07-20T00:01:00+00:00"}
        poll = vcs.parse_status_checks({"checks": [row]})
        repeated_poll = vcs.parse_status_checks({"checks": [dict(row)]})
        two_rows = vcs.parse_status_checks({"checks": [row, dict(row)]})
        self.assertEqual(poll, repeated_poll)
        self.assertEqual(two_rows, poll + repeated_poll)
        self.assertEqual(poll[0]["job_seconds"], 60)
        self.assertEqual(set(poll[0]), set(row) | {"url", "job_seconds"})
        # New structured identity fields require a deliberate schema change,
        # not an adapter injecting unvalidated facts into the closed row set.
        for key, value in (("repository", "b" * 64), ("workflow", "ci"),
                           ("run_id", "workflow-1"), ("job_id", "job-1"),
                           ("attempt", 2)):
            with self.subTest(field=key), self.assertRaises(Denied):
                vcs.parse_status_checks({"checks": [{**row, key: value}]})

    def test_status_refuses_bad_run_and_unknown_keys(self):
        for bad in ("", "x" * 129, "run\n1", "run\x001", 3, None,
                    "ci\x1b[2K", "ci\u202e", "ci\x7f"):
            with self.assertRaises(Denied, msg=repr(bad)):
                vcs.parse_status_checks(
                    {"checks": [{"check": "unit", "state": "failure",
                                 "sha": SHA_A, "run": bad}]})
        with self.assertRaises(Denied):
            vcs.parse_status_checks(
                {"checks": [{"check": "unit", "state": "failure",
                             "sha": SHA_A, "bogus": 1}]})
        with self.assertRaises(Denied):
            vcs.parse_status_checks(
                {"checks": [{"check": "unit", "state": "failure",
                             "sha": SHA_A, "run": "ok", "bogus": 1}]})

    def test_poller_stores_run_in_dedicated_ci_cache(self):
        import dataclasses
        from mizu import vcs as vcs_mod
        from mizu.runtime import current_ci_failures, poll_ci
        payload = {"checks": [{"check": "unit", "state": "failure",
                               "sha": SHA_A, "run": "run-1"}]}
        config = dataclasses.replace(self.config, vcs=adapter_settings(status_code(payload)))
        state: dict = {}
        event = poll_ci(config, self.project, state, now=0.0, interval=15.0,
                        branches=["main"])
        self.assertTrue(event["ok"])
        self.assertEqual(self.project.insights.list(pending=False), [])
        # Failures cache carries the identity.
        self.assertEqual(state["failures"],
                         {"main": [{"sha": SHA_A, "check": "unit", "run": "run-1"}]})
        from mizu.fs import write_json
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 0.0, "failures": state["failures"]})
        self.assertEqual(current_ci_failures(self.project),
                         [{"branch": "main", "sha": SHA_A, "check": "unit",
                           "run": "run-1"}])
        # A rerun advances the dedicated wake identity without an insight.
        payload2 = {"checks": [{"check": "unit", "state": "failure",
                                "sha": SHA_A, "run": "run-2"}]}
        config2 = dataclasses.replace(self.config, vcs=adapter_settings(status_code(payload2)))
        event2 = poll_ci(config2, self.project, state, now=15.0, interval=15.0,
                         branches=["main"])
        self.assertTrue(event2["ok"])
        self.assertEqual(self.project.insights.list(pending=False), [])
        self.assertEqual(state["failures"],
                         {"main": [{"sha": SHA_A, "check": "unit", "run": "run-2"}]})

    def test_clean_drops_invalid_run_but_keeps_row(self):
        from mizu.runtime import _clean_ci_failures
        cleaned = _clean_ci_failures(
            {"main": [{"sha": SHA_A, "check": "unit", "run": ""},
                      {"sha": SHA_A, "check": "lint", "run": "ok-1"}]})
        self.assertEqual(cleaned,
                         {"main": [{"sha": SHA_A, "check": "unit"},
                                   {"sha": SHA_A, "check": "lint", "run": "ok-1"}]})

    def _wait_snapshot(self):
        import time
        gen = self.project.insights.generation()
        cap = self.project.snapshots.capture_files(self.project.workspace)
        wait = self.project.snapshots.create(
            cap, goal=self.project.goal,
            state="Now: parked\nNext: none\nTasks: none\nBlocking: none",
            run=None, outcome="wait", summary="parked", verification=None,
            wake_at=time.time() + 86400, inbox_seen=gen,
            wake_generation=self.project.control().get("wake_generation", ""))
        self.project.snapshots.publish(wait)
        return self.project.snapshots.get()

    def test_wake_new_run_due_same_run_parked(self):
        import dataclasses
        from mizu.fs import write_json
        from mizu.runtime import should_run, wake_ci_failures, write_ci_wake
        config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "mizu"})
        self.project.config = config
        snap = self._wait_snapshot()
        base = 2000.0
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": base,
                    "failures": {"mizu": [{"sha": SHA_A, "check": "unit",
                                           "run": "run-1"}]}})
        self.assertTrue(should_run(self.project, snap, current_time=base,
                                   role_name="worker", config=config))
        write_ci_wake(self.project, "worker", ("mizu",),
                      wake_ci_failures(self.project, config), snap["id"], base)
        # Identical fact including the same run parks.
        self.assertFalse(should_run(self.project, snap, current_time=base + 10,
                                    role_name="worker", config=config))
        # Same sha+check with a NEW run wakes.
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": base + 11,
                    "failures": {"mizu": [{"sha": SHA_A, "check": "unit",
                                           "run": "run-2"}]}})
        self.assertTrue(should_run(self.project, snap, current_time=base + 11,
                                   role_name="worker", config=config))
        write_ci_wake(self.project, "worker", ("mizu",),
                      wake_ci_failures(self.project, config), snap["id"], base + 11)
        self.assertFalse(should_run(self.project, snap, current_time=base + 12,
                                    role_name="worker", config=config))

    def test_wake_legacy_absent_run_stays_sha_keyed(self):
        import dataclasses
        from mizu.fs import write_json
        from mizu.runtime import should_run, wake_ci_failures, write_ci_wake
        config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "mizu"})
        self.project.config = config
        snap = self._wait_snapshot()
        base = 3000.0
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": base,
                    "failures": {"mizu": [{"sha": SHA_A, "check": "unit"}]}})
        write_ci_wake(self.project, "worker", ("mizu",),
                      wake_ci_failures(self.project, config), snap["id"], base)
        # Re-polled without identity: identical sha-keyed fact parks.
        self.assertFalse(should_run(self.project, snap, current_time=base + 10,
                                    role_name="worker", config=config))
        # Old record without run mismatches a new fact with run exactly once.
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": base + 11,
                    "failures": {"mizu": [{"sha": SHA_A, "check": "unit",
                                           "run": "run-9"}]}})
        self.assertTrue(should_run(self.project, snap, current_time=base + 11,
                                   role_name="worker", config=config))

    def test_prompts_include_run_only_when_sent(self):
        import dataclasses
        import json
        from mizu.fs import write_json
        from mizu.runtime import current_ci_failures, prompt_for
        from mizu.runtime import Context
        from mizu.fs import mkdir
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 1.0,
                    "failures": {"mizu": [{"sha": SHA_A, "check": "unit",
                                           "run": "run-1"},
                                          {"sha": SHA_B, "check": "lint"}]}})
        rows = {row["check"]: row for row in current_ci_failures(self.project)}
        self.assertEqual(rows["unit"]["run"], "run-1")
        self.assertEqual(set(rows["lint"]), {"branch", "sha", "check"})
        cfg = dataclasses.replace(self.config, vcs={**self.config.vcs, "ci_branch": "mizu"})
        roles = dict(cfg.roles)
        roles["reporter"] = dataclasses.replace(
            roles["reporter"],
            capabilities=tuple(list(roles["reporter"].capabilities) + ["vcs_read"]))
        cfg = dataclasses.replace(cfg, roles=roles)
        run = self.project.root / "runs" / "ci-run-probe"
        mkdir(run)
        ctx = Context(cfg, self.project, cfg.roles["reporter"], run,
                      self.project.snapshots.get(), self.project.workspace)
        prompt_rows = {row["check"]: row
                       for row in json.loads(prompt_for(ctx))["ci_failures"]}
        self.assertEqual(prompt_rows["unit"]["run"], "run-1")
        self.assertNotIn("run", prompt_rows["lint"])


class CiFailureProjectionTests(Fixture):
    def _cache(self, failures):
        from mizu.fs import write_json
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 1.0, "failures": failures})

    @staticmethod
    def _rows(count):
        return [{"sha": SHA_A, "check": f"check-{i:02d}"} for i in range(count)]

    def _prompt_context(self, with_vcs_read=True):
        import dataclasses
        from mizu.fs import mkdir
        from mizu.runtime import Context
        cfg = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "main"})
        roles = dict(cfg.roles)
        reporter = roles["reporter"]
        capabilities = list(reporter.capabilities)
        if with_vcs_read and "vcs_read" not in capabilities:
            capabilities.append("vcs_read")
        if not with_vcs_read:
            capabilities = [cap for cap in capabilities if cap != "vcs_read"]
        roles["reporter"] = dataclasses.replace(
            reporter, capabilities=tuple(capabilities))
        cfg = dataclasses.replace(cfg, roles=roles)
        self.project.config = cfg
        run = self.project.root / "runs" / "ci-failure-projection"
        mkdir(run)
        return Context(cfg, self.project, cfg.roles["reporter"], run,
                       self.project.snapshots.get(), self.project.workspace)

    def test_below_caps_legacy_rows_and_wake_are_unchanged(self):
        from mizu.runtime import (ci_failures_projection, current_ci_failures,
                                  wake_ci_failures)
        self._cache({"main": [{"sha": SHA_A, "check": "unit"}]})
        rows, may_be_truncated = ci_failures_projection(self.project)
        expected = [{"branch": "main", "sha": SHA_A, "check": "unit"}]
        self.assertEqual(rows, expected)
        self.assertFalse(may_be_truncated)
        # Old caches without run identity keep their exact list shape and
        # the projection marker does not alter writer wake selection.
        self.assertEqual(current_ci_failures(self.project), expected)
        import dataclasses
        wake_config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "main"})
        self.assertEqual(wake_ci_failures(self.project, wake_config), expected)

    def test_wake_filters_configured_branch_before_global_row_cap(self):
        import dataclasses
        from mizu.runtime import (ci_failures_projection, current_ci_failures,
                                  wake_ci_failures)
        self._cache({"a-one": self._rows(16),
                     "b-two": self._rows(16),
                     "z-main": [{"sha": SHA_A, "check": "configured"}]})
        rows, may_be_truncated = ci_failures_projection(self.project)
        self.assertEqual(len(rows), 32)
        self.assertTrue(may_be_truncated)
        self.assertNotIn("z-main", {row["branch"] for row in rows})
        self.assertEqual(len(current_ci_failures(self.project)), 32)

        config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "z-main"})
        self.assertEqual(wake_ci_failures(self.project, config),
                         [{"branch": "z-main", "sha": SHA_A,
                           "check": "configured"}])

    def test_per_branch_cap_is_conservatively_marked(self):
        from mizu.runtime import ci_failures_projection
        self._cache({"main": self._rows(17)})
        rows, may_be_truncated = ci_failures_projection(self.project)
        self.assertEqual(len(rows), 16)
        self.assertTrue(may_be_truncated)

    def test_stored_branch_cap_is_conservatively_marked(self):
        from mizu.runtime import ci_failures_projection
        failures = {
            f"branch-{i:03d}": self._rows(1) if i < 31 else []
            for i in range(128)
        }
        self._cache(failures)
        rows, may_be_truncated = ci_failures_projection(self.project)
        self.assertEqual(len(rows), 31)
        self.assertTrue(may_be_truncated)

    def test_flattened_prompt_cap_is_marked_in_full_and_delta(self):
        import json
        from mizu.runtime import (ci_failures_projection, prompt_delta_for,
                                  prompt_for)
        self._cache({"branch-a": self._rows(11),
                     "branch-b": self._rows(11),
                     "branch-c": self._rows(11)})
        rows, may_be_truncated = ci_failures_projection(self.project)
        self.assertEqual(len(rows), 32)
        self.assertTrue(may_be_truncated)
        context = self._prompt_context()
        full = json.loads(prompt_for(context))
        delta = json.loads(prompt_delta_for(context, []))
        for projection in (full, delta):
            self.assertEqual(len(projection["ci_failures"]), 32)
            self.assertIs(projection["ci_failures_may_be_truncated"], True)

    def test_prompt_marker_is_null_without_capability_or_readable_cache(self):
        import json
        from unittest import mock
        from mizu.runtime import prompt_for
        no_capability = json.loads(prompt_for(self._prompt_context(with_vcs_read=False)))
        self.assertIsNone(no_capability["ci_failures"])
        self.assertIsNone(no_capability["ci_failures_may_be_truncated"])
        context = self._prompt_context()
        with mock.patch("mizu.ci_observation.read_json", side_effect=OSError("unreadable")):
            unreadable = json.loads(prompt_for(context))
        self.assertEqual(unreadable["ci_failures"], [])
        self.assertIsNone(unreadable["ci_failures_may_be_truncated"])

    def test_pruning_deleted_branch_is_not_reported_as_truncation(self):
        import dataclasses
        from mizu import vcs
        config = dataclasses.replace(self.config, vcs=adapter_settings(
            status_code({"checks": []})))
        vcs.inject_refs(self.project.root, {"main": SHA_A})
        state = {"failures": {"deleted-branch": self._rows(17)}}
        event = poll_ci(config, self.project, state, now=1.0, interval=15.0,
                        reader=lambda *_args: {"checks": [], "trust": "external-untrusted"})
        self.assertTrue(event["ok"])
        self.assertFalse(event["ci_failures_may_be_truncated"])
        self.assertEqual(state["failures"], {})

    def test_poll_event_marks_dropped_check_rows_and_pruned_branches(self):
        import dataclasses
        from mizu.runtime import MAX_CI_FAILURE_BRANCHES
        rows = [{"check": f"check-{i:02d}", "state": "failure", "sha": SHA_A}
                for i in range(17)]
        config = dataclasses.replace(self.config, vcs=adapter_settings(
            status_code({"checks": rows})))
        state = {}
        dropped = poll_ci(config, self.project, state, now=1.0, interval=15.0,
                          branches=["main"])
        self.assertTrue(dropped["ci_failures_may_be_truncated"])
        self.assertEqual(len(state["failures"]["main"]), 16)

        # A valid old-cache shape plus one new failure exceeds the branch
        # store cap, causing this poll to prune a branch.
        old_failures = {
            f"branch-{i:03d}": [{"sha": SHA_A, "check": "unit"}]
            for i in range(MAX_CI_FAILURE_BRANCHES)
        }
        config2 = dataclasses.replace(self.config, vcs=adapter_settings(
            status_code({"checks": [{"check": "unit", "state": "failure",
                                     "sha": SHA_A}]})))
        state2 = {"failures": old_failures}
        pruned = poll_ci(config2, self.project, state2, now=2.0, interval=15.0,
                         branches=["new-branch"])
        self.assertTrue(pruned["ci_failures_may_be_truncated"])
        self.assertEqual(len(state2["failures"]), MAX_CI_FAILURE_BRANCHES)

        # Cleaning an oversized pre-existing branch cache is also a row drop
        # performed during this tick, even when the fresh poll is green.
        config3 = dataclasses.replace(self.config, vcs=adapter_settings(
            status_code({"checks": []})))
        state3 = {"failures": {"old-branch": self._rows(17)}}
        cleaned = poll_ci(config3, self.project, state3, now=3.0, interval=15.0,
                          branches=["main"])
        self.assertTrue(cleaned["ci_failures_may_be_truncated"])
        self.assertEqual(len(state3["failures"]["old-branch"]), 16)

class CiCacheRetentionTests(Fixture):
    def _config(self, branches, checks, **settings):
        return dataclasses.replace(self.config, vcs={
            **self.config.vcs, "ci_cache_max_branches": branches,
            "ci_cache_max_checks_per_branch": checks, **settings})

    def _rows(self, count):
        return [{"sha": SHA_A, "check": f"check-{i:04d}"} for i in range(count)]

    def test_load_defaults_ranges_and_effective_product(self):
        from mizu.config import load
        from mizu.errors import ConfigError
        from mizu.ci_observation import ci_cache_limits
        self.assertEqual(ci_cache_limits(self.config), (128, 16))
        base = self.file.read_text()
        # Replace only the two example settings; all other policy stays intact.
        base = "\n".join(line for line in base.splitlines()
                         if not line.startswith(("ci_cache_max_branches =",
                                                 "ci_cache_max_checks_per_branch =")))
        self.file.write_text(base)
        self.assertEqual(ci_cache_limits(load(self.file)), (128, 16))
        for branches, checks, extras, expected in (
                (256, 8, [], (256, 8)), (2048, 1, [], (2048, 1)),
                (2, 1024, [], (2, 1024)), (1, 1024, ["a", "a", "b"], (2, 1024)),
                (1, 1, ["a", "b"], (2, 1))):
            text = (f"ci_cache_max_branches = {branches}\n"
                    f"ci_cache_max_checks_per_branch = {checks}\n")
            # Empty primary here isolates deduplication of the extra set.
            text += 'ci_branch = ""\nci_wake_extra_branches = ' + json.dumps(extras) + '\n'
            clean = "\n".join(line for line in base.splitlines()
                              if not line.startswith(("ci_branch =", "ci_wake_extra_branches =")))
            self.file.write_text(clean.replace("[vcs]\n", "[vcs]\n" + text))
            self.assertEqual(ci_cache_limits(load(self.file)), expected)
        for key in ("ci_cache_max_branches", "ci_cache_max_checks_per_branch"):
            for bad in ("0", "-1", "true", '"16"', "1.5", "2049"):
                self.file.write_text(base.replace("[vcs]\n", f"[vcs]\n{key} = {bad}\n"))
                with self.subTest(key=key, bad=bad), self.assertRaises(ConfigError):
                    load(self.file)
        for settings in (
                'ci_cache_max_branches = 129\nci_cache_max_checks_per_branch = 16',
                'ci_cache_max_branches = 1\nci_cache_max_checks_per_branch = 1025',
                'ci_cache_max_branches = 1\nci_cache_max_checks_per_branch = 1024\n'
                'ci_branch = "primary"\nci_wake_extra_branches = ["a", "b"]'):
            clean = "\n".join(line for line in base.splitlines()
                              if not line.startswith(("ci_branch =", "ci_wake_extra_branches =")))
            self.file.write_text(clean.replace("[vcs]\n", "[vcs]\n" + settings + "\n"))
            with self.subTest(settings=settings), self.assertRaises(ConfigError):
                load(self.file)

    def test_default_helpers_and_malformed_runtime_fallback(self):
        from mizu.ci_observation import _clean_ci_failures, ci_cache_limits
        from mizu.ci_polling import _merge_ci_success_maps
        value = {f"branch-{i:03d}": self._rows(17) for i in range(129)}
        expected = {name: rows[:16] for name, rows in list(value.items())[:128]}
        self.assertEqual(_clean_ci_failures(value), expected)
        self.assertEqual(_merge_ci_success_maps({}, value), expected)
        self.assertEqual(ci_cache_limits(self._config(1, 1)), (1, 1))
        for branches, checks in (
                (True, 16), (128, "bad"), (0, 1), (1, 0), (1, 1025), (129, 16)):
            config = self._config(branches, checks)
            self.assertEqual(ci_cache_limits(config), (128, 16))
            self.assertEqual(_clean_ci_failures(value, config), expected)

    def test_redistribution_survives_refresh_poll_persist_and_projection(self):
        from mizu.ci_observation import ci_failures_projection
        from mizu.fs import read_json, write_json
        from mizu.runtime import poll_project_vcs
        # A 256 x 8 map would be silently cut by any forgotten default helper.
        value = {f"branch-{i:03d}": self._rows(8) for i in range(256)}
        config = self._config(256, 8)
        write_json(self.project.root / "vcs-poll.json", {"failures": value, "successes": value})
        state = {}
        poll_project_vcs(config, self.project, state, now=100.0, interval=15.0)
        stored = read_json(self.project.root / "vcs-poll.json")
        for key in ("failures", "successes"):
            self.assertEqual(state["ci"][key], value)
            self.assertEqual(stored[key], value)
        rows, truncated = ci_failures_projection(self.project, limit=2048, config=config)
        self.assertEqual(len(rows), 2048)
        self.assertTrue(truncated)
        self.assertTrue(all(row["nondeterministic"] for row in rows))
        # Presentation still defaults to 32 rows, independently of retention.
        self.assertEqual(len(ci_failures_projection(self.project, config=config)[0]), 32)

    def test_reduced_retention_preserves_selected_branches_through_pruning(self):
        from mizu.ci_observation import ci_cache_limits, _clean_ci_failures
        config = self._config(1, 2, command=["synthetic"], ci_branch="selected-a",
                              ci_wake_extra_branches=["selected-b", "selected-b"])
        self.assertEqual(ci_cache_limits(config), (2, 2))
        value = {"unrelated": self._rows(3), "selected-b": self._rows(3),
                 "selected-a": self._rows(3)}
        expected = {branch: self._rows(2) for branch in ("selected-a", "selected-b")}
        self.assertEqual(_clean_ci_failures(value, config), expected)
        state = {"failures": value, "successes": value}
        def reader(settings, op, params):
            return {"checks": [{**row, "state": status}
                               for status in ("failure", "success") for row in self._rows(3)]}
        # Explicit targets exercise sorted-name eviction after the incoming merge.
        out = poll_ci(config, self.project, state, now=0.0, interval=15.0,
                      branches=["new"], reader=reader)
        self.assertTrue(out["ok"])
        self.assertTrue(out["ci_failures_may_be_truncated"])
        self.assertEqual(state["failures"], expected)
        self.assertEqual(state["successes"], expected)
        # Refreshed selected rows use the configured check cap too.
        poll_ci(config, self.project, state, now=15.0, interval=15.0,
                branches=["selected-a"], reader=reader)
        self.assertEqual(state["failures"], expected)
        self.assertEqual(state["successes"], expected)
        # Live-ref pruning still ends the lifetime of deleted branch facts,
        # including a configured wake branch; wake selection only protects
        # branches from capacity eviction.
        vcs.inject_refs(self.project.root, {"new": SHA_B})
        poll_ci(config, self.project, state, now=30.0, interval=15.0, reader=reader)
        retained = {"new": self._rows(2)}
        self.assertEqual(state["failures"], retained)
        self.assertEqual(state["successes"], retained)

    def test_deleted_wake_ref_is_pruned_before_reduced_capacity_selection(self):
        from mizu.fs import read_json, write_json
        from mizu.runtime import poll_project_vcs
        config = self._config(2, 1, command=["synthetic"], ci_branch="stale",
                              poll_max_branches=1)
        value = {branch: self._rows(1) for branch in ("live-a", "live-b", "stale")}
        expected = {branch: value[branch] for branch in ("live-a", "live-b")}
        refs = {branch: SHA_A for branch in ("live-a", "live-b", "live-c")}
        for project_poll in (False, True):
            with self.subTest(project_poll=project_poll):
                # Only live-c is refreshed: a lost live-b cannot be recovered by
                # the tick. Both the authoritative shared failures and merged
                # local/shared successes must survive reduced retention.
                ci = {"ci_offset": 2, "failures": value, "successes": value}
                if not project_poll:
                    vcs.inject_refs(self.project.root, refs)
                    event = poll_ci(config, self.project, ci, now=15.0, interval=15.0,
                                    reader=lambda *_: {"checks": []})
                else:
                    vcs.inject_refs(self.project.root, {**refs, "stale": SHA_B})
                    write_json(self.project.root / "vcs-poll.json", {
                        "last_poll": 0.0, "failures": value, "successes": value})
                    def fetch(*_args, **_kwargs):
                        # Upstream refresh deletes the selected ref this tick,
                        # so cache normalization must wait until after fetch.
                        vcs.inject_refs(self.project.root, refs)
                        return None
                    with (patch("mizu.runtime.poll_upstream", side_effect=fetch),
                          patch("mizu.runtime.ci_waits.poll", return_value=None),
                          patch("mizu.runtime.poll_proposals", return_value=None),
                          patch("mizu.vcs.read_via", return_value={"checks": []})):
                        events = poll_project_vcs(config, self.project, {"ci": ci},
                                                  now=15.0, interval=15.0)
                    event, = events
                    stored = read_json(self.project.root / "vcs-poll.json")
                    for key in ("failures", "successes"):
                        with self.subTest(persisted_cache=key):
                            self.assertEqual(stored[key], expected)
                self.assertTrue(event["ok"])
                self.assertEqual(event["branches"], ["live-c"])
                for key in ("failures", "successes"):
                    with self.subTest(memory_cache=key):
                        self.assertEqual(ci[key], expected)

    def test_unknown_live_inventory_does_not_imply_deleted_wake_ref(self):
        from mizu.fs import read_json, write_json
        from mizu.runtime import poll_project_vcs
        config = self._config(2, 1, command=["synthetic"], ci_branch="stale",
                              poll_max_branches=1)
        value = {branch: self._rows(1) for branch in ("live-a", "live-b", "stale")}
        expected = {branch: value[branch] for branch in ("stale", "live-a")}
        for error in (OSError("unavailable"), Denied("incomplete inventory")):
            with self.subTest(error=type(error).__name__):
                write_json(self.project.root / "vcs-poll.json", {
                    "failures": value, "successes": value})
                state = {}
                with (patch("mizu.runtime.poll_upstream", return_value=None),
                      patch("mizu.runtime.ci_waits.poll", return_value=None),
                      patch("mizu.runtime.poll_proposals", return_value=None),
                      patch("mizu.vcs.list_refs", side_effect=error),
                      patch("mizu.vcs.read_via") as reader):
                    event, = poll_project_vcs(config, self.project, state,
                                              now=15.0, interval=15.0)
                reader.assert_not_called()
                self.assertFalse(event["ok"])
                stored = read_json(self.project.root / "vcs-poll.json")
                for key in ("failures", "successes"):
                    self.assertEqual(state["ci"][key], expected)
                    self.assertEqual(stored[key], expected)

    def test_deferred_capacity_still_obeys_total_row_envelope(self):
        from mizu.ci_observation import MAX_CI_CACHE_ROWS, _clean_ci_failures
        from mizu.ci_polling import _merge_ci_success_maps
        config = self._config(2, 1024, ci_branch="wake")
        value = {f"branch-{i:04d}": self._rows(1)
                 for i in range(MAX_CI_CACHE_ROWS + 1)}
        for cache in (_clean_ci_failures(value, config, defer_branch_cap=True),
                      _merge_ci_success_maps({}, value, config, defer_branch_cap=True)):
            self.assertLessEqual(len(cache), MAX_CI_CACHE_ROWS)
            self.assertEqual(sum(map(len, cache.values())), MAX_CI_CACHE_ROWS)

    def test_not_due_reduced_retention_defers_capacity_until_live_inventory(self):
        from mizu.fs import read_json, write_json
        from mizu.runtime import poll_project_vcs
        config = self._config(2, 1024, command=["synthetic"], ci_branch="stale",
                              poll_max_branches=1)
        value = {branch: self._rows(1) for branch in ("live-a", "live-b", "stale")}
        # Not due (last_poll=10, now=15, interval=15): there is no complete
        # live inventory, so reduced capacity must not evict live rows via
        # the stale selected wake ref. Both caches stay over the configured
        # cap within the fixed row envelope until a due poll can prune.
        write_json(self.project.root / "vcs-poll.json", {
            "last_poll": 10.0, "last_fetch": 10.0,
            "last_proposal_poll": 10.0, "last_ci_wait_poll": 10.0,
            "failures": value, "successes": value})
        vcs.inject_refs(self.project.root,
                        {branch: SHA_A for branch in ("live-a", "live-b", "stale")})
        state = {}
        with (patch("mizu.runtime.poll_upstream", return_value=None),
              patch("mizu.runtime.ci_waits.poll", return_value=None),
              patch("mizu.runtime.poll_proposals", return_value=None),
              patch("mizu.vcs.list_refs",
                    side_effect=AssertionError("not-due poll must not read refs")),
              patch("mizu.vcs.read_via",
                    side_effect=AssertionError("not-due poll must not call the adapter"))):
            events = poll_project_vcs(config, self.project, state,
                                      now=15.0, interval=15.0)
        self.assertEqual(events, [])
        stored = read_json(self.project.root / "vcs-poll.json")
        for key in ("failures", "successes"):
            with self.subTest(preserved_cache=key):
                self.assertEqual(state["ci"][key], value)
                self.assertEqual(stored[key], value)
        # A later due tick sees the stale deletion and prunes before capacity
        # selection, so the preserved live-b is still recoverable.
        vcs.inject_refs(self.project.root,
                        {branch: SHA_A for branch in ("live-a", "live-b", "live-c")})
        state["ci"]["ci_offset"] = 2
        with (patch("mizu.runtime.poll_upstream", return_value=None),
              patch("mizu.runtime.ci_waits.poll", return_value=None),
              patch("mizu.runtime.poll_proposals", return_value=None),
              patch("mizu.vcs.read_via", return_value={"checks": []})):
            events = poll_project_vcs(config, self.project, state,
                                      now=25.0, interval=15.0)
        event = next(item for item in events if item["event"] == "ci_poll")
        self.assertTrue(event["ok"])
        self.assertEqual(event["branches"], ["live-c"])
        expected = {branch: value[branch] for branch in ("live-a", "live-b")}
        stored = read_json(self.project.root / "vcs-poll.json")
        for key in ("failures", "successes"):
            with self.subTest(pruned_cache=key):
                self.assertEqual(state["ci"][key], expected)
                self.assertEqual(stored[key], expected)

    def test_wake_covers_total_envelope_not_the_old_65_times_16_limit(self):
        from mizu.ci_observation import wake_ci_failures, ci_failures_projection
        from mizu.fs import write_json
        config = self._config(1, 1024, command=["synthetic"], ci_branch="primary",
                              ci_wake_extra_branches=["extra"])
        state = {}
        def reader(settings, op, params):
            return {"checks": [{**row, "state": "failure"} for row in self._rows(1024)]}
        result = poll_ci(config, self.project, state, now=0.0, interval=15.0,
                         branches=["primary", "extra"], reader=reader)
        self.assertTrue(result["ok"])
        write_json(self.project.root / "vcs-poll.json", state)
        rows = wake_ci_failures(self.project, config)
        self.assertEqual(len(rows), 2048)
        self.assertEqual({row["branch"] for row in rows}, {"primary", "extra"})
        self.assertEqual(rows[-1]["check"], "check-1023")
        self.assertEqual(len(ci_failures_projection(self.project, config=config)[0]), 32)


class CiWakeExtraBranchTests(Fixture):
    def _cache(self, failures):
        from mizu.fs import write_json
        write_json(self.project.root / "vcs-poll.json", {"failures": failures})

    def test_extra_branch_wakes_and_selected_set_red_parks(self):
        from mizu.runtime import (effective_ci_wake_branches, _ci_due_for,
                                  wake_ci_failures, write_ci_wake)
        config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "primary",
                              "ci_wake_extra_branches": ("extra", "extra")})
        failures = {"primary": [{"sha": SHA_A, "check": "unit"}],
                    "extra": [{"sha": SHA_B, "check": "lint"}],
                    "unselected": [{"sha": SHA_A, "check": "other"}]}
        self._cache(failures)
        present = wake_ci_failures(self.project, config)
        self.assertEqual(effective_ci_wake_branches(config), ("extra", "primary"))
        self.assertEqual({row["branch"] for row in present}, {"primary", "extra"})
        write_ci_wake(self.project, "worker", effective_ci_wake_branches(config),
                      present, "published-head", 100.0)
        self.assertEqual(_ci_due_for(self.project, config, "worker",
                                     "published-head", 110.0), [])
        # Only the extra branch fact changes; the primary red is unchanged.
        failures["extra"] = [{"sha": "c" * 40, "check": "lint"}]
        self._cache(failures)
        due = _ci_due_for(self.project, config, "worker", "published-head", 111.0)
        self.assertEqual({row["branch"] for row in due}, {"primary", "extra"})

    def test_empty_primary_still_fails_closed_unless_extra_is_listed(self):
        from mizu.runtime import wake_ci_failures
        self._cache({"extra": [{"sha": SHA_A, "check": "unit"}]})
        self.assertEqual(wake_ci_failures(self.project, self.config), [])
        configured = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "",
                              "ci_wake_extra_branches": ("extra",)})
        self.assertEqual(wake_ci_failures(self.project, configured),
                         [{"branch": "extra", "sha": SHA_A, "check": "unit"}])

    def test_runtime_malformed_or_oversized_wake_sets_fail_closed(self):
        from mizu.runtime import effective_ci_wake_branches, wake_ci_failures
        self._cache({"primary": [{"sha": SHA_A, "check": "unit"}]})
        for extras in (("bad branch",), tuple(f"branch-{i:02d}" for i in range(65)), "not-an-array"):
            config = dataclasses.replace(
                self.config, vcs={**self.config.vcs, "ci_branch": "primary",
                                  "ci_wake_extra_branches": extras})
            self.assertEqual(effective_ci_wake_branches(config), ())
            self.assertEqual(wake_ci_failures(self.project, config), [])

    def test_wake_filters_raw_cache_before_branch_cap_and_prompt_stays_unfiltered(self):
        from mizu.runtime import current_ci_failures, wake_ci_failures
        failures = {"other-%03d" % i: [{"sha": SHA_A, "check": "unit"}]
                    for i in range(128)}
        failures["selected"] = [{"sha": SHA_B, "check": "lint"}]
        self._cache(failures)
        config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "selected"})
        self.assertEqual(wake_ci_failures(self.project, config),
                         [{"branch": "selected", "sha": SHA_B, "check": "lint"}])
        self.assertTrue(any(row["branch"] != "selected"
                            for row in current_ci_failures(self.project)))

    def test_full_selected_fact_set_beyond_prompt_cap_drives_wake_digest(self):
        from mizu.runtime import (effective_ci_wake_branches, _ci_due_for,
                                  wake_ci_failures, write_ci_wake)
        branches = ("primary", "extra-a", "extra-b")
        config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "primary",
                              "ci_wake_extra_branches": branches[1:]})
        failures = {
            branch: [{"sha": SHA_A, "check": f"check-{index:02d}"}
                     for index in range(16)]
            for branch in branches
        }
        self._cache(failures)
        present = wake_ci_failures(self.project, config)
        self.assertEqual(len(present), 48)  # More than the 32-row prompt projection.
        selected = effective_ci_wake_branches(config)
        write_ci_wake(self.project, "worker", selected, present,
                      "published-head", 100.0)
        self.assertEqual(_ci_due_for(self.project, config, "worker",
                                     "published-head", 110.0), [])
        failures["extra-b"][15] = {"sha": SHA_B, "check": "check-15"}
        self._cache(failures)
        self.assertTrue(_ci_due_for(self.project, config, "worker",
                                    "published-head", 111.0))

    def test_old_wake_record_is_due_once_then_parks(self):
        from mizu.fs import write_json
        from mizu.runtime import _ci_due_for, wake_ci_failures, write_ci_wake
        config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "primary"})
        self._cache({"primary": [{"sha": SHA_A, "check": "unit"}]})
        present = wake_ci_failures(self.project, config)
        write_json(self.project.root / "vcs-ci-wake-worker.json",
                   {"version": 1, "branch": "primary",
                    "fact": [{"sha": SHA_A, "check": "unit"}],
                    "snapshot": "published-head", "at": 100.0})
        self.assertTrue(_ci_due_for(self.project, config, "worker",
                                    "published-head", 101.0))
        write_ci_wake(self.project, "worker", ("primary",), present,
                      "published-head", 101.0)
        self.assertEqual(_ci_due_for(self.project, config, "worker",
                                     "published-head", 102.0), [])


class CiNondeterminismTests(Fixture):
    def _adapter_config(self, status_by_branch):
        payload = json.dumps(status_by_branch)
        code = ("import json,sys; req=json.load(sys.stdin); status=" + repr(payload) +
                "; print(json.dumps({'checks': json.loads(status)[req['branch']]}))")
        return dataclasses.replace(
            self.config, vcs={**adapter_settings(code), "poll_enabled": True})

    def _poll_project(self, config, state, current, interval=15.0):
        from unittest.mock import patch
        from mizu.runtime import poll_project_vcs
        with patch("mizu.runtime.poll_upstream", return_value=None):
            return poll_project_vcs(config, self.project, state,
                                    now=current, interval=interval)

    def test_poll_cache_caps_preserve_configured_wake_branch_evidence(self):
        from mizu.runtime import poll_ci
        base_config = self._adapter_config({})
        config = dataclasses.replace(
            base_config, vcs={**base_config.vcs, "ci_branch": "selected"})
        state = {
            "failures": {
                f"z-{index:03d}": [{"sha": SHA_A, "check": "old"}]
                for index in range(128)
            },
            "successes": {
                f"z-{index:03d}": [{"sha": SHA_B, "check": "old"}]
                for index in range(128)
            },
        }
        result = poll_ci(
            config, self.project, state, now=0.0, interval=15.0,
            branches=["selected"],
            reader=lambda *_args: {"checks": [
                {"check": "unit", "state": "failure", "sha": SHA_A},
                {"check": "lint", "state": "success", "sha": SHA_B},
            ]})
        self.assertTrue(result["ok"])
        self.assertIn("selected", state["failures"])
        self.assertIn("selected", state["successes"])
        self.assertEqual(len(state["failures"]), 128)
        self.assertEqual(len(state["successes"]), 128)

    def test_same_tick_cross_branch_annotation_keeps_red_visible(self):
        from mizu import vcs
        from mizu.runtime import current_ci_failures, wake_ci_failures
        vcs.inject_refs(self.project.root, {"passed": SHA_A, "failed": SHA_B})
        config = self._adapter_config({
            "passed": [{"check": "unit", "state": "success", "sha": SHA_A}],
            "failed": [{"check": "unit", "state": "failure", "sha": SHA_A}],
        })
        config = dataclasses.replace(
            config, vcs={**config.vcs, "ci_branch": "failed"})
        events = self._poll_project(config, {}, 0.0)
        poll = next(event for event in events if event["event"] == "ci_poll")
        self.assertTrue(poll["ok"])
        self.assertEqual(poll["failures"], 1)
        self.assertEqual(self.project.insights.list(pending=False), [])
        row, = current_ci_failures(self.project)
        self.assertEqual(row, {"branch": "failed", "sha": SHA_A, "check": "unit",
                               "nondeterministic": True, "passed_on": "passed"})
        self.assertEqual(wake_ci_failures(self.project, config), [row])
        self.assertEqual(self.project.insights.list(pending=True), [])

    def test_cross_poll_success_survives_refresh_and_project_persistence(self):
        from mizu import vcs
        from mizu.fs import read_json
        from mizu.runtime import current_ci_failures
        vcs.inject_refs(self.project.root, {"passed": SHA_A, "failed": SHA_B})
        first_config = self._adapter_config({
            "passed": [{"check": "unit", "state": "success", "sha": SHA_A}],
            "failed": [],
        })
        first_state = {}
        self._poll_project(first_config, first_state, 0.0)
        stored = read_json(self.project.root / "vcs-poll.json", {})
        self.assertEqual(stored["successes"], {"passed": [{"sha": SHA_A, "check": "unit"}]})

        second_config = self._adapter_config({
            "passed": [],
            "failed": [{"check": "unit", "state": "failure", "sha": SHA_A}],
        })
        second_events = self._poll_project(second_config, {}, 15.0)
        poll = next(event for event in second_events if event["event"] == "ci_poll")
        self.assertEqual(poll["failures"], 1)
        stored = read_json(self.project.root / "vcs-poll.json", {})
        self.assertEqual(stored["successes"]["passed"][0]["sha"], SHA_A)
        row, = current_ci_failures(self.project)
        self.assertEqual(row["nondeterministic"], True)
        self.assertEqual(row["passed_on"], "passed")
        self.assertEqual(row["branch"], "failed")

    def test_shared_failure_refresh_drops_stale_reds_without_a_due_poll(self):
        from mizu.fs import read_json, write_json
        from mizu.runtime import current_ci_failures, poll_project_vcs

        config = self._adapter_config({})
        cases = (
            ({}, {"main": [{"sha": SHA_A, "check": "unit"}]}),
            ({"live": [{"sha": SHA_B, "check": "lint"}]}, {}),
        )
        for shared_failures, shared_successes in cases:
            with self.subTest(shared_failures=shared_failures):
                write_json(self.project.root / "vcs-poll.json", {
                    "last_fetch": 1100.0, "last_poll": 1100.0,
                    "last_proposal_poll": 1100.0, "last_ci_wait_poll": 1100.0,
                    "failures": shared_failures, "successes": shared_successes,
                })
                state = {"last_fetch": 1000.0, "ci": {
                    "last_poll": 1000.0,
                    "failures": {
                        "main": [{"sha": SHA_A, "check": "unit"}],
                        "removed": [{"sha": SHA_A, "check": "old"}],
                    },
                }}
                with (patch("mizu.runtime.poll_upstream", return_value=None),
                      patch("mizu.runtime.ci_waits.poll", return_value=None),
                      patch("mizu.runtime.poll_proposals", return_value=None),
                      patch("mizu.vcs.list_refs",
                            side_effect=AssertionError("stale daemon must not poll CI")),
                      patch("mizu.vcs.read_via",
                            side_effect=AssertionError("stale daemon must not call the adapter"))):
                    events = poll_project_vcs(config, self.project, state,
                                              now=1101.0, interval=300.0)
                self.assertEqual(events, [])
                stored = read_json(self.project.root / "vcs-poll.json", {})
                self.assertEqual(stored["failures"], shared_failures)
                self.assertEqual(current_ci_failures(self.project), [
                    {"branch": branch, **row}
                    for branch in sorted(shared_failures)
                    for row in shared_failures[branch]
                ])

    def test_invalid_shared_failure_maps_preserve_local_unpersisted_facts(self):
        from mizu.runtime import refresh_poll_state
        local = {"local": [{"sha": SHA_A, "check": "unit"}]}
        for shared in ({}, {"failures": None}, {"failures": []}, []):
            state = {"ci": {"failures": local}}
            refresh_poll_state(state, shared)
            self.assertEqual(state["ci"]["failures"], local)
