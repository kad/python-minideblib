# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Confined writes for the MCP server (POSIX ``dir_fd`` based).

A :class:`WriteRoot` keeps its directory open as a file descriptor and never
resolves a path string again after opening it: subdirectories are created and
opened one component at a time relative to that descriptor without following
symlinks, files are created exclusively under a hidden temporary name
(:data:`TEMP_PREFIX`) and published with a hard link that never replaces an
existing name. There is no rename fallback: where hard links are not
supported (FAT, some network file systems) publishing fails with a clear
error.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import math
import os
import secrets
import stat
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

from minideblib.checksums import ALGORITHMS
from minideblib.errors import MinidebError

__all__ = [
    "TEMP_PREFIX",
    "NewFile",
    "UnsafeNameError",
    "WriteRoot",
    "WriteRootError",
    "session_temp_dir",
    "session_temp_path",
    "validate_file_name",
]

#: Prefix of temporary and probe files (stale ones are removed by
#: :meth:`WriteRoot.cleanup_stale`).
TEMP_PREFIX = ".minideb-"
#: Longest file or directory name, in bytes of UTF-8.
NAME_MAX = 255

_CLOEXEC: int = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
_DIR_FLAGS: int = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC
_CREATE_FLAGS: int = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
_READ_FLAGS: int = (
    os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0) | _CLOEXEC
)
_CREATE_ATTEMPTS = 16
_CHUNK = 1024 * 1024
_HASHES = frozenset(alg.name for alg in ALGORITHMS)
#: errno values of ``link()`` on file systems without hard links
_NO_LINK = frozenset(
    e
    for e in (
        errno.EPERM,
        errno.EXDEV,
        errno.EMLINK,
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "ENOSYS", None),
    )
    if e is not None
)


class WriteRootError(MinidebError, OSError):
    """A write root, subdirectory or file that cannot be used safely.

    ``errno`` is set; ``str()`` is the plain message (no ``[Errno N]`` prefix).
    """

    def __str__(self) -> str:
        return self.strerror or super().__str__()


class UnsafeNameError(MinidebError, ValueError):
    """A file or directory name that is not a plain, visible basename."""


#: Unicode categories refused in names: controls, format characters (such as
#: bidirectional overrides and zero-width characters), line and paragraph
#: separators, private use, surrogates and unassigned code points
_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn"})


def _random_suffix() -> str:
    return secrets.token_hex(8)


def _check_name(name: object, what: str) -> str:
    if not isinstance(name, str):
        raise UnsafeNameError(f"{what} must be a string, not {type(name).__name__}")
    if name in ("", ".", ".."):
        raise UnsafeNameError(f"invalid {what} {name!r}")
    if "/" in name or "\\" in name:
        raise UnsafeNameError(f"{what} {name!r} must not contain path separators")
    if name.startswith("."):
        raise UnsafeNameError(f"{what} {name!r} must not start with a dot")
    if name.startswith("-"):
        raise UnsafeNameError(f"{what} {name!r} must not start with a dash")
    if any(unicodedata.category(c) in _BAD_CATEGORIES for c in name):
        raise UnsafeNameError(
            f"{what} {name!r} contains control, format, separator or unassigned characters"
        )
    if name[0].isspace() or name[-1].isspace():
        raise UnsafeNameError(f"{what} {name!r} must not start or end with whitespace")
    try:
        size = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        raise UnsafeNameError(f"{what} {name!r} is not valid Unicode") from None
    if size > NAME_MAX:
        raise UnsafeNameError(f"{what} is longer than {NAME_MAX} bytes")
    return name


def validate_file_name(name: str) -> str:
    """Return *name* if it is a plain visible basename, else raise :class:`UnsafeNameError`.

    Refused: anything but a non-empty ``str``, ``.``/``..``, path separators
    (``/`` and ``\\``), a leading dot (hidden files, including the
    :data:`TEMP_PREFIX` names), a leading dash (option-like), leading or
    trailing whitespace (so also whitespace only), characters of the Unicode
    categories Cc (NUL and other controls), Cf, Zl, Zp, Co, Cs (lone
    surrogates) and Cn, and names over 255 bytes of UTF-8. Names are not
    normalized (NFC is not required).
    """
    return _check_name(name, "file name")


