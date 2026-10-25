"""Producer admission: per-source and open-work congestion holds.

Only configured non-writer project roles holding ``submit_insight`` are gated, before any
model or run-slot use. Undecided and deferred live revisions count; resolved
revisions and archive-only history do not. Held attempts claim no run
directory, slot, or lock and acknowledge no event or cursor; release happens
only through existing schedules or subscriptions.
"""
import contextlib
import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.config import load
from mizu.errors import Busy, ConfigError, Denied, LimitExceeded
from mizu.fs import lock, safe_read
from mizu.insights import MAX_DECISION_EVENT_BYTES
from mizu.smoke_refs import record_lock
from mizu.project import Project
from mizu.runtime import Engine, producer_hold, should_run
from mizu.scheduling import ProducerFacts, evaluate_producer


def _submit(fixture, source, title):
    return fixture.project.insights.submit(
        source=source, title=title, body="Input",
        base_snapshot=fixture.project.snapshots.get()["id"])["id"]


def _decide(fixture, insight_id, action, reason="Handled", revisit=""):
    return fixture.project.insights.decide(
        insight_id, action, reason, revisit, "run", expected_rev=1)


def _runs(project):
    root = project.root / "runs"
    if not root.exists():
        return set()
    return {path.name for path in root.iterdir()}


class ProducerConfigTests(Fixture):
    def test_default_wires_through_project(self):
        self.assertEqual(self.config.limits.producer_inputs_per_source, 20)
        self.assertEqual(self.project.insights.producer_inputs_per_source, 20)
        self.assertFalse(self.config.roles["reviewer"].held_at_capacity)

    def test_role_open_work_hold_is_explicit_opt_in(self):
        text = self.file.read_text().replace(
            "[roles.searcher]\n", "[roles.searcher]\nheld_at_capacity = true\n", 1)
        self.file.write_text(text)
        config = load(self.file)
        self.assertTrue(config.roles["searcher"].held_at_capacity)
        self.assertFalse(config.roles["reviewer"].held_at_capacity)

    def test_role_open_work_hold_rejects_invalid_or_unknown_values(self):
        original = self.file.read_text()
        for value in ("1", '"true"', "0.5", '"yes"'):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    "[roles.searcher]\n", f"[roles.searcher]\nheld_at_capacity = {value}\n", 1))
                with self.assertRaises(ConfigError):
                    load(self.file)
        self.file.write_text(original.replace(
            "[roles.searcher]\n",
            "[roles.searcher]\nheld_at_capacity_typo = true\n", 1))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_missing_key_keeps_default(self):
        text = self.file.read_text()
        self.assertIn("producer_inputs_per_source = 20", text)
        self.file.write_text(text.replace(
            "producer_inputs_per_source = 20\n", ""))
        self.assertEqual(load(self.file).limits.producer_inputs_per_source, 20)

    def test_explicit_values_and_zero_disables(self):
        text = self.file.read_text()
        self.assertIn("open_work_items = 100", text)
        self.assertIn("producer_inputs_per_source = 20", text)
        current = 20
        for value in (0, 1, 20, 1073741824):
            self.file.write_text(self.file.read_text().replace(
                f"producer_inputs_per_source = {current}",
                f"producer_inputs_per_source = {value}"))
            current = value
            config = load(self.file)
            self.assertEqual(config.limits.producer_inputs_per_source, value)
            self.assertEqual(
                Project(config, "sample").insights.producer_inputs_per_source, value)

    def test_invalid_values_refused(self):
        text = self.file.read_text()
        self.assertIn("producer_inputs_per_source = 20", text)
        for value in ("-1", "1073741825", "true", "0.5", '"2"'):
            self.file.write_text(self.file.read_text().replace(
                "producer_inputs_per_source = 20",
                f"producer_inputs_per_source = {value}"))
            with self.assertRaises(ConfigError):
                load(self.file)


