"""Delegation: one-shot implementation children in the caller's live workspace."""
import dataclasses
import json
import unittest
from support import Fixture, ScriptDriver
from mizu.errors import ConfigError, Denied
from mizu.fs import read_json
from mizu.runtime import Engine, check_delegate_role


def _configured(fixture):
    roles = dict(fixture.config.roles)
    worker = roles["worker"]
    roles["worker"] = dataclasses.replace(
        worker, capabilities=tuple([*worker.capabilities, "delegate"]))
    searcher = roles["searcher"]
    roles["helper"] = dataclasses.replace(
        searcher, name="helper", capabilities=("files", "read", "finish"))
    return dataclasses.replace(fixture.config, roles=roles)


class DelegateTests(Fixture):
    def test_delegation_completes_in_live_workspace(self):
        config = _configured(self)
        marker = self.project.workspace / "delegate-marker.txt"
        marker.write_text("live")
        child_spend = {}

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                response = ctx.handle("delegate", {"role": "helper", "task": "List the workspace"})
                child_spend["requests"] = ctx.request_count
                child_spend["tools"] = ctx.tool_count
                ctx.handle("finish", {"outcome": "continue",
                                      "summary": "child said: " + response["answer"],
                                      "state": "Now: delegated.\nNext: verify.\nTasks: none\nBlocking: none"})
            else:
                listing = ctx.handle("files", {"limit": 100, "offset": 0})
                names = list(listing["files"])
                assert "delegate-marker.txt" in names, names
                child_spend["child_requests"] = ctx.request_count
                child_spend["child_tools"] = ctx.tool_count
                ctx.handle("finish", {"outcome": "continue", "summary": "child saw marker",
                                      "state": "Now: done.\nNext: none.\nTasks: none\nBlocking: none"})

        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertIn("child saw marker", result["finish"]["summary"])
        # The child's spend counted against the caller's unit. The tool
        # adoption includes the child's finish seal, recorded after the
        # child's own count was read.
        self.assertEqual(child_spend["requests"], child_spend["child_requests"])
        self.assertEqual(child_spend["tools"], child_spend["child_tools"] + 1)
        self.assertGreater(child_spend["requests"], 1)
        runs = self.project.root / "runs"
        child_runs = [d for d in runs.iterdir()
                      if d.is_dir() and (d / "delegation.json").is_file()]
        self.assertEqual(len(child_runs), 1)
        started = read_json(child_runs[0] / "started.json")
        self.assertEqual(started["workspace"], str(self.project.workspace))
        self.assertFalse((child_runs[0] / "input").exists())
        delegation = read_json(child_runs[0] / "delegation.json")
        self.assertEqual(delegation["answer"], "child saw marker")
        self.assertEqual(delegation["activity"]["schema"], 1)
        self.assertGreaterEqual(delegation["activity"]["elapsed_seconds"], 0.0)
        parent_records = list((runs / result["run"] / "delegations").glob("*.json"))
        self.assertEqual(len(parent_records), 1)

    def test_delegated_overspend_coerces_parent_seal(self):
        import dataclasses
        from mizu.runtime import Engine
        from support import ScriptDriver
        config = _configured(self)
        roles = dict(config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], requests_per_run=2)
        config = dataclasses.replace(config, roles=roles)
        seen = {}

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                response = ctx.handle("delegate", {"role": "helper", "task": "spend"})
                seen["bound"] = ctx.bound_hit
                ctx.handle("finish", {"outcome": "continue", "summary": "parent seals",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
            else:
                # One admitted sequence is spent by the driver; the next
                # request exceeds the caller's bound inside the child.
                ctx.handle("_budget", {"sequence": 2})
                seen["child_bound"] = ctx.bound_hit
                ctx.handle("finish", {"outcome": "continue", "summary": "child seals",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})

        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(seen["child_bound"], "requests")
        self.assertEqual(seen["bound"], "requests")
        self.assertEqual(result["finish"]["outcome"], "bounded")

    def test_forbidden_grants_are_refused(self):
        from mizu.runtime import DELEGATE_FORBIDDEN
        base = self.config.roles["searcher"]
        for cap in sorted(DELEGATE_FORBIDDEN):
            role = dataclasses.replace(base, capabilities=("files", cap))
            with self.assertRaises(ConfigError, msg=cap):
                check_delegate_role("helper", role)
        check_delegate_role("helper", dataclasses.replace(base, capabilities=("files", "read")))
        check_delegate_role("helper", dataclasses.replace(base, capabilities=("files", "read", "finish")))

    def test_self_unknown_and_missing_role(self):
        config = _configured(self)

        def callback(ctx, prompt, profile):
            raise AssertionError("must not dispatch")

        ctx = self.context("worker")
        ctx.config = config
        ctx.role = config.roles["worker"]
        engine = Engine(config, driver=ScriptDriver(callback))
        with self.assertRaises(Denied):
            engine.delegate(ctx, {"role": "worker", "task": "t"})
        with self.assertRaises(ConfigError):
            engine.delegate(ctx, {"role": "ghost", "task": "t"})
        with self.assertRaises(Denied):
            engine.delegate(ctx, {"task": "t"})
        with self.assertRaises(Denied):
            engine.delegate(ctx, {"role": "helper", "task": "  "})

    def test_configured_default_delegation_uses_admitted_caller(self):
        config = dataclasses.replace(_configured(self), delegate_role="helper")
        driver = ScriptDriver()
        record = self.admitted_call(Engine(config, driver=driver), "delegate", {"task": "t"})
        self.assertEqual(record["role"], "helper")
        self.assertIn("answer", record)
        self.assertEqual(len(driver.calls), 1)

    def test_nested_delegation_is_disabled(self):
        config = _configured(self)

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                response = ctx.handle("delegate", {"role": "helper", "task": "t"})
                ctx.handle("finish", {"outcome": "continue", "summary": "child said: " + response["answer"],
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
            else:
                with self.assertRaises(Denied):
                    ctx.handle("delegate", {"role": "helper", "task": "nested"})
                ctx.handle("finish", {"outcome": "continue", "summary": "nested refused",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})

        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertIn("nested refused", result["finish"]["summary"])

    def test_delegation_error_returns_result(self):
        config = _configured(self)

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                response = ctx.handle("delegate", {"role": "helper", "task": "t"})
                ctx.handle("finish", {"outcome": "continue", "summary": "child error: " + response["error"],
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
            else:
                raise RuntimeError("synthetic child failure")

        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertIn("synthetic child failure", result["finish"]["summary"])

    def test_selector_delegate_resolves_without_inference(self):
        import copy
        from mizu.selection import validate_selectors
        config = _configured(self)
        spec = {"retry_seconds": 30, "rules": [{"candidates": [
            {"profile": "primary"}, {"profile": "alternate"}]}]}
        validate_selectors({"dynamic": spec}, config.profiles, self.file.parent)
        roles = dict(config.roles)
        helper = roles["helper"]
        roles["helper"] = dataclasses.replace(helper, profile="", selector="dynamic")
        config = dataclasses.replace(config, roles=roles, selectors={"dynamic": spec})

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                response = ctx.handle("delegate", {"role": "helper", "task": "t"})
                ctx.handle("finish", {"outcome": "continue", "summary": "via " + response["profile"],
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
            else:
                ctx.handle("finish", {"outcome": "continue", "summary": "selected child",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})

        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertIn("via primary", result["finish"]["summary"])

    def test_config_loads_delegate_capability_and_role(self):
        from mizu.config import load
        from mizu.errors import ConfigError
        import io
        text = self.file.read_text()
        # delegate_role is a root key: insert before the first section
        # (TOML has no end-of-section marker).
        out = io.StringIO()
        inserted = False
        for line in text.splitlines(keepends=True):
            if not inserted and line.startswith("["):
                out.write('delegate_role = "searcher"\n')
                inserted = True
            out.write(line)
        assert inserted
        probe = out.getvalue()
        self.file.write_text(probe)
        loaded = load(self.file)
        self.assertEqual(loaded.delegate_role, "searcher")
        self.file.write_text(probe.replace('delegate_role = "searcher"', 'delegate_role = "ghost"'))
        with self.assertRaises(ConfigError):
            load(self.file)
        # A delegate implementation role loads with finish and passes the
        # delegation gate: the mechanism ensures the seal (idempotent when
        # already configured), so no role is unusable as a delegate.
        self.file.write_text(text)
        text = self.file.read_text()
        out = io.StringIO()
        inserted = False
        for line in text.splitlines(keepends=True):
            if not inserted and line.startswith("["):
                out.write('delegate_role = "implementer"\n')
                inserted = True
            out.write(line)
        assert inserted
        out.write(
            "\n[roles.implementer]\n"
            'profile = "alternate"\n'
            'policy = "policies/consult.md"\n'
            'workspace = "write"\n'
            'capabilities = ["files", "read", "diff", "exec", "experiment", '
            '"verify", "vcs_read", "materialize", "finish"]\n'
        )
        self.file.write_text(out.getvalue())
        loaded = load(self.file)
        self.assertEqual(loaded.delegate_role, "implementer")
        check_delegate_role("implementer", loaded.roles["implementer"])
        # Without finish the same role still fails load: every configured
        # role carries finish for its own top-level runs.
        self.file.write_text(out.getvalue().replace(
            '"verify", "vcs_read", "materialize", "finish"',
            '"verify", "vcs_read", "materialize"'))
        with self.assertRaises(ConfigError):
            load(self.file)


if __name__ == "__main__":
    unittest.main()
