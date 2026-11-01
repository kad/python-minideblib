# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Offline file-system APT repository builder for tests.

The default layout mirrors a repository whose suite name contains slashes
(``stable/slash-test/1.0``): the suite contains slashes and is used verbatim
under ``dists/``; ``Release`` has ``Codename`` equal to the suite and no
``Suite`` field; one component; no ``Acquire-By-Hash``; MD5Sum/SHA1/SHA256/
SHA512 blocks; only ``.gz`` (zero-padded to a multiple of 10240 bytes) and
uncompressed indices; ``Sources`` is empty; ``InRelease`` is clear-signed
(with a fake signature that cannot be verified). With ``dep11=True`` each
component also gets DEP-11 metadata (``dep11/Components-<arch>.yml.gz`` and
``dep11/icons-48x48.tar.gz``), written as plain text so PyYAML is not needed.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import lzma
import posixpath
import tarfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

ALGOS = ("MD5Sum", "SHA1", "SHA256", "SHA512")
_HASHLIB = {"MD5Sum": "md5", "SHA1": "sha1", "SHA256": "sha256", "SHA512": "sha512"}

FAKE_SIGNATURE = (
    "-----BEGIN PGP SIGNATURE-----\n"
    "\n"
    "iQIzBAEBCgAdFiEEZmFrZWZha2VmYWtlZmFrZWZha2VmYWtlBQJnAAAAAAoJEGZh\n"
    "a2VmYWtlZmFrZQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
    "=fake\n"
    "-----END PGP SIGNATURE-----\n"
)

Paragraphs = str | bytes | Sequence[Mapping[str, str]] | None


def default_packages(arch: str) -> list[dict[str, str]]:
    return [
        {
            "Package": "sample-pkg",
            "Version": "0.4-1",
            "Architecture": arch,
            "Maintainer": "Test <test@example.com>",
            "Filename": f"pool/main/s/sample-pkg/sample-pkg_0.4-1_{arch}.deb",
            "Size": "1234",
            "SHA256": "0" * 64,
            "Description": "sample package",
        }
    ]


def render(paragraphs: Paragraphs, arch: str | None = None) -> bytes:
    """Render paragraphs (raw text/bytes or a list of dicts) to index bytes."""
    if paragraphs is None:
        return b""
    if isinstance(paragraphs, bytes):
        return paragraphs
    if isinstance(paragraphs, str):
        return paragraphs.encode()
    out = []
    for para in paragraphs:
        fields = dict(para)
        if arch is not None and "Package" in fields:
            fields.setdefault("Architecture", arch)
        out.append("".join(f"{k}: {v}\n" for k, v in fields.items()))
    return "\n".join(out).encode()


#: DEP-11 ``Components-<arch>.yml``: header plus two components.
DEP11_YAML = """\
---
File: DEP-11
Version: '0.16'
Origin: example-main
MediaBaseUrl: https://media.example.com/
Priority: 0
---
Type: desktop-application
ID: org.example.Hello
Package: hello
Name:
  C: Hello
  de: Hallo
Summary:
  C: Greets the world
  de: Grüßt die Welt
Description:
  C: <p>Prints a friendly greeting.</p>
Categories:
- Utility
Icon:
  cached:
  - name: hello_org.example.Hello.png
    width: 48
    height: 48
  stock: hello
---
Type: console-application
ID: org.example.sample-tool
Package: sample-tool
Name:
  C: Sample Tool
Summary:
  C: A command line tool
Provides:
  binaries:
  - sample-tool
  mediatypes:
  - text/x-sample
Categories:
- Development
- ConsoleOnly
Releases:
- version: '1.0'
  type: stable
  unix-timestamp: 1767225600
  date: 2026-01-01
Url:
  homepage: https://www.example.com/sample-tool
"""