class ProducerCountsTests(Fixture):
    def test_undecided_and_defer_count_resolved_excluded(self):
        self.project.insights.producer_inputs_per_source = 100
        first = _submit(self, "searcher", "One")
        second = _submit(self, "searcher", "Two")
        other = _submit(self, "reviewer", "Other")
        self.assertEqual(self.project.insights.unresolved_count("searcher"), 2)
        self.assertEqual(self.project.insights.unresolved_count("reviewer"), 1)
        self.assertEqual(self.project.insights.unresolved_count("worker"), 0)
        self.assertEqual(self.project.insights.unresolved_counts(["searcher", "reviewer"]),
                         {"searcher": 2, "reviewer": 1})
        _decide(self, first, "defer", revisit="Later")
        self.assertEqual(self.project.insights.unresolved_count("searcher"), 2)
        _decide(self, second, "reject")
        self.assertEqual(self.project.insights.unresolved_count("searcher"), 1)
        self.assertEqual(self.project.insights.unresolved_counts(["searcher"]),
                         {"searcher": 1})
        self.assertEqual(other is not None, True)

    def test_archive_only_history_excluded(self):
        self.project.insights.producer_inputs_per_source = 100
        insight_id = _submit(self, "searcher", "Old")
        _decide(self, insight_id, "reject")
        decisions_path = self.project.root / "decisions" / f"{insight_id}.json"
        record = json.loads(decisions_path.read_text())
        record["created_at"] = "2020-01-01T00:00:00+00:00"
        decisions_path.write_text(json.dumps(record))
        removed = self.project.insights.gc_decided()
        self.assertEqual(removed, 1)
        self.assertEqual(self.project.insights.unresolved_count("searcher"), 0)
        archived = self.project.insights.list_page(scope="archive", limit=100)["items"]
        self.assertTrue(any(item["id"] == insight_id for item in archived))

    def test_count_serializes_with_concurrent_submission(self):
        _submit(self, "searcher", "Before")
        scan_started = threading.Event()
        contention_checked = threading.Event()
        reader = threading.current_thread()

        @contextlib.contextmanager
        def observed_lock(root):
            if threading.current_thread() is not reader:
                try:
                    # Prove contention at the actual Insights lock without
                    # depending on a short sleep or host scheduling speed.
                    with self.assertRaises(Busy):
                        with lock(root / "locks" / "insights.lock", blocking=False):
                            pass
                finally:
                    contention_checked.set()
            with record_lock(root):
                yield

        def read_during_submission(*args):
            scan_started.set()
            self.assertTrue(contention_checked.wait(60))
            return safe_read(*args)

        def submit_during_scan():
            self.assertTrue(scan_started.wait(60))
            return _submit(self, "searcher", "After")

        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch("mizu.insights.record_lock", observed_lock):
                with patch("mizu.insights.safe_read", read_during_submission):
                    future = pool.submit(submit_during_scan)
                    try:
                        self.assertEqual(self.project.insights.unresolved_counts(["searcher"]),
                                         {"searcher": 1})
                    finally:
                        scan_started.set()
                    future.result(timeout=60)
        self.assertEqual(self.project.insights.unresolved_count("searcher"), 2)

    def test_malformed_live_identity_source_and_revision_fail_closed(self):
        insight_id = _submit(self, "reviewer", "Other source")
        path = self.project.root / "inbox" / f"{insight_id}.json"
        original = json.loads(path.read_text())
        bad_rows = [None, [], {}, {**original, "id": "different"},
                    {**original, "source": None}]
        bad_rows.extend({**original, "rev": rev} for rev in (None, True, "1", 0, -1, 1.5))
        for row in bad_rows:
            with self.subTest(row=row):
                path.write_text(json.dumps(row))
                # A bad row cannot silently disappear even when its source
                # differs from the requested source (or cannot be identified).
                with self.assertRaises(Denied):
                    self.project.insights.unresolved_counts(["searcher"])
        path.write_text("{")
        with self.assertRaises(ValueError):
            self.project.insights.unresolved_count("searcher")

    def test_decision_action_revision_and_identity_are_strict(self):
        insight_id = _submit(self, "searcher", "One")
        _decide(self, insight_id, "reject")
        path = self.project.root / "decisions" / f"{insight_id}.json"
        original = json.loads(path.read_text())
        bad_rows = [None, [], {}, {**original, "id": "different"}]
        bad_rows.extend({**original, "action": action} for action in (None, "unknown", [], 1))
        bad_rows.append({key: value for key, value in original.items() if key != "action"})
        bad_rows.extend({**original, "rev": rev} for rev in (None, True, "1", 0, -1, 1.5))
        for row in bad_rows:
            with self.subTest(row=row):
                path.write_text(json.dumps(row))
                with self.assertRaises(Denied):
                    self.project.insights.unresolved_count("searcher")
        for action in ("accept", "modify", "reject", "defer", "withdraw"):
            path.write_text(json.dumps({**original, "action": action}))
            self.assertEqual(self.project.insights.unresolved_count("searcher"),
                             1 if action == "defer" else 0)
        # Legacy omission is revision 1; a valid stale decision cannot resolve
        # the current input. Strict admission does not change tolerant readers.
        path.write_text(json.dumps({key: value for key, value in original.items() if key != "rev"}))
        self.assertEqual(self.project.insights.unresolved_count("searcher"), 0)
        path.write_text(json.dumps({**original, "rev": 2}))
        self.assertEqual(self.project.insights.unresolved_count("searcher"), 1)

    def test_oversized_live_row_and_decision_fail_closed(self):
        insight_id = _submit(self, "searcher", "One")
        inbox = self.project.root / "inbox" / f"{insight_id}.json"
        original = inbox.read_bytes()
        inbox.write_bytes(b" " * (self.project.insights.maximum + 1))
        with self.assertRaises(LimitExceeded):
            self.project.insights.unresolved_count("searcher")
        inbox.write_bytes(original)
        _decide(self, insight_id, "reject")
        decision = self.project.root / "decisions" / inbox.name
        decision.write_bytes(b" " * (MAX_DECISION_EVENT_BYTES + 1))
        with self.assertRaises(LimitExceeded):
            self.project.insights.unresolved_count("searcher")

    def test_symlink_live_row_and_decision_fail_closed(self):
        insight_id = _submit(self, "searcher", "One")
        _decide(self, insight_id, "reject")
        for directory in ("inbox", "decisions"):
            with self.subTest(directory=directory):
                path = self.project.root / directory / f"{insight_id}.json"
                original = path.read_bytes()
                target = self.root / "retained-row"
                target.write_bytes(original)
                path.unlink()
                try:
                    try:
                        path.symlink_to(target)
                    except (OSError, NotImplementedError):
                        self.skipTest("Host cannot create symlinks")
                    with self.assertRaises(Denied):
                        self.project.insights.unresolved_count("searcher")
                finally:
                    path.unlink(missing_ok=True)
                    path.write_bytes(original)

    def test_vanished_live_row_does_not_undercount_other_source(self):
        insight_id = _submit(self, "reviewer", "Other source")
        path = self.project.root / "inbox" / f"{insight_id}.json"

        def vanish_then_read(*args):
            path.unlink()
            return safe_read(*args)

        with patch("mizu.insights.safe_read", vanish_then_read):
            with self.assertRaises(Denied):
                self.project.insights.unresolved_counts(["searcher"])


