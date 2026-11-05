"""Bounded synthetic diagnostics, including the real CLI success/error exporter."""
import contextlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from support import Fixture
from mizu import cli, fs, lock_diagnostics as diagnostics
from mizu.config import load
from mizu.errors import Busy, ConfigError, SnapshotConflict
from mizu.insights import Insights
from mizu.runtime import Engine
from mizu.smoke_refs import record_lock, shared_lock
from mizu.smoke_retention import retention_apply_lock


class LockDiagnosticsTests(Fixture):
    def enable(self, enabled=True):
        current = "lock_diagnostics = false" if enabled else "lock_diagnostics = true"
        text = self.file.read_text().replace(
            current, f"lock_diagnostics = {str(enabled).lower()}", 1)
        self.file.write_text(text)

    def invoke(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(cli._platform, "is_root", return_value=False):
            code = cli.main(["--config", str(self.file), "run", "sample", "--role", "worker", "--event"])
        return code, out.getvalue(), err.getvalue()

    def summary(self, text):
        line = text.splitlines()[0]
        self.assertLess(len(line.encode()), 16384)
        payload = json.loads(line)
        self.assertEqual(set(payload), {"lock_diagnostics"})
        summary = payload["lock_diagnostics"]
        self.assertEqual(set(summary), {"version", "observations", "omitted"})
        self.assertEqual(summary["version"], 1)
        self.assertLessEqual(len(summary["observations"]), diagnostics.MAX_LOCK_DIAGNOSTIC_ROWS)
        for row in summary["observations"]:
            self.assertEqual(set(row), {"lock", "phase", "operation", "stage", "outcome",
                                        "local_ms", "kernel_ms", "hold_ms"})
            self.assertIn(row["lock"], {"shared-smoke", "project-record", "retention-apply"})
            self.assertIn(row["phase"], {"pre-admission-startup", "editor-ingestion",
                                       "pre-admission-dispatch", "admission", "post-admission"})
            self.assertIn(row["operation"], {"record", "read", "list", "page", "write"})
            self.assertIn(row["stage"], {"local-acquire", "open", "kernel-acquire", "body"})
            self.assertIn(row["outcome"], {"success", "busy", "blocking-io-error", "error"})
            for field in ("local_ms", "kernel_ms", "hold_ms"):
                if row[field] is not None:
                    self.assertIs(type(row[field]), int)
                    self.assertGreaterEqual(row[field], 0)
                    self.assertLessEqual(row[field], diagnostics.MAX_LOCK_DIAGNOSTIC_VALUE)
        for private in (str(self.root), "sample", "sensitive-record", "sensitive-error", "sensitive-note"):
            self.assertNotIn(private, line)
        return summary

    def test_config_defaults_and_strict_boolean(self):
        self.assertIs(self.config.limits.lock_diagnostics, False)
        original = self.file.read_text()
        for value in ("true", "false", "0", "1", '"true"', "[]", "1.0"):
            with self.subTest(value=value):
                self.file.write_text(original.replace("lock_diagnostics = false",
                                                      f"lock_diagnostics = {value}", 1))
                if value in ("true", "false"):
                    self.assertIs(load(self.file).limits.lock_diagnostics, value == "true")
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)

    def test_holder_timings_and_disabled_no_collection(self):
        path = self.root / "sensitive-record" / "insights.lock"
        now = [0]
        local, kernel = fs._acquire_local, fs._platform.lock_fd

        def acquire_local(*args, **kwargs):
            now[0] += 7000000
            return local(*args, **kwargs)

        def acquire_kernel(*args, **kwargs):
            now[0] += 11000000
            return kernel(*args, **kwargs)

        err = io.StringIO()
        with contextlib.redirect_stderr(err), diagnostics.collect(True), \
                patch.object(diagnostics.time, "perf_counter_ns", side_effect=lambda: now[0]), \
                patch.object(fs, "_acquire_local", side_effect=acquire_local), \
                patch.object(fs._platform, "lock_fd", side_effect=acquire_kernel):
            with fs.lock(path, blocking=False):
                now[0] += 13000000
        row = self.summary(err.getvalue())["observations"][0]
        self.assertEqual((row["local_ms"], row["kernel_ms"], row["hold_ms"]), (7, 11, 13))
        self.assertEqual((row["stage"], row["outcome"]), ("body", "success"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), diagnostics.collect(False), \
                patch.object(diagnostics, "_Observation", side_effect=AssertionError("collection disabled")), \
                patch.object(diagnostics.time, "perf_counter_ns", side_effect=AssertionError("timing disabled")):
            with fs.lock(path):
                pass
        self.assertEqual(err.getvalue(), "")

    def test_failed_kernel_timing_excludes_release_cleanup(self):
        now = [0]

        def kernel(*args, **kwargs):
            now[0] += 11000000
            raise BlockingIOError("sensitive-error")

        def cleanup(*args):
            now[0] += 90000000

        err = io.StringIO()
        with contextlib.redirect_stderr(err), diagnostics.collect(True), \
                patch.object(diagnostics.time, "perf_counter_ns", side_effect=lambda: now[0]), \
                patch.object(fs._platform, "lock_fd", side_effect=kernel), \
                patch.object(fs._platform, "unlock_fd", side_effect=cleanup):
            with self.assertRaises(Busy):
                with fs.lock(self.root / "insights.lock"):
                    self.fail("body entered")
        row = self.summary(err.getvalue())["observations"][0]
        self.assertEqual((row["kernel_ms"], row["hold_ms"]), (11, None))

    def test_insight_page_and_read_operations_are_labeled(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), diagnostics.collect(True):
            self.project.insights.list_page(limit=1)
            self.project.insights.unresolved_counts([])
            self.project.insights.pending_insight_events("worker", {}, limit=1)
        rows = self.summary(err.getvalue())["observations"]
        self.assertEqual([row["operation"] for row in rows], ["page", "read", "list"])

    def test_cli_acquisition_and_body_failures_keep_code_and_do_not_replay(self):
        self.enable()
        for mode, expected_stage, expected_outcome in (
                ("shared-local", "local-acquire", "blocking-io-error"),
                ("shared-kernel", "kernel-acquire", "blocking-io-error"),
                ("local", "local-acquire", "blocking-io-error"),
                ("kernel", "kernel-acquire", "blocking-io-error"),
                ("busy", "body", "busy"),
                ("blocking", "body", "blocking-io-error")):
            with self.subTest(mode=mode):
                calls, effects = [], []
                (self.root / "effect").unlink(missing_ok=True)
                original_local, original_kernel = fs._acquire_local, fs._platform.lock_fd
                kernels = []

                def local(key, **kwargs):
                    target = "smoke-retention.lock" if mode == "shared-local" else "insights.lock"
                    if mode in ("local", "shared-local") and key.endswith(target):
                        raise BlockingIOError("sensitive-error")
                    return original_local(key, **kwargs)

                def kernel(fd, **kwargs):
                    kernels.append(fd)
                    if (mode == "kernel" and len(kernels) == 1) or (mode == "shared-kernel" and len(kernels) == 1):
                        raise BlockingIOError("sensitive-error")
                    return original_kernel(fd, **kwargs)

                def ingest(store, **kwargs):
                    calls.append(1)
                    with patch.object(fs, "_acquire_local", side_effect=local), \
                            patch.object(fs._platform, "lock_fd", side_effect=kernel):
                        boundary = (shared_lock(store.root.parent.parent)
                                    if mode.startswith("shared-") else record_lock(store.root))
                        with diagnostics.operation("write"), boundary:
                            effects.append("sensitive-record")
                            with (self.root / "effect").open("a") as stream:
                                stream.write("effect\n")
                            error = Busy("sensitive-error") if mode == "busy" else BlockingIOError("sensitive-error")
                            error.add_note("sensitive-note")
                            raise error

                before = set(self.project.root.glob("runs/*"))
                with patch.object(Insights, "ingest_editor", ingest):
                    code, out, err = self.invoke()
                self.assertEqual(code, 75)
                self.assertEqual(out, "")
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(effects), int(mode in ("busy", "blocking")))
                if effects:
                    self.assertEqual((self.root / "effect").read_text(), "effect\n")
                else:
                    self.assertFalse((self.root / "effect").exists())
                self.assertEqual(set(self.project.root.glob("runs/*")), before)
                rows = self.summary(err)["observations"]
                if mode.startswith("shared-"):
                    shared = rows[-1]
                    self.assertEqual((shared["lock"], shared["stage"], shared["outcome"]),
                                     ("shared-smoke", expected_stage, expected_outcome))
                    self.assertFalse(any(r["lock"] == "project-record" and r["phase"] == "editor-ingestion" for r in rows))
                    continue
                project = [r for r in rows if r["lock"] == "project-record" and r["phase"] == "editor-ingestion"][-1]
                self.assertEqual((project["stage"], project["outcome"]), (expected_stage, expected_outcome))
                self.assertFalse(any(r["lock"] == "shared-smoke" for r in rows))
                self.assertEqual(project["operation"], "write")
                self.assertEqual(json.loads(err.splitlines()[-1])["type"], "Busy")

    def test_retention_apply_lock_has_a_fixed_diagnostic_label(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), diagnostics.collect(True):
            with diagnostics.operation("write"), retention_apply_lock(self.config.data):
                pass
        observations = self.summary(err.getvalue())["observations"]
        self.assertIn("retention-apply", [row["lock"] for row in observations])

    def test_cli_success_dispatch_and_disabled_output_parity(self):
        def dispatch(engine, project, *args, **kwargs):
            with fs.lock(project.root / "locks" / "insights.lock"):
                pass
            return {"skip": {"skipped": "fixture"}}

        with patch.object(Engine, "_dispatch_state", dispatch):
            self.enable()
            enabled = self.invoke()
            self.enable(False)
            disabled = self.invoke()
        self.assertEqual(disabled, (0, '{\n  "skipped": "fixture"\n}\n', ""))
        self.assertEqual(enabled[:2], disabled[:2])
        rows = self.summary(enabled[2])["observations"]
        self.assertTrue(any(r["phase"] == "editor-ingestion" for r in rows))
        startup_writes = [r for r in rows if r["phase"] == "pre-admission-startup"
                          and r["operation"] == "write"]
        self.assertTrue(any(r["lock"] == "project-record" for r in startup_writes))
        self.assertTrue(any(r["lock"] == "shared-smoke" for r in startup_writes))
        self.assertEqual((rows[-1]["phase"], rows[-1]["outcome"]),
                         ("pre-admission-dispatch", "success"))

    def test_post_admission_failure_keeps_exception_identity(self):
        self.enable()
        config = load(self.file)
        error = SnapshotConflict("sensitive-error")
        effects = []

        def after_admission(*args, **kwargs):
            # The guard runs immediately after ordinary execution admission.
            with fs.lock(self.project.root / "locks" / "insights.lock"):
                effects.append(1)
                raise error

        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                patch("mizu.runtime.safe_point.guard", side_effect=after_admission):
            with self.assertRaises(SnapshotConflict) as caught:
                Engine(config).run(self.project, "worker")
        self.assertIs(caught.exception, error)
        self.assertEqual(effects, [1])
        row = self.summary(err.getvalue())["observations"][-1]
        self.assertEqual((row["phase"], row["stage"], row["outcome"]), ("post-admission", "body", "error"))

    def test_context_tool_thread_keeps_run_scope(self):
        err = io.StringIO()
        error = Busy("sensitive-error")
        with contextlib.redirect_stderr(err), diagnostics.collect(True):
            diagnostics.phase("post-admission")
            context = self.context()

            def dispatch(*args):
                with diagnostics.operation("write"), record_lock(self.project.root):
                    raise error

            with patch.object(context, "dispatch", side_effect=dispatch), ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaises(Busy) as caught:
                    pool.submit(context.handle, "files", {}).result(timeout=10)
            self.assertIs(caught.exception, error)
            # A nested disabled run neither collects nor inherits its caller.
            with diagnostics.collect(False), patch.object(diagnostics, "_Observation", side_effect=AssertionError):
                with fs.lock(self.root / "insights.lock"):
                    pass
        rows = self.summary(err.getvalue())["observations"]
        self.assertEqual([(r["lock"], r["phase"], r["operation"], r["outcome"]) for r in rows[-2:]],
                         [("project-record", "post-admission", "write", "busy")])

    def test_bounded_tail_unknown_locks_and_saturation(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), diagnostics.collect(True):
            with fs.lock(self.root / "sensitive-record.lock"):
                pass
            for _ in range(diagnostics.MAX_LOCK_DIAGNOSTIC_ROWS + 3):
                with fs.lock(self.root / "insights.lock"):
                    pass
            # Fixed-width numeric fields remain bounded even for a long hold.
            with patch.object(diagnostics.time, "perf_counter_ns", return_value=0):
                observation = diagnostics.begin("insights.lock")
            with patch.object(diagnostics.time, "perf_counter_ns", return_value=10**30):
                observation.advance("local-acquire")
                observation.started = 0
                observation.finish()
        summary = self.summary(err.getvalue())
        self.assertEqual(len(summary["observations"]), diagnostics.MAX_LOCK_DIAGNOSTIC_ROWS)
        self.assertEqual(summary["omitted"], 4)
        self.assertEqual(summary["observations"][-1]["local_ms"], diagnostics.MAX_LOCK_DIAGNOSTIC_VALUE)

    def test_stderr_failure_does_not_replace_busy(self):
        error = Busy("fixture")
        with patch.object(diagnostics, "print", side_effect=OSError("closed"), create=True):
            with self.assertRaises(Busy) as caught:
                with diagnostics.collect(True):
                    raise error
        self.assertIs(caught.exception, error)