def _check_dir(info: os.stat_result, shown: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise WriteRootError(errno.ENOTDIR, f"{shown}: not a directory")
    mode = stat.S_IMODE(info.st_mode)
    if mode & stat.S_IWOTH and not mode & stat.S_ISVTX:
        raise WriteRootError(
            errno.EPERM, f"{shown}: writable by all users without the sticky bit (refused)"
        )


def _closed_error() -> WriteRootError:
    return WriteRootError(errno.EBADF, "write root is closed")


class WriteRoot:
    """An open directory that files are written into (see the module docs).

    Create with :meth:`open` (or :meth:`subdir`); close with :meth:`close` or
    use as a context manager. :attr:`path` is for reporting only and is never
    used to reach the directory again.
    """

    def __init__(self, fd: int, path: str) -> None:
        self._fd: int | None = fd
        self.path = path
        #: positions in the *parts* of :meth:`subdir` of the components it created
        self.created: tuple[int, ...] = ()

    @classmethod
    def open(cls, path: str | Path, *, create: bool = False, private: bool = False) -> WriteRoot:
        """Open the directory *path* (a final symlink is refused).

        With *create*, a missing last component is created (mode 0777 minus
        the umask). With *private*, the directory must be owned by the
        effective user and inaccessible to anybody else (no group/other
        permission bits). A directory writable by all users without the
        sticky bit is always refused. Failures raise :class:`WriteRootError`
        naming the path. Writability is not required here: see :meth:`writable`.
        """
        shown = os.path.abspath(os.fspath(path))
        if create:
            try:
                os.mkdir(shown)
            except FileExistsError:
                pass
            except OSError as exc:
                raise WriteRootError(
                    exc.errno, f"{shown}: cannot create: {exc.strerror or exc}"
                ) from exc
        try:
            fd = os.open(shown, _DIR_FLAGS)
        except FileNotFoundError as exc:
            raise WriteRootError(exc.errno, f"{shown}: does not exist") from exc
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise WriteRootError(
                    exc.errno, f"{shown}: not a directory (or a symlink, which is refused)"
                ) from exc
            raise WriteRootError(exc.errno, f"{shown}: cannot open: {exc.strerror or exc}") from exc
        try:
            info = os.fstat(fd)
            _check_dir(info, shown)
            if private:
                if info.st_uid != os.geteuid():
                    raise WriteRootError(errno.EPERM, f"{shown}: not owned by the current user")
                if stat.S_IMODE(info.st_mode) & 0o077:
                    raise WriteRootError(errno.EPERM, f"{shown}: accessible by other users")
        except BaseException:
            os.close(fd)
            raise
        return cls(fd, os.path.realpath(shown))

    # -- lifecycle ---------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` was called."""
        return self._fd is None

    def fileno(self) -> int:
        """The directory file descriptor (:class:`WriteRootError` once closed)."""
        if self._fd is None:
            raise _closed_error()
        return self._fd

    def close(self) -> None:
        """Close the directory descriptor (idempotent)."""
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._fd is None else f"fd={self._fd}"
        return f"WriteRoot({self.path!r}, {state})"

    # -- checks ------------------------------------------------------------------

    def writable(self) -> bool:
        """Whether files can be created here by the effective user right now.

        Both ``access(W_OK|X_OK)`` with the effective ids and a real probe:
        a :data:`TEMP_PREFIX` file created exclusively and removed again.
        """
        fd = self.fileno()
        if os.access in os.supports_dir_fd:
            kw: dict[str, bool] = {}
            if os.access in os.supports_effective_ids:
                kw["effective_ids"] = True
            if not os.access(".", os.W_OK | os.X_OK, dir_fd=fd, **kw):
                return False
        name = f"{TEMP_PREFIX}probe-{_random_suffix()}"
        try:
            probe = os.open(name, _CREATE_FLAGS, 0o600, dir_fd=fd)
        except OSError:
            return False
        os.close(probe)
        try:
            os.unlink(name, dir_fd=fd)
        except OSError:
            return False
        return True

    def free_bytes(self) -> int:
        """Bytes available to the user on the file system (``fstatvfs``)."""
        info = os.fstatvfs(self.fileno())
        return info.f_bavail * info.f_frsize

    # -- navigation ----------------------------------------------------------------

    def subdir(self, parts: Sequence[str]) -> WriteRoot:
        """Open (creating as needed) the subdirectory *parts*, one component at a time.

        Each part must pass the same rules as :func:`validate_file_name`
        (:class:`UnsafeNameError` otherwise, before anything is created). A
        component that is a symlink, not a directory, or writable by all users
        without the sticky bit raises :class:`WriteRootError`. An empty *parts*
        opens this directory again. The result is a new, independent root;
        its :attr:`created` lists the positions of the components this call
        created (for :meth:`remove_created`).
        """
        if isinstance(parts, (str, bytes)):
            raise TypeError("parts must be a sequence of names, not a string")
        names = [_check_name(part, "directory name") for part in parts]
        fd = os.dup(self.fileno())
        shown = self.path
        created: list[int] = []
        try:
            for i, name in enumerate(names):
                shown = os.path.join(shown, name)
                try:
                    os.mkdir(name, 0o777, dir_fd=fd)
                    created.append(i)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise WriteRootError(
                        exc.errno, f"{shown}: cannot create: {exc.strerror or exc}"
                    ) from exc
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise WriteRootError(
                            exc.errno, f"{shown}: not a directory (or a symlink, which is refused)"
                        ) from exc
                    raise WriteRootError(
                        exc.errno, f"{shown}: cannot open: {exc.strerror or exc}"
                    ) from exc
                os.close(fd)
                fd = child
                _check_dir(os.fstat(fd), shown)
        except BaseException:
            os.close(fd)
            raise
        sub = WriteRoot(fd, shown)
        sub.created = tuple(created)
        return sub

    def remove_created(self, parts: Sequence[str], created: Sequence[int]) -> None:
        """Remove the directories ``parts[i]`` (*i* in *created*) below this root if empty.

        For undoing :meth:`subdir`: *created* is its :attr:`created`. The
        path is walked again from this root without following symlinks,
        deepest directory first; one that is gone already is skipped, and a
        component that is replaced or not empty stops the removal (nothing
        else is touched). Never raises ``OSError``.
        """
        if not created:
            return
        fds = [os.dup(self.fileno())]
        try:
            for name in parts[: max(created)]:
                try:
                    fds.append(os.open(name, _DIR_FLAGS, dir_fd=fds[-1]))
                except OSError:
                    return
            for i in sorted(created, reverse=True):
                try:
                    os.rmdir(parts[i], dir_fd=fds[i])
                except FileNotFoundError:
                    continue  # removed already
                except OSError:
                    return
        finally:
            for fd in fds:
                os.close(fd)

    # -- files -----------------------------------------------------------------------

    def create_file(self, name: str, *, size_hint: int | None = None) -> NewFile:
        """Start writing the file *name* under a hidden temporary name.

        *name* is validated with :func:`validate_file_name` and becomes the
        default of :meth:`NewFile.commit`. With *size_hint*, fewer free bytes
        than that raise :class:`WriteRootError` (``ENOSPC``) before anything is
        created; a negative one raises ``ValueError``. The temporary file is
        created with ``O_CREAT|O_EXCL|O_NOFOLLOW`` and mode 0666 minus the
        umask (the published file keeps that mode); an existing name is never
        reused.
        """
        validate_file_name(name)
        if size_hint is not None and size_hint < 0:
            raise ValueError(f"size_hint must not be negative, got {size_hint}")
        if size_hint is not None and self.free_bytes() < size_hint:
            raise WriteRootError(
                errno.ENOSPC,
                f"{self.path}: not enough free space for {name} ({size_hint} bytes needed)",
            )
        dir_fd = os.dup(self.fileno())
        try:
            for attempt in range(_CREATE_ATTEMPTS):
                temp = TEMP_PREFIX + _random_suffix()
                try:
                    fd = os.open(temp, _CREATE_FLAGS, 0o666, dir_fd=dir_fd)
                except FileExistsError:
                    if attempt == _CREATE_ATTEMPTS - 1:
                        raise
                    continue
                return NewFile(dir_fd, self.path, temp, name, fd)
            raise AssertionError("unreachable")  # pragma: no cover
        except BaseException:
            os.close(dir_fd)
            raise

    def exists(self, name: str) -> bool:
        """Whether *name* exists here (``lstat``: a dangling symlink exists too)."""
        validate_file_name(name)
        try:
            os.stat(name, dir_fd=self.fileno(), follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def hash_existing(
        self, name: str, algorithm: str, *, check: Callable[[], None] | None = None
    ) -> str:
        """Hex digest of the regular file *name* (symlinks are not followed).

        *algorithm* is a hashlib name from :data:`minideblib.checksums.ALGORITHMS`
        (``ValueError`` otherwise). A symlink, directory, FIFO or device raises
        :class:`WriteRootError`; a missing file :class:`FileNotFoundError`.
        *check* is called before every chunk read (1 MiB); whatever it raises
        (cancellation, a deadline) stops the hashing.
        """
        if algorithm not in _HASHES:
            raise ValueError(f"unsupported checksum algorithm {algorithm!r}")
        validate_file_name(name)
        shown = os.path.join(self.path, name)
        try:
            fd = os.open(name, _READ_FLAGS, dir_fd=self.fileno())
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EISDIR):
                raise WriteRootError(exc.errno, f"{shown}: not a regular file") from exc
            raise
        try:
            regular = stat.S_ISREG(os.fstat(fd).st_mode)
        except BaseException:
            os.close(fd)
            raise
        if not regular:
            os.close(fd)
            raise WriteRootError(errno.EINVAL, f"{shown}: not a regular file")
        with open(fd, "rb", buffering=0) as handle:
            # O_NONBLOCK only guarded the open against FIFOs
            os.set_blocking(fd, True)
            hasher = hashlib.new(algorithm, usedforsecurity=False)
            while True:
                if check is not None:
                    check()
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    def cleanup_stale(self, max_age: float = 3600.0) -> int:
        """Remove leftover :data:`TEMP_PREFIX` files older than *max_age* seconds.

        Only regular files owned by the effective user are removed (symlinks
        and directories with that prefix are left alone, nothing is
        followed). Returns the number of files removed. *max_age* must be a
        finite number >= 0 (``ValueError`` otherwise); a small value is unsafe
        while another writer (another server in the same directory) may still
        be writing its temporary files, and 0 removes them all.
        """
        if (
            isinstance(max_age, bool)
            or not isinstance(max_age, (int, float))
            or not math.isfinite(max_age)
            or max_age < 0
        ):
            raise ValueError(f"max_age must be a finite number >= 0, got {max_age!r}")
        fd = self.fileno()
        uid = os.geteuid()
        limit = time.time() - max_age
        removed = 0
        with os.scandir(fd) as entries:
            names = [e.name for e in entries if e.name.startswith(TEMP_PREFIX)]
        for name in names:
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mtime >= limit:
                continue
            try:
                os.unlink(name, dir_fd=fd)
            except OSError:
                continue
            removed += 1
        return removed


class NewFile:
    """A file being written under a temporary name (from :meth:`WriteRoot.create_file`).

    Write to :attr:`file`, then :meth:`commit` to publish it or
    :meth:`abort` to remove it. Leaving the ``with`` block without a
    successful commit aborts. Holds its own duplicate of the directory
    descriptor, so it outlives the root it came from.
    """

    def __init__(self, dir_fd: int, root_path: str, temp_name: str, name: str, fd: int) -> None:
        self._dir_fd: int | None = dir_fd
        self.root_path = root_path
        self.temp_name = temp_name
        self.name = name
        self.committed = False
        self._ident = _identity(os.fstat(fd))
        self.file: io.BufferedWriter = open(fd, "wb", closefd=True)  # noqa: SIM115

    @property
    def identity(self) -> tuple[int, int]:
        """``(st_dev, st_ino)`` of the file created (also of the published name)."""
        return self._ident

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self.committed:
            self.abort()

    def commit(self, final_name: str | None = None) -> str:
        """Flush, ``fsync`` and publish the file as *final_name* (default: the name given).

        The name is linked with ``link()`` relative to the directory
        descriptor, so an existing name (file, directory or symlink) is never
        replaced: :class:`FileExistsError` then. Then the temporary name is
        removed and the directory synced. Any failure (also an invalid
        *final_name*) removes the temporary file; a file system without hard
        links raises :class:`WriteRootError`. Returns the path of the
        published file (for reporting).

        The checks that the temporary name and the published name are still
        the opened file (device and inode) are best effort against other
        writers of the directory: they narrow races, they cannot close them.
        """
        dir_fd = self._dir_fd
        if dir_fd is None or self.committed:
            raise WriteRootError(errno.EBADF, f"{self.temp_name}: already committed or aborted")
        try:
            name = validate_file_name(self.name if final_name is None else final_name)
            shown = os.path.join(self.root_path, name)
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
            if _identity(os.stat(self.temp_name, dir_fd=dir_fd, follow_symlinks=False)) != (
                self._ident
            ):
                raise WriteRootError(errno.EEXIST, f"{shown}: temporary file was replaced")
            kw: dict[str, bool] = {}
            if os.link in os.supports_follow_symlinks:
                kw["follow_symlinks"] = False
            try:
                os.link(self.temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, **kw)
            except FileExistsError:
                raise FileExistsError(errno.EEXIST, f"{shown}: already exists") from None
            except OSError as exc:
                if exc.errno in _NO_LINK:
                    raise WriteRootError(
                        exc.errno,
                        f"{shown}: cannot publish: the file system does not support hard links "
                        f"({exc.strerror or exc})",
                    ) from exc
                raise
            if _identity(os.stat(name, dir_fd=dir_fd, follow_symlinks=False)) != self._ident:
                # best effort: whatever is under the final name now is not ours, so it is
                # left alone (it may belong to another writer); abort() removes our temp link
                raise WriteRootError(errno.EEXIST, f"{shown}: replaced while publishing")
            try:
                os.unlink(self.temp_name, dir_fd=dir_fd)
                os.fsync(dir_fd)
            except OSError:
                # not durably published: take our own link back so a failed commit
                # never leaves a published file the caller does not know about
                with contextlib.suppress(OSError):
                    if _identity(os.stat(name, dir_fd=dir_fd, follow_symlinks=False)) == (
                        self._ident
                    ):
                        os.unlink(name, dir_fd=dir_fd)
                raise
            self.committed = True
        except BaseException:
            if not self.committed:
                self.abort()
            raise
        finally:
            if self.committed:
                self._close_dir()
        return shown

    def abort(self) -> None:
        """Remove the temporary file (idempotent; nothing to do after a commit)."""
        if self.committed:
            return
        self.file.close()
        dir_fd = self._dir_fd
        if dir_fd is None:
            return
        try:
            info = os.stat(self.temp_name, dir_fd=dir_fd, follow_symlinks=False)
            if _identity(info) == self._ident:
                os.unlink(self.temp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        finally:
            self._close_dir()

    def _close_dir(self) -> None:
        fd, self._dir_fd = self._dir_fd, None
        if fd is not None:
            os.close(fd)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


# -- session temp directory ---------------------------------------------------------

_session: WriteRoot | None = None
_session_lock = threading.Lock()


def session_temp_dir() -> WriteRoot:
    """The private temporary directory of this process, created on first use.

    ``mkdtemp(prefix="minideb-mcp-")`` under :func:`tempfile.gettempdir`
    (mode 0700), opened as a private :class:`WriteRoot`. The directory is
    the same for every call, but each call returns a new root (its own
    descriptor) that the caller owns and should close. The directory is not
    removed at exit. Failing to create it raises :class:`WriteRootError`.
    """
    global _session
    with _session_lock:
        if _session is None:
            try:
                path = tempfile.mkdtemp(prefix="minideb-mcp-", dir=tempfile.gettempdir())
            except OSError as exc:
                raise WriteRootError(
                    exc.errno, f"cannot create a temporary directory: {exc.strerror or exc}"
                ) from exc
            _session = WriteRoot.open(path, private=True)
        return _session.subdir([])


def session_temp_path() -> str | None:
    """The path of the session temporary directory, or None if not created yet.

    Creates nothing (for reporting, e.g. by the overview tool).
    """
    with _session_lock:
        return None if _session is None else _session.path
