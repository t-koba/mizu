"""Consultation gate ownership after the engine delegation split.

The gate stays verbatim in its owner module; runtime keeps the identity
re-export used by Engine.consult, smoke --live and every existing caller.
"""
import unittest
from types import SimpleNamespace

from mizu import engine_delegation, runtime
from mizu.errors import ConfigError


class ConsultGateOwnerTests(unittest.TestCase):
    def test_runtime_reexports_the_owner_gate_and_set_by_identity(self):
        self.assertIs(runtime.check_consult_role,
                      engine_delegation.check_consult_role)
        self.assertIs(runtime.CONSULT_FORBIDDEN,
                      engine_delegation.CONSULT_FORBIDDEN)

    def test_gate_refuses_writable_and_forbidden_roles(self):
        for role in (SimpleNamespace(workspace="write",
                                     capabilities=("files", "read", "finish")),
                     SimpleNamespace(workspace="read",
                                     capabilities=("files", "read", "finish", "decide"))):
            with self.subTest(role=role):
                with self.assertRaisesRegex(ConfigError, "must be read-only without"):
                    runtime.check_consult_role("bad", role)

    def test_gate_accepts_a_read_only_allowed_role(self):
        role = SimpleNamespace(workspace="read",
                               capabilities=("files", "read", "finish", "insights", "diff"))
        runtime.check_consult_role("advisor", role)


if __name__ == "__main__":
    unittest.main()
