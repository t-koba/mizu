"""Doctor budget scope semantics: shared total vs per-project caps. Offline only."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mizu.budget import Budget
from mizu.doctor import budget_file
from mizu.errors import ConfigError


def make_config(root: Path, daily: int, shared: int = 0):
    return SimpleNamespace(
        data=root,
        limits=SimpleNamespace(daily_requests=daily, retention_days=31,
                               shared_daily_requests=shared),
        timezone="UTC",
    )


def budget_dir(root: Path) -> Path:
    target = root / "budget"
    target.mkdir(parents=True, exist_ok=True)
    return target


class DoctorBudgetScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_shared_sum_exceeding_per_project_limit_is_not_corrupt(self):
        # The reported bug: per-project cap 2, shared cap 10, three projects
        # take one each. The shared total (3) exceeds one project's cap (2)
        # without exceeding the shared cap; doctor must not cry corruption.
        config = make_config(self.root, 2, shared=10)
        ledger = Budget(budget_dir(self.root), 2, 31, 10, "UTC")
        ledger.take("r1", project="alpha")
        ledger.take("r2", project="beta")
        ledger.take("r3", project="gamma")
        report = budget_file(config)
        self.assertEqual(report["malformed"], [])
        self.assertEqual(report["shared_used"], 3)
        self.assertEqual(report["projects"], {"alpha": 1, "beta": 1, "gamma": 1})

    def test_malformed_ledger_is_corrupt(self):
        config = make_config(self.root, 100)
        day = Budget(budget_dir(self.root), 100, 31, 0, "UTC").usage()["day"]
        (self.root / "budget" / f"{day}.json").write_text(
            json.dumps({"requests": "not-a-list", "projects": {}}), encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            budget_file(config)
        self.assertIn("corrupt", str(caught.exception))

    def test_unreadable_ledger_is_corrupt(self):
        config = make_config(self.root, 100)
        day = Budget(budget_dir(self.root), 100, 31, 0, "UTC").usage()["day"]
        (self.root / "budget" / f"{day}.json").write_bytes(b"{not json")
        with self.assertRaises(ConfigError) as caught:
            budget_file(config)
        self.assertIn("corrupt", str(caught.exception))

    def test_exhausted_budgets_are_not_corrupt(self):
        # Per-project exhaustion: used == limit is admission state, not corruption.
        config = make_config(self.root, 2)
        ledger = Budget(budget_dir(self.root), 2, 31, 0, "UTC")
        ledger.take("r1", project="alpha")
        ledger.take("r2", project="alpha")
        report = budget_file(config)
        self.assertEqual(report["malformed"], [])
        self.assertEqual(report["projects"], {"alpha": 2})
        self.assertIn("alpha", report["exhausted_projects"])
        # Shared exhaustion likewise reports data instead of corruption.
        config2 = make_config(self.root, 10, shared=2)
        report2 = budget_file(config2)
        self.assertEqual(report2["malformed"], [])
        self.assertTrue(report2["shared_exhausted"])


if __name__ == "__main__":
    unittest.main()
