"""M9: per-project request budget with an optional shared total. Offline only."""
import tempfile
import unittest
from pathlib import Path
from support import Fixture
from mizu.budget import Budget
from mizu.config import load
from mizu.errors import Denied, InfraExceeded


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
    def test_shared_total_is_optional_and_bounded(self):
        self.assertEqual(self.config.limits.shared_daily_requests, 0)
        self.file.write_text(self.file.read_text().replace(
            "shared_daily_requests = 0", "shared_daily_requests = 50"))
        self.assertEqual(load(self.file).limits.shared_daily_requests, 50)
        self.file.write_text(self.file.read_text().replace(
            "shared_daily_requests = 50", "shared_daily_requests = 100001"))
        from mizu.errors import ConfigError
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
