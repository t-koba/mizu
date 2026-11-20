"""M4: park-and-continue keeps runnable work moving without new mechanism.

Fake-engine scenario: item A stalls on operator input while item B is
runnable. The worker parks A in state and finishes ``continue`` (daemon keeps
running per ``should_run``); only when every remaining item waits does the
worker finish ``blocked``, which preserves ``needs_operator_input`` and still
resumes on the operator's answer.
"""
from support import Fixture, ScriptDriver
from mizu.runtime import Engine, should_run


class ParkAndContinueTests(Fixture):
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
        self.assertEqual(self.project.snapshots.get()["outcome"], "blocked")


if __name__ == "__main__":
    raise SystemExit(__import__("unittest").main())
