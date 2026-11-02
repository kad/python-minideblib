# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Debian control data (deb822): paragraphs of ``Name: value`` fields.

Archive indices (``Packages``, ``Sources``), ``Release``/``InRelease``,
``.dsc`` files, the ``control`` member of a ``.deb`` and deb822 ``.sources``
files all use this format: paragraphs separated by blank lines, each an
ordered set of fields whose values may continue on lines that start with a
space or a tab. Lines starting with ``#`` are comments.

:func:`parse_paragraphs` yields :class:`Paragraph` objects lazily from a
``str``, ``bytes`` or any iterable of ``str``/``bytes`` lines (text or binary
file objects). As in apt, lines end only at LF, for every kind of input; one
CR before an LF is dropped. A lone CR, Unicode separators such as U+2028 and
a form feed are ordinary characters. Text streams opened with universal
newlines (the default of :func:`open` in text mode) already turn a lone CR
into a line end, so pass binary streams or text opened with ``newline=""``
to keep that rule.
One leading byte order mark (U+FEFF) is ignored. Malformed input raises
:class:`ControlError` with a ``line N:`` message prefix.

Decoding differs between the input kinds:

* ``bytes`` given at once are decoded as a whole by :func:`decode_text`:
  strict UTF-8, or Latin-1 for the whole input (with one warning) when that
  fails;
* a byte stream is decoded line by line, so only the lines that are not valid
  UTF-8 fall back to Latin-1; at most one warning is logged per stream;
* a text stream is used as it is; its own decoding errors propagate.

:func:`strip_clearsign` removes OpenPGP clear-signature armor. It does not
verify the signature. :func:`parse_file` reads, decodes, unarmors and parses
a file by path.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator, MutableMapping
from itertools import takewhile
from typing import IO

from minideblib.errors import MinidebError

__all__ = [
    "ControlError",
    "Paragraph",
    "decode_text",
    "parse_file",
    "parse_paragraphs",
    "strip_clearsign",
]

log = logging.getLogger(__name__)

_BOM = "\ufeff"
_SIGNED_MARKER = "-----BEGIN PGP SIGNED MESSAGE-----"
_SIGNATURE_MARKER = "-----BEGIN PGP SIGNATURE-----"
#: longest excerpt of an offending line quoted in an error message
_QUOTE_LIMIT = 60


class ControlError(MinidebError, ValueError):
    """Malformed control data.

    ``lineno`` is the 1-based number of the offending line in the text that
    was parsed, or ``None`` when the error is not about one line. When it is
    set, the message starts with ``line N:``.
    """

    def __init__(self, message: str, lineno: int | None = None) -> None:
        self.lineno = lineno
        super().__init__(message if lineno is None else f"line {lineno}: {message}")


def _physical_lines(text: str) -> list[str]:
    """Split *text* at LF only, dropping one CR before each LF.

    A lone CR stays in the line; a final terminator adds no line.
    """
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def _fold(value: str) -> str:
    """Join the stripped, non-empty lines of *value* with single spaces."""
    return " ".join(part for part in (line.strip() for line in _physical_lines(value)) if part)


class Paragraph(MutableMapping[str, str]):
    """One deb822 paragraph: an ordered mapping with case-insensitive names.

    Each field keeps the spelling it was first inserted with; assigning to an
    existing name (in any case) replaces the value in place. Values are stored
    as given. A parsed multi-line value is the first-line text followed, for
    every continuation line, by ``"\\n"`` and that line verbatim.
    """

    def __init__(self, items: Iterable[tuple[str, str]] = ()) -> None:
        # key: name.lower(); value: (spelling first seen, field value)
        self._fields: dict[str, tuple[str, str]] = {}
        for name, value in items:
            self[name] = value

    def __getitem__(self, key: str) -> str:
        return self._fields[key.lower()][1]

    def __setitem__(self, key: str, value: str) -> None:
        folded = key.lower()
        current = self._fields.get(folded)
        self._fields[folded] = (key if current is None else current[0], value)

    def __delitem__(self, key: str) -> None:
        del self._fields[key.lower()]

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._fields

    def __iter__(self) -> Iterator[str]:
        return (name for name, _ in self._fields.values())

    def __len__(self) -> int:
        return len(self._fields)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({list(self.items())!r})"

    def get_folded(self, key: str, default: str = "") -> str:
        """Value of *key* on one line: lines stripped, empty ones dropped, joined by a space.

        Returns *default* only when the field is missing. Whitespace inside a
        line is kept and the `` .`` marker is not interpreted.
        """
        entry = self._fields.get(key.lower())
        if entry is None:
            return default
        return _fold(entry[1])

    def get_list(self, key: str, sep: str | None = ",") -> list[str]:
        """Items of a list field: the folded value split at *sep*, stripped, empties dropped.

        ``sep=None`` splits at runs of whitespace. A missing field gives ``[]``.
        """
        entry = self._fields.get(key.lower())
        if entry is None:
            return []
        return [item for item in (raw.strip() for raw in _fold(entry[1]).split(sep)) if item]

    def fields_in_order(self) -> list[str]:
        """Original-case field names in insertion order."""
        return list(self)

    def dump(self) -> str:
        """Control text of the paragraph: one ``Name: value`` entry per field.

        Empty values and values starting with a line break follow the colon
        directly. Every field ends with one ``"\\n"``; no blank line follows.
        """
        out: list[str] = []
        for name, value in self._fields.values():
            out.append(name)
            out.append(":" if not value or value[0] == "\n" else ": ")
            out.append(value)
            out.append("\n")
        return "".join(out)


