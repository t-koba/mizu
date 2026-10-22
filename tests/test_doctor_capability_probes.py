"""The doctor's capability messages name the child's own outcome.

The doctor probes three capabilities: the Node module and stream features
the SDK needs, the adapter contract of each engine, and the container
runtime and its namespace helper. Every one of those probes is a child of
this process, and a child that merely outlived its bound, or was stopped by
a signal, never said what it holds. These regressions hold the rule at the
doctor's own surfaces: a child that did not answer is named as such, never
as a missing capability, so the operator is not sent to install a
dependency that is present.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from support import Fixture
from mizu import doctor
from mizu.errors import ConfigError
from mizu.process import Result


def config_for(engine="pi"):
    """A configuration with the command the probe starts and nothing else.

    The host's start-up record lives in the data directory, so the
    configuration points it at a temporary directory: a probe never
    writes host state into the product's source tree.
    """
    return SimpleNamespace(command=lambda name: [engine],
                            data=Path(tempfile.mkdtemp(prefix="mizu-startup-")))


class NodeCapabilityProbeTests(unittest.TestCase):
    """The Node capability message reads the child's own outcome."""

    def probe_with(self, result):
        original = doctor.run
        doctor.run = lambda argv, **kwargs: result
        self.addCleanup(setattr, doctor, "run", original)
        return doctor.node_capability(config_for("node"))

    def test_a_child_that_outlived_its_bound_names_the_bound(self):
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(None, "", "", "timeout", 10.0))
        message = str(caught.exception)
        self.assertIn("bound", message)
        self.assertIn("timeout", message)
        # The child never answered, so it never claimed the capability was
        # missing: the operator must not be sent to install Node.
        self.assertNotIn("capabilities missing", message)

    def test_a_child_a_signal_stopped_names_the_signal(self):
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(-9, "", "", "exited", 1.0))
        message = str(caught.exception)
        self.assertIn("killed", message)
        self.assertNotIn("capabilities missing", message)

    def test_a_child_that_answered_a_failure_is_still_a_missing_capability(self):
        # A child that completed and reported a failure keeps its answer:
        # the remedy really is to provision the dependency.
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(1, "", "no such export", "exited", 1.0))
        self.assertIn("capabilities missing", str(caught.exception))

    def test_a_child_that_answered_a_failure_names_its_outcome(self):
        # The message names the child's own outcome, so the operator sees
        # what the child said instead of an empty detail.
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(1, "", "no such export", "exited", 1.0))
        message = str(caught.exception)
        self.assertIn("exit 1", message)
        self.assertIn("no such export", message)
        self.assertFalse(message.rstrip().endswith(":"))

    def test_a_child_that_answered_reports_what_it_holds(self):
        self.assertEqual(self.probe_with(
            Result(0, "module and stream capabilities available\n", "", "exited", 1.0)),
            "module and stream capabilities available")


class CodexCapabilityProbeTests(unittest.TestCase):
    """The app-server capability message reads the child's own outcome."""

    def probe_with(self, result):
        original = doctor.run
        doctor.run = lambda argv, **kwargs: result
        self.addCleanup(setattr, doctor, "run", original)
        return doctor.engine_contract(config_for("codex"), "codex")

    def test_a_child_that_outlived_its_bound_is_not_a_missing_capability(self):
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(None, "", "", "timeout", 150.0))
        message = str(caught.exception)
        self.assertIn("bound", message)
        self.assertIn("timeout", message)
        self.assertNotIn("capability missing", message)

    def test_a_child_a_signal_stopped_names_the_signal(self):
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(-15, "", "", "exited", 1.0))
        message = str(caught.exception)
        self.assertIn("killed", message)
        self.assertNotIn("capability missing", message)

    def test_a_child_that_answered_a_failure_names_its_outcome(self):
        # A child that answered and reported a failure keeps its answer, and
        # the message names that answer rather than ending with a colon.
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(3, "", "app-server refused", "exited", 1.0))
        message = str(caught.exception)
        self.assertIn("capability missing", message)
        self.assertIn("exit 3", message)
        self.assertIn("app-server refused", message)
        self.assertFalse(message.rstrip().endswith(":"))
        # The child never declared which flags it holds, so the message
        # does not claim that any of them are missing.
        self.assertNotIn("--listen", message)

    def test_a_child_that_answered_successfully_names_a_missing_flag(self):
        # A child that answered successfully declared what it holds, so a
        # required flag it did not declare is still named as missing. The
        # failing side must not be the only side that names anything.
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(0, "app-server: no flags here", "", "exited", 1.0))
        message = str(caught.exception)
        self.assertIn("capability missing", message)
        self.assertIn("--listen", message)


class ContainerRuntimeProbeTests(Fixture):
    """The container runtime message reads the child's own outcome."""

    def setUp(self):
        super().setUp()
        original_which = doctor.shutil.which
        doctor.shutil.which = lambda name: "/usr/bin/" + name
        self.addCleanup(setattr, doctor.shutil, "which", original_which)

    def probe_with(self, result):
        original = doctor.run
        doctor.run = lambda argv, **kwargs: result
        self.addCleanup(setattr, doctor, "run", original)
        return doctor.container_runtime(self.config)

    def test_a_child_that_outlived_its_bound_names_the_bound(self):
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(None, "", "", "timeout", 20.0))
        message = str(caught.exception)
        self.assertIn("bound", message)
        self.assertIn("timeout", message)
        # The runtime never said it was missing, so the operator is not sent
        # to install it.
        self.assertNotIn("Container runtime failed", message)
        self.assertNotIn("not found", message)

    def test_a_child_a_signal_stopped_names_the_signal(self):
        with self.assertRaises(ConfigError) as caught:
            self.probe_with(Result(-9, "", "", "exited", 1.0))
        message = str(caught.exception)
        self.assertIn("killed", message)
        self.assertNotIn("Container runtime failed", message)


if __name__ == "__main__":
    unittest.main()
