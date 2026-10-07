"""External proposal observation: normalized reads plus change-gated records. Offline only."""
import dataclasses
import json
import sys
import unittest
from pathlib import Path

from support import Fixture
from mizu import vcs
from mizu.errors import Denied

SHA_A = "a" * 40
SHA_B = "b" * 40


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


def proposals_code(payload):
    import tempfile
    path = Path(tempfile.mkdtemp()) / "proposals.json"
    path.write_text(json.dumps(payload))
    return ("import sys,json; req=json.load(sys.stdin); "
            "print(open(%r).read())" % str(path))


def proposal(state="open", head_sha=SHA_A, url="https://forge/x/pull/1"):
    return {"id": "forge:owner/repo#1", "state": state,
            "head": {"repo": "owner/repo", "ref": "feature", "sha": head_sha},
            "base": {"repo": "owner/repo", "ref": "main", "sha": SHA_B},
            "draft": False, "mergeable": True,
            "checks": [{"check": "unit", "state": "success", "sha": head_sha}],
            "url": url}


class ProposalReadTests(Fixture):
    def test_proposals_normalized(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                proposals_code({"proposals": [proposal()]})))
        out = vcs.read_via(config.vcs, "proposals", {})
        self.assertEqual(out["op"], "proposals")
        self.assertIsNone(out["branch"])
        self.assertEqual(len(out["proposals"]), 1)
        row = out["proposals"][0]
        self.assertEqual(row["head"]["sha"], SHA_A)
        self.assertEqual(row["checks"][0]["check"], "unit")
        self.assertEqual(out["trust"], "external-untrusted")

    def test_proposals_branch_filter_optional(self):
        seen = {}

        def reader(settings, op, params):
            seen.update(params)
            return vcs.read_via(settings, op, params)

        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                proposals_code({"proposals": []})))
        out = reader(config.vcs, "proposals", {"branch": "main"})
        self.assertEqual(out["proposals"], [])
        self.assertEqual(seen.get("branch"), "main")

    def test_proposals_malformed_refused(self):
        for payload in ({"nope": 1},
                        {"proposals": [dict(proposal(), state="wip")]},
                        {"proposals": [dict(proposal(), draft="no")]},
                        {"proposals": [{**proposal(), "extra": 1}]},
                        {"proposals": [dict(proposal(),
                                            head={"repo": "r", "ref": "f"})]}):
            config = dataclasses.replace(
                self.config, vcs=adapter_settings(proposals_code(payload)))
            with self.assertRaises(Denied, msg=json.dumps(payload)[:60]):
                vcs.read_via(config.vcs, "proposals", {})

    def test_publish_ops_still_refused_on_read(self):
        with self.assertRaises(Denied):
            vcs.read_via(self.config.vcs, "push", {"branch": "main"})

    def test_addressed_read_still_requires_branch(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                proposals_code({"checks": []})))
        with self.assertRaises(Denied):
            vcs.read_via(config.vcs, "status", {})


class ProposalToolTests(Fixture):
    def test_tool_serves_proposals_without_branch(self):
        import uuid
        from mizu.fs import mkdir
        from mizu.runtime import Context
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                proposals_code({"proposals": [proposal()]})))
        role = config.roles["worker"]
        role = dataclasses.replace(
            role, capabilities=tuple(list(role.capabilities) + ["vcs_read"]))
        config = dataclasses.replace(
            config, roles={**config.roles, "worker": role})
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        ctx = Context(config, self.project, role, run,
                      self.project.snapshots.get(), self.project.workspace)
        out = ctx.handle("vcs_read", {"op": "proposals"})
        self.assertEqual(len(out["result"]["proposals"]), 1)
        self.assertIsNone(out["branch"])
        # Addressed reads still fail closed without a branch.
        with self.assertRaises(Denied):
            ctx.handle("vcs_read", {"op": "status"})


class ProposalRecordTests(Fixture):
    def test_record_creates_dedupes_and_revises_on_change(self):
        first = vcs.record_proposal_state(self.project, proposal())
        self.assertTrue(first["changed"])
        stored = self.project.insights.read(first["id"])
        self.assertEqual(stored["source"], "vcs")
        self.assertIn(SHA_A, stored["body"])
        self.assertNotIn("https://forge", stored["body"])
        # Identical facts are a read-only no-op: same id, no rev bump.
        repeat = vcs.record_proposal_state(
            self.project, proposal(url="https://forge/x/rerun/9"))
        self.assertEqual(repeat, {"id": first["id"], "changed": False})
        self.assertEqual(self.project.insights.read(first["id"])["rev"],
                         stored["rev"])
        # Head movement revises under the stable id with a rev bump.
        moved = vcs.record_proposal_state(
            self.project, proposal(head_sha=SHA_B))
        self.assertEqual(moved["id"], first["id"])
        self.assertTrue(moved["changed"])
        self.assertEqual(self.project.insights.read(first["id"])["rev"],
                         stored["rev"] + 1)
        # Terminal state change revises too, keeping one record per proposal.
        merged = vcs.record_proposal_state(
            self.project, proposal(head_sha=SHA_B, state="merged"))
        self.assertEqual(merged["id"], first["id"])
        self.assertIn("merged", self.project.insights.read(first["id"])["title"])
        pending = [i for i in self.project.insights.list(pending=True, limit=1000)
                   if i["id"] == first["id"]]
        self.assertEqual(len(pending), 1)

    def test_record_rejects_bad_proposals(self):
        with self.assertRaises(Denied):
            vcs.record_proposal_state(self.project, dict(proposal(), id=""))
        with self.assertRaises(Denied):
            vcs.record_proposal_state(self.project, {"id": "x"})

    def test_stable_id_ignores_facts(self):
        self.assertEqual(vcs.proposal_insight_id("forge:owner/repo#1"),
                         vcs.proposal_insight_id("forge:owner/repo#1"))
        self.assertNotEqual(vcs.proposal_insight_id("forge:owner/repo#1"),
                            vcs.proposal_insight_id("forge:owner/repo#2"))
        with self.assertRaises(Denied):
            vcs.proposal_insight_id("")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
