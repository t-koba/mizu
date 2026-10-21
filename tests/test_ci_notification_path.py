"""CI status reaches the existing wake path without inbox traffic."""
import dataclasses

from support import Fixture
from mizu.fs import write_json
from mizu.runtime import (_ci_due_for, current_ci_failures, poll_ci,
                          wake_ci_failures, write_ci_wake)
from test_vcs_ci import SHA_A, adapter_settings


class CiNotificationTests(Fixture):
    def test_matrix_rerun_and_green_use_cache_without_insight_mutations(self):
        config = dataclasses.replace(self.config, vcs={**adapter_settings(""), "ci_branch": "mizu"})
        self.project.config = config
        generation = self.project.insights.generation()
        rows = [{"check": f"core/os-{i}/python-{j}", "sha": SHA_A,
                 "state": "failure", "run": "attempt-1"}
                for i in range(3) for j in range(4)]
        rows.append({"check": "durable-contract", "sha": SHA_A,
                     "state": "failure", "run": "attempt-1"})
        def reader(settings, op, params):
            return {"checks": rows}
        def poll(at):
            state = {}
            result = poll_ci(config, self.project, state, now=at, interval=15,
                             branches=["mizu"], reader=reader)
            self.assertTrue(result["ok"])
            write_json(self.project.root / "vcs-poll.json", state)
            return result
        first = poll(1000)
        self.assertEqual(first["failures"], 13)
        failures = current_ci_failures(self.project)
        self.assertEqual(len(failures), 13)
        self.assertEqual(len(_ci_due_for(self.project, config, "worker", "snapshot", 1000)), 13)
        write_ci_wake(self.project, "worker", ("mizu",), wake_ci_failures(self.project, config),
                      "snapshot", 1000)
        poll(1010)
        self.assertEqual(_ci_due_for(self.project, config, "worker", "snapshot", 1010), [])
        rows[:] = [dict(row, run="attempt-2") for row in rows]
        poll(1020)
        self.assertEqual(len(_ci_due_for(self.project, config, "worker", "snapshot", 1020)), 13)
        rows[:] = [dict(row, state="success") for row in rows]
        poll(1030)
        self.assertEqual(current_ci_failures(self.project), [])
        self.assertEqual(_ci_due_for(self.project, config, "worker", "snapshot", 1030), [])
        self.assertEqual(self.project.insights.list(pending=False), [])
        self.assertEqual(self.project.insights.generation(), generation)
