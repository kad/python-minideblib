# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy <kad@bifh.org>

import bz2
import gzip
import lzma

import pytest

from minideblib import compress
from tests.fixtures.debs import have_zstd, zstd_compress

PAYLOAD = b"hello world\n" * 50


@pytest.mark.parametrize(
    "func",
    [gzip.compress, lzma.compress, bz2.compress, lambda d: d],
)
def test_roundtrip_by_magic(func):
    assert compress.decompress(func(PAYLOAD)) == PAYLOAD


def test_suffix_ignored_when_magic_present():
    assert compress.decompress(gzip.compress(PAYLOAD), "x.xz") == PAYLOAD


def test_gzip_trailing_zero_padding():
    data = gzip.compress(PAYLOAD) + b"\0" * 10240
    assert compress.decompress(data) == PAYLOAD


def test_concatenated_gzip_members():
    data = gzip.compress(b"a") + gzip.compress(b"b")
    assert compress.decompress(data) == b"ab"


def test_lzma_alone_by_suffix():
    data = lzma.compress(PAYLOAD, format=lzma.FORMAT_ALONE)
    assert compress.decompress(data, "x.lzma") == PAYLOAD


@pytest.mark.skipif(not have_zstd(), reason="no zstd codec")
def test_zstd_roundtrip():
    assert compress.decompress(zstd_compress(PAYLOAD), "x.zst") == PAYLOAD


def test_missing_codec_names_extra(monkeypatch):
    monkeypatch.setattr(compress, "_zstd_open", lambda data: compress._no_zstd())
    with pytest.raises(compress.MissingCodecError, match="zstd"):
        compress.decompress(b"\x28\xb5\x2f\xfd" + b"junk", "x.zst")


def test_missing_codec_is_importerror():
    assert issubclass(compress.MissingCodecError, ImportError)


def test_max_size_enforced():
    bomb = gzip.compress(b"\0" * 100000)
    with pytest.raises(compress.DecompressionLimitError):
        compress.decompress(bomb, max_size=1000)


def test_corrupt_data_raises():
    with pytest.raises(compress.DecompressError):
        compress.decompress(gzip.compress(PAYLOAD)[:20])


def test_gzip_large_incompressible_is_chunked(monkeypatch):
    """Linear work: input is fed and output taken in bounded chunks (no wall clock)."""
    import os
    import zlib

    calls: list[tuple[int, int]] = []
    real = zlib.decompressobj

    class Counting:
        def __init__(self, *a):
            self._d = real(*a)

        def decompress(self, data, max_length=0):
            out = self._d.decompress(data, max_length)
            calls.append((len(data), len(out)))
            return out

        def __getattr__(self, name):
            return getattr(self._d, name)

    monkeypatch.setattr(compress.zlib, "decompressobj", Counting)
    raw = os.urandom(8 << 20)
    comp = gzip.compress(raw, compresslevel=1)
    assert compress.decompress(comp) == raw
    chunk = compress._CHUNK
    assert all(fed <= chunk and out <= chunk for fed, out in calls)
    assert len(raw) // chunk <= len(calls) <= 2 * (len(comp) // chunk + len(raw) // chunk) + 4


def test_gzip_padding_after_multiple_members():
    data = gzip.compress(b"a") + gzip.compress(b"b") + b"\0" * 5000
    assert compress.decompress(data) == b"ab"


def test_gzip_truncated_member_raises():
    with pytest.raises(compress.DecompressError):
        compress.decompress(gzip.compress(b"x" * 100000)[:-30])


@pytest.mark.skipif(not have_zstd(), reason="no zstd codec")
def test_corrupt_zstd_raises_decompress_error():
    with pytest.raises(compress.DecompressError):
        compress.decompress(b"\x28\xb5\x2f\xfd" + b"\xff" * 40, "x.zst")


@pytest.mark.parametrize("name", ["", "Packages", "x.txt"])
def test_max_size_enforced_for_plain_data(name):
    with pytest.raises(compress.DecompressionLimitError):
        compress.decompress(b"x" * 1001, name, max_size=1000)
    assert compress.decompress(b"x" * 1000, name, max_size=1000) == b"x" * 1000


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\0",
        b"\0" * 10240,
        gzip.compress(PAYLOAD)[:5],  # header only
        gzip.compress(PAYLOAD)[:10],  # complete header, no body
        gzip.compress(PAYLOAD)[:-9],  # body without the complete trailer
    ],
)
def test_gzip_without_complete_member_rejected(data):
    with pytest.raises(compress.DecompressError):
        compress.decompress(data, "x.gz")


def test_gzip_member_then_padding_still_accepted():
    data = gzip.compress(PAYLOAD) + b"\0"
    assert compress.decompress(data, "x.gz") == PAYLOAD


def _huge_dict(fmt: int) -> bytes:
    filt = lzma.FILTER_LZMA2 if fmt == lzma.FORMAT_XZ else lzma.FILTER_LZMA1
    c = lzma.LZMACompressor(format=fmt, filters=[{"id": filt, "dict_size": 1 << 30}])
    return c.compress(b"hi") + c.flush()


@pytest.mark.parametrize(("fmt", "name"), [(lzma.FORMAT_XZ, "x.xz"), (lzma.FORMAT_ALONE, "x.lzma")])
def test_lzma_huge_dictionary_hits_memlimit(fmt, name):
    with pytest.raises(compress.DecompressionLimitError, match="memory"):
        compress.decompress(_huge_dict(fmt), name, max_size=1 << 20)


def test_lzma_memlimit_bounds():
    mib = 1 << 20
    assert compress._lzma_memlimit(1000) == 128 * mib
    assert compress._lzma_memlimit(512 * mib) == 512 * mib
    assert compress._lzma_memlimit(1 << 40) == 1 << 30
    assert compress._lzma_memlimit(compress.DEFAULT_MAX_SIZE) == 1 << 30


@pytest.mark.parametrize("preset", [0, 6, 9 | lzma.PRESET_EXTREME])
@pytest.mark.parametrize(("fmt", "name"), [(lzma.FORMAT_XZ, "x.xz"), (lzma.FORMAT_ALONE, "x.lzma")])
def test_lzma_normal_presets_with_small_max_size(preset, fmt, name):
    data = lzma.compress(PAYLOAD, format=fmt, preset=preset)
    assert compress.decompress(data, name, max_size=len(PAYLOAD)) == PAYLOAD


def test_lzma_stream_handling_unchanged():
    a, b = lzma.compress(b"a"), lzma.compress(b"b")
    assert compress.decompress(a + b, "x.xz") == b"ab"
    alone = lzma.compress(b"a", format=lzma.FORMAT_ALONE)
    assert compress.decompress(alone + b"junk", "x.lzma") == b"a"  # as lzma.LZMAFile
    for bad in (b"", a[:-5], a + b"junk", a + b"\0" * 8):
        with pytest.raises(compress.DecompressError, match="cannot decompress xz data"):
            compress.decompress(bad, "x.xz")


def test_lzma_output_limit_still_enforced():
    with pytest.raises(compress.DecompressionLimitError, match="exceeds"):
        compress.decompress(lzma.compress(b"\0" * 100000), "x.xz", max_size=1000)
