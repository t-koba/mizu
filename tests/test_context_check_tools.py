"""Context check-job and verification dispatch is owned by an extracted mixin.

The two wrappers keep their dispatch keys and reach check_jobs (including the
queued-verify branch) with the trusted Context and its arguments.
"""
import unittest
from unittest.mock import Mock, patch

from mizu.context_check_tools import ContextCheckToolsMixin
from mizu.runtime import Context


class ContextCheckToolsTests(unittest.TestCase):
    def test_context_inherits_every_wrapper(self):
        for name in ("_op_check_job", "_op_verify"):
            self.assertTrue(callable(getattr(Context, name)), name)
        self.assertTrue(issubclass(Context, ContextCheckToolsMixin))

    def test_dispatch_handlers_are_the_mixin_methods(self):
        self.assertIs(Context._DISPATCH["verify"], ContextCheckToolsMixin._op_verify)
        self.assertIs(Context._DISPATCH["check_job"], ContextCheckToolsMixin._op_check_job)

    def test_queued_verify_reaches_check_jobs_with_trusted_context(self):
        context = Mock()
        context.role.capabilities = ["check_job"]
        args = {}
        with patch("mizu.context_check_tools.check_jobs") as owner:
            owner.submit.return_value = {"status": "queued"}
            result = Context._DISPATCH["verify"](context, args)
        owner.submit.assert_called_once_with(context)
        self.assertEqual(result, {"status": "queued"})

    def test_check_job_action_reaches_check_jobs_with_trusted_args(self):
        context = Mock()
        args = {"action": "start", "script": "echo ok", "fresh": True,
                "purpose": "acceptance", "scratch_paths": ["scratch"]}
        with patch("mizu.context_check_tools.check_jobs") as owner:
            owner.submit.return_value = {"id": "job"}
            result = Context._DISPATCH["check_job"](context, args)
        owner.submit.assert_called_once_with(
            context, "echo ok", commands=None, fresh=True,
            purpose="acceptance", scratch_paths=["scratch"])
        self.assertEqual(result, {"id": "job"})


if __name__ == "__main__":
    raise SystemExit(unittest.main())
