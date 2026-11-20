"""Delegation resolution owns one mixin module after the runtime split.

The gate, its six resolution methods and their Engine MRO wiring are pinned
here; the behaviour itself stays covered by the delegation and work-order
suites.
"""
import dataclasses

from support import Fixture

from mizu.engine_admission import EngineAdmissionMixin
from mizu.engine_delegation import (
    DELEGATE_FORBIDDEN as GATE_FORBIDDEN,
    EngineDelegationMixin,
    check_delegate_role as gate_check,
)
from mizu.errors import ConfigError
from mizu.runtime import (
    DELEGATE_FORBIDDEN as RUNTIME_FORBIDDEN,
    Engine,
    check_delegate_role as runtime_check,
)

METHODS = (
    "_delegate_role", "_delegate_profile", "_delegation_request",
    "_resolve_delegation_request", "_reserve_delegation", "_selection_retry_wait",
)


class EngineDelegationBoundaryTests(Fixture):
    def test_mixin_owns_every_resolution_method(self):
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIn(name, EngineDelegationMixin.__dict__)
                # Not defined in the Engine body: supplied by inheritance.
                self.assertNotIn(name, Engine.__dict__)

    def test_engine_inherits_the_same_method_objects(self):
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIs(getattr(Engine, name), getattr(EngineDelegationMixin, name))

    def test_gate_reexports_are_identical_objects(self):
        self.assertIs(runtime_check, gate_check)
        self.assertIs(RUNTIME_FORBIDDEN, GATE_FORBIDDEN)

    def test_gate_still_refuses_forbidden_and_accepts_clean_roles(self):
        base = self.config.roles["searcher"]
        for cap in sorted(GATE_FORBIDDEN):
            with self.subTest(capability=cap):
                role = dataclasses.replace(base, capabilities=("files", cap))
                with self.assertRaises(ConfigError):
                    runtime_check("helper", role)
        runtime_check("helper", dataclasses.replace(base, capabilities=("files", "read")))
        runtime_check("helper", dataclasses.replace(base, capabilities=("files", "read", "finish")))

    def test_moved_delegate_role_uses_the_moved_gate(self):
        base = self.config.roles["searcher"]
        forbidden = dataclasses.replace(base, name="helper", capabilities=("files", "decide", "finish"))
        engine = Engine(dataclasses.replace(
            self.config, roles={**self.config.roles, "helper": forbidden}))
        with self.assertRaises(ConfigError):
            engine._delegate_role(base, "helper")

        clean = dataclasses.replace(base, name="helper", capabilities=("files", "read", "finish"))
        engine = Engine(dataclasses.replace(
            self.config, roles={**self.config.roles, "helper": clean}))
        child = engine._delegate_role(base, "helper")
        self.assertEqual(child.name, "helper")
        self.assertIn("finish", child.capabilities)
        self.assertFalse(set(child.capabilities) & GATE_FORBIDDEN)

    def test_admission_mixin_stays_first_in_engine_mro(self):
        self.assertTrue(issubclass(Engine, EngineDelegationMixin))
        self.assertTrue(issubclass(Engine, EngineAdmissionMixin))
        mro = Engine.__mro__
        self.assertLess(mro.index(EngineAdmissionMixin), mro.index(EngineDelegationMixin))
        self.assertIs(Engine._check_admission, EngineAdmissionMixin._check_admission)
        self.assertIs(Engine._admit, EngineAdmissionMixin._admit)
