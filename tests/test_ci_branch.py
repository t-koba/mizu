"""Operator-selected CI/reporting branch context (no guessing, no hardcode)."""
import dataclasses
import json
import time
import unittest
from unittest.mock import patch
from support import Fixture, ScriptDriver
from mizu import ci_wake
from mizu.config import load
from mizu.errors import ConfigError
from mizu.fs import mkdir, read_json, write_json
from mizu.runtime import Context, Engine, prompt_delta_for, prompt_for, session_prompt


def _configured(fixture, branch):
    cfg = dataclasses.replace(fixture.config, vcs={**fixture.config.vcs, "ci_branch": branch})
    roles = dict(cfg.roles)
    roles["reporter"] = dataclasses.replace(roles["reporter"], capabilities=tuple(list(roles["reporter"].capabilities) + ["vcs_read"]))
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
        self.assertEqual(json.loads(prompt_delta_for(_context(self, cfg, "reporter")))["ci_branch"], "mizu")

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

    def _with_extra_branches_line(self, line):
        lines = [ln for ln in self.file.read_text().splitlines(keepends=True)
                 if "ci_wake_extra_branches" not in ln]
        at = next(i for i, ln in enumerate(lines) if ln.strip() == "[vcs]") + 1
        lines.insert(at, line + "\n")
        self.file.write_text("".join(lines))

    def test_extra_wake_branches_default_and_validation(self):
        self.assertEqual(self.config.vcs["ci_wake_extra_branches"], ())
        original = self.file.read_text()
        self.file.write_text("".join(
            line for line in original.splitlines(keepends=True)
            if "ci_wake_extra_branches" not in line))
        self.assertEqual(load(self.file).vcs["ci_wake_extra_branches"], ())
        self.file.write_text(original)
        self._with_extra_branches_line('ci_wake_extra_branches = ["release/next", "mizu"]')
        self.assertEqual(load(self.file).vcs["ci_wake_extra_branches"],
                         ("release/next", "mizu"))
        for bad in ('ci_wake_extra_branches = "release"',
                    'ci_wake_extra_branches = ["bad branch"]',
                    'ci_wake_extra_branches = ["../escape"]',
                    'ci_wake_extra_branches = [' + ", ".join('"b%s"' % i for i in range(65)) + ']'):
            self._with_extra_branches_line(bad)
            with self.assertRaises(ConfigError, msg=bad):
                load(self.file)


RED_SHA = "a" * 40


def _red_config(fixture, **role_caps):
    import dataclasses
    from mizu.fs import write_json
    cfg = dataclasses.replace(fixture.config, vcs={**fixture.config.vcs, "ci_branch": "mizu"})
    write_json(fixture.project.root / "vcs-poll.json",
               {"failures": {"mizu": [{"sha": RED_SHA, "check": "Offline checks"}]}})
    return cfg


