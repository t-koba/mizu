"""Proof that the two remaining 'legacy' readers are live-write authorities.

Decision receipts and retained insight identities are the current formats for a
project without shared/queue authority. Install-time conversion deliberately
does not cover them (see ``uncovered_families`` in the promoter and the shared
backup/identity tests), and their readers cannot be removed without losing live
write behaviour. These tests record that proof so a future removal is caught.
"""
from support import Fixture, forget_ledger

from mizu import insight_archive
from mizu.decision_delivery import authority_kind
from mizu.errors import Denied
from mizu.fs import read_json
from mizu.insights import Insights
from test_decision_events import _reject


class LegacyReaderProofTests(Fixture):

    def test_decision_receipt_reader_is_load_bearing_before_authority(self):
        # A default project has no authority marker, so per-token receipts are
        # the live acknowledgement authority rather than a superseded format.
        self.assertIsNone(authority_kind(self.project.insights.root))
        _reject(self.project, "reviewer")
        event, = self.project.insights.decision_events("reviewer", ("reject",))
        self.project.insights.acknowledge_decisions("reviewer", [event])
        path = self.project.root / "decision-receipts" / "reviewer" / f"{event['ack_token']}.json"
        receipt = read_json(path)
        self.assertEqual((receipt["role"], receipt["event"], receipt["ack_token"]),
                         ("reviewer", event["event"], event["ack_token"]))
        # A restarted reader consumes that exact on-disk receipt. Dropping the
        # reader would re-deliver an already handled decision on every wake.
        restarted = Insights(self.project.root)
        self.assertEqual(restarted.decision_events("reviewer", ("reject",)), [])
        self.assertTrue(restarted._decision_received("reviewer", event))

    def test_retained_identity_reader_is_load_bearing_for_live_writes(self):
        key = "a" * 32
        args = dict(source="searcher", title="T", body="B", base_snapshot=None, insight_id=key)
        self.project.insights.submit(**args)
        identity = self.project.root / "insight-ids" / f"{key}.json"
        self.assertEqual(read_json(identity)["id"], key)
        # With the body gone and no archived row, the live submit path still
        # reconstructs from the retained identity and refuses changed content.
        (self.project.root / "inbox" / f"{key}.json").unlink()
        forget_ledger(self.project.root)
        self.assertIsNone(insight_archive.read(self.project.root, key))
        rebuilt = self.project.insights.submit(**args)
        self.assertEqual((rebuilt["id"], rebuilt["title"], rebuilt["body"], rebuilt["rev"]),
                         (key, "T", "B", 1))
        self.assertFalse(rebuilt.get("retained"))
        with self.assertRaisesRegex(Denied, "reused with different content"):
            self.project.insights.submit(**{**args, "body": "OTHER"})
