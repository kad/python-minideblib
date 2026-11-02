# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2005-2006, 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Pure-Python reader for Debian binary packages (.deb)."""

from __future__ import annotations

import contextlib
import io
import logging
import math
import posixpath
import re
import tarfile
from collections.abc import Generator, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import ar
from .compress import DEFAULT_MAX_SIZE, DecompressError, DecompressionLimitError
from .compress import decompress as _decompress
from .control import Paragraph, parse_paragraphs
from .errors import MinidebError

__all__ = [
    "DebEntryLimitError",
    "DebError",
    "DebFile",
    "DebMemberTooLargeError",
    "DebPackage",
    "DebSizeLimitError",
    "MissingMemberError",
    "decompress",
]

log = logging.getLogger(__name__)

# debian-binary: format 2.x (major 2, any minor), as dpkg-deb accepts
_FORMAT_RE = re.compile(r"2\.[0-9]+")
_QUOTE_LIMIT = 40


def _quote(s: str) -> str:
    """``repr`` of at most :data:`_QUOTE_LIMIT` characters of *s*, ``...`` if cut."""
    if len(s) <= _QUOTE_LIMIT:
        return repr(s)
    return f"{s[:_QUOTE_LIMIT]!r}..."


class DebError(MinidebError):
    """Invalid or unsafe .deb package."""


class DebSizeLimitError(DebError, DecompressionLimitError):
    """Decompressed or extracted data exceeds the size limit."""


class DebMemberTooLargeError(DebSizeLimitError):
    """:meth:`DebPackage.read`: the member is larger than the *max_size* given."""


class DebEntryLimitError(DebSizeLimitError):
    """:meth:`DebPackage.files`: the data archive has more than *max_entries* entries."""


