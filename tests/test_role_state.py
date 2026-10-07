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


class ResearchToolTests(Fixture):
    def setUp(self):
        super().setUp()
        import dataclasses as _dc
        self._dc = _dc

    def capped(self, extra):
        role = self.config.roles["searcher"]
        return self._dc.replace(role, capabilities=tuple(list(role.capabilities) + extra))

    def context(self, role):
        ctx = super().context("searcher")
        ctx.role = role
        return ctx

    def test_read_serves_own_record(self):
        self.project.role_state.replace("searcher", STATE,
                                        expected_generation=0)
        ctx = self.context(self.capped(["research_read"]))
        out = ctx.handle("research_read", {})
        self.assertEqual(out["role"], "searcher")
        self.assertEqual(out["status"], "current")
        self.assertEqual(out["generation"], 1)
        self.assertEqual(out["record"]["state"], STATE)

    def test_read_reports_absent(self):
        ctx = self.context(self.capped(["research_read"]))
        out = ctx.handle("research_read", {})
        self.assertEqual(out["status"], "absent")

    def test_replace_writes_receipt_and_store(self):
        import json as _json
        ctx = self.context(self.capped(["research"]))
        out = ctx.handle("research", {"state": _json.dumps(STATE),
                                      "expected_generation": 0})
        self.assertEqual(out["generation"], 1)
        self.assertEqual(out["evidence"], "research-state.json")
        receipt = _json.loads((ctx.run_dir / "research-state.json").read_text())
        self.assertEqual(receipt["state"], STATE)
        self.assertEqual(receipt["generation"], 1)
        seen = self.project.role_state.read("searcher")
        self.assertEqual(seen["generation"], 1)

    def test_replace_stale_writes_no_receipt(self):
        import json as _json
        self.project.role_state.replace("searcher", STATE,
                                        expected_generation=0)
        ctx = self.context(self.capped(["research"]))
        with self.assertRaisesRegex(Denied, "Stale"):
            ctx.handle("research", {"state": _json.dumps({"other": True}),
                                    "expected_generation": 0})
        self.assertFalse((ctx.run_dir / "research-state.json").exists())

    def test_replace_rejects_non_json(self):
        ctx = self.context(self.capped(["research"]))
        with self.assertRaisesRegex(Denied, "JSON"):
            ctx.handle("research", {"state": "{not json",
                                    "expected_generation": 0})

    def test_replace_rejects_non_finite_constants(self):
        # NaN/Infinity parse must Deny (never leak ValueError) and
        # leave the stored record untouched.
        ctx = self.context(self.capped(["research"]))
        for payload in ('{"x": NaN}', '{"x": Infinity}',
                        '{"x": -Infinity}'):
            with self.assertRaises(Denied):
                ctx.handle("research", {"state": payload,
                                        "expected_generation": 0})
        self.assertEqual(
            self.project.role_state.read("searcher")["status"], "absent")

    def test_replace_rejects_missing_generation(self):
        import json as _json
        ctx = self.context(self.capped(["research"]))
        with self.assertRaises(Denied):
            ctx.handle("research", {"state": _json.dumps(STATE)})

    def test_capability_gate(self):
        ctx = self.context(self.capped([]))
        with self.assertRaises(Denied):
            ctx.handle("research_read", {})
        with self.assertRaises(Denied):
            ctx.handle("research", {"state": "{}", "expected_generation": 0})

    def test_consult_forbids_replace_but_allows_read(self):
        from types import SimpleNamespace as _Role
        from mizu.runtime import check_consult_role
        from mizu.errors import ConfigError
        with self.assertRaises(ConfigError):
            check_consult_role("c", _Role(workspace="read",
                                          capabilities=("finish", "research")))
        check_consult_role("c", _Role(workspace="read",
                                      capabilities=("finish", "research_read")))


