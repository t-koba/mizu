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

    def test_per_role_overrides_never_raise_probe_caps(self):
        import dataclasses
        from mizu.config import role_requests_per_run, role_tools_per_run
        # Auditor-style high overrides still probe at the fixed 4/5 caps.
        high = dataclasses.replace(self.config.roles["consult"],
                                   requests_per_run=200, tools_per_run=400)
        config = dataclasses.replace(self.config, roles={**self.config.roles, "consult": high})
        seen = {}
        def capture(driver_self, project, role_name):
            cfg = driver_self.config
            seen["effective_requests"] = role_requests_per_run(cfg, cfg.roles[role_name])
            seen["effective_tools"] = role_tools_per_run(cfg, cfg.roles[role_name])
            seen.update(cfg.limits.__dict__)
            raise RuntimeError("stop after capture")
        import unittest.mock
        with unittest.mock.patch.object(Engine, "run", capture), \
                unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False):
            try:
                smoke.live(config, role_name="consult")
            except RuntimeError:
                pass
        self.assertEqual(seen["effective_requests"], 4)
        self.assertEqual(seen["effective_tools"], 5)
        # A lower per-role override stays lower, never raised to the caps.
        low_role = dataclasses.replace(self.config.roles["consult"],
                                       requests_per_run=1, tools_per_run=1)
        config_low = dataclasses.replace(self.config, roles={**self.config.roles, "consult": low_role})
        seen.clear()
        with unittest.mock.patch.object(Engine, "run", capture), \
                unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False):
            try:
                smoke.live(config_low, role_name="consult")
            except RuntimeError:
                pass
        self.assertLessEqual(seen["effective_requests"], 1)
        self.assertLessEqual(seen["effective_tools"], 1)

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
        from mizu.errors import DeadlineWrapUp
        with self.assertRaises(DeadlineWrapUp):
            ctx2.handle("_budget", {"sequence": 3})
        self.assertEqual(ctx2.wrap_up_requests, 0)
        ctx2.wrap_up_attempted = True
        # Reduced-grant requests draw the fixed wrap-up allowance (3) before
        # the bound refuses again.
        ctx2.handle("_budget", {"sequence": 3})
        ctx2.handle("_budget", {"sequence": 4})
        ctx2.handle("_budget", {"sequence": 5})
        self.assertEqual(ctx2.bound_hit, "requests")
        with self.assertRaisesRegex(LimitExceeded, "Per-run provider request budget exhausted"):
            ctx2.handle("_budget", {"sequence": 6})

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
        from mizu.smoke_retention import Metadata, read_pair
        pairs = list((self.config.data / "validation/pairs").iterdir())
        self.assertEqual(len(pairs), 1)
        manifest = read_pair(Metadata(self.config.data), pairs[0].name)
        self.assertEqual(manifest["status"], "fail")
        self.assertIsNotNone(manifest["project"])
        self.assertEqual(list((self.config.data / "validation/smoke-runs").iterdir()), [])
        self.assertEqual(list((self.config.data / "projects").iterdir()), [self.project.root])

    def test_new_bundle_preserves_result_and_provider_has_no_retention_lock(self):
        from pathlib import Path
        from mizu.smoke_refs import shared_lock
        from mizu.smoke_retention import Metadata, read_pair
        seen = {}
        def fake_run(engine, project, role_name):
            scratch = project.root.parent.parent
            smoke_id = project.name.removeprefix("smoke-")
            self.assertRegex(scratch.name, "^" + smoke_id + r"-[a-z0-9_]+$")
            self.assertEqual(scratch.parent, self.config.data / "validation/smoke-runs")
            self.assertEqual(engine.config.data, self.config.data)
            self.assertEqual(project.config.data, self.config.data)
            self.assertEqual(project.root, scratch / "projects" / ("smoke-" + smoke_id))
            self.assertEqual(list((self.config.data / "projects").iterdir()), [self.project.root])
            seen["identity"] = {"device": project.root.stat().st_dev, "inode": project.root.stat().st_ino}
            # The production retention lock does not span execution.
            with shared_lock(self.config.data):
                pass
            return {"finish": {"summary": (project.workspace / "probe.txt").read_text()}}
        with unittest.mock.patch.object(Engine, "run", fake_run), \
                unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False):
            result = smoke.live(self.config, role_name="consult")
        self.assertRegex(result["smoke_id"], r"^[0-9a-f]{32}$")
        pair = Path(result["pair"])
        self.assertEqual(pair, self.config.data / "validation/pairs" / result["smoke_id"])
        self.assertEqual(set(p.name for p in pair.iterdir()), {"manifest.json", "report.json", "project"})
        manifest = read_pair(Metadata(self.config.data), result["smoke_id"])
        self.assertEqual(manifest["status"], "pass")
        self.assertEqual(manifest["project"], seen["identity"])
        from mizu.fs import canonical
        self.assertEqual((pair / "report.json").read_bytes(), canonical(result))
        self.assertEqual(list((self.config.data / "validation/smoke-runs").iterdir()), [])
        self.assertTrue(result["result"]["finish"]["summary"])
        self.assertEqual(Path(result["evidence"]), pair / "project")

    def test_publication_failure_rolls_back_and_cleans_isolated_root(self):
        import os
        from pathlib import Path
        from mizu import smoke_retention
        rename = os.rename
        moved = []
        def fail_pair(source, destination):
            destination = Path(destination)
            if destination.parent == self.config.data / "validation/pairs":
                self.assertTrue((Path(source) / "project").is_dir())
                raise OSError("synthetic pair publication failure")
            if destination.name == "project":
                moved.append(Path(source))
            return rename(source, destination)
        def fake_run(engine, project, role_name):
            return {"finish": {"summary": (project.workspace / "probe.txt").read_text()}}
        with unittest.mock.patch.object(Engine, "run", fake_run), \
                unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False), \
                unittest.mock.patch.object(smoke_retention.os, "rename", side_effect=fail_pair):
            with self.assertRaisesRegex(OSError, "synthetic pair publication failure"):
                smoke.live(self.config, role_name="consult")
        self.assertEqual(len(moved), 1)
        for directory in ("smoke-runs", "staging", "pairs"):
            self.assertEqual(list((self.config.data / "validation" / directory).iterdir()), [])
        self.assertEqual(list((self.config.data / "projects").iterdir()), [self.project.root])

    def test_scratch_ancestors_refuse_links_before_creating_children(self):
        target = self.config.data / "alias-target"
        target.mkdir()
        validation = self.config.data / "validation"
        for path in (validation, validation / "smoke-runs"):
            with self.subTest(path=path):
                try:
                    path.symlink_to(target, target_is_directory=True)
                except OSError:
                    self.skipTest("directory symlinks unavailable")
                try:
                    with unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False), \
                            unittest.mock.patch.object(Engine, "run") as run:
                        with self.assertRaises(Denied):
                            smoke.live(self.config, role_name="consult")
                        run.assert_not_called()
                    self.assertEqual(list(target.iterdir()), [])
                finally:
                    path.unlink()
                if path == validation:
                    validation.mkdir()

    def test_cleanup_leaves_replaced_scratch_identity_untouched(self):
        seen = {}
        def replace_scratch(config, name, *args, _smoke_root, **kwargs):
            scratch = _smoke_root.parent.parent
            original = scratch.with_name(scratch.name + "-original")
            scratch.rename(original)
            scratch.mkdir()
            marker = scratch / "replacement.txt"
            marker.write_text("not owned by this probe")
            seen.update(scratch=scratch, original=original, marker=marker)
            raise Denied("synthetic scratch replacement")
        with unittest.mock.patch("mizu.smoke.initialize", side_effect=replace_scratch), \
                unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False):
            with self.assertRaisesRegex(Denied, "synthetic scratch replacement"):
                smoke.live(self.config, role_name="consult")
        self.assertEqual(seen["marker"].read_text(), "not owned by this probe")
        self.assertTrue(seen["original"].is_dir())

    def test_internal_root_override_refuses_managed_and_unbound_roots(self):
        from mizu.project import Project, initialize
        key = "a" * 32
        name = "smoke-" + key
        for root in (self.config.data / "projects" / name,
                     self.config.data / "validation/smoke-runs" / ("b" * 32) / "projects" / name):
            with self.subTest(root=root):
                with self.assertRaises(Denied):
                    initialize(self.config, name, self.project.workspace,
                               self.project.root / "PROJECT.md", ["consult"], [], _smoke_root=root)
                with self.assertRaises(Denied):
                    Project(self.config, name, _smoke_root=root)
                self.assertFalse(root.exists())

    def test_initialization_failure_publishes_complete_report_without_project(self):
        from mizu.smoke_retention import Metadata, read_pair
        with unittest.mock.patch("mizu.smoke.initialize", side_effect=Denied("synthetic init failure")), \
                unittest.mock.patch("mizu.smoke._platform.is_root", return_value=False):
            with self.assertRaisesRegex(Denied, "synthetic init failure"):
                smoke.live(self.config, role_name="consult")
        pairs = list((self.config.data / "validation/pairs").iterdir())
        self.assertEqual(len(pairs), 1)
        manifest = read_pair(Metadata(self.config.data), pairs[0].name)
        self.assertEqual(manifest["status"], "fail")
        self.assertIsNone(manifest["project"])
        self.assertEqual(list((self.config.data / "validation/smoke-runs").iterdir()), [])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
