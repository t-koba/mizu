"""A role receives the optional prompt sections its capabilities can use."""
import dataclasses
import json
from unittest.mock import patch

from support import Fixture
from mizu import prompt_sections
from mizu.errors import ConfigError
from mizu.unit_prompts import prompt_for, prompt_delta_for

WORK = ("work_items_read", "work_items_create", "work_items_transition",
        "work_items_legacy_read", "work_items_reconcile", "work_order")


class PromptSectionTests(Fixture):
    def judge(self, **extra):
        ctx = self.context("reviewer")
        caps = tuple(c for c in ctx.role.capabilities if c not in WORK + ("submit_insight", "decide")) + ("check_job",)
        ctx.role = dataclasses.replace(ctx.role, capabilities=caps, **extra)
        return ctx

    def keys(self, ctx, builder=prompt_for):
        return set(json.loads(builder(ctx) if builder is prompt_for else builder(ctx, [])))

    def test_role_without_work_capability_gets_no_work_inventory(self):
        ctx = self.judge()
        keys = self.keys(ctx)
        self.assertNotIn("open_work_items", keys)
        self.assertNotIn("ready_work", keys)
        self.assertIn("check_jobs", keys)  # check_job is held
        self.assertNotIn("open_work_items", self.keys(ctx, prompt_delta_for))
        with_inventory = self.context("reviewer")
        full = len(prompt_for(with_inventory))
        self.assertLess(len(prompt_for(ctx)), full)

    def test_producers_deciders_and_writer_receive_bounded_work_load(self):
        from mizu.config import load
        self.file.write_text(self.file.read_text().replace("work_items_prompt = 20", "work_items_prompt = 1"))
        self.config = load(self.file)
        self.project.config = self.config
        for index in range(2):
            self.project.work_items.create(idempotency_key=str(index), title="Existing", actor="worker")
        for capability in ("submit_insight", "decide"):
            ctx = self.judge()
            ctx.role = dataclasses.replace(ctx.role, capabilities=(capability,))
            for builder, args in ((prompt_for, ()), (prompt_delta_for, ([],))):
                payload = json.loads(builder(ctx, *args))
                page = payload["open_work_items"]
                self.assertEqual((len(page["items"]), page["total"], page["overflow_count"]), (1, 2, 1))
                self.assertNotIn("ready_work", payload)
        for role in ("worker", "searcher"):
            page = json.loads(prompt_for(self.context(role)))["open_work_items"]
            self.assertEqual((len(page["items"]), page["total"]), (1, 2))

    def test_every_key_belongs_to_one_section_or_is_fixed(self):
        self.assertNotIn("task", prompt_sections.FIXED_KEYS)
        seen = [k for s in prompt_sections.SECTIONS.values() for k in s.keys]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertFalse(set(seen) & prompt_sections.FIXED_KEYS)
        for role in ("worker", "reviewer", "reporter"):
            ctx = self.context(role)
            with patch.object(prompt_sections, "shape", lambda c, p: p):
                every = set(json.loads(prompt_for(ctx)))
            self.assertLessEqual(every, set(seen) | prompt_sections.FIXED_KEYS, role)

    def test_override_restores_and_excludes(self):
        restored = self.judge(context_include=("open_work_items", "ready_work"))
        self.assertLessEqual({"open_work_items", "ready_work"}, self.keys(restored))
        removed = self.judge(context_exclude=("check_jobs",))
        self.assertNotIn("check_jobs", self.keys(removed))
        self.assertNotIn("shared_checks", self.keys(removed))
        self.assertEqual(removed.role.name, "reviewer")

    def test_writer_prompt_is_byte_identical_to_the_unfiltered_projection(self):
        for builder, args in ((prompt_for, ()), (prompt_delta_for, ([],))):
            ctx = self.context("worker")
            clock = {"now": "2026-01-01T00:00:00+00:00", "timezone": "UTC"}
            with patch("mizu.unit_prompts._prompt_clock", return_value=clock):
                with patch.object(prompt_sections, "shape", lambda c, p: p):
                    old = builder(ctx, *args)
                self.assertEqual(builder(ctx, *args), old)

    def test_excluded_rows_do_not_count_as_shown(self):
        ctx = self.judge()
        prompt_for(ctx)
        self.assertEqual(ctx.prompt_row_fingerprints["open_work_items"], {})

    def test_config_validation(self):
        for bad in ({"include": ["nope"]}, {"include": ["run"]}, {"exclude": ["decision_events"]},
                    {"include": ["ci"], "exclude": ["ci"]}, {"include": ["ci", "ci"]}, {"x": []},
                    {"include": "ci"}, []):
            with self.assertRaises(ConfigError):
                prompt_sections.validate("r", bad)
        self.assertEqual(prompt_sections.validate("r", {"include": ["ci"], "exclude": ["report"]}),
                         (("ci",), ("report",)))

    def test_load_reads_the_role_table(self):
        from mizu.config import load
        path = self.root / "config" / "sections.toml"
        text = self.file.read_text().replace(
            "[roles.reviewer]\n", '[roles.reviewer]\ncontext_sections = { include = ["ready_work"], exclude = ["check_jobs"] }\n', 1)
        path.write_text(text)
        role = load(path).roles["reviewer"]
        self.assertEqual((role.context_include, role.context_exclude), (("ready_work",), ("check_jobs",)))
        path.write_text(text.replace('"ready_work"', '"bogus"'))
        with self.assertRaises(ConfigError):
            load(path)
