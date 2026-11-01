# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Readers for Packages, Sources and Contents indices, and index fetching."""

from __future__ import annotations

import logging

from minideblib.compress import DEFAULT_MAX_SIZE, decompress
from minideblib.control import Paragraph, decode_text, parse_paragraphs
from minideblib.repo.fetch import Fetcher, FetchError, redact_url
from minideblib.repo.release import IndexFile, Release, _not_found, index_urls

__all__ = [
    "fetch_index",
    "load_contents",
    "load_packages",
    "load_sources",
    "parse_contents",
    "parse_packages",
    "parse_sources",
]

log = logging.getLogger(__name__)

_BLANK = " \t"  # field separators of a Contents line


def _paragraphs(data: bytes, name: str) -> list[Paragraph]:
    return list(parse_paragraphs(decode_text(data, name)))


def parse_packages(data: bytes, name: str = "Packages") -> list[Paragraph]:
    """Parse already-decompressed ``Packages`` bytes; *name* is for messages."""
    return _paragraphs(data, name)


def parse_sources(data: bytes, name: str = "Sources") -> list[Paragraph]:
    """Parse already-decompressed ``Sources`` bytes; empty data gives ``[]``."""
    return _paragraphs(data, name)


def parse_contents(data: bytes, name: str = "Contents") -> dict[str, list[str]]:
    """Parse already-decompressed ``Contents`` bytes into ``path -> [section/package]``.

    The location is the last field; only spaces and tabs separate it from the
    path (other whitespace, such as U+00A0 or a form feed, is part of the path).
    """
    text = decode_text(data, name)
    out: dict[str, list[str]] = {}
    for raw in text.split("\n"):
        line = raw.rstrip("\r").strip(_BLANK)
        cut = max(line.rfind(" "), line.rfind("\t"))
        path, locations = line[:cut].rstrip(_BLANK), line[cut + 1 :]
        if cut < 0 or not path:
            if line:
                log.warning("Contents line without a location: %r", line)
            continue
        if path == "FILE" and locations == "LOCATION":
            continue
        out.setdefault(path, []).extend(p for p in locations.split(",") if p)
    return out


def load_packages(data: bytes, name: str) -> list[Paragraph]:
    """Parse a (possibly compressed) ``Packages`` index; *name* hints the codec."""
    return parse_packages(decompress(data, name), name)


def load_sources(data: bytes, name: str) -> list[Paragraph]:
    """Parse a (possibly compressed) ``Sources`` index; empty data gives ``[]``."""
    return parse_sources(decompress(data, name), name)


def load_contents(data: bytes, name: str) -> dict[str, list[str]]:
    """Parse a (possibly compressed) ``Contents`` index into ``path -> [section/package]``."""
    return parse_contents(decompress(data, name), name)


def fetch_index(
    fetcher: Fetcher,
    base_url: str,
    rel: Release,
    entry: IndexFile,
    verify: bool = True,
    max_size: int = DEFAULT_MAX_SIZE,
) -> bytes:
    """Download *entry*, verify the served bytes against *rel*, return decompressed data.

    With Acquire-By-Hash, by-hash URLs are tried strongest algorithm first and
    then the plain path; a 404 moves on to the next candidate, any other error
    propagates. *base_url* is unused (``rel.dists_url`` is authoritative) and
    kept for API symmetry. ``HashMismatch`` propagates; ``FetchError`` too if
    every candidate is missing.
    """
    urls = index_urls(rel, entry)
    log.debug("%s: %d candidate URL(s), by-hash=%s", entry.path, len(urls), rel.acquire_by_hash)
    for i, url in enumerate(urls):
        log.debug("trying candidate %d/%d: %s", i + 1, len(urls), redact_url(url))
        try:
            raw = fetcher.get(url)
            break
        except FetchError as e:
            if not _not_found(e) or i == len(urls) - 1:
                raise
            log.debug("%s not found, trying next candidate", redact_url(url))
    if verify:
        rel.verify(entry.path, raw)
        algo = rel.best_hash(entry.path)[0].lower()
        log.info("index %s (%d bytes, %s verified)", entry.path, len(raw), algo)
    else:
        log.info("index %s (%d bytes, NOT verified)", entry.path, len(raw))
    return decompress(raw, entry.path, max_size=max_size)
