# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Debian checksum blocks and streaming file verification.

:func:`parse_multiline_hashes` reads the multi-line ``digest size name``
values of ``Files``, ``Checksums-Sha1``/``-Sha256``/``-Sha512`` (``.dsc``,
``Sources``) and of the Release fields ``MD5Sum``, ``SHA1``, ``SHA256`` and
``SHA512``. It only splits and converts; validating digests and names is up
to the caller.

:func:`verify_file` checks one file on disk against a size and any set of
digests in a single read pass. :data:`ALGORITHMS` lists the supported
algorithms with their field names, strongest first. Deciding which
algorithms are strong enough is left to the callers; :func:`is_weak` only
reports the table's classification.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import stat
from collections.abc import Callable, Mapping
from typing import NamedTuple

from minideblib.errors import MinidebError

__all__ = [
    "ALGORITHMS",
    "Algorithm",
    "ChecksumError",
    "HashEntry",
    "is_weak",
    "parse_multiline_hashes",
    "verify_file",
]

log = logging.getLogger(__name__)


class ChecksumError(MinidebError, ValueError):
    """A malformed checksum block, or a file that does not match its checksums."""


class Algorithm(NamedTuple):
    """One supported digest algorithm and the control fields that carry it."""

    #: hashlib name (also the key used by :func:`verify_file`)
    name: str
    #: length of the hexadecimal digest
    hex_length: int
    #: field name in Release files
    release_field: str
    #: field name in ``.dsc`` files and Sources indices
    sources_field: str
    #: too weak to be trusted on its own (MD5, SHA-1)
    weak: bool


#: Supported algorithms, strongest first.
ALGORITHMS: tuple[Algorithm, ...] = (
    Algorithm("sha512", 128, "SHA512", "Checksums-Sha512", False),
    Algorithm("sha256", 64, "SHA256", "Checksums-Sha256", False),
    Algorithm("sha1", 40, "SHA1", "Checksums-Sha1", True),
    Algorithm("md5", 32, "MD5Sum", "Files", True),
)

_BY_NAME: dict[str, Algorithm] = {alg.name: alg for alg in ALGORITHMS}


def is_weak(name: str) -> bool:
    """Whether the algorithm called *name* (hashlib name) is MD5 or SHA-1 grade.

    Raises :class:`ChecksumError` for a name not in :data:`ALGORITHMS`.
    """
    try:
        return _BY_NAME[name].weak
    except KeyError:
        raise ChecksumError(f"unsupported checksum algorithm {name!r}") from None


# --- parsing --------------------------------------------------------------------

#: Line breaks of control data: CRLF, LF or CR (nothing else ends a line).
_LINE_BREAK = re.compile(r"\r\n?|\n")
#: Size column: optional minus sign, then 1 to 20 ASCII digits. The bound keeps
#: absurd values (and CPython's int-conversion limit) out of reach.
_SIZE = re.compile(r"-?[0-9]{1,20}")
#: Longest piece of input quoted in an error message.
_QUOTE_LIMIT = 40


def _quote(text: str) -> str:
    """``repr`` of *text*, shortened to about :data:`_QUOTE_LIMIT` characters."""
    if len(text) <= _QUOTE_LIMIT:
        return repr(text)
    return repr(text[:_QUOTE_LIMIT]) + "..."


class HashEntry(NamedTuple):
    """One row of a checksum block; compares equal to ``(digest, size, name)``."""

    digest: str
    size: int
    name: str


def parse_multiline_hashes(value: str) -> list[HashEntry]:
    """Parse a checksum block into :class:`HashEntry` rows, in input order.

    Each non-blank line must hold exactly three whitespace separated
    columns: digest, size and name. The digest and the name are returned
    as written. The size is an optional ``-`` followed by 1 to 20 ASCII
    digits (leading zeros count); it may be negative, callers decide what
    to do with that. Anything else (``+5``, ``1_000``, non-ASCII digits,
    more than 20 digits) is malformed. The first malformed line raises
    :class:`ChecksumError`, which quotes the start of the line, and
    nothing is returned.
    """
    rows: list[HashEntry] = []
    for line in _LINE_BREAK.split(value):
        columns = line.split()
        if not columns:
            continue
        if len(columns) != 3:
            raise ChecksumError(
                f"checksum line {_quote(line)} has {len(columns)} columns, expected 3"
            )
        digest, size_text, name = columns
        if not _SIZE.fullmatch(size_text):
            raise ChecksumError(f"bad size {_quote(size_text)} in checksum line {_quote(line)}")
        rows.append(HashEntry(digest, int(size_text), name))
    return rows


