"""Stage-5 example userland: observation queue declarations and policy.

The example files are declarative only: adding them changes no runtime
authority. These tests check that the declarations validate, that their schemas
enforce the stated bounds, and that a host-authenticated driver can append an
observation the way the generic kernel driver does.
"""
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

import support  # noqa: F401  (puts src on the path)
from mizu.cli import configure
from mizu.config import load
from mizu.head_state_observation import ci_head_state_facts
from mizu.errors import Denied
from mizu.kernel import QueueStore, parse_queues
from mizu.kernel.queues import allows
from mizu.runtime import Context

SOURCE_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = SOURCE_ROOT / "examples" / "development"
FRAGMENT = EXAMPLE / "observation_queues.toml"
DRIVER_FRAGMENT = EXAMPLE / "observation_drivers.toml"
DRIVER = EXAMPLE / "drivers" / "ci_costs_driver.py"
HEAD_DRIVER = EXAMPLE / "drivers" / "ci_head_state_driver.py"
HEAD_QUEUE = "ci_head_state_observations"
POLICY = EXAMPLE / "policies" / "observations.md"
README = EXAMPLE / "README.md"


def declarations():
    return tomllib.loads(FRAGMENT.read_text(encoding="utf-8"))["queues"]


def without_tables(text, prefixes):
    """Drop active TOML tables (and their bodies) under the given prefixes."""
    kept, dropping = [], False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("["):
            dropping = any(stripped.startswith("[" + prefix) for prefix in prefixes)
        if not dropping:
            kept.append(line)
    return "".join(kept)


OBSERVATION = {
    "driver": "example-driver",
    "kind": "revision",
    "summary": "a new revision was observed",
    "observed_at": "2026-01-01T00:00:00+00:00",
    "digest": "a" * 64,
    "detail": "bounded evidence detail",
}


