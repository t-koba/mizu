"""Focused finish validation without running an engine or sealing a snapshot."""
from support import Fixture
from mizu.errors import Denied


class UnitFinishTests(Fixture):
    def assert_finish_denied(self, message, **args):
        ctx = self.context()
        with self.assertRaisesRegex(Denied, message):
            ctx._op_finish({"outcome": "wait", "summary": "Checked the inputs",
                            "state": "Inputs checked", **args})
        self.assertIsNone(ctx.finished)
        self.assertFalse((ctx.run_dir / "finish-intent.json").exists())

    def test_disallowed_outcome(self):
        self.assert_finish_denied("does not allow finish outcome", outcome="bounded")

    def test_empty_summary(self):
        self.assert_finish_denied("nonempty work summary", summary=" ")

    def test_undelivered_ack_token(self):
        self.assert_finish_denied("tokens delivered to this role", ack_events=["undelivered"])

    def test_ready_work_blocks_continue_without_handoff_progress(self):
        self.project.work_items.create(idempotency_key="ready-at-finish", title="Ready work", actor="worker")
        # The step is valid, but an unchanged workspace and no work signals
        # mean it cannot qualify as a handoff that bypasses the parking check.
        self.assert_finish_denied(r"1 ready item\(s\)", outcome="continue", next_step="Inspect the inputs")
