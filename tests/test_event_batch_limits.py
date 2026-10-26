"""Per-source event batch limits with per-role overrides.

Decision batches and terminal work-item batches are independent: each
dispatch uses the role-effective decision limit only for decisions and
the role-effective work-item limit only for terminal events. Both stay
under the fixed 100-event mechanism cap, and both sources require explicit
receipt selection.
"""
import dataclasses
import json
import re
import unittest

from support import Fixture, ScriptDriver, finish_with_event_acks
from mizu import work_item_events as events
from mizu.config import (
    load,
    role_decision_events_limit,
    role_work_item_events_limit,
)
from mizu.errors import ConfigError
from mizu.fs import read_json
from mizu.runtime import Engine


class EventBatchLimitTests(Fixture):
    def subscribed(self, **role_changes):
        roles = dict(self.config.roles)
        changes = {"decision_events": ("reject",),
                   "work_item_events": ("done", "dropped")}
        changes.update(role_changes)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], **changes)
        return dataclasses.replace(self.config, roles=roles)

    def reject(self, source="reviewer", title="Finding"):
        item = self.project.insights.submit(
            source=source, title=title, body="evidence",
            base_snapshot=self.project.snapshots.get()["id"])
        self.project.insights.decide(item["id"], "reject", "not material", "", "test", expected_rev=1)
        return item

    def drop(self, source="reviewer", serial=0):
        proposal = self.project.insights.submit(
            source=source, title=f"Terminal {serial}", body="evidence", base_snapshot=None)
        decision = self.project.insights.decide(
            proposal["id"], "accept", "reviewed", "", "run", expected_rev=1)
        return self.project.work_items.transition(
            decision["work_item"]["id"], expected_rev=1,
            status="dropped", actor="worker", reason="no longer needed")

    def test_defaults_and_inheritance(self):
        self.assertEqual(
            (self.config.limits.decision_events_limit,
             self.config.limits.work_item_events_limit), (10, 10))
        role = self.config.roles["reviewer"]
        self.assertIsNone(role.decision_events_limit)
        self.assertIsNone(role.work_item_events_limit)
        self.assertEqual(role_decision_events_limit(self.config, role), 10)
        self.assertEqual(role_work_item_events_limit(self.config, role), 10)

    def test_project_validation(self):
        import re
        original = self.file.read_text()
        for key in ("decision_events_limit", "work_item_events_limit"):
            for value, valid in (("1", True), ("10", True), ("100", True),
                                 ("0", False), ("101", False), ("true", False),
                                 ('"10"', False), ("1.5", False)):
                with self.subTest(key=key, value=value):
                    rewritten, count = re.subn(
                        f"(?m)^{key} = [^\\n]*", f"{key} = {value}", original)
                    self.assertEqual(count, 1)
                    self.file.write_text(rewritten)
                    if valid:
                        self.assertEqual(getattr(load(self.file).limits, key), int(value))
                    else:
                        with self.assertRaises(ConfigError):
                            load(self.file)

    def test_role_validation(self):
        original = self.file.read_text()
        for key in ("decision_events_limit", "work_item_events_limit"):
            for value, valid in (("1", True), ("100", True),
                                 ("0", False), ("101", False), ("true", False),
                                 ('"5"', False), ("2.5", False)):
                with self.subTest(key=key, value=value):
                    self.file.write_text(
                        original.replace("[roles.reviewer]",
                                         f"[roles.reviewer]\n{key} = {value}"))
                    if valid:
                        self.assertEqual(
                            getattr(load(self.file).roles["reviewer"], key), int(value))
                    else:
                        with self.assertRaises(ConfigError):
                            load(self.file)
            with self.subTest(key=key, unknown="unknown-key"):
                self.file.write_text(
                    original.replace("[roles.reviewer]",
                                     "[roles.reviewer]\ndecision_events_limit_typo = 5"))
                with self.assertRaises(ConfigError):
                    load(self.file)

    def test_role_override_applies_per_source(self):
        text = self.file.read_text()
        text = text.replace("[roles.reviewer]",
                            "[roles.reviewer]\ndecision_events_limit = 3")
        text, count = re.subn("(?m)^work_item_events_limit = [^\\n]*",
                              "work_item_events_limit = 7", text)
        self.assertEqual(count, 1)
        self.file.write_text(text)
        config = load(self.file)
        reviewer = config.roles["reviewer"]
        self.assertEqual(role_decision_events_limit(config, reviewer), 3)
        # The unset work-item override inherits the project default, not the
        # decision override: sources are independent.
        self.assertEqual(role_work_item_events_limit(config, reviewer), 7)
        self.assertEqual(role_decision_events_limit(config, config.roles["searcher"]), 10)
        self.assertEqual(role_work_item_events_limit(config, config.roles["searcher"]), 7)

    def test_dispatch_uses_independent_effective_limits(self):
        for _ in range(3):
            self.reject(title=f"Finding {_}")
        for serial in range(3):
            self.drop(serial=serial)
        config = self.subscribed(decision_events_limit=1, work_item_events_limit=2)
        self.project.config = config
        driver = ScriptDriver(finish_with_event_acks)
        result = Engine(config, driver=driver).run(
            self.project, "reviewer", event_trigger=True)
        self.assertEqual(result["status"], "completed")
        run_dir = driver.calls[-1][0].run_dir
        self.assertEqual(len(read_json(run_dir / "decision-events.json")["events"]), 1)
        page = read_json(run_dir / "work-item-events.json")
        self.assertEqual(len(page["events"]), 2)
        self.assertEqual(page["overflow_count"], 1)
        self.assertTrue(page["truncated"])
        # Only the selected batches acknowledge; the rest stays pending.
        self.assertEqual(len(self.project.insights.decision_events("reviewer", ("reject",))), 2)
        self.assertEqual(events.pending(
            self.project.work_items, "reviewer", ("done", "dropped"))["pending"], 1)
        # Draining continues with the same independent windows.
        second = Engine(config, driver=ScriptDriver(finish_with_event_acks)).run(
            self.project, "reviewer", event_trigger=True)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(self.project.insights.decision_events("reviewer", ("reject",))), 1)
        self.assertEqual(events.pending(
            self.project.work_items, "reviewer", ("done", "dropped"))["pending"], 0)

    def test_project_defaults_apply_without_role_override(self):
        for _ in range(3):
            self.reject(title=f"Finding {_}")
        for serial in range(3):
            self.drop(serial=serial)
        limited = dataclasses.replace(self.config.limits,
                                      decision_events_limit=2,
                                      work_item_events_limit=1)
        config = dataclasses.replace(self.subscribed(), limits=limited)
        self.project.config = config
        driver = ScriptDriver(finish_with_event_acks)
        result = Engine(config, driver=driver).run(
            self.project, "reviewer", event_trigger=True)
        self.assertEqual(result["status"], "completed")
        run_dir = driver.calls[-1][0].run_dir
        self.assertEqual(len(read_json(run_dir / "decision-events.json")["events"]), 2)
        self.assertEqual(len(read_json(run_dir / "work-item-events.json")["events"]), 1)

    def test_failure_replays_both_sources_without_partial_ack(self):
        self.reject()
        self.drop()
        config = self.subscribed(decision_events_limit=5, work_item_events_limit=5)
        self.project.config = config
        def boom(context, prompt, profile=None):
            raise RuntimeError("mid-run restart")
        with self.assertRaises(RuntimeError):
            Engine(config, driver=ScriptDriver(boom)).run(
                self.project, "reviewer", event_trigger=True)
        self.assertEqual(len(self.project.insights.decision_events("reviewer", ("reject",))), 1)
        self.assertEqual(events.pending(
            self.project.work_items, "reviewer", ("done", "dropped"))["pending"], 1)
        admitted = Engine(config, driver=ScriptDriver(finish_with_event_acks)).run(
            self.project, "reviewer", event_trigger=True)
        self.assertEqual(admitted["status"], "completed")
        self.assertEqual(self.project.insights.decision_events("reviewer", ("reject",)), [])
        self.assertEqual(events.pending(
            self.project.work_items, "reviewer", ("done", "dropped"))["pending"], 0)

    def test_progress_sample_stays_independent_of_dispatch_batch(self):
        # The read-only progress view keeps its own fixed 10-ID display
        # sample even when the dispatch batch is smaller.
        for serial in range(3):
            self.drop(serial=serial)
        config = self.subscribed(work_item_events_limit=1)
        summary = events.progress(
            self.project.work_items, {"reviewer": config.roles["reviewer"]})
        row = summary["roles"][0]
        self.assertEqual(row["pending"], 3)
        self.assertEqual(len(row["pending_event_ids"]), 3)
        self.assertFalse(row["pending_ids_truncated"])


if __name__ == "__main__":
    unittest.main()
