"""Read-only, incomplete-safe finish-only health facts from retained activity."""
import datetime as dt
import json
import os
from unittest.mock import patch

from support import Fixture
from mizu.fs import write_json
from mizu.run_health import finish_only_rates, MAX_RATE_FILE_BYTES


AS_OF = dt.datetime(2026, 10, 28, 12, tzinfo=dt.timezone.utc)


class FinishOnlyRateTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.project.root / "runs").mkdir()

    def record(self, name="unit", *, role="worker", status="completed", at=None, counts=None, **extra):
        path = self.project.root / "runs" / name
        path.mkdir(exist_ok=True)
        write_json(path / "started.json", {"run": name, "role": role})
        result = {"run": name, "role": role, "status": status,
                  "finished_at": (at or AS_OF - dt.timedelta(hours=1)).isoformat(),
                  "activity": {"schema": 1, "open_model_requests": 0,
                               "counts": counts if counts is not None else {"tool_call:finish": 1}},
                  **extra}
        write_json(path / "result.json", result)
        return path

    def facts(self):
        return finish_only_rates(self.project, as_of=AS_OF)

    def worker(self):
        return self.facts()["roles"]["worker"]

    def test_rate_math_and_role_isolation(self):
        self.record("a", counts={"tool_call:finish": 1, "model_request": 3})
        self.record("b", status="waiting", counts={"tool_call:finish": 1, "tool_call:exec": 2})
        self.record("c", counts={"tool_call:finish": 2, "tool_call:read": 0})
        self.record("other", role="reviewer", counts={"tool_call:finish": 1, "tool_call:read": 1})
        facts = self.facts()
        worker, reviewer = facts["roles"]["worker"], facts["roles"]["reviewer"]
        self.assertEqual((worker["finish_only_units"], worker["completed_units"]), (2, 3))
        self.assertEqual(worker["rate"], 2 / 3)
        self.assertEqual(worker["status"], "known")
        self.assertEqual((reviewer["finish_only_units"], reviewer["completed_units"], reviewer["rate"]), (0, 1, 0))
        self.assertEqual(set(facts["roles"]), set(self.config.roles))
        self.assertEqual(facts["target"], 0)

    def test_exact_half_open_window_and_offsets(self):
        start = AS_OF - dt.timedelta(days=1)
        self.record("before", at=start - dt.timedelta(microseconds=1))
        self.record("start", at=start.astimezone(dt.timezone(dt.timedelta(hours=9))))
        self.record("inside", at=AS_OF - dt.timedelta(microseconds=1))
        self.record("end", at=AS_OF)
        self.record("after", at=AS_OF + dt.timedelta(microseconds=1))
        facts = self.facts()
        self.assertEqual(facts["roles"]["worker"]["completed_units"], 2)
        self.assertEqual(facts["window"], {"start": start.isoformat(), "end": AS_OF.isoformat(),
                                           "seconds": 86400, "bounds": "[start,end)"})
        with self.assertRaises(ValueError):
            finish_only_rates(self.project, as_of=AS_OF.replace(tzinfo=None))

    def test_no_denominator_is_not_zero(self):
        worker = self.worker()
        self.assertEqual(worker["status"], "no_completed_units")
        self.assertEqual(worker["completed_units"], 0)
        self.assertIsNone(worker["rate"])

    def test_excludes_noncompleted_and_child_records(self):
        for status in ("prepared", "bounded", "interrupted"):
            self.record(status, status=status, activity=None)
        # A real parent run outside the window proves children without counting.
        self.record("parent", at=AS_OF - dt.timedelta(days=2))
        for kind in ("consultation", "delegation"):
            path = self.record(kind, parent_run="parent")
            (path / "result.json").rename(path / f"{kind}.json")
            write_json(path / "started.json", {"run": kind, "role": "worker", "parent_run": "parent"})
        self.assertEqual(self.worker()["status"], "no_completed_units")

    def test_missing_corrupt_oversized_and_truncated_results(self):
        path = self.record()
        for raw in (None, b"{", b"x" * (MAX_RATE_FILE_BYTES + 1),
                    b'{"role":"worker","role":"worker"}', b'\xff'):
            with self.subTest(raw_kind=type(raw).__name__, length=len(raw) if raw else 0):
                if raw is None:
                    (path / "result.json").unlink()
                else:
                    (path / "result.json").write_bytes(raw)
                worker = self.worker()
                self.assertEqual(worker["status"], "unknown")
                self.assertIsNone(worker["rate"])
                self.assertEqual(worker["reasons"], {"unavailable_result": 1})
                self.assertEqual(self.facts()["roles"]["reviewer"]["reasons"], {})

    def test_unattributable_record_and_missing_activity_are_unknown(self):
        path = self.record()
        record = json.loads((path / "result.json").read_bytes())
        del record["activity"]
        write_json(path / "result.json", record)
        self.assertEqual(self.worker()["reasons"], {"incomplete_activity": 1})
        (path / "result.json").unlink()
        (path / "started.json").unlink()
        for row in self.facts()["roles"].values():
            self.assertEqual(row["status"], "unknown")
            self.assertEqual(row["reasons"], {"unavailable_record": 1})
            self.assertIsNone(row["rate"])

    def test_invalid_or_incomplete_activity_never_claims_zero(self):
        cases = [None, {}, {"schema": 2}, {"schema": 1, "counts": {}},
                 {"schema": 1, "open_model_requests": 0, "counts": {"tool_call:exec": 1}}]
        valid = {"schema": 1, "open_model_requests": 0, "counts": {"tool_call:finish": 1}}
        cases += [{**valid, key: value} for key, value in
                  (("truncated", True), ("counts_truncated", True), ("incomplete", True),
                   ("open_model_requests", 1), ("counts", {"tool_call:finish": True}),
                   ("counts", {"tool_call:finish": -1}),
                   ("counts", {**{f"tool_call:op{i}": 1 for i in range(128)}, "tool_call:finish": 1}))]
        for activity in cases:
            with self.subTest(activity=activity):
                self.record(activity=activity)
                worker = self.worker()
                self.assertEqual(worker["status"], "incomplete")
                self.assertEqual(worker["completed_units"], 1)
                self.assertIsNone(worker["rate"])
        self.record(truncated=True)
        self.assertEqual(self.worker()["status"], "incomplete")

    def test_mixed_known_and_incomplete_keeps_observed_counts_only(self):
        self.record("known")
        self.record("legacy", activity=None)
        worker = self.worker()
        self.assertEqual((worker["finish_only_units"], worker["completed_units"]), (1, 2))
        self.assertEqual(worker["status"], "incomplete")
        self.assertIsNone(worker["rate"])

    def test_scan_and_byte_caps_suppress_rate(self):
        first = self.record("a")
        self.record("b")
        one_entry_bytes = ((first / "result.json").stat().st_size
                           + (first / "started.json").stat().st_size)
        for bound in ({"MAX_RATE_RUNS": 1}, {"MAX_RATE_BYTES": one_entry_bytes}):
            with self.subTest(bound=bound), patch.multiple("mizu.run_health", **bound):
                worker = self.worker()
                self.assertEqual(worker["status"], "incomplete")
                self.assertEqual(worker["reasons"], {"scan_limit": 1})
                self.assertIsNone(worker["rate"])

    def test_unknown_identity_or_time_and_missing_inventory(self):
        for extra in ({"role": None}, {"role": "bad!"}, {"finished_at": "bad"},
                      {"finished_at": "2026-10-28T11:00:00"},
                      {"status": "unexpected"}, {"run": "mismatch"}):
            with self.subTest(extra=extra):
                self.record(**extra)
                self.assertIsNone(self.worker()["rate"])
                self.assertTrue(self.worker()["reasons"])
        (self.project.root / "runs").rename(self.project.root / "saved-runs")
        self.assertEqual(self.worker()["reasons"], {"unavailable_runs": 1})

    def test_unsafe_files_are_not_followed_and_output_is_sanitized(self):
        path = self.record()
        original = path / "result.json"
        target = path / "saved.json"
        original.rename(target)
        try:
            os.link(target, original)
        except OSError:
            self.skipTest("hardlinks unavailable")
        self.assertIsNone(self.worker()["rate"])
        original.unlink()
        try:
            original.symlink_to(target.name)
        except OSError:
            self.skipTest("symlinks unavailable")
        output = json.dumps(self.facts())
        self.assertNotIn(str(self.project.root), output)
        self.assertNotIn("saved.json", output)
        self.assertIsNone(self.worker()["rate"])

    def test_nonfinite_and_deep_json_are_refused(self):
        path = self.record()
        deep = b'{"k":' * 70 + b'1' + b'}' * 70
        self.assertLess(len(deep), MAX_RATE_FILE_BYTES)
        base = {"run": "unit", "role": "worker", "status": "completed",
                "finished_at": (AS_OF - dt.timedelta(hours=1)).isoformat(),
                "activity": {"schema": 1, "open_model_requests": 0,
                             "counts": {"tool_call:finish": 1}}}
        raws = [json.dumps(base).encode()[:-1] + b', "x": NaN}',
                json.dumps(base).encode()[:-1] + b', "x": Infinity}',
                json.dumps(base).encode()[:-1] + b', "x": -Infinity}',
                deep]
        for raw in raws:
            with self.subTest(length=len(raw)):
                (path / "result.json").write_bytes(raw)
                worker = self.worker()
                self.assertIsNone(worker["rate"])
                self.assertEqual(worker["status"], "unknown")
                self.assertEqual(worker["reasons"], {"unavailable_result": 1})
                self.assertEqual(self.facts()["roles"]["reviewer"]["reasons"], {})
                output = json.dumps(self.facts())
                self.assertNotIn(str(self.project.root), output)

    def test_malformed_result_parent_run_is_invalid(self):
        for parent in ("parent", "", 123, False):
            with self.subTest(parent=parent):
                runs = self.project.root / "runs"
                for child in runs.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        import shutil
                        shutil.rmtree(child)
                self.record(parent_run=parent)
                worker = self.worker()
                self.assertIsNone(worker["rate"])
                self.assertEqual(worker["reasons"], {"invalid_identity": 1})
                self.assertEqual(self.facts()["roles"]["reviewer"]["reasons"], {})
                output = json.dumps(self.facts())
                self.assertNotIn(str(self.project.root), output)

    def test_missing_result_fake_child_is_incomplete(self):
        cases = [
            ("fake-no-parent", {"run": "fake-no-parent", "role": "worker", "parent_run": "parent"}),
            ("self-parent", {"run": "self-parent", "role": "worker", "parent_run": "self-parent"}),
            ("bad-type", {"run": "bad-type", "role": "worker", "parent_run": 123}),
            ("empty-marker", {"run": "empty-marker", "role": "worker", "parent_run": ""}),
            ("run-mismatch", {"run": "other", "role": "worker", "parent_run": "parent"}),
            ("bad-role", {"run": "bad-role", "role": "nope!", "parent_run": "parent"}),
        ]
        for name, started in cases:
            with self.subTest(name=name):
                runs = self.project.root / "runs"
                for child in list(runs.iterdir()):
                    if child.is_dir() and not child.is_symlink():
                        import shutil
                        shutil.rmtree(child)
                path = runs / name
                path.mkdir()
                write_json(path / "started.json", started)
                worker = self.worker()
                self.assertIsNone(worker["rate"])
                self.assertTrue(worker["reasons"] or
                                any(row["reasons"] for row in self.facts()["roles"].values()))
                output = json.dumps(self.facts())
                self.assertNotIn(str(self.project.root), output)
                self.assertNotIn(name, output)
        # A corrupt result is not made invisible by a valid child marker.
        runs = self.project.root / "runs"
        for child in list(runs.iterdir()):
            if child.is_dir() and not child.is_symlink():
                import shutil
                shutil.rmtree(child)
        self.record("parent", at=AS_OF - dt.timedelta(days=2))
        malformed_child = runs / "malformed-child"
        malformed_child.mkdir()
        write_json(malformed_child / "started.json",
                   {"run": "malformed-child", "role": "worker", "parent_run": "parent"})
        (malformed_child / "result.json").write_text("{")
        self.assertEqual(self.worker()["status"], "unknown")
        self.assertEqual(self.worker()["reasons"], {"unavailable_result": 1})

        # A proven child with a real parent directory stays excluded.
        for child in list(runs.iterdir()):
            if child.is_dir() and not child.is_symlink():
                import shutil
                shutil.rmtree(child)
        for child in list(runs.iterdir()):
            if child.is_dir() and not child.is_symlink():
                import shutil
                shutil.rmtree(child)
        self.record("parent", at=AS_OF - dt.timedelta(days=2))
        path = self.record("proven-child", parent_run="parent")
        (path / "result.json").rename(path / "proven-child.json")
        write_json(path / "started.json",
                   {"run": "proven-child", "role": "worker", "parent_run": "parent"})
        self.assertEqual(self.worker()["status"], "no_completed_units")

    def test_completed_result_requires_matching_started(self):
        path = self.record()
        # Missing started evidence never yields a known rate.
        (path / "started.json").unlink()
        worker = self.worker()
        self.assertIsNone(worker["rate"])
        self.assertEqual(worker["reasons"], {"unavailable_record": 1})
        for started in ({"run": "mismatch", "role": "worker"},
                        {"run": "unit", "role": "reviewer"},
                        {"run": "unit", "role": "worker", "parent_run": "parent"}):
            with self.subTest(started=started):
                write_json(path / "started.json", started)
                worker = self.worker()
                self.assertIsNone(worker["rate"])
                self.assertEqual(worker["reasons"], {"invalid_identity": 1})
                if started.get("role") != "worker":
                    self.assertEqual(self.facts()["roles"]["reviewer"]["reasons"],
                                     {"invalid_identity": 1})
                output = json.dumps(self.facts())
                self.assertNotIn(str(self.project.root), output)

    def test_safe_root_and_entry_are_refused(self):
        self.record()
        runs = self.project.root / "runs"
        # A non-directory entry cannot be scanned as a run.
        (runs / "flat").write_bytes(b"x")
        self.assertTrue(self.worker()["reasons"])
        self.assertIsNone(self.worker()["rate"])
        (runs / "flat").unlink()
        # A symlinked entry is refused without being followed.
        try:
            (runs / "link-entry").symlink_to("unit", target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        worker = self.worker()
        self.assertIsNone(worker["rate"])
        self.assertIn("unsafe_run_entry", worker["reasons"] if worker["reasons"] else
                      self.facts()["roles"]["worker"]["reasons"])
        (runs / "link-entry").unlink()
        # A symlinked runs root is refused without being followed.
        saved = self.project.root / "saved-runs"
        runs.rename(saved)
        try:
            runs.symlink_to(saved.name, target_is_directory=True)
        except OSError:
            runs.rename(self.project.root / "runs")
            self.skipTest("symlinks unavailable")
        try:
            for row in self.facts()["roles"].values():
                self.assertEqual(row["reasons"], {"unavailable_runs": 1})
                self.assertIsNone(row["rate"])
            output = json.dumps(self.facts())
            self.assertNotIn(str(self.project.root), output)
        finally:
            if runs.is_symlink():
                runs.unlink()
            if saved.exists():
                saved.rename(runs)

    def test_project_status_wires_metric_without_reading_text(self):
        self.record(summary={"ignored": "text"}, prompt={"ignored": "text"})
        from mizu.cli import execute, parser
        with patch("mizu.run_health.finish_only_rates",
                   side_effect=lambda project: finish_only_rates(project, as_of=AS_OF)) as rates:
            args = parser().parse_args(["--config", str(self.file), "status", "sample"])
            metric = execute(args)["finish_only_unit_rate"]
        rates.assert_called_once()
        self.assertEqual(rates.call_args.args[0].name, self.project.name)
        self.assertEqual(metric["roles"]["worker"]["rate"], 1)
        self.assertNotIn("ignored", json.dumps(metric))
