"""Offline exact-proposal waits; fake adapter observations, no live merge."""
import copy
import dataclasses
import json
from unittest.mock import Mock, patch

from support import Fixture, ScriptDriver
from mizu import proposal_waits, vcs
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
            "poll_enabled": True, "poll_proposals_enabled": True, "command": ["configured-adapter"]})
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

    def seed(self, key=None, actor="worker"):
        self.serial += 1
        key = key or f"item-{self.serial}"
        return self.store.create(actor=actor, idempotency_key=key,
                                 title="Observe exact proposal merge")["record"]["work_item"]

    def block(self, seed=None, wait=None, actor="worker"):
        seed = seed or self.seed(actor=actor)
        return self.store.transition(seed["id"], expected_rev=seed["rev"], status="blocked", actor=actor,
                                     wait=self.wait if wait is None else wait)

    def context(self):
        run = self.project.root / "runs" / "proposal-test"
        run.mkdir(parents=True, exist_ok=True)
        ctx = Context(self.config, self.project, self.config.roles["worker"], run,
                      self.project.snapshots.get(), self.project.workspace)
        ctx.wait_events, ctx.proposal_wait_manifest = proposal_waits.delivery(self.store, "worker")
        return ctx

    def test_registration_pins_receipt_and_both_endpoints(self):
        self.assertTrue(self.poll()["ok"])
        blocked = self.block()
        self.assertEqual(blocked["wait"], {**self.wait, **{k: self.row[k] for k in ("head", "base")}})
        self.assertEqual(self.store.get(blocked["id"]), blocked)
        self.assertEqual(self.store.page_ready()["total"], 0)
        live = self.store.blocked_liveness_readonly()
        self.assertEqual(live["items"][0]["state"], "receipt_bound")
        with self.assertRaises(Denied):
            self.block(wait=blocked["wait"])  # Caller cannot supply its own pins.
        with self.assertRaises(Denied):
            self.store.transition(blocked["id"], expected_rev=1, status="blocked", actor="worker", wait=self.wait)

    def test_latest_complete_omission_refuses_stale_registration(self):
        self.assertTrue(self.poll()["ok"])
        blocked = self.block()
        insight_id = vcs.proposal_insight_id(self.row["id"])
        old = self.project.insights.read(insight_id)
        self.assertTrue(self.poll([])["ok"])
        self.assertEqual(self.project.insights.read(insight_id), old)
        self.assertEqual(read_json(self.project.root / "vcs-poll.json")["proposal_complete_facts"], {})
        with self.assertRaisesRegex(Denied, "latest complete enumeration"):
            self.block()
        # Existing pins and positive release semantics never use this inventory.
        self.assertEqual(self.store.get(blocked["id"]), blocked)
        self.assertEqual(proposal_waits.pending(self.store, "worker"), [])

    def test_changed_complete_facts_refuse_stale_insight_after_row_write_failure(self):
        self.poll()
        insight_id = vcs.proposal_insight_id(self.row["id"])
        old = self.project.insights.read(insight_id)
        for changes in ({"state": "closed"}, {"draft": True},
                        {"base": {**self.row["base"], "sha": "e" * 40}},
                        {"checks": [{"check": "unit", "state": "failure", "sha": SHA}]}):
            with self.subTest(changes=changes):
                changed = {**self.row, **changes}
                title, body = vcs.proposal_facts(changed, enumeration_complete=True)
                expected = {self.row["id"]: digest(canonical({"title": title, "body": body}))}

                def fail_row(*args, **kwargs):
                    # Publication is already durable before the first row write.
                    self.assertEqual(read_json(self.project.root / "vcs-poll.json")[
                        "proposal_complete_facts"], expected)
                    raise OSError("row write failed")

                with patch("mizu.vcs.record_proposal_state", side_effect=fail_row):
                    result = self.poll([changed])
                self.assertFalse(result["ok"])
                self.assertEqual(result["recorded"], 0)
                self.assertEqual(self.project.insights.read(insight_id), old)
                with self.assertRaisesRegex(Denied, "latest complete enumeration"):
                    self.block()
        self.assertTrue(self.poll()["ok"])
        self.block()  # Identical facts in a fresh complete enumeration restore eligibility.

    def test_unavailable_or_malformed_inventory_denies_registration_not_stored_waits(self):
        self.poll()
        blocked = self.block()
        path = self.project.root / "vcs-poll.json"
        for bad in (None, [], {}, {"proposal_complete_facts": None},
                    {"proposal_complete_facts": []},
                    {"proposal_complete_facts": {self.row["id"]: "invalid"}},
                    {"proposal_complete_facts": {"bad\nidentity": "a" * 64}},
                    {"proposal_complete_facts": {str(i): "a" * 64 for i in range(vcs.MAX_PROPOSALS + 1)}}):
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
        with patch("mizu.proposal_waits.read_json", side_effect=OSError("unavailable")):
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

    def test_missing_unpublished_mismatched_and_unpinned_registration(self):
        with self.assertRaises(Denied):
            self.block()
        self.poll()
        for bad in ({"proposal_id": "absent"}, {"proposal_id": ""}, {"proposal_id": None},
                    {"publication_id": "f" * 64}, {"publication_id": "../unsafe"},
                    {"head": self.row["head"]}):
            with self.subTest(bad=bad), self.assertRaises(Denied):
                self.block(wait={**self.wait, **bad})
        for part, field, value in (("head", "sha", "e" * 40), ("head", "ref", "moved")):
            changed = copy.deepcopy(self.row)
            changed[part][field] = value
            self.poll([changed])
            with self.assertRaisesRegex(Denied, "differs"):
                self.block()
        deleted = copy.deepcopy(self.row)
        deleted["base"] = {"repo": "owner/repo", "ref": "main", "deleted": True}
        self.poll([deleted])
        with self.assertRaises(Denied):
            self.block()

    def test_incomplete_observation_cannot_register_or_release(self):
        self.poll(complete=False, cursor="page-two")
        with self.assertRaises(Denied):
            self.block()
        # Even a terminal page cannot prove uniqueness across the enumeration.
        self.poll()
        with self.assertRaises(Denied):
            self.block()
        self.poll()  # Starts with no input cursor: a complete enumeration.
        blocked = self.block()
        self.row["state"] = "merged"
        self.poll(complete=False, cursor="next")
        self.assertEqual(proposal_waits.pending(self.store, "worker"), [])
        self.poll()
        self.assertEqual(proposal_waits.pending(self.store, "worker"), [])
        self.assertEqual(self.store.get(blocked["id"]), blocked)
        self.poll()
        self.assertEqual(len(proposal_waits.pending(self.store, "worker")), 1)

    def test_only_exact_positive_merge_releases(self):
        self.poll()
        blocked = self.block()
        for state in ("open", "closed"):
            self.row["state"] = state
            self.poll()
            self.assertEqual(proposal_waits.pending(self.store, "worker"), [])
        self.row["state"] = "merged"
        for part in ("head", "base"):
            for field in ("repo", "ref", "sha"):
                changed = copy.deepcopy(self.row)
                changed[part][field] = "e" * 40 if field == "sha" else "moved"
                self.poll([changed])
                self.assertEqual(proposal_waits.pending(self.store, "worker"), [], (part, field))
        self.poll([])
        changed = {**self.row, "id": "forge:owner/repo#2"}
        self.poll([changed])
        self.assertEqual(proposal_waits.pending(self.store, "worker"), [])
        self.assertTrue(self.poll()["ok"])
        event = proposal_waits.pending(self.store, "worker")[0]
        self.assertEqual(event["work_item"], blocked["id"])
        self.assertEqual(event["state"], "merged")
        self.assertEqual(event["wait"], blocked["wait"])
        self.assertEqual(proposal_waits.pending(self.store, "reviewer"), [])
        self.assertEqual(self.store.get(blocked["id"]), blocked)
        self.assertTrue(_role_events_due(self.project, "worker"))

    def test_duplicate_or_malformed_page_cannot_record_or_wake(self):
        self.row["state"] = "merged"
        self.assertFalse(self.poll([self.row, self.row])["ok"])
        with self.assertRaises(Denied):
            self.block()
        self.row["state"] = "open"
        self.poll()
        self.block()
        self.row["state"] = "merged"
        for rows in ([self.row, self.row], [self.row, {"id": "malformed"}]):
            result = self.poll(rows)
            self.assertFalse(result["ok"])
            self.assertEqual(result["recorded"], 0)
            self.assertEqual(proposal_waits.pending(self.store, "worker"), [])
        self.assertFalse(self.poll(complete="yes")["ok"])
        self.assertEqual(proposal_waits.pending(self.store, "worker"), [])

    def test_opt_in_and_work_item_only_schema(self):
        request = {"id": "item", "expected_rev": 1, "status": "blocked", "wait": self.wait}
        validate(request, DEFINITIONS["work_items_transition"][1])
        with self.assertRaises(Denied):
            self.project.insights._normalize_wait(self.wait, "defer")
        self.poll()
        for option, value in (("poll_enabled", False), ("poll_proposals_enabled", False), ("command", [])):
            with patch.dict(self.store.vcs, {option: value}), self.assertRaises(Denied):
                self.block()

    def test_exact_replay_explicit_ack_and_current_revision(self):
        self.poll()
        blocked = self.block()
        self.row["state"] = "merged"
        self.poll()
        ctx = self.context()
        tokens = [ctx.wait_events[0]["ack_token"]]
        original = ctx.wait_events
        self.poll()
        self.assertEqual(proposal_waits.pending(self.store, "worker"), original)
        proposal_waits.complete(ctx, {"outcome": "wait"})
        self.assertEqual(proposal_waits.pending(self.store, "worker"), original)
        with self.assertRaises(Denied):
            ctx.handle("finish", {"outcome": "wait", "summary": "bad ack", "ack_events": ["f" * 64]})
        proposal_waits.validate_selection(ctx, tokens)
        ctx.handle("finish", {"outcome": "wait", "summary": "merge observed", "state": "Merge observed; holder decides next", "ack_events": tokens})
        proposal_waits.complete(ctx, {"outcome": "wait", "ack_events": tokens})
        proposal_waits.complete(ctx, {"outcome": "wait", "ack_events": tokens})
        self.assertEqual(proposal_waits.pending(self.store, "worker"), [])
        self.assertEqual(self.store.get(blocked["id"]), blocked)
        # A newly registered revision is a different event, not suppressed by the old receipt.
        opened = self.store.transition(blocked["id"], expected_rev=blocked["rev"],
                                       status="open", actor="worker")
        new = self.block(seed=opened)
        self.poll()
        self.assertEqual(proposal_waits.pending(self.store, "worker")[0]["rev"], new["rev"])
        with self.assertRaises(Denied):
            proposal_waits.validate_selection(ctx, tokens)
        self.store.transition(new["id"], expected_rev=new["rev"], status="open", actor="worker")
        self.assertEqual(proposal_waits.pending(self.store, "worker"), [])

    def test_unsafe_receipt_and_publication_fail_closed(self):
        self.poll()
        self.block()
        self.row["state"] = "merged"
        self.poll()
        ctx = self.context()
        shared = proposal_waits.shared_store(self.store)
        path = self.project.root / shared._receipt_name("worker", ctx.proposal_wait_manifest[0].record_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        target = self.project.root / "unrelated.json"
        write_json(target, {})
        path.symlink_to(target)
        with self.assertRaises(Denied):
            proposal_waits.pending(self.store, "worker")
        path.unlink()
        record_path = self.project.root / shared._record_name(ctx.proposal_wait_manifest[0].record_id)
        raw = record_path.read_bytes()
        record_path.unlink()
        record_path.symlink_to(target)
        with self.assertRaises(Denied):
            proposal_waits.pending(self.store, "worker")
        self.assertTrue(raw)


    def test_engine_delivery_success_boundary_and_restart(self):
        self.poll()
        blocked = self.block(actor="reviewer")
        self.row["state"] = "merged"
        self.poll()
        before = proposal_waits.pending(self.store, "reviewer")
        roles = dict(self.config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], on_change=False)
        config = dataclasses.replace(self.config, roles=roles)

        def finish(ctx, prompt, profile=None):
            self.assertNotIn("proposal_complete_facts", prompt)
            rows = [e for e in json.loads(prompt)["wait_events"] if e["kind"] == "proposal_merged"]
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

    def test_ambiguous_persisted_observation_and_unsafe_publication(self):
        self.poll()
        observation = self.project.insights.read(vcs.proposal_insight_id(self.wait["proposal_id"]))
        body = observation["body"] + "head: owner/repo published " + SHA + "\n"
        self.project.insights.revise(observation["id"], source="vcs", title=observation["title"],
                                    body=body, base_snapshot=None, expected_rev=observation["rev"])
        with self.assertRaisesRegex(Denied, "ambiguous"):
            self.block()
        self.poll()
        path = self.project.root / "evidence" / "publications" / (self.wait["publication_id"] + ".receipt.json")
        target = self.project.root / "unrelated-publication.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(Denied):
            self.block()


    def test_bounded_delivery_partial_ack_and_no_ack_boundaries(self):
        self.poll()
        items = sorted([self.block() for _ in range(3)], key=lambda i: (i["updated_at"], i["id"]))
        self.row["state"] = "merged"
        self.poll()
        before = proposal_waits.pending(self.store, "worker", limit=2)
        self.assertEqual([e["work_item"] for e in before], [i["id"] for i in items[:2]])
        ctx = self.context()
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
        self.assertEqual([e["work_item"] for e in proposal_waits.pending(self.store, "worker", limit=2)],
                         [i["id"] for i in items[1:]])
        # Replaying the poll or restarting cannot revive an exactly receipted revision.
        self.poll()
        restarted = Project(self.config, "sample")
        self.assertEqual(proposal_waits.pending(restarted.work_items, "worker", limit=2),
                         proposal_waits.pending(self.store, "worker", limit=2))
