# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Parse APT sources: one-line ``sources.list`` and deb822 ``.sources`` files."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from minideblib.control import parse_paragraphs
from minideblib.errors import MinidebError

__all__ = [
    "DEFAULT_SOURCES_DIR",
    "DEFAULT_SOURCES_LIST",
    "SourceEntry",
    "SourcesError",
    "load",
    "parse_deb822",
    "parse_one_line",
    "parse_sources_list",
]

log = logging.getLogger(__name__)

DEFAULT_SOURCES_LIST = Path("/etc/apt/sources.list")
DEFAULT_SOURCES_DIR = Path("/etc/apt/sources.list.d")

_TYPES = ("deb", "deb-src")


class SourcesError(MinidebError, ValueError):
    """A sources file that is malformed or not valid UTF-8; the message names the file."""


@dataclass(frozen=True)
class SourceEntry:
    """One repository (a single type/URI/suite combination)."""

    type: str
    uri: str
    suite: str
    components: tuple[str, ...]
    architectures: tuple[str, ...] = ()
    signed_by: str | None = None
    trusted: bool | None = None
    enabled: bool = True
    # Extra options, keys lowercased (operators such as ``arch+`` kept in key).
    options: dict[str, str] = field(default_factory=dict, hash=False)

    @property
    def flat(self) -> bool:
        """True for flat repositories (suite ends with ``/``)."""
        return self.suite.endswith("/")


def _tri(value: str) -> bool | None:
    v = value.strip().lower()
    if v in ("yes", "true", "1"):
        return True
    if v in ("no", "false", "0"):
        return False
    log.warning("ignoring unrecognised boolean value %r", value)
    return None


class _Options(TypedDict, total=False):
    """Keyword arguments for `SourceEntry` produced by `_split_options`."""

    architectures: tuple[str, ...]
    signed_by: str
    trusted: bool | None
    options: dict[str, str]


def _split_options(raw: dict[str, str]) -> _Options:
    """Pull well-known options out of *raw* (lowercased keys)."""
    opts = dict(raw)
    kw: _Options = {}
    if "arch" in opts:
        kw["architectures"] = tuple(a for a in opts.pop("arch").split(",") if a)
    if "signed-by" in opts:
        kw["signed_by"] = opts.pop("signed-by")
    if "trusted" in opts:
        kw["trusted"] = _tri(opts.pop("trusted"))
    for key in opts:
        if key[-1] in "+-":
            log.warning("option operator %r is not interpreted; kept verbatim in options", key)
    kw["options"] = opts
    return kw


# one-line entries separate their fields with runs of spaces and tabs
_BLANK = " \t"
_FIELD_SEP = re.compile(r"[ \t]+")


def _fields(text: str) -> list[str]:
    return [f for f in _FIELD_SEP.split(text) if f]


def parse_one_line(line: str) -> SourceEntry | None:
    """Parse a one-line sources.list entry; None for comments, blanks, errors.

    Fields are separated by spaces and tabs (other whitespace is part of a field).
    """
    text = line.split("#", 1)[0].strip()
    if not text:
        return None
    raw: dict[str, str] = {}
    typ, *tail = _FIELD_SEP.split(text, maxsplit=1)
    rest = tail[0] if tail else ""
    if rest.startswith("["):
        opt_text, close, rest = rest[1:].partition("]")
        if not close:
            log.warning("skipping entry with unterminated option block: %r", line)
            return None
        for item in _fields(opt_text):
            key, eq, val = item.partition("=")
            if not eq or not key:
                log.warning("skipping entry with bad option %r: %r", item, line)
                return None
            raw[key.lower()] = val
        rest = rest.lstrip(_BLANK)
    if rest.startswith("cdrom:["):
        # cdrom URIs contain spaces inside their own brackets
        end = rest.find("]")
        if end < 0:
            log.warning("skipping entry with unterminated cdrom URI: %r", line)
            return None
        sep = _FIELD_SEP.search(rest, end)
        end = len(rest) if sep is None else sep.start()
        parts = [typ, rest[:end], *_fields(rest[end:])]
    else:
        parts = [typ, *_fields(rest)]
    if len(parts) < 3 or parts[0] not in _TYPES:
        log.warning("skipping malformed sources.list entry: %r", line)
        return None
    typ, uri, suite, *components = parts
    if suite.endswith("/") and components:
        log.warning("flat repository with components, ignoring components: %r", line)
        components = []
    return SourceEntry(typ, uri, suite, tuple(components), **_split_options(raw))


