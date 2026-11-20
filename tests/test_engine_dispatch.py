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

    def test_event_trigger_without_relevant_facts_is_skipped(self):
        snapshot = {"id": "snapshot", "code_digest": "a" * 64, "goal_digest": "b" * 64}
        role = SimpleNamespace(
            capabilities=(), on_change=False, decision_events=(), decision_sources="own",
            work_item_events=(), work_item_sources="own", ci_wake=False, scheduled_inputs=False,
            daemon=False, interval_seconds=0, calendar="", workspace="read",
            event_wakes=EVENT_WAKE_KINDS)
        project = SimpleNamespace(
            work_items=object(), control=lambda: {},
            config=SimpleNamespace(limits=SimpleNamespace(wait_events_limit=10,
                                                           work_item_events_limit=20)),
            snapshots=SimpleNamespace(get=lambda: snapshot),
            insights=SimpleNamespace(due_waits=lambda *a, **k: [],
                                     pending_obligations=lambda *a, **k: []))
        engine = Engine(SimpleNamespace(roles={"reader": role}))
        record_state = [{"wake_pending": False, "events": []}]
        with (patch("mizu.engine_dispatch._role_cursor", return_value={}),
              patch("mizu.engine_dispatch._on_change_observed", return_value=False),
              patch("mizu.engine_dispatch._record_delivery") as delivery,
              patch("mizu.engine_dispatch._decision_delivery_state",
                    return_value={"events": [], "diagnostics": []}),
              patch("mizu.engine_dispatch._reconcile_work_item_waits", return_value=(False, False)),
              patch("mizu.engine_dispatch.work_item_events.pending", return_value=[]),
              patch("mizu.engine_dispatch.work_item_events.delivery", return_value={"events": []}),
              patch("mizu.engine_dispatch.check_jobs.wake_ready", return_value=False),
              patch("mizu.engine_dispatch.ci_waits.delivery", return_value=([], [])),
              patch("mizu.engine_dispatch.proposal_waits.delivery", return_value=([], [])),
              patch("mizu.engine_dispatch._ci_due_for", return_value=[]),
              patch("mizu.engine_dispatch._ci_due_for_with_facts", return_value=([], [])),
              patch("mizu.engine_dispatch.selected_ci_failures_present", return_value=False),
              patch("mizu.engine_dispatch._ready_work_observation", return_value=(False, False, False)),
              patch("mizu.engine_dispatch._operator_woke", return_value=False)):
            delivery.return_value.pending.return_value = record_state
            state = engine._dispatch_state(project, "reader", role, event_trigger=True)
        self.assertEqual(state["skip"], {"skipped": "no-event", "snapshot": "snapshot",
                                         "code_digest": "a" * 64})

    def test_run_refuses_flat_records_and_delegates_with_the_same_keywords(self):
        limits = SimpleNamespace(lock_diagnostics="diagnostic-setting")
        config = SimpleNamespace(runtime_policy=contextlib.nullcontext, limits=limits)
        engine = Engine(config)
        project = SimpleNamespace(name="sample")
        with (patch("mizu.engine_dispatch.flat_records_present", return_value=True),
              patch("mizu.engine_dispatch.lock_diagnostics.collect") as collect):
            with self.assertRaisesRegex(Denied, "Flat shared records remain"):
                engine.run(project, "reader")
        collect.assert_not_called()

        keywords = dict(attributes={"hint": "value"}, retry_busy=True, enqueue_busy=True,
                        event_trigger=True, probes_serviced=True, scheduled_invocation=True)
        with (patch("mizu.engine_dispatch.flat_records_present", return_value=False),
              patch("mizu.engine_dispatch.lock_diagnostics.collect",
                    return_value=contextlib.nullcontext()) as collect,
              patch.object(engine, "_run", return_value={"status": "completed"}) as run):
            result = engine.run(project, "reader", **keywords)
        self.assertEqual(result, {"status": "completed"})
        collect.assert_called_once_with(limits.lock_diagnostics)
        run.assert_called_once_with(project, "reader", **keywords)


if __name__ == "__main__":
    unittest.main()
