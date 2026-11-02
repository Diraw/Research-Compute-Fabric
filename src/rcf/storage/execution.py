"""Controller-side process-lifetime exclusion for cleanup execution.

The OS releases this advisory lock on process death; heartbeat age and PID
existence are diagnostics, never grounds for taking over a foreign controller.
The target-side generation lock separately fences late remote deletion calls.
"""

from __future__ import annotations

import hashlib
import os
import platform
import stat
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ..core.errors import Conflict, UnsupportedOperation
from ..core.ids import iso, new_id, now_utc

_PROCESS_INSTANCE = uuid.uuid4().hex
_PROCESS_OBSERVED_AT = iso(now_utc())


def controller_id() -> str:
    # A hashed machine identity; never store a raw MAC address in the ledger.
    source = f"{platform.node()}:{uuid.getnode()}:{os.name}"
    return hashlib.sha256(source.encode()).hexdigest()


def owner_evidence() -> dict:
    return {"token": new_id("execution"), "controller": controller_id(), "pid": os.getpid(),
            "process_instance": _PROCESS_INSTANCE, "process_observed_at": _PROCESS_OBSERVED_AT,
            "claimed_at": iso(now_utc()), "heartbeat_at": iso(now_utc()), "stage": "claimed"}


def _same_file(first, second):
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


@contextmanager
def _posix_lock_file(root: Path, name: str):
    required = (os.open, os.mkdir, os.stat)
    if (not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0)
            or any(func not in os.supports_dir_fd for func in required)
            or os.stat not in os.supports_follow_symlinks):
        raise UnsupportedOperation("safe controller execution locks require dir_fd and O_NOFOLLOW support")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directories = []
    fd = None
    try:
        directories.append(os.open(root.anchor, directory_flags))
        for part in root.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directories[-1])
            except FileExistsError:
                pass
            child = os.open(part, directory_flags, dir_fd=directories[-1])
            directories.append(child)
            current = os.stat(part, dir_fd=directories[-2], follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or not _same_file(os.fstat(child), current):
                raise Conflict("controller execution lock path contains a link or changed directory")
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        # O_NONBLOCK also prevents a malicious FIFO from hanging before fstat.
        fd = os.open(name, flags | getattr(os, "O_NONBLOCK", 0), 0o600, dir_fd=directories[-1])
        attrs = os.fstat(fd)
        current = os.stat(name, dir_fd=directories[-1], follow_symlinks=False)
        if (not stat.S_ISREG(attrs.st_mode) or attrs.st_nlink != 1
                or not stat.S_ISREG(current.st_mode) or not _same_file(attrs, current)):
            raise Conflict("unsafe controller execution lock file")
        # Detect ancestors renamed/replaced while the descriptor chain was built.
        for parent, child, part in zip(directories, directories[1:], root.parts[1:], strict=False):
            current = os.stat(part, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or not _same_file(os.fstat(child), current):
                raise Conflict("controller execution lock directory identity changed")
        yield fd
    finally:
        if fd is not None:
            os.close(fd)
        for directory in reversed(directories):
            os.close(directory)


class _WindowsFiles:
    """Small, typed Win32 boundary. Never traverse a reparse point on open."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class Information(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME),
                        ("accessed", wintypes.FILETIME), ("written", wintypes.FILETIME),
                        ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                        ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                        ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

        self.ctypes = ctypes
        self.Information = Information
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                            wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Information)]
        self.kernel.GetFileInformationByHandle.restype = wintypes.BOOL
        self.kernel.GetFileType.argtypes = [wintypes.HANDLE]
        self.kernel.GetFileType.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.invalid = wintypes.HANDLE(-1).value

    def open(self, path: Path, *, directory: bool, create: bool = False):
        # Directories deny WRITE as well as DELETE sharing: a held ancestor may
        # neither be renamed nor turned into a junction in-place. Lock files must
        # share READ/WRITE so contenders reach msvcrt.locking rather than failing
        # at CreateFileW. Neither kind shares DELETE.
        # LIST_DIRECTORY makes this a data-access open, so Windows actually
        # enforces its sharing restrictions (READ_ATTRIBUTES alone does not).
        access = 0x81 if directory else 0xC0000000  # LIST_DIRECTORY|READ_ATTRIBUTES / GENERIC_READ|WRITE
        sharing = 0x1 if directory else 0x3
        flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT, BACKUP_SEMANTICS
        handle = self.kernel.CreateFileW(str(path), access, sharing, None, 4 if create else 3, flags, None)
        if handle == self.invalid:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return handle

    def information(self, handle, *, directory: bool):
        info = self.Information()
        if not self.kernel.GetFileInformationByHandle(handle, self.ctypes.byref(info)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        if (info.attributes & 0x400 or bool(info.attributes & 0x10) != directory
                or self.kernel.GetFileType(handle) != 1 or (not directory and info.links != 1)):
            raise Conflict("unsafe controller execution lock file or link/reparse directory")
        return info.volume, info.index_high, info.index_low

    def verify(self, path: Path, handle, *, directory: bool):
        identity = self.information(handle, directory=directory)
        other = self.open(path, directory=directory)
        try:
            if identity != self.information(other, directory=directory):
                raise Conflict("controller execution lock path identity changed")
        finally:
            self.close(other)

    def close(self, handle):
        self.kernel.CloseHandle(handle)


@contextmanager
def _windows_lock_file(root: Path, name: str):
    import msvcrt

    api = _WindowsFiles()
    directories = []
    handle = None
    fd = None
    try:
        path = Path(root.anchor)
        # Pin the volume/share root first, then every parent before using a
        # longer pathname. mkdir never follows an unpinned intermediate parent.
        for part in (None, *root.parts[1:]):
            if part is not None:
                path = path / part
                try:
                    path.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            directory = api.open(path, directory=True)
            directories.append(directory)
            api.verify(path, directory, directory=True)
        path = root / name
        handle = api.open(path, directory=False, create=True)
        api.verify(path, handle, directory=False)
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
        handle = None  # Ownership transferred to the CRT descriptor.
        yield fd
    finally:
        if fd is not None:
            os.close(fd)
        if handle is not None:
            api.close(handle)
        for directory in reversed(directories):
            api.close(directory)


@contextmanager
def execution_lock(home: Path, plan_id: str):
    """One live applier, including two threads and the claim-before-guard window."""

    root = Path(home).absolute() / "state" / "storage-executions"
    if ".." in root.parts:
        raise Conflict("controller execution lock path must not contain parent traversal")
    name = hashlib.sha256(plan_id.encode()).hexdigest() + ".lock"
    opener = _windows_lock_file if os.name == "nt" else _posix_lock_file
    with ExitStack() as stack:
        try:
            fd = stack.enter_context(opener(root, name))
        except OSError as exc:
            raise Conflict("unsafe or inaccessible controller execution lock path",
                           detail={"plan_id": plan_id, "errno": exc.errno}) from exc
        locked = False
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                raise Conflict("another live process owns this storage plan; do not resume it yet",
                               detail={"plan_id": plan_id}) from exc
            # Locking an empty range is supported on both platforms. Only
            # the winner writes, so a contender cannot hit a byte-lock error
            # during initialization instead of reporting normal contention.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            evidence = owner_evidence()
            scope = str((root / name).absolute())
            if os.name == "nt":
                scope = scope.casefold()
            evidence["lock_scope"] = hashlib.sha256(scope.encode()).hexdigest()
            yield evidence
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
