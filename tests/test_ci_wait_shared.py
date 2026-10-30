"""CI source cutover; fake status adapter, no network/deployment changes."""
import json
from pathlib import Path as _Path
from unittest.mock import patch

from test_ci_waits import CiWaitFixture, SHA
from support import ScriptDriver
from mizu import ci_waits, protocol, shared_records
from mizu.config import load
from mizu.errors import ConfigError, Denied, LimitExceeded
from mizu.fs import MAX_FRAME, PREVIEW_BYTES, canonical
from mizu.project import Project
from mizu.runtime import Engine, _role_events_due
from mizu.shared_records import RECORD_BYTES
from mizu.storage import backup, restore
from mizu.unit_prompts import prompt_delta_for, prompt_for


class SharedCiWaitTests(CiWaitFixture):
    def setUp(self):
        super().setUp()
        self.project = Project(self.config, "sample")
        self.store = self.project.work_items

    def page(self, role="worker"):
        return ci_waits.delivery(self.store, role)

    def pending(self, role="worker", limit=10):
        return ci_waits.pending(self.store, role, limit=limit)

    def context(self, role="worker"):
        context = self.reader(capabilities=("finish", "vcs_read", "work_items_read"))
        context.role = self.config.roles[role]
        context.wait_events, context.ci_wait_manifest = self.page(role)
        return context

    def complete(self, context, tokens=None, **kwargs):
        args = {"outcome": "wait", "summary": "Handled CI evidence", "state": "Next step.", **kwargs}
        if tokens is not None:
            args["ack_events"] = tokens
        ci_waits.complete(context, args)

    def receipts(self):
        return list((self.project.root / "shared-record-receipts").rglob("*.json"))

    def test_default_route_uses_explicit_receipts_and_ignores_legacy_ack_files(self):
        original = self.file.read_text()
        example = (_Path(__file__).resolve().parents[1] / "config/config.example.toml").read_text()
        self.assertNotIn("ci_wait_shared_records", original)
        self.assertNotIn("ci_wait_shared_records", example)
        self.assertNotIn("ci_wait_shared_records", self.config.vcs)
        legacy = original.replace("[vcs]", "[vcs]\nci_wait_shared_records = false", 1)
        self.file.write_text(legacy)
        with self.assertRaises(ConfigError):
            load(self.file)
        self.file.write_text(original)

        item = self.block()
        self.poll()
        context = self.context()
        event = context.wait_events[0]
        self.assertIn("ack_token", event)
        self.assertEqual(self.pending(), context.wait_events)
        legacy_path = ci_waits._path(self.store, item, "ack")
        legacy_ack = {"role": "worker", "evidence_digest": event["evidence_digest"],
                      "acknowledged_at": "2025-01-01T00:00:00+00:00"}
        legacy_path.write_bytes(canonical(legacy_ack))
        self.assertEqual(self.pending(), context.wait_events)
        self.assertEqual(self.receipts(), [])
        self.complete(context, [])
        self.assertEqual(self.pending(), context.wait_events)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(legacy_path.read_bytes(), canonical(legacy_ack))

    def test_payload_time_order_holder_and_readonly_wake(self):
        items = [self.block(str(i)) for i in range(3)]
        other = self.block("other", actor="reviewer")
        self.poll()
        self.poll()
        with patch.object(shared_records, "atomic_write", side_effect=AssertionError("read-only")), \
             patch.object(ci_waits, "sync_dir", side_effect=AssertionError("read-only")):
            pending = self.pending()
            self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), "worker"))
        self.assertFalse((self.project.root / "shared-records").exists())
        events, manifest = self.page()
        expected = sorted(items, key=lambda i: (i["updated_at"], i["id"]))
        self.assertEqual([e["work_item"] for e in events], [i["id"] for i in expected])
        self.assertEqual(events, pending)
        self.assertNotIn(other["id"], [e["work_item"] for e in events])
        for item, event, entry in zip(expected, events, manifest):
            envelope, computed = ci_waits.shared_store(self.store).get(entry.record_id)
            self.assertEqual(envelope["created_at"], item["updated_at"])
            self.assertEqual(envelope["payload"], ci_waits._evidence(self.store, item))
            self.assertEqual(event["trust"], "external-untrusted")
            self.assertEqual((event["record_digest"], event["ack_token"]), (computed, entry.ack_token))
            self.assertEqual(entry.role, "worker")
        self.assertLessEqual(ci_waits._page_bytes(events), MAX_FRAME)
        self.assertEqual(ci_waits.shared_store(self.store).policies["ci.wait"].maximum, RECORD_BYTES)

    def test_omitted_empty_partial_and_replay_are_explicit(self):
        for i in range(3):
            self.block(str(i))
        self.poll()
        self.poll()
        context = self.context()
        self.complete(context)
        self.complete(context, [])
        self.assertEqual(len(self.pending()), 3)
        token = context.wait_events[1]["ack_token"]
        self.complete(context, [token])
        self.complete(context, [token])
        self.assertEqual(len(self.pending()), 2)
        self.assertEqual(len(self.receipts()), 1)
        self.assertFalse(any((self.project.root / "evidence" / "ci-waits").glob("*.ack.json")))
        restarted = Project(self.config, "sample")
        replay, _ = ci_waits.delivery(restarted.work_items, "worker")
        self.assertEqual(replay, self.pending())
        self.assertNotIn(token, [e["ack_token"] for e in replay])

    def test_finish_rejects_unknown_duplicate_foreign_and_stale_tokens_before_intent(self):
        item = self.block()
        self.poll()
        context = self.context()
        token = context.wait_events[0]["ack_token"]
        args = {"outcome": "wait", "summary": "CI handled", "state": "Next step"}
        for tokens in (["f" * 64], [token, token]):
            with self.assertRaises(Denied):
                context._op_finish({**args, "ack_events": tokens})
            self.assertIsNone(context.finished)
        role = context.role
        context.role = self.config.roles["reviewer"]
        with self.assertRaises(Denied):
            context._op_finish({**args, "ack_events": [token]})
        context.role = role
        self.store.transition(item["id"], expected_rev=2, status="doing", actor="reviewer")
        with self.assertRaises(Denied):
            context._op_finish({**args, "ack_events": [token]})
        self.assertIsNone(context.finished)
        self.assertEqual(self.receipts(), [])
        with self.assertRaises(Denied):
            self.complete(context, [token])
        self.assertEqual(self.receipts(), [])

    def test_changed_revision_holder_or_evidence_refuses_whole_selected_batch(self):
        a, b = self.block("a"), self.block("b")
        self.poll()
        context = self.context()
        tokens = [e["ack_token"] for e in context.wait_events]
        path = ci_waits._path(self.store, b)
        raw = path.read_bytes()
        for key, value in (("wait_digest", "a" * 64), ("holder", "reviewer"), ("sha", "d" * 40),
                           ("checks", self.rows(sha="d" * 40)), ("checks", self.rows() + self.rows()),
                           ("conclusion", "failure"), ("trust", "host")):
            changed = json.loads(raw)
            changed[key] = value
            path.write_bytes(canonical(changed))
            with self.subTest(key=key, value=value), self.assertRaises(Denied):
                self.complete(context, tokens)
            self.assertEqual(self.receipts(), [])
        path.write_bytes(raw)
        self.store.transition(b["id"], expected_rev=2, status="open", actor="worker")
        self.store.transition(b["id"], expected_rev=3, status="blocked", actor="worker", wait=self.wait)
        with self.assertRaises(Denied):
            self.complete(context, tokens)
        self.assertEqual(self.receipts(), [])
        self.assertEqual([e["work_item"] for e in self.pending()], [a["id"]])

    def test_publication_and_full_ledger_authority_are_rechecked(self):
        item = self.block()
        self.poll()
        context = self.context()
        tokens = [e["ack_token"] for e in context.wait_events]
        self.publication.path("receipt").unlink()
        with self.assertRaises(Denied):
            self.page()
        with self.assertRaises(Denied):
            self.complete(context, tokens)
        for error in (Denied("history corrupt"), OSError("history unavailable")):
            with patch.object(self.store, "_history_revisions", side_effect=error):
                with self.assertRaises(type(error)):
                    self.page()
                with self.assertRaises(type(error)):
                    self.complete(context, tokens)
        self.assertEqual(self.receipts(), [])
        self.assertFalse(ci_waits._path(self.store, item, "ack").exists())

    def test_corrupt_source_record_or_receipt_never_hides_another_item(self):
        a, b = self.block("a"), self.block("b")
        self.poll()
        context = self.context()
        entries = {e["work_item"]: entry for e, entry in zip(context.wait_events, context.ci_wait_manifest)}
        shared = ci_waits.shared_store(self.store)
        record = self.store.root / shared._record_name(entries[a["id"]].record_id)
        source = ci_waits._path(self.store, a)
        for path in (source, record):
            raw = path.read_bytes()
            path.write_bytes(b'not json')
            self.assertEqual([e["work_item"] for e in self.pending()], [b["id"]])
            with self.assertRaises(Denied):
                self.complete(context, [entry.ack_token for entry in entries.values()])
            self.assertEqual(path.read_bytes(), b'not json')
            path.write_bytes(raw)
        receipt = self.store.root / shared._receipt_name("worker", entries[a["id"]].record_id)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        for raw in (b'not json', canonical({"role": "reviewer"})):
            receipt.write_bytes(raw)
            self.assertEqual([e["work_item"] for e in self.pending()], [b["id"]])
            with self.assertRaises(Denied):
                self.complete(context, [entries[a["id"]].ack_token])
            self.assertEqual(receipt.read_bytes(), raw)
        self.complete(context, [entries[b["id"]].ack_token])
        self.assertEqual(self.pending(), [])

    def test_status_is_holder_only_readonly_bounded_and_surfaces_corrupt_receipts(self):
        item = self.block()
        self.poll()
        context = self.context()
        entry = context.ci_wait_manifest[0]
        shared = ci_waits.shared_store(self.store)
        path = self.store.root / shared._receipt_name("worker", entry.record_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'not json')
        def status(role="worker"):
            return ci_waits.status_diagnostics(self.store, role, self.receipt, {"checks": self.rows()})
        with patch.object(shared_records, "atomic_write", side_effect=AssertionError("write")), \
             patch.object(ci_waits, "sync_dir", side_effect=AssertionError("sync")):
            result = status()
            self.assertIsNone(status("reviewer"))
        self.assertEqual(result["items"][0]["acknowledgement"], "corrupt")
        self.assertLessEqual(len(result["observed_checks"]), ci_waits.MAX_DIAGNOSTIC_ROWS)
        path.write_bytes(b'x' * (shared_records.RECEIPT_BYTES + 1))
        with self.assertRaises(LimitExceeded):
            status()
        path.unlink()
        try:
            path.symlink_to(ci_waits._path(self.store, item))
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaises((Denied, OSError)):
                status()
            path.unlink()
        # Legacy corruption is ignored; only shared receipts have authority.
        ci_waits._path(self.store, item, "ack").write_bytes(b'not json')
        self.assertNotIn("acknowledgement", status()["items"][0])
        self.assertEqual(len(self.pending()), 1)

    def test_eligibility_and_receipts_are_filtered_before_count_bound(self):
        items = [self.block(str(i)) for i in range(12)]
        for _ in range(6):
            self.poll()
        context = self.context()
        self.assertEqual(len(context.wait_events), 10)
        self.complete(context, [e["ack_token"] for e in context.wait_events])
        remaining = self.pending(limit=1)
        self.assertEqual(len(remaining), 1)
        self.assertNotIn(remaining[0]["work_item"], [e["work_item"] for e in context.wait_events])
        current = self.store.get(remaining[0]["work_item"])
        self.store.transition(current["id"], expected_rev=2, status="doing", actor="worker")
        self.assertEqual(len(self.pending(limit=1)), 1)
        for limit in (0, 101, True):
            with self.assertRaises(Denied):
                self.pending(limit=limit)
        self.assertEqual(len(items), 12)

    def test_unicode_page_measurement_uses_prompt_serialization(self):
        names = [str(i).zfill(2) + "😀" * 4 for i in range(64)]
        for i in range(4):
            self.block(str(i), wait={**self.wait, "checks": names})
        rows = [{"check": c, "state": "failure", "sha": SHA, "run": "😀" * 128} for c in names]
        self.poll(rows)
        self.poll(rows)
        context = self.context()
        # Four non-ASCII rows exceed the old 128 KiB aggregate but fit the
        # existing generic 1 MiB frame; JSON prompt escaping is measured exactly.
        self.assertEqual(len(context.wait_events), 4)
        self.assertGreater(len(canonical(context.wait_events)), PREVIEW_BYTES)
        self.assertLessEqual(ci_waits._page_bytes(context.wait_events), MAX_FRAME)
        # Prompt-accurate bound, not canonical list framing.
        self.assertLessEqual(
            len(json.dumps({"wait_events": context.wait_events}, ensure_ascii=True).encode()),
            MAX_FRAME)
        self.complete(context, [e["ack_token"] for e in context.wait_events])
        replay, _ = self.page()
        self.assertEqual(len(replay) + len(context.wait_events), 4)
        self.assertLessEqual(ci_waits._page_bytes(replay), MAX_FRAME)

    def test_ten_large_rows_fit_generic_frame_without_smaller_aggregate_cap(self):
        # Ten ~25 KiB valid rows exceed 128 KiB in aggregate while fitting the
        # existing generic 1 MiB frame: no smaller aggregate cap may cut them.
        names = [str(i).zfill(2) + "a" * 4 for i in range(64)]
        for i in range(10):
            self.block(str(i), wait={**self.wait, "checks": names})
        rows = [{"check": c, "state": "failure", "sha": SHA, "run": "r" * 128} for c in names]
        for _ in range(5):
            self.poll(rows)
        context = self.context()
        self.assertEqual(len(context.wait_events), 10)
        self.assertGreater(len(canonical(context.wait_events)), PREVIEW_BYTES)
        self.assertLessEqual(ci_waits._page_bytes(context.wait_events), MAX_FRAME)
        self.assertLessEqual(
            len(json.dumps({"wait_events": context.wait_events}, ensure_ascii=True).encode()),
            MAX_FRAME)
        # Per-source evidence stays bounded at 128 KiB each.
        for event in context.wait_events:
            payload = {k: v for k, v in event.items()
                       if k not in ("record_id", "record_digest", "ack_token")}
            self.assertLessEqual(len(canonical(payload)), PREVIEW_BYTES)
        # All ten fit as a contiguous oldest-first page under the generic bound.
        self.complete(context, [e["ack_token"] for e in context.wait_events])
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.receipts()), 10)

    def test_partial_record_and_receipt_writes_replay_after_restart(self):
        for i in range(2):
            self.block(str(i))
        self.poll()
        original = shared_records.atomic_write
        writes = 0
        def fail_second(path, raw, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("interrupted create")
            return original(path, raw, **kwargs)
        with patch.object(shared_records, "atomic_write", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.page()
        self.project = Project(self.config, "sample")
        self.store = self.project.work_items
        context = self.context()
        self.assertEqual(len(context.wait_events), 2)
        writes = 0
        tokens = [e["ack_token"] for e in context.wait_events]
        with patch.object(shared_records, "atomic_write", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.complete(context, tokens)
        self.assertEqual(len(self.pending()), 1)
        self.complete(context, tokens)
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.receipts()), 2)

    def test_post_rename_failure_replays_without_repair(self):
        self.block()
        self.poll()
        original = shared_records.atomic_write
        def fail_after(path, raw, **kwargs):
            original(path, raw, **kwargs)
            raise OSError("post rename failure")
        with patch.object(shared_records, "atomic_write", side_effect=fail_after):
            with self.assertRaises(OSError):
                self.page()
        context = self.context()
        with patch.object(shared_records, "atomic_write", side_effect=fail_after):
            with self.assertRaises(OSError):
                self.complete(context, [context.wait_events[0]["ack_token"]])
        before = [path.read_bytes() for path in self.receipts()]
        self.complete(context, [context.wait_events[0]["ack_token"]])
        self.assertEqual([path.read_bytes() for path in self.receipts()], before)
        self.assertEqual(self.pending(), [])

    def test_backup_restore_retains_records_receipts_and_unselected_replay(self):
        self.block("a")
        self.block("b")
        self.poll()
        context = self.context()
        self.complete(context, [context.wait_events[0]["ack_token"]])
        self.project.set_control(paused=True)
        archive = self.root / "shared-backup.tar.gz"
        backup(self.project, archive)
        restore(self.config, "restored", archive)
        restored = Project(self.config, "restored", read_only=True)
        self.assertEqual(ci_waits.pending(restored.work_items, "worker"), self.pending())
        shared = ci_waits.shared_store(restored.work_items)
        for entry in context.ci_wait_manifest:
            self.assertEqual(shared.get(entry.record_id), ci_waits.shared_store(self.store).get(entry.record_id))
        first = context.ci_wait_manifest[0]
        self.assertTrue(shared.received("worker", first.record_id, first.record_digest))

    def test_completion_gates_and_unselected_rows_write_no_receipts(self):
        self.block()
        self.poll()
        context = self.context()
        tokens = [context.wait_events[0]["ack_token"]]
        self.complete(context, tokens, outcome="bounded")
        context.bound_hit = "requests"
        self.complete(context, tokens)
        context.bound_hit = None
        self.complete(context, tokens, wait_for_check="another-check")
        self.complete(context, [])
        self.assertEqual(self.receipts(), [])
        self.assertEqual(len(self.pending()), 1)

    def test_default_explicit_receipts_successful_selection_and_prompt_contract(self):
        item = self.block(actor="reviewer")
        self.poll()
        engine = Engine(self.config, driver=ScriptDriver())
        for _ in range(2):
            result = engine.run(self.project, "reviewer")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(self.pending("reviewer")), 1)
            context, prompt, _ = engine.driver.calls[-1]
            self.assertIn("ci_wait_delivery", json.loads(prompt))
            self.assertIn("ci_wait_delivery", json.loads(prompt_delta_for(context, [])))
        self.assertEqual(self.receipts(), [])
        def selected(context, prompt, profile):
            event = json.loads(prompt)["wait_events"][0]
            context.handle("finish", {"outcome": "wait", "summary": "CI inspected", "state": "Next step",
                                      "ack_events": [event["ack_token"]]})
            self.assertEqual(self.receipts(), [])  # intent alone is not completion
        engine.driver = ScriptDriver(selected)
        result = engine.run(self.project, "reviewer")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.pending("reviewer"), [])
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(self.store.get(item["id"]), item)
        self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), "reviewer"))
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")

    def test_prompt_policy_matches_always_explicit_ci_receipts(self):
        self.block()
        self.poll()
        context = self.context()
        self.assertIn("ack_token", context.wait_events[0])
        for prompt in (json.loads(prompt_for(context)),
                       json.loads(prompt_delta_for(context, []))):
            contract = prompt["ci_wait_delivery"]
            self.assertIn("Select only handled tokens", contract)
            self.assertIn("omitted/empty/partial", contract)
            self.assertIn("Finishing alone", contract)
        policy = (_Path(__file__).resolve().parents[1] / "policies/shared.md").read_text()
        self.assertIn("always carry identity-bound `ack_token`s", policy)
        self.assertIn("Finishing alone never handles CI rows", policy)
        finish_text = protocol.DEFINITIONS["finish"][0]
        self.assertIn("always use explicit, identity-bound ack_tokens", finish_text)
        self.assertNotIn("legacy CI rows have no tokens", finish_text)

    def test_failed_model_run_cannot_acknowledge(self):
        self.block(actor="reviewer")
        self.poll()
        def failed(context, prompt, profile):
            raise OSError("driver failed before completion")
        with self.assertRaisesRegex(OSError, "driver failed"):
            Engine(self.config, driver=ScriptDriver(failed)).run(self.project, "reviewer")
        self.assertEqual(len(self.pending("reviewer")), 1)
        self.assertEqual(self.receipts(), [])

    def test_source_change_after_finish_intent_prevents_completion_receipt(self):
        item = self.block(actor="reviewer")
        self.poll()
        def changed(context, prompt, profile):
            event = json.loads(prompt)["wait_events"][0]
            context.handle("finish", {"outcome": "wait", "summary": "CI inspected", "state": "Next step",
                                      "ack_events": [event["ack_token"]]})
            self.store.transition(item["id"], expected_rev=2, status="doing", actor="worker")
        result = Engine(self.config, driver=ScriptDriver(changed)).run(self.project, "reviewer")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.pending("reviewer"), [])  # retained evidence is not source eligibility

    def test_noncanonical_equal_values_cannot_change_projected_identity(self):
        self.block()
        self.poll()
        context = self.context()
        entry = context.ci_wait_manifest[0]
        shared = ci_waits.shared_store(self.store)
        path = self.store.root / shared._record_name(entry.record_id)
        record = json.loads(path.read_bytes())
        record["subject"]["rev"] = float(record["subject"]["rev"])
        path.write_bytes(canonical(record))
        self.assertEqual(self.page(), ([], ()))
        with self.assertRaises(Denied):
            self.complete(context, [entry.ack_token])
        self.assertEqual(self.receipts(), [])
