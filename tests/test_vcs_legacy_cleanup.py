"""Disposable ref-cleanup boundaries; Win32 mocks are ABI tests, not OS proof."""
import ctypes
import errno
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mizu import _legacy_refs as cleanup, vcs


class _LegacyFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "project"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        self.legacy = self.workspace / "refs/remotes/upstream"

    def inject(self):
        receipt = vcs.inject_refs(self.root, {"main": "a" * 40})
        self.assertEqual(receipt["injected"], 1)
        self.assertEqual(vcs.read_ref(self.root, "main"), "a" * 40)

    def symlink(self, path, target, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")

    def ancestor_cases(self, make_link):
        parts = ("workspace", *vcs.REF_PREFIX)
        for depth, name in enumerate(parts):
            with self.subTest(ancestor=name):
                root = self.tmp / f"case-{depth}"
                parent = root.joinpath(*parts[:depth])
                parent.mkdir(parents=True)
                external = self.tmp / f"external-{depth}"
                sentinel = external.joinpath(*parts[depth + 1:]) / "sentinel"
                sentinel.parent.mkdir(parents=True)
                sentinel.write_bytes(b"external sentinel\n")
                sentinel.chmod(0o444)
                link = parent / name
                make_link(link, external)
                self.assertEqual(vcs.inject_refs(root, {"main": "a" * 40})["injected"], 1)
                self.assertEqual(sentinel.read_bytes(), b"external sentinel\n")
                self.assertTrue(link.exists())
                self.assertEqual(vcs.read_ref(root, "main"), "a" * 40)
                # Restore readonly fixture files for portable temp cleanup.
                sentinel.chmod(0o600)


class LegacyRefCleanupTests(_LegacyFixture, unittest.TestCase):
    def test_every_symlink_ancestor_preserves_external_sentinel(self):
        self.ancestor_cases(lambda p, t: self.symlink(p, t, directory=True))

    @unittest.skipUnless(os.name == "nt", "Native Windows junction regression")
    def test_every_junction_ancestor_preserves_external_sentinel(self):
        def junction(path, target):
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(target)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.ancestor_cases(junction)

    def test_nested_unsafe_entries_left_ordinary_readonly_refs_removed(self):
        self.legacy.mkdir(parents=True)
        outside = self.tmp / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"keep\n")
        sentinel.chmod(0o444)
        self.addCleanup(sentinel.chmod, 0o600)
        mode = sentinel.stat().st_mode
        self.symlink(self.legacy / "directory-link", outside, directory=True)
        self.symlink(self.legacy / "file-link", sentinel)
        self.symlink(self.legacy / "broken", outside / "missing")
        os.link(sentinel, self.legacy / "hardlink")
        regular = self.legacy / "feature/main"
        regular.parent.mkdir()
        regular.write_bytes(b"legacy ref\n")
        regular.chmod(0o444)
        if os.name == "posix":
            os.mkfifo(self.legacy / "fifo")
        self.inject()
        self.assertFalse(regular.exists())
        self.assertFalse(regular.parent.exists())
        for name in ("directory-link", "file-link", "broken"):
            self.assertTrue((self.legacy / name).is_symlink())
        self.assertTrue((self.legacy / "hardlink").exists())
        self.assertEqual(sentinel.stat().st_mode, mode)
        self.assertEqual(sentinel.read_bytes(), b"keep\n")
        if os.name == "posix":
            self.assertTrue(stat.S_ISFIFO((self.legacy / "fifo").lstat().st_mode))

    def test_parent_pruning_preserves_unrelated_siblings_and_workspace(self):
        for depth in range(3):
            with self.subTest(depth=depth):
                self.legacy.mkdir(parents=True, exist_ok=True)
                (self.legacy / "main").write_bytes(b"legacy\n")
                parent = self.workspace.joinpath(*vcs.REF_PREFIX[:depth])
                sibling = parent / "unrelated"
                sibling.write_bytes(b"keep\n")
                self.inject()
                self.assertFalse(self.legacy.exists())
                self.assertEqual(sibling.read_bytes(), b"keep\n")
                self.assertTrue(self.workspace.is_dir())
                sibling.unlink()
        self.inject()  # Empty parents may be pruned on the next refresh.
        self.assertFalse((self.workspace / "refs").exists())
        self.assertTrue(self.workspace.is_dir())

    def test_non_directory_ancestors_are_untouched(self):
        parts = ("workspace", *vcs.REF_PREFIX)
        for depth, name in enumerate(parts):
            with self.subTest(ancestor=name):
                root = self.tmp / f"file-case-{depth}"
                parent = root.joinpath(*parts[:depth])
                parent.mkdir(parents=True)
                target = parent / name
                target.write_bytes(b"not a directory\n")
                vcs.inject_refs(root, {"main": "a" * 40})
                self.assertEqual(target.read_bytes(), b"not a directory\n")


