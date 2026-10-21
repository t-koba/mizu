import re
import threading
import time
from unittest.mock import patch
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

    def test_retry_wait_outlives_provider_recovery_window(self):
        self._retune(idle_seconds=1, busy_retry_seconds=0)
        self._run_queued(stop_after_wait=False)

    def _run_queued(self, *, stop_after_wait):
        stop = threading.Event()
        outcome = []
        engine = Engine(self.config, driver=ScriptDriver(), stop=stop)
        def run():
            try:
                outcome.append(engine.run(self.project, "worker", retry_busy=True))
            except BaseException as exc:
                outcome.append(exc)
        with lock(self.project.root / "locks/workspace.lock"):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                time.sleep(0.15)
                self.assertTrue(worker.is_alive())
                self.assertEqual(outcome, [])
                if stop_after_wait:
                    stop.set()
            finally:
                if stop_after_wait:
                    worker.join(5)
        worker.join(10)
        if worker.is_alive():
            stop.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        if stop_after_wait:
            self.assertIsInstance(outcome[0], Busy)
        else:
            self.assertEqual(outcome[0]["status"], "completed")

    def test_draining_never_waits(self):
        self._retune(idle_seconds=1, busy_retry_seconds=30)
        self.project.set_control(draining=True)
        start = time.monotonic()
        with self.assertRaises(Busy):
            Engine(self.config, driver=ScriptDriver()).run(
                self.project, "worker", retry_busy=True)
        self.assertLess(time.monotonic() - start, 5)

    def test_stop_cancels_wait_even_without_recovery_window(self):
        self._retune(idle_seconds=1, busy_retry_seconds=0)
        self._run_queued(stop_after_wait=True)

    def test_cli_run_waits_even_without_recovery_window(self):
        self._retune(idle_seconds=1, busy_retry_seconds=0)
        args = parser().parse_args(["--config", str(self.file), "run", "sample", "--role", "worker"])
        held = lock(self.project.root / "locks/workspace.lock")
        held.__enter__()
        timer = threading.Timer(0.15, held.__exit__, args=(None, None, None))
        timer.start()
        try:
            started = time.monotonic()
            with patch("mizu.runtime.Engine", side_effect=lambda config, stop: Engine(config, stop=stop, driver=ScriptDriver())):
                self.assertEqual(execute(args)["status"], "completed")
            self.assertGreaterEqual(time.monotonic() - started, 0.1)
        finally:
            timer.join(5)
