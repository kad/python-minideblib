# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

"""Decompression of gzip, xz, bzip2, lzma and zstd data.

Output is bounded by *max_size* and produced in chunks. The xz/lzma decoder
runs with an lzma ``memlimit`` derived from *max_size* (see
:func:`_lzma_memlimit`: *max_size* clamped to 128 MiB .. 1 GiB), so a hostile
header cannot make it allocate a dictionary far larger than the allowed
output; a stream needing more raises :class:`DecompressionLimitError`. The
gzip and bzip2 decoders use small fixed buffers; the zstd decoder's window
memory is not limited here.
"""

from __future__ import annotations

import bz2
import importlib
import io
import logging
import lzma
import zlib
from typing import IO

from minideblib.errors import MinidebError

__all__ = [
    "DEFAULT_MAX_SIZE",
    "DecompressError",
    "DecompressionLimitError",
    "MissingCodecError",
    "decompress",
]

log = logging.getLogger(__name__)

DEFAULT_MAX_SIZE = 1 << 30  # 1 GiB
_CHUNK = 1 << 16


class MissingCodecError(MinidebError, ImportError):
    """No zstd implementation is available."""


class DecompressError(MinidebError, ValueError):
    """Data could not be decompressed."""


class DecompressionLimitError(DecompressError):
    """Decompressed output exceeds the allowed maximum size."""


def _no_zstd() -> IO[bytes]:
    raise MissingCodecError(
        "zstd support requires Python 3.14+ or the 'zstandard' package; "
        "install with: pip install 'minideblib[zstd]'"
    )


def _zstd_open(data: bytes) -> IO[bytes]:
    try:
        zstd = importlib.import_module("compression.zstd")  # Python 3.14+ only

        return zstd.ZstdFile(io.BytesIO(data))
    except ImportError:
        pass
    try:
        import zstandard
    except ImportError:
        return _no_zstd()
    return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True)


def _zstd_errors() -> tuple[type[BaseException], ...]:
    errs: list[type[BaseException]] = []
    try:
        zstd = importlib.import_module("compression.zstd")  # Python 3.14+ only

        errs.append(zstd.ZstdError)
    except ImportError:
        pass
    try:
        import zstandard

        errs.append(zstandard.ZstdError)
    except ImportError:
        pass
    return tuple(errs)


_MAGICS = (
    (b"\x1f\x8b", "gz"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"BZh", "bz2"),
    (b"\x28\xb5\x2f\xfd", "zst"),
)
_SUFFIXES = {".gz": "gz", ".xz": "xz", ".bz2": "bz2", ".lzma": "lzma", ".zst": "zst"}


def _detect(data: bytes, name: str) -> str:
    for magic, kind in _MAGICS:
        if data.startswith(magic):
            return kind
    for suffix, kind in _SUFFIXES.items():
        if name.endswith(suffix):
            return kind
    return "none"


def _check(total: int, max_size: int) -> None:
    if total > max_size:
        raise DecompressionLimitError(f"decompressed data exceeds {max_size} bytes")


def _gzip(data: bytes, max_size: int) -> bytes:
    out = bytearray()
    mv = memoryview(data)
    n = len(data)
    pos = 0
    members = 0
    while pos < n or not members:
        if members and data[pos] == 0 and data.count(0, pos) == n - pos:
            break  # zero padding after the last member
        d = zlib.decompressobj(31)
        tail = b""
        while not d.eof:
            if tail:
                feed = tail
            elif pos >= n:
                raise DecompressError("truncated compressed data")
            else:
                feed = mv[pos : pos + _CHUNK]
                pos += len(feed)
            out += d.decompress(feed, _CHUNK)
            tail = d.unconsumed_tail
            _check(len(out), max_size)
        pos -= len(d.unused_data)
        members += 1
    return bytes(out)


_LZMA_MEMLIMIT_MIN = 128 << 20  # xz -9/-9e streams (64 MiB dictionary) always decode
_LZMA_MEMLIMIT_MAX = 1 << 30


def _lzma_memlimit(max_size: int) -> int:
    """Decoder memory limit for xz/lzma data: *max_size* clamped to 128 MiB .. 1 GiB.

    A dictionary larger than the allowed output cannot be needed, so the
    limit follows *max_size*, with a floor that every standard preset fits in.
    """
    return max(_LZMA_MEMLIMIT_MIN, min(max_size, _LZMA_MEMLIMIT_MAX))


def _lzma(data: bytes, fmt: int, max_size: int) -> bytes:
    """Decode concatenated xz (or lzma-alone) streams like ``lzma.LZMAFile``, with a memlimit.

    As in ``LZMAFile``, data after a complete stream that is not a valid
    stream is ignored when the decoder rejects it outright, and input that
    ends inside a stream is an error.
    """
    memlimit = _lzma_memlimit(max_size)
    out = bytearray()
    mv = memoryview(data)
    n = len(data)
    pos = 0
    streams = 0
    while pos < n or not streams:
        d = lzma.LZMADecompressor(fmt, memlimit=memlimit)
        while not d.eof:
            if d.needs_input:
                if pos >= n:
                    raise EOFError(
                        "Compressed file ended before the end-of-stream marker was reached"
                    )
                feed: bytes | memoryview = mv[pos : pos + _CHUNK]
                pos += len(feed)
            else:
                feed = b""
            try:
                out += d.decompress(feed, _CHUNK)
            except lzma.LZMAError as exc:
                if "Memory usage limit" in str(exc):
                    raise DecompressionLimitError(
                        f"xz/lzma decoder needs more than {memlimit} bytes of memory"
                    ) from exc
                if streams:
                    return bytes(out)  # trailing garbage, ignored like LZMAFile
                raise
            _check(len(out), max_size)
        pos -= len(d.unused_data)
        streams += 1
    return bytes(out)


def _stream(fobj: IO[bytes], max_size: int) -> bytes:
    out = bytearray()
    while chunk := fobj.read(_CHUNK):
        out += chunk
        _check(len(out), max_size)
    return bytes(out)


def decompress(data: bytes, name: str = "", max_size: int = DEFAULT_MAX_SIZE) -> bytes:
    """Decompress *data*, choosing the codec by magic bytes, then by *name* suffix.

    Data with no recognised compression is returned unchanged; *max_size*
    applies to it too. gzip data must hold at least one complete member;
    zero bytes after the last member are ignored.
    """
    kind = _detect(data, name)
    out = _decompress_kind(data, kind, max_size)
    log.debug("%s: codec %s, %d -> %d bytes", name or "data", kind, len(data), len(out))
    return out


def _decompress_kind(data: bytes, kind: str, max_size: int) -> bytes:
    try:
        if kind == "none":
            _check(len(data), max_size)
            return data
        if kind == "gz":
            return _gzip(data, max_size)
        if kind == "zst":
            return _stream(_zstd_open(data), max_size)
        if kind == "xz":
            return _lzma(data, lzma.FORMAT_XZ, max_size)
        if kind == "lzma":
            return _lzma(data, lzma.FORMAT_ALONE, max_size)
        return _stream(bz2.BZ2File(io.BytesIO(data)), max_size)
    except MissingCodecError:
        raise
    except DecompressError:
        raise
    except (
        OSError,
        EOFError,
        zlib.error,
        lzma.LZMAError,
        ValueError,
        RuntimeError,
        *_zstd_errors(),
    ) as exc:
        raise DecompressError(f"cannot decompress {kind} data: {exc}") from exc
