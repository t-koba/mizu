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


def _receipt():
    return {"publication_id": "a" * 64, "branch": "pinned-branch",
            "published_sha": "b" * 40, "source_commit": "c" * 40,
            "code_digest": "e" * 64, "approval": "approved", "run": "run-1",
            "lineage": {"target": "baseline"}}


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
             "publication listing takes only code_digest, branch, published_sha and cursor filters"),
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
            published_sha=args["published_sha"], limit=100, cursor=None,
            policy=context.config.publication_evidence)
        context._new_invocation_evidence.assert_called_once_with("vcs-read")
        expected = {"op": "publications", "branch": "main",
                    "trust": "recorded-local-untrusted", "evidence_id": "eid-1",
                    "evidence": EVIDENCE, "result": listing}
        write.assert_called_once_with(context.run_dir / EVIDENCE, expected)
        self.assertEqual(result, {**expected, **listing})

    def test_publications_accepts_and_forwards_a_page_cursor(self):
        context = _context()
        cursor = "pub1|1735689600000000|" + "a" * 64
        args = {"op": "publications", "cursor": cursor}
        with patch(f"{OWNER}.Publication") as publication, \
                patch(f"{OWNER}.write_json"):
            publication.list_receipts.return_value = {"publications": [], "next_cursor": None}
            Context._op_vcs_read_publications(context, args)
        publication.list_receipts.assert_called_once_with(
            context.project.root, code_digest=None, branch=None, published_sha=None,
            limit=100, cursor=cursor, policy=context.config.publication_evidence)

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


    def test_vcs_read_is_mixin_owned_and_dispatched_unchanged(self):
        self.assertIs(Context._op_vcs_read, ContextVcsReadToolsMixin._op_vcs_read)
        self.assertEqual(Context._op_vcs_read.__module__, OWNER)
        self.assertIs(Context._DISPATCH["vcs_read"], Context._op_vcs_read)

    def test_vcs_read_denials_keep_their_messages(self):
        cases = (
            ({"op": "status", "branch": "main"}, "none", "This role has no workspace"),
            ({"op": "status", "branch": "main", "check": "unit"}, "local",
             "check is only valid for log reads"),
            ({"op": "status", "branch": "main", "offset": 1}, "local",
             "offset is only valid for CI costs or artifact byte pages"),
            ({"op": "status", "branch": "main", "limit": 1}, "local",
             "limit is only valid for CI costs or artifact reads/listings"),
            ({"op": "status", "branch": "main", "code_digest": "b" * 64}, "local",
             "code_digest is only valid for the publications listing"),
            ({"op": "status", "publication_id": "a" * 64, "branch": "main"}, "local",
             "publication_id status reads take no branch, sha, or other filters"),
        )
        for args, workspace, message in cases:
            with self.subTest(args=args):
                context = _context()
                context.role.workspace = workspace
                with self.assertRaisesRegex(Denied, message), \
                        patch(f"{OWNER}._vcs.read_via") as read, \
                        patch(f"{OWNER}.write_json") as write:
                    Context._op_vcs_read(context, args)
                read.assert_not_called()
                write.assert_not_called()
                context._new_invocation_evidence.assert_not_called()

    def test_publication_status_pins_receipt_branch_and_sha_into_adapter_params(self):
        context = _context()
        # Without both grants the diagnostics branch stays off.
        context.role.capabilities = ("vcs_read",)
        receipt = _receipt()
        observation = {"head": receipt["published_sha"]}
        data = {"checks": [], "lineage_observation": observation,
                "lineage_reason": "fresh", "status": "complete"}
        args = {"op": "status", "publication_id": receipt["publication_id"]}
        with patch(f"{OWNER}.Publication") as publication, \
                patch(f"{OWNER}._vcs.read_via", return_value=data) as read, \
                patch(f"{OWNER}.lineage.status", return_value={"state": "current"}) as status, \
                patch(f"{OWNER}.lineage.validate", return_value={"valid": True}) as validate, \
                patch(f"{OWNER}.ci_waits.status_diagnostics") as diagnostics, \
                patch(f"{OWNER}.write_json") as write:
            publication.read_receipt.return_value = receipt
            result = Context._op_vcs_read(context, args)

        publication.read_receipt.assert_called_once_with(
            context.project.root, receipt["publication_id"])
        read.assert_called_once()
        self.assertEqual(read.call_args.args[1], "status")
        self.assertEqual(read.call_args.args[2],
                         {"branch": receipt["branch"], "sha": receipt["published_sha"],
                          "publication_id": receipt["publication_id"]})
        self.assertIs(read.call_args.kwargs["cancel"], context.tools_cancelled)
        self.assertNotIn("preserve_status_rows", read.call_args.kwargs)
        diagnostics.assert_not_called()
        status.assert_called_once_with(
            receipt, observation, "fresh", context.project.lineage_target)
        validate.assert_called_once_with(receipt["lineage"], receipt["published_sha"])
        self.assertEqual(result, {
            "op": "status", "branch": receipt["branch"], "trust": "external-untrusted",
            "evidence_id": "eid-1", "evidence": EVIDENCE,
            "result": {"checks": [], "status": "complete",
                       "lineage": {"state": "current", "receipt": {"valid": True},
                                   "observation": observation},
                       "publication": {key: receipt[key] for key in (
                           "publication_id", "source_commit", "code_digest", "branch",
                           "published_sha", "approval", "run")}},
        })
        write.assert_called_once_with(context.run_dir / EVIDENCE, result)

    def test_diagnostics_branch_adds_ci_waits_and_filters_exact_sha_rows(self):
        context = _context()
        context.role.capabilities = ("vcs_read", "work_items_read")
        context.role.name = "worker"
        receipt = _receipt()
        sha = receipt["published_sha"]
        rows = [{"check": "keep-1", "sha": sha}, {"check": "drop", "sha": "d" * 40},
                {"check": "keep-2", "sha": sha}]
        data = {"checks": rows, "lineage_observation": {"head": sha}, "lineage_reason": "fresh"}
        diagnostics = {"items": [{"work_item": "wi_1"}], "observed": 1}
        args = {"op": "status", "publication_id": receipt["publication_id"]}
        with patch(f"{OWNER}.Publication") as publication, \
                patch(f"{OWNER}._vcs.read_via", return_value=data) as read, \
                patch(f"{OWNER}.ci_waits.status_diagnostics", return_value=diagnostics) as diag, \
                patch(f"{OWNER}.lineage.status", return_value={"state": "current"}), \
                patch(f"{OWNER}.lineage.validate", return_value={"valid": True}), \
                patch(f"{OWNER}.write_json") as write:
            publication.read_receipt.return_value = receipt
            result = Context._op_vcs_read(context, args)

        read.assert_called_once()
        self.assertTrue(read.call_args.kwargs["preserve_status_rows"])
        diag.assert_called_once_with(
            context.project.work_items, "worker", receipt, data)
        self.assertEqual(diag.call_args.args[3]["checks"], rows)  # unfiltered observation
        self.assertEqual(result["result"]["checks"],
                         [{"check": "keep-1", "sha": sha}, {"check": "keep-2", "sha": sha}])
        self.assertEqual(result["result"]["ci_waits"], diagnostics)
        write.assert_called_once_with(context.run_dir / EVIDENCE, result)
        self.assertEqual(write.call_args.args[1]["result"]["checks"],
                         [{"check": "keep-1", "sha": sha}, {"check": "keep-2", "sha": sha}])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
