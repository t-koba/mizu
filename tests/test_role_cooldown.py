"""Per-role daemon policy/spacing and writer-daemon cooldown: config, helper and runtime."""
import copy
import dataclasses
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, call, patch

from support import Fixture, ScriptDriver
from mizu.config import load, role_cooldown_seconds, role_daemon_spacing_seconds
from mizu.errors import ConfigError, ModelFailure
from mizu.runtime import Engine, daemon


def _selection_spec():
    return {'retry_seconds': 30, 'rules': [{'candidates': [
        {'profile': 'primary', 'group': 'shared'}, {'profile': 'alternate', 'group': 'other'}]}],
        'on_error': [{'when': {'path': 'error.code', 'op': 'eq', 'value': 'synthetic-exhausted'},
                      'scope': 'group', 'seconds': 100}]}


class RoleCooldownConfigTests(Fixture):
    def test_defaults_and_inheritance(self):
        self.assertEqual(self.config.limits.cooldown_seconds, 5)
        role = self.config.roles["worker"]
        self.assertIsNone(role.cooldown_seconds)
        self.assertEqual(role_cooldown_seconds(self.config, role), 5)

    def test_override_applies(self):
        config = dataclasses.replace(
            self.config,
            roles={**self.config.roles,
                   "worker": dataclasses.replace(self.config.roles["worker"], cooldown_seconds=60)})
        self.assertEqual(role_cooldown_seconds(config, config.roles["worker"]), 60)
        # Other roles still inherit the project value.
        self.assertEqual(role_cooldown_seconds(config, config.roles["searcher"]), 5)

    def test_malformed_override_reads_as_project_value(self):
        role = dataclasses.replace(self.config.roles["worker"], cooldown_seconds=0)
        self.assertEqual(role_cooldown_seconds(self.config, role), 5)
        role = dataclasses.replace(self.config.roles["worker"], cooldown_seconds="60")
        self.assertEqual(role_cooldown_seconds(self.config, role), 5)

    def test_role_validation(self):
        original = self.file.read_text()
        for value, valid in (("1", True), ("5", True), ("1073741824", True),
                             ("0", False), ("1073741825", False), ("true", False),
                             ('"5"', False), ("2.5", False)):
            with self.subTest(value=value):
                self.file.write_text(
                    original.replace("[roles.worker]",
                                     f"[roles.worker]\ncooldown_seconds = {value}"))
                if valid:
                    self.assertEqual(load(self.file).roles["worker"].cooldown_seconds, int(value))
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)
        with self.subTest(unknown="unknown-key"):
            self.file.write_text(
                original.replace("[roles.worker]",
                                 "[roles.worker]\ncooldown_seconds_typo = 5"))
            with self.assertRaises(ConfigError):
                load(self.file)

    def test_example_config_documents_override(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / "config" / "config.example.toml").read_text()
        self.assertIn("cooldown_seconds", text)
        self.assertIn("daemon_policy", text)
        self.assertIn("daemon_spacing_seconds", text)


