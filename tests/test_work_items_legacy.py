"""Synthetic legacy history only: explicit reconciliation never migrates decisions."""
import concurrent.futures
import contextlib
import errno
import hashlib
import io
import json
import os
from unittest.mock import patch

from support import Fixture, forget_ledger
from mizu import work_item_migration as migration, work_items as work_items_module
from mizu.cli import main
from mizu.errors import Denied, LimitExceeded
from mizu.fs import PREVIEW_BYTES, canonical, held, lock, rename_exclusive, sync_dir
from mizu.storage import audit as storage_audit
from support import history_write_json as write_json


class LegacyWorkItemTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.serial = 0

    def hardlink(self, target, link):
        try:
            os.link(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("Hard links unavailable")

    def symlink(self, target, link, **kwargs):
        try:
            os.symlink(target, link, **kwargs)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")

    def legacy(self, **changes):
        self.serial += 1
        event_id = f"{self.serial:032x}"
        event = {"id": "synthetic-insight", "action": "accept", "rev": 1,
                 "reason": "Legacy assessment", "revisit": "", "run": "synthetic-run",
                 "created_at": "2024-01-01T00:00:00+00:00", **changes}
        path = self.project.root / "decision-history" / f"{event_id}.json"
        # Deliberately noncanonical original bytes prove no decision rewrite.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(event, indent=2) + "\n")
        return event_id, event, path

    def resolve(self, event_id, **changes):
        return self.store.reconcile(event_id, **{
            "expected_action": "accept", "outcome": "open", "reason": "Still actionable",
            "title": "Implement the retained slice", "actor": "worker", **changes})["record"]

    def sidecar(self, event_id):
        return self.project.root / "work-items" / f"wi_{event_id}" / "legacy.json"

    def current_sidecar(self, event_id):
        return self.sidecar(event_id).with_name("decision-resolution.json")

    def migration_cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["--config", str(self.file), "migrate-work-items", self.project.name, *args])
        return code, output.getvalue(), errors.getvalue()

    def migration_apply(self, *args):
        code, output, errors = self.migration_cli("--apply", *args)
        self.assertEqual(errors, "")
        report = json.loads(output)
        self.assertNotIn("wi_", output)
        self.assertNotIn("NEVER PRINT EVENT BODY", output)
        self.assertNotIn(str(self.project.root), output)
        self.assertFalse(report["dry_run"])
        self.assertEqual(code, 0 if report["ok"] else 1)
        return report

    def test_current_resolution_filename_preserves_open_cas_history(self):
        event_id, event, source = self.legacy()
        current = self.project.root / "decisions" / f"{event['id']}.json"
        write_json(current, event)
        source_bytes = source.read_bytes()
        item_id = f"wi_{event_id}"
        record = self.resolve(event_id)
        doing = self.store.transition(item_id, expected_rev=1, status="doing", actor="worker")
        old, new = self.sidecar(event_id), self.current_sidecar(event_id)
        raw = old.read_bytes()
        old.rename(new)
        forget_ledger(self.project.root)

        self.assertEqual(self.store.get(item_id), doing)
        self.assertEqual(self.store.explain(item_id)["history"], [record["work_item"], doing])
        self.assertEqual(self.store._current_decision(event["id"], include_work_item=True)["work_item_id"], item_id)
        self.assertEqual(self.resolve(event_id), record)
        with self.assertRaisesRegex(Denied, "re-read"):
            self.store.transition(item_id, expected_rev=1, status="open", actor="worker")
        next_state = self.store.transition(item_id, expected_rev=2, status="open", actor="worker")
        self.assertEqual(next_state["rev"], 3)
        self.assertEqual(self.store.page_legacy()["items"][0]["resolution"]["outcome"], "open")
        self.assertFalse(old.exists())
        self.assertEqual(new.read_bytes(), raw)
        self.assertEqual(source.read_bytes(), source_bytes)

    def test_current_resolution_filename_preserves_drop_storage_coverage(self):
        event_id, _, _ = self.legacy()
        self.resolve(event_id, outcome="drop", title=None)
        self.sidecar(event_id).rename(self.current_sidecar(event_id))
        forget_ledger(self.project.root)

        self.assertTrue(self.store.page_open()["coverage_complete"])
        self.assertEqual(self.store.page_legacy()["items"][0]["resolution"]["outcome"], "drop")
        self.assertEqual(storage_audit(self.project)["work_item_events_skipped"], 0)
        with self.assertRaises(Denied):
            self.store.get(f"wi_{event_id}")

    def test_coexisting_resolution_filenames_fail_closed(self):
        other, _, _ = self.legacy()
        self.resolve(other, outcome="drop", title=None)
        event_id, _, _ = self.legacy()
        self.resolve(event_id, outcome="drop", title=None)
        old, new = self.sidecar(event_id), self.current_sidecar(event_id)
        new.write_bytes(old.read_bytes())
        forget_ledger(self.project.root)

        for read in (self.store.page_legacy, self.store.page_open,
                     lambda: self.store.is_drop_only(f"wi_{event_id}"),
                     lambda: self.resolve(event_id, outcome="drop", title=None)):
            with self.assertRaisesRegex(Denied, "Ambiguous"):
                read()
        self.assertEqual(storage_audit(self.project)["work_item_events_skipped"], 1)
        self.project.set_control(paused=True)
        code, output, _ = self.migration_cli()
        report = json.loads(output)
        self.assertEqual((code, report["ambiguous_or_corrupt"], report["resolved_drop"]), (1, 1, 1))
        before = {p: p.read_bytes() for p in (old, new, self.sidecar(other))}
        applied = self.migration_apply()
        self.assertEqual((applied["ok"], applied["ambiguous_or_corrupt"], applied["converted"]), (False, 1, 0))
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertFalse(self.current_sidecar(other).exists())

    def test_migration_preview_refuses_unresolved_without_creating_lock_or_recovering(self):
        _, _, source = self.legacy()
        leftover = source.with_name(".new-abcdefgh")
        leftover.write_bytes(b"uncommitted")
        self.project.set_control(paused=True)
        lock_path = self.project.root / "locks" / "insights.lock"
        self.assertFalse(lock_path.exists())

        code, output, errors = self.migration_cli()
        report = json.loads(output)
        self.assertEqual((code, errors), (1, ""))
        self.assertTrue(report["dry_run"])
        self.assertTrue(report["scan_complete"])
        self.assertEqual(report["unresolved"], 1)
        self.assertIn("mizu_work_items_reconcile", report["action"])
        self.assertFalse(lock_path.exists())
        self.assertEqual(leftover.read_bytes(), b"uncommitted")
        applied = self.migration_apply()
        self.assertEqual((applied["ok"], applied["unresolved"], applied["converted"]), (False, 1, 0))
        self.assertFalse(lock_path.exists())
        self.assertEqual(leftover.read_bytes(), b"uncommitted")

    def test_migration_preview_counts_dispositions_and_respects_scan_bound(self):
        opened, _, _ = self.legacy()
        self.resolve(opened)
        dropped, _, _ = self.legacy(action="modify")
        self.resolve(dropped, expected_action="modify", outcome="drop", title=None)
        self.sidecar(dropped).rename(self.current_sidecar(dropped))
        proposal = self.project.insights.submit(source="worker", title="No work", body="Synthetic", base_snapshot=None)
        self.project.insights.decide(proposal["id"], "accept", "No action", "", "run", expected_rev=1,
                                     work_item_intent="none")
        self.project.set_control(paused=True)
        with lock(self.project.root / "locks" / "insights.lock"):
            code, _, errors = self.migration_cli()
            self.assertEqual(code, 75)
            self.assertIn("Already running", errors)

        with patch.object(work_items_module.WorkItems, "_read_decision_event") as read:
            code, output, errors = self.migration_cli("--scan-limit", "2")
            read.assert_not_called()
        self.assertEqual((code, errors), (1, ""))
        self.assertLess(len(output.encode()), 1024)
        report = json.loads(output)
        self.assertTrue(report["truncated"])
        self.assertFalse(report["scan_complete"])

        def inventory():
            return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in self.project.root.rglob("*")}

        before = inventory()
        code, output, errors = self.migration_cli("--scan-limit", "3")
        self.assertEqual((code, errors), (0, ""))
        report = json.loads(output)
        self.assertEqual([report[name] for name in ("resolved_open", "resolved_drop", "explicit_no_work",
                                                    "unresolved", "ambiguous_or_corrupt")], [1, 1, 1, 0, 0])
        self.assertFalse(report["truncated"])
        self.assertEqual(inventory(), before)

        # Exhaust the work-items bound after already validating the legacy
        # candidates. A partial plan must still authorize no renames.
        for i in range(2):
            (self.sidecar(opened).parent.parent / f"wi_{10 + i:032x}").mkdir()
        before = inventory()
        applied = self.migration_apply("--scan-limit", "3")
        self.assertEqual((applied["truncated"], applied["scan_complete"], applied["candidates"],
                          applied["renamed"], applied["converted"]), (True, False, 1, 0, 0))
        self.assertEqual(inventory(), before)

    def test_apply_preserves_bytes_history_and_drop_coverage_and_retries(self):
        opened, event, source = self.legacy()
        dropped, _, drop_source = self.legacy(action="modify", id="synthetic-drop", body="NEVER PRINT EVENT BODY")
        current = self.project.root / "decisions" / f"{event['id']}.json"
        write_json(current, event)
        item_id = f"wi_{opened}"
        seed = self.resolve(opened)["work_item"]
        doing = self.store.transition(item_id, expected_rev=1, status="doing", actor="worker")
        self.resolve(dropped, expected_action="modify", outcome="drop", title=None)
        already, _, _ = self.legacy(id="synthetic-already")
        self.resolve(already, outcome="drop", title=None)
        rename_exclusive(self.sidecar(already), self.current_sidecar(already))
        proposal = self.project.insights.submit(source="worker", title="No work", body="Synthetic", base_snapshot=None)
        self.project.insights.decide(proposal["id"], "accept", "No action", "", "run", expected_rev=1,
                                     work_item_intent="none")
        self.project.set_control(paused=True)

        # Make the existing JSON deliberately noncanonical so a reserialization
        # or even a byte-exact copy cannot pass the bytes/inode checks.
        for event_id in (opened, dropped):
            path = self.sidecar(event_id)
            path.write_bytes((json.dumps(json.loads(path.read_bytes()), indent=2) + "\n").encode())
        immutable = [source, drop_source, current, self.current_sidecar(already),
                     *(p for p in self.sidecar(opened).parent.glob("*.json") if p != self.sidecar(opened))]

        def identity(path):
            info = path.stat()
            return (info.st_dev, info.st_ino, info.st_mtime_ns)

        before = {p: (p.read_bytes(), identity(p)) for p in immutable}
        record_lock = self.project.root / "locks" / "insights.lock"
        lock_identity = identity(record_lock)
        moved = {event_id: (self.sidecar(event_id).read_bytes(), identity(self.sidecar(event_id)))
                 for event_id in (opened, dropped)}

        def under_exclusion(old, new):
            for name in ("workspace.lock", "publication.lock", "insights.lock"):
                self.assertTrue(held(self.project.root / "locks" / name))
            rename_exclusive(old, new)

        with patch.object(migration, "rename_exclusive", side_effect=under_exclusion):
            applied = self.migration_apply()
        self.assertEqual([applied[key] for key in ("ok", "candidates", "renamed", "converted", "already_current",
                                                  "resolved_open", "resolved_drop", "explicit_no_work")],
                         [True, 2, 2, 2, 1, 1, 2, 1])
        for event_id, expected in moved.items():
            self.assertFalse(self.sidecar(event_id).exists())
            new = self.current_sidecar(event_id)
            self.assertEqual((new.read_bytes(), identity(new)), expected)
        self.assertEqual({p: (p.read_bytes(), identity(p)) for p in before}, before)
        self.assertEqual(identity(record_lock), lock_identity)

        forget_ledger(self.project.root)
        self.assertEqual(self.store.explain(item_id)["history"], [seed, doing])
        self.assertTrue(self.store.page_open()["coverage_complete"])
        self.assertTrue(self.store.is_drop_only(f"wi_{dropped}"))
        with patch.object(migration, "rename_exclusive") as rename:
            retry = self.migration_apply()
        rename.assert_not_called()
        self.assertEqual((retry["ok"], retry["converted"], retry["already_current"]), (True, 0, 3))
        with self.assertRaisesRegex(Denied, "re-read"):
            self.store.transition(item_id, expected_rev=1, status="open", actor="worker")
        self.assertEqual(self.store.transition(item_id, expected_rev=2, status="open", actor="worker")["rev"], 3)

    def test_apply_replans_under_quiescence(self):
        opened, _, _ = self.legacy()
        self.resolve(opened)
        self.project.set_control(paused=True)
        quiescent = migration.quiescent

        @contextlib.contextmanager
        def change_before_lock(project, **kwargs):
            self.legacy(id="synthetic-unresolved")
            with quiescent(project, **kwargs):
                yield

        with (patch.object(migration, "quiescent", side_effect=change_before_lock),
              patch.object(migration, "rename_exclusive") as rename):
            report = self.migration_apply()
        rename.assert_not_called()
        self.assertEqual((report["ok"], report["scan_complete"], report["unresolved"], report["converted"]),
                         (False, True, 1, 0))
        self.assertTrue(self.sidecar(opened).is_file())
        self.assertFalse(self.current_sidecar(opened).exists())

    def test_apply_stops_on_directory_sync_failure_and_retry_converges(self):
        opened = [self.legacy(id=f"synthetic-{i}")[0] for i in range(3)]
        records = {f"wi_{event_id}": self.resolve(event_id)["work_item"] for event_id in opened}
        self.project.set_control(paused=True)
        attempts = []

        def fail_second_sync(directory):
            attempts.append(directory)
            if len(attempts) == 2:
                raise OSError(errno.EIO, "simulated sync failure", directory)
            sync_dir(directory)

        with patch.object(migration, "sync_dir", side_effect=fail_second_sync):
            report = self.migration_apply()
        self.assertEqual((report["ok"], report["candidates"], report["renamed"], report["converted"]),
                         (False, 3, 2, 1))
        self.assertEqual(report["errors"], [{"stage": "sync_directory", "type": "OSError", "errno": errno.EIO}])
        for event_id in opened:
            old, new = self.sidecar(event_id), self.current_sidecar(event_id)
            self.assertEqual((old.exists(), new.exists()), (old.parent not in attempts, old.parent in attempts))
        forget_ledger(self.project.root)
        self.assertEqual({item_id: self.store.get(item_id) for item_id in records}, records)
        self.assertEqual(self.migration_cli()[0], 0)
        retry = self.migration_apply()
        self.assertEqual((retry["ok"], retry["converted"], retry["already_current"]), (True, 1, 2))

    def test_open_drop_coverage_audit_and_history_immutability(self):
        opened, event, path = self.legacy(reason="r" * 1400, body="NEVER COPY BODY")
        dropped, _, drop_path = self.legacy(action="modify")
        original = {p: p.read_bytes() for p in (path, drop_path)}
        self.assertEqual(self.store.page_open()["unseeded_decisions"], 2)
        record = self.resolve(opened, expected_rev=1)
        self.assertEqual(record["event_sha256"], hashlib.sha256(canonical(event)).hexdigest())
        self.assertEqual(record["work_item"]["created_at"], event["created_at"])
        self.assertEqual(self.store.get(f"wi_{opened}"), record["work_item"])
        self.assertEqual(self.store.page_open()["unseeded_decisions"], 1)
        drop = self.resolve(dropped, expected_action="modify", outcome="drop", title=None,
                            reason="Superseded by a later assessed design")
        self.assertNotIn("work_item", drop)
        page = self.store.page_open()
        self.assertTrue(page["coverage_complete"])
        self.assertEqual(page["unseeded_decisions"], 0)
        self.assertEqual([row["id"] for row in page["items"]], [f"wi_{opened}"])
        audit = self.store.page_legacy()
        self.assertEqual(audit["total"], 2)
        self.assertTrue(audit["coverage_complete"])
        self.assertEqual([row["resolution"]["outcome"] for row in audit["items"]], ["open", "drop"])
        self.assertTrue(audit["items"][0]["reason_truncated"])
        self.assertNotIn("NEVER COPY BODY", json.dumps(audit))
        self.assertEqual(audit["items"][1]["resolution"]["reason_preview"], drop["reason"])
        for p, content in original.items():
            self.assertEqual(p.read_bytes(), content)
        with self.assertRaises(Denied):
            self.store.get(f"wi_{dropped}")

    def test_legacy_resolution_title_reason_edges_and_expected_revision(self):
        for title, reason in (("x", "r"), ("t" * 200, "r" * 4000)):
            with self.subTest(title_length=len(title), reason_length=len(reason)):
                event_id, _, _ = self.legacy()
                record = self.store.reconcile(
                    event_id, expected_action="accept", expected_rev=1, outcome="open",
                    reason=reason, title=title, actor="worker")["record"]
                self.assertEqual(record["reason"], reason)
                self.assertEqual(record["work_item"]["title"], title)

        for changes in ({"title": ""}, {"title": "t" * 201}, {"reason": ""},
                        {"reason": " "}, {"reason": "r" * 4001}, {"expected_rev": 0}):
            with self.subTest(changes=changes):
                event_id, _, _ = self.legacy()
                with self.assertRaises(Denied):
                    self.resolve(event_id, **changes)
                self.assertFalse(self.sidecar(event_id).exists())

    def test_by_id_history_reads_open_reconciliation_sidecar_seed(self):
        event_id, _, _ = self.legacy()
        resolution = self.resolve(event_id)

        view = self.store.explain(f"wi_{event_id}")

        self.assertEqual(view["item"], resolution["work_item"])
        self.assertEqual(view["history"], [resolution["work_item"]])
        self.assertEqual(view["history_total"], 1)
        self.assertTrue(view["stale"])

    def test_current_decision_bytes_unchanged_and_seeded_events_refused(self):
        proposal = self.project.insights.submit(source="worker", title="Seeded", body="Synthetic", base_snapshot=None)
        decision = self.project.insights.decide(proposal["id"], "accept", "Assessed", "", "run", expected_rev=1)
        current_path = self.project.root / "decisions" / f"{proposal['id']}.json"
        event_id = decision["work_item"]["id"][3:]
        with self.assertRaises(Denied):
            self.resolve(event_id, outcome="drop", title=None)
        legacy_id, event, _ = self.legacy(id=proposal["id"])
        write_json(current_path, event)
        before = current_path.read_bytes()
        self.resolve(legacy_id)
        self.assertEqual(current_path.read_bytes(), before)
        # Even a syntactically valid sidecar cannot resolve a seeded source.
        record = self.resolve(legacy_id)
        record.update(event_id=event_id, event_sha256=hashlib.sha256(canonical(decision)).hexdigest())
        write_json(self.sidecar(event_id), record)
        for read in (self.store.page_open, self.store.page_legacy):
            with self.assertRaises(Denied):
                read()

    def test_closed_legacy_item_remains_covered_and_auditable(self):
        event_id, _, _ = self.legacy()
        record = self.resolve(event_id, title="t" * 200, reason="理由" * 2000)
        self.store.transition(f"wi_{event_id}", expected_rev=1, status="dropped", actor="worker", reason="No longer needed")
        page = self.store.page_open()
        self.assertEqual(page["items"], [])
        self.assertEqual(page["unseeded_decisions"], 0)
        self.assertTrue(page["coverage_complete"])
        audit = self.store.page_legacy()["items"][0]
        self.assertEqual(audit["resolution"]["outcome"], "open")
        self.assertTrue(audit["resolution"]["reason_truncated"])
        self.assertLessEqual(len(audit["resolution"]["reason_preview"].encode()), 1024)
        self.assertFalse(audit["openable"])
        self.assertEqual(self.resolve(event_id, title="t" * 200, reason="理由" * 2000), record)

    def test_incomplete_origin_can_drop_but_never_open(self):
        for changes in ({"rev": None}, {"rev": True}, {"rev": 0}, {"rev": "1"},
                        {"created_at": None}, {"created_at": "invalid"},
                        {"created_at": "2024-01-01"}, {"id": "../unsafe"}):
            with self.subTest(changes=changes):
                event_id, _, path = self.legacy(**changes)
                event = json.loads(path.read_bytes())
                event = {k: v for k, v in event.items() if v is not None}
                write_json(path, event)
                self.assertFalse(next(row for row in self.store.page_legacy()["items"]
                                      if row["event_id"] == event_id)["openable"])
                with self.assertRaises(Denied):
                    self.resolve(event_id)
                self.assertFalse(self.sidecar(event_id).exists())
                self.resolve(event_id, outcome="drop", title=None, reason="No valid legacy origin")
        self.assertTrue(self.store.page_open()["coverage_complete"])

    def test_exact_retry_is_immutable_even_after_transition_conflicts_refused(self):
        event_id, _, _ = self.legacy()
        first = self.resolve(event_id)
        before = self.sidecar(event_id).read_bytes()
        self.assertEqual(self.resolve(event_id, expected_rev=1), first)
        item_id = f"wi_{event_id}"
        self.store.transition(item_id, expected_rev=1, status="doing", actor="worker")
        self.assertEqual(self.resolve(event_id), first)
        for changes in ({"actor": "other"}, {"title": "Other title"}, {"reason": "Other reason"},
                        {"outcome": "drop", "title": None}, {"expected_action": "modify"},
                        {"expected_rev": 2}):
            with self.subTest(changes=changes), self.assertRaises(Denied):
                self.resolve(event_id, **changes)
        self.assertEqual(self.sidecar(event_id).read_bytes(), before)
        with self.assertRaisesRegex(Denied, "re-read"):
            self.store.transition(item_id, expected_rev=1, status="blocked", actor="worker")
        self.assertEqual(self.store.get(item_id)["rev"], 2)

    def test_drop_retry_and_concurrent_exclusive_resolution(self):
        event_id, _, _ = self.legacy()
        def resolve():
            return self.resolve(event_id, outcome="drop", title=None, reason="Duplicate")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: resolve(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(resolve(), results[0])
        with self.assertRaises(Denied):
            self.resolve(event_id)
        self.assertEqual([p.name for p in self.sidecar(event_id).parent.iterdir()], ["legacy.json"])

    def test_expectations_validation_and_seeded_nonaccept_refusal(self):
        event_id, _, _ = self.legacy()
        for changes in ({"expected_action": "modify"}, {"expected_rev": 2}, {"expected_rev": True},
                        {"reason": " "}, {"reason": "r" * 4001}, {"actor": "../role"},
                        {"title": None}, {"title": ""}, {"title": "t" * 201},
                        {"outcome": "drop"}, {"outcome": "bad"}):
            with self.subTest(changes=changes), self.assertRaises(Denied):
                self.resolve(event_id, **changes)
        self.assertFalse(self.sidecar(event_id).exists())
        for event_id in ("../event", "wi_" + "a" * 32, "a" * 33):
            with self.assertRaises(Denied):
                self.resolve(event_id)
        for changes in ({"action": "reject"}, {"work_item": None}):
            event_id, _, _ = self.legacy(**changes)
            with self.assertRaises(Denied):
                self.resolve(event_id, outcome="drop", title=None)

    def test_hash_binding_and_corruption_fail_closed(self):
        event_id, event, path = self.legacy()
        record = self.resolve(event_id)
        sidecar = self.sidecar(event_id)
        for changes in ({"event_id": "f" * 32}, {"event_sha256": "f" * 64},
                        {"revision": True}, {"revision": 2}, {"action": "modify"},
                        {"actor": "../actor"}, {"reason": " "}, {"outcome": "bad"},
                        {"created_at": "bad"}, {"extra": 1}, {"work_item": []},
                        {"work_item": {**record["work_item"], "rev": True}}):
            with self.subTest(changes=changes):
                write_json(sidecar, {**record, **changes})
                for read in (self.store.page_open, self.store.page_legacy,
                             lambda: self.store.get(f"wi_{event_id}"),
                             lambda: self.resolve(event_id)):
                    with self.assertRaises(Denied):
                        read()
        write_json(sidecar, record)
        write_json(path, {**event, "reason": "Changed original assessment"})
        for read in (self.store.page_open, self.store.page_legacy,
                     lambda: self.store.get(f"wi_{event_id}"), lambda: self.resolve(event_id)):
            with self.assertRaises(Denied):
                read()

    def test_malformed_committed_json_and_byte_bounds(self):
        event_id, _, _ = self.legacy()
        self.resolve(event_id)
        sidecar = self.sidecar(event_id)
        for content in (b"{", b"[]", b'{"value":NaN}'):
            sidecar.write_bytes(content)
            forget_ledger(self.project.root)
            for read in (self.store.page_open, self.store.page_legacy):
                forget_ledger(self.project.root)
                with self.assertRaises(Denied):
                    read()
        sidecar.write_bytes(b" " * (PREVIEW_BYTES + 1))
        forget_ledger(self.project.root)
        with self.assertRaises(LimitExceeded):
            self.store.page_legacy()

    def test_post_link_sidecar_and_orphan_temporary_recovery(self):
        event_id, _, _ = self.legacy()
        record = self.resolve(event_id)
        sidecar = self.sidecar(event_id)
        alias = sidecar.parent / ".new-abcdefgh"
        self.hardlink(sidecar, alias)
        self.assertEqual(self.store.page_legacy()["items"][0]["resolution"]["outcome"], "open")
        self.assertFalse(alias.exists())
        alias.write_bytes(b"not committed")
        self.assertEqual(self.resolve(event_id), record)
        self.assertFalse(alias.exists())
        sidecar.unlink()
        alias.write_bytes(canonical(record))
        forget_ledger(self.project.root)
        self.assertEqual(self.store.page_open()["unseeded_decisions"], 1)
        self.assertFalse(sidecar.exists())
        self.assertFalse(alias.exists())

    def test_temporary_recovery_is_bounded_and_unsafe_links_fail_closed(self):
        event_id, _, _ = self.legacy()
        self.resolve(event_id)
        sidecar = self.sidecar(event_id)
        outside = self.project.root / "unrecognized-link"
        self.hardlink(sidecar, outside)
        forget_ledger(self.project.root)
        with self.assertRaises(Denied):
            self.store.page_legacy()
        self.project.set_control(paused=True)
        applied = self.migration_apply()
        self.assertEqual((applied["ok"], applied["ambiguous_or_corrupt"], applied["renamed"]), (False, 1, 0))
        self.assertTrue(os.path.samefile(sidecar, outside))
        self.assertFalse(self.current_sidecar(event_id).exists())
        outside.unlink()
        outside.write_text("uncommitted")
        self.hardlink(outside, sidecar.parent / ".new-abcdefgh")
        forget_ledger(self.project.root)
        with self.assertRaises(Denied):
            self.resolve(event_id)
        (sidecar.parent / ".new-abcdefgh").unlink()
        unsafe = sidecar.parent / ".new-abcdefgh"
        self.symlink(outside, unsafe)
        forget_ledger(self.project.root)
        with self.assertRaises(Denied):
            self.store.page_legacy()
        unsafe.unlink()
        for i in range(258):
            (sidecar.parent / f".new-{i:08d}").write_text("orphan")
        forget_ledger(self.project.root)
        with self.assertRaisesRegex(Denied, "bound"):
            self.store.page_open()

    def test_symlink_sidecar_and_parent_refused_without_writes(self):
        event_id, _, _ = self.legacy()
        self.resolve(event_id)
        sidecar = self.sidecar(event_id)
        content = sidecar.read_bytes()
        sidecar.unlink()
        target = self.project.root / "sidecar-target"
        target.write_bytes(content)
        self.symlink(target, sidecar)
        forget_ledger(self.project.root)
        with self.assertRaises(Denied):
            self.store.page_open()
        current = self.current_sidecar(event_id)
        sidecar.rename(current)
        self.project.set_control(paused=True)
        applied = self.migration_apply()
        self.assertEqual((applied["ok"], applied["ambiguous_or_corrupt"], applied["already_current"]), (False, 1, 0))
        self.assertTrue(current.is_symlink())
        self.assertEqual(target.read_bytes(), content)
        current.unlink()
        sidecar.parent.rmdir()
        target_dir = self.project.root / "other-directory"
        target_dir.mkdir()
        self.symlink(target_dir, sidecar.parent, target_is_directory=True)
        forget_ledger(self.project.root)
        with self.assertRaises(Denied):
            self.resolve(event_id)
        self.assertEqual(list(target_dir.iterdir()), [])

    def test_drop_cannot_carry_seed_or_transition_history(self):
        event_id, _, _ = self.legacy()
        record = self.resolve(event_id, outcome="drop", title=None)
        write_json(self.sidecar(event_id), {**record, "work_item": {}})
        with self.assertRaises(Denied):
            self.store.page_open()
        write_json(self.sidecar(event_id), record)
        write_json(self.sidecar(event_id).parent / "2.json", {})
        for read in (self.store.page_open, self.store.page_legacy):
            with self.assertRaises(Denied):
                read()

    def test_cursor_live_pages_and_selection_bounds(self):
        ids = [self.legacy()[0] for _ in range(13)]
        cursor, seen = None, []
        while True:
            page = self.store.page_legacy(cursor=cursor, limit=3)
            self.assertEqual(page["total"], 13)
            self.assertLessEqual(len(page["items"]), 3)
            self.assertEqual(page["cursor"], cursor)
            seen.extend(row["event_id"] for row in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
            self.assertLessEqual(len(cursor), 128)
            # Resolution does not offset-skip or remove audit membership.
            self.resolve(page["items"][0]["event_id"], outcome="drop", title=None)
        self.assertEqual(seen, sorted(ids))
        self.assertEqual(self.store.page_legacy(cursor="wl1|" + "f" * 32)["items"], [])
        import heapq
        push = heapq.heappush
        sizes = []
        def bounded_push(heap, row):
            sizes.append(len(heap) + 1)
            return push(heap, row)
        with patch("mizu.work_items.heapq.heappush", side_effect=bounded_push):
            self.store.page_legacy(limit=2)
        self.assertEqual(max(sizes), 2)
        for limit in (True, 0, 101, 1.5, "2", None):
            with self.assertRaises(Denied):
                self.store.page_legacy(limit=limit)
        for cursor in ("", 1, "wl1|" + "a" * 33, "wl1|" + "G" * 32,
                       "wi1|open|0|wi_" + "a" * 32, "x" * 129):
            with self.assertRaises(Denied):
                self.store.page_legacy(cursor=cursor)
        with patch("mizu.work_items.MAX_OPEN_ROW_BYTES", 10):
            with self.assertRaises(LimitExceeded):
                self.store.page_legacy()
        with patch("mizu.work_items.MAX_OPEN_PAGE_BYTES", 10):
            with self.assertRaises(LimitExceeded):
                self.store.page_legacy()

    def test_unsafe_committed_history_refused_even_beyond_cursor(self):
        event_id, _, path = self.legacy()
        for content in (b"[]", b"{", b'{"action":"unknown"}'):
            path.write_bytes(content)
            with self.assertRaises(Denied):
                self.store.page_legacy(cursor="wl1|" + "f" * 32, limit=1)
        path.unlink()
        target = self.project.root / "event-target"
        target.write_text('{"action":"accept"}')
        self.symlink(target, path)
        with self.assertRaises(Denied):
            self.store.page_legacy()

    def test_legacy_current_redecision_and_seeded_validation_remain_strict(self):
        proposal = self.project.insights.submit(source="worker", title="Origin", body="Synthetic", base_snapshot=None)
        event_id, event, _ = self.legacy(id=proposal["id"])
        self.resolve(event_id)
        revised = self.project.insights.revise(proposal["id"], source="worker", title="Revision",
                                               body="New synthetic revision", expected_rev=1, base_snapshot=None)
        current = {**event, "rev": revised["rev"], "action": "modify"}
        current_path = self.project.root / "decisions" / f"{proposal['id']}.json"
        write_json(current_path, current)
        before = current_path.read_bytes()
        with self.assertRaises(Denied):
            self.store.projection(f"wi_{event_id}")  # Well-shaped mutable pointers need history.
        current_event_id = "b" * 32
        write_json(self.project.root / "decision-history" / f"{current_event_id}.json", current)
        view = self.store.projection(f"wi_{event_id}")
        self.assertFalse(view["stale"])
        self.assertEqual(view["redecision"], {"rev": 2, "action": "modify"})
        self.assertEqual(current_path.read_bytes(), before)
        write_json(current_path, {**current, "work_item": None})
        with self.assertRaises(Denied):
            self.store.projection(f"wi_{event_id}")
