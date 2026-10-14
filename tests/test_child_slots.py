"""Child slot lending with offline drivers and real local admission locks."""
import dataclasses
import threading
import unittest
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.errors import Busy, ProtocolError
from mizu.fs import lock, read_json
from mizu.runtime import Engine, slot


FINISH = {"outcome": "continue", "summary": "child result", "state": "Next: review."}


class ChildSlotTests(Fixture):
    def configured(self, parallel_runs=1, parallel_consults=2):
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"], capabilities=tuple(dict.fromkeys(
                [*roles["worker"].capabilities, "consult", "delegate"])))
        roles["helper"] = dataclasses.replace(
            roles["consult"], name="helper", capabilities=("read", "finish"))
        return dataclasses.replace(self.config, roles=roles, limits=dataclasses.replace(
            self.config.limits, parallel_runs=parallel_runs, parallel_consults=parallel_consults))

    def args(self, operation):
        return ({"role": "consult", "question": "Review this tree"} if operation == "consult"
                else {"role": "helper", "task": "Inspect this tree"})

    def test_both_children_run_in_the_single_held_caller_slot(self):
        for operation in ("consult", "delegate"):
            with self.subTest(operation=operation):
                config = self.configured()
                seen = {}
                engine = None
                def work(ctx, *_):
                    if ctx.role.name == "worker":
                        seen["parent"] = ctx
                        self.assertTrue(ctx._slot_lease)
                        response = ctx.handle(operation, self.args(operation))
                        child = response["answers"][0] if operation == "consult" else response
                        self.assertEqual(child["answer"], FINISH["summary"])
                        stored = read_json(self.project.root / "runs" / child["run"] /
                                           ("consultation.json" if operation == "consult" else "delegation.json"))
                        self.assertEqual(stored["status"], "completed")
                        ctx.handle("finish", FINISH)
                    else:
                        self.assertFalse(ctx._slot_lease)
                        self.assertFalse(seen["parent"].joins_idle.is_set())
                        # The only slot and the writer lock stay held for the
                        # entire child, including delegation's live-tree work.
                        with self.assertRaises(Busy), slot(config):
                            pass
                        with self.assertRaises(Busy), lock(
                                self.project.root / "locks/workspace.lock", blocking=False):
                            pass
                        outsider = ScriptDriver()
                        with self.assertRaises(Busy):
                            Engine(config, driver=outsider).run(self.project, "reporter")
                        self.assertEqual(outsider.calls, [])
                        # Even direct child APIs cannot bypass non-lender status.
                        with self.assertRaisesRegex(Busy, "admitted caller"):
                            engine.consult(ctx, self.args("consult"))
                        target = "helper" if operation == "consult" else "consult"
                        with self.assertRaisesRegex(Busy, "admitted caller"):
                            engine.delegate(ctx, {"role": target, "task": "nested"})
                        if operation == "delegate":
                            self.assertEqual(ctx.workspace, self.project.workspace)
                            (ctx.workspace / "child.txt").write_text("one writer")
                        ctx.handle("finish", FINISH)
                driver = ScriptDriver(work)
                engine = Engine(config, driver=driver)
                result = engine.run(self.project, "worker")
                self.assertEqual(result["status"], "completed")
                self.assertEqual(len(driver.calls), 2)
                parent = seen["parent"]
                self.assertTrue(parent.closed)
                self.assertFalse(parent._slot_lease)
                # Retaining a completed Context cannot retain admission.
                for child_operation in ("consult", "delegate"):
                    with self.assertRaisesRegex(Busy, "admitted caller"):
                        getattr(engine, child_operation)(parent, self.args(child_operation))
                self.assertEqual(len(driver.calls), 2)
                with slot(config):  # Admission releases the slot after the join.
                    pass

    def test_unadmitted_calls_fail_before_capture_or_inference_even_with_free_slots(self):
        for parallel_runs in (1, 4):
            with self.subTest(parallel_runs=parallel_runs):
                config = self.configured(parallel_runs)
                parent = self.context()
                self.assertFalse(parent._slot_lease)
                driver = ScriptDriver()
                engine = Engine(config, driver=driver)
                with patch.object(parent.project.snapshots, "capture_files") as capture:
                    for operation in ("consult", "delegate"):
                        with self.assertRaisesRegex(Busy, "admitted caller"):
                            getattr(engine, operation)(parent, self.args(operation))
                    capture.assert_not_called()
                self.assertEqual(driver.calls, [])
                self.assertFalse((parent.run_dir / "consultations").exists())
                self.assertFalse((parent.run_dir / "delegations").exists())

    def test_failed_caller_clears_lease_and_releases_slot(self):
        config = self.configured()
        seen = []
        def work(ctx, *_):
            seen.append(ctx)
            self.assertTrue(ctx._slot_lease)
            raise ProtocolError("offline caller failure")
        with self.assertRaises(ProtocolError):
            Engine(config, driver=ScriptDriver(work)).run(self.project, "worker")
        self.assertTrue(seen[0].closed)
        self.assertFalse(seen[0]._slot_lease)
        with slot(config):
            pass

    def test_consult_fanout_stays_bounded_when_all_global_slots_are_held(self):
        config = self.configured(parallel_runs=2, parallel_consults=2)
        names = [f"advisor-{n}" for n in range(4)]
        roles = dict(config.roles)
        for name in names:
            roles[name] = dataclasses.replace(roles["consult"], name=name)
        config = dataclasses.replace(config, roles=roles)
        barrier = threading.Barrier(config.limits.parallel_consults, timeout=5)
        mutex = threading.Lock()
        active = peak = 0
        def work(ctx, *_):
            nonlocal active, peak
            if ctx.role.name == "worker":
                with slot(config):  # Fill the other slot; children need none.
                    response = ctx.handle("consult", {"roles": names, "question": "Review"})
                self.assertEqual(len(response["answers"]), len(names))
                self.assertTrue(all(a.get("answer") == FINISH["summary"] for a in response["answers"]))
                ctx.handle("finish", FINISH)
            else:
                with mutex:
                    active += 1
                    peak = max(peak, active)
                try:
                    self.assertFalse(ctx._slot_lease)
                    barrier.wait()
                    ctx.handle("finish", FINISH)
                finally:
                    with mutex:
                        active -= 1
        result = Engine(config, driver=ScriptDriver(work)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(peak, config.limits.parallel_consults)
        self.assertEqual(active, 0)


if __name__ == "__main__":
    unittest.main()