def icons_tar(names: Iterable[str] = ("hello_org.example.Hello.png",)) -> bytes:
    """A tiny valid ``icons-<size>.tar.gz`` holding fake PNG files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in names:
            body = b"\x89PNG\r\n\x1a\nfake"
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


DSC_NAME = "sample-pkg_1.0-1.dsc"
DSC_VERSION = "1.0-1"
SRC_FILES: dict[str, bytes] = {
    "sample-pkg_1.0.orig.tar.xz": b"sample upstream tarball\n",
    "sample-pkg_1.0-1.debian.tar.xz": b"sample debian tarball\n",
}


def _hex(algo: str, data: bytes) -> str:
    return hashlib.new(algo, data, usedforsecurity=False).hexdigest()


def _rows(algo: str, files: Mapping[str, bytes]) -> str:
    """Hash lines, each preceded by a newline (empty first line of the field)."""
    return "".join(f"\n {_hex(algo, d)} {len(d)} {n}" for n, d in files.items())


def _hash_block(field: str, algo: str, files: Mapping[str, bytes]) -> str:
    return f"{field}: {_rows(algo, files)}\n"


def sample_source_files() -> dict[str, bytes]:
    """File name -> content of the sample source package (``.dsc`` plus tarballs).

    The ``.dsc`` is clear-signed with a fake signature and lists the tarballs
    in ``SRC_FILES`` with correct hashes.
    """
    text = (
        "Format: 3.0 (quilt)\n"
        "Source: sample-pkg\n"
        "Binary: sample-pkg, sample-pkg-doc,\n"
        " sample-pkg-dev\n"
        "Architecture: any all\n"
        f"Version: {DSC_VERSION}\n"
        "Maintainer: Test <test@example.com>\n"
        "Standards-Version: 4.7.0\n"
        "Build-Depends: debhelper-compat (= 13)\n"
        "Package-List:\n"
        " sample-pkg deb utils optional arch=any\n"
        + _hash_block("Checksums-Sha1", "sha1", SRC_FILES)
        + _hash_block("Checksums-Sha256", "sha256", SRC_FILES)
        + _hash_block("Files", "md5", SRC_FILES)
    )
    return {DSC_NAME: clearsign(text).encode(), **SRC_FILES}


def sample_source_stanza(directory: str) -> dict[str, str]:
    """A ``Sources`` stanza for :func:`sample_source_files` under *directory*."""
    files = sample_source_files()
    return {
        "Package": "sample-pkg",
        "Binary": "sample-pkg, sample-pkg-doc, sample-pkg-dev",
        "Version": DSC_VERSION,
        "Maintainer": "Test <test@example.com>",
        "Architecture": "any all",
        "Directory": directory,
        "Checksums-Sha1": _rows("sha1", files),
        "Checksums-Sha256": _rows("sha256", files),
        "Files": _rows("md5", files),
    }


def gz(data: bytes, pad: bool) -> bytes:
    out = gzip.compress(data, mtime=0)
    if pad and len(out) % 10240:
        out += b"\0" * (10240 - len(out) % 10240)
    return out


def clearsign(text: str) -> str:
    """Wrap *text* in clear-signature armor with a fake signature block."""
    body = "".join(
        f"- {line}\n" if line.startswith("-") else f"{line}\n" for line in text.splitlines()
    )
    return f"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n{body}{FAKE_SIGNATURE}"


def _digest(algo: str, data: bytes) -> str:
    return hashlib.new(_HASHLIB[algo], data, usedforsecurity=False).hexdigest()


def make_repo(
    root: Path,
    suite: str = "stable/slash-test/1.0",
    comps: Iterable[str] = ("unified",),
    arches: Iterable[str] = ("amd64", "i386"),
    packages: Paragraphs = None,
    contents: bool | str | bytes = False,
    by_hash: bool = False,
    flat: bool = False,
    pad_gz: bool = True,
    *,
    sources: Paragraphs = None,
    xz: bool = False,
    uncompressed: bool = True,
    extra_files: dict[str, bytes] | None = None,
    hashes: Sequence[str] = ALGOS,
    valid_until: str | None = None,
    extra_fields: Mapping[str, str] | None = None,
    inrelease: bool = True,
    release: bool = True,
    dep11: bool | str | bytes = False,
    dsc: bool = False,
) -> str:
    """Write a repository under *root* and return its ``file://`` base URL.

    ``packages``/``sources`` are raw text or lists of dict paragraphs (default:
    one sample package per architecture; ``Sources`` empty). ``extra_files``
    maps paths relative to the suite directory to content; they are written
    and listed in ``Release``. With ``flat`` the indices live directly in
    ``root/<suite>`` (``suite`` is ``./`` unless it ends with ``/``) and the
    caller uses that suite string. With ``by_hash`` every index is also stored
    under ``<dir>/by-hash/<ALGO>/<hash>`` and ``Acquire-By-Hash: yes`` is set.
    ``dep11`` (true or custom YAML text) adds ``<comp>/dep11/Components-<arch>.yml.gz``
    (default :data:`DEP11_YAML`) and ``<comp>/dep11/icons-48x48.tar.gz`` per component.
    With ``dsc`` a ``sample-pkg`` source stanza is appended to ``Sources`` and the
    files of :func:`sample_source_files` are written under ``pool/<comp>/s/sample-pkg``
    (flat: ``pool/s/sample-pkg`` next to the indices).
    """
    root = Path(root)
    comps = list(comps)
    arches = list(arches)
    if flat:
        sub = posixpath.normpath(suite) if suite.endswith("/") else "."
        dist = root if sub == "." else root / sub
    else:
        dist = root / "dists" / suite
    dist.mkdir(parents=True, exist_ok=True)

    files: dict[str, bytes] = {}

    def add_index(path: str, data: bytes) -> None:
        if uncompressed:
            files[path] = data
        files[path + ".gz"] = gz(data, pad_gz)
        if xz:
            files[path + ".xz"] = lzma.compress(data)

    def src_for(pool: str) -> bytes:
        base = render(sources)
        if not dsc:
            return base
        stanza = render([sample_source_stanza(pool)])
        return base + b"\n" + stanza if base else stanza

    pool_files: dict[str, bytes] = {}  # relative to the repository base, not in Release

    def add_pool(pool: str) -> str:
        for name, data in sample_source_files().items():
            pool_files[f"{pool}/{name}"] = data
        return pool

    if flat:
        pkgs = packages if packages is not None else default_packages(arches[0])
        add_index("Packages", render(pkgs, arches[0]))
        add_index("Sources", src_for(add_pool("pool/s/sample-pkg") if dsc else ""))
    else:
        for comp in comps:
            for arch in arches:
                pkgs = packages if packages is not None else default_packages(arch)
                add_index(f"{comp}/binary-{arch}/Packages", render(pkgs, arch))
                if contents is not False:
                    body = contents if not isinstance(contents, bool) else _contents(arch)
                    if isinstance(body, str):
                        body = body.encode()
                    files[f"{comp}/Contents-{arch}.gz"] = gz(body, False)
            pool = add_pool(f"pool/{comp}/s/sample-pkg") if dsc else ""
            add_index(f"{comp}/source/Sources", src_for(pool))
            if dep11 is not False:
                yml = DEP11_YAML if dep11 is True else dep11
                if isinstance(yml, str):
                    yml = yml.encode()
                for arch in arches:
                    files[f"{comp}/dep11/Components-{arch}.yml.gz"] = gz(yml, False)
                files[f"{comp}/dep11/icons-48x48.tar.gz"] = icons_tar()
    files.update(extra_files or {})
    for rel, data in pool_files.items():
        target = (dist if flat else root) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    for rel, data in files.items():
        target = dist / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if by_hash:
            for algo in hashes:
                bh = dist / posixpath.dirname(rel) / "by-hash" / algo / _digest(algo, data)
                bh.parent.mkdir(parents=True, exist_ok=True)
                bh.write_bytes(data)

    fields: dict[str, str] = {
        "Origin": "Example",
        "Label": "Example",
        "Codename": suite,
        "Date": "Sat, 01 Nov 2026 00:00:00 UTC",
    }
    if valid_until:
        fields["Valid-Until"] = valid_until
    fields["Architectures"] = " ".join(arches)
    if not flat:
        fields["Components"] = " ".join(comps)
    fields["Description"] = "Test repository"
    if by_hash:
        fields["Acquire-By-Hash"] = "yes"
    fields.update(extra_fields or {})
    text = "".join(f"{k}: {v}\n" for k, v in fields.items())
    width = max((len(str(len(d))) for d in files.values()), default=1)
    for algo in hashes:
        text += f"{algo}:\n"
        for rel, data in sorted(files.items()):
            text += f" {_digest(algo, data)} {len(data):>{width}} {rel}\n"

    if release:
        (dist / "Release").write_text(text)
    if inrelease:
        (dist / "InRelease").write_text(clearsign(text))
    return root.resolve().as_uri()


def _contents(arch: str) -> bytes:
    return (
        f"usr/bin/sample-{arch}                                     utils/sample-pkg\n"
        "usr/share/doc/sample-pkg/copyright               doc/sample-pkg\n"
    ).encode()
