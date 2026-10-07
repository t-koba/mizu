"""Role-owned current research state: replacement with CAS. Offline only."""
import unittest

from support import Fixture
from mizu.errors import ConfigError, Denied
from mizu.project import Project

STATE = {"findings": [{"q": "offline cache shape", "a": "unavailable here"}],
         "unknowns": ["upstream rate limits"],
         "coverage": [{"ref": "run evidence abc", "topic": "cache"}],
         "revisit": ["when cache becomes reachable"]}


class RoleStateTests(Fixture):
    def test_absent_reads_distinct_from_empty(self):
        out = self.project.role_state.read("searcher")
        self.assertEqual(out, {"status": "absent", "generation": 0,
                               "record": None})

    def test_first_replace_starts_generation_one(self):
        out = self.project.role_state.replace("searcher", STATE,
                                              expected_generation=0,
                                              run="run-1")
        self.assertEqual(out["generation"], 1)
        seen = self.project.role_state.read("searcher")
        self.assertEqual(seen["status"], "current")
        self.assertEqual(seen["generation"], 1)
        self.assertEqual(seen["record"]["state"], STATE)
        self.assertEqual(seen["record"]["updated_by_run"], "run-1")

    def test_replace_supersedes_without_history(self):
        self.project.role_state.replace("searcher", STATE,
                                        expected_generation=0)
        second = {"findings": [], "unknowns": [],
                  "coverage": [], "revisit": ["new evidence"]}
        self.project.role_state.replace("searcher", second,
                                        expected_generation=1)
        seen = self.project.role_state.read("searcher")
        self.assertEqual(seen["generation"], 2)
        self.assertEqual(seen["record"]["state"], second)

    def test_stale_replace_refused_and_preserved(self):
        self.project.role_state.replace("searcher", STATE,
                                        expected_generation=0)
        self.project.role_state.replace(
            "searcher", {**STATE, "unknowns": []}, expected_generation=1)
        with self.assertRaisesRegex(Denied, "Stale"):
            self.project.role_state.replace("searcher", {"other": True},
                                            expected_generation=1)
        seen = self.project.role_state.read("searcher")
        self.assertEqual(seen["generation"], 2)
        self.assertIn("unknowns", seen["record"]["state"])

    def test_concurrent_writers_fail_safely(self):
        store = self.project.role_state
        store.replace("searcher", STATE, expected_generation=0)
        # Two sessions read generation 1; only one replacement lands.
        store.replace("searcher", {"by": "first"}, expected_generation=1)
        with self.assertRaisesRegex(Denied, "Stale"):
            store.replace("searcher", {"by": "second"}, expected_generation=1)
        self.assertEqual(store.read("searcher")["record"]["state"],
                         {"by": "first"})

    def test_invalid_state_preserves_previous(self):
        store = self.project.role_state
        store.replace("searcher", STATE, expected_generation=0)
        for bad in (["not", "an", "object"],
                    {"x" * 65: 1},
                    {"ok": "fine", "deep": {"a": {"b": {"c": {"d": 1}}}}},
                    {"ok": object()},
                    {"big": "x" * 9000}):
            with self.assertRaises(Denied):
                store.replace("searcher", bad, expected_generation=1)
        seen = store.read("searcher")
        self.assertEqual(seen["generation"], 1)
        self.assertEqual(seen["record"]["state"], STATE)

    def test_corrupt_file_reads_unavailable_and_blocks_replace(self):
        path = self.project.root / "role-state" / "searcher.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        seen = self.project.role_state.read("searcher")
        self.assertEqual(seen["status"], "unavailable")
        self.assertIsNone(seen["generation"])
        with self.assertRaisesRegex(Denied, "unavailable"):
            self.project.role_state.replace("searcher", STATE,
                                            expected_generation=0)
        # Operator clears the file; the next first write lands at one.
        path.unlink()
        out = self.project.role_state.replace("searcher", STATE,
                                              expected_generation=0)
        self.assertEqual(out["generation"], 1)

    def test_fresh_session_sees_latest_generation(self):
        self.project.role_state.replace("searcher", STATE,
                                        expected_generation=0)
        reopened = Project(self.config, self.project.name)
        seen = reopened.role_state.read("searcher")
        self.assertEqual(seen["generation"], 1)
        self.assertEqual(seen["record"]["state"], STATE)
        # A stale session handle cannot overwrite the newer record.
        with self.assertRaisesRegex(Denied, "Stale"):
            self.project.role_state.replace("searcher", {"old": True},
                                            expected_generation=0)

    def test_state_never_uses_insights(self):
        before = self.project.insights.projection(pending=False)["total"]
        self.project.role_state.replace("searcher", STATE,
                                        expected_generation=0)
        after = self.project.insights.projection(pending=False)["total"]
        self.assertEqual(before, after)

    def test_roles_are_isolated(self):
        self.project.role_state.replace("searcher", STATE,
                                        expected_generation=0)
        self.assertEqual(
            self.project.role_state.read("worker")["status"], "absent")

    def test_bad_role_refused(self):
        with self.assertRaises(Denied):
            self.project.role_state.read("../escape")
        with self.assertRaises(Denied):
            self.project.role_state.replace("../escape", STATE,
                                            expected_generation=0)


class RoleStateConfigTests(Fixture):
    def test_default_bound(self):
        self.assertEqual(self.config.limits.role_state_bytes, 8192)

    def test_out_of_range_refused_at_load(self):
        text = self.file.read_text()
        path = self.root / "config/role-state-bad.toml"
        path.write_text(text + "\n[limits]\nrole_state_bytes = 64\n")
        from mizu.config import load
        with self.assertRaises(ConfigError):
            load(path)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
