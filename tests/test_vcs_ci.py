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

    def test_varying_log_url_dedupes_and_poll_stays_ok(self):
        # Repeats with a different per-run log URL must return the existing
        # record (stable branch/sha/check body) instead of raising Denied,
        # and a due poll with the new URL must stay ok.
        first = vcs.record_ci_result(self.project, branch="main", sha=SHA_A,
                                     check="unit", state="failure",
                                     url="https://ci/run/1")
        self.assertTrue(first["recorded"])
        second = vcs.record_ci_result(self.project, branch="main", sha=SHA_A,
                                      check="unit", state="failure",
                                      url="https://ci/run/2")
        self.assertEqual(second["id"], first["id"])
        stored = self.project.insights.read(first["id"])
        self.assertNotIn("https://ci/run", stored["body"])
        # Due poll emitting the second URL for the same failure stays ok.
        payload = {"checks": [{"check": "unit", "state": "failure", "sha": SHA_A,
                               "url": "https://ci/run/2"}]}
        config = dataclasses.replace(self.config, vcs=adapter_settings(status_code(payload)))
        state = {}
        event = poll_ci(config, self.project, state, now=0.0, interval=15.0,
                        branches=["main"])
        self.assertTrue(event["ok"])
        self.assertEqual(event["failures"], 1)
        self.assertEqual(event["recorded"][0]["id"], first["id"])

    def test_varying_origin_and_run_dedupe_preserving_first(self):
        first = vcs.record_ci_result(self.project, branch="main", sha=SHA_A,
                                     check="unit", state="failure",
                                     url="https://ci/run/1", origin="adapter:A",
                                     run="run-1")
        self.assertTrue(first["recorded"])
        stored = self.project.insights.read(first["id"])
        self.assertEqual(stored["origin"], "adapter:A")
        second = vcs.record_ci_result(self.project, branch="main", sha=SHA_A,
                                      check="unit", state="failure",
                                      url="https://ci/run/2", origin="adapter:B",
                                      run="run-2")
        self.assertEqual(second["id"], first["id"])
        kept = self.project.insights.read(first["id"])
        self.assertEqual(kept["origin"], "adapter:A")
        self.assertEqual(kept["run"], "run-1")

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
        # Parking the insight as noise must not park the red head.
        record = self.project.insights.read(event["recorded"][0]["id"])
        self.project.insights.decide(record["id"], "defer", "noise",
                                     "awaiting CI re-run", run="test-run",
                                     expected_rev=record["rev"])
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
        write_ci_wake(self.project, "worker", "mizu", present, snap["id"], base)
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
        write_ci_wake(self.project, "worker", "mizu",
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
        write_ci_wake(self.project, "worker", "mizu",
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
        write_ci_wake(self.project, "worker", "mizu",
                      wake_ci_failures(self.project, config), snap["id"], base)
        self.assertFalse(should_run(self.project, snap, current_time=base + 1799,
                                    role_name="worker", config=config))
        self.assertTrue(should_run(self.project, snap, current_time=base + 1800,
                                   role_name="worker", config=config))

    def test_moved_snapshot_wakes(self):
        from mizu.runtime import should_run, wake_ci_failures, write_ci_wake
        config, snap = self._red()
        base = 2000.0
        write_ci_wake(self.project, "worker", "mizu",
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
        write_ci_wake(self.project, "worker", "mizu",
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
