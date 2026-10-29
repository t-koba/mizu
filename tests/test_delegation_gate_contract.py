"""Real Context gate tests, not runner/adapter/quiescence integration proof."""
import dataclasses
import tomllib
from unittest.mock import Mock, patch

from support import Fixture
from mizu import delegation_gates, delegation_jobs
from mizu.errors import Denied
from mizu.runtime import Context


MATRIX_PATH = delegation_gates.POLICY_PATH
with MATRIX_PATH.open("rb") as source:
    MATRIX = tomllib.load(source)


class DelegationGateContractTests(Fixture):
    def setUp(self):
        super().setUp()
        self.view = {
            "id": "a" * 64, "owner": "worker", "status": "running",
            "request": {"source_snapshot": self.project.snapshots.get()["id"]},
        }
        self.ctx = self.context(reservation_view=self.view)
        # Exercise the fence with all grants, so a capability refusal cannot
        # accidentally satisfy a reservation test.
        self.ctx.role = dataclasses.replace(
            self.ctx.role, capabilities=tuple(Context._DISPATCH), engine_tools=("Read",))

    def assert_refused_without_effects(self, operation, arguments):
        count = self.ctx.tool_count
        evidence = dict(self.ctx.engine_tool_invocations)
        progress = self.ctx.progress_tool
        requests = self.ctx.request_count
        files = sorted(str(path.relative_to(self.ctx.run_dir))
                       for path in self.ctx.run_dir.rglob("*"))
        with patch.dict(Context._DISPATCH, {operation: Mock()}) as handlers:
            for entry in (self.ctx.handle, self.ctx.dispatch):
                with self.subTest(entry=entry.__name__, operation=operation, arguments=arguments):
                    with self.assertRaises(Denied):
                        entry(operation, arguments)
                    handlers[operation].assert_not_called()
                    self.assertEqual(self.ctx.tool_count, count)
                    self.assertEqual(self.ctx.engine_tool_invocations, evidence)
                    self.assertIsNone(self.ctx.finished)
                    self.assertEqual(self.ctx.progress_tool, progress)
                    self.assertEqual(self.ctx.request_count, requests)
                    self.assertEqual(files, sorted(str(path.relative_to(self.ctx.run_dir))
                                                  for path in self.ctx.run_dir.rglob("*")))

    def test_matrix_classifies_every_public_context_operation_once(self):
        classified = [operation for category in (
            "parent_control_plane", "parent_snapshot_only", "parent_exact_job_wait",
            "parent_denied") for operation in MATRIX[category]]
        classified.append("vcs_read")
        self.assertEqual(len(classified), len(set(classified)))
        self.assertEqual(set(classified), set(Context._DISPATCH))

    def test_control_plane_allowlist_reaches_handlers(self):
        # Parameter schemas remain the normal handle path's responsibility;
        # bypass only that validation for this exhaustive dispatch inventory.
        with patch("mizu.runtime.validate"):
            for operation in MATRIX["parent_control_plane"]:
                handler = Mock(return_value={"allowed": True})
                with self.subTest(operation=operation), patch.dict(Context._DISPATCH, {operation: handler}):
                    self.assertEqual(self.ctx.handle(operation, {}), {"allowed": True})
                    self.assertEqual(self.ctx.dispatch(operation, {}), {"allowed": True})
                    self.assertEqual(handler.call_count, 2)
        # Also execute real local read handlers with ordinary schema validation.
        self.assertIsInstance(self.ctx.handle("work_items_read", {}), dict)
        self.assertIsInstance(self.ctx.dispatch("research_read", {}), dict)

    def test_diff_reads_exact_pinned_snapshot_not_changed_live_tree(self):
        expected = self.ctx.dispatch("diff", {})
        (self.project.workspace / "app.py").write_text("child partial edits\n")
        with patch.object(self.project.snapshots, "capture_files", side_effect=AssertionError("live capture")):
            self.assertEqual(self.ctx.handle("diff", {}), expected)
            self.assertEqual(self.ctx.dispatch("diff", {}), expected)
        self.ctx.snapshot = {**self.ctx.snapshot, "id": "b" * 64}
        self.assert_refused_without_effects("diff", {})

    def test_parent_transport_protocol_is_explicit_but_native_preflight_is_not(self):
        self.assertEqual(set(MATRIX["parent_transport"]),
                         {"_hello", "_budget", "_model_usage", "_request_done"})
        self.assertNotIn("_engine_tool", MATRIX["parent_transport"])
        self.assertNotIn("finish", MATRIX["parent_transport"])
        self.assertEqual(self.ctx.handle("_hello", {}), {})
        self.assertTrue(self.ctx.hello.is_set())
        self.assertEqual(self.ctx.handle("_budget", {"sequence": 1}), {"admitted": True})
        self.assertEqual(self.ctx.handle("_model_usage", {"sequence": 1, "usage": {"input": 1}}),
                         {"recorded": True})
        self.assertEqual(self.ctx.handle("_request_done", {"sequence": 1, "failed": False}),
                         {"recorded": True})
        self.assert_refused_without_effects("_engine_tool", {"name": "Read", "invocation_id": "native-2"})
        self.assert_refused_without_effects("finish", {"outcome": "wait", "summary": "wait"})

    def test_reserved_transport_round_trips_through_bridge_protocol(self):
        from mizu.bridge import Bridge
        from mizu.fs import read_json
        from mizu.mcp_proxy import forward

        with Bridge(self.ctx.handle, [], timeout=2) as bridge:
            config = read_json(bridge.config_file)
            self.assertEqual(forward(config, "_hello", {}), {})
            self.assertEqual(forward(config, "_budget", {"sequence": 1}), {"admitted": True})
            self.assertEqual(forward(config, "_model_usage",
                                     {"sequence": 1, "usage": {"input": 1}}), {"recorded": True})
            self.assertEqual(forward(config, "_request_done",
                                     {"sequence": 1, "failed": False}), {"recorded": True})
            before = sorted((str(path.relative_to(self.ctx.run_dir)), path.read_bytes())
                            for path in self.ctx.run_dir.rglob("*") if path.is_file())
            denied = (
                ("_engine_tool", {"name": "Read", "invocation_id": "native-bridge"}),
                ("finish", {"outcome": "wait", "summary": "wait"}),
                ("files", {}),
                ("read", {"path": "app.py"}),
                ("unclassified", {}),
            )
            for operation, arguments in denied:
                with self.subTest(operation=operation), self.assertRaises(Denied):
                    forward(config, operation, arguments)
                after = sorted((str(path.relative_to(self.ctx.run_dir)), path.read_bytes())
                               for path in self.ctx.run_dir.rglob("*") if path.is_file())
                self.assertEqual(after, before)
                self.assertEqual(self.ctx.tool_count, 0)
                self.assertFalse(self.ctx.engine_tool_invocations)
                self.assertEqual(self.ctx.request_count, 1)
                self.assertFalse(self.ctx.progress_tool)
                self.assertIsNone(self.ctx.finished)

        self.assertTrue(self.ctx.hello.is_set())
        self.assertEqual(self.ctx.request_count, 1)
        self.assertFalse(self.ctx.request_deadlines)
        self.assertTrue(self.ctx.runtime_usage)

    def test_engine_capture_boundaries_refuse_any_reserved_view(self):
        engine = __import__("mizu.runtime", fromlist=["Engine"]).Engine(self.config)
        self.ctx._slot_lease = True
        with patch.object(self.project.snapshots, "capture_files", side_effect=AssertionError("live capture")):
            with self.assertRaisesRegex(Denied, "Consultation capture"):
                engine.consult(self.ctx, {"question": "pinned?"})
            with self.assertRaisesRegex(Denied, "Delegation capture"):
                engine.delegate(self.ctx, {"role": "reporter", "task": "test"})
        self.assertFalse(self.ctx.closed)
        with self.assertRaises(Denied):
            delegation_gates.refuse_live_tree(self.view, "final capture")
        self.assertIsNone(delegation_gates.refuse_live_tree(None, "final capture"))

    def test_vcs_suboperations_are_narrow_and_validate_before_counters(self):
        for operation in MATRIX["parameterized"]["vcs_read"]["allow"]:
            handler = Mock(return_value={"allowed": True})
            with self.subTest(operation=operation), patch.dict(Context._DISPATCH, vcs_read=handler):
                self.assertEqual(self.ctx.handle("vcs_read", {"op": operation}), {"allowed": True})
                self.assertEqual(self.ctx.dispatch("vcs_read", {"op": operation}), {"allowed": True})
                self.assertEqual(handler.call_count, 2)
        for args in ({"op": op} for op in ("log", "comments", "proposals", "acquire", "artifacts", "artifact", "unknown")):
            self.assert_refused_without_effects("vcs_read", args)
        for args in ({}, {"op": None}, {"op": []}, {"op": 3}, {"op": "status", "limit": 0},
                     {"op": "status", "unrecognized": True}, {"op": "status", "sha": []}, []):
            self.assert_refused_without_effects("vcs_read", args)

    def test_live_tree_consult_finish_native_and_unclassified_deny(self):
        self.ctx.consult = Mock(side_effect=AssertionError("consult callback"))
        self.ctx.delegate = Mock(side_effect=AssertionError("delegate callback"))
        for operation in MATRIX["parent_denied"] + ["consult", "finish", "unclassified", "_engine_tool"]:
            args = {"name": "Read", "invocation_id": "native-1"} if operation == "_engine_tool" else {}
            self.assert_refused_without_effects(operation, args)
        for outcome in ("wait", "done", "continue"):
            self.assert_refused_without_effects("finish", {"outcome": outcome, "summary": "hold"})
        # Model arguments cannot substitute the immutable Context snapshot.
        self.assert_refused_without_effects("consult", {"snapshot_id": self.view["request"]["source_snapshot"]})
        self.ctx.consult.assert_not_called()
        self.ctx.delegate.assert_not_called()

    def test_unknown_malformed_and_non_owner_views_deny_every_operation(self):
        views = [False, "unknown", [], {}, {**self.view, "owner": "reviewer"},
                 {**self.view, "status": "terminal"}, {**self.view, "status": []},
                 {**self.view, "id": "invalid"}, {**self.view, "request": None},
                 {**self.view, "request": {}},
                 {**self.view, "request": {"source_snapshot": "invalid"}}]
        for view in views:
            self.ctx._reservation_view = view
            for operation in Context._DISPATCH:
                with self.subTest(view=view, operation=operation):
                    self.assert_refused_without_effects(operation, {"op": "status"} if operation == "vcs_read" else {})
            self.assert_refused_without_effects("_engine_tool", {"name": "Read", "invocation_id": "native-1"})

    def test_policy_is_fail_closed_and_loaded_only_when_gate_is_present(self):
        for error in (FileNotFoundError(), PermissionError(), ValueError(),
                      Denied("unsafe policy")):
            with patch("mizu.delegation_gates.safe_read", side_effect=error) as read_policy:
                self.assert_refused_without_effects("work_items_read", {})
                self.assertTrue(read_policy.called)
                self.ctx._reservation_view = None
                read_policy.reset_mock()
                self.assertIsInstance(self.ctx.handle("files", {}), dict)
                self.assertEqual(self.ctx.handle("_engine_tool", {
                    "name": "Read", "invocation_id": "native-1"}), {"allowed": True})
                self.assertIsInstance(self.ctx.dispatch("diff", {}), dict)
                read_policy.assert_not_called()
                self.ctx._reservation_view = self.view
        for change in ({"parent_control_plane": "files"}, {"parent_control_plane": [None]},
                       {"parameterized": {}}, {"reserved_statuses": []},
                       {"parent_transport": []},
                       {"runner_integration_allowed": True}, {"version": 2}):
            with patch("mizu.delegation_gates.tomllib.loads", return_value={**MATRIX, **change}):
                self.assert_refused_without_effects("work_items_read", {})

    def test_absent_gate_keeps_native_grants_and_normal_handlers(self):
        ctx = self.context()
        self.assertIsNone(ctx._reservation_view)
        self.assertIsInstance(ctx.handle("files", {}), dict)
        self.assertIsInstance(ctx.dispatch("read", {"path": "app.py"}), dict)
        with self.assertRaisesRegex(Denied, "explicit grant"):
            ctx.handle("_engine_tool", {"name": "Read", "invocation_id": "native-1"})
        # Injected view is not an accepted tool parameter.
        with self.assertRaises(Denied):
            ctx.handle("diff", {"reservation_view": self.view})

    def test_job_store_view_keeps_gate_until_proven_quiescence(self):
        store = delegation_jobs.DelegationJobs(self.project.root)
        request = {"role": "helper", "task": "test reservation gating", "profile": None,
                   "source_snapshot": self.ctx.snapshot["id"]}
        job_id = store.start("worker", "key", request)["id"]
        self.ctx._reservation_view = store.reservation()
        token = self.ctx._reservation_view["token"]
        for transition in (lambda: None, lambda: store.runner_started(job_id, token),
                           lambda: store.cancel("worker", job_id),
                           lambda: store.runner_finished(job_id, token, "cancelled")):
            transition()
            self.ctx._reservation_view = store.reservation()
            self.assertIn(self.ctx._reservation_view["status"], MATRIX["reserved_statuses"])
            self.assertIsInstance(self.ctx.handle("work_items_read", {}), dict)
            self.assert_refused_without_effects("exec", {})
        store.runner_quiescent(job_id, token, "Fixture proof only")
        self.ctx._reservation_view = store.reservation()
        self.assertIsNone(self.ctx._reservation_view)
        self.assertIsInstance(self.ctx.handle("files", {}), dict)

    def test_runner_integration_stays_disabled_while_inventory_has_unknowns(self):
        self.assertEqual(MATRIX["status"], "pre-integration-gates")
        self.assertFalse(MATRIX["runner_integration_allowed"])
        self.assertTrue(MATRIX["entrypoints"]["not_yet_proven"])
        entrypoints = MATRIX["entrypoints"]
        for name in ("Context._engine_tool before native adapter execution",
                     "Engine.run continuation workspace comparison capture after admission",
                     "Engine.consult parent.workspace capture",
                     "check_jobs.available source capture",
                     "configured MCP containers mounted at context.workspace"):
            with self.subTest(entrypoint=name):
                self.assertIn(name, entrypoints["parent_live_tree_gates"])
        self.assertIn("Project.initialize source capture when source resolves under a managed project workspace",
                      entrypoints["external_workspace_entrypoints"])
        self.assertIn("actively reserved workspace", entrypoints["external_workspace_action"])
        self.assertTrue(entrypoints["reservation_exclusion"])
        self.assertIn("storage.maintain_automatic_prune snapshot/run retention",
                      entrypoints["reservation_exclusion"])
        self.assertIn("probes.service_due role/workspace lock",
                      entrypoints["reservation_exclusion"])
        self.assertTrue(entrypoints["quiescence_required"])
        self.assertIn("Refuse every live-tree call", entrypoints["parent_live_tree_action"])
        self.assertIn("before any workspace access", entrypoints["reservation_exclusion_action"])
        self.assertIn("uncertain proof never releases it", entrypoints["quiescence_release_rule"])
        authority = MATRIX["child_authority"]
        self.assertIn("effective role grants and tool limits", authority["required_bindings"])
        self.assertTrue(any("writable container identities" in item for item in authority["required_bindings"]))
        self.assertIn("Refuse launch", authority["mismatch_action"])
        lifecycle = MATRIX["observed_lifecycle"]
        self.assertIn("not descendant", lifecycle["driver_channel"])
        self.assertIn("supervisor-death reaper", lifecycle["mcp_containers"])
        self.assertIn("does not import", lifecycle["job_store"])
        self.assertIn("setsid descendant survive", lifecycle["process_group_experiment"])