class RoleCooldownRuntimeTests(Fixture):
    def setup_selection(self, role_name="worker"):
        from mizu.selection import validate_selectors
        spec = copy.deepcopy(_selection_spec())
        validate_selectors({'dynamic': spec}, self.config.profiles, self.file.parent)
        role = dataclasses.replace(self.config.roles[role_name], profile='', selector='dynamic', on_change=False)
        self.config = dataclasses.replace(
            self.config, selectors={'dynamic': spec},
            roles={**self.config.roles, role_name: role})
        self.project.config = self.config
        return role

    def failing_result(self, role_name):
        def callback(ctx, prompt, profile):
            raise ModelFailure('custom', code='synthetic-exhausted')
        driver = ScriptDriver(callback)
        with patch('time.time', return_value=1000):
            return Engine(self.config, driver=driver).run(self.project, role_name)

    def test_writable_deferred_uses_effective_override(self):
        self.setup_selection("worker")
        self.config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, cooldown_seconds=5),
            roles={**self.config.roles,
                   "worker": dataclasses.replace(self.config.roles["worker"], cooldown_seconds=60)})
        self.project.config = self.config
        result = self.failing_result("worker")
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["next_evaluation_at"], 1060)

    def test_writable_deferred_falls_back_to_project(self):
        self.setup_selection("worker")
        self.config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, cooldown_seconds=5))
        self.project.config = self.config
        result = self.failing_result("worker")
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["next_evaluation_at"], 1005)

    def test_readonly_deferred_ignores_role_override(self):
        self.setup_selection("searcher")
        self.config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, cooldown_seconds=5),
            roles={**self.config.roles,
                   "searcher": dataclasses.replace(self.config.roles["searcher"], cooldown_seconds=60)})
        self.project.config = self.config
        result = self.failing_result("searcher")
        self.assertEqual(result["status"], "deferred")
        # Read-only deferrals stay on the project cooldown.
        self.assertEqual(result["next_evaluation_at"], 1005)

    def test_daemon_spacing_branch(self):
        class StopClock:
            """Advance time only on wait; stop after exactly two polls."""
            def __init__(self):
                self.now = 1000.0
                self.waits = []

            def is_set(self):
                return len(self.waits) >= 2

            def wait(self, seconds):
                self.waits.append(seconds)
                self.now += seconds
                return self.is_set()

        config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, cooldown_seconds=5, idle_seconds=15),
            roles={**self.config.roles,
                   "worker": dataclasses.replace(self.config.roles["worker"], cooldown_seconds=60),
                   "searcher": dataclasses.replace(self.config.roles["searcher"], cooldown_seconds=60)})
        for role_name, workspace, expected_runs in (
                ("worker", "write", [1000.0]),
                ("searcher", "read", [1000.0, 1015.0])):
            with self.subTest(role=role_name):
                self.assertEqual(config.roles[role_name].workspace, workspace)
                clock = StopClock()
                project = Mock()
                project.control.return_value = {"armed": True}
                dispatched_at = []

                def run(*args, **kwargs):
                    dispatched_at.append(clock.now)
                    return {"status": "done"}

                # Exercise the actual daemon loop, not a copy of its delay
                # conditional. Readiness stays true, so only ordinary_after
                # can suppress the writer's second dispatch at t=1015.
                with (patch("mizu.runtime.threading.Event", return_value=clock),
                      patch("mizu.runtime.signal.signal"),
                      patch("mizu.runtime.lock", return_value=nullcontext()),
                      patch("mizu.runtime.load", return_value=config),
                      patch("mizu.runtime.Project", return_value=project),
                      patch("mizu.runtime.should_run", return_value=True),
                      patch("mizu.runtime.vcs_poll_enabled", return_value=False),
                      patch("mizu.probes.service_due", return_value=None),
                      patch("mizu.probes.next_deadline", return_value=None),
                      patch("mizu.runtime.time.monotonic", side_effect=lambda: clock.now),
                      patch("mizu.runtime.time.time", side_effect=lambda: clock.now),
                      patch("mizu.runtime.Engine") as engine,
                      patch("builtins.print")):
                    engine.return_value.run.side_effect = run
                    daemon(config, "sample", role_name)

                self.assertEqual(clock.waits, [15, 15])
                self.assertEqual(clock.now, 1030.0)
                self.assertEqual(dispatched_at, expected_runs)
                self.assertEqual(engine.call_args_list,
                                 [call(config, stop=clock)] * len(expected_runs))
                self.assertEqual(engine.return_value.run.call_args_list,
                                 [call(project, role_name, probes_serviced=True,
                                       enqueue_busy=True)] * len(expected_runs))