class ObservationDeclaration(unittest.TestCase):
    def test_all_queues_validate_with_bounded_credentials(self):
        raw = declarations()
        self.assertEqual(set(raw), {"vcs_observations", "ci_observations", HEAD_QUEUE})
        queues = parse_queues(raw)
        for name in ("vcs_observations", "ci_observations", HEAD_QUEUE):
            queue = queues[name]
            # A driver appends as host, so host must hold the write grant.
            self.assertTrue(allows(queue.write, "host"), name)
            # Readers and the wake target are declared; no wildcard wake.
            self.assertIn("role:*", queue.read)
            self.assertEqual(queue.wake, ("role:worker",))
            fields = queue.fields
            self.assertTrue(fields["driver"].required)
            self.assertTrue(fields["kind"].required)
            self.assertTrue(fields["summary"].required)
            self.assertTrue(fields["observed_at"].required)
            self.assertFalse(fields["digest"].required)
            self.assertEqual(fields["driver"].max, 80)
            self.assertEqual(fields["summary"].max, 4096)
            expected_detail = 1048576 if name == HEAD_QUEUE else 16384
            self.assertEqual(fields["detail"].max, expected_detail)
            self.assertEqual(fields["digest"].type, "digest")

    def test_readme_include_loads_through_the_real_config_loader(self):
        # The README composes all three fragments; prove that include graph
        # validates with a real top config. `configure` now writes its own
        # CI observation queues and drivers, so strip those declarations
        # before including the example fragments to avoid duplicate keys.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfgdir = root / "config"
            cfgdir.mkdir()
            top = cfgdir / "config.toml"
            configure(top, None)
            text = without_tables(top.read_text(encoding="utf-8"),
                                  ("queues.", "drivers.")).replace(
                'data_dir = "~/.local/state/mizu"',
                "data_dir = " + repr(str(root / "data")))
            names = ("queues.toml", "outcomes.toml", "observation_queues.toml",
                     "observation_drivers.toml")
            # Copy the fragments beside the top config and include them by bare
            # relative name, as the README documents. A cross-tree os.path.relpath
            # breaks on platform temp symlinks (macOS /var -> /private/var), where
            # the lexically computed ".." count does not match the resolved path.
            for name in names:
                (cfgdir / name).write_bytes((EXAMPLE / name).read_bytes())
            include = "include = [" + ", ".join('"%s"' % n for n in names) + "]\n"
            top.write_text(include + text, encoding="utf-8")
            config = load(top)
            self.assertEqual(set(config.queues), {
                "proposals", "questions", "notices", "work",
                "vcs_observations", "ci_observations", HEAD_QUEUE})
            self.assertEqual(set(config.drivers), {"ci_costs", "ci_head_state"})
            driver = config.drivers["ci_costs"]
            self.assertEqual(driver.command, ("python3", "examples/development/drivers/ci_costs_driver.py"))
            self.assertEqual((driver.observation_queue, driver.timeout_seconds,
                              driver.max_bytes, driver.diagnostic_bytes),
                             ("ci_observations", 20.0, 524288, 0))
            head = config.drivers["ci_head_state"]
            self.assertEqual(head.command, (
                "python3", "examples/development/drivers/ci_head_state_driver.py"))
            self.assertEqual((head.observation_queue, head.timeout_seconds,
                              head.max_bytes, head.diagnostic_bytes),
                             (HEAD_QUEUE, 20.0, 2097152, 0))
            for name in ("vcs_observations", "ci_observations", HEAD_QUEUE):
                self.assertIn("host", config.queues[name].write)

    def test_fragment_introduces_only_the_queues_root_key(self):
        # The queue fragment stays declarative: no owner or command of its own.
        data = tomllib.loads(FRAGMENT.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"queues"})
        self.assertNotIn("command", FRAGMENT.read_text(encoding="utf-8"))
        # The separate driver fragment owns only the declared driver commands.
        declared = tomllib.loads(DRIVER_FRAGMENT.read_text(encoding="utf-8"))
        self.assertEqual(set(declared), {"drivers"})
        self.assertEqual(declared["drivers"]["ci_costs"], {
            "command": ["python3", "examples/development/drivers/ci_costs_driver.py"],
            "observation_queue": "ci_observations",
            "timeout_seconds": 20,
            "max_bytes": 524288,
            "diagnostic_bytes": 0})
        self.assertEqual(declared["drivers"]["ci_head_state"], {
            "command": ["python3", "examples/development/drivers/ci_head_state_driver.py"],
            "observation_queue": HEAD_QUEUE,
            "timeout_seconds": 20,
            "max_bytes": 2097152,
            "diagnostic_bytes": 0})

    def test_schema_bounds_are_enforced_on_host_append(self):
        queues = parse_queues(declarations())
        with tempfile.TemporaryDirectory() as tmp:
            store = QueueStore(Path(tmp), queues)
            for name in ("vcs_observations", "ci_observations", HEAD_QUEUE):
                ack = store.append(name, dict(OBSERVATION), "host")
                self.assertEqual(ack["seq"], 1)
                self.assertIn("id", ack)
                with self.assertRaises(Denied):
                    store.append(name, {**OBSERVATION, "summary": "x" * 5000}, "host")
                with self.assertRaises(Denied):
                    missing = {k: v for k, v in OBSERVATION.items() if k != "summary"}
                    store.append(name, missing, "host")
                with self.assertRaises(Denied):
                    store.append(name, {**OBSERVATION, "undeclared": 1}, "host")
                # A reader role may read the observation back.
                page = store.read(name, "role:worker")
                self.assertEqual(len(page["messages"]), 1)

    def test_policy_and_readme_describe_authority_neutrally(self):
        policy = POLICY.read_text(encoding="utf-8")
        for queue in ("vcs_observations", "ci_observations", HEAD_QUEUE):
            self.assertIn(queue, policy)
        self.assertIn("external-untrusted", policy)
        self.assertIn("authoritative", policy)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("observation_queues.toml", readme)
        self.assertIn("observation_drivers.toml", readme)
        self.assertIn("policies/observations.md", readme)
        self.assertIn("changes no runtime authority", readme)


class CostObservationAppend(unittest.TestCase):
    def test_driver_report_validates_against_the_declared_queue(self):
        payload = {"page": {"items": [], "total": 0, "offset": 0, "limit": 8},
                   "statuses": {}}
        result = subprocess.run([sys.executable, str(DRIVER)],
                                input=json.dumps(payload), text=True,
                                capture_output=True, check=True)
        observation = json.loads(result.stdout)["observations"][0]
        with tempfile.TemporaryDirectory() as tmp:
            store = QueueStore(Path(tmp), parse_queues(declarations()))
            ack = store.append("ci_observations", observation, "host")
            self.assertEqual(ack["seq"], 1)
            page = store.read("ci_observations", "role:worker")
            self.assertEqual(page["messages"][0]["body"], observation)


