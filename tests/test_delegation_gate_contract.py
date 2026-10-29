"""Pre-integration contract tests for the reservation-held parent gate.

These exercise the policy matrix, not runtime enforcement or a child runner.
Runner integration still requires adapter/lock race tests and supported-system
quiescence smokes before the contract can be enabled.
"""
import tempfile
import tomllib
import unittest
from pathlib import Path

from mizu import delegation_jobs
from mizu.runtime import Context


MATRIX_PATH = Path(__file__).resolve().parents[1] / "docs" / "async-delegation-gates.toml"
with MATRIX_PATH.open("rb") as source:
    MATRIX = tomllib.load(source)


_CATEGORIES = (
    "parent_control_plane", "parent_snapshot_only", "parent_exact_job_wait",
    "parent_denied",
)


def _parent_allowed(operation, *, reservation_state, job_id=None,
                    source_snapshot=None, requested_job_id=None, outcome=None,
                    snapshot_id=None, explicit_pinned_input=False,
                    wait_path_skips_live_tree=False, vcs_operation=None):
    """Reference policy oracle used to test the pre-integration contract."""
    if reservation_state == "clear":
        return None  # normal runtime authorization remains responsible
    if reservation_state not in {"active", "unknown"}:
        return False
    if operation in MATRIX["parent_control_plane"]:
        return True
    if operation == "vcs_read":
        return vcs_operation in MATRIX["parameterized"]["vcs_read"]["allow"]
    if operation in MATRIX["parent_snapshot_only"]:
        return (reservation_state == "active" and explicit_pinned_input
                and isinstance(source_snapshot, str)
                and snapshot_id == source_snapshot)
    if operation in MATRIX["parent_exact_job_wait"]:
        return (reservation_state == "active" and outcome == "wait"
                and isinstance(job_id, str) and requested_job_id == job_id
                and wait_path_skips_live_tree)
    return False


