"""Decisions bind to the revision actually read; stale revisions are rejected."""
import json
import unittest

from support import Fixture
from mizu.errors import Denied


class DecisionRevisionFixture(Fixture):
    def submit(self, source="worker"):
        snap = self.project.snapshots.get()
        return self.project.insights.submit(source=source, title="Proposal",
                                            body="original claims", base_snapshot=snap["id"])

    def revise(self, proposal, *, title="Proposal"):
        snap = self.project.snapshots.get()
        return self.project.insights.revise(proposal["id"], source="worker",
                                            title=title, body="revised claims",
                                            base_snapshot=snap["id"], expected_rev=1)

    def decided(self, insight_id):
        return (self.project.root / "decisions" / f"{insight_id}.json").exists()

    def history_records(self):
        directory = self.project.root / "decision-history"
        if not directory.exists():
            return []
        return [json.loads(path.read_text()) for path in directory.glob("*.json")]

    def assert_work_item_seed(self, action, *, revised=False):
        proposal = self.submit()
        current = self.revise(proposal, title="Revised proposal") if revised else proposal
        record = self.project.insights.decide(proposal["id"], action, "reviewed", "", "run-1",
                                              expected_rev=current["rev"])
        decision = json.loads((self.project.root / "decisions" / f"{proposal['id']}.json").read_text())
        history_paths = list((self.project.root / "decision-history").glob("*.json"))
        history = [json.loads(path.read_text()) for path in history_paths]
        self.assertEqual(len(history), 1)
        event = history[0]
        item = record["work_item"]
        self.assertEqual(decision, record)
        self.assertEqual(event, record)
        self.assertEqual(item, {
            "id": f"wi_{event['work_item']['id'][3:]}",
            "title": current["title"],
            "origin": {"insight_id": proposal["id"], "rev": current["rev"]},
            "status": "open",
            "rev": 1,
            "holder": None,
            "created_at": record["created_at"],
            "updated_at": record["created_at"],
        })
        event_id = item["id"][3:]
        self.assertRegex(event_id, r"^[0-9a-f]{32}$")
        self.assertEqual(event_id, history_paths[0].stem)
        self.assertEqual(event["work_item"]["id"], f"wi_{event_id}")


class WorkItemSeedTests(DecisionRevisionFixture):
    def test_accept_seeds_identical_item_in_returned_current_and_history(self):
        self.assert_work_item_seed("accept")

    def test_modify_seeds_identical_item_from_current_revision(self):
        self.assert_work_item_seed("modify", revised=True)

    def test_defer_and_reject_do_not_seed_work_item(self):
        for action in ("defer", "reject"):
            with self.subTest(action=action):
                proposal = self.submit()
                record = self.project.insights.decide(
                    proposal["id"], action, "reviewed", "revisit later" if action == "defer" else "",
                    "run-1", expected_rev=1)
                current = json.loads((self.project.root / "decisions" / f"{proposal['id']}.json").read_text())
                history = [entry for entry in self.history_records() if entry["id"] == proposal["id"]]
                self.assertNotIn("work_item", record)
                self.assertNotIn("work_item", current)
                self.assertEqual(len(history), 1)
                self.assertNotIn("work_item", history[0])