def decode_text(data: bytes, source: str = "") -> str:
    """Decode *data* as strict UTF-8, or as Latin-1 with a warning if that fails.

    A UTF-8 byte order mark is kept as U+FEFF. *source* names the data in the
    warning. Never raises for ``bytes``.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        log.warning("decoding %s as latin-1 (invalid UTF-8)", source or "<unnamed input>")
        return data.decode("latin-1")


def _stream_lines(stream: Iterable[str] | Iterable[bytes]) -> Iterator[str]:
    """Physical lines of an iterable of ``str``/``bytes`` items.

    Every item is used as it is yielded and ends a line: its trailing LF or
    CRLF is removed, an LF inside it splits it further (a lone CR does not),
    and an empty item is one empty line. A text stream opened with universal
    newlines has already split at lone CRs before this sees the lines.
    """
    label = getattr(stream, "name", None)
    warned = False
    for chunk in stream:
        if isinstance(chunk, bytes):
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError:
                if not warned:
                    log.warning(
                        "%s is not valid UTF-8, lines decoded as latin-1",
                        label if isinstance(label, str) else "control stream",
                    )
                    warned = True
                text = chunk.decode("latin-1")
        else:
            text = chunk
        yield from _physical_lines(text) or [""]


def _parse_lines(lines: Iterable[str]) -> Iterator[Paragraph]:
    """Build paragraphs from physical lines (terminators already removed)."""
    para: Paragraph | None = None
    # name of the open field and the pieces of its value
    name: str | None = None
    pieces: list[str] = []
    first = True
    for lineno, line in enumerate(lines, 1):
        if first:
            first = False
            if line.startswith(_BOM):
                line = line[1:]
        if line.startswith("#"):
            continue
        if not line or line.isspace():
            if para is not None:
                if name is not None:
                    para[name] = "\n".join(pieces)
                yield para
                para, name, pieces = None, None, []
            continue
        if line[0] in " \t":
            if name is None:
                raise ControlError("continuation line outside of a field", lineno)
            pieces.append(line)
            continue
        head, colon, rest = line.partition(":")
        if not colon:
            quote = line if len(line) <= _QUOTE_LIMIT else line[:_QUOTE_LIMIT] + "..."
            raise ControlError(f"expected 'Name: value', got {quote!r}", lineno)
        field = head.strip()
        if not field:
            raise ControlError("empty field name", lineno)
        if para is None:
            para = Paragraph()
        elif name is not None:
            para[name] = "\n".join(pieces)
        name, pieces = field, [rest.strip()]
    if para is not None:
        if name is not None:
            para[name] = "\n".join(pieces)
        yield para


def parse_paragraphs(
    text: str | bytes | IO[str] | IO[bytes] | Iterable[str] | Iterable[bytes],
) -> Iterator[Paragraph]:
    """Lazily yield the paragraphs of deb822 control data.

    *text* is a ``str``, ``bytes`` (decoded by :func:`decode_text`) or an
    iterable of ``str``/``bytes`` lines such as an open file. Comments and
    extra blank lines are skipped; a duplicate field name keeps the position
    of its first occurrence and the last value. Raises :class:`ControlError`
    (when iteration reaches the offending line) for a continuation line
    outside a field, a line without a colon or an empty field name.
    """
    if isinstance(text, bytes):
        text = decode_text(text)
    lines = _physical_lines(text) if isinstance(text, str) else _stream_lines(text)
    return _parse_lines(lines)


def strip_clearsign(text: str) -> str:
    """Remove OpenPGP clear-signature armor from *text*; nothing is verified.

    Text whose first line (after one optional BOM, with trailing whitespace
    removed) is not ``-----BEGIN PGP SIGNED MESSAGE-----`` is returned
    unchanged. Otherwise the armor headers up to the first blank line are
    dropped, the body runs up to the signature marker (everything from there
    on is discarded), ``"- "`` dash escapes are removed and every body line
    ends with LF.
    """
    body = text[1:] if text.startswith(_BOM) else text
    if not body.startswith(_SIGNED_MARKER):
        return text
    lines = _physical_lines(body)
    if lines[0].rstrip() != _SIGNED_MARKER:
        return text
    # the armor headers end at the first blank line
    header_end = next((i for i, line in enumerate(lines) if not line.strip()), len(lines))
    body_lines = takewhile(lambda line: line.rstrip() != _SIGNATURE_MARKER, lines[header_end + 1 :])
    return "".join(line.removeprefix("- ") + "\n" for line in body_lines)


def parse_file(path: str | os.PathLike[str]) -> list[Paragraph]:
    """Read, decode, unarmor and parse the control file at *path*.

    The Latin-1 fallback warning names the path. Error line numbers refer to
    the text after the clear-signature armor is removed. ``OSError`` from
    reading propagates.
    """
    name = os.fspath(path)
    with open(name, "rb") as fh:
        data = fh.read()
    return list(parse_paragraphs(strip_clearsign(decode_text(data, name))))
