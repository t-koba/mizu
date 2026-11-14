"""Best-effort removal of the obsolete workspace ref store, never by path walk.

The project root and its ancestors are trusted host state. Below that root,
POSIX uses no-follow directory descriptors; Windows pins each directory against
write/delete sharing and deletes through no-follow handles. No resolve/lstat
fallback is safe enough for a destructive operation. Unsupported APIs or I/O
failures leave leftovers for another refresh; they do not affect host refs.
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


class _PosixDirectory:
    def __init__(self, path, parent=None):
        self.parent = parent
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                          os.O_CLOEXEC, dir_fd=None if parent is None else parent.fd)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        os.close(self.fd)

    def child(self, name):
        return _PosixDirectory(name, self)

    def names(self):
        return os.listdir(self.fd)

    def info(self, name):
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    def unlink(self, name):
        # unlinkat never follows the leaf, even if it was swapped after stat.
        # Do not chmod: POSIX unlink works for read-only files, and chmod could
        # modify an external inode through a concurrently introduced hardlink.
        os.unlink(name, dir_fd=self.fd)

    def remove(self):
        before = os.fstat(self.fd)
        current = self.parent.info(self.path)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            return
        # rmdir does not follow a swapped leaf link either.
        os.rmdir(self.path, dir_fd=self.parent.fd)


class _WindowsAPI:
    """Win32 handle operations, loaded only on Windows (no path chmod/unlink).

    FILE_DISPOSITION_INFO_EX requires Windows 10 1809+ / a supporting filesystem.
    There is deliberately no weaker path-based fallback on older systems.
    """
    def __init__(self):
        import ctypes
        self.ctypes = ctypes
        dword = ctypes.c_uint32
        handle = ctypes.c_void_p

        class FileInfo(ctypes.Structure):
            _fields_ = [("attributes", dword), ("times", dword * 6),
                        ("volume", dword), ("size", dword * 2),
                        ("links", dword), ("index", dword * 2)]

        self.FileInfo = FileInfo
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.argtypes = (ctypes.c_wchar_p, dword, dword,
                                            handle, dword, dword, handle)
        self.kernel.CreateFileW.restype = handle
        self.kernel.GetFileInformationByHandle.argtypes = (handle, ctypes.POINTER(FileInfo))
        self.kernel.GetFileInformationByHandle.restype = ctypes.c_int
        self.kernel.SetFileInformationByHandle.argtypes = (handle, ctypes.c_int, handle, dword)
        self.kernel.SetFileInformationByHandle.restype = ctypes.c_int
        self.kernel.CloseHandle.argtypes = (handle,)
        self.kernel.CloseHandle.restype = ctypes.c_int

    def open(self, path, directory):
        # READ_ATTRIBUTES | DELETE; directories also need LIST_DIRECTORY.
        # Share READ only: a successful open excludes existing/future writers
        # and deleters of this object, so pinned ancestors cannot be renamed
        # or changed into reparse points while descendants use their paths.
        handle = self.kernel.CreateFileW(
            os.path.abspath(path), 0x80 | 0x10000 | (1 if directory else 0),
            1, None, 3, 0x02000000 | 0x00200000, None)
        if handle == self.ctypes.c_void_p(-1).value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            info = self.FileInfo()
            if not self.kernel.GetFileInformationByHandle(handle, self.ctypes.byref(info)):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            if (info.attributes & (0x400 | 0x40) or  # REPARSE_POINT | DEVICE
                    bool(info.attributes & 0x10) != directory or
                    (not directory and info.links != 1)):
                raise OSError(errno.EINVAL, "Unsafe legacy ref entry")
            return handle
        except BaseException:
            self.close(handle)
            raise

    def close(self, handle):
        self.kernel.CloseHandle(handle)

    def delete(self, handle):
        # FileDispositionInfoEx: DELETE | POSIX_SEMANTICS | IGNORE_READONLY.
        # This removes a read-only ref without changing a shared inode's
        # attributes, and targets the opened object, not a replaceable path.
        flags = self.ctypes.c_uint32(0x1 | 0x2 | 0x10)
        if not self.kernel.SetFileInformationByHandle(handle, 21, self.ctypes.byref(flags), 4):
            raise self.ctypes.WinError(self.ctypes.get_last_error())


class _WindowsDirectory:
    def __init__(self, path, api):
        self.path = path
        self.api = api
        self.handle = api.open(path, directory=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.api.close(self.handle)

    def child(self, name):
        return _WindowsDirectory(self.path / name, self.api)

    def names(self):
        return os.listdir(self.path)

    def info(self, name):
        # Classification only. The no-follow open validates the actual object
        # before any deletion; all ancestors remain pinned throughout.
        return (self.path / name).lstat()

    def unlink(self, name):
        handle = self.api.open(self.path / name, directory=False)
        try:
            self.api.delete(handle)
        finally:
            self.api.close(handle)

    def remove(self):
        self.api.delete(self.handle)


def _clean(directory):
    for name in directory.names():
        try:
            info = directory.info(name)
            if getattr(info, "st_file_attributes", 0) & 0x400:
                continue
            if stat.S_ISDIR(info.st_mode):
                with directory.child(name) as child:
                    _clean(child)
                    child.remove()
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                directory.unlink(name)
        except OSError:
            # A single unsafe/busy entry must not veto unrelated cleanup.
            continue


def _descend(directory, parts):
    try:
        with directory.child(parts[0]) as child:
            if len(parts) == 1:
                try:
                    _clean(child)
                except RecursionError:
                    # Input-controlled depth is a cleanup limit, not a failed
                    # injection. Nested handles unwind; leave the rest for retry.
                    return
            else:
                _descend(child, parts[1:])
            child.remove()
    except OSError:
        pass


def remove_legacy_refs(project_root: Path, prefix: tuple[str, ...]) -> None:
    """Remove only workspace/prefix, pruning prefix but never workspace.

    Single pass, no retry. Links, specials, unavailable paths and unsupported
    native operations are left alone. An opened POSIX directory remains the
    authority if renamed; name-based unlink/rmdir cannot follow a substituted
    symlink. As with any concurrent cleanup, newly created entries may survive
    and an entry substituted immediately before unlink may itself be unlinked;
    no substituted link is followed and no file content/permissions are edited.
    """
    try:
        if os.name == "nt":
            root = _WindowsDirectory(project_root, _WindowsAPI())
        elif (all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"))
              and all(call in os.supports_dir_fd for call in (os.open, os.stat, os.unlink, os.rmdir))
              and os.listdir in os.supports_fd):
            root = _PosixDirectory(project_root)
        else:
            return  # No safe destructive fallback.
        with root:
            with root.child("workspace") as workspace:
                _descend(workspace, prefix)
    except OSError:
        pass