class CiWakeTests(Fixture):
    def test_ci_wake_defaults_to_writable_roles(self):
        self.assertTrue(self.config.roles["worker"].ci_wake)
        self.assertFalse(self.config.roles["searcher"].ci_wake)
        self.assertFalse(self.config.roles["reporter"].ci_wake)
        self.assertFalse(self.config.roles["consult"].ci_wake)

    def test_ci_wake_load_explicit_and_refused(self):
        import dataclasses
        from mizu.config import load
        from mizu.errors import ConfigError
        text = self.file.read_text()
        lines = text.splitlines(keepends=True)
        start = next(i for i, line in enumerate(lines) if line.strip() == "[roles.searcher]")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        lines.insert(end, "ci_wake = true\n")
        self.file.write_text("".join(lines))
        self.assertTrue(load(self.file).roles["searcher"].ci_wake)
        self.file.write_text(text)
        lines = self.file.read_text().splitlines(keepends=True)
        start = next(i for i, line in enumerate(lines) if line.strip() == "[roles.worker]")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        lines.insert(end, "ci_wake = false\n")
        self.file.write_text("".join(lines))
        self.assertFalse(load(self.file).roles["worker"].ci_wake)
        lines.insert(end, 'ci_wake = "yes"\n')
        self.file.write_text("".join(lines))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_red_skips_event_trigger_without_ci_wake(self):
        import dataclasses
        from mizu.runtime import Engine
        from support import ScriptDriver
        config = _red_config(self)
        # The fixture starts with an operator-wake token for writer tests;
        # this case isolates foreign CI with no observer wake pending.
        self.project.set_control(wake_generation="")
        driver = ScriptDriver()
        engine = Engine(config, driver=driver)
        # A read role with no CI subscription spends nothing on foreign red:
        # no events of its own, so the trigger exits before any model use.
        skipped = engine.run(self.project, "searcher", event_trigger=True)
        self.assertEqual(skipped.get("skipped"), "no-event")
        self.assertEqual(driver.calls, [])

    def test_red_admits_writer_and_labels_ci(self):
        from mizu.fs import read_json
        from mizu.runtime import Engine
        from support import ScriptDriver
        config = _red_config(self)
        admitted = Engine(config, driver=ScriptDriver()).run(
            self.project, "worker", event_trigger=True)
        self.assertEqual(admitted["status"], "completed")
        started = read_json(self.project.root / "runs" / admitted["run"] / "started.json")
        self.assertEqual(started["admission"], "ci")

    def test_writer_opt_out_skips_red(self):
        import dataclasses
        from mizu.runtime import Engine
        from support import ScriptDriver
        config = _red_config(self)
        roles = dict(config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], ci_wake=False)
        config = dataclasses.replace(config, roles=roles)
        skipped = Engine(config, driver=ScriptDriver()).run(
            self.project, "worker", event_trigger=True)
        self.assertEqual(skipped.get("skipped"), "no-event")

    def test_read_role_opt_in_admits_red(self):
        import dataclasses
        from mizu.fs import read_json
        from mizu.runtime import Engine
        from support import ScriptDriver
        config = _red_config(self)
        roles = dict(config.roles)
        roles["searcher"] = dataclasses.replace(roles["searcher"], ci_wake=True)
        config = dataclasses.replace(config, roles=roles)
        admitted = Engine(config, driver=ScriptDriver()).run(
            self.project, "searcher", event_trigger=True)
        self.assertEqual(admitted["status"], "completed")
        started = read_json(self.project.root / "runs" / admitted["run"] / "started.json")
        self.assertEqual(started["admission"], "ci")

    def test_should_run_parks_writer_without_ci_wake(self):
        import dataclasses
        import time
        from mizu.runtime import should_run
        config = _red_config(self)
        cap = self.project.snapshots.capture_files(self.project.workspace)
        wait = self.project.snapshots.create(
            cap, goal=self.project.goal, state="Parked.", run=None,
            outcome="wait", summary="Parked on red.", verification=None,
            wake_generation=self.project.control().get("wake_generation", ""))
        self.project.snapshots.publish(wait)
        snap = self.project.snapshots.get()
        self.assertTrue(should_run(self.project, snap, current_time=time.time(),
                                  role_name="worker", config=config))
        roles = dict(config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], ci_wake=False)
        config = dataclasses.replace(config, roles=roles)
        self.assertFalse(should_run(self.project, snap, current_time=time.time(),
                                   role_name="worker", config=config))


class CiWakeSettlementTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = _red_config(self)
        self.project.set_control(wake_generation="")
        self.head = self.project.snapshots.get()["id"]
        self.fixed_now = time.time()
        clock = patch("mizu.runtime.time.time", return_value=self.fixed_now)
        clock.start()
        self.addCleanup(clock.stop)
        self.rows, self.facts = self._due()
        self.assertTrue(self.rows)

    def _due(self):
        return ci_wake._ci_due_for_with_facts(
            self.project, self.config, "worker", self.head, self.fixed_now)

    def _offer(self):
        """Capture settlement at driver entry, before publication can refresh it."""
        driver = ScriptDriver()
        execute = driver.execute
        offered = []

        def capture(context, prompt, *, profile=None):
            offered.append((ci_wake.read_ci_wake(self.project, "worker"),
                            json.loads(prompt), self._due()[0]))
            return execute(context, prompt, profile=profile)

        with patch.object(driver, "execute", side_effect=capture):
            result = Engine(self.config, driver=driver).run(
                self.project, "worker", event_trigger=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(offered), 1)
        return result, offered[0]

    def _assert_frozen_record(self, record):
        self.assertIsNotNone(record)
        self.assertEqual(record["snapshot"], self.head)
        self.assertEqual(record["fact_sha256"],
                         ci_wake._ci_wake_fact_digest(self.rows, ("mizu",)))

    def test_pre_offer_failure_does_not_consume_wake(self):
        driver = ScriptDriver()
        sentinel = RuntimeError("prompt construction failed")
        with patch("mizu.runtime.session_prompt", side_effect=sentinel):
            with self.assertRaises(RuntimeError) as raised:
                Engine(self.config, driver=driver).run(
                    self.project, "worker", event_trigger=True)
        self.assertIs(raised.exception, sentinel)
        self.assertEqual(driver.calls, [])
        self.assertIsNone(ci_wake.read_ci_wake(self.project, "worker"))
        self.assertTrue(self._due()[0])

    def test_opening_offer_settles_wake(self):
        _, (record, opening, due) = self._offer()
        self._assert_frozen_record(record)
        self.assertEqual(opening["external_check_wake"]["facts"], self.facts)
        self.assertEqual(due, [])

    def test_resumed_delta_offer_settles_wake(self):
        from mizu.engine_config import effective, save_session, session_record

        first = Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        self.assertEqual(first["status"], "completed")
        self.head = self.project.snapshots.get()["id"]
        context = _context(self, self.config, "worker")
        settings = effective(self.config, context.role, context.role.profile)
        path, _ = session_record(context, context.role.profile, settings)
        save_session(path, "test-session", {"tokens": 0})
        # A new CI run wakes the resumed conversation despite the first settlement.
        write_json(self.project.root / "vcs-poll.json", {"failures": {"mizu": [
            {"sha": RED_SHA, "check": "Offline checks", "run": "attempt-2"}]}})
        self.rows, self.facts = self._due()
        self.assertTrue(self.rows)
        prior = ci_wake.read_ci_wake(self.project, "worker")
        result, (record, opening, due) = self._offer()
        self._assert_frozen_record(record)
        self.assertNotEqual(record["fact_sha256"], prior["fact_sha256"])
        self.assertEqual(opening["external_check_wake"]["facts"], self.facts)
        self.assertEqual(due, [])
        projection = read_json(self.project.root / "runs" / result["run"] / "prompt_projection.json")
        self.assertEqual(projection["prompt_mode"], "delta")

    def test_cache_change_during_prompt_construction_keeps_new_observation_due(self):
        new_sha = "b" * 40

        def change_cache(*args, **kwargs):
            built = session_prompt(*args, **kwargs)
            write_json(self.project.root / "vcs-poll.json", {"failures": {"mizu": [
                {"sha": new_sha, "check": "Offline checks", "run": "attempt-2"}]}})
            return built

        with patch("mizu.runtime.session_prompt", side_effect=change_cache):
            _, (record, opening, due) = self._offer()
        self._assert_frozen_record(record)
        self.assertEqual(opening["external_check_wake"]["facts"], self.facts)
        self.assertTrue(due)
        self.assertEqual({row["sha"] for row in due}, {new_sha})
        self.assertNotEqual(record["fact_sha256"],
                            ci_wake._ci_wake_fact_digest(due, ("mizu",)))
        self.head = self.project.snapshots.get()["id"]
        self.assertTrue(self._due()[0])

    def test_missing_or_altered_fact_prevents_settlement(self):
        fact = self.facts[0]
        cover = ci_wake.wake_facts_cover_due
        for offered in ([], [{**fact, "non_passing_checks": 2}],
                        [{**fact, "may_be_incomplete": not fact["may_be_incomplete"]}]):
            with self.subTest(offered=offered):
                self.assertFalse(cover(offered, [fact]))
        self.assertTrue(cover([{**fact, "revision": "b" * 40}, fact], [fact]))
        for offered, frozen in (([fact], []), (None, [fact]), ([fact], None),
                                ([fact], (fact,)), ((fact,), [fact]),
                                ([fact], [fact, {}]), ([{}], [fact]),
                                ([fact], [{**fact, "extra": True}]),
                                ([fact], [{**fact, "non_passing_checks": True}])):
            with self.subTest(offered=offered, frozen=frozen):
                self.assertFalse(cover(offered, frozen))
        self.assertTrue(cover([None, {}, fact], [fact]))
        other = {**fact, "subject": "release"}
        self.assertFalse(cover([fact], [fact, other]))
        self.assertTrue(cover([other, fact], [fact, other]))


if __name__ == "__main__":
    unittest.main()
