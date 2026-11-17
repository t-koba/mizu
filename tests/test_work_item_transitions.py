"""Versioned completion contracts: inline attachments and immutable replay."""
import dataclasses
import json
from unittest.mock import Mock, patch

from support import Fixture, bind_delivery, forget_ledger, history_write_json
from mizu import acceptance, storage, work_item_events
from mizu.completion_evidence import HOLDER_ATTESTATION, VERIFIED_SNAPSHOT, MAX_EVIDENCE_BYTES
from mizu.config import CompletionEvidence
from mizu.errors import Denied
from mizu.fs import canonical
from mizu.project import Project


class CompletionContractTests(Fixture):
    def configure(self, **kwargs):
        self.config = dataclasses.replace(self.config, completion_evidence=CompletionEvidence(**kwargs))
        self.project = Project(self.config, self.project.name)

    def item(self):
        return self.project.work_items.create(actor="reviewer", idempotency_key="completion",
            title="Complete with recorded evidence")["record"]["work_item"]

    def change(self, item, status, actor="worker", **kwargs):
        return self.project.work_items.transition(item["id"], expected_rev=item["rev"],
                                                  status=status, actor=actor, **kwargs)

    def attested(self):
        self.configure(by_role={"worker": HOLDER_ATTESTATION})
        doing = self.change(self.item(), "doing")
        done = self.change(doing, "done", evidence={"statement": "Checked the requested result",
                         "checks": [True, None, 3.5, {"name": "local"}], "note": "  preserved 日本語  "})
        return doing, done

    def test_role_override_closes_without_snapshot_and_preserves_attachment(self):
        self.configure(by_role={"worker": HOLDER_ATTESTATION})
        doing = self.change(self.item(), "doing")
        evidence = {"statement": "Reviewed the result", "other": [None, False, {"value": 2.5}]}
        store = self.project.work_items
        store.effective_verification = Mock(side_effect=AssertionError("No verification callback"))
        store.effective_verification_projection = store.effective_verification
        with patch.object(store.snapshots, "get", side_effect=AssertionError("No snapshot")), \
             patch.object(store.snapshots, "binding", side_effect=AssertionError("No snapshot")):
            done = self.change(doing, "done", evidence=evidence)
            view = store.explain(done["id"])
            self.assertEqual(view["item"], done)
            self.assertEqual(view["history"][-1], done)
            self.assertEqual(store.get(done["id"]), done)
            self.assertEqual(store.page_closed_readonly()["items"][0]["completion_policy"], HOLDER_ATTESTATION)
        self.assertEqual(done["completion_policy"], HOLDER_ATTESTATION)
        self.assertEqual(done["completion_evidence"], evidence)
        for key in ("snapshot_id", "verification", "delivered_tree", "verification_scope"):
            self.assertIsNone(done[key])
        store.effective_verification.assert_not_called()

    def test_default_and_other_roles_remain_strict(self):
        for overrides in ({}, {"worker": HOLDER_ATTESTATION}):
            self.configure(by_role=overrides)
            item = self.item()
            with self.assertRaisesRegex(Denied, "verified_snapshot_v1:.*prior exact snapshot"):
                self.change(item, "done", actor="reviewer")
            with self.assertRaisesRegex(Denied, "verified_snapshot_v1: inline evidence is not accepted"):
                self.change(item, "done", actor="reviewer", evidence={"statement": "Not authorized by policy"})

    def test_attestation_requires_holder_statement_and_null_snapshot(self):
        self.configure(default=HOLDER_ATTESTATION)
        item = self.item()
        evidence = {"statement": "Checked"}
        with self.assertRaisesRegex(Denied, "holder_attestation_v1:.*current holder"):
            self.change(item, "done", evidence=evidence)
        doing = self.change(item, "doing")
        for bad in (None, {}, {"statement": "  "}, {"statement": 1}, []):
            with self.subTest(bad=bad), self.assertRaisesRegex(Denied, "holder_attestation_v1: evidence"):
                self.change(doing, "done", evidence=bad)
        with self.assertRaisesRegex(Denied, "holder_attestation_v1:.*current holder"):
            self.change(doing, "done", actor="reviewer", evidence=evidence)
        with self.assertRaisesRegex(Denied, "holder_attestation_v1:.*must be null"):
            self.change(doing, "done", snapshot_id="a" * 64, evidence=evidence)
        with self.assertRaisesRegex(Denied, "holder_attestation_v1:.*only for done"):
            self.change(doing, "open", evidence=evidence)
        self.assertEqual(self.project.work_items.get(item["id"]), doing)

    def test_attestation_correction_requires_reason_and_records_responsibility(self):
        self.configure(by_role={"worker": HOLDER_ATTESTATION})
        dropped = self.change(self.item(), "dropped", actor="reviewer", reason="Superseded")
        evidence = {"statement": "Confirmed completion instead"}
        with self.assertRaisesRegex(Denied, "holder_attestation_v1:.*nonempty reason"):
            self.change(dropped, "done", evidence=evidence)
        done = self.change(dropped, "done", reason="Correct the disposition", evidence=evidence)
        self.assertEqual(done["actor"], "worker")
        self.assertIsNone(done["holder"])
        self.assertEqual(self.project.work_items.explain(done["id"])["history"][-2], dropped)
        tampered = {**done, "reason": None}
        self.assert_replay_denied(tampered)

    def assert_replay_denied(self, record):
        history_write_json(self.project.root / "work-items" / record["id"] / f'{record["rev"]}.json', record)
        with self.assertRaises(Denied):
            self.project.work_items.get(record["id"])

    def test_replay_refuses_policy_evidence_actor_and_state_tampering(self):
        _, done = self.attested()
        for changes in ({"completion_policy": "unknown_v1"},
                        {"completion_policy": VERIFIED_SNAPSHOT},
                        {"completion_evidence": {"statement": " "}},
                        {"completion_evidence": None}, {"actor": "reviewer"},
                        {"snapshot_id": "a" * 64}, {"verification": {}},
                        {"delivered_tree": "b" * 64}, {"verification_scope": "c" * 64},
                        {"status": "open"}, {"status": "dropped"}):
            with self.subTest(changes=changes):
                self.assert_replay_denied({**done, **changes})
        for key in ("completion_policy", "completion_evidence"):
            tampered = dict(done)
            del tampered[key]
            self.assert_replay_denied(tampered)
        history_write_json(self.project.root / "work-items" / done["id"] / f'{done["rev"]}.json', done)
        self.assertEqual(self.project.work_items.get(done["id"]), done)

    def test_replay_uses_recorded_contract_not_new_policy_or_smaller_budget(self):
        _, done = self.attested()
        self.configure(maximum_evidence_bytes=1)
        forget_ledger(self.project.root)
        self.assertEqual(self.project.work_items.get(done["id"]), done)
        self.assertEqual(self.project.work_items.explain(done["id"])["history"][-1], done)
        self.project.work_items.audit()
        report = storage.audit(self.project)
        self.assertEqual(report["work_item_events_skipped"], 0)
        self.assertEqual(report["references"]["work_item_snapshots"], 0)
        page = work_item_events.pending(self.project.work_items, "reviewer", ("done",), sources="any")
        self.assertEqual(page["events"][0]["id"], done["id"])
        self.assertEqual(page["events"][0]["completion_policy"], HOLDER_ATTESTATION)
        self.assertIsNone(page["events"][0]["verification"])

    def test_canonical_byte_budget_depth_and_finite_json(self):
        evidence = {"statement": "é" * 20}
        size = len(canonical(evidence))
        self.configure(default=HOLDER_ATTESTATION, maximum_evidence_bytes=size - 1)
        doing = self.change(self.item(), "doing")
        with self.assertRaisesRegex(Denied, "holder_attestation_v1: evidence exceeds"):
            self.change(doing, "done", evidence=evidence)
        nested = []
        for _ in range(33):
            nested = [nested]
        cycle = []
        cycle.append(cycle)
        for invalid in ({"statement": "Checked", "x": nested},
                        {"statement": "Checked", "x": cycle},
                        {"statement": "Checked", "x": float("nan")},
                        {"statement": "Checked", "x": float("inf")},
                        {"statement": "Checked", "x": (1, 2)},
                        {"statement": "Checked", 1: "not a JSON key"}):
            with self.assertRaisesRegex(Denied, "holder_attestation_v1: evidence must be finite JSON"):
                self.change(doing, "done", evidence=invalid)
        self.configure(default=HOLDER_ATTESTATION, maximum_evidence_bytes=size)
        done = self.change(doing, "done", evidence=evidence)
        oversized = {**done, "completion_evidence": {"statement": "x" * MAX_EVIDENCE_BYTES}}
        self.assert_replay_denied(oversized)
        self.assert_replay_denied({**done, "completion_evidence": {"statement": "Checked", "x": nested}})
        path = self.project.root / "work-items" / done["id"] / f'{done["rev"]}.json'
        # Nonfinite JSON must fail closed even before contract replay.
        path.write_text(json.dumps({**done, "completion_evidence": {"statement": "Checked", "x": float("nan")}}))
        forget_ledger(self.project.root)
        with self.assertRaises(Denied):
            self.project.work_items.get(done["id"])

    def test_inherited_delivery_remains_history_not_completion_proof(self):
        self.configure(by_role={"worker": HOLDER_ATTESTATION})
        item = self.item()
        snapshot = self.project.snapshots.get()  # Deliberately no passing verification.
        binding = bind_delivery(self.project, item["id"], snapshot["id"])
        with patch.object(self.project.work_items, "effective_verification_projection",
                          side_effect=AssertionError("Attestation must not select verification")):
            done = self.change(binding, "done", evidence={"statement": "Manual check"})
            with patch.object(self.project.snapshots, "binding", wraps=self.project.snapshots.binding) as manifest:
                self.assertEqual(self.project.work_items.explain(item["id"])["item"], done)
                manifest.assert_called_once_with(snapshot["id"])
        self.assertEqual(done["delivery"], binding["delivery"])
        self.assertIsNone(done["snapshot_id"])
        self.assertIsNone(done["verification"])
        report = storage.audit(self.project)
        self.assertEqual(report["work_item_events_skipped"], 0)
        self.assertEqual(report["references"]["work_item_snapshots"], 1)

    def test_strict_correction_needs_retained_acceptance_and_legacy_replays(self):
        store = self.project.snapshots
        captured = store.capture_files(self.project.workspace)
        snapshot = store.create(captured, goal="Goal", state="State", run="run-1", outcome="done",
            summary="Embedded proof", verification={"passed": True, "code_digest": captured["code_digest"]})
        store.publish(snapshot)
        binding = bind_delivery(self.project, self.item()["id"], snapshot["id"])
        dropped = self.change(binding, "dropped", reason="Mistaken disposition")
        with self.assertRaisesRegex(Denied, "verified_snapshot_v1:.*passing digest-bound verification"):
            self.change(dropped, "done", snapshot_id=snapshot["id"])
        # Retain a synthetic valid local acceptance receipt, independent of the embedded proof.
        self.config = dataclasses.replace(self.config,
            sandbox=dataclasses.replace(self.config.sandbox, check_reuse=True))
        self.project.config = self.config
        scope = acceptance.scope(self.project)
        self.assertIsNotNone(scope)
        receipt_id = acceptance._retain(self.project.root, {"schema": 1,
            "code_digest": snapshot["code_digest"], "scope": scope, "passed": True,
            "producer": {"kind": "verify", "id": "a" * 32, "run": "a" * 32},
            "commands": [{"id": "b" * 32, "exit_code": 0, "reason": "exited"}]})
        done = self.change(dropped, "done", snapshot_id=snapshot["id"])
        self.assertEqual(done["completion_policy"], VERIFIED_SNAPSHOT)
        self.assertIsNone(done["completion_evidence"])
        self.assertEqual(done["verification"]["receipt_id"], receipt_id)
        self.assertEqual(self.project.work_items.explain(done["id"])["item"], done)
        legacy = dict(done)
        del legacy["completion_policy"], legacy["completion_evidence"]
        history_write_json(self.project.root / "work-items" / done["id"] / f'{done["rev"]}.json', legacy)
        self.configure(default=HOLDER_ATTESTATION)
        self.assertEqual(self.project.work_items.get(done["id"]), legacy)
        self.project.work_items.audit()

    def test_tool_attaches_evidence_and_rejects_caller_actor_or_policy(self):
        self.configure(by_role={"worker": HOLDER_ATTESTATION})
        doing = self.change(self.item(), "doing")
        ctx = self.context("worker")
        args = {"id": doing["id"], "expected_rev": doing["rev"], "status": "done",
                "evidence": {"statement": "Checked end to end", "checks": [{"passed": True}], "count": 1}}
        for field in ("actor", "completion_policy"):
            with self.assertRaises(Denied):
                ctx.handle("work_items_transition", {**args, field: "reporter"})
        done = ctx.handle("work_items_transition", args)
        self.assertEqual(done["actor"], "worker")
        self.assertEqual(done["completion_policy"], HOLDER_ATTESTATION)
        self.assertEqual(done["completion_evidence"], args["evidence"])
        self.assertEqual(ctx.handle("work_items_read", {"id": doing["id"]})["history"][-1], done)
