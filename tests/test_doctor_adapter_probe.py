"""Adapter probe bounds: the host-relative bound and the refusal it produces.

The adapter launcher and the app-server probe are children of this process
whose start-up is dominated by loading their SDK. A fixed bound kills such a
child on a loaded host before it can report, and the kill was reported as a
missing SDK capability. These regressions hold both halves: the probe is
bounded by the host-relative ceiling, and a child that outlives its bound is
reported as a bound refusal that names its own reason.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from support import Fixture
from mizu import doctor
from mizu.engine_config import adapter_contract
from mizu.errors import ConfigError
from mizu.process import Result


def load_check_cli():
    """The installed-adapter contract script, loaded as a module."""
    spec = importlib.util.spec_from_file_location("check_cli_probe", ROOT / "scripts/check-cli.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config_for(engine="pi", data=None):
    """A configuration with a command and the data directory the host record
    of measured start-up costs lives in."""
    if data is None:
        data = tempfile.mkdtemp(prefix="mizu-startup-")
    return SimpleNamespace(command=lambda name: ["node"], data=Path(data))


class AdapterStartupBoundTests(unittest.TestCase):
    def test_bound_follows_the_measured_cost(self):
        # Unmeasured or absent cost keeps the floor: a child that has not
        # answered cannot claim a wider bound.
        self.assertEqual(doctor.adapter_startup_bound(None), 60)
        self.assertEqual(doctor.adapter_startup_bound(0), 60)
        self.assertEqual(doctor.adapter_startup_bound("slow"), 60)
        # Twelve times the measured cost, with the floor and the ceiling.
        self.assertEqual(doctor.adapter_startup_bound(1.0), 60)
        self.assertEqual(doctor.adapter_startup_bound(5.0), 60)
        self.assertEqual(doctor.adapter_startup_bound(5.1), 62)
        self.assertEqual(doctor.adapter_startup_bound(12.5), 150)
        self.assertEqual(doctor.adapter_startup_bound(30.2), 150)

    def test_the_product_and_the_harness_hold_one_shape(self):
        # The harness holds the identical child to a 60 s floor and a
        # twelvefold multiplier; the product must not keep a second answer
        # for the same child's start-up. Every term of the shape is pinned,
        # including the child's ceiling and the ceiling the measuring spawn
        # carries: two ceilings would let the two surfaces measure the same
        # child under different bounds and derive two different answers.
        launcher = (ROOT / "tests" / "durable.test.mjs").read_text(encoding="utf-8")
        self.assertIn("LAUNCHER_STARTUP_FLOOR_MS = %d"
                      % (doctor.ADAPTER_STARTUP_FLOOR_SECONDS * 1000), launcher)
        self.assertIn("LAUNCHER_STARTUP_MULTIPLIER = %d"
                      % doctor.ADAPTER_STARTUP_MULTIPLIER, launcher)
        self.assertIn("LAUNCHER_CHILD_CEILING_MS = %d"
                      % (doctor.ADAPTER_STARTUP_CEILING_SECONDS * 1000), launcher)
        self.assertIn("LAUNCHER_MEASURE_CEILING_MS = %d"
                      % (doctor.ADAPTER_STARTUP_MEASURE_CEILING_SECONDS * 1000), launcher)

    def test_the_probe_is_held_to_the_bound_the_measurement_requires(self):
        seen = []

        def fake_run(argv, *, timeout, maximum, **kwargs):
            seen.append(timeout)
            return Result(0, "", "", "exited", 1.0)

        original = doctor.run
        doctor.run = fake_run
        self.addCleanup(setattr, doctor, "run", original)
        doctor.adapter_probe(config_for(), "pi",
                             ["node", "launcher.mjs", "--check-contract"],
                             maximum=8192, label="probe")
        # The measurement carries the ceiling, and the probe that follows is
        # held to the bound that measurement requires.
        self.assertEqual(seen[0], doctor.ADAPTER_STARTUP_MEASURE_CEILING_SECONDS)
        self.assertEqual(seen[1], doctor.adapter_startup_bound(1.0))
        # The legacy fixed bound killed a child the host-relative rule allows.
        self.assertNotEqual(seen[1], 30)


class AdapterProbeOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.original = doctor.run
        self.addCleanup(setattr, doctor, "run", self.original)

    def probe_with(self, result):
        doctor.run = lambda argv, **kwargs: result
        return doctor.engine_contract(config_for(), "pi")

    def test_a_child_that_outlives_its_bound_is_not_a_missing_capability(self):
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(None, "", "", "timeout", 120.0))
        message = str(caught.exception)
        self.assertIn("bound", message)
        self.assertIn("timeout", message)
        self.assertNotIn("SDK capabilities missing", message)

    def test_a_child_that_does_not_complete_names_its_reason(self):
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(None, "", "", "output_limit", 120.0))
        self.assertIn("output_limit", str(caught.exception))

    def test_a_missing_export_is_still_a_missing_export(self):
        report = json.dumps({"exports": []})
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(0, report, "", "exited", 2.0))
        self.assertIn("missing exports", str(caught.exception))

    def test_a_passing_probe_reports_the_host_cost(self):
        exports = adapter_contract("pi")["exports"]
        report = json.dumps({"exports": exports})
        details = self.probe_with(Result(0, report, "", "exited", 3.75))
        self.assertEqual(details["startup_seconds"], 3.75)
        # The published bound is the bound the child was held to.
        self.assertEqual(details["host_relative_bound_seconds"],
                         doctor.adapter_startup_bound(3.75))
        self.assertEqual(details["host_relative_bound_seconds"], 60)

    def test_the_published_pair_follows_the_rule_it_reports(self):
        # The measurement is slow and the probe that follows is fast: the
        # published cost is the one the bound was derived from, so the pair
        # is checkable against the rule it reports.
        exports = adapter_contract("pi")["exports"]
        report = json.dumps({"exports": exports})
        costs = [20.0, 3.0]

        def fake_run(argv, **kwargs):
            return Result(0, report, "", "exited", costs.pop(0))

        doctor.run = fake_run
        details = doctor.engine_contract(config_for(), "pi")
        self.assertEqual(details["startup_seconds"], 20.0)
        self.assertEqual(details["host_relative_bound_seconds"], 150)
        # The published pair satisfies the published rule.
        self.assertEqual(doctor.adapter_startup_bound(details["startup_seconds"]),
                         details["host_relative_bound_seconds"])

    def test_a_child_a_signal_stopped_is_not_a_missing_capability(self):
        # The reachable shape: a child a signal stopped is reported as
        # exited with the signal's number as its exit code.
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(-9, "", "", "exited", 1.0))
        message = str(caught.exception)
        self.assertIn("killed", message)
        self.assertIn("9", message)
        self.assertNotIn("SDK capabilities missing", message)


class ProbeOutcomeTests(unittest.TestCase):
    """One classification for every probe the product runs."""

    def test_a_child_that_outlived_its_bound_names_the_bound(self):
        with self.assertRaises(ConfigError) as caught:
            doctor.probe_outcome("timeout", None, "", "Node capability probe", 10)
        message = str(caught.exception)
        self.assertIn("exceeded its 10 s bound", message)
        self.assertIn("timeout", message)
        # A child that never answered never said a capability was missing.
        self.assertNotIn("missing", message)

    def test_a_child_that_was_killed_names_its_own_outcome(self):
        with self.assertRaises(ConfigError) as caught:
            doctor.probe_outcome("killed", -9, "", "Adapter contract probe")
        message = str(caught.exception)
        self.assertIn("killed", message)
        self.assertNotIn("missing", message)

    def test_a_signal_stopped_child_names_the_signal(self):
        # A child the host stopped is reported as exited with the signal's
        # number as its exit code, so the exit code is what names the kill.
        with self.assertRaises(ConfigError) as caught:
            doctor.probe_outcome("exited", -15, "", "Adapter contract probe")
        message = str(caught.exception)
        self.assertIn("killed by signal 15", message)
        self.assertNotIn("missing", message)

    def test_a_child_that_answered_keeps_its_exit_code(self):
        # A child that completed said what it holds; its exit code is the
        # content judgement and stays with the caller.
        self.assertIsNone(doctor.probe_outcome("exited", 1, "", "probe", 10))


class SandboxImageProbeTests(Fixture):
    """The image probe reads the child's own outcome."""

    def probe_with(self, result):
        original = doctor.run
        doctor.run = lambda argv, **kwargs: result
        self.addCleanup(setattr, doctor, "run", original)
        return doctor.sandbox_image(self.config)

    def test_a_killed_inspect_is_not_a_missing_image(self):
        # A child a signal stopped never said the image was missing.
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(-9, "", "", "exited", 1.0))
        message = str(caught.exception)
        self.assertIn("killed", message)
        self.assertNotIn("not present locally", message)

    def test_an_image_the_runtime_reports_missing_is_still_missing(self):
        # A child that answered and reported the failure keeps its answer.
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(1, "", "no such image", "exited", 1.0))
        self.assertIn("not present locally", str(caught.exception))


