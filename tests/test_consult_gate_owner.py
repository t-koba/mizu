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


if __name__ == "__main__":
    unittest.main()
