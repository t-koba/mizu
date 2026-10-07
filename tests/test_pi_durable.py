"""Selectable pi-durable execution: distinct engine, durable recovery, replay safety."""
import unittest

from support import Fixture
from mizu import pi_durable
from mizu.drivers import driver_for
from mizu.engine_config import effective
from mizu.errors import ConfigError, LimitExceeded, ModelFailure
from mizu.engine_config import rotate_session, rotation_due, session_generation
from mizu.pi_durable_store import (begin_run, check_grant, complete_run, complete_turn,
                                   grant_digest, load_run, open_store, project_context,
                                   prune, prune_generations, record_turn, retention_candidates,
                                   store_path_for)


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


class SessionIsolationTests(Fixture):
    def _durable_profile(self, **options):
        settings = dict(self.config.profiles["primary"])
        settings["engine"] = "pi-durable"
        merged = dict(settings.get("options", {}))
        merged.update(options)
        settings["options"] = merged
        self.config.profiles["durable"] = settings
        return "durable"

    def _succeed(self, driver, tokens=15):
        usage = {"input_tokens": tokens, "output_tokens": 0,
                 "cache_read_tokens": 0, "cache_write_tokens": 0}
        driver._run_once = lambda *a, **k: {"engine": "pi-durable", "requests": 1,
                                            "usage": [dict(usage)]}

    def _ephemeral(self):
        context = self.context()
        context.ephemeral = True
        return context

    def test_two_ephemeral_runs_use_distinct_stores(self):
        profile = self._durable_profile()
        driver = pi_durable.PiDurableDriver(self.config)
        self._succeed(driver)
        first_ctx, second_ctx = self._ephemeral(), self._ephemeral()
        first = driver.execute(first_ctx, "one", profile=profile)
        second = driver.execute(second_ctx, "two", profile=profile)
        self.assertNotEqual(first["durable"]["store"], second["durable"]["store"])
        # Each actual store holds only its own conversation: the other run
        # key resolves in neither store.
        from pathlib import Path
        for own, own_ctx, other_ctx in ((first, first_ctx, second_ctx),
                                        (second, second_ctx, first_ctx)):
            conn = open_store(Path(own["durable"]["store"]))
            try:
                self.assertIsNotNone(load_run(conn, own_ctx.run_dir.name))
                self.assertIsNone(load_run(conn, other_ctx.run_dir.name))
            finally:
                conn.close()

    def test_ephemeral_retry_resumes_same_store(self):
        profile = self._durable_profile()
        driver = pi_durable.PiDurableDriver(self.config)
        context = self._ephemeral()
        def boom(*args, **kwargs):
            raise ModelFailure("pi-durable", kind="error", message="boom")
        driver._run_once = boom
        with self.assertRaises(ModelFailure):
            driver.execute(context, "hello", profile=profile)
        from pathlib import Path
        self._succeed(driver)
        out = driver.execute(context, "hello", profile=profile)
        self.assertTrue(out["durable"]["resumed"])
        conn = open_store(Path(out["durable"]["store"]))
        try:
            record = load_run(conn, context.run_dir.name)
        finally:
            conn.close()
        # Crash recovery kept the same conversation: one run row, now
        # completed, holding a prompt turn from each attempt.
        self.assertEqual(record["status"], "completed")
        self.assertEqual(len(record["turns"]), 2)

    def test_token_rotation_starts_a_fresh_conversation(self):
        import json
        import types
        profile = self._durable_profile()
        self.config.profiles[profile]["session"] = "persistent"
        role = self.config.roles["worker"]
        driver = pi_durable.PiDurableDriver(self.config)
        self._succeed(driver, tokens=15)
        first = driver.execute(self.context(), "one", profile=profile)
        session_file = next((self.project.root / "sessions" / role.name).rglob("session.json"))
        saved = json.loads(session_file.read_text())
        limits = types.SimpleNamespace(session_max_tokens=1, session_max_cost_usd=0,
                                       session_max_age_seconds=0)
        due, reason = rotation_due(limits, session_file, saved, "pi-durable")
        self.assertTrue(due, reason)
        rotate_session(session_file, self.project.root / "runs" / "rot", reason)
        self.assertEqual(session_generation(session_file.parent), 1)
        out = driver.execute(self.context(), "two", profile=profile)
        self.assertNotEqual(out["durable"]["store"], first["durable"]["store"])
        self.assertTrue(out["durable"]["store"].endswith("-g1/store.sqlite"))
        # The abandoned store is retired, and the fresh store holds no
        # trace of the previous conversation.
        from pathlib import Path
        self.assertFalse(Path(first["durable"]["store"]).exists())
        self.assertEqual(out["durable"]["pruned_generations"],
                         [Path(first["durable"]["store"]).parent.name])
        conn = open_store(Path(out["durable"]["store"]))
        try:
            rows = conn.execute("SELECT run_key FROM durable_runs").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        fresh = json.loads(session_file.read_text())
        self.assertEqual(fresh["usage"], {"tokens": 15})

    def test_persistent_retry_keeps_crash_recovery_without_rotation(self):
        profile = self._durable_profile()
        self.config.profiles[profile]["session"] = "persistent"
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        def boom(*args, **kwargs):
            raise ModelFailure("pi-durable", kind="error", message="boom")
        driver._run_once = boom
        with self.assertRaises(ModelFailure):
            driver.execute(context, "hello", profile=profile)
        self._succeed(driver)
        out = driver.execute(context, "hello", profile=profile)
        self.assertTrue(out["durable"]["resumed"])
        self.assertEqual(out["durable"]["pruned_generations"], [])
        from pathlib import Path
        conn = open_store(Path(out["durable"]["store"]))
        try:
            record = load_run(conn, context.run_dir.name)
        finally:
            conn.close()
        self.assertEqual(record["status"], "completed")
        self.assertEqual(len(record["turns"]), 2)

    def test_corrupt_generation_refuses_instead_of_resuming(self):
        profile = self._durable_profile()
        self.config.profiles[profile]["session"] = "persistent"
        role = self.config.roles["worker"]
        driver = pi_durable.PiDurableDriver(self.config)
        self._succeed(driver)
        driver.execute(self.context(), "one", profile=profile)
        session_dir = next((self.project.root / "sessions" / role.name).rglob("session.json")).parent
        (session_dir / "generation.json").write_text("{corrupt")
        with self.assertRaises(ConfigError):
            driver.execute(self.context(), "two", profile=profile)


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


