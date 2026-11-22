"""Consultation/delegation child execution owns one mixin module after the split.

The moved methods and their Engine MRO wiring are pinned here; their behaviour
stays in the runtime, delegation and separation suites.
"""
from support import Fixture

from mizu.engine_collaboration import EngineCollaborationMixin
from mizu.runtime import Engine

METHODS = ("consult", "delegate")


class EngineCollaborationBoundaryTests(Fixture):
    def test_mixin_owns_both_collaboration_methods(self):
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIn(name, EngineCollaborationMixin.__dict__)
                # Not defined in the Engine body: supplied by inheritance.
                self.assertNotIn(name, Engine.__dict__)

    def test_engine_inherits_the_same_method_objects(self):
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIs(getattr(Engine, name), getattr(EngineCollaborationMixin, name))

    def test_collaboration_mixin_is_in_engine_mro(self):
        self.assertTrue(issubclass(Engine, EngineCollaborationMixin))
        self.assertIn(EngineCollaborationMixin, Engine.__mro__)

    def test_moved_methods_keep_their_code_objects(self):
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIs(getattr(Engine, name).__code__,
                              getattr(EngineCollaborationMixin, name).__code__)

