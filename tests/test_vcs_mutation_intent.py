"""Mutation evidence at the dispatch boundary; no external VCS or timing races."""
import dataclasses
import json
import sys
from unittest.mock import patch

from support import Fixture
import test_vcs_dispose as dispose
from test_vcs_publish import init_publish_git
from mizu.publication import Publication
from mizu import fs, vcs
from mizu.errors import Denied
from mizu.process import Result
from mizu.runtime import write_json

CASES = ("push", "pr", "retire", "close", "merge", "close_if_adopted", "refusal")


class MutationIntentTests(Fixture):
    def mutation(self, case):
        """Use real authorization/approval and parsers, mocking only process.run."""
        op = "close_if_adopted" if case == "refusal" else case
        capability = ("vcs_publish" if op in ("push", "pr") else
                      "vcs_retire" if op == "retire" else "vcs_dispose")
        config, role = dispose.with_caps(self, "worker", [capability])
        settings = {"command": [sys.executable, "-c", "raise SystemExit(99)"],
                    "timeout_seconds": 5, "max_bytes": 524288,
                    "retire_grant": True, "close_grant": True, "merge_grant": True,
                    "owned_prefixes": ["mizu/"], "protected_refs": ["main"]}
        config = dataclasses.replace(config, vcs=settings)
        if capability == "vcs_publish":
            source = init_publish_git(self)
            args = {"op": op, "branch": "mizu/" + op}
        elif op == "retire":
            args = {"branch": "mizu/x-1", "expected_sha": dispose.SHA_A}
        elif op == "close_if_adopted":
            args = dispose.close_if_args()
        else:
            args = dispose.close_args(op=op)
        parameters = {key: value for key, value in args.items() if key != "op"}
        if op in ("push", "pr", "merge"):
            code_digest = dispose.current_digest(self)
            endpoints = ({"target": args["target"], "proposal_id": args["id"],
                          "sha": args["sha"], "base": args["base"]}
                         if op == "merge" else {})
            approval = dispose.approve(self, args["branch"], code_digest, **endpoints)
            parameters.update(code_digest=code_digest, approval=approval["id"])
        if capability == "vcs_publish":
            from mizu.distribution import public_source_identity
            _captured = self.project.snapshots.capture_files(self.project.workspace)
            _projection = public_source_identity(_captured.get("files", {}))
            publication = Publication(self.project.root, source, code_digest, args["branch"], op,
                                      projection=_projection)
            parameters.update(source_commit=source, publication_id=publication.id,
                              projection_schema=_projection["schema"],
                              projection_algorithm=_projection["algorithm"],
                              projection_digest=_projection["digest"])
        ctx = dispose.make_context(self, config, role)
        return ctx, capability, args, {"op": op, "parameters": parameters}

    def pending(self, ctx, capability):
        path, = (ctx.run_dir / capability.replace("_", "-")).glob("*.json")
        record = json.loads(path.read_bytes())
        self.assertEqual(record["outcome"], "unknown")
        self.assertEqual(path, ctx.run_dir / record["evidence"])
        self.assertEqual(path.stem, record["evidence_id"])
        self.assertEqual(set(record), {"intent", "outcome", "evidence_id", "evidence"})
        return path, record

    def reply(self, request, case):
        op = request["op"]
        if op in ("push", "pr"):
            data = {"digest": request["digest"], "pushed": True,
                    "projection_schema": request["projection_schema"],
                    "projection_algorithm": request["projection_algorithm"],
                    "projection_digest": request["projection_digest"],
                    "publication_id": request["publication_id"], "published_sha": dispose.SHA_A}
        elif op == "retire":
            data = {"branch": request["branch"], "sha": request["expected_sha"],
                    "deleted": True, "live_refs": []}
        elif op == "close_if_adopted":
            data = dispose.close_if_receipt(closed=case != "refusal")
        else:
            data = {key: request[key] for key in ("id", "sha", "base", "target")}
            data["state"] = "merged" if op == "merge" else "closed"
        return Result(0, json.dumps(data), "", "exited", 0)

    def test_intent_visible_before_dispatch_and_same_identity_after_observation(self):
        for case in CASES:
            with self.subTest(case=case):
                ctx, capability, args, intent = self.mutation(case)
                seen = []

                def dispatch(*_args, **kwargs):
                    path, pending = self.pending(ctx, capability)
                    self.assertEqual(pending["intent"], intent)
                    sync.assert_called_once_with(ctx.run_dir)
                    seen.append((path, pending))
                    return self.reply(json.loads(kwargs["input_data"]), case)

                with patch.object(vcs, "run", side_effect=dispatch) as run, patch(
                        "mizu.evidence_records.sync_dir", wraps=fs.sync_dir) as sync:
                    out = ctx.handle(capability, args)
                sync.assert_called_once_with(ctx.run_dir)
                run.assert_called_once()
                path, pending = seen[0]
                self.assertEqual(out["evidence_id"], pending["evidence_id"])
                self.assertEqual(out["evidence"], pending["evidence"])
                self.assertEqual(out["intent"], pending["intent"])
                self.assertEqual(out["outcome"], "observed")
                self.assertEqual(json.loads(path.read_bytes()), out)
                self.assertEqual(list(path.parent.glob("*.json")), [path])
                if case == "refusal":
                    self.assertFalse(out["disposed"])
                    self.assertFalse(out["adapter_receipt"]["closed"])

    def test_adapter_exception_interruption_and_invalid_observation_leave_unknown(self):
        for case in CASES:
            for failure in (OSError("adapter unavailable"), KeyboardInterrupt(),
                            Result(None, "", "", "cancelled", 0),
                            Result(0, "{}", "", "exited", 0)):
                with self.subTest(case=case, failure=type(failure).__name__):
                    ctx, capability, args, _ = self.mutation(case)
                    seen = []

                    def dispatch(*_args, **_kwargs):
                        seen.append(self.pending(ctx, capability))
                        if isinstance(failure, BaseException):
                            raise failure
                        return failure

                    with patch.object(vcs, "run", side_effect=dispatch) as run:
                        with self.assertRaises((Denied, KeyboardInterrupt)):
                            ctx.handle(capability, args)
                    run.assert_called_once()
                    path, pending = seen[0]
                    self.assertEqual(json.loads(path.read_bytes()), pending)
                    if case in ("close_if_adopted", "refusal"):
                        with patch.object(vcs, "run") as retry:
                            with self.assertRaisesRegex(Denied, "already attempted"):
                                ctx.handle(capability, args)
                        retry.assert_not_called()
                        self.assertEqual(json.loads(path.read_bytes()), pending)

    def test_observation_write_failure_preserves_intent_even_after_replace(self):
        for case in CASES:
            for late in (False, True):
                with self.subTest(case=case, late=late):
                    ctx, capability, args, _ = self.mutation(case)
                    seen = []

                    def dispatch(*_args, **kwargs):
                        seen.append(self.pending(ctx, capability))
                        return self.reply(json.loads(kwargs["input_data"]), case)

                    def fail_observation(path, record, **kwargs):
                        if record.get("outcome") == "observed":
                            if late:
                                write_json(path, record, **kwargs)
                            raise OSError("final evidence write failed")
                        write_json(path, record, **kwargs)

                    with patch.object(vcs, "run", side_effect=dispatch) as run, patch(
                            "mizu.evidence_records.write_json", side_effect=fail_observation):
                        with self.assertRaisesRegex(OSError, "final evidence write failed"):
                            ctx.handle(capability, args)
                    run.assert_called_once()
                    path, pending = seen[0]
                    self.assertEqual(json.loads(path.read_bytes()), pending)
                    self.assertEqual(list(path.parent.glob("*.json")), [path])
                    if case in ("close_if_adopted", "refusal"):
                        with self.assertRaisesRegex(Denied, "already attempted"):
                            ctx.handle(capability, args)

    def test_intent_persistence_failure_prevents_dispatch(self):
        for case in CASES:
            with self.subTest(case=case):
                ctx, capability, args, _ = self.mutation(case)
                # Fail the actual atomic publication, not just the helper:
                # no partial destination file may be exposed.
                def fail_intent(path, value, **kwargs):
                    with patch.object(fs.os, "fsync", side_effect=OSError("storage failed")):
                        write_json(path, value, **kwargs)

                with patch("mizu.evidence_records.write_json", side_effect=fail_intent), patch.object(vcs, "run") as run:
                    with self.assertRaisesRegex(Denied, "storage failed"):
                        ctx.handle(capability, args)
                run.assert_not_called()
                self.assertEqual(list((ctx.run_dir / capability.replace("_", "-")).glob("*.json")), [])
                if case in ("close_if_adopted", "refusal"):
                    with self.assertRaisesRegex(Denied, "already attempted"):
                        ctx.handle(capability, args)

    def test_local_refusals_leave_no_intent_and_do_not_dispatch(self):
        for case in CASES:
            for reason in ("capability", "workspace", "pin", "adapter", "bytes", "authority"):
                with self.subTest(case=case, reason=reason):
                    ctx, capability, args, _ = self.mutation(case)
                    op = args.get("op")
                    if reason == "capability":
                        ctx.role = dataclasses.replace(ctx.role, capabilities=())
                    elif reason == "workspace":
                        ctx.role = dataclasses.replace(ctx.role, workspace="none")
                    elif reason == "pin":
                        key = ("branch" if capability == "vcs_publish" else
                               "expected_sha" if case == "retire" else
                               "head_sha" if op == "close_if_adopted" else "sha")
                        args[key] = "../bad"
                    elif reason in ("adapter", "bytes"):
                        settings = {**ctx.config.vcs, **({"command": []} if reason == "adapter" else {"max_bytes": 1})}
                        ctx.config = dataclasses.replace(ctx.config, vcs=settings)
                    elif capability == "vcs_publish" or case == "merge":
                        # Real approval check must fail against changed content.
                        (ctx.workspace / "app.py").write_bytes((ctx.workspace / "app.py").read_bytes() + b"# changed\n")
                    else:
                        grant = "retire_grant" if case == "retire" else "close_grant"
                        ctx.config = dataclasses.replace(ctx.config, vcs={**ctx.config.vcs, grant: False})
                    with patch.object(vcs, "run") as run:
                        with self.assertRaises(Denied):
                            ctx.handle(capability, args)
                    run.assert_not_called()
                    self.assertEqual(list((ctx.run_dir / capability.replace("_", "-")).glob("*.json")), [])
                    self.assertFalse(ctx.conditional_close_attempted)

    def test_unrepresentable_snapshot_refuses_before_intent(self):
        for case in ("push", "pr", "merge"):
            with self.subTest(case=case):
                ctx, capability, args, _ = self.mutation(case)
                with patch.object(ctx.project.snapshots, "capture_files", return_value={"skipped": ["unsafe"]}), patch.object(
                        vcs, "run") as run:
                    with self.assertRaisesRegex(Denied, "representable snapshot"):
                        ctx.handle(capability, args)
                run.assert_not_called()
                self.assertEqual(list((ctx.run_dir / capability.replace("_", "-")).glob("*.json")), [])
