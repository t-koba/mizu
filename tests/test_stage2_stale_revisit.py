"""A superseded deferral's due revisit is acknowledged, not acted on."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts src and tests on the path)
from mizu.errors import Denied
from mizu.fs import canonical

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "development"))
import convert_legacy  # noqa: E402  (example userland module)
import test_stage2_decision_adapter as stage2_tests  # noqa: E402  (fixture helper)

DEFER_ID = stage2_tests.DEFER_ID
REVISIT_AT = stage2_tests.REVISIT_AT
BEFORE_REVISIT = "2098-12-31T23:59:59+00:00"
AFTER_REVISIT = "2099-01-01T00:00:01+00:00"
WRITER = "role:writer"


class Stage2StaleRevisit(unittest.TestCase):
    """The example reader rechecks the chain before acting on a due revisit."""

    def setUp(self):
        source = stage2_tests.Stage2DecisionAdapter(methodName="runTest")
        source.setUp()
        self.addCleanup(source.doCleanups)
        self.project, self.file, self.config = source.project, source.file, source.config

    def test_superseded_deferral_revisit_is_acked_without_action(self):
        store = self.project.kernel_queues
        report = convert_legacy.apply(self.project)  # idempotent conversion report
        revisit_id = convert_legacy._message_id("revisit", DEFER_ID, 1)
        proposal_id = convert_legacy._message_id("proposal", DEFER_ID, 1)
        self.assertEqual(report["revisits"], [revisit_id])

        pending = store.read("decisions", WRITER, now=BEFORE_REVISIT)
        self.assertTrue(pending["delayed"])
        self.assertNotIn(revisit_id, [message["id"] for message in pending["messages"]])

        # A later decision supersedes the deferral before the revisit is due.
        supersede = store.append(
            "decisions", {"action": "accept", "reason": "Evidence arrived"}, WRITER,
            refs=[proposal_id], now=BEFORE_REVISIT)
        due = store.read("decisions", WRITER, now=REVISIT_AT)
        revisit = next(message for message in due["messages"] if message["id"] == revisit_id)
        self.assertEqual(revisit["body"]["action"], "defer")
        self.assertEqual({message["id"] for message in due["messages"]},
                         {*report["decisions"], revisit_id, supersede["id"]})
        self.assertEqual([message["seq"] for message in due["messages"]],
                         sorted(message["seq"] for message in due["messages"]))
        with self.assertRaisesRegex(Denied, "undelivered message"):
            store.ack("decisions", WRITER, revisit["seq"], now=BEFORE_REVISIT)

        messages = canonical(due["messages"])
        with mock.patch.object(store, "append", side_effect=AssertionError("stale action")):
            # The consumer follows stage2.md: refs give the chain, not authority.
            targets = store.refs(revisit_id, "from", WRITER)["messages"]
            self.assertEqual([message["id"] for message in targets], [proposal_id])
            chain = store.refs(proposal_id, "to", WRITER)["messages"]
            later = [message for message in chain
                     if message["queue"] == "decisions"
                     and message["deliver_at"] is None
                     and message["seq"] > revisit["seq"]]
            self.assertEqual([message["id"] for message in later], [supersede["id"]])
            # Stale: acknowledge only the revisit and append no action.
            self.assertEqual(store.ack("decisions", WRITER, revisit["seq"], now=REVISIT_AT),
                             revisit["seq"])

        after = store.read("decisions", WRITER, now=AFTER_REVISIT)
        self.assertEqual(after["next"], supersede["seq"])
        self.assertEqual([message["id"] for message in after["messages"]],
                         [message["id"] for message in due["messages"]
                          if message["seq"] > revisit["seq"]])
        self.assertEqual(after["head"], supersede["seq"])
        self.assertEqual(canonical(store.read("decisions", WRITER, cursor=0,
                                             now=AFTER_REVISIT)["messages"]), messages)


if __name__ == "__main__":
    unittest.main()