@unittest.skipUnless(os.name == "nt", "Native Windows handle-pinning regression")
class WindowsLegacyRefNativeTests(_LegacyFixture, unittest.TestCase):
    def test_pinned_ancestor_cannot_be_renamed_until_handles_close(self):
        api = cleanup._WindowsAPI()
        saved = self.tmp / "saved-workspace"
        with cleanup._WindowsDirectory(self.root, api) as root:
            with root.child("workspace"):
                with self.assertRaises(OSError):
                    self.workspace.rename(saved)
                # Reparse-point updates require a write-capable handle; the
                # pinned directory must refuse one, not just refuse rename.
                handle = api.kernel.CreateFileW(str(self.workspace), 0x40000000,
                                                7, None, 3, 0x02200000, None)
                if handle != ctypes.c_void_p(-1).value:
                    api.close(handle)
                    self.fail("Pinned ancestor admitted a write-capable handle")
        self.workspace.rename(saved)
        self.assertTrue(saved.is_dir())

    def test_nested_junction_left_in_place_and_external_sentinel_preserved(self):
        self.legacy.mkdir(parents=True)
        external = self.tmp / "external"
        external.mkdir()
        sentinel = external / "sentinel"
        sentinel.write_bytes(b"keep\n")
        junction = self.legacy / "junction"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.inject()
        self.assertTrue(junction.exists())
        self.assertEqual(sentinel.read_bytes(), b"keep\n")


