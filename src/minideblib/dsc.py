# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Debian source package control files (``.dsc``).

A ``.dsc`` is one deb822 paragraph, optionally wrapped in an OpenPGP
clear-signed block. The signature is **never verified** here: the armor is
stripped, everything after the signature is ignored, and the paragraph is
parsed. Integrity of the referenced source files is checked by
:meth:`Dsc.verify`, but nothing authenticates the ``.dsc`` itself.

A missing ``Format`` field is treated as ``1.0``.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path

from minideblib.checksums import ALGORITHMS, ChecksumError, parse_multiline_hashes, verify_file
from minideblib.control import Paragraph, decode_text, parse_paragraphs, strip_clearsign
from minideblib.errors import MinidebError
from minideblib.version import Version

log = logging.getLogger(__name__)

__all__ = ["Dsc", "DscError", "DscFile", "PackageListEntry"]

#: hash algorithms in the order their fields are read (weakest first); the
#: hashlib name doubles as the :class:`DscFile` attribute name
_FIELDS = tuple(reversed(ALGORITHMS))
_HEX = re.compile(r"[0-9a-f]+")
_BUILD_FIELDS = (
    "Build-Depends",
    "Build-Depends-Indep",
    "Build-Depends-Arch",
    "Build-Conflicts",
    "Build-Conflicts-Indep",
    "Build-Conflicts-Arch",
)


class DscError(MinidebError, ValueError):
    """Raised on a malformed ``.dsc`` or a failed source file check."""


@dataclass(frozen=True)
class DscFile:
    """A source file listed in a ``.dsc``, merged across the hash fields."""

    name: str
    size: int
    md5: str | None = None
    sha1: str | None = None
    sha256: str | None = None
    sha512: str | None = None

    def hashes(self) -> dict[str, str]:
        """Hex digests that are present, keyed by hashlib name."""
        pairs = ((alg.name, getattr(self, alg.name)) for alg in _FIELDS)
        return {name: value for name, value in pairs if value is not None}


@dataclass(frozen=True)
class PackageListEntry:
    """One line of ``Package-List``: ``name type section priority [key=value ...]``."""

    name: str
    type: str
    section: str
    priority: str
    extra: dict[str, str] = field(default_factory=dict)


def split_people(value: str) -> list[str]:
    """Split a comma separated list of people.

    Commas inside ``<...>`` or double quotes do not separate people.
    """
    out: list[str] = []
    buf: list[str] = []
    in_quote = False
    angle = 0
    for ch in value:
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote:
            if ch == "<":
                angle += 1
            elif ch == ">" and angle:
                angle -= 1
            elif ch == "," and not angle:
                out.append("".join(buf))
                buf = []
                continue
        buf.append(ch)
    out.append("".join(buf))
    return [p.strip() for p in out if p.strip()]


def _check_name(name: str, where: str) -> None:
    if not name or name.startswith(".") or any(c in name for c in ("/", "\\", "\0")):
        raise DscError(f"Bad file name {name!r} in {where} field")


def _merge_files(fields: Paragraph) -> list[DscFile]:
    sizes: dict[str, int] = {}
    digests: dict[str, dict[str, str]] = {}
    seen_any = False
    for alg in _FIELDS:
        attr, fname, length = alg.name, alg.sources_field, alg.hex_length
        if fname not in fields:
            continue
        seen_any = True
        try:
            rows = parse_multiline_hashes(fields[fname])
        except ChecksumError as exc:
            raise DscError(f"Bad {fname} field: {exc}") from exc
        names: set[str] = set()
        for digest, size, name in rows:
            _check_name(name, fname)
            if len(digest) != length or not _HEX.fullmatch(digest):
                raise DscError(f"Bad {attr} value {digest!r} for {name} in {fname} field")
            if size < 0:
                raise DscError(f"Bad size {size} for {name} in {fname} field")
            if name in names:
                raise DscError(f"{name} listed twice in {fname} field")
            names.add(name)
            if sizes.setdefault(name, size) != size:
                raise DscError(f"size of {name} differs between fields ({fname})")
            digests.setdefault(name, {})[attr] = digest
    if not seen_any:
        raise DscError("missing Files field")
    if not sizes:
        raise DscError("no files listed")
    return [DscFile(name, size, **digests[name]) for name, size in sizes.items()]


