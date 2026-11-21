"""Whole-run ownership lives in one mixin module after the runtime split.

The moved ``_run`` method and its Engine MRO wiring are pinned here; its
behavioural coverage stays in the runtime, delegation and dispatch suites.
"""
from support import Fixture

from mizu.engine_run import EngineRunMixin
from mizu.runtime import Engine


class EngineRunBoundaryTests(Fixture):
    def test_mixin_owns_the_run_method(self):
        self.assertIn('_run', EngineRunMixin.__dict__)
        # Not defined in the Engine body: supplied by inheritance.
        self.assertNotIn('_run', Engine.__dict__)

    def test_engine_inherits_the_same_run_object(self):
        self.assertIs(getattr(Engine, '_run'), getattr(EngineRunMixin, '_run'))

    def test_run_mixin_is_in_engine_mro(self):
        self.assertTrue(issubclass(Engine, EngineRunMixin))
        self.assertIn(EngineRunMixin, Engine.__mro__)

    def test_moved_run_keeps_its_code_object(self):
        self.assertIs(getattr(Engine, '_run').__code__,
                      getattr(EngineRunMixin, '_run').__code__)
