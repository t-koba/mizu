import contextlib
import datetime as dt
import io
import json
import os
from unittest.mock import patch

from support import Fixture
from mizu import scheduler_evidence as evidence
from mizu.cli import execute, main, parser
from mizu.errors import ConfigError, Denied
from mizu.fs import mkdir, write_json


def attempt(index=0, **changes):
    origin = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    row = {"run": "SENTINEL_RUN", "role": "SENTINEL_ROLE", "engine": "codex",
           "started_at": (origin + dt.timedelta(seconds=index * 2)).isoformat(),
           "ended_at": (origin + dt.timedelta(seconds=index * 2 + 1)).isoformat(),
           "status": "returned", "session": {"key": "a" * 64, "generation": 1},
           "models": [{"provider": "SENTINEL_PROVIDER", "model": "SENTINEL_MODEL"}],
           "models_unknown": False, "models_truncated": False,
           "usage": {"status": "partial", "input_tokens": 100, "cache_read_tokens": 25},
           "prompt": "SENTINEL_PROMPT", "error": "SENTINEL_ERROR", "path": "/SENTINEL_PATH"}
    row.update(changes)
    return row


class SchedulerEvidenceTests(Fixture):
    def write(self, rows, name="SENTINEL_DIRECTORY", dropped=0):
        directory = self.project.root / "runs" / name
        mkdir(directory)
        target = directory / "dispatch-attempts.json"
        write_json(target, {"schema": 1, "attempts": rows, "dropped_attempts": dropped})
        return target

    def view(self):
        return evidence.summarize(self.project)

    def test_private_fixed_aggregate_real_independent_groups_and_read_only(self):
        rows = []
        for session, generation, provider, model in (
                ("a", 1, "SENTINEL_PROVIDER", "SENTINEL_MODEL"),
                ("b", 1, "SENTINEL_PROVIDER", "SENTINEL_MODEL"),
                ("a", 2, "SENTINEL_PROVIDER", "SENTINEL_MODEL"),
                ("a", 1, "SENTINEL_PROVIDER2", "SENTINEL_MODEL"),
                ("a", 1, "SENTINEL_PROVIDER", "SENTINEL_MODEL2")):
            for index in range(2):
                row = attempt(len(rows))
                row["session"] = {"key": session * 64, "generation": generation}
                row["models"] = [{"provider": provider, "model": model}]
                rows.append(row)
        target = self.write(rows)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in target.parent.iterdir()}
        view = self.view()
        self.assertEqual(view["groups"]["distinct"], 5)
        self.assertEqual(view["groups"]["with_pairs"], 5)
        self.assertEqual(view["groups"]["attempt_counts"]["quantiles"]["p50"], 2)
        self.assertEqual(view["pairs"]["valid"], 5)
        self.assertEqual(view["period_utc"], {"first": rows[0]["started_at"], "last": rows[-1]["ended_at"]})
        encoded = json.dumps(view)
        for sentinel in ("SENTINEL", "a" * 64, "b" * 64, "codex", str(self.project.root)):
            self.assertNotIn(sentinel, encoded)
        self.assertLess(len(encoded), 8192)
        self.assertIsInstance(view["groups"], dict)  # no group rows or retrieval tokens
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in target.parent.iterdir()})
        with patch.object(evidence, "safe_read", side_effect=Denied("SENTINEL_SECRET /SENTINEL_PATH")):
            failed = self.view()
        self.assertNotIn("SENTINEL", json.dumps(failed))
        self.assertEqual(failed["coverage"]["unavailable_files"], 1)

    def test_engine_denominators_unknown_and_invalid_usage(self):
        rows = [attempt(i) for i in range(8)]
        for index, engine in enumerate(("codex", "pi", "pi-durable", "claude")):
            rows[index]["engine"] = engine
            rows[index]["usage"].update(input_tokens=50, cache_read_tokens=25, cache_write_tokens=25)
        rows[4]["engine"] = "unsupported"
        rows[5]["usage"]["cache_read_tokens"] = 101
        rows[6]["engine"] = "pi"  # missing cache-write is not zero
        rows[7]["usage"]["input_tokens"] = True
        self.write(rows)
        view = self.view()
        self.assertEqual(view["attempts"]["eligible"], 4)
        self.assertEqual(view["attempts"]["excluded"]["unknown_denominator"], 4)
        self.assertEqual(view["cache_read_share"]["quantiles"]["p50"], .25)
        self.assertEqual(view["cache_read_share"]["quantiles"]["p100"], .5)
        self.assertEqual(view["pairs"]["excluded"]["different_model_or_engine"], 2)

    def test_disjoint_chronological_pairs_quantiles_and_sparse_bins(self):
        rows = [attempt(i) for i in range(10)]
        for row, share in zip(rows, (0, 100, 25, 75, 50, 50, 75, 25, 100, 0)):
            row["usage"]["cache_read_tokens"] = share
        self.write(list(reversed(rows)))
        view = self.view()
        self.assertEqual(view["pairs"]["valid"], 5)
        self.assertEqual(view["pairs"]["change_quantiles"], {
            "count": 5, "sparse": False,
            "quantiles": {"p0": -1, "p25": -.5, "p50": 0, "p75": .5, "p100": 1}})
        self.assertEqual([b["count"] for b in view["cache_read_share"]["bins"]], [2, 2, 2, 4])
        self.assertTrue(all(b["sparse"] for b in view["cache_read_share"]["bins"]))
        self.assertTrue(view["groups"]["median_changes"]["sparse"])

    def test_idle_gap_seconds_use_only_disjoint_valid_pairs(self):
        rows = [attempt(i) for i in range(10)]
        for index, gap in enumerate((0, .25, .5, .75, 1)):
            rows[index * 2 + 1]["started_at"] = (
                dt.datetime.fromisoformat(rows[index * 2]["ended_at"])
                + dt.timedelta(seconds=gap)).isoformat()
        self.write(list(reversed(rows)))
        view = self.view()
        self.assertEqual(view["pairs"]["idle_gap_seconds"], {
            "count": view["pairs"]["valid"], "sparse": False,
            "quantiles": {"p0": 0, "p25": .25, "p50": .5, "p75": .75, "p100": 1}})
        self.write(rows[:2])
        self.assertEqual(self.view()["pairs"]["idle_gap_seconds"], {
            "count": 1, "sparse": True,
            "quantiles": dict.fromkeys(("p0", "p25", "p50", "p75", "p100"), 0)})

    def test_idle_gap_seconds_exclude_invalid_and_overlapping_pairs(self):
        for bad in (dict(status="started"), dict(usage={"status": "unknown"}),
                    dict(started_at=None), dict(started_at=attempt(0)["started_at"]),
                    dict(ended_at=attempt(0)["ended_at"]),
                    dict(models=[{"provider": "other", "model": "other"}])):
            with self.subTest(bad=bad):
                # A separate session supplies one valid positive gap; no invalid
                # or overlapping endpoint may contribute a zero or negative gap.
                self.write([attempt(0), attempt(1, **bad),
                            attempt(2, session={"key": "b" * 64, "generation": 1}),
                            attempt(3, session={"key": "b" * 64, "generation": 1})])
                view = self.view()
                self.assertEqual(view["pairs"]["valid"], 1)
                self.assertEqual(view["pairs"]["idle_gap_seconds"], {
                    "count": 1, "sparse": True,
                    "quantiles": dict.fromkeys(("p0", "p25", "p50", "p75", "p100"), 1)})

    def test_ended_driver_error_attempt_remains_measurable(self):
        self.write([attempt(0), attempt(1, status="raised")])
        view = self.view()
        self.assertEqual(view["attempts"]["eligible"], 2)
        self.assertEqual(view["pairs"]["valid"], 1)
        self.assertEqual(view["attempts"]["excluded"]["unfinished"], 0)

    def test_hidden_invalid_attempts_are_barriers_not_filtered_adjacency(self):
        for bad in (dict(models_unknown=True), dict(usage={"status": "unknown"}),
                    dict(engine="unknown"), dict(status="started")):
            with self.subTest(bad=bad):
                self.write([attempt(0), attempt(1, **bad), attempt(2)])
                view = self.view()
                self.assertEqual(view["pairs"]["valid"], 0)
                self.assertEqual(view["pairs"]["idle_gap_seconds"]["count"], 0)
                self.assertEqual(view["pairs"]["candidates"], 1)
                self.assertEqual(view["pairs"]["unpaired"], 1)
        self.write([attempt(0), attempt(1, session={}), attempt(2)])
        self.assertEqual(self.view()["pairs"]["excluded"]["unsequenced"], 1)
        self.write([attempt(0), attempt(1, started_at=None), attempt(2), attempt(3)])
        self.assertEqual(self.view()["pairs"]["excluded"]["unsequenced"], 2)

    def test_overlapping_equal_nested_and_missing_intervals(self):
        rows = [attempt(i) for i in range(4)]
        rows[0]["ended_at"] = rows[3]["ended_at"]  # overlaps all, not only neighbor
        self.write(rows)
        self.assertEqual(self.view()["pairs"]["excluded"]["overlap"], 2)
        self.write([attempt(0), attempt(0)])
        self.assertEqual(self.view()["pairs"]["excluded"]["overlap"], 1)
        self.write([attempt(0), attempt(1, ended_at=None)])
        self.assertEqual(self.view()["pairs"]["excluded"]["unsequenced"], 1)
        self.write([attempt(0), attempt(1, ended_at=attempt(1)["started_at"])])
        self.assertEqual(self.view()["pairs"]["valid"], 0)
        self.write([attempt(0), attempt(1, started_at=attempt(0)["ended_at"])])
        self.assertEqual(self.view()["pairs"]["valid"], 1)  # touching, not overlapping

    def test_exact_model_and_engine_comparisons_and_no_cross_file_pairs(self):
        second = attempt(1, models=[{"provider": "other", "model": "SENTINEL_MODEL"}])
        self.write([attempt(0), second])
        self.assertEqual(self.view()["pairs"]["excluded"]["different_model_or_engine"], 1)
        self.write([attempt(0)])
        self.write([attempt(1)], name="another")
        view = self.view()
        self.assertEqual(view["pairs"]["valid"], 0)
        self.assertEqual(view["groups"]["distinct"], 1)
        self.assertEqual(view["groups"]["with_pairs"], 0)
        self.assertIsNone(view["groups"]["median_changes"]["quantiles"]["p50"])

    def test_interleaved_files_are_not_skipped_and_cross_file_overlaps_rejected(self):
        self.write([attempt(0), attempt(2)], name="first")
        self.write([attempt(1)], name="second")
        view = self.view()
        self.assertEqual(view["pairs"]["valid"], 0)
        self.assertEqual(view["pairs"]["excluded"]["file_boundary"], 1)
        self.write([attempt(0), attempt(1)], name="first")
        self.write([attempt(1, ended_at=attempt(3)["ended_at"])], name="second")
        view = self.view()
        self.assertEqual(view["pairs"]["excluded"]["overlap"], 1)
        self.assertEqual(view["pairs"]["valid"], 0)

    def test_missing_corrupt_dropped_and_empty_coverage_are_unknown(self):
        mkdir(self.project.root / "runs")
        empty = self.view()
        self.assertEqual(empty["coverage"]["status"], "empty_unknown")
        self.assertEqual(empty["coverage"]["history"], "unknown")
        self.assertIsNone(empty["cache_read_share"]["quantiles"]["p50"])
        self.assertEqual(empty["pairs"]["idle_gap_seconds"], {
            "count": 0, "sparse": True,
            "quantiles": dict.fromkeys(("p0", "p25", "p50", "p75", "p100"))})
        self.write([attempt(0), attempt(1)], dropped=7)
        missing = self.project.root / "runs" / "missing"
        mkdir(missing)
        corrupt = self.write([], name="corrupt")
        corrupt.write_text('{"secret": "SENTINEL_ERROR"')
        view = self.view()
        self.assertEqual(view["coverage"]["status"], "incomplete_unknown")
        self.assertEqual(view["coverage"]["dropped_attempts"], 7)
        self.assertEqual(view["coverage"]["unavailable_files"], 1)
        self.assertEqual(view["coverage"]["malformed_files"], 1)
        self.assertEqual(view["pairs"]["excluded"]["unsequenced"], 1)
        (self.project.root / "runs" / "corrupt" / "dispatch-attempts.json").write_text(
            '{"schema":1,"schema":1,"attempts":[],"dropped_attempts":0}')
        self.assertEqual(self.view()["coverage"]["malformed_files"], 1)
        for invalid in ({}, {"schema": 1, "attempts": [], "dropped_attempts": -1},
                        {"schema": 2, "attempts": [], "dropped_attempts": 0}):
            write_json(corrupt, invalid)
            self.assertEqual(self.view()["coverage"]["malformed_files"], 1)

    def test_attempt_byte_and_output_limits(self):
        self.write([attempt(i) for i in range(evidence.MAX_ATTEMPTS + 1)])
        view = self.view()
        self.assertEqual(view["attempts"]["observed"], evidence.MAX_ATTEMPTS)
        self.assertEqual(view["coverage"]["attempt_limit_files"], 1)
        self.assertEqual(view["pairs"]["valid"], 0)
        oversized = self.write([], name="oversized")
        oversized.write_bytes(b" " * (evidence.MAX_BYTES + 1))
        self.assertEqual(self.view()["coverage"]["oversized_files"], 1)
        self.assertLess(len(json.dumps(view)), 8192)

    def test_fixed_output_shape_retains_eight_kib_headroom(self):
        self.write([attempt(0), attempt(1)])

        def width_envelope(value):
            # Counts are bounded by scan limits (including summed 63-bit
            # dropped counts: <=22 digits). Float repr needs <=24 bytes.
            # Quoted placeholders overestimate both, even for null quantiles.
            if isinstance(value, dict):
                return {key: width_envelope(item) for key, item in value.items()}
            if isinstance(value, list):
                return [width_envelope(item) for item in value]
            if isinstance(value, bool):
                return False  # longer than true
            if value is None or isinstance(value, (int, float)):
                return "9" * 24
            return value

        envelope = width_envelope(self.view())
        envelope["period_utc"] = dict.fromkeys(("first", "last"), "9" * 32)
        for indent in (None, 2):  # include the CLI's pretty JSON and newline
            self.assertLess(len(json.dumps(envelope, indent=indent)) + 1, 8192)

    def test_candidate_enumeration_and_file_limits(self):
        for index in range(6):
            self.write([attempt(0)], name=f"run{index}")
        with patch.object(evidence, "MAX_RUNS_SCANNED", 4), patch.object(evidence, "MAX_CACHE_AFFINITY_RUNS", 2):
            view = self.view()
        self.assertEqual(view["coverage"]["candidate_entries"], 4)
        self.assertEqual(view["coverage"]["files_attempted"], 2)
        self.assertTrue(view["coverage"]["candidate_limit"])
        self.assertTrue(view["coverage"]["file_limit"])
        self.assertEqual(view["coverage"]["status"], "incomplete_unknown")
        # Non-directories consume enumeration budget too, not an unbounded filter.
        for path in (self.project.root / "runs").iterdir():
            (path / "dispatch-attempts.json").unlink()
            path.rmdir()
            path.write_text("ignored")
        with patch.object(evidence, "MAX_RUNS_SCANNED", 4):
            view = self.view()
        self.assertEqual(view["coverage"]["candidate_entries"], 4)
        self.assertEqual(view["coverage"]["unsafe_entries"], 4)

    def test_timeout_before_scan_and_after_read(self):
        self.write([attempt(0), attempt(1)])
        with patch.object(evidence.time, "monotonic", side_effect=[0] + [3] * 20):
            view = self.view()
        self.assertTrue(view["coverage"]["timeout"])
        self.assertEqual(view["coverage"]["files_attempted"], 0)
        original = evidence.safe_read
        clock = [0]

        def slow_read(*args):
            raw = original(*args)
            clock[0] = 3
            return raw

        with patch.object(evidence.time, "monotonic", side_effect=lambda: clock[0]), patch.object(evidence, "safe_read", side_effect=slow_read):
            view = self.view()
        self.assertTrue(view["coverage"]["timeout"])
        self.assertEqual(view["coverage"]["files_attempted"], 1)
        self.assertEqual(view["attempts"]["observed"], 0)

    def test_symlinks_and_hardlinks_do_not_read_outside_private_root(self):
        target = self.write([attempt(0)])
        outside = self.root / "outside.json"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            self.skipTest("Host does not permit symlinks")
        self.assertEqual(self.view()["coverage"]["unavailable_files"], 1)
        target.unlink()
        os.link(outside, target)
        self.assertEqual(self.view()["coverage"]["unavailable_files"], 1)
        target.unlink()
        target.parent.rmdir()
        target.parent.symlink_to(outside.parent, target_is_directory=True)
        self.assertEqual(self.view()["coverage"]["unsafe_entries"], 1)
        target.parent.unlink()
        target.parent.parent.rmdir()
        target.parent.parent.symlink_to(outside.parent, target_is_directory=True)
        self.assertTrue(self.view()["coverage"]["scan_unavailable"])

    def test_cli_separate_mode_normal_authentication_and_read_only_project(self):
        self.write([attempt(0), attempt(1)])
        args = parser().parse_args(["--config", str(self.file), "usage", self.project.name, "--cache-evidence"])
        before = {str(p.relative_to(self.project.root)): p.read_bytes()
                  for p in self.project.root.rglob("*") if p.is_file()}
        view = execute(args)
        self.assertEqual(view["pairs"]["valid"], 1)
        self.assertEqual(before, {str(p.relative_to(self.project.root)): p.read_bytes()
                                  for p in self.project.root.rglob("*") if p.is_file()})
        args.cache_evidence = False
        with patch("mizu.usage.summarize", return_value={"legacy": True}) as legacy:
            self.assertEqual(execute(args), {"legacy": True})
            legacy.assert_called_once()
        args.cache_evidence = True
        stderr = io.StringIO()
        with patch("mizu.cli.load", side_effect=ConfigError("SENTINEL_SECRET /SENTINEL_PATH")), contextlib.redirect_stderr(stderr):
            self.assertNotEqual(main(["--config", str(self.file), "usage", self.project.name, "--cache-evidence"]), 0)
        self.assertNotIn("SENTINEL", stderr.getvalue())
        (self.project.root / "project.toml").unlink()
        with self.assertRaises(ConfigError):
            execute(args)
        args.project = "../outside"
        with self.assertRaises((Denied, ConfigError)):
            execute(args)
