import re
import threading
import time
from support import Fixture, ScriptDriver
from mizu.cli import parser, execute
from mizu.config import load
from mizu.errors import Busy
from mizu.fs import lock
from mizu.project import Project
from mizu.runtime import Engine


class BusyRetryTests(Fixture):
    def _retune(self, **values):
        text = self.file.read_text()
        for key, value in values.items():
            updated, count = re.subn(rf"^{key} = .*$", f"{key} = {value}", text, flags=re.M)
            if count != 1:
                updated = text.replace("[limits]", f"[limits]\n{key} = {value}", 1)
            text = updated
        self.file.write_text(text)
        self.config = load(self.file)
        self.project = Project(self.config, "sample")

    def test_busy_fails_fast_without_retry(self):
        with lock(self.project.root / "locks/workspace.lock"):
            with self.assertRaises(Busy):
                Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")

    def test_retry_waits_for_release_then_runs(self):
        self._retune(idle_seconds=1)
        held = lock(self.project.root / "locks/workspace.lock")
        held.__enter__()
        outcome = {}
        try:
            worker = threading.Thread(
                target=lambda: outcome.setdefault(
                    "result", Engine(self.config, driver=ScriptDriver()).run(
                        self.project, "worker", retry_busy=True)))
            worker.start()
            time.sleep(0.5)
            held.__exit__(None, None, None)
            held = None
            worker.join(60)
        finally:
            if held is not None:
                held.__exit__(None, None, None)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome["result"]["status"], "completed")

    def test_retry_window_expires_as_busy(self):
        self._retune(idle_seconds=1, busy_retry_seconds=2)
        start = time.monotonic()
        with lock(self.project.root / "locks/workspace.lock"):
            with self.assertRaises(Busy):
                Engine(self.config, driver=ScriptDriver()).run(
                    self.project, "worker", retry_busy=True)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertLess(elapsed, 30)

    def test_draining_never_waits(self):
        self._retune(idle_seconds=1, busy_retry_seconds=30)
        self.project.set_control(draining=True)
        start = time.monotonic()
        with self.assertRaises(Busy):
            Engine(self.config, driver=ScriptDriver()).run(
                self.project, "worker", retry_busy=True)
        self.assertLess(time.monotonic() - start, 5)

    def test_zero_window_disables_wait(self):
        self._retune(idle_seconds=1, busy_retry_seconds=0)
        start = time.monotonic()
        with lock(self.project.root / "locks/workspace.lock"):
            with self.assertRaises(Busy):
                Engine(self.config, driver=ScriptDriver()).run(
                    self.project, "worker", retry_busy=True)
        self.assertLess(time.monotonic() - start, 5)

    def test_cli_run_waits_within_window(self):
        # One-shot dispatch (what timers invoke) waits instead of dropping
        # the unit at once; expiry still surfaces the same Busy error.
        self._retune(idle_seconds=1, busy_retry_seconds=2)
        args = parser().parse_args(["--config", str(self.file), "run", "sample", "--role", "worker"])
        start = time.monotonic()
        with lock(self.project.root / "locks/workspace.lock"):
            with self.assertRaises(Busy):
                execute(args)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertLess(elapsed, 30)