# --- verification ---------------------------------------------------------------

_CHUNK_SIZE = 64 * 1024
#: extra open(2) flags: never follow a final symlink, never block on a FIFO
_OPEN_FLAGS: int = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _hardened_opener(path: str, flags: int) -> int:
    return os.open(path, flags | _OPEN_FLAGS)


def _label(name: str) -> str:
    """Name of the algorithm in mismatch messages (dpkg calls MD5 ``md5sum``)."""
    return "md5sum" if name == "md5" else name


def verify_file(
    path: str | os.PathLike[str],
    size: int,
    algs: Mapping[str, str],
    error: type[Exception] | None = None,
    *,
    progress: Callable[[], None] | None = None,
) -> None:
    """Check that *path* is a regular file of *size* bytes matching *algs*.

    *algs* maps hashlib names from :data:`ALGORITHMS` to the expected
    lowercase hex digests; every one of them is checked, all in one read
    pass. An empty mapping or an unknown name raises :class:`ChecksumError`
    before touching the file.

    Every failure raises *error* (default :class:`ChecksumError`) built
    from a one-line message naming the file: a missing or unreadable
    file (the :class:`OSError` is chained), anything but a regular file
    (a final symlink is never followed), a wrong size (found before any
    content is read) or the first digest that differs, in *algs* order.

    *progress*, when given, is called before every read; an exception it
    raises (cancellation, a deadline) propagates unchanged.
    """
    fail: type[Exception] = ChecksumError if error is None else error
    shown = os.fspath(path)
    if not algs:
        raise ChecksumError(f"{shown}: no checksum to verify")
    unknown = [name for name in algs if name not in _BY_NAME]
    if unknown:
        raise ChecksumError(f"{shown}: unsupported checksum algorithm {unknown[0]!r}")
    log.debug("checking %s (%d bytes, %s)", shown, size, "/".join(algs))

    try:
        info = os.lstat(path)
    except OSError as exc:
        raise fail(f"{shown}: cannot look up: {exc.strerror or exc}") from exc
    _check_stat(info, shown, size, fail)

    hashers = [(name, hashlib.new(name, usedforsecurity=False)) for name in algs]
    # the only open of the file; open errors are told apart from read errors,
    # so the handle is entered by the ``with`` below rather than here
    try:
        handle = open(path, "rb", buffering=0, opener=_hardened_opener)  # noqa: SIM115
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR):
            raise fail(f"{shown}: not a regular file") from exc
        raise fail(f"{shown}: cannot open: {exc.strerror or exc}") from exc
    with handle:
        try:
            opened = os.fstat(handle.fileno())
        except OSError as exc:
            raise fail(f"{shown}: cannot look up: {exc.strerror or exc}") from exc
        _check_stat(opened, shown, size, fail)
        # O_NONBLOCK only guarded the open against FIFOs; a regular file is
        # read in blocking mode so that no read can return None
        try:
            os.set_blocking(handle.fileno(), True)
        except OSError as exc:
            raise fail(f"{shown}: cannot read: {exc.strerror or exc}") from exc
        total = 0
        # read at most one byte past the expected end, enough to notice growth
        while total <= size:
            if progress is not None:
                progress()
            try:
                chunk = handle.read(min(_CHUNK_SIZE, size + 1 - total))
            except OSError as exc:
                raise fail(f"{shown}: cannot read: {exc.strerror or exc}") from exc
            if not chunk:
                break
            total += len(chunk)
            for _, hasher in hashers:
                hasher.update(chunk)
    if total != size:
        raise fail(f"{shown}: size changed while reading (expected {size} bytes)")

    for name, hasher in hashers:
        actual = hasher.hexdigest()
        expected = algs[name]
        if actual != expected:
            raise fail(f"{shown}: {_label(name)} {actual} != expected {expected}")


def _check_stat(info: os.stat_result, shown: str, size: int, fail: type[Exception]) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise fail(f"{shown}: not a regular file")
    if info.st_size != size:
        raise fail(f"{shown}: size {info.st_size} != expected {size}")
