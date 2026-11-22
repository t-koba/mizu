"""Adapter-owned CI reachability is holder-actionable liveness, never auto-resolution."""
import contextlib
import sqlite3
import threading
from unittest import TestCase
from unittest.mock import patch

from mizu import ci_waits, vcs
from mizu.ci_observation import POLL_STATE_FILE
from mizu.ci_requests import CiRequests, request_id
from mizu.errors import Busy, Denied
from mizu.fs import lock, write_json
from mizu.ledger import NAME as LEDGER_NAME
from mizu.project import Project
from mizu.protocol import DEFINITIONS
from support import index_items
from test_ci_waits import CiWaitFixture, SHA

OTHER = "c" * 40
REASON = "Pinned commit is outside the configured CI target"


class ReachabilityValidatorTests(TestCase):
    def answer(self, **changes):
        data = {"sha": SHA, "answer": "yes", "reason": ""}
        data.update(changes)
        return data

    def test_acceptable_answers_normalize_the_reason(self):
        self.assertEqual(vcs.parse_ci_reachability(self.answer(), sha=SHA),
                         {"answer": "yes", "reason": ""})
        self.assertEqual(vcs.parse_ci_reachability({"sha": SHA, "answer": "unknown"}, sha=SHA),
                         {"answer": "unknown", "reason": ""})
        self.assertEqual(vcs.parse_ci_reachability(
            self.answer(answer="no", reason=REASON), sha=SHA),
            {"answer": "no", "reason": REASON})

    def test_bad_sha_echo_and_unknown_answer_are_denied(self):
        with self.assertRaisesRegex(Denied, "echo"):
            vcs.parse_ci_reachability(self.answer(sha=OTHER), sha=SHA)
        with self.assertRaisesRegex(Denied, "answer"):
            vcs.parse_ci_reachability(self.answer(answer="maybe"), sha=SHA)
        with self.assertRaisesRegex(Denied, "revision"):
            vcs.parse_ci_reachability(self.answer(), sha="not-a-revision")

    def test_no_without_a_reason_is_denied(self):
        for reason in (None, ""):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(Denied, "reason"):
                    answer = self.answer(answer="no")
                    if reason is not None:
                        answer["reason"] = reason
                    vcs.parse_ci_reachability(answer, sha=SHA)

    def test_oversized_and_terminal_control_reasons_are_denied(self):
        for reason in ("r" * (vcs.CI_REACHABILITY_REASON_MAX + 1),
                       "line\nbreak", "nul\x00", "\x1b[31mred"):
            with self.subTest(reason=repr(reason)):
                with self.assertRaisesRegex(Denied, "reason"):
                    vcs.parse_ci_reachability(
                        self.answer(answer="no", reason=reason), sha=SHA)

    def test_query_is_internal_one_adapter_call_and_not_a_role_op(self):
        settings = {"command": ["adapter"], "timeout_seconds": 5, "max_bytes": 1024}
        response = {"sha": SHA, "answer": "no", "reason": REASON}
        with patch("mizu.vcs.invoke", return_value=response) as invoke:
            result = vcs.ci_reachable(settings, SHA)
        self.assertEqual(result, {"answer": "no", "reason": REASON})
        invoke.assert_called_once_with(settings, {"op": "ci_reachable", "sha": SHA})
        self.assertEqual(vcs.CI_REACHABILITY_OP, "ci_reachable")
        self.assertNotIn(vcs.CI_REACHABILITY_OP, vcs.READ_OPS)
        self.assertNotIn(vcs.CI_REACHABILITY_OP,
                         DEFINITIONS["vcs_read"][1]["properties"]["op"]["enum"])


