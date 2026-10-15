"""External proposal observation: normalized reads plus change-gated records. Offline only."""
import dataclasses
import json
import sys
import unittest
from pathlib import Path

from support import Fixture
from mizu import vcs
from mizu.errors import Denied
from mizu.runtime import poll_proposals, poll_project_vcs
from unittest import mock

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

    def test_tool_passes_cursor_through(self):
        # Regression: the cursor reached read_via but the tool schema
        # denied it, so Context.handle never dispatched. The adapter
        # echoes the request cursor; the envelope must surface it.
        code = ("import sys,json; req=json.load(sys.stdin); "
                "print(json.dumps({'proposals': [], "
                "'cursor': 'next:' + req.get('cursor', ''), "
                "'complete': False}))")
        import uuid
        from mizu.fs import mkdir
        from mizu.runtime import Context
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(code))
        role = config.roles["worker"]
        role = dataclasses.replace(
            role, capabilities=tuple(list(role.capabilities) + ["vcs_read"]))
        config = dataclasses.replace(
            config, roles={**config.roles, "worker": role})
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        ctx = Context(config, self.project, role, run,
                      self.project.snapshots.get(), self.project.workspace)
        out = ctx.handle("vcs_read", {"op": "proposals", "cursor": "c1"})
        self.assertEqual(out["result"]["cursor"], "next:c1")
        self.assertFalse(out["result"]["complete"])


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


def tombstoned_proposal(state="closed"):
    row = proposal(state=state)
    row["head"] = {"deleted": True, "repo": "fork-owner/repo",
                   "ref": "feature"}
    row["checks"] = []
    return row


def reviewed_proposal():
    row = proposal()
    row["reviews"] = [{"reviewer": "octo", "verdict": "approved",
                       "sha": SHA_A}]
    return row