class DecisionRevisionTests(DecisionRevisionFixture):
    def test_matching_rev_records(self):
        proposal = self.submit()
        record = self.project.insights.decide(proposal["id"], "accept", "good", "", "run-1",
                                              expected_rev=1)
        self.assertEqual(record["rev"], 1)

    def test_stale_rev_rejected_without_side_effects(self):
        proposal = self.submit()
        self.assertEqual(self.revise(proposal)["rev"], 2)
        with self.assertRaises(Denied):
            self.project.insights.decide(proposal["id"], "accept", "read rev1 rationale",
                                         "", "run-1", expected_rev=1)
        self.assertFalse(self.decided(proposal["id"]))
        self.assertEqual(self.history_records(), [])
        # The current revision stays actionable for reassessment.
        record = self.project.insights.decide(proposal["id"], "accept", "read rev2 rationale",
                                              "", "run-1", expected_rev=2)
        self.assertEqual(record["rev"], 2)

    def test_absent_rev_denied(self):
        proposal = self.submit()
        with self.assertRaises(Denied):
            self.project.insights.decide(proposal["id"], "accept", "current", "", "run-1")
        self.assertFalse(self.decided(proposal["id"]))
        ctx = self.context("worker")
        with self.assertRaises(Denied):
            ctx.handle("decide", {"id": proposal["id"], "action": "accept",
                                  "reason": "no rev echoed"})
        self.assertFalse(self.decided(proposal["id"]))

    def test_malformed_rev_denied(self):
        proposal = self.submit()
        for bad in (0, -1, True, "1", 1.0):
            with self.assertRaises(Denied, msg=f"rev={bad!r}"):
                self.project.insights.decide(proposal["id"], "accept", "x", "", "run-1",
                                             expected_rev=bad)
        self.assertFalse(self.decided(proposal["id"]))

    def test_stale_defer_registers_no_wait(self):
        proposal = self.submit()
        self.revise(proposal)
        with self.assertRaises(Denied):
            self.project.insights.decide(proposal["id"], "defer", "later", "when calm", "run-1",
                                         wait={"kind": "code_change"}, expected_rev=1)
        self.assertFalse(self.decided(proposal["id"]))
        self.assertFalse((self.project.root / "waits" / f"{proposal['id']}.json").exists())


class DecideToolRaceTests(DecisionRevisionFixture):
    def test_read_revise_decide_race_through_tool_path(self):
        ctx = self.context("worker")
        proposal = self.submit()
        read = ctx.handle("insights", {"id": proposal["id"]})
        self.assertEqual(read["rev"], 1)
        # An edit lands between the read and the decision.
        self.revise(proposal)
        with self.assertRaises(Denied):
            ctx.handle("decide", {"id": proposal["id"], "action": "accept",
                                  "reason": "rev1 rationale", "rev": 1})
        self.assertFalse(self.decided(proposal["id"]))
        # Re-reading the current revision makes the decision land on it.
        reread = ctx.handle("insights", {"id": proposal["id"]})
        record = ctx.handle("decide", {"id": proposal["id"], "action": "accept",
                                       "reason": "rev2 rationale", "rev": reread["rev"]})
        self.assertEqual(record["rev"], 2)

    def test_tool_rejects_out_of_range_rev(self):
        ctx = self.context("worker")
        proposal = self.submit()
        with self.assertRaises(Denied):
            ctx.handle("decide", {"id": proposal["id"], "action": "accept",
                                  "reason": "x", "rev": 0})
        self.assertFalse(self.decided(proposal["id"]))


def _decision_key_for_test(insight_id):
    from mizu.insights import _decision_key
    return _decision_key(insight_id, 1)


