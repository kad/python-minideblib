# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""High-level APT repository client.

:class:`RepoClient` answers questions about one or more repositories
(configured from :mod:`minideblib.sources` entries or a bare URL): suites,
components, packages, sources, file lookup via ``Contents``, search,
versions and DEP-11/AppStream metadata. Index data is downloaded on demand, hash-verified against
``Release`` (fail closed; see ``verify``) and cached per client instance.

The OpenPGP signature of ``Release``/``InRelease`` is not verified.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import posixpath
import queue
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any, Literal, overload
from urllib.parse import urlsplit
from urllib.request import url2pathname

from minideblib.checksums import (
    ALGORITHMS,
    ChecksumError,
    HashEntry,
    is_weak,
    parse_multiline_hashes,
)
from minideblib.control import Paragraph
from minideblib.dsc import Dsc
from minideblib.errors import MinidebError
from minideblib.repo import dep11
from minideblib.repo.fetch import (
    CancelToken,
    FetchCancelled,
    Fetcher,
    FetchError,
    FetchTimeout,
    redact_url,
)
from minideblib.repo.index import fetch_index, parse_contents, parse_packages, parse_sources
from minideblib.repo.release import (
    HashMismatch,
    IndexFile,
    IndexKind,
    Release,
    dists_url,
    fetch_release,
    is_not_found,
    is_unsafe_path,
    select_indices,
)
from minideblib.sources import SourceEntry
from minideblib.version import Version

__all__ = [
    "CANDIDATE_SUITES",
    "DEBIAN_SUITES",
    "MAX_PROBE_REQUESTS",
    "UBUNTU_SUITES",
    "DownloadPlan",
    "FileSpec",
    "PackageGroup",
    "PackageRecord",
    "Probe",
    "RepoClient",
    "RepoError",
    "SourceRecord",
]

log = logging.getLogger(__name__)

# Default probe candidates. The lists are short on purpose (a few requests
# per `suites` call); any other suite, such as an older codename,
# `oldstable`, `sid` or a `-proposed` pocket, is probed when named
# explicitly (`from_url(suites=...)`, `minideb suites --suite NAME`).
# Reviewed 2026-11 against the official Ubuntu release list: latest release
# stonking (26.10), LTS releases resolute (26.04) and noble (24.04). When a
# new Ubuntu release ships, update UBUNTU_SUITES.

#: Debian suites probed with ``probe="debian"``: the symbolic names of the
#: current stable release (with ``-updates`` and ``-backports``) and testing.
DEBIAN_SUITES: tuple[str, ...] = ("stable", "stable-updates", "stable-backports", "testing")

#: Ubuntu suites probed with ``probe="ubuntu"``: the latest release
#: (``stonking``, 26.10) and the current and previous LTS releases
#: (``resolute`` 26.04, ``noble`` 24.04), each with the ``-updates``,
#: ``-security`` and ``-backports`` pockets.
UBUNTU_SUITES: tuple[str, ...] = tuple(
    f"{c}{p}"
    for c in ("stonking", "resolute", "noble")
    for p in ("", "-updates", "-security", "-backports")
)

#: Suites probed by :meth:`RepoClient.suites` for clients made with
#: :meth:`RepoClient.from_url` (``probe="all"``, the default): the union of
#: :data:`DEBIAN_SUITES` and :data:`UBUNTU_SUITES`, in that order.
CANDIDATE_SUITES: tuple[str, ...] = DEBIAN_SUITES + UBUNTU_SUITES

#: Candidate sets for the ``probe`` argument of :meth:`RepoClient.from_url`.
Probe = Literal["all", "debian", "ubuntu", "none"]
_PROBE_SETS: dict[str, tuple[str, ...]] = {
    "all": CANDIDATE_SUITES,
    "debian": DEBIAN_SUITES,
    "ubuntu": UBUNTU_SUITES,
    "none": (),
}

#: Budget of :meth:`Fetcher.exists` calls for probing suites. Every suite
#: costs at most two calls (``InRelease``, then ``Release``), so at most
#: ``MAX_PROBE_REQUESTS // 2`` suites are probed: the explicit ``suites=``
#: of :meth:`RepoClient.from_url` first (always probed, even beyond the
#: budget), then candidates in order. A single ``exists`` call may itself
#: retry or fall back from HEAD to GET when the server refuses HEAD.
MAX_PROBE_REQUESTS = 150

#: Worker threads used for probing.
PROBE_WORKERS = 8

#: HTTP statuses meaning "this suite is not there" when probing.
_ABSENT_STATUS = (401, 403, 404, 410)

_UPDATES_PREFIX = "updates/"

#: Newest entries kept in :attr:`RepoClient.problems`.
MAX_PROBLEMS = 1000

#: Default of the ``cache_budget_bytes`` argument of :class:`RepoClient`.
DEFAULT_CACHE_BUDGET = 512 * 1024**2

#: Estimated memory of a parsed index per byte of its decompressed text, by
#: kind. Measured: a parsed 100k-stanza Packages index retains about 7.35
#: times its text in RSS (tracemalloc: 6.6 retained, 8.9 at the peak), a
#: Contents dict about 3.8; Sources and DEP-11 are taken as Packages.
_PARSED_COST_FACTOR: dict[str, int] = {"binary": 8, "source": 8, "dep11": 8, "contents": 4}

#: ``Packages`` hash fields, strongest first, with their hashlib names.
_PACKAGES_HASHES = (
    ("SHA512", "sha512", 128),
    ("SHA256", "sha256", 64),
    ("SHA1", "sha1", 40),
    ("MD5sum", "md5", 32),
)


class RepoError(MinidebError, ValueError):
    """Repository data the client cannot use, or a suite it does not know.

    Raised for an unsafe or missing path in an index record, a source
    package without a ``.dsc`` file, and a suite that is not configured.
    """


def _short_description(para: Paragraph) -> str:
    return para.get("Description", "").split("\n", 1)[0].strip()


def _safe_join(base: str, *parts: str) -> str:
    """Join relative *parts* under *base*, rejecting paths that could escape it.

    Empty parts (after dropping leading ``./``) are skipped.
    """
    out = []
    for part in parts:
        name = part
        while name.startswith("./"):
            name = name[2:]
        if is_unsafe_path(name):
            raise RepoError(f"unsafe repository path {part!r}")
        if name.rstrip("/"):
            out.append(name.rstrip("/"))
    return "/".join([base.rstrip("/"), *out])


