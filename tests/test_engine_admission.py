"""Focused tests of the Engine's authority, budget and lock admission."""
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import patch

from support import Fixture
from mizu.admission import role_exclusion
from mizu.budget import Budget
from mizu.errors import Busy, Denied, InfraExceeded
from mizu.fs import lock
from mizu.runtime import Engine


class EngineAdmissionTests(Fixture):
    def test_paused_project_denies_admission(self):
        self.project.set_control(paused=True)
        with self.assertRaisesRegex(Denied, 'Project is unarmed or paused'):
            Engine(self.config)._check_admission(self.project)

    def test_exhausted_daily_budget_denies_admission(self):
        config = replace(self.config, limits=replace(self.config.limits, daily_requests=1))
        budget = Budget(config.data / 'budget', 1, retention_days=0, timezone=config.timezone)
        with patch.object(Budget, '_today', return_value='2026-01-01'):
            budget.take('first-request', project=self.project.name)
            with self.assertRaisesRegex(InfraExceeded, 'daily model-request budget unavailable'):
                Engine(config)._check_admission(self.project)

    def test_admit_holds_role_exclusion_until_stack_exits(self):
        role = self.config.roles['reporter']
        exclusion = role_exclusion(self.project.root, role.name)
        with ExitStack() as stack:
            elapsed = Engine(self.config)._admit(self.project, role.name, role, stack, wait=False)
            self.assertGreaterEqual(elapsed, 0.0)
            with self.assertRaises(Busy), lock(exclusion, blocking=False):
                pass
        with lock(exclusion, blocking=False):
            pass
