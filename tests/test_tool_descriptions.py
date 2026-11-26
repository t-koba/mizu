import re
import unittest

from mizu.protocol import DEFINITIONS, GRANTED_MANUALS, tool_definitions


PURPOSE_PREFIXES = {
    "diff": "Compare your input snapshot's code with a baseline without editing workspace files.",
    "files": "List visible workspace paths without opening their files.",
    "read": "Read a workspace text file by relative path without editing it.",
    "command_evidence_read": "Inspect a retained command record without running the command again.",
    "experiment_evidence_read": "Inspect a retained experiment record without rerunning the experiment.",
    "exec": "Run a shell script in your sandbox, not on the host.",
    "experiment": "Test a hypothesis in disposable /work without editing /workspace.",
    "check_job": "Queue or join a sandbox check on frozen inputs so you can work while it runs.",
    "verify": "Run configured acceptance checks to obtain a result bound to your code digest.",
    "fetch": (
        "Fetch allowlisted HTTPS text and receive an external-untrusted result with a replay "
        "receipt."),
    "probe": (
        "Inspect an allowlisted HTTPS endpoint with one bounded request and receive status, "
        "headers, body and a replay receipt."),
    "search": (
        "Search configured feeds or an operator-provided adapter and receive results with stated "
        "scope and coverage."),
    "feed_updates": "Read configured RSS/Atom updates without a query.",
    "ready_work_read": (
        "Inspect what your role can act on without claiming, transitioning or acknowledging any "
        "work."),
    "work_order_read": (
        "Inspect the stored work order against your unit's opening generation without refreshing "
        "that generation or editing the plan."),
    "report_read": "Read the latest published report preview without staging or publishing a report.",
    "work_items_read": (
        "Inspect open work or trace one item's revisions without changing status or ownership."),
    "work_items_create": "Create self-directed or split-off work without assigning or scheduling it.",
    "work_items_reopen": "Challenge a done item by reopening it so it can be claimed and closed again.",
    "work_items_transition": (
        "Move a work item to a new state or update scope, smoke pins or delivery in place."),
    "delegate": (
        "Give a configured delegate a self-contained implementation task in your live workspace."),
    "delegate_join": "Join one opted-in async delegation by its exact durable job id.",
    "consult": "Ask named read-only roles independently about one immutable code snapshot.",
    "report": "Stage a Markdown report as your unit's artifact; staging does not publish it.",
    "sync": (
        "Refresh the upstream refs you can read at refs/remotes/upstream/ without editing your "
        "worktree."),
    "vcs_read": (
        "Inspect CI status, logs, artifacts, PR comments, proposals and publication evidence "
        "without publishing or dispatching CI."),
    "vcs_request": "Request CI at an exact published commit and record the dispatch outcome.",
    "vcs_publish": "Push a branch or open a PR through the trusted VCS adapter to publish your code.",
    "vcs_retire": (
        "Retire an owned temporary integration branch through the trusted VCS adapter, without "
        "affecting other branches."),
    "vcs_dispose": (
        "Close or merge an external proposal at the assessed endpoints through the trusted VCS "
        "adapter."),
    "research_read": "Read your role's research state and generation without changing either.",
    "materialize": (
        "Prepare offline inputs through the trusted host adapter or write receipt-bound bytes "
        "into your workspace."),
    "research": (
        "Replace your role's research state with a full JSON object and record audit evidence in "
        "research-state.json."),
    "work_order": (
        "Record your ordered work-item plan without changing any item's priority, status or "
        "scheduling."),
    "result_read": "Retrieve the next page of a truncated tool result without rerunning the tool.",
    "queue_write": "Append a message to a declared queue.",
    "queue_read": "Read undelivered due messages from a declared queue, recording what you read",
    "message_read": "Read one message by its 32-hex id if your role may read its queue.",
    "message_refs": "List messages referring to id, or that id refers to, in either direction",
    "finish": "End this work unit and release its model slot and workspace lock.",
}

HISTORY_WORDING = re.compile(
    r"\b(?:legacy|unchanged|compatib(?:ility|le)|prior|old(?:er)?|previous(?:ly)?|formerly|"
    r"historically|was|were|now|existing)\b|\bas before\b",
    re.IGNORECASE,
)


