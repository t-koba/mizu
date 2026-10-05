"""M6 smoke probe: unambiguous goal plus a bounded 4-request margin.

Offline regression for the 2026-10-03 live failure where the probe capped at
2 requests and the contributor model listed files before reading probe.txt
(3 sequences) hit "Per-run provider request budget exhausted".
Fake-engine scenario only; no provider, no container, no network.
"""
import unittest.mock
from support import Fixture, ScriptDriver
from mizu import smoke
from mizu.errors import Denied
from mizu.runtime import Engine


class SmokeProbeTests(Fixture):
    def test_goal_names_exact_path_and_discourages_listing(self):
        seen = {}
        real_run = Engine.run

        def capture(driver_self, project, role_name):
            seen["goal"] = project.goal
            seen["limits"] = driver_self.config.limits
            raise RuntimeError("stop after capture")

        with unittest.mock.patch.object(Engine, "run", capture), \
                unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False):
            try:
                smoke.live(self.config, role_name="consult")
            except RuntimeError:
                pass
        self.assertIn("exact path probe.txt", seen["goal"])
        self.assertIn("Do not list files first", seen["goal"])
        self.assertIn("mizu_finish alone", seen["goal"])
        self.assertIn("exactly the file contents", seen["goal"])

    def test_probe_budget_is_four_and_never_raises_operator_limit(self):
        self.assertEqual(smoke.SMOKE_REQUESTS_PER_RUN, 4)
        self.assertEqual(smoke.SMOKE_TOOLS_PER_RUN, 5)
        self.assertEqual(smoke.SMOKE_RUN_SECONDS, 120)
        seen = {}

        def capture(driver_self, project, role_name):
            seen.update(driver_self.config.limits.__dict__)
            raise RuntimeError("stop after capture")

        with unittest.mock.patch.object(Engine, "run", capture), \
                unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False):
            try:
                smoke.live(self.config, role_name="consult")
            except RuntimeError:
                pass
        self.assertEqual(seen["requests_per_run"], 4)
        self.assertEqual(seen["tools_per_run"], 5)
        # A low operator limit is respected, never raised.
        import dataclasses
        low = dataclasses.replace(self.config.limits, requests_per_run=1, tools_per_run=1)
        config = dataclasses.replace(self.config, limits=low)
        seen.clear()
        with unittest.mock.patch.object(Engine, "run", capture), \
                unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False):
            try:
                smoke.live(config, role_name="consult")
            except RuntimeError:
                pass
        self.assertLessEqual(seen["requests_per_run"], 1)
        self.assertLessEqual(seen["tools_per_run"], 1)

    def test_listing_plus_read_plus_finish_fits_new_cap(self):
        # The observed live shape: files, read, finish consume 3 provider
        # sequences. Under the old cap of 2 the third admission raised
        # LimitExceeded; under the fixed cap of 4 it is admitted.
        def listing_work(ctx, *_):
            ctx.handle("_budget", {"sequence": 1})
            ctx.handle("files", {})
            ctx.handle("_budget", {"sequence": 2})
            ctx.handle("read", {"path": "probe.txt"})
            ctx.handle("_budget", {"sequence": 3})
            ctx.handle("finish", {"outcome": "wait", "summary": "mizu-probe-x"})

        ctx = self.context("consult")
        import dataclasses
        from mizu.config import Config
        capped = dataclasses.replace(
            self.config.limits, requests_per_run=smoke.SMOKE_REQUESTS_PER_RUN,
            tools_per_run=smoke.SMOKE_TOOLS_PER_RUN)
        ctx.config = dataclasses.replace(self.config, limits=capped)
        (ctx.workspace / "probe.txt").write_text("mizu-probe-x")
        listing_work(ctx)
        self.assertEqual(ctx.request_count, 3)
        # Old cap would have refused the third sequence.
        from mizu.errors import LimitExceeded
        ctx2 = self.context("consult")
        ctx2.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, requests_per_run=2, tools_per_run=5))
        ctx2.handle("_budget", {"sequence": 1})
        ctx2.handle("_budget", {"sequence": 2})
        with self.assertRaisesRegex(LimitExceeded, "Per-run provider request budget exhausted"):
            ctx2.handle("_budget", {"sequence": 3})

    def test_probe_policy_ignores_operator_consult_text(self):
        # Live smoke must test the engine/provider path only: even a consult
        # policy that forbids workspace reads must not reach the model.
        hostile = "Advise only from the given snapshot. Never read workspace files.\n"
        policy_path = next(iter(self.config.roles["consult"].policy))
        policy_path.write_bytes(hostile.encode("utf-8"))
        seen = {}
        real_run = Engine.run

        def capture(driver_self, project, role_name):
            from mizu.config import role_policy_text
            seen["policy"] = role_policy_text(driver_self.config.roles[role_name])
            raise RuntimeError("stop after capture")

        with unittest.mock.patch.object(Engine, "run", capture), \
                unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False):
            try:
                smoke.live(self.config, role_name="consult")
            except RuntimeError:
                pass
        self.assertEqual(seen["policy"], smoke.SMOKE_PROBE_POLICY)
        self.assertNotIn("Never read workspace files", seen["policy"])
        self.assertIn("exact path probe.txt", seen["policy"])

    def test_evidence_check_still_refuses_wrong_summary(self):
        def wrong_summary(ctx, *_):
            ctx.handle("_budget", {"sequence": 1})
            ctx.handle("read", {"path": "probe.txt"})
            ctx.handle("finish", {"outcome": "wait", "summary": "wrong contents"})

        with unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False), \
                unittest.mock.patch.object(Engine, "run", lambda self, project, role: {
                    "finish": {"summary": "wrong contents"}}):
            with self.assertRaisesRegex(Denied, "did not return the requested evidence"):
                smoke.live(self.config, role_name="consult")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
