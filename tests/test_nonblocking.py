"""M4: park-and-continue keeps runnable work moving without new mechanism.

Fake-engine scenario: item A stalls on operator input while item B is
runnable. The worker parks A in state and finishes ``continue`` (daemon keeps
running per ``should_run``); only when every remaining item waits does the
worker finish ``blocked``, which preserves ``needs_operator_input`` and still
resumes on the operator's answer.
"""
from support import Fixture, ScriptDriver
from mizu.dashboard import collect
from mizu.runtime import Engine, should_run


class ParkAndContinueTests(Fixture):
    def test_parked_item_continues_and_blocked_still_signals(self):
        # Unit 1: A stalls, B is runnable -> park A, finish continue.
        def park_and_progress(ctx, *_):
            ctx.handle("finish", {
                "outcome": "continue",
                "summary": "Parked A; progressed B.",
                "state": ("Q1 (parked): backend choice for A. Tried both adapters; "
                          "need operator pick; resume A on answer. Next: finish B."),
            })
        Engine(self.config, driver=ScriptDriver(park_and_progress)).run(self.project, "worker")
        parked = self.project.snapshots.get()
        self.assertEqual(parked["outcome"], "continue")
        self.assertIn("Q1 (parked)", parked["state"])
        # Daemon continues: runnable work is not held by the parked item.
        self.assertTrue(should_run(self.project, parked))
        core = collect(self.project)
        self.assertFalse(core["needs_operator_input"])

        # Unit 2: B done, only parked A remains -> finish blocked.
        def all_stalled(ctx, *_):
            ctx.handle("finish", {
                "outcome": "blocked",
                "summary": "B done; A still needs the backend choice.",
                "state": ("Q1: backend choice for A. Tried both adapters; "
                          "need operator pick; resume A on answer."),
            })
        Engine(self.config, driver=ScriptDriver(all_stalled)).run(self.project, "worker")
        stalled = self.project.snapshots.get()
        self.assertEqual(stalled["outcome"], "blocked")
        self.assertTrue(collect(self.project)["needs_operator_input"])
        # Blocked idles until operator input ...
        self.assertFalse(should_run(self.project, stalled))
        # ... and the operator's answer resumes it.
        self.project.insights.submit(source="operator", title="A: backend B",
                                     body="Use backend B for A.",
                                     base_snapshot=stalled["id"])
        self.assertTrue(should_run(self.project, self.project.snapshots.get()))

    def test_blocked_alone_idles_without_news(self):
        # Documents why parking matters: a lone blocked outcome does not spin.
        def stalled(ctx, *_):
            ctx.handle("finish", {
                "outcome": "blocked",
                "summary": "Need a call.",
                "state": "Q1: which backend? Tried both; need pick; resume on answer.",
            })
        Engine(self.config, driver=ScriptDriver(stalled)).run(self.project, "worker")
        snap = self.project.snapshots.get()
        self.assertFalse(should_run(self.project, snap))
        self.assertTrue(collect(self.project)["needs_operator_input"])


if __name__ == "__main__":
    raise SystemExit(__import__("unittest").main())
