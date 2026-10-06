"""Offline audit over retained run records: what receipts prove, and what they cannot.

Fake drivers are explicitly used; these tests make no live inference claim.
Synthetic violating run directories prove each violated verdict; a genuine
ScriptDriver run proves the auditor accepts real receipts and writes nothing.
"""
import os
from support import Fixture, ScriptDriver
from mizu.errors import Denied
from mizu.fs import read_json, write_json
from mizu.runtime import Engine
from mizu.trajectory import audit_run


def _verdict(report, check):
    return next(item for item in report["checks"] if item["check"] == check)["verdict"]


class TrajectoryAuditTests(Fixture):
    def _run_dir(self, role="worker", **finish):
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, role)
        return self.project.root / "runs" / result["run"]

    def _snapshot_files(self, run_dir):
        found = []
        for root, _, files in os.walk(run_dir):
            for name in files:
                found.append(os.path.join(root, name))
        return {path: open(path, "rb").read() for path in found}

    def test_genuine_run_holds_and_audit_writes_nothing(self):
        run_dir = self._run_dir()
        before = self._snapshot_files(run_dir)
        report = audit_run(run_dir, read_roles=("searcher", "reviewer"))
        self.assertEqual(report["violated"], 0)
        self.assertEqual(_verdict(report, "admission_recorded"), "held")
        self.assertEqual(_verdict(report, "sealed_after_finish"), "held")
        # Structural limits are reported, never invented.
        self.assertEqual(_verdict(report, "engine_tool_calls_receipted"), "unverifiable")
        self.assertEqual(self._snapshot_files(run_dir), before)

    def test_tool_after_finish_is_violated(self):
        run_dir = self._run_dir()
        intent = read_json(run_dir / "finish-intent.json")
        write_json(run_dir / "commands" / "late.json",
                   {"id": "late", "kind": "command", "role": "worker",
                    "created_at": intent["recorded_at"], "finished_at": intent["recorded_at"],
                    "writable": True})
        report = audit_run(run_dir)
        self.assertEqual(report["violated"], 1)
        self.assertEqual(_verdict(report, "sealed_after_finish"), "violated")

    def test_read_role_writable_command_is_violated(self):
        run_dir = self._run_dir(role="reviewer")
        started = read_json(run_dir / "started.json")
        self.assertEqual(started["role"], "reviewer")
        write_json(run_dir / "commands" / "op.json",
                   {"id": "op", "kind": "command", "role": "reviewer",
                    "created_at": started["started_at"], "finished_at": started["started_at"],
                    "writable": True})
        self.assertEqual(_verdict(audit_run(run_dir, read_roles=("reviewer",)),
                                  "read_role_no_writable_commands"), "violated")
        self.assertEqual(_verdict(audit_run(run_dir), "read_role_no_writable_commands"), "held")

    def test_contradictory_verification_is_violated(self):
        run_dir = self._run_dir()
        write_json(run_dir / "verification.json",
                   {"passed": True, "unchanged_during_verification": False,
                    "code_digest": "abc", "run": run_dir.name, "created_at": "2026-10-06T00:00:00+00:00"})
        self.assertEqual(_verdict(audit_run(run_dir), "verification_consistent"), "violated")

    def test_done_without_proof_is_violated(self):
        run_dir = self._run_dir()
        result = read_json(run_dir / "result.json")
        result["finish"] = {**result["finish"], "outcome": "done"}
        write_json(run_dir / "result.json", result)
        self.assertFalse((run_dir / "verification.json").exists())
        self.assertEqual(_verdict(audit_run(run_dir), "verification_consistent"), "violated")

    def test_terminal_refusal_reads_as_held(self):
        run_dir = self._run_dir()
        (run_dir / "finish-intent.json").unlink()
        write_json(run_dir / "error.json",
                   {"run": run_dir.name, "role": "worker",
                    "error": "Denied: this work unit is sealed; no tools may run after finish"})
        self.assertEqual(_verdict(audit_run(run_dir), "sealed_after_finish"), "held")

    def test_missing_directory_is_refused(self):
        with self.assertRaises(Denied):
            audit_run(self.root / "no-such-run")
