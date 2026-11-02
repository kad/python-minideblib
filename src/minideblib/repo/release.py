# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""APT ``Release``/``InRelease`` parsing, hash verification and index selection.

The OpenPGP signature of ``InRelease``/``Release`` is **not** verified here:
the armor is stripped and the content trusted as-is. Index integrity is only
as good as the (unauthenticated) hashes listed in ``Release``.
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import unquote

from minideblib.checksums import ALGORITHMS, ChecksumError, is_weak, parse_multiline_hashes
from minideblib.control import Paragraph, decode_text, parse_paragraphs, strip_clearsign
from minideblib.errors import MinidebError
from minideblib.repo.fetch import Fetcher, FetchError, _is_missing_file, redact_url

__all__ = [
    "HASH_ALGOS",
    "HashMismatch",
    "IndexFile",
    "MissingIndexError",
    "Release",
    "ReleaseError",
    "dists_url",
    "fetch_release",
    "icon_size",
    "index_url",
    "index_urls",
    "is_not_found",
    "is_unsafe_path",
    "select_indices",
]

log = logging.getLogger(__name__)

#: Release checksum fields, strongest first, with their hashlib names.
HASH_ALGOS: dict[str, str] = {alg.release_field: alg.name for alg in ALGORITHMS}

#: Compression suffixes in order of preference ("" = uncompressed).
COMPRESSION_ORDER = (".zst", ".xz", ".gz", ".bz2", ".lzma", "")

IndexKind = Literal["binary", "source", "contents", "dep11", "dep11-index", "icons"]

#: kind -> index base paths (without compression suffix), in preference order.
#: ``{component}``, ``{arch}`` and (for ``icons``) ``{size}`` are substituted.
_KIND_PATHS: dict[str, tuple[str, ...]] = {
    "binary": ("{component}/binary-{arch}/Packages",),
    "source": ("{component}/source/Sources",),
    "contents": ("{component}/Contents-{arch}", "Contents-{arch}"),
    "dep11": ("{component}/dep11/Components-{arch}.yml",),
    "dep11-index": ("{component}/dep11/CID-Index-{arch}.json",),
    "icons": ("{component}/dep11/icons-{size}.tar",),
}
#: Kinds keyed by icon size instead of architecture.
_SIZED_KINDS = frozenset({"icons"})
_ICONS_RE = re.compile(r"(?:.*/)?dep11/icons-([^/]+?)\.tar(\.[a-z0-9]+)?")
#: Same for flat repositories (no components; indices next to Release).
_FLAT_KIND_PATHS: dict[str, tuple[str, ...]] = {
    "binary": ("Packages",),
    "source": ("Sources",),
    "contents": ("Contents-{arch}",),
}


class ReleaseError(MinidebError):
    """Malformed Release data or a request it cannot satisfy."""


