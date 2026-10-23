"""Delegation: one-shot implementation children in the caller's live workspace."""
import dataclasses
import json
import unittest
from support import Fixture, ScriptDriver
from mizu.errors import ConfigError, Denied, LimitExceeded, RunDeadlineExceeded
from mizu.admission import role_exclusion
from mizu.drivers import admit_invocation
from mizu.fs import held, lock, read_json
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


class _AdmitDriver:
    """A driver shaped like the Claude and Codex adapters.

    Those adapters admit the invocation through the shared admission path
    and do not seal on their own, so the boundary under test is the one
    every engine crosses after a turn.
    """

    requires_sandbox = False

    def __init__(self, callback=None):
        self.callback = callback

    def execute(self, context, prompt, profile=None):
        admit_invocation(context, unit="query")
        if self.callback:
            self.callback(context, prompt, profile)
        # The adapters end their request when the turn ends; the double does
        # the same, so a turn does not leave a request deadline behind.
        context.clear_request_deadlines()
        return {"requests": context.request_count, "profile": profile or context.role.profile}


class _PollDriver:
    """A driver shaped like the Pi engine's transport poll.

    The Pi engine admits each provider request behind a bridge and then
    polls the transport between requests, so a child can meet the drain
    at that poll rather than at an admission.
    """

    requires_sandbox = False

    def __init__(self, callback=None):
        self.callback = callback
        self.admissions = {}

    def execute(self, context, prompt, profile=None):
        role = context.role.name
        self.admissions[role] = self.admissions.get(role, 0) + 1
        admit_invocation(context, unit="query")
        if self.callback:
            self.callback(context, prompt, profile)
        # The request finished, so the engine polls the transport again.
        context.clear_request_deadlines()
        context.check_deadlines(wrap_up=True)
        return {"requests": context.request_count, "profile": profile or context.role.profile}


