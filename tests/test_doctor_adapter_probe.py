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

    def test_a_measurement_that_cannot_be_taken_keeps_the_floor(self):
        seen = []
        exports = adapter_contract("pi")["exports"]

        def fake_run(argv, *, timeout, maximum, **kwargs):
            seen.append(timeout)
            if len(seen) == 1:
                # The measurement could not be taken: the child did not
                # answer inside the measuring ceiling.
                return Result(None, "", "", "timeout", 150.0)
            return Result(0, json.dumps({"exports": exports}), "", "exited", 2.0)

        original = doctor.run
        doctor.run = fake_run
        self.addCleanup(setattr, doctor, "run", original)
        details = doctor.engine_contract(config_for(), "pi")
        # The floor answers for an unmeasured cost, the same answer the
        # launcher harness gives for the identical child, and no cost is
        # published for a child that did not answer.
        self.assertEqual(seen[0], doctor.ADAPTER_STARTUP_MEASURE_CEILING_SECONDS)
        self.assertEqual(seen[1], doctor.ADAPTER_STARTUP_FLOOR_SECONDS)
        self.assertEqual(details["host_relative_bound_seconds"],
                         doctor.ADAPTER_STARTUP_FLOOR_SECONDS)
        self.assertIsNone(details["startup_seconds"])

    def test_a_passing_probe_reports_the_host_cost(self):
        exports = adapter_contract("pi")["exports"]
        report = json.dumps({"exports": exports})
        details = self.probe_with(Result(0, report, "", "exited", 3.75))
        self.assertEqual(details["startup_seconds"], 3.75)
        # The published bound is the bound the child was held to.
        self.assertEqual(details["host_relative_bound_seconds"],
                         doctor.adapter_startup_bound(3.75))
        self.assertEqual(details["host_relative_bound_seconds"], 60)
        # The endpoint names the measurement the cost came from, so the
        # operator can tell a probe's spawn-to-exit interval from a
        # managed runtime's spawn-to-first-answer interval.
        self.assertEqual(details["startup_endpoint"], "probe")

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
        self.assertEqual(details["startup_endpoint"], "probe")

    def test_an_unwritable_record_publishes_a_consistent_pair(self):
        # The host's record cannot be written or read back, so it holds no
        # measurement the surfaces can share. The published pair must still
        # satisfy the published rule: no cost, and the floor bound.
        blocker = Path(tempfile.mkdtemp(prefix="mizu-startup-record-")) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        config = SimpleNamespace(command=lambda name: ["node"], data=blocker / "data")
        exports = adapter_contract("pi")["exports"]
        report = json.dumps({"exports": exports})
        doctor.run = lambda argv, **kwargs: Result(0, report, "", "exited", 20.0)
        details = doctor.engine_contract(config, "pi")
        self.assertIsNone(details["startup_seconds"])
        self.assertTrue(details["startup_unmeasured"])
        self.assertEqual(details["host_relative_bound_seconds"],
                         doctor.ADAPTER_STARTUP_FLOOR_SECONDS)
        self.assertNotIn("startup_endpoint", details)

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

    def test_product_stop_reason_is_primary_and_signal_is_secondary(self):
        for reason, code in (("output_limit", -15), ("cancelled", -15),
                             ("input_error", 1), ("output_error", 1)):
            with self.subTest(reason=reason, code=code):
                with self.assertRaises(ConfigError) as caught:
                    doctor.probe_outcome(reason, code, "detail", "Adapter contract probe")
                message = str(caught.exception)
                self.assertIn(f"({reason})", message)
                self.assertNotIn("was killed by signal", message)
                if code < 0:
                    self.assertIn("stopped by signal 15", message)
                else:
                    self.assertNotIn("signal", message)

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
        result, startup, bound, endpoint = self.module.run_probe(["node", "launcher.mjs"])
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
            doctor.record_startup(config, "pi", 20.0, doctor.PROBE_ENDPOINT)
            seen = []

            def fake_run(command, **kwargs):
                seen.append(kwargs.get("timeout"))
                return subprocess.CompletedProcess(command, 0, "", "")

            original = self.module.subprocess.run
            self.module.subprocess.run = fake_run
            self.addCleanup(setattr, self.module.subprocess, "run", original)
            result, cost, bound, endpoint = self.module.run_probe(
                ["node", "launcher.mjs"], engine="pi", config=config)
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


