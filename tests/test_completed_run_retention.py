"""Synthetic completed-run retention: no engines, network or installed state."""
import dataclasses
import datetime as dt
import json
import os
import uuid
from unittest import mock

from support import Fixture
from mizu.config import load
from mizu.errors import Busy, ConfigError, Denied
from mizu.fs import lock, write_json
from mizu.storage import backup, prune, reclaim
from mizu import storage_runs as runs

CLOCK = 2_000_000_000
DAY = 86400


def stamp(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


class CompletedRunRetentionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(runs.time, "time", return_value=CLOCK).start()

    def run_record(self, age=40, *, parent=None, terminal="result.json", status=None):
        run = uuid.uuid4().hex
        directory = self.project.root / "runs" / run
        started = {"run": run, "role": "worker", "snapshot": "a" * 64,
                   "started_at": stamp(CLOCK - age * DAY)}
        result = {"run": run, "role": "worker", "snapshot": "a" * 64,
                  "finished_at": stamp(CLOCK - age * DAY + 1),
                  "status": status or "completed"}
        if parent:
            started["parent_run"] = parent
            result["parent_run"] = parent
        if terminal in ("consultation.json", "delegation.json"):
            result.update(answer="answer", snapshot="a" * 64, profile="primary")
        elif terminal == "error.json":
            result.update(status=status or "interrupted", error="failure")
        else:
            result["finish"] = {"outcome": "done", "summary": "done", "state": ""}
        write_json(directory / "started.json", started)
        if terminal:
            write_json(directory / terminal, result)
        return run

    def read(self, run, name):
        return json.loads((self.project.root / "runs" / run / name).read_bytes())

    def change(self, run_id, name, **fields):
        write_json(self.project.root / "runs" / run_id / name, {**self.read(run_id, name), **fields})

    def preview(self):
        result = prune(self.project)["run_retention"]
        self.assertTrue(result["complete"], result)
        return result

    def candidates(self):
        return set(self.preview()["runs"])

    def policy(self, days):
        self.project.config = dataclasses.replace(self.project.config,
                            limits=dataclasses.replace(self.config.limits, run_retention_days=days))

    def test_policy_defaults_zero_and_config_boundaries(self):
        self.assertEqual(load(self.file).limits.run_retention_days, 31)
        old = self.run_record(age=4000)
        self.policy(0)
        self.assertEqual(self.candidates(), set())
        self.policy(3650)
        self.assertEqual(self.candidates(), {old})
        original = self.file.read_text()
        for value in ("0", "3650", "-1", "3651", "true", '"31"', "31.0"):
            with self.subTest(value=value):
                self.file.write_text(original.replace("run_retention_days = 31", "run_retention_days = " + value))
                if value in ("0", "3650"):
                    self.assertEqual(load(self.file).limits.run_retention_days, int(value))
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)
        self.file.write_text(original)

    def test_zero_policy_skips_corrupt_live_references_but_prunes_projections(self):
        old = self.run_record()
        blocked = self.block({"kind": "deadline", "at": "2040-01-01T00:00:00+00:00"})
        corrupt_paths = [self.project.root / "inbox" / "bad.json",
                         self.project.root / "work-items" / blocked["id"] / "2.json"]
        for path in corrupt_paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{")
        self.policy(0)
        directory = self.project.root / "runs" / old
        (directory / "input").mkdir()
        (directory / "input" / "prompt").write_bytes(b"reproducible")
        log = directory / "worker-events.jsonl"
        log.write_bytes(b"old log")
        os.utime(log, (CLOCK - 40 * DAY,) * 2)
        cache = self.config.data / "web-cache" / ("a" * 64 + ".json")
        write_json(cache, {"retrieved_epoch": CLOCK - 40 * DAY})
        dry = prune(self.project)
        self.assertTrue(dry["run_retention"]["complete"])
        self.assertEqual(dry["run_retention"]["run_count"], 0)
        self.assertTrue(log.exists())
        self.assertFalse(self.project.control()["paused"])
        applied = prune(self.project, apply=True)
        self.assertTrue(applied["applied"])
        self.assertFalse(log.exists())
        self.assertFalse(cache.exists())
        self.assertFalse((directory / "input").exists())
        self.assertTrue((directory / "result.json").exists())
        self.assertTrue(all(path.read_bytes() == b"{" for path in corrupt_paths))

    def test_strict_age_boundary_started_clock_and_unknown_envelopes(self):
        old, boundary, younger = self.run_record(31 + 1 / DAY), self.run_record(31), self.run_record(30)
        kept = [boundary, younger, self.run_record(-1), self.run_record(40, terminal=None),
                self.run_record(status="unknown")]
        for fields in ({"started_at": "bad"}, {"started_at": "2000-01-01T00:00:00"},
                       {"run": "wrong"}, {"snapshot": "bad"}):
            run = self.run_record()
            self.change(run, "started.json", **fields)
            kept.append(run)
        bad_result = self.run_record()
        self.change(bad_result, "result.json", snapshot=None)
        kept.append(bad_result)
        run = self.run_record()
        self.change(run, "result.json", finish={"outcome": "unknown"})
        kept.append(run)
        future = self.run_record()
        self.change(future, "result.json", finished_at=stamp(CLOCK + DAY))
        kept.append(future)
        self.assertEqual(self.candidates(), {old})
        prune(self.project, apply=True)
        self.assertFalse((self.project.root / "runs" / old).exists())
        self.assertTrue(all((self.project.root / "runs" / run).is_dir() for run in kept))

    def test_known_error_and_bounded_results_are_terminal(self):
        error = self.run_record(terminal="error.json", status="bounded")
        bounded = self.run_record(status="bounded")
        self.change(bounded, "result.json", error="deadline")
        self.assertEqual(self.candidates(), {error, bounded})

    def test_source_error_envelopes_and_unknown_statuses(self):
        parent = self.run_record(age=1)
        ordinary = self.run_record(terminal="error.json", status="interrupted")
        self.change(ordinary, "error.json", publication="not_published", durability="confirmed")
        bounded = self.run_record(status="bounded")
        self.change(bounded, "result.json", error="deadline", bound="deadline",
                    publication="not_published", code_digest="a" * 64, model={}, activity={})
        child = self.run_record(parent=parent, terminal="error.json", status="bounded")
        self.change(child, "error.json", profile="primary", snapshot="a" * 64)
        # Child failure is valid, but the young parent keeps its component.
        self.assertEqual(self.candidates(), {ordinary, bounded})
        for name, status in (("error.json", "completed"), ("result.json", "failed"),
                             ("result.json", "deferred"), ("result.json", "running")):
            bad = self.run_record(terminal=name, status=status)
            self.assertNotIn(bad, self.candidates())

    def test_conflicting_terminal_envelopes_keep_runs(self):
        parent = self.run_record()
        free = self.run_record()
        kept = [parent]
        for terminal in ("result.json", "consultation.json", "delegation.json"):
            with self.subTest(terminal=terminal):
                run = self.run_record(parent=parent if terminal != "result.json" else None,
                                      terminal=terminal)
                error = {"run": run, "role": "worker", "status": "interrupted",
                         "finished_at": stamp(CLOCK - 40 * DAY + 2), "error": "rewrite failed"}
                write_json(self.project.root / "runs" / run / "error.json", error)
                kept.append(run)
        multiple = self.run_record(parent=parent, terminal="consultation.json")
        write_json(self.project.root / "runs" / multiple / "delegation.json",
                   self.read(multiple, "consultation.json"))
        kept.append(multiple)
        self.assertEqual(self.candidates(), {free})
        prune(self.project, apply=True)
        self.assertFalse((self.project.root / "runs" / free).exists())
        self.assertTrue(all((self.project.root / "runs" / run).is_dir() for run in kept))

    def test_runtime_interrupted_result_must_merge_the_same_error(self):
        valid = self.run_record()
        error = {"run": valid, "role": "worker", "status": "interrupted",
                 "finished_at": stamp(CLOCK - 40 * DAY + 2), "error": "postprocessing failed",
                 "publication": "not_published", "durability": "confirmed"}
        write_json(self.project.root / "runs" / valid / "error.json", error)
        self.change(valid, "result.json", **error)
        self.assertEqual(self.candidates(), {valid})
        for fields in ({"error": "different"}, {"finished_at": stamp(CLOCK - 40 * DAY + 3)},
                       {"publication": "published"}, {"status": "bounded"}):
            with self.subTest(fields=fields):
                self.change(valid, "result.json", **{**error, **fields})
                self.assertEqual(self.candidates(), set())
        # Two bounded envelopes are not a runtime error/result rewrite either.
        self.change(valid, "error.json", status="bounded")
        self.assertEqual(self.candidates(), set())
        self.change(valid, "error.json", **error)
        self.change(valid, "result.json", **error)
        prune(self.project, apply=True)
        self.assertFalse((self.project.root / "runs" / valid).exists())

    def test_runtime_prepared_and_final_result_envelopes(self):
        # Engine.run's started/result dicts, including the durable prepared
        # write before its final completed/waiting rewrite. Synthetic payloads
        # are deliberately opaque; retention validates the lifecycle, not logs.
        run = uuid.uuid4().hex
        directory = self.project.root / "runs" / run
        started = {"run": run, "role": "worker", "snapshot": "a" * 64,
                   "started_at": stamp(CLOCK - 40 * DAY), "admission_elapsed_seconds": 0.0,
                   "admission": {}, "decision_events": 0, "decision_receipt_diagnostics": 0,
                   "work_item_events": 0, "wait_events": 0, "obligation_events": 0,
                   "ci_failures": 0, "config_sha256": "b" * 64, "policy_sha256": "c" * 64}
        finish = {"outcome": "continue", "summary": "Saved work", "state": "Next step",
                  "wait_seconds": 60, "recorded_at": stamp(CLOCK - 40 * DAY + 1)}
        result = {"run": run, "role": "worker", "status": "prepared",
                  "finished_at": stamp(CLOCK - 40 * DAY + 1), "finish": finish,
                  "model": {}, "snapshot": "d" * 64, "activity": {}, "ci_requests": {}}
        write_json(directory / "started.json", started)
        for status in ("prepared", "completed", "waiting"):
            with self.subTest(status=status):
                write_json(directory / "result.json", {**result, "status": status})
                self.assertEqual(self.candidates(), set() if status == "prepared" else {run})
        for fields in ({"finish": None}, {"finish": {**finish, "outcome": "unknown"}},
                       {"snapshot": None}, {"role": "editor"}):
            with self.subTest(fields=fields):
                write_json(directory / "result.json", {**result, "status": "completed", **fields})
                self.assertEqual(self.candidates(), set())
        write_json(directory / "result.json", result)
        prune(self.project, apply=True)
        self.assertEqual(self.read(run, "result.json"), result)
        write_json(directory / "result.json", {**result, "status": "completed"})
        prune(self.project, apply=True)
        self.assertFalse(directory.exists())

    def test_current_inbox_evidence_active_and_selector_pins(self):
        a, b, c, d, free = [self.run_record() for _ in range(5)]
        write_json(self.project.root / "inbox" / "sample.json", {"run": a, "evidence": [{"run": b}]})
        write_json(self.project.root / "active" / "worker.json", {"run": c})
        self.change(d, "result.json", status="waiting", finish=None, selection={"profile": None})
        write_json(self.project.root / "runs" / d / "selection.json", {"profile": None})
        write_json(self.project.root / "selection" / "worker-wait.json",
                   {"version": 1, "run": d, "fingerprint": "b" * 64})
        self.assertEqual(self.candidates(), {free})
        (self.project.root / "runs" / d / "selection.json").unlink()
        self.assertEqual(self.candidates(), {free, d})

    def test_prose_provider_names_and_permanent_audit_are_not_pins(self):
        free = self.run_record()
        write_json(self.project.root / "inbox" / "sample.json",
                   {"run": None, "body": free, "origin": {"run": free},
                    "evidence": [{"text": free}]})
        write_json(self.project.root / "runs" / free / "vcs-status.json", {"run": free})
        write_json(self.project.root / "maintenance" / "past.json", {"run": free})
        self.assertEqual(self.candidates(), {free})

    def test_nested_links_and_young_or_incomplete_member_keep_component(self):
        a = self.run_record()
        b = self.run_record(parent=a, terminal="consultation.json")
        c = self.run_record(parent=b, terminal="delegation.json", age=1)
        free = self.run_record()
        self.assertEqual(self.candidates(), {free})
        self.change(c, "started.json", started_at=stamp(CLOCK - 40 * DAY))
        self.change(c, "delegation.json", finished_at=stamp(CLOCK - 39 * DAY))
        self.assertEqual(self.candidates(), {a, b, c, free})
        (self.project.root / "runs" / c / "delegation.json").unlink()
        self.assertEqual(self.candidates(), {free})

    def test_parent_side_links_and_pin_anywhere_keep_whole_component(self):
        a, b, c = [self.run_record() for _ in range(3)]
        write_json(self.project.root / "runs" / a / "consultations" / "one.json", {"answers": [{"run": b}]})
        write_json(self.project.root / "runs" / b / "delegations" / "one.json", {"run": c})
        write_json(self.project.root / "active" / "worker.json", {"run": c})
        self.assertEqual(self.candidates(), set())
        (self.project.root / "active" / "worker.json").unlink()
        self.assertEqual(runs.plan(self.project)["components"], [sorted([a, b, c])])
        prune(self.project, apply=True)
        self.assertTrue(all(not (self.project.root / "runs" / r).exists() for r in (a, b, c)))

    def block(self, wait):
        item = self.project.work_items.create(idempotency_key=uuid.uuid4().hex, title="Wait for evidence", actor="worker")
        return self.project.work_items.transition(item["record"]["work_item"]["id"], expected_rev=1, status="blocked",
                                                 actor="worker", wait=wait)

    def test_blocked_insight_wait_pins_exact_target_decision(self):
        pinned, free = self.run_record(), self.run_record()
        proposal = self.project.insights.submit(source="worker", title="Target", body="Result", base_snapshot=None)
        self.project.insights.decide(proposal["id"], "reject", "reviewed", "", pinned, expected_rev=1)
        self.block({"kind": "insight_decided", "insight": proposal["id"]})
        self.assertEqual(self.candidates(), {free})
        self.assertTrue((self.project.root / "decisions" / (proposal["id"] + ".json")).exists())

    def test_blocked_dependency_pins_exact_historical_revision_not_new_head(self):
        pinned, new = self.run_record(), self.run_record()
        proposal = self.project.insights.submit(source="worker", title="Target", body="Result", base_snapshot=None)
        self.project.insights.decide(proposal["id"], "reject", "reviewed", "", pinned, expected_rev=1)
        self.block({"kind": "dependency", "recipient": "reviewer",
                    "requires": {"insight": proposal["id"], "rev": 1, "action": "reject"}})
        self.project.insights.revise(proposal["id"], source="worker", title="Moved", body="Changed", base_snapshot=None, expected_rev=1)
        self.project.insights.decide(proposal["id"], "reject", "new review", "", new, expected_rev=2)
        self.assertEqual(self.candidates(), {new})

    def test_operator_sentinel_pins_nothing_but_history_still_binds(self):
        free = self.run_record()
        proposal = self.project.insights.submit(source="worker", title="Target", body="Result", base_snapshot=None)
        self.project.insights.decide(proposal["id"], "reject", "reviewed", "", "operator", expected_rev=1)
        self.block({"kind": "insight_decided", "insight": proposal["id"]})
        # No local run named "operator" exists, yet the scan stays complete.
        self.assertEqual(self.candidates(), {free})
        second = self.project.insights.submit(source="worker", title="Other", body="Result", base_snapshot=None)
        self.project.insights.decide(second["id"], "reject", "reviewed", "", "operator", expected_rev=1)
        self.block({"kind": "dependency", "recipient": "reviewer",
                    "requires": {"insight": second["id"], "rev": 1, "action": "reject"}})
        self.assertEqual(self.candidates(), {free})
        # Exact historical evidence is still required for the dependency.
        history = self.project.root / "decision-history"
        saved = [(path, path.read_bytes()) for path in sorted(history.glob("*.json"))]
        for path, _ in saved:
            path.unlink()
        self.assertEqual(prune(self.project)["run_retention"]["reason"],
                         "Missing exact dependency decision evidence")
        self.assert_refused_without_deletion([free])
        for path, data in saved:
            path.write_bytes(data)
        self.assertEqual(self.candidates(), {free})

    def test_near_miss_operator_and_arbitrary_run_refuse_apply(self):
        free = self.run_record()
        for invalid in ("Operator", "not-a-run"):
            with self.subTest(invalid=invalid):
                proposal = self.project.insights.submit(source="worker", title="Target", body="Result",
                                                         base_snapshot=None)
                self.project.insights.decide(proposal["id"], "reject", "reviewed", "", invalid, expected_rev=1)
                self.block({"kind": "insight_decided", "insight": proposal["id"]})
                self.assert_refused_without_deletion([free])

    def test_insight_wait_without_current_decision_is_valid(self):
        free = self.run_record()
        proposal = self.project.insights.submit(source="worker", title="Target", body="Result", base_snapshot=None)
        self.block({"kind": "insight_decided", "insight": proposal["id"]})
        self.assertEqual(self.candidates(), {free})

    def test_exact_dependency_requires_one_historical_decision(self):
        pinned, free = self.run_record(), self.run_record()
        proposal = self.project.insights.submit(source="worker", title="Target", body="Result", base_snapshot=None)
        self.project.insights.decide(proposal["id"], "reject", "reviewed", "", pinned, expected_rev=1)
        self.block({"kind": "dependency", "recipient": "reviewer",
                    "requires": {"insight": proposal["id"], "rev": 1, "action": "reject"}})
        self.assertEqual(self.candidates(), {free})
        history = self.project.root / "decision-history"
        path, = history.glob("*.json")
        original = path.read_bytes()
        path.unlink()
        # Even a matching current decision cannot replace the exact history.
        self.assertEqual(prune(self.project)["run_retention"]["reason"],
                         "Missing exact dependency decision evidence")
        self.assert_refused_without_deletion([pinned, free])
        path.write_bytes(original)
        event = json.loads(original)
        # Legacy events are valid without a decision key, but cannot duplicate
        # the exact historical binding that a live dependency requires.
        event.pop("decision_key")
        duplicate = history / (uuid.uuid4().hex + ".json")
        write_json(duplicate, event)
        self.assertEqual(prune(self.project)["run_retention"]["reason"],
                         "Duplicate exact dependency decision evidence")
        self.assert_refused_without_deletion([pinned, free])
        duplicate.unlink()
        self.assertEqual(self.candidates(), {free})

    def test_bad_reference_refuses_all_apply_including_old_projection_prune(self):
        old = self.run_record()
        input_dir = self.project.root / "runs" / old / "input"
        input_dir.mkdir()
        path = self.project.root / "inbox" / "bad.json"
        path.parent.mkdir(exist_ok=True)
        for body in (b"{", b"null", b'{"evidence": [null]}', b'{"run": "../escape"}',
                     b" " * (runs.RECORD_BYTES + 1)):
            with self.subTest(body=body[:30]):
                path.write_bytes(body)
                self.assertFalse(prune(self.project)["run_retention"]["complete"])
                with self.assertRaises(Denied):
                    prune(self.project, apply=True)
                self.assertTrue(input_dir.exists())
                self.assertTrue((self.project.root / "runs" / old).exists())

    def test_symlink_and_hardlink_reference_fail_closed(self):
        old = self.run_record()
        target = self.root / "reference.json"
        write_json(target, {"run": old})
        path = self.project.root / "active" / "worker.json"
        path.parent.mkdir(exist_ok=True)
        for kind in ("symlink", "hardlink"):
            try:
                os.symlink(target, path) if kind == "symlink" else os.link(target, path)
            except OSError:
                continue  # Windows can require an operator symlink privilege.
            self.assertFalse(prune(self.project)["run_retention"]["complete"])
            with self.assertRaises(Denied):
                prune(self.project, apply=True)
            path.unlink()

    def test_bad_nested_link_is_incomplete_and_systemic_io_propagates(self):
        old = self.run_record()
        write_json(self.project.root / "runs" / old / "consultations" / "one.json", {"answers": [{"run": None}]})
        self.assertFalse(prune(self.project)["run_retention"]["complete"])
        with self.assertRaises(Denied):
            prune(self.project, apply=True)
        with mock.patch.object(runs.Scan, "record", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                prune(self.project, apply=True)

    def test_entry_byte_and_output_bounds(self):
        names = {self.run_record() for _ in range(runs.PREVIEW_ROWS + 1)}
        preview = self.preview()
        self.assertEqual(preview["run_count"], len(names))
        self.assertEqual(len(preview["runs"]), runs.PREVIEW_ROWS)
        self.assertTrue(preview["truncated"])
        for bound in ("MAX_ENTRIES", "MAX_BYTES"):
            with mock.patch.object(runs, bound, 1):
                self.assertFalse(prune(self.project)["run_retention"]["complete"])
                with self.assertRaises(Denied):
                    prune(self.project, apply=True)
        self.assertEqual(len(list((self.project.root / "runs").iterdir())), len(names))

    def test_dry_run_is_immutable_and_apply_recomputes(self):
        old = self.run_record()
        before = {p.relative_to(self.project.root): p.read_bytes() for p in self.project.root.rglob("*")
                  if p.is_file() and "locks" not in p.parts}
        self.assertEqual(self.candidates(), {old})
        after = {p.relative_to(self.project.root): p.read_bytes() for p in self.project.root.rglob("*")
                 if p.is_file() and "locks" not in p.parts}
        self.assertEqual(before, after)
        write_json(self.project.root / "active" / "worker.json", {"run": old})
        self.assertEqual(prune(self.project, apply=True)["run_retention"]["run_count"], 0)
        self.assertTrue((self.project.root / "runs" / old).exists())

    def test_lock_order_busy_refuses_without_pause_and_other_maintenance_stays_paused(self):
        self.run_record()
        for name in ("workspace.lock", "run-worker.lock", "run-retired-role.lock", "editor-ingest.lock", "insights.lock"):
            with self.subTest(name=name):
                with lock(self.project.root / "locks" / name):
                    with self.assertRaises(Busy):
                        prune(self.project, apply=True)
        self.assertFalse(self.project.control()["paused"])
        self.assertTrue(prune(self.project, apply=True)["applied"])
        with self.assertRaises(Denied):
            backup(self.project, self.root / "backup.tgz")
        with self.assertRaises(Denied):
            reclaim(self.project, apply=True)

    def test_interrupted_staging_rolls_back_whole_component_before_new_scan(self):
        a, b = self.run_record(), self.run_record()
        write_json(self.project.root / "runs" / a / "delegations" / "one.json", {"run": b})
        rename = type(self.project.root).rename
        moved = []
        def interrupt(path, target):
            if path.parent == self.project.root / "runs":
                if moved:
                    raise OSError("interrupted staging")
                moved.append(path.name)
            return rename(path, target)
        with mock.patch("pathlib.Path.rename", interrupt):
            with self.assertRaisesRegex(OSError, "interrupted staging"):
                prune(self.project, apply=True)
        self.assertEqual(len(runs.quarantines(self.project)), 1)
        self.assertFalse(prune(self.project)["run_retention"]["complete"])
        # Restoration can precede a failed scan, but even quarantine intent
        # must remain until that scan is complete (no apply deletion at all).
        corrupt = self.project.root / "inbox" / "bad.json"
        corrupt.parent.mkdir(exist_ok=True)
        corrupt.write_bytes(b"{")
        with self.assertRaises(Denied):
            prune(self.project, apply=True)
        self.assertEqual(len(runs.quarantines(self.project)), 1)
        self.assertTrue(all((self.project.root / "runs" / r).exists() for r in (a, b)))
        corrupt.unlink()
        write_json(self.project.root / "active" / "worker.json", {"run": b})
        prune(self.project, apply=True)
        self.assertEqual(runs.quarantines(self.project), [])
        self.assertTrue(all((self.project.root / "runs" / r).exists() for r in (a, b)))

    def test_partial_quarantine_deletion_resumes_without_claiming_success(self):
        old = self.run_record()
        original = runs.shutil.rmtree
        def partial(path, *args, **kwargs):
            if path.name == "runs" and path.parent.name.startswith("retired-"):
                (path / old / "result.json").unlink()
                raise OSError("partial removal")
            return original(path, *args, **kwargs)
        with mock.patch.object(runs.shutil, "rmtree", side_effect=partial):
            with self.assertRaisesRegex(OSError, "partial removal"):
                prune(self.project, apply=True)
        self.assertFalse((self.project.root / "runs" / old).exists())
        self.assertEqual(self.preview()["quarantine_count"], 1)
        self.assertFalse(list((self.project.root / "maintenance").glob("prune-2*.json")))
        prune(self.project, apply=True)
        self.assertEqual(runs.quarantines(self.project), [])
        self.assertEqual(len(list((self.project.root / "maintenance").glob("retired-*.json"))), 1)

    def test_empty_staging_and_completion_unlink_interruptions(self):
        base = self.project.root / "maintenance" / "run-quarantine"
        empty = base / ("staging-" + uuid.uuid4().hex)
        (empty / "runs").mkdir(parents=True)
        self.assertFalse(prune(self.project)["run_retention"]["complete"])
        prune(self.project, apply=True)
        self.assertFalse(empty.exists())
        self.run_record()
        unlink = type(base).unlink
        def interrupt(path, *args, **kwargs):
            result = unlink(path, *args, **kwargs)
            if path.name == "manifest.json" and path.parent.name.startswith("retired-"):
                raise OSError("after completion unlink")
            return result
        with mock.patch("pathlib.Path.unlink", interrupt):
            with self.assertRaisesRegex(OSError, "after completion unlink"):
                prune(self.project, apply=True)
        self.assertEqual(self.preview()["quarantine_count"], 1)
        prune(self.project, apply=True)
        self.assertEqual(runs.quarantines(self.project), [])

    def test_corrupt_work_item_chain_and_symlink_reference_directory_refuse(self):
        self.run_record()
        blocked = self.block({"kind": "deadline", "at": "2040-01-01T00:00:00+00:00"})
        path = self.project.root / "work-items" / blocked["id"] / "2.json"
        saved = path.read_bytes()
        path.write_bytes(b"{")
        self.assertFalse(prune(self.project)["run_retention"]["complete"])
        with self.assertRaises(Denied):
            prune(self.project, apply=True)
        path.write_bytes(saved)
        active = self.project.root / "active"
        if active.exists():
            active.rmdir()
        external = self.root / "external-active"
        external.mkdir()
        try:
            active.symlink_to(external, target_is_directory=True)
        except OSError:
            return  # Windows may require an operator symlink privilege.
        self.assertFalse(prune(self.project)["run_retention"]["complete"])
        with self.assertRaises(Denied):
            prune(self.project, apply=True)

    def assert_refused_without_deletion(self, expected):
        self.assertFalse(prune(self.project)["run_retention"]["complete"])
        with self.assertRaises(Denied):
            prune(self.project, apply=True)
        self.assertTrue(all((self.project.root / "runs" / r).is_dir() for r in expected))

    def test_missing_linked_member_refuses_unrelated_deletion(self):
        parent, free = self.run_record(), self.run_record()
        missing = uuid.uuid4().hex
        link = self.project.root / "runs" / parent / "delegations" / "one.json"
        write_json(link, {"run": missing})
        self.assert_refused_without_deletion([parent, free])
        link.unlink()
        self.change(parent, "started.json", parent_run=missing)
        self.assert_refused_without_deletion([parent, free])

    def test_invalid_child_completion_keeps_existing_component(self):
        parent = self.run_record()
        malformed = self.run_record(parent=parent, terminal="consultation.json")
        self.change(malformed, "consultation.json", status="interrupted", error="not a consultation schema")
        free = self.run_record()
        self.assertEqual(self.candidates(), {free})

    def test_missing_typed_pins_refuse_unrelated_deletion(self):
        free = self.run_record()
        missing = uuid.uuid4().hex
        for path, record in (
                ("active/worker.json", {"run": missing}),
                ("inbox/pin.json", {"run": missing}),
                ("inbox/pin.json", {"evidence": [{"run": missing}]}),
                ("selection/worker-wait.json", {"version": 1, "run": missing, "fingerprint": "b" * 64})):
            with self.subTest(path=path, record=record):
                target = self.project.root / path
                write_json(target, record)
                self.assert_refused_without_deletion([free])
                target.unlink()
        proposal = self.project.insights.submit(source="worker", title="Target", body="Result", base_snapshot=None)
        self.project.insights.decide(proposal["id"], "reject", "reviewed", "", missing, expected_rev=1)
        self.block({"kind": "insight_decided", "insight": proposal["id"]})
        self.assert_refused_without_deletion([free])

    def test_physical_run_bound_is_separate_from_entry_bound(self):
        from mizu.usage import MAX_RUNS_SCANNED
        self.assertEqual(runs.MAX_RUNS_SCANNED, MAX_RUNS_SCANNED)
        a, b = self.run_record(), self.run_record()
        with mock.patch.object(runs, "MAX_RUNS_SCANNED", 2):
            self.assertEqual(self.candidates(), {a, b})
            c = self.run_record()
            self.assert_refused_without_deletion([a, b, c])

    def test_nested_payload_links_are_never_traversed_or_deleted(self):
        old, free = self.run_record(), self.run_record()
        target = self.root / "external"
        target.mkdir()
        sentinel = target / "sentinel"
        sentinel.write_bytes(b"keep")
        nested = self.project.root / "runs" / old / "input" / "nested"
        nested.mkdir(parents=True)
        link = nested / "link"
        for kind in ("directory", "file", "dangling", "hardlink"):
            with self.subTest(kind=kind):
                try:
                    if kind == "directory":
                        link.symlink_to(target, target_is_directory=True)
                    elif kind == "hardlink":
                        os.link(sentinel, link)
                    else:
                        link.symlink_to(sentinel if kind == "file" else target / "missing")
                except OSError:
                    continue  # Windows can require an operator symlink privilege.
                original = runs.os.scandir
                def guarded(path):
                    self.assertNotEqual(path, link, "must reject the link before descent")
                    return original(path)
                with mock.patch.object(runs.os, "scandir", side_effect=guarded):
                    self.assert_refused_without_deletion([old, free])
                self.assertEqual(sentinel.read_bytes(), b"keep")
                link.unlink()

    def test_quarantine_payload_is_preflighted_before_deletion_or_rollback(self):
        old = self.run_record()
        with mock.patch.object(runs.shutil, "rmtree", side_effect=OSError("interrupted deletion")):
            with self.assertRaisesRegex(OSError, "interrupted deletion"):
                prune(self.project, apply=True)
        leftover, _ = runs.quarantines(self.project)[0]
        target = self.root / "external"
        target.write_bytes(b"keep")
        nested = leftover / "runs" / old / "opaque" / "nested"
        nested.mkdir(parents=True)
        os.link(target, nested / "uninspected")
        free = self.run_record()
        # Zero disables only new expiry, never validation of existing quarantine.
        self.policy(0)
        for prefix in ("retired", "staging"):
            with self.subTest(prefix=prefix):
                if prefix == "staging":
                    leftover = leftover.rename(leftover.with_name(leftover.name.replace("retired-", "staging-")))
                with mock.patch.object(runs.shutil, "rmtree") as remove:
                    with mock.patch("pathlib.Path.rename") as rename:
                        with self.assertRaises(Denied):
                            prune(self.project, apply=True)
                    rename.assert_not_called()
                remove.assert_not_called()
                self.assertTrue((self.project.root / "runs" / free).exists())
                self.assertTrue((leftover / "runs" / old / "result.json").exists())
                self.assertFalse((self.project.root / "runs" / old).exists())
                self.assertEqual(target.read_bytes(), b"keep")
        (leftover / "runs" / old / "opaque" / "nested" / "uninspected").unlink()
        prune(self.project, apply=True)
        self.assertFalse(leftover.exists())
        self.assertTrue((self.project.root / "runs" / old / "result.json").exists())

    def test_large_opaque_payload_does_not_consume_metadata_byte_budget(self):
        old, free = self.run_record(), self.run_record()
        directory = self.project.root / "runs" / old / "opaque"
        directory.mkdir()
        payload = directory / "output"
        # Sparse: greater than the metadata budget without reading/allocating it.
        with payload.open("wb") as stream:
            stream.truncate(runs.MAX_BYTES + 1)
        self.assertEqual(self.candidates(), {old, free})
        # Both new expiry and committed quarantine retry accept large payloads.
        with mock.patch.object(runs.shutil, "rmtree", side_effect=OSError("interrupted deletion")):
            with self.assertRaisesRegex(OSError, "interrupted deletion"):
                prune(self.project, apply=True)
        self.policy(0)
        prune(self.project, apply=True)
        self.assertFalse(payload.exists())
        self.assertEqual(runs.quarantines(self.project), [])

    def test_payload_depth_is_bounded_before_any_deletion(self):
        old, free = self.run_record(), self.run_record()
        directory = self.project.root / "runs" / old
        for _ in range(runs.MAX_DEPTH + 1):
            directory = directory / "nested"
            directory.mkdir()
        self.assert_refused_without_deletion([old, free])

    def test_success_audit_binds_full_component_plan_and_permanent_ledgers_stay(self):
        a, b = self.run_record(), self.run_record()
        write_json(self.project.root / "runs" / a / "delegations" / "one.json", {"run": b})
        proposal = self.project.insights.submit(source="worker", title="Historical", body="Completed", base_snapshot=None)
        # An unrelated historical decision run is not a live pin.
        self.project.insights.decide(proposal["id"], "reject", "finished", "", a, expected_rev=1)
        before = {p: p.read_bytes() for name in ("decisions", "decision-history", "work-items", "snapshots", "objects")
                  for p in (self.project.root / name).rglob("*") if p.is_file()}
        dry = self.preview()
        result = prune(self.project, apply=True)
        view = result["run_retention"]
        intent = json.loads((self.project.root / view["intent"]).read_bytes())
        self.assertEqual(intent["components"], [sorted([a, b])])
        self.assertEqual(view["plan_sha256"], dry["plan_sha256"])
        audit = json.loads((self.project.root / result["audit"]).read_bytes())
        self.assertEqual(audit["run_retention"]["run_count"], 2)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(prune(self.project, apply=True)["run_retention"]["run_count"], 0)
