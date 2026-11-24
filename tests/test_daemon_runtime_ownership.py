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
            start = [0.0]
            original_iss = daemon_runtime.threading.Event.is_set
            def timed_is_set(self):
                return real_time.monotonic() - start[0] > 1.2
            with (patch.object(daemon_runtime, "load", side_effect=counting_load),
                  patch.object(daemon_runtime.threading.Event, "is_set", timed_is_set),
                  patch.object(daemon_runtime.signal, "signal"),
                  patch.object(daemon_runtime, "lock", return_value=nullcontext()),
                  patch("builtins.print")):
                start[0] = real_time.monotonic()
                try:
                    daemon_runtime.daemon(config, "p0", "worker")
                finally:
                    daemon_runtime.threading.Event.is_set = original_iss
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
            start = [0.0]
            original_iss = daemon_runtime.threading.Event.is_set
            def timed_is_set(self):
                return real_time.monotonic() - start[0] > 3.2
            with (patch.object(daemon_runtime, "should_run", side_effect=counted),
                  patch.object(daemon_runtime.threading.Event, "is_set", timed_is_set),
                  patch.object(daemon_runtime.signal, "signal"),
                  patch.object(daemon_runtime, "lock", return_value=nullcontext()),
                  patch("builtins.print")):
                start[0] = real_time.monotonic()
                try:
                    daemon_runtime.daemon(config, "p0", "worker")
                finally:
                    daemon_runtime.threading.Event.is_set = original_iss
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
            def edited_wait(stop, watched_root, baseline, deadline, poll=None,
                            config_file=None):
                waits[0] += 1
                if waits[0] == 2:
                    (watched_root / "PROJECT.md").write_text("a different goal\n")
                return real_wait(stop, watched_root, baseline, deadline, poll,
                                 config_file=config_file)
            start = [0.0]
            original_iss = daemon_runtime.threading.Event.is_set
            def timed_is_set(self):
                return real_time.monotonic() - start[0] > 2.2
            with (patch.object(daemon_runtime, "should_run", side_effect=counted),
                  patch.object(daemon_runtime.daemon_wake, "wait", side_effect=edited_wait),
                  patch.object(daemon_runtime.threading.Event, "is_set", timed_is_set),
                  patch.object(daemon_runtime.signal, "signal"),
                  patch.object(daemon_runtime, "lock", return_value=nullcontext()),
                  patch("builtins.print")):
                start[0] = real_time.monotonic()
                try:
                    daemon_runtime.daemon(config, "p0", "worker")
                finally:
                    daemon_runtime.threading.Event.is_set = original_iss
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
            def edited_wait(stop, watched_root, baseline, deadline, poll=None,
                            config_file=None):
                waits[0] += 1
                if waits[0] == 2:
                    cfg.write_text(cfg.read_text().replace("idle_seconds = 60",
                                                           "idle_seconds = 6100"))
                return real_wait(stop, watched_root, baseline, deadline, poll,
                                 config_file=config_file)
            start = [0.0]
            original_iss = daemon_runtime.threading.Event.is_set
            def timed_is_set(self):
                return real_time.monotonic() - start[0] > 2.2
            with (patch.object(daemon_runtime, "should_run", side_effect=counted),
                  patch.object(daemon_runtime.daemon_wake, "wait", side_effect=edited_wait),
                  patch.object(daemon_runtime.threading.Event, "is_set", timed_is_set),
                  patch.object(daemon_runtime.signal, "signal"),
                  patch.object(daemon_runtime, "lock", return_value=nullcontext()),
                  patch("builtins.print")):
                start[0] = real_time.monotonic()
                try:
                    daemon_runtime.daemon(config, "p0", "worker")
                finally:
                    daemon_runtime.threading.Event.is_set = original_iss
            self.assertGreaterEqual(admissions[0], 3, admissions[0])

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