class SharedProbeClassificationTests(unittest.TestCase):
    """One classification and one child name for every surface that probes it.

    The doctor and the contract check hold the identical child to the
    identical bound, so a child that did not answer reads the same way
    wherever it is probed: the refusal is produced by the one rule both
    surfaces use, and the child is named by the one rule both surfaces use.
    """

    def setUp(self):
        self.module = load_check_cli()

    def test_the_refusal_is_the_shared_one(self):
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

        original = self.module.subprocess.run
        self.module.subprocess.run = fake_run
        self.addCleanup(setattr, self.module.subprocess, "run", original)
        with self.assertRaises(self.module.ProbeTimeout) as caught:
            self.module.run_probe(["node", "launcher.mjs"], engine="pi")
        # The measurement could not be taken, so the floor answers for the
        # identical child in both surfaces, exactly as the launcher harness
        # answers for it.
        bound = self.module.ADAPTER_STARTUP_FLOOR_SECONDS
        with self.assertRaises(ConfigError) as shared:
            doctor.probe_outcome("timeout", None, "", doctor.probe_label("pi"), bound)
        # The script keeps only the exception type; the classification and
        # the child's name are the product's.
        self.assertEqual(str(caught.exception), str(shared.exception))
        self.assertIn("pi adapter contract probe", str(caught.exception))
        self.assertNotIn("missing", str(caught.exception))

    def test_the_contract_check_keeps_the_floor_when_the_measurement_cannot_be_taken(self):
        seen = []

        def fake_run(command, **kwargs):
            seen.append(kwargs.get("timeout"))
            raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

        original = self.module.subprocess.run
        self.module.subprocess.run = fake_run
        self.addCleanup(setattr, self.module.subprocess, "run", original)
        with self.assertRaises(self.module.ProbeTimeout) as caught:
            self.module.run_probe(["node", "launcher.mjs"], engine="pi")
        # The measurement could not be taken, so the floor is the bound the
        # child is held to, the same answer the harness gives.
        self.assertEqual(seen[0], self.module.ADAPTER_STARTUP_MEASURE_CEILING_SECONDS)
        self.assertEqual(seen[1], self.module.ADAPTER_STARTUP_FLOOR_SECONDS)
        self.assertIn("60 s bound", str(caught.exception))

    def test_a_killed_child_reads_the_same_way_in_both_surfaces(self):
        original = self.module.subprocess.run
        self.module.subprocess.run = lambda command, **kwargs: subprocess.CompletedProcess(command, -9, "", "")
        self.addCleanup(setattr, self.module.subprocess, "run", original)
        record = self.module.probe_engine("pi", ["node", "launcher.mjs"], ["ModelRuntime"])
        original_run = doctor.run
        self.addCleanup(setattr, doctor, "run", original_run)
        doctor.run = lambda argv, **kwargs: Result(-9, "", "", "exited", 1.0)
        with self.assertRaises(ConfigError) as shared:
            doctor.engine_contract(config_for(), "pi")
        self.assertEqual(record["reason"], str(shared.exception))
        self.assertIn("killed", record["reason"])

    def test_the_child_name_is_derived_once(self):
        # The name is derived from the engine in the product, so the two
        # surfaces cannot drift into two names for one child.
        self.assertEqual(doctor.probe_label("codex"), "Codex app-server stdio probe")
        self.assertEqual(doctor.probe_label("pi"), "pi adapter contract probe")
        self.assertEqual(doctor.probe_label("pi-durable"), "pi-durable adapter contract probe")
        self.assertEqual(doctor.probe_label(None), "Adapter contract probe")


