"""The VCS publication handler keeps its dispatch and receipt behaviour after extraction."""
import contextlib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from support import Fixture
from mizu.context_vcs_publish_tools import ContextVcsPublishMixin
from mizu.distribution import public_source_identity
from mizu.errors import Denied
from mizu.runtime import Context

OWNER = "mizu.context_vcs_publish_tools"
DIGEST = "a" * 64
SHA = "b" * 40
SOURCE = "source-sha"
FILES = {"app.py": {"sha256": "c" * 64, "executable": False}}
PROJECTION = public_source_identity(FILES)
APPROVAL = {"approval": "message-1", "branch": "main", "code_digest": DIGEST}
RECEIPT_PATH = "evidence/publications/pub-1/receipt.json"


def _fake_context():
    context = Mock()
    context.role.workspace = "write"
    context.workspace = Path("/workspace")
    context.run_dir = Path("/runs/run-1")
    context.project.root = Path("/project")
    context.project.snapshots.capture_files.return_value = {"code_digest": DIGEST, "files": FILES}
    context.project.vcs_settings.return_value = {"command": ["adapter"]}
    context.config.vcs = {"approval_queue": "proposals"}
    context.config.publication.source_commit_timeout_seconds = 7
    context.tools_cancelled = "cancel-token"
    before_local, observe = Mock(), Mock(return_value={"observed": True})
    context._mutation_evidence.return_value = (before_local, observe)
    return context



class ContextVcsPublishDispatchTests(Fixture):
    def test_read_only_role_is_denied_by_the_existing_dispatch_handler(self):
        handler = ContextVcsPublishMixin._op_vcs_publish
        self.assertTrue(issubclass(Context, ContextVcsPublishMixin))
        self.assertIs(Context._op_vcs_publish, handler)
        self.assertIs(Context._DISPATCH["vcs_publish"], handler)
        self.assertEqual(handler.__module__, OWNER)
        context = self.context("searcher")
        self.assertEqual(context.role.workspace, "read")
        with self.assertRaises(Denied) as refused:
            context.dispatch("vcs_publish", {"op": "push", "branch": "main"})
        self.assertEqual(str(refused.exception), "Publication requires a writable workspace")


class ContextVcsPublishMixinTests(unittest.TestCase):
    def test_dispatch_records_the_receipt_and_observes_it(self):
        context = _fake_context()
        receipt = {"publication_id": "pub-1", "published_sha": SHA, "approval": "message-1"}
        data = {"published_sha": SHA, "lineage": {"pushed": True}}
        before_local, observe = context._mutation_evidence.return_value
        with patch(f"{OWNER}._vcs.require_go_approval", return_value=APPROVAL) as require_approval, \
                patch(f"{OWNER}.reviews.require_approvals", return_value=({"role": "reviewer"},)) as reviews, \
                patch(f"{OWNER}.source_commit", return_value=SOURCE) as commit, \
                patch(f"{OWNER}._vcs.publish_via", return_value=data) as publish_via, \
                patch(f"{OWNER}.lock", return_value=contextlib.nullcontext()) as locked, \
                patch(f"{OWNER}.Publication") as publication_cls:
            publication = publication_cls.return_value
            publication.id = "pub-1"
            publication.lock = "/project/locks/pub-1.lock"
            publication.identity = {"branch": "main", "code_digest": DIGEST}
            publication.prepare.return_value = ("intent", None)
            publication.complete.return_value = receipt
            publication.path.return_value = Path("/project") / RECEIPT_PATH
            result = Context._op_vcs_publish(context, {"op": "push", "branch": "main"})

        context.close_exec.assert_called_once_with()
        context.project.snapshots.capture_files.assert_called_once_with(context.workspace)
        require_approval.assert_called_once_with(context.project, "main", DIGEST,
                                                queue="proposals")
        reviews.assert_called_once_with(context, DIGEST)
        commit.assert_called_once_with(context.workspace, timeout=7, cancel="cancel-token")
        publication_cls.assert_called_once_with(context.project.root, SOURCE, DIGEST,
                                               "main", "push", projection=PROJECTION)
        locked.assert_called_once_with(publication.lock)
        publication.prepare.assert_called_once_with("message-1", "run-1")
        context._mutation_evidence.assert_called_once_with(
            "vcs-publish", "push",
            {"branch": "main", "code_digest": DIGEST,
             "projection_schema": PROJECTION["schema"],
             "projection_algorithm": PROJECTION["algorithm"],
             "projection_digest": PROJECTION["digest"],
             "approval": "message-1", "source_commit": SOURCE,
             "publication_id": "pub-1"})
        publish_via.assert_called_once_with(
            context.project.vcs_settings.return_value, "push",
            {"branch": "main", "code_digest": DIGEST, "publication_id": "pub-1"},
            cancel="cancel-token", before_invoke=publish_via.call_args.kwargs["before_invoke"])
        publish_via.call_args.kwargs["before_invoke"]()
        before_local.assert_called_once_with()
        publication.mark_dispatch.assert_called_once_with()
        publication.complete.assert_called_once_with("intent", SHA, {"pushed": True})
        expected = {**receipt, "published": True, "review": [{"role": "reviewer"}],
                    "receipt": RECEIPT_PATH, "trust": "external-untrusted", "result": data}
        observe.assert_called_once_with(expected)
        self.assertEqual(result, {"observed": True})

    def test_durable_replay_keeps_the_local_observation_without_adapter_spend(self):
        context = _fake_context()
        receipt = {"publication_id": "pub-1", "published_sha": SHA}
        before_local, observe = context._mutation_evidence.return_value
        with patch(f"{OWNER}._vcs.require_go_approval", return_value=APPROVAL), \
                patch(f"{OWNER}.reviews.require_approvals", return_value=()), \
                patch(f"{OWNER}.source_commit", return_value=SOURCE), \
                patch(f"{OWNER}._vcs.publish_via") as publish_via, \
                patch(f"{OWNER}.lock", return_value=contextlib.nullcontext()), \
                patch(f"{OWNER}.Publication") as publication_cls:
            publication = publication_cls.return_value
            publication.id = "pub-1"
            publication.prepare.return_value = ("intent", receipt)
            publication.path.return_value = Path("/project") / RECEIPT_PATH
            Context._op_vcs_publish(context, {"op": "push", "branch": "main"})

        publish_via.assert_not_called()
        publication.complete.assert_not_called()
        publication.mark_dispatch.assert_not_called()
        before_local.assert_called_once_with()
        expected = {**receipt, "published": True, "review": [],
                    "receipt": RECEIPT_PATH, "trust": "external-untrusted",
                    "result": {"digest": DIGEST, "publication_id": "pub-1",
                               "projection_schema": PROJECTION["schema"],
                               "projection_algorithm": PROJECTION["algorithm"],
                               "projection_digest": PROJECTION["digest"],
                               "published_sha": SHA, "trust": "external-untrusted"}}
        observe.assert_called_once_with(expected)

    def test_missing_approval_refuses_before_reviews_or_adapter(self):
        context = _fake_context()
        context.project.kernel_queues._head.return_value = 0
        with patch(f"{OWNER}.reviews.require_approvals") as reviews, \
                patch(f"{OWNER}.source_commit") as commit, \
                patch(f"{OWNER}._vcs.publish_via") as publish_via:
            with self.assertRaises(Denied) as refused:
                Context._op_vcs_publish(context, {"op": "push", "branch": "main"})
        self.assertEqual(str(refused.exception),
                         "No recorded human approval 'GO main' from the operator in queue proposals")
        reviews.assert_not_called()
        commit.assert_not_called()
        publish_via.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(unittest.main())