def parse_sources_list(text: str) -> list[SourceEntry]:
    """Parse the text of a one-line-format file."""
    return [e for line in text.splitlines() if (e := parse_one_line(line)) is not None]


_DEB822_CONSUMED = {"types", "uris", "suites", "components", "enabled"}


def parse_deb822(text: str) -> list[SourceEntry]:
    """Parse deb822 text. Other fields (e.g. Languages) go to ``options``
    with lowercased keys."""
    out: list[SourceEntry] = []
    for para in parse_paragraphs(text):
        if "enabled" in para and _tri(para["enabled"]) is False:
            continue
        types = para.get("Types", "").split()
        uris = para.get("URIs", "").split()
        suites = para.get("Suites", "").split()
        if not (types and uris and suites):
            log.warning("skipping deb822 stanza lacking Types, URIs or Suites")
            continue
        bad = [t for t in types if t not in _TYPES]
        if bad:
            log.warning("skipping deb822 stanza with unknown Types %s", bad)
            continue
        raw = {k.lower(): v for k, v in para.items() if k.lower() not in _DEB822_CONSUMED}
        if "architectures" in raw:
            raw["arch"] = ",".join(raw.pop("architectures").split())
        kw = _split_options(raw)
        components = tuple(para.get("Components", "").split())
        for typ in types:
            for uri in uris:
                for suite in suites:
                    comps = () if suite.endswith("/") else components
                    own: _Options = {**kw, "options": dict(kw["options"])}
                    out.append(SourceEntry(typ, uri, suite, comps, **own))
    return out


def _load_file(path: Path) -> list[SourceEntry]:
    """Entries of one file; :class:`SourcesError` naming *path* if it is malformed."""
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as e:
        raise SourcesError(f"{path}: not valid UTF-8: {e}") from e
    try:
        if path.suffix == ".sources":
            return parse_deb822(text)
        return parse_sources_list(text)
    except ValueError as e:
        raise SourcesError(f"{path}: {e}") from e


def _load_dir(path: Path) -> list[SourceEntry]:
    files = sorted(
        (p for p in path.iterdir() if p.suffix in (".list", ".sources") and p.is_file()),
        key=lambda p: p.name,
    )
    out: list[SourceEntry] = []
    for p in files:
        try:
            out += _load_file(p)
        except OSError as exc:
            log.warning("skipping unreadable sources file %s: %s", p.name, exc)
        except SourcesError as exc:  # message names the file
            log.warning("skipping malformed sources file: %s", exc)
    return out


def load(path: Path | None = None) -> list[SourceEntry]:
    """Load entries from a file or directory (``*.list``, ``*.sources``).

    With None, read ``/etc/apt/sources.list`` and ``sources.list.d/`` if present.
    In a directory, unreadable or malformed files are skipped with a warning
    naming the file; an explicitly given file raises ``OSError`` when it
    cannot be read and :class:`SourcesError` (a ``ValueError`` naming the
    file) when it is not valid UTF-8 or malformed.
    """
    if path is not None:
        path = Path(path)
        return _load_dir(path) if path.is_dir() else _load_file(path)
    out: list[SourceEntry] = []
    if DEFAULT_SOURCES_LIST.is_file():
        out += _load_file(DEFAULT_SOURCES_LIST)
    if DEFAULT_SOURCES_DIR.is_dir():
        out += _load_dir(DEFAULT_SOURCES_DIR)
    return out
