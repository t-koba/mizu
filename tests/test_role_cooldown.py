"""Per-role writer-daemon cooldown override: config, helper and runtime."""
import copy
import dataclasses
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, call, patch

from support import Fixture, ScriptDriver
from mizu.config import load, role_cooldown_seconds
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


if __name__ == "__main__":
    unittest.main()
