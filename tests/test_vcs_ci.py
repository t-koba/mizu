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
        self.assertFalse(first["ci_failures_may_be_truncated"])
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
        # A check that has not finished may report only its start.
        started = vcs.parse_status_checks(
            {"checks": [{"check": "unit", "state": "pending", "sha": SHA_A,
                         "started_at": "2026-07-20T05:00:00+00:00"}]})
        self.assertNotIn("completed_at", started[0])
        # Absent stays absent (exactly check/state/sha/url).
        plain = vcs.parse_status_checks(
            {"checks": [{"check": "unit", "state": "success", "sha": SHA_A}]})
        self.assertEqual(set(plain[0]), {"check", "state", "sha", "url"})

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

    def test_poller_stores_run_and_passes_it_to_record(self):
        import dataclasses
        from mizu import vcs as vcs_mod
        from mizu.runtime import current_ci_failures, poll_ci
        payload = {"checks": [{"check": "unit", "state": "failure",
                               "sha": SHA_A, "run": "run-1"}]}
        config = dataclasses.replace(self.config, vcs=adapter_settings(status_code(payload)))
        seen = []
        real_record = vcs_mod.record_ci_result

        def wrapped(project, **kwargs):
            seen.append(kwargs.get("run"))
            return real_record(project, **kwargs)
        vcs_mod.record_ci_result = wrapped
        try:
            state: dict = {}
            event = poll_ci(config, self.project, state, now=0.0, interval=15.0,
                            branches=["main"])
        finally:
            vcs_mod.record_ci_result = real_record
        self.assertTrue(event["ok"])
        self.assertEqual(seen, ["run-1"])
        # Failures cache carries the identity.
        self.assertEqual(state["failures"],
                         {"main": [{"sha": SHA_A, "check": "unit", "run": "run-1"}]})
        from mizu.fs import write_json
        write_json(self.project.root / "vcs-poll.json",
                   {"last_poll": 0.0, "failures": state["failures"]})
        self.assertEqual(current_ci_failures(self.project),
                         [{"branch": "main", "sha": SHA_A, "check": "unit",
                           "run": "run-1"}])
        # Re-poll with a new run keeps the same insight (provenance only,
        # first-stored preserved) while the cache advances.
        stored = self.project.insights.read(event["recorded"][0]["id"])
        self.assertEqual(stored["run"], "run-1")
        payload2 = {"checks": [{"check": "unit", "state": "failure",
                                "sha": SHA_A, "run": "run-2"}]}
        config2 = dataclasses.replace(self.config, vcs=adapter_settings(status_code(payload2)))
        event2 = poll_ci(config2, self.project, state, now=15.0, interval=15.0,
                         branches=["main"])
        self.assertTrue(event2["ok"])
        self.assertEqual(event2["recorded"][0]["id"], event["recorded"][0]["id"])
        kept = self.project.insights.read(event["recorded"][0]["id"])
        self.assertEqual(kept["run"], "run-1")
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
        with mock.patch("mizu.runtime.read_json", side_effect=OSError("unreadable")):
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
        self.assertEqual(len(poll["recorded"]), 1)  # Passes create no insight.
        row, = current_ci_failures(self.project)
        self.assertEqual(row, {"branch": "failed", "sha": SHA_A, "check": "unit",
                               "nondeterministic": True, "passed_on": "passed"})
        self.assertEqual(wake_ci_failures(self.project, config), [row])
        self.assertEqual(self.project.insights.list(pending=True, limit=1000)[0]["source"], "vcs")

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
