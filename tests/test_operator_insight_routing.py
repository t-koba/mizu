"""Operator insights reach a configured project role by read-time resolution.

Synthetic drivers only: both legacy and shared/queue authority are covered
with the real local boundary, no model, service manager or deployment.
"""
import json
from pathlib import Path

from support import Fixture
from mizu import decision_delivery as delivery
from mizu.config import load
from mizu.decision_reconciliation import activate_decision_delivery
from mizu.errors import ConfigError
from mizu.fs import read_json
from mizu.insight_tools import display_insight
from mizu.runtime import _role_events_due
from mizu.unit_prompts import prompt_delta_for, prompt_for


class OperatorInsightRoutingTests(Fixture):
    shared = False

    def setUp(self):
        super().setUp()
        if self.shared:
            activate_decision_delivery(self.project)
        self.default = self.config.insight_decisions["operator_addressee"]
        self.assertEqual(self.default, "worker")

    def submit(self, **metadata):
        return self.project.insights.submit(source="operator", title="Operator attention",
                                            body="Exact current body", base_snapshot=None,
                                            **metadata)

    def pending(self, role):
        return delivery.pending(self.project, self.config.roles[role])["events"]

    def test_default_recipient_receives_pending_event_and_wake(self):
        item = self.submit()
        # Nothing in the stored record or its content identity is rewritten.
        stored = self.project.insights.read(item["id"])
        self.assertEqual((stored["source"], stored["addressee"]), ("operator", "writer"))
        events = self.pending(self.default)
        self.assertEqual([e["insight"] for e in events], [item["id"]])
        row = events[0]
        self.assertEqual((row["kind"], row["source"], row["target"], row["addressee"]),
                         ("insight.pending", "operator", self.default, self.default))
        snap = self.project.snapshots.get()
        self.assertTrue(_role_events_due(self.project, snap, self.default))
        for role in ("searcher", "reviewer", "reporter"):
            self.assertEqual(self.pending(role), [])
            self.assertFalse(_role_events_due(self.project, snap, role))

    def test_explicit_operator_addressee_stays_in_host_inbox(self):
        item = self.submit(addressee="operator")
        self.assertEqual(self.project.insights.read(item["id"])["addressee"], "operator")
        for role in self.project.roles:
            self.assertNotIn(item["id"], {e["insight"] for e in self.pending(role)})
        snap = self.project.snapshots.get()
        for role in self.project.roles:
            self.assertFalse(_role_events_due(self.project, snap, role))

    def test_attention_names_the_deciding_role(self):
        item = self.submit()
        record = self.project.insights.read(item["id"])
        projected = display_insight(record, self.config.roles[self.default].capabilities,
                                    self.default)
        self.assertEqual(projected["attention"], self.default)
        self.assertNotIn("addressee", projected)
        self.assertEqual(record["addressee"], "writer")
        ctx = self.context(self.default)
        ctx.decision_events = self.pending(self.default)
        event = ctx.decision_events[0]
        for prompt in (prompt_for(ctx),
                       prompt_delta_for(ctx, self.project.insights.list())):
            body = json.loads(prompt)
            row = next(r for r in body["decision_events"] if r.get("insight") == item["id"])
            self.assertEqual(row["attention"], self.default)
            self.assertEqual(row["ack_token"], event["ack_token"])
            pending = next(r for r in body["pending_insights"] if r["id"] == item["id"])
            self.assertEqual(pending["attention"], self.default)
        self.assertEqual(ctx.decision_events[0], event)

    def test_preexisting_undecided_record_becomes_deliverable_without_resubmission(self):
        insights = self.project.insights
        insights.operator_addressee = ""  # A record written before the setting existed.
        item = self.submit()
        path = self.project.root / "inbox" / f"{item['id']}.json"
        raw = path.read_bytes()
        self.assertEqual(read_json(path)["addressee"], "writer")
        self.assertEqual(self.pending(self.default), [])
        # Only the read-time policy changes; the record and its event stay exact.
        insights.operator_addressee = self.default
        events = self.pending(self.default)
        self.assertEqual([e["insight"] for e in events], [item["id"]])
        self.assertEqual(events[0]["target"], self.default)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.project.insights.read(item["id"])["rev"], 1)

    def test_cli_submit_honors_explicit_addressee_or_configured_default(self):
        from mizu.cli import _cmd_insight, parser
        body = self.root / "body.md"
        body.write_text("Operator CLI body")

        def submit(*extra):
            args = parser().parse_args(
                ["insight", "submit", self.project.name, "--title", "CLI attention",
                 "--body", str(body), *extra])
            return _cmd_insight(self.config, self.project, args)

        item = submit()
        self.assertEqual(self.project.insights.read(item["id"])["addressee"], "writer")
        self.assertEqual([e["insight"] for e in self.pending(self.default)], [item["id"]])
        human = submit("--addressee", "operator")
        self.assertEqual(self.project.insights.read(human["id"])["addressee"], "operator")
        self.assertEqual([e["insight"] for e in self.pending(self.default)], [item["id"]])
        with self.assertRaises(ConfigError):
            submit("--addressee", "missing")

    def test_config_rejects_unusable_operator_addressee(self):
        base = self.file.read_text()
        for value in ('"missing"', '"../escape"', '"operator"', '"vcs"', '"Bad Name"', '1'):
            with self.subTest(value=value):
                self.file.write_text(base.replace('operator_addressee = "worker"',
                                                  f'operator_addressee = {value}'))
                with self.assertRaises(ConfigError):
                    load(self.file)
        for value in ('""', '"reviewer"'):
            with self.subTest(value=value):
                self.file.write_text(base.replace('operator_addressee = "worker"',
                                                  f'operator_addressee = {value}'))
                config = load(self.file)
                self.assertEqual(config.insight_decisions["operator_addressee"], value.strip('"'))
        self.file.write_text(base)

    def test_config_rejects_writer_sentinel_even_with_a_writer_role(self):
        # The 'writer' addressee is the route-to-source sentinel. If a project
        # actually names a role 'writer', accepting it as the default recipient
        # still resolves to an emitted addressee that pending projection rejects
        # with "Invalid pending insight target". Fail closed at load instead.
        base = self.file.read_text()
        writer_role = base[base.index("[roles.worker]"):base.index("[roles.searcher]")].replace(
            "[roles.worker]", "[roles.writer]", 1)
        with_writer = base.replace("[roles.searcher]", writer_role + "[roles.searcher]", 1)
        self.file.write_text(with_writer.replace('operator_addressee = "worker"',
                                                 'operator_addressee = "writer"'))
        with self.assertRaises(ConfigError):
            load(self.file)
        # A valid explicitly-named recipient, including the writer role itself
        # as an explicit insight addressee, is unaffected by config validation.
        self.file.write_text(with_writer.replace('operator_addressee = "worker"',
                                                 'operator_addressee = "searcher"'))
        config = load(self.file)
        self.assertEqual(config.insight_decisions["operator_addressee"], "searcher")
        self.assertIn("writer", config.roles)
        self.file.write_text(base)


class OperatorInsightRoutingSharedTests(OperatorInsightRoutingTests):
    shared = True
