"""The extracted VCS CI request handler keeps its dispatch and fail-closed gates.

The handler and its three private coverage helpers now live with their owner;
``Context`` inherits them and the ``vcs_request`` dispatch name resolves to the
same method object. These tests cover that identity plus the referenced
no-spend refusals, without repeating the full CI request suites.
"""
import unittest
from unittest.mock import Mock, patch

from mizu.context_vcs_request_tools import (MAX_COVERED_BYTES,
                                            ContextVcsRequestToolsMixin)
from mizu.errors import Denied
from mizu.runtime import Context

OWNER = "mizu.context_vcs_request_tools"
HANDLER = ContextVcsRequestToolsMixin._op_vcs_request
HELPERS = ("_covered_waits", "_coverage_state", "_recorded_coverage")
PUB = "a" * 64
SHA = "b" * 40
RECEIPT = {"publication_id": PUB, "published_sha": SHA}


class ContextVcsRequestDispatchTests(unittest.TestCase):
    def test_context_inherits_the_mixin_and_dispatches_its_identity(self):
        self.assertTrue(issubclass(Context, ContextVcsRequestToolsMixin))
        self.assertIs(Context._op_vcs_request, HANDLER)
        self.assertIs(Context._DISPATCH["vcs_request"], HANDLER)
        self.assertEqual(HANDLER.__module__, OWNER)

    def test_helpers_are_owned_by_the_mixin_and_inherited_by_context(self):
        for name in HELPERS:
            with self.subTest(name=name):
                method = getattr(ContextVcsRequestToolsMixin, name)
                self.assertIs(getattr(Context, name), method)
                self.assertEqual(method.__module__, OWNER)

    def test_constant_moves_with_the_owner_and_runtime_drops_it(self):
        import mizu.runtime as runtime
        self.assertEqual(MAX_COVERED_BYTES, 2048)
        self.assertFalse(hasattr(runtime, "MAX_COVERED_BYTES"))
        self.assertFalse(hasattr(runtime, "CiRequests"))
        self.assertFalse(hasattr(runtime, "ci_request_id"))
        self.assertFalse(hasattr(runtime, "Publication"))
        # Explicit re-export seams stay in runtime.
        self.assertTrue(hasattr(runtime, "source_commit"))
        self.assertTrue(hasattr(runtime, "_vcs"))


def _coverage_context(capabilities=("work_items_transition",), poll=True):
    context = Mock()
    context.role.capabilities = set(capabilities)
    context.config.vcs = {"poll_enabled": poll, "command": ["git"] if poll else ""}
    return context


class InvalidCoveredItemsTests(unittest.TestCase):
    def test_empty_coverage_is_allowed(self):
        context = _coverage_context(capabilities=(), poll=False)
        self.assertEqual(ContextVcsRequestToolsMixin._covered_waits(context, None, RECEIPT), [])

    def test_invalid_covered_item_shapes_are_refused_before_normalization(self):
        item = {"id": "wi_" + "0" * 32, "rev": 1, "checks": ["unit"]}
        cases = (
            (_coverage_context(), [item] * 17, "A CI request covers at most 16 work items"),
            (_coverage_context(), {}, "A CI request covers at most 16 work items"),
            (_coverage_context(capabilities=()), [item],
             "Covering work items requires the work-item capability"),
            (_coverage_context(poll=False), [item],
             "Covering work items requires automatic VCS polling and an adapter"),
        )
        for context, raw, message in cases:
            with self.subTest(message=message), \
                    patch("mizu.ci_waits.normalize") as normalize:
                with self.assertRaisesRegex(Denied, message):
                    ContextVcsRequestToolsMixin._covered_waits(context, raw, RECEIPT)
                normalize.assert_not_called()

    def test_duplicate_and_oversized_coverage_are_refused(self):
        wait = {"kind": "ci", "publication_id": PUB, "checks": []}
        context = _coverage_context()
        one = {"id": "wi_" + "0" * 32, "rev": 1, "checks": []}
        with patch("mizu.ci_waits.normalize", return_value=wait) as normalize:
            with self.assertRaisesRegex(Denied, "Duplicate covered work item"):
                ContextVcsRequestToolsMixin._covered_waits(context, [one, one], RECEIPT)
            normalize.assert_called_once()
        # Sixteen items whose records push the canonical set past the moved bound.
        bulky = [{"id": "wi_%032d" % i, "rev": "r" * 200, "checks": []} for i in range(16)]
        with patch("mizu.ci_waits.normalize", return_value=wait):
            with self.assertRaisesRegex(Denied, "The covered work-item set exceeds 2048 UTF-8 bytes"):
                ContextVcsRequestToolsMixin._covered_waits(context, bulky, RECEIPT)


