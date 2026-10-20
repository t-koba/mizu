"""CI requests: one run per workflow at an exact published commit.

Offline only: the adapter is a local double, no provider, no publication.
"""
import dataclasses
import json
import sys
import uuid

from support import Fixture
from mizu.ci_requests import CiRequests, request_id
from mizu.errors import Denied
from mizu.fs import mkdir, read_json
from mizu.protocol import tool_definitions
from mizu.publication import Publication
from mizu.runtime import Context

SHA = "a" * 40
OTHER_SHA = "b" * 40


def adapter_settings(payload, **extra):
    code = ("import sys,json; req=json.load(sys.stdin); "
            "print(json.dumps(%s))" % json.dumps(payload))
    settings = {"command": [sys.executable, "-c", code], "timeout_seconds": 5,
                "max_bytes": 524288, "request_grant": True}
    settings.update(extra)
    return settings


def runs(head_sha=SHA, workflow="ci", run_id="run-1"):
    return {"runs": [{"workflow": workflow, "run_id": run_id,
                      "head_sha": head_sha, "url": "https://ci.invalid/1"}]}


class CiRequestTests(Fixture):
    def setUp(self):
        super().setUp()
        self.publication = Publication(self.project.root, "e" * 40, "c" * 64, "published", "push")
        intent, _ = self.publication.prepare("approval", "run")
        self.publication.mark_dispatch()
        self.receipt = self.publication.complete(intent, SHA)

    def context(self, payload, **extra):
        config = dataclasses.replace(self.config, vcs=adapter_settings(payload, **extra))
        role = dataclasses.replace(
            config.roles["worker"],
            capabilities=tuple(list(config.roles["worker"].capabilities) + ["vcs_request"]))
        config = dataclasses.replace(config, roles={**config.roles, "worker": role})
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        return Context(config, self.project, role, run, self.project.snapshots.get(),
                       self.project.workspace)

    def request(self, ctx, workflows=("ci",)):
        return ctx.handle("vcs_request", {"publication_id": self.receipt["publication_id"],
                                           "workflows": list(workflows)})

    def test_request_runs_at_the_published_commit_and_records_it(self):
        ctx = self.context(runs())
        out = self.request(ctx)
        self.assertEqual(out["sha"], SHA)
        self.assertEqual(out["publication_id"], self.receipt["publication_id"])
        self.assertEqual(out["runs"][0]["run_id"], "run-1")
        self.assertFalse(out["replayed"])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        stored = CiRequests(self.project.root).outcome(identity)
        self.assertEqual(stored["runs"][0]["head_sha"], SHA)
        self.assertIsNotNone(CiRequests(self.project.root).dispatched(identity))

    def test_a_run_at_another_commit_is_refused_and_never_recorded(self):
        ctx = self.context(runs(head_sha=OTHER_SHA))
        with self.assertRaises(Denied):
            self.request(ctx)
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        self.assertIsNone(CiRequests(self.project.root).outcome(identity))
        # The dispatch marker survives: the outcome is uncertain, so a second
        # request is refused instead of spending a second run.
        self.assertIsNotNone(CiRequests(self.project.root).dispatched(identity))

    def test_a_recorded_request_replays_instead_of_spending_a_second_run(self):
        ctx = self.context(runs())
        first = self.request(ctx)
        second = self.request(ctx)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["request_id"], first["request_id"])
        self.assertEqual(second["runs"], first["runs"])

    def test_an_unresolved_dispatch_is_never_retried_blindly(self):
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        CiRequests(self.project.root).mark_dispatch(
            identity, {"request_id": identity, "sha": SHA, "workflows": ["ci"]})
        ctx = self.context(runs())
        with self.assertRaises(Denied):
            self.request(ctx)
        self.assertIsNone(CiRequests(self.project.root).outcome(identity))

    def test_the_provider_grant_is_required(self):
        ctx = self.context(runs(), request_grant=False)
        with self.assertRaises(Denied):
            self.request(ctx)

    def test_an_unpublished_target_is_refused(self):
        ctx = self.context(runs())
        with self.assertRaises(Denied):
            ctx.handle("vcs_request", {"publication_id": "0" * 64, "workflows": ["ci"]})

    def test_the_capability_is_off_by_default(self):
        self.assertNotIn("vcs_request", self.config.roles["worker"].capabilities)
        self.assertNotIn("mizu_vcs_request",
                         [tool["name"] for tool in tool_definitions(self.config.roles["worker"].capabilities)])
        granted = dataclasses.replace(
            self.config.roles["worker"],
            capabilities=tuple(list(self.config.roles["worker"].capabilities) + ["vcs_request"]))
        self.assertIn("mizu_vcs_request",
                      [tool["name"] for tool in tool_definitions(granted.capabilities)])
