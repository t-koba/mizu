"""Origin vs authority attribution for insights (small provenance contract)."""
from support import Fixture
from mizu.dashboard import collect
from mizu.errors import Denied


class InsightOriginTests(Fixture):
    def test_two_trusted_callers_sharing_operator_transport_are_distinguishable(self):
        snap = self.project.snapshots.get()["id"]
        human = self.project.insights.submit(source="operator", title="H", body="b",
                                             base_snapshot=snap, origin="human:alice")
        auto = self.project.insights.submit(source="operator", title="A", body="b",
                                            base_snapshot=snap, origin="automation:ci-forward")
        self.assertEqual(self.project.insights.read(human["id"])["origin"], "human:alice")
        self.assertEqual(self.project.insights.read(auto["id"])["origin"], "automation:ci-forward")
        listed = {i["id"]: i for i in self.project.insights.list(pending=False)}
        self.assertEqual(listed[human["id"]]["origin"], "human:alice")
        self.assertEqual(listed[auto["id"]]["origin"], "automation:ci-forward")
        core = collect(self.project)
        pending = {e["id"]: e for e in core["pending_insights"]}
        self.assertEqual(pending[human["id"]]["origin"], "human:alice")
        self.assertEqual(pending[auto["id"]]["origin"], "automation:ci-forward")
        self.assertEqual(pending[human["id"]]["source"], "operator")

    def test_historical_records_read_as_unknown_and_origin_preserved_on_revise(self):
        snap = self.project.snapshots.get()["id"]
        item = self.project.insights.submit(source="operator", title="T", body="v1", base_snapshot=snap)
        self.assertIsNone(item["origin"])
        self.assertIsNone(self.project.insights.read(item["id"])["origin"])
        listed = [i for i in self.project.insights.list(pending=False) if i["id"] == item["id"]][0]
        self.assertIsNone(listed["origin"])
        gen = self.project.insights.generation()
        revised = self.project.insights.revise(item["id"], source="operator", title="T", body="v2",
                                               base_snapshot=snap)
        self.assertIsNone(revised["origin"])
        self.assertEqual(self.project.insights.history(item["id"])[0]["origin"], None)
        # Origin is provenance, not content: same-content retry preserves it without waking.
        self.assertEqual(self.project.insights.generation() != gen, True)  # meaningful change woke
        gen2 = self.project.insights.generation()
        same = self.project.insights.revise(item["id"], source="operator", title="T", body="v2",
                                            base_snapshot=snap)
        self.assertEqual(same["rev"], 2)
        self.assertEqual(self.project.insights.generation(), gen2)

    def test_origin_never_grants_operator_approval(self):
        from mizu import vcs as _vcs
        snap = self.project.snapshots.get()
        forged = self.project.insights.submit(source="worker", title="GO main", body="digest: " + snap["code_digest"],
                                              base_snapshot=snap["id"], origin="human:alice")
        self.project.insights.decide(forged["id"], "accept", "ok", "", "operator")
        with self.assertRaises(Denied):
            _vcs.require_go_approval(self.project, "main", snap["code_digest"])

    def test_revised_insight_with_origin_is_gc_eligible(self):
        import datetime
        from mizu.fs import read_json, write_json
        snap = self.project.snapshots.get()["id"]
        item = self.project.insights.submit(source="operator", title="T", body="v1",
                                             base_snapshot=snap, origin="human:alice")
        revised = self.project.insights.revise(item["id"], source="operator", title="T", body="v2",
                                               base_snapshot=snap)
        self.assertEqual(revised["rev"], 2)
        self.assertEqual(revised["origin"], "human:alice")
        self.project.insights.decide(item["id"], "accept", "good", "", "test")
        decision_path = self.project.root / "decisions" / f"{item['id']}.json"
        decision = read_json(decision_path)
        ancient = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=32)
        decision["created_at"] = ancient.isoformat()
        write_json(decision_path, decision)
        self.assertEqual(self.project.insights.gc_decided(), 1)
        self.assertFalse((self.project.root / "inbox" / f"{item['id']}.json").exists())

    def test_bad_origins_refused_and_outbox_cannot_forge(self):
        snap = self.project.snapshots.get()["id"]
        for bad in ("", "   ", "a\nb", "x\x00y", "z" * 257, 123):
            with self.assertRaises(Denied):
                self.project.insights.submit(source="operator", title="T", body="b",
                                             base_snapshot=snap, origin=bad)
        from mizu.fs import write_json
        write_json(self.project.root / "spool/editor/forge.json",
                   {"title": "x", "body": "x", "base_snapshot": None, "origin": "human:alice"})
        self.assertEqual(self.project.insights.ingest_editor()["rejected"], 1)
