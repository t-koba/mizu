"""Pi cacheRetention profile option: allowlist, validation, live semantics."""
import json
import unittest

from support import Fixture
from mizu import pi_durable
from mizu.engine_config import (conversation_settings, durable_session_identity,
                                effective)
from mizu.errors import ConfigError


class CacheRetentionTests(Fixture):
    def _profile(self, engine, **options):
        settings = dict(self.config.profiles["primary"])
        settings["engine"] = engine
        merged = dict(settings.get("options", {}))
        merged.update(options)
        settings["options"] = merged
        name = f"cr-{engine}"
        self.config.profiles[name] = settings
        return name

    def test_pi_and_durable_accept_three_values(self):
        for engine in ("pi", "pi-durable"):
            for value in ("none", "short", "long"):
                profile = self._profile(engine, cacheRetention=value)
                settings = effective(self.config, self.config.roles["worker"], profile)
                self.assertEqual(settings["options"]["cacheRetention"], value)

    def test_invalid_value_refused(self):
        for engine in ("pi", "pi-durable"):
            for bad in ("LONG", "long ", "", "medium", 1, None, True):
                profile = self._profile(engine, cacheRetention=bad)
                with self.assertRaises(ConfigError, msg=f"{engine}:{bad!r}"):
                    effective(self.config, self.config.roles["worker"], profile)

    def test_codex_and_claude_keep_native_behavior(self):
        # No Mizu allowlist on codex/claude (as today): the key passes
        # through as native engine options without Mizu validation.
        for engine in ("codex", "claude"):
            profile = self._profile(engine, cacheRetention="long")
            settings = effective(self.config, self.config.roles["worker"], profile)
            self.assertEqual(settings["options"]["cacheRetention"], "long")

    def test_unset_omits(self):
        for engine in ("pi", "pi-durable"):
            profile = self._profile(engine)
            settings = effective(self.config, self.config.roles["worker"], profile)
            self.assertNotIn("cacheRetention", settings["options"])

    def test_pi_cache_retention_is_live(self):
        from mizu.engine_config import session_record
        profile = self._profile("pi")
        role = self.config.roles["worker"]
        before = effective(self.config, role, profile)
        path_before, _ = session_record(self.context(), profile, before)
        key_before = path_before.parent.name
        self.config.profiles[profile]["options"] = {
            **before["options"], "cacheRetention": "long"}
        after = effective(self.config, role, profile)
        path_after, _ = session_record(self.context(), profile, after)
        self.assertEqual(path_after.parent.name, key_before)
        self.assertNotIn("cacheRetention", conversation_settings(after)["options"])

    def test_durable_identity_ignores_cache_retention(self):
        base = {"engine": "pi-durable", "options": {}}
        changed = {"engine": "pi-durable", "options": {"cacheRetention": "long"}}
        self.assertEqual(durable_session_identity(base), durable_session_identity(changed))

    def test_grant_binds_cache_retention(self):
        role = self.config.roles["worker"]
        model = {"provider": "p", "model": "m"}
        base = pi_durable.grant_for({"options": {}}, role, model, self.project.goal)
        for value in ("none", "short", "long"):
            changed = pi_durable.grant_for({"options": {"cacheRetention": value}}, role, model, self.project.goal)
            self.assertNotEqual(base, changed, value)

    def test_durable_effective_config_includes_omits(self):
        import types
        from pathlib import Path
        from unittest import mock
        profile = self._profile("pi-durable")
        driver = pi_durable.PiDurableDriver(self.config)
        context = self.context()
        context.handle("_hello", {})
        context.handle("_budget", {"sequence": 1})
        context.handle("finish", {"outcome": "wait", "summary": "s", "state": "n"})
        reported = {"durable_result": True, "status": "done", "finish_called": True,
                    "requests": 1, "usage": {"input": 1, "output": 1}}
        fake = types.SimpleNamespace(reason="exited", exit_code=0,
                                     stdout=json.dumps(reported), stderr="")
        settings = effective(self.config, context.role, profile)
        model = self.config.model(profile)
        with mock.patch.object(pi_durable, "Bridge") as bridge_cls, \
                mock.patch.object(pi_durable, "run", return_value=fake):
            bridge_cls.return_value.__enter__.return_value.config_file = str(
                context.run_dir / "bridge.json")
            driver._run_once(context, settings, model, "hello", profile,
                             "run-key", Path("/tmp/store.sqlite"))
        written = json.loads((context.run_dir / "pi-durable-effective.json").read_text())
        self.assertNotIn("cacheRetention", written)
        # Set value carries through effective.json.
        self.config.profiles[profile]["options"] = {"cacheRetention": "long"}
        settings = effective(self.config, context.role, profile)
        context2 = self.context()
        context2.handle("_hello", {})
        context2.handle("_budget", {"sequence": 1})
        context2.handle("finish", {"outcome": "wait", "summary": "s", "state": "n"})
        with mock.patch.object(pi_durable, "Bridge") as bridge_cls, \
                mock.patch.object(pi_durable, "run", return_value=fake):
            bridge_cls.return_value.__enter__.return_value.config_file = str(
                context2.run_dir / "bridge.json")
            driver._run_once(context2, settings, model, "hello", profile,
                             "run-key", Path("/tmp/store.sqlite"))
        written2 = json.loads((context2.run_dir / "pi-durable-effective.json").read_text())
        self.assertEqual(written2["cacheRetention"], "long")

    def test_durable_launcher_refuses_invalid_cache_retention(self):
        import importlib.util
        import subprocess
        import tempfile
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('check_gate', root / 'scripts/check.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Environment-coupled like the durable contract suite: without the
        # pinned SDK the launcher fails on missing imports before reaching
        # validation, so skip for absent provisioning only. A version mismatch
        # is a contract violation and must fail like any other.
        state, details = module.pi_sdk_deps_status(root / 'adapters/pi-durable')
        if state == "absent":
            self.skipTest(f"pinned SDK not provisioned here: {details}")
        if state != "ready":
            self.fail(f"pinned SDK version mismatch is a contract violation: {details}")
        launcher = root / "adapters" / "pi-durable" / "launcher.mjs"
        with tempfile.TemporaryDirectory() as td:
            cfg = {"provider": "p", "model": "m", "instructions": "i",
                   "store": str(Path(td) / "s.sqlite"), "requestId": "r",
                   "prompt": "go", "cwd": td, "tools": [{"name": "t"}],
                   "authPath": str(Path(td) / "auth.json"), "cacheRetention": "bogus"}
            target = Path(td) / "effective.json"
            target.write_text(json.dumps(cfg))
            child = subprocess.run(["node", str(launcher), str(target)],
                                   capture_output=True, text=True, timeout=120)
            self.assertNotEqual(child.returncode, 0)
            self.assertIn("cacheRetention", child.stderr)


if __name__ == "__main__":
    unittest.main()