class MissingIndexError(ReleaseError, KeyError):
    """The requested path is not listed in Release."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class HashMismatch(ReleaseError):
    """Downloaded data does not match the size or hash listed in Release."""


@dataclass
class IndexFile:
    """One file listed in Release: path relative to the suite directory."""

    path: str
    size: int
    hashes: dict[str, str] = field(default_factory=dict)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        log.debug("unparseable Release date %r", value)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


#: percent-encoded separators and NUL (``/``, ``\\``, NUL), any case
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c|00)", re.IGNORECASE)
#: rounds of percent-decoding checked (``%252e`` is two rounds away from ``.``)
_DECODE_ROUNDS = 4
_QUOTE_LIMIT = 40


def _quote(text: str) -> str:
    """``repr`` of at most :data:`_QUOTE_LIMIT` characters of *text*, ``...`` if cut."""
    if len(text) <= _QUOTE_LIMIT:
        return repr(text)
    return f"{text[:_QUOTE_LIMIT]!r}..."


def _plainly_unsafe(path: str) -> bool:
    return "\0" in path or "\\" in path or path.startswith("/") or ".." in path.split("/")


def is_unsafe_path(path: str) -> bool:
    """True for relative paths that could escape their base directory.

    Rejects NUL, backslash, a leading ``/`` and any ``..`` segment, in the
    text as written and after each round of percent-decoding (a ``file:``
    URL is decoded before use, and some servers decode ``%2e``). Encoded
    separators or NUL (``%2f``, ``%5c``, ``%00``, any case) are rejected
    too. Other escapes such as ``%2B`` or ``%3a`` are allowed.
    """
    for _ in range(_DECODE_ROUNDS + 1):
        if _plainly_unsafe(path) or _ENCODED_SEPARATOR.search(path):
            return True
        decoded = unquote(path, errors="surrogateescape")
        if decoded == path:
            return False
        path = decoded
    return True  # still changing after several rounds: refuse rather than guess


class Release:
    """Parsed Release file.

    ``dists_url`` is the URL of the directory holding Release (used by
    :func:`index_url`); it is empty when parsed from text alone.
    """

    def __init__(self, fields: Paragraph, dists_url: str = "") -> None:
        self.fields = fields
        self.dists_url = dists_url.rstrip("/")
        self.files: dict[str, IndexFile] = {}
        for algo in reversed(HASH_ALGOS):  # weakest first; strongest size wins
            value = fields.get(algo)
            if value is None:
                continue
            try:
                entries = parse_multiline_hashes(value)
            except ChecksumError as e:
                raise ReleaseError(f"malformed {algo} block in Release: {e}") from e
            for digest, size, path in entries:
                if size < 0:
                    log.warning("ignoring Release entry %r with negative size", path)
                    continue
                if is_unsafe_path(path):
                    log.warning("ignoring Release entry with unsafe path %r", path)
                    continue
                entry = self.files.setdefault(path, IndexFile(path, size))
                entry.size = size
                entry.hashes[algo] = digest.lower()

    @classmethod
    def parse(cls, text: str | bytes, dists_url: str = "") -> Release:
        """Parse Release or clear-signed InRelease *text* (signature ignored)."""
        if isinstance(text, bytes):
            text = decode_text(text, "Release")
        text = strip_clearsign(text.lstrip("\ufeff"))
        try:
            para = next(parse_paragraphs(text), None)
        except ValueError as e:
            raise ReleaseError(f"malformed Release: {e}") from e
        if para is None:
            raise ReleaseError("empty Release file")
        return cls(para, dists_url)

    @property
    def codename(self) -> str | None:
        return self.fields.get("Codename")

    @property
    def suite_field(self) -> str | None:
        return self.fields.get("Suite")

    @property
    def components(self) -> list[str]:
        return self.fields.get("Components", "").split()

    @property
    def architectures(self) -> list[str]:
        return self.fields.get("Architectures", "").split()

    @property
    def acquire_by_hash(self) -> bool:
        return self.fields.get("Acquire-By-Hash", "").strip().lower() == "yes"

    @property
    def date(self) -> datetime | None:
        return _parse_date(self.fields.get("Date"))

    @property
    def valid_until(self) -> datetime | None:
        return _parse_date(self.fields.get("Valid-Until"))

    def is_expired(self, now: datetime | None = None) -> bool:
        vu = self.valid_until
        return vu is not None and vu < (now or datetime.now(UTC))

    def _entry(self, path: str) -> IndexFile:
        try:
            return self.files[path]
        except KeyError:
            raise MissingIndexError(f"{path} is not listed in Release") from None

    def best_hash(self, path: str) -> tuple[str, str]:
        """Return ``(algo, hexdigest)`` of the strongest hash listed for *path*."""
        entry = self._entry(path)
        for algo in HASH_ALGOS:
            if algo in entry.hashes:
                return algo, entry.hashes[algo]
        raise ReleaseError(f"no hash listed for {path}")  # pragma: no cover

    def verify(self, path: str, data: bytes) -> None:
        """Check *data* against the size and strongest hash listed for *path*.

        Fails closed: :class:`HashMismatch` also when only MD5 and/or SHA-1
        are listed for *path* (SHA256 or stronger is required, as in apt).
        """
        entry = self._entry(path)
        algo, expected = self.best_hash(path)
        if is_weak(HASH_ALGOS[algo]):
            raise HashMismatch(f"{path}: no SHA256 or stronger hash listed in Release")
        if len(data) != entry.size:
            raise HashMismatch(f"{path}: size {len(data)} != expected {entry.size}")
        actual = hashlib.new(HASH_ALGOS[algo], data, usedforsecurity=False).hexdigest()
        log.debug("%s: %s expected %s... actual %s...", path, algo, expected[:12], actual[:12])
        if actual != expected:
            raise HashMismatch(f"{path}: {algo} {actual} != expected {expected}")


def _is_flat(suite: str) -> bool:
    return suite.endswith("/")


def dists_url(base_url: str, suite: str) -> str:
    """URL of the directory holding Release for *suite* (no trailing slash).

    A suite ending in ``/`` (e.g. ``./``) denotes a flat repository whose
    files live at ``base_url/<suite>``; otherwise ``base_url/dists/<suite>``.
    :class:`ReleaseError` for an empty suite or one that could leave
    *base_url* (see :func:`is_unsafe_path`; ``/`` alone means the base itself).
    """
    if suite != "/" and (not suite or is_unsafe_path(suite)):
        raise ReleaseError(f"unsafe suite name {_quote(suite)}")
    base = base_url.rstrip("/")
    if _is_flat(suite):
        sub = posixpath.normpath(suite).lstrip("/")
        return base if sub in (".", "") else f"{base}/{sub}"
    return f"{base}/dists/{suite}"


def is_not_found(err: FetchError) -> bool:
    """Whether *err* means "not there": HTTP 404 or a missing ``file://`` file."""
    return err.status == 404 or (err.status is None and _is_missing_file(err))


