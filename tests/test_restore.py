import io
import shutil
from contextlib import contextmanager
from pathlib import Path
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from support import Fixture, bind_delivery
from mizu.errors import Busy, Denied
from mizu.project import Project
from mizu.storage import backup, restore
from mizu import storage, smoke_retention as retention, snapshot as snapshot_module
from mizu.fs import held


class RestoreTests(Fixture):
    def test_roundtrip_preserves_uncommitted_code_and_remains_unarmed(self):
        self.project.set_control(paused=True)
        (self.project.workspace / 'app.py').write_text('VALUE = 7\n')
        archive = self.root / 'saved.tar.gz'
        backup(self.project, archive)
        result = restore(self.config, 'restored', archive)
        restored = Project(self.config, 'restored')
        self.assertFalse(result['armed'])
        self.assertTrue(restored.control()['paused'])
        self.assertEqual((restored.workspace / 'app.py').read_text(), 'VALUE = 7\n')
        self.assertEqual(restored.snapshots.get()['outcome'], 'wait')
        self.assertTrue((restored.root / 'spool/editor/.mizu-outbox').is_file())
        with self.assertRaises(Denied):
            restore(self.config, 'restored', archive)


    def test_restore_stage_is_invisible_to_active_retention(self):
        self.project.set_control(paused=True)
        archive = self.root / 'saved.tar.gz'
        backup(self.project, archive)
        data = self.config.data
        staging = data / 'staging/projects'
        original = storage.acceptance.audit
        (data / 'validation').mkdir()
        for fail in (False, True):
            with self.subTest(fail=fail):
                name = f'restored-{int(fail)}'
                active = set((data / 'projects').iterdir())
                smoke_id = str(int(fail) + 1) * 32
                retention.publish(data, smoke_id,
                                  {'status': 'pass', 'time': '2000-01-01T00:00:00+00:00'}, None)
                paused, release = threading.Event(), threading.Event()

                def audit(stage):
                    original(stage)
                    self.assertEqual(stage.parent, staging)
                    self.assertTrue(held(data / 'locks' / f'init-{name}.lock'))
                    paused.set()
                    self.assertTrue(release.wait(10), 'Stage was not released')
                    if fail:
                        raise Denied('Injected restore failure')

                with patch.object(storage.acceptance, 'audit', side_effect=audit), \
                     patch.object(storage, 'sync_dir', wraps=storage.sync_dir) as sync, \
                     ThreadPoolExecutor(max_workers=2) as pool:
                    future = pool.submit(restore, self.config, name, archive)
                    try:
                        self.assertTrue(paused.wait(5), 'Restore did not pause')
                        self.assertEqual(set((data / 'projects').iterdir()), active)
                        self.assertEqual(len(list(staging.glob('.restore-*'))), 1)
                        result = pool.submit(retention.run, data, apply=True).result(timeout=5)
                        self.assertTrue(result['complete'])
                        self.assertEqual(result['retired'], [smoke_id])
                        self.assertFalse(future.done())
                    finally:
                        release.set()
                    if fail:
                        with self.assertRaises(Denied):
                            future.result(timeout=5)
                        self.assertFalse((data / 'projects' / name).exists())
                        sync.assert_not_called()
                    else:
                        self.assertFalse(future.result(timeout=5)['armed'])
                        restored = Project(self.config, name)
                        self.assertTrue(restored.control()['paused'])
                        self.assertEqual((restored.workspace / 'app.py').read_bytes(), b'VALUE = 2\n')
                        self.assertEqual(restored.snapshots.get()['outcome'], 'wait')
                        self.assertEqual([call.args[0] for call in sync.call_args_list],
                                         [staging, data / 'projects'])
                self.assertEqual(list(staging.iterdir()), [])
                self.assertTrue(all(not p.name.startswith('.') for p in (data / 'projects').iterdir()))

    def test_restore_checks_filesystem_before_creation_and_publication(self):
        self.project.set_control(paused=True)
        archive = self.root / 'saved.tar.gz'
        backup(self.project, archive)
        staging = self.config.data / 'staging/projects'
        destination = self.config.data / 'projects/restored'
        original_stat, original_audit = Path.lstat, storage.acceptance.audit
        original_rename = storage.os.rename
        project_publications = []

        def observe_rename(source, target):
            if Path(target) == destination:
                project_publications.append((Path(source), Path(target)))
            return original_rename(source, target)
        for late in (False, True):
            mismatch = not late

            def device(path):
                info = original_stat(path)
                if path == staging and mismatch:
                    return Mock(st_mode=info.st_mode, st_dev=info.st_dev + 1, st_ino=info.st_ino, st_reparse_tag=0, st_file_attributes=0)
                return info

            def audit(stage):
                nonlocal mismatch
                original_audit(stage)
                mismatch = True

            with self.subTest(late=late), patch.object(Path, 'lstat', device), \
                 patch.object(storage.acceptance, 'audit', side_effect=audit), \
                 patch.object(storage.tempfile, 'mkdtemp', wraps=storage.tempfile.mkdtemp) as create, \
                 patch.object(storage.os, 'rename', side_effect=observe_rename):
                with self.assertRaisesRegex(Denied, 'share a filesystem|identity changed'):
                    restore(self.config, 'restored', archive)
                self.assertEqual(create.call_count, int(late))
                self.assertEqual(project_publications, [])
            self.assertEqual(len(list(staging.iterdir())), int(late))
            for leftover in staging.iterdir():
                shutil.rmtree(leftover)
            self.assertFalse((self.config.data / 'projects/restored').exists())


    def test_publication_rejects_replaced_directories_without_cleanup(self):
        self._check_publication_replacement('directory')

    def test_publication_rejects_replaced_symlinks_without_cleanup(self):
        probe = self.root / 'link-probe'
        try:
            probe.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Directory symlinks unavailable on this platform')
        probe.unlink()
        self._check_publication_replacement('symlink')

    def _check_publication_replacement(self, kind):
        self.project.set_control(paused=True)
        archive = self.root / 'saved.tar.gz'
        backup(self.project, archive)
        data = self.config.data
        staging = data / 'staging/projects'
        original = snapshot_module.Snapshots.publish
        # Pause after the final stage write, before the publication check.
        # Include both staging ancestors and the destination parent; a fresh
        # real directory must be rejected just as strictly as a link.
        for subject in ('stage', 'staging', 'staging-ancestor', 'destination'):
            for fail in (False, True):
                with self.subTest(subject=subject, kind=kind, fail=fail):
                    paused, release = threading.Event(), threading.Event()
                    stages = []

                    def publish(store, snapshot):
                        original(store, snapshot)
                        stages.append(store.root)
                        paused.set()
                        if not release.wait(10):
                            raise AssertionError('Publication was not released')
                        if fail:
                            raise Denied('Injected failure after stage replacement')

                    with patch.object(snapshot_module.Snapshots, 'publish', publish), \
                         ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(restore, self.config, 'replaced', archive)
                        target = saved = stage = None
                        try:
                            self.assertTrue(paused.wait(5), 'Publication did not pause')
                            stage = stages[0]
                            target = {'stage': stage, 'staging': staging,
                                      'staging-ancestor': staging.parent,
                                      'destination': data / 'projects'}[subject]
                            saved = self.root / 'original-directory'
                            target.replace(saved)
                            if kind == 'symlink':
                                target.symlink_to(saved, target_is_directory=True)
                            else:
                                # A complete, plausible replacement must not publish.
                                shutil.copytree(saved, target)
                            (target / 'replacement-marker').write_text('untouched')
                            with patch.object(storage.os, 'rename') as rename, \
                                 patch.object(storage.shutil, 'rmtree', wraps=shutil.rmtree) as remove:
                                release.set()
                                with self.assertRaises(Denied):
                                    future.result(timeout=5)
                                rename.assert_not_called()
                                remove.assert_not_called()
                            self.assertFalse((data / 'projects/replaced').exists())
                            self.assertEqual((target / 'replacement-marker').read_text(), 'untouched')
                            self.assertTrue(saved.is_dir())
                        finally:
                            release.set()
                            # Join before restoring fixture identities on assertion failure.
                            try:
                                future.result(timeout=5)
                            except Denied:
                                pass
                            if saved is not None and saved.exists():
                                if target.is_symlink():
                                    target.unlink()
                                else:
                                    shutil.rmtree(target)
                                saved.replace(target)
                            if stage is not None and stage.exists():
                                shutil.rmtree(stage)


    def test_unidentifiable_new_stage_is_not_published_or_removed(self):
        self.project.set_control(paused=True)
        archive = self.root / 'saved.tar.gz'
        backup(self.project, archive)
        staging = self.config.data / 'staging/projects'
        original = Path.lstat

        def unidentified(path):
            info = original(path)
            if path.parent == staging and path.name.startswith('.restore-'):
                return Mock(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=0,
                            st_reparse_tag=0, st_file_attributes=0)
            return info

        with patch.object(Path, 'lstat', unidentified), \
             patch.object(storage.os, 'rename') as rename, \
             patch.object(storage.shutil, 'rmtree', wraps=shutil.rmtree) as remove:
            with self.assertRaises(Denied):
                restore(self.config, 'unidentifiable', archive)
            rename.assert_not_called()
            remove.assert_not_called()
        # Identity could not be established: leave the uncertain stage alone.
        self.assertEqual(len(list(staging.iterdir())), 1)

    def test_roundtrip_preserves_work_item_completion_evidence(self):
        proposal = self.project.insights.submit(
            source="worker", title="Restore durable work", body="Keep transition evidence", base_snapshot=None)
        decision = self.project.insights.decide(
            proposal["id"], "accept", "reviewed", "", "run-restore", expected_rev=1)
        captured = self.project.snapshots.capture_files(self.project.workspace)
        verification = {"passed": True, "code_digest": captured["code_digest"], "run": "run-restore"}
        snapshot = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Verified work item", run="run-restore",
            outcome="done", summary="Completion evidence", verification=verification)
        self.project.snapshots.publish(snapshot)
        bind_delivery(self.project, decision["work_item"]["id"], snapshot["id"])
        completed = self.project.work_items.transition(
            decision["work_item"]["id"], expected_rev=3, status="done", actor="worker",
            snapshot_id=snapshot["id"])
        self.project.set_control(paused=True)
        archive = self.root / "work-items.tar.gz"
        backup(self.project, archive)
        restore(self.config, "restored", archive)
        restored = Project(self.config, "restored")
        self.assertEqual(restored.work_items.get(decision["work_item"]["id"]), completed)
        self.assertTrue((restored.root / "snapshots" / f"{snapshot['id']}.json").is_file())

    def test_restore_fails_closed_while_a_quarantined_pair_awaits_recovery(self):
        from mizu.smoke_retention import publish
        data = self.config.data
        (data / "validation").mkdir()
        smoke_id = "c" * 32
        pair = Path(publish(data, smoke_id, {"status": "pass", "time": "2020-01-01T00:00:00+00:00"}, None)["pair"])
        proposal = self.project.insights.submit(
            source="worker", title="Pinned evidence", body="Restore this reference", base_snapshot=None)
        self.project.insights.attach_evidence(
            proposal["id"], source="worker", text="Pin", expected_rev=1, smoke_refs=[smoke_id])
        self.project.set_control(paused=True)
        archive = self.root / "smoke-recovery.tar.gz"
        backup(self.project, archive)
        self.project.insights.revise(
            proposal["id"], source="worker", title="Unpinned", body="No current reference",
            base_snapshot=None, expected_rev=1)

        rename = retention.os.rename
        def interrupt(source, destination):
            if str(destination).endswith(".committed"):
                raise OSError("synthetic interruption before deletion commit")
            return rename(source, destination)
        with patch.object(retention.os, "rename", side_effect=interrupt):
            result = retention.run(data, apply=True)
        self.assertFalse(result["complete"])
        self.assertTrue((data / "validation/quarantine" / f"{smoke_id}.pending").is_dir())
        self.assertFalse(pair.exists())
        with self.assertRaises(Denied):
            restore(self.config, "restored-before-recovery", archive)
        self.assertFalse((data / "projects/restored-before-recovery").exists())
        self.assertEqual(list((data / "staging/projects").iterdir()), [])

        recovered = retention.run(data, apply=True)
        self.assertTrue(recovered["complete"])
        self.assertEqual(recovered["recovered"], [smoke_id])
        self.assertEqual(recovered["retired"], [])
        self.assertTrue(pair.is_dir())
        restore(self.config, "restored-after-recovery", archive)
        restored = Project(self.config, "restored-after-recovery")
        self.assertEqual(restored.insights.read(proposal["id"])["evidence"][0]["smoke_refs"], [smoke_id])
        self.assertIn(smoke_id, retention.run(data)["pinned"])

    def test_restore_root_publication_precedes_retention_scan_atomically(self):
        from mizu.smoke_retention import publish
        data = self.config.data
        (data / "validation").mkdir()
        smoke_id = "d" * 32
        pair = Path(publish(data, smoke_id, {"status": "pass", "time": "2020-01-01T00:00:00+00:00"}, None)["pair"])
        proposal = self.project.insights.submit(
            source="worker", title="Pinned evidence", body="Restore this reference", base_snapshot=None)
        self.project.insights.attach_evidence(
            proposal["id"], source="worker", text="Pin", expected_rev=1, smoke_refs=[smoke_id])
        self.project.set_control(paused=True)
        archive = self.root / "smoke-publication.tar.gz"
        backup(self.project, archive)
        self.project.insights.revise(
            proposal["id"], source="worker", title="Unpinned", body="No current reference",
            base_snapshot=None, expected_rev=1)

        entered, release = threading.Event(), threading.Event()
        attempted = threading.Event()
        original_validate, original_shared = retention.validate_restored_refs, retention.shared_lock
        def pause_validation(root_data, root):
            self.assertTrue(held(root_data / "locks/smoke-retention.lock"))
            entered.set()
            self.assertTrue(release.wait(10), "Restore validation was not released")
            return original_validate(root_data, root)
        @contextmanager
        def observe_retention_lock(root_data, *, blocking=False):
            attempted.set()
            with original_shared(root_data, blocking=blocking):
                yield

        with patch.object(retention, "validate_restored_refs", side_effect=pause_validation), \
             patch.object(retention, "shared_lock", observe_retention_lock), \
             ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(restore, self.config, "restored-first", archive)
            try:
                self.assertTrue(entered.wait(5), "Restore did not reach locked reference validation")
                self.assertFalse((data / "projects/restored-first").exists())
                retention_future = pool.submit(retention.run, data, apply=True)
                self.assertTrue(attempted.wait(5), "Retention did not attempt the shared lock")
                with self.assertRaises(Busy):
                    retention_future.result(timeout=5)
                self.assertTrue(pair.is_dir())
            finally:
                release.set()
            self.assertFalse(future.result(timeout=5)["armed"])

        restored = Project(self.config, "restored-first")
        self.assertEqual(restored.insights.read(proposal["id"])["evidence"][0]["smoke_refs"], [smoke_id])
        result = retention.run(data, apply=True)
        self.assertTrue(result["complete"])
        self.assertIn(smoke_id, result["pinned"])
        self.assertNotIn(smoke_id, result["retired"])
        self.assertTrue(pair.is_dir())

    def test_shared_smoke_refs_restore_only_with_complete_shared_pairs(self):
        from mizu.smoke_retention import publish
        import shutil
        (self.config.data / "validation").mkdir()
        smoke_id = "a" * 32
        result = publish(self.config.data, smoke_id, {"status": "pass", "time": "2020-01-01T00:00:00+00:00"}, None)
        proposal = self.project.insights.submit(source="worker", title="Shared evidence", body="Keep it", base_snapshot=None)
        self.project.insights.attach_evidence(proposal["id"], source="worker", text="Pin", expected_rev=1, smoke_refs=[smoke_id])
        self.project.set_control(paused=True)
        archive = self.root / "smoke-refs.tar.gz"
        backup(self.project, archive)
        restore(self.config, "restored", archive)
        restored = Project(self.config, "restored")
        self.assertEqual(restored.insights.read(proposal["id"])["evidence"][0]["smoke_refs"], [smoke_id])
        shutil.rmtree(result["pair"])
        with self.assertRaises(Denied):
            restore(self.config, "missing-pair", archive)
        self.assertFalse((self.config.data / "projects/missing-pair").exists())

    def malicious(self, name, kind=None):
        archive = self.root / 'malicious.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            info = tarfile.TarInfo(name)
            if kind:
                info.type = kind
                info.linkname = 'escape'
            else:
                info.size = 3
            tar.addfile(info, None if kind else io.BytesIO(b'bad'))
        return archive

    def test_refuses_traversal(self):
        with self.assertRaises(Denied):
            restore(self.config, 'restored', self.malicious('../escape'))
        self.assertFalse((self.config.data / 'projects/restored').exists())

    def test_refuses_symlink_and_hardlink(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            with self.assertRaises(Denied):
                restore(self.config, 'restored', self.malicious('objects/escape', kind))

    def test_refuses_large_archive(self):
        with self.assertRaises(Denied):
            restore(self.config, 'restored', self.malicious('backup.json'), max_bytes=2)

    def test_refuses_unexpected_top_level(self):
        with self.assertRaises(Denied):
            restore(self.config, 'restored', self.malicious('workspace/exploit'))
