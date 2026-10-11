"""Owner revise/withdraw transport: submitting roles maintain their own insights."""
import dataclasses
import unittest

from support import Fixture
from mizu.errors import Denied, ConfigError
from mizu.runtime import check_consult_role


def with_caps(config, name, extra):
    role = config.roles[name]
    return dataclasses.replace(role, capabilities=tuple([*role.capabilities, *extra]))


class OwnerReviseTransportTests(Fixture):
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