class AnsweredChildIsAMeasurementTests(unittest.TestCase):
    """One answer for the identical child's start-up, failure or not.

    The launcher harness measures the identical child by its first answer,
    which is a refusal, so an answer is a measurement whatever it said. The
    doctor and the contract check answered a child that failed with the
    measuring ceiling instead: a second answer for one child's start-up,
    and a published pair that could contradict the published rule. Both
    surfaces now measure the answer they got and publish the bound the
    host's record derives for the identical child.
    """

    def setUp(self):
        self.original = doctor.run
        self.addCleanup(setattr, doctor, "run", self.original)
        self.module = load_check_cli()

    def failing_child(self, seen=None):
        def fake_run(argv, **kwargs):
            if seen is not None:
                seen.append(kwargs.get("timeout"))
            return Result(1, "", "boom", "exited", 0.5)
        return fake_run

    def test_the_doctor_measures_a_child_that_answered_with_a_failure(self):
        seen = []
        doctor.run = self.failing_child(seen)
        config = config_for()
        result, bound, cost, endpoint = doctor.adapter_probe(
            config, "pi", ["node", "launcher.mjs", "--check-contract"],
            maximum=8192, label="probe")
        # The measurement carried the measuring ceiling; the probe that
        # follows is held to the bound the host's record derives, never to
        # the ceiling that measured the answer.
        self.assertEqual(seen[0], doctor.ADAPTER_STARTUP_MEASURE_CEILING_SECONDS)
        self.assertEqual(seen[1], doctor.ADAPTER_STARTUP_FLOOR_SECONDS)
        self.assertEqual(bound, doctor.ADAPTER_STARTUP_FLOOR_SECONDS)
        self.assertEqual(cost, 0.5)
        # The published pair satisfies the published rule.
        self.assertEqual(doctor.adapter_startup_bound(cost), bound)
        # The runtime starts the identical child later and reads the same
        # record: one answer for one child's start-up.
        self.assertEqual(doctor.startup_bound(config, "pi"), bound)
        # The child's own answer is unchanged.
        self.assertEqual(result.exit_code, 1)

    def test_the_failing_child_keeps_its_own_answer(self):
        doctor.run = self.failing_child()
        with self.assertRaises(ConfigError) as caught:
            doctor.engine_contract(config_for(), "pi")
        # The bound does not change what the child said: a child that
        # answered and reported a failure is still the child's own answer.
        message = str(caught.exception)
        self.assertIn("SDK capabilities missing", message)
        # The message names that answer, so the operator sees the exit code
        # and the text the child produced rather than an empty detail.
        self.assertIn("exit 1", message)
        self.assertIn("boom", message)
        self.assertFalse(message.rstrip().endswith(":"))

    def test_the_contract_check_names_the_answer_of_a_failing_child(self):
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "boom")

        original = self.module.subprocess.run
        self.module.subprocess.run = fake_run
        self.addCleanup(setattr, self.module.subprocess, "run", original)
        with tempfile.TemporaryDirectory() as td:
            record = self.module.probe_engine("pi", ["node", "launcher.mjs"],
                                              ["ModelRuntime"],
                                              config=SimpleNamespace(data=Path(td)))
        # The child answered and reported a failure, so the record names
        # that answer instead of leaving the operator with an empty reason.
        self.assertEqual(record["status"], "fail")
        self.assertIn("exit 1", record["reason"])
        self.assertIn("boom", record["reason"])

    def test_the_contract_check_measures_a_child_that_answered_with_a_failure(self):
        seen = []

        def fake_run(command, **kwargs):
            seen.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(command, 1, "", "boom")

        original = self.module.subprocess.run
        self.module.subprocess.run = fake_run
        self.addCleanup(setattr, self.module.subprocess, "run", original)
        with tempfile.TemporaryDirectory() as td:
            config = SimpleNamespace(data=Path(td))
            record = self.module.probe_engine("pi", ["node", "launcher.mjs"],
                                              ["ModelRuntime"], config=config)
            self.assertEqual(record["status"], "fail")
            # The measurement carried the measuring ceiling; the probe that
            # follows is held to the bound the host's record derives.
            self.assertEqual(seen[0], self.module.ADAPTER_STARTUP_MEASURE_CEILING_SECONDS)
            self.assertEqual(seen[1], doctor.ADAPTER_STARTUP_FLOOR_SECONDS)
            contract = record["contract"]
            self.assertEqual(contract["host_relative_bound_seconds"],
                             doctor.ADAPTER_STARTUP_FLOOR_SECONDS)
            # The published cost is the one the bound was derived from, so
            # the published pair satisfies the rule the product reports.
            self.assertEqual(doctor.adapter_startup_bound(contract["startup_seconds"]),
                             contract["host_relative_bound_seconds"])
            # The runtime reads the same record for the identical child.
            self.assertEqual(doctor.startup_bound(config, "pi"),
                             contract["host_relative_bound_seconds"])

    def test_the_contract_check_publishes_the_recorded_cost(self):
        # The host's record holds a larger cost from an earlier, slower run:
        # the published cost is the one the published bound was derived
        # from, not this run's sample.
        with tempfile.TemporaryDirectory() as td:
            config = SimpleNamespace(data=Path(td))
            doctor.record_startup(config, "pi", 20.0, doctor.PROBE_ENDPOINT)

            def fake_run(command, **kwargs):
                return subprocess.CompletedProcess(command, 0,
                                                   json.dumps({"exports": ["ModelRuntime"]}), "")

            original = self.module.subprocess.run
            self.module.subprocess.run = fake_run
            self.addCleanup(setattr, self.module.subprocess, "run", original)
            record = self.module.probe_engine("pi", ["node", "launcher.mjs"],
                                              ["ModelRuntime"], config=config)
            contract = record["contract"]
            self.assertEqual(record["status"], "pass")
            self.assertEqual(contract["startup_seconds"], 20.0)
            self.assertEqual(contract["host_relative_bound_seconds"], 150)

    def test_a_child_that_did_not_answer_publishes_no_cost(self):
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

        original = self.module.subprocess.run
        self.module.subprocess.run = fake_run
        self.addCleanup(setattr, self.module.subprocess, "run", original)
        record = self.module.probe_engine("pi", ["node", "launcher.mjs"], ["ModelRuntime"])
        # The child did not answer, so no cost is published for it: the
        # refusal names the bound it was held to.
        self.assertEqual(record["status"], "fail")
        self.assertIn("bound", record["reason"])
        self.assertNotIn("contract", record)


if __name__ == "__main__":
    unittest.main()
