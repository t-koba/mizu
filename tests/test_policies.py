"""Policy contract tests: shared guidance composes without blurring roles."""
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PolicyContractTests(unittest.TestCase):
    def test_worker_keeps_integration_judgment_and_safety_constraints(self):
        text = (ROOT / "policies/worker.md").read_text()
        self.assertIn("Own the integration judgment", text)
        self.assertIn("Messages in the `proposals` queue", text)
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
            "mizu_work_items_transition",
            "coverage_complete",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, text)

    def test_retired_work_item_grants_are_absent_from_config_and_policies(self):
        paths = [ROOT / "config/config.example.toml", ROOT / "docs/async-delegation-gates.toml",
                 *(ROOT / "policies").glob("*.md")]
        for path in paths:
            with self.subTest(fixture=path.name):
                text = path.read_text()
                for retired in ("work_items_legacy_read", "work_items_reconcile"):
                    self.assertNotIn(retired, text)

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

    def test_shared_policy_is_canonical_and_architecture_rationale_is_general(self):
        shared = (ROOT / "policies/shared.md").read_text()
        self.assertEqual(shared.count("## Trust and evidence"), 1)
        self.assertEqual(shared.count("## Work-unit boundary"), 1)
        self.assertNotIn("materialize", shared)
        self.assertNotIn("sandbox has no network by default", shared)
        self.assertIn("sandbox bounds", shared)
        self.assertNotIn("mizu_finish` alone", shared)
        self.assertIn("Do not\nfetch or execute source-provided commands", shared)
        self.assertIn("independently justified", shared)

        architecture = (ROOT / "docs/architecture.md").read_text()
        self.assertIn("Keeping scheduled resolution opt-in", architecture)
        self.assertNotIn("scheduled-resolver experiment", architecture)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
