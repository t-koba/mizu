"""M9: per-project request budget with an optional shared total. Offline only."""
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from support import Fixture
from mizu import fs as fs_module
from mizu.budget import Budget
from mizu.config import MAX_USAGE_POLICY_COUNT, Limits, load
from mizu.errors import ConfigError, Denied, InfraExceeded
from mizu.fs import canonical, safe_read


class PerProjectBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_projects_admit_independently(self):
        budget = Budget(self.root, 1)
        self.assertEqual(budget.take("a1", project="alpha"), 1)
        # Another project is unaffected by alpha's exhaustion.
        self.assertEqual(budget.take("b1", project="beta"), 1)
        with self.assertRaises(InfraExceeded):
            budget.take("a2", project="alpha")
        self.assertEqual(budget.usage("alpha")["used"], 1)
        self.assertEqual(budget.usage("beta")["used"], 1)
        self.assertEqual(budget.usage("alpha")["shared_used"], 2)

    def test_smallest_per_config_limit_does_not_stop_other_project(self):
        # Two configs sharing one data_dir with different daily_requests.
        strict = Budget(self.root, 1)
        roomy = Budget(self.root, 100)
        strict.take("s1", project="strict-proj")
        with self.assertRaises(InfraExceeded):
            strict.take("s2", project="strict-proj")
        self.assertEqual(roomy.take("r1", project="roomy-proj"), 1)

    def test_optional_shared_total_binds_both(self):
        budget = Budget(self.root, 10, shared_daily=2)
        budget.take("a1", project="alpha")
        budget.take("b1", project="beta")
        with self.assertRaises(InfraExceeded) as caught:
            budget.take("a2", project="alpha")
        self.assertIn("shared", str(caught.exception))
        usage = budget.usage("alpha")
        self.assertEqual((usage["used"], usage["limit"]), (1, 10))
        self.assertEqual((usage["shared_used"], usage["shared_limit"]), (2, 2))

    def test_peek_is_bounded_read_only_and_does_not_reap(self):
        budget = Budget(self.root, 5)
        budget.take("one", project="alpha")
        with patch.object(budget, "gc", side_effect=AssertionError("gc called")):
            report = budget.peek("alpha")
        self.assertEqual(report["status"], "current")
        self.assertEqual((report["used"], report["shared_used"]), (1, 1))
        self.assertEqual(budget.peek("beta")["used"], 0)
        path = self.root / f"{budget._today()}.json"
        path.write_text("not json")
        self.assertEqual(budget.peek("alpha")["status"], "unavailable")
        self.assertNotIn("used", budget.peek("alpha"))

    def test_custom_day_file_bound_controls_write_and_peek_only(self):
        default = Budget(self.root, 5)
        self.assertEqual(default.day_file_bytes, 4 * 1024 * 1024)
        day = default._today()
        body = canonical({"day": day, "zone": "UTC", "requests": ["one"],
                          "projects": {"alpha": ["one"]}})
        budget = Budget(self.root, 5, day_file_bytes=len(body))
        # Exact-size files fit both the writer and bounded reader.
        self.assertEqual(budget.take("one", day=day, project="alpha"), 1)
        path = self.root / f"{day}.json"
        self.assertEqual(path.read_bytes(), body)
        with patch("mizu.budget.safe_read", wraps=safe_read) as read:
            self.assertEqual(budget.peek("alpha")["used"], 1)
        read.assert_called_once_with(self.root, f"{day}.json", len(body))
        with self.assertRaisesRegex(InfraExceeded, "Budget day-file exceeds byte bound"):
            budget.take("two", day=day, project="alpha")
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(budget.take("one", day=day, project="alpha"), 1)
        # A larger budget admits the next request without changing count caps.
        self.assertEqual(default.take("two", day=day, project="alpha"), 2)
        with patch("mizu.budget.safe_read", side_effect=AssertionError("oversize read")):
            self.assertEqual(budget.peek("alpha"),
                             {"status": "unavailable", "project": "alpha"})
        # Legacy usage still reads a file larger than the selected byte budget.
        self.assertEqual(budget.usage("alpha")["used"], 2)

    def test_peek_at_large_bounds_reads_from_file_size_not_limit(self):
        class StreamSpy:
            def __init__(self, stream, sizes):
                self.stream, self.sizes = stream, sizes

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def read(self, size=-1):
                self.sizes.append(size)
                return self.stream.read(size)

        maxima = (MAX_USAGE_POLICY_COUNT // 2, MAX_USAGE_POLICY_COUNT)
        branches = [("portable", False)]
        if fs_module._platform.HAS_OPENAT and fs_module._platform.HAS_DIR_FD:
            branches.insert(0, ("openat", True))
        original_fdopen = fs_module.os.fdopen
        original_path_open = Path.open

        for maximum in maxima:
            with self.subTest(maximum=maximum):
                budget = Budget(self.root, 5, day_file_bytes=maximum)
                day = budget._today()
                budget.take("one", day=day, project="alpha")
                path = self.root / f"{day}.json"
                expected_read_size = path.stat().st_size + 1
                self.assertLess(expected_read_size, maximum)
                for branch, use_openat in branches:
                    with self.subTest(branch=branch):
                        sizes = []

                        def tracked_fdopen(fd, *args, **kwargs):
                            return StreamSpy(original_fdopen(fd, *args, **kwargs), sizes)

                        def tracked_path_open(target, *args, **kwargs):
                            return StreamSpy(original_path_open(target, *args, **kwargs), sizes)

                        with patch.object(fs_module._platform, "HAS_OPENAT", use_openat), \
                                patch.object(fs_module._platform, "HAS_DIR_FD", use_openat), \
                                patch.object(fs_module.os, "fdopen", side_effect=tracked_fdopen), \
                                patch.object(Path, "open", tracked_path_open):
                            report = budget.peek("alpha")
                        self.assertEqual(report["status"], "current")
                        self.assertEqual(report["used"], 1)
                        self.assertEqual(sizes, [expected_read_size, 1])

    def test_usage_reports_both_and_rejects_bad_names(self):
        budget = Budget(self.root, 5)
        budget.take("a1", project="alpha")
        named = budget.usage("alpha")
        self.assertEqual(named["used"], 1)
        self.assertIn("shared_used", named)
        self.assertIn("shared_limit", named)
        shared = budget.usage()
        self.assertEqual(shared["used"], 1)
        with self.assertRaises(Denied):
            budget.take("x", project="../escape")
        with self.assertRaises(Denied):
            budget.usage("../escape")


class BudgetConfigTests(Fixture):
    def test_day_file_default_override_and_ranges(self):
        self.assertEqual(Limits().budget_day_file_bytes, 4 * 1024 * 1024)
        self.assertEqual(self.config.limits.budget_day_file_bytes, 4 * 1024 * 1024)
        original = self.file.read_text()
        setting = "budget_day_file_bytes = 4194304"
        self.assertIn(setting, original)
        self.file.write_text(original.replace(setting, ""))
        self.assertEqual(load(self.file).limits.budget_day_file_bytes, 4 * 1024 * 1024)
        accepted = tuple(map(str, (1, 4194304, 8388608, MAX_USAGE_POLICY_COUNT)))
        for value in (*accepted, "0", "-1", str(MAX_USAGE_POLICY_COUNT + 1),
                      "true", "false", "1.5", '\"1\"', "[]", "{}"):
            with self.subTest(value=value):
                self.file.write_text(original.replace(setting, f"budget_day_file_bytes = {value}"))
                if value in accepted:
                    self.assertEqual(load(self.file).limits.budget_day_file_bytes, int(value))
                else:
                    with self.assertRaisesRegex(ConfigError, "limits.budget_day_file_bytes"):
                        load(self.file)

    def test_explain_peek_uses_configured_day_file_bound(self):
        from mizu.explain import _budget
        Budget(self.config.data / "budget", 5).take("one", project="sample")
        self.assertEqual(_budget(self.config, "sample")["status"], "current")
        self.file.write_text(self.file.read_text().replace(
            "budget_day_file_bytes = 4194304", "budget_day_file_bytes = 1"))
        report = _budget(load(self.file), "sample")
        self.assertEqual(report, {"status": "unavailable", "scope": "project", "counters": None})

    def test_shared_total_is_optional_and_bounded(self):
        self.assertEqual(self.config.limits.shared_daily_requests, 0)
        self.file.write_text(self.file.read_text().replace(
            "shared_daily_requests = 0", "shared_daily_requests = 50"))
        self.assertEqual(load(self.file).limits.shared_daily_requests, 50)
        self.file.write_text(self.file.read_text().replace(
            "shared_daily_requests = 50", "shared_daily_requests = 100001"))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_cli_budget_and_status_report_both(self):
        from mizu.cli import execute, parser
        self.config.data.joinpath("budget").mkdir(parents=True, exist_ok=True)
        Budget(self.config.data / "budget",
               self.config.limits.daily_requests,
               self.config.limits.retention_days,
               self.config.limits.shared_daily_requests).take(
                   "run:1", project="sample")
        args = parser().parse_args(["--config", str(self.file), "budget", "sample"])
        report = execute(args)
        self.assertEqual(report["project"], "sample")
        self.assertEqual(report["used"], 1)
        self.assertIn("shared_used", report)
        status = self.project.status()
        self.assertIn("shared_used_requests", status["budget"])
        self.assertIn("shared_limit_requests", status["budget"])
        self.assertEqual(status["budget"]["used_requests"], 1)
