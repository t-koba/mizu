"""Selectable pi-durable execution: distinct engine, durable recovery, replay safety."""
import unittest

from support import Fixture
from mizu import pi_durable
from mizu.drivers import driver_for
from mizu.engine_config import effective
from mizu.errors import ConfigError, LimitExceeded, ModelFailure
from mizu.pi_durable_store import (begin_run, check_grant, complete_run, complete_turn,
                                   grant_digest, load_run, open_store, project_context,
                                   prune, record_turn, retention_candidates, store_path_for)


class SelectionTests(Fixture):
    def test_distinct_engine_preserves_pi(self):
        self.assertEqual(self.config.profiles["primary"]["engine"], "pi")
        settings = dict(self.config.profiles["primary"])
        settings["engine"] = "pi-durable"
        self.config.profiles["durable"] = settings
        self.assertEqual(effective(self.config, self.config.roles["worker"], "durable")["engine"], "pi-durable")
        self.assertEqual(effective(self.config, self.config.roles["worker"], "primary")["engine"], "pi")
        self.assertIsInstance(driver_for(self.config, "durable", {}), pi_durable.PiDurableDriver)

    def test_unsupported_backend_refused(self):
        with self.assertRaises(ConfigError):
            pi_durable.durable_options({"options": {"durable_backend": "redis"}})

    def test_unknown_resume_refused(self):
        with self.assertRaises(ConfigError):
            pi_durable.durable_options({"options": {"durable_resume": "always"}})


class PolicyTests(Fixture):
    def _durable_profile(self, **options):
        settings = dict(self.config.profiles["primary"])
        settings["engine"] = "pi-durable"
        merged = dict(settings.get("options", {}))
        merged.update(options)
        settings["options"] = merged
        self.config.profiles["durable"] = settings
        return "durable"

    def test_grant_binds_durable_policy(self):
        role = self.config.roles["worker"]
        model = {"provider": "p", "model": "m"}
        base = pi_durable.grant_for({"options": {}}, role, model)
        for key, value in (("durable_backend", "sqlite"), ("durable_resume", "fresh"),
                           ("durable_retention_days", 30), ("durable_max_turns", 7)):
            changed = pi_durable.grant_for({"options": {key: value}}, role, model)
            self.assertNotEqual(base, changed, key)

    def test_pi_rejects_durable_options(self):
        settings = dict(self.config.profiles["primary"])
        merged = dict(settings.get("options", {}))
        merged["durable_resume"] = "fresh"
        settings["options"] = merged
        self.config.profiles["bad"] = settings
        with self.assertRaises(ConfigError):
            effective(self.config, self.config.roles["worker"], "bad")

    def test_pi_durable_rejects_pi_sdk_knobs(self):
        profile = self._durable_profile(codemode=True)
        with self.assertRaises(ConfigError):
            effective(self.config, self.config.roles["worker"], profile)

    def test_turn_bound_enforced(self):
        profile = self._durable_profile(durable_max_turns=2)
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        seen = {}
        def boom(*args, **kwargs):
            seen.update(kwargs)
            raise ModelFailure("pi-durable", kind="error", message="boom")
        driver._run_once = boom
        with self.assertRaises(ModelFailure):
            driver.execute(context, "hello", profile=profile)
        # The shared budget subtracts the just-recorded prompt turn: with two
        # allowed and nothing spent, one admission remains for the launcher.
        self.assertEqual(seen.get("remaining"), 1)
        # The interrupted attempt left persisted turns; the next submission
        # records its prompt and finds nothing remains, so it is refused
        # without dispatch instead of retried past the grant.
        dispatched = {}
        def never(*args, **kwargs):
            dispatched["called"] = True
            raise ModelFailure("pi-durable", kind="error", message="must not dispatch")
        driver._run_once = never
        with self.assertRaises(LimitExceeded):
            driver.execute(context, "hello", profile=profile)
        self.assertNotIn("called", dispatched)

    def test_exhausted_budget_refuses_without_dispatch(self):
        profile = self._durable_profile(durable_max_turns=1)
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        called = {}
        def never(*args, **kwargs):
            called["ran"] = True
            raise AssertionError("must not dispatch past the grant")
        driver._run_once = never
        with self.assertRaises(LimitExceeded):
            driver.execute(context, "hello", profile=profile)
        self.assertNotIn("ran", called)

    def test_turn_bound_result_maps_to_limit(self):
        from mizu.errors import ProtocolError
        context = self.context()
        context.handle("_hello", {})
        context.handle("_budget", {"sequence": 1})
        context.handle("finish", {"outcome": "wait", "summary": "s", "state": "n"})
        context.model_evidence = {"requests": 0, "usage": [], "usage_known": False}
        model = {"provider": "p", "model": "m"}
        with self.assertRaises(LimitExceeded):
            pi_durable.interpret_result({"durable_result": True, "status": "turn_bound",
                                         "reason": "spent", "requests": 5,
                                         "usage": {"input": 10, "output": 5}}, context, model, None)
        # The bound-exhausted path keeps launcher accounting, like the done path.
        self.assertEqual(context.model_evidence["requests"], 5)
        self.assertTrue(context.model_evidence["usage_known"])
        self.assertEqual(context.model_evidence["usage"][0]["input_tokens"], 10)
        self.assertEqual(context.model_evidence["usage"][0]["output_tokens"], 5)
        with self.assertRaises(ProtocolError):
            pi_durable.interpret_result({"durable_result": True, "status": "done",
                                         "finish_called": False}, context, model, None)
        pi_durable.interpret_result({"durable_result": True, "status": "done",
                                     "finish_called": True, "requests": 2,
                                     "usage": {"input": 3, "output": 4}},
                                    context, model, None)
        self.assertTrue(context.model_evidence["usage_known"])
        self.assertEqual(context.model_evidence["usage"][0]["input_tokens"], 3)

    def test_persistent_session_saves_cumulative_tokens(self):
        profile = self._durable_profile()
        role = self.config.roles["worker"]
        self.config.profiles[profile]["session"] = "persistent"
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        usage = {"input_tokens": 10, "output_tokens": 5,
                 "cache_read_tokens": 0, "cache_write_tokens": 0}
        driver._run_once = lambda *a, **k: {"engine": "pi-durable", "requests": 1,
                                            "usage": [dict(usage)]}
        out = driver.execute(context, "hello", profile=profile)
        self.assertEqual(out["requests"], 1)
        parallel = context.run_dir.parents[1] / "sessions" / role.name
        saved = [p for p in parallel.rglob("session.json")]
        self.assertEqual(len(saved), 1)
        import json
        record = json.loads(saved[0].read_text())
        self.assertEqual(record["usage"], {"tokens": 15})
        # A second run carries the saved total forward.
        context2 = self.context()
        driver.execute(context2, "again", profile=profile)
        record2 = json.loads(saved[0].read_text())
        self.assertEqual(record2["usage"], {"tokens": 30})


