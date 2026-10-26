"""Owner revise/withdraw transport: submitting roles maintain their own insights."""
import dataclasses
import json
import unittest
from unittest.mock import patch

from support import Fixture
from mizu.errors import Denied, ConfigError
from mizu.config import load
from mizu.runtime import check_consult_role


def with_caps(config, name, extra):
    role = config.roles[name]
    return dataclasses.replace(role, capabilities=tuple([*role.capabilities, *extra]))


class OwnerReviseTransportTests(Fixture):
    def test_context_handlers_forward_to_insight_tools(self):
        ctx = self.context("searcher")
        args = {"title": "Claim", "body": "v1"}
        for handler, operation_name in (
                ("_op_insights", "insights"), ("_op_decide", "decide"),
                ("_op_submit_insight", "submit_insight"),
                ("_op_revise_insight", "revise_insight"),
                ("_op_withdraw_insight", "withdraw_insight"),
                ("_op_attach_evidence", "attach_evidence")):
            expected = {"forwarded": handler}
            with patch(f"mizu.insight_tools.{operation_name}", return_value=expected) as operation:
                self.assertIs(getattr(ctx, handler)(args), expected)
            operation.assert_called_once_with(ctx, args)

    def test_owner_revise_own_insight_reopens_topic(self):
        ctx = self.context("searcher")
        item = ctx.handle("submit_insight", {"title": "Claim", "body": "v1"})
        self.assertEqual(item["rev"], 1)
        revised = ctx.handle("revise_insight", {"id": item["id"], "title": "Claim", "body": "v2", "rev": 1})
        self.assertEqual(revised["rev"], 2)
        self.assertEqual(revised["source"], "searcher")
        self.assertEqual(self.project.insights.read(item["id"])["body"], "v2")

    def test_revise_requires_capability(self):
        ctx = self.context("searcher")
        item = ctx.handle("submit_insight", {"title": "Claim", "body": "v1"})
        ctx_no_cap = self.context("reporter")
        with self.assertRaises(Denied):
            ctx_no_cap.handle("revise_insight", {"id": item["id"], "title": "Claim", "body": "hijack", "rev": 1})
        self.assertEqual(self.project.insights.read(item["id"])["body"], "v1")

    def test_cannot_revise_others_insight(self):
        searcher = self.context("searcher")
        item = searcher.handle("submit_insight", {"title": "Claim", "body": "v1"})
        reviewer = self.context("reviewer")
        with self.assertRaises(Denied):
            reviewer.handle("revise_insight", {"id": item["id"], "title": "Claim", "body": "hijack", "rev": 1})
        self.assertEqual(self.project.insights.read(item["id"])["body"], "v1")

    def test_stale_rev_refused(self):
        ctx = self.context("searcher")
        item = ctx.handle("submit_insight", {"title": "Claim", "body": "v1"})
        ctx.handle("revise_insight", {"id": item["id"], "title": "Claim", "body": "v2", "rev": 1})
        with self.assertRaises(Denied):
            ctx.handle("revise_insight", {"id": item["id"], "title": "Claim", "body": "stale", "rev": 1})
        self.assertEqual(self.project.insights.read(item["id"])["body"], "v2")

    def test_withdraw_decided_is_absent_from_default_roles(self):
        self.assertTrue(all("withdraw_decided" not in role.capabilities
                            for role in self.config.roles.values()))

    def _grant_withdraw_decided(self):
        text = self.file.read_text()
        self.assertIn('"withdraw_insight", "work_items_read"', text)
        self.file.write_text(text.replace('"withdraw_insight", "work_items_read"',
                                          '"withdraw_insight", "withdraw_decided", "work_items_read"', 1))
        self.config = load(self.file)
        role = self.config.roles["searcher"]
        self.assertIn("withdraw_insight", role.capabilities)
        self.assertIn("withdraw_decided", role.capabilities)
        return self.context("searcher")

    def test_accept_and_modify_block_withdrawal_without_writes(self):
        for action in ("accept", "modify"):
            with self.subTest(action=action):
                ctx = self.context("searcher")
                item = ctx.handle("submit_insight", {"title": "Claim", "body": action})
                original = self.project.insights.decide(item["id"], action, "reviewed", "", "run-review",
                                                        expected_rev=1)
                current_path = self.project.root / "decisions" / f"{item['id']}.json"
                history_dir = self.project.root / "decision-history"
                before_current = current_path.read_bytes()
                before_history = {path.name: path.read_bytes() for path in history_dir.glob("*.json")}
                with self.assertRaisesRegex(Denied, action):
                    ctx.handle("withdraw_insight", {"id": item["id"], "reason": "retract", "rev": 1})
                self.assertEqual(current_path.read_bytes(), before_current)
                self.assertEqual({path.name: path.read_bytes() for path in history_dir.glob("*.json")},
                                 before_history)
                self.assertEqual(json.loads(current_path.read_text()), original)
                self.assertEqual(json.loads(current_path.read_text())["action"], action)

    def test_withdraw_decided_opt_in_is_supplemental_and_history_is_preserved(self):
        ctx = self._grant_withdraw_decided()
        item = ctx.handle("submit_insight", {"title": "Claim", "body": "v1"})
        accepted = self.project.insights.decide(item["id"], "modify", "reviewed", "", "run-review",
                                                expected_rev=1)
        result = ctx.handle("withdraw_insight", {"id": item["id"], "reason": "retract", "rev": 1})
        self.assertEqual(result["action"], "withdraw")
        current = json.loads((self.project.root / "decisions" / f"{item['id']}.json").read_text())
        self.assertEqual(current, result)
        history = [json.loads(path.read_text()) for path in
                   (self.project.root / "decision-history").glob("*.json")]
        self.assertEqual({entry["action"] for entry in history}, {"modify", "withdraw"})
        self.assertIn(accepted, history)

    def test_operator_path_does_not_inherit_supplemental_grant(self):
        ctx = self.context("searcher")
        item = ctx.handle("submit_insight", {"title": "Claim", "body": "v1"})
        decision = self.project.insights.decide(item["id"], "accept", "reviewed", "", "run-review",
                                                expected_rev=1)
        with self.assertRaisesRegex(Denied, "current accept"):
            self.project.insights.withdraw(item["id"], source="operator", reason="retract",
                                           expected_rev=1)
        self.assertEqual(json.loads((self.project.root / "decisions" / f"{item['id']}.json").read_text()),
                         decision)

    def test_deferred_and_rejected_proposals_remain_withdrawable(self):
        ctx = self.context("searcher")
        for action in ("defer", "reject"):
            with self.subTest(action=action):
                item = ctx.handle("submit_insight", {"title": "Claim", "body": action})
                self.project.insights.decide(item["id"], action, "reviewed", "revisit" if action == "defer" else "",
                                             "run-review", expected_rev=1)
                result = ctx.handle("withdraw_insight", {"id": item["id"], "reason": "retract", "rev": 1})
                self.assertEqual(result["action"], "withdraw")

    def test_revision_reopens_decided_insight_for_normal_withdrawal(self):
        ctx = self.context("searcher")
        item = ctx.handle("submit_insight", {"title": "Claim", "body": "v1"})
        self.project.insights.decide(item["id"], "accept", "reviewed", "", "run-review",
                                     expected_rev=1)
        with self.assertRaisesRegex(Denied, "current accept"):
            ctx.handle("withdraw_insight", {"id": item["id"], "reason": "retract", "rev": 1})
        revised = ctx.handle("revise_insight", {"id": item["id"], "title": "Claim", "body": "v2", "rev": 1})
        self.assertEqual(revised["rev"], 2)
        result = ctx.handle("withdraw_insight", {"id": item["id"], "reason": "retract revised proposal", "rev": 2})
        self.assertEqual((result["action"], result["rev"]), ("withdraw", 2))
        history = [json.loads(path.read_text()) for path in
                   (self.project.root / "decision-history").glob("*.json")]
        self.assertEqual({(entry["action"], entry["rev"]) for entry in history},
                         {("accept", 1), ("withdraw", 2)})

    def test_stale_expected_revision_still_blocks_withdrawal(self):
        ctx = self.context("searcher")
        item = ctx.handle("submit_insight", {"title": "Claim", "body": "v1"})
        revised = ctx.handle("revise_insight", {"id": item["id"], "title": "Claim", "body": "v2", "rev": 1})
        decision = self.project.insights.decide(item["id"], "accept", "reviewed", "", "run-review",
                                                expected_rev=revised["rev"])
        with self.assertRaisesRegex(Denied, "revision conflict"):
            ctx.handle("withdraw_insight", {"id": item["id"], "reason": "stale", "rev": 1})
        self.assertEqual(json.loads((self.project.root / "decisions" / f"{item['id']}.json").read_text()),
                         decision)

    def test_withdraw_decided_requires_withdraw_insight(self):
        text = self.file.read_text().replace('"withdraw_insight", "work_items_read"',
                                             '"withdraw_decided", "work_items_read"', 1)
        self.file.write_text(text)
        with self.assertRaisesRegex(ConfigError, "withdraw_decided requires withdraw_insight"):
            load(self.file)

    def test_consult_role_cannot_hold_decided_withdraw_permission(self):
        role = with_caps(self.config, "consult", ("withdraw_decided",))
        with self.assertRaises(ConfigError):
            check_consult_role("consult", role)

    def test_owner_withdraw_own_insight(self):
        ctx = self.context("searcher")
        item = ctx.handle("submit_insight", {"title": "Claim", "body": "v1"})
        record = ctx.handle("withdraw_insight", {"id": item["id"], "reason": "no longer needed", "rev": 1})
        self.assertEqual((record["action"], record["actor"]), ("withdraw", "searcher"))

    def test_cannot_withdraw_others_insight(self):
        searcher = self.context("searcher")
        item = searcher.handle("submit_insight", {"title": "Claim", "body": "v1"})
        reviewer = self.context("reviewer")
        with self.assertRaises(Denied):
            reviewer.handle("withdraw_insight", {"id": item["id"], "reason": "seizing", "rev": 1})

    def test_withdraw_requires_capability(self):
        ctx = self.context("searcher")
        item = ctx.handle("submit_insight", {"title": "Claim", "body": "v1"})
        ctx_no_cap = self.context("reporter")
        with self.assertRaises(Denied):
            ctx_no_cap.handle("withdraw_insight", {"id": item["id"], "reason": "x", "rev": 1})

    def test_malformed_rev_denied_by_schema(self):
        ctx = self.context("searcher")
        item = ctx.handle("submit_insight", {"title": "Claim", "body": "v1"})
        with self.assertRaises(Denied):
            ctx.handle("revise_insight", {"id": item["id"], "title": "Claim", "body": "v2", "rev": 0})
        with self.assertRaises(Denied):
            ctx.handle("withdraw_insight", {"id": item["id"], "reason": "x", "rev": 0})

    def test_consult_role_cannot_hold_owner_transport(self):
        role = with_caps(self.config, "consult", ("revise_insight",))
        with self.assertRaises(ConfigError):
            check_consult_role("consult", role)
        role = with_caps(self.config, "consult", ("withdraw_insight",))
        with self.assertRaises(ConfigError):
            check_consult_role("consult", role)


if __name__ == "__main__":
    unittest.main()
