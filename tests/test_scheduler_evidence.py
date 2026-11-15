import contextlib
import datetime as dt
import dataclasses
import io
import json
import os
import shutil
from unittest.mock import patch

from support import Fixture
from mizu import dispatch_evidence as dispatch
from mizu import scheduler_evidence as evidence
from mizu.cli import execute, main, parser
from mizu.engine_config import effective, reset_role_sessions, session_record
from mizu.config import MAX_USAGE_POLICY_COUNT, TOML_INTEGER_MAX, DispatchEvidence, SchedulerEvidence
from mizu.errors import ConfigError, Denied
from mizu.fs import mkdir, write_json


def attempt(index=0, **changes):
    origin = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    row = {"run": "SENTINEL_RUN", "role": "SENTINEL_ROLE", "engine": "codex",
           "started_at": (origin + dt.timedelta(seconds=index * 2)).isoformat(),
           "ended_at": (origin + dt.timedelta(seconds=index * 2 + 1)).isoformat(),
           "status": "returned", "session": {"key": "a" * 64, "generation": 1},
           "dispatch_sequence_id": "a" * 32,
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

    def sequence_state(self, *, key="a" * 64, generation=1, ordinal=1,
                       sequence_id="a" * 32, role="worker", witness=b"00"):
        directory = self.project.root / "sessions" / role / key
        mkdir(directory)
        (directory / "dispatch-order.lock").write_bytes(witness)
        write_json(directory / "dispatch-order.json",
                   {"generation": generation, "ordinal": ordinal,
                    "sequence_id": sequence_id})
        return directory

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
        for sentinel in ("SENTINEL", "a" * 64, "b" * 64, "a" * 32, "codex", str(self.project.root)):
            self.assertNotIn(sentinel, encoded)
        self.assertLess(len(encoded), 8192)
        self.assertIsInstance(view["groups"], dict)  # no group rows or retrieval tokens
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in target.parent.iterdir()})
        with patch.object(evidence, "safe_read", side_effect=Denied("SENTINEL_SECRET /SENTINEL_PATH")):
            failed = self.view()
        self.assertNotIn("SENTINEL", json.dumps(failed))
        self.assertEqual(failed["coverage"]["unavailable_files"], 1)

    def test_dispatch_policy_controls_reader_not_scan_windows(self):
        rows = [attempt(i) for i in range(66)]
        rows[0]["ignored_payload"] = "x" * 262144
        target = self.write(rows)
        size = target.stat().st_size
        self.assertEqual(self.view()["coverage"]["oversized_files"], 1)
        for count in (2, 66):
            self.project.config = dataclasses.replace(self.config, dispatch_evidence=DispatchEvidence(
                max_attempts=count, max_bytes=size))
            view = self.view()
            self.assertEqual(view["attempts"]["observed"], count)
            self.assertEqual(view["coverage"]["attempt_limit_files"], int(count < 66))
            self.assertEqual(view["limits"]["attempts_per_file"], count)
            self.assertEqual(view["limits"]["bytes_per_file"], size)
            self.assertEqual(view["limits"]["files"], self.config.scheduler_evidence.files)
            self.project.config = dataclasses.replace(self.project.config,
                dispatch_evidence=DispatchEvidence(max_attempts=count, max_bytes=size - 1))
            self.assertEqual(self.view()["coverage"]["oversized_files"], 1)

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

    def test_ordinal_continuity_crosses_files_without_timestamp_ordering(self):
        self.sequence_state(ordinal=8)
        self.write([attempt(1, role="worker", dispatch_ordinal=8)], name="second")
        self.write([attempt(0, role="worker", dispatch_ordinal=7)], name="first")
        view = self.view()
        self.assertEqual(view["pairs"]["valid"], 1)
        self.assertEqual(view["pairs"]["idle_gap_seconds"]["quantiles"]["p50"], 1)
        # Swapping ordinals must not be repaired by sorting wall timestamps.
        self.write([attempt(1, role="worker", dispatch_ordinal=7)], name="second")
        self.write([attempt(0, role="worker", dispatch_ordinal=8)], name="first")
        self.assertEqual(self.view()["pairs"]["valid"], 0)
        self.assertEqual(self.view()["pairs"]["excluded"]["overlap"], 1)

    def test_explicit_reset_cannot_bridge_missing_first_post_reset_run(self):
        context = self.context()
        settings = effective(self.config, context.role, context.role.profile)
        path, _ = session_record(context, context.role.profile, settings)
        before = dispatch._dispatch_reservation(path.parent, 0)
        self.write([attempt(0, role=context.role.name,
                            session={"key": path.parent.name, "generation": 0},
                            dispatch_ordinal=before[0], dispatch_sequence_id=before[1])],
                   name="pre-reset")

        reset_role_sessions(self.config, self.project, context.role.name)
        new_path, _ = session_record(context, context.role.profile, settings)
        first = dispatch._dispatch_reservation(new_path.parent, 0)
        second = dispatch._dispatch_reservation(new_path.parent, 0)
        self.assertEqual((first[0], second[0]), (1, 2))
        self.assertNotEqual(before[1], first[1])
        missing = self.write([attempt(1, role=context.role.name,
                                      session={"key": new_path.parent.name, "generation": 0},
                                      dispatch_ordinal=first[0], dispatch_sequence_id=first[1])],
                             name="post-reset-first")
        self.write([attempt(2, role=context.role.name,
                            session={"key": new_path.parent.name, "generation": 0},
                            dispatch_ordinal=second[0], dispatch_sequence_id=second[1])],
                   name="post-reset-second")
        missing.unlink()
        self.assertEqual(self.view()["pairs"]["valid"], 0)

    def test_missing_or_mismatched_sequence_identity_blocks_pair(self):
        self.sequence_state(generation=1, ordinal=2)
        cases = ((None, None), ("b" * 32, "b" * 32), ("!" * 32, "!" * 32))
        for index, ids in enumerate(cases):
            with self.subTest(ids=ids):
                target = self.write([attempt(0, role="worker", dispatch_ordinal=1,
                                             dispatch_sequence_id=ids[0]),
                                     attempt(1, role="worker", dispatch_ordinal=2,
                                             dispatch_sequence_id=ids[1])],
                                    name=f"identity-{index}")
                view = self.view()
                self.assertEqual(view["pairs"]["valid"], 0)
                self.assertGreater(view["pairs"]["excluded"]["unsequenced"], 0)
                target.unlink()

    def test_unknown_sequence_identity_blocks_numbered_pairs_for_session(self):
        for sequence_id in (None, "!" * 32):
            with self.subTest(sequence_id=sequence_id):
                self.sequence_state(ordinal=2, sequence_id="a" * 32)
                self.write([attempt(0, role="worker", dispatch_ordinal=1),
                            attempt(1, role="worker", dispatch_ordinal=2),
                            attempt(2, role="worker", dispatch_ordinal=None,
                                    dispatch_sequence_id=sequence_id)])
                view = self.view()
                self.assertEqual(view["pairs"]["valid"], 0)
                self.assertEqual(view["pairs"]["excluded"]["unsequenced"], 1)

    def test_unknown_sequence_identity_only_blocks_its_session_generation(self):
        self.sequence_state(generation=2, ordinal=2, sequence_id="a" * 32)
        self.sequence_state(key="b" * 64, ordinal=2, sequence_id="b" * 32)
        self.write([attempt(0, role="worker", dispatch_ordinal=1),
                    attempt(1, role="worker", dispatch_ordinal=2),
                    attempt(2, role="worker", dispatch_ordinal=None,
                            dispatch_sequence_id=None),
                    attempt(3, role="worker", session={"key": "a" * 64, "generation": 2},
                            dispatch_ordinal=1, dispatch_sequence_id="a" * 32),
                    attempt(4, role="worker", session={"key": "a" * 64, "generation": 2},
                            dispatch_ordinal=2, dispatch_sequence_id="a" * 32),
                    attempt(5, role="worker", session={"key": "b" * 64, "generation": 1},
                            dispatch_ordinal=1, dispatch_sequence_id="b" * 32),
                    attempt(6, role="worker", session={"key": "b" * 64, "generation": 1},
                            dispatch_ordinal=2, dispatch_sequence_id="b" * 32)])
        view = self.view()
        self.assertEqual(view["pairs"]["valid"], 2)
        self.assertEqual(view["pairs"]["excluded"]["unsequenced"], 1)

    def test_unknown_sequence_identity_blocks_all_matching_reset_stream_ids(self):
        rows = [attempt(0, role="worker", dispatch_ordinal=1),
                attempt(1, role="worker", dispatch_ordinal=2),
                attempt(2, role="worker", dispatch_ordinal=1, dispatch_sequence_id="b" * 32),
                attempt(3, role="worker", dispatch_ordinal=2, dispatch_sequence_id="b" * 32),
                attempt(4, role="worker", dispatch_ordinal=None, dispatch_sequence_id=None)]
        self.write(rows)
        # Isolate generation-level attribution from the single current-counter
        # check: either exact identity would otherwise be allowed by the pairer.
        with patch.object(evidence, "_sequence_state", return_value=True):
            view = self.view()
        self.assertEqual(view["pairs"]["candidates"], 2)
        self.assertEqual(view["pairs"]["valid"], 0)
        self.assertEqual(view["pairs"]["excluded"]["unsequenced"], 2)

    def test_legacy_missing_identity_still_pairs_within_one_file(self):
        rows = [attempt(0), attempt(1)]
        for row in rows:
            row.pop("dispatch_sequence_id")
        self.write(rows)
        self.assertEqual(self.view()["pairs"]["valid"], 1)

    def test_ordinal_gaps_duplicates_unknowns_and_legacy_fail_closed(self):
        cases = [((1, 3), "ordinal_gap"), ((1, 1), "duplicate_ordinal"),
                 ((1, 2, 2, 3), "duplicate_ordinal")]
        cases.extend(((1, invalid), "unsequenced") for invalid in (None, True, 0, -1, "2", 2**63))
        for ordinals, reason in cases:
            with self.subTest(ordinals=ordinals):
                valid = [o for o in ordinals if type(o) is int and 1 <= o < 2**63]
                self.sequence_state(ordinal=max(valid, default=1))
                self.write([attempt(i, role="worker", dispatch_ordinal=o)
                            for i, o in enumerate(ordinals)])
                view = self.view()
                self.assertEqual(view["pairs"]["valid"], 0)
                self.assertEqual(view["pairs"]["excluded"][reason], len(ordinals) // 2)
        self.write([attempt(0, dispatch_ordinal=1), attempt(1)])
        self.assertEqual(self.view()["pairs"]["excluded"]["unsequenced"], 1)
        # Old records remain timestamp-ordered and never cross file boundaries.
        self.write([attempt(1), attempt(0)])
        self.assertEqual(self.view()["pairs"]["valid"], 1)
        self.write([attempt(0)])
        self.write([attempt(1)], name="another")
        self.assertEqual(self.view()["pairs"]["excluded"]["file_boundary"], 1)

    def test_numbered_pairs_retain_endpoint_and_file_safety(self):
        self.sequence_state(ordinal=3)
        first = attempt(0, role="worker", dispatch_ordinal=1)
        second = attempt(1, role="worker", dispatch_ordinal=2)
        for change in (dict(status="started"), dict(usage={"status": "unknown"}),
                       dict(models_unknown=True), dict(ended_at=None),
                       dict(started_at=first["started_at"]),
                       dict(session={"key": None, "generation": None})):
            with self.subTest(change=change):
                self.write([first], name="first")
                self.write([{**second, **change}], name="second")
                self.assertEqual(self.view()["pairs"]["valid"], 0)
        self.write([second], name="second", dropped=1)
        self.assertEqual(self.view()["pairs"]["excluded"]["unsequenced"], 1)
        self.write([second, attempt(2, role="worker", dispatch_ordinal=3)], name="second")
        self.project.config = dataclasses.replace(self.config,
            dispatch_evidence=DispatchEvidence(max_attempts=1))
        self.assertEqual(self.view()["pairs"]["excluded"]["unsequenced"], 1)

    def test_poisoned_or_missing_sequence_state_blocks_pairs_after_run_loss(self):
        self.write([attempt(0, role="worker", dispatch_ordinal=1)], name="first")
        missing = self.write([attempt(1, role="worker", dispatch_ordinal=None)], name="lost")
        self.write([attempt(2, role="worker", dispatch_ordinal=2)], name="last")
        self.sequence_state(ordinal=2, witness=b"0!")
        missing.unlink()
        view = self.view()
        self.assertEqual(view["pairs"]["valid"], 0)
        self.assertEqual(view["pairs"]["excluded"]["unsequenced"], 1)
        self.assertEqual(view["pairs"]["excluded"]["ordinal_gap"], 0)

        # An explicit session reset removes the witness; old ordinals are then
        # unknown rather than trusted using a same-key recreated directory.
        shutil.rmtree(self.project.root / "sessions")
        view = self.view()
        self.assertEqual(view["pairs"]["valid"], 0)
        self.assertEqual(view["pairs"]["excluded"]["unsequenced"], 1)

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
        self.write([attempt(i) for i in range(self.config.dispatch_evidence.max_attempts + 1)])
        view = self.view()
        self.assertEqual(view["attempts"]["observed"], self.config.dispatch_evidence.max_attempts)
        self.assertEqual(view["coverage"]["attempt_limit_files"], 1)
        self.assertEqual(view["pairs"]["valid"], 0)
        oversized = self.write([], name="oversized")
        oversized.write_bytes(b" " * (self.config.dispatch_evidence.max_bytes + 1))
        self.assertEqual(self.view()["coverage"]["oversized_files"], 1)
        self.assertLess(len(json.dumps(view)), 8192)

    def test_fixed_output_shape_retains_eight_kib_headroom(self):
        self.write([attempt(0), attempt(1)])

        def width_envelope(value):
            # Counts are bounded by scan limits (including summed 63-bit
            # dropped counts: <=38 digits). Float repr needs <=24 bytes.
            # Quoted placeholders overestimate both, even for null quantiles.
            if isinstance(value, dict):
                return {key: width_envelope(item) for key, item in value.items()}
            if isinstance(value, list):
                return [width_envelope(item) for item in value]
            if isinstance(value, bool):
                return False  # longer than true
            if value is None or isinstance(value, (int, float)):
                return "9" * 40
            return value

        envelope = width_envelope(self.view())
        envelope["period_utc"] = dict.fromkeys(("first", "last"), "9" * 32)
        for indent in (None, 2):  # include the CLI's pretty JSON and newline
            self.assertLess(len(json.dumps(envelope, indent=indent)) + 1, 8192)

    def test_candidate_enumeration_and_file_limits(self):
        for index in range(6):
            self.write([attempt(0)], name=f"run{index}")
        self.project.config = dataclasses.replace(
            self.config, scheduler_evidence=SchedulerEvidence(candidate_entries=4, files=2))
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
        view = self.view()
        self.assertEqual(view["coverage"]["candidate_entries"], 4)
        self.assertEqual(view["coverage"]["unsafe_entries"], 4)

    def test_cli_configured_limits_cutoffs_and_usage_independence(self):
        for index in range(6):
            target = self.write([attempt(index)], name=f"run{index}")
            stamp = 1700000000000000000 + index * 1000000000
            os.utime(target.parent, ns=(stamp, stamp))
        args = parser().parse_args(["--config", str(self.file), "usage", self.project.name, "--cache-evidence"])
        original = self.file.read_text()
        self.file.write_text(original + "\n[usage]\nruns_scanned = 1\ncache_affinity_runs = 0\n")
        view = execute(args)
        self.assertEqual(view["limits"], dict(candidate_entries=5000, files=200, seconds=2.0,
                                             bytes_per_file=262144, attempts_per_file=64))
        self.assertIs(type(view["limits"]["seconds"]), float)
        self.assertEqual(view["coverage"]["files_read"], 6)
        for candidates, files, seconds in ((4, 2, 7), (5001, 201, 3),
                                            (MAX_USAGE_POLICY_COUNT, MAX_USAGE_POLICY_COUNT, TOML_INTEGER_MAX)):
            self.file.write_text(original + f"\n[scheduler_evidence]\ncandidate_entries = {candidates}\n"
                                 f"files = {files}\nscan_seconds = {seconds}\n")
            with patch.object(evidence, "safe_read", wraps=evidence.safe_read) as reader:
                view = execute(args)
            self.assertEqual(view["limits"], dict(candidate_entries=candidates, files=files,
                                                 seconds=float(seconds), bytes_per_file=262144,
                                                 attempts_per_file=64))
            self.assertEqual(view["coverage"]["candidate_entries"], min(candidates, 6))
            self.assertEqual(view["coverage"]["files_read"], min(files, candidates, 6))
            self.assertEqual(reader.call_count, min(files, candidates, 6))
            self.assertEqual(view["coverage"]["candidate_limit"], candidates <= 6)
            self.assertEqual(view["coverage"]["file_limit"], files < min(candidates, 6))
            # The file window selects newest-first, not enumeration order.
            names = [call.args[1].split("/")[1] for call in reader.call_args_list]
            self.assertEqual(names, sorted(names, reverse=True))
        self.file.write_text(original + "\n[scheduler_evidence]\nscan_seconds = 7\n")
        clock = [0]
        read = evidence.safe_read

        def slow_read(*args):
            raw = read(*args)
            clock[0] += 3.5
            return raw

        with patch.object(evidence.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
                evidence, "safe_read", side_effect=slow_read):
            view = execute(args)
        self.assertEqual(view["limits"]["seconds"], 7.0)
        self.assertTrue(view["coverage"]["timeout"])
        self.assertEqual(view["coverage"]["files_attempted"], 2)
        self.assertEqual(view["coverage"]["files_read"], 1)
        self.assertEqual(view["coverage"]["status"], "incomplete_unknown")
        self.assertEqual(view["attempts"]["observed"], 1)

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
