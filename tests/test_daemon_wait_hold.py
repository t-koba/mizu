"""A unit's waiting bound holds dispatch only until the watched state changes."""
import dataclasses
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from support import Fixture
from mizu.runtime import daemon


class StopClock:
    """Fake clock that advances only on wait and stops after ``limit`` waits."""

    def __init__(self, limit):
        self.now = 1000.0
        self.waits = []
        self.limit = limit

    def is_set(self):
        return len(self.waits) >= self.limit

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        return self.is_set()


class WaitingHoldTests(Fixture):
    def _run(self, change_at):
        """Dispatch once (ending waiting with a far bound), then idle.

        ``change_at`` is the fake time at which the watched state changes
        (``None``: never). The waiter stand-in returns ``change`` at that time
        and otherwise sleeps to the deadline, like the real detector.
        """
        config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, cooldown_seconds=5),
            roles={**self.config.roles,
                   "worker": dataclasses.replace(self.config.roles["worker"], cooldown_seconds=60)})
        clock = StopClock(limit=3)
        project = Mock()
        project.control.return_value = {"armed": True}
        dispatched_at = []
        state = {"generation": 0}

        def run(*args, **kwargs):
            dispatched_at.append(clock.now)
            # The unit waits for something a day away.
            return {"status": "waiting", "next_evaluation_at": clock.now + 86400}

        def fingerprint(*args, **kwargs):
            return ("state", state["generation"])

        def wait(stop, root, baseline, deadline, poll=None, config_file=None, config_files=()):
            target = deadline if deadline is not None else clock.now + 0.1
            if change_at is not None and state["generation"] == 0 and change_at < target:
                stop.wait(max(0.0, change_at - clock.now))
                state["generation"] += 1
                return "stop" if stop.is_set() else "change"
            stop.wait(max(0.0, target - clock.now))
            return "stop" if stop.is_set() else "deadline"

        with (patch("mizu.runtime.threading.Event", return_value=clock),
              patch("mizu.runtime.signal.signal"),
              patch("mizu.daemon_runtime.lock", return_value=nullcontext()),
              patch("mizu.daemon_runtime.load", return_value=config),
              patch("mizu.daemon_runtime.Project", return_value=project),
              patch("mizu.runtime.editor.ingest"),
              patch("mizu.daemon_runtime.should_run", return_value=True),
              patch("mizu.daemon_runtime.vcs_poll_enabled", return_value=False),
              patch("mizu.probes.service_due", return_value=None),
              patch("mizu.probes.next_deadline", return_value=None),
              patch("mizu.daemon_runtime.queue_facts.next_delivery_at", return_value=None),
              patch("mizu.daemon_runtime.daemon_wake.fingerprint", side_effect=fingerprint),
              patch("mizu.daemon_runtime.daemon_wake.wait", side_effect=wait),
              patch("mizu.runtime.time.monotonic", side_effect=lambda: clock.now),
              patch("mizu.runtime.time.time", side_effect=lambda: clock.now),
              patch("mizu.runtime.Engine") as engine,
              patch("builtins.print")):
            engine.return_value.run.side_effect = run
            daemon(config, "sample", "worker")
        return dispatched_at

    def test_without_a_change_the_bound_holds_dispatch(self):
        dispatched_at = self._run(change_at=None)
        self.assertEqual(dispatched_at[0], 1000.0)
        self.assertTrue(all(at >= 1000.0 + 86400 for at in dispatched_at[1:]))

    def test_a_change_after_the_unit_lifts_the_bound(self):
        # A message arrives ten minutes later: the role runs then (its cooldown
        # has passed), not a day later.
        dispatched_at = self._run(change_at=1600.0)
        self.assertEqual(dispatched_at[:2], [1000.0, 1600.0])

    def test_a_change_inside_the_cooldown_waits_for_the_cooldown(self):
        dispatched_at = self._run(change_at=1010.0)
        self.assertEqual(dispatched_at[:2], [1000.0, 1060.0])


class AdmissionQueueWakeTests(Fixture):
    def test_busy_dispatch_watches_the_admission_queue_until_it_runs(self):
        from mizu.errors import Busy
        config = self.config
        clock = StopClock(limit=3)
        project = Mock()
        project.control.return_value = {"armed": True}
        outcomes = [Busy("Older execution admission is ready"), {"status": "done"}]
        watched = []

        def run(*args, **kwargs):
            outcome = outcomes.pop(0) if outcomes else {"status": "done"}
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def wait(stop, root, baseline, deadline, poll=None, config_file=None, config_files=()):
            watched.append(tuple(config_files))
            stop.wait(1.0)
            return "stop" if stop.is_set() else "change"

        with (patch("mizu.runtime.threading.Event", return_value=clock),
              patch("mizu.runtime.signal.signal"),
              patch("mizu.daemon_runtime.lock", return_value=nullcontext()),
              patch("mizu.daemon_runtime.load", return_value=config),
              patch("mizu.daemon_runtime.Project", return_value=project),
              patch("mizu.runtime.editor.ingest"),
              patch("mizu.daemon_runtime.should_run", return_value=True),
              patch("mizu.daemon_runtime.vcs_poll_enabled", return_value=False),
              patch("mizu.probes.service_due", return_value=None),
              patch("mizu.probes.next_deadline", return_value=None),
              patch("mizu.daemon_runtime.queue_facts.next_delivery_at", return_value=None),
              patch("mizu.daemon_runtime.daemon_wake.fingerprint", return_value=("state",)),
              patch("mizu.daemon_runtime.daemon_wake.wait", side_effect=wait),
              patch("mizu.runtime.time.monotonic", side_effect=lambda: clock.now),
              patch("mizu.runtime.time.time", side_effect=lambda: clock.now),
              patch("mizu.runtime.Engine") as engine,
              patch("builtins.print")):
            engine.return_value.run.side_effect = run
            daemon(config, "sample", "worker")
        queue = config.data / "slots" / "queue.json"
        # Refused as Busy: the next wait also watches the admission queue.
        self.assertIn(queue, watched[0])
        # The pass that runs still waits on the inputs it armed with; after
        # it, the queue is no longer one of the daemon's inputs.
        self.assertNotIn(queue, watched[2])


if __name__ == "__main__":
    unittest.main()