class ResearchPromptTests(Fixture):
    def capped_context(self, extra):
        import dataclasses as _dc
        role = self.config.roles["searcher"]
        ctx = super().context("searcher")
        ctx.role = _dc.replace(role, capabilities=tuple(list(role.capabilities) + extra))
        return ctx

    def test_prompt_injects_current_state(self):
        import json as _json
        self.project.role_state.replace("searcher", STATE,
                                        expected_generation=0)
        from mizu.runtime import prompt_for, prompt_delta_for
        for prompt in (prompt_for(self.capped_context(["research_read"])),
                       prompt_delta_for(self.capped_context(["research_read"]), [])):
            block = _json.loads(prompt)["research_state"]
            self.assertEqual(block["status"], "current")
            self.assertEqual(block["generation"], 1)
            self.assertEqual(block["state"], STATE)

    def test_prompt_reports_absent_not_empty(self):
        import json as _json
        from mizu.runtime import prompt_for
        block = _json.loads(prompt_for(self.capped_context(["research_read"])))["research_state"]
        self.assertEqual(block, {"status": "absent"})

    def test_prompt_omits_without_capability(self):
        import json as _json
        from mizu.runtime import prompt_for
        self.assertIsNone(_json.loads(prompt_for(self.capped_context([])))["research_state"])

    def test_prompt_truncates_without_partial_content(self):
        import json as _json
        big = {"pad": "x" * 5000}
        self.project.role_state.replace("searcher", big, expected_generation=0)
        from mizu.runtime import prompt_for
        block = _json.loads(prompt_for(self.capped_context(["research_read"])))["research_state"]
        self.assertEqual(block["status"], "truncated")
        self.assertEqual(block["generation"], 1)
        self.assertGreater(block["state_bytes"], block["prompt_bytes"])
        self.assertNotIn("state", block)
        self.assertNotIn("pad", _json.dumps(block))


class RoleStateRaceTests(Fixture):
    def test_overlapping_writers_share_no_generation(self):
        import threading
        store = self.project.role_state
        store.replace("searcher", {"seed": True}, expected_generation=0)
        barrier = threading.Barrier(2, timeout=30)
        outcomes = []

        def writer(value):
            try:
                seen = store.read("searcher")
                barrier.wait(timeout=30)
                store.replace("searcher", {"by": value},
                              expected_generation=seen["generation"])
                outcomes.append(("ok", value))
            except Denied as exc:
                outcomes.append(("denied", str(exc)))
            except Exception as exc:  # noqa: BLE001 - surfaced below
                outcomes.append(("leak", f"{type(exc).__name__}: {exc}"))

        threads = [threading.Thread(target=writer, args=(name,))
                   for name in ("first", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sorted(kind for kind, _ in outcomes),
                         ["denied", "ok"], outcomes)
        seen = store.read("searcher")
        self.assertEqual(seen["generation"], 2)
        self.assertIn(seen["record"]["state"], ({"by": "first"},
                                                {"by": "second"}))

    def test_non_finite_floats_refused(self):
        store = self.project.role_state
        store.replace("searcher", STATE, expected_generation=0)
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaisesRegex(Denied, "finite"):
                store.replace("searcher", {"x": bad}, expected_generation=1)
            with self.assertRaisesRegex(Denied, "finite"):
                store.replace("searcher", {"x": [bad]}, expected_generation=1)
        self.assertEqual(store.read("searcher")["record"]["state"], STATE)

    def test_bad_run_refused_and_good_run_recorded(self):
        store = self.project.role_state
        for bad in (object(), "", "x" * 9000, "has\nnewline", 123):
            with self.assertRaises(Denied):
                store.replace("searcher", STATE, expected_generation=0,
                              run=bad)
        self.assertEqual(store.read("searcher")["status"], "absent")
        store.replace("searcher", STATE, expected_generation=0, run="run-9")
        self.assertEqual(store.read("searcher")["record"]["updated_by_run"],
                         "run-9")

    def test_envelope_counts_against_bound(self):
        from mizu.fs import canonical
        from mizu.role_state import RoleStateStore
        store = RoleStateStore(self.project.root / "role-state",
                               max_bytes=512)
        state = {"pad": "x" * 420}
        self.assertLessEqual(len(canonical(state)), 512)
        with self.assertRaisesRegex(Denied, "byte bound"):
            store.replace("searcher", state, expected_generation=0)
        self.assertEqual(store.read("searcher")["status"], "absent")

    def test_bool_generation_refused(self):
        store = self.project.role_state
        with self.assertRaisesRegex(Denied, "generation"):
            store.replace("searcher", STATE, expected_generation=True)
        self.assertEqual(store.read("searcher")["status"], "absent")