class DaemonPolicyTests(Fixture):
    """Explicit daemon policy/spacing: config, helper and loop.

    The daemon loop consumes ``role.daemon_policy`` (``signals`` consults
    the role-aware ``should_run`` gate, ``periodic`` uses armed/paused/
    draining control only), never ``role.workspace``. Omitted policies
    resolve from the workspace with identical effective behavior.
    """

    def _append_role(self, name, workspace):
        original = self.file.read_text()
        self.file.write_text(
            original + f"""\n[roles.{name}]
profile = "alternate"
policy = ["policies/shared.md", "policies/consult.md"]
workspace = "{workspace}"
capabilities = ["files", "read", "finish"]
""")
        return load(self.file)

    def test_omitted_policy_defaults_by_workspace(self):
        self.assertEqual(self.config.roles["worker"].daemon_policy, "signals")
        self.assertEqual(self.config.roles["searcher"].daemon_policy, "periodic")
        self.assertEqual(self.config.roles["consult"].daemon_policy, "periodic")
        self.assertIsNone(self.config.roles["worker"].daemon_spacing_seconds)
        config = self._append_role("nonewatch", "none")
        self.assertEqual(config.roles["nonewatch"].daemon_policy, "periodic")
        self.assertIsNone(config.roles["nonewatch"].daemon_spacing_seconds)

    def test_policy_and_spacing_validation(self):
        original = self.file.read_text()
        for value, valid in (('"signals"', True), ('"periodic"', True),
                             ('"sometimes"', False), ('""', False), ("true", False), ("5", False)):
            with self.subTest(value=value):
                self.file.write_text(
                    original.replace("[roles.searcher]",
                                     f"[roles.searcher]\ndaemon_policy = {value}"))
                if valid:
                    self.assertEqual(load(self.file).roles["searcher"].daemon_policy,
                                     value.strip('"'))
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)
        for value, valid in (("1", True), ("7", True), ("1073741824", True),
                             ("0", False), ("1073741825", False), ("true", False),
                             ('"7"', False), ("2.5", False)):
            with self.subTest(value=value):
                self.file.write_text(
                    original.replace("[roles.searcher]",
                                     f"[roles.searcher]\ndaemon_spacing_seconds = {value}"))
                if valid:
                    self.assertEqual(load(self.file).roles["searcher"].daemon_spacing_seconds,
                                     int(value))
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)
        for unknown in ("daemon_policy_typo = \"signals\"", "daemon_spacing_seconds_typo = 5"):
            with self.subTest(unknown=unknown):
                self.file.write_text(
                    original.replace("[roles.searcher]", f"[roles.searcher]\n{unknown}"))
                with self.assertRaises(ConfigError):
                    load(self.file)

    def test_explicit_cross_workspace_values_load(self):
        original = self.file.read_text()
        text = original.replace("daemon = true",
                                "daemon = true\ndaemon_policy = \"periodic\"\ndaemon_spacing_seconds = 42", 1)
        text = text.replace('[roles.searcher]\nprofile = "alternate"',
                            '[roles.searcher]\nprofile = "alternate"\ndaemon_policy = "signals"\ndaemon_spacing_seconds = 7', 1)
        self.file.write_text(text)
        config = load(self.file)
        self.assertEqual(config.roles["worker"].daemon_policy, "periodic")
        self.assertEqual(config.roles["worker"].daemon_spacing_seconds, 42)
        self.assertEqual(config.roles["searcher"].daemon_policy, "signals")
        self.assertEqual(config.roles["searcher"].daemon_spacing_seconds, 7)

    def test_spacing_helper_effective_defaults(self):
        config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, cooldown_seconds=5, idle_seconds=15))
        worker = config.roles["worker"]
        searcher = config.roles["searcher"]
        # Omitted spacing follows the policy: signals uses the effective
        # cooldown, periodic uses idle.
        self.assertEqual(role_daemon_spacing_seconds(config, worker), 5)
        self.assertEqual(role_daemon_spacing_seconds(config, searcher), 15)
        cooled = dataclasses.replace(worker, cooldown_seconds=60)
        self.assertEqual(role_daemon_spacing_seconds(config, cooled), 60)
        # Explicit spacing wins for either policy, even cross-workspace.
        self.assertEqual(
            role_daemon_spacing_seconds(
                config, dataclasses.replace(worker, daemon_policy="periodic",
                                            daemon_spacing_seconds=42)), 42)
        self.assertEqual(
            role_daemon_spacing_seconds(
                config, dataclasses.replace(searcher, daemon_policy="signals",
                                            daemon_spacing_seconds=7)), 7)
        # Malformed overrides read as the policy default, never raise.
        self.assertEqual(
            role_daemon_spacing_seconds(
                config, dataclasses.replace(searcher, daemon_spacing_seconds=0)), 15)
        self.assertEqual(
            role_daemon_spacing_seconds(
                config, dataclasses.replace(worker, daemon_spacing_seconds="60")), 5)

    def _run_loop(self, config, role_name, should_run_value, result):
        """Drive the actual daemon loop twice on a fake clock; no model, no sleep."""
        class StopClock:
            def __init__(self):
                self.now = 1000.0
                self.waits = []

            def is_set(self):
                return len(self.waits) >= 2

            def wait(self, seconds):
                self.waits.append(seconds)
                self.now += seconds
                return self.is_set()

        clock = StopClock()
        project = Mock()
        project.control.return_value = {"armed": True}
        dispatched_at = []

        def run(*args, **kwargs):
            dispatched_at.append(clock.now)
            if callable(result):
                return result()
            return result

        with (patch("mizu.runtime.threading.Event", return_value=clock),
              patch("mizu.runtime.signal.signal"),
              patch("mizu.runtime.lock", return_value=nullcontext()),
              patch("mizu.runtime.load", return_value=config),
              patch("mizu.runtime.Project", return_value=project),
              patch("mizu.runtime.should_run", return_value=should_run_value) as should_run,
              patch("mizu.runtime.vcs_poll_enabled", return_value=False),
              patch("mizu.probes.service_due", return_value=None),
              patch("mizu.probes.next_deadline", return_value=None),
              patch("mizu.runtime.time.monotonic", side_effect=lambda: clock.now),
              patch("mizu.runtime.time.time", side_effect=lambda: clock.now),
              patch("mizu.runtime.Engine") as engine,
              patch("builtins.print")):
            engine.return_value.run.side_effect = run
            daemon(config, "sample", role_name)
        return clock, dispatched_at, should_run, engine

    def test_loop_periodic_ignores_should_run_on_write_workspace(self):
        config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, cooldown_seconds=5, idle_seconds=15),
            roles={**self.config.roles,
                   "worker": dataclasses.replace(self.config.roles["worker"],
                                                 daemon_policy="periodic",
                                                 daemon_spacing_seconds=9)})
        # should_run refuses, but the periodic policy still dispatches.
        clock, dispatched_at, should_run, engine = self._run_loop(
            config, "worker", False, {"status": "done"})
        self.assertEqual(should_run.call_count, 0)
        self.assertEqual(dispatched_at, [1000.0, 1009.0])
        self.assertEqual(clock.waits, [9, 9])

    def test_loop_signals_consults_should_run_on_read_workspace(self):
        config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, cooldown_seconds=5, idle_seconds=15),
            roles={**self.config.roles,
                   "searcher": dataclasses.replace(self.config.roles["searcher"],
                                                   daemon_policy="signals",
                                                   daemon_spacing_seconds=7)})
        # A refusing gate dispatches nothing but still polls at idle cadence.
        clock, dispatched_at, should_run, engine = self._run_loop(
            config, "searcher", False, {"status": "done"})
        self.assertGreaterEqual(should_run.call_count, 1)
        self.assertEqual(dispatched_at, [])
        # An admitting gate dispatches at the explicit spacing, not idle.
        clock, dispatched_at, should_run, engine = self._run_loop(
            config, "searcher", True, {"status": "done"})
        self.assertGreaterEqual(should_run.call_count, 1)
        self.assertEqual(dispatched_at, [1000.0, 1007.0])
        self.assertEqual(clock.waits, [7, 7])

    def test_loop_deferred_reentry_ignores_explicit_spacing(self):
        config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, cooldown_seconds=5, idle_seconds=15),
            roles={**self.config.roles,
                   "worker": dataclasses.replace(self.config.roles["worker"],
                                                 daemon_policy="signals",
                                                 daemon_spacing_seconds=60)})
        clock_holder = {}

        class StopClock:
            def __init__(self):
                self.now = 1000.0
                self.waits = []

            def is_set(self):
                return len(self.waits) >= 2

            def wait(self, seconds):
                self.waits.append(seconds)
                self.now += seconds
                return self.is_set()

        clock = StopClock()
        clock_holder["clock"] = clock
        project = Mock()
        project.control.return_value = {"armed": True}

        def waiting():
            return {"status": "waiting", "next_evaluation_at": clock.now + 5}

        with (patch("mizu.runtime.threading.Event", return_value=clock),
              patch("mizu.runtime.signal.signal"),
              patch("mizu.runtime.lock", return_value=nullcontext()),
              patch("mizu.runtime.load", return_value=config),
              patch("mizu.runtime.Project", return_value=project),
              patch("mizu.runtime.should_run", return_value=True),
              patch("mizu.runtime.vcs_poll_enabled", return_value=False),
              patch("mizu.probes.service_due", return_value=None),
              patch("mizu.probes.next_deadline", return_value=None),
              patch("mizu.runtime.time.monotonic", side_effect=lambda: clock.now),
              patch("mizu.runtime.time.time", side_effect=lambda: clock.now),
              patch("mizu.runtime.Engine") as engine,
              patch("builtins.print")):
            engine.return_value.run.side_effect = lambda *a, **k: waiting()
            daemon(config, "sample", "worker")
        # Deferred re-entry uses next_evaluation_at capped by idle_seconds,
        # not the explicit ordinary spacing of 60.
        self.assertEqual(clock.waits, [5, 5])

if __name__ == "__main__":
    unittest.main()
