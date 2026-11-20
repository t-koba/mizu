"""Engine dispatch owns its entry points after the runtime split.

The three dispatch methods, their Engine wiring and the preparation/permission
boundaries they enforce are pinned here; the wider dispatch behaviour stays
covered by the runtime and wake suites.
"""
import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mizu.config import EVENT_WAKE_KINDS
from mizu.engine_dispatch import EngineDispatchMixin
from mizu.errors import Denied
from mizu.runtime import Engine

METHODS = ("preview", "_dispatch_state", "run")


class EngineDispatchOwnerTests(unittest.TestCase):
    def test_mixin_owns_every_dispatch_method(self):
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIn(name, EngineDispatchMixin.__dict__)
                self.assertNotIn(name, Engine.__dict__)
                self.assertIs(getattr(Engine, name), getattr(EngineDispatchMixin, name))
                self.assertEqual(getattr(Engine, name).__module__, "mizu.engine_dispatch")

    def test_dispatch_mixin_follows_the_existing_engine_mixins(self):
        mro = Engine.__mro__
        for owner in ("EngineAdmissionMixin", "EngineDelegationMixin", "EngineExecutionMixin"):
            with self.subTest(owner=owner):
                self.assertLess(mro.index(next(base for base in mro if base.__name__ == owner)),
                                mro.index(EngineDispatchMixin))

    def test_preview_uses_the_bounded_selection_prepare_path(self):
        snapshot = {"id": "snapshot"}
        role = SimpleNamespace(profile="configured", selector=(("code", "python"),))
        config = SimpleNamespace(roles={"reader": role})
        project = SimpleNamespace(roles={"reader": True},
                                  snapshots=SimpleNamespace(get=lambda: snapshot))
        engine = Engine(config)
        with patch("mizu.classification.prepare", return_value={"profile": "selected"}) as prepare:
            result = engine.preview(project, "reader", {"hint": "value"})
        prepare.assert_called_once_with(engine, project, role, snapshot, {"hint": "value"}, preview=True)
        self.assertEqual(result, {"profile": "selected"})
        role.selector = None
        self.assertEqual(engine.preview(project, "reader"),
                         {"profile": "configured", "selector": None})
        with self.assertRaisesRegex(Denied, "Role is not enabled"):
            engine.preview(project, "absent")


if __name__ == "__main__":
    unittest.main()
