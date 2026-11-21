"""External proposal observation: normalized reads plus change-gated records. Offline only."""
import dataclasses
import json
import sys
import unittest
from pathlib import Path

from support import Fixture
from mizu import vcs
from mizu.errors import Denied
from mizu.fs import PREVIEW_BYTES, read_json
from mizu.runtime import poll_proposals, poll_project_vcs, refresh_poll_state
from unittest import mock

SHA_A = "a" * 40
SHA_B = "b" * 40

#: The default config declares this host-writable queue. Proposal observations
#: are appended there instead of being kept as in-memory insight facts.
OBSERVATION_QUEUE = "proposals"


def observe_into(fixture):
    """Bind the project to a declared observation queue for record assertions."""
    fixture.project.config = dataclasses.replace(
        fixture.project.config,
        vcs={**fixture.project.config.vcs, "observation_queue": OBSERVATION_QUEUE})


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
    def setUp(self):
        super().setUp()
        observe_into(self)

    def test_record_rejects_bad_proposals(self):
        with self.assertRaises(Denied):
            vcs.record_proposal_state(self.project, dict(proposal(), id=""))
        with self.assertRaises(Denied):
            vcs.record_proposal_state(self.project, {"id": "x"})

    def test_facts_derive_stable_queue_message_id(self):
        row = proposal()
        first = vcs.record_proposal_state(self.project, row)
        second = vcs.record_proposal_state(self.project, row)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertRegex(first["id"], r"^[0-9a-f]{32}$")
        changed = dict(row, checks=[])
        updated = vcs.record_proposal_state(self.project, changed)
        self.assertTrue(updated["changed"])
        self.assertNotEqual(updated["id"], first["id"])
        row = proposal()
        row["id"] = "x" * 128
        title, _ = vcs.proposal_facts(vcs.parse_proposals({"proposals": [row]})[0])
        self.assertLessEqual(len(title), vcs.MAX_PROPOSAL_TITLE_CHARS)
        self.assertEqual(title, f"Proposal {'x' * 128} open")
        with self.assertRaises(Denied):
            vcs.parse_proposals({"proposals": [dict(row, id="")]})


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
    def setUp(self):
        super().setUp()
        observe_into(self)

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

    def test_proposal_cursor_schema_accepts_its_shared_maximum(self):
        cursor = "x" * vcs.MAX_PROPOSAL_CURSOR
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(proposals_code({
                "proposals": [], "complete": False, "cursor": cursor})))
        out = vcs.read_via(config.vcs, "proposals", {"cursor": cursor})
        self.assertEqual(out["cursor"], cursor)
        from mizu.vcs_proposal_poll import _valid_proposal_cursor
        self.assertEqual(_valid_proposal_cursor(cursor), cursor)
        self.assertIsNone(_valid_proposal_cursor(cursor + "x"))

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
            vcs.record_proposal_state(self.project,
                                      dict(base, id="forge:owner/repo#1\u202e"))

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
    def setUp(self):
        super().setUp()
        observe_into(self)

    def _config(self, **updates):
        settings = {**self.config.vcs, "command": ["offline-vcs"],
                    "poll_enabled": True, "poll_proposals_enabled": True,
                    "observation_queue": OBSERVATION_QUEUE, **updates}
        return dataclasses.replace(self.config, vcs=settings)

    def test_proposal_poll_requires_both_opt_ins_and_adapter(self):
        calls = []
        reader = lambda settings, op, params: calls.append((op, params)) or {
            "proposals": [], "complete": True, "cursor": None}
        state = {}
        for config in (self._config(observation_queue=""),
                       self._config(poll_enabled=False),
                       self._config(poll_proposals_enabled=False),
                       self._config(command=[])):
            self.assertIsNone(poll_proposals(config, self.project, state,
                                             now=0, interval=15, reader=reader))
        self.assertEqual(calls, [])
        self.assertNotIn("last_proposal_poll", state)
        event = poll_proposals(self._config(), self.project, state, now=0,
                               interval=15, reader=reader)
        self.assertTrue(event["ok"])
        self.assertEqual(calls, [("proposals", {})])

    def test_complete_inventory_survives_restart_and_pages_without_event_exposure(self):
        row = proposal()
        expected = {row["id"]: ["feature", "main"]}
        responses = [
            {"proposals": [row], "complete": True},
            {"proposals": [], "complete": False, "cursor": "next-page"},
            {"proposals": [], "complete": True},  # Terminal page, not enumeration.
            {"proposals": [], "complete": True},
        ]
        with (mock.patch("mizu.runtime.poll_upstream", return_value=None),
              mock.patch("mizu.runtime.poll_ci", return_value=None),
              mock.patch("mizu.runtime.ci_waits.poll", return_value=None),
              mock.patch("mizu.vcs.read_via", side_effect=responses)):
            for tick, inventory in enumerate((expected, expected, expected, {})):
                # Each tick starts with a new daemon's empty memory.
                state = {}
                events = poll_project_vcs(self._config(), self.project, state,
                                          now=tick * 30, interval=15)
                self.assertTrue(events[0]["ok"], events)
                self.assertNotIn("proposal_refs", json.dumps(events))
                self.assertEqual(state["proposal_refs"], inventory)
                self.assertEqual(read_json(self.project.root / "vcs-poll.json")[
                    "proposal_refs"], inventory)

    def test_refresh_proposal_refs_is_authoritative_when_empty(self):
        # An authoritative empty map clears stale refs; an absent or corrupt
        # one leaves them in place, which only preserves more branches against
        # retirement. Either way an old daemon cannot revive cleared refs.
        stale = {"stale": ["feature"]}
        for shared, expected in (
                ({}, stale),
                ([], stale),
                ({"proposal_refs": None}, stale),
                ({"proposal_refs": {}}, {}),
                ({"proposal_refs": {"open": ["mizu/x-1"]}},
                 {"open": ["mizu/x-1"]})):
            with self.subTest(shared=shared):
                state = {"proposal_refs": dict(stale)}
                refresh_poll_state(state, shared)
                self.assertEqual(state["proposal_refs"], expected)

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

    def test_poll_error_diagnostic_is_bounded(self):
        from mizu.vcs_proposal_poll import MAX_PROPOSAL_POLL_ERROR_CHARS
        state = {}

        def fail(settings, op, params):
            raise Denied("x" * (MAX_PROPOSAL_POLL_ERROR_CHARS + 50))

        event = poll_proposals(self._config(), self.project, state, now=76.0,
                               interval=15.0, reader=fail)
        self.assertFalse(event["ok"])
        self.assertEqual(event["error"], "x" * MAX_PROPOSAL_POLL_ERROR_CHARS)

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