class DelegateTests(Fixture):
    def test_children_inherit_parent_charter_after_operator_goal_changes(self):
        from mizu.config import system_context_text
        from mizu.fs import digest
        config = _configured(self)
        pinned = self.project.goal
        observed = []
        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                (self.project.root / "PROJECT.md").write_text("New live operator charter")
                for operation, args in (
                    ("consult", {"roles": ["consult"], "question": "Check the charter"}),
                    ("delegate", {"role": "helper", "task": "Check the charter"}),
                ):
                    ctx.handle(operation, args)
                ctx.handle("finish", {"outcome": "wait", "summary": "Children checked", "state": "Checked"})
            else:
                self.assertEqual(ctx.goal, pinned)
                self.assertEqual(ctx.goal_digest, digest(pinned.encode()))
                self.assertTrue(system_context_text(ctx.role, ctx.goal).endswith(pinned))
                decoded = json.loads(prompt.split("\n\nQuestion:")[0].split("\n\nTask:")[0])
                self.assertNotIn("goal", decoded)
                self.assertEqual(decoded["goal_digest"], ctx.goal_digest)
                observed.append(ctx.role.name)
                ctx.handle("finish", {"outcome": "wait", "summary": "Pinned charter", "state": "Checked"})
        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(observed, ["consult", "helper"])

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
        dispatch = read_json(child_runs[0] / "dispatch-attempts.json")["attempts"][0]
        self.assertEqual(dispatch["parent_run"], result["run"])
        self.assertEqual(dispatch["class"], "delegate")
        self.assertEqual(dispatch["role"], "helper")
        self.assertEqual(dispatch["usage"]["status"], "unknown")
        self.assertIsNone(dispatch["usage"]["cache_read_tokens"])
        delegation = read_json(child_runs[0] / "delegation.json")
        self.assertEqual(delegation["answer"], "child saw marker")
        self.assertEqual(delegation["activity"]["schema"], 1)
        self.assertGreaterEqual(delegation["activity"]["elapsed_seconds"], 0.0)
        parent_records = list((runs / result["run"] / "delegations").glob("*.json"))
        self.assertEqual(len(parent_records), 1)

    def test_delegated_overspend_coerces_parent_seal(self):
        config = _configured(self)
        roles = dict(config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], requests_per_run=2)
        config = dataclasses.replace(config, roles=roles)
        seen = {}

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                if ctx.wrap_up_attempted:
                    seen["root_requests"] = ctx.wrap_up_requests
                    ctx.handle("finish", {"outcome": "continue", "summary": "parent seals",
                                          "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
                    return
                ctx.handle("delegate", {"role": "helper", "task": "spend"})
                seen["bound"] = ctx.bound_hit
                seen["child_requests"] = ctx.delegated_wrap_up_requests
                # Child used its full share; the root retains one request.
                ctx.handle("_budget", {"sequence": 2})
                seen["root_requests"] = ctx.wrap_up_requests
                ctx.handle("finish", {"outcome": "continue", "summary": "parent seals",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
            else:
                # ScriptDriver's request reaches the normal cap; these two
                # admitted requests use the delegated-child share.
                ctx.handle("_budget", {"sequence": 2})
                seen["child_bound"] = ctx.bound_hit
                ctx.handle("finish", {"outcome": "continue", "summary": "child seals",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})

        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(seen["child_bound"], "requests")
        self.assertEqual(seen["bound"], "requests")
        self.assertEqual(seen["child_requests"], config.limits.delegate_wrap_up_requests)
        self.assertEqual(seen["root_requests"], 1)
        self.assertEqual(result["finish"]["outcome"], "bounded")

    def test_child_deadline_aggregate_share_preserves_root_request(self):
        config = _configured(self)
        roles = dict(config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], requests_per_run=6)
        config = dataclasses.replace(config, roles=roles)
        seen = {"children": 0, "pool_exhausted": False}

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                if ctx.wrap_up_attempted:
                    seen["root_requests_after_seal_request"] = ctx.wrap_up_requests
                    ctx.handle("finish", {"outcome": "continue", "summary": "root sealed",
                                          "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
                    return
                responses = [ctx.handle("delegate", {"role": "helper", "task": str(i)})
                             for i in range(3)]
                seen["parent_bound"] = ctx.bound_hit
                seen["child_requests"] = ctx.delegated_wrap_up_requests
                seen["root_requests_before_seal"] = ctx.wrap_up_requests
                seen["third_status"] = responses[2].get("status", "completed")
                ctx.handle("_budget", {"sequence": 2})
                seen["root_requests_after_seal_request"] = ctx.wrap_up_requests
                ctx.handle("finish", {"outcome": "continue", "summary": "root sealed",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
            else:
                seen["children"] += 1
                ctx.bound_hit = "deadline"  # Child-local deadline only.
                if seen["children"] <= 2:
                    ctx.handle("_budget", {"sequence": 2})
                else:
                    with self.assertRaises(RunDeadlineExceeded):
                        ctx.handle("_budget", {"sequence": 2})
                    seen["pool_exhausted"] = True
                ctx.handle("finish", {"outcome": "continue", "summary": "child bounded",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})

        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(seen["children"], 3)
        self.assertTrue(seen["pool_exhausted"])
        self.assertEqual(seen["third_status"], "completed")
        self.assertIsNone(seen["parent_bound"])
        self.assertEqual(seen["child_requests"], config.limits.delegate_wrap_up_requests)
        self.assertEqual(seen["root_requests_before_seal"], 0)
        self.assertEqual(seen["root_requests_after_seal_request"], 1)
        self.assertEqual(result["finish"]["outcome"], "bounded")

    def test_tool_bound_child_leaves_root_finish_tool(self):
        config = _configured(self)
        roles = dict(config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], tools_per_run=2)
        roles["helper"] = dataclasses.replace(
            roles["helper"], capabilities=tuple([*roles["helper"].capabilities, "research"]))
        config = dataclasses.replace(config, roles=roles)
        seen = {}

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                ctx.handle("delegate", {"role": "helper", "task": "spend tools"})
                seen["bound"] = ctx.bound_hit
                seen["child_tools"] = ctx.delegated_wrap_up_tools
                ctx.handle("finish", {"outcome": "continue", "summary": "root seals",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
                seen["root_tools"] = ctx.wrap_up_tools
            else:
                ctx.handle("files", {"limit": 1, "offset": 0})
                with self.assertRaises(LimitExceeded):
                    ctx.handle("read", {})
                dispatch = ctx.dispatch
                ctx.dispatch = lambda operation, arguments: ({} if operation == "research"
                                                             else dispatch(operation, arguments))
                for _ in range(config.limits.delegate_wrap_up_tools - 1):
                    ctx.handle("research", {"state": "{}", "expected_generation": 0})
                ctx.handle("finish", {"outcome": "continue", "summary": "child seals",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})

        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(seen["bound"], "tools")
        self.assertEqual(seen["child_tools"], config.limits.delegate_wrap_up_tools)
        self.assertEqual(seen["root_tools"], 1)
        self.assertEqual(result["finish"]["outcome"], "bounded")

    def test_root_fallback_notice_uses_residual_after_child_spend(self):
        config = _configured(self)
        roles = dict(config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], requests_per_run=2, tools_per_run=1)
        config = dataclasses.replace(config, roles=roles)
        seen = {}

        def callback(ctx, prompt, profile):
            if ctx.role.name != "worker":
                ctx.handle("_budget", {"sequence": 2})
                ctx.handle("finish", {"outcome": "continue", "summary": "child sealed",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
                return
            if ctx.execution_attempt == 1:
                ctx.handle("delegate", {"role": "helper", "task": "use child allowance"})
                self.assertEqual(ctx.delegated_wrap_up_requests, 2)
                self.assertEqual(ctx.delegated_wrap_up_tools, 1)
                raise LimitExceeded("exercise ordinary root fallback")
            seen["notice"] = prompt
            ctx.handle("finish", {"outcome": "continue", "summary": "root sealed",
                                  "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})

        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finish"]["outcome"], "bounded", (seen, result["finish"]))
        self.assertIn("1 requests and 4 wrap-up tools remain", seen["notice"])
        self.assertNotIn("3 requests and 5 wrap-up tools remain", seen["notice"])

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

    def test_the_delegated_child_holds_its_role_exclusion(self):
        config = _configured(self)
        seen = {}
        exclusion = role_exclusion(self.project.root, "helper")

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                response = ctx.handle("delegate", {"role": "helper", "task": "List the workspace"})
                seen["answer"] = response["answer"]
                ctx.handle("finish", {"outcome": "continue", "summary": "child ran",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
            else:
                # The child is an invocation of its own role: it holds that
                # role's exclusion while it runs, so a sibling cleanup
                # cannot remove the containers it is using.
                seen["held"] = held(exclusion)
                ctx.handle("finish", {"outcome": "continue", "summary": "child ran",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})

        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(seen["held"])
        # The exclusion is released with the child, so the role is free again.
        self.assertFalse(held(exclusion))

    def test_delegation_to_an_active_role_is_refused(self):
        config = _configured(self)
        engine = Engine(config, driver=ScriptDriver())
        with lock(role_exclusion(self.project.root, "helper"), blocking=False):
            with self.assertRaises(Denied):
                self.admitted_call(engine, "delegate", {"role": "helper", "task": "t"})

    def test_a_delegated_child_stops_when_the_project_drains(self):
        config = _configured(self)
        seen = {"delegated": False}

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                if not seen["delegated"]:
                    seen["delegated"] = True
                    self.project.set_control(draining=True, reason="Operator requested project drain")
                    response = ctx.handle("delegate", {"role": "helper", "task": "List the workspace"})
                    seen["response"] = response
                try:
                    ctx.handle("finish", {"outcome": "continue", "summary": "child stopped",
                                          "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
                except Denied as exc:
                    # The drain bound reached the caller too: the seal waits
                    # for the reduced-grant wrap-up the engine would drive.
                    seen["seal_refused"] = str(exc)
            else:
                ctx.handle("finish", {"outcome": "continue", "summary": "child ran",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})

        result = Engine(config, driver=ScriptDriver(callback)).run(self.project, "worker")
        # The child stops at its safe boundary and says so, so the draining
        # parent is not held by a child that cannot finish.
        self.assertEqual(seen["response"]["status"], "interrupted")
        self.assertIn("Drain bound refused", seen["response"]["error"])
        # The caller seals under the drain bound, and the exact outcome is
        # the drain-bound one rather than any other completion.
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["wrap_up"]["bound"], "drain")

    def test_a_delegated_child_stops_on_the_claude_style_admission_path(self):
        config = _configured(self)
        seen = {"delegated": False}

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                if not seen["delegated"]:
                    seen["delegated"] = True
                    self.project.set_control(draining=True, reason="Operator requested project drain")
                    response = ctx.handle("delegate", {"role": "helper", "task": "List the workspace"})
                    seen["response"] = response
                try:
                    ctx.handle("finish", {"outcome": "continue", "summary": "child stopped",
                                          "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
                except Denied as exc:
                    seen["seal_refused"] = str(exc)
            else:
                seen["child_ran"] = True
                ctx.handle("finish", {"outcome": "continue", "summary": "child ran",
                                      "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})

        result = Engine(config, driver=_AdmitDriver(callback)).run(self.project, "worker")
        # The same rule holds on the path the Claude and Codex adapters use:
        # the child is stopped before its first turn and reports the stop.
        self.assertEqual(seen["response"]["status"], "interrupted")
        self.assertIn("Drain bound refused", seen["response"]["error"])
        self.assertNotIn("child_ran", seen)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["wrap_up"]["bound"], "drain")

    def test_a_delegated_child_stops_on_the_pi_style_transport_poll(self):
        config = _configured(self)
        seen = {"delegated": False}
        driver = _PollDriver()

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                if not seen["delegated"]:
                    seen["delegated"] = True
                    response = ctx.handle("delegate", {"role": "helper", "task": "List the workspace"})
                    seen["response"] = response
                try:
                    ctx.handle("finish", {"outcome": "continue", "summary": "child stopped",
                                          "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
                except Denied as exc:
                    seen["seal_refused"] = str(exc)
            else:
                # The drain arrives while the child is between provider
                # requests, so the transport poll is the crossing it meets.
                self.project.set_control(draining=True, reason="Operator requested project drain")

        driver.callback = callback
        result = Engine(config, driver=driver).run(self.project, "worker")
        # The same rule holds at the transport poll: the child stops there
        # and reports the interruption to its caller.
        self.assertEqual(seen["response"]["status"], "interrupted")
        self.assertIn("Drain bound refused", seen["response"]["error"])
        # The child took no wrap-up turn: a child that cannot finish is not
        # allowed to spend the caller's grant on one.
        self.assertEqual(driver.admissions.get("helper"), 1)
        # The caller seals under the drain bound, as on every other path.
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["wrap_up"]["bound"], "drain")

    def test_a_stopped_child_keeps_the_work_it_already_did(self):
        config = _configured(self)
        seen = {"delegated": False}
        marker = self.project.workspace / "child-work.txt"

        def callback(ctx, prompt, profile):
            if ctx.role.name == "worker":
                if not seen["delegated"]:
                    seen["delegated"] = True
                    response = ctx.handle("delegate", {"role": "helper", "task": "Do the work"})
                    seen["response"] = response
                try:
                    ctx.handle("finish", {"outcome": "continue", "summary": "child stopped",
                                          "state": "Now: x.\nNext: y.\nTasks: none\nBlocking: none"})
                except Denied as exc:
                    seen["seal_refused"] = str(exc)
            else:
                # The child works in the caller's live workspace, and the
                # drain arrives while it works: the child stops at the next
                # boundary and the work it did stays.
                marker.write_text("child work")
                self.project.set_control(draining=True, reason="Operator requested project drain")

        result = Engine(config, driver=_AdmitDriver(callback)).run(self.project, "worker")
        self.assertEqual(seen["response"]["status"], "interrupted")
        self.assertIn("Drain bound refused", seen["response"]["error"])
        self.assertEqual(marker.read_text(), "child work")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["wrap_up"]["bound"], "drain")

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
