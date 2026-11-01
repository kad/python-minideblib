# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Pure-Python .deb builder for tests (no dpkg-deb)."""

from __future__ import annotations

import bz2
import gzip
import hashlib
import importlib
import io
import lzma
import tarfile


def have_zstd() -> bool:
    try:
        importlib.import_module("compression.zstd")
    except ImportError:
        try:
            import zstandard  # noqa: F401
        except ImportError:
            return False
    return True


def zstd_compress(data: bytes) -> bytes:
    try:
        zstd = importlib.import_module("compression.zstd")

        return zstd.compress(data)
    except ImportError:
        import zstandard

        return zstandard.ZstdCompressor().compress(data)


def compress(data: bytes, comp: str) -> tuple[bytes, str]:
    """Return (compressed data, filename suffix)."""
    if comp == "gz":
        return gzip.compress(data), ".gz"
    if comp == "xz":
        return lzma.compress(data), ".xz"
    if comp == "bz2":
        return bz2.compress(data), ".bz2"
    if comp == "zst":
        return zstd_compress(data), ".zst"
    if comp in ("none", ""):
        return data, ""
    raise ValueError(comp)


def make_tar(files: dict[str, bytes], dirs: tuple[str, ...] = ("./",)) -> bytes:
    """Build an uncompressed tar. Names are prefixed with './' unless absolute."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for d in dirs:
            ti = tarfile.TarInfo(d)
            ti.type = tarfile.DIRTYPE
            ti.mode = 0o755
            tf.addfile(ti)
        for path, content in files.items():
            ti = tarfile.TarInfo(path if path.startswith("/") else "./" + path)
            ti.size = len(content)
            ti.mode = 0o755 if "/bin/" in path else 0o644
            ti.mtime = 1700000000
            tf.addfile(ti, io.BytesIO(content))
    return buf.getvalue()


def make_ar(members: list[tuple[str, bytes]]) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        hdr = f"{name + '/':<16}{1700000000:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
        assert len(hdr) == 60
        out += hdr.encode() + data
        if len(data) % 2:
            out += b"\n"
    return bytes(out)


def make_deb(
    control: dict[str, str],
    files: dict[str, bytes],
    data_comp: str = "gz",
    control_comp: str = "gz",
) -> bytes:
    ctrl_text = "".join(f"{k}: {v}\n" for k, v in control.items())
    md5s = "".join(f"{hashlib.md5(c).hexdigest()}  {p}\n" for p, c in files.items())
    ctrl_tar = make_tar({"control": ctrl_text.encode(), "md5sums": md5s.encode()})
    data_tar = make_tar(files)
    cdata, csuf = compress(ctrl_tar, control_comp)
    ddata, dsuf = compress(data_tar, data_comp)
    return make_ar(
        [
            ("debian-binary", b"2.0\n"),
            ("control.tar" + csuf, cdata),
            ("data.tar" + dsuf, ddata),
        ]
    )
