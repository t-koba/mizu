"""The per-engine start-up record that both measurement endpoints write.

ADR-042 states the rules this record keeps: it holds the largest interval
measured for an engine together with the endpoint that measured it, so a
one-shot probe's spawn-to-exit sample and a managed runtime's
spawn-to-first-answer sample are never published as one unlabeled
measurement; the published bound derives from the published cost; an engine
this host has never measured keeps the floor; and a record this host cannot
read or write measures nothing. These regressions pin those rules, so a
record that overwrites, that mixes engines, that widens the bound from a
malformed entry or that publishes a pair contradicting the published rule is
caught here.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mizu.startup import (ADAPTER_STARTUP_CEILING_SECONDS, ADAPTER_STARTUP_FLOOR_SECONDS,
                          PROBE_ENDPOINT, RUNTIME_ENDPOINT, adapter_startup_bound,
                          measured_startup, observed_startup, record_path,
                          record_startup, startup_bound, startup_sample)


def config_for():
    """A configuration whose data directory holds this host's record."""
    data = tempfile.mkdtemp(prefix="mizu-startup-record-")
    return SimpleNamespace(data=Path(data))


class StartupRecordTests(unittest.TestCase):
    def test_a_measurement_never_lowers_the_record(self):
        config = config_for()
        # A host that was loaded once keeps the bound that load required.
        record_startup(config, "pi", 20.0, PROBE_ENDPOINT)
        record_startup(config, "pi", 5.0, PROBE_ENDPOINT)
        self.assertEqual(measured_startup(config, "pi"), 20.0)
        self.assertEqual(startup_bound(config, "pi"), adapter_startup_bound(20.0))
        self.assertEqual(startup_bound(config, "pi"), ADAPTER_STARTUP_CEILING_SECONDS)

    def test_the_record_names_the_endpoint_that_measured_it(self):
        # The two endpoints measure different intervals, so the record keeps
        # the endpoint with the retained interval and the published evidence
        # can name it.
        config = config_for()
        record_startup(config, "pi", 12.0, PROBE_ENDPOINT)
        self.assertEqual(startup_sample(config, "pi")["endpoint"], PROBE_ENDPOINT)
        record_startup(config, "pi", 20.0, RUNTIME_ENDPOINT)
        self.assertEqual(measured_startup(config, "pi"), 20.0)
        self.assertEqual(startup_sample(config, "pi")["endpoint"], RUNTIME_ENDPOINT)

    def test_the_two_endpoints_feed_one_record(self):
        # The probe measures spawn through process exit; the runtime
        # measures spawn through its first protocol answer. They are
        # distinct endpoints, and the record keeps the larger interval for
        # the engine whichever order the two samples arrive in.
        for first, second in ((12.0, 4.0), (4.0, 12.0)):
            config = config_for()
            record_startup(config, "pi", first, PROBE_ENDPOINT)
            record_startup(config, "pi", second, RUNTIME_ENDPOINT)
            self.assertEqual(measured_startup(config, "pi"), 12.0)
            self.assertEqual(startup_bound(config, "pi"), adapter_startup_bound(12.0))

    def test_each_engine_keeps_its_own_record(self):
        config = config_for()
        record_startup(config, "pi", 20.0, PROBE_ENDPOINT)
        # Another engine's cost never widens this one's bound: the record
        # is per engine.
        self.assertIsNone(measured_startup(config, "codex"))
        self.assertEqual(startup_bound(config, "codex"), ADAPTER_STARTUP_FLOOR_SECONDS)
        self.assertEqual(startup_bound(config, "pi"), ADAPTER_STARTUP_CEILING_SECONDS)

    def test_an_unmeasured_engine_keeps_the_floor(self):
        config = config_for()
        self.assertIsNone(measured_startup(config, "pi"))
        self.assertEqual(startup_bound(config, "pi"), ADAPTER_STARTUP_FLOOR_SECONDS)

    def test_a_malformed_record_measures_nothing(self):
        config = config_for()
        path = record_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        # A record that cannot be read measures nothing, so the floor
        # applies: it never widens the bound on its own.
        self.assertIsNone(measured_startup(config, "pi"))
        self.assertEqual(startup_bound(config, "pi"), ADAPTER_STARTUP_FLOOR_SECONDS)

    def test_a_malformed_record_is_replaced_by_the_next_measurement(self):
        # A record that cannot be parsed holds no measurement, so the next
        # one replaces it instead of leaving the host at the floor for ever.
        config = config_for()
        path = record_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        record_startup(config, "pi", 7.0, PROBE_ENDPOINT)
        self.assertEqual(measured_startup(config, "pi"), 7.0)
        self.assertEqual(startup_bound(config, "pi"), adapter_startup_bound(7.0))

    def test_an_unusable_entry_measures_nothing(self):
        config = config_for()
        path = record_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        for entry in ({"seconds": 0}, {"seconds": -1}, {"seconds": "slow"}, {}, {"seconds": True}):
            path.write_text(json.dumps({"pi": entry}), encoding="utf-8")
            self.assertIsNone(measured_startup(config, "pi"), entry)
            self.assertEqual(startup_bound(config, "pi"), ADAPTER_STARTUP_FLOOR_SECONDS, entry)

    def test_the_recorded_interval_derives_the_bound(self):
        config = config_for()
        cost, bound, endpoint = observed_startup(config, "pi", 7.0, PROBE_ENDPOINT)
        self.assertEqual(cost, 7.0)
        self.assertEqual(bound, adapter_startup_bound(7.0))
        self.assertEqual(bound, 84)
        self.assertEqual(endpoint, PROBE_ENDPOINT)
        self.assertEqual(measured_startup(config, "pi"), 7.0)

    def test_the_recorded_cost_is_the_one_the_bound_came_from(self):
        # The published cost is the one the published bound was derived
        # from: a smaller later sample never lowers the published pair.
        config = config_for()
        record_startup(config, "pi", 20.0, PROBE_ENDPOINT)
        cost, bound, endpoint = observed_startup(config, "pi", 3.0, RUNTIME_ENDPOINT)
        self.assertEqual(cost, 20.0)
        self.assertEqual(bound, adapter_startup_bound(20.0))
        self.assertEqual(endpoint, PROBE_ENDPOINT)

    def test_an_unusable_record_publishes_no_cost_and_the_floor(self):
        # The host's record cannot be written or read back, so it holds no
        # measurement the surfaces can share. The published pair must still
        # satisfy the published rule: no cost, and the floor bound.
        blocker = Path(tempfile.mkdtemp(prefix="mizu-startup-record-")) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        config = SimpleNamespace(data=blocker / "data")
        cost, bound, endpoint = observed_startup(config, "pi", 20.0, PROBE_ENDPOINT)
        self.assertIsNone(cost)
        self.assertEqual(bound, ADAPTER_STARTUP_FLOOR_SECONDS)
        self.assertIsNone(endpoint)
        # The rule holds for the published pair: the bound is twelve times
        # the published cost, at least the floor and at most the ceiling.
        self.assertEqual(adapter_startup_bound(cost), bound)

    def test_an_unknown_endpoint_is_not_recorded(self):
        config = config_for()
        record_startup(config, "pi", 20.0, "somewhere-else")
        self.assertIsNone(measured_startup(config, "pi"))


if __name__ == "__main__":
    unittest.main()