def _merge_scope(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    """Union of two component or architecture lists; ``()`` (unrestricted) wins."""
    if not a or not b:
        return ()
    return tuple(dict.fromkeys(a + b))


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _size_or_none(value: str | int | None) -> int | None:
    """A non-negative size, else None."""
    n = _int_or_none(value) if isinstance(value, str) or value is None else value
    return n if n is not None and n >= 0 else None


def _is_hex(text: str, length: int) -> bool:
    return len(text) == length and all(c in "0123456789abcdef" for c in text)


@dataclass(frozen=True)
class FileSpec:
    """One file to download: its name, URL, size and hashes from the repository metadata.

    *name* is the basename taken from ``Filename`` or the ``Sources`` file
    list; *hashes* maps the hashlib name of the strongest algorithm listed
    for the file to its lowercase hex digest (empty when none is listed;
    possibly a weak one only, which callers requiring SHA256 or stronger
    must refuse). *size* is None when not listed.
    """

    name: str
    url: str
    size: int | None
    hashes: Mapping[str, str]


@dataclass(frozen=True)
class DownloadPlan:
    """The files of one binary package (one ``.deb``) or of one source package.

    *architecture* is the ``Architecture`` of the binary package (``all``
    for architecture-independent ones) and None for sources. Source plans
    list the ``.dsc`` first.
    """

    kind: Literal["binary", "source"]
    name: str
    version: str
    suite: str
    component: str
    architecture: str | None
    files: tuple[FileSpec, ...]


@dataclass(frozen=True)
class PackageGroup:
    """The records of one package version in one suite and component, over architectures.

    *architectures* are the distinct ``Architecture`` values of the
    variants, sorted (``("all",)`` for an architecture-independent
    package); *variants* holds one record per distinct file (an
    ``Architecture: all`` record listed identically in several per-arch
    indices appears once), sorted by index architecture. *multi_arch* is
    the ``Multi-Arch`` field of the first variant that has one.
    """

    name: str
    version: str
    component: str
    suite: str
    architectures: tuple[str, ...]
    multi_arch: str | None
    variants: tuple[PackageRecord, ...]


@dataclass(frozen=True)
class PackageRecord:
    """One stanza of a ``Packages`` index and where it came from.

    ``base_url`` is the URL that ``Filename`` is relative to (the sources
    entry URI, or the flat-repository directory). :attr:`url` raises
    :class:`RepoError` (a ``ValueError``) for a missing or unsafe
    ``Filename`` (``..`` segment, leading ``/``, backslash, NUL);
    :meth:`RepoClient.packages` skips
    records with an unsafe (but not a missing) ``Filename``.
    """

    paragraph: Paragraph = field(hash=False)
    suite: str
    component: str
    arch: str
    base_url: str = field(default="", repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.paragraph.get("Package", "")

    @cached_property
    def version(self) -> Version:
        """``Version`` (default ``0``), parsed on first use and then kept."""
        return Version(self.paragraph.get("Version", "0"))

    @property
    def source(self) -> str:
        """Source package name (``Source`` without a version, else ``Package``)."""
        src = self.paragraph.get("Source", "").split("(", 1)[0].strip()
        return src or self.name

    @property
    def filename(self) -> str:
        return self.paragraph.get("Filename", "")

    @property
    def url(self) -> str:
        if not self.filename:
            raise RepoError(f"package {self.name!r} has no Filename")
        return _safe_join(self.base_url, self.filename)

    @property
    def size(self) -> int | None:
        return _int_or_none(self.paragraph.get("Size"))

    @property
    def sha256(self) -> str | None:
        return self.paragraph.get("SHA256")

    @property
    def description(self) -> str:
        """First line of ``Description``."""
        return _short_description(self.paragraph)

    @property
    def architecture(self) -> str:
        """The ``Architecture`` field (``all`` included), else the index architecture."""
        return self.paragraph.get("Architecture", "") or self.arch

    @property
    def strongest_hash(self) -> dict[str, str]:
        """``{hashlib name: lowercase hex}`` of the strongest well-formed hash field, or ``{}``."""
        for key, algo, length in _PACKAGES_HASHES:
            digest = self.paragraph.get(key, "").strip().lower()
            if _is_hex(digest, length):
                return {algo: digest}
        return {}


@dataclass(frozen=True)
class SourceRecord:
    """One stanza of a ``Sources`` index; ``arch`` is always ``"source"``.

    ``base_url`` is the URL that ``Directory`` is relative to.
    :attr:`file_urls` raises :class:`RepoError` (a ``ValueError``) for an
    unsafe ``Directory`` or file name; :meth:`RepoClient.sources` never returns such records.
    """

    paragraph: Paragraph = field(hash=False)
    suite: str
    component: str
    arch: str = "source"
    base_url: str = field(default="", repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.paragraph.get("Package", "")

    @cached_property
    def version(self) -> Version:
        """``Version`` (default ``0``), parsed on first use and then kept."""
        return Version(self.paragraph.get("Version", "0"))

    @property
    def directory(self) -> str:
        return self.paragraph.get("Directory", "")

    @property
    def binaries(self) -> list[str]:
        """Binary package names built from this source."""
        return self.paragraph.get_list("Binary")

    @property
    def files(self) -> list[HashEntry]:
        """``(hash, size, name)`` of the listed files, each from the strongest field listing it.

        Fields are tried strongest first: ``Checksums-Sha512``, ``-Sha256``,
        ``-Sha1``, then ``Files`` (MD5), the order :meth:`RepoClient.fetch_dsc`
        verifies in. Names keep the order of the first field listing them; a
        malformed field is skipped with a warning. This is a listing only: a
        file listed with a weak hash only still fails verification.
        """
        return [row for _, row in self._listed().values()]

    def _listed(self) -> dict[str, tuple[str, HashEntry]]:
        """File name -> ``(hashlib name, row)`` from the strongest field listing it."""
        merged: dict[str, tuple[str, HashEntry]] = {}
        for key, algo in _SOURCES_HASHES:
            if key not in self.paragraph:
                continue
            try:
                rows = parse_multiline_hashes(self.paragraph[key])
            except ChecksumError:
                log.warning("malformed %s in source %s", key, self.name)
                continue
            for row in rows:
                merged.setdefault(row.name, (algo, row))
        return merged

    @property
    def file_urls(self) -> list[str]:
        """Download URLs of :attr:`files`."""
        return [_safe_join(self.base_url, self.directory, name) for _, _, name in self.files]

    @property
    def dsc_name(self) -> str | None:
        """Name of the ``.dsc`` file listed in :attr:`files`, or None."""
        return next((name for _, _, name in self.files if name.endswith(".dsc")), None)

    @property
    def dsc_url(self) -> str | None:
        """Download URL of the ``.dsc`` file, or None if none is listed.

        Raises :class:`RepoError` for an unsafe ``Directory`` or file name.
        """
        name = self.dsc_name
        return None if name is None else _safe_join(self.base_url, self.directory, name)


class RepoClient:
    """Query APT repositories described by *sources* entries.

    Only enabled entries are used. ``verify=False`` skips index hash checks;
    otherwise :class:`~minideblib.repo.release.HashMismatch` propagates.

    *components* and *architectures* scope every query (and so every
    download) to those names: for an entry that configures components or
    architectures the effective set is the intersection, an empty filter
    means no restriction. An explicit *component*/*arch* argument of a
    query method wins over the filter. :meth:`components` reports the
    scoped components; :meth:`architectures` always reports Release.
    Without a scope, queries read every component and architecture of a
    suite, which for a large archive such as Debian ``main`` means tens of
    megabytes per suite.

    Indices that are listed but missing on the server (404) are skipped:
    a warning is logged and a message appended to :attr:`problems`.
    Suites served by several entries (e.g. mirrors or different URIs) are
    queried through every such entry. Records with unsafe paths are
    skipped with a problem.

    :attr:`problems` (read-only for callers) keeps the newest
    :data:`MAX_PROBLEMS` messages;
    :attr:`problems_dropped` counts the older ones dropped, and
    :meth:`problems_mark`/:meth:`problems_since` give the messages added
    by one call.

    Notes:

    - The caches and :attr:`problems` are guarded by a lock, so a client
      may be shared between threads (indices are downloaded outside it).
    - Whole parsed indices (and Release files, including "not found"
      results) are cached for the lifetime of the client; call
      :meth:`clear_cache` to refetch. Parsed indices are bounded by
      *cache_budget_bytes* (default 512 MiB): their memory is estimated as
      8 times the decompressed index size (4 for Contents), and when the estimate
      exceeds the budget the least recently used parsed indices are
      dropped (never the one just loaded, so a single index larger than
      the budget is still kept) and downloaded again when needed.
    - ``Architecture: all`` packages appear once per architecture index
      they are listed in, tagged with that index's architecture. In flat
      repositories (one ``Packages`` file) records are tagged with their
      own ``Architecture`` field instead and appear once.
    - Entries sharing a URI and suite are merged: every listed component is
      queried with every architecture (cross product), and only the first
      entry's ``signed_by``/``trusted``/options are kept.
    - :meth:`search`: an empty pattern matches everything; matching uses
      ``str.lower``, not ``casefold``.
    - :meth:`suites` of a :meth:`from_url` client probes candidate suites
      (see *probe* there) and raises :class:`FetchError` when the
      repository is unreachable.
    """

    def __init__(
        self,
        sources: list[SourceEntry] | None = None,
        fetcher: Fetcher | None = None,
        verify: bool = True,
        components: Iterable[str] = (),
        architectures: Iterable[str] = (),
        *,
        cache_budget_bytes: int = DEFAULT_CACHE_BUDGET,
    ) -> None:
        self.entries = [e for e in (sources or []) if e.enabled]
        self.fetcher = fetcher or Fetcher()
        self.verify = verify
        #: client-wide scope (empty: no restriction); see the class docstring
        self.scope_components: tuple[str, ...] = tuple(dict.fromkeys(components))
        self.scope_architectures: tuple[str, ...] = tuple(dict.fromkeys(architectures))
        self.problems: list[str] = []
        #: messages dropped from the front of :attr:`problems` (see MAX_PROBLEMS)
        self.problems_dropped = 0
        self._problems_total = 0
        #: estimated memory budget of the parsed indices (see the class notes)
        self.cache_budget_bytes = cache_budget_bytes
        #: base URL for suites not in ``entries`` (set by :meth:`from_url`)
        self._url: str | None = None
        self._explicit: tuple[str, ...] = ()
        self._candidates: tuple[str, ...] = CANDIDATE_SUITES
        self._suites: list[str] | None = None
        self._releases: dict[tuple[str, str], Release | FetchError] = {}
        self._noted: set[tuple[str, str]] = set()
        self._noted_missing: set[tuple[str, str]] = set()
        self._noted_scope: set[tuple[str, str, str]] = set()
        self._indices: dict[tuple[str, str, str, str, str], tuple[str, Any] | None] = {}
        self._parsed: dict[tuple[str, str, str], Any] = {}
        # estimated cost of each parsed index, in least recently used order
        self._cost: dict[tuple[str, str, str], int] = {}
        # guards the caches and the problem list against concurrent use
        self._lock = threading.RLock()

    @classmethod
    def from_url(
        cls,
        url: str,
        suites: Iterable[str] = (),
        probe: Probe = "all",
        components: Iterable[str] = (),
        architectures: Iterable[str] = (),
        **kw: Any,
    ) -> RepoClient:
        """Client for the repository at *url*.

        Any suite name may be queried. :meth:`suites` probes *suites* and
        the candidates chosen by *probe* for an ``InRelease`` or ``Release``
        file, because many archives do not allow directory listings:
        ``"all"`` (:data:`CANDIDATE_SUITES`), ``"debian"``
        (:data:`DEBIAN_SUITES`), ``"ubuntu"`` (:data:`UBUNTU_SUITES`) or
        ``"none"`` (only *suites*). Each probed suite costs up to two small
        requests. *components* and *architectures* scope the client (see
        :class:`RepoClient`) and are recorded in the entries it builds.
        Other keyword arguments go to the constructor. ``ValueError`` for an
        unknown *probe*.
        """
        if probe not in _PROBE_SETS:
            raise ValueError(f"unknown probe set {probe!r}; use one of {', '.join(_PROBE_SETS)}")
        suites = list(dict.fromkeys(suites))
        comps, arches = tuple(dict.fromkeys(components)), tuple(dict.fromkeys(architectures))
        entries = [SourceEntry("deb", url, s, comps, architectures=arches) for s in suites]
        client = cls(entries, components=comps, architectures=arches, **kw)
        client._url = url
        client._explicit = tuple(suites)
        client._candidates = _PROBE_SETS[probe]
        return client

    # -- bookkeeping -------------------------------------------------------

    def clear_cache(self) -> None:
        """Forget downloaded Release/index data and probed suites."""
        with self._lock:
            self._releases.clear()
            self._indices.clear()
            self._parsed.clear()
            self._cost.clear()
            self._noted.clear()
            self._noted_missing.clear()
            self._noted_scope.clear()
            self._suites = None

    def _problem(self, msg: str) -> None:
        log.warning("%s", msg, extra={"problem": True})
        with self._lock:
            self.problems.append(msg)
            self._problems_total += 1
            excess = len(self.problems) - MAX_PROBLEMS
            if excess > 0:
                del self.problems[:excess]
                self.problems_dropped += excess

    def _problem_once(self, key: tuple[str, str, str], msg: str) -> None:
        """:meth:`_problem` unless *key* was noted already (until :meth:`clear_cache`)."""
        with self._lock:
            if key in self._noted_scope:
                return
            self._noted_scope.add(key)
            self._problem(msg)

    def problems_mark(self) -> int:
        """A mark for :meth:`problems_since`: the number of problems recorded so far."""
        with self._lock:
            return self._problems_total

    def problems_since(self, mark: int) -> list[str]:
        """The problems recorded after *mark* (from :meth:`problems_mark`) still kept."""
        with self._lock:
            first_kept = self._problems_total - len(self.problems)
            return self.problems[max(0, mark - first_kept) :]

    @property
    def cache_bytes(self) -> int:
        """Estimated memory of the cached parsed indices (see the class notes)."""
        with self._lock:
            return sum(self._cost.values())

    def _touch(self, pkey: tuple[str, str, str]) -> None:
        """Mark a parsed index as most recently used (the caller holds :attr:`_lock`)."""
        if pkey in self._cost:
            self._cost[pkey] = self._cost.pop(pkey)

    def _store_parsed(
        self, pkey: tuple[str, str, str], parsed: Any, size: int, kind: IndexKind
    ) -> None:
        """Cache *parsed* (from *size* decompressed bytes), then evict over budget.

        The caller holds :attr:`_lock`.
        """
        self._parsed[pkey] = parsed
        self._cost.pop(pkey, None)
        self._cost[pkey] = size * _PARSED_COST_FACTOR.get(kind, 8)
        total = sum(self._cost.values())
        for old in list(self._cost):
            if total <= self.cache_budget_bytes:
                break
            if old == pkey:
                continue
            total -= self._cost.pop(old)
            del self._parsed[old]
            # query results pointing at the dropped data are forgotten too
            for key in [k for k, v in self._indices.items() if v and (*k[:2], v[0]) == old]:
                del self._indices[key]
            log.debug("cache budget: dropped parsed %s", old[2])

    def _entries(self, suite: str, types: tuple[str, ...] = ("deb",)) -> list[SourceEntry]:
        """Entries for *suite*, preferring *types*; one per URI.

        Entries sharing a URI are merged: components and architectures are
        combined (in first-seen order), except that an unrestricted entry
        (no components, or no architectures) keeps the merged entry
        unrestricted in that respect. The first entry's options are kept.
        """
        matching = [e for e in self.entries if e.suite == suite]
        preferred = [e for e in matching if e.type in types] or matching
        if not preferred and self._url is not None:
            arches = self.scope_architectures
            preferred = [
                SourceEntry("deb", self._url, suite, self.scope_components, architectures=arches)
            ]
        if not preferred:
            raise RepoError(f"suite {suite!r} is not configured")
        merged: dict[str, SourceEntry] = {}
        for e in preferred:
            prev = merged.get(e.uri)
            if prev is None:
                merged[e.uri] = e
                continue
            merged[e.uri] = replace(
                prev,
                components=_merge_scope(prev.components, e.components),
                architectures=_merge_scope(prev.architectures, e.architectures),
            )
        return list(merged.values())

    def _release(self, entry: SourceEntry) -> Release:
        """Release of *entry*, cached; a 404 is cached too (and raised).

        The raised error is not recorded in :attr:`problems`; callers that
        skip it note it with :meth:`_note_missing`.
        """
        key = (entry.uri, entry.suite)
        if key in self._releases:
            log.debug("Release cache hit: %s %s", entry.suite, redact_url(entry.uri))
        else:
            try:
                self._releases[key] = fetch_release(self.fetcher, entry.uri, entry.suite)
            except FetchError as e:
                if not is_not_found(e):
                    raise
                self._releases[key] = e
        cached = self._releases[key]
        if isinstance(cached, FetchError):
            raise cached
        return cached

    def _note_missing(self, entry: SourceEntry, err: FetchError) -> None:
        """Record once in :attr:`problems` that *entry* has no Release."""
        key = (entry.uri, entry.suite)
        if key not in self._noted_missing:
            self._noted_missing.add(key)
            self._problem(f"{entry.suite}: Release not found at {redact_url(entry.uri)}: {err}")

    def _releases_for(
        self, suite: str, types: tuple[str, ...] = ("deb",)
    ) -> list[tuple[SourceEntry, Release]]:
        """``(entry, Release)`` for every entry of *suite*.

        An entry whose Release is missing (404) is skipped, and noted once
        in :attr:`problems`, when another entry serves the suite; if none
        has a Release, the last error propagates (and is not noted). Other
        errors always propagate.
        """
        out = []
        missing: list[tuple[SourceEntry, FetchError]] = []
        for entry in self._entries(suite, types):
            try:
                out.append((entry, self._release(entry)))
            except FetchError as e:
                if not is_not_found(e):
                    raise
                missing.append((entry, e))
        if not out:
            raise missing[-1][1]
        for entry, e in missing:
            self._note_missing(entry, e)
        return out

    # -- suites, components, architectures --------------------------------

    def suites(self) -> list[str]:
        """Known suites.

        With a sources list: the suites of ``deb`` entries, deduplicated in
        order (nothing is downloaded). With :meth:`from_url`: those of the
        explicit and candidate suites whose ``InRelease`` or ``Release``
        exists, probed in parallel with at most :data:`MAX_PROBE_REQUESTS`
        ``exists`` calls (no index is downloaded). Cached.

        Probing tells "nothing found" from "unreachable". A suite answered
        with HTTP 401/403/404/410 (or a missing ``file://`` file) is absent,
        and absent everywhere gives ``[]``. The repository is unreachable
        when *every* probe failed with a network-level :class:`FetchError`
        (no HTTP status, e.g. connection refused or DNS failure) or with an
        HTTP status of 500 or more after the Fetcher's retries, or when a
        ``file://`` base directory does not exist; then a :class:`FetchError`
        is raised (and nothing is cached or recorded in :attr:`problems`).
        As soon as one probe got another answer, failed probes are only
        recorded in :attr:`problems`. With no suite to probe (``probe="none"``
        and no explicit suites) the result is ``[]`` without any request.
        :meth:`Fetcher.cancel` during probing raises
        :class:`~minideblib.repo.fetch.FetchCancelled`; nothing is cached.
        The token and deadline of an enclosing :meth:`Fetcher.scope` apply
        to the probes too (:class:`~minideblib.repo.fetch.FetchCancelled`
        or :class:`~minideblib.repo.fetch.FetchTimeout`).
        """
        if self._suites is None:
            if self._url is None:
                found = [e.suite for e in self.entries if e.type == "deb"]
            else:
                found = self._probe_suites(self._url)
            self._suites = list(dict.fromkeys(found))
        return list(self._suites)

    def _probe_suites(self, url: str) -> list[str]:
        """Existing suites among the explicit ones and the candidates.

        The work list is fixed before probing starts (see
        :data:`MAX_PROBE_REQUESTS`), probes run on :data:`PROBE_WORKERS`
        threads and only return result tuples; results keep the work-list
        order and are logged and recorded here, in the calling thread.
        """
        explicit = list(self._explicit)
        candidates = [c for c in self._candidates if c not in explicit]
        room = max(0, MAX_PROBE_REQUESTS // 2 - len(explicit))
        if room < len(candidates):
            self._problem(
                f"{len(candidates) - room} candidate suites not probed "
                f"(MAX_PROBE_REQUESTS={MAX_PROBE_REQUESTS})"
            )
        work = explicit + candidates[:room]
        if not work:
            log.debug("no suites to probe at %s", redact_url(url))
            return []
        if _missing_local_dir(url):
            raise FetchError(f"repository unreachable: {redact_url(url)} does not exist", url)
        log.info("probing %d candidate suites on %s", len(work), redact_url(url))
        results = self._run_probes(url, work)
        errors = [err for _, _, err in results if err is not None]
        if len(errors) == len(results) and all(_unreachable(e) for e in errors):
            first = errors[0]
            raise FetchError(
                f"repository unreachable: all {len(results)} suite probes on "
                f"{redact_url(url)} failed, first: {first}",
                first.url,
                first.status,
                transient=first.transient,
            ) from first
        found, absent = [], []
        for suite, present, err in results:
            if present:
                found.append(suite)
                log.info("found suite %s", suite)
            elif err is None:
                absent.append(suite)
            else:
                self._problem(f"probe {suite}: {err}")
        if absent:
            log.debug("absent: %s", ", ".join(absent))
        log.info(
            "probed %d suites: %d found, %d absent, %d errors",
            len(results),
            len(found),
            len(absent),
            len(errors),
        )
        return found

    def _run_probes(self, url: str, work: list[str]) -> list[tuple[str, bool, FetchError | None]]:
        """:meth:`_probe_one` for every suite of *work* on daemon threads, in order.

        The workers are daemon threads so that interpreter exit does not wait
        for in-flight probes. On any exception in the calling thread (Ctrl-C,
        the token or deadline of an enclosing :meth:`Fetcher.scope`, a
        :meth:`Fetcher.cancel`) or in a worker (such as
        :class:`FetchCancelled`), queued probes are dropped, the retries of
        this run's in-flight probes are cancelled through a token of their
        own (other requests of the Fetcher, in other threads, are not
        affected) and the exception propagates at once.
        """
        jobs: queue.SimpleQueue[int] = queue.SimpleQueue()
        for i in range(len(work)):
            jobs.put(i)
        done: queue.SimpleQueue[tuple[int, Any]] = queue.SimpleQueue()
        stop = threading.Event()
        # every probe request uses the cancellation event current now, so that
        # cancel() reaches requests that start after it, too; likewise the
        # token and deadline of the caller's Fetcher.scope()
        cancelled = self.fetcher._current_cancel()
        call = self.fetcher._current_call()
        # stops this run's probes only (Fetcher.cancel() would stop every
        # request of the Fetcher, in all threads)
        probe_stop = CancelToken()

        def worker() -> None:
            self.fetcher._bind_cancel(cancelled)
            with self.fetcher._call_scope(token=probe_stop, base=call):
                while not stop.is_set():  # after cancel(), requests raise FetchCancelled
                    try:
                        i = jobs.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        done.put((i, self._probe_one(url, work[i], stop)))
                    except BaseException as e:  # re-raised in the calling thread
                        done.put((i, e))

        for _ in range(min(PROBE_WORKERS, len(work))):
            threading.Thread(target=worker, name="minideb-probe", daemon=True).start()
        results: list[Any] = [None] * len(work)
        try:
            for _ in work:
                while True:
                    try:  # a timeout keeps the wait interruptible on every platform
                        i, result = done.get(timeout=0.1)
                        break
                    except queue.Empty:
                        # cancel() and the scope's token/deadline also stop a
                        # run whose probes are blocked in a read
                        if cancelled.is_set():
                            raise FetchCancelled(
                                f"request cancelled: {redact_url(url)}", url
                            ) from None
                        call.check(url)
                if isinstance(result, BaseException):
                    raise result
                results[i] = result
        except BaseException:
            stop.set()
            probe_stop.cancel()
            raise
        return results

    def _probe_one(
        self, url: str, suite: str, stop: threading.Event | None = None
    ) -> tuple[str, bool, FetchError | None]:
        """``(suite, present, error)``; runs in a worker and does not log.

        401/403/404/410 mean absent (``(suite, False, None)``); any other
        failure is returned as the error and leaves the suite unknown. Must
        not touch shared client state. Once *stop* is set no further request
        is made (the result is then discarded by the caller).
        """
        dists = dists_url(url, suite)
        for name in ("InRelease", "Release"):
            if stop is not None and stop.is_set():
                break
            try:
                if self.fetcher.exists(f"{dists}/{name}"):
                    return suite, True, None
            except (FetchCancelled, FetchTimeout):
                raise
            except FetchError as e:
                if e.status not in _ABSENT_STATUS:
                    return suite, False, e
        return suite, False, None

    def release(self, suite: str) -> Release:
        """Release of *suite* from its first entry."""
        return self._releases_for(suite)[0][1]

    def components(self, suite: str) -> list[str]:
        """Components listed in Release (union over entries, in order).

        For flat repositories this is ``[""]``. When Release has no
        ``Components`` field, the components configured in the sources
        entries are returned (possibly ``[]``). For suites ending in
        ``/updates`` the old Debian-security ``updates/`` prefix is dropped.
        The client's component scope applies. Downloads only Release.
        """
        out: list[str] = []
        for entry, rel in self._releases_for(suite):
            out += [display for _, display in self._components(entry, rel, None)]
        return list(dict.fromkeys(out))

    def architectures(self, suite: str) -> list[str]:
        """``Architectures`` from Release (union over entries, in order)."""
        out: list[str] = []
        for _, rel in self._releases_for(suite):
            out += rel.fields.get_list("Architectures", sep=None)
        return list(dict.fromkeys(out))

    def _components(
        self,
        entry: SourceEntry,
        rel: Release,
        wanted: str | None,
        only: Sequence[str] | None = None,
    ) -> list[tuple[str, str]]:
        """``(name used in paths, display name)`` pairs to query.

        Without *wanted*, the entry's (or Release's) components are kept if
        listed in *only* (``None``: the client scope; empty: everything).
        """
        if entry.flat:
            return [("", "")]
        strip = entry.suite.endswith("/updates")

        def display(comp: str) -> str:
            if strip and comp.startswith(_UPDATES_PREFIX):
                return comp[len(_UPDATES_PREFIX) :]
            return comp

        listed = rel.fields.get_list("Components", sep=None)
        if not listed and not entry.components:
            key = (entry.uri, entry.suite)
            if key not in self._noted:
                self._noted.add(key)
                self._problem(
                    f"{entry.suite}: Release at {redact_url(entry.uri)} has no Components field "
                    "and no components are configured"
                )
        if wanted is not None:
            for comp in listed:
                if wanted in (comp, display(comp)):
                    return [(comp, display(comp))]
            return [(wanted, wanted)]
        names = list(entry.components) or listed
        by_display = {display(c): c for c in listed}
        pairs = [(by_display.get(c, c), display(c)) for c in names]
        scope = self.scope_components if only is None else only
        if scope:
            pairs = [(c, d) for c, d in pairs if c in scope or d in scope]
        return pairs

    def _arches(
        self,
        entry: SourceEntry,
        rel: Release,
        wanted: str | None,
        only: Sequence[str] | None = None,
    ) -> list[str]:
        """Architectures to query; *only* as in :meth:`_components`."""
        if wanted is not None:
            return [wanted]
        names = list(entry.architectures) or rel.fields.get_list("Architectures", sep=None)
        scope = self.scope_architectures if only is None else only
        return [a for a in names if a in scope] if scope else names

    def _scoped(
        self,
        entry: SourceEntry,
        rel: Release,
        component: str | None,
        arch: str | None,
        comps: Sequence[str] | None = None,
        arches: Sequence[str] | None = None,
        with_arches: bool = True,
    ) -> list[tuple[str, str, list[str]]]:
        """``(component, display, architectures)`` to query for *entry*.

        When the scope removes every component (or, with *with_arches*,
        every architecture) a problem is recorded once per entry, so that an
        empty result is not silent. Scoped architectures that Release does
        not list at all are reported as such instead.
        """
        pairs = self._components(entry, rel, component, comps)
        archs = self._arches(entry, rel, arch, arches) if with_arches else []
        c_scope = list(self.scope_components if comps is None else comps)
        a_scope = list(self.scope_architectures if arches is None else arches)
        empty = []
        if with_arches and not archs and self._arches(entry, rel, arch, ()):
            listed = rel.fields.get_list("Architectures", sep=None)
            if listed and not set(a_scope) & set(listed):
                key = (entry.uri, entry.suite, "unlisted architecture")
                if key not in self._noted_scope:
                    self._noted_scope.add(key)
                    self._problem(
                        f"{entry.suite}: architecture {' '.join(a_scope)} not listed in Release "
                        f"at {redact_url(entry.uri)} (Release lists: {' '.join(listed)})"
                    )
            else:
                empty.append("architecture")
        if not pairs and self._components(entry, rel, component, ()):
            empty.append("component")
        if empty:
            what = " and ".join(empty)
            key = (entry.uri, entry.suite, what)
            if key not in self._noted_scope:
                self._noted_scope.add(key)
                detail = ", ".join(
                    f"{name}: {' '.join(values)}"
                    for name, values in (("components", c_scope), ("architectures", a_scope))
                    if values
                )
                self._problem(
                    f"{entry.suite}: no architecture/component left after scoping "
                    f"({what}; scope {detail or 'none'}) at {redact_url(entry.uri)}"
                )
        return [(c, d, archs) for c, d in pairs]

    # -- index loading -----------------------------------------------------

    def _index(
        self,
        entry: SourceEntry,
        rel: Release,
        comp: str,
        arch: str,
        kind: IndexKind,
        unlisted_ok: bool = False,
    ) -> tuple[str, Any] | None:
        """``(path, parsed data)`` of the best available index, cached.

        Compressed variants are tried in preference order; a 404 moves on.
        None (with a problem recorded) if nothing is listed or retrievable;
        with *unlisted_ok* an index not listed in Release is no problem.
        """
        key = (entry.uri, entry.suite, comp, arch, kind)
        with self._lock:
            if key in self._indices:
                log.debug("index cache hit: %s %s", entry.suite, _describe(comp, arch, kind))
                hit = self._indices[key]
                if hit is not None:
                    self._touch((entry.uri, entry.suite, hit[0]))
                return hit
        result = None
        candidates = _select(rel, comp, arch, kind)
        what = _describe(comp, arch, kind)
        log.debug(
            "%s: %s candidates in preference order: %s",
            entry.suite,
            what,
            ", ".join(c.path for c in candidates) or "none",
        )
        if not candidates:
            if unlisted_ok:
                log.debug(
                    "%s: %s not listed in Release at %s", entry.suite, what, redact_url(entry.uri)
                )
            else:
                self._problem(
                    f"{entry.suite}: {what} not listed in Release at {redact_url(entry.uri)}"
                )
        else:
            for cand in candidates:
                pkey = (entry.uri, entry.suite, cand.path)
                with self._lock:
                    if pkey in self._parsed:  # e.g. top-level Contents shared by components
                        log.debug("parsed cache hit: %s", cand.path)
                        self._touch(pkey)
                        result = (cand.path, self._parsed[pkey])
                        self._indices[key] = result
                        return result
                # downloaded and parsed without the lock (another thread may
                # load the same index meanwhile: the last one stored wins)
                try:
                    data = fetch_index(self.fetcher, entry.uri, rel, cand, verify=self.verify)
                except FetchError as e:
                    if not is_not_found(e):
                        raise
                    log.debug("%s missing, trying next candidate", redact_url(e.url))
                    continue
                parsed = _parse(kind, data)
                log.debug("parsed %s: %d entries", cand.path, _count(parsed))
                result = (cand.path, parsed)
                with self._lock:
                    self._store_parsed(pkey, parsed, len(data), kind)
                    self._indices[key] = result
                return result
            else:
                self._problem(f"{entry.suite}: {what} missing on server at {redact_url(entry.uri)}")
        with self._lock:
            self._indices[key] = result
        return result

    # -- queries -----------------------------------------------------------

    def packages(
        self, suite: str, component: str | None = None, arch: str | None = None
    ) -> list[PackageRecord]:
        """Binary package records of *suite*, optionally for one component/arch.

        Without *arch* the entry's configured architectures are used, else
        those listed in Release (both narrowed by the client scope). For
        flat repositories the single ``Packages`` index is filtered by
        ``Architecture`` (``all`` included).

        Download cost: one ``Packages`` index per component and
        architecture queried (Debian ``main``/``amd64`` is about 10 MB
        compressed); pass *component*/*arch* or scope the client to limit it.
        """
        return self._packages(suite, component, arch)

    def _packages(
        self,
        suite: str,
        component: str | None,
        arch: str | None,
        comps: Sequence[str] | None = None,
        arches: Sequence[str] | None = None,
    ) -> list[PackageRecord]:
        out: list[PackageRecord] = []
        for entry, rel in self._releases_for(suite):
            if entry.flat:
                found = self._index(entry, rel, "", arch or "", "binary")
                base = _dists(entry)
                scope = self.scope_architectures if arches is None else arches
                wanted = (arch,) if arch is not None else scope
                for para in found[1] if found else []:
                    para_arch = para.get("Architecture", "")
                    if not wanted or para_arch in wanted or para_arch == "all":
                        self._keep(out, PackageRecord(para, suite, "", para_arch, base))
                continue
            for comp, display, archs in self._scoped(entry, rel, component, arch, comps, arches):
                for a in archs:
                    found = self._index(entry, rel, comp, a, "binary")
                    for para in found[1] if found else []:
                        self._keep(out, PackageRecord(para, suite, display, a, entry.uri))
        return out

    def sources(self, suite: str, component: str | None = None) -> list[SourceRecord]:
        """Source package records of *suite* (``deb-src`` entries preferred).

        Download cost: one ``Sources`` index per component queried (the
        client's component scope applies).
        """
        return self._sources(suite, component)

    def _sources(
        self, suite: str, component: str | None, comps: Sequence[str] | None = None
    ) -> list[SourceRecord]:
        out: list[SourceRecord] = []
        for entry, rel in self._releases_for(suite, ("deb-src",)):
            base = _dists(entry) if entry.flat else entry.uri
            scoped = self._scoped(entry, rel, component, None, comps, with_arches=False)
            for comp, display, _ in scoped:
                found = self._index(entry, rel, comp, "source", "source")
                for para in found[1] if found else []:
                    self._keep(out, SourceRecord(para, suite, display, base_url=base))
        return out

    def fetch_dsc(self, record: SourceRecord, *, verify: bool | None = None) -> Dsc:
        """Download and parse the ``.dsc`` of *record*.

        The bytes are checked against the strongest hash the ``Sources``
        paragraph lists for the file (``Checksums-Sha512``, ``-Sha256``,
        ``-Sha1``, then ``Files``) and its size. A mismatch, or no listed hash
        at all, raises :class:`~minideblib.repo.release.HashMismatch` unless the
        client was built with ``verify=False``. The OpenPGP signature of the
        ``.dsc`` is not verified. :class:`RepoError` if no ``.dsc`` is listed.

        *verify* overrides the client's setting for this fetch (``True`` keeps
        the hash check even on a client built with ``verify=False``).
        """
        name, url = record.dsc_name, record.dsc_url
        if name is None or url is None:
            raise RepoError(f"{record.name} {record.version}: no .dsc file listed")
        log.info("fetching %s", name)
        data = self.fetcher.get(url)
        if self.verify if verify is None else verify:
            _check_listed_hash(record.paragraph, name, data)
        return Dsc.parse(data)

    def _keep(self, out: list, rec: PackageRecord | SourceRecord) -> None:
        """Append *rec* unless its paths are unsafe (then note a problem)."""
        try:
            _ = rec.version  # an invalid Version would break sorting and output later
            if isinstance(rec, PackageRecord):
                _safe_join(rec.base_url, rec.filename)
            else:
                rec.file_urls  # noqa: B018 - validates Directory and file names
                _safe_join(rec.base_url, rec.directory)
        except ValueError as e:
            msg = f"{rec.suite}: skipping {rec.name} {rec.component}/{rec.arch}: {e}"
            self._problem_once((rec.base_url, rec.suite, msg), msg)
            return
        out.append(rec)

    def find_files(
        self,
        suite: str,
        path: str,
        arch: str | None = None,
        components: Sequence[str] | None = None,
    ) -> list[tuple[str, str]]:
        """``(path, "section/package")`` pairs from Contents.

        *path* matches a Contents path exactly (leading ``/`` ignored) or,
        when it has no ``/``, by basename. For each component (*components*,
        else the client scope, else all) and architecture (*arch*, else
        the scope or all), the per-component ``<comp>/Contents-<arch>`` is
        read, falling back to the top-level ``Contents-<arch>`` (read once);
        repositories without Contents give ``[]`` and a problem entry.

        Download cost: Contents indices are the largest files of an
        archive. Debian has one per component (``main/Contents-amd64.gz``
        is about 12 MB compressed, ``contrib`` well under 1 MB); Ubuntu has
        a single archive-wide top-level ``Contents-amd64.gz`` (about 50 MB)
        that no component restriction avoids. Pass *arch* and *components*.

        Memory: each Contents file read is parsed into a full dict (path ->
        packages) and cached, about 3.8 times its uncompressed text size in
        RSS. Ubuntu's archive-wide file therefore needs several GB; scope the
        components, or look up files in a small component.
        """
        target = path.lstrip("/")
        by_basename = "/" not in target
        seen: set[tuple[str, str]] = set()
        out: list[tuple[str, str]] = []
        for entry, rel in self._releases_for(suite):
            for comp, _, archs in self._scoped(entry, rel, None, arch, components):
                for a in archs:
                    found = self._index(entry, rel, comp, a, "contents")
                    if found is None or (entry.uri, found[0]) in seen:
                        continue  # e.g. top-level Contents already read for another component
                    seen.add((entry.uri, found[0]))
                    for p, locations in found[1].items():
                        if p == target or (by_basename and posixpath.basename(p) == target):
                            out += [(p, loc) for loc in locations]
        return list(dict.fromkeys(out))

    @overload
    def search(
        self,
        suite: str,
        pattern: str,
        *,
        components: Sequence[str] | None = ...,
        architectures: Sequence[str] | None = ...,
        kind: Literal["binary"] = ...,
    ) -> list[PackageRecord]: ...

    @overload
    def search(
        self,
        suite: str,
        pattern: str,
        *,
        components: Sequence[str] | None = ...,
        architectures: Sequence[str] | None = ...,
        kind: Literal["source"],
    ) -> list[SourceRecord]: ...

    def search(
        self,
        suite: str,
        pattern: str,
        *,
        components: Sequence[str] | None = None,
        architectures: Sequence[str] | None = None,
        kind: Literal["binary", "source"] = "binary",
    ) -> list[PackageRecord] | list[SourceRecord]:
        """Packages of *suite* matching *pattern*, case-insensitively.

        A pattern containing ``*`` or ``?`` is a glob matched against the
        package name only; otherwise it is a substring of the name or of
        the short (first-line) description. Downloads what
        :meth:`packages` does for the whole suite (the client scope applies).

        *components* narrows the components read (default: the client
        scope; empty: everything). For binary packages *architectures*
        narrows the indices read and keeps the records of those index
        architectures plus ``Architecture: all`` packages; ``["all"]``
        alone keeps only ``Architecture: all`` packages.
        ``kind="source"`` searches ``Sources`` instead and returns
        :class:`SourceRecord` items (a substring also matches a name in
        ``Binary``; *architectures* is ignored). ``ValueError`` for another
        *kind*.
        """
        pat = pattern.lower()
        glob = "*" in pat or "?" in pat
        if kind == "source":
            srcs = self._sources(suite, None, components)
            if glob:
                return [r for r in srcs if fnmatch.fnmatchcase(r.name.lower(), pat)]
            return [
                r
                for r in srcs
                if pat in r.name.lower() or any(pat in b.lower() for b in r.binaries)
            ]
        if kind != "binary":
            raise ValueError(f"unknown kind {kind!r}; use 'binary' or 'source'")
        recs = self._packages(
            suite, None, None, components, self._index_arches(suite, architectures)
        )
        if architectures:
            wanted = set(architectures)
            if wanted == {"all"}:
                recs = [r for r in recs if r.architecture == "all"]
            else:
                recs = [
                    r
                    for r in recs
                    if r.arch in wanted or r.architecture in wanted or r.architecture == "all"
                ]
        if glob:
            return [r for r in recs if fnmatch.fnmatchcase(r.name.lower(), pat)]
        return [r for r in recs if pat in r.name.lower() or pat in r.description.lower()]

    # -- download plans and grouping ----------------------------------------

    def _index_arches(self, suite: str, architectures: Sequence[str] | None) -> list[str] | None:
        """Index architectures to read for an architecture filter.

        None means the client scope and an empty list everything, as for
        the *architectures* of :meth:`versions`. ``Architecture: all``
        packages are listed in the per-arch indices and, where Release
        lists ``all`` (without ``No-Support-for-Architecture-all:
        Packages``), in ``binary-all``: that index is added to concrete
        architectures, and ``["all"]`` alone reads it plus one concrete
        architecture (not every one). Reads Release only.
        """
        if not architectures:
            return None if architectures is None else []
        concrete = [a for a in architectures if a != "all"]
        releases = self._releases_for(suite)
        extra = ["all"] if any(_supports_binary_all(rel) for _, rel in releases) else []
        if concrete:
            return concrete + extra
        for entry, rel in releases:
            first = next((a for a in self._arches(entry, rel, None) if a != "all"), None)
            if first is not None:
                return extra + [first]
        return None

    def plan_binary(
        self,
        suite: str,
        name: str,
        *,
        version: str | None = None,
        architectures: Sequence[str] | None = None,
        components: Sequence[str] | None = None,
    ) -> list[DownloadPlan]:
        """Download plans for binary package *name* of *suite*, one per distinct file.

        Architecture-specific packages give one plan per architecture; an
        ``Architecture: all`` package listed in several per-arch indices
        gives one plan (architecture ``all``). *architectures* keeps
        packages of the listed architectures plus ``Architecture: all``
        ones (installable everywhere, reported as ``all``); ``["all"]``
        alone keeps only the latter; empty or None: no filter. *components*
        as for :meth:`search`.

        Without *version* the highest version (Debian ordering) is chosen
        for every target architecture (each concrete architecture asked
        for, else each seen) among its own and the ``all`` records, so a
        package that moved to or from ``Architecture: all`` gives only the
        current file per architecture; with *version*, records of an equal
        version.
        Records with an unsafe or missing ``Filename`` are skipped with a
        problem. Sorted by architecture, then component. Only indices are
        downloaded (through the cache); ``[]`` when nothing matches.
        """
        index_arches = self._index_arches(suite, architectures)
        recs = self._packages(suite, None, None, components, index_arches)
        wanted = set(architectures) if architectures else None
        cands: list[PackageRecord] = []
        for r in recs:
            if r.name != name:
                continue
            if wanted is not None and r.architecture != "all" and r.architecture not in wanted:
                continue
            if not r.filename:
                msg = f"{r.suite}: skipping {r.name} {r.component}/{r.arch}: no Filename listed"
                self._problem_once((r.base_url, r.suite, msg), msg)
                continue
            cands.append(r)
        if version is not None:
            want = Version(version)
            cands = [r for r in cands if r.version == want]
        else:
            cands = _current_per_target(cands, wanted)
        plans: dict[tuple[str, ...], DownloadPlan] = {}
        for r in cands:
            spec = FileSpec(
                posixpath.basename(r.filename), r.url, _size_or_none(r.size), r.strongest_hash
            )
            # the same file listed in several indices (or mirrors) is planned once
            key = (r.architecture, r.component, str(r.version), r.filename, *spec.hashes.values())
            if key not in plans:
                plans[key] = DownloadPlan(
                    "binary",
                    r.name,
                    r.paragraph.get("Version", "0"),
                    r.suite,
                    r.component,
                    r.architecture,
                    (spec,),
                )
        return sorted(plans.values(), key=lambda p: (p.architecture or "", p.component))

    def plan_source(
        self,
        suite: str,
        name: str,
        *,
        version: str | None = None,
        components: Sequence[str] | None = None,
    ) -> DownloadPlan:
        """Download plan for source package *name* of *suite* (highest version by default).

        The files are those of the ``Sources`` stanza under its
        ``Directory``, each with the strongest hash listed for it (see
        :attr:`SourceRecord.files`), the ``.dsc`` first. :class:`RepoError`
        when no such source (version) exists or it lists no ``.dsc``.
        Only the ``Sources`` indices are downloaded (through the cache).
        """
        recs = [r for r in self._sources(suite, None, components) if r.name == name]
        if version is not None:
            want = Version(version)
            recs = [r for r in recs if r.version == want]
        if not recs:
            what = name if version is None else f"{name} {version}"
            raise RepoError(f"{suite}: source package {what} not found")
        rec = max(recs, key=lambda r: r.version)  # the first of the highest
        listed = rec._listed()
        dsc = rec.dsc_name
        if dsc is None:
            raise RepoError(f"{rec.name} {rec.version}: no .dsc file listed")
        order = [dsc] + [n for n in listed if n != dsc]
        files = tuple(
            FileSpec(
                n,
                _safe_join(rec.base_url, rec.directory, n),
                _size_or_none(listed[n][1].size),
                {listed[n][0]: listed[n][1].digest.lower()},
            )
            for n in order
        )
        version_text = rec.paragraph.get("Version", "0")
        return DownloadPlan("source", rec.name, version_text, suite, rec.component, None, files)

    @staticmethod
    def group_packages(records: Iterable[PackageRecord]) -> list[PackageGroup]:
        """Group *records* into one :class:`PackageGroup` per package version.

        The key is suite, name, version (compared as :class:`Version`; the
        text of the first record is reported) and component. Identical
        ``Architecture: all`` records (from several per-arch indices) become
        one variant. Groups are sorted by name, then version descending
        (Debian ordering), then component and suite.
        """
        groups: dict[tuple[str, str, Version, str], list[PackageRecord]] = {}
        for r in records:
            # Version equality: "1.0" and "0:1.0" are one version
            key = (r.suite, r.name, r.version, r.component)
            variants = groups.setdefault(key, [])
            if r.architecture == "all" and any(
                v.architecture == "all" and v.paragraph == r.paragraph for v in variants
            ):
                continue
            variants.append(r)
        out: list[PackageGroup] = []
        for (suite, name, _, component), variants in groups.items():
            version = variants[0].paragraph.get("Version", "0")  # as first seen
            variants.sort(key=lambda v: (v.arch, v.architecture))
            multi = next((m for v in variants if (m := v.paragraph.get("Multi-Arch"))), None)
            arches = tuple(sorted({v.architecture for v in variants}))
            out.append(
                PackageGroup(name, version, component, suite, arches, multi, tuple(variants))
            )
        out.sort(key=lambda g: (g.component, g.suite))
        out.sort(key=lambda g: g.variants[0].version, reverse=True)
        out.sort(key=lambda g: g.name)
        return out

    def appstream(
        self, suite: str, component: str | None = None, arch: str | None = None
    ) -> list[dict]:
        """DEP-11 (AppStream) component documents of *suite*.

        Each document is a parsed ``Components-<arch>.yml`` entry (header
        dropped) with ``_suite``, ``_component`` and ``_arch`` keys added.
        Architectures without a listed DEP-11 file (e.g. ``all``) and
        repositories without DEP-11 metadata (or flat ones) silently give
        nothing; a listed file missing on the server is recorded in
        :attr:`problems`. Needs PyYAML
        (:class:`~minideblib.repo.dep11.MissingYamlError` otherwise).
        """
        out: list[dict] = []
        for entry, rel in self._releases_for(suite):
            if entry.flat or not _has_dep11(rel):
                continue
            for comp, display in self._components(entry, rel, component):
                for a in self._arches(entry, rel, arch):
                    found = self._index(entry, rel, comp, a, "dep11", unlisted_ok=True)
                    if found is None:
                        continue
                    _, components = found[1]
                    tags = {"_suite": suite, "_component": display, "_arch": a}
                    out += [{**doc, **tags} for doc in components]
        return out

    def icon_indices(self, suite: str, component: str | None = None) -> list[IndexFile]:
        """DEP-11 icon tarballs (``<comp>/dep11/icons-<size>.tar.*``) listed in Release.

        Every listed size and compression is returned, per component in
        natural size order; nothing is downloaded. ``[]`` without DEP-11.
        """
        out: list[IndexFile] = []
        for entry, rel in self._releases_for(suite):
            if entry.flat or not _has_dep11(rel):
                continue
            for comp, _ in self._components(entry, rel, component):
                for idx in _select(rel, comp, "", "icons"):
                    if idx not in out:
                        out.append(idx)
        return out

    def versions(
        self,
        name: str,
        suites: Iterable[str] | None = None,
        components: Sequence[str] | None = None,
        architectures: Sequence[str] | None = None,
    ) -> list[PackageRecord]:
        """Records of package *name* in *suites* (default: :meth:`suites`).

        *components* and *architectures* narrow what is read (default: the
        client scope; empty: everything). Sorted by suite, then version
        descending. Suites whose Release is missing are skipped with a
        problem entry.

        Download cost: one ``Packages`` index per suite, component and
        architecture, about 10 MB compressed each for Debian ``main`` and
        ``amd64``, and without *suites* also the probing of
        :meth:`suites`; scope all three for quick answers.
        """
        out: list[PackageRecord] = []
        wanted = self.suites() if suites is None else list(dict.fromkeys(suites))
        for suite in wanted:
            try:
                recs = self._packages(suite, None, None, components, architectures)
            except FetchError as e:
                if not is_not_found(e):
                    raise
                for entry in self._entries(suite):
                    cached = self._releases.get((entry.uri, entry.suite))
                    if isinstance(cached, FetchError):
                        self._note_missing(entry, cached)
                continue
            out += [r for r in recs if r.name == name]
        out.sort(key=lambda r: r.version, reverse=True)
        out.sort(key=lambda r: r.suite)
        return out


#: Sources checksum fields, strongest first, with their hashlib names.
_SOURCES_HASHES = tuple((alg.sources_field, alg.name) for alg in ALGORITHMS)


def _check_listed_hash(para: Paragraph, name: str, data: bytes) -> None:
    """Check *data* against the strongest hash *para* lists for *name* (fail closed).

    Only ``Checksums-Sha1`` or ``Files`` (MD5) listing *name* is not enough:
    :class:`HashMismatch` (SHA256 or stronger is required).
    """
    for field_name, algo in _SOURCES_HASHES:
        if field_name not in para:
            continue
        try:
            rows = parse_multiline_hashes(para[field_name])
        except ChecksumError as e:
            raise HashMismatch(f"{name}: malformed {field_name}: {e}") from e
        for digest, size, row_name in rows:
            if row_name != name:
                continue
            if is_weak(algo):
                raise HashMismatch(
                    f"{name}: no SHA256 or stronger hash listed in the Sources index"
                )
            if len(data) != size:
                raise HashMismatch(f"{name}: size {len(data)} != expected {size}")
            actual = hashlib.new(algo, data, usedforsecurity=False).hexdigest()
            if actual != digest:
                raise HashMismatch(f"{name}: {algo} {actual} != expected {digest}")
            log.info("%s verified (%s, %d bytes)", name, algo, len(data))
            return
    raise HashMismatch(f"{name}: no hash listed in the Sources index")


def _current_per_target(cands: list[PackageRecord], wanted: set[str] | None) -> list[PackageRecord]:
    """The highest-version records for every target architecture.

    Targets are the concrete architectures in *wanted*, else every concrete
    architecture seen (``Architecture`` or index architecture), else
    ``all``. A target's candidates are its own records and the ``all``
    ones. Input order is kept.
    """
    targets = [a for a in sorted(wanted or ()) if a != "all"]
    if not targets:
        seen = {a for r in cands for a in (r.architecture, r.arch)}
        targets = sorted(seen - {"all", ""}) or ["all"]
    chosen: set[int] = set()
    for target in targets:
        mine = [r for r in cands if r.architecture in (target, "all")]
        if not mine:
            continue
        best = max(r.version for r in mine)
        chosen.update(id(r) for r in mine if r.version == best)
    return [r for r in cands if id(r) in chosen]


def _supports_binary_all(rel: Release) -> bool:
    """Whether *rel* lists ``all`` and does not say its packages are elsewhere."""
    if "all" not in rel.fields.get_list("Architectures", sep=None):
        return False
    return rel.fields.get("No-Support-for-Architecture-all", "").strip() != "Packages"


def _unreachable(err: FetchError) -> bool:
    """Network-level failure, server error or rate limit: the repository did not answer."""
    return err.status is None or err.status >= 500 or err.status == 429


def _missing_local_dir(url: str) -> bool:
    """True for a ``file://`` URL whose directory does not exist."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "file" or parts.netloc not in ("", "localhost"):
        return False
    return not os.path.isdir(url2pathname(parts.path))


def _dists(entry: SourceEntry) -> str:
    return dists_url(entry.uri, entry.suite)


def _select(rel: Release, comp: str, arch: str, kind: IndexKind) -> list[IndexFile]:
    """:func:`select_indices`, retrying without the old Debian-security ``updates/`` prefix."""
    candidates = select_indices(rel, comp, arch, kind)
    if not candidates and comp.startswith(_UPDATES_PREFIX):
        candidates = select_indices(rel, comp[len(_UPDATES_PREFIX) :], arch, kind)
    return candidates


def _has_dep11(rel: Release) -> bool:
    return any("dep11/" in path for path in rel.files)


def _describe(comp: str, arch: str, kind: IndexKind) -> str:
    prefix = f"{comp}/" if comp else ""
    if kind == "dep11":
        return f"{prefix}dep11/Components-{arch}.yml"
    if kind == "binary":
        return f"{prefix}binary-{arch}/Packages" if comp else "Packages"
    if kind == "source":
        return f"{prefix}source/Sources" if comp else "Sources"
    return f"Contents-{arch}"


def _count(parsed: Any) -> int:
    if isinstance(parsed, tuple):  # dep11: (header, components)
        parsed = parsed[-1]
    return len(parsed) if hasattr(parsed, "__len__") else 0


def _parse(kind: IndexKind, data: bytes) -> Any:
    # fetch_index already decompressed the data
    if kind == "binary":
        return parse_packages(data)
    if kind == "source":
        return parse_sources(data)
    if kind == "dep11":
        return dep11.parse_components(data)
    return parse_contents(data)