class MissingMemberError(DebError, KeyError):
    """:meth:`DebPackage.read` found no regular file of that name (also a ``KeyError``)."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


def decompress(data: bytes, name: str, max_size: int) -> bytes:
    """:func:`minideblib.compress.decompress` raising :class:`DebError`.

    :class:`DebSizeLimitError` when the output would exceed *max_size*.
    """
    try:
        return _decompress(data, name, max_size)
    except DecompressionLimitError as exc:
        raise DebSizeLimitError(str(exc)) from exc
    except DecompressError as exc:
        raise DebError(f"cannot decompress {name}: {exc}") from exc


@dataclass(frozen=True)
class DebFile:
    """Entry of the data archive. *type* is 'f', 'd', 'l' (symlink), 'h' (hardlink) or 'o'."""

    path: str
    size: int
    mode: int
    uid: int
    gid: int
    mtime: float
    type: str
    linkname: str


_TYPES = {
    tarfile.REGTYPE: "f",
    tarfile.AREGTYPE: "f",
    tarfile.CONTTYPE: "f",
    tarfile.DIRTYPE: "d",
    tarfile.SYMTYPE: "l",
    tarfile.LNKTYPE: "h",
}


#: Member mtimes must lie in ``[0, _MTIME_LIMIT)`` (up to the year 6325; safe
#: for ``os.utime`` with a 64-bit ``time_t`` and for ``datetime``).
_MTIME_LIMIT = 2**37


def _valid_mtime(value: float) -> bool:
    return isinstance(value, int | float) and math.isfinite(value) and 0 <= value < _MTIME_LIMIT


def _norm(name: str) -> str:
    n = posixpath.normpath(name)
    return "" if n == "." else n


def _unsafe(name: str) -> bool:
    return name.startswith("/") or ".." in PurePosixPath(name).parts


@contextlib.contextmanager
def _tar_errors() -> Generator[None]:
    try:
        yield
    except MinidebError:
        raise
    except (tarfile.TarError, KeyError, ValueError, OverflowError) as exc:
        raise DebError(f"bad tar archive: {exc}") from exc


def _open_tar(data: bytes) -> tarfile.TarFile:
    try:
        return tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except tarfile.TarError as exc:
        raise DebError(f"bad tar archive: {exc}") from exc


def _check_hard_link(
    name: str, link: str, target: tarfile.TarInfo | None, skipped: set[str]
) -> None:
    """Reject a hard link unless it points to an earlier regular member.

    A link to a skipped symlink could otherwise recreate it as a copy (or,
    with older tarfile fallbacks, as the absolute link itself).
    """
    if target is None:
        raise DebError(f"hard link to a missing member: {name!r} -> {link!r}")
    if target.issym():
        what = "skipped symlink" if _norm(link) in skipped else "symlink"
        raise DebError(f"hard link to {what}: {name!r} -> {link!r}")
    if not target.isreg():
        raise DebError(f"hard link to a non-regular member: {name!r} -> {link!r}")


#: symlinks followed while resolving one link target before giving up
_MAX_LINK_HOPS = 40


def _resolves_inside(path: str, links: dict[str, str]) -> bool:
    """Whether archive path *path* stays below the extraction root.

    Resolution is lexical but follows the symlinks in *links* (archive path
    -> target, for the links that will be created), the way the file system
    will once they exist: ``..`` above the root, an absolute target or too
    many links mean the path escapes.
    """
    parts = [p for p in path.split("/") if p not in ("", ".")]
    out: list[str] = []
    hops = 0
    while parts:
        part = parts.pop(0)
        if part == "..":
            if not out:
                return False
            out.pop()
            continue
        target = links.get("/".join([*out, part]))
        if target is None:
            out.append(part)
            continue
        hops += 1
        if hops > _MAX_LINK_HOPS or target.startswith("/"):
            return False
        parts = [p for p in target.split("/") if p not in ("", ".")] + parts
    return True


class DebPackage:
    """A ``.deb`` read into memory: control data, file list, reading and safe extraction.

    The ``ar`` container and the control archive are parsed on construction
    (:class:`DebError` if they are malformed); the data archive is
    decompressed on first use and kept. *max_size* bounds every
    decompression and, by default, the size :meth:`extract` writes.

    The ``debian-binary`` member must hold format ``2.x`` (any minor
    version); another or malformed content raises :class:`DebError`.

    Attributes: ``format_version`` (``debian-binary``), ``control`` (the
    first paragraph of ``control``), ``control_files`` (name -> bytes of the
    regular files of the control archive) and ``md5sums`` (path -> digest).
    """

    def __init__(self, data: bytes, max_size: int = DEFAULT_MAX_SIZE) -> None:
        self._max_size = max_size
        self._data_tar: bytes | None = None
        self._control_name = self._data_name = ""
        self._control_raw = self._data_raw = b""
        self.format_version = ""
        try:
            members = list(ar.iter_members(data))
        except ar.ArError as exc:
            raise DebError(str(exc)) from exc
        found_ver = False
        for m in members:
            if m.name == "debian-binary" and not found_ver:
                self.format_version = m.data.decode("ascii", "replace").strip()
                found_ver = True
            elif m.name.startswith("control.tar") and not self._control_name:
                self._control_name, self._control_raw = m.name, m.data
            elif m.name.startswith("data.tar") and not self._data_name:
                self._data_name, self._data_raw = m.name, m.data
        if not found_ver or not self._control_name or not self._data_name:
            raise DebError("not a Debian binary package: missing ar members")
        if not _FORMAT_RE.fullmatch(self.format_version):
            raise DebError(f"unsupported debian-binary format {_quote(self.format_version)}")

        self.control_files: dict[str, bytes] = {}
        ctar = _open_tar(decompress(self._control_raw, self._control_name, max_size))
        with ctar, _tar_errors():
            for ti in ctar:
                if ti.isreg():
                    f = ctar.extractfile(ti)
                    assert f is not None
                    self.control_files[_norm(ti.name)] = f.read()
        if "control" not in self.control_files:
            raise DebError("package has no control file")
        paras = list(parse_paragraphs(self.control_files["control"].decode("utf-8", "replace")))
        if not paras:
            raise DebError("empty control file")
        self.control: Paragraph = paras[0]
        self.md5sums: dict[str, str] = {}
        md5sums = self.control_files.get("md5sums", b"").decode("utf-8", "replace")
        # only LF ends a line, like dpkg (str.splitlines also splits at U+2028 etc.)
        for line in md5sums.split("\n"):
            digest, _, path = line.removesuffix("\r").partition(" ")
            if digest and path.strip():
                self.md5sums[path.strip()] = digest

    @classmethod
    def open(cls, path: str | Path, max_size: int = DEFAULT_MAX_SIZE) -> DebPackage:
        data = Path(path).read_bytes()
        log.info("opening %s (%d bytes)", path, len(data))
        return cls(data, max_size)

    def _tar(self) -> tarfile.TarFile:
        if self._data_tar is None:
            self._data_tar = decompress(self._data_raw, self._data_name, self._max_size)
        return _open_tar(self._data_tar)

    def files(self, max_entries: int | None = None) -> list[DebFile]:
        """Entries of the data archive in archive order.

        An mtime that is not finite or outside ``[0, 2**37)`` is listed as 0.
        With *max_entries*, more entries than that raise
        :class:`DebEntryLimitError` (a :class:`DebSizeLimitError`; nothing
        is returned).
        """
        out: list[DebFile] = []
        with self._tar() as tf, _tar_errors():
            for ti in tf:
                if max_entries is not None and len(out) >= max_entries:
                    raise DebEntryLimitError(f"more than {max_entries} entries in the data archive")
                out.append(
                    DebFile(
                        ti.name,
                        ti.size,
                        ti.mode,
                        ti.uid,
                        ti.gid,
                        ti.mtime if _valid_mtime(ti.mtime) else 0,
                        _TYPES.get(ti.type, "o"),
                        ti.linkname,
                    )
                )
        return out

    def read(self, path: str, max_size: int | None = None) -> bytes:
        """Contents of the regular file *path* of the data archive.

        :class:`MissingMemberError` (a :class:`DebError` and ``KeyError``)
        if there is no such regular file. With *max_size*, a member larger
        than that raises :class:`DebMemberTooLargeError` before it is read.
        """
        want = _norm(path)
        with self._tar() as tf, _tar_errors():
            for ti in tf:
                if _norm(ti.name) == want and ti.isreg():
                    if max_size is not None and ti.size > max_size:
                        raise DebMemberTooLargeError(f"{path!r} is larger than {max_size} bytes")
                    f = tf.extractfile(ti)
                    assert f is not None
                    return f.read()
        raise MissingMemberError(f"no regular file {path!r} in the data archive")

    def extract(
        self,
        dest: str | Path,
        members: Iterable[str] | None = None,
        max_size: int | None = None,
        *,
        skip_absolute_symlinks: bool = False,
    ) -> list[tuple[str, str]]:
        """Safely extract the data archive (or selected paths and their children) to *dest*.

        Raises DebError for absolute paths, '..' traversal, links escaping *dest*,
        special files, or when total size exceeds *max_size* (default: the package's limit).
        Nothing is written if any selected member is rejected by the up-front checks.
        *members* ``["."]`` (or ``""``) selects everything, like ``None``.

        Symlinks with an absolute target (common in real packages, e.g.
        ``usr/lib/ssl/certs -> /etc/ssl/certs``) are rejected by default
        (fail closed: such a link points outside *dest*). With
        *skip_absolute_symlinks* they are not created: each is logged as a
        WARNING and the rest is extracted; everything else unsafe is still
        rejected, and so is a hard link to a skipped symlink. Hard links must
        point to an earlier regular member, and member mtimes must be finite
        and in ``[0, 2**37)``; nothing is written otherwise. Returns the
        skipped ``(path, target)`` pairs (``[]`` when nothing was skipped).

        Relative symlink targets are resolved up front (following the
        archive's own symlinks); a link that would point outside *dest*
        is rejected before anything is written.

        *dest* itself must not be a symlink (:class:`DebError`). Only that
        final component is checked: the directories leading to *dest* are
        the caller's choice and symlinks among them are followed (many
        systems have symlinked home directories or ``/tmp``). The check
        cannot stop a local user who can write to a parent directory from
        swapping *dest* afterwards; extract into a private directory.

        Needs tarfile extraction filters (Python 3.11.4 or later).
        """
        if Path(dest).is_symlink():
            raise DebError(f"destination {dest} is a symlink")
        limit = self._max_size if max_size is None else max_size
        log.info("extracting to %s (size limit %d bytes)", dest, limit)
        wanted = None if members is None else [_norm(m) for m in members]
        if wanted is not None and "" in wanted:
            wanted = None  # "." (or "./", "") selects the whole archive
        if not hasattr(tarfile, "data_filter"):
            raise DebError("safe extraction needs Python 3.11.4+ (tarfile filters)")
        with self._tar() as tf, _tar_errors():
            selected = []
            skipped: list[tuple[str, str]] = []
            skipped_names: set[str] = set()
            # every earlier member (selected or not) by normalised name
            seen: dict[str, tarfile.TarInfo] = {}
            # selected symlinks (created by extraction): path -> target
            links: dict[str, str] = {}
            total = 0
            for ti in tf:
                if _unsafe(ti.name):
                    raise DebError(f"unsafe member path: {ti.name!r}")
                n = _norm(ti.name)
                if not _valid_mtime(ti.mtime):
                    raise DebError(f"invalid mtime {ti.mtime!r} of member {ti.name!r}")
                target = seen.get(_norm(ti.linkname)) if ti.islnk() else None
                seen[n] = ti
                if wanted is not None and not any(n == w or n.startswith(w + "/") for w in wanted):
                    continue
                if ti.issym() or ti.islnk():
                    link = ti.linkname
                    if ti.issym() and link.startswith("/") and skip_absolute_symlinks:
                        log.warning("deb: skipping absolute symlink %s -> %s", n, link)
                        skipped.append((n, link))
                        skipped_names.add(n)
                        continue
                    if link.startswith("/") or (ti.islnk() and ".." in PurePosixPath(link).parts):
                        hint = (
                            " (skip_absolute_symlinks, minideb --skip-absolute-symlinks, skips it)"
                            if ti.issym() and link.startswith("/")
                            else ""
                        )
                        raise DebError(f"unsafe link target: {ti.name!r} -> {link!r}{hint}")
                    if ti.islnk():
                        _check_hard_link(n, link, target, skipped_names)
                    else:
                        resolved = posixpath.join(posixpath.dirname(n), link)
                        if not _resolves_inside(resolved, links):
                            raise DebError(
                                f"unsafe link target: {ti.name!r} -> {link!r} leaves the root"
                            )
                        links[n] = link
                total += ti.size
                if total > limit:
                    raise DebError(f"extracted size exceeds {limit} bytes")
                selected.append(ti)
            try:
                log.debug("extracting %d members, %d bytes", len(selected), total)
                tf.extractall(Path(dest), members=selected, filter="data")
            except (tarfile.TarError, KeyError, ValueError, OverflowError) as exc:
                raise DebError(f"unsafe or invalid member: {exc}") from exc
        return skipped
