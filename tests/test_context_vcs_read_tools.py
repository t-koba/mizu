"""Context VCS read listings are owned by an extracted mixin.

The three internal helpers keep their filters, defaults, evidence records and
patch seams while reaching their own modules with the trusted Context.
"""
import inspect
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mizu.context_vcs_read_tools import ContextVcsReadToolsMixin
from mizu.errors import Denied
from mizu.runtime import Context

OWNER = "mizu.context_vcs_read_tools"
HANDLERS = ("_op_vcs_read_publications", "_op_vcs_read_ci_requests",
            "_op_vcs_read_ci_costs")
EVIDENCE = "vcs-read/eid-1.json"


def _context():
    context = Mock()
    context.project.root = Path("/project")
    context.config.publication_evidence = "pub-policy"
    context.config.ci_requests = "ci-policy"
    context.run_dir = Path("/run")
    context.role.workspace = "local"
    context._new_invocation_evidence.return_value = ("eid-1", EVIDENCE)
    return context


class ContextVcsReadToolsTests(unittest.TestCase):
    def test_context_inherits_every_helper(self):
        for name in (*HANDLERS, "_op_vcs_read"):
            self.assertTrue(callable(getattr(Context, name)), name)
        self.assertTrue(issubclass(Context, ContextVcsReadToolsMixin))

    def test_helpers_are_owned_by_the_mixin_not_direct_dispatch_entries(self):
        for name in HANDLERS:
            with self.subTest(name=name):
                method = getattr(Context, name)
                self.assertIs(method, getattr(ContextVcsReadToolsMixin, name))
                self.assertEqual(method.__module__, OWNER)
                self.assertNotIn(method, tuple(Context._DISPATCH.values()))
        self.assertEqual(Context._op_vcs_read.__module__, "mizu.runtime")
        self.assertIs(Context._DISPATCH["vcs_read"], Context._op_vcs_read)

    def test_vcs_read_reaches_each_helper_through_self(self):
        context = _context()
        for op, name in (("publications", "_op_vcs_read_publications"),
                         ("ci_requests", "_op_vcs_read_ci_requests"),
                         ("ci_costs", "_op_vcs_read_ci_costs")):
            with self.subTest(op=op):
                helper = Mock(return_value={"op": op})
                setattr(context, name, helper)
                args = {"op": op}
                self.assertEqual(Context._op_vcs_read(context, args), {"op": op})
                helper.assert_called_once_with(args)

    def test_unknown_filters_are_refused_with_unchanged_messages(self):
        cases = (
            ("_op_vcs_read_publications", {"limit": 1},
             "publication listing takes only code_digest, branch and published_sha filters"),
            ("_op_vcs_read_ci_requests", {"code_digest": "b" * 64},
             "CI request listing takes no filters"),
            ("_op_vcs_read_ci_costs", {"branch": "main"},
             "CI costs take only limit and offset"),
        )
        for name, extra, message in cases:
            with self.subTest(name=name):
                context = _context()
                with self.assertRaisesRegex(Denied, message), \
                        patch(f"{OWNER}.write_json") as write, \
                        patch(f"{OWNER}.Publication") as publication, \
                        patch(f"{OWNER}.CiRequests") as requests:
                    getattr(Context, name)(context, {"op": name, **extra})
                publication.list_receipts.assert_not_called()
                requests.assert_not_called()
                write.assert_not_called()
                context._new_invocation_evidence.assert_not_called()

    def test_publications_lists_local_receipts_and_writes_evidence(self):
        context = _context()
        listing = {"publications": [{"publication_id": "a" * 64}], "total": 1}
        args = {"op": "publications", "code_digest": "b" * 64,
                "branch": "main", "published_sha": "c" * 40}
        with patch(f"{OWNER}.Publication") as publication, \
                patch(f"{OWNER}.write_json") as write:
            publication.list_receipts.return_value = listing
            result = Context._op_vcs_read_publications(context, args)
        publication.list_receipts.assert_called_once_with(
            context.project.root, code_digest=args["code_digest"], branch="main",
            published_sha=args["published_sha"], limit=100,
            policy=context.config.publication_evidence)
        context._new_invocation_evidence.assert_called_once_with("vcs-read")
        expected = {"op": "publications", "branch": "main",
                    "trust": "recorded-local-untrusted", "evidence_id": "eid-1",
                    "evidence": EVIDENCE, "result": listing}
        write.assert_called_once_with(context.run_dir / EVIDENCE, expected)
        self.assertEqual(result, {**expected, **listing})

    def test_ci_requests_lists_local_days_and_writes_evidence(self):
        context = _context()
        listing = {"days": [], "total": 0}
        with patch(f"{OWNER}.CiRequests") as requests, \
                patch(f"{OWNER}.write_json") as write:
            requests.return_value.daily.return_value = listing
            result = Context._op_vcs_read_ci_requests(context, {"op": "ci_requests"})
        requests.assert_called_once_with(context.project.root, context.config.ci_requests)
        requests.return_value.daily.assert_called_once_with(limit=100)
        context._new_invocation_evidence.assert_called_once_with("vcs-read")
        expected = {"op": "ci_requests", "trust": "recorded-local-untrusted",
                    "evidence_id": "eid-1", "evidence": EVIDENCE, "result": listing}
        write.assert_called_once_with(context.run_dir / EVIDENCE, expected)
        self.assertEqual(result, {**expected, **listing})

    def test_ci_costs_pages_requests_and_reads_each_receipt_status(self):
        for args, limit, offset in (({"op": "ci_costs"}, 8, 0),
                                    ({"op": "ci_costs", "limit": 3, "offset": 5}, 3, 5)):
            with self.subTest(args=args):
                context = _context()
                listing = {"observed_wall_seconds": 60, "total": 1}
                context._op_vcs_read.return_value = {"result": "status"}

                def cost(page, read, _listing=listing):
                    self.assertEqual(page, "page")
                    self.assertEqual(read("a" * 64), {"result": "status"})
                    return _listing

                with patch(f"{OWNER}.CiRequests") as requests, \
                        patch("mizu.ci_costs.observed_costs", side_effect=cost) as observed, \
                        patch(f"{OWNER}.write_json") as write:
                    requests.return_value.page.return_value = "page"
                    result = Context._op_vcs_read_ci_costs(context, args)
                requests.assert_called_once_with(context.project.root, context.config.ci_requests)
                requests.return_value.page.assert_called_once_with(limit=limit, offset=offset)
                observed.assert_called_once()
                context._op_vcs_read.assert_called_once_with(
                    {"op": "status", "publication_id": "a" * 64})
                context._new_invocation_evidence.assert_called_once_with("vcs-read")
                expected = {"op": "ci_costs", "trust": "external-untrusted",
                            "evidence_id": "eid-1", "evidence": EVIDENCE, "result": listing}
                write.assert_called_once_with(context.run_dir / EVIDENCE, expected)
                self.assertEqual(result, {**expected, **listing})

    def test_ci_costs_keeps_its_local_observation_import(self):
        source = inspect.getsource(ContextVcsReadToolsMixin._op_vcs_read_ci_costs)
        self.assertIn("from .ci_costs import observed_costs", source)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
