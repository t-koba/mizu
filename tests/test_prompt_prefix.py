"""The standing request prefix is identical across units; volatile data follows it."""
import dataclasses
import json
import sys
import time
from pathlib import Path

from support import Fixture
from mizu.pi import PiDriver, request_prefix, PROMPT_CWD, cache_key
from mizu.protocol import tool_definitions
from mizu.unit_prompts import prompt_for

ROOT = Path(__file__).resolve().parents[1]
MARKER = "\n--- unit prompt ---\n"


class PromptPrefixTests(Fixture):
    def unit(self, role="reviewer"):
        cfg = dataclasses.replace(self.config, engines={**self.config.engines, "pi": {
            **self.config.engines["pi"], "command": (sys.executable, str(ROOT / "tests/fake_pi.py"))}})
        ctx = self.context(role)
        prompt = prompt_for(ctx)
        PiDriver(cfg).execute(ctx, prompt)
        effective = json.loads((ctx.run_dir / "pi-effective.json").read_text())
        tools = tool_definitions(ctx.role.capabilities)
        return ctx, effective, request_prefix(effective, tools) + MARKER + prompt, prompt

    def test_prefix_is_byte_identical_across_units_and_volatile_data_follows_it(self):
        ctx1, eff1, full1, prompt1 = self.unit()
        time.sleep(1.1)  # clock differs
        ctx2, eff2, full2, prompt2 = self.unit()
        self.assertNotEqual(ctx1.run_dir.name, ctx2.run_dir.name)
        head1, head2 = full1.split(MARKER)[0], full2.split(MARKER)[0]
        self.assertEqual(head1, head2)
        self.assertNotEqual(prompt1, prompt2)
        # Standing instructions and tools are in the prefix.
        from mizu.config import role_policy_parts
        for part in role_policy_parts(ctx1.role):
            self.assertIn(part, head1)
        self.assertIn(ctx1.goal, head1)
        for tool in tool_definitions(ctx1.role.capabilities):
            self.assertIn(tool["name"], head1)
        self.assertIn(f"<cwd>\n{PROMPT_CWD}\n</cwd>", head1)
        # Volatile data (run id, per-run paths, clock) appears only after the marker.
        for ctx in (ctx1, ctx2):
            self.assertNotIn(ctx.run_dir.name, head1)
            self.assertNotIn(str(ctx.run_dir), head1)
        self.assertIn(ctx2.run_dir.name, full2.split(MARKER)[1])
        self.assertNotIn(ctx1.run_dir.name, full2)
        self.assertEqual(eff1["cacheKey"], eff2["cacheKey"])
        self.assertEqual(eff1["promptCwd"], PROMPT_CWD)

    def test_cache_key_is_stable_per_project_role_profile_model(self):
        model = {"provider": "p", "model": "m"}
        base = cache_key("proj", "worker", "main", model)
        self.assertEqual(base, cache_key("proj", "worker", "main", dict(model)))
        self.assertRegex(base, r"^[0-9a-f]{32}$")
        for other in (cache_key("proj2", "worker", "main", model), cache_key("proj", "reviewer", "main", model),
                      cache_key("proj", "worker", "alt", model), cache_key("proj", "worker", "main", {"provider": "p", "model": "n"})):
            self.assertNotEqual(base, other)
