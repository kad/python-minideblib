# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2005-2009, 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Parsing of Debian changelog files."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime

from minideblib.errors import MinidebError
from minideblib.version import Version

__all__ = ["ChangelogEntry", "ChangelogError", "parse"]

_PACKAGE = r"[a-z0-9][a-z0-9.+-]+"
_VERSION = r"(?:[0-9]+:)?[a-zA-Z0-9~.+-]+"
_START = re.compile(
    rf"(?P<package>{_PACKAGE}) \((?P<version>{_VERSION})\) "
    r"(?P<dists>[\w-]+(?:\s+[\w-]+)*); (?P<attrs>.*)"
)
_END = re.compile(r" -- (?P<maintainer>.+? <.+?>)  (?P<date>.*)")
_CLOSES = re.compile(r"closes:\s*(?:bug)?#?\s?\d+(?:,\s?(?:bug)?#?\s?\d+)*", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")
_ITEM = re.compile(r"#?([\w.-]+)")


class ChangelogError(MinidebError, ValueError):
    """Malformed changelog text; the message starts with ``line N:``."""


@dataclass
class ChangelogEntry:
    """A single changelog stanza."""

    package: str
    version: Version
    distributions: list[str]
    urgency: str
    changes: str
    maintainer: str
    date: datetime
    attributes: dict[str, str] = field(default_factory=dict)
    bugs: list[str] = field(default_factory=list)
    keywords: dict[str, list[str]] = field(default_factory=dict)


def _extract(changes: str, keywords: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    bugs: list[str] = []
    for match in _CLOSES.finditer(changes):
        bugs.extend(_DIGITS.findall(match.group(0)))
    extra: dict[str, list[str]] = {}
    for kw in keywords:
        # a whole word (not "notLP:" or "X-LP:"); the list stops before "Other:"
        pattern = re.compile(
            rf"(?<![\w-]){re.escape(kw)}:\s*#?[\w.-]+(?:,\s*#?[\w.-]+\b(?!:))*", re.IGNORECASE
        )
        for match in pattern.finditer(changes):
            value = match.group(0).split(":", 1)[1]
            extra.setdefault(kw, []).extend(_ITEM.findall(value))
    return bugs, extra


def parse(text: str, keywords: Iterable[str] = ()) -> list[ChangelogEntry]:
    """Parse changelog *text* into entries, newest first.

    *keywords* names extra ``Keyword: item, item`` markers (like ``LP``)
    to collect from the change text into ``ChangelogEntry.keywords``;
    bugs listed after ``Closes:`` are always collected into ``bugs``.
    Raises :class:`ChangelogError` (a ``ValueError``, with a line number)
    on malformed input.
    """
    kws = list(keywords)
    # lines end at LF only, like dpkg-parsechangelog; one CR before it is dropped
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    entries: list[ChangelogEntry] = []
    i = 0
    n = len(lines)

    def skip_blank() -> None:
        nonlocal i
        while i < n and not lines[i].strip():
            i += 1

    skip_blank()
    while i < n:
        start = _START.match(lines[i])
        if not start:
            raise ChangelogError(f"line {i + 1}: invalid first line: {lines[i]!r}")
        attrs: dict[str, str] = {}
        # "urgency=medium, binary-only=yes": commas and/or blanks separate
        for attr in (a for a in re.split(r"[,\s]+", start.group("attrs")) if a):
            key, sep, value = attr.partition("=")
            if not sep or not key:
                raise ChangelogError(f"line {i + 1}: invalid attribute: {attr!r}")
            attrs[key] = value
        if "urgency" not in attrs:
            raise ChangelogError(f"line {i + 1}: missing urgency attribute")
        try:
            version = Version(start.group("version"))
        except ValueError as exc:
            raise ChangelogError(f"line {i + 1}: invalid version: {exc}") from exc
        i += 1
        body: list[str] = []
        while i < n and (lines[i].startswith("  ") or not lines[i].strip()):
            body.append(lines[i][2:] if lines[i].strip() else "")
            i += 1
        if i >= n:
            raise ChangelogError(f"line {i}: entry for {start.group('package')} has no trailer")
        end = _END.match(lines[i])
        if not end:
            raise ChangelogError(f"line {i + 1}: invalid trailer line: {lines[i]!r}")
        try:
            date = parsedate_to_datetime(end.group("date"))
        except (TypeError, ValueError) as exc:
            raise ChangelogError(f"line {i + 1}: invalid date: {end.group('date')!r}") from exc
        changes = "\n".join(body).strip("\n")
        bugs, extra = _extract(changes, kws)
        entries.append(
            ChangelogEntry(
                package=start.group("package"),
                version=version,
                distributions=start.group("dists").split(),
                urgency=attrs["urgency"],
                changes=changes,
                maintainer=end.group("maintainer"),
                date=date,
                attributes=attrs,
                bugs=bugs,
                keywords=extra,
            )
        )
        i += 1
        skip_blank()
    return entries
