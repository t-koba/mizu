"""Operator-selected CI/reporting branch context (no guessing, no hardcode)."""
import dataclasses
import json
import unittest
from support import Fixture
from mizu.config import load
from mizu.errors import ConfigError
from mizu.fs import mkdir
from mizu.runtime import Context, prompt_delta_for, prompt_for


def _configured(fixture, branch):
    cfg = dataclasses.replace(fixture.config, vcs={**fixture.config.vcs, "ci_branch": branch})
    roles = dict(cfg.roles)
    roles["reporter"] = dataclasses.replace(roles["reporter"], engine_tools=("vcs_read",))
    return dataclasses.replace(cfg, roles=roles)


def _context(fixture, config, name):
    run = fixture.project.root / "runs" / "ci-branch-probe"
    mkdir(run)
    role = config.roles[name]
    snap = fixture.project.snapshots.get()
    return Context(config, fixture.project, role, run, snap, fixture.project.workspace)


class CiBranchTests(Fixture):
    def test_unset_branch_reads_null_everywhere(self):
        self.assertEqual(self.config.vcs["ci_branch"], "")
        self.assertIsNone(json.loads(prompt_for(self.context("reporter")))["ci_branch"])
        self.assertIsNone(json.loads(prompt_for(self.context("worker")))["ci_branch"])

    def test_branch_offered_only_to_vcs_read_roles(self):
        cfg = _configured(self, "mizu")
        self.assertEqual(json.loads(prompt_for(_context(self, cfg, "reporter")))["ci_branch"], "mizu")
        self.assertIsNone(json.loads(prompt_for(_context(self, cfg, "worker")))["ci_branch"])
        self.assertEqual(json.loads(prompt_delta_for(_context(self, cfg, "reporter"), []))["ci_branch"], "mizu")

    def _with_branch_line(self, line):
        text = self.file.read_text()
        lines = [ln for ln in text.splitlines(keepends=True) if "ci_branch" not in ln]
        if "[vcs]\n" in lines:
            at = lines.index("[vcs]\n") + 1
            lines.insert(at, line + "\n")
        else:
            lines += ["[vcs]\n", line + "\n"]
        self.file.write_text("".join(lines))

    def test_load_accepts_and_refuses_branch_values(self):
        self._with_branch_line('ci_branch = "mizu"')
        self.assertEqual(load(self.file).vcs["ci_branch"], "mizu")
        for bad in ('ci_branch = "has space"', 'ci_branch = "has\\nnewline"', "ci_branch = 3",
                    'ci_branch = "' + "x" * 257 + '"'):
            self._with_branch_line(bad)
            with self.assertRaises(ConfigError):
                load(self.file)


if __name__ == "__main__":
    unittest.main()
