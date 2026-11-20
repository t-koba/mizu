"""Model execution owns one mixin module after the runtime split.

The five inference methods, their Engine MRO wiring and an injected-driver
smoke are pinned here; the dispatch/deadline behaviour stays covered by the
runtime, deadline and safe-point suites.
"""
from support import Fixture, ScriptDriver

from mizu.engine_admission import EngineAdmissionMixin
from mizu.engine_delegation import EngineDelegationMixin
from mizu.engine_execution import EngineExecutionMixin
from mizu.runtime import Engine

METHODS = (
    "resolve", "_dispatch", "_seal_driver_checkpoint",
    "_maybe_safe_point_yield", "_execute",
)


class EngineExecutionBoundaryTests(Fixture):
    def test_mixin_owns_every_execution_method(self):
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIn(name, EngineExecutionMixin.__dict__)
                # Not defined in the Engine body: supplied by inheritance.
                self.assertNotIn(name, Engine.__dict__)

    def test_engine_inherits_the_same_method_objects(self):
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIs(getattr(Engine, name), getattr(EngineExecutionMixin, name))

    def test_execution_mixin_follows_the_existing_mixins_in_engine_mro(self):
        self.assertTrue(issubclass(Engine, EngineExecutionMixin))
        mro = Engine.__mro__
        self.assertLess(mro.index(EngineAdmissionMixin), mro.index(EngineExecutionMixin))
        self.assertLess(mro.index(EngineDelegationMixin), mro.index(EngineExecutionMixin))

    def test_moved_call_graph_keeps_the_same_code_objects(self):
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIs(getattr(Engine, name).__code__,
                              getattr(EngineExecutionMixin, name).__code__)

    def test_injected_driver_is_resolved_without_a_registry_lookup(self):
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        self.assertIs(engine.resolve(None), driver)
        self.assertEqual(engine._drivers, {})
        for name in ("_execute", "_dispatch"):
            self.assertTrue(callable(getattr(engine, name)))
