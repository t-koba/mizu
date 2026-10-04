"""Daemon VCS polling is opt-in, per-project, and time-bounded. Offline only."""
import dataclasses
import unittest

from support import Fixture
from mizu import vcs
from mizu.errors import Busy, Denied
from mizu.fs import lock
from mizu.runtime import (MAX_CI_BRANCHES, capped_vcs_settings, poll_ci,
                          vcs_poll_enabled, vcs_poll_interval)


class PollBoundsTests(Fixture):
    def test_disabled_by_default(self):
        self.assertFalse(vcs_poll_enabled(self.config))
        self.assertEqual(vcs_poll_interval(self.config), 300.0)

    def test_opt_in_requires_command_and_flag(self):
        cfg = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "command": ["true"], "poll_enabled": True, "poll_interval_seconds": 60})
        self.assertTrue(vcs_poll_enabled(cfg))
        self.assertEqual(vcs_poll_interval(cfg), 60.0)
        off = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "command": ["true"], "poll_enabled": False})
        self.assertFalse(vcs_poll_enabled(off))

    def test_per_call_timeout_capped(self):
        out = capped_vcs_settings({"timeout_seconds": 1000}, 15)
        self.assertEqual(out["timeout_seconds"], 15)
        kept = capped_vcs_settings({"timeout_seconds": 5}, 15)
        self.assertEqual(kept["timeout_seconds"], 5)

    def test_branch_fanout_bounded(self):
        import dataclasses
        self.assertEqual(MAX_CI_BRANCHES, 4)
        cfg = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "command": ["true"], "poll_enabled": True})
        event = poll_ci(cfg, self.project, {}, now=0.0, interval=15.0,
                        branches=["a", "b", "c", "d", "e"])
        self.assertEqual(event["event"], "ci_poll")
        self.assertFalse(event["ok"])
        self.assertIn("Too many", event["error"])

    def test_poll_bounds_follow_operator_config(self):
        self.assertEqual(self.config.vcs["poll_max_branches"], 4)
        self.assertEqual(self.config.vcs["poll_fetch_timeout_seconds"], 30)
        self.assertEqual(self.config.vcs["poll_status_timeout_seconds"], 15)
        cfg = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "command": ["true"], "poll_enabled": True, "poll_max_branches": 2})
        def reader(settings, op, params):
            return {"checks": [], "trust": "external-untrusted"}
        ok = poll_ci(cfg, self.project, {}, now=0.0, interval=15.0,
                     branches=["a", "b"], reader=reader)
        self.assertTrue(ok["ok"])
        refused = poll_ci(cfg, self.project, {}, now=100.0, interval=15.0,
                          branches=["a", "b", "c"], reader=reader)
        self.assertFalse(refused["ok"])
        self.assertIn("Too many", refused["error"])

    def test_per_project_lock_is_single_flight(self):
        path = self.project.root / "locks" / "vcs-poll.lock"
        with lock(path, blocking=False):
            with self.assertRaises(Busy):
                with lock(path, blocking=False):
                    pass


if __name__ == "__main__":
    raise SystemExit(unittest.main())