class ProducerEvaluatorTests(unittest.TestCase):
    def test_non_producer_never_held(self):
        self.assertFalse(evaluate_producer(ProducerFacts(
            is_producer=False, per_source_limit=1, open_work_limit=1,
            source_pending=10, open_count=10, load_ok=True)).held)

    def test_disabled_limits_never_hold(self):
        self.assertFalse(evaluate_producer(ProducerFacts(
            is_producer=True, per_source_limit=0, open_work_limit=0,
            source_pending=10, open_count=10, load_ok=True)).held)

    def test_each_cause_and_release(self):
        self.assertEqual(evaluate_producer(ProducerFacts(
            is_producer=True, per_source_limit=2, open_work_limit=100,
            source_pending=2, open_count=0, load_ok=True)).reasons,
            ("per_source_full",))
        self.assertFalse(evaluate_producer(ProducerFacts(
            is_producer=True, per_source_limit=100, open_work_limit=1,
            source_pending=0, open_count=1, load_ok=True)).held)
        self.assertEqual(evaluate_producer(ProducerFacts(
            is_producer=True, per_source_limit=100, open_work_limit=1,
            source_pending=0, open_count=1, load_ok=True,
            held_at_capacity=True)).reasons,
            ("open_work_full",))
        self.assertEqual(evaluate_producer(ProducerFacts(
            is_producer=True, per_source_limit=2, open_work_limit=1,
            source_pending=2, open_count=1, load_ok=True,
            held_at_capacity=True)).reasons,
            ("per_source_full", "open_work_full"))
        self.assertFalse(evaluate_producer(ProducerFacts(
            is_producer=True, per_source_limit=2, open_work_limit=1,
            source_pending=1, open_count=0, load_ok=True)).held)
        self.assertEqual(evaluate_producer(ProducerFacts(
            is_producer=True, per_source_limit=2, open_work_limit=1,
            source_pending=0, open_count=0, load_ok=False)).reasons,
            ("load_unavailable",))


