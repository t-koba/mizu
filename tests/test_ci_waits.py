"""Offline receipt-pinned CI waits: no publication or real network adapter."""
import dataclasses
import json
import uuid
from unittest.mock import Mock, patch

from support import Fixture, ScriptDriver, without_blocked_age
from mizu import ci_waits, vcs
from mizu.config import PublicationEvidence
from mizu.errors import Denied, LimitExceeded
from mizu.fs import PREVIEW_BYTES, canonical, read_json, write_json
from mizu.project import Project
from mizu.protocol import DEFINITIONS, validate
from mizu.publication import Publication
from mizu.distribution import public_source_identity
from mizu.runtime import Context, Engine, _role_events_due, poll_project_vcs, refresh_poll_state, should_run

SHA = "b" * 40


class CiWaitFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "poll_enabled": True, "command": ["configured-adapter"], "poll_max_branches": 2})
        self.project = Project(self.config, "sample")
        self.store = self.project.work_items
        self.publication = Publication(self.project.root, "a" * 40, "c" * 64, "published", "push",
                                       projection=public_source_identity({}))
        intent, _ = self.publication.prepare("approval", "run")
        self.publication.mark_dispatch()
        self.receipt = self.publication.complete(intent, SHA)
        self.wait = {"kind": "ci", "publication_id": self.publication.id, "checks": ["unit", "lint"]}
        self.clock = 1000
        self.state = {}

    def block(self, key="item", actor="worker", wait=None):
        seed = self.store.create(actor=actor, idempotency_key=key, title="Observe CI")["record"]["work_item"]
        return self.store.transition(seed["id"], expected_rev=1, status="blocked", actor=actor,
                                     wait=self.wait if wait is None else wait)

    def rows(self, state="success", sha=SHA):
        return [{"check": c, "sha": sha, "state": state, "run": "run-" + c}
                for c in ["lint", "unit"]]

    def poll(self, rows=None, reader=None):
        self.clock += 30
        if reader is None:
            reader = Mock(return_value={"checks": self.rows() if rows is None else rows})
        result = ci_waits.poll(self.config, self.project, self.state,
                              now=self.clock, interval=15, reader=reader)
        return result, reader

    def reader(self, capabilities=("vcs_read", "work_items_read")):
        role = dataclasses.replace(self.config.roles["worker"], capabilities=capabilities)
        run = self.project.root / "runs" / uuid.uuid4().hex
        run.mkdir(parents=True)
        return Context(self.config, self.project, role, run,
                       self.project.snapshots.get(), self.project.workspace)

    def delivery_context(self, role_name="worker", *, limit=10):
        role = dataclasses.replace(self.config.roles[role_name],
                                   capabilities=("vcs_read", "work_items_read"))
        run = self.project.root / "runs" / uuid.uuid4().hex
        run.mkdir(parents=True)
        context = Context(self.config, self.project, role, run,
                          self.project.snapshots.get(), self.project.workspace)
        context.wait_events, context.ci_wait_manifest = ci_waits.delivery(
            self.store, role_name, limit=limit)
        return context

    def acknowledge_delivery(self, context, events=None):
        selected = context.wait_events if events is None else events
        ci_waits.complete(context, {"outcome": "wait", "summary": "Handled selected CI results",
                                    "ack_events": [event["ack_token"] for event in selected]})


