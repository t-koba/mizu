"""Offline exact VCS change-proposal merge waits; fake observations, no live merge."""
import dataclasses
import json
import uuid
from unittest.mock import Mock, patch

from support import Fixture, ScriptDriver, without_blocked_age
from mizu import delivery_settlement, proposal_waits, vcs, work_item_resolution
from mizu.distribution import public_source_identity
from mizu.errors import Denied
from mizu.fs import canonical, digest, read_json, write_json
from mizu.project import Project
from mizu.protocol import DEFINITIONS, validate
from mizu.publication import Publication
from mizu.runtime import Context, Engine, _role_events_due
from mizu.vcs_proposal_poll import poll_proposals

SHA = "b" * 40


class ProposalWaitTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "poll_enabled": True, "command": ["configured-adapter"],
            "observation_queue": "proposals"})
        self.project = Project(self.config, "sample")
        self.store = self.project.work_items
        publication = Publication(self.project.root, "a" * 40, "c" * 64, "published", "push",
                                  projection=public_source_identity({}))
        intent, _ = publication.prepare("approval", "run")
        publication.mark_dispatch()
        self.receipt = publication.complete(intent, SHA)
        self.wait = {"kind": "proposal_merged", "proposal_id": "forge:owner/repo#1",
                     "publication_id": publication.id}
        self.row = {"id": self.wait["proposal_id"], "state": "open", "draft": False,
                    "head": {"repo": "owner/repo", "ref": "refs/heads/published", "sha": SHA},
                    "base": {"repo": "owner/repo", "ref": "main", "sha": "d" * 40}}
        self.clock = 1000
        self.state = {}
        self.serial = 0

    def poll(self, rows=None, **metadata):
        self.clock += 30
        return poll_proposals(self.config, self.project, self.state,
            now=self.clock, interval=15,
            reader=Mock(return_value={"proposals": [self.row] if rows is None else rows,
                                      "complete": True, **metadata}))

    def block(self, key=None, actor="worker", wait=None):
        self.serial += 1
        seed = self.store.create(actor=actor, idempotency_key=key or f"item-{self.serial}",
                                 title="Observe exact proposal merge")["record"]["work_item"]
        return self.store.transition(seed["id"], expected_rev=seed["rev"], status="blocked",
                                     actor=actor, wait=self.wait if wait is None else wait)

    def context(self, role_name="worker", *, limit=10):
        run = self.project.root / "runs" / uuid.uuid4().hex
        run.mkdir(parents=True)
        ctx = Context(self.config, self.project, self.config.roles[role_name], run,
                      self.project.snapshots.get(), self.project.workspace)
        ctx.wait_events, ctx.proposal_wait_manifest = proposal_waits.delivery(
            self.store, role_name, limit=limit)
        return ctx

    def test_exact_binding_pins_endpoints_and_stays_stored(self):
        self.assertTrue(self.poll()["ok"])
        blocked = self.block()
        expected = {**self.wait, **{k: self.row[k] for k in ("head", "base")}}
        self.assertEqual(blocked["wait"], expected)
        self.assertEqual(self.store.get(blocked["id"]), blocked)
        with self.assertRaises(Denied):
            self.block(wait=expected)  # caller cannot supply its own pins
        with self.assertRaises(Denied):
            self.store.transition(blocked["id"], expected_rev=1, status="blocked",
                                  actor="worker", wait=self.wait)
        with patch.dict(self.store.vcs, {"poll_enabled": False}), self.assertRaises(Denied):
            self.block(wait=self.wait)

    def test_wrong_receipt_proposal_or_endpoints_denied(self):
        with self.assertRaises(Denied):  # no observation yet
            self.block()
        self.assertTrue(self.poll()["ok"])
        for bad in ({"proposal_id": "absent"}, {"proposal_id": ""},
                    {"publication_id": "f" * 64}, {"publication_id": "../unsafe"},
                    {"head": self.row["head"]}):
            with self.subTest(bad=bad), self.assertRaises(Denied):
                self.block(wait={**self.wait, **bad})
        for part, field, value in (("head", "sha", "e" * 40), ("head", "ref", "moved")):
            changed = {"id": self.row["id"], "state": "open", "draft": False,
                       "head": dict(self.row["head"]), "base": dict(self.row["base"])}
            changed[part][field] = value
            self.assertTrue(self.poll([changed])["ok"])
            with self.subTest(part=part, field=field), self.assertRaises(Denied):
                self.block()
        missing = {"id": self.row["id"], "state": "open", "draft": False,
                   "head": dict(self.row["head"]),
                   "base": {"repo": "owner/repo", "ref": "main", "deleted": True}}
        self.assertTrue(self.poll([missing])["ok"])
        with self.assertRaises(Denied):
            self.block()
        self.assertTrue(self.poll()["ok"])
        registered = self.block()
        self.assertEqual(registered["wait"]["base"], self.row["base"])
        with self.assertRaises(Denied):
            self.block(wait={**registered["wait"],
                "base": {"repo": "owner/repo", "ref": "main", "sha": "e" * 40}})

    def test_partial_enumeration_never_binds_or_releases(self):
        self.assertTrue(self.poll(complete=False, cursor="page-two")["ok"])
        with self.assertRaises(Denied):
            self.block()
        # A terminal page with an input cursor is still not a complete enumeration.
        self.assertTrue(self.poll(complete=True)["ok"])
        with self.assertRaises(Denied):
            self.block()
        self.assertTrue(self.poll()["ok"])
        blocked = self.block()
        self.row["state"] = "merged"
        self.assertTrue(self.poll(complete=False, cursor="next")["ok"])
        self.assertEqual(proposal_waits.pending(self.store, "worker"), [])
        self.assertEqual(self.store.get(blocked["id"]), blocked)
        self.assertTrue(self.poll()["ok"])
        self.assertEqual(proposal_waits.pending(self.store, "worker"), [])

    def test_only_exact_merge_in_complete_enumeration_delivers(self):
        self.poll()
        blocked = self.block()
        for state in ("open", "closed"):
            self.row["state"] = state
            self.assertTrue(self.poll()["ok"])
            self.assertEqual(proposal_waits.pending(self.store, "worker"), [])
        self.row["state"] = "merged"
        for part, field, value in (("head", "sha", "e" * 40), ("base", "ref", "moved"),
                                   ("base", "sha", "e" * 40)):
            changed = {"id": self.row["id"], "state": "merged", "draft": False,
                       "head": dict(self.row["head"]), "base": dict(self.row["base"])}
            changed[part][field] = value
            self.assertTrue(self.poll([changed])["ok"])
            self.assertEqual(proposal_waits.pending(self.store, "worker"), [], (part, field))
        self.assertTrue(self.poll([])["ok"])
        self.assertTrue(self.poll([{**self.row, "id": "forge:owner/repo#2"}])["ok"])
        self.assertEqual(proposal_waits.pending(self.store, "worker"), [])
        self.assertTrue(self.poll()["ok"])
        events = proposal_waits.pending(self.store, "worker")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["work_item"], blocked["id"])
        self.assertEqual(events[0]["state"], "merged")
        self.assertEqual(events[0]["wait"], blocked["wait"])
        self.assertEqual(events[0]["trust"], "external-untrusted")
        self.assertEqual(proposal_waits.pending(self.store, "reviewer"), [])
        self.assertEqual(self.store.get(blocked["id"]), blocked)
        self.assertTrue(_role_events_due(self.project, "worker"))

    def test_observation_and_ack_never_transition_work_item(self):
        self.poll()
        blocked = self.block()
        self.row["state"] = "merged"
        with patch("mizu.proposal_waits.observe"):
            self.assertTrue(self.poll()["ok"])
        self.assertEqual(self.store.page_ready(role_name="worker")["total"], 0)
        self.assertTrue(self.poll()["ok"])
        ready = self.store.page_ready(role_name="worker")
        self.assertEqual(ready["total"], 1)
        live = ready["items"][0]["wait_liveness"]
        self.assertEqual((live["state"], live["outcome"]), ("terminal", "merged"))
        self.assertIsNone(work_item_resolution.observe(self.store, blocked))
        ctx = self.context()
        self.assertEqual(live["evidence"],
                         {"record_digest": ctx.proposal_wait_manifest[0].record_digest})
        delivery_settlement.register(ctx)
        families = set(delivery_settlement._read(
            self.project.root, ctx.run_dir.name)["manifests"])
        self.assertEqual(families, {"ci", "proposal"})
        original = ctx.wait_events
        self.poll()
        self.assertEqual(proposal_waits.pending(self.store, "worker"), original)
        token = original[0]["ack_token"]
        proposal_waits.complete(ctx, {"outcome": "wait"})
        with self.assertRaises(Denied):
            ctx.handle("finish", {"outcome": "wait", "summary": "bad ack",
                                  "ack_events": ["f" * 64]})
        finish = {"outcome": "wait", "summary": "Merge observed", "state": "Merge observed",
                  "ack_events": [token]}
        ctx.handle("finish", finish)
        proposal_waits.complete(ctx, finish)
        proposal_waits.complete(ctx, finish)
        self.assertEqual(proposal_waits.pending(self.store, "worker"), [])
        self.assertEqual(without_blocked_age(self.store.page_ready(role_name="worker")),
                         without_blocked_age(ready))
        self.assertEqual(self.store.get(blocked["id"]), blocked)

    def test_unsafe_or_corrupt_evidence_fails_closed(self):
        self.poll()
        blocked = self.block()
        self.row["state"] = "merged"
        self.poll()
        self.context()
        shared = proposal_waits.shared_store(self.store)
        record_id = proposal_waits._record_id(blocked)
        envelope, _ = shared.get(record_id)
        path = self.project.root / shared.record_name(record_id)
        for changes in ({"holder": "reviewer"}, {"rev": blocked["rev"] + 1},
                        {"wait": {**blocked["wait"], "proposal_id": "forge:owner/repo#2"}},
                        {"state": "open"}):
            with self.subTest(changes=changes):
                write_json(path, {**envelope, "payload": {**envelope["payload"], **changes}})
                page = self.store.page_ready(role_name="worker")
                self.assertEqual(page["total"], 0)
                self.assertEqual(page["unknown_blocked"], 1)
                with self.assertRaises(Denied):
                    proposal_waits.pending(self.store, "worker")
        path.write_text("not JSON")
        self.assertEqual(self.store.page_ready()["unknown_blocked"], 1)
        with self.assertRaises(Denied):
            proposal_waits.pending(self.store, "worker")
        write_json(path, envelope)
        target = self.project.root / "unrelated.json"
        write_json(target, {})
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(Denied):
            proposal_waits.pending(self.store, "worker")
        path.unlink()
        write_json(path, envelope)
        receipt = self.project.root / shared._receipt_name("worker", record_id)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.symlink_to(target)
        with self.assertRaises(Denied):
            proposal_waits.pending(self.store, "worker")

    def test_missing_or_corrupt_inventory_denies_registration_not_stored_waits(self):
        self.assertTrue(self.poll()["ok"])
        blocked = self.block()
        path = self.project.root / "vcs-poll.json"
        for bad in (None, [], {}, {"proposal_complete_messages": None},
                    {"proposal_complete_messages": []},
                    {"proposal_complete_messages": {self.row["id"]: "invalid"}},
                    {"proposal_complete_messages": {"bad\nidentity": "a" * 32}},
                    {"proposal_complete_messages": {str(i): "a" * 32
                                                    for i in range(vcs.MAX_PROPOSALS + 1)}}):
            with self.subTest(bad=bad):
                write_json(path, bad)
                with self.assertRaises(Denied):
                    self.block()
                self.assertEqual(self.store.get(blocked["id"]), blocked)
        path.write_text("not JSON")
        with self.assertRaises(Denied):
            self.block()
        path.unlink()
        with self.assertRaises(Denied):
            self.block()

    def test_inventory_write_failure_prevents_row_writes(self):
        self.poll()
        before = read_json(self.project.root / "vcs-poll.json")
        with (patch("mizu.vcs_proposal_poll.write_json", side_effect=OSError("disk full")),
              patch("mizu.vcs.record_proposal_state") as record):
            result = self.poll()
        self.assertFalse(result["ok"])
        record.assert_not_called()
        self.assertEqual(read_json(self.project.root / "vcs-poll.json"), before)

    def test_changed_complete_facts_refuse_stale_registration(self):
        self.assertTrue(self.poll()["ok"])
        blocked = self.block()
        changed = {**self.row, "head": {**self.row["head"], "sha": "e" * 40}}
        self.assertTrue(self.poll([changed])["ok"])
        with self.assertRaises(Denied):
            self.block()
        self.assertEqual(self.store.get(blocked["id"]), blocked)
        self.assertEqual(proposal_waits.pending(self.store, "worker"), [])

    def test_foreign_ambiguous_or_tampered_observation_denied(self):
        self.assertTrue(self.poll()["ok"])
        path = self.project.root / "vcs-poll.json"
        pid = self.row["id"]

        def point_at(message_id):
            state = dict(read_json(path))
            write_json(path, {**state, "proposal_complete_messages": {pid: message_id}})

        message_id = read_json(path)["proposal_complete_messages"][pid]
        self.assertTrue(self.block()["wait"]["kind"] == "proposal_merged")
        queues = self.project.kernel_queues
        title, body = vcs.proposal_facts(
            vcs.parse_proposals({"proposals": [self.row]})[0], enumeration_complete=True)
        foreign_id = "f" * 32
        queues.append("proposals", {"title": title, "body": body}, "operator", id=foreign_id)
        point_at(foreign_id)
        with self.assertRaisesRegex(Denied, "host-recorded"):
            self.block()
        tampered_id = "e" * 32
        queues.append("proposals", {"title": title, "body": body}, "host", id=tampered_id)
        point_at(tampered_id)
        with self.assertRaisesRegex(Denied, "differs from its facts"):
            self.block()
        changed = body.replace("head: owner/repo refs/heads/published " + SHA,
                               "head: owner/repo refs/heads/published " + SHA + "\nhead: moved")
        changed_id = digest(canonical({"proposal": pid, "title": title, "body": changed}))[:32]
        queues.append("proposals", {"title": title, "body": changed}, "host", id=changed_id)
        point_at(changed_id)
        with self.assertRaisesRegex(Denied, "Ambiguous"):
            self.block()
        point_at(message_id)
        self.assertTrue(self.block()["wait"]["kind"] == "proposal_merged")

    def test_bounded_finishes_and_partial_ack_replay(self):
        self.poll()
        items = sorted([self.block() for _ in range(3)],
                       key=lambda i: (i["updated_at"], i["id"]))
        self.row["state"] = "merged"
        self.poll()
        before = proposal_waits.pending(self.store, "worker", limit=2)
        self.assertEqual([e["work_item"] for e in before], [i["id"] for i in items[:2]])
        ctx = self.context(limit=2)
        self.assertEqual(ctx.wait_events, before)
        token = ctx.wait_events[0]["ack_token"]
        for flags in ({"outcome": "bounded"}, {"wait_for_check": "queued-check"}):
            proposal_waits.complete(ctx, {"ack_events": [token], **flags})
            self.assertEqual(proposal_waits.pending(self.store, "worker", limit=2), before)
        ctx.bound_hit = "requests"
        proposal_waits.complete(ctx, {"ack_events": [token]})
        self.assertEqual(proposal_waits.pending(self.store, "worker", limit=2), before)
        ctx.bound_hit = None
        with self.assertRaises(Denied):
            proposal_waits.validate_selection(ctx, [token, token])
        ctx.role = self.config.roles["reviewer"]
        with self.assertRaises(Denied):
            proposal_waits.validate_selection(ctx, [token])
        ctx.role = self.config.roles["worker"]
        proposal_waits.complete(ctx, {"ack_events": [token]})
        self.assertEqual([e["work_item"] for e in proposal_waits.pending(
            self.store, "worker", limit=2)], [i["id"] for i in items[1:]])
        self.poll()
        restarted = Project(self.config, "sample")
        self.assertEqual(proposal_waits.pending(restarted.work_items, "worker", limit=2),
                         proposal_waits.pending(self.store, "worker", limit=2))

    def test_engine_delivery_restart_and_explicit_ack(self):
        self.poll()
        blocked = self.block(actor="reviewer")
        self.row["state"] = "merged"
        self.poll()
        before = proposal_waits.pending(self.store, "reviewer")
        roles = dict(self.config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], on_change=False)
        config = dataclasses.replace(self.config, roles=roles)

        def finish(ctx, prompt, profile=None):
            rows = [e for e in json.loads(prompt)["wait_events"]
                    if e["kind"] == "proposal_merged"]
            self.assertEqual(rows, before)
            ctx.handle("finish", {"outcome": "wait", "summary": "Observed exact merge",
                                  "ack_events": [rows[0]["ack_token"]]})

        def fail_result(path, value, **kwargs):
            if path.name == "result.json" and value.get("status") == "completed":
                raise OSError("result persistence interrupted")
            return write_json(path, value, **kwargs)

        with patch("mizu.runtime.write_json", side_effect=fail_result), self.assertRaises(OSError):
            Engine(config, driver=ScriptDriver(finish)).run(self.project, "reviewer")
        restarted = Project(config, "sample")
        self.assertEqual(proposal_waits.pending(restarted.work_items, "reviewer"), before)
        Engine(config, driver=ScriptDriver(finish)).run(restarted, "reviewer")
        self.assertEqual(proposal_waits.pending(restarted.work_items, "reviewer"), [])
        self.assertEqual(restarted.work_items.get(blocked["id"]), blocked)

    def test_dispatch_projects_proposal_wait_events(self):
        self.poll()
        self.block()
        self.row["state"] = "merged"
        self.poll()
        engine = Engine(self.config, driver=ScriptDriver())
        state = engine._dispatch_state(self.project, "worker", self.config.roles["worker"],
                                       event_trigger=False)
        events = [e for e in state["wait_events"] if e["kind"] == "proposal_merged"]
        self.assertEqual(len(events), 1)
        self.assertEqual(state["proposal_manifest"][0].record_id, events[0]["record_id"])
        validate({"id": "item", "expected_rev": 1, "status": "blocked", "wait": self.wait},
                 DEFINITIONS["work_items_transition"][1])
