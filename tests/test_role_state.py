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

    def test_externally_written_oversized_record_is_unavailable(self):
        from mizu.fs import canonical, write_json
        store = self.project.role_state
        state = {"pad": "x" * (store.max_bytes)}
        record = {"role": "searcher", "generation": 1,
                  "updated_at": "2026-01-01T00:00:00+00:00",
                  "updated_by_run": None, "state": state}
        self.assertGreater(len(canonical(record)), store.max_bytes)
        path = self.project.root / "role-state" / "searcher.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, record)
        seen = store.read("searcher")
        self.assertEqual(seen["status"], "unavailable")
        self.assertIsNone(seen["generation"])
        self.assertIsNone(seen["record"])
        with self.assertRaisesRegex(Denied, "unavailable"):
            store.replace("searcher", STATE, expected_generation=1)

    def test_persisted_boolean_generation_is_unavailable(self):
        from mizu.fs import write_json
        store = self.project.role_state
        record = {"role": "searcher", "generation": True,
                  "updated_at": "2026-01-01T00:00:00+00:00",
                  "updated_by_run": None, "state": {"ok": True}}
        path = self.project.root / "role-state" / "searcher.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, record)
        seen = store.read("searcher")
        self.assertEqual(seen["status"], "unavailable")
        self.assertIsNone(seen["generation"])
        with self.assertRaisesRegex(Denied, "unavailable"):
            store.replace("searcher", STATE, expected_generation=1)
        # A raw JSON boolean literal behaves the same without a writer.
        path.write_text('{"role":"searcher","generation":true,'
                        '"updated_at":"2026-01-01T00:00:00+00:00",'
                        '"updated_by_run":null,"state":{"ok":true}}')
        seen = store.read("searcher")
        self.assertEqual(seen["status"], "unavailable")

    def test_persisted_invalid_state_and_run_is_unavailable(self):
        from mizu.fs import write_json
        store = self.project.role_state
        path = self.project.root / "role-state" / "searcher.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        bad_states = [{"ok": "fine", "deep": {"a": {"b": {"c": {"d": 1}}}}},
                      {"x" * 65: 1},
                      {"ok": ["fine", ["nested", ["too", ["deep", ["here"]]]]]}]
        for bad in bad_states:
            record = {"role": "searcher", "generation": 1,
                      "updated_at": "2026-01-01T00:00:00+00:00",
                      "updated_by_run": None, "state": bad}
            write_json(path, record)
            seen = store.read("searcher")
            self.assertEqual(seen["status"], "unavailable", bad)
            self.assertIsNone(seen["generation"])
            with self.assertRaisesRegex(Denied, "unavailable"):
                store.replace("searcher", STATE, expected_generation=1)
        for bad_run in ("", "x" * 9000, "has\nnewline", 123):
            record = {"role": "searcher", "generation": 1,
                      "updated_at": "2026-01-01T00:00:00+00:00",
                      "updated_by_run": bad_run, "state": {"ok": True}}
            write_json(path, record)
            seen = store.read("searcher")
            self.assertEqual(seen["status"], "unavailable", repr(bad_run))
            with self.assertRaisesRegex(Denied, "unavailable"):
                store.replace("searcher", STATE, expected_generation=1)

    def test_persisted_envelope_must_match_writer_shape(self):
        from mizu.fs import write_json
        store = self.project.role_state
        path = self.project.root / "role-state" / "searcher.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        valid = {"role": "searcher", "generation": 1,
                 "updated_at": "2026-01-01T00:00:00+00:00",
                 "updated_by_run": None, "state": {"ok": True}}
        cases = [{k: v for k, v in valid.items() if k != missing} for missing in valid]
        cases += [dict(valid, extra=True)]
        for key, values in {
                "role": ("worker", None),
                "generation": (True, 0, -1, 1.0, "1", None),
                "state": (None, [], "state"),
                "updated_at": (None, 123, "", "not-a-date",
                               "2026-02-30T00:00:00+00:00",
                               "2026-01-01T00:00:00", "2026-01-01T00:00:00Z",
                               "2026-01-01T00:00:00.000000+00:00",
                               "2026-01-01T00:00:00+01:00",
                               "2026-01-01 00:00:00+00:00")}.items():
            cases.extend(dict(valid, **{key: value}) for value in values)
        for record in cases:
            with self.subTest(record=record):
                write_json(path, record)
                self.assertEqual(store.read("searcher"),
                                 {"status": "unavailable", "generation": None, "record": None})
        write_json(path, valid)
        self.assertEqual(store.read("searcher")["status"], "current")

    def test_read_failures_remain_unavailable_but_bad_role_is_denied(self):
        from unittest.mock import patch
        from mizu.errors import LimitExceeded
        for error in (Denied("unstable or invalid file"), LimitExceeded("oversize"),
                      PermissionError("denied"), ValueError("invalid JSON"),
                      RecursionError("deep JSON")):
            with self.subTest(error=error), patch("mizu.role_state.read_json", side_effect=error):
                self.assertEqual(self.project.role_state.read("searcher"),
                                 {"status": "unavailable", "generation": None, "record": None})
        with self.assertRaises(Denied):
            self.project.role_state.read("../bad")

    def test_exact_byte_bound_reads_current(self):
        from mizu.fs import canonical, write_json
        from mizu.role_state import RoleStateStore
        root = self.project.root / "role-state"
        root.mkdir(parents=True, exist_ok=True)
        state = {"ok": True}
        record = {"role": "searcher", "generation": 1,
                  "updated_at": "2026-01-01T00:00:00+00:00",
                  "updated_by_run": None, "state": state}
        size = len(canonical(record))
        path = root / "searcher.json"
        write_json(path, record)
        exact = RoleStateStore(root, max_bytes=size)
        seen = exact.read("searcher")
        self.assertEqual(seen["status"], "current")
        self.assertEqual(seen["generation"], 1)
        self.assertEqual(seen["record"]["state"], state)
        tight = RoleStateStore(root, max_bytes=size - 1)
        seen = tight.read("searcher")
        self.assertEqual(seen["status"], "unavailable")
        self.assertIsNone(seen["generation"])

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