class StoreTests(unittest.TestCase):
    def test_isolation_by_project_role_session(self):
        a = store_path_for("/data", "proj", "worker", "aaa")
        b = store_path_for("/data", "proj", "worker", "bbb")
        c = store_path_for("/data", "proj", "other", "aaa")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a.parent.parts[-4:], ("durable", "proj", "worker", "aaa"))
        self.assertEqual(a.name, "store.sqlite")

    def test_real_persistence_recovery_and_replay_safety(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "store.sqlite"
            conn = open_store(path)
            grant = grant_digest(policy_text="p", capabilities=("a",), model={"provider": "x", "model": "y"},
                                 adapter_digest_value="d", options_digest="o")
            first = begin_run(conn, run_key="r1", session_key="s", project="p", role="r", grant=grant)
            self.assertFalse(first["resumed"])
            record_turn(conn, "r1", 1, "prompt", {"text": "hello"})
            dup = record_turn(conn, "r1", 1, "prompt", {"text": "hello"})
            self.assertTrue(dup["duplicate"])
            complete_turn(conn, "r1", 1, {"ok": True})
            complete_run(conn, "r1", {"engine": "pi-durable"})
            conn.close()
            # Crash recovery: reopen, reload, resume with same grant.
            conn = open_store(path)
            record = load_run(conn, "r1")
            self.assertEqual(record["status"], "completed")
            self.assertEqual(len(record["turns"]), 1)
            resumed = begin_run(conn, run_key="r1", session_key="s", project="p", role="r", grant=grant)
            self.assertTrue(resumed["resumed"])
            self.assertIsNotNone(resumed["result"])
            projection = project_context(conn, "r1")
            self.assertEqual(projection["turns"], 1)
            conn.close()

    def test_path_alone_never_authorizes_resume(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            conn = open_store(Path(td) / "s.sqlite")
            grant = grant_digest(policy_text="p", capabilities=("a",), model={"provider": "x", "model": "y"},
                                 adapter_digest_value="d", options_digest="o")
            begin_run(conn, run_key="r", session_key="s", project="p", role="r", grant=grant)
            with self.assertRaises(ConfigError):
                check_grant(conn, "r", "different-grant")
            with self.assertRaises(ConfigError):
                begin_run(conn, run_key="r", session_key="s", project="p", role="r", grant="different-grant")
            conn.close()

    def test_retention_keeps_unfinished_and_unknown(self):
        import tempfile, time
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            conn = open_store(Path(td) / "s.sqlite")
            old = time.time() - 90 * 86400
            for key, status in (("done", "completed"), ("active", "active"), ("broken", "completed")):
                conn.execute("INSERT INTO durable_runs(run_key, session_key, project, role, grant_digest, status, created_at, updated_at, result_json) VALUES(?,?,?,?,?,?,?,?,?)",
                             (key, "s", "p", "r", "g", status, old, old, '{"x":1}' if status != "active" else None))
            conn.execute("INSERT INTO durable_turns(run_key, seq, kind, payload_json, state) VALUES(?,?,?,?,?)",
                         ("broken", 1, "prompt", '{"a":1}', "unknown"))
            conn.commit()
            candidates = retention_candidates(conn, retention_days=31, now=time.time())
            self.assertEqual(candidates, ["done"])
            preview = prune(conn, candidates, dry_run=True)
            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["candidates"], ["done"])
            applied = prune(conn, candidates, dry_run=False)
            self.assertEqual(applied["removed"], ["done"])
            self.assertIsNone(load_run(conn, "done"))
            self.assertIsNotNone(load_run(conn, "active"))
            self.assertIsNotNone(load_run(conn, "broken"))
            conn.close()


if __name__ == "__main__":
    unittest.main()