class UnreachableCiWaitTests(CiWaitFixture):
    def setUp(self):
        super().setUp()
        self.item = self.block()

    def answered(self, answer, reason=""):
        return patch("mizu.ci_waits.vcs.ci_reachable",
                     return_value={"answer": answer, "reason": reason})

    def live(self, item_id=None):
        rows = self.store.blocked_liveness_readonly()["items"]
        if item_id is None:
            return rows[0]
        row = next((r for r in rows if r["id"] == item_id), None)
        self.assertIsNotNone(row, f"blocked liveness has no row for {item_id}")
        return row

    def assert_pending(self):
        self.assertEqual(self.live()["state"], "pending")
        self.assertEqual(self.store.page_ready(role_name="worker")["total"], 0)

    def plant_local_facts(self):
        vcs.inject_refs(self.project.root, {"published": OTHER, "other-published": SHA})
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        CiRequests(self.project.root).mark_dispatch(identity, {
            "request_id": identity, "publication_id": self.receipt["publication_id"],
            "sha": SHA, "workflows": ["ci"], "dispatched_at": "2026-10-20T00:00:00+00:00"})
        write_json(self.project.root / POLL_STATE_FILE,
                   {"failures": {"published": [{"sha": SHA, "check": "unit"}]}, "successes": {}})

    def test_no_with_reason_is_actionable_in_status_and_ready_without_a_transition(self):
        with self.answered("no", REASON) as query:
            row = self.live()
            self.assertEqual((row["state"], row["reason"], row["wait_kind"]),
                             ("impossible", REASON, "ci"))
            page = self.store.page_ready(role_name="worker")
            live = page["items"][0]["wait_liveness"]
            self.assertEqual((live["state"], live["reason"]), ("impossible", REASON))
            current = self.store.get(self.item["id"])
            self.assertEqual((current["status"], current["rev"]), ("blocked", 2))
        query.assert_called_with(self.store.ci_wait_settings(), SHA)

    def test_yes_and_unknown_stay_pending_even_with_a_reason(self):
        for answer in ("yes", "unknown"):
            with self.subTest(answer=answer):
                with self.answered(answer, "adapter could not commit to absence"):
                    self.assert_pending()

    def test_no_stays_actionable_despite_any_local_head_request_or_poll_facts(self):
        self.plant_local_facts()
        with self.answered("no", REASON):
            self.assertEqual(self.live()["state"], "impossible")
            self.assertEqual(self.live()["reason"], REASON)
        for inventory in (patch("mizu.vcs.list_refs", side_effect=OSError("unreadable")),
                          patch("mizu.vcs.list_refs", return_value=["../escape"])):
            with inventory:
                with self.answered("no", REASON):
                    self.assertEqual(self.live()["state"], "impossible")
                with self.answered("yes"):
                    self.assert_pending()

    def test_local_head_request_and_poll_facts_do_not_override_adapter_yes(self):
        self.plant_local_facts()
        with self.answered("yes"):
            self.assert_pending()

    def test_empty_ref_inventory_and_missing_poll_file_do_not_decide(self):
        vcs.inject_refs(self.project.root, {})
        (self.project.root / POLL_STATE_FILE).unlink(missing_ok=True)
        with self.answered("no", REASON):
            self.assertEqual(self.live()["state"], "impossible")
        with self.answered("yes"):
            self.assert_pending()

    def test_adapter_error_or_malformed_answer_stays_pending(self):
        for failure in (Denied("adapter refused"), OSError("offline"),
                        ValueError("bad frame"), KeyError("reason")):
            with self.subTest(failure=failure):
                with patch("mizu.ci_waits.vcs.ci_reachable", side_effect=failure):
                    self.assert_pending()
        with patch("mizu.ci_waits.vcs.ci_reachable", return_value={"answer": "no"}):
            self.assert_pending()

    def test_current_evidence_or_unresolved_receipt_skips_the_query(self):
        with patch("mizu.ci_waits._read", return_value={}), self.answered("no", REASON) as query:
            self.assertIsNone(ci_waits.unreachable(self.store, self.item))
        self.assertEqual(query.call_count, 0)
        with patch("mizu.ci_waits._resolved", return_value=None), self.answered("no", REASON) as query:
            self.assertIsNone(ci_waits.unreachable(self.store, self.item))
        self.assertEqual(query.call_count, 0)

    def test_missing_settings_callback_falls_back_to_store_vcs(self):
        with (patch.object(self.store, "ci_wait_settings", None),
              self.answered("no", REASON) as query):
            self.assertEqual(ci_waits.unreachable(self.store, self.item), REASON)
        query.assert_called_once_with(self.store.vcs, SHA)

    def test_transition_reads_only_its_target_from_a_large_index(self):
        seed = self.store.create(actor="worker", idempotency_key="scale", title="Scale")["record"]["work_item"]
        extras = [{**seed, "id": f"wi_{i:032x}"} for i in range(1, 202)]
        index_items(self.project.root, [seed, *extras], replace=True)
        self.assertEqual(len(self.store.current_items(statuses=("open",))), 202)
        scanned = []
        plains = []
        active_rows = self.store._active_rows
        plain = self.store._plain

        def counted_rows(*args, **kwargs):
            for row in active_rows(*args, **kwargs):
                scanned.append(row)
                yield row

        def counted_plain(item_id, **kwargs):
            plains.append((item_id, kwargs))
            return plain(item_id, **kwargs)

        with (patch.object(self.store, "_active_rows", counted_rows),
              patch.object(self.store, "_plain", counted_plain)):
            changed = self.store.transition(seed["id"], expected_rev=1, status="doing", actor="worker")
        self.assertEqual(changed["status"], "doing")
        self.assertEqual(scanned, [], "transition scanned the active index")
        self.assertEqual(plains, [(seed["id"], {"locked": False, "durable": True})])

    def test_pending_intent_denies_ci_reopen_without_verification(self):
        with contextlib.closing(sqlite3.connect(self.project.root / LEDGER_NAME)) as db, db:
            db.execute("INSERT INTO intents(domain, key) VALUES ('items', ?)", (self.item["id"],))
        with self.answered("no", REASON) as query:
            with self.assertRaisesRegex(Denied, "Cannot verify.*retry"):
                self.store.transition(self.item["id"], expected_rev=2, status="open", actor="worker")
        self.assertEqual(query.call_count, 0, "adapter ran for an unverifiable index")
        unchanged = self.store.get(self.item["id"])
        self.assertEqual((unchanged["status"], unchanged["rev"]), ("blocked", 2))

    def test_changed_wait_identity_cannot_inherit_a_pre_read_answer(self):
        plain = self.store._plain

        def stale_plain(item_id, **kwargs):
            item = plain(item_id, **kwargs)
            return {**item, "rev": item["rev"] + 1}

        with (self.answered("no", REASON),
              patch.object(self.store, "_plain", stale_plain)):
            with self.assertRaisesRegex(Denied, "Cannot verify.*changed"):
                self.store.transition(self.item["id"], expected_rev=2, status="open", actor="worker")
        self.assertEqual(self.store.get(self.item["id"])["rev"], 2)

    def test_clean_index_keeps_the_reopen_with_reason_guard(self):
        with self.answered("no", REASON) as query:
            with self.assertRaisesRegex(Denied, "Reopening blocked work requires a reason"):
                self.store.transition(self.item["id"], expected_rev=2, status="open", actor="worker")
            opened = self.store.transition(self.item["id"], expected_rev=2, status="open", actor="worker",
                                           reason="Pinned commit is no longer a branch head")
        self.assertEqual(opened["status"], "open")
        self.assertEqual(query.call_count, 2)

    def test_yes_and_unknown_answers_stay_pending_on_a_normal_reopen(self):
        for answer in ("yes", "unknown"):
            with self.subTest(answer=answer):
                item = self.block(key=f"pending-{answer}")
                with self.answered(answer, "adapter cannot commit to absence") as query:
                    opened = self.store.transition(item["id"], expected_rev=2, status="open", actor="worker")
                self.assertEqual(opened["status"], "open")
                self.assertEqual(query.call_count, 1)
                blocked_ids = [row["id"] for row in self.store.blocked_liveness_readonly()["items"]]
                self.assertNotIn(item["id"], blocked_ids)

    def test_holder_reopens_with_a_reason_and_cannot_renew_without_it(self):
        with self.answered("no", REASON):
            with self.assertRaisesRegex(Denied, "Reopening blocked work requires a reason"):
                self.store.transition(self.item["id"], expected_rev=2, status="open", actor="worker")
            opened = self.store.transition(self.item["id"], expected_rev=2, status="open", actor="worker",
                                           reason="Pinned commit is no longer a branch head")
        self.assertEqual(opened["status"], "open")
        self.assertEqual(self.store.blocked_liveness_readonly()["total"], 0)

    def test_publication_ci_wait_uses_the_same_adapter_answer(self):
        self.config.vcs["ci_branch"] = "published"
        self.project = Project(self.config, "sample")
        self.store = self.project.work_items
        self.snapshot = snapshot = self.project.snapshots.get()
        self.receipt_for()
        item = self.block(key="publication", wait={"kind": "publication_ci",
            "snapshot_id": snapshot["id"], "checks": ["unit", "lint"]})
        with self.answered("no", REASON) as query:
            row = self.live(item["id"])
            self.assertEqual((row["state"], row["reason"], row["wait_kind"]),
                             ("impossible", REASON, "publication_ci"))
            ready = [r["id"] for r in self.store.page_ready(role_name="worker")["items"]]
            self.assertIn(item["id"], ready)
        query.assert_called_with(self.store.ci_wait_settings(), SHA)

    def probing_adapter(self):
        """An adapter that checks records.lock from another thread per query."""
        path = self.project.root / "locks" / "records.lock"
        probes = []

        def adapter(settings, sha):
            outcome = {}

            def probe():
                try:
                    with lock(path, blocking=False):
                        outcome["acquired"] = True
                except Busy:
                    outcome["error"] = "Busy"
                except BaseException as exc:
                    outcome["error"] = repr(exc)

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "record-lock probe hung")
            probes.append(outcome)
            return {"sha": sha, "answer": "no", "reason": REASON}

        return probes, adapter

    def test_page_ready_adapter_never_holds_records_lock(self):
        probes, adapter = self.probing_adapter()
        with patch("mizu.vcs.ci_reachable", side_effect=adapter):
            row = self.live()  # unlocked diagnostic keeps the direct query
            self.assertEqual((row["state"], row["reason"]), ("impossible", REASON))
            page = self.store.page_ready(role_name="worker")
        self.assertGreaterEqual(len(probes), 2)
        self.assertEqual(probes, [{"acquired": True}] * len(probes))
        live = page["items"][0]["wait_liveness"]
        self.assertEqual((live["state"], live["reason"]), ("impossible", REASON))

    def test_transition_adapter_never_holds_records_lock(self):
        probes, adapter = self.probing_adapter()
        with patch("mizu.vcs.ci_reachable", side_effect=adapter):
            with self.assertRaisesRegex(Denied, "Reopening blocked work requires a reason"):
                self.store.transition(self.item["id"], expected_rev=2, status="open", actor="worker")
            opened = self.store.transition(self.item["id"], expected_rev=2, status="open",
                                           actor="worker", reason="Pinned commit is gone")
        self.assertEqual(opened["status"], "open")
        self.assertTrue(probes)
        self.assertEqual(probes, [{"acquired": True}] * len(probes))
