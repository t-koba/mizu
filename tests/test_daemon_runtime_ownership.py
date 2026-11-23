"""Daemon runtime ownership: re-export seams and delegation behavior."""
import ast
import dataclasses
import time
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from support import Fixture
from mizu import daemon_runtime, runtime
from mizu.errors import Busy


class DaemonRuntimeOwnershipTests(Fixture):
    def test_reexports_same_objects(self):
        self.assertIs(runtime.POLL_LOCK_NAME, daemon_runtime.POLL_LOCK_NAME)
        self.assertIs(runtime.now_iso, daemon_runtime.now_iso)
        self.assertIs(runtime.poll_project_vcs, daemon_runtime.poll_project_vcs)
        self.assertIs(runtime.daemon, daemon_runtime.daemon)

    def test_no_runtime_import_at_module_scope(self):
        source = (Path(__file__).resolve().parents[1] / "src" / "mizu" / "daemon_runtime.py").read_text()
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").strip("."), ("runtime", "mizu.runtime"))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name, ("runtime", "mizu.runtime"))
        # The Engine seam is a local import inside daemon, not a module import.
        self.assertIn("from .runtime import Engine", source)

    def test_poll_inherits_lock_state_event(self):
        # Busy lock yields no events without touching helpers.
        with patch.object(daemon_runtime, "lock", side_effect=Busy("busy")):
            self.assertEqual(
                daemon_runtime.poll_project_vcs(self.config, self.project, {}, now=1000.0, interval=15.0), [])
        # Representative event path via the new owner.
        marker = {"event": "upstream_fetch", "ok": True}
        with (patch.object(daemon_runtime, "poll_upstream", return_value=marker),
              patch.object(daemon_runtime, "poll_ci", return_value=None),
              patch.object(daemon_runtime.ci_waits, "poll", return_value=None),
              patch.object(daemon_runtime, "poll_proposals", return_value=None)):
            events = daemon_runtime.poll_project_vcs(self.config, self.project, {}, now=1000.0, interval=15.0)
        self.assertEqual(events, [marker])

    def test_daemon_caps_sleep_by_queue_delivery_deadline(self):
        # Two loop passes exercise both stop.wait sites; a ~2s deadline is
        # shorter than both role spacing (5s) and the raised idle_seconds.
        self.config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, idle_seconds=3600))
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        deadline = time.time() + 2.0
        with (patch.object(daemon_runtime.threading, "Event", return_value=stop),
              patch.object(daemon_runtime.signal, "signal"),
              patch.object(daemon_runtime, "lock", return_value=nullcontext()),
              patch.object(daemon_runtime, "load", return_value=self.config),
              patch.object(daemon_runtime, "Project", return_value=self.project),
              patch.object(daemon_runtime, "should_run", return_value=True),
              patch.object(daemon_runtime, "vcs_poll_enabled", return_value=False),
              patch("mizu.probes.service_due", return_value=None),
              patch("mizu.probes.next_deadline", return_value=None),
              patch.object(daemon_runtime.queue_facts, "next_delivery_at",
                           return_value=deadline) as next_at,
              patch("mizu.runtime.Engine") as engine,
              patch("builtins.print")):
            engine.return_value.run.return_value = {"status": "done", "next_evaluation_at": 0.0}
            daemon_runtime.daemon(self.config, "sample", "worker")
        next_at.assert_any_call(self.project, "worker")
        delays = [call.args[0] for call in stop.wait.call_args_list]
        # Only the ~2s delivery deadline can yield a wait this short; without
        # the cap the two sites would sleep for 5s and 3600s respectively.
        self.assertTrue(any(0 < d <= 2.5 for d in delays), delays)

    def test_daemon_survives_first_pass_config_failure(self):
        # A load failure before Project() is built leaves `project` as None at
        # the outer wait; the delivery-deadline cap must not dereference it.
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with (patch.object(daemon_runtime.threading, "Event", return_value=stop),
              patch.object(daemon_runtime.signal, "signal"),
              patch.object(daemon_runtime, "lock", return_value=nullcontext()),
              patch.object(daemon_runtime, "load", side_effect=ValueError("bad config")),
              patch("mizu.probes.next_deadline", return_value=None),
              patch.object(daemon_runtime.queue_facts, "next_delivery_at") as next_at,
              patch("builtins.print")):
            daemon_runtime.daemon(self.config, "sample", "worker")
        next_at.assert_not_called()
        self.assertEqual(stop.wait.call_count, 1)

    def test_daemon_invokes_engine_via_runtime_seam(self):
        # daemon_runtime owns no module-scope Engine; daemon reads it from runtime.
        self.assertFalse(hasattr(daemon_runtime, "Engine"))
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with (patch.object(daemon_runtime.threading, "Event", return_value=stop),
              patch.object(daemon_runtime.signal, "signal"),
              patch.object(daemon_runtime, "lock", return_value=nullcontext()),
              patch.object(daemon_runtime, "load", return_value=self.config),
              patch.object(daemon_runtime, "Project", return_value=self.project),
              patch.object(daemon_runtime, "should_run", return_value=True),
              patch.object(daemon_runtime, "vcs_poll_enabled", return_value=False),
              patch("mizu.probes.service_due", return_value=None),
              patch("mizu.probes.next_deadline", return_value=None),
              patch("mizu.runtime.Engine") as engine,
              patch("builtins.print")):
            engine.return_value.run.return_value = {"status": "done", "next_evaluation_at": 0.0}
            daemon_runtime.daemon(self.config, "sample", "worker")
        self.assertEqual(engine.call_count, 1)