class ProducerHoldTests(Fixture):
    def setUp(self):
        super().setUp()
        self.project.insights.producer_inputs_per_source = 2
        self.project.insights.open_work_items = 100

    def test_per_source_hold_and_release(self):
        self.assertFalse(self.config.roles["searcher"].held_at_capacity)
        first = _submit(self, "searcher", "One")
        _submit(self, "searcher", "Two")
        held = producer_hold(self.project, "searcher")
        self.assertIsNotNone(held)
        self.assertEqual(held["reason"], "per_source_full")
        self.assertEqual(held["source_pending"], 2)
        before = _runs(self.project)
        result = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "searcher")
        self.assertEqual(result["skip"]["skipped"], "producer-held")
        self.assertEqual(result["skip"]["reason"], "per_source_full")
        self.assertEqual(_runs(self.project), before)
        _decide(self, first, "reject")
        self.assertIsNone(producer_hold(self.project, "searcher"))
        result = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "searcher")
        self.assertNotIn("skip", result)
        self.assertIn("run", result)

    def test_open_work_hold_and_release(self):
        self.config.roles["searcher"] = dataclasses.replace(
            self.config.roles["searcher"], held_at_capacity=True)
        self.project.insights.open_work_items = 1
        item = self.project.work_items.create(
            idempotency_key="one", title="One", actor="worker")["record"]["work_item"]
        held = producer_hold(self.project, "searcher")
        self.assertIsNotNone(held)
        self.assertIn("open_work_full", held["reasons"])
        self.assertFalse(should_run(
            self.project, self.project.snapshots.get(), role_name="searcher"))
        before = _runs(self.project)
        result = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "searcher")
        self.assertEqual(result["skip"]["skipped"], "producer-held")
        self.assertIn("open_work_full", result["skip"]["reasons"])
        self.assertEqual(_runs(self.project), before)
        self.project.work_items.transition(
            item["id"], expected_rev=1, status="doing", actor="worker")
        self.project.work_items.transition(
            item["id"], expected_rev=2, status="dropped", actor="worker",
            reason="Merged")
        self.assertIsNone(producer_hold(self.project, "searcher"))
        self.assertTrue(should_run(
            self.project, self.project.snapshots.get(), role_name="searcher"))
        result = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "searcher")
        self.assertNotIn("skip", result)

    def test_default_reviewer_is_not_held_by_open_work_capacity(self):
        self.project.insights.open_work_items = 1
        self.project.work_items.create(
            idempotency_key="one", title="One", actor="worker")
        self.assertFalse(self.config.roles["reviewer"].held_at_capacity)
        self.assertIsNone(producer_hold(self.project, "reviewer"))
        self.assertEqual(self.project.status()["backpressure"]["held"], [])
        before = _runs(self.project)
        result = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "reviewer")
        self.assertNotEqual(result.get("skip", {}).get("skipped"), "producer-held")
        self.assertNotEqual(_runs(self.project), before)

    def test_zero_disables_both_causes(self):
        self.project.insights.producer_inputs_per_source = 0
        self.project.insights.open_work_items = 0
        _submit(self, "searcher", "One")
        _submit(self, "searcher", "Two")
        self.project.work_items.create(
            idempotency_key="one", title="One", actor="worker")
        self.assertIsNone(producer_hold(self.project, "searcher"))
        result = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "searcher")
        self.assertNotIn("skip", result)

    def test_non_producer_never_held(self):
        self.config.roles["searcher"] = dataclasses.replace(
            self.config.roles["searcher"], held_at_capacity=True)
        _submit(self, "searcher", "One")
        _submit(self, "searcher", "Two")
        self.project.work_items.create(
            idempotency_key="one", title="One", actor="worker")
        self.project.insights.open_work_items = 1
        self.assertIsNotNone(producer_hold(self.project, "searcher"))
        self.assertIsNone(producer_hold(self.project, "reporter"))
        result = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "reporter")
        self.assertNotIn("skip", result)
        self.assertIn("reporter", result.get("role", "reporter"))

    def test_operator_channel_never_held(self):
        _submit(self, "operator", "Ops")
        self.assertIsNone(producer_hold(self.project, "operator"))

    def test_writer_is_not_held_even_with_submit_capability(self):
        self.project.insights.producer_inputs_per_source = 1
        self.project.insights.open_work_items = 1
        _submit(self, "worker", "Writer question")
        self.project.work_items.create(
            idempotency_key="writer-full", title="Existing", actor="worker")
        writer = dataclasses.replace(
            self.config.roles["worker"],
            capabilities=tuple(self.config.roles["worker"].capabilities) + ("submit_insight",))
        self.config.roles["worker"] = writer
        self.assertIsNone(producer_hold(self.project, "worker", writer))
        view = self.project.backpressure()
        self.assertNotIn("worker", {row["source"] for row in view["producer_inputs"]})
        self.assertNotIn("worker", {row["source"] for row in view["held"]})

    def test_fail_closed_on_unreadable_load(self):
        _submit(self, "searcher", "One")
        with patch.object(self.project.insights, "unresolved_counts",
                          side_effect=OSError("injected")):
            held = producer_hold(self.project, "searcher")
            self.assertEqual(held["reason"], "load_unavailable")
            result = Engine(self.config, driver=ScriptDriver()).run(
                self.project, "searcher")
            self.assertEqual(result["skip"]["reason"], "load_unavailable")
        self.config.roles["searcher"] = dataclasses.replace(
            self.config.roles["searcher"], held_at_capacity=True)
        with patch.object(self.project.work_items, "count_open_readonly",
                          side_effect=OSError("injected")):
            held = producer_hold(self.project, "searcher")
            self.assertEqual(held["reason"], "load_unavailable")

    def test_malformed_live_record_holds_fail_closed(self):
        first = _submit(self, "searcher", "One")
        self.assertIsNone(producer_hold(self.project, "searcher"))
        bad = self.project.root / "inbox" / "badrecord.json"
        bad.write_text("{not-json")
        try:
            held = producer_hold(self.project, "searcher")
            self.assertIsNotNone(held)
            self.assertEqual(held["reason"], "load_unavailable")
            # Tolerant prompt/list paths still skip the bad file.
            self.assertTrue(isinstance(self.project.insights.generation(), str))
            self.assertTrue(isinstance(self.project.insights.list(limit=10), list))
        finally:
            try:
                bad.unlink()
            except OSError:
                pass
        self.assertIsNone(producer_hold(self.project, "searcher"))
        _decide(self, first, "reject")
        self.assertIsNone(producer_hold(self.project, "searcher"))

    def test_disabled_bound_ignores_unrelated_unavailable_store(self):
        self.config.roles["searcher"] = dataclasses.replace(
            self.config.roles["searcher"], held_at_capacity=True)
        _submit(self, "searcher", "One")
        # The opted-in open-work bound does not depend on inbox readability.
        self.project.insights.producer_inputs_per_source = 0
        self.project.insights.open_work_items = 100
        with patch.object(self.project.insights, "unresolved_counts",
                          side_effect=OSError("injected")):
            self.assertIsNone(producer_hold(self.project, "searcher"))
            view = self.project.status()["backpressure"]
            self.assertFalse(view["load_ok"])
            self.assertEqual(view["held"], [])
            counts = {row["source"]: row["pending"] for row in view["producer_inputs"]}
            self.assertIsNone(counts.get("searcher"))
            self.assertEqual(view["open_work_count"], 0)
        # Open-work disabled: open-ledger failures must not hold when only per-source matters.
        self.project.insights.producer_inputs_per_source = 100
        self.project.insights.open_work_items = 0
        with patch.object(self.project.work_items, "count_open_readonly",
                          side_effect=OSError("injected")):
            self.assertIsNone(producer_hold(self.project, "searcher"))
            view = self.project.status()["backpressure"]
            self.assertFalse(view["load_ok"])
            self.assertEqual(view["held"], [])
            self.assertIsNone(view["open_work_count"])
            self.assertEqual({row["source"]: row["pending"] for row in view["producer_inputs"]}["searcher"], 1)
        # Both disabled: status attempts both stores, admission never holds.
        self.project.insights.producer_inputs_per_source = 0
        self.project.insights.open_work_items = 0
        with patch.object(self.project.insights, "unresolved_counts",
                          side_effect=OSError("injected")):
            with patch.object(self.project.work_items, "count_open_readonly",
                              side_effect=OSError("injected")):
                self.assertIsNone(producer_hold(self.project, "searcher"))
                view = self.project.status()["backpressure"]
                self.assertFalse(view["load_ok"])
                self.assertEqual(view["held"], [])

    def test_invalid_limit_values_hold_fail_closed(self):
        _submit(self, "searcher", "One")
        for bad in (-1, 1073741825, True, "20", 0.5, None):
            self.project.insights.producer_inputs_per_source = bad
            self.project.insights.open_work_items = 100
            held = producer_hold(self.project, "searcher")
            self.assertIsNotNone(held, msg=f"per-source {bad!r} must hold")
            self.assertEqual(held["reason"], "load_unavailable")
            view = self.project.status()["backpressure"]
            self.assertFalse(view["load_ok"], msg=f"per-source {bad!r} status must be unavailable")
        self.project.insights.producer_inputs_per_source = 100
        self.config.roles["searcher"] = dataclasses.replace(
            self.config.roles["searcher"], held_at_capacity=True)
        for bad in (-1, 1073741825, False, "100", 1.5, None):
            self.project.insights.open_work_items = bad
            held = producer_hold(self.project, "searcher")
            self.assertIsNotNone(held, msg=f"open {bad!r} must hold")
            self.assertEqual(held["reason"], "load_unavailable")
            view = self.project.status()["backpressure"]
            self.assertFalse(view["load_ok"], msg=f"open {bad!r} status must be unavailable")

    def test_invalid_role_policy_fails_closed(self):
        self.config.roles["searcher"] = dataclasses.replace(
            self.config.roles["searcher"], held_at_capacity=1)
        held = producer_hold(self.project, "searcher")
        self.assertEqual(held["reason"], "load_unavailable")
        view = self.project.status()["backpressure"]
        rows = {row["source"]: row for row in view["held"]}
        self.assertEqual(rows["searcher"]["reasons"], ["load_unavailable"])

    def test_daemon_admission_veto_and_release(self):
        first = _submit(self, "searcher", "One")
        _submit(self, "searcher", "Two")
        snapshot = self.project.snapshots.get()
        self.assertTrue(should_run(self.project, snapshot, role_name="reporter"))
        self.assertFalse(should_run(self.project, snapshot, role_name="searcher"))
        _decide(self, first, "reject")
        snapshot = self.project.snapshots.get()
        self.assertTrue(should_run(self.project, snapshot, role_name="searcher"))


