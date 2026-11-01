# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>
"""Debian package version parsing and comparison.

The algorithm follows Debian Policy Manual section 5.6.12; this module is an
independent reimplementation, written without the earlier MIT-licensed code.
"""

from __future__ import annotations

import re
from functools import total_ordering
from itertools import zip_longest

from minideblib.errors import MinidebError

__all__ = ["Version", "VersionError", "compare"]


class VersionError(MinidebError, ValueError):
    """Raised when a string is not a valid Debian version."""


_EPOCH_RE = re.compile(r"[0-9]+\Z", re.ASCII)
_UPSTREAM_RE = re.compile(r"[A-Za-z0-9.+:~-]+\Z", re.ASCII)
_REVISION_RE = re.compile(r"[A-Za-z0-9.+~]+\Z", re.ASCII)
_PART_RE = re.compile(r"([^0-9]*)([0-9]*)", re.ASCII)


def _char_weight(ch: str) -> int:
    """Sort weight of one non-digit character: ``~`` < end < letters < others."""
    if ch == "~":
        return -1
    if ch.isalpha():
        return ord(ch)
    return ord(ch) + 256


def _text_key(text: str) -> tuple[int, ...]:
    # A trailing 0 stands for "end of text", which sorts above ``~`` only.
    return (*map(_char_weight, text), 0)


#: Longest epoch accepted (dpkg stores it in an int).
MAX_EPOCH_DIGITS = 10
#: longest part of an invalid version string quoted in an error message
_QUOTE_LIMIT = 40

_Digits = tuple[int, str]
_Fragment = tuple[tuple[tuple[int, ...], _Digits], ...]
_NEUTRAL: tuple[tuple[int, ...], _Digits] = ((0,), (0, ""))


def _digits_key(digits: str) -> _Digits:
    """Order digit runs numerically without ``int()`` (no 4300-digit limit).

    Without leading zeros a longer run is a bigger number; equal lengths
    compare as strings. ``""`` and ``"0"`` both give ``(0, "")``.
    """
    d = digits.lstrip("0")
    return (len(d), d)


def _fragment_key(s: str) -> _Fragment:
    """Comparison key for an upstream or revision string.

    Split into (non-digit run, digit run) pairs; digit runs are compared as
    numbers (empty = 0) by :func:`_digits_key`.  The end-of-text marker in ``_text_key`` is lower
    than any letter or symbol but higher than ``~``, as the policy requires.
    Trailing pairs equal to ``_NEUTRAL`` (empty text, zero) are dropped so
    that equal versions have identical keys (needed for hashing); comparison
    must use ``_cmp_fragments``, which pads the shorter key with ``_NEUTRAL``.
    """
    pairs = []
    pos = 0
    while pos < len(s):
        m = _PART_RE.match(s, pos)
        assert m is not None
        text, digits = m.groups()
        pairs.append((_text_key(text), _digits_key(digits)))
        pos = m.end()
    while pairs and pairs[-1] == _NEUTRAL:
        pairs.pop()
    return tuple(pairs)


def _cmp_fragments(a: _Fragment, b: _Fragment) -> int:
    for x, y in zip_longest(a, b, fillvalue=_NEUTRAL):
        if x != y:
            return -1 if x < y else 1
    return 0


def _quote(s: str) -> str:
    """``repr`` of at most :data:`_QUOTE_LIMIT` characters of *s*, ``...`` if cut."""
    if len(s) <= _QUOTE_LIMIT:
        return repr(s)
    return f"{s[:_QUOTE_LIMIT]!r}..."


@total_ordering
class Version:
    """An immutable, hashable Debian version: ``[epoch:]upstream[-revision]``.

    Parsing is permissive by default: like dpkg, which only warns, an
    upstream version that does not start with a digit (``a1``) is accepted.
    With ``strict=True`` such a version raises :class:`VersionError`.
    *strict* only affects validation; ordering, hashing and pickling are the
    same in both modes.
    """

    __slots__ = ("_epoch", "_key", "_revision", "_upstream")

    def __init__(self, s: str, *, strict: bool = False) -> None:
        if not isinstance(s, str):
            raise VersionError(f"version must be a string, not {type(s).__name__}")
        epoch = 0
        rest = s
        head, sep, tail = s.partition(":")
        if sep:
            if not _EPOCH_RE.match(head):
                raise VersionError(f"invalid epoch in version {_quote(s)}")
            if len(head) > MAX_EPOCH_DIGITS:
                raise VersionError(
                    f"epoch longer than {MAX_EPOCH_DIGITS} digits in version {_quote(s)}"
                )
            epoch = int(head)
            rest = tail
        upstream, sep, revision = rest.rpartition("-")
        if not sep:
            upstream, revision = rest, ""
        if not upstream or not _UPSTREAM_RE.match(upstream):
            raise VersionError(f"invalid upstream version in {_quote(s)}")
        if strict and not upstream[0].isdigit():
            raise VersionError(f"upstream version does not start with a digit in {_quote(s)}")
        if sep and not _REVISION_RE.match(revision):
            raise VersionError(f"invalid revision in version {_quote(s)}")
        object.__setattr__(self, "_epoch", epoch)
        object.__setattr__(self, "_upstream", upstream)
        object.__setattr__(self, "_revision", revision)
        object.__setattr__(self, "_key", (epoch, _fragment_key(upstream), _fragment_key(revision)))

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def upstream(self) -> str:
        return self._upstream

    @property
    def revision(self) -> str:
        return self._revision

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __reduce__(self) -> tuple[type[Version], tuple[str]]:
        # copy, deepcopy and pickle rebuild from the string (slots, immutable)
        return (Version, (str(self),))

    def __str__(self) -> str:
        out = f"{self._epoch}:" if self._epoch else ""
        out += self._upstream
        if self._revision:
            out += f"-{self._revision}"
        return out

    def __repr__(self) -> str:
        return f"Version({str(self)!r})"

    def _cmp(self, other: Version) -> int:
        (e1, u1, r1), (e2, u2, r2) = self._key, other._key
        if e1 != e2:
            return -1 if e1 < e2 else 1
        return _cmp_fragments(u1, u2) or _cmp_fragments(r1, r2)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._cmp(other) == 0

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._cmp(other) < 0

    def __hash__(self) -> int:
        return hash(self._key)


def compare(a: str, b: str, *, strict: bool = False) -> int:
    """Compare two version strings; return -1, 0 or 1.

    *strict* is passed to :class:`Version` for both strings.
    """
    return Version(a, strict=strict)._cmp(Version(b, strict=strict))