class ProposalSchemaV2Tests(Fixture):
    def test_deleted_fork_head_is_explicit_tombstone(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                proposals_code({"proposals": [tombstoned_proposal()]})))
        out = vcs.read_via(config.vcs, "proposals", {})
        (row,) = out["proposals"]
        self.assertTrue(row["head"].get("deleted"))
        self.assertNotIn("sha", row["head"])
        title, body = vcs.proposal_facts(row)
        self.assertIn("head: fork-owner/repo feature deleted", body)
        self.assertIn("closed", title)

    def test_tombstone_forms_refused(self):
        bad_heads = ({"deleted": False, "repo": "r", "ref": "f"},
                     {"deleted": True, "repo": "r", "ref": "f",
                      "sha": SHA_A},
                     {"deleted": True, "repo": "r"},
                     {"repo": "r", "ref": "f"})
        for head in bad_heads:
            row = proposal()
            row["head"] = head
            with self.assertRaises(Denied, msg=json.dumps(head)):
                vcs.parse_proposals({"proposals": [row]})

    def test_deleted_fork_does_not_block_open_proposals(self):
        payload = {"proposals": [tombstoned_proposal(), proposal()],
                   "complete": True}
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(proposals_code(payload)))
        out = vcs.read_via(config.vcs, "proposals", {})
        self.assertEqual(len(out["proposals"]), 2)
        self.assertTrue(out["complete"])
        # The open row still records under its stable id.
        recorded = vcs.record_proposal_state(self.project, out["proposals"][1])
        self.assertTrue(recorded["changed"])
        stored = self.project.insights.read(recorded["id"])
        self.assertIn(SHA_A, stored["body"])

    def test_reviews_are_revision_bound(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                proposals_code({"proposals": [reviewed_proposal()]})))
        out = vcs.read_via(config.vcs, "proposals", {})
        (row,) = out["proposals"]
        self.assertEqual(row["reviews"],
                         [{"reviewer": "octo", "verdict": "approved",
                           "sha": SHA_A}])
        _, body = vcs.proposal_facts(row)
        self.assertIn(f"review: octo approved {SHA_A}", body)

    def test_stale_review_sha_stays_put(self):
        row = reviewed_proposal()
        row["head"]["sha"] = SHA_B
        parsed = vcs.parse_proposals({"proposals": [row]})[0]
        self.assertEqual(parsed["reviews"][0]["sha"], SHA_A)
        self.assertEqual(parsed["head"]["sha"], SHA_B)

    def test_missing_reviews_read_unknown_not_none(self):
        _, body = vcs.proposal_facts(proposal())
        self.assertIn("reviews: unknown", body)
        _, body = vcs.proposal_facts(dict(proposal(), reviews=[]))
        self.assertIn("reviews: none", body)
        for reviews in ([{"reviewer": "", "verdict": "approved",
                          "sha": SHA_A}],
                        [{"reviewer": "o", "verdict": "commented",
                          "sha": SHA_A}],
                        [{"reviewer": "o", "verdict": "approved",
                          "sha": "xyz"}],
                        [{"reviewer": "o", "verdict": "approved"}],
                        "yes",
                        [{}] * 65):
            with self.assertRaises(Denied):
                vcs.parse_proposals(
                    {"proposals": [dict(proposal(), reviews=reviews)]})

    def test_check_sha_keeps_tested_revision_provenance(self):
        # Rollup checks may run on a merge commit, not the head sha: the
        # row keeps the tested revision instead of reattaching it.
        row = proposal()
        row["checks"] = [{"check": "rollup", "state": "success",
                          "sha": SHA_B}]
        parsed = vcs.parse_proposals({"proposals": [row]})[0]
        self.assertEqual(parsed["checks"][0]["sha"], SHA_B)
        _, body = vcs.proposal_facts(row)
        self.assertIn(f"check: rollup success {SHA_B}", body)

    def test_completeness_and_cursor_round_trip(self):
        code = ("import sys,json; req=json.load(sys.stdin); "
                "assert req == {'op': 'proposals', 'cursor': 'c1'}, req; "
                "print(json.dumps({'proposals': [], 'complete': False, "
                "'cursor': 'c2'}))")
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(code))
        out = vcs.read_via(config.vcs, "proposals", {"cursor": "c1"})
        self.assertFalse(out["complete"])
        self.assertEqual(out["cursor"], "c2")

    def test_partial_envelope_shapes_refused(self):
        for payload in ({"proposals": [], "complete": "yes"},
                        {"proposals": [], "cursor": ""},
                        {"proposals": [], "cursor": "x" * 257}):
            config = dataclasses.replace(
                self.config, vcs=adapter_settings(proposals_code(payload)))
            with self.assertRaises(Denied, msg=json.dumps(payload)[:60]):
                vcs.read_via(config.vcs, "proposals", {})

    def test_terminal_and_bidi_controls_refused(self):
        base = proposal()
        for identity in ("1\r\x1b[8m", "forge:owner/repo#\u202e", "x\x7f"):
            row = dict(base, id=identity)
            with self.assertRaises(Denied, msg=repr(identity)):
                vcs.parse_proposals({"proposals": [row]})
        row = reviewed_proposal()
        row["reviews"] = [{"reviewer": "mallory\u202e", "verdict": "approved",
                           "sha": SHA_A}]
        with self.assertRaises(Denied):
            vcs.parse_proposals({"proposals": [row]})
        with self.assertRaises(Denied):
            vcs.parse_proposals({"proposals": [dict(base, url="https://x/\r")]})
        with self.assertRaises(Denied):
            vcs.read_via(self.config.vcs, "proposals", {"cursor": "c\x1b"})
        with self.assertRaises(Denied):
            vcs.proposal_insight_id("forge:owner/repo#1\u202e")

    def test_current_shape_still_validates(self):
        # The working adapter shape gains unknown reviews/completeness,
        # never a validation failure.
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                proposals_code({"proposals": [proposal()]})))
        out = vcs.read_via(config.vcs, "proposals", {})
        self.assertIsNone(out["complete"])
        self.assertIsNone(out["cursor"])
        self.assertIsNone(out["proposals"][0]["reviews"])