class ProducerEventPreservationTests(Fixture):
    def test_held_event_dispatch_consumes_nothing(self):
        self.project.insights.producer_inputs_per_source = 2
        self.config.roles["reviewer"] = dataclasses.replace(
            self.config.roles["reviewer"], decision_events=("reject",))
        event_source = _submit(self, "reviewer", "Find")
        _decide(self, event_source, "reject")
        _submit(self, "reviewer", "Pending one")
        _submit(self, "reviewer", "Pending two")
        before = self.project.insights.decision_events("reviewer", ("reject",))
        self.assertEqual(len(before), 1)
        before_runs = _runs(self.project)
        result = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "reviewer", event_trigger=True)
        self.assertEqual(result["skip"]["skipped"], "producer-held")
        self.assertEqual(_runs(self.project), before_runs)
        after = self.project.insights.decision_events("reviewer", ("reject",))
        self.assertEqual([event["event"] for event in after],
                         [event["event"] for event in before])
        pending = self.project.insights.unresolved_count("reviewer")
        self.assertEqual(pending, 2)
        self.project.insights.producer_inputs_per_source = 0
        admitted = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "reviewer", event_trigger=True)
        self.assertNotIn("skipped", admitted)
        from mizu.fs import read_json
        new_runs = _runs(self.project) - before_runs
        self.assertEqual(len(new_runs), 1)
        started = read_json(self.project.root / "runs" / new_runs.pop() / "started.json")
        self.assertEqual(started["admission"], "decision")