class CiWaitTests(CiWaitFixture):
    def _idle_writer_snapshot(self):
        engine = Engine(self.config, driver=ScriptDriver())
        self.assertEqual(engine.run(self.project, "worker")["status"], "completed")
        return self.project.snapshots.get()

    def test_should_run_reuses_successful_negative_wait_probes(self):
        snapshot = self._idle_writer_snapshot()
        worker = self.config.roles["worker"]
        self.config.roles["worker"] = dataclasses.replace(
            worker, capabilities=tuple(worker.capabilities) + ("check_job",))
        with (patch("mizu.runtime.check_jobs.wake_ready", return_value=False) as check_ready,
              patch("mizu.runtime._receipt_wait_due", return_value=False) as wait_due):
            self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
        self.assertEqual(check_ready.call_count, 1)
        self.assertEqual(wait_due.call_count, 1)

    def test_should_run_retries_wait_probes_after_a_source_error(self):
        snapshot = self._idle_writer_snapshot()
        with (patch("mizu.runtime.check_jobs.wake_ready", return_value=False) as check_ready,
              patch("mizu.runtime._receipt_wait_due",
                    side_effect=[OSError("transient"), False]) as wait_due):
            self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
        # A failed first probe is not memoized; routed-event evaluation retries.
        self.assertEqual(check_ready.call_count, 1)
        self.assertEqual(wait_due.call_count, 2)

    def test_wait_arriving_between_probes_is_seen_on_next_evaluation(self):
        self.block()
        snapshot = self._idle_writer_snapshot()
        from mizu import runtime as runtime_module
        original_due = runtime_module._receipt_wait_due
        calls = 0

        def due_then_publish(store, role, *, limit=1):
            nonlocal calls
            calls += 1
            due = original_due(store, role, limit=limit)
            if calls == 1:
                # Publish durable CI evidence after the first successful empty
                # probe, at the point the old second scan would have seen it.
                self.poll()
            return due

        with (patch("mizu.runtime._ready_work_observation", return_value=(False, False, False)),
              patch("mizu.runtime.check_jobs.wake_ready", return_value=False),
              patch("mizu.runtime._receipt_wait_due", side_effect=due_then_publish) as wait_due):
            self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
            self.assertEqual(wait_due.call_count, 1)
            # The negative result is local to the first evaluation. The new
            # durable receipt is found by the next call, not lost in a cache.
            self.assertTrue(should_run(self.project, snapshot, role_name="worker"))
            self.assertEqual(wait_due.call_count, 2)

    def test_terminal_readiness_survives_ack_without_repeated_admission(self):
        blocked = self.block()
        self.config.roles["worker"] = dataclasses.replace(self.config.roles["worker"], on_change=True)
        engine = Engine(self.config, driver=ScriptDriver())
        self.assertEqual(engine.run(self.project, "worker")["status"], "completed")
        snapshot = self.project.snapshots.get()
        pending = self.store.page_ready(role_name="worker")
        self.assertEqual(pending["total"], 0)
        self.poll(self.rows(state="pending"))
        self.assertEqual(self.store.page_ready(role_name="worker")["signature"], pending["signature"])
        self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
        self.poll(self.rows(state="failure"))
        # Readiness validates local source evidence, never polls, syncs or writes.
        before = sorted(p.relative_to(self.project.root) for p in self.project.root.rglob("*") if p.is_file())
        with (patch("mizu.ci_waits.poll", side_effect=AssertionError("no polling")),
              patch("mizu.ci_waits.sync_dir", side_effect=AssertionError("no sync")),
              patch("mizu.ci_waits.write_json", side_effect=AssertionError("no writes"))):
            terminal = self.store.page_ready(role_name="worker")
        self.assertEqual(before, sorted(p.relative_to(self.project.root) for p in self.project.root.rglob("*") if p.is_file()))
        self.assertNotEqual(terminal["signature"], pending["signature"])
        row = terminal["items"][0]
        self.assertEqual((row["id"], row["rev"], row["holder"]),
                         (blocked["id"], blocked["rev"], "worker"))
        live = row["wait_liveness"]
        self.assertEqual(live["state"], "terminal")
        self.assertEqual(live["outcome"], "failure")
        self.assertEqual(set(live["evidence"]), {"record_digest"})
        self.assertLess(len(canonical(live)), 512)
        with patch("mizu.ci_waits.evidence", side_effect=AssertionError("other holder")):
            self.assertEqual(self.store.page_ready(role_name="reviewer")["total"], 0)
        context = self.delivery_context()
        self.assertEqual(live["evidence"], {"record_digest": context.wait_events[0]["record_digest"]})
        self.acknowledge_delivery(context)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        after_ack = self.store.page_ready(role_name="worker")
        self.assertEqual(without_blocked_age(after_ack), without_blocked_age(terminal))
        self.assertTrue(should_run(self.project, snapshot, role_name="worker"))
        self.assertEqual(engine.run(self.project, "worker")["status"], "completed")
        projected = json.loads(engine.driver.calls[-1][1])["ready_work"]["items"][0]
        projected_live = projected["wait_liveness"]
        self.assertEqual(without_blocked_age(projected_live), without_blocked_age(live))
        self.assertIs(type(projected_live["blocked_age_seconds"]), int)
        self.assertGreaterEqual(projected_live["blocked_age_seconds"], 0)
        self.assertFalse(projected["wait_satisfied"])  # Terminal input is not completion.
        for _ in range(2):
            self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name="worker"))
            self.assertEqual(engine.run(self.project, "worker")["skipped"], "unchanged")
            self.assertEqual(engine.run(self.project, "worker", event_trigger=True)["skipped"], "no-event")
        self.assertEqual(self.store.get(blocked["id"]), blocked)
        # Status uses the same exact terminal route without writing history/evidence.
        before = {p.relative_to(self.project.root): p.read_bytes()
                  for p in self.project.root.rglob("*") if p.is_file()}
        self.assertEqual(self.store.blocked_liveness_readonly()["items"][0]["state"], "terminal")
        self.assertEqual({p.relative_to(self.project.root): p.read_bytes()
                          for p in self.project.root.rglob("*") if p.is_file()}, before)
        with self.assertRaisesRegex(Denied, "Reopen"):
            self.store.transition(blocked["id"], expected_rev=blocked["rev"], status="blocked", actor="worker",
                                   wait=self.wait, scope="Renew wait", reason="Try again")
        with self.assertRaisesRegex(Denied, "reason"):
            self.store.transition(blocked["id"], expected_rev=blocked["rev"], status="open", actor="worker")

    def test_ready_refuses_invalid_or_unavailable_terminal_source(self):
        blocked = self.block()
        self.poll()
        path = ci_waits._path(self.store, blocked)
        original = read_json(path)
        original_signature = self.store.page_ready()["signature"]
        for changes in ({"holder": "reviewer"}, {"rev": blocked["rev"] + 1},
                        {"wait_digest": "0" * 64}, {"sha": "e" * 40},
                        {"conclusion": "failure"}, {"checks": self.rows("pending")},
                        {"checks": self.rows() + self.rows()}):
            with self.subTest(changes=changes):
                write_json(path, {**original, **changes})
                page = self.store.page_ready(role_name="worker")
                self.assertEqual(page["total"], 0)
                self.assertEqual(page["unknown_blocked"], 1)
                self.assertEqual(self.store.blocked_liveness_readonly()["items"][0]["state"], "unknown")
                self.assertFalse(page["coverage_complete"])
        path.write_text("not JSON")
        self.assertEqual(self.store.page_ready()["total"], 0)
        write_json(path, original)
        with patch("mizu.ci_waits._read", side_effect=OSError("unavailable")):
            self.assertEqual(self.store.page_ready()["unknown_blocked"], 1)
        self.assertEqual(self.store.page_ready()["total"], 1)
        context = self.delivery_context()
        shared = ci_waits.shared_store(self.store)
        envelope, _ = shared.get(context.ci_wait_manifest[0].record_id)
        shared_path = self.project.root / shared._record_names(envelope["id"])[0]
        write_json(shared_path, {**envelope, "payload": {**envelope["payload"], "wait_digest": "0" * 64}})
        self.assertEqual(self.store.page_ready()["unknown_blocked"], 1)
        write_json(shared_path, envelope)
        opened = self.store.transition(blocked["id"], expected_rev=blocked["rev"], status="open",
                                       actor="worker", reason="receipt_route_terminal: reassess CI")
        current = self.store.transition(opened["id"], expected_rev=opened["rev"], status="blocked",
                                        actor="worker", wait=self.wait)
        # Old evidence is not reused by a later revision of the same wait.
        self.assertEqual(self.store.page_ready()["total"], 0)
        current_path = ci_waits._path(self.store, current)
        write_json(current_path, original)
        self.assertEqual(self.store.page_ready()["unknown_blocked"], 1)
        current_path.unlink()
        self.poll()
        self.assertEqual(self.store.page_ready()["total"], 1)
        self.assertNotEqual(self.store.page_ready()["signature"], original_signature)

    @patch("mizu.work_item_waits.now", return_value="2000-01-01T00:00:01+00:00")
    def test_blocked_problem_rows_precede_pending_and_receipt_bound_rows(self, _clock):
        with patch("mizu.work_items.now", return_value="2000-01-01T00:00:00+00:00"):
            self.block("pending", wait={"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"})
            self.block("ci")
        with patch("mizu.work_items.now", return_value="2001-01-01T00:00:00+00:00"):
            absent = self.block("absent", wait={"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"})
            absent["wait"] = None  # Preserve legacy no-wait reads.
            write_json(self.project.root / "work-items" / absent["id"] / "2.json", absent)
            impossible = self.block("impossible", wait={"kind": "insight_decided", "insight": "f" * 32})
            due = self.block("due", wait={"kind": "deadline", "at": "2000-01-01T00:00:00+00:00"})
            target = self.project.insights.submit(source="worker", title="Unknown target",
                                                  body="Evidence", base_snapshot=None)
            unknown = self.block("unknown", wait={"kind": "insight_decided", "insight": target["id"]})
        (self.project.root / "inbox" / f"{target['id']}.json").write_bytes(b"[]")
        view = self.store.blocked_liveness_readonly(limit=4)
        expected = sorted([absent, impossible, due, unknown], key=lambda item: item["id"])
        self.assertEqual([row["id"] for row in view["items"]], [item["id"] for item in expected])
        self.assertEqual({row["state"] for row in view["items"]}, {"absent", "impossible", "due", "unknown"})
        self.assertEqual((view["overflow_count"], view["truncated"]), (2, True))
        self.assertFalse(view["coverage_complete"])
        self.assertIsNone(view["counts"])  # Unknown is still fail-closed.
        self.store.transition(unknown["id"], expected_rev=2, status="open", actor="worker")
        view = self.store.blocked_liveness_readonly(limit=3)
        self.assertEqual({row["state"] for row in view["items"]}, {"absent", "impossible", "due"})
        self.assertEqual(view["counts"], {"absent": 1, "impossible": 1, "due": 1,
                                         "unknown": 0, "pending": 2, "receipt_bound": 0, "terminal": 0, "expired": 0})
        self.assertEqual((view["total"], view["overflow_count"], view["truncated"]), (5, 2, True))
        self.assertTrue(view["coverage_complete"])

    def test_diagnosis_is_exact_current_bounded_and_pure(self):
        name = "Offline checks / core (windows-latest, 3.13)"
        wait = self.block(wait={**self.wait, "checks": [name]})["wait"]
        exact = {"check": name, "sha": SHA, "state": "success", "run": "run-1"}
        cases = [
            ([], "absent", 0, 0),
            ([{**exact, "check": "core (windows-latest, 3.13)"}], "absent", 0, 0),
            ([exact, exact], "duplicate", 2, 0),
            ([exact, {**exact, "sha": "d" * 40}], "duplicate", 2, 1),
            ([{**exact, "state": "queued"}], "nonterminal", 1, 0),
            ([{**exact, "state": "running"}], "nonterminal", 1, 0),
            ([{**exact, "sha": "d" * 40}], "wrong_sha", 1, 1),
            ([exact], "terminal", 1, 0),
            ([{**exact, "state": "failure"}], "terminal", 1, 0),
        ]
        for rows, observation, count, wrong in cases:
            with self.subTest(observation=observation, rows=rows):
                data = {"checks": rows}
                before = canonical([wait, data])
                self.assertEqual(ci_waits.diagnose(wait, data), [{
                    "check": name, "observation": observation,
                    "exact_name_count": count, "wrong_sha_count": wrong}])
                self.assertEqual(ci_waits.terminal(wait, data) is not None,
                                 observation == "terminal")
                self.assertEqual(canonical([wait, data]), before)
        self.assertEqual(ci_waits.diagnose(wait, {"checks": [exact], "branch": "other"})
                         [0]["observation"], "branch_mismatch")
        for bad_wait, data in (({**wait, "checks": ["x"] * 65}, {"checks": []}),
                               (wait, {"checks": [{**exact, "check": "unsafe\nname"}]}),
                               (wait, {"checks": [exact] * 1025})):
            with self.assertRaises(Denied):
                ci_waits.diagnose(bad_wait, data)

    def test_publication_status_reuses_response_and_only_explains_current_holder_waits(self):
        name = "Offline checks / unit"
        item = self.block(wait={**self.wait, "checks": [name]})
        foreign = self.block("foreign", actor="editor", wait={**self.wait, "checks": ["private"]})
        inactive = self.block("inactive")
        self.store.transition(inactive["id"], expected_rev=2, status="doing", actor="worker")
        reader = self.reader()
        args = {"op": "status", "publication_id": self.publication.id}
        rows = [{"check": "unit", "sha": SHA, "state": "success"},
                {"check": name, "sha": "d" * 40, "state": "success"}]
        for current_rows, expected in ((rows, "wrong_sha"), (rows[:1], "absent"),
                                       ([{**rows[1], "sha": SHA}], "terminal")):
            with self.subTest(expected=expected):
                with patch.object(vcs, "invoke", return_value={"checks": current_rows}) as adapter:
                    observed = reader.handle("vcs_read", args)
                adapter.assert_called_once()
                self.assertEqual(adapter.call_args.args[1],
                                 {"op": "status", "branch": "published", "sha": SHA})
                result = observed["result"]
                self.assertEqual(result["checks"], [row for row in vcs.parse_status_checks(
                    {"checks": current_rows}) if row["sha"] == SHA])
                diagnostics = result["ci_waits"]
                self.assertEqual(diagnostics["items"], [{"work_item": item["id"], "rev": 2,
                    "checks": [{"check": name, "observation": expected,
                                "exact_name_count": int(expected != "absent"),
                                "wrong_sha_count": int(expected == "wrong_sha")}]}])
                self.assertEqual(diagnostics["observed_checks"], current_rows)
                self.assertNotIn(foreign["id"], canonical(diagnostics).decode())
                self.assertNotIn(inactive["id"], canonical(diagnostics).decode())
                self.assertEqual(read_json(reader.run_dir / observed["evidence"]), observed)
                self.assertEqual(self.store.get(item["id"]), item)
                self.assertFalse(ci_waits._path(self.store, item).exists())
                if expected != "terminal":
                    polled, _ = self.poll(rows=current_rows)
                    self.assertEqual(polled["observed"], 0)
        self.assertFalse((self.project.root / "waits").exists())
        # A later normal poll can still retain exact terminal evidence.
        result, _ = self.poll(rows=[{**rows[1], "sha": SHA}])
        self.assertEqual(result["observed"], 1)
        self.assertEqual(self.store.get(item["id"]), item)

    def test_status_diagnostics_require_both_grants_and_receipt_locator(self):
        self.block()
        rows = self.rows() + self.rows(sha="d" * 40)
        for caps in (("vcs_read",), ("vcs_read", "work_items_read")):
            reader = self.reader(caps)
            with patch.object(vcs, "invoke", return_value={"checks": rows}) as adapter:
                observed = reader.handle("vcs_read", {"op": "status", "branch": "published", "sha": SHA})
            adapter.assert_called_once()
            self.assertEqual(observed["result"], {
                "op": "status", "branch": "published", "trust": "external-untrusted",
                "checks": vcs.parse_status_checks({"checks": self.rows()})})
        reader = self.reader(("vcs_read",))
        with patch.object(vcs, "invoke", return_value={"checks": rows}), \
                patch.object(ci_waits, "status_diagnostics", side_effect=AssertionError("not granted")):
            observed = reader.handle("vcs_read", {"op": "status", "publication_id": self.publication.id})
        self.assertNotIn("ci_waits", observed["result"])
        self.assertEqual(observed["result"]["checks"], vcs.parse_status_checks({"checks": self.rows()}))
        reader = self.reader(("work_items_read",))
        with patch.object(vcs, "invoke") as adapter:
            with self.assertRaisesRegex(Denied, "no capability"):
                reader.handle("vcs_read", {"op": "status", "publication_id": self.publication.id})
            adapter.assert_not_called()

    def test_diagnostic_projection_is_bounded_and_filters_receipt_pins(self):
        wait = self.block()["wait"]
        items = [{"id": "wi_" + f"{index:032x}", "rev": 2, "holder": "worker", "wait": wait}
                 for index in range(ci_waits.MAX_DIAGNOSTIC_ITEMS + 2)]
        other = [{**items[0], "wait": {**wait, key: value}} for key, value in (
            ("publication_id", "f" * 64), ("branch", "other"), ("sha", "d" * 40))]
        rows = [{"check": "界" * 256, "state": "x" * 64, "sha": SHA, "run": "r" * 128}
                for _ in range(ci_waits.MAX_DIAGNOSTIC_ROWS + 1)]
        with patch.object(ci_waits, "_items", return_value=iter(other + list(reversed(items)))):
            result = ci_waits.status_diagnostics(self.store, "worker", self.receipt, {"checks": rows})
        self.assertEqual(len(result["items"]), ci_waits.MAX_DIAGNOSTIC_ITEMS)
        self.assertEqual([item["work_item"] for item in result["items"]],
                         [item["id"] for item in items[:ci_waits.MAX_DIAGNOSTIC_ITEMS]])
        self.assertTrue(result["items_truncated"])
        self.assertEqual(len(result["observed_checks"]), ci_waits.MAX_DIAGNOSTIC_ROWS)
        self.assertTrue(result["observed_checks_truncated"])
        self.assertLess(len(canonical(result)), PREVIEW_BYTES)
        with patch.object(ci_waits, "_items", return_value=iter(other)):
            self.assertIsNone(ci_waits.status_diagnostics(self.store, "worker", self.receipt, {"checks": rows}))

    def test_registration_is_work_item_only_and_pins_valid_receipt(self):
        item = self.block()
        self.assertEqual(item["wait"], {**self.wait, "checks": ["lint", "unit"],
                                        "branch": "published", "sha": SHA})
        self.assertEqual(self.store.get(item["id"])["wait"], item["wait"])
        self.assertEqual(self.store.page_open()["items"][0]["wait"], item["wait"])
        args = {"id": item["id"], "expected_rev": 2, "status": "blocked", "wait": self.wait}
        validate(args, DEFINITIONS["work_items_transition"][1])
        with self.assertRaises(Denied):
            validate({"id": "proposal", "action": "defer", "reason": "wait", "rev": 1,
                      "wait": self.wait}, DEFINITIONS["decide"][1])
        with self.assertRaises(Denied):
            self.project.insights._normalize_wait(self.wait, "defer")
        # Stored wait reads revalidate the completed receipt, not just ID syntax.
        self.publication.path("receipt").unlink()
        with self.assertRaises(Denied):
            self.store.get(item["id"])

    def test_publication_and_ci_evidence_survive_backup_restore(self):
        from mizu.storage import backup, restore
        item = self.block("backup-roundtrip")
        observed, _ = self.poll()
        context = self.delivery_context()
        events = context.wait_events
        self.assertEqual(observed["observed"], 1)
        self.assertEqual(len(events), 1)
        self.acknowledge_delivery(context)

        # Preserve the point-of-no-return marker even when publication has no
        # receipt: restoring without it would authorize a fresh dispatch.
        uncertain = Publication(self.project.root, "d" * 40, "e" * 64, "published", "push",
                                projection=public_source_identity({}))
        uncertain.prepare("uncertain-approval", "uncertain-run")
        uncertain.mark_dispatch()

        self.project.set_control(paused=True)
        archive = self.root / "backup.tar.gz"
        backup(self.project, archive)
        restore(self.config, "restored", archive)
        restored = Project(self.config, "restored", read_only=True)

        self.assertEqual(Publication.read_receipt(restored.root, self.publication.id), self.receipt)
        restored_item = restored.work_items.get(item["id"])
        self.assertEqual(restored_item["wait"], item["wait"])
        self.assertEqual(ci_waits._evidence(restored.work_items, restored_item),
                         {key: value for key, value in events[0].items()
                          if key not in ("record_id", "record_digest", "ack_token")})
        entry = context.ci_wait_manifest[0]
        self.assertTrue(ci_waits.shared_store(restored.work_items).received(
            "worker", entry.record_id, entry.record_digest))
        self.assertEqual(ci_waits.pending(restored.work_items, "worker"), [])

        restored_uncertain = Publication(restored.root, "d" * 40, "e" * 64, "published", "push",
                                         projection=public_source_identity({}))
        with self.assertRaisesRegex(Denied, "outcome unresolved; already dispatched"):
            restored_uncertain.prepare("uncertain-approval", "retry-run")

    def test_wait_evidence_carries_the_job_time(self):
        item = self.block()
        rows = [{"check": c, "state": "success", "sha": SHA, "run": "run-" + c,
                 "started_at": "2026-07-20T05:00:00+00:00",
                 "completed_at": "2026-07-20T05:03:20+00:00"} for c in ["lint", "unit"]]
        observed, _ = self.poll(rows=rows)
        self.assertEqual(observed["observed"], 1)
        events = ci_waits.pending(self.store, "worker")
        self.assertEqual(len(events), 1)
        checks = {row["check"]: row for row in events[0]["checks"]}
        self.assertEqual(checks["unit"]["job_seconds"], 200)
        self.assertEqual(checks["unit"]["started_at"], "2026-07-20T05:00:00+00:00")
        # The stored evidence revalidates through the same parser.
        self.assertEqual(ci_waits._evidence(self.store, self.store.get(item["id"])),
                         {key: value for key, value in events[0].items()
                          if key not in ("record_id", "record_digest", "ack_token")})

    def test_registration_refuses_invalid_bounds_uncertain_receipts_and_disabled_polling(self):
        bad = [[], ["unit"] * 2, [""], ["x\x85"], ["x\n"], ["x" * 257], [1], ["x\ud800"],
               [str(i) for i in range(65)], ["x" * 256 for _ in range(64)]]
        for i, checks in enumerate(bad):
            with self.subTest(checks=repr(checks)[:80]), self.assertRaises(Denied):
                self.block(str(i), wait={**self.wait, "checks": checks})
        for extra in ({"branch": "other"}, {"sha": "d" * 40}, {"publication_id": "0" * 64}):
            with self.subTest(extra=extra), self.assertRaises(Denied):
                self.block(str(extra), wait={**self.wait, **extra})
        for changes in ({"poll_enabled": False}, {"command": []}):
            with patch.dict(self.store.vcs, changes), self.assertRaises(Denied):
                self.block(str(changes))
        self.publication.path("receipt").unlink()  # dispatched, uncertain, not completed
        with self.assertRaises(Denied):
            self.block("uncertain")

    def test_moved_branch_is_never_used_instead_of_receipt_sha(self):
        item = self.block()
        # Adapter serves only the newer head: even all-green is unresolved.
        result, reader = self.poll(self.rows(sha="d" * 40))
        self.assertEqual(result["observed"], 0)
        reader.assert_called_once_with(self.config.vcs, "status", {"branch": "published", "sha": SHA})
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        result, _ = self.poll()
        self.assertEqual(result["observed"], 1)
        self.assertEqual(ci_waits.pending(self.store, "worker")[0]["sha"], SHA)
        self.assertEqual(self.store.get(item["id"]), item)

    def test_only_full_unique_declared_terminal_set_resolves(self):
        item = self.block()
        good = self.rows()
        cases = [[], good[:1], [good[0], {**good[1], "state": "pending"}],
                 [good[0], {**good[1], "state": "unknown"}], good + [good[0]],
                 good + [{**good[0], "sha": "d" * 40}],
                 [good[0], {**good[1], "sha": "d" * 40}]]
        for rows in cases:
            with self.subTest(rows=rows):
                result, _ = self.poll(rows)
                self.assertEqual(result["observed"], 0)
                self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        # An undeclared row is not a requirement and cannot decide the conclusion.
        result, _ = self.poll(good + [{"check": "other", "state": "pending", "sha": SHA}])
        self.assertEqual(result["observed"], 1)
        self.assertEqual(ci_waits.pending(self.store, "worker")[0]["conclusion"], "success")
        self.assertEqual(self.store.get(item["id"])["status"], "blocked")

    def test_terminal_failure_wakes_and_only_explicit_receipt_stops_replay(self):
        item = self.block()
        result, _ = self.poll([self.rows()[0], {**self.rows()[1], "state": "failure"}])
        self.assertEqual(result["observed"], 1)
        context = self.delivery_context()
        events = context.wait_events
        self.assertEqual((len(events), events[0]["conclusion"], events[0]["rev"]), (1, "failure", 2))
        self.assertEqual(events[0]["publication_id"], self.publication.id)
        self.assertEqual(events[0]["trust"], "external-untrusted")
        self.assertEqual(events[0]["checks"][1]["run"], "run-unit")
        self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), "worker"))
        self.assertEqual(ci_waits.pending(self.store, "reviewer"), [])
        result, reader = self.poll()
        self.assertEqual(result["polled"], 0)
        reader.assert_not_called()
        self.acknowledge_delivery(context)
        self.acknowledge_delivery(context)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), "worker"))
        self.assertEqual(self.store.get(item["id"]), item)

    def test_dispatch_view_preserves_ci_delivery_and_rechecks_waiter(self):
        item = self.block()
        self.poll()
        role = dataclasses.replace(self.config.roles["worker"], on_change=False,
                                   work_item_events=("done", "dropped"))
        engine = Engine(self.config, driver=ScriptDriver())

        def dispatch():
            return engine._dispatch_state(self.project, "worker", role, event_trigger=True)

        with patch("mizu.runtime.readonly_view", side_effect=lambda store, **kwargs: store):
            expected = dispatch()
        self.assertEqual(dispatch(), expected)
        self.assertEqual(expected["_dispatch"].admission, "wait")
        self.assertEqual([event["work_item"] for event in expected["wait_events"]], [item["id"]])
        opened = self.store.transition(item["id"], expected_rev=item["rev"], status="open",
                                       actor="worker", reason="receipt_route_terminal: process CI")
        self.store.transition(item["id"], expected_rev=opened["rev"], status="doing", actor="worker")
        current = dispatch()
        self.assertEqual(current["wait_events"], [])
        self.assertEqual(current["_dispatch"].admission, "ready_work")
        with patch("mizu.runtime.readonly_view", side_effect=lambda store, **kwargs: store):
            self.assertEqual(dispatch(), current)

    def test_runtime_replays_unselected_explicit_ci_delivery(self):
        item = self.block("replay", actor="reviewer")
        engine = Engine(self.config, driver=ScriptDriver())
        engine.run(self.project, "reviewer")
        self.poll(self.rows(state="success"))
        first = engine.run(self.project, "reviewer")
        self.assertEqual(first["status"], "completed")
        first_event = read_json(self.project.root / "runs" / first["run"] / "wait-events.json")["events"][0]
        self.assertIn("ack_token", first_event)
        self.assertEqual(first_event["work_item"], item["id"])
        self.assertEqual(first_event["conclusion"], "success")
        self.assertEqual(self.store.get(item["id"]), item)
        self.assertEqual(ci_waits.pending(self.store, "reviewer"), [first_event])

        second = engine.run(self.project, "reviewer")
        self.assertEqual(second["status"], "completed")
        second_event = read_json(self.project.root / "runs" / second["run"] / "wait-events.json")["events"][0]
        self.assertEqual(second_event, first_event)
        self.assertEqual(ci_waits.pending(self.store, "reviewer"), [first_event])

    def test_stale_observation_and_old_token_cannot_ack_reopened_waiter(self):
        item = self.block()
        def changed_reader(*args):
            self.store.transition(item["id"], expected_rev=2, status="open", actor="worker")
            self.store.transition(item["id"], expected_rev=3, status="blocked", actor="worker", wait=self.wait)
            return {"checks": self.rows()}
        result, _ = self.poll(reader=changed_reader)
        self.assertEqual(result["observed"], 0)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.poll()
        old_context = self.delivery_context("worker")
        old = old_context.wait_events
        self.assertEqual(len(old), 1)
        opened = self.store.transition(item["id"], expected_rev=4, status="open", actor="worker",
                                       reason="receipt_route_terminal: reassign CI review")
        doing = self.store.transition(item["id"], expected_rev=opened["rev"], status="doing", actor="reviewer")
        current = self.store.transition(item["id"], expected_rev=doing["rev"], status="blocked", actor="worker", wait=self.wait)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.assertEqual(ci_waits.pending(self.store, "reviewer"), [])
        with self.assertRaises(Denied):
            self.acknowledge_delivery(old_context, old)
        self.poll()
        fresh_context = self.delivery_context("reviewer")
        fresh = fresh_context.wait_events
        self.assertEqual(fresh[0]["rev"], current["rev"])
        self.assertNotEqual(fresh[0]["evidence_digest"], old[0]["evidence_digest"])
        with self.assertRaises(Denied):
            self.acknowledge_delivery(old_context, old)
        self.assertEqual(ci_waits.pending(self.store, "reviewer"), fresh)

    def test_bounded_rotation_throttle_errors_and_shared_state(self):
        for i in range(5):
            self.block(str(i), wait={**self.wait, "checks": [str(i)]})
        seen = []
        def reader(settings, op, params):
            # Capture item selection through its cursor, all responses unresolved.
            seen.append(self.state["ci_wait_cursor"])
            if len(seen) == 1:
                raise Denied("unavailable")
            return {"checks": []}
        for _ in range(3):
            result, _ = self.poll(reader=reader)
            self.assertEqual(result["polled"], 2)
        self.assertEqual(len(set(seen)), 5)
        self.assertIsNone(ci_waits.poll(self.config, self.project, self.state,
                                       now=self.clock, interval=15, reader=reader))
        shared = {k: self.state[k] for k in ("last_ci_wait_poll", "ci_wait_cursor")}
        resumed = {}
        refresh_poll_state(resumed, shared)
        self.assertEqual({k: resumed[k] for k in shared}, shared)
        self.assertIsNone(ci_waits.poll(self.config, self.project, resumed,
                                       now=self.clock, interval=15, reader=reader))

    def test_writer_wakes_after_done_but_control_gates_and_ack_still_hold(self):
        self.block()
        snap = {**self.project.snapshots.get(), "outcome": "done",
                "wake_generation": self.project.control()["wake_generation"]}
        self.assertFalse(should_run(self.project, snap, role_name="worker"))
        self.poll()
        self.assertTrue(should_run(self.project, snap, role_name="worker"))
        for control in ({"paused": True}, {"draining": True}, {"armed": False}):
            self.project.set_control(armed=True, paused=False, draining=False)
            self.project.set_control(**control)
            self.assertFalse(should_run(self.project, snap, role_name="worker"))
        self.project.set_control(armed=True, paused=False, draining=False)
        context = self.delivery_context()
        self.acknowledge_delivery(context)
        self.assertFalse(_role_events_due(self.project, snap, "worker"))
        self.assertTrue(should_run(self.project, snap, role_name="worker"))  # Unhandled blocked work stays ready.

    def test_poll_project_persists_ci_wait_state(self):
        self.block()
        with patch("mizu.runtime.poll_upstream", return_value=None), \
                patch("mizu.runtime.poll_ci", return_value=None), \
                patch("mizu.runtime.poll_proposals", return_value=None), \
                patch("mizu.vcs.read_via", return_value={"checks": self.rows()}) as reader:
            events = poll_project_vcs(self.config, self.project, {}, now=1000, interval=15)
        self.assertEqual(events[0]["observed"], 1)
        reader.assert_called_once()
        shared = read_json(self.project.root / "vcs-poll.json")
        self.assertEqual(shared["last_ci_wait_poll"], 1000)
        self.assertTrue(shared["ci_wait_cursor"].startswith("wi_"))

    def test_default_reader_preserves_mismatched_duplicate_rows(self):
        self.block()
        rows = self.rows() + [{**self.rows()[0], "sha": "d" * 40}]
        with patch("mizu.vcs.invoke", return_value={"checks": rows}) as adapter:
            result = ci_waits.poll(self.config, self.project, {}, now=1000, interval=15)
        self.assertEqual(result["observed"], 0)
        self.assertEqual(adapter.call_args.args[1], {"op": "status", "branch": "published", "sha": SHA})
        # Existing exact-SHA tool behavior remains filtered.
        with patch("mizu.vcs.invoke", return_value={"checks": rows}):
            data = vcs.read_via(self.config.vcs, "status", {"branch": "published", "sha": SHA})
        self.assertEqual(len(data["checks"]), 2)

    def test_large_valid_set_and_evidence_bounds(self):
        names = [str(i).zfill(2) + "😀" * 4 for i in range(64)]
        item = self.block(wait={**self.wait, "checks": names})
        rows = [{"check": c, "state": "success", "sha": SHA, "run": "😀" * 128} for c in names]
        result, _ = self.poll(rows)
        self.assertEqual(result["observed"], 1)
        event = ci_waits.pending(self.store, "worker")[0]
        self.assertLess(len(canonical(event)), PREVIEW_BYTES)
        self.assertLessEqual(len(canonical(item["wait"]["checks"])), ci_waits.MAX_CHECK_SET_BYTES)
        self.assertLessEqual(len(canonical(self.store.page_open()["items"][0])), 8192)
        self.assertEqual(self.store.page_open()["items"][0]["id"], item["id"])

    def test_bounded_delivery_and_partial_explicit_ack_replay(self):
        items = [self.block(str(i)) for i in range(3)]
        self.poll()
        self.poll()
        context = self.delivery_context(limit=2)
        page = context.wait_events
        self.assertEqual(len(page), 2)
        self.acknowledge_delivery(context, [page[1]])
        remaining = ci_waits.pending(self.store, "worker")
        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[0], page[0])
        self.assertNotIn(page[1], remaining)
        self.assertTrue(all(self.store.get(i["id"])["status"] == "blocked" for i in items))

    def test_oversized_evidence_is_not_delivery_or_a_poll_crash(self):
        item = self.block()
        write_json(ci_waits._path(self.store, item), {"padding": "x" * PREVIEW_BYTES})
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        result, reader = self.poll()
        self.assertFalse(result["ok"])
        reader.assert_not_called()
        snap = {**self.project.snapshots.get(), "outcome": "done",
                "wake_generation": self.project.control()["wake_generation"]}
        self.assertFalse(should_run(self.project, snap, role_name="worker"))

    def test_failed_evidence_write_never_wakes_and_tampered_evidence_fails_closed(self):
        item = self.block()
        with patch("mizu.ci_waits.write_json", side_effect=OSError("unwritable")):
            result, _ = self.poll()
        self.assertFalse(result["ok"])
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.poll()
        context = self.delivery_context()
        event = context.wait_events[0]
        path = ci_waits._path(self.store, item)
        record = read_json(path)
        record["conclusion"] = "failure"
        write_json(path, record)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        result, reader = self.poll()
        self.assertFalse(result["ok"])
        reader.assert_not_called()
        with self.assertRaises(Denied):
            self.acknowledge_delivery(context)
        self.assertFalse(ci_waits._path(self.store, item, "ack").exists())
        self.assertEqual(read_json(path), record)
        self.assertEqual(event["conclusion"], "success")

    def test_corrupt_result_does_not_block_delivery_ack_or_unrelated_poll(self):
        bad, good = [self.block(key) for key in ("bad", "good")]
        self.assertEqual(self.poll()[0]["observed"], 2)
        path = ci_waits._path(self.store, bad)
        record = read_json(path)
        record["conclusion"] = "failure"
        write_json(path, record)
        corrupted = path.read_bytes()

        context = self.delivery_context()
        self.assertEqual([event["work_item"] for event in context.wait_events], [good["id"]])
        self.acknowledge_delivery(context)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])

        third = self.block("third")
        result, reader = self.poll()
        reader.assert_called_once_with(self.config.vcs, "status", {"branch": "published", "sha": SHA})
        self.assertEqual((result["ok"], result["polled"], result["observed"]), (False, 1, 1))
        self.assertEqual(result["errors"], ["denied"])
        self.assertNotIn(bad["id"], json.dumps(result))
        fresh_context = self.delivery_context()
        self.assertEqual([event["work_item"] for event in fresh_context.wait_events], [third["id"]])
        self.acknowledge_delivery(fresh_context)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.assertEqual(path.read_bytes(), corrupted)
        for item in (bad, good, third):
            self.assertEqual(self.store.get(item["id"]), item)

    def test_status_ignores_legacy_ack_files_and_reports_shared_source_only(self):
        item = self.block()
        self.poll()
        legacy = ci_waits._path(self.store, item, "ack")
        legacy.write_bytes(b"not json")
        result = ci_waits.status_diagnostics(self.store, "worker", self.receipt,
                                            {"checks": self.rows()})
        self.assertNotIn("acknowledgement", result["items"][0])
        self.assertEqual(len(ci_waits.pending(self.store, "worker")), 1)
        self.assertEqual(legacy.read_bytes(), b"not json")

    def test_evidence_io_errors_are_local_and_poll_errors_are_bounded(self):
        bad = [self.block("unreadable-" + str(i)) for i in range(4)]
        good = self.block("readable")
        original = ci_waits._read

        def unreadable(store, item):
            if item["id"] in {i["id"] for i in bad}:
                raise OSError("unreadable result")
            return original(store, item)

        with patch("mizu.ci_waits._read", side_effect=unreadable):
            result, reader = self.poll()
            reader.assert_called_once()
            self.assertEqual((result["ok"], result["polled"], result["observed"]), (False, 1, 1))
            self.assertEqual(len(result["errors"]), self.config.vcs["poll_max_branches"])
            context = self.delivery_context()
            self.assertEqual([event["work_item"] for event in context.wait_events], [good["id"]])
            self.acknowledge_delivery(context)
            self.assertEqual(ci_waits.pending(self.store, "worker"), [])

    def test_invalid_receipt_or_ledger_still_fails_globally(self):
        self.block()
        self.poll()
        context = self.delivery_context()
        self.publication.path("receipt").unlink()
        with self.assertRaises(Denied):
            ci_waits.pending(self.store, "worker")
        with self.assertRaises(Denied):
            self.acknowledge_delivery(context)
        result, reader = self.poll()
        self.assertFalse(result["ok"])
        self.assertIn("error", result)  # global scan failure, not per-item errors
        reader.assert_not_called()
        for failure in (Denied("malformed ledger"), OSError("unreadable ledger")):
            with self.subTest(failure=failure), patch.object(self.store, "_iter_item_events", side_effect=failure):
                with self.assertRaises(type(failure)):
                    ci_waits.pending(self.store, "worker")
                result, reader = self.poll()
                self.assertFalse(result["ok"])
                self.assertIn("error", result)
                reader.assert_not_called()