class RoleStateSessionTests(Fixture):
    def capped(self, extra):
        import dataclasses as _dc
        role = self.config.roles["searcher"]
        return _dc.replace(role, capabilities=tuple(list(role.capabilities) + extra))

    def fresh_session(self, role):
        # A successive session: rebuilt project and context over the
        # same on-disk root with a new run dir, sharing nothing
        # in-memory with the earlier session.
        import uuid
        from mizu.fs import mkdir
        from mizu.project import Project
        from mizu.runtime import Context
        project = Project(self.config, "sample")
        run = project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        snap = project.snapshots.get()
        return Context(self.config, project, role, run, snap,
                       project.workspace)

    def test_successor_reads_without_rediscovery(self):
        # Session one records conclusions through the tool; session two
        # reads the same generation back through a rebuilt stack, so
        # concluded work is continued, not researched again.
        import dataclasses as _dc
        first = super().context("searcher")
        first.role = self.capped(["research"])
        first.handle("research", {"state": '{"questions": [], "conclusions": ["x"]}',
                                  "expected_generation": 0})
        role = _dc.replace(self.config.roles["searcher"],
                           capabilities=tuple(list(self.config.roles["searcher"].capabilities)
                                              + ["research_read"]))
        second = self.fresh_session(role)
        out = second.handle("research_read", {})
        self.assertEqual(out["status"], "current")
        self.assertEqual(out["generation"], 1)
        self.assertEqual(out["record"]["state"],
                         {"questions": [], "conclusions": ["x"]})

    def test_successor_prompt_carries_state_unsolicited(self):
        # Autonomous selection needs no operator pointer: the fresh
        # session's prompt already holds the current record, and the
        # resumed-session delta carries it constant-size too.
        import json as _json
        self.project.role_state.replace("searcher", STATE,
                                        expected_generation=0)
        second = self.fresh_session(self.capped(["research_read"]))
        from mizu.runtime import prompt_for, prompt_delta_for
        full = _json.loads(prompt_for(second))
        self.assertEqual(full["research_state"]["status"], "current")
        self.assertEqual(full["research_state"]["generation"], 1)
        self.assertEqual(full["research_state"]["state"], STATE)
        delta = _json.loads(prompt_delta_for(second, []))
        self.assertEqual(delta["research_state"]["status"], "current")
        self.assertEqual(delta["research_state"]["generation"], 1)

    def test_successor_reopens_and_stale_first_session_loses(self):
        # Reopening: the successor merges new evidence onto the read
        # generation; the earlier session's generation is stale now and
        # its overwrite is refused instead of clobbering the reopen.
        self.project.role_state.replace("searcher", STATE,
                                        expected_generation=0)
        second = self.fresh_session(self.capped(["research", "research_read"]))
        seen = second.handle("research_read", {})
        merged = dict(STATE)
        merged["conclusions"] = ["reopened with fresh evidence"]
        import json as _json2
        second.handle("research", {"state": _json2.dumps(merged),
                                   "expected_generation": seen["generation"]})
        with self.assertRaises(Denied):
            self.project.role_state.replace("searcher", STATE,
                                            expected_generation=1)
        third = self.fresh_session(self.capped(["research_read"]))
        out = third.handle("research_read", {})
        self.assertEqual(out["generation"], 2)
        self.assertEqual(out["record"]["state"], merged)

    def test_successor_prompt_stays_bounded(self):
        # A record over the prompt bound injects truncated metadata in
        # both full and delta prompts: generation and sizes travel,
        # content never does.
        import json as _json
        big = {"pad": "x" * 5000}
        self.project.role_state.replace("searcher", big, expected_generation=0)
        import dataclasses as _dc2
        config = _dc2.replace(
            self.config,
            limits=_dc2.replace(self.config.limits,
                                role_state_prompt_bytes=64))
        # A fresh session under tightened bounds: same disk, new stack.
        real_config, self.config = self.config, config
        try:
            second = self.fresh_session(self.capped(["research_read"]))
        finally:
            self.config = real_config
        from mizu.runtime import prompt_for, prompt_delta_for
        for prompt in (prompt_for(second), prompt_delta_for(second, [])):
            parsed = _json.loads(prompt)["research_state"]
            self.assertEqual(parsed["status"], "truncated")
            self.assertEqual(parsed["generation"], 1)
            self.assertNotIn("state", parsed)
            self.assertLessEqual(len(prompt.encode()), 10000)


