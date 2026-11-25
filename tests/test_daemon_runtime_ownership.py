"""Daemon runtime ownership: re-export seams and delegation behavior."""
import ast
import dataclasses
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from support import Fixture
from mizu import daemon_runtime, runtime
from mizu.errors import Busy
from mizu.work_item_waits import next_deadline


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
        started = time.monotonic()
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
              patch.object(daemon_runtime.daemon_wake, "wait") as wake_wait,
              patch("mizu.runtime.Engine") as engine,
              patch("builtins.print")):
            engine.return_value.run.return_value = {"status": "done", "next_evaluation_at": 0.0}
            daemon_runtime.daemon(self.config, "sample", "worker")
        next_at.assert_any_call(self.project, "worker")
        deadlines = [call.args[3] for call in wake_wait.call_args_list]
        # Both loop passes must be capped by the ~2s delivery deadline. The
        # daemon now funnels every idle path through one wait: removing the cap
        # leaves each deadline at its ~5s role spacing bound and fails this test.
        self.assertEqual(len(deadlines), 2, deadlines)
        self.assertTrue(all(started < d <= started + 2.5 for d in deadlines), deadlines)

    def test_daemon_wake_boundary_covers_loaded_config_inputs(self):
        # A settled daemon must arm and wait on the loaded config's bounded
        # input set (root + transitive include), not on the root path alone.
        from mizu.config import load as load_config

        fragment = self.file.parent / "shared.toml"
        fragment.write_text("[limits]\nidle_seconds = 15\n")
        text = "".join(ln for ln in self.file.read_text().splitlines(keepends=True)
                       if "idle_seconds = 15" not in ln)
        self.file.write_text('include = ["shared.toml"]\n' + text)
        config = load_config(self.file)
        expected = tuple(sorted((self.file.resolve(), fragment.resolve())))
        self.assertEqual(config.input_files, expected)

        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with (patch.object(daemon_runtime.threading, "Event", return_value=stop),
              patch.object(daemon_runtime.signal, "signal"),
              patch.object(daemon_runtime, "lock", return_value=nullcontext()),
              patch.object(daemon_runtime, "load", return_value=config),
              patch.object(daemon_runtime, "Project", return_value=self.project),
              patch.object(daemon_runtime, "should_run", return_value=False),
              patch.object(daemon_runtime, "vcs_poll_enabled", return_value=False),
              patch("mizu.probes.service_due", return_value=None),
              patch("mizu.probes.next_deadline", return_value=None),
              patch.object(daemon_runtime.queue_facts, "next_delivery_at", return_value=None),
              patch.object(daemon_runtime.daemon_wake, "fingerprint",
                           return_value=("baseline",)) as fingerprint,
              patch.object(daemon_runtime.daemon_wake, "wait",
                           return_value="stop") as wake_wait,
              patch("builtins.print")):
            daemon_runtime.daemon(config, "sample", "worker")
        self.assertEqual(fingerprint.call_count, 1)
        self.assertEqual(fingerprint.call_args.args[1], config.file)
        self.assertEqual(fingerprint.call_args.kwargs, {"config_files": expected})
        self.assertEqual(wake_wait.call_count, 1)
        self.assertEqual(wake_wait.call_args.args[2], ("baseline",))
        self.assertEqual(wake_wait.call_args.kwargs,
                         {"config_file": config.file, "config_files": expected})

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
              patch.object(daemon_runtime.daemon_wake, "wait") as wake_wait,
              patch("builtins.print")):
            daemon_runtime.daemon(self.config, "sample", "worker")
        next_at.assert_not_called()
        self.assertEqual(wake_wait.call_count, 1)
        self.assertIsNone(wake_wait.call_args.args[1])

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
              patch.object(daemon_runtime.daemon_wake, "wait"),
              patch("mizu.runtime.Engine") as engine,
              patch("builtins.print")):
            engine.return_value.run.return_value = {"status": "done", "next_evaluation_at": 0.0}
            daemon_runtime.daemon(self.config, "sample", "worker")
        self.assertEqual(engine.call_count, 1)

    def test_settled_daemon_actually_sleeps(self):
        # A settled daemon has no idle deadline, so it must sleep rather than
        # re-render on every pass. Ledger journal churn must not look like a
        # wake: watching the project root here used to spin at 100% CPU.
        import json
        import tempfile
        import time as real_time
        from pathlib import Path as TPath

        from mizu.cli import configure
        from mizu.config import load as load_config
        from mizu.project import Project, initialize as initialize_project

        with tempfile.TemporaryDirectory(prefix="daemon-sleeps-") as td:
            root = TPath(td)
            cfg = root / "config/config.toml"
            configure(cfg, None)
            text = cfg.read_text()
            text = text.replace('data_dir = "~/.local/state/mizu"',
                                'data_dir = ' + json.dumps(str(root / "projects")))
            text = text.replace("free_disk_mb = 1024", "free_disk_mb = 0")
            text = text.replace("idle_seconds = 15", "idle_seconds = 1")
            cfg.write_text(text)
            config = load_config(cfg)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("x = 1\n")
            goal = root / "goal.md"
            goal.write_text("g\n")
            initialize_project(config, "p0", source, goal, ["worker"], ["python3 -m unittest"])
            project = Project(config, "p0")
            project.set_control(armed=True, paused=False, wake_generation="w")
            for _ in range(20):
                project.kernel_queues.append(
                    "questions", {"question": "s", "default": "x", "context": "c"}, "role:worker")
            project = Project(config, "p0")
            snap = project.snapshots.get()
            captured = {"files": snap["files"], "code_digest": snap["code_digest"],
                        "goal_digest": snap["goal_digest"], "goal": snap["goal"]}
            settled = project.snapshots.create(
                captured, goal=snap["goal"], state="wait", run=None, outcome="wait",
                summary="idle", wake_generation="w", parent=snap["id"])
            project.snapshots.publish(settled)

            passes = [0]
            original_load = daemon_runtime.load
            def counting_load(*args, **kwargs):
                passes[0] += 1
                return original_load(*args, **kwargs)
            stop_handlers = []
            def capture_signal(sig, handler):
                if callable(handler):
                    stop_handlers.append(handler)
                return None
            with (patch.object(daemon_runtime, "load", side_effect=counting_load),
                  
                  patch.object(daemon_runtime.signal, "signal", side_effect=capture_signal),
                  patch.object(daemon_runtime, "lock", return_value=nullcontext()),
                  patch("builtins.print")):
                stopper = threading.Timer(1.2, lambda: stop_handlers[0](None, None))
                stopper.start()
                daemon_runtime.daemon(config, "p0", "worker")
                stopper.join()
            self.assertLessEqual(passes[0], 5, passes[0])

    def test_settled_daemon_does_not_rescan_backlog_each_deadline(self):
        # With no fixed idle tick, a settled daemon has no reason to re-check
        # admission: the backlog is scanned once until a real wake arrives, so
        # idle cost stays independent of retained messages.
        import json
        import tempfile
        import time as real_time
        from pathlib import Path as TPath

        from mizu.cli import configure
        from mizu.config import load as load_config
        from mizu.project import Project, initialize as initialize_project

        with tempfile.TemporaryDirectory(prefix="daemon-memo-") as td:
            root = TPath(td)
            cfg = root / "config/config.toml"
            configure(cfg, None)
            text = cfg.read_text()
            text = text.replace('data_dir = "~/.local/state/mizu"',
                                'data_dir = ' + json.dumps(str(root / "projects")))
            text = text.replace("free_disk_mb = 1024", "free_disk_mb = 0")
            text = text.replace("idle_seconds = 15", "idle_seconds = 1")
            cfg.write_text(text)
            config = load_config(cfg)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("x = 1\n")
            goal = root / "goal.md"
            goal.write_text("g\n")
            initialize_project(config, "p0", source, goal, ["worker"], ["python3 -m unittest"])
            project = Project(config, "p0")
            project.set_control(armed=True, paused=False, wake_generation="w")
            for _ in range(200):
                project.kernel_queues.append(
                    "questions", {"question": "s", "default": "x", "context": "c"}, "role:worker")
            project = Project(config, "p0")
            snap = project.snapshots.get()
            captured = {"files": snap["files"], "code_digest": snap["code_digest"],
                        "goal_digest": snap["goal_digest"], "goal": snap["goal"]}
            settled = project.snapshots.create(
                captured, goal=snap["goal"], state="wait", run=None, outcome="wait",
                summary="idle", wake_generation="w", parent=snap["id"])
            project.snapshots.publish(settled)

            admissions = [0]
            real_admission = daemon_runtime.should_run
            counted = lambda *a, **k: (admissions.__setitem__(0, admissions[0] + 1),
                                       real_admission(*a, **k))[1]
            stop_handlers = []
            def capture_signal(sig, handler):
                if callable(handler):
                    stop_handlers.append(handler)
                return None
            with (patch.object(daemon_runtime, "should_run", side_effect=counted),
                  
                  patch.object(daemon_runtime.signal, "signal", side_effect=capture_signal),
                  patch.object(daemon_runtime, "lock", return_value=nullcontext()),
                  patch("builtins.print")):
                stopper = threading.Timer(3.2, lambda: stop_handlers[0](None, None))
                stopper.start()
                daemon_runtime.daemon(config, "p0", "worker")
                stopper.join()
            self.assertLessEqual(admissions[0], 2, admissions[0])

    def test_goal_edit_wakes_a_settled_daemon(self):
        # An out-of-band PROJECT.md edit changes admission. It must change the
        # fingerprint, clearing the memoized negative result and waking the
        # daemon even though no writer published a hint and no deadline lapsed.
        import json
        import tempfile
        import time as real_time
        from pathlib import Path as TPath

        from mizu.cli import configure
        from mizu.config import load as load_config
        from mizu.project import Project, initialize as initialize_project

        with tempfile.TemporaryDirectory(prefix="daemon-goal-") as td:
            root = TPath(td)
            cfg = root / "config/config.toml"
            configure(cfg, None)
            text = cfg.read_text()
            text = text.replace('data_dir = "~/.local/state/mizu"',
                                'data_dir = ' + json.dumps(str(root / "projects")))
            text = text.replace("free_disk_mb = 1024", "free_disk_mb = 0")
            text = text.replace("idle_seconds = 15", "idle_seconds = 60")
            cfg.write_text(text)
            config = load_config(cfg)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("x = 1\n")
            goal = root / "goal.md"
            goal.write_text("g\n")
            initialize_project(config, "p0", source, goal, ["worker"], ["python3 -m unittest"])
            project = Project(config, "p0")
            project.set_control(armed=True, paused=False, wake_generation="w")
            project = Project(config, "p0")
            snap = project.snapshots.get()
            captured = {"files": snap["files"], "code_digest": snap["code_digest"],
                        "goal_digest": snap["goal_digest"], "goal": snap["goal"]}
            settled = project.snapshots.create(
                captured, goal=snap["goal"], state="wait", run=None, outcome="wait",
                summary="idle", wake_generation="w", parent=snap["id"])
            project.snapshots.publish(settled)

            admissions = [0]
            real_admission = daemon_runtime.should_run
            counted = lambda *a, **k: (admissions.__setitem__(0, admissions[0] + 1),
                                       real_admission(*a, **k))[1]
            waits = [0]
            real_wait = daemon_runtime.daemon_wake.wait
            # Terminate the daemon by setting its real stop event, so the
            # native notification wait is released the same way a SIGTERM
            # would release it in production.
            stop_seen = []
            def edited_wait(stop, watched_root, baseline, deadline, poll=None,
                            config_file=None, config_files=()):
                if not stop_seen:
                    stop_seen.append(stop)
                    threading.Timer(2.2, stop.set).start()
                waits[0] += 1
                if waits[0] == 2:
                    (watched_root / "PROJECT.md").write_text("a different goal\n")
                return real_wait(stop, watched_root, baseline, deadline, poll,
                                 config_file=config_file, config_files=config_files)
            with (patch.object(daemon_runtime, "should_run", side_effect=counted),
                  patch.object(daemon_runtime.daemon_wake, "wait", side_effect=edited_wait),
                  patch.object(daemon_runtime.signal, "signal"),
                  patch.object(daemon_runtime, "lock", return_value=nullcontext()),
                  patch("builtins.print")):
                daemon_runtime.daemon(config, "p0", "worker")
            self.assertGreaterEqual(admissions[0], 3, admissions[0])

    def test_config_edit_wakes_a_settled_daemon(self):
        # The authoritative deployment config lives outside the project root.
        # An out-of-band config edit changes policy (e.g. daemon policy, limits)
        # and must wake a settled daemon with no deadline and no writer hint.
        import json
        import tempfile
        import time as real_time
        from pathlib import Path as TPath

        from mizu.cli import configure
        from mizu.config import load as load_config
        from mizu.project import Project, initialize as initialize_project

        with tempfile.TemporaryDirectory(prefix="daemon-config-edit-") as td:
            root = TPath(td)
            cfg = root / "config/config.toml"
            configure(cfg, None)
            text = cfg.read_text()
            text = text.replace('data_dir = "~/.local/state/mizu"',
                                'data_dir = ' + json.dumps(str(root / "projects")))
            text = text.replace("free_disk_mb = 1024", "free_disk_mb = 0")
            text = text.replace("idle_seconds = 15", "idle_seconds = 60")
            cfg.write_text(text)
            config = load_config(cfg)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("x = 1\n")
            goal = root / "goal.md"
            goal.write_text("g\n")
            initialize_project(config, "p0", source, goal, ["worker"], ["python3 -m unittest"])
            project = Project(config, "p0")
            project.set_control(armed=True, paused=False, wake_generation="w")
            snap = project.snapshots.get()
            captured = {"files": snap["files"], "code_digest": snap["code_digest"],
                        "goal_digest": snap["goal_digest"], "goal": snap["goal"]}
            settled = project.snapshots.create(
                captured, goal=snap["goal"], state="wait", run=None, outcome="wait",
                summary="idle", wake_generation="w", parent=snap["id"])
            project.snapshots.publish(settled)

            admissions = [0]
            real_admission = daemon_runtime.should_run
            counted = lambda *a, **k: (admissions.__setitem__(0, admissions[0] + 1),
                                       real_admission(*a, **k))[1]
            waits = [0]
            real_wait = daemon_runtime.daemon_wake.wait
            # Terminate the daemon by setting its real stop event, so the
            # native notification wait is released the same way a SIGTERM
            # would release it in production.
            stop_seen = []
            def edited_wait(stop, watched_root, baseline, deadline, poll=None,
                            config_file=None, config_files=()):
                if not stop_seen:
                    stop_seen.append(stop)
                    threading.Timer(2.2, stop.set).start()
                waits[0] += 1
                if waits[0] == 2:
                    cfg.write_text(cfg.read_text().replace("idle_seconds = 60",
                                                           "idle_seconds = 6100"))
                return real_wait(stop, watched_root, baseline, deadline, poll,
                                 config_file=config_file, config_files=config_files)
            with (patch.object(daemon_runtime, "should_run", side_effect=counted),
                  patch.object(daemon_runtime.daemon_wake, "wait", side_effect=edited_wait),
                  patch.object(daemon_runtime.signal, "signal"),
                  patch.object(daemon_runtime, "lock", return_value=nullcontext()),
                  patch("builtins.print")):
                daemon_runtime.daemon(config, "p0", "worker")
            self.assertGreaterEqual(admissions[0], 3, admissions[0])

    def test_expired_or_unactionable_wait_contributes_no_deadline(self):
        # A persisted wake_at is an edge to observe once, not permission for a
        # fresh 0.1s timer on every pass. An elapsed bound is admitted at the
        # top of the loop; an unactionable one blocks on a real input change.
        mono, wall = 1000.0, 2000.0

        class Snaps:
            # The real shape: the outcome is in `outcome`; `state` is the role's free-text note.
            state = {"outcome": "wait", "state": "Now: waiting for CI.", "wake_at": wall - 5.0}

            @classmethod
            def get(cls):
                return cls.state

        class Stub:
            root = None
            snapshots = Snaps()
            control_value = {"armed": True, "paused": False}

            @classmethod
            def control(cls):
                return cls.control_value

        with (patch.object(daemon_runtime.time, "monotonic", return_value=mono),
              patch.object(daemon_runtime.time, "time", return_value=wall),
              patch("mizu.probes.next_deadline", return_value=None),
              patch.object(daemon_runtime.check_jobs, "deferral", return_value=None),
              patch.object(daemon_runtime.queue_facts, "next_delivery_at", return_value=None)):
            project = Stub()
            # Elapsed wait: no deadline, admission observes it at pass start.
            self.assertIsNone(daemon_runtime._wake_deadline(
                self.config, project, "sample", "worker", 0.0))
            # Future wait while paused: unactionable, block on a real change.
            Snaps.state = {"outcome": "wait", "state": "Now: waiting for CI.", "wake_at": wall + 30.0}
            Stub.control_value = {"armed": True, "paused": True}
            self.assertIsNone(daemon_runtime._wake_deadline(
                self.config, project, "sample", "worker", 0.0))
            # Future wait while disarmed: same.
            Stub.control_value = {"armed": False, "paused": False}
            self.assertIsNone(daemon_runtime._wake_deadline(
                self.config, project, "sample", "worker", 0.0))
            # Actionable future wait keeps the exact deadline.
            Stub.control_value = {"armed": True, "paused": False}
            self.assertEqual(daemon_runtime._wake_deadline(
                self.config, project, "sample", "worker", 0.0), 1030.0)
            # Only a wait outcome sets that deadline, whatever the free-text state says.
            Snaps.state = {"outcome": "continue", "state": "wait", "wake_at": wall + 30.0}
            self.assertIsNone(daemon_runtime._wake_deadline(
                self.config, project, "sample", "worker", 0.0))

    def test_next_vcs_poll_is_a_wake_deadline(self):
        import json
        import tempfile
        from mizu.ci_observation import POLL_STATE_FILE
        mono, wall = 1000.0, 2000.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class Snaps:
                @classmethod
                def get(cls):
                    return {"outcome": "continue", "state": "Now: waiting for CI."}

            class Stub:
                snapshots = Snaps()
                control_value = {"armed": True, "paused": False}

                @classmethod
                def control(cls):
                    return cls.control_value

            Stub.root = root
            with (patch.object(daemon_runtime.time, "monotonic", return_value=mono),
                  patch.object(daemon_runtime.time, "time", return_value=wall),
                  patch("mizu.probes.next_deadline", return_value=None),
                  patch.object(daemon_runtime.check_jobs, "deferral", return_value=None),
                  patch.object(daemon_runtime.queue_facts, "next_delivery_at", return_value=None),
                  patch.object(daemon_runtime, "vcs_poll_enabled", return_value=True),
                  patch.object(daemon_runtime, "vcs_poll_interval", return_value=300.0)):
                # Never polled: due now (one-second floor).
                self.assertEqual(daemon_runtime._wake_deadline(
                    self.config, Stub(), "sample", "worker", 0.0), 1001.0)
                # Polled 100 s ago with a 300 s interval: wake in 200 s.
                (root / POLL_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
                (root / POLL_STATE_FILE).write_text(json.dumps(
                    {"last_fetch": wall - 100, "last_poll": wall - 100,
                     "last_ci_wait_poll": wall - 100, "last_proposal_poll": None}))
                self.assertEqual(daemon_runtime._wake_deadline(
                    self.config, Stub(), "sample", "worker", 0.0), 1200.0)
                # A stale proposal stamp is only a deadline while that stream is
                # enabled: a disabled proposal poll never advances it.
                (root / POLL_STATE_FILE).write_text(json.dumps(
                    {"last_fetch": wall - 100, "last_poll": wall - 100,
                     "last_ci_wait_poll": wall - 100, "last_proposal_poll": wall - 1000}))
                with patch.object(daemon_runtime, "vcs_poll_proposals_enabled",
                                  return_value=False):
                    self.assertEqual(daemon_runtime._wake_deadline(
                        self.config, Stub(), "sample", "worker", 0.0), 1200.0)
                with patch.object(daemon_runtime, "vcs_poll_proposals_enabled",
                                  return_value=True):
                    self.assertEqual(daemon_runtime._wake_deadline(
                        self.config, Stub(), "sample", "worker", 0.0), 1001.0)
                # Paused: no poll runs, so no poll deadline.
                Stub.control_value = {"armed": True, "paused": True}
                self.assertIsNone(daemon_runtime._wake_deadline(
                    self.config, Stub(), "sample", "worker", 0.0))

    def test_blocked_deadline_item_is_a_wake_deadline(self):
        mono, wall = 1000.0, 1795600000.0

        class Snaps:
            @classmethod
            def get(cls):
                return {"outcome": "continue", "state": "Now."}

        class Items:
            rows = []

            def current_items(self, *, statuses=None):
                assert statuses == ("blocked",)
                return self.rows

        class Stub:
            root = None
            snapshots = Snaps()
            work_items = Items()

            @staticmethod
            def control():
                return {"armed": True, "paused": False}

        def at(seconds):
            from datetime import datetime, timezone
            return datetime.fromtimestamp(wall + seconds, timezone.utc).isoformat()

        Items.rows = [
            {"status": "blocked", "holder": "worker",
             "wait": {"kind": "deadline", "at": at(600)}},
            {"status": "blocked", "holder": "worker",
             "wait": {"kind": "deadline", "at": at(-60)}},  # already due
            {"status": "blocked", "holder": "auditor",
             "wait": {"kind": "deadline", "at": at(60)}},  # another role's
            {"status": "blocked", "holder": "worker",
             "wait": {"kind": "code_change", "digest": "x"}},
        ]
        expected = next_deadline(Items(), "worker", now=wall)
        self.assertIsNotNone(expected)
        with (patch.object(daemon_runtime.time, "monotonic", return_value=mono),
              patch.object(daemon_runtime.time, "time", return_value=wall),
              patch("mizu.probes.next_deadline", return_value=None),
              patch.object(daemon_runtime.check_jobs, "deferral", return_value=None),
              patch.object(daemon_runtime.queue_facts, "next_delivery_at", return_value=None),
              patch.object(daemon_runtime, "vcs_poll_enabled", return_value=False)):
            self.assertEqual(daemon_runtime._wake_deadline(
                self.config, Stub(), "sample", "worker", 0.0),
                mono + (expected - wall))

    def test_work_item_deadline_helper_owns_kind_time_and_visibility(self):
        wall = 1795600000.0

        def at(seconds):
            from datetime import datetime, timezone
            return datetime.fromtimestamp(wall + seconds, timezone.utc).isoformat()

        class Store:
            def __init__(self, rows):
                self.rows = rows

            def current_items(self, *, statuses=None):
                self.statuses = statuses
                return self.rows

        rows = [
            {"status": "blocked", "holder": None,
             "wait": {"kind": "deadline", "at": at(90)}},
            {"status": "blocked", "holder": "worker",
             "wait": {"kind": "deadline", "at": at(30)}},
            {"status": "blocked", "holder": "auditor",
             "wait": {"kind": "deadline", "at": at(1)}},
            {"status": "blocked", "holder": "worker",
             "wait": {"kind": "code_change", "digest": "x"}},
            {"status": "blocked", "holder": "worker",
             "wait": {"kind": "deadline", "at": at(-5)}},
        ]
        store = Store(rows)
        self.assertEqual(next_deadline(store, "worker", now=wall), wall + 30)
        self.assertEqual(store.statuses, ("blocked",))
        self.assertEqual(next_deadline(store, "auditor", now=wall), wall + 1)
        self.assertEqual(next_deadline(store, "observer", now=wall), wall + 90)
        self.assertEqual(next_deadline(store, None, now=wall), wall + 1)

    def test_paused_elapsed_wait_does_not_reload_in_a_busy_loop(self):
        # A paused deployment with a due wait used to reload config and re-arm a
        # 0.1s deadline ~10x/s with zero admissions. It must settle instead.
        import json
        import tempfile
        import time as real_time
        from pathlib import Path as TPath

        from mizu.cli import configure
        from mizu.config import load as load_config
        from mizu.project import Project, initialize as initialize_project

        with tempfile.TemporaryDirectory(prefix="daemon-paused-wait-") as td:
            root = TPath(td)
            cfg = root / "config/config.toml"
            configure(cfg, None)
            text = cfg.read_text()
            text = text.replace('data_dir = "~/.local/state/mizu"',
                                'data_dir = ' + json.dumps(str(root / "projects")))
            text = text.replace("free_disk_mb = 1024", "free_disk_mb = 0")
            cfg.write_text(text)
            config = load_config(cfg)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("x = 1\n")
            goal = root / "goal.md"
            goal.write_text("g\n")
            initialize_project(config, "p0", source, goal, ["worker"], ["python3 -m unittest"])
            project = Project(config, "p0")
            project.set_control(armed=True, paused=True, wake_generation="w")
            snap = project.snapshots.get()
            captured = {"files": snap["files"], "code_digest": snap["code_digest"],
                        "goal_digest": snap["goal_digest"], "goal": snap["goal"]}
            settled = project.snapshots.create(
                captured, goal=snap["goal"], state="wait", run=None, outcome="wait",
                summary="idle", wake_at=time.time() - 30.0, wake_generation="w",
                parent=snap["id"])
            project.snapshots.publish(settled)

            loads = [0]
            real_load = daemon_runtime.load
            def counted_load(*a, **k):
                loads[0] += 1
                return real_load(*a, **k)
            stop_handlers = []
            def capture_signal(sig, handler):
                if callable(handler):
                    stop_handlers.append(handler)
                return None
            with (patch.object(daemon_runtime, "load", side_effect=counted_load),
                  
                  patch.object(daemon_runtime.signal, "signal", side_effect=capture_signal),
                  patch.object(daemon_runtime, "lock", return_value=nullcontext()),
                  patch("builtins.print")):
                stopper = threading.Timer(2.2, lambda: stop_handlers[0](None, None))
                stopper.start()
                daemon_runtime.daemon(config, "p0", "worker")
                stopper.join()
            self.assertLessEqual(loads[0], 2, loads[0])

    def test_only_real_facts_bound_the_wait_without_an_idle_tick(self):
        config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, idle_seconds=3600))
        mono, wall = 1000.0, 2000.0
        with (patch.object(daemon_runtime.time, "monotonic", return_value=mono),
              patch.object(daemon_runtime.time, "time", return_value=wall),
              patch("mizu.probes.next_deadline", return_value=None),
              patch.object(daemon_runtime.check_jobs, "deferral", return_value=None),
              patch.object(self.project.snapshots, "get", return_value={"state": "run"})):
            # No fact means no deadline: a quiet daemon waits for a writer hint.
            self.assertIsNone(
                daemon_runtime._wake_deadline(config, None, "sample", "worker", 0.0))
            # Delayed delivery and cooldown are exact facts, not idle samples.
            delivery = {"baseline": (), "at": 2003.0}
            self.assertEqual(daemon_runtime._wake_deadline(
                config, None, "sample", "worker", 0.0, delivery_cache=delivery), 1003.0)
            self.assertEqual(daemon_runtime._wake_deadline(
                config, None, "sample", "worker", 1001.5, delivery_cache=delivery), 1001.5)
            # A persisted check wait survives a restart that reset cooldown.
            hold = {"id": "job", "wait_until": 2002.0, "status": "queued"}
            with patch.object(daemon_runtime.check_jobs, "deferral", return_value=hold):
                self.assertEqual(daemon_runtime._wake_deadline(
                    config, self.project, "sample", "worker", 0.0), 1002.0)
            # A malformed hold must not fall back to an idle observation.
            with patch.object(daemon_runtime.check_jobs, "deferral",
                              return_value={**hold, "wait_until": float("nan")}):
                self.assertIsNone(daemon_runtime._wake_deadline(
                    config, self.project, "sample", "worker", 0.0))

    def test_watched_dir_hint_wakes_before_the_exact_deadline(self):
        import tempfile
        import threading

        from mizu import daemon_wake, fs
        with tempfile.TemporaryDirectory(prefix="daemon-hint-") as td:
            root = Path(td)
            (root / "wake").mkdir()
            baseline = daemon_wake.fingerprint(root)
            stop = threading.Event()

            def publish_hint():
                (root / "wake" / "hint").write_text("wake\n")
                # NTFS does not reliably move a directory mtime on child
                # creation; writers publish a hint explicitly and the wait
                # path watches that hint, not raw filesystem churn.
                fs.touch_directory(root / "wake")

            hint = threading.Timer(0.05, publish_hint)
            hint.start()
            try:
                started = time.monotonic()
                reason = daemon_wake.wait(stop, root, baseline, started + 5, poll=0.01)
            finally:
                stop.set()
                hint.join(timeout=1)
            self.assertEqual(reason, "change")
            self.assertLess(time.monotonic() - started, 2.0)

    def test_deadline_wake_rechecks_admission_without_a_state_change(self):
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, True]
        with (patch.object(daemon_runtime.threading, "Event", return_value=stop),
              patch.object(daemon_runtime.signal, "signal"),
              patch.object(daemon_runtime, "lock", return_value=nullcontext()),
              patch.object(daemon_runtime, "load", return_value=self.config),
              patch.object(daemon_runtime, "Project", return_value=self.project),
              patch.object(daemon_runtime, "should_run", return_value=False) as admission,
              patch.object(daemon_runtime, "vcs_poll_enabled", return_value=False),
              patch("mizu.probes.service_due", return_value=None),
              patch("mizu.probes.next_deadline", return_value=None),
              patch.object(daemon_runtime.queue_facts, "next_delivery_at", return_value=None),
              patch.object(daemon_runtime.daemon_wake, "wait",
                           side_effect=["change", "deadline", "stop"]) as wake_wait,
              patch("builtins.print")):
            daemon_runtime.daemon(self.config, "sample", "worker")
        # Without memo invalidation on a deadline, the last pass would reuse the
        # negative admission and a fact that lapsed could never wake the role.
        self.assertEqual(admission.call_count, 3)
        self.assertEqual([call.args[3] for call in wake_wait.call_args_list], [None] * 3)

    def test_check_wait_result_reports_the_real_bound(self):
        from support import ScriptDriver
        from mizu import check_jobs
        from mizu.runtime import Engine

        roles = {name: dataclasses.replace(role, capabilities=(*role.capabilities, "check_job"))
                 for name, role in self.config.roles.items()}
        config = dataclasses.replace(
            self.config, roles=roles,
            limits=dataclasses.replace(self.config.limits, idle_seconds=3600))
        self.project.config = config

        def act(ctx, *_):
            job = ctx._op_verify({})
            ctx._op_finish({"outcome": "wait", "summary": "Frozen", "state": "Waiting",
                            "wait_for_check": job["id"], "wait_seconds": 42})

        before = time.time()
        result = Engine(config, driver=ScriptDriver(act)).run(self.project, "worker")
        bound = result["waiting_for_check"]["wait_until"]
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["next_evaluation_at"], bound)
        self.assertGreaterEqual(bound, before + 41)
        self.assertEqual(check_jobs.deferral(self.project, "worker")["wait_until"], bound)
