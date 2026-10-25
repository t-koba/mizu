"""Doctor check-job runner diagnostic: missing runner is a failed capability.

A project can grant the ``check_job`` capability with no checks runner
installed, and the queue then sits empty with no defect anywhere. The doctor
names the runner the project needs: on Linux every granted project requires
its full generated runner set (check ``.service``, notification ``.path``,
recovery ``.timer``), and a missing file fails with the exact unit names. On
macOS/Windows the runner is operator-managed and generated definitions are
absent by design (docs/check-jobs.md), so the doctor reports ``not_run``
instead of passing or failing on generated units. With no grant there is
nothing to verify, and unreadable or malformed project settings fail closed
instead of reading as no grant.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from support import Fixture
from mizu import doctor
from mizu.errors import ConfigError


def make_config(root: Path, grants=()) -> SimpleNamespace:
    """A minimal config: ``grants`` names the roles holding check_job."""
    roles = {
        "worker": SimpleNamespace(capabilities=("check_job",) if "worker" in grants else ()),
        "reviewer": SimpleNamespace(capabilities=("check_job",) if "reviewer" in grants else ()),
    }
    return SimpleNamespace(data=root, roles=roles)


def write_project(root: Path, name: str, roles) -> Path:
    target = root / "projects" / name
    target.mkdir(parents=True, exist_ok=True)
    if isinstance(roles, str):
        (target / "project.toml").write_text(roles, encoding="utf-8")
    else:
        (target / "project.toml").write_text(
            "roles = [%s]\n" % ", ".join('"%s"' % role for role in roles),
            encoding="utf-8")
    return target


class CheckRunnerDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mizu-check-runner-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.units = self.root / "units"
        self.units.mkdir()

    def test_no_initialized_project_is_not_run(self):
        config = make_config(self.root, grants={"worker"})
        report = doctor.check_runner(config, self.units, system="linux")
        self.assertEqual(report["state"], "no_grant")
        row = doctor.check_runner_row(config, self.units, system="linux")
        self.assertEqual((row["name"], row["status"]), ("check-job runner", "not_run"))

    def test_project_without_grant_is_not_run(self):
        config = make_config(self.root, grants=set())
        write_project(self.root, "demo", ["worker"])
        # An uninitialized directory carries no settings and is skipped.
        (self.root / "projects" / "scratch").mkdir(parents=True)
        report = doctor.check_runner(config, self.units, system="linux")
        self.assertEqual(report["state"], "no_grant")
        self.assertEqual(
            doctor.check_runner_row(config, self.units, system="linux")["status"], "not_run")

    def test_missing_linux_units_fail_with_exact_names(self):
        config = make_config(self.root, grants={"worker"})
        write_project(self.root, "demo", ["worker"])
        with self.assertRaises(ConfigError) as caught:
            doctor.check_runner(config, self.units, system="linux")
        message = str(caught.exception)
        self.assertIn("mizu-4-demo-checks.service", message)
        self.assertIn("mizu-4-demo-checks.path", message)
        self.assertIn("mizu-4-demo-checks.timer", message)
        row = doctor.check_runner_row(config, self.units, system="linux")
        self.assertEqual(row["status"], "fail")
        self.assertIn("mizu-4-demo-checks.path", row["details"])

    def test_partial_linux_units_still_fail_on_the_missing_ones(self):
        from mizu.services import check_runner_units
        config = make_config(self.root, grants={"worker"})
        write_project(self.root, "demo", ["worker"])
        service, path, timer = check_runner_units("demo")
        (self.units / service).write_text("unit", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            doctor.check_runner(config, self.units, system="linux")
        message = str(caught.exception)
        self.assertIn(path, message)
        self.assertIn(timer, message)

    def test_complete_linux_units_pass_and_report_definitions(self):
        from mizu.services import check_runner_units
        config = make_config(self.root, grants={"worker"})
        write_project(self.root, "demo", ["worker"])
        expected = sorted(check_runner_units("demo"))
        for unit in expected:
            (self.units / unit).write_text("unit", encoding="utf-8")
        report = doctor.check_runner(config, self.units, system="linux")
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["projects"], ["demo"])
        self.assertEqual(sorted(report["units"]), expected)
        row = doctor.check_runner_row(config, self.units, system="linux")
        self.assertEqual(row["status"], "pass")

    def test_manual_platforms_are_not_run_without_touching_units(self):
        config = make_config(self.root, grants={"worker"})
        write_project(self.root, "demo", ["worker"])
        missing = self.root / "does-not-exist"
        for system in ("macos", "windows"):
            report = doctor.check_runner(config, missing, system=system)
            self.assertEqual(report["state"], "manual")
            self.assertEqual(report["projects"], ["demo"])
            self.assertIn("operator-managed", report["detail"])
            self.assertIn("check-jobs", report["detail"])
            # The manual diagnostic never consults the unit directory, so a
            # missing directory still reports not_run rather than failing.
            row = doctor.check_runner_row(config, missing, system=system)
            self.assertEqual(row["status"], "not_run")
            self.assertIn("operator-managed", str(row["details"]))

    def test_malformed_settings_fail_closed_and_name_the_project(self):
        config = make_config(self.root, grants={"worker"})
        write_project(self.root, "demo", "{not toml")
        with self.assertRaises(ConfigError) as caught:
            doctor.check_runner(config, self.units, system="linux")
        self.assertIn("demo", str(caught.exception))
        row = doctor.check_runner_row(config, self.units, system="linux")
        self.assertEqual(row["status"], "fail")
        self.assertIn("demo", row["details"])

    def test_unknown_role_fails_closed(self):
        config = make_config(self.root, grants={"worker"})
        write_project(self.root, "demo", ["ghost"])
        with self.assertRaises(ConfigError) as caught:
            doctor.check_runner(config, self.units, system="linux")
        self.assertIn("demo", str(caught.exception))

    def test_empty_project_roles_fail_closed(self):
        config = make_config(self.root, grants={"worker"})
        write_project(self.root, "demo", [])
        with self.assertRaises(ConfigError) as caught:
            doctor.check_runner(config, self.units, system="linux")
        self.assertIn("demo", str(caught.exception))
        self.assertIn("Project roles", str(caught.exception))

    def test_unknown_project_settings_key_fail_closed(self):
        config = make_config(self.root, grants={"worker"})
        write_project(self.root, "demo", 'roles = ["worker"]\nmisspelled = true\n')
        with self.assertRaises(ConfigError) as caught:
            doctor.check_runner(config, self.units, system="linux")
        self.assertIn("demo", str(caught.exception))
        self.assertIn("Unknown keys", str(caught.exception))

    def test_project_toml_directory_is_not_ignored(self):
        config = make_config(self.root, grants={"worker"})
        project = self.root / "projects" / "demo"
        (project / "project.toml").mkdir(parents=True)
        with self.assertRaises(ConfigError) as caught:
            doctor.check_runner(config, self.units, system="linux")
        self.assertIn("demo", str(caught.exception))

    def test_symlink_project_directory_fails_closed(self):
        config = make_config(self.root, grants={"worker"})
        projects = self.root / "projects"
        projects.mkdir()
        target = self.root / "external-project"
        target.mkdir()
        (target / "project.toml").write_text('roles = ["worker"]\n', encoding="utf-8")
        try:
            (projects / "demo").symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaises(ConfigError) as caught:
            doctor.check_runner(config, self.units, system="linux")
        self.assertIn("demo", str(caught.exception))
        self.assertIn("symlink", str(caught.exception))

    def test_unsupported_platform_fails(self):
        config = make_config(self.root, grants=set())
        with self.assertRaises(ConfigError):
            doctor.check_runner(config, self.units, system="plan9")

    def test_unit_names_share_one_source_of_truth_with_generation(self):
        from mizu.services import check_runner_base, check_runner_units
        self.assertEqual(check_runner_base("sample"), "mizu-6-sample-checks")
        self.assertEqual(check_runner_units("sample"),
                         ("mizu-6-sample-checks.service",
                          "mizu-6-sample-checks.path",
                          "mizu-6-sample-checks.timer"))


class CheckRunnerRenderAgreementTests(Fixture):
    def test_rendered_linux_units_match_the_shared_names(self):
        import dataclasses
        from mizu.services import check_runner_units, render
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], capabilities=(*roles["worker"].capabilities, "check_job"))
        config = dataclasses.replace(self.config, roles=roles)
        units = render(config, self.project, ROOT / "bin/mizu", system="linux")
        for unit in check_runner_units(self.project.name):
            self.assertIn(unit, units)


if __name__ == "__main__":
    unittest.main()