class DelegationGateContractTests(unittest.TestCase):
    def test_matrix_classifies_every_public_context_operation_once(self):
        classified = [operation for category in _CATEGORIES for operation in MATRIX[category]]
        classified.append("vcs_read")  # sub-operation policy is separate above
        self.assertEqual(len(classified), len(set(classified)))
        self.assertEqual(set(classified), set(Context._DISPATCH))

    def test_parent_control_plane_remains_available_when_reservation_is_held_or_unknown(self):
        for state in ("active", "unknown"):
            for operation in MATRIX["parent_control_plane"]:
                with self.subTest(state=state, operation=operation):
                    self.assertTrue(_parent_allowed(operation, reservation_state=state))

    def test_live_tree_and_unclassified_operations_fail_closed(self):
        for state in ("active", "unknown"):
            for operation in MATRIX["parent_denied"]:
                with self.subTest(state=state, operation=operation):
                    self.assertFalse(_parent_allowed(operation, reservation_state=state))
        for operation in ("_engine_tool", "new_unclassified_operation"):
            with self.subTest(operation=operation):
                self.assertFalse(_parent_allowed(operation, reservation_state="unknown"))

    def test_snapshot_operations_need_exact_immutable_binding(self):
        for operation in MATRIX["parent_snapshot_only"]:
            with self.subTest(operation=operation):
                self.assertFalse(_parent_allowed(
                    operation, reservation_state="active", source_snapshot="a" * 64,
                    snapshot_id="a" * 64, explicit_pinned_input=False))
                self.assertFalse(_parent_allowed(
                    operation, reservation_state="active", source_snapshot="a" * 64,
                    snapshot_id="b" * 64, explicit_pinned_input=True))
                self.assertFalse(_parent_allowed(
                    operation, reservation_state="unknown", source_snapshot="a" * 64,
                    snapshot_id="a" * 64, explicit_pinned_input=True))
                self.assertTrue(_parent_allowed(
                    operation, reservation_state="active", source_snapshot="a" * 64,
                    snapshot_id="a" * 64, explicit_pinned_input=True))

    def test_finish_is_only_an_exact_wait_that_skips_tree_capture(self):
        args = {"reservation_state": "active", "job_id": "job-exact",
                "requested_job_id": "job-exact", "outcome": "wait",
                "wait_path_skips_live_tree": True}
        self.assertTrue(_parent_allowed("finish", **args))
        for change in ({"requested_job_id": "other"}, {"outcome": "done"},
                       {"wait_path_skips_live_tree": False}):
            with self.subTest(change=change):
                self.assertFalse(_parent_allowed("finish", **{**args, **change}))
        self.assertFalse(_parent_allowed(
            "finish", **{**args, "reservation_state": "unknown", "job_id": None}))

    def test_vcs_read_suboperations_are_narrow_and_unknowns_deny(self):
        policy = MATRIX["parameterized"]["vcs_read"]
        self.assertEqual(policy["default"], "deny")
        for operation in policy["allow"]:
            with self.subTest(operation=operation):
                self.assertTrue(_parent_allowed(
                    "vcs_read", reservation_state="active", vcs_operation=operation))
        for operation in ("log", "comments", "proposals", "acquire", "artifact", "unknown"):
            with self.subTest(operation=operation):
                self.assertFalse(_parent_allowed(
                    "vcs_read", reservation_state="active", vcs_operation=operation))

    def test_job_store_keeps_parent_gate_until_quiescence(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = delegation_jobs.DelegationJobs(Path(temporary))
            request = {"role": "helper", "task": "test reservation gating",
                       "profile": None, "source_snapshot": "a" * 64}
            job_id = store.start("worker", "key", request)["id"]
            reservation = store.reservation()
            token = reservation["token"]
            self.assertIn(reservation["status"], MATRIX["reserved_statuses"])
            self.assertEqual(reservation["status"], "queued")
            self.assertFalse(_parent_allowed("exec", reservation_state="active"))
            self.assertTrue(_parent_allowed("work_items_read", reservation_state="active"))

            store.runner_started(job_id, token)
            store.cancel("worker", job_id)
            store.runner_finished(job_id, token, "cancelled")
            still_reserved = store.reservation()
            self.assertIsNotNone(still_reserved)
            self.assertIn(still_reserved["status"], MATRIX["reserved_statuses"])
            self.assertEqual(still_reserved["status"], "cancel-requested")
            self.assertEqual(still_reserved["execution"], "cancelled")
            self.assertFalse(_parent_allowed("exec", reservation_state="active"))

            store.runner_quiescent(job_id, token, "Teardown proven by test fixture")
            self.assertIsNone(store.reservation())
            self.assertIsNone(_parent_allowed("exec", reservation_state="clear"))

    def test_runner_integration_stays_disabled_while_inventory_has_unknowns(self):
        self.assertEqual(MATRIX["status"], "contract-only")
        self.assertFalse(MATRIX["runner_integration_allowed"])
        self.assertTrue(MATRIX["entrypoints"]["not_yet_proven"])
        entrypoints = MATRIX["entrypoints"]
        for name in ("Context._engine_tool before native adapter execution",
                     "Engine.run continuation workspace comparison capture after admission",
                     "Engine.consult parent.workspace capture",
                     "check_jobs.available source capture",
                     "configured MCP containers mounted at context.workspace"):
            with self.subTest(entrypoint=name):
                self.assertIn(name, entrypoints["parent_live_tree_gates"])
        self.assertTrue(entrypoints["reservation_exclusion"])
        self.assertIn("storage.maintain_automatic_prune snapshot/run retention",
                      entrypoints["reservation_exclusion"])
        self.assertIn("probes.service_due role/workspace lock",
                      entrypoints["reservation_exclusion"])
        self.assertTrue(entrypoints["quiescence_required"])
        self.assertIn("Refuse every live-tree call", entrypoints["parent_live_tree_action"])
        self.assertIn("before any workspace access", entrypoints["reservation_exclusion_action"])
        self.assertIn("uncertain proof never releases it", entrypoints["quiescence_release_rule"])
        authority = MATRIX["child_authority"]
        self.assertIn("effective role grants and tool limits", authority["required_bindings"])
        self.assertTrue(any("writable container identities" in item for item in authority["required_bindings"]))
        self.assertIn("Refuse launch", authority["mismatch_action"])


if __name__ == "__main__":
    unittest.main()