class PublicationCiWaitTests(CiWaitFixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs, "ci_branch": "published"})
        self.project = Project(self.config, "sample")
        self.store = self.project.work_items
        self.snapshot = self.project.snapshots.get()
        self.wait = {"kind": "publication_ci", "snapshot_id": self.snapshot["id"],
                     "checks": ["unit", "lint"]}

    def receipt_for(self, *, code_digest=None, branch="published", source="d" * 40, sha=SHA):
        publication = Publication(self.project.root, source,
                                  self.snapshot["code_digest"] if code_digest is None else code_digest,
                                  branch, "push", projection=public_source_identity(self.snapshot["files"]))
        intent, _ = publication.prepare("approval", "run")
        publication.mark_dispatch()
        return publication.complete(intent, sha)

    def test_publication_rebinding_preserves_age_and_expires_without_remote_failure(self):
        from test_blocked_wait_age import clock
        self.store.blocked_max_age_seconds = 10
        with clock(0) as start:
            item = self.block()
        with clock(3):
            doing = self.store.transition(item["id"], expected_rev=item["rev"], status="doing", actor="worker")
            child = self.descendant("new publication")
            item = self.store.transition(item["id"], expected_rev=doing["rev"], status="blocked", actor="worker",
                                         wait={**self.wait, "snapshot_id": child["id"]})
        self.assertEqual(item["blocked_since"], start)
        self.assertEqual(item["wait"]["snapshot_id"], child["id"])
        with clock(10):
            live = self.store.page_ready(role_name="worker")["items"][0]["wait_liveness"]
            self.assertEqual((live["state"], live["reason"]), ("expired", "blocked_max_age_reached"))
            self.assertNotIn("outcome", live)  # No receipt/dispatch is not a remote CI failure.
            self.assertEqual(self.store.blocked_liveness_readonly()["items"][0]["state"], "expired")
            with self.assertRaisesRegex(Denied, "Reopen"):
                self.store.transition(item["id"], expected_rev=item["rev"], status="blocked", actor="worker",
                                       wait=self.wait, scope="Rebind", reason="Try again")
            with patch("mizu.ci_waits.evidence", side_effect=Denied("corrupt exact receipt")):
                page = self.store.page_ready(role_name="worker")
                self.assertEqual((page["total"], page["unknown_blocked"]), (0, 1))
        self.assertEqual(self.store.get(item["id"]), item)

    def test_pre_receipt_wait_pins_snapshot_digest_and_config_without_polling_or_releasing(self):
        args = {"id": "wi_" + "a" * 32, "expected_rev": 1, "status": "blocked", "wait": self.wait}
        validate(args, DEFINITIONS["work_items_transition"][1])
        with self.assertRaises(Denied):
            validate({"id": "a" * 32, "action": "defer", "reason": "CI", "wait": self.wait},
                     DEFINITIONS["decide"][1])
        with self.assertRaises(Denied):
            self.project.insights._normalize_wait(self.wait, "defer")
        item = self.block()
        self.assertEqual(item["wait"], {**self.wait, "checks": ["lint", "unit"],
                                        "code_digest": self.snapshot["code_digest"], "branch": "published"})
        # Stored identities never rebase to config or a moving branch head.
        self.store.vcs = {**self.store.vcs, "ci_branch": "moved"}
        self.assertEqual(self.store.get(item["id"]), item)
        result, reader = self.poll()
        self.assertEqual(result["observed"], 0)
        reader.assert_not_called()
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.assertEqual(self.store.blocked_liveness_readonly()["items"][0]["state"], "pending")
        self.assertEqual(self.store.page_ready()["total"], 0)
        unrelated = self.store.create(actor="worker", idempotency_key="unrelated", title="Other work")
        self.assertEqual(self.store.page_ready()["items"][0]["id"], unrelated["record"]["work_item"]["id"])

    def test_registration_refuses_overrides_unpublished_unknown_and_invalid_bindings(self):
        invalid = [dict(self.wait, snapshot_id="f" * 64), dict(self.wait, code_digest="c" * 64),
                   dict(self.wait, branch="published"), dict(self.wait, publication_id=self.publication.id),
                   dict(self.wait, sha=SHA), dict(self.wait, checks=[]),
                   dict(self.wait, checks=["unit", "unit"]), dict(self.wait, checks=["x"] * 65),
                   dict(self.wait, checks=["x" * 257]), dict(self.wait, checks=["bad\nname"]),
                   dict(self.wait, checks=[str(i) + "x" * 100 for i in range(30)])]
        for index, wait in enumerate(invalid):
            with self.subTest(wait=wait), self.assertRaises(Denied):
                self.block(str(index), wait=wait)
        for settings in ({"ci_branch": ""}, {"poll_enabled": False}, {"command": []}):
            with self.subTest(settings=settings), self.assertRaises(Denied):
                with patch.object(self.store, "vcs", {**self.store.vcs, **settings}):
                    self.block(str(settings))
        with patch.object(self.project.snapshots, "_history_ids", return_value=[]):
            with self.assertRaisesRegex(Denied, "retained published"):
                self.block("unpublished")
        item = self.block("valid")
        for key, value in (("code_digest", "c" * 64), ("snapshot_id", "f" * 64)):
            with self.assertRaises(Denied):
                self.store._stored_wait({**item["wait"], key: value})

    def test_matching_receipt_terminal_result_uses_host_poll_and_explicit_replay_ack(self):
        item = self.block()
        snap = {**self.snapshot, "outcome": "done",
                "wake_generation": self.project.control()["wake_generation"]}
        self.assertFalse(should_run(self.project, snap, role_name="worker"))
        receipt = Publication.record_external(
            self.project.root, branch="published", published_sha=SHA,
            code_digest=self.snapshot["code_digest"], approval="approval", run="external-run",
            projection=public_source_identity(self.snapshot["files"]))
        # Receipt alone is not a wake; no branch-head fallback or auto dispatch.
        self.assertFalse(should_run(self.project, snap, role_name="worker"))
        with patch("mizu.runtime.poll_upstream", return_value=None), \
                patch("mizu.runtime.poll_ci", return_value=None), \
                patch("mizu.runtime.poll_proposals", return_value=None), \
                patch("mizu.vcs.invoke", return_value={"checks": self.rows("failure")}) as adapter:
            events = poll_project_vcs(self.config, self.project, {}, now=1000, interval=15)
        self.assertEqual(events[0]["observed"], 1)
        self.assertEqual(adapter.call_args.args[1], {"op": "status", "branch": "published", "sha": SHA})
        self.assertTrue(should_run(self.project, snap, role_name="worker"))
        self.assertEqual(ci_waits.pending(self.store, "searcher"), [])
        context = self.delivery_context()
        event = context.wait_events[0]
        self.assertEqual({key: event[key] for key in ("snapshot_id", "code_digest", "publication_id", "sha")},
                         {"snapshot_id": self.snapshot["id"], "code_digest": self.snapshot["code_digest"],
                          "publication_id": receipt["publication_id"], "sha": SHA})
        self.assertEqual(event["conclusion"], "failure")
        ci_waits.complete(context, {"outcome": "wait", "summary": "Not handled"})
        self.assertEqual(self.delivery_context().wait_events, context.wait_events)
        self.acknowledge_delivery(context)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.assertFalse(_role_events_due(self.project, snap, "worker"))
        self.assertTrue(should_run(self.project, snap, role_name="worker"))
        self.assertEqual(self.store.page_ready(role_name="worker")["items"][0]["wait_liveness"]["outcome"], "failure")
        result, reader = self.poll()
        self.assertEqual(result["observed"], 0)
        reader.assert_not_called()
        self.assertEqual(self.store.get(item["id"]), item)  # ack never transitions

    def test_configured_receipt_budgets_never_resolve_an_incomplete_scan(self):
        item = self.block()
        self.receipt_for()
        wait = item["wait"]
        # The fixture also has a nonmatching receipt. Even finding the only
        # match in a bounded prefix cannot establish exact uniqueness.
        for policy in (PublicationEvidence(name_processing_limit=1),
                       PublicationEvidence(record_limit=1)):
            with self.subTest(policy=policy):
                config = dataclasses.replace(self.config, publication_evidence=policy)
                store = Project(config, "sample").work_items
                self.assertEqual(store.publication_evidence, policy)
                self.assertIsNone(ci_waits._resolved(store, wait))
        policy = PublicationEvidence(name_processing_limit=10001, record_limit=1001)
        store = Project(dataclasses.replace(self.config, publication_evidence=policy), "sample").work_items
        with patch.object(Publication, "list_receipts", wraps=Publication.list_receipts) as listing:
            self.assertEqual(ci_waits._resolved(store, wait)["sha"], SHA)
        self.assertEqual(listing.call_args.kwargs["policy"], policy)
        self.assertTrue(listing.call_args.kwargs["strict"])
        self.assertEqual(listing.call_args.kwargs["limit"], 2)
        # Standalone construction retains safe, independent defaults.
        from mizu.work_items import WorkItems
        standalone = WorkItems(store.root, store.snapshots, store.insights)
        self.assertEqual(standalone.publication_evidence, PublicationEvidence())
        self.assertEqual(ci_waits._resolved(standalone, wait)["sha"], SHA)

    def test_mismatched_receipts_unknown_or_ambiguous_scans_never_query_status(self):
        self.block()
        self.receipt_for(code_digest="e" * 64)
        self.receipt_for(branch="other")
        result, reader = self.poll()
        self.assertEqual(result["observed"], 0)
        reader.assert_not_called()
        receipt = self.receipt_for()
        for page in (
            {"publications": [receipt], "total": 1, "truncated": True, "scan_stopped": False},
            {"publications": [receipt], "total": 1, "truncated": False, "scan_stopped": True},
        ):
            with patch.object(Publication, "list_receipts", return_value=page):
                result, reader = self.poll()
            self.assertEqual(result["observed"], 0)
            reader.assert_not_called()
        # One corrupt candidate can conceal a second match; ordinary listings
        # still skip it, but this strict uniqueness consumer stays pending.
        path = self.project.root / "evidence" / "publications" / ("f" * 64 + ".external.json")
        path.write_bytes(b"[]")
        self.assertEqual(Publication.list_receipts(self.project.root)["total"], 4)
        result, reader = self.poll()
        self.assertFalse(result["ok"])
        self.assertEqual(result["observed"], 0)
        reader.assert_not_called()
        path.unlink()
        self.receipt_for(source="e" * 40, sha="e" * 40)
        result, reader = self.poll()
        self.assertEqual(result["observed"], 0)
        reader.assert_not_called()
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])

    def test_unknown_status_is_pending_even_when_required_rows_appear_green(self):
        item = self.block()
        self.receipt_for()
        invalid_rows = [[], self.rows()[:1], self.rows() + self.rows()[:1],
                        self.rows(sha="e" * 40), self.rows("running"),
                        [{**row, "check": row["check"].upper()} for row in self.rows()],
                        [{**row, "state": None} for row in self.rows()]]
        for rows in invalid_rows:
            with self.subTest(rows=rows):
                result, _ = self.poll(rows=rows)
                self.assertEqual(result["observed"], 0)
        # Exercise the actual host adapter projection, not just an injected reader.
        for metadata in ({"branch": "other"}, {"sha": "e" * 40}, {"complete": False},
                         {"truncated": True}, {"scan_stopped": True}, {"cursor": "more"},
                         {"complete": None}, {"truncated": "false"}):
            with self.subTest(metadata=metadata), patch("mizu.vcs.invoke", return_value={
                    "checks": self.rows(), **metadata}):
                self.clock += 30
                result = ci_waits.poll(self.config, self.project, self.state,
                                       now=self.clock, interval=15)
                self.assertEqual(result["observed"], 0)
        result, _ = self.poll(reader=Mock(return_value=None))
        self.assertEqual(result["observed"], 0)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.assertEqual(self.store.get(item["id"]), item)
        result, _ = self.poll()
        self.assertEqual(result["observed"], 1)
        self.assertEqual(ci_waits.pending(self.store, "worker")[0]["conclusion"], "success")

    def test_evidence_revalidates_unique_receipt_and_stale_waiter_tokens(self):
        item = self.block()
        self.receipt_for()
        self.poll()
        context = self.delivery_context()
        # Later ambiguity refuses delivery and acknowledgement rather than
        # trusting the previously stored terminal payload in isolation.
        self.assertEqual(self.store.page_ready(role_name="worker")["total"], 1)
        self.receipt_for(source="e" * 40)
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])
        self.assertEqual(self.store.page_ready(role_name="worker")["unknown_blocked"], 1)
        with self.assertRaises(Denied):
            self.acknowledge_delivery(context)
        opened = self.store.transition(item["id"], expected_rev=2, status="open", actor="worker")
        with self.assertRaises(Denied):
            self.acknowledge_delivery(context)
        self.assertEqual(self.store.get(item["id"]), opened)


    def test_receipt_ambiguity_during_status_read_cannot_publish_result(self):
        item = self.block()
        self.receipt_for()
        def reader(*_):
            self.receipt_for(source="e" * 40)
            return {"checks": self.rows()}
        result, _ = self.poll(reader=reader)
        self.assertEqual(result["observed"], 0)
        self.assertIsNone(ci_waits._read(self.store, item))
        self.assertEqual(ci_waits.pending(self.store, "worker"), [])

    def test_unknown_publication_does_not_hide_existing_receipt_pinned_ci_result(self):
        self.block()
        old = self.block("receipt-pinned", wait={"kind": "ci", "publication_id": self.publication.id,
                                                "checks": ["unit", "lint"]})
        self.receipt_for()
        path = self.project.root / "evidence" / "publications" / ("f" * 64 + ".external.json")
        path.write_bytes(b"[]")
        result, reader = self.poll()
        self.assertEqual(result["observed"], 1)
        self.assertFalse(result["ok"])
        reader.assert_called_once_with(self.config.vcs, "status", {"branch": "published", "sha": SHA})
        self.assertEqual([event["work_item"] for event in ci_waits.pending(self.store, "worker")],
                         [old["id"]])

    def descendant(self, label, *, parent=None, publish=True, verified=False):
        snapshots = self.project.snapshots
        (self.project.workspace / 'descendant.txt').write_text(label)
        captured = snapshots.capture_files(self.project.workspace)
        verification = ({'passed': True, 'code_digest': captured['code_digest'], 'run': 'offline'}
                        if verified else None)
        snap = snapshots.create(captured, goal='Goal', state='State', run='offline',
                                outcome='wait', summary=label, verification=verification,
                                **({'parent': parent} if parent else {}))
        if publish:
            snapshots.publish(snap)
        return snap

    def publish_descendant(self, snapshot, sha=SHA, **kwargs):
        return self.receipt_for(code_digest=snapshot['code_digest'], sha=sha, **kwargs)

    def poll_until_observed(self, reader=None, *, ticks=8):
        for _ in range(ticks):
            result, adapter = self.poll(reader=reader)
            self.assertLessEqual(adapter.call_count if isinstance(adapter, Mock) else 0,
                                 self.config.vcs['poll_max_branches'])
            if result['observed']:
                return result
        self.fail('No terminal candidate observed within bounded rotation')

    def test_descendant_evidence_is_frozen_and_completion_requires_explicit_binding(self):
        item = self.block()
        child = self.descendant('verified child', verified=True)
        receipt = self.publish_descendant(child)
        self.poll_until_observed()
        context = self.delivery_context()
        event = context.wait_events[0]
        self.assertEqual(event['observed_snapshot'],
                         {'snapshot_id': child['id'], 'code_digest': child['code_digest']})
        self.assertEqual(event['snapshot_id'], item['wait']['snapshot_id'])
        self.assertEqual(event['code_digest'], item['wait']['code_digest'])
        self.assertEqual(event['publication_id'], receipt['publication_id'])
        self.assertEqual(event['conclusion'], 'success')
        # A later completed publication cannot redirect source validation/ack.
        later = self.descendant('later')
        self.publish_descendant(later, sha='e' * 40, source='e' * 40)
        with patch.object(self.project.snapshots, '_history_ids', return_value=[later['id']]):
            self.assertEqual(self.delivery_context().wait_events, context.wait_events)
            self.acknowledge_delivery(context)
        self.assertEqual(ci_waits.pending(self.store, 'worker'), [])
        self.assertEqual(self.store.page_ready(role_name='worker')['items'][0]['wait_liveness']['outcome'], 'success')
        self.assertEqual(self.store.get(item['id']), item)
        opened = self.store.transition(item['id'], expected_rev=item['rev'], status='open', actor='worker',
                                       reason='receipt_route_terminal: validate delivery before closing')
        doing = self.store.transition(item['id'], expected_rev=opened['rev'], status='doing', actor='worker')
        with self.assertRaisesRegex(Denied, 'prior exact snapshot'):
            self.store.transition(item['id'], expected_rev=doing['rev'], status='done', actor='worker',
                                  snapshot_id=child['id'])
        bound = self.store.transition(item['id'], expected_rev=doing['rev'], status='doing', actor='worker',
                                      snapshot_id=child['id'])
        self.assertEqual(bound['scope'], doing['scope'])
        self.assertEqual(bound['delivery']['snapshot_id'], child['id'])
        done = self.store.transition(item['id'], expected_rev=bound['rev'], status='done', actor='worker',
                                     snapshot_id=child['id'])
        self.assertEqual(done['status'], 'done')

    def test_disabled_descendants_preserve_exact_resolution_and_evidence_shape(self):
        item = self.block()
        child = self.descendant('child')
        self.publish_descendant(child)
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs, 'ci_wait_descendants': False})
        result, adapter = self.poll()
        self.assertEqual(result['observed'], 0)
        adapter.assert_not_called()
        self.receipt_for()
        self.poll_until_observed()
        event = self.delivery_context().wait_events[0]
        self.assertNotIn('observed_snapshot', event)
        self.assertEqual(event['snapshot_id'], item['wait']['snapshot_id'])
        self.assertEqual(set(ci_waits._read(self.store, item)), {
            'kind', 'trust', 'work_item', 'rev', 'holder', 'publication_id', 'branch', 'sha',
            'snapshot_id', 'code_digest', 'wait_digest', 'checks', 'conclusion', 'observed_at'})

    def test_descendant_ancestry_is_strict_complete_and_edge_bounded(self):
        item = self.block()
        one = self.descendant('one')
        two = self.descendant('two')
        three = self.descendant('three')
        for snap in (one, two, three):
            self.publish_descendant(snap, source=snap['id'][:40])
        with patch.object(self.project.snapshots, 'history_index', 2):
            resolved = ci_waits._candidate_wait(self.store, item['wait'], two['id'])
            self.assertEqual(resolved['observed_snapshot']['snapshot_id'], two['id'])
            for sid in (three['id'], self.snapshot['id']):
                with self.subTest(sid=sid), self.assertRaises(Denied):
                    ci_waits._candidate_wait(self.store, item['wait'], sid)
            with patch.object(self.project.snapshots, '_history_ids', return_value=[three['id']]):
                result, adapter = self.poll()
                self.assertEqual(result['observed'], 0)
                adapter.assert_not_called()
            with patch.object(self.project.snapshots, '_history_ids', return_value=[two['id']]):
                self.poll_until_observed()
            self.assertEqual(self.delivery_context().wait_events[0]['observed_snapshot']['snapshot_id'], two['id'])
        # Losing a middle manifest is not permission to infer ancestry from history order.
        path = self.project.snapshots.root / 'snapshots' / (one['id'] + '.json')
        raw = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(Denied, 'not found'):
            ci_waits._candidate_wait(self.store, item['wait'], two['id'])
        path.write_bytes(raw)
        # A retained-looking legacy root cannot prove the edge, nor can a sibling.
        legacy = {k: v for k, v in three.items() if k != 'parent'}
        from mizu.fs import digest
        legacy['id'] = digest(canonical({k: v for k, v in legacy.items() if k != 'id'}))
        write_json(self.project.snapshots.root / 'snapshots' / (legacy['id'] + '.json'), legacy)
        with self.assertRaisesRegex(Denied, 'not a newer descendant'):
            ci_waits._candidate_wait(self.store, item['wait'], legacy['id'])
        sibling = self.descendant('sibling', parent=self.snapshot['id'], publish=False)
        child_wait = {**item['wait'], 'snapshot_id': one['id'], 'code_digest': one['code_digest']}
        with self.assertRaisesRegex(Denied, 'not a newer descendant'):
            ci_waits._candidate_wait(self.store, child_wait, sibling['id'])
        with patch.object(self.project.snapshots, '_history_ids', return_value=[
                self.snapshot['id'], sibling['id'], legacy['id'], one['id'], two['id']]):
            # Neither retained-looking siblings/legacy roots nor real ancestors
            # displace valid status targets. Order alone grants no ancestry.
            candidates = list(ci_waits._candidates(self.store, {**item, 'wait': child_wait}, True))
            self.assertEqual(candidates, [(one['id'], None), (two['id'], two['id'])])
            self.assertEqual(list(ci_waits._candidates(
                self.store, {**item, 'wait': child_wait}, True, newest_first=True)), candidates)

    def test_candidate_discovery_reads_linear_manifests_in_any_history_order(self):
        item = self.block()
        snapshots = self.project.snapshots
        children = [self.descendant(str(i))['id'] for i in range(64)]
        retained = snapshots._history_ids()
        self.assertTrue(set(children).issubset(retained))
        orders = [retained, list(reversed(retained)), retained[::2] + retained[1::2]]
        for history in orders:
            for newest_first in (False, True):
                with self.subTest(history=history[:2], newest_first=newest_first), patch.object(
                        snapshots, '_history_ids', return_value=history), patch.object(
                        snapshots, 'validate_manifest', wraps=snapshots.validate_manifest) as reads:
                    candidates = list(ci_waits._candidates(
                        self.store, item, True, newest_first=newest_first))
                ordered = reversed(history) if newest_first else history
                self.assertEqual(candidates, [(self.snapshot['id'], None)] + [
                    (sid, sid) for sid in ordered if sid in children])
                self.assertEqual(len(candidates), 65)
                self.assertLessEqual(reads.call_count, len(children) + 1)

    def test_candidate_memo_preserves_bounds_branches_duplicates_and_revalidates(self):
        item = self.block()
        snapshots = self.project.snapshots
        one, two, three, four = [self.descendant(str(i)) for i in range(4)]
        # The first walk runs out of edges. It must not poison the still-eligible
        # older descendants whose links that walk already validated.
        with patch.object(snapshots, 'history_index', 3), patch.object(
                snapshots, '_history_ids', return_value=[four['id'], three['id'], two['id']]):
            self.assertEqual(list(ci_waits._candidates(self.store, item, True)), [
                (self.snapshot['id'], None), (three['id'], three['id']), (two['id'], two['id'])])
        sibling = self.descendant('sibling', parent=self.snapshot['id'], publish=False)
        child_wait = {**item, 'wait': {**item['wait'], 'snapshot_id': one['id'],
                                    'code_digest': one['code_digest']}}
        history = [four['id'], sibling['id'], two['id'], three['id'], two['id'], one['id']]
        path = snapshots.root / 'snapshots' / (two['id'] + '.json')
        raw = path.read_bytes()
        for corrupt in (False, True):
            if corrupt:
                write_json(path, {**two, 'parent': self.snapshot['id']})
            else:
                path.unlink()
            with self.subTest(corrupt=corrupt), patch.object(snapshots, '_history_ids', return_value=history):
                self.assertEqual(list(ci_waits._candidates(self.store, child_wait, True)), [(one['id'], None)])
                # An unrelated valid branch still qualifies for the original wait.
                self.assertEqual(list(ci_waits._candidates(self.store, item, True)), [
                    (self.snapshot['id'], None), (sibling['id'], sibling['id']), (one['id'], one['id'])])
            path.write_bytes(raw)
            # No cache survives a discovery call; repaired links are read again.
            with patch.object(snapshots, '_history_ids', return_value=history):
                self.assertEqual(list(ci_waits._candidates(self.store, child_wait, True)), [
                    (one['id'], None), (four['id'], four['id']),
                    (two['id'], two['id']), (three['id'], three['id'])])

    def test_newer_pending_candidate_does_not_starve_older_red_candidate(self):
        item = self.block()
        older = self.descendant('older')
        newer = self.descendant('newer')
        self.publish_descendant(older)
        self.publish_descendant(newer, sha='e' * 40, source='e' * 40)
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs, 'poll_max_branches': 1})
        self.state['ci_wait_cursor'] = f"{item['id']}:{item['rev']:020d}:{int(newer['id'], 16) - 1:064x}"
        seen = []
        def reader(settings, op, params):
            seen.append(params)
            return {'checks': self.rows('failure') if params['sha'] == SHA else []}
        result, _ = self.poll(reader=reader)
        self.assertEqual(result['observed'], 0)
        self.assertEqual(seen, [{'branch': 'published', 'sha': 'e' * 40}])
        self.poll_until_observed(reader)
        event = self.delivery_context().wait_events[0]
        self.assertEqual(event['observed_snapshot']['snapshot_id'], older['id'])
        self.assertEqual(event['conclusion'], 'failure')
        self.assertEqual(self.store.get(item['id']), item)
        self.assertTrue(all(p['branch'] == 'published' for p in seen))

    def test_ambiguous_descendant_is_isolated_and_status_still_requires_exact_complete_coverage(self):
        item = self.block()
        ambiguous = self.descendant('ambiguous')
        self.publish_descendant(ambiguous, sha='a' * 40)
        self.publish_descendant(ambiguous, sha='e' * 40, source='e' * 40)
        healthy = self.descendant('healthy')
        self.publish_descendant(healthy, branch='other', sha='f' * 40, source='f' * 40)
        # Neither a wrong-branch receipt nor the ambiguous pair can query status.
        for _ in range(2):
            _, adapter = self.poll()
            adapter.assert_not_called()
        receipt = self.publish_descendant(healthy, source='1' * 40)
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs, 'poll_max_branches': 4})
        bad = [{'checks': self.rows()[:1]}, {'checks': self.rows(sha='e' * 40)},
               {'checks': self.rows(), 'branch': 'other'}, {'checks': self.rows(), 'sha': 'e' * 40},
               {'checks': self.rows(), 'complete': False}, {'checks': self.rows(), 'cursor': 'more'}]
        for response in bad:
            with self.subTest(response=response):
                result, adapter = self.poll(reader=Mock(return_value=response))
                self.assertEqual(result['observed'], 0)
                adapter.assert_called_once_with(self.config.vcs, 'status', {'branch': 'published', 'sha': SHA})
        self.poll_until_observed()
        event = self.delivery_context().wait_events[0]
        self.assertEqual(event['publication_id'], receipt['publication_id'])
        self.assertEqual(event['observed_snapshot']['snapshot_id'], healthy['id'])
        self.assertEqual(event['snapshot_id'], item['wait']['snapshot_id'])

    def test_frozen_descendant_evidence_is_revalidated_for_delivery_and_ack(self):
        item = self.block()
        child = self.descendant('child')
        receipt = self.publish_descendant(child)
        self.poll_until_observed()
        context = self.delivery_context()
        original = ci_waits._read(self.store, item)
        path = ci_waits._path(self.store, item)
        for changes in ({'sha': 'e' * 40}, {'publication_id': 'e' * 64},
                        {'observed_snapshot': {'snapshot_id': child['id'], 'code_digest': 'f' * 64}},
                        {'observed_snapshot': {'snapshot_id': None, 'code_digest': child['code_digest']}}):
            with self.subTest(changes=changes):
                write_json(path, {**original, **changes})
                self.assertEqual(ci_waits.pending(self.store, 'worker'), [])
                with self.assertRaises(Denied):
                    self.acknowledge_delivery(context)
        write_json(path, original)
        manifest = self.project.snapshots.root / 'snapshots' / (child['id'] + '.json')
        raw = manifest.read_bytes()
        write_json(manifest, {**child, 'parent': None})  # invalid immutable identity
        with self.assertRaisesRegex(Denied, 'integrity'):
            self.acknowledge_delivery(context)
        manifest.write_bytes(raw)
        # New ambiguity for the frozen digest refuses ack, rather than selecting another descendant.
        self.publish_descendant(child, source='e' * 40)
        with self.assertRaisesRegex(Denied, 'unique'):
            self.acknowledge_delivery(context)
        self.assertEqual(ci_waits._read(self.store, item), original)
        self.assertEqual(original['publication_id'], receipt['publication_id'])

    def test_rotation_caps_calls_and_covers_item_revision_candidate_targets_after_errors(self):
        self.receipt_for()
        children = [self.descendant(str(i)) for i in range(3)]
        for i, child in enumerate(children):
            self.publish_descendant(child, source=str(i + 1) * 40, sha=str(i + 1) * 40)
        items = [self.block(str(i)) for i in range(2)]
        seen = []
        def reader(settings, op, params):
            seen.append(self.state['ci_wait_cursor'])
            if len(seen) == 1:
                raise Denied(f"/tmp/private/path for {items[0]['id']}: arbitrary adapter note")
            return {'checks': []}
        for _ in range(4):
            before = len(seen)
            result, _ = self.poll(reader=reader)
            self.assertEqual(result['polled'], 2)
            self.assertEqual(len(seen) - before, 2)
            if before == 0:
                self.assertEqual(result['errors'], ['denied'])
                rendered = json.dumps(result)
                self.assertNotIn(items[0]['id'], rendered)
                self.assertNotIn('/tmp/private/path', rendered)
                self.assertNotIn('arbitrary adapter note', rendered)
        self.assertEqual(len(set(seen)), 8)
        self.assertEqual(set(seen), {f"{item['id']}:{item['rev']:020d}:{snap['id']}"
                                    for item in items for snap in [self.snapshot, *children]})
        self.assertEqual(set(self.state), {'last_ci_wait_poll', 'ci_wait_cursor'})
        self.assertEqual(ci_waits.pending(self.store, 'worker'), [])
