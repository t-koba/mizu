"""Real wait stores and fake model dispatch: no provider, CI or sandbox claims."""
import dataclasses
import json
import os
from unittest.mock import patch

from support import Fixture, ScriptDriver, delivered_ack_tokens, finish_with_decision_acks
from test_waits import _defer, _past
from test_dependencies import _submit, _wait
from mizu import wait_events as events
from mizu.errors import Denied, LimitExceeded
from mizu.fs import read_json, write_json
from mizu.project import Project
from mizu.runtime import Engine, prompt_for, prompt_delta_for


class WaitReceiptTests(Fixture):
    def config_for(self, role="reviewer"):
        roles = dict(self.config.roles)
        roles[role] = dataclasses.replace(roles[role], on_change=False)
        return dataclasses.replace(self.config, roles=roles)

    def seed(self, role="reviewer", count=3):
        result = _submit(self.project, "searcher", "Result")
        for i in range(count):
            _defer(self.project, role, {"kind": "deadline", "at": _past()}, title=f"Due {i}")
            _defer(self.project, "searcher", _wait(recipient=role, target=result["id"]), title=f"Need {i}")

    def rows(self, project=None, role="reviewer"):
        project = project or self.project
        return (project.insights.due_waits(role, project.snapshots.get()),
                project.insights.pending_obligations(role))

    def finish(self, ctx, **args):
        return ctx.handle("finish", {"outcome": "wait", "summary": "Handled selections",
                                     "state": "Remaining work is explicit", **args})

    def test_partial_empty_omitted_restart_and_idempotent_exact_receipts(self):
        self.seed()
        before = self.rows()
        config = self.config_for()
        for selection in (None, []):
            def no_ack(ctx, prompt, profile=None):
                self.finish(ctx, **({} if selection is None else {"ack_events": selection}))
            Engine(config, driver=ScriptDriver(no_ack)).run(self.project, "reviewer")
            self.assertEqual(self.rows(), before)
        def partial(ctx, prompt, profile=None):
            data = json.loads(prompt)
            self.finish(ctx, ack_events=[data[k][1]["ack_token"] for k in ("wait_events", "obligations")])
        Engine(config, driver=ScriptDriver(partial)).run(self.project, "reviewer")
        restarted = Project(config, "sample")
        expected = tuple([rows[0], rows[2]] for rows in before)
        self.assertEqual(self.rows(restarted), expected)
        path = events._path(self.project.root, "reviewer", before[1][1])
        retained = path.read_bytes()
        events.acknowledge_obligations(self.project.root, "reviewer", [before[1][1]])
        self.assertEqual(path.read_bytes(), retained)
        self.assertEqual(restarted.insights.consume_waits("reviewer", [before[0][1]]), 0)
        # Receipt filtering happens before the limit, so acknowledged heads cannot starve later rows.
        self.assertEqual(restarted.insights.pending_obligations("reviewer", limit=1), [before[1][0]])
        Engine(config, driver=ScriptDriver(finish_with_decision_acks)).run(restarted, "reviewer")
        self.assertEqual(self.rows(restarted), ([], []))

    def test_legacy_registrations_are_explicitly_consumed_and_receipted(self):
        self.seed(count=1)
        for group in self.rows():
            path = self.project.root / "waits" / f"{group[0]['insight']}.json"
            stored = read_json(path)
            stored.pop("registration_id")
            write_json(path, stored)
        before = self.rows()
        for group in before:
            self.assertTrue(group[0]["registration_id"].startswith("legacy:"))
            self.assertLess(len(group[0]["registration_id"]), 140)
        Engine(self.config_for(), driver=ScriptDriver(finish_with_decision_acks)).run(self.project, "reviewer")
        self.assertEqual(self.rows(), ([], []))
        self.assertTrue(events.received(self.project.root, "reviewer", before[1][0]))

    def test_partial_wait_unlink_failure_preserves_unwritten_and_unselected(self):
        from pathlib import Path
        self.seed()
        before = self.rows()
        failed = self.project.root / "waits" / f"{before[0][0]['insight']}.json"
        real_unlink = Path.unlink
        def fail_one(path, *args, **kwargs):
            if path == failed:
                raise OSError("interrupted wait removal")
            return real_unlink(path, *args, **kwargs)
        def partial(ctx, prompt, profile=None):
            self.finish(ctx, ack_events=[row["ack_token"] for row in json.loads(prompt)["wait_events"][:2]])
        with patch.object(Path, "unlink", fail_one):
            Engine(self.config_for(), driver=ScriptDriver(partial)).run(self.project, "reviewer")
        self.assertEqual(self.rows(), ([before[0][0], before[0][2]], before[1]))
        restarted = Project(self.config, "sample")
        self.assertEqual(restarted.insights.consume_waits("reviewer", before[0][:2]), 1)
        self.assertEqual(self.rows(restarted), (before[0][2:], before[1]))

    def test_explicit_selection_is_independent_of_outcome_and_check_wait(self):
        roles = dict(self.config_for().roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], capabilities=(
            *roles["reviewer"].capabilities, "check_job"))
        config = dataclasses.replace(self.config, roles=roles)
        for outcome in ("continue", "blocked", "done", "bounded", "wait"):
            self.seed(count=1)
            for explicit in (False, True):
                with self.subTest(outcome=outcome, explicit=explicit):
                    def work(ctx, prompt, profile=None):
                        if outcome == "bounded":
                            ctx.bound_hit = "requests"
                        self.finish(ctx, outcome="wait" if outcome == "bounded" else outcome,
                                    **({"ack_events": delivered_ack_tokens(prompt)} if explicit else {}),
                                    **({"wait_for_check": "c" * 64} if outcome == "wait" else {}))
                    with patch("mizu.runtime.check_jobs.read", return_value={"role": "reviewer"}), \
                            patch("mizu.runtime.check_jobs.set_wait"):
                        Engine(config, driver=ScriptDriver(work)).run(self.project, "reviewer")
                    for group in self.rows():
                        self.assertEqual(len(group), 0 if explicit else 1)

    def test_tokens_bind_role_source_payload_and_current_unit_before_intent(self):
        self.seed(count=2)
        for index, attr, source in ((0, "wait_events", events.WAIT), (1, "obligations", events.OBLIGATION)):
            row, undelivered = self.rows()[index]
            ctx = self.context("reviewer")
            setattr(ctx, attr, [row])
            foreign = events.delivery("searcher", source, row)
            cross_source = events.delivery("reviewer", events.OBLIGATION if index == 0 else events.WAIT, row)
            for selection in ([row["ack_token"]] * 2, [foreign["ack_token"]], [undelivered["ack_token"]],
                              [cross_source["ack_token"]], ["0" * 64], [""], ["x" * 65],
                              [None], "not-a-list", ["x"] * 513):
                with self.subTest(attr=attr, selection=selection), self.assertRaises(Denied):
                    self.finish(ctx, ack_events=selection)
                self.assertIsNone(ctx.finished)
                self.assertFalse((ctx.run_dir / "finish-intent.json").exists())
            # Even injected foreign/cross-source or changed payload rows cannot pass recomputation.
            for altered in (foreign, cross_source, {**row, "reason": "changed"},
                            {**row, "registration_id": "0" * 32}):
                setattr(ctx, attr, [altered])
                with self.assertRaises(Denied):
                    self.finish(ctx, ack_events=[altered["ack_token"]])
                self.assertFalse((ctx.run_dir / "finish-intent.json").exists())
            setattr(ctx, attr, [row])
            self.finish(ctx, ack_events=[row["ack_token"]])
            self.assertEqual(len(self.rows()[index]), 2)  # intent is not acknowledgement

    def test_reregistered_same_timestamp_survives_old_selection(self):
        self.seed(count=1)
        before = self.rows()
        def reregister(ctx, prompt, profile=None):
            for row in (before[0][0], before[1][0]):
                old = read_json(self.project.root / "waits" / f"{row['insight']}.json")
                # A new decision on the same revision is legal; no timestamp precision assumption.
                spec = ({"kind": "deadline", "at": _past()} if "kind" in row else
                        {"kind": "dependency", "recipient": row["recipient"], "requires": row["requires"]})
                with patch("mizu.insights.now", return_value=old["registered_at"]):
                    self.project.insights.decide(row["insight"], "defer", "again", "again", "test",
                                                 wait=spec, expected_rev=row["rev"])
            self.finish(ctx, ack_events=delivered_ack_tokens(prompt))
        Engine(self.config_for(), driver=ScriptDriver(reregister)).run(self.project, "reviewer")
        after = self.rows()
        for old, new in zip(before, after):
            self.assertEqual(len(new), 1)
            self.assertEqual(old[0]["registered_at"], new[0]["registered_at"])
            self.assertNotEqual(old[0]["registration_id"], new[0]["registration_id"])
            ctx = self.context("reviewer")
            ctx.wait_events, ctx.obligations = after
            with self.assertRaises(Denied):
                self.finish(ctx, ack_events=[old[0]["ack_token"]])

    def test_partial_receipt_write_replays_unwritten_and_never_consumes_request(self):
        self.seed()
        before = self.rows()
        real_write = events.write_json
        writes = 0
        def fail_second(path, value, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("interrupted receipt write")
            return real_write(path, value, **kwargs)
        def obligations_only(ctx, prompt, profile=None):
            self.finish(ctx, ack_events=[r["ack_token"] for r in json.loads(prompt)["obligations"][:2]])
        with patch.object(events, "write_json", side_effect=fail_second):
            Engine(self.config_for(), driver=ScriptDriver(obligations_only)).run(self.project, "reviewer")
        self.assertEqual(self.rows(), (before[0], before[1][1:]))
        restarted = Project(self.config, "sample")
        events.acknowledge_obligations(restarted.root, "reviewer", before[1][:2])
        self.assertEqual(self.rows(restarted), (before[0], before[1][2:]))
        # Suppression belongs only to this role, even for an identical registration.
        foreign = events.delivery("worker", events.OBLIGATION, before[1][0])
        self.assertFalse(events.received(restarted.root, "worker", foreign))

    def test_failed_finish_result_and_publication_leave_all_pending(self):
        self.seed(role="worker", count=1)
        before = self.rows(role="worker")
        config = self.config_for("worker")
        def invalid_finish(ctx, prompt, profile=None):
            with self.assertRaises(Denied):
                self.finish(ctx, ack_events=[*delivered_ack_tokens(prompt), "0" * 64])
            raise RuntimeError("role stopped before finish")
        with self.assertRaises(RuntimeError):
            Engine(config, driver=ScriptDriver(invalid_finish)).run(self.project, "worker")
        self.assertEqual(self.rows(role="worker"), before)
        real_write = write_json
        def fail_intent(path, value, **kwargs):
            if path.name == "finish-intent.json":
                raise OSError("finish intent persistence failed")
            return real_write(path, value, **kwargs)
        def fail_result(path, value, **kwargs):
            if path.name == "result.json" and value.get("status") == "completed":
                raise OSError("result persistence failed")
            return real_write(path, value, **kwargs)
        for failure in (patch.object(self.project.snapshots, "publish", side_effect=OSError("publication failed")),
                        patch("mizu.runtime.write_json", side_effect=fail_intent),
                        patch("mizu.runtime.write_json", side_effect=fail_result)):
            self.project.set_control(armed=True, paused=False, wake_generation="retry-failure")
            with failure, self.assertRaises(OSError):
                Engine(config, driver=ScriptDriver(finish_with_decision_acks)).run(self.project, "worker")
            self.assertEqual(self.rows(role="worker"), before)

    def test_obligation_receipts_survive_backup_restore(self):
        from mizu.storage import backup, restore
        self.seed(count=1)
        row = self.rows()[1][0]
        events.acknowledge_obligations(self.project.root, "reviewer", [row])
        path = events._path(self.project.root, "reviewer", row)
        retained = path.read_bytes()
        self.project.set_control(paused=True)
        archive = self.root / "wait-receipts.tar.gz"
        backup(self.project, archive)
        restore(self.config, "restored", archive)
        restored = Project(self.config, "restored", read_only=True)
        self.assertEqual(restored.insights.pending_obligations("reviewer"), [])
        self.assertEqual(events._path(restored.root, "reviewer", row).read_bytes(), retained)
        self.assertEqual(len(restored.insights.due_waits("reviewer", restored.snapshots.get())), 1)

    def test_full_and_delta_prompts_keep_identical_tokens_and_ci_has_none(self):
        self.seed(count=1)
        ctx = self.context("reviewer")
        ctx.wait_events, ctx.obligations = self.rows()
        ci = {"kind": "ci", "publication_id": "a" * 64}
        ctx.wait_events.append(ci)
        full, delta = json.loads(prompt_for(ctx)), json.loads(prompt_delta_for(ctx, []))
        for key in ("wait_events", "obligations"):
            self.assertEqual(full[key], delta[key])
            self.assertEqual(len(full[key][0]["ack_token"]), 64)
        self.assertEqual(full["wait_events"][-1], ci)
        self.assertNotIn("ack_token", full["wait_events"][-1])

    def test_corrupt_unsafe_and_oversize_receipts_fail_closed_without_repair(self):
        self.seed(count=1)
        row = self.rows()[1][0]
        path = events._path(self.project.root, "reviewer", row)
        path.parent.mkdir(parents=True)
        for content in (b"{", b"[]", b"{}", b"x" * (events.RECEIPT_BYTES + 1)):
            path.write_bytes(content)
            with self.assertRaises((Denied, LimitExceeded)):
                self.project.insights.pending_obligations("reviewer")
            with self.assertRaises((Denied, LimitExceeded)):
                events.acknowledge_obligations(self.project.root, "reviewer", [row])
            ctx = self.context("reviewer")
            ctx.obligations = [row]
            with self.assertRaises((Denied, LimitExceeded)):
                self.finish(ctx, ack_events=[row["ack_token"]])
            self.assertFalse((ctx.run_dir / "finish-intent.json").exists())
            self.assertEqual(path.read_bytes(), content)
        path.unlink()
        events.acknowledge_obligations(self.project.root, "reviewer", [row])
        receipt = read_json(path)
        for field, value in (("role", "worker"), ("registration_id", "0" * 32),
                             ("ack_token", "0" * 64), ("acknowledged_at", "yesterday"),
                             ("acknowledged_at", "2026-01-01T00:00:00")):
            write_json(path, {**receipt, field: value})
            with self.assertRaises(Denied):
                events.received(self.project.root, "reviewer", row)
        path.unlink()
        target = self.root / "outside.json"
        target.write_text("untouched")
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            return
        with self.assertRaises(Denied):
            events.acknowledge_obligations(self.project.root, "reviewer", [row])
        path.unlink()
        os.link(target, path)
        with self.assertRaises(Denied):
            events.received(self.project.root, "reviewer", row)
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(self.root)
        with self.assertRaises(Denied):
            self.project.insights.pending_obligations("reviewer")
        self.assertEqual(target.read_text(), "untouched")

    def test_malformed_ids_are_not_legacy_and_utf8_byte_bounds_include_tokens(self):
        from mizu import smoke_retention
        self.seed(count=1)
        rows = self.rows()
        for row in (rows[0][0], rows[1][0]):
            path = self.project.root / "waits" / f"{row['insight']}.json"
            stored = read_json(path)
            for bad in (None, "", "../unsafe", "A" * 32, "x" * 65, "é" * 32, "legacy:x:y"):
                write_json(path, {**stored, "registration_id": bad})
                self.assertNotIn(row["insight"], [r["insight"] for group in self.rows() for r in group])
                with self.assertRaises(Denied):
                    smoke_retention._preflight(smoke_retention.Metadata(self.config.data),
                                                self.project.root, ("waits",))
            write_json(path, stored)
        from mizu.fs import canonical
        raw = {k: v for k, v in rows[0][0].items() if k != "ack_token"}
        with patch.object(events, "MAX_ROW_BYTES", len(canonical(raw))):
            with self.assertRaises(LimitExceeded):  # The token itself must fit, too.
                events.delivery("reviewer", events.WAIT, raw)
        with patch.object(events, "MAX_ROW_BYTES", 300):
            with self.assertRaises(LimitExceeded):
                events.delivery("reviewer", events.WAIT, {**rows[0][0], "body": "界" * 100})
        # Oversized registration files refuse bounded reads instead of hashing arbitrary legacy bytes.
        path = self.project.root / "waits" / f"{rows[0][0]['insight']}.json"
        stored = read_json(path)
        write_json(path, {**stored, "registered_at": "界" * events.MAX_ROW_BYTES})
        with self.assertRaises(LimitExceeded):
            self.project.insights.due_waits("reviewer", self.project.snapshots.get())
