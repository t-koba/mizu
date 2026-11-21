"""Terminal delivery protocol: real stores and fake model dispatch, no RPC/network."""
import concurrent.futures
import contextlib
import dataclasses
import json
import os
import threading
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from support import Fixture, bind_delivery, ScriptDriver, finish_with_event_acks, forget_ledger
from mizu import fs, work_item_events as events
from mizu.cli import execute, parser
from mizu.config import load
from mizu.errors import Busy, ConfigError, Denied, InfraExceeded, LimitExceeded, MissingFinish
from mizu.fs import canonical, digest, lock, read_json
from support import history_write_json as write_json
from mizu.runtime import Engine, _role_events_due, prompt_for, prompt_delta_for
from mizu.services import event_watches, render
from mizu.platform import IS_WINDOWS
from mizu.work_items import _timestamp_order



class WorkItemEventTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.serial = 0

    def seed(self, source="reviewer"):
        self.serial += 1
        seed = self.store.create(idempotency_key=f"terminal-{self.serial}",
                                 title=f"Terminal work {self.serial}", actor=source)["record"]["work_item"]
        return seed, seed

    def terminal(self, source="reviewer", *, timestamp=None, done=False):
        proposal, seed = self.seed(source)
        kwargs = {}
        if done:
            captured = self.project.snapshots.capture_files(self.project.workspace)
            verification = {"passed": True, "code_digest": captured["code_digest"], "run": "verified-run"}
            snapshot = self.project.snapshots.create(
                captured, goal="Goal", state="State", run="verified-run", outcome="done",
                summary="Verified", verification=verification)
            self.project.snapshots.publish(snapshot)
            kwargs["snapshot_id"] = snapshot["id"]
            bind_delivery(self.project, seed["id"], snapshot["id"])
        with patch("mizu.work_items.now", return_value=timestamp) if timestamp else nullcontext():
            item = self.store.transition(seed["id"], expected_rev=3 if done else 1,
                                         status="done" if done else "dropped", actor="worker",
                                         reason="Terminal reason " + "界" * 2000, **kwargs)
        return proposal, item

    def page(self, role="reviewer", actions=("done", "dropped"), **kwargs):
        return events.pending(self.store, role, actions, **kwargs)

    def subscribed_config(self, **changes):
        roles = dict(self.config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], work_item_events=("done", "dropped"), **changes)
        return dataclasses.replace(self.config, roles=roles)

    def delivered(self, role="reviewer", **kwargs):
        return events.delivery(role, self.page(role, **kwargs))["events"]

    def finish(self, ctx, **kwargs):
        return ctx.handle("finish", {"outcome": "wait", "summary": "Handled selected events",
                                     "state": "Continue remaining work", **kwargs})

    def test_explicit_partial_empty_omitted_and_restart_receipts(self):
        from mizu.project import Project
        for _ in range(3):
            self.terminal()
        original = self.page()["events"]
        delivered = self.delivered()
        config = self.subscribed_config()
        for selection in (None, []):
            def no_ack(ctx, prompt, profile=None):
                self.finish(ctx, **({} if selection is None else {"ack_events": selection}))
            Engine(config, driver=ScriptDriver(no_ack)).run(self.project, "reviewer")
            self.assertEqual(self.page()["events"], original)
            self.assertEqual(self.delivered(), delivered)
        def partial(ctx, prompt, profile=None):
            self.finish(ctx, ack_events=[json.loads(prompt)["work_item_events"][1]["ack_token"]])
            self.assertEqual(self.page(ctx.role.name, sources="any")["events"], original)  # intent is not a receipt
        real_ack = events.acknowledge
        with patch.object(events, "acknowledge", wraps=real_ack) as ack:
            Engine(config, driver=ScriptDriver(partial)).run(self.project, "reviewer")
        self.assertEqual(ack.call_args.args[2], [original[1]])  # no token in stored row
        restarted = Project(config, "sample")
        self.assertEqual(events.pending(restarted.work_items, "reviewer", ("dropped",))["events"],
                         [original[0], original[2]])
        row = original[1]
        path = self.project.root / "work-item-receipts/reviewer" / f"{row['id']}.{row['rev']}.json"
        retained = path.read_bytes()
        self.assertEqual(set(read_json(path)), {"role", "source", "event", "acknowledged_at"})
        raw = events.acknowledgement_payload(restarted.work_items, "reviewer", delivered[1])
        events.acknowledge(restarted.work_items, "reviewer", [raw])
        self.assertEqual(path.read_bytes(), retained)
        self.assertEqual(len(self.page("searcher", sources="any")["events"]), 3)
        Engine(config, driver=ScriptDriver(finish_with_event_acks)).run(restarted, "reviewer")
        self.assertEqual(self.page()["events"], [])
        roles = dict(config.roles)
        roles["searcher"] = dataclasses.replace(roles["searcher"], work_item_events=("dropped",),
                                                work_item_sources="any")
        config = dataclasses.replace(config, roles=roles)
        Engine(config, driver=ScriptDriver(partial)).run(restarted, "searcher")
        self.assertEqual(self.page("searcher", sources="any")["events"], [original[0], original[2]])
        self.assertEqual(self.page()["events"], [])

    def test_terminal_token_validation_precedes_finish_intent(self):
        self.terminal()
        row = self.delivered()[0]
        other_role = self.delivered("searcher", sources="any")[0]
        self.assertNotEqual(row["ack_token"], other_role["ack_token"])
        self.terminal()
        # Timestamp ties are ordered by event identity, not insertion order.
        # Select the new event by identity rather than assuming it is row 1.
        undelivered = next(event for event in self.delivered()
                           if event["event"] != row["event"])
        ctx = self.context("reviewer")
        ctx.work_item_events = [row]
        for tokens in ([row["ack_token"], row["ack_token"]], [other_role["ack_token"]],
                       [undelivered["ack_token"]], ["0" * 64], [""], ["x" * 65],
                       [None], "not-an-array", ["x"] * 513):
            with self.subTest(tokens=str(tokens)[:80]), self.assertRaises(Denied):
                self.finish(ctx, ack_events=tokens)
            self.assertIsNone(ctx.finished)
            self.assertFalse((ctx.run_dir / "finish-intent.json").exists())
        # Even copying a row into delivery cannot break its role/payload binding.
        for altered in (other_role, {**row, "reason": "forged"}, {**row, "rev": row["rev"] + 1}):
            ctx.work_item_events = [altered]
            with self.assertRaises(Denied):
                self.finish(ctx, ack_events=[altered["ack_token"]])
            self.assertIsNone(ctx.finished)
            self.assertFalse((ctx.run_dir / "finish-intent.json").exists())
        ctx.work_item_events = []
        with self.assertRaises(Denied):
            self.finish(ctx, ack_events=[row["ack_token"]])
        ctx.work_item_events = [row]
        self.finish(ctx, ack_events=[row["ack_token"]])
        self.assertEqual(self.page()["pending"], 2)

    def test_corrected_terminal_revision_invalidates_old_delivered_token(self):
        from mizu import acceptance
        _, seed = self.seed()
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snapshot = self.project.snapshots.create(
            captured, goal="Goal", state="State", run="run", outcome="continue",
            summary="Delivery", verification={"passed": True, "code_digest": captured["code_digest"]})
        self.project.snapshots.publish(snapshot)
        bind_delivery(self.project, seed["id"], snapshot["id"])
        dropped = self.store.transition(seed["id"], expected_rev=3, status="dropped",
                                        actor="worker", reason="Mistaken disposition")
        ctx = self.context("reviewer")
        ctx.work_item_events = self.delivered()
        bound = {"commands": ["check"], "execution": "1" * 64, "environment": "2" * 64,
                 "image": "sha256:" + "3" * 64}
        acceptance._retain(self.project.root, {
            "schema": 1, "code_digest": snapshot["code_digest"], "scope": bound,
            "producer": {"kind": "verify", "id": "4" * 32, "run": "4" * 32}, "passed": True,
            "commands": [{"id": "5" * 32, "exit_code": 0, "reason": "exited"}]})
        with patch("mizu.acceptance._current_scope", return_value=bound):
            self.store.transition(seed["id"], expected_rev=dropped["rev"], status="done",
                                  actor="worker", snapshot_id=snapshot["id"])
        current = self.delivered()[0]
        self.assertNotEqual(current["event"], ctx.work_item_events[0]["event"])
        self.assertNotEqual(current["ack_token"], ctx.work_item_events[0]["ack_token"])
        with self.assertRaises(Denied):
            self.finish(ctx, ack_events=[ctx.work_item_events[0]["ack_token"]])
        self.assertIsNone(ctx.finished)
        self.assertFalse((ctx.run_dir / "finish-intent.json").exists())
        self.assertEqual(self.page()["pending"], 1)

    def test_token_bearing_delivery_retains_row_and_page_bounds(self):
        self.terminal()
        page = self.page()
        with patch.object(events, "MAX_CLOSED_ROW_BYTES", len(canonical(page["events"][0]))):
            with self.assertRaises(LimitExceeded):
                events.delivery("reviewer", page)
        with patch.object(events, "MAX_CLOSED_PAGE_BYTES", len(canonical(page))):
            with self.assertRaises(LimitExceeded):
                events.delivery("reviewer", page)
        self.assertNotIn("ack_token", page["events"][0])  # raw store view unchanged
        self.assertEqual(len(events.delivery("reviewer", page)["events"][0]["ack_token"]), 64)

    def test_terminal_selection_is_independent_of_outcome_and_check_wait(self):
        roles = dict(self.subscribed_config().roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], capabilities=(
            *roles["reviewer"].capabilities, "check_job"))
        config = dataclasses.replace(self.config, roles=roles)
        for outcome in ("continue", "blocked", "done", "bounded", "wait"):
            for explicit in (False, True):
                with self.subTest(outcome=outcome, explicit=explicit):
                    if not explicit:
                        self.terminal()
                    def work(ctx, prompt, profile=None):
                        if outcome == "bounded":
                            ctx.bound_hit = "requests"
                        self.finish(ctx, outcome="wait" if outcome == "bounded" else outcome,
                                    **({"ack_events": [r["ack_token"] for r in ctx.work_item_events]}
                                       if explicit else {}),
                                    **({"wait_for_check": "c" * 64} if outcome == "wait" else {}))
                    with patch("mizu.runtime.check_jobs.read", return_value={"role": "reviewer"}), \
                            patch("mizu.runtime.check_jobs.set_wait"):
                        Engine(config, driver=ScriptDriver(work)).run(self.project, "reviewer")
                    self.assertEqual(self.page()["pending"], 0 if explicit else 1)

    def test_failed_writer_publication_leaves_terminal_selection_pending(self):
        self.terminal(source="worker")
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], work_item_events=("dropped",))
        config = dataclasses.replace(self.config, roles=roles)
        with patch.object(self.project.snapshots, "publish", side_effect=OSError("publication failed")), \
                patch.object(events, "acknowledge") as ack:
            with self.assertRaises(OSError):
                Engine(config, driver=ScriptDriver(finish_with_event_acks)).run(self.project, "worker")
        ack.assert_not_called()
        self.assertEqual(self.page("worker")["pending"], 1)
        Engine(config, driver=ScriptDriver(finish_with_event_acks)).run(self.project, "worker")
        self.assertEqual(self.page("worker")["pending"], 0)

    def test_done_has_snapshot_and_bounded_verification_reference(self):
        _, item = self.terminal(done=True)
        row = self.page()["events"][0]
        self.assertEqual(row["snapshot_id"], item["snapshot_id"])
        self.assertEqual(row["verification"], {"sha256": digest(canonical(item["verification"])),
                                               "passed": True,
                                               "code_digest": item["verification"]["code_digest"]})
        self.assertLessEqual(len(canonical(row)), 8192)
        events.acknowledge(self.store, "reviewer", [row])
        self.assertEqual(self.page()["events"], [])

    def test_pending_page_limit_edges(self):
        self.terminal()
        self.assertEqual(self.page(limit=1)["pending"], 1)
        self.assertEqual(self.page(limit=100)["pending"], 1)
        for limit in (0, 101):
            with self.subTest(limit=limit), self.assertRaises(Denied):
                self.page(limit=limit)

    def test_acknowledgement_batch_accepts_100_and_rejects_101(self):
        item = {"id": "wi_" + "a" * 32, "rev": 1}
        with patch.object(events, "_validate_payload", return_value=item) as validate, \
                patch.object(events, "_received", return_value=True):
            events.acknowledge(self.store, "reviewer", [{}] * 100)
            self.assertEqual(validate.call_count, 100)
            with self.assertRaisesRegex(Denied, "Invalid work-item acknowledgement batch"):
                events.acknowledge(self.store, "reviewer", [{}] * 101)
            self.assertEqual(validate.call_count, 100)

    def test_progress_role_page_limit_edges(self):
        role = SimpleNamespace(work_item_events=("done",), work_item_sources="own")
        roles = {f"role_{index:03d}": role for index in range(101)}
        one = events.progress(self.store, roles, limit=1)
        self.assertEqual((one["total"], len(one["roles"])), (101, 1))
        full = events.progress(self.store, roles, limit=100)
        self.assertEqual((full["total"], len(full["roles"])), (101, 100))
        for limit in (0, 101):
            with self.subTest(limit=limit), self.assertRaises(Denied):
                events.progress(self.store, roles, limit=limit)

    def test_timestamp_normalization_ties_rollback_and_limit_drain(self):
        times = ("2030-03-01T09:00:00+09:00", "2030-03-01T00:00:00+00:00",
                 "2020-01-01T00:00:00+00:00", "2030-03-01T00:00:01+00:00")
        items = [self.terminal(timestamp=t)[1] for t in times]
        ordered = [items[2]["id"], *sorted([items[0]["id"], items[1]["id"]]), items[3]["id"]]
        all_seen = []
        for n in range(4):
            page = self.page(limit=1)
            self.assertEqual((page["pending"], page["acknowledged"]), (4-n, n))
            self.assertEqual(page["overflow_count"], 3-n)
            self.assertEqual(page["truncated"], n < 3)
            all_seen += [page["events"][0]["id"]]
            events.acknowledge(self.store, "reviewer", page["events"])
        self.assertEqual(all_seen, ordered)
        self.assertEqual(self.page()["events"], [])
        # A terminal event can arrive after a successful batch with an older
        # clock value. Exact receipts must still expose it.
        _, rollback = self.terminal(timestamp="2010-01-01T00:00:00+00:00")
        self.assertEqual(self.page(limit=1)["events"][0]["id"], rollback["id"])

    def test_partial_ack_failure_is_exact_and_replay_safe_and_per_role(self):
        for _ in range(3):
            self.terminal()
        selected = self.page()["events"]
        real_write = events.write_json
        writes = []
        def fail_second(path, value, **kwargs):
            if "work-item-receipts" not in path.parts:
                return real_write(path, value, **kwargs)
            writes.append(path)
            if len(writes) == 2:
                raise OSError("receipt unavailable")
            real_write(path, value, **kwargs)
        with patch.object(events, "write_json", side_effect=fail_second):
            with self.assertRaises(OSError):
                events.acknowledge(self.store, "reviewer", selected)
        page = self.page()
        self.assertEqual(page["acknowledged"], 1)
        self.assertEqual(page["events"], selected[1:])
        # A disjoint concurrent acknowledgement cannot overwrite another one.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda row: events.acknowledge(self.store, "reviewer", [row]), selected[1:]))
        events.acknowledge(self.store, "reviewer", selected)
        self.assertEqual(self.page()["acknowledged"], 3)
        # Identical concurrent receipts are idempotent and immutable.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: events.acknowledge(self.store, "searcher", [selected[0]]), range(2)))
        self.assertEqual(self.page("searcher", sources="any")["acknowledged"], 1)
        self.assertEqual(self.page("searcher", sources="any")["pending"], 2)
        # An uncommitted atomic-write leftover is never a receipt.
        directory = self.project.root / "work-item-receipts" / "reviewer"
        (directory / ".new-abcdefgh").write_text("incomplete")
        self.assertEqual(self.page()["acknowledged"], 3)
        self.assertTrue((directory / ".new-abcdefgh").exists())

    def test_concurrent_acknowledgements_with_distinct_timestamps_are_idempotent(self):
        _, item = self.terminal()
        selected = self.page()["events"]
        timestamps = ["2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"]
        barrier = threading.Barrier(2)
        real_write = events.write_json
        real_sync = fs.sync_dir
        published = threading.Event()
        loser_finished = threading.Event()
        writer = threading.local()
        receipt = events._receipt_path(self.store, "reviewer", item)

        def simultaneous_write(path, value, **kwargs):
            if path != receipt:
                return real_write(path, value, **kwargs)
            # Both calls pass the absence check before publication. The loser
            # must validate the winner while its atomic_write is still active.
            barrier.wait(timeout=10)
            if value["acknowledged_at"] == timestamps[0]:
                return real_write(path, value, **kwargs)
            writer.loser = True
            if not published.wait(10):
                raise AssertionError("winner did not publish")
            return real_write(path, value, **kwargs)

        def acknowledge(_):
            try:
                events.acknowledge(self.store, "reviewer", selected)
            finally:
                # Signal after acknowledge's collision handler, not just after
                # write_json: validation must finish before the winner resumes.
                if getattr(writer, "loser", False):
                    loser_finished.set()

        def hold_publication(directory):
            if directory == receipt.parent:
                # Pause after the commit point but before temporary cleanup.
                # A hard-link publication leaves nlink=2 throughout this gap.
                published.set()
                if not loser_finished.wait(10):
                    raise AssertionError("losing acknowledgement did not finish")
                self.assertEqual(receipt.stat().st_nlink, 1)
                self.assertTrue(events._received(self.store, "reviewer", item))
            return real_sync(directory)

        # Receipt collision is independent of records.lock; suspend the new
        # serialization here so both exclusive writes still overlap.
        with patch.object(events, "record_lock", return_value=nullcontext()), \
                patch.object(events, "now", side_effect=timestamps), patch.object(
                events, "write_json", side_effect=simultaneous_write) as writes, patch.object(
                fs, "sync_dir", side_effect=hold_publication):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(acknowledge, range(2)))
        receipts = [call for call in writes.call_args_list if call.args[0] == receipt]
        self.assertEqual(sorted(call.args[1]["acknowledged_at"] for call in receipts), timestamps)
        self.assertEqual([call.args[0] for call in receipts], [receipt, receipt])
        self.assertTrue(all(call.kwargs == {"exclusive": True} for call in receipts))
        original = receipt.read_bytes()
        self.assertIn(read_json(receipt)["acknowledged_at"], timestamps)
        events.acknowledge(self.store, "reviewer", selected)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(self.page()["acknowledged"], 1)
        self.assertEqual(self.page()["pending"], 0)

    def test_malformed_or_incomplete_chain_and_unsafe_receipt_fail_closed(self):
        _, item = self.terminal()
        path = self.project.root / "work-items" / item["id"] / "2.json"
        original = path.read_bytes()
        bad = dict(item, transition={"from": "doing", "to": "dropped"})
        write_json(path, bad)
        with self.assertRaises(Denied):
            self.page()
        path.write_bytes(original)
        gap = path.with_name("3.json")
        path.rename(gap)
        with self.assertRaises(Denied):
            self.page()
        gap.rename(path)
        # Direct history edits now change the ledger generation; a write
        # boundary re-pins the index before the restored history is read.
        self.project.refresh_settings()
        selected = self.page()["events"]
        events.acknowledge(self.store, "reviewer", selected)
        receipt = self.project.root / "work-item-receipts/reviewer" / f"{item['id']}.2.json"
        write_json(receipt, {"event": selected[0]["event"]})
        # Receipts are checked when written, never on a read.
        with self.assertRaises(Denied):
            events.acknowledge(self.store, "reviewer", selected)
        with self.assertRaises(Denied):
            events.acknowledge(self.store, "reviewer", [{**selected[0], "id": "../outside"}])

    def test_acknowledged_position_is_exact_bounded_and_ordered(self):
        for _ in range(12):
            self.terminal()
        rows = self.page(limit=100)["events"]
        events.acknowledge(self.store, "reviewer", rows)
        page = self.page(limit=100)
        expected = [row["event"] for row in sorted(
            rows, key=lambda row: (_timestamp_order(row["terminal_at"]), row["id"], row["rev"]))[-10:]]
        self.assertEqual(page["acknowledged"], 12)
        self.assertEqual(page["acknowledged_event_ids"], expected)
        self.assertTrue(page["acknowledged_ids_truncated"])

    def test_ack_failure_replays_and_new_events_during_run_not_acknowledged(self):
        self.terminal()
        original = events.delivery("reviewer", self.page())["events"]
        def arrive(ctx, prompt, profile=None):
            self.terminal()
            finish_with_event_acks(ctx, prompt, profile)
        driver = ScriptDriver(arrive)
        engine = Engine(self.subscribed_config(), driver=driver)
        with patch.object(events, "acknowledge", side_effect=OSError("full disk")):
            result = engine.run(self.project, "reviewer", event_trigger=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.page()["pending"], 2)
        self.assertEqual(read_json(driver.calls[-1][0].run_dir / "work-item-events.json")["events"], original)
        # After retry both are delivered; an arrival during this second run
        # remains pending because acknowledgements use only the frozen batch.
        engine.run(self.project, "reviewer", event_trigger=True)
        self.assertEqual((self.page()["pending"], self.page()["acknowledged"]), (1, 2))

    def test_failed_unsealed_and_bounded_runs_do_not_ack(self):
        self.terminal()
        def fail(*_):
            raise Denied("injected execution failure")
        with self.assertRaises(Denied):
            Engine(self.subscribed_config(), driver=ScriptDriver(fail)).run(self.project, "reviewer", event_trigger=True)
        class Unsealed:
            requires_sandbox = False
            def execute(self, *args, **kwargs):
                return {"requests": 0}
        with self.assertRaises(MissingFinish):
            Engine(self.subscribed_config(), driver=Unsealed()).run(self.project, "reviewer", event_trigger=True)
        def bounded(ctx, *_):
            ctx.bound_hit = "requests"
        Engine(self.subscribed_config(), driver=ScriptDriver(bounded)).run(self.project, "reviewer", event_trigger=True)
        self.assertEqual((self.page()["pending"], self.page()["acknowledged"]), (1, 0))

    def test_busy_and_selector_wait_do_not_ack(self):
        self.terminal()
        engine = Engine(self.subscribed_config(), driver=ScriptDriver(finish_with_event_acks))
        with lock(self.project.root / "locks/run-reviewer.lock"):
            with self.assertRaises(Busy):
                engine.run(self.project, "reviewer", event_trigger=True)
        # Use a real empty selector policy, as in selection integration tests.
        # A coalesced pre-run selector wait is enough to exercise the gate;
        # the selection mechanism's own tests cover initial wait persistence.
        waiting = {"profile": "", "next_evaluation_at": None}
        selector_config = self.subscribed_config(selector="test-selector")
        with patch("mizu.classification.prepare", return_value=waiting), \
                patch("mizu.selection.wait_fingerprint", return_value="stable"), \
                patch("mizu.selection.read_wait", return_value={"fingerprint": "stable", "run": "prior-wait"}):
            result = Engine(selector_config, driver=ScriptDriver(finish_with_event_acks)).run(self.project, "reviewer", event_trigger=True)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(self.page()["pending"], 1)
        exhausted = dataclasses.replace(self.subscribed_config(),
                                         limits=dataclasses.replace(self.config.limits, daily_requests=0))
        with self.assertRaises(InfraExceeded):
            Engine(exhausted, driver=ScriptDriver(finish_with_event_acks)).run(self.project, "reviewer", event_trigger=True)
        self.assertEqual(self.page()["acknowledged"], 0)

    def test_daemon_gate_and_platform_watch_rendering(self):
        config = self.subscribed_config(interval_seconds=0, calendar=(), daemon=False)
        self.project.config = config
        watches = event_watches(config, self.project)
        self.assertIn(str(self.project.root / "work-items"), watches["reviewer"])
        self.assertNotIn(str(self.project.root / "work-item-receipts/reviewer"), watches["reviewer"])
        for system, marker in (("linux", "PathChanged="), ("macos", "WatchPaths")):
            definitions = render(dataclasses.replace(config, timezone="local"), self.project, self.root / "mizu", system=system)
            text = "\n".join(definitions.values())
            self.assertIn(marker, text)
            self.assertIn(str(self.project.root / "work-items"), text)
        self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), "reviewer"))
        with patch("mizu.work_items.os.utime") as hint:
            self.terminal()
        hint.assert_called_once_with(self.project.root / "work-items", None, follow_symlinks=False)
        self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), "reviewer"))
        with patch("mizu.work_item_events.os.utime") as ack_hint:
            events.acknowledge(self.store, "reviewer", self.page()["events"])
        ack_hint.assert_called_once_with(self.project.root / "work-items", None, follow_symlinks=False)
        self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), "reviewer"))

    def grouped_terminal_fixture(self, *, groups=4, unrelated=24):
        proposals, decisions, items = [], [], []
        for n in range(groups + unrelated):
            proposal = self.project.insights.submit(
                source="reviewer" if n % 2 == 0 else "searcher",
                title=f"Grouped ledger {n}", body="Evidence", base_snapshot=None)
            decision = self.project.insights.decide(
                proposal["id"], "accept", "Reviewed", "", "run", expected_rev=1,
                **({"work_items": [{"title": f"Slice {i}", "description": "Deliver slice"}
                                   for i in range(2)]} if n < groups else {"work_item_intent": "none"}))
            if n < groups:
                proposals.append(proposal)
                decisions.append(decision)
                for seed in decision["work_items"]:
                    # Ties and clock rollback keep the existing id tie-breaks relevant.
                    with patch("mizu.work_items.now", return_value=f"2030-01-{groups-n:02d}T00:00:00Z"):
                        items.append(self.store.transition(seed["id"], expected_rev=1,
                                                           status="dropped", actor="worker", reason="Retired"))
        return proposals, decisions, items

    def test_cli_progress_is_bounded_and_read_only(self):
        # Persist an opt-in so the CLI's read-only Project uses it.
        self.file.write_text(self.file.read_text().replace(
            "[roles.reviewer]", '[roles.reviewer]\nwork_item_events = ["dropped"]'))
        self.terminal()
        def contents():
            return {p.relative_to(self.project.root).as_posix(): (p.stat().st_mtime_ns, p.read_bytes())
                    for p in self.project.root.rglob("*") if p.is_file()}
        before = contents()
        args = parser().parse_args(["--config", str(self.file), "work-items", self.project.name])
        with patch("mizu.work_items.lock", side_effect=AssertionError("read lock")), \
                patch.object(events, "acknowledge", side_effect=AssertionError("read ack")):
            output = execute(args)
        rows = output["event_delivery"]["roles"]
        self.assertTrue(all(row["subscribed"] for row in rows))
        reviewer = next(row for row in rows if row["role"] == "reviewer")
        current_event_id = self.page()["events"][0]["event"]
        self.assertEqual((reviewer["subscribed"], reviewer["pending"], reviewer["acknowledged"]),
                         (["dropped"], 1, 0))
        self.assertEqual(reviewer["pending_event_ids"], [current_event_id])
        self.assertEqual(reviewer["acknowledged_event_ids"], [])
        self.assertNotIn("events", reviewer)
        self.assertEqual(contents(), before)
        events.acknowledge(self.store, "reviewer", self.page()["events"])
        output = execute(args)
        reviewer = next(row for row in output["event_delivery"]["roles"] if row["role"] == "reviewer")
        self.assertEqual((reviewer["pending"], reviewer["acknowledged"]), (0, 1))
        self.assertEqual(reviewer["pending_event_ids"], [])
        self.assertEqual(reviewer["acknowledged_event_ids"], [current_event_id])
        roles = dict(self.subscribed_config().roles)
        roles["searcher"] = dataclasses.replace(roles["searcher"], work_item_events=("dropped",))
        bounded = events.progress(self.store, roles, limit=1)
        self.assertEqual(len(bounded["roles"]), 1)
        self.assertTrue(bounded["truncated"])
        self.assertFalse(bounded["coverage_complete"])


    def test_closed_acknowledgements_bounds_and_invalid_receipts_fail_closed(self):
        _, item = self.terminal()
        roles = self.subscribed_config().roles
        base = self.store.page_closed_readonly()
        with patch.object(events, "MAX_CLOSED_PAGE_BYTES", len(canonical(base))):
            with self.assertRaises(LimitExceeded):
                events.closed_page(self.store, roles)
        # More than the summary's 100 roles must not be silently omitted.
        many = {f"role-{n:04d}": dataclasses.replace(roles["reviewer"], work_item_sources="any")
                for n in range(120)}
        result = events.closed_page(self.store, many)
        self.assertEqual(len(result["items"][0]["event_acknowledgements"]), 120)
        payload_size = len(canonical(self.page()["events"][0]))
        with patch.object(events, "MAX_CLOSED_ROW_BYTES", payload_size):
            with self.assertRaises(LimitExceeded):
                events.closed_page(self.store, many)
        events.acknowledge(self.store, "reviewer", self.page()["events"])
        receipt = self.project.root / "work-item-receipts/reviewer" / f"{item['id']}.2.json"
        value = read_json(receipt)
        value["event"] = f"work_item:{item['id']}:1"
        write_json(receipt, value)
        with self.assertRaises(Denied):
            events.closed_page(self.store, roles)

    def test_receipts_survive_backup_restore(self):
        from mizu.storage import backup, restore
        from mizu.project import Project
        self.terminal()
        events.acknowledge(self.store, "reviewer", self.page()["events"])
        self.project.set_control(paused=True)
        archive = self.root / "backup.tar.gz"
        backup(self.project, archive)
        restore(self.config, "restored", archive)
        restored = Project(self.config, "restored", read_only=True)
        page = events.pending(restored.work_items, "reviewer", ("dropped",))
        self.assertEqual((page["pending"], page["acknowledged"]), (0, 1))

    def test_result_commit_failure_does_not_ack(self):
        self.terminal()
        from mizu import runtime
        real_write = runtime.write_json
        def fail_completion(path, value, **kwargs):
            if path.name == "result.json" and value.get("status") == "completed":
                raise OSError("injected completion durability failure")
            return real_write(path, value, **kwargs)
        with patch.object(runtime, "write_json", side_effect=fail_completion), \
                patch.object(events, "acknowledge") as ack:
            with self.assertRaises(OSError):
                Engine(self.subscribed_config(), driver=ScriptDriver(finish_with_event_acks)).run(
                    self.project, "reviewer", event_trigger=True)
        ack.assert_not_called()
        self.assertEqual(self.page()["pending"], 1)

    def test_bounded_dispatch_drain_excludes_receipts_before_limit(self):
        for _ in range(3):
            self.terminal()
        config = self.subscribed_config()
        config = dataclasses.replace(config, limits=dataclasses.replace(config.limits, work_item_events_limit=1))
        self.project.config = config
        driver = ScriptDriver(finish_with_event_acks)
        engine = Engine(config, driver=driver)
        selected = []
        for remaining in (2, 1, 0):
            result = engine.run(self.project, "reviewer", event_trigger=True)
            self.assertEqual(result["status"], "completed")
            page = read_json(driver.calls[-1][0].run_dir / "work-item-events.json")
            selected.append(page["events"][0]["event"])
            self.assertEqual(len(page["events"]), 1)
            self.assertEqual(page["overflow_count"], remaining)
            self.assertEqual(self.page()["pending"], remaining)
        self.assertEqual(len(set(selected)), 3)
        self.assertEqual(engine.run(self.project, "reviewer", event_trigger=True)["skipped"], "no-event")

    def test_missing_index_is_built_on_a_write_and_reads_fail_closed_until_then(self):
        _, item = self.terminal()
        rows = self.page()["events"]
        events.acknowledge(self.store, "reviewer", rows)
        self.assertEqual(self.page()["acknowledged"], 1)
        for name in (events.INDEX_NAME, events.HEADS_NAME):
            (self.project.root / name).unlink()
        with self.assertRaisesRegex(Denied, "index missing"):
            self.page()
        # The next terminal write rebuilds from validated source and receipts.
        _, fresh = self.terminal()
        page = self.page()
        self.assertEqual((page["pending"], page["acknowledged"]), (1, 1))
        self.assertEqual([row["id"] for row in page["events"]], [fresh["id"]])
        self.assertEqual(page["acknowledged_event_ids"], [rows[0]["event"]])

    def test_advanced_marker_fails_closed_until_a_write_rebuilds(self):
        import sqlite3
        self.terminal()
        self.assertEqual(self.page()["pending"], 1)
        # Simulate a ledger commit whose index append was interrupted: only
        # the one-row marker moved, so the compact index is stale.
        with contextlib.closing(sqlite3.connect(self.project.root / "ledger.sqlite3")) as db, db:
            db.execute("UPDATE terminal_changes SET seq=seq+1 WHERE singleton=1")
        with self.assertRaisesRegex(Denied, "stale"):
            self.page()
        self.terminal()
        self.assertEqual(self.page()["pending"], 2)

    def test_terminal_closures_append_without_rescanning_terminal_history(self):
        original = events._compose_index
        scanned = []

        def counting(store, found, roles, received):
            scanned.append(len(found))
            return original(store, found, roles, received)

        for closure in range(5):
            with patch.object(events, "_compose_index", side_effect=counting):
                self.terminal()
            page = self.page()
            self.assertEqual(page["pending"], closure + 1)
            index = events._read_index(self.store)
            self.assertEqual((index["generation"], index["terminal_seq"]),
                             events.terminal_marker(self.store))
        # The first close rebuilds once with its own row; the next four
        # closures append without scanning terminal history again.
        self.assertEqual(scanned, [1])

    def test_new_role_sees_existing_events_on_refresh_and_fresh_project(self):
        import dataclasses as dc
        from mizu.project import Project, initialize
        roles = dict(self.config.roles)
        roles["maintainer"] = dc.replace(roles["maintainer"], work_item_events=("dropped",),
                                         work_item_sources="any")
        config = dc.replace(self.config, roles=roles)
        source = self.root / "source"

        # A fresh project whose settings already name the new role, but whose
        # earlier events were written while the role was not subscribed.
        fresh = initialize(config, "fresh", source, self.goal,
                           ["worker", "searcher", "reviewer", "reporter", "consult"],
                           ["python3 -m unittest"])
        for number in range(2):
            seed = fresh.work_items.create(idempotency_key=f"pre-role-{number}",
                                           title="Before subscription", actor="reviewer")["record"]["work_item"]
            fresh.work_items.transition(seed["id"], expected_rev=1, status="dropped",
                                        actor="worker", reason="Retired")
        path = fresh.root / "project.toml"
        path.write_text(path.read_text().replace(
            '"consult"]', '"consult", "maintainer"]'))
        restarted = Project(config, "fresh")
        page = events.pending(restarted.work_items, "maintainer", ("dropped",), sources="any")
        self.assertEqual((page["pending"], len(page["events"])), (2, 2))

        # The same seeding happens when a running project reloads settings.
        for _ in range(2):
            self.terminal()
        self.assertEqual(self.page("maintainer", sources="any")["pending"], 0)
        path = self.project.root / "project.toml"
        path.write_text(path.read_text().replace(
            '"consult"]', '"consult", "maintainer"]'))
        self.project.config = config
        self.project.refresh_settings()
        page = events.pending(self.store, "maintainer", ("dropped",), sources="any")
        self.assertEqual((page["pending"], len(page["events"])), (2, 2))
        self.assertEqual(self.page()["pending"], 2)  # the original subscriber is unchanged

    def test_pending_read_does_not_grow_with_acknowledged_history(self):
        _, item = self.terminal()
        row = self.page()["events"][0]
        template = self.store.current(item["id"])

        def scan(acked):
            found = [(canonical(template).decode(), "reviewer", "dropped")]
            for number in range(acked):
                synthetic = {**template, "id": f"wi_{number:032x}",
                             "updated_at": f"2030-01-01T00:{number // 60 % 60:02d}:{number % 60:02d}+00:00"}
                found.append((canonical(synthetic).decode(), "reviewer", "dropped"))
            def received(store, role, candidate):
                return candidate["id"] != item["id"]
            index, heads = events._compose_index(self.store, found, ("reviewer",), received)
            events._write_index(self.store, index)
            events._write_heads(self.store, heads)
            reads = []
            real_read = events.safe_read
            def counting_read(root, name, maximum):
                raw = real_read(root, name, maximum)
                reads.append((name, len(raw)))
                return raw
            with patch.object(events, "safe_read", side_effect=counting_read), \
                    patch.object(self.store, "current_items", side_effect=AssertionError("history read")), \
                    patch.object(events, "_receipts", side_effect=AssertionError("receipt scan")):
                page = events.pending(self.store, "reviewer", ("dropped",))
            self.assertEqual([name for name, _ in reads], [events.INDEX_NAME])
            return page, reads, len(events._read_index(self.store)["events"]), heads

        small, small_reads, small_rows, small_heads = scan(2_000)
        large, large_reads, large_rows, large_heads = scan(4_000)
        self.assertEqual((small_rows, large_rows), (1, 1))
        self.assertEqual(small_reads, large_reads)  # exact deterministic read work
        self.assertEqual(len(small_heads["heads"]), 2_001)
        self.assertEqual(len(large_heads["heads"]), 4_001)
        self.assertEqual((small["pending"], len(small["events"])), (1, 1))
        self.assertEqual((large["pending"], len(large["events"])), (1, 1))
        self.assertEqual((small["acknowledged"], large["acknowledged"]), (2_000, 4_000))
        self.assertEqual(len(large["acknowledged_event_ids"]), 10)
        self.assertTrue(large["acknowledged_ids_truncated"])
        self.assertEqual({k: v for k, v in small.items() if k not in
                          ("acknowledged", "acknowledged_event_ids", "acknowledged_ids_truncated")},
                         {k: v for k, v in large.items() if k not in
                          ("acknowledged", "acknowledged_event_ids", "acknowledged_ids_truncated")})

    def test_subscription_and_limit_bounds_fail_closed(self):
        for actions in (("doing",), ("done", "done")):
            with self.assertRaises(Denied):
                self.page(actions=actions)
        for limit in (0, 101, True, 1.5):
            with self.assertRaises(Denied):
                self.page(limit=limit)
        with self.assertRaises(Denied):
            self.page(sources="actor")
