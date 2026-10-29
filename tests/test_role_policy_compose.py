"""Composed role policies: string or list, concatenated in order, fail closed."""
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import Fixture
from mizu.config import load, role_policy_text, role_policy_bytes, system_context_text
from mizu.errors import ConfigError
from mizu.fs import digest


class ComposedPolicyTests(Fixture):
    def _rewrite_policy(self, value_toml):
        lines = self.file.read_text().splitlines(keepends=True)
        start = next(i for i, line in enumerate(lines) if line.strip() == "[roles.worker]")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        hits = [i for i in range(start, end) if lines[i].startswith("policy = ")]
        self.assertEqual(len(hits), 1)
        lines[hits[0]] = "policy = " + value_toml + "\n"
        self.file.write_text("".join(lines))

    def test_string_policy_still_loads(self):
        self._rewrite_policy('"policies/worker.md"')
        role = load(self.file).roles["worker"]
        self.assertEqual(len(role.policy), 1)
        self.assertTrue(role_policy_text(role).strip())

    def test_list_concatenates_in_order(self):
        shared = self.file.parent / "policies" / "shared.md"
        worker = self.file.parent / "policies" / "worker.md"
        shared.write_bytes(b"# Shared\n")
        worker.write_bytes(b"# Worker\n")
        self._rewrite_policy('["policies/shared.md", "policies/worker.md"]')
        config = load(self.file)
        text = role_policy_text(config.roles["worker"])
        self.assertEqual(text, "# Shared\n# Worker\n")
        self.assertLess(text.index("Shared"), text.index("Worker"))

    def test_system_context_appends_exact_pinned_charter_after_ordered_policy(self):
        shared = self.file.parent / "policies" / "shared.md"
        shared.write_bytes(b"# Shared\n")
        self._rewrite_policy('["policies/shared.md", "policies/worker.md"]')
        role = load(self.file).roles["worker"]
        goal = "  # Operator charter\r\nKeep \u03bb exact.\n\n"
        self.assertEqual(system_context_text(role, goal),
                         role_policy_text(role) + "\n# Project charter\n\n" + goal)

    def test_charter_only_change_rekeys_only_non_pi_engines(self):
        from mizu.engine_config import effective, session_record, save_session
        before = self.context("worker")
        settings = effective(self.config, before.role, before.role.profile)
        paths = {}
        for engine in ("pi", "codex", "claude", "pi-durable"):
            options = {**settings, "engine": engine, "session": "persistent"}
            paths[engine], _ = session_record(before, before.role.profile, options)
            save_session(paths[engine], "old-charter-session", {})
        (self.project.root / "PROJECT.md").write_text("A changed operator charter\n")
        after = self.context("worker")
        self.assertNotEqual(before.goal_digest, after.goal_digest)
        for engine, original in paths.items():
            with self.subTest(engine=engine):
                options = {**settings, "engine": engine, "session": "persistent"}
                path, saved = session_record(after, after.role.profile, options)
                if engine in ("pi", "pi-durable"):
                    # Both Pi engines now apply authority live, through distinct SDK paths.
                    self.assertEqual(path, original)
                    self.assertEqual(saved["id"], "old-charter-session")
                else:
                    self.assertNotEqual(path, original)
                    self.assertIsNone(saved)

    def test_missing_part_fails_closed(self):
        self._rewrite_policy('["policies/absent.md", "policies/worker.md"]')
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_oversize_part_fails_closed(self):
        big = self.file.parent / "policies" / "big.md"
        big.write_bytes(b"x" * 70000)
        self._rewrite_policy('["policies/big.md"]')
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_non_string_policy_refused(self):
        self._rewrite_policy('42')
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_session_identity_changes_with_any_part(self):
        from mizu.engine_config import effective, session_record
        ctx = self.context("worker")
        # Non-Pi engines retain the content-bound policy identity.
        settings = {**effective(self.config, ctx.role, ctx.role.profile), "engine": "codex"}
        first, _ = session_record(ctx, ctx.role.profile, settings)
        # Rewrite the underlying file; the same Role tuple now digests differently.
        target = self.config.roles["worker"].policy[0]
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n# extra\n")
            second, _ = session_record(ctx, ctx.role.profile, settings)
            self.assertNotEqual(first, second)
            self.assertNotEqual(
                digest(role_policy_bytes(self.config.roles["worker"])),
                digest(original),
            )
        finally:
            target.write_bytes(original)

    def test_started_digest_covers_composed_text(self):
        shared = self.file.parent / "policies" / "shared.md"
        shared.write_bytes(b"# Shared\n")
        self._rewrite_policy('["policies/shared.md", "policies/worker.md"]')
        config = load(self.file)
        text = role_policy_text(config.roles["worker"])
        self.assertIn("Shared", text)
        self.assertIn("Own the integration", text)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