class PruneGenerationsTests(unittest.TestCase):
    def _role_dir(self, root, project="proj", role="worker"):
        from pathlib import Path
        role_dir = Path(root) / "durable" / project / role
        role_dir.mkdir(parents=True)
        return role_dir

    def _grant(self):
        return grant_digest(policy_text="p", capabilities=("a",),
                            model={"provider": "x", "model": "y"},
                            adapter_digest_value="d", options_digest="o")

    def _terminal_store(self, directory):
        from pathlib import Path
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        conn = open_store(directory / "store.sqlite")
        try:
            begin_run(conn, run_key="r1", session_key="s", project="p",
                      role="r", grant=self._grant())
            record_turn(conn, "r1", 1, "prompt", {"text": "hi"})
            complete_turn(conn, "r1", 1, {"ok": True})
            complete_run(conn, "r1", {"engine": "pi-durable"})
        finally:
            conn.close()

    def test_prune_removes_only_terminal_older_generations(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            role_dir = self._role_dir(td)
            for name in ("aaa", "aaa-g1"):
                self._terminal_store(role_dir / name)
            for name in ("aaa-g2", "bbb", "aaa-gX", "aa", "aaa-g2-extra"):
                (role_dir / name).mkdir()
            removed = prune_generations(td, project="proj", role="worker",
                                        base_key="aaa", generation=2)
            self.assertEqual(removed, ["aaa", "aaa-g1"])
            remaining = sorted(p.name for p in role_dir.iterdir())
            self.assertEqual(remaining, ["aa", "aaa-g2", "aaa-g2-extra", "aaa-gX", "bbb"])

    def test_prune_retains_unresolved_recovery_evidence(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            role_dir = self._role_dir(td)
            # Active run: recovery evidence still needed.
            active = role_dir / "aaa"
            active.mkdir()
            conn = open_store(active / "store.sqlite")
            begin_run(conn, run_key="r1", session_key="s", project="p",
                      role="r", grant=self._grant())
            conn.close()
            # Unknown-completion turn: side effects unaccounted.
            unknown = role_dir / "aaa-g1"
            unknown.mkdir()
            conn = open_store(unknown / "store.sqlite")
            begin_run(conn, run_key="r2", session_key="s", project="p",
                      role="r", grant=self._grant())
            record_turn(conn, "r2", 1, "prompt", {"text": "hi"})
            complete_run(conn, "r2", {"engine": "pi-durable"})
            conn.execute("UPDATE durable_turns SET state='unknown' WHERE run_key='r2'")
            conn.commit()
            conn.close()
            # Corrupt and missing stores cannot be verified: retained.
            corrupt = role_dir / "aaa-g2"
            corrupt.mkdir()
            (corrupt / "store.sqlite").write_bytes(b"not a database")
            missing = role_dir / "aaa-g3"
            missing.mkdir()
            removed = prune_generations(td, project="proj", role="worker",
                                        base_key="aaa", generation=9)
            self.assertEqual(removed, [])
            remaining = sorted(p.name for p in role_dir.iterdir())
            self.assertEqual(remaining, ["aaa", "aaa-g1", "aaa-g2", "aaa-g3"])

    def test_prune_retains_unexpected_run_and_turn_states(self):
        import tempfile
        from pathlib import Path
        from mizu.pi_durable_store import generation_terminal
        with tempfile.TemporaryDirectory() as td:
            role_dir = self._role_dir(td)
            # A corrupt edit or future run status is not verifiable.
            bogus_run = role_dir / "aaa"
            bogus_run.mkdir()
            conn = open_store(bogus_run / "store.sqlite")
            conn.execute("INSERT INTO durable_runs(run_key, session_key, project, role, grant_digest, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                         ("r1", "s", "p", "r", "g", "bogus", 0, 0))
            conn.commit()
            conn.close()
            self.assertFalse(generation_terminal(bogus_run))
            # A non-enumerated turn state counts as unresolved evidence.
            bogus_turn = role_dir / "aaa-g1"
            bogus_turn.mkdir()
            conn = open_store(bogus_turn / "store.sqlite")
            begin_run(conn, run_key="r2", session_key="s", project="p",
                      role="r", grant=self._grant())
            record_turn(conn, "r2", 1, "prompt", {"text": "hi"})
            complete_turn(conn, "r2", 1, {"ok": True})
            complete_run(conn, "r2", {"engine": "pi-durable"})
            conn.execute("UPDATE durable_turns SET state='bogus' WHERE run_key='r2'")
            conn.commit()
            conn.close()
            self.assertFalse(generation_terminal(bogus_turn))
            removed = prune_generations(td, project="proj", role="worker",
                                        base_key="aaa", generation=9)
            self.assertEqual(removed, [])
            self.assertTrue(bogus_run.is_dir())
            self.assertTrue(bogus_turn.is_dir())

    def test_prune_never_touches_files_or_symlinks(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            role_dir = self._role_dir(td)
            (role_dir / "aaa-g1").write_text("stray")
            real = role_dir / "real"
            self._terminal_store(real)
            link = role_dir / "aaa-g2"
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            removed = prune_generations(td, project="proj", role="worker",
                                        base_key="aaa", generation=9)
            self.assertEqual(removed, [])
            self.assertTrue((role_dir / "aaa-g1").is_file())
            self.assertTrue(link.is_symlink())

    def test_prune_missing_role_dir_is_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(prune_generations(td, project="proj", role="worker",
                                               base_key="aaa", generation=1), [])

    def test_prune_rejects_bad_generation(self):
        from mizu.errors import ConfigError
        for bad in (0, -1, "2", None):
            with self.assertRaises(ConfigError):
                prune_generations("/data", project="p", role="r",
                                  base_key="aaa", generation=bad)


class RotationTransitionTests(unittest.TestCase):
    def _session_dir(self, root):
        import json
        from pathlib import Path
        directory = Path(root) / "sess"
        directory.mkdir(parents=True)
        (directory / "session.json").write_text(json.dumps(
            {"engine": "pi", "session_id": "s", "totals": {}}))
        run = Path(root) / "run"
        run.mkdir()
        return directory, run

    def _fail_on(self, name):
        from pathlib import Path
        from mizu import fs as fs_mod
        real = fs_mod.write_json

        def guarded(path, *args, **kwargs):
            if Path(path).name == name:
                raise OSError(f"injected {name} failure")
            return real(path, *args, **kwargs)
        return guarded

    def test_failed_generation_bump_changes_nothing(self):
        import tempfile
        from unittest import mock
        from mizu import fs as fs_mod
        with tempfile.TemporaryDirectory() as td:
            directory, run = self._session_dir(td)
            with mock.patch.object(fs_mod, "write_json",
                                   side_effect=self._fail_on("generation.json")):
                with self.assertRaises(OSError):
                    rotate_session(directory / "session.json", run, "test")
            # Nothing moved: the next dispatch retries the due rotation
            # instead of silently resuming or half-rotating.
            self.assertTrue((directory / "session.json").is_file())
            self.assertFalse((directory / "generation.json").exists())
            self.assertFalse((run / "rotation.json").exists())
            self.assertEqual(session_generation(directory), 0)

    def test_post_bump_failure_still_points_at_fresh_generation(self):
        import tempfile
        from unittest import mock
        from mizu import fs as fs_mod
        with tempfile.TemporaryDirectory() as td:
            directory, run = self._session_dir(td)
            with mock.patch.object(fs_mod, "write_json",
                                   side_effect=self._fail_on("rotation.json")):
                with self.assertRaises(OSError):
                    rotate_session(directory / "session.json", run, "test")
            # The bump already landed: the next dispatch mints its fresh
            # conversation under the new generation, never the prior one.
            self.assertEqual(session_generation(directory), 1)
            self.assertFalse((directory / "session.json").exists())


if __name__ == "__main__":
    unittest.main()