def _state_context(items):
    context = Mock()
    context.project.work_items.get.side_effect = items.__getitem__
    return context


def _wait_entry(item_id, rev=1, wait=None):
    wait = wait if wait is not None else {"kind": "ci"}
    return {"id": item_id, "rev": rev, "wait": wait, "input": wait}


class CoverageCannotTakeWaitTests(unittest.TestCase):
    def test_finished_and_foreign_wait_items_are_refused_without_transition(self):
        blocked_wait = {"kind": "code_change"}
        items = {
            "done": {"status": "done", "rev": 1},
            "dropped": {"status": "dropped", "rev": 1},
            "blocked": {"status": "blocked", "rev": 1, "wait": blocked_wait},
        }
        context = _state_context(items)
        for item_id, rev in (("done", 1), ("dropped", 1), ("blocked", 1)):
            with self.subTest(item_id=item_id):
                with self.assertRaisesRegex(
                        Denied, "Covered work item %s cannot take the CI wait" % item_id):
                    ContextVcsRequestToolsMixin._coverage_state(
                        context, [_wait_entry(item_id, rev)], "req-1", register=False)
        context.project.work_items.transition.assert_not_called()

    def test_a_moved_revision_is_refused_before_registration(self):
        context = _state_context({"moved": {"status": "open", "rev": 7}})
        with self.assertRaisesRegex(Denied, "Covered work item moved changed; "):
            ContextVcsRequestToolsMixin._coverage_state(
                context, [_wait_entry("moved", 1)], "req-1", register=False)
        context.project.work_items.transition.assert_not_called()

    def test_an_already_registered_item_reports_waiting_without_a_second_transition(self):
        wait = {"kind": "ci", "publication_id": PUB}
        context = _state_context({"held": {"status": "blocked", "rev": 2, "wait": wait}})
        state = ContextVcsRequestToolsMixin._coverage_state(
            context, [_wait_entry("held", 2, wait)], "req-1", register=True)
        self.assertEqual(state["waiting"], ["held"])
        self.assertEqual(state["registered"], [])
        context.project.work_items.transition.assert_not_called()


class RetryGateTests(unittest.TestCase):
    def _retry(self, store, previous_probe=None):
        context = Mock()
        with patch(f"{OWNER}.Publication") as publication, \
                patch(f"{OWNER}.CiRequests", return_value=store) as requests_cls, \
                patch(f"{OWNER}._vcs.request_runs_via") as dispatch:
            publication.read_receipt.return_value = RECEIPT
            if previous_probe is None:
                store.outcome.side_effect = [None, None]
            else:
                store.outcome.side_effect = [None, previous_probe]
            store.dispatched.return_value = None
            with self.assertRaisesRegex(
                    Denied, "A new CI attempt requires the preceding attempt's durable "
                            "no-dispatch outcome"):
                HANDLER(context, {"publication_id": PUB, "workflows": ["ci", "lint"],
                                  "attempt": 2})
            publication.read_receipt.assert_called_once_with(context.project.root, PUB)
            requests_cls.assert_called_once()
            dispatch.assert_not_called()
            store.mark_dispatch.assert_not_called()
            store.resolve.assert_not_called()

    def test_retry_without_a_preceding_attempt_is_refused(self):
        self._retry(Mock())

    def test_retry_after_a_spent_preceding_attempt_is_refused(self):
        self._retry(Mock(), {"outcome": "resolved", "runs": []})


if __name__ == "__main__":
    unittest.main()