class ProducerStatusTests(Fixture):
    def test_backpressure_reports_limits_counts_holds(self):
        for name in ("searcher", "reviewer"):
            self.config.roles[name] = dataclasses.replace(
                self.config.roles[name], held_at_capacity=True)
        self.project.insights.producer_inputs_per_source = 2
        self.project.insights.open_work_items = 1
        _submit(self, "searcher", "One")
        _submit(self, "searcher", "Two")
        status = self.project.status()
        view = status["backpressure"]
        self.assertEqual(view["limits"], {"producer_inputs_per_source": 2,
                                          "open_work_items": 1})
        self.assertEqual(view["open_work_count"], 0)
        counts = {row["source"]: row["pending"] for row in view["producer_inputs"]}
        self.assertEqual(counts.get("searcher"), 2)
        self.assertEqual(counts.get("reviewer"), 0)
        self.assertNotIn("operator", counts)
        held = {row["source"]: row["reasons"] for row in view["held"]}
        self.assertEqual(held.get("searcher"), ["per_source_full"])
        held_rows = {row["source"]: row for row in view["held"]}
        self.assertEqual(held_rows["searcher"]["thresholds"],
                         {"producer_inputs_per_source": 2})
        self.assertTrue(view["load_ok"])
        self.project.work_items.create(
            idempotency_key="one", title="One", actor="worker")
        view = self.project.status()["backpressure"]
        self.assertEqual(view["open_work_count"], 1)
        held = {row["source"]: row["reasons"] for row in view["held"]}
        self.assertIn("open_work_full", held.get("searcher"))
        self.assertIn("open_work_full", held.get("reviewer"))
        self.assertEqual({row["source"]: row["thresholds"] for row in view["held"]},
                         {"searcher": {"producer_inputs_per_source": 2, "open_work_items": 1},
                          "reviewer": {"open_work_items": 1}})

    def test_disabled_bounds_still_report_current_counts(self):
        _submit(self, "searcher", "One")
        _submit(self, "not-configured", "Not a project producer")
        self.project.work_items.create(idempotency_key="one", title="One", actor="worker")
        for per_source, open_work in ((0, 100), (20, 0), (0, 0)):
            with self.subTest(per_source=per_source, open_work=open_work):
                self.project.insights.producer_inputs_per_source = per_source
                self.project.insights.open_work_items = open_work
                view = self.project.status()["backpressure"]
                self.assertTrue(view["load_ok"])
                self.assertEqual(view["open_work_count"], 1)
                counts = {row["source"]: row["pending"] for row in view["producer_inputs"]}
                self.assertEqual(counts["searcher"], 1)
                self.assertEqual(counts["reviewer"], 0)
                self.assertNotIn("not-configured", counts)
                self.assertEqual(view["held"], [])

    def test_unreadable_disabled_metric_does_not_mask_enabled_full_metric(self):
        _submit(self, "searcher", "One")
        self.project.insights.producer_inputs_per_source = 1
        self.project.insights.open_work_items = 0
        for name in ("searcher", "reviewer"):
            self.config.roles[name] = dataclasses.replace(
                self.config.roles[name], held_at_capacity=True)
        with patch.object(self.project.work_items, "count_open_readonly", side_effect=OSError("injected")):
            view = self.project.status()["backpressure"]
            self.assertFalse(view["load_ok"])
            self.assertIsNone(view["open_work_count"])
            self.assertEqual(view["held"], [{"source": "searcher", "pending": 1,
                                             "reasons": ["per_source_full"],
                                             "thresholds": {"producer_inputs_per_source": 1}}])
        self.project.insights.producer_inputs_per_source = 0
        self.project.insights.open_work_items = 1
        for name in ("searcher", "reviewer"):
            self.config.roles[name] = dataclasses.replace(
                self.config.roles[name], held_at_capacity=True)
        self.project.work_items.create(idempotency_key="one", title="One", actor="worker")
        with patch.object(self.project.insights, "unresolved_counts", side_effect=OSError("injected")):
            view = self.project.status()["backpressure"]
            self.assertFalse(view["load_ok"])
            self.assertEqual(view["open_work_count"], 1)
            self.assertTrue(view["held"])
            self.assertTrue(all(row["reasons"] == ["open_work_full"] and row["pending"] is None
                                and row["thresholds"] == {"open_work_items": 1}
                                for row in view["held"]))

    def test_status_stays_usable_when_load_unreadable(self):
        for name in ("searcher", "reviewer"):
            self.config.roles[name] = dataclasses.replace(
                self.config.roles[name], held_at_capacity=True)
        with patch.object(self.project.work_items, "count_open_readonly",
                          side_effect=OSError("injected")):
            view = self.project.status()["backpressure"]
            self.assertFalse(view["load_ok"])
            self.assertIsNone(view["open_work_count"])
            self.assertTrue(all(row["reasons"] == ["load_unavailable"]
                                for row in view["held"]))
