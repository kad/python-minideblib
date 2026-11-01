# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Minimal reader for ar archives as used by .deb files."""

from __future__ import annotations

from collections.abc import Iterator
from typing import NamedTuple

from minideblib.errors import MinidebError

__all__ = ["MAGIC", "ArError", "ArMember", "iter_members"]

MAGIC = b"!<arch>\n"
_HDR = 60


class ArError(MinidebError, ValueError):
    """Malformed ar archive."""


class ArMember(NamedTuple):
    """One member of an ar archive: its name (without the trailing ``/``) and data."""

    name: str
    data: bytes


def iter_members(data: bytes) -> Iterator[ArMember]:
    if not data.startswith(MAGIC):
        raise ArError("bad ar magic")
    pos = len(MAGIC)
    longnames = b""
    while pos < len(data):
        hdr = data[pos : pos + _HDR]
        if len(hdr) < _HDR:
            raise ArError("truncated ar header")
        if hdr[58:60] != b"`\n":
            raise ArError("bad ar header terminator")
        size_field = hdr[48:58].strip()
        if not (size_field.isascii() and size_field.isdigit()):
            raise ArError("bad ar member size")
        size = int(size_field)
        raw_name = hdr[:16].decode("latin-1").rstrip()
        pos += _HDR
        body = data[pos : pos + size]
        if len(body) < size:
            raise ArError("truncated ar member")
        pos += size
        if size & 1:
            # an odd-sized member is followed by one padding byte (its value is not checked)
            if pos >= len(data):
                raise ArError("missing ar padding byte")
            pos += 1
        if raw_name == "//":
            longnames = body
            continue
        if raw_name.startswith("/") and raw_name[1:].isdigit():
            start = int(raw_name[1:])
            end = longnames.find(b"\n", start)
            if start > len(longnames) or end < 0:
                raise ArError("bad GNU long name reference")
            raw_name = longnames[start:end].decode("latin-1")
        name = raw_name.removesuffix("/")
        yield ArMember(name, body)