@unittest.skipUnless(os.name == "posix", "POSIX descriptor boundary tests")
class PosixLegacyRefCleanupTests(_LegacyFixture, unittest.TestCase):
    def test_ancestor_swap_before_open_refuses_new_symlink(self):
        parts = ("workspace", *vcs.REF_PREFIX, "nested")
        original = cleanup._PosixDirectory.child
        for depth, name in enumerate(parts):
            with self.subTest(ancestor=name):
                root = self.tmp / f"before-{depth}"
                legacy = root.joinpath(*parts)
                legacy.mkdir(parents=True)
                (legacy / "ordinary").write_bytes(b"old\n")
                external = self.tmp / f"before-external-{depth}"
                sentinel = external.joinpath(*parts[depth + 1:]) / "sentinel"
                sentinel.parent.mkdir(parents=True)
                sentinel.write_bytes(b"keep\n")
                swapped = False

                def swap(directory, child_name):
                    nonlocal swapped
                    if not swapped and child_name == name:
                        swapped = True
                        target = root.joinpath(*parts[:depth + 1])
                        target.rename(self.tmp / f"before-saved-{depth}")
                        target.symlink_to(external, target_is_directory=True)
                    return original(directory, child_name)

                with mock.patch.object(cleanup._PosixDirectory, "child", swap):
                    vcs.inject_refs(root, {"main": "a" * 40})
                self.assertTrue(swapped)
                self.assertEqual(sentinel.read_bytes(), b"keep\n")
                self.assertTrue(root.joinpath(*parts[:depth + 1]).is_symlink())
                saved = self.tmp / f"before-saved-{depth}"
                self.assertEqual((saved.joinpath(*parts[depth + 1:]) / "ordinary").read_bytes(), b"old\n")

    def test_ancestor_swap_after_open_cannot_redirect_pinned_cleanup(self):
        parts = ("workspace", *vcs.REF_PREFIX, "nested")
        original = cleanup._PosixDirectory.names
        for depth, name in enumerate(parts):
            with self.subTest(ancestor=name):
                root = self.tmp / f"after-{depth}"
                legacy = root.joinpath(*parts)
                legacy.mkdir(parents=True)
                (legacy / "ordinary").write_bytes(b"old\n")
                external = self.tmp / f"after-external-{depth}"
                sentinel = external.joinpath(*parts[depth + 1:]) / "sentinel"
                sentinel.parent.mkdir(parents=True)
                sentinel.write_bytes(b"keep\n")
                swapped = False

                def swap(directory):
                    nonlocal swapped
                    if not swapped and directory.path == "nested":
                        swapped = True
                        target = root.joinpath(*parts[:depth + 1])
                        target.rename(self.tmp / f"after-saved-{depth}")
                        target.symlink_to(external, target_is_directory=True)
                    return original(directory)

                with mock.patch.object(cleanup._PosixDirectory, "names", swap):
                    vcs.inject_refs(root, {"main": "a" * 40})
                self.assertTrue(swapped)
                self.assertEqual(sentinel.read_bytes(), b"keep\n")
                self.assertTrue(root.joinpath(*parts[:depth + 1]).is_symlink())
                saved = self.tmp / f"after-saved-{depth}"
                self.assertFalse((saved.joinpath(*parts[depth + 1:]) / "ordinary").exists())

    def test_leaf_swap_does_not_modify_external_inode(self):
        external = self.tmp / "external"
        external.write_bytes(b"keep\n")
        external.chmod(0o444)
        original = cleanup._PosixDirectory.unlink
        for hardlink in (False, True):
            with self.subTest(hardlink=hardlink):
                self.legacy.mkdir(parents=True)
                target = self.legacy / "main"
                target.write_bytes(b"old\n")

                def swap(directory, name):
                    target.unlink()
                    if hardlink:
                        os.link(external, target)
                    else:
                        target.symlink_to(external)
                    original(directory, name)

                with mock.patch.object(cleanup._PosixDirectory, "unlink", swap):
                    self.inject()
                self.assertEqual(external.read_bytes(), b"keep\n")
                self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o444)
                self.assertEqual(external.stat().st_nlink, 1)

    def test_io_errors_leave_failed_entry_and_retry_on_next_refresh(self):
        self.legacy.mkdir(parents=True)
        failed = self.legacy / "failed"
        failed.write_bytes(b"keep until retry\n")
        other = self.legacy / "other"
        other.write_bytes(b"remove\n")
        original = cleanup._PosixDirectory.unlink

        def fail(directory, name):
            if name == "failed":
                raise PermissionError(errno.EACCES, "injected failure")
            original(directory, name)

        with mock.patch.object(cleanup._PosixDirectory, "unlink", fail):
            self.inject()
        self.assertEqual(failed.read_bytes(), b"keep until retry\n")
        self.assertFalse(other.exists())
        self.inject()
        self.assertFalse((self.workspace / "refs").exists())

    def test_enumeration_error_does_not_veto_injection(self):
        self.legacy.mkdir(parents=True)
        target = self.legacy / "main"
        target.write_bytes(b"keep until retry\n")
        with mock.patch.object(cleanup._PosixDirectory, "names", side_effect=OSError(errno.EIO, "I/O")):
            self.inject()
        self.assertTrue(target.exists())
        with mock.patch.object(cleanup._PosixDirectory, "names", side_effect=RuntimeError("bug")):
            with self.assertRaisesRegex(RuntimeError, "bug"):
                self.inject()
        self.inject()
        self.assertFalse(target.exists())

    def test_deep_tree_does_not_veto_injection_and_can_be_retried(self):
        self.legacy.mkdir(parents=True)
        leaf = self.legacy
        for _ in range(110):
            leaf /= "d"
            leaf.mkdir()
        sentinel = leaf / "sentinel"
        sentinel.write_bytes(b"keep until retry\n")
        previous = sys.getrecursionlimit()
        # Leave enough headroom for injection but not this input-derived tree.
        frame, depth = sys._getframe(), 0
        while frame is not None:
            depth += 1
            frame = frame.f_back
        try:
            sys.setrecursionlimit(depth + 60)
            self.inject()  # Includes host ref verification after legacy cleanup.
        finally:
            # Restore before assertions, retry and TemporaryDirectory cleanup.
            sys.setrecursionlimit(previous)
        self.assertEqual(sentinel.read_bytes(), b"keep until retry\n")
        self.inject()
        self.assertFalse((self.workspace / "refs").exists())

    def test_missing_native_capability_has_no_path_fallback(self):
        self.legacy.mkdir(parents=True)
        target = self.legacy / "main"
        target.write_bytes(b"keep\n")
        with mock.patch.object(os, "supports_dir_fd", set()):
            self.inject()
        self.assertTrue(target.exists())
        self.inject()
        self.assertFalse(target.exists())