def _parse_package_list(fields: Paragraph) -> list[PackageListEntry]:
    out = []
    for line in fields.get("Package-List", "").splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) < 4:
            raise DscError(f"Bad Package-List line {line.strip()!r}")
        extra = {}
        for item in parts[4:]:
            key, _, value = item.partition("=")
            extra[key] = value
        out.append(PackageListEntry(parts[0], parts[1], parts[2], parts[3], extra))
    return out


class Dsc:
    """A parsed ``.dsc`` file (the signature, if any, is not verified)."""

    def __init__(self, fields: Paragraph) -> None:
        self.fields = fields
        for required in ("Source", "Version"):
            if not fields.get(required, "").strip():
                raise DscError(f"missing {required} field")
        self.source: str = fields["Source"].strip()
        try:
            self.version: Version = Version(fields["Version"].strip())
        except ValueError as exc:
            raise DscError(f"Bad Version field: {exc}") from exc
        self.format: str = fields.get("Format", "").strip() or "1.0"
        self.files: list[DscFile] = _merge_files(fields)
        self.binaries: list[str] = fields.get_list("Binary")
        self.architectures: list[str] = fields.get_list("Architecture", sep=None)
        self.maintainer: str = fields.get_folded("Maintainer")
        self.uploaders: list[str] = split_people(fields.get_folded("Uploaders"))
        self.standards_version: str | None = fields.get_folded("Standards-Version") or None
        self.homepage: str | None = fields.get_folded("Homepage") or None
        self.vcs: dict[str, str] = {
            name[4:].lower(): fields.get_folded(name)
            for name in fields.fields_in_order()
            if name.lower().startswith("vcs-")
        }
        wanted: dict[str, str] = {n.lower(): n for n in _BUILD_FIELDS}
        self.build_depends: dict[str, str] = {
            wanted[name.lower()]: fields.get_folded(name)
            for name in fields.fields_in_order()
            if name.lower() in wanted
        }
        self.package_list: list[PackageListEntry] = _parse_package_list(fields)
        self.is_native: bool = self.format == "3.0 (native)" or (
            self.format == "1.0" and self.diff() is None
        )

    @classmethod
    def parse(cls, data: str | bytes) -> Dsc:
        """Parse ``.dsc`` text (clear-signed or not). The signature is ignored."""
        text = decode_text(data, ".dsc") if isinstance(data, bytes) else data
        text = text.lstrip("\r\n")
        try:
            para = next(parse_paragraphs(strip_clearsign(text)), None)
        except ValueError as exc:
            raise DscError(str(exc)) from exc
        if para is None:
            raise DscError("no control data")
        return cls(para)

    @classmethod
    def load(cls, path: str | PathLike[str]) -> Dsc:
        """Read and parse the ``.dsc`` at *path*."""
        p = Path(path)
        try:
            return cls.parse(p.read_bytes())
        except DscError as exc:
            raise DscError(f"{p}: {exc}") from exc

    def _matching(self, pattern: str) -> list[DscFile]:
        return [f for f in self.files if fnmatch.fnmatchcase(f.name, pattern)]

    def orig_tarballs(self) -> list[DscFile]:
        """``*.orig.tar.*`` and ``*.orig-<component>.tar.*`` files."""
        # linear: the component may not contain "." (dpkg allows [A-Za-z0-9-])
        return [f for f in self.files if re.search(r"\.orig(-[^/.]+)?\.tar\.[^./]+$", f.name)]

    def debian_tarball(self) -> DscFile | None:
        """The ``*.debian.tar.*`` file, if listed."""
        found = self._matching("*.debian.tar.*")
        return found[0] if found else None

    def diff(self) -> DscFile | None:
        """The ``*.diff.gz`` file of a ``1.0`` source package, if listed."""
        found = self._matching("*.diff.gz")
        return found[0] if found else None

    def verify(self, directory: str | PathLike[str]) -> None:
        """Check size and every listed hash of the files found in *directory*.

        One read pass per file computes all algorithms present. Raises
        :class:`DscError` naming the file (and algorithm) on a missing file,
        non-regular file (symlinks included), or size or hash mismatch.
        """
        base = Path(directory)
        for f in self.files:
            log.debug("verifying %s (%d bytes, %s)", f.name, f.size, "/".join(f.hashes()))
            verify_file(str(base / f.name), f.size, f.hashes(), DscError)
        log.info(".dsc verified: %d files in %s", len(self.files), base)