class WorkItemIntentTests(DecisionRevisionFixture):
    def decide(self, proposal, action="accept", **options):
        return self.project.insights.decide(proposal["id"], action, "reviewed",
                                            "later" if action == "defer" else "", "run-1",
                                            expected_rev=proposal["rev"], **options)

    def test_configured_source_defaults_and_both_overrides(self):
        from mizu.config import load
        from mizu.project import Project
        # Load policy through the public configuration path, not a model field.
        text = self.file.read_text().replace('operator = "none"', 'operator = "create"')
        text = text.replace('vcs = "none"', 'vcs = "create"').replace(
            'work_item_intent_default = "create"', 'work_item_intent_default = "none"')
        self.file.write_text(text)
        self.project = Project(load(self.file), self.project.name)
        for source, override, expected in (("operator", None, "create"), ("vcs", None, "create"),
                                            ("worker", None, "none"), ("editor", None, "none"),
                                            ("worker", "create", "create"), ("vcs", "none", "none")):
            with self.subTest(source=source, override=override):
                proposal = self.project.insights.submit(source=source, title="operator vcs create",
                                                        body="source=operator intent=create", base_snapshot=None,
                                                        origin="operator")
                decision = self.decide(proposal, "modify", work_item_intent=override)
                self.assertEqual(decision["work_item_intent"], expected)
                self.assertEqual("work_item" in decision, expected == "create")
                self.assertIn(decision, self.history_records())
                self.assertEqual(json.loads((self.project.root / "decisions" / f"{proposal['id']}.json").read_text()), decision)

    def test_unconfigured_default_and_example_source_policy(self):
        from mizu.config import load
        from mizu.project import Project
        self.assertEqual(self.decide(self.submit("operator"))["work_item_intent"], "none")
        self.assertEqual(self.decide(self.submit("vcs"))["work_item_intent"], "none")
        base = self.file.read_text()
        start, end = base.index('# Decision-to-work policy:'), base.index('[limits]')
        self.file.write_text(base[:start] + base[end:])
        config = load(self.file)
        self.assertEqual(config.insight_decisions, {"work_item_intent_default": "create",
                                                   "work_item_intent_by_source": {}})
        self.project = Project(config, self.project.name)
        self.assertEqual(self.decide(self.submit("operator"))["work_item_intent"], "create")

    def test_no_work_is_durable_resolved_coverage_not_a_legacy_gap(self):
        proposal = self.submit()
        decision = self.decide(proposal, work_item_intent="none")
        store = self.project.work_items
        event_id = next(path.stem for path in (self.project.root / "decision-history").glob("*.json"))
        original = (self.project.root / "decision-history" / f"{event_id}.json").read_bytes()
        self.assertEqual(store.page_open()["items"], [])
        self.assertTrue(store.page_open()["coverage_complete"])
        row = store.page_legacy()["items"][0]
        self.assertFalse(row["openable"])
        self.assertEqual(row["work_item_intent"], "none")
        self.assertEqual(row["resolution"], {"outcome": "none"})
        self.assertIsNone(store.submitter_status(proposal["id"]))
        self.assertNotIn("work_item_status", self.context().handle("insights", {"id": proposal["id"]}))
        for outcome in ("open", "drop"):
            with self.assertRaises(Denied):
                store.reconcile(event_id, expected_action="accept", expected_rev=1, outcome=outcome,
                                reason="No migration", actor="worker", **({"title": "Work"} if outcome == "open" else {}))
        with self.assertRaises(Denied):
            store.get(f"wi_{event_id}")
        # Missing old markers remain unresolved; no inference from rationale/source.
        from mizu.fs import write_json
        legacy = {key: value for key, value in decision.items()
                  if key not in ("work_item_intent", "decision_key")}
        write_json(self.project.root / "decision-history" / f"{'a' * 32}.json", legacy, exclusive=True)
        self.assertEqual(store.page_open()["unseeded_decisions"], 1)
        self.assertFalse(store.page_legacy()["coverage_complete"])
        self.assertEqual((self.project.root / "decision-history" / f"{event_id}.json").read_bytes(), original)

    def test_invalid_intent_or_action_has_no_side_effects(self):
        proposal = self.submit()
        for action in ("accept", "modify", "defer", "reject"):
            for intent in ("bad", "", True, [], {}, 1, "create", "none"):
                if action in ("accept", "modify") and intent in ("create", "none"):
                    continue
                with self.subTest(action=action, intent=intent), self.assertRaises(Denied):
                    self.decide(proposal, action, work_item_intent=intent)
        self.assertEqual(self.history_records(), [])
        self.assertFalse(self.decided(proposal["id"]))

    def test_exact_substantive_replay_is_noop_and_conflicts_require_revision(self):
        for first in ("accept", "modify", "reject"):
            for intent in ("create", "none") if first != "reject" else (None,):
                proposal = self.submit()
                original = self.decide(proposal, first, work_item_intent=intent)
                count = len(self.history_records())
                for action in ("accept", "modify", "reject", "defer"):
                    with self.subTest(first=first, intent=intent, action=action):
                        if action == first:
                            repeated = self.decide(proposal, action, work_item_intent=intent)
                            self.assertEqual(repeated, original)
                        else:
                            with self.assertRaisesRegex(Denied, "revise"):
                                self.decide(proposal, action)
                        self.assertEqual(len(self.history_records()), count)
                        self.assertEqual(json.loads((self.project.root / "decisions" / f"{proposal['id']}.json").read_text()), original)
                revised = self.revise(proposal)
                self.assertEqual(self.decide(revised)["rev"], 2)

    def test_defer_can_be_superseded_but_old_substantive_history_still_blocks(self):
        from mizu.fs import write_json
        proposal = self.submit()
        deferred = self.decide(proposal, "defer")
        self.decide(proposal, "defer")
        self.decide(proposal)
        count = len(self.history_records())
        # A keyed event is still found if its pointer was replaced.
        write_json(self.project.root / "decisions" / f"{proposal['id']}.json", deferred)
        with self.assertRaisesRegex(Denied, "revise"):
            self.decide(proposal, "reject")
        self.assertEqual(len(self.history_records()), count)

        # Actual pre-key history: random ID, no new-format marker, defer pointer.
        legacy = self.submit()
        deferred = {**deferred, "id": legacy["id"]}
        deferred.pop("decision_key")
        old = {**deferred, "action": "accept", "revisit": ""}
        history_path = self.project.root / "decision-history" / f"{'b' * 32}.json"
        write_json(history_path, old, exclusive=True)
        original = history_path.read_bytes()
        write_json(self.project.root / "decisions" / f"{legacy['id']}.json", deferred)
        count = len(self.history_records())
        with self.assertRaisesRegex(Denied, "revise"):
            self.decide(legacy)
        self.assertEqual(len(self.history_records()), count)
        self.assertEqual(history_path.read_bytes(), original)

    def test_cli_and_runtime_expose_override(self):
        from mizu.cli import _cmd_insight, parser
        proposal = self.submit("operator")
        args = parser().parse_args(["insight", "decide", self.project.name, "--id", proposal["id"],
                                    "--decision", "accept", "--reason", "Work", "--expected-rev", "1",
                                    "--work-item-intent", "create"])
        self.assertEqual(_cmd_insight(self.config, self.project, args)["work_item_intent"], "create")
        proposal = self.submit()
        result = self.context().handle("decide", {"id": proposal["id"], "action": "modify", "reason": "No work",
                                                  "rev": 1, "work_item_intent": "none"})
        self.assertEqual(result["work_item_intent"], "none")

    def test_config_policy_validation(self):
        from mizu.config import load
        from mizu.errors import ConfigError
        base = self.file.read_text()
        for before, after in (('work_item_intent_default = "create"', 'work_item_intent_default = false'),
                              ('work_item_intent_default = "create"', 'work_item_intent_default = "bad"'),
                              ('operator = "none"', 'operator = "bad"'),
                              ('operator = "none"', '"not a source" = "none"'),
                              ('[insight_decisions.work_item_intent_by_source]\noperator = "none"\nvcs = "none"',
                               'work_item_intent_by_source = []'),
                              ('[insight_decisions]', '[insight_decisions]\nunknown = "none"')):
            with self.subTest(after=after):
                self.assertIn(before, base)
                self.file.write_text(base.replace(before, after, 1))
                with self.assertRaises(ConfigError):
                    load(self.file)

    def test_concurrent_decisions_create_exactly_one_event(self):
        import concurrent.futures
        proposal = self.submit()
        def attempt(_):
            try:
                return self.decide(proposal)
            except Denied:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertTrue(all(result is not None for result in results))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.history_records()), 1)
        self.assertEqual(self.project.work_items.page_open()["total"], 1)

    def test_pointer_failure_after_history_commit_does_not_allow_duplicate(self):
        from unittest.mock import patch
        from mizu.fs import write_json
        for action, intent in (("accept", "create"), ("modify", "none"), ("reject", None)):
            for defer_first in (False, True):
                with self.subTest(action=action, intent=intent, defer_first=defer_first):
                    proposal = self.submit()
                    if defer_first:
                        self.decide(proposal, "defer")
                    count = len(self.history_records())
                    def write(path, *args, **kwargs):
                        if path.parent.name == "decisions":
                            raise OSError("synthetic pointer failure")
                        return write_json(path, *args, **kwargs)
                    with patch("mizu.insights.write_json", side_effect=write), self.assertRaises(OSError):
                        self.decide(proposal, action, work_item_intent=intent)
                    history = self.history_records()
                    self.assertEqual(len(history), count + 1)
                    original = next(e for e in history if e["id"] == proposal["id"] and e["action"] == action)
                    for changes in ({"action": "defer"}, {"action": "reject" if action != "reject" else "accept"},
                                    {"reason": "different"}, {"work_item_intent": "none" if intent == "create" else "create"}):
                        options = {"action": action, "reason": "reviewed", "revisit": "", "run": "run-1",
                                   "work_item_intent": intent, **changes}
                        with self.assertRaises(Denied):
                            self.project.insights.decide(proposal["id"], expected_rev=1, **options)
                        self.assertEqual(self.history_records(), history)
                    recovered = self.decide(proposal, action, work_item_intent=intent)
                    self.assertEqual(recovered, original)
                    self.assertEqual(self.history_records(), history)
                    pointer = self.project.root / "decisions" / f"{proposal['id']}.json"
                    self.assertEqual(json.loads(pointer.read_text()), original)

    def test_fresh_decisions_and_keyed_defers_never_scan_history(self):
        from pathlib import Path
        from unittest.mock import patch
        glob = Path.glob
        def bounded_glob(path, *args, **kwargs):
            if path.name == "decision-history":
                self.fail("Common decision path scanned history")
            return glob(path, *args, **kwargs)
        for action in ("accept", "modify", "reject", "defer"):
            for defer_first in (False, True):
                proposal = self.submit()
                with patch.object(Path, "glob", bounded_glob):
                    if defer_first:
                        self.decide(proposal, "defer")
                    self.decide(proposal, action)

    def test_stable_event_id_collision_is_refused_without_writes(self):
        from unittest.mock import patch
        from mizu.insights import _decision_key
        from mizu.fs import write_json
        proposal = self.submit()
        original = self.decide(proposal)
        key = _decision_key(proposal["id"], 1)
        self.assertRegex(key, r"^[0-9a-f]{32}$")
        other = self.submit()
        history = self.history_records()
        with patch("mizu.insights._decision_key", return_value=key), self.assertRaisesRegex(Denied, "collision"):
            self.decide(other)
        self.assertFalse(self.decided(other["id"]))
        self.assertEqual(self.history_records(), history)
        # A legacy event accidentally occupying the key is not recovery.
        other_key = _decision_key(other["id"], 1)
        legacy = {**original, "id": other["id"]}
        legacy.pop("decision_key")
        write_json(self.project.root / "decision-history" / f"{other_key}.json", legacy, exclusive=True)
        with self.assertRaisesRegex(Denied, "collision"):
            self.decide(other)

    def test_partial_commit_retry_reuses_intent_across_runs_and_policy_changes(self):
        from unittest.mock import patch
        from mizu.fs import write_json
        proposal = self.submit("worker")
        original_run = "first-run"
        def write(path, *args, **kwargs):
            if path.parent.name == "decisions":
                raise OSError("synthetic pointer failure")
            return write_json(path, *args, **kwargs)
        with patch("mizu.insights.write_json", side_effect=write), self.assertRaises(OSError):
            self.project.insights.decide(proposal["id"], "accept", "reviewed", "", original_run,
                                         expected_rev=1)
        history = self.history_records()
        self.assertEqual(len(history), 1)
        self.project.insights.work_item_intent_default = "none"
        retried = self.project.insights.decide(proposal["id"], "accept", "reviewed", "", "later-run",
                                               expected_rev=1)
        self.assertEqual(retried, history[0])
        self.assertEqual(retried["run"], original_run)
        self.assertEqual(retried["work_item_intent"], "create")
        self.assertEqual(self.history_records(), history)
        self.assertEqual(json.loads((self.project.root / "decisions" / f"{proposal['id']}.json").read_text()),
                         retried)
        with self.assertRaises(Denied):
            self.project.insights.decide(proposal["id"], "accept", "reviewed", "", "later-run",
                                         expected_rev=1, work_item_intent="none")
        self.assertEqual(self.history_records(), history)

    def test_pointer_recovery_rejects_seed_bound_to_different_title(self):
        from unittest.mock import patch
        from mizu.fs import write_json
        proposal = self.submit()
        def write(path, *args, **kwargs):
            if path.parent.name == "decisions":
                raise OSError("synthetic pointer failure")
            return write_json(path, *args, **kwargs)
        with patch("mizu.insights.write_json", side_effect=write), self.assertRaises(OSError):
            self.decide(proposal)
        event_path = self.project.root / "decision-history" / f"{_decision_key_for_test(proposal['id'])}.json"
        event = json.loads(event_path.read_text())
        event["work_item"]["title"] = "A different proposal"
        write_json(event_path, event)
        before = event_path.read_bytes()
        with self.assertRaisesRegex(Denied, "seed"):
            self.decide(proposal)
        self.assertEqual(event_path.read_bytes(), before)
        self.assertFalse(self.decided(proposal["id"]))

    def test_no_work_rejects_sidecars_and_malformed_marker_bindings(self):
        from mizu.fs import write_json
        self.decide(self.submit(), work_item_intent="none")
        path = next((self.project.root / "decision-history").glob("*.json"))
        event = json.loads(path.read_text())
        for changes in ({"rev": None}, {"work_item_intent": "bad"}, {"work_item": {}},
                        {"action": "reject"}, {"decision_key": "0" * 32},
                        {"decision_key": None}):
            write_json(path, {**event, **changes})
            with self.subTest(changes=changes), self.assertRaises(Denied):
                self.project.work_items.page_open()
        without_key = {key: value for key, value in event.items() if key != "decision_key"}
        write_json(path, without_key)
        with self.assertRaises(Denied):
            self.project.work_items.page_open()
        write_json(path, event)
        self.assertTrue(self.project.work_items.page_legacy()["coverage_complete"])
        write_json(self.project.root / "work-items" / f"wi_{path.stem}" / "legacy.json", {})
        with self.assertRaises(Denied):
            self.project.work_items.page_legacy()

    def test_no_work_audit_rejects_mismatched_decision_key(self):
        self.decide(self.submit(), work_item_intent="none")
        path = next((self.project.root / "decision-history").glob("*.json"))
        event = json.loads(path.read_text())
        event["decision_key"] = "0" * 32
        from mizu.fs import write_json
        write_json(path, event)
        with self.assertRaisesRegex(Denied, "decision key"):
            self.project.work_items.page_legacy()

    def test_new_revision_no_work_does_not_drop_previous_work(self):
        proposal = self.submit()
        original = self.decide(proposal)["work_item"]
        revised = self.revise(proposal)
        self.assertTrue(self.project.work_items.projection(original["id"])["stale"])
        self.decide(revised, "modify", work_item_intent="none")
        view = self.project.work_items.projection(original["id"])
        self.assertFalse(view["stale"])
        self.assertEqual(view["item"], original)
        self.assertIsNone(self.project.work_items.submitter_status(proposal["id"]))
        self.assertTrue(self.project.work_items.page_open()["coverage_complete"])


if __name__ == "__main__":
    unittest.main()
