"""M15: budget day follows the configured timezone (UTC default). Offline only."""
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
import datetime as real_dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mizu.budget import Budget
from mizu.config import ConfigError


def frozen(instant):
    """Patch only mizu.budget's datetime view so the clock is deterministic."""
    mdt = mock.patch("mizu.budget.dt")
    handle = mdt.start()
    handle.datetime.now.side_effect = lambda tz=None: instant.astimezone(tz)
    handle.date.fromisoformat.side_effect = real_dt.date.fromisoformat
    handle.timezone = real_dt.timezone
    return mdt


class BudgetTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_default_day_is_utc(self):
        budget = Budget(self.root, 10)
        self.assertEqual(budget.timezone, "UTC")
        self.assertEqual(budget.usage()["zone"], "UTC")
        self.assertEqual(
            budget._today(),
            real_dt.datetime.now(real_dt.timezone.utc).date().isoformat())

    def test_same_instant_names_different_days(self):
        # 2026-10-03T15:00Z is still Oct 3 in UTC but Oct 4 in Tokyo.
        instant = real_dt.datetime(2026, 10, 3, 15, 0, tzinfo=real_dt.timezone.utc)
        stopper = frozen(instant)
        self.addCleanup(stopper.stop)
        utc = Budget(self.root, 10)
        tokyo = Budget(self.root, 10, timezone="Asia/Tokyo")
        self.assertEqual(utc._today(), "2026-10-03")
        self.assertEqual(tokyo._today(), "2026-10-04")
        utc.take("u1", project="alpha")
        tokyo.take("t1", project="alpha")
        self.assertTrue((self.root / "2026-10-03.json").is_file())
        self.assertTrue((self.root / "2026-10-04.json").is_file())
        record = json.loads((self.root / "2026-10-04.json").read_text())
        self.assertEqual((record["day"], record["zone"]), ("2026-10-04", "Asia/Tokyo"))
        self.assertEqual(tokyo.usage("alpha")["day"], "2026-10-04")

    def test_dst_zone_computes_local_day(self):
        # Spring forward in New York: 07:30Z is 03:30 EDT on Mar 8.
        instant = real_dt.datetime(2026, 3, 8, 7, 30, tzinfo=real_dt.timezone.utc)
        stopper = frozen(instant)
        self.addCleanup(stopper.stop)
        budget = Budget(self.root, 10, timezone="America/New_York")
        self.assertEqual(budget._today(), "2026-03-08")
        budget.take("d1", project="alpha")
        record = json.loads((self.root / "2026-03-08.json").read_text())
        self.assertEqual(record["zone"], "America/New_York")

    def test_unknown_zone_fails_closed(self):
        with self.assertRaises(ConfigError):
            Budget(self.root, 10, timezone="Mars/Olympus")
