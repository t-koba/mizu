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

    def test_event_trigger_labels_substantive_reason(self):
        import dataclasses
        from mizu.fs import read_json
        from mizu.runtime import Engine
        from support import ScriptDriver
        config = _red_config(self)
        roles = dict(config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"], decision_events=("reject",))
        config = dataclasses.replace(config, roles=roles)
        item = self.project.insights.submit(
            source="worker", title="Finding", body="v1",
            base_snapshot=self.project.snapshots.get()["id"])
        self.project.insights.decide(item["id"], "reject", "not material", "", "test", expected_rev=1)
        admitted = Engine(config, driver=ScriptDriver()).run(
            self.project, "worker", event_trigger=True)
        self.assertEqual(admitted["status"], "completed")
        started = read_json(self.project.root / "runs" / admitted["run"] / "started.json")
        self.assertEqual(started["admission"], "decision")

    def test_should_run_parks_writer_without_ci_wake(self):
        import dataclasses
        import time
        from mizu.runtime import should_run
        config = _red_config(self)
        gen = self.project.insights.generation()
        cap = self.project.snapshots.capture_files(self.project.workspace)
        wait = self.project.snapshots.create(
            cap, goal=self.project.goal, state="Parked.", run=None,
            outcome="wait", summary="Parked on red.", verification=None,
            inbox_seen=gen,
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


if __name__ == "__main__":
    unittest.main()
