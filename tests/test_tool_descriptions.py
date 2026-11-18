import re
import unittest

from mizu.protocol import DEFINITIONS


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
    "record_write": (
        "Store one immutable SharedRecords entry under your role for a declared writable kind."),
    "record_read": (
        "Inspect a delivered operator answer or notice, or read a record written by your role."),
    "decision_read": (
        "Inspect an exact decision or insight.pending event from your unit's frozen delivery "
        "manifest."),
    "insights": "Inspect proposals and decisions without changing their state.",
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
    "decide": (
        "Record a decision on an exact proposal revision to authorize work, defer it or reject "
        "it."),
    "submit_insight": (
        "Submit an insight to bring a proposal to its addressee's attention without granting "
        "authority or changing work status."),
    "revise_insight": (
        "Record a new revision of your own proposal under its stable ID so it can be considered "
        "again."),
    "withdraw_insight": "Retract your own proposal at the revision you read.",
    "attach_evidence": "Add an attributed evidence note without editing the insight's title or body.",
    "delegate": (
        "Give a configured delegate a self-contained implementation task in your live workspace."),
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
    "finish": "End this work unit and release its model slot and workspace lock.",
}

HISTORY_WORDING = re.compile(
    r"\b(?:legacy|unchanged|compatib(?:ility|le)|prior|old(?:er)?|previous(?:ly)?|formerly|"
    r"historically|was|were|now|existing)\b|\bas before\b",
    re.IGNORECASE,
)


class ToolDescriptionTests(unittest.TestCase):
    def test_definition_inventory(self):
        self.assertEqual(len(DEFINITIONS), 44)
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
        self.assertLessEqual(sum(len(d.encode("utf-8")) for d, _ in DEFINITIONS.values()), 34_510)