class CheckCliProbeTests(unittest.TestCase):
    def setUp(self):
        self.module = load_check_cli()

    def test_the_probe_uses_the_shared_bound(self):
        seen = []

        def fake_run(command, **kwargs):
            seen.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(command, 0, "", "")

        original = self.module.subprocess.run
        self.module.subprocess.run = fake_run
        self.addCleanup(setattr, self.module.subprocess, "run", original)
        result, startup, bound = self.module.run_probe(["node", "launcher.mjs"])
        # The measurement carries the measuring ceiling and the probe the
        # bound that measurement requires, the same shape the doctor uses.
        self.assertEqual(seen[0], self.module.ADAPTER_STARTUP_MEASURE_CEILING_SECONDS)
        self.assertEqual(seen[1], bound)
        self.assertNotEqual(bound, 30)

    def test_the_contract_check_holds_the_child_to_the_host_bound(self):
        # The host's record holds a larger cost from an earlier, slower run:
        # the contract check reads the same record the doctor writes, so the
        # two surfaces hold the identical child to one bound, and the cost it
        # publishes is the one that bound was derived from.
        with tempfile.TemporaryDirectory() as td:
            config = SimpleNamespace(data=Path(td))
            doctor.record_startup(config, "pi", 20.0)
            seen = []

            def fake_run(command, **kwargs):
                seen.append(kwargs.get("timeout"))
                return subprocess.CompletedProcess(command, 0, "", "")

            original = self.module.subprocess.run
            self.module.subprocess.run = fake_run
            self.addCleanup(setattr, self.module.subprocess, "run", original)
            result, cost, bound = self.module.run_probe(["node", "launcher.mjs"],
                                                         engine="pi", config=config)
            # The host's recorded cost of 20 s requires the ceiling, and the
            # child is held to it.
            self.assertEqual(bound, 150)
            self.assertEqual(cost, 20.0)
            self.assertEqual(seen[1], 150)
            # The published pair satisfies the published rule.
            self.assertEqual(doctor.adapter_startup_bound(cost), bound)

    def test_a_killed_child_is_not_read_as_missing_exports(self):
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, -9, "", "")

        original = self.module.subprocess.run
        self.module.subprocess.run = fake_run
        self.addCleanup(setattr, self.module.subprocess, "run", original)
        record = self.module.probe_engine("pi", ["node", "launcher.mjs"], ["ModelRuntime"])
        # The child was killed before it could report: it never said the
        # export was missing.
        self.assertEqual(record["status"], "fail")
        self.assertIn("killed", record["reason"])
        self.assertNotIn("missing exports", record["reason"])

    def test_a_child_that_outlives_its_bound_is_a_bound_refusal(self):
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

        original = self.module.subprocess.run
        self.module.subprocess.run = fake_run
        self.addCleanup(setattr, self.module.subprocess, "run", original)
        with self.assertRaises(self.module.ProbeTimeout) as caught:
            self.module.run_probe(["node", "launcher.mjs"])
        self.assertIn("bound", str(caught.exception))

    def test_the_refusal_is_recorded_as_a_failure_naming_the_bound(self):
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

        original = self.module.subprocess.run
        self.module.subprocess.run = fake_run
        self.addCleanup(setattr, self.module.subprocess, "run", original)
        record = self.module.probe_engine("pi", ["node", "launcher.mjs"], ["ModelRuntime"])
        self.assertEqual(record["status"], "fail")
        self.assertIn("bound", record["reason"])
        self.assertNotIn("missing", record["reason"])


if __name__ == "__main__":
    unittest.main()
