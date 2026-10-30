"""The container sweep never removes a live role's containers."""
import shutil
import unittest
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.admission import role_exclusion
from mizu.errors import Busy, Denied
from mizu.fs import held, lock
from mizu.process import Result
from mizu.runtime import Engine
from mizu.sandbox import cleanup


class RoleCleanupTests(Fixture):
    """A sibling service's cleanup leaves a running role's containers alone.

    The regression is controlled: the container runtime is mocked, so no
    container is created, listed or removed on the host. Only the argv the
    sweep would issue is observed.
    """

    def _runtime(self, calls):
        def fake(argv, **_):
            calls.append(list(argv))
            if "rm" in argv:
                return Result(0, "", "", "exited", 0.0)
            return Result(0, "abc123 def456\n", "", "exited", 0.0)
        return fake

    def _removals(self, calls):
        return [argv for argv in calls if "rm" in argv]

    def test_a_sibling_cleanup_leaves_a_live_role_alone(self):
        calls = []
        exclusion = role_exclusion(self.project.root, "worker")
        with lock(exclusion, blocking=False):
            with patch("mizu.sandbox.run", side_effect=self._runtime(calls)):
                result = cleanup(self.config, self.project.root, "worker")
        self.assertEqual(result, {"removed": 0, "skipped": "role-active"})
        self.assertEqual(self._removals(calls), [])

    def test_orphans_are_removed_once_the_owner_exits(self):
        calls = []
        with patch("mizu.sandbox.run", side_effect=self._runtime(calls)):
            result = cleanup(self.config, self.project.root, "worker")
        self.assertEqual(result, {"removed": 2})
        removals = self._removals(calls)
        self.assertEqual(len(removals), 1)
        self.assertIn("abc123", removals[0])
        self.assertIn("def456", removals[0])

    def test_the_owner_sweeps_its_own_role(self):
        calls = []
        exclusion = role_exclusion(self.project.root, "worker")
        with lock(exclusion, blocking=False):
            with patch("mizu.sandbox.run", side_effect=self._runtime(calls)):
                result = cleanup(self.config, self.project.root, "worker", owner=True)
        self.assertEqual(result, {"removed": 2})
        self.assertEqual(len(self._removals(calls)), 1)

    def test_a_project_sweep_refuses_while_one_role_is_active(self):
        calls = []
        exclusion = role_exclusion(self.project.root, "worker")
        with lock(exclusion, blocking=False):
            with patch("mizu.sandbox.run", side_effect=self._runtime(calls)):
                result = cleanup(self.config, self.project.root, None)
        self.assertEqual(result, {"removed": 0, "skipped": "role-active"})
        self.assertEqual(self._removals(calls), [])

    def test_a_project_sweep_refuses_while_a_delegated_role_is_live(self):
        # A delegated child's role need not be enabled for the project, so
        # the sweep must exclude the roles that hold an exclusion, not the
        # project's enabled roles.
        calls = []
        self.assertNotIn("maintainer", self.project.roles)
        exclusion = role_exclusion(self.project.root, "maintainer")
        with lock(exclusion, blocking=False):
            with patch("mizu.sandbox.run", side_effect=self._runtime(calls)):
                result = cleanup(self.config, self.project.root, None)
        self.assertEqual(result, {"removed": 0, "skipped": "role-active"})
        self.assertEqual(self._removals(calls), [])

    def test_a_project_sweep_runs_when_no_role_is_active(self):
        calls = []
        with patch("mizu.sandbox.run", side_effect=self._runtime(calls)):
            result = cleanup(self.config, self.project.root, None)
        self.assertEqual(result, {"removed": 2})
        self.assertEqual(len(self._removals(calls)), 1)

    def test_a_project_sweep_holds_a_delegated_role_that_never_ran(self):
        # A delegated child's role need not be enabled for the project, and a
        # role that has never run has no exclusion file, so the sweep must
        # hold every configured role's exclusion: a child that starts inside
        # the sweep's own window must not be able to.
        calls = []
        seen = {}

        def runtime(argv, **_):
            calls.append(list(argv))
            if "rm" in argv:
                return Result(0, "", "", "exited", 0.0)
            seen["held"] = held(role_exclusion(self.project.root, "maintainer"))
            return Result(0, "abc123\n", "", "exited", 0.0)

        with patch("mizu.sandbox.run", side_effect=runtime):
            result = cleanup(self.config, self.project.root, None)
        self.assertTrue(seen["held"])
        self.assertEqual(result, {"removed": 1})

    def test_a_project_sweep_holds_a_role_that_has_not_run_yet(self):
        # A lock file appears when a role takes its exclusion, so a role that
        # has never run has none. The sweep must still hold that role's
        # exclusion, or the role could start while the sweep is listing.
        calls = []
        seen = {}

        def runtime(argv, **_):
            calls.append(list(argv))
            if "rm" in argv:
                return Result(0, "", "", "exited", 0.0)
            seen["held"] = held(role_exclusion(self.project.root, "searcher"))
            return Result(0, "abc123\n", "", "exited", 0.0)

        with patch("mizu.sandbox.run", side_effect=runtime):
            result = cleanup(self.config, self.project.root, None)
        self.assertTrue(seen["held"])
        self.assertEqual(result, {"removed": 1})

    def test_a_project_sweep_refuses_an_unreadable_locks_directory(self):
        calls = []
        locks = self.project.root / "locks"
        shutil.rmtree(locks, ignore_errors=True)
        locks.write_bytes(b"not a directory")
        with patch("mizu.sandbox.run", side_effect=self._runtime(calls)):
            with self.assertRaises(Denied):
                cleanup(self.config, self.project.root, None)
        self.assertEqual(self._removals(calls), [])

    def test_the_storage_maintenance_excludes_a_delegated_role(self):
        from mizu.storage import quiescent
        self.project.set_control(paused=True, reason="Maintenance")
        with lock(role_exclusion(self.project.root, "helper"), blocking=False):
            with self.assertRaises(Busy):
                with quiescent(self.project):
                    pass

    def test_a_live_invocation_holds_the_exclusion_the_sweep_takes(self):
        # The sweep protects the same file the invocation holds. If the two
        # paths ever drift, the sweep would remove a live role's containers
        # with a green suite.
        calls = []
        seen = {}

        def callback(ctx, prompt, profile):
            seen["held"] = held(role_exclusion(self.project.root, ctx.role.name))
            with patch("mizu.sandbox.run", side_effect=self._runtime(calls)):
                seen["project"] = cleanup(self.config, self.project.root, None)
                seen["scoped"] = cleanup(self.config, self.project.root, ctx.role.name)
            ctx.handle("finish", {"outcome": "continue", "summary": "swept",
                                  "state": 'Now: swept.\nNext: Report the result.\nTasks: none\nBlocking: none'})

        result = Engine(self.config, driver=ScriptDriver(callback)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(seen["held"])
        self.assertEqual(seen["project"], {"removed": 0, "skipped": "role-active"})
        self.assertEqual(seen["scoped"], {"removed": 0, "skipped": "role-active"})
        self.assertEqual(self._removals(calls), [])


if __name__ == "__main__":
    unittest.main()
