# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Tiny stdlib-only ``.deb`` builder shared by examples 05 and 06 (not an example itself)."""

from __future__ import annotations

import io
import tarfile

CONTROL = (
    "Package: minideb-sample\nVersion: 1.0-1\nArchitecture: all\n"
    "Maintainer: Example <user@example.com>\nDepends: libc6 (>= 2.36), sh\n"
    "Description: sample package built by the minideblib examples\n"
)
FILES = {
    "usr/bin/minideb-sample": b"#!/bin/sh\necho hello\n",
    "usr/share/doc/minideb-sample/copyright": b"Public domain.\n",
}


def _tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def build_deb(files: dict[str, bytes] | None = None, control: str = CONTROL) -> bytes:
    """Return the bytes of a ``.deb`` holding *files* (names are used verbatim)."""
    members = [
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", _tar_gz({"control": control.encode()})),
        ("data.tar.gz", _tar_gz(FILES if files is None else files)),
    ]
    out = b"!<arch>\n"
    for name, data in members:
        out += f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
        out += data + (b"\n" if len(data) % 2 else b"")
    return out
