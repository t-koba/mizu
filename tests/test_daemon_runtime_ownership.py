"""Daemon runtime ownership: re-export seams and delegation behavior."""
import ast
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
