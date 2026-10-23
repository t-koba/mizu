"""Exact, explicitly selected decision receipts; no completion watermarks."""
import dataclasses
import json
from unittest.mock import patch

from support import Fixture, ScriptDriver, finish_with_decision_acks
from test_decision_events import _reject, _trigger_config
from mizu.errors import Denied, LimitExceeded
from mizu.fs import read_json, write_json
from mizu.insights import Insights
from mizu.runtime import Engine, prompt_for, prompt_delta_for, _role_events_due


class DecisionReceiptTests(Fixture):
    def rows(self, role="reviewer", **kwargs):
        return self.project.insights.decision_events(role, ("reject", "defer"), **kwargs)

    def finish(self, ctx, **kwargs):
        return ctx.handle("finish", {"outcome": "wait", "summary": "Handled selected events",
                                     "state": "Continue remaining work", **kwargs})

    def test_partial_empty_and_omitted_selection_replay_after_restart(self):
        for i in range(3):
            _reject(self.project, "reviewer", title=f"Finding {i}")
        before = self.rows()
        config = _trigger_config(self.config)
        for selection in (None, []):
            def no_ack(ctx, prompt, profile=None):
                self.finish(ctx, **({} if selection is None else {"ack_events": selection}))
            result = Engine(config, driver=ScriptDriver(no_ack)).run(self.project, "reviewer")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(self.rows(), before)
        def partial(ctx, prompt, profile=None):
            self.finish(ctx, ack_events=[json.loads(prompt)["decision_events"][1]["ack_token"]])
            self.assertEqual(self.rows(), before)  # an intent is not a receipt
        Engine(config, driver=ScriptDriver(partial)).run(self.project, "reviewer")
        self.assertEqual(self.rows(), [before[0], before[2]])
        restarted = Insights(self.project.root)
        self.assertEqual(restarted.decision_events("reviewer", ("reject",)), [before[0], before[2]])
        # Durable exact retries do not rewrite the first receipt.
        path = self.project.root / "decision-receipts/reviewer" / f"{before[1]['ack_token']}.json"
        retained = path.read_bytes()
        restarted.acknowledge_decisions("reviewer", [before[1]])
        self.assertEqual(path.read_bytes(), retained)
        Engine(config, driver=ScriptDriver(finish_with_decision_acks)).run(self.project, "reviewer")
        self.assertEqual(self.rows(), [])

    def test_token_validation_before_seal(self):
        _reject(self.project, "reviewer")
        row = self.rows()[0]
        foreign = self.rows("searcher", sources="any")[0]
        ctx = self.context("reviewer")
        ctx.decision_events = [row]
        for value in ([row["ack_token"], row["ack_token"]], [foreign["ack_token"]],
                      ["0" * 64], [""], ["x" * 65], [None], "not-an-array", ["x"] * 513):
            with self.subTest(value=str(value)[:80]):
                with self.assertRaises(Denied):
                    self.finish(ctx, ack_events=value)
                self.assertIsNone(ctx.finished)
                self.assertFalse((ctx.run_dir / "finish-intent.json").exists())
        # Valid host tokens are not authority unless delivered in this unit.
        ctx.decision_events = []
        with self.assertRaises(Denied):
            self.finish(ctx, ack_events=[row["ack_token"]])
        # Nor can untrusted evidence turn a token string into a delivery.
        ctx.decision_events = [row]
        _reject(self.project, "reviewer", title="Undelivered")
        other = next(r for r in self.rows() if r["event"] != row["event"])
        with self.assertRaises(Denied):
            self.finish(ctx, ack_events=[other["ack_token"]])
        self.finish(ctx, ack_events=[row["ack_token"]])
        self.assertEqual(len(self.rows()), 2)  # sealing alone persists no receipt

    def test_receipts_are_role_and_exact_payload_bound(self):
        _reject(self.project, "reviewer")
        row = self.rows()[0]
        other = self.rows("searcher", sources="any")[0]
        self.assertNotEqual(row["ack_token"], other["ack_token"])
        for target, payload in (("searcher", row), ("reviewer", {**row, "reason": "forged"})):
            with self.assertRaises(Denied):
                self.project.insights.acknowledge_decisions(target, [payload], sources="any")
        self.project.insights.acknowledge_decisions("reviewer", [row])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows("searcher", sources="any"), [other])
        with self.assertRaises(Denied):
            self.project.insights.acknowledge_decisions("searcher", [other, other], sources="any")
        self.assertEqual(self.rows("searcher", sources="any"), [other])

    def test_legacy_cursor_cannot_hide_equal_time_batches_or_partial_holes(self):
        with patch("mizu.insights.now", return_value="2026-01-01T00:00:00+00:00"):
            for i in range(205):
                _reject(self.project, "reviewer", title=f"Finding {i}")
        ids = sorted(p.stem for p in (self.project.root / "decision-history").glob("*.json"))
        write_json(self.project.root / "decision-cursors/reviewer.json",
                   {"role": "reviewer", "acknowledged_at": "2099-01-01T00:00:00+00:00",
                    "acknowledged": ids[:100], "defer_offset": 12345})
        first = self.rows(limit=100)
        self.assertEqual([r["event"] for r in first], ids[:100])
        self.project.insights.acknowledge_decisions("reviewer", first[1:])
        second = self.rows(limit=100)
        self.assertEqual([r["event"] for r in second], [ids[0]] + ids[100:199])
        self.project.insights.acknowledge_decisions("reviewer", second)
        third = self.rows(limit=100)
        self.assertEqual([r["event"] for r in third], ids[199:])
        self.project.insights.acknowledge_decisions("reviewer", third)
        self.assertEqual(self.rows(limit=100), [])
        self.assertEqual(len(list((self.project.root / "decision-history").glob("*.json"))), 205)

    def test_full_and_delta_prompts_carry_same_tokens(self):
        _reject(self.project, "reviewer")
        ctx = self.context("reviewer")
        ctx.decision_events = self.rows()
        full = json.loads(prompt_for(ctx))["decision_events"]
        delta = json.loads(prompt_delta_for(ctx, []))["decision_events"]
        self.assertEqual(full, delta)
        self.assertEqual(len(full[0]["ack_token"]), 64)

    def test_topic_movement_while_handling_preserves_exact_receipt(self):
        item = _reject(self.project, "reviewer")
        row = self.rows()[0]
        def revise(ctx, prompt, profile=None):
            self.project.insights.revise(item["id"], source="reviewer", title="Improved",
                                         body="new evidence", base_snapshot=None)
            self.finish(ctx, ack_events=[row["ack_token"]])
        Engine(_trigger_config(self.config), driver=ScriptDriver(revise)).run(self.project, "reviewer")
        path = self.project.root / "decision-receipts/reviewer" / f"{row['ack_token']}.json"
        self.assertEqual(read_json(path)["event"], row["event"])
        # Old token is stale/undelivered in the next unit.
        with self.assertRaises(Denied):
            self.finish(self.context("reviewer"), ack_events=[row["ack_token"]])

    def test_failed_writer_publication_persists_no_selected_receipts(self):
        _reject(self.project, "worker")
        config = _trigger_config(self.config, role="worker")
        with patch.object(self.project.snapshots, "publish", side_effect=OSError("publication failed")):
            with self.assertRaises(OSError):
                Engine(config, driver=ScriptDriver(finish_with_decision_acks)).run(self.project, "worker")
        self.assertEqual(len(self.rows("worker")), 1)
        Engine(config, driver=ScriptDriver(finish_with_decision_acks)).run(self.project, "worker")
        self.assertEqual(self.rows("worker"), [])

    def test_receipts_survive_backup_restore(self):
        from mizu.project import Project
        from mizu.storage import backup, restore

        _reject(self.project, "reviewer")
        row = self.rows()[0]
        self.project.insights.acknowledge_decisions("reviewer", [row])
        self.project.set_control(paused=True)
        archive = self.root / "decision-receipts.tar.gz"
        backup(self.project, archive)
        restore(self.config, "restored", archive)
        restored = Project(self.config, "restored", read_only=True)
        self.assertEqual(restored.insights.decision_events("reviewer", ("reject",)), [])

    def test_receipt_corruption_and_unsafe_paths_fail_closed(self):
        # Safely read invalid content isolates that event; unsafe paths and
        # size-bound refusals still fail closed. Corrupt bytes are never
        # altered and acknowledgement of the suspect token is refused.
        _reject(self.project, "reviewer")
        row = self.rows()[0]
        path = self.project.root / "decision-receipts/reviewer" / f"{row['ack_token']}.json"
        write_json(path, {"role": "searcher"})
        before = path.read_bytes()
        self.assertEqual(self.rows(), [])
        _, diagnostics = self.project.insights.decision_events_with_diagnostics("reviewer", ("reject",))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual((diagnostics[0]["event"], diagnostics[0]["ack_token"]),
                         (row["event"], row["ack_token"]))
        self.assertIn(diagnostics[0]["reason"], ("malformed_json", "invalid_receipt", "invalid_timestamp"))
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(Denied):
            self.project.insights.acknowledge_decisions("reviewer", [row])
        self.assertEqual(path.read_bytes(), before)
        path.write_bytes(b"x" * 1025)
        with self.assertRaises(LimitExceeded):
            self.rows()
        path.unlink()
        target = self.root / "receipt.json"
        write_json(target, {"role": "reviewer"})
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            return  # Windows may not grant symlink creation
        with self.assertRaises((Denied, OSError)):
            self.rows()

    def test_corrupt_receipt_malformed_json_isolated_two_events(self):
        _reject(self.project, "reviewer", title="First")
        _reject(self.project, "reviewer", title="Second")
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        first = next(r for r in rows if r["title"] == "First")
        second = next(r for r in rows if r["title"] == "Second")
        path = self.project.root / "decision-receipts/reviewer" / f"{first['ack_token']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{invalid-json")
        before = path.read_bytes()
        delivered = self.rows()
        self.assertEqual([r["ack_token"] for r in delivered], [second["ack_token"]])
        self.assertNotIn(first["ack_token"], [r["ack_token"] for r in delivered])
        events, diagnostics = self.project.insights.decision_events_with_diagnostics(
            "reviewer", ("reject",))
        self.assertEqual([r["ack_token"] for r in events], [second["ack_token"]])
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["event"], first["event"])
        self.assertEqual(diagnostics[0]["ack_token"], first["ack_token"])
        self.assertEqual(diagnostics[0]["reason"], "malformed_json")
        self.assertNotIn("invalid-json", json.dumps(diagnostics))
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(Denied):
            self.project.insights.acknowledge_decisions("reviewer", [first])
        self.assertEqual(path.read_bytes(), before)
        self.project.insights.acknowledge_decisions("reviewer", [second])
        self.assertEqual([r["ack_token"] for r in self.rows()], [])
        # The suspect stays undelivered and unacknowledged; its bytes remain.
        _, troubled = self.project.insights.decision_events_with_diagnostics("reviewer", ("reject",))
        self.assertEqual([d["ack_token"] for d in troubled], [first["ack_token"]])
        self.assertEqual(path.read_bytes(), before)

    def test_corrupt_receipt_invalid_timestamp_isolated_two_events(self):
        _reject(self.project, "reviewer", title="First")
        _reject(self.project, "reviewer", title="Second")
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        first = next(r for r in rows if r["title"] == "First")
        second = next(r for r in rows if r["title"] == "Second")
        path = self.project.root / "decision-receipts/reviewer" / f"{first['ack_token']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps({"role": "reviewer", "event": first["event"],
                                     "ack_token": first["ack_token"],
                                     "acknowledged_at": "not-a-timestamp"}).encode())
        before = path.read_bytes()
        delivered = self.rows()
        self.assertEqual([r["ack_token"] for r in delivered], [second["ack_token"]])
        events, diagnostics = self.project.insights.decision_events_with_diagnostics(
            "reviewer", ("reject",))
        self.assertEqual([r["ack_token"] for r in events], [second["ack_token"]])
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["ack_token"], first["ack_token"])
        self.assertEqual(diagnostics[0]["reason"], "invalid_timestamp")
        self.assertNotIn("not-a-timestamp", json.dumps(diagnostics))
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(Denied):
            self.project.insights.acknowledge_decisions("reviewer", [first])
        self.assertEqual(path.read_bytes(), before)
        # Diagnostics alone admit a real event-triggered run and reach both
        # full/delta prompt payloads and the bounded run sidecar.
        self.project.insights.acknowledge_decisions("reviewer", [second])
        self.assertEqual(self.rows(), [])
        config = _trigger_config(self.config)
        self.project.config = config
        driver = ScriptDriver()
        result = Engine(config, driver=driver).run(self.project, "reviewer", event_trigger=True)
        self.assertEqual(result["status"], "completed")
        prompt = json.loads(driver.calls[0][1])
        self.assertEqual(prompt["decision_events"], [])
        self.assertEqual(len(prompt["decision_receipt_diagnostics"]), 1)
        delta = json.loads(prompt_delta_for(driver.calls[0][0], []))
        self.assertEqual(len(delta["decision_receipt_diagnostics"]), 1)
        run_dir = self.project.root / "runs" / result["run"]
        sidecar = read_json(run_dir / "decision-receipt-diagnostics.json")
        self.assertEqual(sidecar["diagnostics"], prompt["decision_receipt_diagnostics"])

    def test_unsafe_shared_receipt_directory_fails_closed(self):
        _reject(self.project, "reviewer")
        receipt_root = self.project.root / "decision-receipts"
        receipt_root.mkdir(parents=True, exist_ok=True)
        target = self.root / "receipt-target"
        target.mkdir()
        shared = receipt_root / "reviewer"
        try:
            shared.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            return  # Windows may not grant symlink creation.
        with self.assertRaises(Denied):
            self.rows()

    def test_systemic_receipt_read_error_fails_closed(self):
        _reject(self.project, "reviewer")
        row = self.rows()[0]
        self.project.insights.acknowledge_decisions("reviewer", [row])
        from mizu import insights as insights_module
        safe_read = insights_module.safe_read
        def failing_receipt_read(root, name, maximum):
            if name.startswith("decision-receipts/"):
                raise OSError("simulated receipt-store I/O failure")
            return safe_read(root, name, maximum)
        with patch("mizu.insights.safe_read", side_effect=failing_receipt_read):
            with self.assertRaises(OSError):
                self.rows()

    def test_corrupt_history_record_does_not_hide_good_event(self):
        _reject(self.project, "reviewer")
        good = self.rows()
        self.assertEqual(len(good), 1)
        path = self.project.root / "decision-history" / ("f" * 32 + ".json")
        corruptions = (
            b"not-json{",
            b"[]",
            json.dumps({"action": "reject", "created_at": "not-a-timestamp"}).encode(),
        )
        for payload in corruptions:
            with self.subTest(payload=payload[:24]):
                path.write_bytes(payload)
                self.assertEqual(self.rows(), good)
                self.assertEqual(path.read_bytes(), payload)

    def test_prompt_limit_does_not_hide_substantive_admission_behind_defers(self):
        with patch("mizu.insights.now", return_value="2026-01-01T00:00:00+00:00"):
            item = self.project.insights.submit(source="reviewer", title="Deferred", body="evidence",
                                                base_snapshot=self.project.snapshots.get()["id"])
            self.project.insights.decide(item["id"], "defer", "later", "wait", "test", expected_rev=1)
        _reject(self.project, "reviewer", title="Newer rejection")
        config = _trigger_config(self.config, events=("reject", "defer"))
        roles = dict(config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], decision_events_limit=1)
        config = dataclasses.replace(config, roles=roles)
        self.project.config = config
        self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), "reviewer"))
        driver = ScriptDriver()  # no implicit acknowledgement, including defer
        result = Engine(config, driver=driver).run(self.project, "reviewer", event_trigger=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(json.loads(driver.calls[0][1])["decision_events"][0]["action"], "defer")
        self.assertEqual(len(self.rows()), 2)

    def test_explicit_receipt_does_not_depend_on_check_wait_or_outcome(self):
        config = _trigger_config(self.config)
        roles = dict(config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], capabilities=(
            *roles["reviewer"].capabilities, "check_job"))
        config = dataclasses.replace(config, roles=roles)
        for outcome in ("continue", "blocked", "done", "wait"):
            _reject(self.project, "reviewer", title=outcome)
            row = self.rows()[0]
            def work(ctx, prompt, profile=None):
                self.finish(ctx, outcome=outcome, ack_events=[row["ack_token"]],
                            **({"wait_for_check": "a" * 64} if outcome == "wait" else {}))
            with patch("mizu.runtime.check_jobs.read", return_value={"role": "reviewer"}), \
                    patch("mizu.runtime.check_jobs.set_wait"):
                result = Engine(config, driver=ScriptDriver(work)).run(self.project, "reviewer")
            self.assertEqual(result["status"], "waiting" if outcome == "wait" else "completed")
            self.assertEqual(self.rows(), [])
