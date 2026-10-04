"""M10: on_change triggers on code digest change only, not state republication."""
from support import Fixture, ScriptDriver
from mizu.fs import read_json
from mizu.runtime import Engine


class OnChangeDigestTests(Fixture):
    def test_state_only_republication_does_not_reschedule(self):
        reviewer_driver = ScriptDriver()
        engine = Engine(self.config, driver=reviewer_driver)
        first = engine.run(self.project, "reviewer")
        self.assertEqual(first["status"], "completed")
        self.assertEqual(len(reviewer_driver.calls), 1)
        cursor = read_json(self.project.root / "observed" / "reviewer.json")
        self.assertEqual(cursor["code_digest"], self.project.snapshots.get()["code_digest"])

        # Writer publishes a state-only snapshot: same files, new id.
        captured = self.project.snapshots.capture_files(self.project.workspace)
        self.assertEqual(captured["code_digest"], self.project.snapshots.get()["code_digest"])
        republished = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Same code, new state.",
            run=None, outcome="wait", summary="State-only republication.")
        self.project.snapshots.publish(republished)
        self.assertNotEqual(republished["id"], cursor["snapshot"])
        self.assertEqual(republished["code_digest"], cursor["code_digest"])

        # Identical code must skip without a model call, even with a new id.
        skipped = engine.run(self.project, "reviewer")
        self.assertEqual(skipped.get("skipped"), "unchanged")
        self.assertEqual(len(reviewer_driver.calls), 1)

    def test_code_change_reschedules(self):
        engine = Engine(self.config, driver=ScriptDriver())
        engine.run(self.project, "reviewer")
        calls = engine.driver.calls
        before = len(calls)
        (self.project.workspace / "app.py").write_bytes(b"VALUE = 3\n")
        captured = self.project.snapshots.capture_files(self.project.workspace)
        changed = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Changed code.",
            run=None, outcome="wait", summary="Code change.")
        self.project.snapshots.publish(changed)
        result = engine.run(self.project, "reviewer")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(calls), before + 1)
        cursor = read_json(self.project.root / "observed" / "reviewer.json")
        self.assertEqual(cursor["code_digest"], changed["code_digest"])


if __name__ == "__main__":
    raise SystemExit(__import__("unittest").main())