class RoleStateCommitmentTests(Fixture):
    def test_unverified_content_never_becomes_memory(self):
        # Recall is not commitment: reading own (absent) state,
        # serving external-untrusted proposals, and rendering prompts
        # leave the store untouched; only an explicit replacement
        # behind a generation commits to memory.
        import dataclasses as _dc
        import sys as _sys
        import uuid as _uuid
        from mizu.fs import mkdir as _mkdir
        from mizu.runtime import Context as _Context
        from mizu.runtime import prompt_for as _prompt_for
        adapter = {"command": [_sys.executable, "-c",
                               "import sys,json; json.load(sys.stdin); "
                               "print(json.dumps({'proposals': [], "
                               "'complete': True}))"],
                   "timeout_seconds": 5, "max_bytes": 524288}
        config = _dc.replace(self.config, vcs=adapter)
        role = config.roles["searcher"]
        role = _dc.replace(role, capabilities=tuple(
            list(role.capabilities) + ["research_read", "vcs_read",
                                       "research"]))
        run = self.project.root / "runs" / _uuid.uuid4().hex
        _mkdir(run)
        ctx = _Context(config, self.project, role, run,
                       self.project.snapshots.get(),
                       self.project.workspace)
        ctx.handle("research_read", {})
        out = ctx.handle("vcs_read", {"op": "proposals"})
        self.assertTrue(out["result"]["complete"])
        _prompt_for(ctx)
        self.assertEqual(
            self.project.role_state.read("searcher")["status"], "absent")
        ctx.handle("research", {"state": '{"conclusions": ["committed"]}',
                                "expected_generation": 0})
        self.assertEqual(
            self.project.role_state.read("searcher")["generation"], 1)