class ToolDescriptionTests(unittest.TestCase):
    def test_definition_inventory(self):
        self.assertEqual(len(DEFINITIONS), 40)
        for retired in ("work_items_legacy_read", "work_items_reconcile"):
            self.assertNotIn(retired, DEFINITIONS)
        for tool, (description, _) in DEFINITIONS.items():
            with self.subTest(tool=tool):
                self.assertTrue(description.strip())

    def test_purpose_effect_openers(self):
        self.assertEqual(DEFINITIONS.keys(), PURPOSE_PREFIXES.keys())
        for tool, (description, _) in DEFINITIONS.items():
            with self.subTest(tool=tool):
                prefix = PURPOSE_PREFIXES[tool]
                self.assertTrue(description.startswith(prefix), f"Expected opening: {prefix!r}")

    def test_no_history_wording(self):
        for tool, (description, _) in DEFINITIONS.items():
            with self.subTest(tool=tool):
                self.assertNotRegex(description, HISTORY_WORDING)

    def test_description_byte_budget(self):
        # The rewritten manual plus the four declared queue tools is 27,582
        # bytes; it must stay at or below that measured size.
        self.assertLessEqual(sum(len(d.encode("utf-8")) for d, _ in DEFINITIONS.values()), 27_582)


class GrantAwareDescriptionTests(unittest.TestCase):
    @staticmethod
    def tool(capabilities, operation):
        tools = [tool for tool in tool_definitions(capabilities) if tool["operation"] == operation]
        return tools[0] if len(tools) == 1 else None

    def test_gates_are_parts_of_the_default_manuals(self):
        for name, (grant, text, _, hidden) in GRANTED_MANUALS.items():
            with self.subTest(name=name):
                description, schema = DEFINITIONS[name]
                self.assertTrue(grant)
                self.assertEqual(description.count(text), 1)
                self.assertTrue(description.endswith(text))
                self.assertTrue(set(hidden) <= set(schema["properties"]))

    def test_delegate_without_join_is_synchronous(self):
        tool = self.tool(("delegate",), "delegate")
        self.assertIn("Delegation is synchronous.", tool["description"])
        self.assertNotIn("async_mode", tool["description"])
        self.assertNotIn("mizu_delegate_join", tool["description"])
        self.assertNotIn("async_mode", tool["inputSchema"]["properties"])
        self.assertEqual(tool["inputSchema"]["required"], ["task"])

    def test_delegate_with_join_keeps_the_full_manual(self):
        tool = self.tool(("delegate", "delegate_join"), "delegate")
        self.assertEqual(tool["description"], DEFINITIONS["delegate"][0])
        self.assertEqual(tool["inputSchema"], DEFINITIONS["delegate"][1])
        self.assertIn("async_mode=true", tool["description"])
        self.assertIn("mizu_delegate_join", tool["description"])
        self.assertIn("async_mode", tool["inputSchema"]["properties"])

    def test_exec_and_experiment_hide_async_without_check_job(self):
        for operation in ("exec", "experiment"):
            with self.subTest(operation=operation):
                narrow = self.tool((operation,), operation)
                full = self.tool((operation, "check_job"), operation)
                self.assertEqual(full["description"], DEFINITIONS[operation][0])
                self.assertEqual(full["inputSchema"], DEFINITIONS[operation][1])
                self.assertNotIn("mode=async", narrow["description"])
                self.assertNotIn("check_job", narrow["description"])
                self.assertNotIn("mode", narrow["description"])
                self.assertNotIn("mode", narrow["inputSchema"]["properties"])
                if operation == "experiment":
                    self.assertNotIn("scratch_paths", narrow["description"])
                    self.assertNotIn("scratch_paths", narrow["inputSchema"]["properties"])
                self.assertIn("runs directly in this call", narrow["description"])

    def test_verify_hides_queued_collection_without_check_job(self):
        narrow = self.tool(("verify",), "verify")
        full = self.tool(("verify", "check_job"), "verify")
        self.assertEqual(full["description"], DEFINITIONS["verify"][0])
        self.assertNotIn("check_job", narrow["description"])
        self.assertIn("return their result directly", narrow["description"])

    def test_transition_hides_reopen_without_its_grant(self):
        operation = "work_items_transition"
        narrow = self.tool((operation,), operation)
        full = self.tool((operation, "work_items_reopen"), operation)
        self.assertEqual(full["description"], DEFINITIONS[operation][0])
        self.assertNotIn("work_items_reopen", narrow["description"])
