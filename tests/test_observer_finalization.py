"""An interrupted observer finish must leave its source eligible for retry."""
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.errors import Busy
from mizu.fs import read_json
from mizu.runtime import Engine


class ObserverFinalizationTests(Fixture):
    def test_acknowledgement_conflict_preserves_observation_for_retry(self):
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        self.assertEqual(engine.run(self.project, "reviewer")["status"], "completed")
        cursor_path = self.project.root / "observed" / "reviewer.json"
        original_cursor = read_json(cursor_path)

        (self.project.workspace / "app.py").write_bytes(b"VALUE = 3\n")
        changed = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state="Changed code.", run=None,
            outcome="wait", summary="Code change.")
        self.project.snapshots.publish(changed)
        self.assertNotEqual(changed["code_digest"], original_cursor["code_digest"])

        with patch("mizu.runtime.operator_records.acknowledge",
                   side_effect=Busy("Already running: insights")) as acknowledge:
            with self.assertRaisesRegex(Busy, "Already running: insights"):
                engine.run(self.project, "reviewer")
        acknowledge.assert_called_once()
        self.assertEqual(len(driver.calls), 2)
        context = driver.calls[-1][0]
        self.assertIsNotNone(context.finished)
        interrupted = read_json(context.run_dir / "result.json")
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["error"], "Already running: insights")
        self.assertEqual(read_json(cursor_path), original_cursor)

        retried = engine.run(self.project, "reviewer")
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(len(driver.calls), 3)
        cursor = read_json(cursor_path)
        self.assertEqual(cursor["snapshot"], changed["id"])
        self.assertEqual(cursor["code_digest"], changed["code_digest"])
        self.assertEqual(engine.run(self.project, "reviewer").get("skipped"), "unchanged")
        self.assertEqual(len(driver.calls), 3)
