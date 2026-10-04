"""M1 step 5: daemon periodic fetch with fake-clock tests. Offline only."""
import dataclasses
import sys
import unittest

from support import Fixture
from mizu import vcs
from mizu.errors import Denied
from mizu.runtime import poll_upstream, refresh_upstream, upstream_fetch_due

SHA_A = "a" * 40


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


def fetch_code(refs_json):
    return ("import sys,json; json.load(sys.stdin); "
            "print(json.dumps({'refs': %s}))" % refs_json)


class DaemonPeriodicFetchTests(Fixture):
    def _configured(self, refs_json='{"main": "%s"}' % SHA_A):
        return dataclasses.replace(
            self.config, vcs=adapter_settings(fetch_code(refs_json)))

    def test_due_needs_fake_clock_interval(self):
        self.assertTrue(upstream_fetch_due(None, 100.0, 15.0))
        self.assertTrue(upstream_fetch_due(0.0, 15.0, 15.0))
        self.assertFalse(upstream_fetch_due(0.0, 14.9, 15.0))
        self.assertTrue(upstream_fetch_due(10.0, 25.0, 15.0))
        self.assertFalse(upstream_fetch_due(10.0, 24.9, 15.0))

    def test_disabled_adapter_never_polls(self):
        calls = []
        state = {"last_fetch": None}
        out = poll_upstream(self.config, self.project, state, now=100.0,
                            interval=15.0,
                            refresh=lambda c, p: calls.append(1) or {"injected": 1})
        self.assertIsNone(out)
        self.assertEqual(calls, [])
        self.assertIsNone(state.get("last_fetch"))

    def test_poll_refreshes_once_per_interval(self):
        config = self._configured()
        before = self.project.snapshots.get()["id"]
        state: dict = {"last_fetch": None}
        first = poll_upstream(config, self.project, state, now=0.0, interval=15.0)
        self.assertTrue(first["ok"])
        self.assertEqual(first["event"], "upstream_fetch")
        self.assertEqual(first["injected"], 1)
        self.assertEqual(vcs.read_ref(self.project.workspace, "main"), SHA_A)
        # Not due yet: no second adapter call, no new event.
        calls = []
        orig = refresh_upstream
        def counting(c, p):
            calls.append(1)
            return orig(c, p)
        skipped = poll_upstream(config, self.project, state, now=14.0,
                                interval=15.0, refresh=counting)
        self.assertIsNone(skipped)
        self.assertEqual(calls, [])
        # Due again at the interval boundary.
        second = poll_upstream(config, self.project, state, now=15.0,
                               interval=15.0, refresh=counting)
        self.assertTrue(second["ok"])
        self.assertEqual(calls, [1])
        # Periodic refresh never publishes: same snapshot, refs digest-excluded.
        self.assertEqual(self.project.snapshots.get()["id"], before)

    def test_poll_failure_is_best_effort_event_never_silent_publish(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings("import sys; sys.exit(3)"))
        before = self.project.snapshots.get()["id"]
        state: dict = {"last_fetch": None}
        event = poll_upstream(config, self.project, state, now=50.0, interval=15.0)
        self.assertEqual(event["event"], "upstream_fetch")
        self.assertFalse(event["ok"])
        self.assertIn("error", event)
        # Failure advances the clock so the daemon cannot busy-loop ...
        self.assertEqual(state["last_fetch"], 50.0)
        # ... and the very next tick stays quiet until the interval elapses.
        again = poll_upstream(config, self.project, state, now=51.0, interval=15.0)
        self.assertIsNone(again)
        # Nothing published and nothing injected on failure.
        self.assertEqual(self.project.snapshots.get()["id"], before)
        self.assertEqual(vcs.list_refs(self.project.workspace), [])

    def test_poll_failure_does_not_raise_denied(self):
        def boom(config, project):
            raise Denied("boom")
        state: dict = {"last_fetch": None}
        config = self._configured()
        event = poll_upstream(config, self.project, state, now=7.0,
                              interval=15.0, refresh=boom)
        self.assertFalse(event["ok"])
        self.assertIn("boom", event["error"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
