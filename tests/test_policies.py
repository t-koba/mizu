"""Policy contract tests: shared guidance composes without blurring roles."""
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PolicyContractTests(unittest.TestCase):
    def test_worker_keeps_integration_judgment_and_safety_constraints(self):
        text = (ROOT / "policies/worker.md").read_text()
        self.assertIn("Own the integration judgment", text)
        self.assertIn("Triage incoming Insights", text)
        self.assertIn("mizu_verify", text)
        self.assertIn("Do not weaken tests", text)
        self.assertIn("never push, deploy, or promote", text)

    def test_worker_no_longer_carries_templates_or_work_item_manual(self):
        text = (ROOT / "policies/worker.md").read_text()
        for removed in (
            "State format (four lines",
            "When you finish `blocked`",
            "Parked items must not stop runnable work",
            "mizu_work_items_read",
            "mizu_work_items_legacy_read",
            "mizu_work_items_reconcile",
            "mizu_work_items_transition",
            "coverage_complete",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, text)

    def test_configured_and_commented_policies_compose_shared_first(self):
        config = tomllib.loads((ROOT / "config/config.example.toml").read_text())
        roles = config["roles"]
        self.assertTrue(roles)
        for name, role in roles.items():
            with self.subTest(role=name):
                policy = role["policy"]
                self.assertEqual(policy[0], "policies/shared.md")
                self.assertEqual(len(policy), 2)
                self.assertEqual(policy[1], f"policies/{name}.md")

        example = (ROOT / "config/config.example.toml").read_text()
        self.assertIn('# policy = ["policies/shared.md", "policies/publisher.md"]', example)
        self.assertIn('# policy = ["policies/shared.md", "policies/classifier.md"]', example)
        # The integrator example intentionally remains without a role policy file.
        self.assertIn('# policy = "policies/integrator.md"', example)
        self.assertFalse((ROOT / "policies/integrator.md").exists())

    def test_role_files_do_not_duplicate_shared_or_tool_manual_sections(self):
        shared_headings = ("## Trust and evidence", "## Work-unit boundary")
        for path in sorted((ROOT / "policies").glob("*.md")):
            if path.name == "shared.md":
                continue
            text = path.read_text()
            with self.subTest(policy=path.name):
                for heading in shared_headings:
                    self.assertNotIn(heading, text)
                for manual in (
                    "Call `mizu_finish` alone",
                    "sandbox has no network by default",
                    "`materialize` capability",
                ):
                    self.assertNotIn(manual, text)

    def test_role_specific_reconsideration_workspace_and_ci_guidance_remains(self):
        editor = (ROOT / "policies/editor.md").read_text()
        self.assertIn("no shared-code write authority", editor)
        self.assertIn("submit_insight", editor)
        self.assertIn("Only the exported snapshot and proposal outbox", editor)
        self.assertIn("Do not claim access to the live Worker workspace", editor)

        for name in ("reviewer", "searcher"):
            text = (ROOT / f"policies/{name}.md").read_text()
            with self.subTest(role=name):
                self.assertIn("## Reconsideration on rejection", text)
                self.assertIn("Never pursue a\nwithdrawn finding", text)

        publisher = (ROOT / "policies/publisher.md").read_text()
        self.assertIn("GO <branch>", publisher)
        self.assertIn("current `code_digest`", publisher)
        self.assertIn("never holds `submit_insight`", publisher)

        reporter = (ROOT / "policies/reporter.md").read_text()
        self.assertIn("supplied `ci_branch` context", reporter)
        self.assertIn("run evidence for every CI claim", reporter)
        self.assertNotIn("previous-code", reporter)
        self.assertNotIn("previous-report", reporter)

    def test_shared_policy_is_canonical_and_architecture_rationale_is_general(self):
        shared = (ROOT / "policies/shared.md").read_text()
        self.assertEqual(shared.count("## Trust and evidence"), 1)
        self.assertEqual(shared.count("## Work-unit boundary"), 1)
        self.assertNotIn("materialize", shared)
        self.assertNotIn("sandbox", shared)
        self.assertNotIn("mizu_finish` alone", shared)

        architecture = (ROOT / "docs/architecture.md").read_text()
        self.assertIn("Keeping scheduled resolution opt-in", architecture)
        self.assertNotIn("scheduled-resolver experiment", architecture)

    def test_publisher_ships_only_approved_tree(self):
        text = (ROOT / "policies/publisher.md").read_text()
        self.assertIn("GO <branch>", text)
        self.assertIn("vcs_publish", text)
        self.assertIn("vcs_read", text)
        self.assertIn("never holds `submit_insight`", text)
        example = (ROOT / "config/config.example.toml").read_text()
        self.assertIn('[roles.publisher]', example)
        self.assertIn('policies/publisher.md', example)
        self.assertIn('vcs_publish', example)
        head, tail = example.split('[roles.publisher]')
        comment = head.split('Dedicated publisher for external publication only')[1]
        self.assertIn('required plumbing', comment)
        stanza = tail.split('\n\n')[0]
        self.assertIn('workspace = "write"', stanza)
        self.assertNotIn('"exec"', stanza)
        self.assertIn('required plumbing', text)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