class GeneratedDeploymentCosts(unittest.TestCase):
    """A fresh `mizu configure` deployment serves `vcs_read ci_costs`."""

    def test_generated_config_reports_through_the_real_host_seam(self):
        with tempfile.TemporaryDirectory() as tmp:
            # configure() canonicalizes the config path before baking the
            # driver command. macOS tempfile returns a symlinked
            # /var/folders/... path, so resolve the root once and derive every
            # expected path from it to match what configure bakes.
            root = Path(tmp).resolve()
            config_file = root / "config" / "config.toml"
            configure(config_file, None)
            text = config_file.read_text(encoding="utf-8").replace(
                'data_dir = "~/.local/state/mizu"',
                "data_dir = " + json.dumps(str(root / "data")))
            config_file.write_text(text, encoding="utf-8")
            config = load(config_file)

            driver = config.drivers["ci_costs"]
            durable = config_file.parent / "drivers" / "ci_costs_driver.py"
            self.assertEqual(driver.command, (sys.executable, str(durable)))
            self.assertTrue(durable.is_file())
            self.assertEqual(durable.read_bytes(), DRIVER.read_bytes())
            self.assertFalse(durable.is_relative_to(EXAMPLE))
            self.assertFalse(durable.is_relative_to(SOURCE_ROOT))
            self.assertEqual(driver.observation_queue, "ci_observations")
            self.assertTrue(all(Path(arg).is_absolute() for arg in driver.command))
            example = tomllib.loads(DRIVER_FRAGMENT.read_text(encoding="utf-8"))["drivers"]["ci_costs"]
            self.assertEqual((driver.observation_queue, driver.timeout_seconds,
                              driver.max_bytes, driver.diagnostic_bytes),
                             (example["observation_queue"], example["timeout_seconds"],
                              example["max_bytes"], example["diagnostic_bytes"]))
            head = config.drivers["ci_head_state"]
            head_durable = config_file.parent / "drivers" / "ci_head_state_driver.py"
            self.assertEqual(head.command, (sys.executable, str(head_durable)))
            self.assertTrue(head_durable.is_file())
            self.assertEqual(head_durable.read_bytes(), HEAD_DRIVER.read_bytes())
            self.assertTrue(all(Path(arg).is_absolute() for arg in head.command))
            head_example = tomllib.loads(DRIVER_FRAGMENT.read_text(encoding="utf-8"))["drivers"]["ci_head_state"]
            self.assertEqual((head.observation_queue, head.timeout_seconds,
                              head.max_bytes, head.diagnostic_bytes),
                             (head_example["observation_queue"], head_example["timeout_seconds"],
                              head_example["max_bytes"], head_example["diagnostic_bytes"]))
            self.assertEqual(config.queues[HEAD_QUEUE],
                             parse_queues(declarations())[HEAD_QUEUE])

            queue = config.queues["ci_observations"]
            self.assertEqual(queue, parse_queues(declarations())["ci_observations"])
            self.assertTrue(allows(queue.write, "host"))
            self.assertIn("role:*", queue.read)
            self.assertEqual(queue.wake, ("role:worker",))
            self.assertEqual(queue.fields["detail"].max, 16384)

            # Real config and real host seam: only the Context shell is a shim.
            project = SimpleNamespace(root=root, kernel_queues=QueueStore(root, config.queues))
            head_config = dataclasses.replace(config, vcs={**config.vcs, "ci_branch": "main"})
            head_project = SimpleNamespace(root=root, config=head_config,
                                           kernel_queues=project.kernel_queues)
            rows = ci_head_state_facts(head_project, head_config)
            self.assertEqual(rows, [{
                "subject": "main", "revision": "", "runs": [], "requests": [],
                "no_head": True, "no_evidence": False, "may_be_incomplete": False}])
            head_page = project.kernel_queues.read(HEAD_QUEUE, "role:worker")
            self.assertEqual((head_page["head"], len(head_page["messages"])), (1, 1))
            self.assertEqual(head_page["messages"][0]["author"], "host")
            head_body = head_page["messages"][0]["body"]
            self.assertEqual((head_body["driver"], head_body["kind"]),
                             ("ci_head_state", "ci_head_state"))
            self.assertEqual(json.loads(head_body["detail"])["branches"], rows)

            run_dir = root / "run"
            (run_dir / "vcs-read").mkdir(parents=True)
            context = SimpleNamespace(
                config=config, project=project, run_dir=run_dir,
                _new_invocation_evidence=lambda op: ("eid-1", "vcs-read/eid-1.json"))
            result = Context._op_vcs_read_ci_costs(context, {"op": "ci_costs"})

            self.assertEqual(result["op"], "ci_costs")
            self.assertEqual(result["trust"], "external-untrusted")
            self.assertEqual(result["days"], [])
            self.assertIsNone(result["observed_wall_seconds"])
            self.assertEqual(result["measure"], "runner_wall_seconds")
            self.assertEqual(result["coverage"], "partial")
            self.assertTrue((run_dir / "vcs-read" / "eid-1.json").is_file())
            page = project.kernel_queues.read("ci_observations", "role:worker")
            self.assertEqual((page["head"], len(page["messages"])), (1, 1))
            self.assertEqual(page["messages"][0]["author"], "host")
            body = page["messages"][0]["body"]
            self.assertEqual((body["driver"], body["kind"]), ("ci_costs", "ci_costs"))

    def test_migrated_config_reports_from_a_foreign_cwd(self):
        """An existing config with the documented durable argv needs no source tree."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            generated = root / "generated" / "config.toml"
            configure(generated, None)
            existing_dir = root / "existing"
            existing = existing_dir / "config.toml"
            durable = existing_dir / "drivers" / "ci_costs_driver.py"
            head_durable = existing_dir / "drivers" / "ci_head_state_driver.py"
            # Migration: copy the reviewed programs into the durable config dir
            # and declare their absolute paths with the operator's interpreter.
            (durable.parent).mkdir(parents=True)
            shutil.copyfile(DRIVER, durable)
            shutil.copyfile(HEAD_DRIVER, head_durable)
            shutil.copytree(generated.parent / "policies",
                            existing_dir / "policies", dirs_exist_ok=True)
            text = generated.read_text(encoding="utf-8").replace(
                'data_dir = "~/.local/state/mizu"',
                "data_dir = " + json.dumps(str(root / "data")))
            for name, target in (("ci_costs", durable), ("ci_head_state", head_durable)):
                generated_command = "command = " + json.dumps(
                    [sys.executable, str(generated.parent / "drivers" / f"{name}_driver.py")])
                documented_command = "command = " + json.dumps([sys.executable, str(target)])
                self.assertIn(generated_command, text)
                text = text.replace(generated_command, documented_command)
            existing.write_text(text, encoding="utf-8")
            self.assertEqual(configure(existing, None)["status"], "unchanged")
            config = load(existing)
            self.assertEqual(config.drivers["ci_costs"].command,
                             (sys.executable, str(durable)))
            self.assertEqual(config.drivers["ci_head_state"].command,
                             (sys.executable, str(head_durable)))

            project = SimpleNamespace(root=root, kernel_queues=QueueStore(root, config.queues))
            run_dir = root / "run"
            (run_dir / "vcs-read").mkdir(parents=True)
            context = SimpleNamespace(
                config=config, project=project, run_dir=run_dir,
                _new_invocation_evidence=lambda op: ("eid-1", "vcs-read/eid-1.json"))
            foreign = root / "foreign-cwd"
            foreign.mkdir()
            head_config = dataclasses.replace(config, vcs={**config.vcs, "ci_branch": "main"})
            head_project = SimpleNamespace(root=root, config=head_config,
                                           kernel_queues=project.kernel_queues)
            previous = os.getcwd()
            head_rows = []
            try:
                os.chdir(foreign)
                result = Context._op_vcs_read_ci_costs(context, {"op": "ci_costs"})
                head_rows = ci_head_state_facts(head_project, head_config)
            finally:
                os.chdir(previous)
            self.assertEqual(head_rows, [{
                "subject": "main", "revision": "", "runs": [], "requests": [],
                "no_head": True, "no_evidence": False, "may_be_incomplete": False}])

            self.assertEqual(result["op"], "ci_costs")
            self.assertEqual(result["trust"], "external-untrusted")
            self.assertEqual(result["days"], [])
            self.assertIsNone(result["observed_wall_seconds"])
            self.assertEqual(result["coverage"], "partial")
            self.assertTrue((run_dir / "vcs-read" / "eid-1.json").is_file())
            page = project.kernel_queues.read("ci_observations", "role:worker")
            self.assertEqual((page["head"], len(page["messages"])), (1, 1))
            self.assertEqual(page["messages"][0]["author"], "host")
            self.assertEqual(page["messages"][0]["body"]["driver"], "ci_costs")


class HeadStateObservationAppend(unittest.TestCase):
    def test_driver_report_validates_against_the_declared_queue(self):
        payload = {"branches": [{
            "subject": "main", "head": "", "head_unreadable": False,
            "head_page_scan_stopped": False,
            "exact_page": {"pairs": [], "truncated": False, "scan_stopped": False},
            "recordings": [], "runs": [], "cache_incomplete": False,
            "branch_incomplete": False, "requests": None}]}
        result = subprocess.run([sys.executable, str(HEAD_DRIVER)],
                                input=json.dumps(payload), text=True,
                                capture_output=True, check=True)
        observation = json.loads(result.stdout)["observations"][0]
        self.assertEqual((observation["driver"], observation["kind"]),
                         ("ci_head_state", "ci_head_state"))
        self.assertEqual(json.loads(observation["detail"])["branches"][0]["no_head"], True)
        with tempfile.TemporaryDirectory() as tmp:
            store = QueueStore(Path(tmp), parse_queues(declarations()))
            ack = store.append(HEAD_QUEUE, observation, "host")
            self.assertEqual(ack["seq"], 1)
            page = store.read(HEAD_QUEUE, "role:worker")
            self.assertEqual(page["messages"][0]["body"], observation)


if __name__ == "__main__":
    unittest.main()
