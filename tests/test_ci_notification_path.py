"""CI status reaches the existing wake path without inbox traffic."""
import dataclasses

from support import Fixture
from mizu.ci_wake import CI_WAKE_VERSION, _ci_due, _ci_wake_fact_digest
from mizu.fs import write_json
from mizu.runtime import (_ci_due_for, current_ci_failures, poll_ci,
                          wake_ci_failures, write_ci_wake)
from test_vcs_ci import SHA_A, adapter_settings


class CiNotificationTests(Fixture):
    def test_ci_due_after_backward_clock_jump(self):
        branches = ("main",)
        present = [{"branch": "main", "sha": "a" * 40, "check": "unit"}]
        record = {"version": CI_WAKE_VERSION, "branches": list(branches),
                  "fact_sha256": _ci_wake_fact_digest(present, branches),
                  "snapshot": "snapshot", "at": 10000}
        self.assertTrue(_ci_due(present, record, "snapshot", 6400, 1800, branches))
        self.assertFalse(_ci_due(present, record, "snapshot", 10000, 1800, branches))
        self.assertFalse(_ci_due(present, record, "snapshot", 11799, 1800, branches))
        self.assertTrue(_ci_due(present, record, "snapshot", 11800, 1800, branches))
        self.assertFalse(_ci_due([], record, "snapshot", 6400, 1800, branches))