def fetch_release(
    fetcher: Fetcher, base_url: str, suite: str, verify_expiry: bool = True
) -> Release:
    """Fetch and parse ``InRelease`` (falling back to ``Release`` on 404).

    The OpenPGP signature is NOT verified. Errors other than "not found" on
    ``InRelease`` propagate. An expired ``Valid-Until`` only logs a warning
    (when *verify_expiry* is true).
    """
    url = dists_url(base_url, suite)
    log.info("fetching Release for suite %s from %s", suite, redact_url(url))
    try:
        data = fetcher.get(f"{url}/InRelease")
        log.info("using InRelease")
    except FetchError as e:
        if not is_not_found(e):
            raise
        log.info("InRelease not found, using Release")
        data = fetcher.get(f"{url}/Release")
    rel = Release.parse(data, url)
    log.debug("Release: %d files listed, Acquire-By-Hash=%s", len(rel.files), rel.acquire_by_hash)
    if verify_expiry and rel.is_expired():
        log.warning("Release for %s expired on %s (Valid-Until)", redact_url(url), rel.valid_until)
    return rel


def _plain_url(rel: Release, path: str) -> str:
    if not rel.dists_url:
        raise ReleaseError("Release has no dists_url; cannot build index URLs")
    if is_unsafe_path(path):
        raise ReleaseError(f"unsafe index path {path!r}")
    return f"{rel.dists_url}/{path}"


def _by_hash_path(rel: Release, path: str, algo: str) -> str:
    d = posixpath.dirname(path)
    digest = rel._entry(path).hashes[algo]
    return f"{d}/by-hash/{algo}/{digest}" if d else f"by-hash/{algo}/{digest}"


def index_url(rel: Release, index: IndexFile | str) -> str:
    """URL for *index*; uses ``<dir>/by-hash/<ALGO>/<hash>`` with Acquire-By-Hash."""
    path = index if isinstance(index, str) else index.path
    if rel.acquire_by_hash:
        algo, _ = rel.best_hash(path)
        return _plain_url(rel, _by_hash_path(rel, path, algo))
    return _plain_url(rel, path)


def index_urls(rel: Release, index: IndexFile | str) -> list[str]:
    """Candidate URLs for *index*, best first.

    With Acquire-By-Hash: by-hash URLs for each listed algorithm (strongest
    first), then the plain path. Otherwise just the plain path.
    """
    path = index if isinstance(index, str) else index.path
    urls = []
    if rel.acquire_by_hash:
        entry = rel._entry(path)
        urls = [
            _plain_url(rel, _by_hash_path(rel, path, algo))
            for algo in HASH_ALGOS
            if algo in entry.hashes
        ]
    urls.append(_plain_url(rel, path))
    return urls


def icon_size(path: str) -> str | None:
    """Icon size (e.g. ``48x48``, ``128x128@2``) of a ``dep11/icons-<size>.tar*`` path."""
    m = _ICONS_RE.fullmatch(path)
    if m is None or (m.group(2) or "") not in COMPRESSION_ORDER:
        return None
    return m.group(1)


def _size_key(size: str) -> tuple:
    """Natural order: ``48x48`` < ``64x64`` < ``128x128`` < ``128x128@2``."""
    return tuple(int(n) for n in re.findall(r"\d+", size)), size


def _listed_sizes(rel: Release, component: str) -> list[str]:
    prefix = f"{component}/dep11/icons-"
    sizes = {icon_size(p) for p in rel.files if p.startswith(prefix)}
    return sorted((s for s in sizes if s), key=_size_key)


def select_indices(
    rel: Release, component: str, arch: str, kind: IndexKind, *, size: str | None = None
) -> list[IndexFile]:
    """Index files of *kind* listed in Release, most preferred first.

    Compressed forms are ordered zst, xz, gz, bz2, lzma, uncompressed. An
    empty *component* selects the flat-repository layout, which has no
    DEP-11 kinds (they give ``[]``). Returns ``[]`` when nothing matches
    (e.g. unknown architecture); ``ValueError`` for an unknown *kind*.

    Kinds: ``binary``, ``source``, ``contents``, ``dep11``
    (``Components-<arch>.yml``), ``dep11-index`` (``CID-Index-<arch>.json``)
    and ``icons`` (``icons-<size>.tar``). ``icons`` ignores *arch* and is
    keyed by *size* instead: one size, or ``None`` for every size listed,
    in natural size order and compression order within a size.
    """
    if is_unsafe_path(component) or is_unsafe_path(arch):
        raise ReleaseError(f"unsafe component or architecture: {component!r}, {arch!r}")
    if size is not None and (is_unsafe_path(size) or "/" in size):
        raise ReleaseError(f"unsafe icon size: {size!r}")
    if kind not in _KIND_PATHS:
        raise ValueError(f"unknown index kind {kind!r}")
    table = _KIND_PATHS if component else _FLAT_KIND_PATHS
    if kind not in table:
        return []  # no flat layout for this kind
    if kind in _SIZED_KINDS:
        sizes = [size] if size is not None else _listed_sizes(rel, component)
    else:
        sizes = [""]
    out = []
    for sz in sizes:
        for pattern in table[kind]:
            stem = pattern.format(component=component, arch=arch, size=sz)
            for suffix in COMPRESSION_ORDER:
                entry = rel.files.get(stem + suffix)
                if entry is not None:
                    out.append(entry)
    return out
