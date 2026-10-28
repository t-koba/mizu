"""Explicit task manifests commit all accepted work together, never prose-derived."""
import copy
import dataclasses
import json
from unittest.mock import patch

from support import Fixture
from mizu.errors import Denied, LimitExceeded
from mizu.fs import PREVIEW_BYTES, canonical, write_json
from mizu.insights import _decision_key
from mizu.work_item_seed import MAX_DECISION_TASKS, task_id


class DecisionTaskTests(Fixture):
    def setUp(self):
        super().setUp()
        self.insights = self.project.insights
        self.store = self.project.work_items
        self.proposal = self.insights.submit(source="worker", title="Whole scope", body="review me",
                                              base_snapshot=None)
        self.tasks = [{"title": "First slice", "description": "Implement the first part"},
                      {"title": "Second slice", "description": "Implement the second part"}]
        self.key = _decision_key(self.proposal["id"], 1)
        self.event_path = self.project.root / "decision-history" / f"{self.key}.json"
        self.current_path = self.project.root / "decisions" / f"{self.proposal['id']}.json"

    def decide(self, **kwargs):
        return self.insights.decide(self.proposal["id"], kwargs.pop("action", "accept"),
                                    kwargs.pop("reason", "The accepted whole, not a task parser"),
                                    "", kwargs.pop("run", "first-run"), expected_rev=1, **kwargs)

    def assert_uncommitted(self):
        self.assertFalse(self.event_path.exists())
        self.assertFalse(self.current_path.exists())
        self.assertEqual(self.store.page_open()["total"], 0)

    def test_group_read_enumeration_scope_status_and_transitions(self):
        event = self.decide(work_items=self.tasks, scope="Deliver both parts")
        self.assertNotIn("work_item", event)
        self.assertEqual(event["work_item_specs"], self.tasks)
        self.assertEqual(event["work_item_intent"], "create")
        self.assertEqual(json.loads(self.event_path.read_text()), event)
        self.assertEqual(json.loads(self.current_path.read_text()), event)
        self.assertEqual(len(list(self.event_path.parent.glob("*.json"))), 1)
        ids = [seed["id"] for seed in event["work_items"]]
        self.assertEqual(ids, [task_id(self.key, self.tasks, i) for i in range(2)])
        self.assertEqual(len(set(ids)), 2)
        self.assertNotIn(f"wi_{self.key}", ids)
        for task, seed in zip(self.tasks, event["work_items"]):
            item = self.store.get(seed["id"])
            self.assertEqual(item, seed)
            self.assertEqual(item["description"], task["description"])
            self.assertEqual(item["title"], task["title"])
            self.assertEqual(item["scope"], "Deliver both parts")
            self.assertEqual(item["origin"], {"insight_id": self.proposal["id"], "rev": 1})
            self.assertEqual(item["status"], "open")
            self.assertNotIn("wait", item)
        for page in (self.store.page_open(), self.store.page_open_readonly()):
            self.assertEqual({row["id"] for row in page["items"]}, set(ids))
            self.assertEqual(page["total"], 2)
            self.assertTrue(page["coverage_complete"])
        self.assertEqual(self.store.page_legacy()["unseeded_decisions"], 0)
        self.assertEqual(self.store.page_legacy()["total"], 0)
        with self.assertRaises(Denied):
            self.store.reconcile(self.key, expected_action="accept", outcome="open", reason="no", actor="worker")
        projected = self.store._current_decision(self.proposal["id"], include_work_item=True)
        self.assertEqual(projected, {"rev": 1, "action": "accept", "work_item_ids": ids})
        statuses = self.store.submitter_status(self.proposal["id"])["items"]
        self.assertEqual([s["id"] for s in statuses], ids)
        self.store.transition(ids[0], expected_rev=1, status="doing", actor="worker")
        self.assertEqual(self.store.get(ids[0])["rev"], 2)
        self.assertEqual(self.store.page_open_readonly()["total"], 2)
        self.store.transition(ids[0], expected_rev=2, status="dropped", actor="worker", reason="superseded")
        self.assertEqual(self.store.page_closed_readonly()["items"][0]["id"], ids[0])
        self.assertEqual(self.decide(work_items=self.tasks), event)

    def test_exact_and_conflicting_replays(self):
        event = self.decide(work_items=self.tasks, action="modify")
        self.assertEqual(self.decide(work_items=copy.deepcopy(self.tasks), action="modify", run="retry"), event)
        changes = [None, list(reversed(self.tasks)), self.tasks[:1],
                   [dict(self.tasks[0], description="different"), self.tasks[1]]]
        for tasks in changes:
            with self.subTest(tasks=tasks), self.assertRaises(Denied):
                self.decide(work_items=tasks, action="modify")
        for kwargs in ({"action": "accept"}, {"reason": "different"}, {"work_item_intent": "none"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Denied):
                self.decide(work_items=self.tasks, **{"action": "modify", **kwargs})
        self.assertEqual(json.loads(self.event_path.read_text()), event)
        self.assertEqual(len(list(self.event_path.parent.glob("*.json"))), 1)

    def test_explicit_none_and_source_policy_none_deny_tasks(self):
        with self.assertRaises(Denied):
            self.decide(work_items=self.tasks, work_item_intent="none")
        self.assert_uncommitted()
        self.insights.work_item_intent_by_source = {"worker": "none"}
        with self.assertRaises(Denied):
            self.decide(work_items=self.tasks)
        self.assert_uncommitted()
        self.insights.work_item_intent_by_source = {}
        self.insights.work_item_intent_default = "none"
        with self.assertRaises(Denied):
            self.decide(work_items=self.tasks)
        self.assert_uncommitted()
        event = self.decide(work_items=self.tasks, work_item_intent="create")
        self.assertEqual(len(event["work_items"]), 2)

    def test_invalid_specs_and_count_fail_before_commit(self):
        invalid = [[], {}, "tasks", [None], self.tasks * 9,
                   [{"title": "Only title"}], [{"title": "", "description": "d"}],
                   [{"title": "  ", "description": "d"}],
                   [{"title": "x" * 201, "description": "d"}],
                   [{"title": "x\x00", "description": "d"}],
                   [{"title": "\ud800", "description": "d"}],
                   [{"title": "t", "description": " "}],
                   [{"title": "t", "description": "d" * 4001}],
                   [{"title": "t", "description": "d", "wait": {"kind": "ci"}}],
                   [{"title": "t", "description": "d", "scope": "task scope"}]]
        for tasks in invalid:
            with self.subTest(tasks=repr(tasks)[:80]), self.assertRaises(Denied):
                self.decide(work_items=tasks)
            self.assert_uncommitted()
        for action in ("reject", "defer"):
            with self.subTest(action=action), self.assertRaises(Denied):
                self.decide(work_items=self.tasks, action=action)
            self.assert_uncommitted()

    def test_derived_id_collision_fails_before_commit(self):
        self.tasks[1]["wait"] = {"kind": "code_change"}
        collision = "wi_" + "a" * 32
        with patch("mizu.insights.task_id", return_value=collision), \
                patch("mizu.work_items.task_id", return_value=collision), \
                patch("mizu.work_item_seed.task_id", return_value=collision), \
                self.assertRaises(Denied):
            self.decide(work_items=self.tasks)
        self.assert_uncommitted()

    def test_total_manifest_bytes_not_just_input_are_bounded(self):
        # Each spec is individually valid; duplication in immutable seeds must
        # count toward the aggregate event bound as well.
        tasks = [{"title": f"Part {i}", "description": "x" * 4000}
                 for i in range(MAX_DECISION_TASKS)]
        self.assertLess(len(canonical(tasks)), PREVIEW_BYTES)
        with self.assertRaises(LimitExceeded):
            self.decide(work_items=tasks)
        self.assert_uncommitted()
        # UTF-8 bytes, not characters, bound the decision.
        with self.assertRaises(LimitExceeded):
            self.decide(work_items=tasks[:8], reason="界" * 23000)
        self.assert_uncommitted()

    def test_later_seed_slot_failure_records_no_partial_scope(self):
        self.tasks[1]["wait"] = {"kind": "code_change"}
        path = self.project.root / "work-items" / task_id(self.key, self.tasks, 1)
        path.mkdir(parents=True)
        write_json(path / "2.json", {"not": "an empty seed slot"})
        with self.assertRaises(Denied):
            self.decide(work_items=self.tasks)
        self.assertFalse(self.event_path.exists())
        self.assertFalse(self.current_path.exists())
        (path / "2.json").unlink()
        self.assertEqual(self.store.page_open()["total"], 0)
        self.assertEqual(len(self.decide(work_items=self.tasks)["work_items"]), 2)

    def test_interruption_before_commit_has_no_visible_members_and_retry_commits_all(self):
        self.tasks[1]["wait"] = {"kind": "code_change"}
        def fail_commit(path, value, **kwargs):
            if path == self.event_path:
                raise OSError("simulated precommit interruption")
            return write_json(path, value, **kwargs)
        with patch("mizu.insights.write_json", side_effect=fail_commit), self.assertRaises(OSError):
            self.decide(work_items=self.tasks)
        self.assert_uncommitted()
        self.assertEqual(self.store.page_open_readonly()["total"], 0)
        event = self.decide(work_items=self.tasks)
        self.assertEqual(self.store.page_open()["total"], 2)
        self.assertEqual(self.decide(work_items=self.tasks), event)

    def test_interruption_after_manifest_commit_exposes_whole_group_and_repairs_pointer(self):
        self.tasks[1]["wait"] = {"kind": "code_change"}
        def fail_pointer(path, value, **kwargs):
            if path == self.current_path:
                raise OSError("simulated pointer interruption")
            return write_json(path, value, **kwargs)
        with patch("mizu.insights.write_json", side_effect=fail_pointer), self.assertRaises(OSError):
            self.decide(work_items=self.tasks)
        event = json.loads(self.event_path.read_text())
        self.assertFalse(self.current_path.exists())
        self.assertEqual(self.store.page_open()["total"], 2)
        self.assertEqual(self.store.page_open_readonly()["total"], 2)
        with self.assertRaises(Denied):
            self.decide(work_items=self.tasks[:1])
        self.assertFalse(self.current_path.exists())
        self.assertEqual(self.decide(work_items=self.tasks, run="retry"), event)
        self.assertEqual(json.loads(self.current_path.read_text()), event)

    def test_missing_or_tampered_members_fail_closed_on_all_read_paths(self):
        self.tasks[1]["wait"] = {"kind": "code_change"}
        event = self.decide(work_items=self.tasks)
        member = event["work_items"][0]["id"]
        mutations = []
        broken = copy.deepcopy(event)
        broken["work_items"].pop()
        mutations.append(broken)
        broken = copy.deepcopy(event)
        broken["work_items"][1]["description"] = "tampered"
        mutations.append(broken)
        broken = copy.deepcopy(event)
        broken["work_items"][1]["origin"]["rev"] = 2
        mutations.append(broken)
        broken = copy.deepcopy(event)
        broken["work_items"][1]["id"] = "wi_" + "f" * 32
        mutations.append(broken)
        broken = copy.deepcopy(event)
        broken["work_item_specs"].pop()
        broken["work_items"].pop()  # IDs bind the whole set, not just index.
        mutations.append(broken)
        broken = copy.deepcopy(event)
        del broken["work_items"]
        mutations.append(broken)
        for broken in mutations:
            write_json(self.event_path, broken)
            write_json(self.current_path, broken)
            readers = [lambda: self.store.get(member), self.store.page_open,
                       self.store.page_open_readonly, self.store.page_closed_readonly,
                       self.store.page_legacy, lambda: self.store.submitter_status(self.proposal["id"]),
                       lambda: self.decide(work_items=self.tasks)]
            for reader in readers:
                with self.subTest(mutation=mutations.index(broken), reader=reader), self.assertRaises(Denied):
                    reader()
        write_json(self.event_path, event)
        write_json(self.current_path, event)
        self.assertEqual(self.store.page_open()["total"], 2)

    def test_current_pointer_cannot_hide_a_member(self):
        event = self.decide(work_items=self.tasks)
        broken = copy.deepcopy(event)
        broken["work_items"].pop()
        write_json(self.current_path, broken)
        with self.assertRaises(Denied):
            self.store.submitter_status(self.proposal["id"])
        with self.assertRaises(Denied):
            self.decide(work_items=self.tasks)
        write_json(self.current_path, event)
        self.assertEqual(self.decide(work_items=self.tasks), event)

    def test_omitted_list_retains_single_seed_and_no_work_shapes(self):
        event = self.decide()
        self.assertEqual(event["work_item"]["id"], f"wi_{self.key}")
        self.assertNotIn("work_items", event)
        self.assertNotIn("work_item_specs", event)
        self.assertEqual(self.decide(run="retry"), event)
        other = self.insights.submit(source="worker", title="No work", body="review", base_snapshot=None)
        event = self.insights.decide(other["id"], "accept", "no task", "", "run", expected_rev=1,
                                     work_item_intent="none")
        self.assertNotIn("work_item", event)
        self.assertNotIn("work_items", event)
        self.assertEqual(self.store.page_open()["total"], 1)
        self.assertTrue(self.store.page_legacy()["coverage_complete"])

    def test_runtime_schema_and_cli_expose_tasks_without_new_grants(self):
        from mizu.cli import _cmd_insight, parser
        from mizu.errors import ConfigError
        from mizu.protocol import DEFINITIONS, validate
        schema = DEFINITIONS["decide"][1]
        args = {"id": self.proposal["id"], "action": "modify", "reason": "Tasks", "rev": 1,
                "work_items": self.tasks}
        validate(args, schema)
        event = self.context("worker").handle("decide", args)
        self.assertEqual(event["work_item_specs"], self.tasks)
        owner = self.context("worker").handle("insights", {"id": self.proposal["id"]})
        other = self.context("reporter").handle("insights", {"id": self.proposal["id"]})
        self.assertEqual(len(owner["work_item_status"]["items"]), 2)
        self.assertNotIn("work_item_status", other)
        for tasks in ([], self.tasks * 9, [{"title": "t"}],
                      [{"title": "t", "description": "d", "scope": "s"}]):
            with self.subTest(tasks=tasks), self.assertRaises(Denied):
                validate({**args, "work_items": tasks}, schema)
        proposal = self.insights.submit(source="operator", title="CLI group", body="review",
                                        base_snapshot=None)
        task_file = self.project.workspace / "tasks.json"
        task_file.write_text(json.dumps(self.tasks), encoding="utf-8")
        args = parser().parse_args(["insight", "decide", self.project.name,
                                   "--id", proposal["id"], "--decision", "accept", "--reason", "Tasks",
                                   "--expected-rev", "1", "--work-items", str(task_file),
                                   "--work-item-intent", "create"])
        event = _cmd_insight(self.config, self.project, args)
        self.assertEqual(event["work_item_specs"], self.tasks)
        self.assertEqual(_cmd_insight(self.config, self.project, args), event)
        for content in ("null", "not json"):
            task_file.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(ConfigError):
                _cmd_insight(self.config, self.project, args)
        task_file.write_bytes(b" " * (PREVIEW_BYTES + 1))
        with self.assertRaises(Denied):
            _cmd_insight(self.config, self.project, args)
        args.action = "list"
        with self.assertRaises(ConfigError):
            _cmd_insight(self.config, self.project, args)

    def test_count_boundary_duplicate_specs_and_exact_revision_binding(self):
        tasks = [self.tasks[0]] * MAX_DECISION_TASKS
        event = self.decide(work_items=tasks)
        ids = {seed["id"] for seed in event["work_items"]}
        self.assertEqual(len(ids), MAX_DECISION_TASKS)
        self.assertEqual(self.store.page_open()["total"], MAX_DECISION_TASKS)
        self.insights.revise(self.proposal["id"], source="worker", title="Whole scope",
                             body="A meaningful revised scope", base_snapshot=None, expected_rev=1)
        revised = self.insights.decide(self.proposal["id"], "accept", "reassessed", "", "run",
                                       expected_rev=2, work_items=tasks)
        self.assertTrue(ids.isdisjoint({seed["id"] for seed in revised["work_items"]}))
        self.assertEqual(self.store.page_open_readonly()["total"], MAX_DECISION_TASKS * 2)
        for seed in revised["work_items"]:
            self.assertEqual(seed["origin"]["rev"], 2)
        with self.assertRaises(Denied):
            self.decide(work_items=tasks)

    def test_orphan_atomic_temporary_never_exposes_any_group_members(self):
        # Model the two durable states around the exclusive atomic event link.
        # The filesystem writer's link/fsync behavior is tested separately.
        event = self.decide(work_items=self.tasks)
        self.current_path.unlink()
        self.event_path.unlink()
        temporary = self.event_path.parent / ".new-abcdefgh"
        temporary.write_bytes(canonical(event))
        self.assertEqual(self.store.page_open_readonly()["total"], 0)
        self.assertTrue(temporary.exists())  # read-only scans do not recover.
        retry = self.decide(work_items=self.tasks)
        self.assertFalse(temporary.exists())
        self.assertEqual([s["id"] for s in retry["work_items"]],
                         [s["id"] for s in event["work_items"]])
        self.assertEqual(self.store.page_open()["total"], 2)

    def test_replay_validates_every_members_storage_chain(self):
        event = self.decide(work_items=self.tasks)
        directory = self.project.root / "work-items" / event["work_items"][1]["id"]
        directory.mkdir(parents=True)
        write_json(directory / "3.json", {"not": "a contiguous chain"})
        self.current_path.unlink()
        with self.assertRaises(Denied):
            self.decide(work_items=self.tasks)
        self.assertFalse(self.current_path.exists())
        (directory / "3.json").unlink()
        self.assertEqual(self.decide(work_items=self.tasks), event)

    def test_mixed_group_wait_normalization_readiness_and_routing(self):
        target = self.insights.submit(source="reviewer", title="Required result", body="assess",
                                      base_snapshot=None)
        waits = [{"kind": "deadline", "at": "2000-01-01T00:00:00.123Z"},
                 {"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"},
                 {"kind": "code_change"},
                 {"kind": "insight_decided", "insight": target["id"]},
                 {"kind": "dependency", "recipient": "reviewer",
                  "requires": {"insight": target["id"], "rev": 1, "action": "accept"}}]
        tasks = self.tasks[:1] + [dict(self.tasks[1], title=f"Wait {i}", wait=wait)
                                 for i, wait in enumerate(waits)]
        baseline = self.project.snapshots.get()["code_digest"]
        event = self.decide(work_items=tasks, scope="Deliver all contingent slices")
        seeds = event["work_items"]
        ids = [seed["id"] for seed in seeds]
        expected = [{"kind": "deadline", "at": "2000-01-01T00:00:00+00:00"}, waits[1],
                    {"kind": "code_change", "digest": baseline},
                    {"kind": "insight_decided", "target": target["id"]}, waits[4]]
        self.assertEqual(event["work_item_specs"], tasks)  # Raw, not normalized.
        self.assertEqual([s["wait"] for s in seeds[1:]], expected)
        self.assertEqual([s["status"] for s in seeds], ["open"] + ["blocked"] * 5)
        self.assertTrue(all(s["holder"] is None and s["rev"] == 1 for s in seeds))
        self.assertFalse((self.project.root / "waits").exists())
        self.assertEqual(list((self.project.root / "work-items").glob("*/*.json")), [])
        for seed in seeds:
            self.assertEqual(self.store.get(seed["id"]), seed)
            self.assertEqual(seed["scope"], "Deliver all contingent slices")
        for page in (self.store.page_open(), self.store.page_open_readonly()):
            self.assertEqual(page["total"], len(tasks))
            rows = {r["id"]: r for r in page["items"]}
            self.assertEqual([rows[i]["wait"] for i in ids[1:]], expected)
        for role in ("worker", "reviewer"):
            self.assertEqual({r["id"] for r in self.store.page_ready(role_name=role)["items"]}, set(ids[:2]))
        # A real published code change satisfies only the code-change member.
        (self.project.workspace / "new.txt").write_text("changed\n", encoding="utf-8")
        current = self.project.snapshots.get()
        changed = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace), goal=self.project.goal,
            state="Changed", run=None, outcome="continue", summary="Changed code", parent=current["id"])
        self.project.snapshots.publish(changed)
        self.assertEqual({r["id"] for r in self.store.page_ready(role_name="reviewer")["items"]},
                         {ids[0], ids[1], ids[3]})
        self.insights.decide(target["id"], "accept", "Assessed", "", "test", expected_rev=1,
                             work_item_intent="none")
        self.assertEqual({r["id"] for r in self.store.page_ready(role_name="reviewer")["items"]},
                         set(ids) - {ids[2]})
        # The exact dependency revision is now impossible and actionable for
        # replanning; the current-revision insight wait remains pending.
        self.insights.revise(target["id"], source="reviewer", title="Required result",
                             body="reassess", base_snapshot=None, expected_rev=1)
        rows = self.store.page_ready(role_name="reviewer")["items"]
        self.assertEqual({row["id"] for row in rows}, {ids[0], ids[1], ids[3], ids[5]})
        liveness_rows = self.store.blocked_liveness_readonly()["items"]
        by_id = {row["id"]: row for row in liveness_rows}
        self.assertEqual(by_id[ids[4]]["state"], "pending")
        self.assertEqual(by_id[ids[5]]["state"], "impossible")
        self.assertEqual(self.decide(work_items=tasks), event)

    def test_code_change_retry_preserves_baseline_and_repairs_pointer_without_reading_head(self):
        self.tasks[1]["wait"] = {"kind": "code_change"}
        event = self.decide(work_items=self.tasks)
        self.current_path.unlink()
        with patch.object(self.insights, "project_snapshots_digest", side_effect=AssertionError("rebased")):
            self.assertEqual(self.decide(work_items=copy.deepcopy(self.tasks), run="retry"), event)
        self.assertEqual(json.loads(self.current_path.read_text()), event)
        # A different condition conflicts with the original request.
        changed = copy.deepcopy(self.tasks)
        changed[1]["wait"] = {"kind": "deadline", "at": "2000-01-01T00:00:00Z"}
        with self.assertRaises(Denied):
            self.decide(work_items=changed)
        self.assertNotEqual(task_id(self.key, changed, 0), event["work_items"][0]["id"])

    def test_code_change_baseline_is_bound_to_group_wait_manifest(self):
        self.tasks[1]["wait"] = {"kind": "code_change"}
        event = self.decide(work_items=self.tasks)
        seed = event["work_items"][1]
        baseline = event["work_item_waits"][1]
        self.assertEqual(seed["wait"], baseline)

        changed_seed = json.loads(json.dumps(event))
        changed_seed["work_items"][1]["wait"]["digest"] = "b" * 64
        changed_manifest = json.loads(json.dumps(event))
        changed_manifest["work_item_waits"][1]["digest"] = "c" * 64
        missing_manifest = json.loads(json.dumps(event))
        del missing_manifest["work_item_waits"]
        for broken in (changed_seed, changed_manifest, missing_manifest):
            write_json(self.event_path, broken)
            write_json(self.current_path, broken)
            with self.subTest(broken=broken), self.assertRaises(Denied):
                self.store.get(seed["id"])
        write_json(self.event_path, event)
        write_json(self.current_path, event)
        self.assertEqual(self.store.get(seed["id"]), seed)

    def test_raw_wait_identity_is_not_just_normalized_condition(self):
        self.tasks[1]["wait"] = {"kind": "deadline", "at": "2000-01-01T00:00:00Z"}
        event = self.decide(work_items=self.tasks)
        changed = copy.deepcopy(self.tasks)
        changed[1]["wait"]["at"] = "2000-01-01T00:00:00+00:00"
        for i in range(2):
            self.assertNotEqual(task_id(self.key, changed, i), event["work_items"][i]["id"])
        with self.assertRaises(Denied):
            self.decide(work_items=changed)
        self.assertEqual(self.decide(work_items=self.tasks), event)

    def test_invalid_task_waits_reject_entire_group(self):
        invalid = [None, [], "wait", {}, {"kind": []}, {"kind": "ci"},
                   {"kind": "ci", "publication_id": "a" * 64, "checks": ["core"]},
                   {"kind": "other"}, {"kind": "code_change", "digest": "a" * 64},
                   {"kind": "deadline"}, {"kind": "deadline", "at": "2000-01-01"},
                   {"kind": "deadline", "at": "2000-01-01T00:00:00Z", "extra": 1},
                   {"kind": "insight_decided"}, {"kind": "insight_decided", "insight": "../bad"},
                   {"kind": "dependency", "recipient": "worker", "requires": {}},
                   {"kind": "dependency", "recipient": "worker",
                    "requires": {"insight": self.proposal["id"], "rev": True, "action": "accept"}},
                   {"kind": "dependency", "recipient": "worker",
                    "requires": {"insight": self.proposal["id"], "rev": 1, "action": "defer"}}]
        for wait in invalid:
            tasks = [self.tasks[0], dict(self.tasks[1], wait=wait)]
            with self.subTest(wait=wait), self.assertRaises(Denied):
                self.decide(work_items=tasks)
            self.assert_uncommitted()
        self.tasks[1]["wait"] = {"kind": "code_change"}
        with patch.object(self.insights, "project_snapshots_digest", side_effect=ValueError("no head")), \
                self.assertRaises(Denied):
            self.decide(work_items=self.tasks)
        self.assert_uncommitted()

    def test_stored_wait_corruption_fails_closed_for_whole_group(self):
        self.tasks[1]["wait"] = {"kind": "deadline", "at": "2000-01-01T00:00:00Z"}
        event = self.decide(work_items=self.tasks)
        mutations = []
        for key, value in (("wait", None), ("wait", {"kind": "code_change", "digest": "b" * 64}),
                           ("wait", {"kind": "deadline", "at": "2001-01-01T00:00:00+00:00"}),
                           ("wait", {"kind": "deadline", "at": "2000-01-01T00:00:00Z"}),
                           ("holder", "worker"), ("status", "open")):
            broken = copy.deepcopy(event)
            broken["work_items"][1][key] = value
            mutations.append(broken)
        broken = copy.deepcopy(event)
        del broken["work_items"][1]["wait"]
        mutations.append(broken)
        for broken in mutations:
            write_json(self.event_path, broken)
            write_json(self.current_path, broken)
            for reader in (lambda: self.store.get(event["work_items"][0]["id"]), self.store.page_ready,
                           self.store.page_open_readonly, lambda: self.decide(work_items=self.tasks)):
                with self.subTest(broken=broken, reader=reader), self.assertRaises(Denied):
                    reader()
        write_json(self.event_path, event)
        write_json(self.current_path, event)

    def test_seed_wait_allowance_is_only_for_decision_task_groups(self):
        from mizu.work_item_seed import validate_seed
        self.tasks[1]["wait"] = {"kind": "code_change"}
        event = self.decide(work_items=self.tasks)
        with self.assertRaises(Denied):
            validate_seed(event["work_items"][1])
        for wait in (None, {"kind": "code_change", "digest": "not a digest"}, {"kind": "ci"}):
            broken = copy.deepcopy(event)
            broken["work_items"][1]["wait"] = wait
            write_json(self.event_path, broken)
            with self.assertRaises(Denied):
                self.store.get(event["work_items"][0]["id"])
        write_json(self.event_path, event)

    def test_unheld_satisfied_seed_claimable_by_transition_capable_role(self):
        from mizu.protocol import DEFINITIONS, validate
        self.tasks[1]["wait"] = {"kind": "deadline", "at": "2000-01-01T00:00:00Z"}
        args = {"id": self.proposal["id"], "action": "accept", "reason": "Tasks", "rev": 1,
                "work_items": self.tasks}
        validate(args, DEFINITIONS["decide"][1])
        event = self.context("worker").handle("decide", args)
        item_id = event["work_items"][1]["id"]
        claim = {"id": item_id, "expected_rev": 1, "status": "doing"}
        with self.assertRaises(Denied):
            self.context("reviewer").handle("work_items_transition", claim)
        role = self.config.roles["reviewer"]
        self.config.roles["reviewer"] = dataclasses.replace(
            role, capabilities=(*role.capabilities, "work_items_transition"))
        claimed = self.context("reviewer").handle("work_items_transition", claim)
        self.assertEqual((claimed["holder"], claimed["actor"], claimed["status"]),
                         ("reviewer", "reviewer", "doing"))
        self.context("reviewer").handle("work_items_transition", {
            "id": item_id, "expected_rev": 2, "status": "blocked", "wait": self.tasks[1]["wait"]})
        self.assertIn(item_id, {r["id"] for r in self.store.page_ready(role_name="reviewer")["items"]})
        self.assertNotIn(item_id, {r["id"] for r in self.store.page_ready(role_name="worker")["items"]})
        with self.assertRaises(Denied):
            self.context("reviewer").handle("work_items_transition", {**claim, "actor": "worker"})

    def test_wait_bytes_count_toward_the_complete_group_bound_before_commit(self):
        from mizu.work_item_seed import make_seed
        tasks = [{"title": f"Part {i}", "description": "x" * 3900} for i in range(MAX_DECISION_TASKS)]
        stamp = "2026-01-01T00:00:00+00:00"
        candidate = {"id": self.proposal["id"], "action": "accept", "reason": "r", "revisit": "",
                     "run": "first-run", "created_at": stamp, "rev": 1, "decision_key": self.key,
                     "work_item_intent": "create", "work_item_specs": tasks,
                     "work_items": [make_seed(task_id(self.key, tasks, i), t["title"],
                                              {"insight_id": self.proposal["id"], "rev": 1}, stamp,
                                              description=t["description"]) for i, t in enumerate(tasks)]}
        reason = "r" * (PREVIEW_BYTES - len(canonical(candidate)))
        self.assertTrue(1 <= len(reason) <= 4000)
        blocked = [dict(t, wait={"kind": "code_change"}) for t in tasks]
        self.assertLess(len(canonical(blocked)), PREVIEW_BYTES)
        with patch("mizu.insights.now", return_value=stamp):
            with self.assertRaises(LimitExceeded):
                self.decide(work_items=blocked, reason=reason)
            self.assert_uncommitted()
            self.assertEqual(list((self.project.root / "work-items").glob("*/*.json")), [])
            # The same descriptions without waits fit: wait specs AND normalized
            # seed evidence, not just the request bytes, exhausted the real bound.
            event = self.decide(work_items=tasks, reason=reason)
        self.assertEqual(len(canonical(event)), PREVIEW_BYTES - 1)