class WindowsLegacyRefABITests(unittest.TestCase):
    """Mock kernel calls to check flags/layout/closure on non-Windows too."""
    def setUp(self):
        self.kernel = mock.Mock()
        self.kernel.CreateFileW.return_value = 42
        loader = mock.patch.object(ctypes, "WinDLL", create=True, return_value=self.kernel)
        loader.start()
        self.addCleanup(loader.stop)
        self.api = cleanup._WindowsAPI()
        self.attributes = 0x10
        self.links = 1

        def info(handle, pointer):
            pointer._obj.attributes = self.attributes
            pointer._obj.links = self.links
            return 1
        self.kernel.GetFileInformationByHandle.side_effect = info

    def test_no_follow_no_write_delete_sharing_and_exact_handle_disposition(self):
        handle = self.api.open(Path("project"), directory=True)
        args = self.kernel.CreateFileW.call_args.args
        self.assertEqual(args[1], 0x10081)  # DELETE | READ_ATTRIBUTES | LIST_DIRECTORY
        self.assertEqual(args[2], 1)  # FILE_SHARE_READ only
        self.assertEqual(args[4:6], (3, 0x02200000))  # EXISTING, BACKUP | OPEN_REPARSE
        self.assertEqual(ctypes.sizeof(self.api.FileInfo), 52)
        self.assertEqual(self.api.FileInfo.links.offset, 40)
        self.api.delete(handle)
        args = self.kernel.SetFileInformationByHandle.call_args.args
        self.assertEqual(args[:2], (42, 21))
        self.assertEqual(args[2]._obj.value, 0x13)
        self.assertEqual(args[3], 4)
        self.api.close(handle)
        self.kernel.CloseHandle.assert_called_once_with(42)

    def test_reparse_devices_wrong_type_and_hardlinks_refused_and_closed(self):
        for attributes, links, directory in ((0x410, 1, True), (0x400, 1, False),
                                             (0x40, 1, False), (0x10, 1, False),
                                             (0x80, 1, True), (0x80, 2, False)):
            with self.subTest(attributes=attributes, links=links, directory=directory):
                self.attributes, self.links = attributes, links
                self.kernel.CloseHandle.reset_mock()
                with self.assertRaises(OSError):
                    self.api.open(Path("entry"), directory)
                self.kernel.CloseHandle.assert_called_once_with(42)
        self.kernel.SetFileInformationByHandle.assert_not_called()

    def test_readonly_file_deleted_by_handle_without_chmod(self):
        self.attributes = 1  # FILE_ATTRIBUTE_READONLY
        handle = self.api.open(Path("readonly-ref"), directory=False)
        self.api.delete(handle)
        self.api.close(handle)
        self.assertEqual(self.kernel.SetFileInformationByHandle.call_count, 1)

    def test_unsupported_disposition_propagates_and_closes_leaf_handle(self):
        self.kernel.SetFileInformationByHandle.return_value = 0
        self.attributes = 0x80
        with mock.patch.object(ctypes, "get_last_error", create=True, return_value=50), \
                mock.patch.object(ctypes, "WinError", create=True, side_effect=lambda code: OSError(code, "unsupported")):
            directory = object.__new__(cleanup._WindowsDirectory)
            directory.path, directory.api = Path("pinned"), self.api
            with self.assertRaises(OSError):
                directory.unlink("leaf")
        self.kernel.CloseHandle.assert_called_once_with(42)

    def test_open_and_metadata_errors_do_not_delete_or_leak_handles(self):
        with mock.patch.object(ctypes, "get_last_error", create=True, return_value=32), \
                mock.patch.object(ctypes, "WinError", create=True, side_effect=lambda code: OSError(code, "sharing violation")):
            self.kernel.CreateFileW.return_value = ctypes.c_void_p(-1).value
            with self.assertRaises(OSError):
                self.api.open(Path("busy"), directory=True)
            self.kernel.CloseHandle.assert_not_called()
            self.kernel.CreateFileW.return_value = 42
            self.kernel.GetFileInformationByHandle.side_effect = None
            self.kernel.GetFileInformationByHandle.return_value = 0
            with self.assertRaises(OSError):
                self.api.open(Path("unavailable"), directory=True)
            self.kernel.CloseHandle.assert_called_once_with(42)
        self.kernel.SetFileInformationByHandle.assert_not_called()
