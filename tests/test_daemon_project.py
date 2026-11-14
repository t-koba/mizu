"""Daemon Project lifetime across configuration reloads; no sleep or adapters."""
import dataclasses
import json
from contextlib import nullcontext
from unittest.mock import Mock, call, patch

from support import Fixture
from mizu.project import Project
from mizu.runtime import daemon


class DaemonProjectTests(Fixture):
    def run_polls(self, configs, *, ready_side_effect=None):
        stop = Mock()
        stop.is_set.side_effect = [False] * len(configs) + [True]
        projects = []

        def construct(config, name):
            project = Project(config, name)
            project.insights.ingest_editor = Mock()
            projects.append(project)
            return project

        with (patch("mizu.runtime.threading.Event", return_value=stop),
              patch("mizu.runtime.signal.signal"),
              patch("mizu.runtime.lock", return_value=nullcontext()),
              patch("mizu.runtime.load", side_effect=configs),
              patch("mizu.runtime.Project", side_effect=construct),
              patch("mizu.runtime.should_run", side_effect=ready_side_effect,
                    return_value=False) as ready,
              patch("mizu.runtime.vcs_poll_enabled", return_value=True),
              patch("mizu.runtime.vcs_poll_interval", return_value=1),
              patch("mizu.runtime.poll_project_vcs", return_value=[]) as poll,
              patch("mizu.probes.service_due", return_value=None),
              patch("mizu.probes.next_deadline", return_value=None),
              patch("mizu.runtime.Engine") as engine,
              patch("builtins.print") as output):
            daemon(self.config, "sample", "worker")
        engine.assert_not_called()
        self.assertEqual(stop.wait.call_count, len(configs))
        return projects, ready, poll, output

    def test_equal_reload_reuses_project_and_keeps_ingestion_and_polling_fresh(self):
        configs = [dataclasses.replace(self.config) for _ in range(3)]
        self.assertIsNot(configs[0], configs[1])
        self.assertEqual(configs[0], configs[1])
        projects, ready, poll, _ = self.run_polls(configs)
        self.assertEqual(len(projects), 1)
        project = projects[0]
        self.assertEqual([args.args[0] for args in ready.call_args_list], [project] * 3)
        project.insights.ingest_editor.assert_has_calls(
            [call(keep_days=self.config.limits.retention_days)] * 3)
        self.assertEqual(project.insights.ingest_editor.call_count, 3)
        self.assertEqual(poll.call_count, 3)
        for args, config in zip(poll.call_args_list, configs):
            self.assertIs(args.args[0], config)
            self.assertIs(args.args[1], project)

    def test_equal_reload_refreshes_project_settings_without_rebuilding_stores(self):
        settings = self.project.root / "project.toml"
        original = settings.read_text(encoding="utf-8")
        updated = original.replace(
            'roles = ["worker", "searcher", "reviewer", "reporter", "consult"]',
            'roles = ["searcher"]').replace(
                'verify = ["python3 -m unittest"]',
                'verify = ["python3 -m unittest -q"]')
        self.assertNotEqual(updated, original)
        observations = []
        stores = []

        def ready(project, *_args, **_kwargs):
            observations.append((list(project.roles), project.verify))
            if not stores:
                stores.append((project.snapshots, project.work_items))
            if len(observations) == 1:
                settings.write_text(updated, encoding="utf-8")
            return False

        projects, _, _, _ = self.run_polls(
            [dataclasses.replace(self.config) for _ in range(3)], ready_side_effect=ready)
        self.assertEqual(len(projects), 1)
        self.assertEqual(observations, [
            (["worker", "searcher", "reviewer", "reporter", "consult"],
             ("python3 -m unittest",)),
            (["searcher"], ("python3 -m unittest -q",)),
            (["searcher"], ("python3 -m unittest -q",)),
        ])
        self.assertEqual(projects[0].roles, ("searcher",))
        self.assertIs(projects[0].snapshots, stores[0][0])
        self.assertIs(projects[0].work_items, stores[0][1])

    def test_invalid_or_deleted_project_settings_fail_closed_on_equal_reload(self):
        settings = self.project.root / "project.toml"
        original = settings.read_text(encoding="utf-8")
        configs = [dataclasses.replace(self.config) for _ in range(3)]
        for change in ("invalid", "deleted"):
            with self.subTest(change=change):
                settings.write_text(original, encoding="utf-8")
                observations = []

                def ready(project, *_args, **_kwargs):
                    observations.append(project)
                    if len(observations) == 1:
                        if change == "invalid":
                            settings.write_text("roles = [", encoding="utf-8")
                        else:
                            settings.unlink()
                    return False

                projects, ready_mock, _, output = self.run_polls(
                    configs, ready_side_effect=ready)
                self.assertEqual(len(projects), 1)
                self.assertEqual(ready_mock.call_count, 1)
                self.assertEqual(projects[0].roles, [])
                self.assertEqual(projects[0].verify, ())
                events = [json.loads(call.args[0]) for call in output.call_args_list]
                self.assertEqual(len(events), 2)
                self.assertTrue(all(event["event"] == "run_deferred" for event in events))

    def test_changed_config_rebuilds_project_and_validation_stores(self):
        changed_limits = dataclasses.replace(
            self.config, limits=dataclasses.replace(
                self.config.limits, retention_days=self.config.limits.retention_days + 1))
        changed_role = dataclasses.replace(
            self.config, roles={**self.config.roles, "worker": dataclasses.replace(
                self.config.roles["worker"], cooldown_seconds=60)})
        for changed in (changed_limits, changed_role):
            with self.subTest(config=changed):
                projects, ready, poll, _ = self.run_polls(
                    [self.config, changed, dataclasses.replace(changed)])
                self.assertEqual(len(projects), 2)
                first, second = projects
                self.assertIs(second.config, changed)
                self.assertIsNot(first.snapshots, second.snapshots)
                self.assertIsNot(first.work_items, second.work_items)
                self.assertEqual([args.args[0] for args in ready.call_args_list],
                                 [first, second, second])
                self.assertEqual([args.args[1] for args in poll.call_args_list],
                                 [first, second, second])
                second.insights.ingest_editor.assert_has_calls(
                    [call(keep_days=changed.limits.retention_days)] * 2)
                self.assertEqual(second.insights.ingest_editor.call_count, 2)

    def test_data_directory_change_still_requires_restart(self):
        changed = dataclasses.replace(self.config, data=self.root / "other-data")
        projects, ready, poll, output = self.run_polls([self.config, changed])
        self.assertEqual(len(projects), 1)
        self.assertEqual(ready.call_count, 1)
        self.assertEqual(poll.call_count, 1)
        event = json.loads(output.call_args.args[0])
        self.assertEqual(event["event"], "run_deferred")
        self.assertEqual(event["error"], "Changing data_dir requires a daemon restart")