class ProposalPollingTests(Fixture):
    def _config(self, **updates):
        settings = {**self.config.vcs, "command": ["offline-vcs"],
                    "poll_enabled": True, "poll_proposals_enabled": True,
                    **updates}
        return dataclasses.replace(self.config, vcs=settings)

    def test_proposal_poll_requires_both_opt_ins_and_adapter(self):
        calls = []
        reader = lambda settings, op, params: calls.append((op, params)) or {
            "proposals": [], "complete": True, "cursor": None}
        state = {}
        for config in (self._config(poll_proposals_enabled=False),
                       self._config(poll_enabled=False),
                       self._config(command=[])):
            self.assertIsNone(poll_proposals(config, self.project, state,
                                             now=0, interval=15, reader=reader))
        self.assertEqual(calls, [])
        self.assertNotIn("last_proposal_poll", state)
        event = poll_proposals(self._config(), self.project, state, now=0,
                               interval=15, reader=reader)
        self.assertTrue(event["ok"])
        self.assertEqual(calls, [("proposals", {})])

    def test_complete_observations_dedupe_and_revise_on_changed_facts(self):
        config = self._config()
        state = {}
        rows = [proposal(), proposal(), proposal(head_sha=SHA_B)]
        calls = []

        def reader(settings, op, params):
            calls.append((op, params))
            return {"proposals": [rows.pop(0)], "complete": True,
                    "cursor": None}

        first = poll_proposals(config, self.project, state, now=0,
                                interval=10, reader=reader)
        self.assertEqual(first["event"], "proposal_poll")
        self.assertTrue(first["ok"])
        self.assertEqual(first["observed"], 1)
        self.assertEqual(first["changed"], 1)
        insight_id = vcs.proposal_insight_id("forge:owner/repo#1")
        initial = self.project.insights.read(insight_id)
        self.assertEqual(initial["source"], "vcs")

        duplicate = poll_proposals(config, self.project, state, now=10,
                                   interval=10, reader=reader)
        self.assertEqual(duplicate["changed"], 0)
        self.assertEqual(self.project.insights.read(insight_id)["rev"],
                         initial["rev"])

        revised = poll_proposals(config, self.project, state, now=20,
                                 interval=10, reader=reader)
        self.assertEqual(revised["changed"], 1)
        current = self.project.insights.read(insight_id)
        self.assertEqual(current["rev"], initial["rev"] + 1)
        self.assertIn(SHA_B, current["body"])
        self.assertEqual(len(calls), 3)

    def test_partial_page_cursor_persists_and_resumes_across_daemon_state(self):
        open_row = json.dumps(proposal())
        merged_row = json.dumps(proposal(state="merged"))
        code = f'''import json, sys
request = json.load(sys.stdin)
open_row = json.loads({open_row!r})
merged_row = json.loads({merged_row!r})
if request["op"] == "fetch":
    response = {{"refs": {{}}}}
elif request["op"] == "status":
    response = {{"checks": []}}
elif request["op"] == "proposals":
    if request.get("cursor") == "next-page-token":
        response = {{"proposals": [merged_row], "complete": True}}
    else:
        response = {{"proposals": [open_row], "complete": False,
                    "cursor": "next-page-token"}}
else:
    raise SystemExit("unexpected operation")
print(json.dumps(response))
'''
        config = dataclasses.replace(
            self.config,
            vcs={**self.config.vcs, "command": [sys.executable, "-c", code],
                 "poll_enabled": True, "poll_proposals_enabled": True})
        first_state = {"last_fetch": None}
        first_events = poll_project_vcs(config, self.project, first_state,
                                        now=100.0, interval=15.0)
        first_proposal = next(e for e in first_events
                              if e["event"] == "proposal_poll")
        self.assertTrue(first_proposal["ok"])
        self.assertTrue(first_proposal["has_cursor"])
        shared = json.loads((self.project.root / "vcs-poll.json").read_text())
        self.assertEqual(shared["proposal_cursor"], "next-page-token")

        # A new in-memory daemon state must resume from the persisted token.
        second_state = {"last_fetch": None}
        second_events = poll_project_vcs(config, self.project, second_state,
                                         now=115.0, interval=15.0)
        second_proposal = next(e for e in second_events
                               if e["event"] == "proposal_poll")
        self.assertTrue(second_proposal["ok"])
        self.assertFalse(second_proposal["has_cursor"])
        stored = self.project.insights.read(
            vcs.proposal_insight_id("forge:owner/repo#1"))
        self.assertIn("Proposal forge:owner/repo#1 merged", stored["title"])
        shared = json.loads((self.project.root / "vcs-poll.json").read_text())
        self.assertIsNone(shared["proposal_cursor"])

    def test_missing_proposal_row_does_not_change_open_observation(self):
        existing = vcs.record_proposal_state(self.project, proposal())
        before = self.project.insights.read(existing["id"])
        event = poll_proposals(
            self._config(), self.project, {}, now=1.0, interval=15.0,
            reader=lambda settings, op, params: {
                "proposals": [], "complete": True, "cursor": None})
        self.assertTrue(event["ok"])
        after = self.project.insights.read(existing["id"])
        self.assertEqual(after["title"], before["title"])
        self.assertEqual(after["body"], before["body"])
        self.assertEqual(after["rev"], before["rev"])
        self.assertIn("open", after["title"])

    def test_non_advancing_cursor_is_visible_and_preserves_cursor(self):
        state = {"proposal_cursor": "same-token"}
        event = poll_proposals(
            self._config(), self.project, state, now=50.0, interval=15.0,
            reader=lambda settings, op, params: {
                "proposals": [], "complete": False, "cursor": "same-token"})
        self.assertFalse(event["ok"])
        self.assertIn("did not advance", event["error"])
        self.assertEqual(state["proposal_cursor"], "same-token")
        self.assertEqual(state["last_proposal_poll"], 50.0)

    def test_recording_failure_is_visible_and_keeps_input_cursor(self):
        state = {"proposal_cursor": "useful-token"}
        reader = lambda settings, op, params: {
            "proposals": [proposal()], "complete": False,
            "cursor": "next-token"}
        with mock.patch("mizu.runtime._vcs.record_proposal_state",
                        side_effect=OSError("insight storage unavailable")):
            event = poll_proposals(self._config(), self.project, state,
                                   now=60.0, interval=15.0, reader=reader)
        self.assertFalse(event["ok"])
        self.assertIn("insight storage unavailable", event["error"])
        self.assertEqual(event["recorded"], 0)
        self.assertEqual(state["proposal_cursor"], "useful-token")
        self.assertEqual(state["last_proposal_poll"], 60.0)
        self.assertEqual(event["trust"], "external-untrusted")

    def test_poll_failure_is_visible_and_preserves_cursor(self):
        state = {"proposal_cursor": "useful-token"}
        calls = []

        def fail(settings, op, params):
            calls.append((op, params))
            raise Denied("adapter unavailable")

        config = self._config()
        event = poll_proposals(config, self.project, state, now=77.0,
                               interval=15.0, reader=fail)
        self.assertEqual(event["event"], "proposal_poll")
        self.assertFalse(event["ok"])
        self.assertIn("adapter unavailable", event["error"])
        self.assertEqual(event["observed"], 0)
        self.assertEqual(state["last_proposal_poll"], 77.0)
        self.assertEqual(state["proposal_cursor"], "useful-token")
        self.assertEqual(calls, [("proposals", {"cursor": "useful-token"})])
        self.assertIsNone(poll_proposals(config, self.project, state,
                                         now=78.0, interval=15.0, reader=fail))
        self.assertEqual(len(calls), 1)

    def test_recording_failure_keeps_page_cursor_for_retry(self):
        state = {"proposal_cursor": "old-token"}
        with mock.patch("mizu.vcs.record_proposal_state",
                        side_effect=Denied("recording unavailable")):
            event = poll_proposals(
                self._config(), self.project, state, now=1.0, interval=15.0,
                reader=lambda settings, op, params: {
                    "proposals": [proposal()], "complete": False,
                    "cursor": "new-token"})
        self.assertFalse(event["ok"])
        self.assertIn("recording unavailable", event["error"])
        self.assertEqual(state["proposal_cursor"], "old-token")

    def test_poll_state_write_failure_is_visible_for_proposal_tick(self):
        def proposal_tick(config, project, state, *, now, interval):
            state["last_proposal_poll"] = now
            state["proposal_cursor"] = "next-token"
            return {"event": "proposal_poll", "ok": True,
                    "observed": 0, "recorded": 0, "changed": 0,
                    "complete": False, "has_cursor": True}

        with (mock.patch("mizu.runtime.poll_upstream", return_value=None),
              mock.patch("mizu.runtime.poll_ci", return_value=None),
              mock.patch("mizu.runtime.poll_proposals", side_effect=proposal_tick),
              mock.patch("mizu.runtime.write_json", side_effect=OSError("disk full"))):
            events = poll_project_vcs(self._config(), self.project, {},
                                      now=3.0, interval=15.0)
        event = next(item for item in events if item["event"] == "proposal_poll")
        self.assertFalse(event["ok"])
        self.assertIn("Could not persist proposal poll state", event["error"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
